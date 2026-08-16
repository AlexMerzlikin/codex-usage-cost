"""Task-level comparison metrics for the local Codex usage cost report."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from .pricing import PriceCatalog


def _round_cost(value: float) -> float:
    return round(value, 12)


def _average(value: float, count: int) -> float:
    return round(value / count, 2) if count else 0.0


def _effort(message: Any) -> str:
    value = getattr(message, "effort", "unknown")
    return str(value or "unknown").strip().lower() or "unknown"


def _bucket() -> dict[str, Any]:
    return {
        "tasks": set(),
        "sessions": set(),
        "tokens": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
        "messages": 0,
        "cost": 0.0,
        "cache_read_cost": 0.0,
    }


def _accumulate(
    bucket: dict[str, Any],
    task: tuple[str, int],
    message: Any,
    token_count: int,
    cost: float,
    cache_read_cost: float,
) -> None:
    usage = message.usage
    bucket["tasks"].add(task)
    bucket["sessions"].add(str(message.session_id))
    bucket["tokens"] += token_count
    bucket["input_tokens"] += usage.input
    bucket["output_tokens"] += usage.output
    bucket["cache_read_tokens"] += usage.cache_read
    bucket["cache_write_tokens"] += usage.cache_write
    bucket["reasoning_tokens"] += usage.reasoning
    bucket["messages"] += 1
    bucket["cost"] += cost
    bucket["cache_read_cost"] += cache_read_cost


def _task_key(message: Any, active: dict[str, tuple[str, int]], next_index: dict[str, int]) -> tuple[str, int]:
    session_id = str(message.session_id)
    if session_id not in active or bool(message.turn_start):
        next_index[session_id] = next_index.get(session_id, 0) + 1
        active[session_id] = (session_id, next_index[session_id])
    return active[session_id]


def _serialize_bucket(bucket: dict[str, Any]) -> dict[str, Any]:
    tasks = len(bucket["tasks"])
    context_tokens = bucket["input_tokens"] + bucket["cache_read_tokens"]
    return {
        "tasks": tasks,
        "tokens": bucket["tokens"],
        "average_tokens_per_task": _average(bucket["tokens"], tasks),
        "estimated_cost_usd": _round_cost(bucket["cost"]),
        "average_cost_per_task_usd": _round_cost(bucket["cost"] / tasks) if tasks else 0.0,
        "sessions": len(bucket["sessions"]),
        "messages": bucket["messages"],
        "input_tokens": bucket["input_tokens"],
        "output_tokens": bucket["output_tokens"],
        "cache_read_tokens": bucket["cache_read_tokens"],
        "cache_write_tokens": bucket["cache_write_tokens"],
        "reasoning_tokens": bucket["reasoning_tokens"],
        "cache_read_cost_usd": _round_cost(bucket["cache_read_cost"]),
        "cache_hit_rate": round(bucket["cache_read_tokens"] / context_tokens, 6) if context_tokens else 0.0,
    }


def build_comparison(messages: Iterable[Any], catalog: PriceCatalog) -> dict[str, Any]:
    """Aggregate usage by logical task, then compare models and days."""

    active: dict[str, tuple[str, int]] = {}
    next_index: dict[str, int] = {}
    tasks: set[tuple[str, int]] = set()
    models: dict[str, dict[str, Any]] = defaultdict(_bucket)
    days: dict[str, dict[str, Any]] = defaultdict(_bucket)
    efforts: dict[str, dict[str, Any]] = defaultdict(_bucket)
    total_tokens = 0
    total_cost = 0.0

    for message in messages:
        task = _task_key(message, active, next_index)
        model = str(message.model)
        day = str(message.date)
        usage = message.usage
        price = catalog.resolve(model)
        cost = price.estimate(usage) if price is not None else 0.0
        token_count = usage.total
        effort = _effort(message)
        cache_read_cost = 0.0
        if price is not None:
            cache_read_cost = price._tiered_cost(
                usage.cache_read,
                price.cache_read_rate,
                price.cache_read_tiers,
                (200_000, 272_000),
            )

        tasks.add(task)
        total_tokens += token_count
        total_cost += cost
        for bucket in (models[model], days[day], efforts[effort]):
            _accumulate(bucket, task, message, token_count, cost, cache_read_cost)

        by_effort = models[model].setdefault("by_effort", defaultdict(_bucket))
        _accumulate(by_effort[effort], task, message, token_count, cost, cache_read_cost)

    by_model = {}
    for model, bucket in sorted(models.items()):
        serialized = _serialize_bucket(bucket)
        serialized["by_effort"] = {
            effort: _serialize_bucket(effort_bucket)
            for effort, effort_bucket in sorted(bucket.get("by_effort", {}).items())
        }
        by_model[model] = serialized
    by_day = {day: _serialize_bucket(bucket) for day, bucket in sorted(days.items())}
    by_effort = {effort: _serialize_bucket(bucket) for effort, bucket in sorted(efforts.items())}
    task_count = len(tasks)
    known_messages = sum(bucket["messages"] for effort, bucket in efforts.items() if effort != "unknown")
    return {
        "tasks": task_count,
        "average_tokens_per_task": _average(total_tokens, task_count),
        "average_cost_per_task_usd": _round_cost(total_cost / task_count) if task_count else 0.0,
        "by_model": by_model,
        "by_day": by_day,
        "by_effort": by_effort,
        "effort_coverage": {
            "known_messages": known_messages,
            "unknown_messages": max(0, sum(bucket["messages"] for bucket in efforts.values()) - known_messages),
            "total_messages": sum(bucket["messages"] for bucket in efforts.values()),
        },
    }


def attach_comparison(report: dict[str, Any], messages: Iterable[Any], catalog: PriceCatalog) -> dict[str, Any]:
    report["comparison"] = build_comparison(messages, catalog)
    return report
