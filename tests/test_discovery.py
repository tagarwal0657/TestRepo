import json

import pytest

from boomi_release_check.client import BoomiClient
from boomi_release_check.discovery import load_sub_accounts_file, resolve_sub_accounts
from boomi_release_check.errors import ConfigError

from .fakes import FakeBoomiAPI


def make_client(api: FakeBoomiAPI) -> BoomiClient:
    return BoomiClient(
        account_id="master-ACCT",
        username="user",
        password="token",
        transport=api,
        sleep=lambda _s: None,
    )


def test_text_file_ignores_comments_and_blanks(tmp_path):
    path = tmp_path / "accounts.txt"
    path.write_text("# customers\n\nsub-001\nsub-002\n", encoding="utf-8")

    assert [a.account_id for a in load_sub_accounts_file(str(path))] == ["sub-001", "sub-002"]


def test_csv_file_reads_id_and_name(tmp_path):
    path = tmp_path / "accounts.csv"
    path.write_text("accountId,name\nsub-001,Customer One\nsub-002,Customer Two\n", encoding="utf-8")

    accounts = load_sub_accounts_file(str(path))

    assert [(a.account_id, a.name) for a in accounts] == [
        ("sub-001", "Customer One"),
        ("sub-002", "Customer Two"),
    ]


def test_json_file_accepts_strings_and_objects(tmp_path):
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps(["sub-001", {"accountId": "sub-002", "name": "Two"}]), encoding="utf-8")

    accounts = load_sub_accounts_file(str(path))

    assert [(a.account_id, a.name) for a in accounts] == [("sub-001", None), ("sub-002", "Two")]


def test_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_sub_accounts_file(str(tmp_path / "nope.txt"))


def test_discovery_drops_the_parent_and_inactive_accounts():
    api = FakeBoomiAPI(
        accounts=[
            {"accountId": "master-ACCT", "name": "Master", "status": "active"},
            {"accountId": "sub-001", "name": "One", "status": "active"},
            {"accountId": "sub-002", "name": "Two", "status": "suspended"},
            {"accountId": "sub-003", "name": "Three", "status": "trial"},
        ]
    )

    accounts = resolve_sub_accounts(make_client(api), discover=True)

    assert [a.account_id for a in accounts] == ["sub-001", "sub-003"]


def test_include_inactive_keeps_suspended_accounts():
    api = FakeBoomiAPI(
        accounts=[
            {"accountId": "sub-001", "status": "active"},
            {"accountId": "sub-002", "status": "suspended"},
        ]
    )

    accounts = resolve_sub_accounts(make_client(api), discover=True, only_active=False)

    assert [a.account_id for a in accounts] == ["sub-001", "sub-002"]


def test_sources_are_merged_and_deduplicated(tmp_path):
    path = tmp_path / "accounts.txt"
    path.write_text("sub-001\nsub-004\n", encoding="utf-8")
    api = FakeBoomiAPI(accounts=[{"accountId": "sub-001", "status": "active"}])

    accounts = resolve_sub_accounts(
        make_client(api),
        explicit=["sub-001", "sub-002"],
        file_path=str(path),
        discover=True,
        exclude=["sub-002"],
    )

    assert [a.account_id for a in accounts] == ["sub-001", "sub-004"]


def test_no_source_raises():
    with pytest.raises(ConfigError):
        resolve_sub_accounts(make_client(FakeBoomiAPI()))
