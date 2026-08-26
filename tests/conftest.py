import pytest

# The CLI reads these as argparse defaults, so a developer's shell (or a demo run
# against the mock API) must not leak into the tests.
BOOMI_ENV_VARS = (
    "BOOMI_ACCOUNT_ID",
    "BOOMI_USERNAME",
    "BOOMI_API_TOKEN",
    "BOOMI_PASSWORD",
    "BOOMI_BASE_URL",
    "BOOMI_RELEASE_REQUEST_ID",
)


@pytest.fixture(autouse=True)
def clear_boomi_env(monkeypatch):
    for name in BOOMI_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
