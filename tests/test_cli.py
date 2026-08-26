import json

import pytest

from boomi_release_check import cli
from boomi_release_check.client import BoomiClient

from .fakes import FakeBoomiAPI, deployed_payload, release_payload

BASE_ARGS = [
    "--request-id",
    "release-1111",
    "--account-id",
    "master-ACCT",
    "--username",
    "BOOMI_TOKEN.user@example.com",
    "--token",
    "secret",
]


@pytest.fixture()
def install_api(monkeypatch):
    def _install(api: FakeBoomiAPI):
        def factory(**kwargs):
            kwargs.pop("transport", None)
            return BoomiClient(transport=api, sleep=lambda _s: None, **kwargs)

        monkeypatch.setattr(cli, "BoomiClient", factory)
        return api

    return _install


def test_exit_code_zero_when_every_sub_account_matches(install_api, capsys):
    install_api(
        FakeBoomiAPI(
            release=release_payload(components=[("component-a", "6.0")]),
            deployed={"sub-001": {"component-a": [deployed_payload("component-a", "6.0")]}},
        )
    )

    code = cli.main(BASE_ARGS + ["--sub-account", "sub-001"])

    assert code == cli.EXIT_OK
    assert "UP_TO_DATE" in capsys.readouterr().out


def test_exit_code_one_when_a_sub_account_is_behind(install_api, capsys):
    install_api(
        FakeBoomiAPI(
            release=release_payload(components=[("component-a", "6.0")]),
            deployed={"sub-002": {"component-a": [deployed_payload("component-a", "5.0")]}},
        )
    )

    code = cli.main(BASE_ARGS + ["--sub-account", "sub-002"])

    assert code == cli.EXIT_DRIFT
    assert "OUT_OF_DATE" in capsys.readouterr().out


def test_discover_uses_the_account_query(install_api, capsys):
    api = install_api(
        FakeBoomiAPI(
            release=release_payload(components=[("component-a", "6.0")]),
            accounts=[
                {"accountId": "sub-001", "name": "Customer One", "status": "active"},
                {"accountId": "sub-002", "name": "Customer Two", "status": "suspended"},
            ],
            deployed={"sub-001": {"component-a": [deployed_payload("component-a", "6.0")]}},
        )
    )

    code = cli.main(BASE_ARGS + ["--discover", "--format", "json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == cli.EXIT_OK
    assert [entry["accountId"] for entry in payload["accounts"]] == ["sub-001"]
    assert ("POST", "Account/query", "master-ACCT") in api.calls


def test_exclude_account_is_skipped(install_api, capsys):
    install_api(
        FakeBoomiAPI(
            release=release_payload(components=[("component-a", "6.0")]),
            deployed={
                "sub-001": {"component-a": [deployed_payload("component-a", "6.0")]},
                "sub-002": {"component-a": [deployed_payload("component-a", "5.0")]},
            },
        )
    )

    code = cli.main(
        BASE_ARGS
        + ["--sub-account", "sub-001", "--sub-account", "sub-002", "--exclude-account", "sub-002"]
    )

    assert code == cli.EXIT_OK
    assert "sub-002" not in capsys.readouterr().out


def test_no_fail_on_drift_returns_zero(install_api):
    install_api(
        FakeBoomiAPI(
            release=release_payload(components=[("component-a", "6.0")]),
            deployed={"sub-002": {"component-a": [deployed_payload("component-a", "5.0")]}},
        )
    )

    code = cli.main(BASE_ARGS + ["--sub-account", "sub-002", "--no-fail-on-drift"])

    assert code == cli.EXIT_OK


def test_missing_credentials_exit_with_error(capsys):
    code = cli.main(["--request-id", "release-1111", "--sub-account", "sub-001"])

    assert code == cli.EXIT_ERROR
    assert "Missing required options" in capsys.readouterr().err


def test_no_sub_accounts_selected_exits_with_error(install_api, capsys):
    install_api(FakeBoomiAPI(release=release_payload()))

    code = cli.main(BASE_ARGS)

    assert code == cli.EXIT_ERROR
    assert "No sub-accounts selected" in capsys.readouterr().err


def test_unfinished_release_suggests_wait(install_api, capsys):
    install_api(FakeBoomiAPI(release=release_payload(), release_pending_calls=10))

    code = cli.main(BASE_ARGS + ["--sub-account", "sub-001"])

    assert code == cli.EXIT_ERROR
    assert "--wait" in capsys.readouterr().err


def test_report_can_be_written_to_a_file(install_api, tmp_path, capsys):
    install_api(
        FakeBoomiAPI(
            release=release_payload(components=[("component-a", "6.0")]),
            deployed={"sub-001": {"component-a": [deployed_payload("component-a", "6.0")]}},
        )
    )
    destination = tmp_path / "report.csv"

    code = cli.main(BASE_ARGS + ["--sub-account", "sub-001", "--format", "csv", "--output", str(destination)])

    assert code == cli.EXIT_OK
    assert "sub-001" in destination.read_text(encoding="utf-8")
    assert "Report written to" in capsys.readouterr().err


def test_sub_accounts_file_is_read(install_api, tmp_path):
    install_api(
        FakeBoomiAPI(
            release=release_payload(components=[("component-a", "6.0")]),
            deployed={"sub-001": {"component-a": [deployed_payload("component-a", "6.0")]}},
        )
    )
    listing = tmp_path / "accounts.txt"
    listing.write_text("# OEM customers\nsub-001\n", encoding="utf-8")

    code = cli.main(BASE_ARGS + ["--sub-accounts-file", str(listing)])

    assert code == cli.EXIT_OK
