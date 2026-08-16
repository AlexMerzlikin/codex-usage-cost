"""Portable export/import for local Codex token usage reports."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


REPORT_FORMAT = "codex-usage-cost-report"
SUPPORTED_REPORT_FORMATS = {REPORT_FORMAT, "codex-usage-report"}
SCHEMA_VERSION = 1


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _record_without_id(message: Any) -> dict[str, Any]:
    return {
        "session_id": str(message.session_id),
        "model": str(message.model),
        "provider": str(message.provider),
        "timestamp": message.timestamp.astimezone(timezone.utc).isoformat(),
        "usage": message.usage.as_dict(),
        "turn_start": bool(message.turn_start),
        "headless": bool(message.headless),
    }


def portable_message_id(message: Any) -> str:
    """Return a stable ID that is independent of device paths."""

    payload = _canonical_json(_record_without_id(message)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _message_record(message: Any) -> dict[str, Any]:
    record = _record_without_id(message)
    record["timestamp"] = message.timestamp.isoformat()
    record["effort"] = str(getattr(message, "effort", "unknown") or "unknown")
    record["id"] = portable_message_id(message)
    return record


def deduplicate_portable_messages(messages: Iterable[Any]) -> tuple[Any, ...]:
    seen: set[str] = set()
    unique: list[Any] = []
    for message in messages:
        message_id = portable_message_id(message)
        if message_id in seen:
            continue
        seen.add(message_id)
        unique.append(message)
    return tuple(unique)


def export_report(
    path: Path,
    report: dict[str, Any],
    messages: Iterable[Any],
    device_name: str | None = None,
) -> dict[str, Any]:
    records = [_message_record(message) for message in messages]
    record_ids = sorted(record["id"] for record in records)
    report_id = hashlib.sha256(_canonical_json(record_ids).encode("utf-8")).hexdigest()
    exported_report = dict(report)
    exported_report["files_scanned"] = []
    exported_report.pop("scan_errors", None)
    exported_report["files_found"] = len(records)
    envelope = {
        "format": REPORT_FORMAT,
        "schema_version": SCHEMA_VERSION,
        "report_id": report_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "device_name": device_name or None,
        "record_count": len(records),
        "report": exported_report,
        "messages": records,
    }
    target = path.expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(envelope, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return envelope


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("message timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"invalid message timestamp: {value}") from error
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc)


def load_report_messages(path: Path, message_type: Any, usage_type: Any) -> tuple[tuple[Any, ...], dict[str, Any]]:
    target = path.expanduser()
    with target.open("r", encoding="utf-8") as handle:
        envelope = json.load(handle)
    if not isinstance(envelope, dict) or envelope.get("format") not in SUPPORTED_REPORT_FORMATS:
        raise ValueError(f"{target} is not a {REPORT_FORMAT} export")
    if envelope.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"{target} uses unsupported report schema {envelope.get('schema_version')!r}")
    records = envelope.get("messages")
    if not isinstance(records, list):
        raise ValueError(f"{target} has no portable message records")

    imported: list[Any] = []
    source_path = f"<imported:{target.name}>"
    for record in records:
        if not isinstance(record, dict):
            raise ValueError(f"{target} contains an invalid message record")
        usage = record.get("usage")
        if not isinstance(usage, dict):
            raise ValueError(f"{target} contains a message without usage counters")
        message_id = record.get("id")
        if not isinstance(message_id, str) or not message_id:
            raise ValueError(f"{target} contains a message without a stable id")
        imported_message = message_type(
            session_id=str(record.get("session_id", "imported")),
            model=str(record.get("model", "unknown")),
            provider=str(record.get("provider", "openai")),
            timestamp=_timestamp(record.get("timestamp")),
            usage=usage_type(
                input=max(0, int(usage.get("input", 0))),
                output=max(0, int(usage.get("output", 0))),
                cache_read=max(0, int(usage.get("cache_read", 0))),
                cache_write=max(0, int(usage.get("cache_write", 0))),
                reasoning=max(0, int(usage.get("reasoning", 0))),
            ),
            source_path=source_path,
            workspace=None,
            headless=bool(record.get("headless", False)),
            turn_start=bool(record.get("turn_start", False)),
            dedup_key=("portable", message_id),
            effort=str(record.get("effort", "unknown") or "unknown").strip().lower(),
        )
        if portable_message_id(imported_message) != message_id:
            raise ValueError(f"{target} contains a message with an invalid stable id")
        imported.append(imported_message)
    metadata = {
        "path": str(target),
        "report_id": envelope.get("report_id"),
        "device_name": envelope.get("device_name"),
        "record_count": len(imported),
    }
    return tuple(imported), metadata


def merge_report_messages(
    messages: Iterable[Any],
    report_paths: Iterable[Path],
    message_type: Any,
    usage_type: Any,
) -> tuple[tuple[Any, ...], tuple[dict[str, Any], ...]]:
    merged = list(messages)
    metadata: list[dict[str, Any]] = []
    for path in report_paths:
        try:
            imported, report_metadata = load_report_messages(path, message_type, usage_type)
        except (OSError, TypeError, ValueError) as error:
            raise ValueError(f"{path}: {error}") from error
        merged.extend(imported)
        metadata.append(report_metadata)
    unique = deduplicate_portable_messages(merged)
    ordered = tuple(sorted(unique, key=lambda message: (message.session_id, message.timestamp, message.model)))
    return ordered, tuple(metadata)


def select_report_messages(messages: Iterable[Any], since: str | None, until: str | None) -> tuple[Any, ...]:
    return tuple(
        message
        for message in messages
        if (not since or message.date >= since) and (not until or message.date <= until)
    )


def import_reports_or_exit(
    messages: Iterable[Any],
    report_paths: Iterable[Path],
    message_type: Any,
    usage_type: Any,
) -> tuple[tuple[Any, ...], tuple[dict[str, Any], ...]]:
    try:
        return merge_report_messages(messages, report_paths, message_type, usage_type)
    except ValueError as error:
        raise SystemExit(f"cannot import report: {error}") from error


def export_report_or_exit(
    path: Path,
    report: dict[str, Any],
    messages: Iterable[Any],
    since: str | None,
    until: str | None,
    device_name: str | None,
) -> None:
    try:
        export_report(path, report, select_report_messages(messages, since, until), device_name)
    except OSError as error:
        raise SystemExit(f"cannot export report: {error}") from error
