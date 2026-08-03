"""Shared client and date helpers for the Cursor Admin API.

Auth is HTTP Basic with the API key as the username and an empty password.
All monetary fields returned by the API are cents, as floating point values, so
callers convert with `cents_to_dollars` and keep `Decimal` until final aggregation.
"""

from __future__ import annotations

import calendar
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterator

import requests

API_BASE = "https://api.cursor.com"
USAGE_EVENTS_PATH = "/teams/filtered-usage-events"
MEMBERS_PATH = "/teams/members"
SPEND_PATH = "/teams/spend"
AUDIT_LOGS_PATH = "/teams/audit-logs"

# The usage events endpoint allows 60 requests/minute per team and caps pages at 1000 rows.
MAX_PAGE_SIZE = 1000
AUDIT_LOG_MAX_PAGE_SIZE = 500
# Audit logs and daily usage cannot span more than 30 days per request.
MAX_RANGE_DAYS = 30
RATE_LIMIT_BACKOFF_SECONDS = (1, 2, 4, 8, 16)

VENDOR = "Cursor"
SERVICE = "Cursor AI"

MEMBERSHIP_EVENT_TYPES = ("add_user", "remove_user", "update_user_role")


class CursorApiError(RuntimeError):
    pass


def cents_to_dollars(value: Any) -> Decimal:
    """Convert an API cents field to dollars without losing precision.

    Cents arrive as floats such as 21.36232, so they are routed through str() rather
    than Decimal(float) to avoid binary representation noise.
    """
    return Decimal(str(value or 0)) / Decimal(100)


@dataclass
class Client:
    api_key: str
    base_url: str = API_BASE
    timeout: int = 60
    session: requests.Session = field(default_factory=requests.Session)

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        auth = (self.api_key, "")

        last_error: Exception | None = None
        for backoff in (0,) + RATE_LIMIT_BACKOFF_SECONDS:
            if backoff:
                time.sleep(backoff)
            try:
                response = self.session.request(
                    method, url, auth=auth, timeout=self.timeout, **kwargs
                )
            except requests.RequestException as exc:
                last_error = exc
                continue

            if response.status_code == 429 or response.status_code >= 500:
                last_error = CursorApiError(
                    f"{response.status_code} from {path}: {response.text[:400]}"
                )
                continue
            if response.status_code >= 400:
                raise CursorApiError(
                    f"{response.status_code} from {path}: {response.text[:400]}"
                )
            return response.json()

        raise CursorApiError(f"Giving up on {path} after retries") from last_error

    def members(self) -> list[dict[str, Any]]:
        return self._request("GET", MEMBERS_PATH).get("teamMembers", [])

    def spend(self, page_size: int = 100) -> dict[str, Any]:
        """Per-user spend for the current billing cycle.

        This endpoint takes no date parameters and only ever reports the open cycle,
        so callers must snapshot it before the cycle rolls over.
        """
        members: list[dict[str, Any]] = []
        page = 1
        body: dict[str, Any] = {}
        while True:
            body = self._request(
                "POST",
                SPEND_PATH,
                json={"page": page, "pageSize": page_size},
                headers={"Content-Type": "application/json"},
            )
            members.extend(body.get("teamMemberSpend", []) or [])
            if page >= body.get("totalPages", 1):
                break
            page += 1
        return {
            "teamMemberSpend": members,
            "subscriptionCycleStart": body.get("subscriptionCycleStart"),
            "totalMembers": body.get("totalMembers"),
        }

    def usage_events(
        self, start_ms: int, end_ms: int, page_size: int = MAX_PAGE_SIZE
    ) -> Iterator[dict[str, Any]]:
        page = 1
        while True:
            body = self._request(
                "POST",
                USAGE_EVENTS_PATH,
                json={
                    "startDate": start_ms,
                    "endDate": end_ms,
                    "page": page,
                    "pageSize": page_size,
                },
                headers={"Content-Type": "application/json"},
            )
            yield from body.get("usageEvents", []) or []

            pagination = body.get("pagination", {})
            if not pagination.get("hasNextPage"):
                return
            page = pagination.get("currentPage", page) + 1

    def audit_logs(
        self,
        start: date,
        end: date,
        event_types: tuple[str, ...] = MEMBERSHIP_EVENT_TYPES,
        page_size: int = AUDIT_LOG_MAX_PAGE_SIZE,
    ) -> Iterator[dict[str, Any]]:
        """Audit events over a range, chunked to respect the 30-day per-request cap."""
        for chunk_start, chunk_end in chunk_range(start, end, MAX_RANGE_DAYS):
            page = 1
            while True:
                body = self._request(
                    "GET",
                    AUDIT_LOGS_PATH,
                    params={
                        "startTime": chunk_start.isoformat(),
                        "endTime": chunk_end.isoformat(),
                        "eventTypes": ",".join(event_types),
                        "page": page,
                        "pageSize": page_size,
                    },
                )
                events = body.get("events", []) or []
                yield from events
                if not body.get("hasNextPage") and len(events) < page_size:
                    break
                if not events:
                    break
                page += 1


def chunk_range(start: date, end: date, max_days: int) -> Iterator[tuple[date, date]]:
    current = start
    while current <= end:
        chunk_end = min(current + timedelta(days=max_days - 1), end)
        yield current, chunk_end
        current = chunk_end + timedelta(days=1)


def day_windows(start: date, end: date) -> Iterator[tuple[date, int, int]]:
    """Yield inclusive [00:00:00.000, 23:59:59.999] UTC epoch-millisecond bounds per day.

    Both API bounds are inclusive, so windows must end on the final millisecond of the
    day to avoid double-counting an event that lands exactly on midnight.
    """
    current = start
    while current <= end:
        start_dt = datetime(current.year, current.month, current.day, tzinfo=timezone.utc)
        end_dt = start_dt + timedelta(days=1) - timedelta(milliseconds=1)
        yield current, int(start_dt.timestamp() * 1000), int(end_dt.timestamp() * 1000)
        current += timedelta(days=1)


def add_months(value: date, months: int) -> date:
    """Shift by whole months, clamping to the last valid day of the target month."""
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def cycle_bounds(cycle_start: date) -> tuple[date, date]:
    """Inclusive bounds of the billing cycle beginning on `cycle_start`.

    Cycles follow the subscription anniversary, not the calendar month, so callers
    should derive `cycle_start` from `subscriptionCycleStart` on /teams/spend rather
    than assuming the first of the month.
    """
    return cycle_start, add_months(cycle_start, 1) - timedelta(days=1)


def cycle_containing(cycle_anchor: date, day: date) -> tuple[date, date]:
    """Bounds of the cycle containing `day`, given any known cycle start date."""
    months = (day.year - cycle_anchor.year) * 12 + (day.month - cycle_anchor.month)
    start = add_months(cycle_anchor, months)
    if day < start:
        start = add_months(cycle_anchor, months - 1)
    elif day > add_months(start, 1) - timedelta(days=1):
        start = add_months(cycle_anchor, months + 1)
    return cycle_bounds(start)


def epoch_ms_to_date(value: Any) -> date:
    return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).date()
