# Postman collection — Boomi release verification

Import these files into Postman to send the same Platform API calls
`boomi-release-check` uses, and inspect the raw JSON yourself.

| File | What it is |
| --- | --- |
| [`boomi-release-verification.postman_collection.json`](boomi-release-verification.postman_collection.json) | Collection (folders, requests, tests) |
| [`boomi-mock.postman_environment.json`](boomi-mock.postman_environment.json) | Local mock: `http://127.0.0.1:8099` + demo credentials |
| [`boomi-platform.postman_environment.json`](boomi-platform.postman_environment.json) | Live US Platform API (fill in secrets) |

A curl walk of the same requests lives at [`examples/verify-with-curl.sh`](../examples/verify-with-curl.sh).

## Import in Postman

1. Start the mock API (skip this step for a live tenant):

   ```bash
   python3 tools/mock_boomi_api.py --port 8099
   ```

2. In Postman: **Import** → drop in the collection JSON and one environment JSON.
3. Select the environment in the top-right picker (**Boomi Mock (local)** or **Boomi Platform (live)**).
4. Open **Collection Runner** (or **Run** on the collection) and run every folder in order.

The mock returns **HTTP 202 twice** on `ReleaseIntegrationPackStatus`, then **200 SUCCESS**. Folder 1 sends that GET three times so a single run covers the polling contract.

Auth is collection-level **Basic**: username `{{username}}`, password `{{apiToken}}`. Token users look like `BOOMI_TOKEN.you@example.com`.

## What each folder checks

1. **Parent** — `GET /ReleaseIntegrationPackStatus/{requestId}`. Tests copy `componentId` / `releasedVersion` into collection variables when the body is 200.
2. **Discover** — `POST /Account/query` then `POST /Account/queryMore`. The more-call body is the raw `queryToken` with `Content-Type: text/plain`.
3. **Deployments** — `POST /{subAccount}/DeployedPackage/query` for every mock tenant outcome (match, behind, partial, not deployed, 403, `6.00` numeric match). Named mock requests bake the account ID into the URL so a collection run hits every fixture; the generic `{{subAccountId}}` request is for a live tenant.
4. **Install check** — `POST /IntegrationPackInstance/query` (the CLI's `--check-instances`).
5. **Partner API** — same DeployedPackage query with `?overrideAccount=`.

## Live tenant

Import `boomi-platform.postman_environment.json` and set:

- `baseUrl` — `https://api.boomi.com` or `https://api.platform.gb.boomi.com`
- `masterAccountId`, `username`, `apiToken`, `requestId`
- `subAccountId` / `componentId` if you skip folder 1 (otherwise the SUCCESS tests fill them)

Replace the mock sub-account IDs in folder 3 with accounts you can actually read. The Echo 403 test only asserts 403 against localhost.

## Newman

```bash
python3 tools/mock_boomi_api.py --port 8099 &
npx --yes newman run postman/boomi-release-verification.postman_collection.json \
  -e postman/boomi-mock.postman_environment.json
```
