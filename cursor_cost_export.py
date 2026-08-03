"""Export Cursor cost and usage data into a CSV suitable for Apptio Cloudability ingestion.

Pulls per-request billing events from the Cursor Admin API endpoint
POST https://api.cursor.com/teams/filtered-usage-events and emits either the raw
events or a daily rollup keyed by user, model and billing kind.

Cost is split into two measures that must not be added together when reconciling to an
invoice. `chargeable_cost_usd` is on-demand spend billed in arrears, and is the only
part that appears as a usage line on the invoice. `included_cost_usd` is consumption
drawn from the allowance already paid for through the seat subscription, so loading it
as cost alongside a modeled seat charge double-counts it.

Costs are reported by the API in cents as floating point values; they are converted to
dollars here and only rounded at the final aggregation.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from cursor_api import (
    API_BASE,
    MAX_PAGE_SIZE,
    SERVICE,
    VENDOR,
    Client,
    CursorApiError,
    cents_to_dollars,
    day_windows,
)

CHARGE_TYPE = "usage"


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

    # `chargedCents` is the reconciliation field: it already includes the Cursor Token
    # Rate and any discount, so it must not be recomputed from tokenUsage.totalCents.
    cost = cents_to_dollars(event.get("chargedCents"))
    chargeable = bool(event.get("isChargeable"))

    return {
        "usage_date": occurred_at.date().isoformat(),
        "usage_timestamp": occurred_at.isoformat(),
        "vendor": VENDOR,
        "service": SERVICE,
        "charge_type": CHARGE_TYPE,
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
        "is_chargeable": chargeable,
        "is_headless": bool(event.get("isHeadless")),
        "is_token_based": bool(event.get("isTokenBasedCall")),
        "request_units": event.get("requestsCosts", 0),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_write_tokens": cache_write,
        "cache_read_tokens": cache_read,
        "total_tokens": input_tokens + output_tokens + cache_write + cache_read,
        "model_cost_usd": cents_to_dollars(tokens.get("totalCents")),
        "cursor_token_fee_usd": cents_to_dollars(event.get("cursorTokenFee")),
        "discount_percent_off": tokens.get("discountPercentOff", ""),
        "chargeable_cost_usd": cost if chargeable else Decimal(0),
        "included_cost_usd": Decimal(0) if chargeable else cost,
        "consumption_usd": cost,
    }


EVENT_COLUMNS = [
    "usage_date",
    "usage_timestamp",
    "vendor",
    "service",
    "charge_type",
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
    "chargeable_cost_usd",
    "included_cost_usd",
    "consumption_usd",
]

DAILY_GROUP_KEYS = [
    "usage_date",
    "vendor",
    "service",
    "charge_type",
    "user_email",
    "user_name",
    "user_id",
    "principal_type",
    "service_account_name",
    "model",
    "billing_kind",
]

DAILY_COUNTERS = [
    "request_units",
    "input_tokens",
    "output_tokens",
    "cache_write_tokens",
    "cache_read_tokens",
    "total_tokens",
]

DAILY_MONEY = [
    "cursor_token_fee_usd",
    "chargeable_cost_usd",
    "included_cost_usd",
    "consumption_usd",
]

DAILY_COLUMNS = DAILY_GROUP_KEYS + ["event_count"] + DAILY_COUNTERS + DAILY_MONEY


def roll_up_daily(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[tuple, dict[str, Any]] = {}
    for row in rows:
        key = tuple(row[column] for column in DAILY_GROUP_KEYS)
        bucket = buckets.get(key)
        if bucket is None:
            bucket = {column: row[column] for column in DAILY_GROUP_KEYS}
            bucket["event_count"] = 0
            bucket.update({counter: 0 for counter in DAILY_COUNTERS})
            bucket.update({measure: Decimal(0) for measure in DAILY_MONEY})
            buckets[key] = bucket
        bucket["event_count"] += 1
        for counter in DAILY_COUNTERS:
            bucket[counter] += row[counter]
        for measure in DAILY_MONEY:
            bucket[measure] += row[measure]

    return sorted(
        buckets.values(),
        key=lambda row: (row["usage_date"], row["user_email"], row["model"]),
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
                (row["chargeable_cost_usd"] for row in day_rows), Decimal(0)
            )
            print(
                f"{day.isoformat()}: {len(day_rows)} events, "
                f"${totals[day.isoformat()]:.2f} on-demand",
                file=sys.stderr,
            )
    except CursorApiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.granularity == "daily":
        write_csv(args.out, DAILY_COLUMNS, roll_up_daily(rows))
    else:
        write_csv(args.out, EVENT_COLUMNS, rows)

    chargeable = sum(totals.values(), Decimal(0))
    included = sum((row["included_cost_usd"] for row in rows), Decimal(0))
    print(
        f"total: {len(rows)} events, ${chargeable:.2f} on-demand, "
        f"${included:.2f} drawn from included allowance",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
