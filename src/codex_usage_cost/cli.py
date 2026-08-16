#!/usr/bin/env python3
"""Local Codex usage cost report.

The Codex event/state handling is a focused, dependency-free port of the
Codex-specific behavior in Tokscale.  It is deliberately local-only: no
credentials, login, upload, or pricing-network access is implemented here.
See NOTICE for the upstream MIT attribution.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .html import default_html_path, write_html_report
from .metrics import attach_comparison
from .pricing import PriceCatalog
from .report_io import export_report_or_exit, import_reports_or_exit

DEFAULT_PRICING_FILE = Path(__file__).with_name("prices.json")

def _non_negative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, number)

def _timestamp(raw: Any, fallback: datetime) -> datetime:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        seconds = float(raw) / (1000 if raw > 10_000_000_000 else 1)
        return datetime.fromtimestamp(seconds, tz=timezone.utc).astimezone()
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone()
        except ValueError:
            pass
    return fallback.astimezone()

def _model_from(payload: Mapping[str, Any], info: Mapping[str, Any] | None = None) -> str | None:
    for container in (payload, info or {}):
        for key in ("model", "model_name"):
            value = container.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        model_info = container.get("model_info")
        if isinstance(model_info, Mapping):
            value = model_info.get("slug")
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _effort_from(payload: Mapping[str, Any]) -> str | None:
    """Read the effective reasoning effort from a turn context."""

    candidates: list[Any] = [payload.get("effort")]
    collaboration_mode = payload.get("collaboration_mode")
    if isinstance(collaboration_mode, Mapping):
        settings = collaboration_mode.get("settings")
        if isinstance(settings, Mapping):
            candidates.append(settings.get("reasoning_effort"))
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip().lower()
    return None


def _provider_for(model: str | None, explicit: str | None) -> str:
    if explicit and explicit.strip():
        return explicit.strip()
    if model and "/" in model:
        prefix = model.split("/", 1)[0].strip()
        if prefix:
            return prefix
    return "openai"

@dataclass(frozen=True)
class RawUsage:
    """The cumulative Codex counters before cache normalization."""

    input: int = 0
    output: int = 0
    cached: int = 0
    reasoning: int = 0

    @classmethod
    def from_mapping(cls, value: Any) -> "RawUsage | None":
        if not isinstance(value, Mapping):
            return None
        return cls(
            input=_non_negative_int(value.get("input_tokens")),
            output=_non_negative_int(value.get("output_tokens")),
            cached=max(
                _non_negative_int(value.get("cached_input_tokens")),
                _non_negative_int(value.get("cache_read_input_tokens")),
            ),
            reasoning=_non_negative_int(value.get("reasoning_output_tokens")),
        )

    @property
    def total(self) -> int:
        return self.input + self.output + self.cached + self.reasoning

    def delta_from(self, previous: "RawUsage") -> "RawUsage | None":
        if any(
            current < old
            for current, old in zip(
                (self.input, self.output, self.cached, self.reasoning),
                (previous.input, previous.output, previous.cached, previous.reasoning),
            )
        ):
            return None
        return RawUsage(
            self.input - previous.input,
            self.output - previous.output,
            self.cached - previous.cached,
            self.reasoning - previous.reasoning,
        )

    def looks_like_stale_regression(self, previous: "RawUsage", last: "RawUsage") -> bool:
        if min(previous.total, self.total, last.total) <= 0:
            return False
        return self.total * 100 >= previous.total * 98 or self.total + last.total * 2 >= previous.total

    def add(self, other: "RawUsage") -> "RawUsage":
        return RawUsage(
            self.input + other.input,
            self.output + other.output,
            self.cached + other.cached,
            self.reasoning + other.reasoning,
        )

    def to_usage(self) -> "TokenUsage":
        cached = min(self.cached, self.input)
        return TokenUsage(
            input=self.input - cached,
            output=self.output,
            cache_read=cached,
            reasoning=self.reasoning,
        )

@dataclass(frozen=True)
class TokenUsage:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    reasoning: int = 0

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_write + self.reasoning

    def add(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            self.input + other.input,
            self.output + other.output,
            self.cache_read + other.cache_read,
            self.cache_write + other.cache_write,
            self.reasoning + other.reasoning,
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "input": self.input,
            "output": self.output,
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
            "reasoning": self.reasoning,
            "total": self.total,
        }

@dataclass(frozen=True)
class CodexMessage:
    session_id: str
    model: str
    provider: str
    timestamp: datetime
    usage: TokenUsage
    source_path: str
    workspace: str | None
    headless: bool
    turn_start: bool
    dedup_key: tuple[Any, ...]
    effort: str = "unknown"

    @property
    def date(self) -> str:
        return self.timestamp.date().isoformat()

@dataclass(frozen=True)
class ParsedFile:
    session_id: str
    forked_from: str | None
    messages: tuple[CodexMessage, ...]

def _codex_source_is_exec(source: Any) -> bool:
    return source == "exec"

def _forked_from(payload: Mapping[str, Any]) -> str | None:
    direct = payload.get("forked_from_id")
    if isinstance(direct, str) and direct:
        return direct
    source = payload.get("source")
    if isinstance(source, Mapping):
        subagent = source.get("subagent")
        if isinstance(subagent, Mapping):
            thread_spawn = subagent.get("thread_spawn")
            if isinstance(thread_spawn, Mapping):
                parent = thread_spawn.get("parent_thread_id")
                if isinstance(parent, str) and parent:
                    return parent
    return None
def _is_human_message(payload: Mapping[str, Any]) -> bool:
    message = payload.get("message")
    return isinstance(message, str) and bool(message.strip()) and not message.lstrip().startswith("<")
def _token_increment(
    total: RawUsage | None,
    last: RawUsage | None,
    previous: RawUsage | None,
) -> tuple[TokenUsage | None, RawUsage | None]:
    """Mirror Tokscale's last-first, total-for-dedup decision table."""

    if total is not None and last is not None and previous is not None:
        if total == previous:
            return None, previous
        if total.delta_from(previous) is None and total.looks_like_stale_regression(previous, last):
            return None, previous
        return last.to_usage(), total
    if total is not None and last is not None:
        return last.to_usage(), total
    if total is not None and previous is not None:
        if total == previous:
            return None, previous
        delta = total.delta_from(previous)
        return (delta.to_usage(), total) if delta is not None else (None, total)
    if total is not None:
        return total.to_usage(), total
    if last is not None and previous is not None:
        return last.to_usage(), previous.add(last)
    if last is not None:
        return last.to_usage(), None
    return None, previous

