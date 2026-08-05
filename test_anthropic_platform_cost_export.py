"""Unit tests for Platform cost↔usage allocation (no live API calls)."""

from __future__ import annotations

from decimal import Decimal

from anthropic_platform_cost_export import (
    allocate_costs,
    cents_to_usd,
    unpivot_usage_for_token_type,
)


def test_cents_to_usd():
    assert cents_to_usd("123.45") == Decimal("1.234500")
    assert cents_to_usd("100") == Decimal("1.000000")


def test_unpivot_cache_tokens():
    row = {
        "uncached_input_tokens": 100,
        "output_tokens": 50,
        "cache_read_input_tokens": 20,
        "cache_creation": {
            "ephemeral_5m_input_tokens": 7,
            "ephemeral_1h_input_tokens": 3,
        },
    }
    assert unpivot_usage_for_token_type(row, "uncached_input_tokens") == 100
    assert unpivot_usage_for_token_type(row, "output_tokens") == 50
    assert unpivot_usage_for_token_type(row, "cache_read_input_tokens") == 20
    assert (
        unpivot_usage_for_token_type(row, "cache_creation.ephemeral_5m_input_tokens")
        == 7
    )
    assert (
        unpivot_usage_for_token_type(row, "cache_creation.ephemeral_1h_input_tokens")
        == 3
    )


def test_allocate_exact_reconcile_two_users():
    """$3.00 input-token cost split 1:2 across two users must sum to exactly $3."""
    cost_rows = [
        {
            "usage_date": "2026-07-01",
            "workspace_id": "wrk_1",
            "model": "claude-sonnet-4",
            "token_type": "uncached_input_tokens",
            "cost_type": "tokens",
            "service_tier": "standard",
            "context_window": "0-200k",
            "inference_geo": "global",
            "description": "Claude Sonnet 4 Usage - Input Tokens",
            "currency": "USD",
            "amount_cents": Decimal("300"),
            "cost_usd": Decimal("3.000000"),
        }
    ]
    usage_rows = [
        {
            "usage_date": "2026-07-01",
            "account_id": "user_a",
            "api_key_id": "key_a",
            "workspace_id": "wrk_1",
            "model": "claude-sonnet-4",
            "service_tier": "standard",
            "context_window": "0-200k",
            "inference_geo": "global",
            "uncached_input_tokens": 100,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation": {},
            "web_search_requests": 0,
        },
        {
            "usage_date": "2026-07-01",
            "account_id": "user_b",
            "api_key_id": "key_b",
            "workspace_id": "wrk_1",
            "model": "claude-sonnet-4",
            "service_tier": "standard",
            "context_window": "0-200k",
            "inference_geo": "global",
            "uncached_input_tokens": 200,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation": {},
            "web_search_requests": 0,
        },
    ]
    users = {
        "user_a": {"id": "user_a", "email": "a@example.com", "name": "A"},
        "user_b": {"id": "user_b", "email": "b@example.com", "name": "B"},
    }

    output, stats = allocate_costs(cost_rows, usage_rows, users)

    assert stats["reconcile_delta_usd"] == Decimal("0")
    assert stats["total_allocated_usd"] == Decimal("3.000000")
    assert len(output) == 2

    by_user = {r["user_id"]: r for r in output}
    assert by_user["user_a"]["tokens"] == 100
    assert by_user["user_b"]["tokens"] == 200
    assert Decimal(by_user["user_a"]["cost_usd"]) == Decimal("1.000000")
    assert Decimal(by_user["user_b"]["cost_usd"]) == Decimal("2.000000")
    assert by_user["user_a"]["user_email"] == "a@example.com"
    assert by_user["user_a"]["model"] == "claude-sonnet-4"
    assert by_user["user_a"]["token_type"] == "uncached_input_tokens"


def test_unmatched_cost_preserved():
    """Tool costs with no usage metric still appear so invoice totals hold."""
    cost_rows = [
        {
            "usage_date": "2026-07-01",
            "workspace_id": "wrk_1",
            "model": None,
            "token_type": None,
            "cost_type": "code_execution",
            "service_tier": None,
            "context_window": None,
            "inference_geo": None,
            "description": "Code Execution Usage",
            "currency": "USD",
            "amount_cents": Decimal("50"),
            "cost_usd": Decimal("0.500000"),
        }
    ]
    output, stats = allocate_costs(cost_rows, [], {})
    assert len(output) == 1
    assert output[0]["principal_type"] == "unallocated"
    assert stats["reconcile_delta_usd"] == Decimal("0")
    assert Decimal(output[0]["cost_usd"]) == Decimal("0.500000")
