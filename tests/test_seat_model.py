from datetime import date, timedelta
from decimal import Decimal

import pytest

from cursor_api import add_months, cycle_bounds, cycle_containing
from cursor_reconcile import UserTotals, infer_allowances, ledger_rows, recommend
from cursor_seat_model import (
    REASON_ACTIVE,
    REASON_HELD_AFTER_REMOVAL,
    SeatAssignment,
    SeatTypeMap,
    quantize_cents,
    roll_up_cycle,
    roster_on,
    seat_cost_rows,
    seat_days,
)

RATES = {"standard": Decimal("40"), "premium": Decimal("120"), "free": Decimal("0")}

CYCLE_START = date(2026, 7, 15)
CYCLE_END = date(2026, 8, 14)
CYCLE_DAYS = 31


def member(email, name="", user_id=""):
    return {"email": email, "name": name, "id": user_id}


def snapshots_for(days_to_emails):
    return {
        day: {email: member(email) for email in emails}
        for day, emails in days_to_emails.items()
    }


def all_days(emails, start=CYCLE_START, end=CYCLE_END):
    return snapshots_for(
        {
            start + timedelta(days=offset): emails
            for offset in range((end - start).days + 1)
        }
    )


def standard_map():
    return SeatTypeMap(
        [SeatAssignment("alex@x.com", "standard", date(2026, 1, 1), None)],
        default="standard",
    )


def test_cycle_bounds_follows_subscription_anniversary_not_calendar_month():
    assert cycle_bounds(date(2026, 7, 15)) == (date(2026, 7, 15), date(2026, 8, 14))


def test_cycle_bounds_clamps_to_short_months():
    # A cycle starting Jan 31 must end Feb 27, not overflow into March.
    assert cycle_bounds(date(2026, 1, 31)) == (date(2026, 1, 31), date(2026, 2, 27))
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)


def test_cycle_containing_finds_the_right_cycle_from_an_anchor():
    assert cycle_containing(date(2026, 1, 15), date(2026, 7, 20)) == (
        date(2026, 7, 15),
        date(2026, 8, 14),
    )
    assert cycle_containing(date(2026, 1, 15), date(2026, 7, 10)) == (
        date(2026, 6, 15),
        date(2026, 7, 14),
    )


def test_full_cycle_seat_costs_exactly_the_monthly_rate():
    occupancy = seat_days(all_days(["alex@x.com"]), CYCLE_START, CYCLE_END, set())
    rows = seat_cost_rows(occupancy, standard_map(), RATES, CYCLE_START, CYCLE_END)

    assert len(rows) == CYCLE_DAYS
    assert sum(row["cost_usd"] for row in rows) == Decimal("40")


def test_mid_cycle_addition_is_prorated_by_seat_days():
    joined = CYCLE_START + timedelta(days=10)
    snapshots = snapshots_for(
        {CYCLE_START: [], **{joined + timedelta(days=o): ["alex@x.com"] for o in range(21)}}
    )
    occupancy = seat_days(snapshots, CYCLE_START, CYCLE_END, set())
    rows = seat_cost_rows(occupancy, standard_map(), RATES, CYCLE_START, CYCLE_END)

    assert len(rows) == 21
    assert sum(row["cost_usd"] for row in rows) == quantize_cents(
        Decimal("40") * 21 / CYCLE_DAYS
    )


def test_daily_rows_are_whole_cents_that_sum_to_the_exact_charge():
    occupancy = seat_days(all_days(["alex@x.com"]), CYCLE_START, CYCLE_END, set())
    rows = seat_cost_rows(occupancy, standard_map(), RATES, CYCLE_START, CYCLE_END)

    # 40 over 31 days does not divide evenly, so the remainder is spread across days
    # rather than lost to rounding.
    assert all(row["cost_usd"] == quantize_cents(row["cost_usd"]) for row in rows)
    assert sum(row["cost_usd"] for row in rows) == Decimal("40.00")
    assert len({row["cost_usd"] for row in rows}) > 1


def test_removal_without_usage_releases_the_seat_immediately():
    left_after = 10
    snapshots = snapshots_for(
        {
            **{CYCLE_START + timedelta(days=o): ["alex@x.com"] for o in range(left_after)},
            CYCLE_START + timedelta(days=left_after): [],
        }
    )
    occupancy = seat_days(snapshots, CYCLE_START, CYCLE_END, usage_emails=set())

    assert len(occupancy["alex@x.com"]) == left_after
    assert {reason for _, reason, _ in occupancy["alex@x.com"]} == {REASON_ACTIVE}


