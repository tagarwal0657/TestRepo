"""Minimal Boomi Platform API client used by the release verification tool.

The client only implements what the verification flow needs:

* ``GET /ReleaseIntegrationPackStatus/{requestId}`` including its HTTP 202
  "still running" contract.
* ``POST /{object}/query`` plus ``POST /{object}/queryMore`` paging.
* Running a request against a sub-account, either by placing the sub-account in
  the URL path (Platform API) or via ``?overrideAccount=`` (Partner API).

Only the standard library is used so the tool runs on any Boomi/CI host without
installing dependencies. HTTP is isolated behind :class:`Transport` so tests can
drive the client without a network.
"""

from __future__ import annotations

import base64
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Tuple

from .errors import BoomiHTTPError, ConfigError
from .models import as_list

LOGGER = logging.getLogger("boomi_release_check.client")

# Documented Boomi Platform API hosts. Other regions can be passed with --base-url.
REGIONS = {
    "us": "https://api.boomi.com",
    "gb": "https://api.platform.gb.boomi.com",
}
DEFAULT_BASE_URL = REGIONS["us"]

RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_PAGE_SIZE = 100


@dataclass
class HttpResponse:
    status: int
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        if not self.body:
            return None
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


class Transport:
    """HTTP seam so the client can be exercised without network access."""

    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: Optional[bytes],
        timeout: float,
    ) -> HttpResponse:  # pragma: no cover - interface definition
        raise NotImplementedError


class UrllibTransport(Transport):
    """Default transport built on :mod:`urllib.request`."""

    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: Optional[bytes],
        timeout: float,
    ) -> HttpResponse:
        request = urllib.request.Request(url=url, data=body, method=method)
        for key, value in headers.items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return HttpResponse(
                    status=response.status,
                    body=response.read(),
                    headers={k.lower(): v for k, v in response.headers.items()},
                )
        except urllib.error.HTTPError as exc:  # 4xx/5xx are responses, not failures
            return HttpResponse(
                status=exc.code,
                body=exc.read(),
                headers={k.lower(): v for k, v in (exc.headers or {}).items()},
            )


