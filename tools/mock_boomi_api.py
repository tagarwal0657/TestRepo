#!/usr/bin/env python3
"""A small stand-in for the Boomi Platform API used to demo the release check.

It implements just enough of the real contract to exercise the tool end to end
over real HTTP: the HTTP 202 polling behaviour of ReleaseIntegrationPackStatus,
Basic auth, sub-account discovery via Account/query, queryToken paging, and a
sub-account that returns 403 so the error path is visible.

    python3 tools/mock_boomi_api.py --port 8099
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Tuple
from urllib.parse import parse_qs, urlparse

MASTER_ACCOUNT = "apptio-master-OEM"
REQUEST_ID = "release-dcfbfd2c-09d9-492d-9965-bbd9ab8f2ffc"
INTEGRATION_PACK_ID = "d7c16f5d-3311-417e-a149-3c55436f7d8d"
USERNAME = "BOOMI_TOKEN.releasebot@example.com"
TOKEN = "demo-token"

COMPONENT_ORDER_INTAKE = "bb8b6c9d-9c39-4309-b07f-cdd96d201b27"
COMPONENT_COST_EXPORT = "9d05717c-4dfe-4d5f-8e60-9196a6f78ef9"
RELEASED_VERSION = "6.0"

# Number of 202 responses returned before the release reports SUCCESS.
PENDING_POLLS = 2
PAGE_SIZE = 2

PATH_RE = re.compile(r"^/api/rest/v1/(?P<account>[^/]+)/(?P<rest>.+)$")
PARTNER_PATH_RE = re.compile(r"^/partner/api/rest/v1/(?P<account>[^/]+)/(?P<rest>.+)$")

ACCOUNTS: List[Dict[str, Any]] = [
    {"accountId": "customer-alpha-A1B2C3", "name": "Alpha Logistics", "status": "active"},
    {"accountId": "customer-bravo-D4E5F6", "name": "Bravo Manufacturing", "status": "active"},
    {"accountId": "customer-charlie-G7H8I9", "name": "Charlie Retail", "status": "active"},
    {"accountId": "customer-delta-J1K2L3", "name": "Delta Health", "status": "active"},
    {"accountId": "customer-echo-M4N5O6", "name": "Echo Financial", "status": "active"},
    {"accountId": "customer-foxtrot-P7Q8R9", "name": "Foxtrot Energy", "status": "active"},
    {"accountId": "customer-golf-S1T2U3", "name": "Golf Media (churned)", "status": "suspended"},
]

# Sub-accounts that reject the API call, to exercise the per-account error path.
FORBIDDEN_ACCOUNTS = {"customer-echo-M4N5O6"}


def deployment(
    component_id: str,
    package_version: str,
    *,
    environment_id: str,
    active: bool = True,
    deployed_date: str = "2026-08-26T09:15:00Z",
) -> Dict[str, Any]:
    return {
        "@type": "DeployedPackage",
        "deploymentId": f"deploy-{component_id[:8]}-{package_version}-{environment_id}",
        "packageId": f"package-{component_id[:8]}-{package_version}",
        "packageVersion": package_version,
        "componentId": component_id,
        "componentVersion": "2.0",
        "componentType": "process",
        "environmentId": environment_id,
        "deployedBy": "releasebot@example.com",
        "deployedDate": deployed_date,
        "active": active,
    }


DEPLOYMENTS: Dict[str, Dict[str, List[Dict[str, Any]]]] = {
    # Fully on the released version in both environments.
    "customer-alpha-A1B2C3": {
        COMPONENT_ORDER_INTAKE: [
            deployment(COMPONENT_ORDER_INTAKE, "6.0", environment_id="alpha-prod"),
            deployment(COMPONENT_ORDER_INTAKE, "6.0", environment_id="alpha-test"),
        ],
        COMPONENT_COST_EXPORT: [
            deployment(COMPONENT_COST_EXPORT, "6.0", environment_id="alpha-prod"),
        ],
    },
    # Never picked up the release.
    "customer-bravo-D4E5F6": {
        COMPONENT_ORDER_INTAKE: [
            deployment(COMPONENT_ORDER_INTAKE, "5.0", environment_id="bravo-prod"),
        ],
        COMPONENT_COST_EXPORT: [
            deployment(COMPONENT_COST_EXPORT, "5.0", environment_id="bravo-prod"),
        ],
    },
    # Half updated: one component moved, the other did not.
    "customer-charlie-G7H8I9": {
        COMPONENT_ORDER_INTAKE: [
            deployment(COMPONENT_ORDER_INTAKE, "6.0", environment_id="charlie-prod"),
        ],
        COMPONENT_COST_EXPORT: [
            deployment(COMPONENT_COST_EXPORT, "5.0", environment_id="charlie-prod"),
        ],
    },
    # Pack installed but nothing deployed.
    "customer-delta-J1K2L3": {},
    # Echo returns 403 (see FORBIDDEN_ACCOUNTS).
    "customer-echo-M4N5O6": {},
    # Deployed as "6.00" plus a retired inactive deployment; still up to date.
    "customer-foxtrot-P7Q8R9": {
        COMPONENT_ORDER_INTAKE: [
            deployment(COMPONENT_ORDER_INTAKE, "6.00", environment_id="foxtrot-prod"),
            deployment(COMPONENT_ORDER_INTAKE, "4.0", environment_id="foxtrot-old", active=False),
        ],
        COMPONENT_COST_EXPORT: [
            deployment(COMPONENT_COST_EXPORT, "6.00", environment_id="foxtrot-prod"),
        ],
    },
}

INSTANCES = {
    account: [
        {
            "@type": "IntegrationPackInstance",
            "id": f"instance-{account[:12]}",
            "integrationPackId": INTEGRATION_PACK_ID,
            "integrationPackOverrideName": "Apptio Cost Intake",
        }
    ]
    for account in DEPLOYMENTS
}


class MockState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.release_polls = 0
        self.tokens: Dict[str, Tuple[str, List[Dict[str, Any]]]] = {}
        self.token_seq = 0
        self.request_log: List[str] = []

    def next_release_response(self) -> Tuple[int, Dict[str, Any]]:
        with self.lock:
            self.release_polls += 1
            polls = self.release_polls
        if polls <= PENDING_POLLS:
            return 202, {
                "@type": "ReleaseIntegrationPackStatus",
                "responseStatusCode": 202,
                "requestId": REQUEST_ID,
                "integrationPackId": INTEGRATION_PACK_ID,
                "name": "Apptio Cost Intake",
                "releaseStatus": "IN_PROGRESS",
                "releaseProgress": str(polls * 40),
            }
        return 200, {
            "@type": "ReleaseIntegrationPackStatus",
            "responseStatusCode": 200,
            "requestId": REQUEST_ID,
            "integrationPackId": INTEGRATION_PACK_ID,
            "name": "Apptio Cost Intake",
            "installationType": "MULTI",
            "releaseSchedule": "IMMEDIATELY",
            "releaseStatus": "SUCCESS",
            "ReleasePackagedComponents": {
                "@type": "ReleasePackagedComponents",
                "ReleasePackagedComponent": [
                    {
                        "@type": "ReleasePackagedComponent",
                        "componentId": COMPONENT_ORDER_INTAKE,
                        "releasedVersion": RELEASED_VERSION,
                    },
                    {
                        "@type": "ReleasePackagedComponent",
                        "componentId": COMPONENT_COST_EXPORT,
                        "releasedVersion": RELEASED_VERSION,
                        "version": "2.0",
                    },
                ],
            },
        }

    def page(self, object_path: str, records: List[Dict[str, Any]]) -> Dict[str, Any]:
        page = records[:PAGE_SIZE]
        remaining = records[PAGE_SIZE:]
        body: Dict[str, Any] = {
            "@type": "QueryResult",
            "numberOfResults": len(page),
            "result": page,
        }
        if remaining:
            with self.lock:
                self.token_seq += 1
                token = f"queryToken-{self.token_seq}"
                self.tokens[token] = (object_path, remaining)
            body["queryToken"] = token
        return body

    def resume(self, token: str) -> Dict[str, Any]:
        with self.lock:
            entry = self.tokens.pop(token, None)
        if entry is None:
            return {"@type": "QueryResult", "numberOfResults": 0, "result": []}
        object_path, remaining = entry
        return self.page(object_path, remaining)


STATE = MockState()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        STATE.request_log.append(fmt % args)
        print(f"  mock-api  {fmt % args}", flush=True)

    # -- helpers ------------------------------------------------------
    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
        except Exception:
            return False
        return decoded == f"{USERNAME}:{TOKEN}"

    def _route(self) -> Tuple[str, str, Dict[str, List[str]]]:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        match = PATH_RE.match(parsed.path) or PARTNER_PATH_RE.match(parsed.path)
        if not match:
            return "", "", query
        account = query.get("overrideAccount", [match.group("account")])[0]
        return account, match.group("rest"), query

    def _body(self) -> str:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length).decode("utf-8") if length else ""

    # -- verbs --------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        if not self._authorized():
            return self._send(401, {"message": "Unauthorized"})
        account, rest, _ = self._route()
        if not rest:
            return self._send(404, {"message": "Not found"})
        if rest.startswith("ReleaseIntegrationPackStatus/"):
            request_id = rest.split("/", 1)[1]
            if request_id != REQUEST_ID:
                return self._send(404, {"message": f"Unknown requestId {request_id}"})
            status, payload = STATE.next_release_response()
            return self._send(status, payload)
        self._send(404, {"message": f"Unsupported GET {rest}"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            return self._send(401, {"message": "Unauthorized"})
        account, rest, _ = self._route()
        body = self._body()
        if not rest:
            return self._send(404, {"message": "Not found"})

        if rest.endswith("/queryMore"):
            return self._send(200, STATE.resume(body.strip()))

        if rest == "Account/query":
            if account != MASTER_ACCOUNT:
                return self._send(403, {"message": "Only the parent account can list sub-accounts"})
            return self._send(200, STATE.page("Account/query", list(ACCOUNTS)))

        if account in FORBIDDEN_ACCOUNTS:
            return self._send(
                403,
                {"message": f"Access denied: the parent account cannot read {account}"},
            )

        if rest == "DeployedPackage/query":
            component_id = _component_from_filter(body)
            records = DEPLOYMENTS.get(account, {}).get(component_id, [])
            active_only = '"active"' in body
            if active_only:
                records = [record for record in records if record.get("active")]
            return self._send(200, STATE.page("DeployedPackage/query", list(records)))

        if rest == "IntegrationPackInstance/query":
            return self._send(
                200, STATE.page("IntegrationPackInstance/query", list(INSTANCES.get(account, [])))
            )

        self._send(404, {"message": f"Unsupported POST {rest}"})


def _component_from_filter(body: str) -> str:
    if not body:
        return ""
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return ""
    expression = (data.get("QueryFilter") or {}).get("expression") or {}
    for entry in expression.get("nestedExpression") or [expression]:
        if entry.get("property") == "componentId":
            arguments = entry.get("argument") or []
            return arguments[0] if arguments else ""
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8099)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Mock Boomi Platform API listening on http://{args.host}:{args.port}", flush=True)
    print(f"  master account : {MASTER_ACCOUNT}", flush=True)
    print(f"  release request: {REQUEST_ID}", flush=True)
    print(f"  202 polls first: {PENDING_POLLS}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