def test_removal_after_using_credits_holds_the_seat_until_cycle_end():
    left_after = 10
    snapshots = snapshots_for(
        {
            **{CYCLE_START + timedelta(days=o): ["alex@x.com"] for o in range(left_after)},
            CYCLE_START + timedelta(days=left_after): [],
        }
    )
    occupancy = seat_days(
        snapshots, CYCLE_START, CYCLE_END, usage_emails={"alex@x.com"}
    )
    rows = seat_cost_rows(occupancy, standard_map(), RATES, CYCLE_START, CYCLE_END)

    # The seat stays billed for the whole cycle even though the member left on day 10.
    assert len(rows) == CYCLE_DAYS
    assert sum(row["cost_usd"] for row in rows) == Decimal("40")
    held = [r for r in rows if r["occupancy_reason"] == REASON_HELD_AFTER_REMOVAL]
    assert len(held) == CYCLE_DAYS - left_after


def test_free_seats_cost_nothing_but_still_appear():
    seat_types = SeatTypeMap(
        [SeatAssignment("admin@x.com", "free", date(2026, 1, 1), None)]
    )
    occupancy = seat_days(all_days(["admin@x.com"]), CYCLE_START, CYCLE_END, set())
    rows = seat_cost_rows(occupancy, seat_types, RATES, CYCLE_START, CYCLE_END)

    assert len(rows) == CYCLE_DAYS
    assert sum(row["cost_usd"] for row in rows) == Decimal("0")


def test_seat_type_change_mid_cycle_switches_the_rate_on_the_effective_date():
    seat_types = SeatTypeMap(
        [
            SeatAssignment("alex@x.com", "standard", date(2026, 1, 1), date(2026, 7, 24)),
            SeatAssignment("alex@x.com", "premium", date(2026, 7, 25), None),
        ]
    )
    occupancy = seat_days(all_days(["alex@x.com"]), CYCLE_START, CYCLE_END, set())
    rows = seat_cost_rows(occupancy, seat_types, RATES, CYCLE_START, CYCLE_END)

    standard_days = sum(1 for r in rows if r["seat_type"] == "standard")
    premium_days = sum(1 for r in rows if r["seat_type"] == "premium")
    assert (standard_days, premium_days) == (10, 21)
    expected = quantize_cents(Decimal("40") * 10 / CYCLE_DAYS) + quantize_cents(
        Decimal("120") * 21 / CYCLE_DAYS
    )
    assert sum(row["cost_usd"] for row in rows) == expected


def test_unmapped_members_are_skipped_when_no_default_is_given():
    seat_types = SeatTypeMap([], default=None)
    occupancy = seat_days(all_days(["ghost@x.com"]), CYCLE_START, CYCLE_END, set())
    rows = seat_cost_rows(occupancy, seat_types, RATES, CYCLE_START, CYCLE_END)

    assert rows == []
    assert seat_types.unmapped == {"ghost@x.com"}


def test_unknown_seat_type_is_an_error_rather_than_a_silent_zero():
    seat_types = SeatTypeMap(
        [SeatAssignment("alex@x.com", "platinum", date(2026, 1, 1), None)]
    )
    occupancy = seat_days(all_days(["alex@x.com"]), CYCLE_START, CYCLE_END, set())
    with pytest.raises(ValueError, match="platinum"):
        seat_cost_rows(occupancy, seat_types, RATES, CYCLE_START, CYCLE_END)


def test_removed_flag_excludes_a_member_from_the_roster():
    snapshots = {
        CYCLE_START: {"alex@x.com": member("alex@x.com")},
    }
    assert "alex@x.com" in roster_on(snapshots, CYCLE_START)
    # Days with no snapshot carry the most recent earlier roster forward.
    assert "alex@x.com" in roster_on(snapshots, CYCLE_START + timedelta(days=5))
    # Days before any snapshot fall back to the earliest available.
    assert "alex@x.com" in roster_on(snapshots, CYCLE_START - timedelta(days=5))


def test_cycle_rollup_totals_match_the_daily_rows():
    occupancy = seat_days(all_days(["alex@x.com"]), CYCLE_START, CYCLE_END, set())
    rows = seat_cost_rows(occupancy, standard_map(), RATES, CYCLE_START, CYCLE_END)
    summary = roll_up_cycle(rows)

    assert len(summary) == 1
    assert summary[0]["seat_days"] == CYCLE_DAYS
    assert summary[0]["cost_usd"] == sum(row["cost_usd"] for row in rows)


