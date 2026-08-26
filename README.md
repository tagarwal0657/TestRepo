# boomi-release-check

Verify that a version released from a Boomi **master (OEM) account** actually reached
every **sub-account**.

When a master account releases an integration pack, `ReleaseIntegrationPackStatus`
only tells you what the release *pushed* and whether the request itself succeeded.
It does not confirm that each individual sub-account switched to that version. This
tool closes that gap by reading the released version on the parent side and then
querying `DeployedPackage` inside every sub-account to compare what is actually
deployed.

## How it maps to the Boomi Platform API

| Step | Where | Call | Fields used |
| --- | --- | --- | --- |
| 1. What was released | Parent account | `GET /ReleaseIntegrationPackStatus/{requestId}` | `releaseStatus`, `releaseProgress`, `ReleasePackagedComponents.ReleasePackagedComponent[].componentId`, `.releasedVersion` |
| 2. What is deployed | Each sub-account (account override) | `POST /DeployedPackage/query` filtered by `componentId` + `active=true` | `packageVersion`, `componentVersion`, `environmentId`, `deploymentId`, `deployedDate` |
| 3. Optional install check | Each sub-account | `POST /IntegrationPackInstance/query` by `integrationPackId` | `id` |

Step 3 is opt-in (`--check-instances`) and exists only to distinguish "the pack was
never installed here" from "installed but the new version was not deployed".

`GET /ReleaseIntegrationPackStatus/{requestId}` returns **HTTP 202** while the release
is `IN_PROGRESS` or `SCHEDULED` and **HTTP 200** once the released details exist. The
tool honours that contract: it fails fast by default and polls only with `--wait`.
A `SCHEDULED` release is never polled, because waiting on a future calendar date is
not something a verification run should block on.

## Install

No third-party runtime dependencies — the client is built on the standard library.

```bash
git clone <this repo> && cd <this repo>
python3 -m boomi_release_check --help
# or install the console script
pip install -e .
boomi-release-check --help
```

## Quickstart

```bash
export BOOMI_ACCOUNT_ID='your-master-ACCOUNTID'
export BOOMI_USERNAME='BOOMI_TOKEN.releasebot@example.com'
export BOOMI_API_TOKEN='<api token>'

boomi-release-check \
  --request-id release-dcfbfd2c-09d9-492d-9965-bbd9ab8f2ffc \
  --discover \
  --wait \
  --detailed
```

`--discover` lists sub-accounts with `POST /Account/query` (which returns the accounts
created by the parent account) and drops deleted, suspended and expired tenants. You
can instead pass `--sub-account` repeatedly or point at a file:

```bash
boomi-release-check --request-id release-... --sub-accounts-file examples/sub-accounts.csv
```

The file may be `.csv` (`accountId,name`), `.json` (list of IDs or objects), or plain
text with one account ID per line and `#` comments.

## Output

```
Sub-account results
--------------------------------------------
ACCOUNT ID               NAME                 STATUS        DETAIL
-----------------------  -------------------  ------------  ------------------
customer-alpha-A1B2C3    Alpha Logistics      UP_TO_DATE    MATCH=2
customer-bravo-D4E5F6    Bravo Manufacturing  OUT_OF_DATE   MISMATCH=2
customer-charlie-G7H8I9  Charlie Retail       PARTIAL       MATCH=1, MISMATCH=1
customer-delta-J1K2L3    Delta Health         NOT_DEPLOYED  NOT_DEPLOYED=2
```

| Sub-account status | Meaning |
| --- | --- |
| `UP_TO_DATE` | Every released component is deployed and active on the released version |
| `PARTIAL` | Some components moved to the released version, others did not |
| `OUT_OF_DATE` | The pack is deployed but no component is on the released version |
| `NOT_DEPLOYED` | No active `DeployedPackage` exists for the released components |
| `NOT_INSTALLED` | `--check-instances` found no integration pack instance in the sub-account |
| `ERROR` | The Platform API call for that sub-account failed (permissions, rate limit, ...) |

Formats: `--format table|json|csv|markdown`, optionally written with `--output FILE`.
`--detailed` adds the per-component and per-deployment breakdown.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Every sub-account is on the released version |
| `1` | At least one sub-account is behind (use `--no-fail-on-drift` to suppress) |
| `2` | The run could not complete, or a sub-account returned an API error |

This makes the command usable directly as a post-release gate in CI.

## Account override modes

Boomi offers two ways for a parent account to act on a sub-account, and the tool
supports both:

