# Cursor cost & usage export for Apptio

Pulls Cursor cost and usage data from the Cursor Admin API and writes a CSV that can be
loaded into Apptio Cloudability as a custom vendor cost & usage feed.

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
| `/teams/daily-usage-data` | POST | Activity metrics only — no dollar amounts. Max 30-day range. |
| `/teams/groups` | GET | Enterprise-only billing groups with `dailySpend[]` per cost center and `billingCycle` history. |
| `/organizations/pooled-usage` | POST | Enterprise-only budget vs. actual (`limitCents`, `usedCents`, `remainingCents`). |

## Caveats that matter for chargeback

- **Sum `chargedCents`, never `tokenUsage.totalCents`.** `chargedCents` already includes
  the Cursor Token Rate and any discount; `totalCents` is model cost before both.
- **`requestsCosts` is a count of request units, not money.**
- **Cents fields are floats, not integers** (`chargedCents: 21.36232`). Use decimal types
  and round only at final aggregation.
- **Seat subscriptions are not in any API.** Every dollar figure above is inference/usage
  spend. Per-seat charges come from Stripe invoices, which are manual PDF downloads from
  `cursor.com/dashboard/billing`. Model seat cost separately in Apptio.
- **`spendCents` vs `overallSpendCents`** on `/teams/spend`: the first is on-demand only,
  the second includes consumed-included usage.
- **Data is aggregated hourly.** Poll at most once per hour and treat the current hour as
  provisional. Ingest closed windows ending at `23:59:59.999`.
- **Both date bounds are inclusive**, with millisecond precision, so daily windows must end
  at `23:59:59.999` to avoid double-counting midnight events.
- **Service accounts and automations** carry a `userEmail` too. Branch on `serviceAccountId`
  or you will bill automation spend to a human.
- **User IDs are inconsistent across endpoints** (encoded string vs. number, and usage
  events carry only `userEmail`). Join on email.
- Rate limits are per team per minute: 60 for `/teams/filtered-usage-events`, 20 for most
  other Admin API routes. Back off at 1s, 2s, 4s, 8s, 16s.

## Usage

```bash
pip install -r requirements.txt
export CURSOR_API_KEY=crsr_...

# Daily rollup per user/model for a month
python cursor_cost_export.py --start 2026-07-01 --end 2026-07-31 --out july.csv

# Full per-event detail
python cursor_cost_export.py --start 2026-07-01 --granularity event --out events.csv
```

The script walks the range one UTC day at a time, paginates at 1000 rows per page, retries
`429`/`5xx` with exponential backoff, and joins `/teams/members` onto each event by email.
Per-day event counts and totals are written to stderr so the CSV on stdout stays clean.

### Output columns

The daily rollup groups on `usage_date`, `user_email`, `user_id`, `principal_type`,
`service_account_name`, `model` and `billing_kind`, and sums `event_count`,
`request_units`, token counts, `cursor_token_fee_usd` and `cost_usd`. Map `cost_usd` to
Cloudability's cost measure, `usage_date` to the billing date, and the user/model fields to
dimensions for business mapping.

Event granularity adds `usage_timestamp`, `cloud_agent_id`, `automation_id`, `max_mode`,
`is_chargeable`, `is_headless`, `model_cost_usd` and `discount_percent_off`.

## Reference

- [Admin API](https://cursor.com/docs/account/teams/admin-api)
- [API overview: auth, rate limits, errors](https://cursor.com/docs/api)
- [Organization API](https://cursor.com/docs/account/organizations/organization-admin-api)
