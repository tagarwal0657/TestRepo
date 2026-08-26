import json
import threading
from http.server import ThreadingHTTPServer

import pytest

from boomi_release_check.client import BoomiClient
from boomi_release_check.errors import ConfigError
from boomi_release_check.webapp import (
    HARDCODED_BASE_URL,
    make_handler,
    serve,
    split_sub_account_ids,
    verify_from_payload,
)

from .fakes import FakeBoomiAPI, deployed_payload, release_payload


def test_split_sub_account_ids_accepts_commas_and_newlines():
    assert split_sub_account_ids("alpha, bravo\ncharlie;alpha") == ["alpha", "bravo", "charlie"]


def fake_factory(api: FakeBoomiAPI):
    def factory(**kwargs):
        kwargs.setdefault("transport", api)
        kwargs.setdefault("sleep", lambda _s: None)
        return BoomiClient(**kwargs)

    return factory


def test_verify_from_payload_matches_a_sub_account():
    api = FakeBoomiAPI(
        release=release_payload(components=[("component-a", "6.0")]),
        deployed={"sub-001": {"component-a": [deployed_payload("component-a", "6.0")]}},
    )
    report = verify_from_payload(
        {
            "accountId": "master-ACCT",
            "username": "BOOMI_TOKEN.user@example.com",
            "password": "secret",
            "requestId": "release-1111",
            "subAccountId": "sub-001",
            "wait": True,
        },
        client_factory=fake_factory(api),
        sleep=lambda _s: None,
    )
    assert report.accounts[0].status == "UP_TO_DATE"
    assert report.accounts[0].checks[0].deployed_version == "6.0"


def test_verify_from_payload_rejects_missing_fields():
    with pytest.raises(ConfigError, match="Master account ID"):
        verify_from_payload({"username": "u", "password": "p"})


def test_client_supplied_base_url_is_ignored():
    api = FakeBoomiAPI(
        release=release_payload(components=[("component-a", "6.0")]),
        deployed={"sub-001": {"component-a": [deployed_payload("component-a", "6.0")]}},
    )
    captured = {}

    def factory(**kwargs):
        captured["base_url"] = kwargs.get("base_url")
        kwargs.setdefault("transport", api)
        kwargs.setdefault("sleep", lambda _s: None)
        return BoomiClient(**kwargs)

    verify_from_payload(
        {
            "accountId": "master-ACCT",
            "username": "u",
            "password": "p",
            "requestId": "release-1111",
            "subAccountId": "sub-001",
            "baseUrl": "http://attacker.example",
        },
        base_url=HARDCODED_BASE_URL,
        client_factory=factory,
        sleep=lambda _s: None,
    )
    assert captured["base_url"] == HARDCODED_BASE_URL == "https://api.boomi.com"


@pytest.fixture()
def http_server():
    api = FakeBoomiAPI(
        release=release_payload(components=[("component-a", "6.0")]),
        deployed={
            "sub-001": {"component-a": [deployed_payload("component-a", "6.0")]},
            "sub-002": {"component-a": [deployed_payload("component-a", "5.0")]},
        },
        release_pending_calls=1,
    )
    handler = make_handler(base_url=HARDCODED_BASE_URL, client_factory=fake_factory(api), sleep=lambda _s: None)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, api
    finally:
        server.shutdown()
        server.server_close()


def _request(server, method, path, body=None):
    import urllib.error
    import urllib.request

    host, port = server.server_address
    url = f"http://{host}:{port}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8")) if path.startswith("/api") else response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8")
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            parsed = payload
        return exc.code, parsed


def test_ui_page_contains_the_hardcoded_host(http_server):
    server, _api = http_server
    status, html = _request(server, "GET", "/")
    assert status == 200
    assert "https://api.boomi.com" in html
    assert "Master account ID" in html
    assert "Sub-account ID" in html
    assert "Password" in html


def test_health_reports_hardcoded_host(http_server):
    server, _api = http_server
    status, payload = _request(server, "GET", "/api/health")
    assert status == 200
    assert payload["baseUrl"] == "https://api.boomi.com"


def test_verify_endpoint_returns_account_status(http_server):
    server, _api = http_server
    status, payload = _request(
        server,
        "POST",
        "/api/verify",
        {
            "accountId": "master-ACCT",
            "username": "BOOMI_TOKEN.user@example.com",
            "password": "secret",
            "requestId": "release-1111",
            "subAccountId": "sub-001, sub-002",
            "wait": True,
        },
    )
    assert status == 200
    assert payload["upToDate"] is False
    by_id = {row["accountId"]: row["status"] for row in payload["accounts"]}
    assert by_id == {"sub-001": "UP_TO_DATE", "sub-002": "OUT_OF_DATE"}


def test_verify_endpoint_requires_fields(http_server):
    server, _api = http_server
    status, payload = _request(server, "POST", "/api/verify", {"accountId": "master"})
    assert status == 400
    assert "Missing required fields" in payload["error"]


def test_serve_uses_the_next_port_when_the_requested_one_is_busy():
    blocker = ThreadingHTTPServer(("127.0.0.1", 0), make_handler())
    occupied = blocker.server_address[1]
    thread = threading.Thread(target=blocker.serve_forever, daemon=True)
    thread.start()
    server = None
    try:
        server = serve("127.0.0.1", occupied)
        assert server.server_address[1] != occupied
        assert server.server_address[1] > occupied
    finally:
        if server is not None:
            server.server_close()
        blocker.shutdown()
        blocker.server_close()


def test_serve_raises_a_clear_error_when_no_port_is_free():
    blocker = ThreadingHTTPServer(("127.0.0.1", 0), make_handler())
    occupied = blocker.server_address[1]
    thread = threading.Thread(target=blocker.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(OSError, match="already in use"):
            serve("127.0.0.1", occupied, port_attempts=1)
    finally:
        blocker.shutdown()
        blocker.server_close()
