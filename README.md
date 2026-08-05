# Anthropic cost & usage APIs for Apptio TCO

Recommendation for pulling **user**, **model**, and **cost** data from Anthropic into
Apptio (Cloudability / TBM) for Total Cost of Ownership and chargeback.

Anthropic does **not** expose one universal endpoint that always returns user + model +
USD together. Which API you use depends on how your org buys Claude.

## TL;DR — pick by organization type

| Your Anthropic product | Best API for Apptio (user + model + cost) | Auth |
| --- | --- | --- |
| **Claude Enterprise** (claude.ai seats / usage credits) | **`GET /v1/organizations/analytics/user_cost_report`** with `group_by[]=model` | Analytics API key (`read:analytics`) |
| **Claude Platform / Console** (API org, Messages API spend) | Combine **Usage** + **Cost** Admin APIs (see below) | Admin API key (`sk-ant-admin01-...`) |
| **Claude Code only** (Platform org) | **`GET /v1/organizations/usage_report/claude_code`** | Admin API key |

Base URL for all of these: `https://api.anthropic.com`

Required headers:

```http
x-api-key: <key>
anthropic-version: 2023-06-01
```

---

## Recommendation 1 (best fit when available): Claude Enterprise Analytics

If the org is on **Claude Enterprise**, use the **Claude Enterprise Analytics API**.
This is the only Anthropic surface that returns **per-user USD cost** with optional
**model** breakout in a single call — closest analogue to Cursor’s
`/teams/filtered-usage-events` for Apptio.

### Primary endpoint (cost by user + model)

```bash
curl "https://api.anthropic.com/v1/organizations/analytics/user_cost_report?\
starting_at=2026-07-01T00:00:00Z&\
ending_at=2026-08-01T00:00:00Z&\
bucket_width=1d&\
group_by[]=model&\
group_by[]=product&\
limit=1000" \
  -H "anthropic-version: 2023-06-01" \
  -H "x-api-key: $ANTHROPIC_ANALYTICS_KEY"
```

Returns one row per user (actor), with:

| Field | Apptio use |
| --- | --- |
| `actor.user_id` / `actor.email` / `actor.name` | User dimension / chargeback identity |
| `model` (when `group_by[]=model`) | Model dimension |
| `product` (when `group_by[]=product`) | Product surface (chat, claude_code, cowork, …) |
| `amount` | **Post-discount cost in fractional cents** (parse as decimal, ÷ 100 → USD) |
| `list_amount` | Pre-discount list price (cents) |
| `starting_at` / `ending_at` | Billing date window |
| `requests` | Request count (null if also grouping by `cost_type` / `token_type`) |

### Companion endpoint (tokens by user + model)

```bash
curl "https://api.anthropic.com/v1/organizations/analytics/user_usage_report?\
starting_at=2026-07-01T00:00:00Z&\
ending_at=2026-08-01T00:00:00Z&\
bucket_width=1d&\
group_by[]=model&\
limit=1000" \
  -H "anthropic-version: 2023-06-01" \
  -H "x-api-key: $ANTHROPIC_ANALYTICS_KEY"
```

Use this when Apptio also needs usage quantities (tokens), not only dollars.

### Org-wide reconciliation (includes API-key / automation traffic)

`user_cost_report` only includes **seat-user attributable** spend. For invoice-level
totals that also include direct API-key and automation traffic:

```text
GET /v1/organizations/analytics/cost_report?group_by[]=model&group_by[]=product
```

### Key caveats (Enterprise)

- Key is created in **claude.ai → Organization settings → API** (Analytics key), **not**
  the Console Admin key. Keys are not interchangeable.
- Cost/usage data typically refreshes within ~4 hours; values can revise for up to
  **30 days**. For invoicing-grade TCO, prefer windows ≥ 30 days old, or pin
  `ending_at` to a previously returned `data_refreshed_at`.
- `amount` is a **decimal string in cents** (e.g. `"41280.000000"` = $412.80). Use
  decimal types; divide by 100 for USD.
- Date floor: data available on/after **2026-01-01**. Max range per call: **31 days**.
- Seat-based Enterprise plans: cost endpoints reflect **usage credits**, not seat fees.
  Model seat/subscription cost separately in Apptio (same pattern as Cursor seats).

