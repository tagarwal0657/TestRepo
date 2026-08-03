# Cursor cost & usage export for Apptio

Pulls Cursor cost and usage data from the Cursor Admin API, models the seat subscription
charge that no Cursor API exposes, and reconciles the two against an actual invoice so
the result can be loaded into Apptio Cloudability as a custom vendor cost feed.

---

## Why the API alone will never match your invoice

A Cursor Teams invoice has two components, and the API only reports one of them:

```
Invoice = seat subscription (prorated) + on-demand usage (in arrears) + tax - credits
```

Every dollar figure returned by the Admin API is **inference/usage spend**. Seat charges
of $40/user/month (Standard) and $120/user/month (Premium) appear nowhere in any
endpoint. Missing them understates cost.

At the same time, naively summing every `chargedCents` from the usage events
**overstates** usage, because that sum includes consumption drawn from the allowance
already bundled into the seat fee. You paid for that in the seat charge; counting it
again double-books it.

The two errors run in opposite directions and partially cancel, which is why the
variance tends to look erratic rather than like a single clean missing line item.

### The distinction that fixes it

`/teams/spend` exposes both sides:

| Field | Meaning |
| --- | --- |
| `spendCents` | On-demand only. **This is the cash number that reaches the invoice.** |
| `overallSpendCents` | On-demand *plus* consumed included usage. Economic consumption, not cash. |

The same split appears on usage events as `kind` (`Usage-based` vs `Included in
Business`) and the `isChargeable` boolean. Note that the documented sample payload
contains a non-chargeable event that still carries `chargedCents: 8` — a populated
`chargedCents` does **not** mean money changed hands.

`cursor_cost_export.py` therefore emits three separate cost measures that must never be
summed together:

| Column | Use |
| --- | --- |
| `chargeable_cost_usd` | On-demand spend. The only part that hits the invoice. |
| `included_cost_usd` | Allowance consumption. Load as a **$0-cost quantity metric**, not as cost. |
| `consumption_usd` | The two combined, for showback of total economic consumption. |

---

## Modeling the seat subscription

Cursor bills per **active** seat with proration, so the unit of account is seat-days, not
headcount at month end:

```
seat cost = (monthly rate / days in billing cycle) x days the seat was occupied
```

`cursor_seat_model.py` implements this. Three billing rules drive the occupancy
calculation, and all three are covered by tests in `tests/test_seat_model.py`:

1. **Adding a member mid-cycle creates a pro-rated charge** from the day they were
   added, so partial months are real and must be counted in days.
2. **Removing a member who consumed usage keeps their seat occupied until the cycle
   ends.** Their billed end date is the cycle boundary, not their removal date. This is
   the single most common source of under-modeling. The model detects it by checking the
   usage export for any consumption by that member during the cycle, which is why
   `--usage` should always be supplied.
3. **Unpaid Admin seats are free**, and seat type is independent of team role, so a free
   seat cannot be identified from the API.

Two further details matter for tie-out:

- **Cycles follow the subscription anniversary, not the calendar month.** Derive the
  cycle start from `subscriptionCycleStart` on `/teams/spend`. Booking against calendar
  months puts a few days in the wrong period every cycle. `cycle_bounds` also clamps
  short months, so a cycle starting Jan 31 ends Feb 27 rather than overflowing.
- **Daily amounts are allocated cumulatively, not by dividing.** $40 over 31 days does
  not divide evenly, so the model takes the difference between successive rounded
  cumulative totals. Daily rows are therefore whole cents that sum to exactly the
  prorated charge.

### Reconstructing the roster

The seat model needs to know who held a seat on each day, which requires history that
`/teams/members` does not provide — it only reports the present.

```bash
# Run daily from cron. Appends one roster snapshot per day.
python cursor_seat_model.py snapshot --out roster.jsonl

# One-time: rebuild history by replaying audit log membership events backwards.
python cursor_seat_model.py backfill --start 2026-01-01 --end 2026-07-31 --out roster.jsonl
```

`backfill` reads `add_user`, `remove_user` and `update_user_role` events from
`/teams/audit-logs`, chunked to respect the 30-day per-request cap, and replays them in
reverse from today's roster. Use it to seed history, then rely on daily snapshots, which
are more reliable. Days with no snapshot carry the most recent earlier roster forward, so
a partial history degrades into an approximation rather than silently dropping seats.

### The seat type gap

