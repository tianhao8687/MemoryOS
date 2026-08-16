from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from memoryos.evaluation.provider_usage import (
    ModelPricing,
    PricingSnapshot,
    UsageSource,
    calculate_cost,
    map_provider_usage,
)


def pricing() -> PricingSnapshot:
    return PricingSnapshot(
        snapshot_id="deepseek-2026-08-15",
        effective_at=datetime(2026, 8, 15, tzinfo=UTC),
        prices=(
            ModelPricing(
                provider="deepseek",
                model="deepseek-v4-flash",
                cache_miss_input_usd_per_million=Decimal("0.14"),
                cache_hit_input_usd_per_million=Decimal("0.0028"),
                output_usd_per_million=Decimal("0.28"),
            ),
        ),
    )


@pytest.mark.v23
def test_deepseek_usage_maps_exact_cache_split_reasoning_and_cost() -> None:
    mapped = map_provider_usage(
        raw_usage={
            "prompt_tokens": 1000,
            "prompt_cache_hit_tokens": 750,
            "prompt_cache_miss_tokens": 250,
            "completion_tokens": 100,
            "completion_tokens_details": {"reasoning_tokens": 80},
        },
        provider="deepseek",
        model="deepseek-v4-flash",
        pricing=pricing(),
        fallback_input_tokens=None,
        fallback_output_tokens=None,
        fallback_source=None,
    )

    assert mapped == (
        1000,
        750,
        250,
        100,
        80,
        Decimal("0.0000651"),
        UsageSource.PROVIDER_EXACT,
    )


@pytest.mark.v23
def test_provider_without_cache_split_keeps_cache_and_split_price_cost_null() -> None:
    mapped = map_provider_usage(
        raw_usage={"prompt_tokens": 1000, "completion_tokens": 100},
        provider="deepseek",
        model="deepseek-v4-flash",
        pricing=pricing(),
        fallback_input_tokens=999,
        fallback_output_tokens=99,
        fallback_source=UsageSource.TOKENIZER_EXACT,
    )

    assert mapped[:5] == (1000, None, None, 100, None)
    assert mapped[5] is None
    assert mapped[6] is UsageSource.PROVIDER_EXACT


@pytest.mark.v23
def test_missing_provider_usage_uses_only_explicit_exact_tokenizer_fallback() -> None:
    mapped = map_provider_usage(
        raw_usage=None,
        provider="ollama",
        model="qwen3-8b",
        pricing=None,
        fallback_input_tokens=321,
        fallback_output_tokens=45,
        fallback_source=UsageSource.TOKENIZER_EXACT,
    )
    assert mapped == (321, None, None, 45, None, None, UsageSource.TOKENIZER_EXACT)

    with pytest.raises(ValueError, match="no tokenizer accounting"):
        map_provider_usage(
            raw_usage=None,
            provider="ollama",
            model="qwen3-8b",
            pricing=None,
            fallback_input_tokens=None,
            fallback_output_tokens=None,
            fallback_source=None,
        )


@pytest.mark.v23
def test_partial_provider_usage_keeps_authoritative_fields() -> None:
    mapped = map_provider_usage(
        raw_usage={"prompt_tokens": 333},
        provider="ollama",
        model="qwen3-8b",
        pricing=None,
        fallback_input_tokens=321,
        fallback_output_tokens=45,
        fallback_source=UsageSource.TOKENIZER_EXACT,
    )

    assert mapped == (333, None, None, 45, None, None, UsageSource.TOKENIZER_EXACT)


@pytest.mark.v23
def test_equal_input_prices_can_be_costed_without_a_cache_split() -> None:
    price = ModelPricing(
        provider="local",
        model="qwen",
        cache_miss_input_usd_per_million=Decimal("0"),
        cache_hit_input_usd_per_million=Decimal("0"),
        output_usd_per_million=Decimal("0"),
    )
    assert calculate_cost(
        price,
        input_tokens=10,
        cache_hit_tokens=None,
        cache_miss_tokens=None,
        output_tokens=5,
    ) == Decimal(0)
