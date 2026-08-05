"""Reconcile Anthropic Platform Cost + Usage Admin APIs into one Apptio CSV.

Anthropic does not return user + model + USD on a single Platform endpoint.
This script:

1. Pulls cost_report grouped by description + workspace_id
   → daily USD rows at (model, token_type, service_tier, context_window, …).
2. Pulls usage_report/messages grouped to the same dimensions PLUS account_id
   (and api_key_id as a fallback identity when account_id is null).
3. Unpivots usage token columns so each row matches one cost token_type.
4. Allocates each cost bucket across users pro-rata by matching token counts.
5. Joins /organizations/users for email.
6. Emits one CSV with cost_usd, tokens, model, user — and asserts that
   sum(allocated cost) equals sum(cost_report) per bucket (remainder goes to
   the largest share so totals reconcile exactly).

Auth: Admin API key (sk-ant-admin01-...) via ANTHROPIC_ADMIN_KEY.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable, Iterator
from urllib.parse import urlencode

import requests

API_BASE = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"

COST_PATH = "/v1/organizations/cost_report"
USAGE_PATH = "/v1/organizations/usage_report/messages"
USERS_PATH = "/v1/organizations/users"

# Cost amount is fractional cents as a decimal string; convert to USD.
CENTS_TO_USD = Decimal("100")
USD_QUANT = Decimal("0.000001")

# Map cost_report.token_type → field(s) on a usage_report/messages result.
TOKEN_TYPE_FIELDS: dict[str, tuple[str, ...]] = {
    "uncached_input_tokens": ("uncached_input_tokens",),
    "output_tokens": ("output_tokens",),
    "cache_read_input_tokens": ("cache_read_input_tokens",),
    "cache_creation.ephemeral_5m_input_tokens": (
        "cache_creation",
        "ephemeral_5m_input_tokens",
    ),
    "cache_creation.ephemeral_1h_input_tokens": (
        "cache_creation",
        "ephemeral_1h_input_tokens",
    ),
}

# Usage service_tier values that cost_report collapses / excludes.
# Priority Tier is not billed through cost_report; map batch-like tiers to
# the cost_report service_tier vocabulary where possible.
COST_SERVICE_TIERS = {"batch", "standard"}

RATE_LIMIT_BACKOFF_SECONDS = (1, 2, 4, 8, 16)

VENDOR = "Anthropic"
SERVICE = "Claude Platform API"

OUTPUT_COLUMNS = [
    "usage_date",
    "vendor",
    "service",
    "workspace_id",
    "user_id",
    "user_email",
    "user_name",
    "api_key_id",
    "principal_type",
    "model",
    "token_type",
    "cost_type",
    "service_tier",
    "context_window",
    "inference_geo",
    "tokens",
    "cost_usd",
    "currency",
    "cost_description",
    "allocation_share",
]


class AnthropicApiError(RuntimeError):
    pass


@dataclass
class Client:
    api_key: str
    base_url: str = API_BASE
    timeout: int = 60
    session: requests.Session = field(default_factory=requests.Session)

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "User-Agent": "ApptioAnthropicPlatformExport/1.0",
        }

    def _request(self, path: str, params: list[tuple[str, str]] | None = None) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"

        last_error: Exception | None = None
        for attempt, backoff in enumerate((0,) + RATE_LIMIT_BACKOFF_SECONDS):
            if backoff:
                time.sleep(backoff)
            try:
                response = self.session.get(url, headers=self._headers(), timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = exc
                continue

            if response.status_code == 429 or response.status_code >= 500:
                last_error = AnthropicApiError(
                    f"{response.status_code} from {path}: {response.text[:400]}"
                )
                continue
            if response.status_code >= 400:
                raise AnthropicApiError(
                    f"{response.status_code} from {path}: {response.text[:400]}"
                )
            return response.json()

        raise AnthropicApiError(f"Giving up on {path} after retries") from last_error

    def paginate_buckets(
        self, path: str, params: list[tuple[str, str]]
    ) -> Iterator[dict[str, Any]]:
        """Yield time-bucket objects across next_page cursors."""
        page: str | None = None
        while True:
            request_params = list(params)
            if page:
                request_params.append(("page", page))
            payload = self._request(path, request_params)
            for bucket in payload.get("data") or []:
                yield bucket
            if not payload.get("has_more"):
                return
            page = payload.get("next_page")
            if not page:
                return

    def paginate_users(self) -> list[dict[str, Any]]:
        users: list[dict[str, Any]] = []
        after_id: str | None = None
        while True:
            params: list[tuple[str, str]] = [("limit", "1000")]
            if after_id:
                params.append(("after_id", after_id))
            payload = self._request(USERS_PATH, params)
            batch = payload.get("data") or []
            users.extend(batch)
            if not payload.get("has_more") or not batch:
                break
            after_id = payload.get("last_id") or batch[-1].get("id")
            if not after_id:
                break
        return users


def parse_day(value: str) -> date:
    return date.fromisoformat(value)


def day_bounds(day: date) -> tuple[str, str]:
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    return (
        start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        end.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def daterange(start: date, end: date) -> Iterable[date]:
    """Inclusive start, exclusive end — walk one UTC day at a time."""
    cur = start
    while cur < end:
        yield cur
        cur += timedelta(days=1)


def cents_to_usd(amount: str | int | float | Decimal) -> Decimal:
    return (Decimal(str(amount)) / CENTS_TO_USD).quantize(USD_QUANT)


def nested_int(row: dict[str, Any], path: tuple[str, ...]) -> int:
    cur: Any = row
    for key in path:
        if not isinstance(cur, dict):
            return 0
        cur = cur.get(key)
    try:
        return int(cur or 0)
    except (TypeError, ValueError):
        return 0


def normalize_service_tier(tier: str | None) -> str | None:
    """Align usage tiers with cost_report vocabulary where possible."""
    if tier is None:
        return None
    if tier == "batch":
        return "batch"
    if tier in {"standard", "flex", "flex_discount", "priority_on_demand"}:
        # flex / on-demand still appear as standard-priced lines in cost_report
        # for most orgs; keep the raw tier on the output row separately if needed.
        return "standard" if tier != "batch" else "batch"
    if tier == "priority":
        # Priority Tier is intentionally absent from cost_report.
        return "priority"
    return tier


def cost_bucket_key(
    day: str,
    workspace_id: str | None,
    model: str | None,
    token_type: str | None,
    cost_type: str | None,
    service_tier: str | None,
    context_window: str | None,
    inference_geo: str | None,
) -> tuple:
    return (
        day,
        workspace_id,
        model,
        token_type,
        cost_type,
        service_tier,
        context_window,
        inference_geo,
    )


def fetch_cost_rows(client: Client, start: date, end: date) -> list[dict[str, Any]]:
    """Pull cost_report for [start, end) in ≤31-day windows."""
    rows: list[dict[str, Any]] = []
    window_start = start
    while window_start < end:
        window_end = min(window_start + timedelta(days=31), end)
        starting_at, _ = day_bounds(window_start)
        _, ending_at = day_bounds(window_end - timedelta(days=1))
        # ending_at for API is exclusive; day_bounds already gives next midnight.
        ending_at = day_bounds(window_end)[0]

        params = [
            ("starting_at", starting_at),
            ("ending_at", ending_at),
            ("bucket_width", "1d"),
            ("group_by[]", "description"),
            ("group_by[]", "workspace_id"),
            ("limit", "31"),
        ]
        for bucket in client.paginate_buckets(COST_PATH, params):
            day = bucket["starting_at"][:10]
            for result in bucket.get("results") or []:
                amount = Decimal(str(result.get("amount") or "0"))
                rows.append(
                    {
                        "usage_date": day,
                        "workspace_id": result.get("workspace_id"),
                        "model": result.get("model"),
                        "token_type": result.get("token_type"),
                        "cost_type": result.get("cost_type"),
                        "service_tier": result.get("service_tier"),
                        "context_window": result.get("context_window"),
                        "inference_geo": result.get("inference_geo"),
                        "description": result.get("description"),
                        "currency": result.get("currency") or "USD",
                        "amount_cents": amount,
                        "cost_usd": cents_to_usd(amount),
                    }
                )
        window_start = window_end
    return rows


def fetch_usage_rows(client: Client, start: date, end: date) -> list[dict[str, Any]]:
    """Pull usage_report/messages day-by-day with user + cost-matching dims."""
    rows: list[dict[str, Any]] = []
    for day in daterange(start, end):
        starting_at, ending_at = day_bounds(day)
        params = [
            ("starting_at", starting_at),
            ("ending_at", ending_at),
            ("bucket_width", "1d"),
            ("group_by[]", "account_id"),
            ("group_by[]", "api_key_id"),
            ("group_by[]", "model"),
            ("group_by[]", "workspace_id"),
            ("group_by[]", "service_tier"),
            ("group_by[]", "context_window"),
            ("group_by[]", "inference_geo"),
            ("limit", "1"),
        ]
        for bucket in client.paginate_buckets(USAGE_PATH, params):
            for result in bucket.get("results") or []:
                rows.append(
                    {
                        "usage_date": day.isoformat(),
                        "account_id": result.get("account_id"),
                        "api_key_id": result.get("api_key_id"),
                        "workspace_id": result.get("workspace_id"),
                        "model": result.get("model"),
                        "service_tier": result.get("service_tier"),
                        "context_window": result.get("context_window"),
                        "inference_geo": result.get("inference_geo"),
                        "uncached_input_tokens": int(result.get("uncached_input_tokens") or 0),
                        "output_tokens": int(result.get("output_tokens") or 0),
                        "cache_read_input_tokens": int(
                            result.get("cache_read_input_tokens") or 0
                        ),
                        "cache_creation": result.get("cache_creation") or {},
                        "web_search_requests": nested_int(
                            result, ("server_tool_use", "web_search_requests")
                        ),
                        "raw": result,
                    }
                )
        print(
            f"usage {day.isoformat()}: {sum(1 for r in rows if r['usage_date'] == day.isoformat())} rows",
            file=sys.stderr,
        )
    return rows


def unpivot_usage_for_token_type(usage_row: dict[str, Any], token_type: str) -> int:
    """Return the usage token count that corresponds to a cost_report token_type."""
    path = TOKEN_TYPE_FIELDS.get(token_type)
    if not path:
        return 0
    return nested_int(usage_row, path)


def allocate_costs(
    cost_rows: list[dict[str, Any]],
    usage_rows: list[dict[str, Any]],
    users_by_id: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Allocate each cost line across matching usage users by token share.

    Returns (output_rows, reconcile_stats).
    """
    # Index usage by cost-matching key (without user).
    usage_by_key: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for u in usage_rows:
        aligned_tier = normalize_service_tier(u.get("service_tier"))
        # Index under both raw and aligned tier so we still match cost rows.
        for tier in {u.get("service_tier"), aligned_tier}:
            key = cost_bucket_key(
                u["usage_date"],
                u.get("workspace_id"),
                u.get("model"),
                None,  # token_type filled per cost row
                None,
                tier,
                u.get("context_window"),
                u.get("inference_geo"),
            )
            # Store under a partial key without token/cost_type; we filter later.
            usage_by_key[
                (
                    u["usage_date"],
                    u.get("workspace_id"),
                    u.get("model"),
                    tier,
                    u.get("context_window"),
                    u.get("inference_geo"),
                )
            ].append(u)

    # Deduplicate usage rows appended under multiple tier keys: keep unique ids.
    # We'll select candidates per cost row instead of pre-building token keys.

    output: list[dict[str, Any]] = []
    total_cost = Decimal("0")
    total_allocated = Decimal("0")
    unmatched_cost = Decimal("0")
    priority_usage_tokens = 0

    for cost in cost_rows:
        total_cost += cost["cost_usd"]
        cost_type = cost.get("cost_type")
        token_type = cost.get("token_type")
        tier = cost.get("service_tier")

        candidates_key = (
            cost["usage_date"],
            cost.get("workspace_id"),
            cost.get("model"),
            tier,
            cost.get("context_window"),
            cost.get("inference_geo"),
        )
        # Also try None model / workspace for non-token tool costs.
        candidate_lists = [usage_by_key.get(candidates_key, [])]
        if cost.get("model") is None:
            # Broaden: same day + workspace only for tool costs.
            for key, rows in usage_by_key.items():
                if key[0] == cost["usage_date"] and key[1] == cost.get("workspace_id"):
                    candidate_lists.append(rows)

        # Unique by (account_id, api_key_id, model, service_tier, …)
        seen: set[tuple] = set()
        candidates: list[dict[str, Any]] = []
        for group in candidate_lists:
            for u in group:
                ident = (
                    u.get("account_id"),
                    u.get("api_key_id"),
                    u.get("model"),
                    u.get("service_tier"),
                    u.get("workspace_id"),
                    u.get("context_window"),
                    u.get("inference_geo"),
                )
                if ident in seen:
                    continue
                seen.add(ident)
                candidates.append(u)

        shares: list[tuple[dict[str, Any], int]] = []
        if cost_type == "tokens" and token_type:
            for u in candidates:
                tokens = unpivot_usage_for_token_type(u, token_type)
                if tokens > 0:
                    shares.append((u, tokens))
        elif cost_type == "web_search":
            for u in candidates:
                reqs = int(u.get("web_search_requests") or 0)
                if reqs > 0:
                    shares.append((u, reqs))
        else:
            # code_execution / session_usage / unknown: no usage metric.
            # Attribute to a synthetic unmatched row (workspace-level).
            shares = []

        bucket_cost = cost["cost_usd"]
        if not shares:
            unmatched_cost += bucket_cost
            output.append(
                _output_row(
                    cost=cost,
                    usage=None,
                    users_by_id=users_by_id,
                    tokens=0,
                    cost_usd=bucket_cost,
                    share=Decimal("1") if bucket_cost else Decimal("0"),
                    principal_type="unallocated",
                )
            )
            total_allocated += bucket_cost
            continue

        denom = sum(t for _, t in shares)
        # Largest-remainder method so allocated USD sums exactly to bucket_cost.
        raw_amounts = [
            (u, tokens, (bucket_cost * Decimal(tokens) / Decimal(denom)))
            for u, tokens in shares
        ]
        floored = [
            (u, tokens, amt.quantize(USD_QUANT, rounding=ROUND_HALF_UP))
            for u, tokens, amt in raw_amounts
        ]
        allocated_sum = sum((a for _, _, a in floored), Decimal("0"))
        remainder = bucket_cost - allocated_sum
        # Give remainder pennies to the largest token share.
        floored.sort(key=lambda x: x[1], reverse=True)
        if floored and remainder != 0:
            u0, t0, a0 = floored[0]
            floored[0] = (u0, t0, a0 + remainder)

        for u, tokens, amount in floored:
            share = Decimal(tokens) / Decimal(denom)
            output.append(
                _output_row(
                    cost=cost,
                    usage=u,
                    users_by_id=users_by_id,
                    tokens=tokens,
                    cost_usd=amount,
                    share=share,
                    principal_type=_principal_type(u),
                )
            )
            total_allocated += amount

    # Track priority usage that cannot be cost-reconciled.
    for u in usage_rows:
        if u.get("service_tier") == "priority":
            priority_usage_tokens += (
                int(u.get("uncached_input_tokens") or 0)
                + int(u.get("output_tokens") or 0)
                + int(u.get("cache_read_input_tokens") or 0)
            )

    stats = {
        "cost_rows": len(cost_rows),
        "usage_rows": len(usage_rows),
        "output_rows": len(output),
        "total_cost_usd": total_cost,
        "total_allocated_usd": total_allocated,
        "unmatched_cost_usd": unmatched_cost,
        "priority_usage_tokens": priority_usage_tokens,
        "reconcile_delta_usd": total_allocated - total_cost,
    }
    return output, stats


