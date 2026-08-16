"""Dependency-free terminal dashboard for the local Codex usage cost report."""

from __future__ import annotations

import sys
from datetime import date, timedelta
from typing import Any


SHADES = "·░▒▓█"
TAB_NAMES = ("Overview", "Models", "Daily", "Stats")


def _comparison(report: dict[str, Any]) -> dict[str, Any]:
    return report.get("comparison", {})


def _compact(value: float | int) -> str:
    number = float(value)
    absolute = abs(number)
    if absolute >= 1_000_000_000:
        return f"{number / 1_000_000_000:.1f}B"
    if absolute >= 1_000_000:
        return f"{number / 1_000_000:.1f}M"
    if absolute >= 1_000:
        return f"{number / 1_000:.1f}K"
    return f"{int(number):,}" if number.is_integer() else f"{number:,.2f}"


def _money(value: float, precise: bool = False) -> str:
    if precise or abs(value) < 0.01:
        return f"${value:.6f}"
    return f"${value:,.2f}"


def _truncate(value: str, width: int) -> str:
    if width <= 0:
        return ""
    return value if len(value) <= width else value[: max(1, width - 1)] + "…"


def _bar(value: float, maximum: float, width: int) -> str:
    if width <= 0:
        return ""
    if value <= 0 or maximum <= 0:
        return "·" * width
    filled = max(1, min(width, round(value / maximum * width)))
    return "█" * filled + "·" * (width - filled)