def parse_codex_file(path: Path) -> ParsedFile:
    fallback = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    session_id = path.stem
    forked_from: str | None = None
    explicit_provider: str | None = None
    workspace: str | None = None
    current_model: str | None = None
    current_effort = "unknown"
    previous_totals: RawUsage | None = None
    pending_turn_start = False
    headless = False
    messages: list[CodexMessage] = []

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, Mapping):
                continue
            payload = entry.get("payload")
            if not isinstance(payload, Mapping):
                continue
            entry_type = entry.get("type")

            if entry_type == "session_meta":
                if isinstance(payload.get("id"), str) and payload["id"]:
                    session_id = payload["id"]
                forked_from = _forked_from(payload) or forked_from
                source = payload.get("source")
                headless = headless or _codex_source_is_exec(source)
                provider = payload.get("model_provider")
                if isinstance(provider, str) and provider.strip():
                    explicit_provider = provider.strip()
                cwd = payload.get("cwd")
                if isinstance(cwd, str) and cwd.strip():
                    workspace = cwd.strip()

            if entry_type == "turn_context":
                current_model = _model_from(payload) or current_model
                current_effort = _effort_from(payload) or current_effort

            if entry_type == "event_msg" and payload.get("type") == "user_message":
                if _is_human_message(payload):
                    pending_turn_start = True

            if entry_type != "event_msg" or payload.get("type") != "token_count":
                continue
            info = payload.get("info")
            if not isinstance(info, Mapping):
                continue
            model = _model_from(payload, info) or current_model
            current_model = model or current_model
            total = RawUsage.from_mapping(info.get("total_token_usage"))
            last = RawUsage.from_mapping(info.get("last_token_usage"))
            usage, next_totals = _token_increment(total, last, previous_totals)
            if usage is None or usage.total == 0:
                continue
            previous_totals = next_totals

            timestamp = _timestamp(entry.get("timestamp"), fallback)
            resolved_model = model or "unknown"
            provider = _provider_for(resolved_model, explicit_provider)
            scope = forked_from or session_id
            if total is not None:
                dedup_key = (scope, resolved_model, "total", total)
            else:
                dedup_key = (scope, resolved_model, "usage", usage, timestamp.isoformat())
            messages.append(
                CodexMessage(
                    session_id=session_id,
                    model=resolved_model,
                    provider=provider,
                    timestamp=timestamp,
                    usage=usage,
                    source_path=str(path),
                    workspace=workspace,
                    headless=headless,
                    turn_start=pending_turn_start,
                    dedup_key=dedup_key,
                    effort=current_effort,
                )
            )
            pending_turn_start = False

    return ParsedFile(session_id, forked_from, tuple(messages))

