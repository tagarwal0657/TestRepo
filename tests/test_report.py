import csv
import io
import json

import pytest

from boomi_release_check.checker import ReleaseVerifier
from boomi_release_check.client import BoomiClient
from boomi_release_check.models import SubAccount
from boomi_release_check.report import render

from .fakes import FakeBoomiAPI, deployed_payload, release_payload


@pytest.fixture()
def report():
    api = FakeBoomiAPI(
        release=release_payload(components=[("component-a", "6.0")]),
        deployed={
            "sub-001": {"component-a": [deployed_payload("component-a", "6.0")]},
            "sub-002": {"component-a": [deployed_payload("component-a", "5.0")]},
            "sub-003": {},
        },
    )
    client = BoomiClient(
        account_id="master-ACCT",
        username="user",
        password="token",
        transport=api,
        sleep=lambda _s: None,
    )
    verifier = ReleaseVerifier(client, max_workers=1, sleep=lambda _s: None)
    return verifier.verify(
        "release-1111",
        [SubAccount("sub-001", "Customer One"), SubAccount("sub-002"), SubAccount("sub-003")],
    )


def test_table_lists_every_account_and_the_summary(report):
    output = render(report, "table")

    assert "release-1111" in output
    assert "sub-001" in output and "Customer One" in output
    assert "UP_TO_DATE" in output and "OUT_OF_DATE" in output and "NOT_DEPLOYED" in output
    assert "Sub-accounts checked: 3" in output


def test_detailed_table_adds_the_component_breakdown(report):
    output = render(report, "table", detailed=True)

    assert "Per-component detail" in output
    assert "BEHIND" in output


def test_json_is_machine_readable(report):
    payload = json.loads(render(report, "json"))

    assert payload["release"]["requestId"] == "release-1111"
    assert payload["summary"]["TOTAL"] == 3
    statuses = {entry["accountId"]: entry["status"] for entry in payload["accounts"]}
    assert statuses == {
        "sub-001": "UP_TO_DATE",
        "sub-002": "OUT_OF_DATE",
        "sub-003": "NOT_DEPLOYED",
    }


def test_json_only_includes_raw_deployments_when_detailed(report):
    compact = json.loads(render(report, "json"))
    detailed = json.loads(render(report, "json", detailed=True))

    assert "deployments" not in compact["accounts"][0]["checks"][0]
    assert detailed["accounts"][0]["checks"][0]["deployments"][0]["packageVersion"] == "6.0"


def test_csv_has_one_row_per_account_component(report):
    rows = list(csv.DictReader(io.StringIO(render(report, "csv"))))

    assert len(rows) == 3
    by_account = {row["accountId"]: row for row in rows}
    assert by_account["sub-002"]["deployedVersion"] == "5.0"
    assert by_account["sub-002"]["drift"] == "BEHIND"
    assert by_account["sub-001"]["componentStatus"] == "MATCH"


def test_markdown_renders_tables(report):
    output = render(report, "markdown")

    assert output.startswith("# Release verification")
    assert "| Account ID | Name | Status | Detail |" in output
    assert "**UP_TO_DATE**" in output


def test_unknown_format_raises(report):
    with pytest.raises(ValueError):
        render(report, "yaml")
