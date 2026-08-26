# Verifying an integration pack release across OEM sub-accounts

This note records the Boomi Platform API behaviour the tool depends on, so the logic
can be reviewed without re-reading the API reference.

## The problem

A master (OEM) account releases an integration pack. The release request reports its
own success, but that is a statement about the *push*, not about each tenant. A
sub-account can lag behind because it was suspended during the release window, because
the pack was never installed there, or because only some environments were redeployed.
Confirming the rollout therefore needs two different objects read from two different
account contexts.

## Step 1 — what the release pushed (parent account)

```
GET /api/rest/v1/{masterAccountId}/ReleaseIntegrationPackStatus/{requestId}
```

`{requestId}` is the value returned by `ReleaseIntegrationPack`, also visible on the
Integration Packs page.

Status code is part of the contract:

| Code | Meaning |
| --- | --- |
| `202` | The release is `IN_PROGRESS` or `SCHEDULED`; poll again |
| `200` | Released details are available |

Fields consumed:

- `releaseStatus` — `IN_PROGRESS`, `SUCCESS`, `SCHEDULED`, `ERROR`
- `releaseProgress` — percentage, present only while `IN_PROGRESS`
- `integrationPackId`, `installationType` (`SINGLE` / `MULTI`)
- `ReleasePackagedComponents.ReleasePackagedComponent[]` — each entry carries
  `componentId` and `releasedVersion`, and sometimes `version` (the component's
  revision number, distinct from the packaged version)

Two parsing details matter. Boomi collapses a single-entry collection into a bare
object rather than a one-element array, so the parser accepts both shapes. And
`releasedVersion` is the packaged-component version to compare against; `version` is
the build-tab revision and is reported separately rather than used for matching.

Handling of `SCHEDULED`: the tool refuses to poll. A scheduled release has not pushed
anything, so blocking a verification run until a future calendar date would hang CI for
an arbitrary period. It exits with a clear message instead.

## Step 2 — what each sub-account runs (account override)

```
POST /api/rest/v1/{subAccountId}/DeployedPackage/query
```

```json
{
  "QueryFilter": {
    "expression": {
      "operator": "and",
      "nestedExpression": [
        { "property": "componentId", "operator": "EQUALS", "argument": ["<componentId from step 1>"] },
        { "property": "active",      "operator": "EQUALS", "argument": ["true"] }
      ]
    }
  }
}
```

One query runs per released component. Fields consumed: `packageVersion` (the
user-defined packaged-component version — this is the deployed version), plus
`componentVersion`, `environmentId`, `deploymentId`, `deployedDate` and `active` for
context in the report.

### Account override

Two supported forms:

- **Platform API** (default): the sub-account ID replaces the account segment of the
  path. The authenticated user must be able to reach that sub-account.
- **Partner API** (`--partner-api`): the authenticated account stays in the path and
  the target is passed as `?overrideAccount=<subAccountId>` against
  `/partner/api/rest/v1/...`.

### Paging

QUERY responses cap at 100 results and include a `queryToken` when more exist. The
follow-up goes to `{object}/queryMore` with the token as the **raw request body** and
`Content-Type: text/plain` — not JSON. Tokens are single-use and time-sensitive, so
paging is sequential per query.

## Step 3 — optional install confirmation

```
POST /api/rest/v1/{subAccountId}/IntegrationPackInstance/query
{"QueryFilter": {"expression": {"property": "integrationPackId", "operator": "EQUALS", "argument": ["<integrationPackId>"]}}}
```

Enabled with `--check-instances`. Its only purpose is to separate `NOT_INSTALLED` from
`NOT_DEPLOYED`, which are very different operational problems. It costs one extra call
per sub-account, so it is off by default.

## Step 4 — comparison

For each sub-account and each released component:

1. Keep active deployments only (unless `--include-inactive-deployments`).
2. Sort by `deployedDate`, newest first.
3. `MATCH` if any active deployment's `packageVersion` equals `releasedVersion`.
   A sub-account with the release in production and an older version still in test is
   reported as a match, with the other versions noted in the detail column.
4. Otherwise `MISMATCH`, reported against the newest deployment and classified as
   `BEHIND`, `AHEAD` or `DIFFERENT`.
5. No active deployments at all gives `NOT_DEPLOYED`.

Version equality is numeric-aware by default: `6.0`, `6.00` and `6` collapse to the
same value, and a leading `v` is ignored. Non-numeric versions such as
`2026.08-RC1` fall back to a case-insensitive string comparison. `--strict-version`
disables all of this and requires byte-for-byte equality.

## Required privileges

- **Parent account:** Write access to *Integration Pack* / *API Publisher Integration
  Pack* for `ReleaseIntegrationPackStatus`; the `API` privilege for the query calls.
- **Sub-accounts:** the authenticating user must be able to act on each sub-account,
  either through the account hierarchy (Platform API) or as a partner account
  (Partner API). A sub-account the caller cannot read is reported as `ERROR` rather
  than silently skipped — a permissions gap should not look like a clean result.

## Operational notes

- **Rate limits.** HTTP 429 and 5xx responses are retried with exponential backoff and
  honour `Retry-After`. Reduce `--max-workers` if a large tenant estate trips limits.
- **Isolation.** One failing sub-account never aborts the sweep; it is recorded as
  `ERROR` and the run continues.
- **Exit codes.** `0` clean, `1` drift, `2` run failure or per-account API error. Use
  `--no-fail-on-drift` when the report is informational rather than a gate.
- **Scheduling.** Run once after each release, then again on a schedule. Sub-accounts
  can drift later if a tenant redeploys an older package.

To inspect the same HTTP calls by hand, import the Postman collection in
[`postman/`](../postman/README.md) or run [`examples/verify-with-curl.sh`](../examples/verify-with-curl.sh)
against the mock API.
