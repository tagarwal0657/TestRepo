import pytest

from boomi_release_check.checker import ReleaseVerifier
from boomi_release_check.client import BoomiClient
from boomi_release_check.errors import ReleaseNotReady
from boomi_release_check.models import AccountStatus, CheckStatus, SubAccount

from .fakes import FakeBoomiAPI, deployed_payload, release_payload

RELEASED = [("component-a", "6.0"), ("component-b", "6.0")]


def build(api: FakeBoomiAPI, **kwargs) -> ReleaseVerifier:
    client = BoomiClient(
        account_id="master-ACCT",
        username="BOOMI_TOKEN.user@example.com",
        password="secret-token",
        transport=api,
        sleep=lambda _seconds: None,
    )
    kwargs.setdefault("sleep", lambda _seconds: None)
    kwargs.setdefault("max_workers", 1)
    return ReleaseVerifier(client, **kwargs)


def accounts(*ids):
    return [SubAccount(account_id=value) for value in ids]


def test_sub_account_on_the_released_version_is_up_to_date():
    api = FakeBoomiAPI(
        release=release_payload(components=RELEASED),
        deployed={
            "sub-001": {
                "component-a": [deployed_payload("component-a", "6.0")],
                "component-b": [deployed_payload("component-b", "6.0")],
            }
        },
    )
    report = build(api).verify("release-1111", accounts("sub-001"))

    result = report.accounts[0]
    assert result.status == AccountStatus.UP_TO_DATE
    assert [check.status for check in result.checks] == [CheckStatus.MATCH, CheckStatus.MATCH]
    assert report.has_drift is False


def test_sub_account_on_an_older_version_reports_behind():
    api = FakeBoomiAPI(
        release=release_payload(components=RELEASED),
        deployed={
            "sub-002": {
                "component-a": [deployed_payload("component-a", "5.0")],
                "component-b": [deployed_payload("component-b", "5.0")],
            }
        },
    )
    report = build(api).verify("release-1111", accounts("sub-002"))

    result = report.accounts[0]
    assert result.status == AccountStatus.OUT_OF_DATE
    assert result.checks[0].deployed_version == "5.0"
    assert result.checks[0].drift == "BEHIND"
    assert report.has_drift is True


def test_partially_updated_sub_account_is_flagged_partial():
    api = FakeBoomiAPI(
        release=release_payload(components=RELEASED),
        deployed={
            "sub-003": {
                "component-a": [deployed_payload("component-a", "6.0")],
                "component-b": [deployed_payload("component-b", "5.0")],
            }
        },
    )
    report = build(api).verify("release-1111", accounts("sub-003"))

    assert report.accounts[0].status == AccountStatus.PARTIAL


def test_missing_deployment_is_not_deployed():
    api = FakeBoomiAPI(release=release_payload(components=RELEASED), deployed={"sub-004": {}})
    report = build(api).verify("release-1111", accounts("sub-004"))

    result = report.accounts[0]
    assert result.status == AccountStatus.NOT_DEPLOYED
    assert all(check.status == CheckStatus.NOT_DEPLOYED for check in result.checks)


def test_inactive_deployments_are_ignored_by_default():
    api = FakeBoomiAPI(
        release=release_payload(components=[("component-a", "6.0")]),
        deployed={
            "sub-005": {
                "component-a": [
                    deployed_payload("component-a", "6.0", active=False),
                    deployed_payload("component-a", "5.0", active=True),
                ]
            }
        },
    )
    report = build(api).verify("release-1111", accounts("sub-005"))

    check = report.accounts[0].checks[0]
    assert check.status == CheckStatus.MISMATCH
    assert check.deployed_version == "5.0"


def test_multi_environment_account_matches_when_any_environment_has_the_release():
    api = FakeBoomiAPI(
        release=release_payload(components=[("component-a", "6.0")]),
        deployed={
            "sub-006": {
                "component-a": [
                    deployed_payload("component-a", "6.0", environment_id="prod"),
                    deployed_payload("component-a", "5.0", environment_id="test"),
                ]
            }
        },
    )
    report = build(api).verify("release-1111", accounts("sub-006"))

    check = report.accounts[0].checks[0]
    assert check.status == CheckStatus.MATCH
    assert "also deployed: 5.0" in (check.detail or "")


def test_check_instances_distinguishes_not_installed():
    api = FakeBoomiAPI(
        release=release_payload(components=[("component-a", "6.0")]),
        deployed={"sub-007": {}},
        instances={},
    )
    report = build(api, check_instances=True).verify("release-1111", accounts("sub-007"))

    assert report.accounts[0].status == AccountStatus.NOT_INSTALLED
    assert ("POST", "IntegrationPackInstance/query", "sub-007") in api.calls