def _principal_type(usage: dict[str, Any] | None) -> str:
    if usage is None:
        return "unallocated"
    if usage.get("account_id"):
        return "user"
    if usage.get("api_key_id"):
        return "api_key"
    return "unknown"


def _output_row(
    *,
    cost: dict[str, Any],
    usage: dict[str, Any] | None,
    users_by_id: dict[str, dict[str, Any]],
    tokens: int,
    cost_usd: Decimal,
    share: Decimal,
    principal_type: str,
) -> dict[str, Any]:
    user_id = usage.get("account_id") if usage else None
    user = users_by_id.get(user_id or "") if user_id else None
    return {
        "usage_date": cost["usage_date"],
        "vendor": VENDOR,
        "service": SERVICE,
        "workspace_id": cost.get("workspace_id") or "",
        "user_id": user_id or "",
        "user_email": (user or {}).get("email") or "",
        "user_name": (user or {}).get("name") or "",
        "api_key_id": (usage or {}).get("api_key_id") or "",
        "principal_type": principal_type,
        "model": cost.get("model") or (usage or {}).get("model") or "",
        "token_type": cost.get("token_type") or "",
        "cost_type": cost.get("cost_type") or "",
        "service_tier": cost.get("service_tier")
        or (usage or {}).get("service_tier")
        or "",
        "context_window": cost.get("context_window")
        or (usage or {}).get("context_window")
        or "",
        "inference_geo": cost.get("inference_geo")
        or (usage or {}).get("inference_geo")
        or "",
        "tokens": tokens,
        "cost_usd": f"{cost_usd:.6f}",
        "currency": cost.get("currency") or "USD",
        "cost_description": cost.get("description") or "",
        "allocation_share": f"{share:.8f}",
    }


