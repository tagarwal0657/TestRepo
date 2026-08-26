"""Comparison helpers for Boomi packaged-component versions.

Boomi packaged component versions are user-defined strings, so "1.0", "1.00"
and "1" all describe the same release even though they differ textually. These
helpers implement a lenient numeric comparison with an opt-in strict mode for
tenants that treat the version string as an exact identifier.
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

_NUMERIC_VERSION = re.compile(r"^\d+(\.\d+)*$")


def normalize(version: Optional[str]) -> str:
    """Trim whitespace and a leading ``v`` so "v1.0" and " 1.0 " compare equal."""
    if version is None:
        return ""
    text = str(version).strip()
    if text[:1] in {"v", "V"} and _NUMERIC_VERSION.match(text[1:]):
        text = text[1:]
    return text


def parse_numeric(version: Optional[str]) -> Optional[Tuple[int, ...]]:
    """Return a comparable tuple for dotted numeric versions, else ``None``."""
    text = normalize(version)
    if not text or not _NUMERIC_VERSION.match(text):
        return None
    parts = tuple(int(part) for part in text.split("."))
    # Drop trailing zeroes so 1.0, 1.0.0 and 1 collapse to the same tuple.
    while len(parts) > 1 and parts[-1] == 0:
        parts = parts[:-1]
    return parts


def versions_equal(expected: Optional[str], actual: Optional[str], strict: bool = False) -> bool:
    """Return ``True`` when ``actual`` represents the same version as ``expected``."""
    expected_text = normalize(expected)
    actual_text = normalize(actual)
    if not expected_text or not actual_text:
        return False
    if expected_text == actual_text:
        return True
    if strict:
        return False
    expected_numeric = parse_numeric(expected_text)
    actual_numeric = parse_numeric(actual_text)
    if expected_numeric is None or actual_numeric is None:
        return expected_text.casefold() == actual_text.casefold()
    return expected_numeric == actual_numeric


def compare(expected: Optional[str], actual: Optional[str]) -> Optional[int]:
    """Order two versions numerically.

    Returns a negative number when ``actual`` is behind ``expected``, zero when
    they match, a positive number when ``actual`` is ahead, and ``None`` when the
    versions are not numerically comparable.
    """
    expected_numeric = parse_numeric(expected)
    actual_numeric = parse_numeric(actual)
    if expected_numeric is None or actual_numeric is None:
        return None
    if actual_numeric == expected_numeric:
        return 0
    return -1 if actual_numeric < expected_numeric else 1


def describe_drift(expected: Optional[str], actual: Optional[str]) -> Optional[str]:
    """Classify a version mismatch as BEHIND, AHEAD or DIFFERENT."""
    ordering = compare(expected, actual)
    if ordering is None:
        return "DIFFERENT"
    if ordering == 0:
        return None
    return "BEHIND" if ordering < 0 else "AHEAD"