def test_api_failure_for_one_account_does_not_stop_the_run():
    api = FakeBoomiAPI(
        release=release_payload(components=[("component-a", "6.0")]),
        deployed={
            "sub-008": {"component-a": [deployed_payload("component-a", "6.0")]},
            "sub-009": {"component-a": [deployed_payload("component-a", "6.0")]},
        },
        failures={"DeployedPackage/query": [403]},
    )
    report = build(api).verify("release-1111", accounts("sub-008", "sub-009"))

    statuses = {result.account_id: result.status for result in report.accounts}
    assert statuses["sub-008"] == AccountStatus.ERROR
    assert statuses["sub-009"] == AccountStatus.UP_TO_DATE
    assert report.has_errors is True


def test_wait_polls_until_the_release_finishes():
    api = FakeBoomiAPI(
        release=release_payload(components=[("component-a", "6.0")]),
        release_pending_calls=2,
        deployed={"sub-010": {"component-a": [deployed_payload("component-a", "6.0")]}},
    )
    slept = []
    verifier = build(api, sleep=slept.append, poll_interval=5.0)

    report = verifier.verify("release-1111", accounts("sub-010"), wait=True)

    assert report.release.release_status == "SUCCESS"
    assert slept == [5.0, 5.0]


def test_without_wait_an_unfinished_release_raises():
    api = FakeBoomiAPI(release=release_payload(), release_pending_calls=5)
    with pytest.raises(ReleaseNotReady):
        build(api).verify("release-1111", accounts("sub-011"))


def test_scheduled_release_is_not_polled():
    api = FakeBoomiAPI(release=release_payload(status="SCHEDULED"))
    slept = []
    verifier = build(api, sleep=slept.append)

    with pytest.raises(ReleaseNotReady) as excinfo:
        verifier.verify("release-1111", accounts("sub-012"), wait=True)

    assert excinfo.value.release_status == "SCHEDULED"
    assert slept == []


def test_polling_stops_at_the_timeout():
    api = FakeBoomiAPI(release=release_payload(), release_pending_calls=99)
    ticks = iter([0.0, 10.0, 20.0, 30.0, 40.0, 50.0])
    verifier = build(
        api, poll_interval=10.0, poll_timeout=20.0, sleep=lambda _s: None, clock=lambda: next(ticks)
    )

    with pytest.raises(ReleaseNotReady):
        verifier.verify("release-1111", accounts("sub-013"), wait=True)


def test_component_filter_limits_the_comparison():
    api = FakeBoomiAPI(
        release=release_payload(components=RELEASED),
        deployed={
            "sub-014": {
                "component-a": [deployed_payload("component-a", "6.0")],
                "component-b": [deployed_payload("component-b", "1.0")],
            }
        },
    )
    report = build(api).verify("release-1111", accounts("sub-014"), component_ids=["component-a"])

    assert [check.component_id for check in report.accounts[0].checks] == ["component-a"]
    assert report.accounts[0].status == AccountStatus.UP_TO_DATE


def test_lenient_version_matching_accepts_equivalent_strings():
    api = FakeBoomiAPI(
        release=release_payload(components=[("component-a", "1.0")]),
        deployed={"sub-015": {"component-a": [deployed_payload("component-a", "1.00")]}},
    )

    lenient = build(api).verify("release-1111", accounts("sub-015"))
    assert lenient.accounts[0].status == AccountStatus.UP_TO_DATE

    strict = build(api, strict_version=True).verify("release-1111", accounts("sub-015"))
    assert strict.accounts[0].status == AccountStatus.OUT_OF_DATE


def test_parallel_workers_check_every_account():
    deployed = {
        f"sub-{index:03d}": {"component-a": [deployed_payload("component-a", "6.0")]}
        for index in range(20, 30)
    }
    api = FakeBoomiAPI(release=release_payload(components=[("component-a", "6.0")]), deployed=deployed)
    verifier = build(api, max_workers=4)

    report = verifier.verify("release-1111", accounts(*deployed))

    assert len(report.accounts) == 10
    assert {result.status for result in report.accounts} == {AccountStatus.UP_TO_DATE}
    assert sorted(api.accounts_called()) == sorted(deployed)


def test_summary_counts_every_status():
    api = FakeBoomiAPI(
        release=release_payload(components=[("component-a", "6.0")]),
        deployed={
            "sub-031": {"component-a": [deployed_payload("component-a", "6.0")]},
            "sub-032": {"component-a": [deployed_payload("component-a", "5.0")]},
            "sub-033": {},
        },
    )
    report = build(api).verify("release-1111", accounts("sub-031", "sub-032", "sub-033"))
    summary = report.summary()

    assert summary["TOTAL"] == 3
    assert summary[AccountStatus.UP_TO_DATE] == 1
    assert summary[AccountStatus.OUT_OF_DATE] == 1
    assert summary[AccountStatus.NOT_DEPLOYED] == 1
