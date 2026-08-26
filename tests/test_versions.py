import pytest

from boomi_release_check import versions


@pytest.mark.parametrize(
    "raw,expected",
    [(" 1.0 ", "1.0"), ("v2.3", "2.3"), ("V10", "10"), ("vNext", "vNext"), (None, "")],
)
def test_normalize(raw, expected):
    assert versions.normalize(raw) == expected


@pytest.mark.parametrize(
    "expected,actual",
    [("1.0", "1.0"), ("1.0", "1.00"), ("1.0", "1"), ("2.1.0", "2.1"), ("1.0", " v1.0 ")],
)
def test_versions_equal_lenient(expected, actual):
    assert versions.versions_equal(expected, actual)


@pytest.mark.parametrize("expected,actual", [("1.0", "2.0"), ("1.0", ""), ("", "1.0"), ("1.0", None)])
def test_versions_not_equal(expected, actual):
    assert not versions.versions_equal(expected, actual)


def test_strict_mode_requires_exact_string():
    assert versions.versions_equal("1.0", "1.00") is True
    assert versions.versions_equal("1.0", "1.00", strict=True) is False
    assert versions.versions_equal("1.0", "1.0", strict=True) is True


def test_non_numeric_versions_compare_case_insensitively():
    assert versions.versions_equal("2026.08-RC1", "2026.08-rc1")
    assert not versions.versions_equal("2026.08-RC1", "2026.08-RC2")


@pytest.mark.parametrize(
    "expected,actual,drift",
    [
        ("6.0", "6.0", None),
        ("6.0", "5.0", "BEHIND"),
        ("6.0", "7.0", "AHEAD"),
        ("6.0", "hotfix", "DIFFERENT"),
    ],
)
def test_describe_drift(expected, actual, drift):
    assert versions.describe_drift(expected, actual) == drift


def test_compare_returns_none_for_incomparable_versions():
    assert versions.compare("1.0", "release-candidate") is None
