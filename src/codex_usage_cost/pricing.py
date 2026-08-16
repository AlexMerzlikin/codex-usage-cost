"""Offline pricing helpers for the local Codex Usage Cost CLI.

This is intentionally a small, dependency-free pricing boundary.  Prices are
read from the checked-in JSON file or a user-supplied override; this tool never
fetches pricing data and never uploads usage data.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


TIER_THRESHOLDS = (128_000, 200_000, 256_000, 272_000)
MODEL_TIER_SUFFIX = re.compile(r"(?:-xhigh|-high|-medium|-low|-max)$", re.IGNORECASE)
MODEL_SNAPSHOT_SUFFIX = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PROVIDER_PREFIX = re.compile(r"^(?:openai|azure|azure_ai|openai-codex)/", re.IGNORECASE)


def _finite_non_negative(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _read_rate(data: Mapping[str, Any], names: tuple[str, ...]) -> float:
    """Read either a per-token or per-million-token rate."""

    for name in names:
        if name not in data:
            continue
        value = _finite_non_negative(data[name])
        if value is None:
            continue
        return value / 1_000_000 if "million" in name else value
    return 0.0


def _rate_names(kind: str, threshold: int | None = None) -> tuple[str, ...]:
    suffix = "" if threshold is None else f"_above_{threshold // 1000}k"
    token_suffix = "" if threshold is None else f"_above_{threshold // 1000}k_tokens"
    if kind == "input":
        return (
            f"input_per_million{suffix}",
            f"input_cost_per_million_tokens{token_suffix}",
            f"input_cost_per_million{suffix}",
            f"input_cost_per_token{token_suffix}",
        )
    if kind == "output":
        return (
            f"output_per_million{suffix}",
            f"output_cost_per_million_tokens{token_suffix}",
            f"output_cost_per_million{suffix}",
            f"output_cost_per_token{token_suffix}",
        )
    if kind == "cache_read":
        return (
            f"cache_read_per_million{suffix}",
            f"cache_read_input_token_cost_per_million_tokens{token_suffix}",
            f"cache_read_input_token_cost_per_million{suffix}",
            f"cache_read_input_token_cost{token_suffix}",
        )
    return (
        f"cache_write_per_million{suffix}",
        f"cache_creation_input_token_cost_per_million_tokens{token_suffix}",
        f"cache_creation_input_token_cost_per_million{suffix}",
        f"cache_creation_input_token_cost{token_suffix}",
    )


@dataclass(frozen=True)
class ModelPrice:
    """Per-token rates with the same tier shape used by Tokscale."""

    input_rate: float
    output_rate: float
    cache_read_rate: float
    cache_write_rate: float
    input_tiers: tuple[float, ...]
    output_tiers: tuple[float, ...]
    cache_read_tiers: tuple[float, ...]
    cache_write_tiers: tuple[float, ...]

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ModelPrice":
        def rates(kind: str, thresholds: tuple[int, ...]) -> tuple[float, ...]:
            return tuple(_read_rate(data, _rate_names(kind, threshold)) for threshold in thresholds)

        return cls(
            input_rate=_read_rate(data, _rate_names("input")),
            output_rate=_read_rate(data, _rate_names("output")),
            cache_read_rate=_read_rate(data, _rate_names("cache_read")),
            cache_write_rate=_read_rate(data, _rate_names("cache_write")),
            input_tiers=rates("input", TIER_THRESHOLDS),
            output_tiers=rates("output", TIER_THRESHOLDS),
            cache_read_tiers=rates("cache_read", (200_000, 272_000)),
            cache_write_tiers=rates("cache_write", (200_000,)),
        )

    @property
    def is_usable(self) -> bool:
        return any(
            rate > 0
            for rate in (
                self.input_rate,
                self.output_rate,
                self.cache_read_rate,
                self.cache_write_rate,
                *self.input_tiers,
                *self.output_tiers,
                *self.cache_read_tiers,
                *self.cache_write_tiers,
            )
        )

    @staticmethod
    def _tiered_cost(tokens: int, base: float, tiers: tuple[float, ...], thresholds: tuple[int, ...]) -> float:
        remaining = max(0, tokens)
        lower_bound = 0
        active_rate = base
        cost = 0.0
        for threshold, tier_rate in zip(thresholds, tiers):
            if remaining <= threshold:
                return cost + max(0, remaining - lower_bound) * active_rate
            cost += (threshold - lower_bound) * active_rate
            lower_bound = threshold
            active_rate = tier_rate or active_rate
        return cost + max(0, remaining - lower_bound) * active_rate

    def estimate(self, usage: Any) -> float:
        """Estimate USD; reasoning is billed at the output rate for Codex."""

        output_tokens = max(0, usage.output) + max(0, usage.reasoning)
        return (
            self._tiered_cost(usage.input, self.input_rate, self.input_tiers, TIER_THRESHOLDS)
            + self._tiered_cost(usage.cache_read, self.cache_read_rate, self.cache_read_tiers, (200_000, 272_000))
            + self._tiered_cost(output_tokens, self.output_rate, self.output_tiers, TIER_THRESHOLDS)
            + self._tiered_cost(usage.cache_write, self.cache_write_rate, self.cache_write_tiers, (200_000,))
        )


def normalize_model(model: str) -> str:
    normalized = model.strip().lower()
    normalized = normalized.split("(", 1)[0].strip()
    normalized = MODEL_TIER_SUFFIX.sub("", normalized)
    return PROVIDER_PREFIX.sub("", normalized)


class PriceCatalog:
    """Exact-first model pricing with conservative base-model matching."""

    def __init__(self, prices: Mapping[str, ModelPrice]) -> None:
        self._prices = {normalize_model(model): price for model, price in prices.items() if price.is_usable}

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "PriceCatalog":
        models = data.get("models", data)
        if not isinstance(models, Mapping):
            return cls({})
        prices = {
            str(model): ModelPrice.from_mapping(value)
            for model, value in models.items()
            if isinstance(value, Mapping)
        }
        return cls(prices)

    @classmethod
    def from_file(cls, path: Path) -> "PriceCatalog":
        import json

        with path.open("r", encoding="utf-8") as handle:
            return cls.from_mapping(json.load(handle))

    def resolve(self, model: str) -> ModelPrice | None:
        normalized = normalize_model(model)
        exact = self._prices.get(normalized)
        if exact is not None:
            return exact

        # Match dated snapshots, but do not price an unrelated variant (for
        # example gpt-5.5-cyber) from a shorter base model by accident.
        candidates = sorted(self._prices.items(), key=lambda item: len(item[0]), reverse=True)
        for base_model, price in candidates:
            if normalized.startswith(f"{base_model}-") and MODEL_SNAPSHOT_SUFFIX.fullmatch(
                normalized[len(base_model) + 1 :]
            ):
                return price
        return None