def _model_rows(report: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    models = _comparison(report).get("by_model", {})
    return sorted(models.items(), key=lambda item: (-item[1].get("estimated_cost_usd", 0.0), item[0]))


def _daily_rows(report: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    days = _comparison(report).get("by_day", {})
    return sorted(days.items())


def _summary_lines(report: dict[str, Any]) -> list[str]:
    comparison = _comparison(report)
    tokens = report.get("tokens", {}).get("total", 0)
    cost = float(report.get("estimated_cost_usd", 0.0))
    lines = [
        f"Sessions {report.get('sessions', 0):,}   Tasks {comparison.get('tasks', 0):,}   "
        f"Messages {report.get('messages', 0):,}",
        f"Tokens {_compact(tokens)}   Cost {_money(cost)}   "
        f"Average task {_money(float(comparison.get('average_cost_per_task_usd', 0.0)), precise=True)}",
        f"Average tokens/task {_compact(comparison.get('average_tokens_per_task', 0))}",
    ]
    unpriced = report.get("unpriced_models", [])
    if unpriced:
        lines.append("Unpriced models: " + ", ".join(str(model) for model in unpriced))
    return lines


def _cost_graph(report: dict[str, Any], width: int, limit: int = 6) -> list[str]:
    rows = _model_rows(report)[:limit]
    if not rows:
        return ["No priced model usage found."]
    maximum = max(float(row.get("estimated_cost_usd", 0.0)) for _, row in rows)
    label_width = min(24, max(12, width // 4))
    graph_width = max(10, width - label_width - 17)
    lines = ["Cost by model"]
    for model, row in rows:
        label = _truncate(model, label_width).ljust(label_width)
        graph = _bar(float(row.get("estimated_cost_usd", 0.0)), maximum, graph_width)
        lines.append(f"{label} {graph} {_money(float(row.get('estimated_cost_usd', 0.0)))}")
    return lines


def _token_graph(report: dict[str, Any], width: int, limit: int = 6) -> list[str]:
    rows = sorted(
        _model_rows(report),
        key=lambda item: (-item[1].get("tokens", 0), item[0]),
    )[:limit]
    if not rows:
        return ["No model usage found."]
    maximum = max(int(row.get("tokens", 0)) for _, row in rows)
    label_width = min(24, max(12, width // 4))
    graph_width = max(10, width - label_width - 17)
    lines = ["Tokens by model"]
    for model, row in rows:
        label = _truncate(model, label_width).ljust(label_width)
        graph = _bar(float(row.get("tokens", 0)), maximum, graph_width)
        lines.append(f"{label} {graph} {_compact(row.get('tokens', 0))}")
    return lines


def render_overview(report: dict[str, Any], width: int = 100) -> list[str]:
    lines = ["Overview", "", *_summary_lines(report), ""]
    lines.extend(_cost_graph(report, width))
    lines.append("")
    lines.extend(_token_graph(report, width))
    return lines


def render_models(report: dict[str, Any], width: int = 100) -> list[str]:
    comparison = _comparison(report)
    rows = _model_rows(report)
    lines = [
        "Model comparison",
        f"Overall: {comparison.get('tasks', 0):,} tasks | "
        f"average {_compact(comparison.get('average_tokens_per_task', 0))} tokens/task | "
        f"average {_money(float(comparison.get('average_cost_per_task_usd', 0.0)), precise=True)}/task",
        "",
    ]
    if not rows:
        return lines + ["No model usage found."]
    if width < 100:
        header = f"{'Model':<28} {'Tasks':>7} {'Avg tokens/task':>17} {'Avg cost/task':>16}"
        lines.append(header)
        lines.append("-" * min(width, len(header)))
        for model, row in rows:
            lines.append(
                f"{_truncate(model, 28):<28} {row.get('tasks', 0):>7,} "
                f"{_compact(row.get('average_tokens_per_task', 0)):>17} "
                f"{_money(float(row.get('average_cost_per_task_usd', 0.0)), precise=True):>16}"
            )
        return lines

    header = (
        f"{'Model':<26} {'Tasks':>7} {'Avg tokens/task':>17} {'Avg cost/task':>16} "
        f"{'Total tokens':>14} {'Total cost':>13}"
    )
    lines.append(header)
    lines.append("-" * min(width, len(header)))
    for model, row in rows:
        lines.append(
            f"{_truncate(model, 26):<26} {row.get('tasks', 0):>7,} "
            f"{_compact(row.get('average_tokens_per_task', 0)):>17} "
            f"{_money(float(row.get('average_cost_per_task_usd', 0.0)), precise=True):>16} "
            f"{_compact(row.get('tokens', 0)):>14} "
            f"{_money(float(row.get('estimated_cost_usd', 0.0))):>13}"
        )
    return lines


def render_daily(report: dict[str, Any], width: int = 100, limit: int = 24) -> list[str]:
    rows = _daily_rows(report)
    if not rows:
        return ["Daily activity", "", "No daily usage found."]
    rows = rows[-limit:]
    token_values = [int(row.get("tokens", 0)) for _, row in rows]
    maximum = max(token_values, default=0)
    graph_width = max(8, width - 56)
    lines = ["Daily activity", "", f"{'Date':<12} {'Tasks':>7} {'Tokens':>10} {'Cost':>13}  Graph"]
    lines.append("-" * min(width, len(lines[-1]) + graph_width))
    for day, row in rows:
        lines.append(
            f"{day:<12} {row.get('tasks', 0):>7,} { _compact(row.get('tokens', 0)):>10} "
            f"{_money(float(row.get('estimated_cost_usd', 0.0))):>13}  "
            f"{_bar(float(row.get('tokens', 0)), maximum, graph_width)}"
        )
    return lines


def _parse_day(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def render_stats(report: dict[str, Any], width: int = 100) -> list[str]:
    daily = report.get("by_day", {})
    parsed = {day: _parse_day(day) for day in daily}
    parsed = {day: value for day, value in parsed.items() if value is not None}
    if not parsed:
        return ["Stats", "", "No token activity to graph."]

    columns = max(1, min(52, (max(width, 20) - 8) // 2))
    end = max(parsed.values())
    start = end - timedelta(days=columns * 7 - 1)
    values = {parsed_day: int(daily[day].get("tokens", {}).get("total", 0)) for day, parsed_day in parsed.items()}
    maximum = max(values.values(), default=0)
    lines = ["Stats — token activity heatmap", "", f"Range: {start.isoformat()} → {end.isoformat()}", ""]
    lines.append("      " + " ".join((start + timedelta(days=7 * index)).strftime("%m/%d") for index in range(columns)))
    weekdays = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    for weekday, name in enumerate(weekdays):
        cells = []
        for column in range(columns):
            current = start + timedelta(days=column * 7 + weekday)
            value = values.get(current, 0)
            level = min(4, int(value / maximum * 4)) if maximum and value else 0
            cells.append(SHADES[level])
        lines.append(f"{name}   " + " ".join(cells))
    lines.extend(["", "Less " + " ".join(SHADES) + " More", "Intensity is based on total tokens per day."])
    return lines


def render_tab(report: dict[str, Any], tab: str, width: int = 100) -> list[str]:
    if tab == "Models":
        return render_models(report, width)
    if tab == "Daily":
        return render_daily(report, width)
    if tab == "Stats":
        return render_stats(report, width)
    return render_overview(report, width)


def render_static(report: dict[str, Any], width: int = 100) -> str:
    """Render a non-interactive fallback suitable for pipes and CI logs."""

    lines = render_overview(report, width)
    lines.extend(["", "=" * min(width, 100), ""])
    lines.extend(render_models(report, width))
    lines.extend(["", "=" * min(width, 100), ""])
    lines.extend(render_stats(report, width))
    return "\n".join(lines)


def _draw(screen: Any, report: dict[str, Any], tab_index: int) -> None:
    import curses

    height, width = screen.getmaxyx()
    screen.erase()
    title = "Codex Usage Cost"
    screen.addnstr(0, 0, title, max(0, width - 1), curses.A_BOLD)
    tabs = "  ".join(
        f"[{name}]" if index == tab_index else f" {name} " for index, name in enumerate(TAB_NAMES)
    )
    screen.addnstr(1, 0, tabs, max(0, width - 1), curses.A_REVERSE)
    lines = render_tab(report, TAB_NAMES[tab_index], max(40, width - 2))
    for row, line in enumerate(lines, start=3):
        if row >= height - 2:
            break
        screen.addnstr(row, 1, line, max(0, width - 2))
    footer = "Tab/←→ switch view   1-4 jump   q/Esc quit"
    screen.addnstr(max(0, height - 1), 0, footer, max(0, width - 1), curses.A_DIM)
    screen.refresh()


def _curses_main(screen: Any, report: dict[str, Any]) -> None:
    import curses

    try:
        curses.curs_set(0)
    except curses.error:
        pass
    tab_index = 0
    while True:
        _draw(screen, report, tab_index)
        key = screen.getch()
        if key in (ord("q"), ord("Q"), 27):
            return
        if key in (9, curses.KEY_RIGHT, ord("l")):
            tab_index = (tab_index + 1) % len(TAB_NAMES)
        elif key in (curses.KEY_LEFT, ord("h")):
            tab_index = (tab_index - 1) % len(TAB_NAMES)
        elif ord("1") <= key <= ord("4"):
            tab_index = key - ord("1")


def run_tui(report: dict[str, Any]) -> int:
    """Run the interactive TUI, or static dashboard when no terminal exists."""

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print(render_static(report))
        return 0
    try:
        import curses
    except (ImportError, OSError):
        print(render_static(report))
        return 0
    try:
        curses.wrapper(_curses_main, report)
    except (OSError, curses.error):
        print(render_static(report))
    return 0
