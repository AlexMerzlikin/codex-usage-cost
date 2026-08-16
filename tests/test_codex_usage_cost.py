from __future__ import annotations

import json
import io
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from codex_usage_cost.cli import (  # noqa: E402
    TokenUsage,
    _parser,
    build_report,
    discover_codex_files,
    main,
    parse_codex_file,
    scan_codex_files,
)
from codex_usage_cost.html import default_html_path, render_html  # noqa: E402
from codex_usage_cost.pricing import PriceCatalog  # noqa: E402
from codex_usage_cost.report_io import export_report, load_report_messages, merge_report_messages  # noqa: E402
from codex_usage_cost.tui import render_static  # noqa: E402


PRICING_FILE = PROJECT_ROOT / "src" / "codex_usage_cost" / "prices.json"


FIXTURE = Path(__file__).parent / "fixtures" / "codex_session.jsonl"


def _write_session(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")


class CodexUsageTests(unittest.TestCase):
    def test_last_usage_is_counted_once_and_cache_is_split(self) -> None:
        parsed = parse_codex_file(FIXTURE)

        self.assertEqual(parsed.session_id, "session-fixture")
        self.assertEqual(len(parsed.messages), 2)
        self.assertTrue(parsed.messages[0].turn_start)
        self.assertEqual(parsed.messages[0].usage.input, 600)
        self.assertEqual(parsed.messages[0].usage.cache_read, 400)
        self.assertEqual(parsed.messages[1].usage.input, 100)
        self.assertEqual(parsed.messages[1].usage.cache_read, 100)
        self.assertEqual(sum(message.usage.total for message in parsed.messages), 1_355)

    def test_duplicate_session_copies_are_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "sessions" / "one.jsonl"
            second = root / "archived_sessions" / "one.jsonl"
            first.parent.mkdir(parents=True)
            second.parent.mkdir(parents=True)
            shutil.copyfile(FIXTURE, first)
            shutil.copyfile(FIXTURE, second)

            messages, errors = scan_codex_files((first, second))

        self.assertEqual(errors, ())
        self.assertEqual(len(messages), 2)

    def test_default_discovery_honors_codex_home(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory) / "sessions" / "today.jsonl"
            _write_session(session, [])

            with patch.dict(os.environ, {"CODEX_HOME": directory}, clear=False):
                paths = discover_codex_files()

        self.assertEqual(paths, [session.resolve()])

    def test_total_only_events_use_a_delta_after_the_first_snapshot(self) -> None:
        events = [
            {"type": "session_meta", "payload": {"id": "total-only"}},
            {"type": "turn_context", "payload": {"model": "gpt-5-codex"}},
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {"input_tokens": 100, "output_tokens": 10}},
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {"input_tokens": 150, "output_tokens": 15}},
                },
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "total-only.jsonl"
            _write_session(path, events)
            parsed = parse_codex_file(path)

        self.assertEqual([message.usage.total for message in parsed.messages], [110, 55])

    def test_effort_is_read_from_turn_context_and_collaboration_settings(self) -> None:
        events = [
            {"type": "session_meta", "payload": {"id": "effort-session"}},
            {"type": "turn_context", "payload": {"model": "gpt-5.3-codex", "effort": "high"}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "first task"}},
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {"input_tokens": 100, "output_tokens": 10}},
                },
            },
            {
                "type": "turn_context",
                "payload": {
                    "model": "gpt-5.3-codex",
                    "collaboration_mode": {"settings": {"reasoning_effort": "low"}},
                },
            },
            {"type": "event_msg", "payload": {"type": "user_message", "message": "second task"}},
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {"input_tokens": 200, "output_tokens": 20}},
                },
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "effort.jsonl"
            _write_session(path, events)
            parsed = parse_codex_file(path)

        self.assertEqual([message.effort for message in parsed.messages], ["high", "low"])

    def test_effort_comparison_and_html_foldout(self) -> None:
        parsed = parse_codex_file(FIXTURE)
        messages = (
            replace(parsed.messages[0], effort="high"),
            replace(parsed.messages[1], effort="low"),
        )
        catalog = PriceCatalog.from_file(PRICING_FILE)
        report = build_report(messages, catalog)

        comparison = report["comparison"]
        self.assertEqual(set(comparison["by_effort"]), {"high", "low"})
        self.assertEqual(comparison["effort_coverage"], {"known_messages": 2, "unknown_messages": 0, "total_messages": 2})
        self.assertEqual(set(comparison["by_model"]["gpt-5.1-codex"]["by_effort"]), {"high", "low"})
        self.assertEqual(comparison["by_effort"]["high"]["tasks"], 1)
        self.assertEqual(comparison["by_effort"]["low"]["tasks"], 1)
        self.assertAlmostEqual(
            sum(row["estimated_cost_usd"] for row in comparison["by_effort"].values()),
            report["estimated_cost_usd"],
        )

        html = render_html(report)
        self.assertIn('<details class="model-foldout">', html)
        self.assertIn("Effort breakdown", html)
        self.assertIn("high", html)
        self.assertIn("low", html)
        self.assertIn("Cache hit rate", html)

    def test_effort_survives_portable_export_and_import(self) -> None:
        parsed = parse_codex_file(FIXTURE)
        message = replace(parsed.messages[0], effort="xhigh")
        catalog = PriceCatalog.from_file(PRICING_FILE)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "effort.json"
            envelope = export_report(path, build_report((message,), catalog), (message,), "laptop")
            imported, _ = load_report_messages(path, message.__class__, TokenUsage)

        self.assertEqual(envelope["messages"][0]["effort"], "xhigh")
        self.assertEqual(imported[0].effort, "xhigh")

    def test_cost_uses_reasoning_as_output_and_keeps_unknown_models_unpriced(self) -> None:
        catalog = PriceCatalog.from_mapping(
            {
                "models": {
                    "gpt-5.1-codex": {
                        "input_cost_per_million_tokens": 1.0,
                        "output_cost_per_million_tokens": 10.0,
                        "cache_read_input_token_cost_per_million_tokens": 0.1,
                    }
                }
            }
        )
        parsed = parse_codex_file(FIXTURE)
        report = build_report(parsed.messages, catalog)

        expected = (700 * 1.0 + 500 * 0.1 + (130 + 25) * 10.0) / 1_000_000
        self.assertAlmostEqual(report["estimated_cost_usd"], expected)
        self.assertEqual(report["unpriced_models"], [])
        comparison = report["comparison"]
        self.assertEqual(comparison["tasks"], 1)
        self.assertEqual(comparison["average_tokens_per_task"], 1_355)
        self.assertAlmostEqual(comparison["average_cost_per_task_usd"], expected)
        model_comparison = comparison["by_model"]["gpt-5.1-codex"]
        self.assertEqual(model_comparison["tasks"], 1)
        self.assertEqual(model_comparison["average_tokens_per_task"], 1_355)
        self.assertAlmostEqual(model_comparison["average_cost_per_task_usd"], expected)
        dashboard = render_static(report)
        self.assertIn("Model comparison", dashboard)
        self.assertIn("Average task", dashboard)
        html = render_html(report)
        self.assertIn("Codex Usage Cost", html)
        self.assertIn("Token activity", html)
        self.assertIn("gpt-5.1-codex", html)
        self.assertIn("aria-label=", html)
        self.assertIn("level-5", html)

        unknown = parsed.messages[0].__class__(
            session_id="unknown",
            model="new-model",
            provider="openai",
            timestamp=parsed.messages[0].timestamp,
            usage=parsed.messages[0].usage,
            source_path=parsed.messages[0].source_path,
            workspace=None,
            headless=False,
            turn_start=False,
            dedup_key=("unknown",),
        )
        unknown_report = build_report((unknown,), catalog)
        self.assertEqual(unknown_report["estimated_cost_usd"], 0.0)
        self.assertEqual(unknown_report["unpriced_models"], ["new-model"])

    def test_comparison_counts_new_user_turns_as_tasks(self) -> None:
        events = [
            {"type": "session_meta", "payload": {"id": "two-tasks"}},
            {"type": "turn_context", "payload": {"model": "gpt-5.1-codex"}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "first task"}},
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {"input_tokens": 100, "output_tokens": 10},
                        "total_token_usage": {"input_tokens": 100, "output_tokens": 10},
                    },
                },
            },
            {"type": "turn_context", "payload": {"model": "gpt-5.3-codex"}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "second task"}},
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {"input_tokens": 200, "output_tokens": 20},
                        "total_token_usage": {"input_tokens": 300, "output_tokens": 30},
                    },
                },
            },
        ]
        catalog = PriceCatalog.from_mapping(
            {
                "models": {
                    "gpt-5.1-codex": {
                        "input_cost_per_million_tokens": 1.0,
                        "output_cost_per_million_tokens": 10.0,
                    },
                    "gpt-5.3-codex": {
                        "input_cost_per_million_tokens": 2.0,
                        "output_cost_per_million_tokens": 20.0,
                    },
                }
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "two-tasks.jsonl"
            _write_session(path, events)
            report = build_report(parse_codex_file(path).messages, catalog)

        self.assertEqual(report["comparison"]["tasks"], 2)
        self.assertEqual(report["comparison"]["average_tokens_per_task"], 165.0)
        self.assertEqual(report["comparison"]["by_model"]["gpt-5.1-codex"]["tasks"], 1)
        self.assertEqual(report["comparison"]["by_model"]["gpt-5.3-codex"]["tasks"], 1)

    def test_official_catalog_has_current_openai_rates_and_rejects_unrelated_variants(self) -> None:
        catalog = PriceCatalog.from_file(PRICING_FILE)
        price = catalog.resolve("gpt-5.3-codex")
        self.assertIsNotNone(price)
        self.assertAlmostEqual(price.estimate(TokenUsage(input=1_000_000)), 1.75)
        self.assertAlmostEqual(price.estimate(TokenUsage(output=1_000_000)), 14.0)
        self.assertAlmostEqual(price.estimate(TokenUsage(cache_read=1_000_000)), 0.175)
        self.assertIsNotNone(catalog.resolve("gpt-5.3-codex-2026-03-05"))
        self.assertIsNone(catalog.resolve("gpt-5.5-cyber"))

    def test_report_round_trip_is_portable_and_reimport_is_deduplicated(self) -> None:
        parsed = parse_codex_file(FIXTURE)
        catalog = PriceCatalog.from_file(PRICING_FILE)
        report = build_report(parsed.messages, catalog)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "laptop.json"
            envelope = export_report(path, report, parsed.messages, "laptop")
            imported, metadata = load_report_messages(path, parsed.messages[0].__class__, TokenUsage)
            merged, imported_reports = merge_report_messages(
                parsed.messages,
                (path,),
                parsed.messages[0].__class__,
                TokenUsage,
            )

        self.assertEqual(envelope["format"], "codex-usage-cost-report")
        self.assertEqual(envelope["record_count"], 2)
        self.assertEqual(metadata["device_name"], "laptop")
        self.assertEqual(len(imported), 2)
        self.assertNotIn("source_path", envelope["messages"][0])
        self.assertNotIn("message", envelope["messages"][0])
        self.assertEqual(len(merged), 2)
        self.assertEqual(len(imported_reports), 1)

    def test_distinct_device_exports_are_added_to_the_merged_report(self) -> None:
        parsed = parse_codex_file(FIXTURE)
        catalog = PriceCatalog.from_file(PRICING_FILE)
        desktop_message = replace(
            parsed.messages[0],
            session_id="desktop-session",
            source_path="/desktop/.codex/sessions/desktop.jsonl",
            dedup_key=("desktop",),
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            laptop_path = root / "laptop.json"
            desktop_path = root / "desktop.json"
            export_report(laptop_path, build_report(parsed.messages, catalog), parsed.messages, "laptop")
            export_report(
                desktop_path,
                build_report((desktop_message,), catalog),
                (desktop_message,),
                "desktop",
            )
            merged, metadata = merge_report_messages(
                (),
                (laptop_path, desktop_path),
                parsed.messages[0].__class__,
                TokenUsage,
            )

        self.assertEqual(len(merged), 3)
        self.assertEqual(len(metadata), 2)
        self.assertEqual(sum(message.usage.total for message in merged), 2_475)

    def test_import_accepts_exports_from_the_previous_tool_name(self) -> None:
        parsed = parse_codex_file(FIXTURE)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.json"
            envelope = export_report(path, {}, parsed.messages)
            envelope["format"] = "codex-usage-report"
            path.write_text(json.dumps(envelope), encoding="utf-8")
            imported, _ = load_report_messages(path, parsed.messages[0].__class__, TokenUsage)

        self.assertEqual(len(imported), 2)

    def test_html_is_the_default_output_and_can_write_a_report(self) -> None:
        self.assertEqual(_parser().parse_args([]).html, default_html_path())

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.html"
            with redirect_stdout(io.StringIO()):
                result = main(["--home", directory, "--html", str(output)])

            self.assertEqual(result, 0)
            rendered = output.read_text(encoding="utf-8")
            self.assertIn("<!doctype html>", rendered)
            self.assertIn("No token activity found", rendered)


if __name__ == "__main__":
    unittest.main()