def write_csv(path: str, rows: list[dict[str, Any]]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Combine Anthropic Platform cost_report + usage_report/messages "
            "into one Apptio CSV with user, model, tokens, and cost_usd."
        )
    )
    p.add_argument("--start", required=True, help="Start date YYYY-MM-DD (inclusive, UTC)")
    p.add_argument("--end", required=True, help="End date YYYY-MM-DD (exclusive, UTC)")
    p.add_argument("--out", required=True, help="Output CSV path")
    p.add_argument(
        "--api-key",
        default=os.environ.get("ANTHROPIC_ADMIN_KEY", ""),
        help="Admin API key (or set ANTHROPIC_ADMIN_KEY)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if not args.api_key:
        print("Set ANTHROPIC_ADMIN_KEY or pass --api-key", file=sys.stderr)
        return 2

    start = parse_day(args.start)
    end = parse_day(args.end)
    if end <= start:
        print("--end must be after --start", file=sys.stderr)
        return 2

    client = Client(api_key=args.api_key)

    print("Fetching organization users…", file=sys.stderr)
    users = client.paginate_users()
    users_by_id = {u["id"]: u for u in users if u.get("id")}
    print(f"  {len(users_by_id)} users", file=sys.stderr)

    print(f"Fetching cost_report {start} → {end}…", file=sys.stderr)
    cost_rows = fetch_cost_rows(client, start, end)
    print(f"  {len(cost_rows)} cost lines", file=sys.stderr)

    print(f"Fetching usage_report/messages {start} → {end}…", file=sys.stderr)
    usage_rows = fetch_usage_rows(client, start, end)
    print(f"  {len(usage_rows)} usage lines", file=sys.stderr)

    output, stats = allocate_costs(cost_rows, usage_rows, users_by_id)
    write_csv(args.out, output)

    print("Reconcile:", file=sys.stderr)
    for k, v in stats.items():
        print(f"  {k}: {v}", file=sys.stderr)
    if stats["reconcile_delta_usd"] != 0:
        print(
            "WARNING: allocated total does not equal cost_report total",
            file=sys.stderr,
        )
        return 1

    print(f"Wrote {len(output)} rows → {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
