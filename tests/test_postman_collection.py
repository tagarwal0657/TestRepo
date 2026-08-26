"""The Postman collection and curl script must stay aligned with the client."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from boomi_release_check.client import BoomiClient

ROOT = Path(__file__).resolve().parents[1]
COLLECTION_PATH = ROOT / "postman" / "boomi-release-verification.postman_collection.json"
MOCK_ENV_PATH = ROOT / "postman" / "boomi-mock.postman_environment.json"
LIVE_ENV_PATH = ROOT / "postman" / "boomi-platform.postman_environment.json"
CURL_SCRIPT = ROOT / "examples" / "verify-with-curl.sh"

REQUIRED_SNIPPETS = (
    "ReleaseIntegrationPackStatus",
    "Account/query",
    "Account/queryMore",
    "DeployedPackage/query",
    "IntegrationPackInstance/query",
    "overrideAccount",
    "text/plain",
)


def _walk_requests(items):
    for item in items:
        if "item" in item:
            yield from _walk_requests(item["item"])
        elif "request" in item:
            yield item


def _request_url(request) -> str:
    url = request.get("url")
    if isinstance(url, dict):
        return str(url.get("raw") or "")
    return str(url or "")


@pytest.fixture(scope="module")
def collection():
    return json.loads(COLLECTION_PATH.read_text(encoding="utf-8"))


def test_collection_and_environments_are_valid_json(collection):
    assert collection["info"]["name"] == "Boomi Release Verification"
    assert collection["auth"]["type"] == "basic"
    mock_env = json.loads(MOCK_ENV_PATH.read_text(encoding="utf-8"))
    live_env = json.loads(LIVE_ENV_PATH.read_text(encoding="utf-8"))
    assert mock_env["name"] == "Boomi Mock (local)"
    assert live_env["name"] == "Boomi Platform (live)"
    keys = {entry["key"] for entry in mock_env["values"]}
    assert {"baseUrl", "masterAccountId", "username", "apiToken", "requestId"} <= keys


def test_collection_covers_the_verification_flow(collection):
    requests = list(_walk_requests(collection["item"]))
    blob = json.dumps(collection)
    for snippet in REQUIRED_SNIPPETS:
        assert snippet in blob, snippet
    methods_and_urls = [((item["request"]["method"]), _request_url(item["request"])) for item in requests]
    assert any(method == "GET" and "ReleaseIntegrationPackStatus" in url for method, url in methods_and_urls)
    assert any(method == "POST" and url.endswith("Account/query") for method, url in methods_and_urls)
    query_more = next(item for item in requests if _request_url(item["request"]).endswith("Account/queryMore"))
    headers = {header["key"]: header["value"] for header in query_more["request"]["header"]}
    assert headers["Content-Type"] == "text/plain"
    assert query_more["request"]["body"]["raw"] == "{{queryToken}}"


def test_collection_urls_match_the_python_client(collection):
    client = BoomiClient(
        account_id="apptio-master-OEM",
        username="user",
        password="token",
        base_url="https://api.boomi.com",
    )
    partner = BoomiClient(
        account_id="apptio-master-OEM",
        username="user",
        password="token",
        base_url="https://api.boomi.com",
        partner_api=True,
    )
    platform_release = client.build_url("ReleaseIntegrationPackStatus/req-1")
    assert platform_release.endswith("/api/rest/v1/apptio-master-OEM/ReleaseIntegrationPackStatus/req-1")
    partner_deployed = partner.build_url("DeployedPackage/query", account="customer-alpha-A1B2C3")
    assert "overrideAccount=customer-alpha-A1B2C3" in partner_deployed
    assert "/partner/api/rest/v1/apptio-master-OEM/DeployedPackage/query" in partner_deployed

    blob = json.dumps(collection)
    assert "{{baseUrl}}/api/rest/v1/{{masterAccountId}}/ReleaseIntegrationPackStatus/{{requestId}}" in blob
    assert "{{baseUrl}}/api/rest/v1/{{subAccountId}}/DeployedPackage/query" in blob
    assert "{{baseUrl}}/api/rest/v1/customer-bravo-D4E5F6/DeployedPackage/query" in blob
    assert "{{baseUrl}}/api/rest/v1/customer-echo-M4N5O6/DeployedPackage/query" in blob
    assert "overrideAccount=customer-alpha-A1B2C3" in blob


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_port(port: int, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    raise RuntimeError(f"mock API did not listen on {port}")


def test_curl_script_walks_the_mock_api():
    port = _free_port()
    env = os.environ.copy()
    for name in (
        "BOOMI_ACCOUNT_ID",
        "BOOMI_USERNAME",
        "BOOMI_API_TOKEN",
        "BOOMI_PASSWORD",
        "BOOMI_BASE_URL",
        "BOOMI_RELEASE_REQUEST_ID",
    ):
        env.pop(name, None)
    env["BOOMI_BASE_URL"] = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "tools" / "mock_boomi_api.py"), "--port", str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_for_port(port)
        result = subprocess.run(
            ["bash", str(CURL_SCRIPT)],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "ReleaseIntegrationPackStatus SUCCESS" in result.stdout
        assert "Account/queryMore" in result.stdout
        assert "Partner API overrideAccount" in result.stdout
        assert "Echo Order Intake (ERROR)" in result.stdout
        assert "failed" in result.stdout.splitlines()[-1]
        assert result.stdout.strip().endswith("0 failed")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
