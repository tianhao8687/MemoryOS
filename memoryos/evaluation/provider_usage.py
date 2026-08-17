from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from memoryos.context.token_meter import canonical_json


class UsageSource(StrEnum):
    PROVIDER_EXACT = "provider_exact"
    TOKENIZER_EXACT = "tokenizer_exact"
    ESTIMATED = "estimated"


class CachePhase(StrEnum):
    COLD = "cold"
    WARM = "warm"


class ModelPricing(BaseModel):
    """One immutable model price row, expressed in USD per one million tokens."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(min_length=1, max_length=120)
    model: str = Field(min_length=1, max_length=300)
    cache_miss_input_usd_per_million: Decimal = Field(ge=0)
    cache_hit_input_usd_per_million: Decimal | None = Field(default=None, ge=0)
    output_usd_per_million: Decimal = Field(ge=0)


class PricingSnapshot(BaseModel):
    """Frozen prices supplied at run start; reports never fetch live prices."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    snapshot_id: str = Field(min_length=1, max_length=200)
    effective_at: datetime
    source_url: str | None = Field(default=None, max_length=2000)
    prices: tuple[ModelPricing, ...]

    @field_validator("effective_at")
    @classmethod
    def require_aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("pricing effective_at must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def require_unique_rows(self) -> PricingSnapshot:
        keys = [(row.provider, row.model) for row in self.prices]
        if len(keys) != len(set(keys)):
            raise ValueError("pricing rows must be unique by provider and model")
        return self

    def find(self, provider: str, model: str) -> ModelPricing | None:
        return next(
            (row for row in self.prices if row.provider == provider and row.model == model),
            None,
        )

    def digest(self) -> str:
        return hashlib.sha256(
            canonical_json(self.model_dump(mode="json")).encode("utf-8")
        ).hexdigest()


class ProviderUsageRecord(BaseModel):
    """Authoritative accounting for exactly one model request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    run_id: str
    task_id: str
    condition: str
    cache_phase: CachePhase
    session_id: str
    step_index: int = Field(ge=0)
    provider: str
    model: str
    input_tokens: int = Field(ge=0)
    cache_hit_tokens: int | None = Field(default=None, ge=0)
    cache_miss_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int = Field(ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    cost_usd: Decimal | None = Field(default=None, ge=0)
    ttft_seconds: float | None = Field(default=None, ge=0)
    latency_seconds: float = Field(ge=0)
    usage_source: UsageSource
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    response_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_bytes: int = Field(ge=0)
    memory_payload_tokens: int | None = Field(default=None, ge=0)
    memory_wrapper_tokens: int | None = Field(default=None, ge=0)
    memory_tool_schema_tokens: int | None = Field(default=None, ge=0)
    other_tool_schema_tokens: int | None = Field(default=None, ge=0)
    cache_namespace_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_cache_split(self) -> ProviderUsageRecord:
        split = (self.cache_hit_tokens, self.cache_miss_tokens)
        if (split[0] is None) != (split[1] is None):
            raise ValueError("cache hit and miss token fields must be both present or both null")
        if (
            split[0] is not None
            and split[1] is not None
            and split[0] + split[1] != self.input_tokens
        ):
            raise ValueError("cache hit plus miss tokens must equal total input tokens")
        if self.reasoning_tokens is not None and self.reasoning_tokens > self.output_tokens:
            raise ValueError("reasoning tokens are already included in output tokens")
        return self


class UsageTotals(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    requests: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    cache_hit_tokens: int | None = Field(default=None, ge=0)
    cache_miss_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int = Field(ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    cost_usd: Decimal | None = Field(default=None, ge=0)
    ttft_seconds_total: float | None = Field(default=None, ge=0)
    latency_seconds: float = Field(ge=0)

    @property
    def cache_hit_rate(self) -> float | None:
        if self.cache_hit_tokens is None or self.cache_miss_tokens is None:
            return None
        denominator = self.cache_hit_tokens + self.cache_miss_tokens
        return None if denominator == 0 else self.cache_hit_tokens / denominator


def map_provider_usage(
    *,
    raw_usage: dict[str, Any] | None,
    provider: str,
    model: str,
    pricing: PricingSnapshot | None,
    fallback_input_tokens: int | None,
    fallback_output_tokens: int | None,
    fallback_source: UsageSource | None,
) -> tuple[int, int | None, int | None, int, int | None, Decimal | None, UsageSource]:
    """Map OpenAI, DeepSeek, and Ollama usage without inventing cache fields."""

    usage = raw_usage or {}
    provider_input = _integer(usage.get("prompt_tokens"))
    provider_output = _integer(usage.get("completion_tokens"))
    if provider_input is None:
        provider_input = _integer(usage.get("input_tokens"))
    if provider_output is None:
        provider_output = _integer(usage.get("output_tokens"))
    # Native Ollama response aliases are accepted when an OpenAI-compatible
    # deployment returns them alongside the otherwise compatible response.
    if provider_input is None:
        provider_input = _integer(usage.get("prompt_eval_count"))
    if provider_output is None:
        provider_output = _integer(usage.get("eval_count"))

    if provider_input is not None and provider_output is not None:
        source = UsageSource.PROVIDER_EXACT
        input_tokens = provider_input
        output_tokens = provider_output
    else:
        if (
            fallback_input_tokens is None
            or fallback_output_tokens is None
            or fallback_source is None
        ):
            raise ValueError("provider omitted usage and no tokenizer accounting is available")
        source = fallback_source
        # Preserve every authoritative field the provider did return. The
        # record source remains the fallback source because the request as a
        # whole required tokenizer accounting for at least one field.
        input_tokens = provider_input if provider_input is not None else fallback_input_tokens
        output_tokens = provider_output if provider_output is not None else fallback_output_tokens

    hit = _integer(usage.get("prompt_cache_hit_tokens"))
    miss = _integer(usage.get("prompt_cache_miss_tokens"))
    details = usage.get("prompt_tokens_details")
    if hit is None and isinstance(details, dict):
        hit = _integer(details.get("cached_tokens"))
        if hit is not None and provider_input is not None:
            miss = provider_input - hit
    if hit is None or miss is None or hit < 0 or miss < 0 or hit + miss != input_tokens:
        hit = None
        miss = None

    completion_details = usage.get("completion_tokens_details")
    reasoning = (
        _integer(completion_details.get("reasoning_tokens"))
        if isinstance(completion_details, dict)
        else _integer(usage.get("reasoning_tokens"))
    )
    if reasoning is not None and reasoning > output_tokens:
        reasoning = None

    cost = calculate_cost(
        pricing.find(provider, model) if pricing is not None else None,
        input_tokens=input_tokens,
        cache_hit_tokens=hit,
        cache_miss_tokens=miss,
        output_tokens=output_tokens,
    )
    return input_tokens, hit, miss, output_tokens, reasoning, cost, source


def calculate_cost(
    price: ModelPricing | None,
    *,
    input_tokens: int,
    cache_hit_tokens: int | None,
    cache_miss_tokens: int | None,
    output_tokens: int,
) -> Decimal | None:
    if price is None:
        return None
    million = Decimal(1_000_000)
    if cache_hit_tokens is not None and cache_miss_tokens is not None:
        if price.cache_hit_input_usd_per_million is None:
            return None
        input_cost = (
            Decimal(cache_miss_tokens) * price.cache_miss_input_usd_per_million
            + Decimal(cache_hit_tokens) * price.cache_hit_input_usd_per_million
        ) / million
    elif (
        price.cache_hit_input_usd_per_million is None
        or price.cache_hit_input_usd_per_million == price.cache_miss_input_usd_per_million
    ):
        input_cost = Decimal(input_tokens) * price.cache_miss_input_usd_per_million / million
    else:
        # A split-price provider cannot be costed honestly without its actual
        # cache split. Reporting total tokens is still valid; cost remains null.
        return None
    output_cost = Decimal(output_tokens) * price.output_usd_per_million / million
    return input_cost + output_cost


def aggregate_usage(records: list[ProviderUsageRecord]) -> UsageTotals:
    hits = _sum_optional_int([record.cache_hit_tokens for record in records])
    misses = _sum_optional_int([record.cache_miss_tokens for record in records])
    return UsageTotals(
        requests=len(records),
        input_tokens=sum(record.input_tokens for record in records),
        cache_hit_tokens=hits if misses is not None else None,
        cache_miss_tokens=misses if hits is not None else None,
        output_tokens=sum(record.output_tokens for record in records),
        reasoning_tokens=_sum_optional_int([record.reasoning_tokens for record in records]),
        cost_usd=_sum_optional_decimal([record.cost_usd for record in records]),
        ttft_seconds_total=_sum_optional_float([record.ttft_seconds for record in records]),
        latency_seconds=sum(record.latency_seconds for record in records),
    )


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _sum_optional_int(values: list[int | None]) -> int | None:
    if not values:
        return None
    total = 0
    for value in values:
        if value is None:
            return None
        total += value
    return total


def _sum_optional_float(values: list[float | None]) -> float | None:
    if not values:
        return None
    total = 0.0
    for value in values:
        if value is None:
            return None
        total += value
    return total


def _sum_optional_decimal(values: list[Decimal | None]) -> Decimal | None:
    if not values:
        return None
    total = Decimal(0)
    for value in values:
        if value is None:
            return None
        total += value
    return total


__all__ = [
    "CachePhase",
    "ModelPricing",
    "PricingSnapshot",
    "ProviderUsageRecord",
    "UsageSource",
    "UsageTotals",
    "aggregate_usage",
    "calculate_cost",
    "map_provider_usage",
]