**Seat type is not returned by any endpoint** and the docs state explicitly that it is
independent of `role`, so `/teams/members` cannot tell you who is on Premium. It has to
come from a mapping table you maintain from procurement records or invoice line items.
See `seat_types.example.csv`:

```csv
email,seat_type,effective_from,effective_to
alex@company.com,standard,2026-01-01,
jordan@company.com,standard,2026-01-01,2026-06-30
jordan@company.com,premium,2026-07-01,
finance-admin@company.com,free,2026-01-01,
```

Effective dates let a mid-cycle upgrade switch rates on the correct day. Members missing
from the table are skipped with a warning unless `--default-seat-type` is passed, and an
unrecognized seat type is a hard error rather than a silent zero.

There is a way to cross-check the mapping from data alone. A member who incurred
on-demand spend has by definition exhausted their allowance, so their consumed included
usage *equals* that allowance. Across the team these values cluster into tiers, which
both reveals the Standard allowance in dollars (not published anywhere) and indicates who
is on Premium. `cursor_reconcile.py --infer-allowance` computes this. Treat it as a
cross-check against your contract, not as authoritative.

---

## The reconciliation ledger

```
modeled  = seat subscription accrual + on-demand usage
variance = invoice total - tax + credits - modeled
```

`cursor_reconcile.py` produces this ledger and exits non-zero when variance exceeds a
tolerance (2% by default), which makes it usable as a monthly CI check.

**Variance is expected to be small but non-zero, and should be tracked as a standing line
rather than forced to zero.** Cursor applies billing adjustments as account credit on a
*future* invoice, so a mid-cycle downgrade produces a genuine timing difference between
accrual and cash that no amount of modeling removes. Alert on drift, not on any nonzero
value. Persistent drift almost always traces back to a seat-type misclassification or a
removed-member seat released too early.

Included usage appears in the ledger as a memo line only. It is never added to modeled
cost.

---

## Loading into Apptio Cloudability

Keep these as distinct streams so the cost total stays invoice-faithful:

| Stream | Source | Loaded as |
| --- | --- | --- |
| `Cursor / Seat Subscription` | `cursor_seat_model.py cost` | Cost. One row per user per day, dimensioned by user, seat type, cost center. |
| `Cursor / On-Demand Usage` | `chargeable_cost_usd` from the export | Cost. Dimensioned by user, model, `kind`, service account. |
| `Cursor / Adjustments` | Invoice PDF | Cost. Credits, tax, proration true-ups. Usually one unallocated row per month. |
| Included usage consumed | `included_cost_usd` from the export | **Quantity metric at $0 cost.** Never as cost. |

Both tools emit `vendor`, `service` and `charge_type` columns so the streams can share a
single custom-vendor feed and still be separated by business mapping rules.

Since Teams plans do not have billing groups (Enterprise only), the email to cost-centre
mapping has to be your own table joined on `user_email`. Watch out for service accounts
and automations: they carry a `userEmail` too, so branch on `principal_type` or bot spend
lands on a human's cost center. Service accounts consume usage but do **not** occupy a
seat, so they legitimately appear in the usage stream with no matching seat row.

---

## Seat utilization and rightsizing

This is the analysis that justifies the whole integration, and it is only possible once
the seat line is modeled. Included usage is **per-user, non-transferable, and resets each
cycle** — one person's underuse cannot offset another's overage.

`cursor_reconcile.py --utilization` compares each member's consumed included usage
against their seat's allowance and recommends an action:

- **Idle seat** — a paid seat with no usage at all. Reclaim it. This waste is completely
  invisible if you only look at on-demand spend, because a fully wasted seat generates
  exactly $0 of usage-based cost.
- **Underused Premium** — consuming less than the Standard allowance. Downgrading saves
  the $80/month rate difference outright.
- **Heavy Standard** — upgrading converts on-demand spend into included allowance. The
  saving is `(current rate + current on-demand) - (candidate rate + candidate on-demand)`.

Note the break-even: Premium carries 5x the Standard allowance for a fixed $80/month
more. If the Standard allowance is exactly $20, then Premium buys $80 of extra allowance
for $80 — dead even for any heavy user, and the tool correctly makes no recommendation.
Whether upgrading ever wins depends on the real allowance figure, which is why deriving
it via `--infer-allowance` or your contract matters. Free seats are never offered as a
downgrade target, since Unpaid Admins have no Cursor access.

---

## Validation order