Docs: [Analytics APIs](https://platform.claude.com/docs/en/manage-claude/analytics-api),
[Admin analytics reference](https://platform.claude.com/docs/en/api/admin/analytics).

---

## Recommendation 2: Claude Platform (API Console) — combine Cost + Usage

If the org is a **Claude Console / Platform** API customer (Messages API billing),
Anthropic does **not** return user + model + USD on one endpoint. You must combine:

| Role | Endpoint | Grain | Has USD? | Has user? |
| --- | --- | --- | --- | --- |
| **Money (source of truth)** | `GET /v1/organizations/cost_report` | day × workspace × model × **token_type** | Yes | **No** |
| **Allocation keys** | `GET /v1/organizations/usage_report/messages` | day × user × model × … (wide token columns) | No | Yes |
| **Email** | `GET /v1/organizations/users` | user id → email | — | — |

The Cost API is intentionally small (one row per model/token_type line item).
The Usage API is large (thousands of rows once you group by `account_id`). That is
expected: **Cost defines the dollars; Usage only splits those dollars across users.**

### Why a naive row-join fails

- Cost is **long** (one `token_type` per row: `uncached_input_tokens`, `output_tokens`,
  `cache_read_input_tokens`, `cache_creation.ephemeral_5m_input_tokens`, …).
- Usage is **wide** (all token counts on one row per user/model).
- Cost has **no `account_id`**. You cannot `JOIN … ON user`.
- So reconciliation is **allocation**, not an equi-join on user.

### Perfect reconcile algorithm (what the exporter does)

```text
For each cost_report row C  (day, workspace, model, token_type, service_tier, …, amount):
  1. Find matching usage rows U where dimensions equal C
     (day, workspace_id, model, service_tier, context_window, inference_geo).
  2. Unpivot each U: tokens_i = U[C.token_type]   # map wide → long
  3. denom = Σ tokens_i  (skip users with 0 for that token_type)
  4. cost_i = C.amount_usd × (tokens_i / denom)
     Use largest-remainder rounding so Σ cost_i == C.amount_usd exactly.
  5. Emit one output row per user with:
     user_id, user_email, model, token_type, tokens, cost_usd, …

If no matching usage (e.g. code_execution):
  emit one unallocated row so invoice dollars are never dropped.
```

**Reconcile check (must pass):**

```text
Σ output.cost_usd  ==  Σ cost_report.amount_usd
```

Usage row count does **not** need to equal cost row count. Thousands of usage rows
are fine; they only contribute allocation weights inside each cost bucket. After
allocation, output grain is roughly:

```text
day × user × model × token_type   (plus workspace / tier / …)
```

which is larger than cost_report but far smaller than raw request logs.

### Exact API calls

**Cost (authoritative USD by model + token_type):**

```bash
curl "https://api.anthropic.com/v1/organizations/cost_report?\
starting_at=2026-07-01T00:00:00Z&\
ending_at=2026-08-01T00:00:00Z&\
bucket_width=1d&\
group_by[]=description&\
group_by[]=workspace_id" \
  -H "anthropic-version: 2023-06-01" \
  -H "x-api-key: $ANTHROPIC_ADMIN_KEY"
```

`group_by[]=description` expands each line into parsed `model`, `token_type`,
`cost_type`, `service_tier`, `context_window`, `inference_geo`, `amount`.

**Usage (users + token weights — group to the SAME dims as cost, plus user):**

```bash
curl "https://api.anthropic.com/v1/organizations/usage_report/messages?\
starting_at=2026-07-01T00:00:00Z&\
ending_at=2026-07-02T00:00:00Z&\
bucket_width=1d&\
group_by[]=account_id&\
group_by[]=api_key_id&\
group_by[]=model&\
group_by[]=workspace_id&\
group_by[]=service_tier&\
group_by[]=context_window&\
group_by[]=inference_geo&\
limit=1" \
  -H "anthropic-version: 2023-06-01" \
  -H "x-api-key: $ANTHROPIC_ADMIN_KEY"
```

Walk **one UTC day at a time** and follow `next_page` until `has_more=false`.
Do not request the whole month in one usage call when grouping by user — paginate
by day to keep each response bounded.

**Users (email join):**

```bash
curl "https://api.anthropic.com/v1/organizations/users?limit=1000" \
  -H "anthropic-version: 2023-06-01" \
  -H "x-api-key: $ANTHROPIC_ADMIN_KEY"
```

Join `usage.account_id` → `users.id` → `email`. If `account_id` is null (common for
service traffic), keep `api_key_id` as the chargeback principal.

### Token-type mapping (wide usage → long cost)

| `cost_report.token_type` | Usage field |
| --- | --- |
| `uncached_input_tokens` | `uncached_input_tokens` |
| `output_tokens` | `output_tokens` |
| `cache_read_input_tokens` | `cache_read_input_tokens` |
| `cache_creation.ephemeral_5m_input_tokens` | `cache_creation.ephemeral_5m_input_tokens` |
| `cache_creation.ephemeral_1h_input_tokens` | `cache_creation.ephemeral_1h_input_tokens` |
| `cost_type=web_search` | `server_tool_use.web_search_requests` |
| `cost_type=code_execution` | *(not in usage API — leave unallocated)* |

### Worked example

Cost API returns one line:

| day | model | token_type | amount |
| --- | --- | --- | --- |
| 2026-07-01 | claude-sonnet-4 | uncached_input_tokens | $3.00 |

Usage API returns two users for that same day/model/tier:

| user | uncached_input_tokens |
| --- | --- |
| alice@co | 100 |
| bob@co | 200 |

Allocated output (one file, all fields):

| user | model | token_type | tokens | cost_usd |
| --- | --- | --- | --- | --- |
| alice@co | claude-sonnet-4 | uncached_input_tokens | 100 | **1.00** |
| bob@co | claude-sonnet-4 | uncached_input_tokens | 200 | **2.00** |

`1.00 + 2.00 = 3.00` → matches cost_report exactly. Repeat for every cost line
(output tokens, cache writes, web search, …).

### Run the exporter

```bash
pip install -r requirements.txt
export ANTHROPIC_ADMIN_KEY=sk-ant-admin01-...

python3 anthropic_platform_cost_export.py \
  --start 2026-07-01 --end 2026-08-01 \
  --out anthropic_july.csv
```

Output columns: `usage_date`, `user_id`, `user_email`, `model`, `token_type`,
`tokens`, `cost_usd`, plus workspace / tier / `principal_type` / `allocation_share`.
The script prints a reconcile summary and exits non-zero if
`Σ cost_usd ≠ Σ cost_report`.

### Key caveats (Platform)

- Requires an **Admin API key** from Claude Console → Settings → Admin keys.
- Unavailable for individual (non-org) accounts; not available for Claude Platform on AWS.
- Cost endpoint: **daily buckets only** (`1d`).
- **Priority Tier** costs are excluded from `cost_report` — track via usage
  `service_tier=priority` and price separately; they will not appear in allocated USD.
- Workbench usage often has `api_key_id = null`. Default workspace has `workspace_id = null`.
- Null `account_id` is normal for pure API-key traffic — charge back on `api_key_id`.
- Data usually appears within ~5 minutes; poll ≤ 1/minute for sustained use.

Docs: [Usage and Cost API](https://platform.claude.com/docs/en/manage-claude/usage-cost-api).

---

## Recommendation 3: Claude Code–only shortcut (Platform)

If the TCO scope is **Claude Code developers only** (not general Messages API traffic):

```bash
curl "https://api.anthropic.com/v1/organizations/usage_report/claude_code?\
starting_at=2026-07-15&\
limit=1000" \
  -H "anthropic-version: 2023-06-01" \
  -H "x-api-key: $ANTHROPIC_ADMIN_KEY"
```

One day per call. Each row includes:

- `actor.email_address` (or API key name)
- `model_breakdown[].model`
- `model_breakdown[].estimated_cost.amount` (cents) + `currency`
- token breakdowns and productivity metrics

Anthropic documents this as the preferred path for **per-user Claude Code cost** without
exploding the Usage API across many API keys. Costs here are **estimated**, not the same
as Console invoice lines — reconcile totals against `cost_report` if finance needs lockstep.

Docs: [Claude Code Analytics API](https://platform.claude.com/docs/en/manage-claude/claude-code-analytics-api).

---

## What to load into Apptio

Suggested daily fact grain for Cloudability custom vendor feed:

| Column | Source (Enterprise) | Source (Platform exporter) |
| --- | --- | --- |
| `usage_date` | bucket `starting_at` | cost/usage bucket day |
| `vendor` | `Anthropic` | `Anthropic` |
| `user_email` | `actor.email` | `users` join on `account_id` |
| `user_id` | `actor.user_id` | `account_id` (else `api_key_id`) |
| `model` | `model` | `model` |
| `token_type` | optional `group_by[]=token_type` | from `cost_report.token_type` |
| `tokens` | usage report | unpivoted usage count for that token_type |
| `product` / `service` | `product` | `Claude Platform API` / workspace |
| `cost_usd` | `amount / 100` | allocated share of `cost_report.amount` |
| `list_cost_usd` | `list_amount / 100` | n/a on Platform Admin API |

Map `cost_usd` to Cloudability’s cost measure; map user/model/product to business
dimensions for TBM chargeback.

---

## Decision guide

```text
Is this a Claude Enterprise (claude.ai) org?
  YES → Use analytics/user_cost_report?group_by[]=model   ★ best for Apptio TCO
  NO  → Is spend mostly Claude Code?
          YES → usage_report/claude_code (email + model + estimated_cost)
          NO  → usage_report/messages (user+model+tokens)
                + cost_report (USD by model)
                + organizations/users (email join)
                + pro-rata allocation
```

## Official references

- [Usage and Cost API](https://platform.claude.com/docs/en/manage-claude/usage-cost-api)
- [Claude Enterprise Analytics API](https://platform.claude.com/docs/en/manage-claude/analytics-api)
- [Get Cost Report (Platform)](https://platform.claude.com/docs/en/api/admin/cost_report/retrieve)
- [Messages Usage Report](https://platform.claude.com/docs/en/api/admin/usage_report)
- [List Users](https://platform.claude.com/docs/en/api/admin/users/list)
- [Pricing](https://platform.claude.com/docs/en/about-claude/pricing)
