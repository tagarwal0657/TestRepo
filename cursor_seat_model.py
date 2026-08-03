"""Model Cursor Teams seat subscription cost, which no Cursor API exposes.

A Teams invoice is `seat subscription (prorated) + on-demand usage (in arrears)
+ tax - credits`. The Admin API only reports the usage term, so the seat term has to
be reconstructed from the team roster over time and a seat-type mapping maintained
outside Cursor.

Seats are billed per *active* seat with proration, so the unit of account here is
seat-days rather than headcount:

    seat cost = (monthly rate / days in billing cycle) * days the seat was occupied

Three billing rules drive the occupancy calculation:

1. Adding a member mid-cycle creates a pro-rated charge from the day they were added.
2. Removing a member who consumed usage keeps their seat occupied until the cycle ends,
   so the billed end date is the cycle boundary rather than the removal date.
3. Unpaid Admin seats are free, and seat type is independent of team role, so seat type
   cannot be derived from the API and must come from the mapping table.

Subcommands:
    snapshot   Append today's /teams/members roster to a JSONL history file.
    backfill   Reconstruct historical rosters from /teams/audit-logs membership events.
    cost       Emit seat-day cost rows for a billing cycle.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Iterable

from cursor_api import (
    MEMBERSHIP_EVENT_TYPES,
    SERVICE,
    VENDOR,
    Client,
    CursorApiError,
    cycle_bounds,
    epoch_ms_to_date,
)

# Published Teams list prices. Premium carries 5x the included usage allowance of
# Standard; Free is the Unpaid Admin seat, which has no Cursor access.
DEFAULT_RATES: dict[str, Decimal] = {
    "standard": Decimal("40"),
    "premium": Decimal("120"),
    "free": Decimal("0"),
}

CHARGE_TYPE = "seat_subscription"

REASON_ACTIVE = "active"
REASON_HELD_AFTER_REMOVAL = "held_after_removal"

SEAT_COLUMNS = [
    "usage_date",
    "vendor",
    "service",
    "charge_type",
    "user_email",
    "user_name",
    "user_id",
    "seat_type",
    "occupancy_reason",
    "monthly_rate_usd",
    "cycle_start",
    "cycle_end",
    "cycle_days",
    "cost_usd",
]

CYCLE_COLUMNS = [
    "cycle_start",
    "cycle_end",
    "vendor",
    "service",
    "charge_type",
    "user_email",
    "user_name",
    "user_id",
    "seat_type",
    "monthly_rate_usd",
    "seat_days",
    "held_days",
    "cost_usd",
]


CENT = Decimal("0.01")


def quantize_cents(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class SeatAssignment:
    email: str
    seat_type: str
    effective_from: date
    effective_to: date | None

    def covers(self, day: date) -> bool:
        if day < self.effective_from:
            return False
        return self.effective_to is None or day <= self.effective_to


class SeatTypeMap:
    """Email to seat type over time, sourced outside Cursor.

    Seat type is not returned by any Admin API endpoint and is explicitly independent
    of the `role` field, so it has to come from procurement records or invoice lines.
    """

    def __init__(self, assignments: Iterable[SeatAssignment], default: str | None = None):
        self._by_email: dict[str, list[SeatAssignment]] = defaultdict(list)
        for assignment in assignments:
            self._by_email[assignment.email.lower()].append(assignment)
        for entries in self._by_email.values():
            entries.sort(key=lambda a: a.effective_from)
        self.default = default
        self.unmapped: set[str] = set()

    def seat_type(self, email: str, day: date) -> str | None:
        for assignment in reversed(self._by_email.get(email.lower(), [])):
            if assignment.covers(day):
                return assignment.seat_type
        if self.default is None:
            self.unmapped.add(email.lower())
            return None
        self.unmapped.add(email.lower())
        return self.default


def load_seat_types(path: str, default: str | None) -> SeatTypeMap:
    assignments = []
    with open(path, newline="", encoding="utf-8") as handle:
        for line, row in enumerate(csv.DictReader(handle), start=2):
            email = (row.get("email") or "").strip()
            seat_type = (row.get("seat_type") or "").strip().lower()
            if not email or not seat_type:
                continue
            effective_from = (row.get("effective_from") or "").strip()
            effective_to = (row.get("effective_to") or "").strip()
            if not effective_from:
                raise ValueError(f"{path}:{line}: effective_from is required")
            assignments.append(
                SeatAssignment(
                    email=email,
                    seat_type=seat_type,
                    effective_from=parse_day(effective_from),
                    effective_to=parse_day(effective_to) if effective_to else None,
                )
            )
    return SeatTypeMap(assignments, default=default)


def load_snapshots(path: str) -> dict[date, dict[str, dict[str, Any]]]:
    """Read the roster history file into {day: {email: member}}.

    Members flagged `isRemoved` are excluded: they still appear in /teams/members after
    offboarding, and their continued seat charge is handled by the removal rule instead.
    """
    snapshots: dict[date, dict[str, dict[str, Any]]] = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            day = parse_day(record["date"])
            snapshots[day] = {
                member["email"].lower(): member
                for member in record.get("teamMembers", [])
                if member.get("email") and not member.get("isRemoved")
            }
    return snapshots


def roster_on(
    snapshots: dict[date, dict[str, dict[str, Any]]], day: date
) -> dict[str, dict[str, Any]]:
    """Roster for a day, carrying the most recent earlier snapshot forward.

    Days before the first snapshot fall back to the earliest one available, so a
    partial history degrades into an approximation rather than silently dropping seats.
    """
    if day in snapshots:
        return snapshots[day]
    earlier = [d for d in snapshots if d < day]
    if earlier:
        return snapshots[max(earlier)]
    later = [d for d in snapshots if d > day]
    if later:
        return snapshots[min(later)]
    return {}


def load_usage_emails(path: str, cycle_start: date, cycle_end: date) -> set[str]:
    """Emails that consumed any usage during the cycle.

    Drives the rule that removing a member who used credits keeps their seat occupied
    until the cycle ends. Accepts either granularity emitted by cursor_cost_export.
    """
    emails: set[str] = set()
    with open(path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            email = (row.get("user_email") or "").strip().lower()
            usage_date = (row.get("usage_date") or "").strip()
            if not email or not usage_date:
                continue
            if cycle_start <= parse_day(usage_date) <= cycle_end:
                emails.add(email)
    return emails


def seat_days(
    snapshots: dict[date, dict[str, dict[str, Any]]],
    cycle_start: date,
    cycle_end: date,
    usage_emails: set[str],
) -> dict[str, list[tuple[date, str, dict[str, Any]]]]:
    """Occupied seat-days per member for the cycle, with the reason for each day."""
    days = [
        cycle_start + timedelta(days=offset)
        for offset in range((cycle_end - cycle_start).days + 1)
    ]

    present: dict[str, set[date]] = defaultdict(set)
    metadata: dict[str, dict[str, Any]] = {}
    for day in days:
        for email, member in roster_on(snapshots, day).items():
            present[email].add(day)
            metadata[email] = member

    occupancy: dict[str, list[tuple[date, str, dict[str, Any]]]] = {}
    for email, present_days in present.items():
        # A member who left mid-cycle after consuming usage keeps the seat to cycle end.
        held_from: date | None = None
        if cycle_end not in present_days and email in usage_emails:
            held_from = max(present_days) + timedelta(days=1)

        entries = []
        for day in days:
            if day in present_days:
                entries.append((day, REASON_ACTIVE, metadata[email]))
            elif held_from is not None and day >= held_from:
                entries.append((day, REASON_HELD_AFTER_REMOVAL, metadata[email]))
        occupancy[email] = entries
    return occupancy


def seat_cost_rows(
    occupancy: dict[str, list[tuple[date, str, dict[str, Any]]]],
    seat_types: SeatTypeMap,
    rates: dict[str, Decimal],
    cycle_start: date,
    cycle_end: date,
) -> list[dict[str, Any]]:
    """Spread each seat's monthly rate across the days it was occupied.

    Daily amounts are derived from the running cumulative total rather than by dividing
    the rate by the cycle length, because a rate like $40 over 31 days does not divide
    evenly. Taking the difference between successive rounded cumulative totals makes the
    daily rows sum to exactly the prorated charge, which is what has to tie to the
    invoice.
    """
    cycle_days = (cycle_end - cycle_start).days + 1
    rows: list[dict[str, Any]] = []
    for email, entries in sorted(occupancy.items()):
        billed_days: dict[str, int] = defaultdict(int)
        allocated: dict[str, Decimal] = defaultdict(Decimal)
        for day, reason, member in entries:
            seat_type = seat_types.seat_type(email, day)
            if seat_type is None:
                continue
            if seat_type not in rates:
                raise ValueError(f"no rate configured for seat type {seat_type!r}")
            monthly_rate = rates[seat_type]

            billed_days[seat_type] += 1
            cumulative = quantize_cents(
                monthly_rate * billed_days[seat_type] / cycle_days
            )
            day_cost = cumulative - allocated[seat_type]
            allocated[seat_type] = cumulative

            rows.append(
                {
                    "usage_date": day.isoformat(),
                    "vendor": VENDOR,
                    "service": SERVICE,
                    "charge_type": CHARGE_TYPE,
                    "user_email": email,
                    "user_name": member.get("name", ""),
                    "user_id": member.get("id", ""),
                    "seat_type": seat_type,
                    "occupancy_reason": reason,
                    "monthly_rate_usd": monthly_rate,
                    "cycle_start": cycle_start.isoformat(),
                    "cycle_end": cycle_end.isoformat(),
                    "cycle_days": cycle_days,
                    "cost_usd": day_cost,
                }
            )
    return rows


def roll_up_cycle(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[tuple, dict[str, Any]] = {}
    for row in rows:
        key = (row["user_email"], row["seat_type"])
        bucket = buckets.get(key)
        if bucket is None:
            bucket = {
                "cycle_start": row["cycle_start"],
                "cycle_end": row["cycle_end"],
                "vendor": row["vendor"],
                "service": row["service"],
                "charge_type": row["charge_type"],
                "user_email": row["user_email"],
                "user_name": row["user_name"],
                "user_id": row["user_id"],
                "seat_type": row["seat_type"],
                "monthly_rate_usd": row["monthly_rate_usd"],
                "seat_days": 0,
                "held_days": 0,
                "cost_usd": Decimal(0),
            }
            buckets[key] = bucket
        bucket["seat_days"] += 1
        if row["occupancy_reason"] == REASON_HELD_AFTER_REMOVAL:
            bucket["held_days"] += 1
        bucket["cost_usd"] += row["cost_usd"]
    return sorted(buckets.values(), key=lambda row: (row["user_email"], row["seat_type"]))


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


def parse_rates(pairs: list[str]) -> dict[str, Decimal]:
    rates = dict(DEFAULT_RATES)
    for pair in pairs:
        if "=" not in pair:
            raise ValueError(f"--rate expects seat_type=amount, got {pair!r}")
        seat_type, amount = pair.split("=", 1)
        rates[seat_type.strip().lower()] = Decimal(amount.strip())
    return rates


def command_cycle(args: argparse.Namespace, client: Client) -> int:
    """Report the current billing cycle bounds, which drive every proration calculation.

    Cycles follow the subscription anniversary rather than the calendar month, so this
    is the authoritative source for --cycle-start.
    """
    body = client.spend()
    anchor = body.get("subscriptionCycleStart")
    if anchor is None:
        print("error: /teams/spend did not return subscriptionCycleStart", file=sys.stderr)
        return 1

    start, end = cycle_bounds(epoch_ms_to_date(anchor))
    spend = sum(
        (Decimal(str(m.get("spendCents") or 0)) for m in body.get("teamMemberSpend", [])),
        Decimal(0),
    ) / Decimal(100)
    print(f"cycle_start: {start.isoformat()}")
    print(f"cycle_end:   {end.isoformat()}")
    print(f"cycle_days:  {(end - start).days + 1}")
    print(f"on_demand_spend_usd: {spend:.6f}")
    print(f"members: {body.get('totalMembers')}")
    return 0


def command_snapshot(args: argparse.Namespace, client: Client) -> int:
    day = args.date or datetime.now(timezone.utc).date()
    record = {"date": day.isoformat(), "teamMembers": client.members()}
    with open(args.out, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
    print(
        f"{day.isoformat()}: recorded {len(record['teamMembers'])} members to {args.out}",
        file=sys.stderr,
    )
    return 0


def command_backfill(args: argparse.Namespace, client: Client) -> int:
    """Reconstruct daily rosters backwards from audit log membership events.

    /teams/members only reports the present, so history is rebuilt by taking today's
    roster and replaying add_user and remove_user events in reverse.
    """
    current = {
        member["email"].lower(): member
        for member in client.members()
        if member.get("email") and not member.get("isRemoved")
    }

    events = sorted(
        client.audit_logs(args.start, args.end, MEMBERSHIP_EVENT_TYPES),
        key=lambda event: event.get("timestamp", ""),
        reverse=True,
    )

    rosters: dict[date, dict[str, dict[str, Any]]] = {}
    day = args.end
    event_index = 0
    while day >= args.start:
        while event_index < len(events):
            event = events[event_index]
            timestamp = event.get("timestamp", "")
            event_day = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).date()
            if event_day <= day:
                break
            email = (event.get("event_data", {}).get("email") or "").lower()
            if email:
                if event.get("event_type") == "add_user":
                    current.pop(email, None)
                elif event.get("event_type") == "remove_user":
                    current[email] = {"email": email, "name": "", "id": ""}
            event_index += 1
        rosters[day] = dict(current)
        day -= timedelta(days=1)

    with open(args.out, "a", encoding="utf-8") as handle:
        for day in sorted(rosters):
            handle.write(
                json.dumps(
                    {"date": day.isoformat(), "teamMembers": list(rosters[day].values())}
                )
                + "\n"
            )
    print(
        f"reconstructed {len(rosters)} daily rosters from {len(events)} audit events",
        file=sys.stderr,
    )
    return 0


def command_cost(args: argparse.Namespace, _client: Client | None) -> int:
    cycle_start, cycle_end = cycle_bounds(args.cycle_start)
    if args.cycle_end:
        cycle_end = args.cycle_end

    rates = parse_rates(args.rate)
    seat_types = load_seat_types(args.seat_types, args.default_seat_type)
    snapshots = load_snapshots(args.snapshots)
    if not snapshots:
        print(f"error: no roster snapshots in {args.snapshots}", file=sys.stderr)
        return 1

    usage_emails = (
        load_usage_emails(args.usage, cycle_start, cycle_end) if args.usage else set()
    )
    if not args.usage:
        print(
            "warning: no --usage supplied, so seats held after removal are not modeled",
            file=sys.stderr,
        )

    occupancy = seat_days(snapshots, cycle_start, cycle_end, usage_emails)
    rows = seat_cost_rows(occupancy, seat_types, rates, cycle_start, cycle_end)

    if seat_types.unmapped:
        listed = ", ".join(sorted(seat_types.unmapped))
        if args.default_seat_type:
            print(
                f"warning: assumed {args.default_seat_type} for unmapped members: {listed}",
                file=sys.stderr,
            )
        else:
            print(f"warning: skipped unmapped members: {listed}", file=sys.stderr)

    if args.granularity == "cycle":
        write_csv(args.out, CYCLE_COLUMNS, roll_up_cycle(rows))
    else:
        write_csv(args.out, SEAT_COLUMNS, rows)

    total = sum((row["cost_usd"] for row in rows), Decimal(0))
    held = sum(1 for row in rows if row["occupancy_reason"] == REASON_HELD_AFTER_REMOVAL)
    print(
        f"{cycle_start.isoformat()}..{cycle_end.isoformat()}: "
        f"{len(rows)} seat-days ({held} held after removal), ${total:.2f}",
        file=sys.stderr,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--api-key",
        default=os.environ.get("CURSOR_API_KEY", ""),
        help="Cursor Admin API key. Defaults to $CURSOR_API_KEY.",
    )
    parser.add_argument("--base-url", default=None)
    subparsers = parser.add_subparsers(dest="command", required=True)

    cycle = subparsers.add_parser(
        "cycle", help="Print the current billing cycle bounds from /teams/spend."
    )
    cycle.set_defaults(handler=command_cycle, needs_api=True)

    snapshot = subparsers.add_parser(
        "snapshot", help="Append today's roster to a JSONL history file."
    )
    snapshot.add_argument("--out", default="roster.jsonl")
    snapshot.add_argument("--date", type=parse_day, help="Override the snapshot date.")
    snapshot.set_defaults(handler=command_snapshot, needs_api=True)

    backfill = subparsers.add_parser(
        "backfill", help="Rebuild historical rosters from audit log membership events."
    )
    backfill.add_argument("--start", type=parse_day, required=True)
    backfill.add_argument("--end", type=parse_day, required=True)
    backfill.add_argument("--out", default="roster.jsonl")
    backfill.set_defaults(handler=command_backfill, needs_api=True)

    cost = subparsers.add_parser("cost", help="Emit seat-day cost rows for a cycle.")
    cost.add_argument(
        "--cycle-start",
        type=parse_day,
        required=True,
        help="First day of the billing cycle, from subscriptionCycleStart on /teams/spend.",
    )
    cost.add_argument(
        "--cycle-end", type=parse_day, help="Override the derived cycle end date."
    )
    cost.add_argument("--snapshots", default="roster.jsonl")
    cost.add_argument("--seat-types", required=True, help="CSV of email to seat type.")
    cost.add_argument(
        "--usage",
        help="Usage CSV from cursor_cost_export, used to detect members who consumed "
        "credits before being removed.",
    )
    cost.add_argument(
        "--default-seat-type",
        help="Seat type assumed for members missing from the mapping. Skipped if unset.",
    )
    cost.add_argument(
        "--rate",
        action="append",
        default=[],
        metavar="TYPE=USD",
        help="Override a monthly seat rate, e.g. --rate premium=110.",
    )
    cost.add_argument("--granularity", choices=("daily", "cycle"), default="daily")
    cost.add_argument("--out", default="-")
    cost.set_defaults(handler=command_cost, needs_api=False)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    client = None
    if args.needs_api:
        if not args.api_key:
            parser.error("no API key: pass --api-key or set CURSOR_API_KEY")
        client_kwargs = {"api_key": args.api_key}
        if args.base_url:
            client_kwargs["base_url"] = args.base_url
        client = Client(**client_kwargs)

    try:
        return args.handler(args, client)
    except (CursorApiError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
