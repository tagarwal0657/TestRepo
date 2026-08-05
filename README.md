# OpenAI cost & usage APIs for Apptio TCO

How to pull **user**, **model**, **usage**, and **cost** from OpenAI into Apptio
(Cloudability / TBM). OpenAI does **not** have one endpoint that covers every
product (API Platform, ChatGPT Enterprise, Team/Plus subscriptions). Pick the
API by **how you buy OpenAI**.

## TL;DR — which product are you on?

| What you buy | Where you manage it | Programmatic cost/usage | Key type |
| --- | --- | --- | --- |
| **API Platform** (pay-per-token API) | [platform.openai.com](https://platform.openai.com) | **Yes** — Organization Costs + Usage Admin APIs | **Admin API key** (`Authorization: Bearer …`) |
| **ChatGPT Enterprise / Edu** | ChatGPT Global Admin Console | **Partial** — credit/spend via Cost API + Analytics UI/CSV; seat fees often invoice-only | Admin / Enterprise admin access |
| **ChatGPT Team** | ChatGPT workspace admin | **Limited** — Workspace Analytics CSV export; no full public cost API like Platform | Workspace owner/admin |
| **ChatGPT Plus / Pro (personal)** | chatgpt.com account billing | **No public org API** — Stripe/invoice only | n/a |

**Important:** “Enterprise”, “Platform”, and “ChatGPT” are **different billing systems**.
An API Platform Admin key does **not** automatically return ChatGPT seat spend, and
ChatGPT workspace analytics does **not** replace Platform `/organization/costs`.

---

## 1) API Platform (recommended for Apptio when you pay for API tokens)

This is the OpenAI equivalent of Anthropic’s Claude Console Admin Usage & Cost APIs.

### Auth

Create an **Admin API key** in Platform → Organization → Admin keys.

```http
Authorization: Bearer $OPENAI_ADMIN_KEY
Content-Type: application/json
```

Unlike Anthropic (`x-api-key`), OpenAI Admin endpoints use **Bearer** auth.
Regular project inference keys (`sk-…`) cannot call these endpoints (401).

Base URL: `https://api.openai.com`

### A. Costs API — authoritative USD

```http
GET /v1/organization/costs
```

| Param | Notes |
| --- | --- |
| `start_time` | **Required.** Unix seconds, inclusive |
| `end_time` | Unix seconds, exclusive |
| `bucket_width` | Only `1d` |
| `group_by` | `project_id`, `line_item`, `api_key_id` (any combo) |
| `project_ids` / `api_key_ids` | Optional filters |
| `limit` | 1–180 buckets (default 7) |
| `page` | Cursor from `next_page` |

```bash
# Example: daily costs by project + line item for Aug 2026
START=$(date -u -d '2026-08-01' +%s)
END=$(date -u -d '2026-09-01' +%s)

curl "https://api.openai.com/v1/organization/costs?\
start_time=${START}&end_time=${END}&bucket_width=1d&\
group_by=project_id&group_by=line_item&limit=31" \
  -H "Authorization: Bearer $OPENAI_ADMIN_KEY" \
  -H "Content-Type: application/json"
```

Response shape (per day bucket):

```json
{
  "object": "organization.costs.result",
  "amount": { "value": 0.06, "currency": "usd" },
  "line_item": "gpt-4o, input",
  "project_id": "proj_abc",
  "api_key_id": null,
  "quantity": 12345
}
```

- `amount.value` is already **USD dollars** (not cents).
- `line_item` is the billed SKU (often encodes model + token/product type).
- **No `user_id` and no structured `model` field** on Costs — only `line_item` / project / api_key.

Docs: [Costs](https://developers.openai.com/api/reference/resources/admin/subresources/organization/subresources/usage/methods/costs)

### B. Usage APIs — tokens + user + model

Usage is **split by capability**. Completions (Chat Completions + Responses) is usually most of the bill:

```http
GET /v1/organization/usage/completions
```

Other surfaces (call each if present on your invoice):

| Endpoint | Measures |
| --- | --- |
| `/v1/organization/usage/completions` | Chat/Responses tokens + requests |
| `/v1/organization/usage/embeddings` | Embedding tokens |
| `/v1/organization/usage/images` | Image generations |
| `/v1/organization/usage/audio_speeches` | TTS characters |
| `/v1/organization/usage/audio_transcriptions` | STT seconds |
| `/v1/organization/usage/moderations` | Moderation tokens |
| `/v1/organization/usage/vector_stores` | Storage bytes |
| `/v1/organization/usage/code_interpreter_sessions` | Sessions |
| `/v1/organization/usage/file_searches` | File search calls |
| `/v1/organization/usage/web_searches` | Web search calls |

Common query params for completions:

| Param | Notes |
| --- | --- |
| `start_time` | Required (Unix seconds) |
| `end_time` | Exclusive |
| `bucket_width` | `1m`, `1h`, or `1d` |
| `group_by` | **`model`**, **`user_id`**, `project_id`, `api_key_id`, `batch`, `service_tier` |
| `models` / `user_ids` / `project_ids` / `api_key_ids` | Filters |
| `batch` | `true` / `false` for Batch API traffic |
| `limit` / `page` | Pagination |

```bash
curl "https://api.openai.com/v1/organization/usage/completions?\
start_time=${START}&end_time=${END}&bucket_width=1d&\
group_by=user_id&group_by=model&group_by=project_id&group_by=api_key_id&\
limit=31" \
  -H "Authorization: Bearer $OPENAI_ADMIN_KEY" \
  -H "Content-Type: application/json"
```

If you omit `group_by`, fields like `model` / `user_id` come back **null**.

Docs / cookbook: [Completions Usage API cookbook](https://developers.openai.com/cookbook/examples/completions_usage_api)

### C. Users API — map `user_id` → email

```bash
curl "https://api.openai.com/v1/organization/users?limit=100" \
  -H "Authorization: Bearer $OPENAI_ADMIN_KEY"
```

Paginate with `after=<last_id>`. Join usage `user_id` → user email/name for Apptio chargeback.

Docs: [List users](https://developers.openai.com/api/reference/resources/admin/subresources/organization/subresources/users/methods/list)

### How to get ONE feed: user + model + usage + cost (Platform)

Same pattern as Anthropic Platform: **Costs owns dollars; Usage owns allocation keys.**

```text
1. Pull /organization/costs
     group_by=project_id,line_item   → invoice-grade USD by day/project/SKU

2. Pull /organization/usage/completions
     group_by=user_id,model,project_id,api_key_id
     (+ other usage/* endpoints for non-completion line items)

3. Pull /organization/users
     → email for each user_id

4. Allocate each cost bucket across matching usage rows
   (same day + project; match line_item ↔ model / product when possible)
   using token (or request) share:

     user_cost = cost_usd × (user_tokens / Σ tokens in bucket)

5. Largest-remainder rounding so Σ allocated == Σ costs exactly.
```

| You need | Simplest Platform approach |
| --- | --- |
| Invoice USD by project / SKU | **Costs only** (`group_by=project_id,line_item`) |
| User + model + tokens (no invoice lockstep) | **Usage only** (`group_by=user_id,model`) |
| User + model + invoice USD | **Costs + Usage allocate** (above) |
| Chargeback by app/team without users | Costs by `project_id` or `api_key_id` |

### Caveats (Platform)

- Costs API has **daily** buckets only; no native `user_id` / `model` group_by.
- `line_item` strings are the cost SKU — parse carefully (model names appear inside them, not as a separate field).
- Completions usage is most of spend for many orgs, but images/audio/embeddings/tools need their own usage endpoints if you want full model/user breakdown.
- Token × public price ≠ invoice (caching, batch, Scale Tier, credits). Prefer Costs for finance.
- Paginate with `next_page` until `has_more=false`.

---

## 2) ChatGPT Enterprise / Edu

This is **workspace credit / seat** spend for chatgpt.com (and related products like Codex in the workspace), **not** the same ledger as API Platform token bills — unless your contract explicitly unifies them.

### What OpenAI provides

| Need | How |
| --- | --- |
| Credit usage by user / product / model (UI) | Global Admin Console → Analytics (credit usage analytics) |
| CSV exports (users, GPTs, projects, activity) | Workspace Analytics → Export |
| Programmatic credit / estimated $ | OpenAI’s **unified Cost API** path for Enterprise credit data (same Cost API family; shows credit usage and, when overage rates exist, **estimated** $ — not a settled invoice substitute) |
| Usage limits / overages automation | Spend Controls / usage-limits APIs for workspace, group, user caps |
| Audit / investigation events | Compliance API (activity/audit — not a clean Apptio cost fact table) |
| Codex aggregated reporting | Codex Analytics API (Enterprise governance docs) |

References:

- [ChatGPT Enterprise spend controls announcement](https://openai.com/index/chatgpt-enterprise-spend-controls/)
- [Manage usage limits and overages](https://help.openai.com/en/articles/20001001-manage-usage-limits-and-overages-in-chatgpt-enterprise-and-edu)
- [Workspace analytics](https://help.openai.com/en/articles/10875114)
- [Enterprise governance (analytics vs compliance)](https://developers.openai.com/codex/enterprise/governance)

### Practical Apptio pattern (Enterprise ChatGPT)

1. **Seat / subscription fees** → load from invoice / contract (often **not** in Cost API).
2. **Usage credits / overages** → Cost API + Admin Console analytics/exports for user × model × product.
3. Treat Cost API dollar fields as **planning estimates** when they are derived from overage rates; reconcile to **Billing → Invoices** for finance-grade TCO.
4. If you also have API Platform spend, keep it as a **second vendor feed** (or second service dimension). Do not assume one key covers both.

---

## 3) ChatGPT Team

| Data | Availability |
| --- | --- |
| Adoption / user activity | Workspace Analytics UI + CSV export |
| Per-user model $ via public Admin Costs API | **Not equivalent to API Platform** — Team is seat-based; use billing invoices + analytics exports |
| Automated Apptio feed | Usually CSV export + invoice seat cost model (similar to Cursor seats) |

---

## 4) ChatGPT Plus / Pro (individual subscription)

No organization Admin Usage/Costs API. Cost is the monthly subscription on the personal billing page / card statement. For TCO, model as a fixed subscription cost in Apptio; there is no per-model usage API for personal Plus/Pro.

---

## Decision guide

```text
Do you pay OpenAI for API tokens (platform.openai.com)?
  YES → Admin key + /organization/costs + /organization/usage/* (+ /users)
        For user+model+USD: allocate Costs using Usage weights

Do you also have ChatGPT Enterprise / Edu seats or credits?
  YES → Separate feed: Admin Console analytics/CSV + Enterprise Cost/credit APIs
        Seat fees from invoice; credit/overage from analytics/Cost API

ChatGPT Team only?
  → Analytics CSV + invoice seats

ChatGPT Plus/Pro only?
  → Subscription invoice line item only
```

---

## Suggested Apptio columns (API Platform)

| Column | Source |
| --- | --- |
| `usage_date` | Cost/usage bucket `start_time` (UTC day) |
| `vendor` | `OpenAI` |
| `service` | `API Platform` / `ChatGPT Enterprise` / … |
| `project_id` | Costs / Usage `project_id` |
| `user_id` / `user_email` | Usage `user_id` + `/organization/users` |
| `api_key_id` | Costs/Usage when grouping by key |
| `model` | Usage `model` (or parsed from Costs `line_item`) |
| `line_item` | Costs `line_item` |
| `input_tokens` / `output_tokens` / … | Usage completions fields |
| `cost_usd` | Costs `amount.value` (allocated to user if needed) |
| `currency` | `amount.currency` (usually `usd`) |

---

## Official references

- [Admin APIs guide](https://developers.openai.com/api/docs/guides/admin-apis)
- [Organization Costs API](https://developers.openai.com/api/reference/resources/admin/subresources/organization/subresources/usage/methods/costs)
- [Usage + Costs cookbook](https://developers.openai.com/cookbook/examples/completions_usage_api)
- [List organization users](https://developers.openai.com/api/reference/resources/admin/subresources/organization/subresources/users/methods/list)
- [ChatGPT Enterprise spend controls](https://openai.com/index/chatgpt-enterprise-spend-controls/)
- [Workspace analytics (Enterprise/Edu)](https://help.openai.com/en/articles/10875114)
- [Usage limits & overages (Enterprise/Edu)](https://help.openai.com/en/articles/20001001-manage-usage-limits-and-overages-in-chatgpt-enterprise-and-edu)