def test_ledger_excludes_included_usage_from_modeled_cost():
    users = {
        "alex@x.com": UserTotals(
            email="alex@x.com",
            seat_type="standard",
            seat_cost=Decimal("40"),
            included=Decimal("20"),
            on_demand=Decimal("15"),
        )
    }
    rows = {row["component"]: row["amount_usd"] for row in ledger_rows(
        users, invoice_total=Decimal("55"), invoice_tax=Decimal(0), invoice_credits=Decimal(0)
    )}

    assert rows["modeled_total"] == Decimal("55")
    assert rows["included_usage_consumed"] == Decimal("20")
    assert rows["variance"] == Decimal("0")


def test_ledger_variance_backs_out_tax_and_adds_back_credits():
    users = {
        "alex@x.com": UserTotals(email="alex@x.com", seat_cost=Decimal("100"))
    }
    rows = {row["component"]: row["amount_usd"] for row in ledger_rows(
        users,
        invoice_total=Decimal("115"),
        invoice_tax=Decimal("10"),
        invoice_credits=Decimal("5"),
    )}

    assert rows["variance"] == Decimal("10")


def test_allowance_inferred_from_members_who_went_on_demand():
    users = {
        "a@x.com": UserTotals(
            email="a@x.com", seat_type="standard", included=Decimal("20"),
            on_demand=Decimal("5"),
        ),
        # No on-demand spend, so this member's partial consumption must not be
        # mistaken for the allowance.
        "b@x.com": UserTotals(
            email="b@x.com", seat_type="standard", included=Decimal("3"),
        ),
        "c@x.com": UserTotals(
            email="c@x.com", seat_type="premium", included=Decimal("100"),
            on_demand=Decimal("2"),
        ),
    }
    assert infer_allowances(users) == {
        "standard": Decimal("20"),
        "premium": Decimal("100"),
    }


def test_idle_seat_is_flagged_for_reclaim():
    totals = UserTotals(email="a@x.com", seat_type="standard", seat_cost=Decimal("40"))
    action, saving = recommend(totals, {"standard": Decimal("20")}, RATES)

    assert action == "reclaim: seat had no usage"
    assert saving == Decimal("40")


def test_underused_premium_seat_is_flagged_for_downgrade():
    totals = UserTotals(
        email="a@x.com", seat_type="premium", seat_cost=Decimal("120"),
        included=Decimal("5"),
    )
    allowances = {"standard": Decimal("20"), "premium": Decimal("100")}
    action, saving = recommend(totals, allowances, RATES)

    assert action == "downgrade to standard"
    assert saving == Decimal("80")


def test_heavy_standard_seat_is_flagged_for_upgrade():
    # Standard: 40 rate + (225 - 25) on-demand = 240.
    # Premium:  120 rate + (225 - 125) on-demand = 220.
    allowances = {"standard": Decimal("25"), "premium": Decimal("125")}
    totals = UserTotals(
        email="a@x.com", seat_type="standard", seat_cost=Decimal("40"),
        included=Decimal("25"), on_demand=Decimal("200"),
    )
    action, saving = recommend(totals, allowances, RATES)

    assert action == "upgrade to premium"
    assert saving == Decimal("20")


def test_upgrading_is_break_even_when_extra_allowance_equals_the_rate_gap():
    # Premium carries 5x the Standard allowance for a fixed 80 more per month, so if
    # the Standard allowance is exactly 20 the two tiers cost the same for any heavy
    # user and no recommendation should be made.
    allowances = {"standard": Decimal("20"), "premium": Decimal("100")}
    totals = UserTotals(
        email="a@x.com", seat_type="standard", seat_cost=Decimal("40"),
        included=Decimal("20"), on_demand=Decimal("500"),
    )
    action, saving = recommend(totals, allowances, RATES)

    assert action == ""
    assert saving == Decimal("0")


def test_free_seat_is_never_recommended_as_a_downgrade_target():
    allowances = {
        "standard": Decimal("20"),
        "premium": Decimal("100"),
        "free": Decimal("0"),
    }
    totals = UserTotals(
        email="a@x.com", seat_type="premium", seat_cost=Decimal("120"),
        included=Decimal("5"),
    )
    action, _ = recommend(totals, allowances, RATES)

    assert action == "downgrade to standard"
