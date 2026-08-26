#!/usr/bin/env bash
# Walk the same Boomi Platform API calls boomi-release-check makes.
#
# Defaults target the local mock:
#   python3 tools/mock_boomi_api.py --port 8099
#   ./examples/verify-with-curl.sh
#
# Point at a live tenant by exporting BOOMI_BASE_URL, BOOMI_ACCOUNT_ID,
# BOOMI_USERNAME, BOOMI_API_TOKEN, BOOMI_RELEASE_REQUEST_ID and the
# component / sub-account IDs below.
set -euo pipefail

BASE_URL="${BOOMI_BASE_URL:-http://127.0.0.1:8099}"
MASTER="${BOOMI_ACCOUNT_ID:-apptio-master-OEM}"
USERNAME="${BOOMI_USERNAME:-BOOMI_TOKEN.releasebot@example.com}"
TOKEN="${BOOMI_API_TOKEN:-demo-token}"
REQUEST_ID="${BOOMI_RELEASE_REQUEST_ID:-release-dcfbfd2c-09d9-492d-9965-bbd9ab8f2ffc}"
COMPONENT_ID="${BOOMI_COMPONENT_ID:-bb8b6c9d-9c39-4309-b07f-cdd96d201b27}"
COMPONENT_ID_2="${BOOMI_COMPONENT_ID_2:-9d05717c-4dfe-4d5f-8e60-9196a6f78ef9}"
PACK_ID="${BOOMI_INTEGRATION_PACK_ID:-d7c16f5d-3311-417e-a149-3c55436f7d8d}"
WAIT_RELEASE="${BOOMI_WAIT_RELEASE:-1}"

pretty() {
  if command -v jq >/dev/null 2>&1; then
    jq -C . 2>/dev/null || cat
  else
    python3 -m json.tool 2>/dev/null || cat
  fi
}

json_field() {
  python3 -c 'import json,sys; data=json.load(sys.stdin)
path=sys.argv[1].split(".")
cur=data
for key in path:
    if isinstance(cur, dict):
        cur=cur.get(key)
    else:
        cur=None
        break
if cur is None:
    sys.exit(0)
if isinstance(cur, (dict, list)):
    json.dump(cur, sys.stdout)
else:
    sys.stdout.write(str(cur))
' "$1"
}

PASS=0
FAIL=0
RESULTS=()

expect_status() {
  local name="$1" actual="$2" expected="$3"
  if [[ "$actual" == "$expected" ]]; then
    PASS=$((PASS + 1))
    RESULTS+=("PASS  HTTP ${actual}  ${name}")
  else
    FAIL=$((FAIL + 1))
    RESULTS+=("FAIL  HTTP ${actual} (expected ${expected})  ${name}")
  fi
}

request() {
  local method="$1" url="$2" content_type="${3:-}" body="${4:-}"
  local args=(
    -sS
    -o /tmp/boomi-curl-body
    -w "%{http_code}"
    -u "${USERNAME}:${TOKEN}"
    -H "Accept: application/json"
    -X "${method}"
  )
  if [[ -n "$content_type" ]]; then
    args+=(-H "Content-Type: ${content_type}")
  fi
  if [[ -n "$body" ]]; then
    args+=(--data-binary "$body")
  fi
  curl "${args[@]}" "$url"
}

print_exchange() {
  local method="$1" url="$2" status="$3"
  echo
  echo "================================================================"
  echo "${method} ${url}"
  echo "HTTP ${status}"
  echo "----------------------------------------------------------------"
  pretty </tmp/boomi-curl-body
  echo
}

deployed_filter() {
  local component_id="$1"
  cat <<EOF
{"QueryFilter":{"expression":{"operator":"and","nestedExpression":[{"property":"componentId","operator":"EQUALS","argument":["${component_id}"]},{"property":"active","operator":"EQUALS","argument":["true"]}]}}}
EOF
}

echo "Boomi release verification (curl)"
echo "  base URL : ${BASE_URL}"
echo "  account  : ${MASTER}"
echo "  request  : ${REQUEST_ID}"

# --- 1. Parent release status ------------------------------------------
release_url="${BASE_URL}/api/rest/v1/${MASTER}/ReleaseIntegrationPackStatus/${REQUEST_ID}"
status=""
attempts=0
max_attempts=6
while true; do
  attempts=$((attempts + 1))
  status="$(request GET "$release_url")"
  print_exchange GET "$release_url" "$status"
  if [[ "$status" == "200" ]]; then
    expect_status "ReleaseIntegrationPackStatus SUCCESS" "$status" "200"
    IFS='|' read -r COMPONENT_ID COMPONENT_ID_2 PACK_ID < <(python3 - <<'PY'
import json
body = json.load(open("/tmp/boomi-curl-body"))
comps = body.get("ReleasePackagedComponents", {}).get("ReleasePackagedComponent")
if isinstance(comps, dict):
    comps = [comps]
comps = comps or []
first = comps[0]["componentId"] if comps else ""
second = comps[1]["componentId"] if len(comps) > 1 else ""
pack = body.get("integrationPackId") or ""
print(f"{first}|{second}|{pack}")
PY
)
    break
  fi
  if [[ "$status" != "202" ]]; then
    expect_status "ReleaseIntegrationPackStatus" "$status" "200"
    break
  fi
  if [[ "$WAIT_RELEASE" != "1" || "$attempts" -ge "$max_attempts" ]]; then
    expect_status "ReleaseIntegrationPackStatus still 202 after ${attempts} poll(s)" "$status" "200"
    break
  fi
  echo "Release still IN_PROGRESS (HTTP 202); polling again..."
  sleep 0.2
