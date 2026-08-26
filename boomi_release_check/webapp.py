"""Local HTML UI for verifying a release against one or more sub-accounts.

The browser cannot call api.boomi.com directly (CORS), so this process both
serves the page and runs the same verification the CLI uses.

The Platform API host is hardcoded to ``https://api.boomi.com``. Pass
``--base-url`` only when pointing at the mock API.

    python3 -m boomi_release_check.webapp
    python3 -m boomi_release_check.webapp --port 8765 --base-url http://127.0.0.1:8099
"""

from __future__ import annotations

import argparse
import errno
import json
import logging
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional
from urllib.parse import urlparse

from .checker import ReleaseVerifier
from .client import DEFAULT_BASE_URL, BoomiClient
from .errors import BoomiError, ConfigError, ReleaseNotReady
from .models import SubAccount, VerificationReport

LOGGER = logging.getLogger("boomi_release_check.webapp")

HARDCODED_BASE_URL = DEFAULT_BASE_URL  # https://api.boomi.com
STATIC_DIR = Path(__file__).resolve().parent / "static"

REQUIRED_FIELDS = (
    ("accountId", "account_id", "Master account ID"),
    ("username", "username", "Username"),
    ("password", "password", "Password"),
    ("requestId", "request_id", "Release request ID"),
    ("subAccountId", "sub_account_id", "Sub-account ID"),
)


def split_sub_account_ids(raw: str) -> List[str]:
    """Split a form value into unique sub-account IDs.

    Accepts commas, semicolons, whitespace and newlines as separators.
    """
    if not raw:
        return []
    tokens: List[str] = []
    for chunk in raw.replace(";", ",").replace("\n", ",").replace("\r", ",").split(","):
        for token in chunk.split():
            value = token.strip()
            if value:
                tokens.append(value)
    seen = set()
    unique: List[str] = []
    for token in tokens:
        if token in seen:
            continue
        seen.add(token)
        unique.append(token)
    return unique


def _field(data: Mapping[str, Any], *names: str) -> str:
    for name in names:
        value = data.get(name)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _bool(data: Mapping[str, Any], *names: str, default: bool = False) -> bool:
    for name in names:
        if name not in data:
            continue
        value = data[name]
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().casefold() in {"1", "true", "yes", "on"}
        return bool(value)
    return default


def verify_from_payload(
    data: Mapping[str, Any],
    *,
    base_url: str = HARDCODED_BASE_URL,
    client_factory: Callable[..., BoomiClient] = BoomiClient,
    sleep: Callable[[float], None] = time.sleep,
) -> VerificationReport:
    """Run a verification from a JSON form payload.

    ``baseUrl`` in the payload is ignored so a browser cannot redirect the
    local server at an arbitrary host.
    """
    missing = [label for camel, snake, label in REQUIRED_FIELDS if not _field(data, camel, snake)]
    if missing:
        raise ConfigError("Missing required fields: " + ", ".join(missing))

    account_id = _field(data, "accountId", "account_id")
    username = _field(data, "username")
    password = _field(data, "password", "token", "apiToken")
    request_id = _field(data, "requestId", "request_id")
    sub_ids = split_sub_account_ids(_field(data, "subAccountId", "sub_account_id"))
    if not sub_ids:
        raise ConfigError("Missing required fields: Sub-account ID")

    wait = _bool(data, "wait", default=True)
    check_instances = _bool(data, "checkInstances", "check_instances", default=False)
    partner_api = _bool(data, "partnerApi", "partner_api", default=False)
    poll_interval = 2.0 if wait else 15.0

    client = client_factory(
        account_id=account_id,
        username=username,
        password=password,
        base_url=base_url,
        partner_api=partner_api,
        sleep=sleep,
    )
    verifier = ReleaseVerifier(
        client,
        check_instances=check_instances,
        poll_interval=poll_interval,
        poll_timeout=900.0,
        sleep=sleep,
    )
    accounts = [SubAccount(account_id=value) for value in sub_ids]
    return verifier.verify(request_id, accounts, wait=wait)


def report_response(report: VerificationReport) -> Dict[str, Any]:
    payload = report.to_dict()
    payload["ok"] = not report.has_errors
    payload["upToDate"] = not report.has_drift
    return payload


