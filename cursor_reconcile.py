"""Reconcile modeled Cursor cost against an actual Teams invoice, and size seats.

The ledger it produces is:

    modeled  = seat subscription accrual + on-demand usage
    variance = invoice total - tax - credits - modeled

Variance is expected to be small but non-zero and is tracked as a standing line rather
than forced to zero, because Cursor applies billing adjustments as account credit on a
*future* invoice. A mid-cycle downgrade therefore produces a real timing difference
between accrual and cash that no amount of modeling removes.

Included usage is deliberately excluded from the cost side of the ledger: it was already
paid for through the seat subscription, so counting it again overstates spend. It is
instead used here to measure how much of each seat's allowance was actually consumed,
which is the signal that identifies idle and mis-sized seats.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from cursor_seat_model import format_value, parse_rates, write_csv

LEDGER_COLUMNS = ["component", "amount_usd", "source"]

UTILIZATION_COLUMNS = [
    "user_email",
    "user_name",
    "seat_type",
    "seat_days",
    "seat_cost_usd",
    "included_allowance_usd",
    "included_consumed_usd",
    "utilization_percent",
    "on_demand_usd",
    "total_cost_usd",
    "recommendation",
    "estimated_monthly_saving_usd",
]


@dataclass
class UserTotals:
    email: str
    name: str = ""
    seat_type: str = ""
    seat_days: int = 0
    seat_cost: Decimal = field(default_factory=lambda: Decimal(0))
    included: Decimal = field(default_factory=lambda: Decimal(0))
    on_demand: Decimal = field(default_factory=lambda: Decimal(0))


def read_rows(path: str) -> list[dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def decimal_of(row: dict[str, str], column: str) -> Decimal:
    value = (row.get(column) or "").strip()
    return Decimal(value) if value else Decimal(0)


def collect(seat_path: str, usage_path: str | None) -> dict[str, UserTotals]:
    users: dict[str, UserTotals] = {}

    for row in read_rows(seat_path):
        email = (row.get("user_email") or "").strip().lower()
        if not email:
            continue
        totals = users.setdefault(email, UserTotals(email=email))
        totals.name = totals.name or (row.get("user_name") or "")
        totals.seat_type = row.get("seat_type") or totals.seat_type
        totals.seat_cost += decimal_of(row, "cost_usd")
        # The seat model emits one row per seat-day at daily granularity and a
        # pre-summed seat_days column at cycle granularity.
        totals.seat_days += int(decimal_of(row, "seat_days")) if row.get("seat_days") else 1

    if usage_path:
        for row in read_rows(usage_path):
            email = (row.get("user_email") or "").strip().lower()
            if not email:
                continue
            totals = users.setdefault(email, UserTotals(email=email))
            totals.name = totals.name or (row.get("user_name") or "")
            totals.included += decimal_of(row, "included_cost_usd")
            totals.on_demand += decimal_of(row, "chargeable_cost_usd")

    return users


def infer_allowances(users: dict[str, UserTotals]) -> dict[str, Decimal]:
    """Estimate each seat type's included allowance from members who went on-demand.

    A member who incurred on-demand spend has by definition exhausted their allowance,
    so their consumed included usage equals it. The allowance is not published as a
    dollar figure, so this is the only way to derive it from data alone. Treat the
    result as a cross-check against the contract, not as authoritative.
    """
    per_type: dict[str, list[Decimal]] = defaultdict(list)
    for totals in users.values():
        if totals.on_demand > 0 and totals.seat_type:
            per_type[totals.seat_type].append(totals.included)
    return {
        seat_type: max(values) for seat_type, values in per_type.items() if values
    }


def recommend(
    totals: UserTotals,
    allowances: dict[str, Decimal],
    rates: dict[str, Decimal],
) -> tuple[str, Decimal]:
    """Suggest a seat action and the monthly saving it would produce.

    Both directions are the same trade: moving between tiers swaps a fixed rate
    difference for a change in included allowance, which converts into on-demand spend.
    """
    if totals.seat_cost <= 0:
        return "", Decimal(0)
    if totals.included == 0 and totals.on_demand == 0:
        return "reclaim: seat had no usage", totals.seat_cost

    current = totals.seat_type
    allowance = allowances.get(current)
    if allowance is None:
        return "", Decimal(0)

    best_action, best_saving = "", Decimal(0)
    for candidate, candidate_rate in rates.items():
        if candidate == current or candidate not in allowances:
            continue
        # Free seats are Unpaid Admin seats with no Cursor access, so they are never a
        # valid target for someone who is actually using the product.
        if candidate_rate <= 0:
            continue
        candidate_allowance = allowances[candidate]
        consumption = totals.included + totals.on_demand
        current_on_demand = max(Decimal(0), consumption - allowance)
        candidate_on_demand = max(Decimal(0), consumption - candidate_allowance)
        saving = (rates[current] + current_on_demand) - (
            candidate_rate + candidate_on_demand
        )
        if saving > best_saving:
            verb = "downgrade" if candidate_rate < rates[current] else "upgrade"
            best_action, best_saving = f"{verb} to {candidate}", saving

    return best_action, best_saving


def utilization_rows(
    users: dict[str, UserTotals],
    allowances: dict[str, Decimal],
    rates: dict[str, Decimal],
) -> list[dict[str, Any]]:
    rows = []
    for totals in sorted(users.values(), key=lambda t: t.email):
        allowance = allowances.get(totals.seat_type)
        utilization = ""
        if allowance and allowance > 0:
            utilization = f"{min(totals.included / allowance, Decimal(1)) * 100:.1f}"
        action, saving = recommend(totals, allowances, rates)
        rows.append(
            {
                "user_email": totals.email,
                "user_name": totals.name,
                "seat_type": totals.seat_type,
                "seat_days": totals.seat_days,
                "seat_cost_usd": totals.seat_cost,
                "included_allowance_usd": allowance if allowance is not None else "",
                "included_consumed_usd": totals.included,
                "utilization_percent": utilization,
                "on_demand_usd": totals.on_demand,
                "total_cost_usd": totals.seat_cost + totals.on_demand,
                "recommendation": action,
                "estimated_monthly_saving_usd": saving,
            }
        )
    return rows


def ledger_rows(
    users: dict[str, UserTotals],
    invoice_total: Decimal | None,
    invoice_tax: Decimal,
    invoice_credits: Decimal,
) -> list[dict[str, Any]]:
    seat_accrual = sum((t.seat_cost for t in users.values()), Decimal(0))
    on_demand = sum((t.on_demand for t in users.values()), Decimal(0))
    included = sum((t.included for t in users.values()), Decimal(0))
    modeled = seat_accrual + on_demand

    rows = [
        {
            "component": "seat_subscription_accrual",
            "amount_usd": seat_accrual,
            "source": "modeled from roster seat-days",
        },
        {
            "component": "on_demand_usage",
            "amount_usd": on_demand,
            "source": "sum of chargeable chargedCents",
        },
        {
            "component": "modeled_total",
            "amount_usd": modeled,
            "source": "seat accrual + on-demand usage",
        },
        {
            "component": "included_usage_consumed",
            "amount_usd": included,
            "source": "memo only, already paid for via seats",
        },
    ]

    if invoice_total is not None:
        variance = invoice_total - invoice_tax + invoice_credits - modeled
        rows.extend(
            [
                {
                    "component": "invoice_total",
                    "amount_usd": invoice_total,
                    "source": "invoice PDF",
                },
                {"component": "invoice_tax", "amount_usd": invoice_tax, "source": "invoice PDF"},
                {
                    "component": "invoice_credits",
                    "amount_usd": invoice_credits,
                    "source": "invoice PDF",
                },
                {
                    "component": "variance",
                    "amount_usd": variance,
                    "source": "invoice - tax + credits - modeled",
                },
            ]
        )
        if modeled > 0:
            rows.append(
                {
                    "component": "variance_percent",
                    "amount_usd": variance / modeled * 100,
                    "source": "variance / modeled_total",
                }
            )
    return rows


def parse_allowances(pairs: list[str]) -> dict[str, Decimal]:
    allowances: dict[str, Decimal] = {}
    for pair in pairs:
        if "=" not in pair:
            raise ValueError(f"--allowance expects seat_type=amount, got {pair!r}")
        seat_type, amount = pair.split("=", 1)
        allowances[seat_type.strip().lower()] = Decimal(amount.strip())
    return allowances


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seats", required=True, help="Seat cost CSV from cursor_seat_model.")
    parser.add_argument("--usage", help="Usage CSV from cursor_cost_export.")
    parser.add_argument("--invoice-total", type=Decimal, help="Invoice grand total in USD.")
    parser.add_argument("--invoice-tax", type=Decimal, default=Decimal(0))
    parser.add_argument(
        "--invoice-credits",
        type=Decimal,
        default=Decimal(0),
        help="Credits applied on the invoice, as a positive number.",
    )
    parser.add_argument(
        "--allowance",
        action="append",
        default=[],
        metavar="TYPE=USD",
        help="Included usage allowance per seat type, e.g. --allowance standard=20.",
    )
    parser.add_argument(
        "--infer-allowance",
        action="store_true",
        help="Estimate allowances from members who exhausted theirs and went on-demand.",
    )
    parser.add_argument(
        "--rate", action="append", default=[], metavar="TYPE=USD",
        help="Override a monthly seat rate, matching cursor_seat_model.",
    )
    parser.add_argument("--out", default="-", help="Ledger CSV path, or - for stdout.")
    parser.add_argument("--utilization", help="Optional per-user utilization CSV path.")
    parser.add_argument(
        "--variance-tolerance-percent",
        type=Decimal,
        default=Decimal("2"),
        help="Exit non-zero when variance exceeds this share of modeled cost.",
    )
    args = parser.parse_args(argv)

    try:
        rates = parse_rates(args.rate)
        allowances = parse_allowances(args.allowance)
        users = collect(args.seats, args.usage)
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.infer_allowance:
        inferred = infer_allowances(users)
        for seat_type, amount in sorted(inferred.items()):
            print(f"inferred {seat_type} allowance: ${amount:.2f}", file=sys.stderr)
        allowances = {**inferred, **allowances}

    rows = ledger_rows(users, args.invoice_total, args.invoice_tax, args.invoice_credits)
    write_csv(args.out, LEDGER_COLUMNS, rows)

    if args.utilization:
        if not allowances:
            print(
                "warning: no allowances supplied, so utilization and seat "
                "recommendations are omitted; pass --allowance or --infer-allowance",
                file=sys.stderr,
            )
        write_csv(
            args.utilization,
            UTILIZATION_COLUMNS,
            utilization_rows(users, allowances, rates),
        )

    for row in rows:
        print(f"{row['component']:<28} {format_value(row['amount_usd'])}", file=sys.stderr)

    variance = next(
        (row["amount_usd"] for row in rows if row["component"] == "variance_percent"), None
    )
    if variance is not None and abs(variance) > args.variance_tolerance_percent:
        print(
            f"variance {variance:.2f}% exceeds tolerance "
            f"{args.variance_tolerance_percent}%",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
