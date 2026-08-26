"""In-memory Boomi Platform API used by the unit tests."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from boomi_release_check.client import HttpResponse, Transport

PATH_RE = re.compile(r"/(?:partner/)?api/rest/v1/(?P<account>[^/]+)/(?P<rest>.+)")


class FakeBoomiAPI(Transport):
    """Serves canned ReleaseIntegrationPackStatus / DeployedPackage responses.

    ``deployed`` maps ``accountId -> componentId -> [DeployedPackage payloads]``.
    """

    def __init__(
        self,
        *,
        release: Optional[Mapping[str, Any]] = None,
        release_pending_calls: int = 0,
        deployed: Optional[Mapping[str, Mapping[str, List[Dict[str, Any]]]]] = None,
        instances: Optional[Mapping[str, List[Dict[str, Any]]]] = None,
        accounts: Optional[List[Dict[str, Any]]] = None,
        page_size: int = 100,
        failures: Optional[Mapping[str, List[int]]] = None,
    ) -> None:
        self.release = dict(release or {})
        self.release_pending_calls = release_pending_calls
        self.deployed = {a: dict(c) for a, c in (deployed or {}).items()}
        self.instances = {a: list(v) for a, v in (instances or {}).items()}
        self.accounts = list(accounts or [])
        self.page_size = page_size
        self.failures = {k: list(v) for k, v in (failures or {}).items()}
        self.calls: List[Tuple[str, str, Optional[str]]] = []
        self._release_calls = 0
        self._tokens: Dict[str, Tuple[str, str, List[Dict[str, Any]]]] = {}
        self._token_seq = 0

    # -- Transport ----------------------------------------------------
    def request(self, method, url, headers, body, timeout) -> HttpResponse:  # noqa: D102
        parsed = urlparse(url)
        match = PATH_RE.match(parsed.path)
        assert match, f"unexpected URL: {url}"
        path_account = match.group("account")
        rest = match.group("rest")
        override = parse_qs(parsed.query).get("overrideAccount", [None])[0]
        effective_account = override or path_account
        payload = body.decode("utf-8") if body else None
        self.calls.append((method, rest, effective_account))

        queued = self.failures.get(rest)
        if queued:
            return HttpResponse(status=queued.pop(0), body=b'{"message":"transient"}')

        if rest.startswith("ReleaseIntegrationPackStatus/"):
            return self._release_response()
        if rest == "Account/query":
            return self._page("Account/query", effective_account, self.accounts)
        if rest == "DeployedPackage/query":
            component_id = _component_from_filter(payload)
            records = self.deployed.get(effective_account, {}).get(component_id, [])
            return self._page("DeployedPackage/query", effective_account, list(records))
        if rest == "IntegrationPackInstance/query":
            return self._page(
                "IntegrationPackInstance/query",
                effective_account,
                list(self.instances.get(effective_account, [])),
            )
        if rest.endswith("/queryMore"):
            token = (payload or "").strip()
            assert token in self._tokens, f"unknown queryToken: {token!r}"
            object_path, account, remaining = self._tokens.pop(token)
            return self._page(object_path, account, remaining)
        return HttpResponse(status=404, body=b'{"message":"not found"}')

    # -- helpers ------------------------------------------------------
    def _release_response(self) -> HttpResponse:
        self._release_calls += 1
        if self._release_calls <= self.release_pending_calls:
            pending = {
                "releaseStatus": "IN_PROGRESS",
                "releaseProgress": str(min(99, 30 * self._release_calls)),
                "requestId": self.release.get("requestId", ""),
            }
            return HttpResponse(status=202, body=json.dumps(pending).encode("utf-8"))
        return HttpResponse(status=200, body=json.dumps(self.release).encode("utf-8"))

    def _page(self, object_path: str, account: str, records: List[Dict[str, Any]]) -> HttpResponse:
        page = records[: self.page_size]
        remaining = records[self.page_size :]
        body: Dict[str, Any] = {"numberOfResults": len(page), "result": page}
        if remaining:
            self._token_seq += 1
            token = f"token-{self._token_seq}"
            self._tokens[token] = (object_path, account, remaining)
            body["queryToken"] = token
        return HttpResponse(status=200, body=json.dumps(body).encode("utf-8"))

    def accounts_called(self) -> List[str]:
        return [account for _, rest, account in self.calls if rest == "DeployedPackage/query"]


def _component_from_filter(payload: Optional[str]) -> str:
    if not payload:
        return ""
    data = json.loads(payload)
    expression = (data.get("QueryFilter") or {}).get("expression") or {}
    candidates = expression.get("nestedExpression") or [expression]
    for entry in candidates:
        if entry.get("property") == "componentId":
            arguments = entry.get("argument") or []
            return arguments[0] if arguments else ""
    return ""


def release_payload(
    *,
    request_id: str = "release-1111",
    integration_pack_id: str = "ipack-9999",
    name: str = "Apptio OEM Pack",
    status: str = "SUCCESS",
    components: Optional[List[Tuple[str, str]]] = None,
) -> Dict[str, Any]:
    entries = components or [("component-a", "6.0")]
    return {
        "@type": "ReleaseIntegrationPackStatus",
        "responseStatusCode": 200,
        "requestId": request_id,
        "integrationPackId": integration_pack_id,
        "name": name,
        "installationType": "MULTI",
        "releaseSchedule": "IMMEDIATELY",
        "releaseStatus": status,
        "ReleasePackagedComponents": {
            "@type": "ReleasePackagedComponents",
            "ReleasePackagedComponent": [
                {"@type": "ReleasePackagedComponent", "componentId": cid, "releasedVersion": version}
                for cid, version in entries
            ],
        },
    }


def deployed_payload(
    component_id: str,
    package_version: str,
    *,
    active: bool = True,
    environment_id: str = "env-1",
    component_version: str = "2.0",
    deployed_date: str = "2026-08-20T10:00:00Z",
) -> Dict[str, Any]:
    return {
        "@type": "DeployedPackage",
        "deploymentId": f"deploy-{component_id}-{package_version}",
        "packageId": f"package-{component_id}",
        "packageVersion": package_version,
        "componentId": component_id,
        "componentVersion": component_version,
        "componentType": "process",
        "environmentId": environment_id,
        "active": active,
        "deployedDate": deployed_date,
    }