```bash
# Platform API: the sub-account goes in the URL path (default)
https://api.boomi.com/api/rest/v1/<subAccountId>/DeployedPackage/query

# Partner API: your own account stays in the path, the sub-account is a parameter
boomi-release-check --partner-api ...
https://api.boomi.com/partner/api/rest/v1/<yourAccountId>/DeployedPackage/query?overrideAccount=<subAccountId>
```

Use `--region gb` or `--base-url` for non-US platform hosts.

## Version matching

Boomi `packageVersion` values are user-defined strings, so `6.0`, `6.00` and `6` all
describe the same release. The default comparison is numeric-aware and treats those as
equal; `--strict-version` requires an exact string match instead.

A mismatch is classified as `BEHIND`, `AHEAD` or `DIFFERENT` so you can tell a
sub-account that missed the release from one that is running something unexpected.

## Validate before scaling out

Before trusting the report across every tenant, confirm on one known-good sub-account
that the released version and the deployed version line up the way you expect:

```bash
boomi-release-check \
  --request-id release-... \
  --sub-account <one-known-good-sub-account> \
  --format json --detailed
```

Check that `release.components[].releasedVersion` equals
`accounts[0].checks[].deployedVersion`, and that `deployments[].environmentId` matches
the environment you expect that customer to run. Once that lines up, switch to
`--discover` for the full sweep.

## Local HTML UI

Credentials stay on your machine. The page posts to a tiny local server, which then
calls `https://api.boomi.com` (hardcoded) using the same verification logic as the CLI.

```bash
python3 -m boomi_release_check.webapp
# open the URL it prints (http://127.0.0.1:8765 by default)
```

If 8765 is already taken it binds the next free port and prints that URL. Pass `--port` to choose one.

Fill in master account ID, username, password (API token), the release request ID,
and the sub-account ID to check. Several sub-accounts can be pasted as a comma- or
newline-separated list.

To try the UI against the mock API instead of Boomi:

```bash
python3 tools/mock_boomi_api.py --port 8099 &
python3 -m boomi_release_check.webapp --port 8765 --base-url http://127.0.0.1:8099
```

Then use `apptio-master-OEM` / `BOOMI_TOKEN.releasebot@example.com` / `demo-token`,
request ID `release-dcfbfd2c-09d9-492d-9965-bbd9ab8f2ffc`, and a mock sub-account
such as `customer-alpha-A1B2C3`.

## Try it without a Boomi tenant

A mock Platform API is included. It reproduces the 202 polling contract, Basic auth,
`queryToken` paging, and a sub-account that returns 403.

```bash
python3 tools/mock_boomi_api.py --port 8099 &

BOOMI_ACCOUNT_ID=apptio-master-OEM \
BOOMI_USERNAME='BOOMI_TOKEN.releasebot@example.com' \
BOOMI_API_TOKEN=demo-token \
BOOMI_BASE_URL=http://127.0.0.1:8099 \
python3 -m boomi_release_check \
  --request-id release-dcfbfd2c-09d9-492d-9965-bbd9ab8f2ffc \
  --discover --wait --poll-interval 2 --detailed
```

## Verify the API in Postman or curl

The mock (or a live tenant) can be exercised request-by-request:

- **Postman** — import [`postman/boomi-release-verification.postman_collection.json`](postman/boomi-release-verification.postman_collection.json) and [`postman/boomi-mock.postman_environment.json`](postman/boomi-mock.postman_environment.json). See [`postman/README.md`](postman/README.md).
- **curl** — `./examples/verify-with-curl.sh` walks the same calls and asserts the mock status codes (202 polling, paging, MATCH / BEHIND / 403).

```bash
python3 tools/mock_boomi_api.py --port 8099 &
./examples/verify-with-curl.sh
```

## Tests

```bash
pip install -e '.[dev]'
python3 -m pytest -q
```

## Further reading

- [`docs/boomi-release-verification.md`](docs/boomi-release-verification.md) — the API
  flow, required privileges, and operational notes.
- [`postman/README.md`](postman/README.md) — Postman collection to inspect those calls.
- [ReleaseIntegrationPackStatus](https://developer.boomi.com/docs/api/platformapi/ReleaseIntegrationPackStatus)
- [DeployedPackage](https://developer.boomi.com/docs/api/platformapi/DeployedPackage)
- [IntegrationPackInstance](https://developer.boomi.com/docs/api/platformapi/IntegrationPackInstance)
