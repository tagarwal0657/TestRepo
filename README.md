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

## Recommendation 2: Claude Platform (API Console) — Usage & Cost Admin API

If the org is a **Claude Console / Platform** API customer (Messages API billing), use
the **Usage & Cost Admin API**. No single endpoint returns user + model + USD together
for general API traffic, so Apptio ingestion should **combine two endpoints**.

### A. User + model + tokens (usage)

```bash
curl "https://api.anthropic.com/v1/organizations/usage_report/messages?\
starting_at=2026-07-01T00:00:00Z&\
ending_at=2026-08-01T00:00:00Z&\
bucket_width=1d&\
group_by[]=account_id&\
group_by[]=model&\
group_by[]=api_key_id&\
group_by[]=workspace_id&\
group_by[]=service_tier" \
  -H "anthropic-version: 2023-06-01" \
  -H "x-api-key: $ANTHROPIC_ADMIN_KEY"
```

| Field | Notes |
| --- | --- |
| `account_id` | User id for OAuth/user-attributed traffic; `null` for many service/API-key-only calls |
| `model` | Model name |
| `api_key_id` | Useful fallback identity when `account_id` is null |
| `uncached_input_tokens`, `cache_*`, `output_tokens` | Usage quantities — **no USD** |

Map `account_id` → email via:

```text
GET /v1/organizations/users
```

(`id`, `email`, `name`, `role`). Map API keys via `GET /v1/organizations/api_keys`.

### B. Authoritative USD by model (cost)

```bash
curl "https://api.anthropic.com/v1/organizations/cost_report?\
starting_at=2026-07-01T00:00:00Z&\
ending_at=2026-08-01T00:00:00Z&\
group_by[]=description&\
group_by[]=workspace_id" \
  -H "anthropic-version: 2023-06-01" \
  -H "x-api-key: $ANTHROPIC_ADMIN_KEY"
```

When grouping by `description`, each row includes parsed `model`, `cost_type`,
`token_type`, `service_tier`, `amount` (cents as decimal string), `currency`.

**Cost `group_by` only supports `description` and `workspace_id`** — there is **no
`account_id` / user dimension on the Cost API**.

### Apptio join pattern (Platform)

1. Pull **cost_report** (`group_by[]=description`) → invoice-reconcilable USD by day/model/workspace.
2. Pull **usage_report/messages** (`group_by[]=account_id,model,...`) → allocation keys.
3. Allocate each model/day/workspace cost across users **pro-rata by token share**
   (or by weighted token type if you need closer pricing fidelity).
4. Attribute remaining / null-`account_id` spend to the API key or workspace owner
   (join `api_key_id` / `workspace_id`).

That gives Apptio user + model + cost even though Anthropic does not ship it pre-joined.

### Key caveats (Platform)

- Requires an **Admin API key** from Claude Console → Settings → Admin keys.
- Unavailable for individual (non-org) accounts; not available for Claude Platform on AWS.
- Cost endpoint: **daily buckets only** (`1d`). Priority Tier costs are **excluded** from
  cost_report — track Priority via usage `service_tier=priority` and price separately.
- Workbench usage often has `api_key_id = null`. Default workspace has `workspace_id = null`.
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

| Column | Source (Enterprise) | Source (Platform) |
| --- | --- | --- |
| `usage_date` | bucket `starting_at` | bucket `starting_at` |
| `vendor` | `Anthropic` | `Anthropic` |
| `user_email` | `actor.email` | users join on `account_id` |
| `user_id` | `actor.user_id` | `account_id` |
| `model` | `model` | `model` |
| `product` / `service` | `product` | workspace / Claude Code / Messages |
| `cost_usd` | `amount / 100` | allocated from `cost_report.amount` |
| `list_cost_usd` | `list_amount / 100` | n/a (or price from public rates) |
| `input_tokens` / `output_tokens` / … | `user_usage_report` | `usage_report/messages` |

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