Pick one **closed** billing cycle and check these in order. Whichever breaks first
localizes the problem:

1. Sum of chargeable `chargedCents` equals the sum of `spendCents` across members.
2. That sum matches the usage line on the invoice.
3. Modeled seat-days times your rate table matches the subscription line.

The docs say to sum `chargedCents` to reconcile against "`/teams/spend` totals" without
saying which of the two fields. The mapping used here (all events ↔ `overallSpendCents`,
chargeable-only ↔ `spendCents`) is the natural reading, but confirm it on your own tenant
before trusting it in production.

**Snapshot `/teams/spend` daily**, and especially right before the cycle boundary. It has
no date parameters and only ever returns the open cycle, so once it rolls over that
per-user split is gone permanently and you would be rebuilding it from events.

---

## Which Cursor API to use

Everything lives on `https://api.cursor.com` and authenticates with HTTP Basic, using the
API key as the username and an **empty password** (note the trailing colon):

```bash
curl https://api.cursor.com/teams/members -u YOUR_API_KEY:
```

Create the key as a team admin at [cursor.com/dashboard/api](https://cursor.com/dashboard/api).
It is shown once, and needs the `admin:*` scope.

| Endpoint | Method | What it gives you |
| --- | --- | --- |
| `/teams/filtered-usage-events` | POST | Per-request events with `chargedCents`, model, tokens, and user email. **The primary cost feed.** |
| `/teams/spend` | POST | Per-user spend for the *current billing cycle only*. No date range parameter. |
| `/teams/members` | GET | Team roster (`id`, `email`, `name`, `role`, `isRemoved`) for cost allocation. |
| `/teams/audit-logs` | GET | Membership timeline via `add_user`, `remove_user`, `update_user_role`. Max 30-day range. |
| `/teams/daily-usage-data` | POST | Activity metrics only — no dollar amounts. Max 30-day range. |
| `/teams/groups` | GET | Enterprise-only billing groups with `dailySpend[]` per cost center. |
| `/organizations/pooled-usage` | POST | Enterprise-only budget vs. actual (`limitCents`, `usedCents`, `remainingCents`). |

### Other caveats

- **Sum `chargedCents`, never `tokenUsage.totalCents`.** `chargedCents` already includes
  the Cursor Token Rate and any discount; `totalCents` is model cost before both.
- **`requestsCosts` is a count of request units, not money.**
- **Cents fields are floats, not integers** (`chargedCents: 21.36232`). Everything here
  uses `Decimal` and rounds only at final aggregation.
- **Data is aggregated hourly.** Poll at most once per hour and treat the current hour as
  provisional. Ingest closed windows ending at `23:59:59.999`.
- **Both date bounds are inclusive**, at millisecond precision, so daily windows must end
  at `23:59:59.999` to avoid double-counting midnight events.
- **User IDs are inconsistent across endpoints** (encoded string vs. number, and usage
  events carry only `userEmail`). Join on email.
- Rate limits are per team per minute: 60 for `/teams/filtered-usage-events`, 20 for most
  other Admin API routes. Both tools back off at 1s, 2s, 4s, 8s, 16s.

---

## Usage

```bash
pip install -r requirements.txt
export CURSOR_API_KEY=crsr_...
```

**Daily**, from cron:

```bash
python cursor_seat_model.py snapshot --out roster.jsonl
python cursor_cost_export.py --start 2026-07-15 --end 2026-08-14 --out usage.csv
```

**Per cycle**, to produce the feed and reconcile it:

```bash
python cursor_seat_model.py cost \
    --cycle-start 2026-07-15 \
    --snapshots roster.jsonl \
    --seat-types seat_types.csv \
    --usage usage.csv \
    --out seats.csv

python cursor_reconcile.py \
    --seats seats.csv \
    --usage usage.csv \
    --invoice-total 12450.88 --invoice-tax 0 --invoice-credits 240.00 \
    --infer-allowance \
    --out ledger.csv \
    --utilization utilization.csv
```

Both tools write progress and totals to stderr so the CSV on stdout stays clean, and
`--out -` streams to stdout.

### Tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

---

## Reference

- [Admin API](https://cursor.com/docs/account/teams/admin-api)
- [Team pricing: seats, included usage, proration](https://cursor.com/docs/account/teams/pricing)
- [API overview: auth, rate limits, errors](https://cursor.com/docs/api)
- [Organization API](https://cursor.com/docs/account/organizations/organization-admin-api)
