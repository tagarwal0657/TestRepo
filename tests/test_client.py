import pytest

from boomi_release_check.client import BoomiClient
from boomi_release_check.errors import BoomiHTTPError, ConfigError

from .fakes import FakeBoomiAPI, deployed_payload, release_payload


def make_client(api: FakeBoomiAPI, **kwargs) -> BoomiClient:
    return BoomiClient(
        account_id="master-ACCT",
        username="BOOMI_TOKEN.user@example.com",
        password="secret-token",
        transport=api,
        sleep=lambda _seconds: None,
        **kwargs,
    )


def test_platform_api_puts_sub_account_in_the_path():
    client = make_client(FakeBoomiAPI())
    url = client.build_url("DeployedPackage/query", account="sub-001")
    assert url == "https://api.boomi.com/api/rest/v1/sub-001/DeployedPackage/query"


def test_partner_api_uses_override_account_parameter():
    client = make_client(FakeBoomiAPI(), partner_api=True)
    url = client.build_url("DeployedPackage/query", account="sub-001")
    assert url == (
        "https://api.boomi.com/partner/api/rest/v1/master-ACCT/DeployedPackage/query"
        "?overrideAccount=sub-001"
    )


def test_partner_api_omits_override_for_the_parent_account():
    client = make_client(FakeBoomiAPI(), partner_api=True)
    assert "overrideAccount" not in client.build_url("Account/query", account="master-ACCT")


def test_missing_credentials_raise_config_error():
    with pytest.raises(ConfigError):
        BoomiClient(account_id="", username="u", password="p")
    with pytest.raises(ConfigError):
        BoomiClient(account_id="a", username="u", password="")


def test_release_status_returns_202_while_in_progress():
    api = FakeBoomiAPI(release=release_payload(), release_pending_calls=1)
    client = make_client(api)
    status, payload = client.get_release_status_raw("release-1111")
    assert status == 202
    assert payload["releaseStatus"] == "IN_PROGRESS"

    status, payload = client.get_release_status_raw("release-1111")
    assert status == 200
    assert payload["releaseStatus"] == "SUCCESS"


def test_query_follows_query_token_paging():
    records = [deployed_payload("component-a", str(index)) for index in range(5)]
    api = FakeBoomiAPI(deployed={"sub-001": {"component-a": records}}, page_size=2)
    client = make_client(api)

    results = client.query_deployed_packages("component-a", account="sub-001")

    assert len(results) == 5
    assert [entry["packageVersion"] for entry in results] == ["0", "1", "2", "3", "4"]
    assert sum(1 for _, rest, _ in api.calls if rest == "DeployedPackage/queryMore") == 2


def test_deployed_package_filter_matches_the_documented_shape():
    api = FakeBoomiAPI(deployed={"sub-001": {"component-a": []}})
    client = make_client(api)
    client.query_deployed_packages("component-a", account="sub-001")

    assert ("POST", "DeployedPackage/query", "sub-001") in api.calls


def test_retries_transient_5xx_then_succeeds():
    api = FakeBoomiAPI(
        release=release_payload(),
        failures={"ReleaseIntegrationPackStatus/release-1111": [503, 503]},
    )
    client = make_client(api, max_retries=3)

    status, payload = client.get_release_status_raw("release-1111")

    assert status == 200
    assert payload["releaseStatus"] == "SUCCESS"


def test_gives_up_after_max_retries():
    api = FakeBoomiAPI(
        release=release_payload(),
        failures={"ReleaseIntegrationPackStatus/release-1111": [503, 503, 503]},
    )
    client = make_client(api, max_retries=1)

    with pytest.raises(BoomiHTTPError) as excinfo:
        client.get_release_status_raw("release-1111")
    assert excinfo.value.status == 503


def test_non_retryable_error_raises_immediately():
    api = FakeBoomiAPI(
        release=release_payload(),
        failures={"ReleaseIntegrationPackStatus/release-1111": [403]},
    )
    client = make_client(api)

    with pytest.raises(BoomiHTTPError) as excinfo:
        client.get_release_status_raw("release-1111")
    assert excinfo.value.status == 403