def discover_codex_files(home: Path | None = None, explicit_paths: Iterable[Path] = ()) -> list[Path]:
    roots = [Path(path).expanduser() for path in explicit_paths]
    if not roots:
        # Task 274: use the configured Codex data root before the legacy home fallback.
        if home is not None:
            codex_root = home.expanduser() / ".codex"
        else:
            configured_root = os.environ.get("CODEX_HOME")
            codex_root = Path(configured_root).expanduser() if configured_root else Path.home() / ".codex"
        roots = [codex_root / "sessions", codex_root / "archived_sessions"]
    files: set[Path] = set()
    for root in roots:
        if root.is_file() and root.suffix == ".jsonl":
            files.add(root.resolve())
        elif root.is_dir():
            files.update(path.resolve() for path in root.rglob("*.jsonl"))
    return sorted(files)

def scan_codex_files(paths: Iterable[Path]) -> tuple[tuple[CodexMessage, ...], tuple[str, ...]]:
    messages: list[CodexMessage] = []
    seen_keys: set[tuple[Any, ...]] = set()
    errors: list[str] = []
    for path in paths:
        try:
            parsed = parse_codex_file(path)
        except OSError as error:
            errors.append(f"{path}: {error}")
            continue
        for message in parsed.messages:
            if message.dedup_key in seen_keys:
                continue
            seen_keys.add(message.dedup_key)
            messages.append(message)
    return tuple(messages), tuple(errors)

def _usage_bucket(bucket: dict[str, Any], usage: TokenUsage, cost: float, message: CodexMessage) -> None:
    bucket["tokens"] = bucket["tokens"].add(usage)
    bucket["cost"] += cost
    bucket["messages"] += 1
    bucket["models"].add(message.model)


def _serialize_bucket(bucket: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "tokens": bucket["tokens"].as_dict(),
        "estimated_cost_usd": round(bucket["cost"], 12),
        "messages": bucket["messages"],
        "models": sorted(bucket["models"]),
    }