done

# --- 2. Discover sub-accounts + paging ---------------------------------
account_url="${BASE_URL}/api/rest/v1/${MASTER}/Account/query"
account_body='{"QueryFilter":{"expression":{"property":"status","operator":"NOT_EQUALS","argument":["deleted"]}}}'
status="$(request POST "$account_url" "application/json" "$account_body")"
print_exchange POST "$account_url" "$status"
expect_status "Account/query" "$status" "200"
query_token="$(json_field queryToken </tmp/boomi-curl-body)"

if [[ -n "${query_token}" ]]; then
  more_url="${BASE_URL}/api/rest/v1/${MASTER}/Account/queryMore"
  status="$(request POST "$more_url" "text/plain" "$query_token")"
  print_exchange POST "$more_url" "$status"
  expect_status "Account/queryMore" "$status" "200"
else
  echo "No queryToken on Account/query (live pages at 100; mock pages at 2)."
fi

# --- 3. DeployedPackage per representative sub-account -----------------
query_deployed() {
  local label="$1" account="$2" component="$3" expected_status="$4"
  local url="${BASE_URL}/api/rest/v1/${account}/DeployedPackage/query"
  local body
  body="$(deployed_filter "$component")"
  local status
  status="$(request POST "$url" "application/json" "$body")"
  print_exchange POST "$url" "$status"
  expect_status "$label" "$status" "$expected_status"
}

query_deployed "Alpha Order Intake (Platform API)" "customer-alpha-A1B2C3" "$COMPONENT_ID" "200"
query_deployed "Alpha Cost Export" "customer-alpha-A1B2C3" "${COMPONENT_ID_2:-$COMPONENT_ID}" "200"
query_deployed "Bravo Order Intake" "customer-bravo-D4E5F6" "$COMPONENT_ID" "200"
query_deployed "Charlie Order Intake" "customer-charlie-G7H8I9" "$COMPONENT_ID" "200"
query_deployed "Charlie Cost Export" "customer-charlie-G7H8I9" "${COMPONENT_ID_2:-$COMPONENT_ID}" "200"
query_deployed "Delta Order Intake (NOT_DEPLOYED)" "customer-delta-J1K2L3" "$COMPONENT_ID" "200"
query_deployed "Echo Order Intake (ERROR)" "customer-echo-M4N5O6" "$COMPONENT_ID" "403"
query_deployed "Foxtrot Order Intake (6.00)" "customer-foxtrot-P7Q8R9" "$COMPONENT_ID" "200"

# Inactive filter (Foxtrot should include the retired 4.0 row on the mock)
foxtrot_url="${BASE_URL}/api/rest/v1/customer-foxtrot-P7Q8R9/DeployedPackage/query"
foxtrot_inactive="{\"QueryFilter\":{\"expression\":{\"property\":\"componentId\",\"operator\":\"EQUALS\",\"argument\":[\"${COMPONENT_ID}\"]}}}"
status="$(request POST "$foxtrot_url" "application/json" "$foxtrot_inactive")"
print_exchange POST "$foxtrot_url" "$status"
expect_status "Foxtrot including inactive" "$status" "200"

# --- 4. Optional install check -----------------------------------------
query_instance() {
  local label="$1" account="$2"
  local url="${BASE_URL}/api/rest/v1/${account}/IntegrationPackInstance/query"
  local body="{\"QueryFilter\":{\"expression\":{\"property\":\"integrationPackId\",\"operator\":\"EQUALS\",\"argument\":[\"${PACK_ID}\"]}}}"
  local status
  status="$(request POST "$url" "application/json" "$body")"
  print_exchange POST "$url" "$status"
  expect_status "$label" "$status" "200"
}

query_instance "Alpha IntegrationPackInstance" "customer-alpha-A1B2C3"
query_instance "Delta IntegrationPackInstance" "customer-delta-J1K2L3"

# --- 5. Partner API override -------------------------------------------
partner_url="${BASE_URL}/partner/api/rest/v1/${MASTER}/DeployedPackage/query?overrideAccount=customer-alpha-A1B2C3"
status="$(request POST "$partner_url" "application/json" "$(deployed_filter "$COMPONENT_ID")")"
print_exchange POST "$partner_url" "$status"
expect_status "Partner API overrideAccount" "$status" "200"

echo
echo "Summary"
echo "-------"
for line in "${RESULTS[@]}"; do
  echo "  ${line}"
done
echo "  ${PASS} passed, ${FAIL} failed"
if [[ "$FAIL" -ne 0 ]]; then
  exit 1
fi
