"""Export Cursor cost and usage data into a CSV suitable for Apptio Cloudability ingestion.

Pulls per-request billing events from the Cursor Admin API endpoint
POST https://api.cursor.com/teams/filtered-usage-events and emits either the raw
events or a daily rollup keyed by user, model and billing kind.

Costs are reported by the API in cents as floating point values; they are converted
to dollars here and only rounded at the final aggregation.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterator

import requests

API_BASE = "https://api.cursor.com"
USAGE_EVENTS_PATH = "/teams/filtered-usage-events"
MEMBERS_PATH = "/teams/members"

# The usage events endpoint allows 60 requests/minute per team and caps pages at 1000 rows.
MAX_PAGE_SIZE = 1000
RATE_LIMIT_BACKOFF_SECONDS = (1, 2, 4, 8, 16)

VENDOR = "Cursor"
SERVICE = "Cursor AI"


class CursorApiError(RuntimeError):
    pass


@dataclass
class Client:
    api_key: str
    base_url: str = API_BASE
    timeout: int = 60
    session: requests.Session = field(default_factory=requests.Session)

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        # The Admin API authenticates with HTTP Basic using the key as the username
        # and an empty password.
        auth = (self.api_key, "")

        last_error: Exception | None = None
        for attempt, backoff in enumerate((0,) + RATE_LIMIT_BACKOFF_SECONDS):
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

    def usage_events(
        self, start_ms: int, end_ms: int, page_size: int = MAX_PAGE_SIZE
    ) -> Iterator[dict[str, Any]]:
        page = 1
        while True:
            payload = {
                "startDate": start_ms,
                "endDate": end_ms,
                "page": page,
                "pageSize": page_size,
            }
            body = self._request(
                "POST",
                USAGE_EVENTS_PATH,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            events = body.get("usageEvents", []) or []
            yield from events

            pagination = body.get("pagination", {})
            if not pagination.get("hasNextPage"):
                return
            page = pagination.get("currentPage", page) + 1


def day_windows(start: date, end: date) -> Iterator[tuple[date, int, int]]:
    """Yield inclusive [00:00:00.000, 23:59:59.999] UTC epoch-millisecond bounds per day.

    Both API bounds are inclusive, so windows must end on the final millisecond of the
    day to avoid double-counting an event that lands exactly on midnight.
    """
    current = start
    while current <= end:
        start_dt = datetime(
            current.year, current.month, current.day, tzinfo=timezone.utc
        )
        end_dt = start_dt + timedelta(days=1) - timedelta(milliseconds=1)
        yield current, int(start_dt.timestamp() * 1000), int(end_dt.timestamp() * 1000)
        current += timedelta(days=1)


def event_cost_dollars(event: dict[str, Any]) -> Decimal:
    """Charged amount for an event, in dollars.

    `chargedCents` is the reconciliation field: it already includes the Cursor Token
    Rate and any discount, so it must not be recomputed from `tokenUsage.totalCents`.
    """
    return Decimal(str(event.get("chargedCents") or 0)) / Decimal(100)


def event_row(event: dict[str, Any], members: dict[str, dict[str, Any]]) -> dict[str, Any]:
    timestamp_ms = int(event.get("timestamp") or 0)
    occurred_at = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    tokens = event.get("tokenUsage") or {}
    email = event.get("userEmail") or ""
    member = members.get(email.lower(), {})

    input_tokens = int(tokens.get("inputTokens") or 0)
    output_tokens = int(tokens.get("outputTokens") or 0)
    cache_write = int(tokens.get("cacheWriteTokens") or 0)
    cache_read = int(tokens.get("cacheReadTokens") or 0)

    return {
        "usage_date": occurred_at.date().isoformat(),
        "usage_timestamp": occurred_at.isoformat(),
        "vendor": VENDOR,
        "service": SERVICE,
        "user_email": email,
        "user_name": member.get("name", ""),
        "user_id": member.get("id", ""),
        "user_role": member.get("role", ""),
        "principal_type": "service_account" if event.get("serviceAccountId") else "user",
        "service_account_id": event.get("serviceAccountId", ""),
        "service_account_name": event.get("serviceAccountName", ""),
        "cloud_agent_id": event.get("cloudAgentId", ""),
        "automation_id": event.get("automationId", ""),
        "model": event.get("model", ""),
        "billing_kind": event.get("kind", ""),
        "max_mode": bool(event.get("maxMode")),
        "is_chargeable": bool(event.get("isChargeable")),
        "is_headless": bool(event.get("isHeadless")),
        "is_token_based": bool(event.get("isTokenBasedCall")),
        "request_units": event.get("requestsCosts", 0),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_write_tokens": cache_write,
        "cache_read_tokens": cache_read,
        "total_tokens": input_tokens + output_tokens + cache_write + cache_read,
        "model_cost_usd": Decimal(str(tokens.get("totalCents") or 0)) / Decimal(100),
        "cursor_token_fee_usd": Decimal(str(event.get("cursorTokenFee") or 0))
        / Decimal(100),
        "discount_percent_off": tokens.get("discountPercentOff", ""),
        "cost_usd": event_cost_dollars(event),
    }


EVENT_COLUMNS = [
    "usage_date",
    "usage_timestamp",
    "vendor",
    "service",
    "user_email",
    "user_name",
    "user_id",
    "user_role",
    "principal_type",
    "service_account_id",
    "service_account_name",
    "cloud_agent_id",
    "automation_id",
    "model",
    "billing_kind",
    "max_mode",
    "is_chargeable",
    "is_headless",
    "is_token_based",
    "request_units",
    "input_tokens",
    "output_tokens",
    "cache_write_tokens",
    "cache_read_tokens",
    "total_tokens",
    "model_cost_usd",
    "cursor_token_fee_usd",
    "discount_percent_off",
    "cost_usd",
]

DAILY_GROUP_KEYS = [
    "usage_date",
    "vendor",
    "service",
    "user_email",
    "user_name",
    "user_id",
    "principal_type",
    "service_account_name",
    "model",
    "billing_kind",
]

DAILY_MEASURES = [
    "event_count",
    "request_units",
    "input_tokens",
    "output_tokens",
    "cache_write_tokens",
    "cache_read_tokens",
    "total_tokens",
    "cursor_token_fee_usd",
    "cost_usd",
]

DAILY_COLUMNS = DAILY_GROUP_KEYS + DAILY_MEASURES


def roll_up_daily(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[tuple, dict[str, Any]] = {}
    for row in rows:
        key = tuple(row[column] for column in DAILY_GROUP_KEYS)
        bucket = buckets.get(key)
        if bucket is None:
            bucket = {column: row[column] for column in DAILY_GROUP_KEYS}
            bucket.update({measure: 0 for measure in DAILY_MEASURES})
            bucket["cost_usd"] = Decimal(0)
            bucket["cursor_token_fee_usd"] = Decimal(0)
            buckets[key] = bucket
        bucket["event_count"] += 1
        for measure in (
            "request_units",
            "input_tokens",
            "output_tokens",
            "cache_write_tokens",
            "cache_read_tokens",
            "total_tokens",
        ):
            bucket[measure] += row[measure]
        bucket["cost_usd"] += row["cost_usd"]
        bucket["cursor_token_fee_usd"] += row["cursor_token_fee_usd"]

    return sorted(
        buckets.values(), key=lambda row: (row["usage_date"], row["user_email"], row["model"])
    )


def format_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return f"{value:.6f}"
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def write_csv(path: str, columns: list[str], rows: list[dict[str, Any]]) -> None:
    handle = sys.stdout if path == "-" else open(path, "w", newline="", encoding="utf-8")
    try:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: format_value(row[column]) for column in columns})
    finally:
        if handle is not sys.stdout:
            handle.close()


def parse_day(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=parse_day, required=True, help="First UTC day, YYYY-MM-DD")
    parser.add_argument(
        "--end",
        type=parse_day,
        help="Last UTC day, inclusive, YYYY-MM-DD. Defaults to --start.",
    )
    parser.add_argument(
        "--granularity",
        choices=("event", "daily"),
        default="daily",
        help="Emit one row per billing event, or a daily rollup per user/model.",
    )
    parser.add_argument("--out", default="-", help="Output CSV path, or - for stdout.")
    parser.add_argument(
        "--page-size", type=int, default=MAX_PAGE_SIZE, help=f"1..{MAX_PAGE_SIZE}"
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("CURSOR_API_KEY", ""),
        help="Cursor Admin API key. Defaults to $CURSOR_API_KEY.",
    )
    parser.add_argument("--base-url", default=API_BASE)
    args = parser.parse_args(argv)

    if not args.api_key:
        parser.error("no API key: pass --api-key or set CURSOR_API_KEY")
    if not 1 <= args.page_size <= MAX_PAGE_SIZE:
        parser.error(f"--page-size must be between 1 and {MAX_PAGE_SIZE}")

    end = args.end or args.start
    if end < args.start:
        parser.error("--end is before --start")

    client = Client(api_key=args.api_key, base_url=args.base_url)

    rows: list[dict[str, Any]] = []
    totals: dict[str, Decimal] = defaultdict(Decimal)
    try:
        members = {
            member["email"].lower(): member
            for member in client.members()
            if member.get("email")
        }
        for day, start_ms, end_ms in day_windows(args.start, end):
            day_rows = [
                event_row(event, members)
                for event in client.usage_events(start_ms, end_ms, args.page_size)
            ]
            rows.extend(day_rows)
            totals[day.isoformat()] = sum(
                (row["cost_usd"] for row in day_rows), Decimal(0)
            )
            print(
                f"{day.isoformat()}: {len(day_rows)} events, ${totals[day.isoformat()]:.2f}",
                file=sys.stderr,
            )
    except CursorApiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.granularity == "daily":
        write_csv(args.out, DAILY_COLUMNS, roll_up_daily(rows))
    else:
        write_csv(args.out, EVENT_COLUMNS, rows)

    grand_total = sum(totals.values(), Decimal(0))
    print(f"total: {len(rows)} events, ${grand_total:.2f}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