class ReleaseCheckHandler(BaseHTTPRequestHandler):
    """Serves the UI and ``POST /api/verify``."""

    server_version = "boomi-release-check-ui"
    base_url = HARDCODED_BASE_URL
    client_factory: Callable[..., BoomiClient] = staticmethod(BoomiClient)
    sleep: Callable[[float], None] = staticmethod(time.sleep)

    def log_message(self, fmt: str, *args: Any) -> None:
        LOGGER.info("%s - " + fmt, self.address_string(), *args)

    def _send(self, status: int, payload: Any, content_type: str = "application/json") -> None:
        if isinstance(payload, (dict, list)):
            body = json.dumps(payload).encode("utf-8")
        elif isinstance(payload, bytes):
            body = payload
        else:
            body = str(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConfigError(f"Request body is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ConfigError("Request body must be a JSON object")
        return payload

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path in {"/", "/index.html"}:
            html_path = STATIC_DIR / "index.html"
            if not html_path.exists():
                return self._send(500, {"error": f"UI file missing: {html_path}"})
            return self._send(200, html_path.read_bytes(), "text/html; charset=utf-8")
        if path == "/api/health":
            return self._send(
                200,
                {"ok": True, "baseUrl": self.base_url, "hardcodedBaseUrl": HARDCODED_BASE_URL},
            )
        self._send(404, {"error": "Not found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path != "/api/verify":
            return self._send(404, {"error": "Not found"})
        try:
            data = self._read_json()
            report = verify_from_payload(
                data,
                base_url=self.base_url,
                client_factory=self.client_factory,
                sleep=self.sleep,
            )
        except ConfigError as exc:
            return self._send(400, {"ok": False, "error": str(exc)})
        except ReleaseNotReady as exc:
            return self._send(
                409,
                {
                    "ok": False,
                    "error": str(exc),
                    "releaseStatus": exc.release_status,
                    "progress": exc.progress,
                },
            )
        except BoomiError as exc:
            return self._send(502, {"ok": False, "error": str(exc)})
        except Exception as exc:  # pragma: no cover - unexpected
            LOGGER.exception("Verification failed")
            return self._send(500, {"ok": False, "error": str(exc)})
        return self._send(200, report_response(report))


def make_handler(
    *,
    base_url: str = HARDCODED_BASE_URL,
    client_factory: Callable[..., BoomiClient] = BoomiClient,
    sleep: Callable[[float], None] = time.sleep,
) -> type[ReleaseCheckHandler]:
    class BoundHandler(ReleaseCheckHandler):
        pass

    BoundHandler.base_url = base_url
    BoundHandler.client_factory = staticmethod(client_factory)
    BoundHandler.sleep = staticmethod(sleep)
    return BoundHandler


class _ReuseThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


def serve(
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    base_url: str = HARDCODED_BASE_URL,
    port_attempts: int = 20,
) -> ThreadingHTTPServer:
    """Bind the UI server, skipping to the next port if the requested one is taken."""
    handler = make_handler(base_url=base_url)
    last_error: Optional[OSError] = None
    for offset in range(max(1, port_attempts)):
        candidate = port + offset
        try:
            return _ReuseThreadingHTTPServer((host, candidate), handler)
        except OSError as exc:
            if getattr(exc, "errno", None) not in {errno.EADDRINUSE, errno.EACCES}:
                raise
            last_error = exc
    last = port + max(1, port_attempts) - 1
    raise OSError(
        f"Port {port} is already in use (tried {port}-{last}). "
        f"The UI may already be running at http://{host}:{port} — open that URL, "
        f"or pass --port with a free port."
    ) from last_error


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="Bind address")
    parser.add_argument("--port", type=int, default=8765, help="Bind port")
    parser.add_argument(
        "--base-url",
        default=HARDCODED_BASE_URL,
        help="Platform API host (default: the hardcoded US host). Use the mock here.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        server = serve(args.host, args.port, base_url=args.base_url)
    except OSError as exc:
        print(f"error: {exc}", flush=True)
        return 1
    bound_host, bound_port = server.server_address[:2]
    if bound_port != args.port:
        print(
            f"Port {args.port} is already in use; serving on {bound_port} instead.",
            flush=True,
        )
    print(f"Release verification UI: http://{bound_host}:{bound_port}", flush=True)
    print(f"  Platform API: {args.base_url}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
