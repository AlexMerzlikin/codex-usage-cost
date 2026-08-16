"""Dependency-free HTML dashboard for local Codex usage cost reports."""

from __future__ import annotations

import math
from datetime import date, timedelta
from html import escape
from pathlib import Path
from typing import Any


def default_html_path(today: date | None = None) -> Path:
    """Return the default dated HTML output path relative to the cwd."""

    report_date = today or date.today()
    return Path("output") / f"codex-usage-cost-report-{report_date.isoformat()}.html"


def _integer(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _compact(value: Any) -> str:
    number = _number(value)
    absolute = abs(number)
    if absolute >= 1_000_000_000:
        return f"{number / 1_000_000_000:.1f}B"
    if absolute >= 1_000_000:
        return f"{number / 1_000_000:.1f}M"
    if absolute >= 1_000:
        return f"{number / 1_000:.1f}K"
    return f"{int(number):,}" if number.is_integer() else f"{number:,.2f}"


def _money(value: Any) -> str:
    return f"${_number(value):,.2f}"


def _text(value: Any) -> str:
    return escape(str(value), quote=True)


def _parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _daily_data(report: dict[str, Any]) -> dict[date, dict[str, Any]]:
    comparison_days = report.get("comparison", {}).get("by_day", {})
    result: dict[date, dict[str, Any]] = {}
    for raw_day, bucket in report.get("by_day", {}).items():
        day = _parse_date(raw_day)
        if day is None or not isinstance(bucket, dict):
            continue
        tokens = bucket.get("tokens", {})
        comparison = comparison_days.get(str(raw_day), {})
        result[day] = {
            "tokens": _integer(tokens.get("total", 0)),
            "cost": _number(bucket.get("estimated_cost_usd", 0.0)),
            "messages": _integer(bucket.get("messages", 0)),
            "tasks": _integer(comparison.get("tasks", 0)),
        }
    return result


def _activity_level(value: int, maximum: int) -> int:
    if value <= 0 or maximum <= 0:
        return 0
    if value == maximum:
        return 5
    scaled = math.log1p(value) / math.log1p(maximum) * 5
    return max(1, min(5, math.ceil(scaled)))


def _heatmap(report: dict[str, Any]) -> str:
    days = _daily_data(report)
    if not days:
        return (
            '<section class="panel activity-panel"><div class="section-heading">'
            '<div><p class="eyebrow">Activity</p><h2>Token activity</h2></div></div>'
            '<p class="empty">No token activity found for this report.</p></section>'
        )

    first = min(days)
    last = max(days)
    start = first - timedelta(days=first.weekday())
    end = last + timedelta(days=6 - last.weekday())
    weeks = (end - start).days // 7 + 1
    maximum = max((bucket["tokens"] for bucket in days.values()), default=0)

    month_labels: list[str] = []
    week_markup: list[str] = []
    for week_index in range(weeks):
        week_start = start + timedelta(days=week_index * 7)
        label = ""
        cells: list[str] = []
        for offset in range(7):
            current = week_start + timedelta(days=offset)
            if current.day == 1 or (week_index == 0 and offset == 0):
                label = current.strftime("%b")
            bucket = days.get(current, {"tokens": 0, "cost": 0.0, "tasks": 0, "messages": 0})
            tokens = bucket["tokens"]
            level = _activity_level(tokens, maximum)
            title = (
                f"{current.isoformat()} | {_compact(tokens)} tokens | "
                f"{_money(bucket['cost'])} | {bucket['tasks']:,} tasks"
            )
            cells.append(
                f'<span class="heat-cell level-{level}" title="{_text(title)}" '
                f'aria-label="{_text(title)}"></span>'
            )
        month_labels.append(f'<span class="month-label">{_text(label)}</span>')
        week_markup.append(f'<div class="heat-week">{"".join(cells)}</div>')

    weekday_labels = "".join(f'<span>{name}</span>' for name in ("Mon", "", "Wed", "", "Fri", "", "Sun"))
    legend = "".join(f'<span class="heat-cell level-{level}"></span>' for level in range(6))
    range_label = f"{first.strftime('%b %d, %Y')} - {last.strftime('%b %d, %Y')}"
    return f"""
    <section class="panel activity-panel">
      <div class="section-heading">
        <div><p class="eyebrow">Activity</p><h2>Token activity</h2></div>
        <span class="section-note">{_text(range_label)}</span>
      </div>
      <div class="heatmap-layout">
        <div class="heatmap-weekdays">{weekday_labels}</div>
        <div class="heatmap-scroll">
          <div class="heatmap-months">{"".join(month_labels)}</div>
          <div class="heatmap-weeks">{"".join(week_markup)}</div>
          <div class="heatmap-legend"><span>Less</span>{legend}<span>More</span>
            <span class="legend-note">Levels use logarithmic scaling so quiet days stay visible.</span>
          </div>
        </div>
      </div>
    </section>
    """


def _kpis(report: dict[str, Any]) -> str:
    comparison = report.get("comparison", {})
    tokens = report.get("tokens", {})
    input_tokens = _integer(tokens.get("input", 0))
    cache_read_tokens = _integer(tokens.get("cache_read", 0))
    context_tokens = input_tokens + cache_read_tokens
    cache_rate = cache_read_tokens / context_tokens if context_tokens else 0.0
    effort_coverage = comparison.get("effort_coverage", {})
    effort_known = _integer(effort_coverage.get("known_messages", 0))
    effort_total = _integer(effort_coverage.get("total_messages", report.get("messages", 0)))
    effort_rate = effort_known / effort_total if effort_total else 0.0
    cards = (
        ("Estimated cost", _money(report.get("estimated_cost_usd", 0.0)), "API list-price estimate"),
        ("Tokens", _compact(tokens.get("total", 0)), f"{_compact(tokens.get('output', 0))} output"),
        ("Tasks", _compact(comparison.get("tasks", 0)), "logical Codex tasks"),
        ("Sessions", _compact(report.get("sessions", 0)), f"{_compact(report.get('messages', 0))} messages"),
        ("Cache hit rate", f"{cache_rate * 100:.1f}%", f"{_compact(cache_read_tokens)} cached input tokens"),
        ("Effort coverage", f"{effort_rate * 100:.1f}%", f"{_compact(effort_known)} / {_compact(effort_total)} messages"),
    )
    return "".join(
        f'<article class="kpi"><p>{_text(label)}</p><strong>{_text(value)}</strong>'
        f'<span>{_text(detail)}</span></article>'
        for label, value, detail in cards
    )


def _token_breakdown(report: dict[str, Any]) -> str:
    tokens = report.get("tokens", {})
    total = _integer(tokens.get("total", 0))
    labels = (
        ("Input", "input", "blue"),
        ("Output", "output", "violet"),
        ("Cache read", "cache_read", "cyan"),
        ("Reasoning", "reasoning", "amber"),
        ("Cache write", "cache_write", "green"),
    )
    rows: list[str] = []
    for label, key, color in labels:
        value = _integer(tokens.get(key, 0))
        share = min(100.0, value / total * 100) if total else 0.0
        rows.append(
            f'<div class="breakdown-row"><div><span>{_text(label)}</span>'
            f'<strong>{_compact(value)}</strong></div><div class="bar-track">'
            f'<i class="bar-{color}" style="width:{share:.2f}%"></i></div></div>'
        )
    return "".join(rows)


_EFFORT_ORDER = {name: index for index, name in enumerate(("none", "low", "medium", "high", "xhigh", "max", "unknown"))}


def _cache_rate(row: dict[str, Any]) -> float:
    stored = row.get("cache_hit_rate")
    if stored is not None:
        return max(0.0, min(1.0, _number(stored)))
    input_tokens = _integer(row.get("input_tokens", 0))
    cache_read_tokens = _integer(row.get("cache_read_tokens", 0))
    context_tokens = input_tokens + cache_read_tokens
    return cache_read_tokens / context_tokens if context_tokens else 0.0


def _effort_label(value: Any) -> str:
    normalized = str(value or "unknown").strip().lower() or "unknown"
    return "Unknown" if normalized == "unknown" else normalized


def _effort_table(row: dict[str, Any]) -> str:
    efforts = row.get("by_effort", {})
    if not isinstance(efforts, dict) or not efforts:
        return ""
    ordered = sorted(
        efforts.items(),
        key=lambda item: (_EFFORT_ORDER.get(str(item[0]).lower(), len(_EFFORT_ORDER)), str(item[0])),
    )
    body: list[str] = []
    for effort, stats in ordered:
        if not isinstance(stats, dict):
            continue
        body.append(
            "<tr>"
            f'<td class="effort-name">{_text(_effort_label(effort))}</td>'
            f'<td>{_compact(stats.get("tasks", 0))}</td>'
            f'<td>{_compact(stats.get("sessions", 0))}</td>'
            f'<td>{_compact(stats.get("messages", 0))}</td>'
            f'<td>{_compact(stats.get("tokens", 0))}</td>'
            f'<td>{_money(stats.get("estimated_cost_usd", 0.0))}</td>'
            f'<td>{_money(stats.get("average_cost_per_task_usd", 0.0))}</td>'
            f'<td>{_compact(stats.get("cache_read_tokens", 0))}</td>'
            f'<td>{_money(stats.get("cache_read_cost_usd", 0.0))}</td>'
            f'<td>{_cache_rate(stats) * 100:.1f}%</td>'
            "</tr>"
        )
    if not body:
        return ""
    return (
        '<div class="effort-breakdown"><div class="effort-heading">Effort breakdown</div>'
        '<div class="table-scroll"><table class="effort-table"><thead><tr>'
        '<th>Effort</th><th>Tasks</th><th>Sessions</th><th>Messages</th><th>Tokens</th>'
        '<th>Cost</th><th>Avg/task</th><th>Cache read</th><th>Cache cost</th><th>Hit rate</th>'
        f'</tr></thead><tbody>{"".join(body)}</tbody></table></div></div>'
    )


def _model_table(report: dict[str, Any]) -> str:
    models = report.get("comparison", {}).get("by_model", {})
    rows = sorted(
        models.items(),
        key=lambda item: (-_number(item[1].get("estimated_cost_usd", 0.0)), item[0]),
    )
    if not rows:
        return '<p class="empty">No model usage found.</p>'
    total_cost = _number(report.get("estimated_cost_usd", 0.0))
    body: list[str] = []
    for model, row in rows:
        cost = _number(row.get("estimated_cost_usd", 0.0))
        share = min(100.0, cost / total_cost * 100) if total_cost else 0.0
        model_cell = _text(model)
        effort_table = _effort_table(row)
        if effort_table:
            model_cell = f'<details class="model-foldout"><summary>{model_cell}</summary>{effort_table}</details>'
        body.append(
            "<tr>"
            f'<td class="model-name">{model_cell}</td>'
            f'<td>{_compact(row.get("tasks", 0))}</td>'
            f'<td>{_compact(row.get("tokens", 0))}</td>'
            f'<td>{_money(cost)}</td>'
            f'<td>{_money(row.get("average_cost_per_task_usd", 0.0))}</td>'
            f'<td class="share"><span class="bar-track"><i style="width:{share:.2f}%"></i></span>'
            f'<small>{share:.1f}%</small></td>'
            "</tr>"
        )
    return (
        '<div class="table-scroll"><table><thead><tr><th>Model</th><th>Tasks</th>'
        '<th>Tokens</th><th>Cost</th><th>Avg/task</th><th>Share</th></tr></thead>'
        f'<tbody>{"".join(body)}</tbody></table></div>'
    )


def _daily_table(report: dict[str, Any]) -> str:
    days = _daily_data(report)
    if not days:
        return '<p class="empty">No daily usage found.</p>'
    rows = sorted(days.items(), reverse=True)[:14]
    maximum = max((bucket["tokens"] for _, bucket in rows), default=0)
    body: list[str] = []
    for day, bucket in rows:
        share = _activity_level(bucket["tokens"], maximum) * 20
        body.append(
            "<tr>"
            f'<td class="date-cell">{day.strftime("%b %d, %Y")}</td>'
            f'<td>{_compact(bucket["tasks"])}</td>'
            f'<td>{_compact(bucket["tokens"])}</td>'
            f'<td>{_money(bucket["cost"])}</td>'
            f'<td><span class="bar-track"><i style="width:{share}%"></i></span></td>'
            "</tr>"
        )
    return (
        '<div class="table-scroll"><table><thead><tr><th>Date</th><th>Tasks</th>'
        '<th>Tokens</th><th>Cost</th><th>Activity</th></tr></thead>'
        f'<tbody>{"".join(body)}</tbody></table></div>'
    )


def _status(report: dict[str, Any]) -> str:
    notices: list[str] = []
    unpriced = report.get("unpriced_models", [])
    if unpriced:
        names = ", ".join(_text(model) for model in unpriced)
        notices.append(f'<div class="notice warning"><strong>Unpriced models</strong><span>{names}</span></div>')
    errors = report.get("scan_errors", [])
    if errors:
        notices.append(
            f'<div class="notice error"><strong>Scan warnings</strong><span>{_compact(len(errors))} files could not be read.</span></div>'
        )
    return "".join(notices)


_STYLES = r"""
:root {
  color-scheme: dark;
  --bg: #080b12;
  --panel: rgba(17, 24, 39, .82);
  --panel-strong: #121a2a;
  --line: #26344c;
  --text: #eef4ff;
  --muted: #92a0b9;
  --cyan: #62e6ef;
  --violet: #a78bfa;
  --blue: #60a5fa;
  --green: #6ee7b7;
  --amber: #fbbf24;
  --red: #fb7185;
  --heat-0: #172235;
  --heat-1: #153f58;
  --heat-2: #126578;
  --heat-3: #0b91a0;
  --heat-4: #35c6b5;
  --heat-5: #b6f2c9;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  min-width: 320px;
  color: var(--text);
  background: radial-gradient(circle at 12% -10%, #253b68 0, transparent 34rem),
              radial-gradient(circle at 90% 0, #42265e 0, transparent 32rem), var(--bg);
  font: 14px/1.5 Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
.shell { width: min(1480px, 100%); margin: 0 auto; padding: 38px 26px 64px; }
.hero { display: flex; align-items: end; justify-content: space-between; gap: 24px; margin-bottom: 28px; }
.eyebrow { margin: 0 0 7px; color: var(--cyan); font-size: 11px; font-weight: 800; letter-spacing: .16em; text-transform: uppercase; }
h1, h2, h3, p { margin-top: 0; }
h1 { margin-bottom: 10px; font-size: clamp(30px, 5vw, 58px); letter-spacing: -.055em; line-height: 1; }
h2 { margin-bottom: 0; font-size: 19px; letter-spacing: -.02em; }
.subtitle { max-width: 760px; margin-bottom: 0; color: var(--muted); }
.stamp { flex: 0 0 auto; padding: 11px 14px; border: 1px solid var(--line); border-radius: 14px; color: var(--muted); background: rgba(12, 18, 30, .72); text-align: right; }
.stamp strong { display: block; color: var(--text); font-size: 18px; }
.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 13px; margin-bottom: 18px; }
.kpi, .panel { border: 1px solid var(--line); background: linear-gradient(145deg, rgba(24, 34, 55, .92), var(--panel)); box-shadow: 0 18px 55px rgba(0, 0, 0, .18); }
.kpi { min-height: 132px; padding: 19px; border-radius: 18px; }
.kpi p, .kpi span { color: var(--muted); }
.kpi p { margin-bottom: 14px; font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: .08em; }
.kpi strong { display: block; margin-bottom: 5px; font-size: 30px; letter-spacing: -.04em; }
.kpi span { font-size: 12px; }
.panel { padding: 22px; border-radius: 21px; }
.activity-panel { margin-bottom: 18px; }
.section-heading { display: flex; align-items: end; justify-content: space-between; gap: 15px; margin-bottom: 20px; }
.section-note { color: var(--muted); font-size: 12px; }
.heatmap-layout { display: flex; gap: 12px; }
.heatmap-weekdays { display: grid; grid-template-rows: repeat(7, 14px); gap: 4px; width: 29px; padding-top: 22px; color: var(--muted); font-size: 10px; line-height: 14px; }
.heatmap-scroll { min-width: 0; overflow-x: auto; padding-bottom: 7px; }
.heatmap-months, .heatmap-weeks { display: flex; gap: 4px; min-width: max-content; }
.heatmap-months { height: 18px; }
.month-label { flex: 0 0 14px; width: 14px; overflow: visible; color: var(--muted); font-size: 10px; }
.heat-week { display: grid; grid-template-rows: repeat(7, 14px); gap: 4px; flex: 0 0 14px; width: 14px; }
.heat-cell { display: block; width: 14px; height: 14px; border-radius: 4px; background: var(--heat-0); }
.heat-cell.level-0 { background: #101827; }
.heat-cell.level-1 { background: var(--heat-1); }
.heat-cell.level-2 { background: var(--heat-2); }
.heat-cell.level-3 { background: var(--heat-3); }
.heat-cell.level-4 { background: var(--heat-4); }
.heat-cell.level-5 { background: var(--heat-5); }
.heatmap-legend { display: flex; align-items: center; gap: 5px; min-width: max-content; margin-top: 16px; color: var(--muted); font-size: 10px; }
.legend-note { margin-left: 9px; }
.grid { display: grid; grid-template-columns: minmax(0, 1.35fr) minmax(320px, .65fr); gap: 18px; }
.daily-panel { grid-column: 1 / -1; }
.stack { display: grid; gap: 18px; }
.table-scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; white-space: nowrap; }
th { padding: 0 12px 11px; color: var(--muted); font-size: 10px; font-weight: 800; letter-spacing: .08em; text-align: left; text-transform: uppercase; }
td { padding: 13px 12px; border-top: 1px solid rgba(56, 70, 96, .5); color: #dbe5f5; }
th:not(:first-child), td:not(:first-child) { text-align: right; }
.model-name, .date-cell { color: var(--text); font-weight: 700; text-align: left !important; }
.model-foldout { min-width: 260px; }
.model-foldout summary { cursor: pointer; color: var(--cyan); list-style-position: inside; }
.model-foldout summary::marker { color: var(--violet); }
.model-foldout[open] summary { margin-bottom: 10px; }
.effort-breakdown { min-width: 840px; padding: 12px; border: 1px solid var(--line); border-radius: 12px; background: rgba(8, 12, 22, .72); font-weight: 400; }
.effort-heading { margin-bottom: 9px; color: var(--muted); font-size: 11px; font-weight: 800; letter-spacing: .08em; text-transform: uppercase; }
.effort-table { font-size: 12px; }
.effort-table th { padding-bottom: 8px; }
.effort-table td { padding: 8px 9px; }
.effort-table .effort-name { color: var(--text); font-weight: 700; text-align: left !important; }
.share { min-width: 150px; }
.share small { display: inline-block; width: 42px; color: var(--muted); text-align: right; }
.bar-track { display: inline-block; width: 92px; height: 7px; overflow: hidden; border-radius: 99px; background: #1d293d; vertical-align: middle; }
.bar-track i { display: block; height: 100%; min-width: 2px; border-radius: inherit; background: linear-gradient(90deg, var(--violet), var(--cyan)); }
.breakdown-row { margin-bottom: 16px; }
.breakdown-row > div:first-child { display: flex; justify-content: space-between; margin-bottom: 6px; color: var(--muted); font-size: 12px; }
.breakdown-row strong { color: var(--text); }
.breakdown-row .bar-track { width: 100%; height: 8px; }
.bar-blue { background: var(--blue) !important; }
.bar-violet { background: var(--violet) !important; }
.bar-cyan { background: var(--cyan) !important; }
.bar-amber { background: var(--amber) !important; }
.bar-green { background: var(--green) !important; }
.notice { display: flex; gap: 10px; margin: 0 0 18px; padding: 12px 15px; border: 1px solid var(--line); border-radius: 13px; background: rgba(18, 26, 42, .75); }
.notice strong { color: var(--amber); }
.notice span { color: var(--muted); }
.notice.error strong { color: var(--red); }
.empty { margin: 0; color: var(--muted); }
.footer { margin-top: 20px; color: var(--muted); font-size: 11px; }
@media (max-width: 900px) { .hero { display: block; } .stamp { display: inline-block; margin-top: 18px; text-align: left; } .grid { grid-template-columns: 1fr; } }
@media (max-width: 560px) { .shell { padding: 24px 14px 45px; } .panel { padding: 16px; } .section-heading { align-items: start; display: block; } .section-note { display: block; margin-top: 8px; } }
"""


def render_html(report: dict[str, Any]) -> str:
    """Render a complete self-contained HTML dashboard."""

    comparison = report.get("comparison", {})
    days = _daily_data(report)
    first = min(days).strftime("%b %d, %Y") if days else "No activity"
    last = max(days).strftime("%b %d, %Y") if days else ""
    range_text = f"{first} - {last}" if last else first
    source = report.get("source", "codex-cli")
    files = _integer(report.get("files_found", 0))
    imported = _integer(report.get("reports_imported", 0))
    details = f"{files:,} local files"
    if imported:
        details += f" + {imported:,} imported report{'s' if imported != 1 else ''}"
    status = _status(report)
    effort_coverage = comparison.get("effort_coverage", {})
    effort_known = _integer(effort_coverage.get("known_messages", 0))
    effort_total = _integer(effort_coverage.get("total_messages", report.get("messages", 0)))
    effort_details = (
        f"Effort levels were recorded for {effort_known:,} of {effort_total:,} messages; "
        "older/imported records without the field appear as Unknown."
    )
    generated = date.today().strftime("%b %d, %Y")
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark">
  <title>Codex Usage Cost</title>
  <style>{_STYLES}</style>
</head>
<body>
  <main class="shell">
    <header class="hero">
      <div><p class="eyebrow">Local usage intelligence</p><h1>Codex Usage Cost</h1>
        <p class="subtitle">A clear view of token volume, estimated API cost, model mix, and daily rhythm.</p>
      </div>
      <div class="stamp"><strong>{_text(range_text)}</strong><span>Generated {generated}</span></div>
    </header>
    <section class="kpis">{_kpis(report)}</section>
    {status}
    {_heatmap(report)}
    <div class="grid">
      <section class="panel"><div class="section-heading"><div><p class="eyebrow">Models</p><h2>Where the spend goes</h2></div></div>{_model_table(report)}</section>
      <div class="stack">
        <section class="panel"><div class="section-heading"><div><p class="eyebrow">Tokens</p><h2>Usage mix</h2></div><span class="section-note">{_compact(report.get("tokens", {}).get("total", 0))} total</span></div>{_token_breakdown(report)}</section>
        <section class="panel"><div class="section-heading"><div><p class="eyebrow">Method</p><h2>Report details</h2></div></div><p class="subtitle">Source: <strong>{_text(source)}</strong><br>{_text(details)}<br>{_compact(comparison.get("tasks", 0))} logical tasks at an average of {_money(comparison.get("average_cost_per_task_usd", 0.0))} each.<br>{_text(effort_details)}</p></section>
      </div>
      <section class="panel daily-panel"><div class="section-heading"><div><p class="eyebrow">Recent days</p><h2>Daily activity</h2></div><span class="section-note">Latest 14 active days</span></div>{_daily_table(report)}</section>
    </div>
    <p class="footer">Estimated cost uses the checked-in local pricing catalog. This report contains aggregate usage data, not prompts or responses.</p>
  </main>
</body>
</html>
"""


def write_html_report(path: Path, report: dict[str, Any]) -> Path:
    """Write a self-contained dashboard and return the expanded target path."""

    target = path.expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_html(report), encoding="utf-8")
    return target