class BoomiClient:
    """Authenticated access to one Boomi Platform (or Partner) API account."""

    def __init__(
        self,
        account_id: str,
        username: str,
        password: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        partner_api: bool = False,
        timeout: float = 60.0,
        max_retries: int = 4,
        backoff_factor: float = 2.0,
        transport: Optional[Transport] = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not account_id:
            raise ConfigError("A Boomi account ID is required")
        if not username or not password:
            raise ConfigError("Boomi API credentials (username and token) are required")
        self.account_id = account_id
        self.base_url = base_url.rstrip("/")
        self.partner_api = partner_api
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self._transport = transport or UrllibTransport()
        self._sleep = sleep
        credentials = f"{username}:{password}".encode("utf-8")
        self._auth_header = "Basic " + base64.b64encode(credentials).decode("ascii")

    # ------------------------------------------------------------------
    # URL construction
    # ------------------------------------------------------------------
    def build_url(self, path: str, account: Optional[str] = None) -> str:
        """Return the absolute URL for ``path``, targeting ``account`` when given.

        Partner API callers keep their own account in the path and select the
        sub-account with ``?overrideAccount=``. Platform API callers address the
        sub-account directly in the path segment.
        """
        path = path.lstrip("/")
        target = account or self.account_id
        if self.partner_api:
            url = f"{self.base_url}/partner/api/rest/v1/{urllib.parse.quote(self.account_id)}/{path}"
            if account and account != self.account_id:
                url += "?" + urllib.parse.urlencode({"overrideAccount": account})
            return url
        return f"{self.base_url}/api/rest/v1/{urllib.parse.quote(target)}/{path}"

    # ------------------------------------------------------------------
    # Request plumbing
    # ------------------------------------------------------------------
    def request(
        self,
        method: str,
        path: str,
        *,
        account: Optional[str] = None,
        json_body: Optional[Any] = None,
        text_body: Optional[str] = None,
        expected: Iterable[int] = (200,),
    ) -> HttpResponse:
        url = self.build_url(path, account)
        headers: Dict[str, str] = {
            "Authorization": self._auth_header,
            "Accept": "application/json",
        }
        body: Optional[bytes] = None
        if json_body is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(json_body).encode("utf-8")
        elif text_body is not None:
            # queryMore tokens must be posted verbatim as text/plain.
            headers["Content-Type"] = "text/plain"
            body = text_body.encode("utf-8")

        expected_statuses = set(expected)
        attempt = 0
        while True:
            attempt += 1
            try:
                response = self._transport.request(method, url, headers, body, self.timeout)
            except Exception as exc:  # network-level failure
                if attempt > self.max_retries:
                    raise BoomiHTTPError(0, method, url, f"transport error: {exc}") from exc
                delay = self._retry_delay(attempt, None)
                LOGGER.warning(
                    "%s %s failed (%s); retrying in %.1fs (attempt %d/%d)",
                    method, url, exc, delay, attempt, self.max_retries,
                )
                self._sleep(delay)
                continue

            if response.status in expected_statuses:
                return response
            if response.status in RETRYABLE_STATUSES and attempt <= self.max_retries:
                delay = self._retry_delay(attempt, response.headers.get("retry-after"))
                LOGGER.warning(
                    "%s %s returned HTTP %d; retrying in %.1fs (attempt %d/%d)",
                    method, url, response.status, delay, attempt, self.max_retries,
                )
                self._sleep(delay)
                continue
            raise BoomiHTTPError(response.status, method, url, response.text())

    def _retry_delay(self, attempt: int, retry_after: Optional[str]) -> float:
        if retry_after:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                pass
        return self.backoff_factor ** (attempt - 1)

    # ------------------------------------------------------------------
    # Boomi objects
    # ------------------------------------------------------------------
    def get_release_status_raw(self, request_id: str) -> Tuple[int, Optional[Mapping[str, Any]]]:
        """Return ``(status_code, payload)`` for a release request.

        Boomi answers 202 while the release is IN_PROGRESS or SCHEDULED and 200
        once the released details are available, so the status code is part of
        the contract and is returned to the caller rather than swallowed.
        """
        if not request_id:
            raise ConfigError("A release requestId is required")
        response = self.request(
            "GET",
            f"ReleaseIntegrationPackStatus/{urllib.parse.quote(request_id, safe='')}",
            expected=(200, 202),
        )
        payload = response.json()
        return response.status, payload if isinstance(payload, Mapping) else None

    def query(
        self,
        object_type: str,
        query_filter: Optional[Mapping[str, Any]] = None,
        *,
        account: Optional[str] = None,
        max_results: Optional[int] = None,
    ) -> Iterator[Dict[str, Any]]:
        """Yield every result of a Boomi QUERY, following ``queryToken`` paging."""
        payload: Dict[str, Any] = dict(query_filter) if query_filter else {"QueryFilter": {}}
        response = self.request("POST", f"{object_type}/query", account=account, json_body=payload)
        emitted = 0
        while True:
            body = response.json() or {}
            for entry in as_list(body.get("result")):
                if not isinstance(entry, Mapping):
                    continue
                yield dict(entry)
                emitted += 1
                if max_results is not None and emitted >= max_results:
                    return
            token = body.get("queryToken")
            if not token:
                return
            response = self.request(
                "POST",
                f"{object_type}/queryMore",
                account=account,
                text_body=str(token),
            )

    def query_deployed_packages(
        self,
        component_id: str,
        *,
        account: Optional[str] = None,
        active_only: bool = True,
    ) -> List[Dict[str, Any]]:
        """Query DeployedPackage for one component inside ``account``."""
        expressions: List[Dict[str, Any]] = [
            {"property": "componentId", "operator": "EQUALS", "argument": [component_id]}
        ]
        if active_only:
            expressions.append(
                {"property": "active", "operator": "EQUALS", "argument": ["true"]}
            )
        if len(expressions) == 1:
            query_filter = {"QueryFilter": {"expression": expressions[0]}}
        else:
            query_filter = {
                "QueryFilter": {
                    "expression": {"operator": "and", "nestedExpression": expressions}
                }
            }
        return list(self.query("DeployedPackage", query_filter, account=account))

    def query_integration_pack_instances(
        self,
        integration_pack_id: str,
        *,
        account: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Query IntegrationPackInstance to confirm the pack is installed."""
        query_filter = {
            "QueryFilter": {
                "expression": {
                    "property": "integrationPackId",
                    "operator": "EQUALS",
                    "argument": [integration_pack_id],
                }
            }
        }
        return list(self.query("IntegrationPackInstance", query_filter, account=account))

    def query_accounts(self, exclude_deleted: bool = True) -> List[Dict[str, Any]]:
        """List the sub-accounts created by the authenticated parent account."""
        query_filter: Dict[str, Any]
        if exclude_deleted:
            query_filter = {
                "QueryFilter": {
                    "expression": {
                        "property": "status",
                        "operator": "NOT_EQUALS",
                        "argument": ["deleted"],
                    }
                }
            }
        else:
            query_filter = {"QueryFilter": {}}
        return list(self.query("Account", query_filter))