def build_report(
    messages: Iterable[CodexMessage],
    catalog: PriceCatalog,
    since: str | None = None,
    until: str | None = None,
) -> dict[str, Any]:
    total_usage = TokenUsage()
    total_cost = 0.0
    selected: list[CodexMessage] = []
    by_model: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"tokens": TokenUsage(), "cost": 0.0, "messages": 0, "models": set()}
    )
    by_day: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"tokens": TokenUsage(), "cost": 0.0, "messages": 0, "models": set()}
    )
    unpriced: set[str] = set()

    for message in messages:
        if since and message.date < since:
            continue
        if until and message.date > until:
            continue
        selected.append(message)
        price = catalog.resolve(message.model)
        cost = price.estimate(message.usage) if price is not None else 0.0
        if price is None and message.usage.total > 0:
            unpriced.add(message.model)
        total_usage = total_usage.add(message.usage)
        total_cost += cost
        _usage_bucket(by_model[message.model], message.usage, cost, message)
        _usage_bucket(by_day[message.date], message.usage, cost, message)

    sessions = {message.session_id for message in selected}
    return attach_comparison({
        "source": "codex-cli",
        "files_scanned": sorted({message.source_path for message in selected}),
        "sessions": len(sessions),
        "messages": len(selected),
        "tokens": total_usage.as_dict(),
        "estimated_cost_usd": round(total_cost, 12),
        "unpriced_models": sorted(unpriced),
        "by_model": {model: _serialize_bucket(bucket) for model, bucket in sorted(by_model.items())},
        "by_day": {day: _serialize_bucket(bucket) for day, bucket in sorted(by_day.items())},
    }, selected, catalog)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read local Codex CLI JSONL sessions and estimate token cost.")
    parser.add_argument("--home", type=Path, help="Alternate home directory containing .codex/")
    parser.add_argument("--path", action="append", type=Path, help="Explicit JSONL file or directory; repeatable")
    parser.add_argument("--pricing-file", type=Path, default=DEFAULT_PRICING_FILE, help="Local JSON pricing file")
    parser.add_argument("--since", help="Inclusive local date, YYYY-MM-DD")
    parser.add_argument("--until", help="Inclusive local date, YYYY-MM-DD")
    parser.add_argument("--json", action="store_true", dest="json_output", help="Print machine-readable JSON")
    parser.add_argument("--tui", action="store_true", help="Launch the interactive terminal dashboard")
    html_path = default_html_path()
    parser.add_argument(
        "--html",
        nargs="?",
        const=html_path,
        default=html_path,
        type=Path,
        metavar="PATH",
        help=f"Write the HTML dashboard (default: {html_path})",
    )
    parser.add_argument("--export-report", type=Path, help="Write a portable JSON report for sharing or backup")
    parser.add_argument("--import-report", action="append", type=Path, help="Import a portable report; repeatable")
    parser.add_argument("--device-name", help="Optional device label stored in an exported report")
    parser.add_argument("--verbose", action="store_true", help="Print ignored files and pricing warnings to stderr")
    return parser


def _validate_date(value: str | None, flag: str) -> str | None:
    if value is None:
        return None
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as error:
        raise SystemExit(f"{flag} must be YYYY-MM-DD: {error}") from error
    return value


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.device_name and not args.export_report:
        raise SystemExit("--device-name requires --export-report")
    since = _validate_date(args.since, "--since")
    until = _validate_date(args.until, "--until")
    if since and until and since > until:
        raise SystemExit("--since must not be later than --until")

    paths = discover_codex_files(args.home, args.path or ())
    errors: tuple[str, ...] = ()
    messages: tuple[CodexMessage, ...] = ()
    if paths:
        messages, errors = scan_codex_files(paths)
    messages, imported_reports = import_reports_or_exit(messages, args.import_report or (), CodexMessage, TokenUsage)

    try:
        catalog = PriceCatalog.from_file(args.pricing_file)
    except (OSError, json.JSONDecodeError) as error:
        catalog = PriceCatalog({})
        if args.verbose:
            print(f"pricing file unavailable: {args.pricing_file}: {error}", file=sys.stderr)

    report = build_report(messages, catalog, since, until)
    report["files_found"] = len(paths)
    report["reports_imported"] = len(imported_reports)
    if errors:
        report["scan_errors"] = list(errors)
    if args.export_report:
        export_report_or_exit(args.export_report, report, messages, since, until, args.device_name)
    if args.tui:
        from .tui import run_tui

        return run_tui(report)
    if args.json_output:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        try:
            html_path = write_html_report(args.html, report)
        except OSError as error:
            raise SystemExit(f"cannot write HTML report: {error}") from error
        print(f"HTML report: {html_path.resolve()}")
    if not paths and not args.json_output:
        print("No Codex JSONL files found under the selected path.")
    if args.verbose and errors:
        for error in errors:
            print(f"scan warning: {error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
