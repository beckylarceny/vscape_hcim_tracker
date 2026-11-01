"""Compute rankings and render the plain-text report."""

from __future__ import annotations

import datetime as dt
import json
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    import psycopg

from .config import DEFAULT_PRIORITY_INTERVAL_SECONDS, SKILLS, SPANS
from .player_report import (
    Record,
    SKILL_TABS_CSS,
    SKILL_TAB_SCRIPT,
    _bucket_count,
    _coverage_runs,
    _date,
    _esc,
    player_page_href,
    scan_player_points as _scan_player_points,
    skill_panel_open,
    skill_tabs_html,
)
from .storage import as_datetime, coverage_buckets, xp_at_or_before


class ReportData(NamedTuple):
    generated: str
    now_epoch: int
    interval: int
    collect_interval: int
    lookback: int
    player_count: int
    coverage_total: int
    gains: dict[str, dict[str, list[tuple[str, int]]]]
    records: dict[str, dict[str, list[Record]]]
    coverage: list[tuple[int, int]]
    players: tuple[str, ...] = ()
    version: str = "software version unknown"


def read_version(report_dir) -> str:
    """One-line version label written by scripts/update.sh."""
    try:
        text = (report_dir / "version").read_text(encoding="utf-8").strip()
    except OSError:
        return "software version unknown"
    parts = text.split(maxsplit=1)
    if not parts:
        return "software version unknown"
    if len(parts) == 1:
        return f"software version {parts[0]}"
    return f"software version {parts[0]}, updated {parts[1]}"


def _hours(seconds: int) -> str:
    hours = seconds / 3600
    return f"{hours:g} {'hour' if hours == 1 else 'hours'}"


def _years_months(seconds: int) -> str:
    days = seconds // 86400
    years, remainder = divmod(days, 365)
    months = remainder // 30
    parts: list[str] = []
    if years:
        parts.append(f"{years} {'year' if years == 1 else 'years'}")
    if months:
        parts.append(f"{months} {'month' if months == 1 else 'months'}")
    if not parts:
        return f"{days} {'day' if days == 1 else 'days'}"
    return " ".join(parts)


def _top_gains_for_skill(
    conn: psycopg.Connection,
    skill: str,
    now_epoch: int,
    span: int,
    top_n: int,
) -> list[tuple[str, int]]:
    latest = xp_at_or_before(conn, skill, now_epoch)
    earlier = xp_at_or_before(conn, skill, now_epoch - span)
    gains: list[tuple[str, int]] = []
    for username, xp in latest.items():
        before = earlier.get(username)
        if before is not None and xp > before:
            gains.append((username, xp - before))
    gains.sort(key=lambda item: item[1], reverse=True)
    return gains[:top_n]


def _scan_records_for_skill(
    conn: psycopg.Connection,
    skill: str,
    now_epoch: int,
    lookback: int,
    slack: int,
    top_n: int,
) -> dict[str, list[Record]]:
    """Best window of each span, sliding across the whole lookback period.

    Stored points are sparse: a skill's xp is treated as constant between two
    stored points. A window therefore needs only one stored point at or before
    its start, and one at its end.
    """
    spans = list(SPANS.items())
    heaps: dict[str, list[Record]] = {name: [] for name, _ in spans}
    margin = max(span for _, span in spans)

    sql = """
        SELECT username, observed_at, xp
        FROM snapshots
        WHERE skill = %s AND observed_at >= %s
        ORDER BY username, observed_at
    """
    earliest = now_epoch - lookback - margin

    with conn.cursor(name=f"records_{skill}") as cursor:
        cursor.itersize = 20000
        cursor.execute(sql, (skill, as_datetime(earliest)))

        current_user: str | None = None
        points: list[tuple[int, int]] = []
        for username, observed_at, xp in cursor:
            if username != current_user:
                if current_user is not None:
                    _scan_player_points(
                        current_user, points, spans, now_epoch, lookback,
                        slack, top_n, heaps,
                    )
                current_user = username
                points = []
            points.append((int(observed_at.timestamp()), xp))
        if current_user is not None:
            _scan_player_points(
                current_user, points, spans, now_epoch, lookback,
                slack, top_n, heaps,
            )

    result: dict[str, list[Record]] = {}
    for name, _ in spans:
        result[name] = sorted(heaps[name], reverse=True)[:top_n]
    return result


def build_report_data(
    conn: psycopg.Connection,
    now_epoch: int,
    interval: int,
    lookback: int,
    top_n: int,
    player_count: int,
    collect_interval: int | None = None,
    coverage_total: int | None = None,
) -> ReportData:
    """Compute every ranking once so the text and HTML reports share it."""
    resolution = collect_interval or interval
    coverage_total = max(1, coverage_total or player_count)
    generated = dt.datetime.now(tz=dt.timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC"
    )

    gains: dict[str, dict[str, list[tuple[str, int]]]] = {}
    for name, span in SPANS.items():
        gains[name] = {
            skill: _top_gains_for_skill(conn, skill, now_epoch, span, top_n)
            for skill in SKILLS
        }

    records: dict[str, dict[str, list[Record]]] = {
        name: {} for name in SPANS
    }
    for skill in SKILLS:
        per_span = _scan_records_for_skill(
            conn, skill, now_epoch, lookback, interval, top_n
        )
        for name in SPANS:
            records[name][skill] = per_span[name]

    # One bar segment per day of the lookback window.
    buckets = _bucket_count(lookback)
    coverage = coverage_buckets(
        conn, now_epoch - lookback, now_epoch, buckets
    )

    players = tuple(
        row[0]
        for row in conn.execute(
            "SELECT username FROM players ORDER BY lower(username)"
        ).fetchall()
    )

    return ReportData(
        generated=generated,
        now_epoch=now_epoch,
        interval=interval,
        collect_interval=resolution,
        lookback=lookback,
        player_count=player_count,
        coverage_total=coverage_total,
        gains=gains,
        records=records,
        coverage=coverage,
        players=players,
    )


def _text_span_block(
    lines: list[str], title: str, rows_for
) -> None:
    """One PAST or BEST block: a span heading, its rows, blank line."""
    lines.append(title)
    for span_name, span in SPANS.items():
        lines.append(f"    {span_name.capitalize()} ({span // 3600}h)")
        rows = rows_for(span_name)
        lines.extend(rows)
        if not rows:
            lines.append("          (no data)")
        lines.append("")


def render_text(data: ReportData) -> str:
    lines = ["XP TRACKER REPORT"]
    for name, value in (
        ("generated at", data.generated),
        ("grid end", _date(data.now_epoch)),
        ("backfill interval", _hours(data.interval)),
        ("live interval", _hours(data.collect_interval)),
        ("priority interval", f"{DEFAULT_PRIORITY_INTERVAL_SECONDS // 60} minutes"),
        ("lookback", _years_months(data.lookback)),
        ("players logged", _players_line(data)),
    ):
        lines.append(f"{name + ':':<20}{value}")
    lines.append("")

    for skill in SKILLS:
        lines.append("=" * 78)
        lines.append(skill)
        lines.append("=" * 78)
        lines.append("")
        _text_span_block(
            lines,
            "  PAST",
            lambda span: [
                f"      {rank:>2}. {username:<24} {gain:>15,}"
                for rank, (username, gain) in enumerate(
                    data.gains[span].get(skill, []), start=1
                )
            ],
        )
        _text_span_block(
            lines,
            "  BEST",
            lambda span: [
                f"      {rank:>2}. {record.username:<24} "
                f"{record.gain:>15,}  "
                f"{_date(record.start_epoch)}..{_date(record.end_epoch)}"
                for rank, record in enumerate(
                    data.records[span].get(skill, []), start=1
                )
            ],
        )

    return "\n".join(lines) + "\n\n" + data.version + "\n"


_CSS = """
:root{color-scheme:light dark}
*{box-sizing:border-box}
body{font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;margin:0 auto;
     padding:2rem 1rem;max-width:72rem;text-transform:lowercase}
h1{font-size:1.9rem;margin:0 0 .35rem}
h2{font-size:1.5rem;margin:2.75rem 0 .6rem;border-bottom:1px solid #8884;
   padding-bottom:.3rem}
h3{font-size:1.15rem;margin:1.4rem 0 .35rem;font-weight:600}
.meta{color:#888;font-size:.85rem;margin:0 0 .15rem}
.sub{margin:.2rem 0 .8rem;font-size:1.05rem}
.coverage{display:flex;gap:1px;height:14px;margin:.3rem 0 .1rem;
          background:#8882;border-radius:0;overflow:hidden}
.coverage span{flex:1;background:#9079B6;min-width:1px}
.coverage span.zero{background:#8883;opacity:1}
.range{display:flex;justify-content:space-between;color:#888;font-size:.75rem}
.find{display:flex;gap:.5rem;margin:1.25rem 0 2.5rem;
      padding-bottom:1.25rem;border-bottom:1px solid #8884}
.find input{flex:1;min-width:0;max-width:26rem;padding:.4rem .55rem;
            font:inherit;color:inherit;background:transparent;
            border:1px solid #8886;border-radius:0}
.find button{padding:.4rem .9rem;font:inherit;border:1px solid #8886;
             border-radius:0;background:#9079B6;color:#fff;cursor:pointer}
h4{font-size:.95rem;margin:.9rem 0 .2rem;color:#888;text-transform:lowercase;
   letter-spacing:.04em}
h2,h3,h4{text-align:center}
.tables{display:grid;align-items:start;
        grid-template-columns:repeat(auto-fit,minmax(min(32rem,100%),1fr));
        gap:.75rem 2rem}
.tables>div{min-width:0;overflow-x:auto}
table{width:auto;max-width:100%;border-collapse:collapse;
      margin:.25rem auto 1rem;table-layout:auto}
th,td{text-align:left;padding:.25rem .55rem;border-bottom:1px solid #8884;
      overflow-wrap:anywhere}
th{font-weight:600}
td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
th.num{text-align:right}
td.date{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
table.past th:nth-child(1),table.past td:nth-child(1){width:6%}
table.past th:nth-child(2),table.past td:nth-child(2){width:56%}
table.past td:nth-child(2),table.best td:nth-child(2){white-space:nowrap}
table.past th:nth-child(3),table.past td:nth-child(3){width:38%}
table.best th:nth-child(1),table.best td:nth-child(1){width:6%}
table.best th:nth-child(3),table.best td:nth-child(3){width:26%}
table.best.wide th:nth-child(2),table.best.wide td:nth-child(2){width:22%}
table.best.wide th:nth-child(4),table.best.wide td:nth-child(4){width:23%}
table.best.wide th:nth-child(5),table.best.wide td:nth-child(5){width:23%}
table.best.day th:nth-child(2),table.best.day td:nth-child(2){width:42%}
table.best.day th:nth-child(4),table.best.day td:nth-child(4){width:26%}
td.rank{width:2.5rem;color:#888}
tr:nth-child(even) td{background:#8881}
td.empty{text-align:center;color:#888}
a{color:#7BB59A}
.to-top{position:fixed;right:1rem;bottom:1rem;z-index:10;padding:.45rem .7rem;
        background:#9079B6;color:#fff;font-size:.85rem;line-height:1;
        text-decoration:none;border:1px solid #8886;border-radius:0;
        box-shadow:0 1px 4px #0003}
.to-top:hover{background:#7B659C;color:#fff}
.version{text-align:right;color:#888;font-size:.75rem;margin:2rem 0 3.5rem}
@media (max-width:48rem){
  table{font-size:.8em;table-layout:fixed;width:calc(100% - 2rem);
        margin-left:auto;margin-right:auto}
  th,td{padding:.15rem .3rem}
  table.past td:nth-child(2),table.best td:nth-child(2){white-space:normal}
  table.best.wide th:nth-child(2),table.best.wide td:nth-child(2){width:20%}
  table.best.wide th:nth-child(3),table.best.wide td:nth-child(3){width:28%}
  table.best.wide th:nth-child(4),table.best.wide td:nth-child(4){width:23%}
  table.best.wide th:nth-child(5),table.best.wide td:nth-child(5){width:23%}
}
"""


def _player_link(username: str) -> str:
    return f'<a href="{_esc(player_page_href(username))}">{_esc(username)}</a>'


def _table(css: str, header: str, rows: list[str], colspan: int) -> str:
    if not rows:
        rows = [
            f'<tr><td class=empty colspan="{colspan}">(no data yet)</td></tr>'
        ]
    return (
        f'<table class="{css}"><thead><tr>{header}</tr></thead><tbody>'
        + "".join(rows)
        + "</tbody></table>"
    )


def _players_line(data: ReportData) -> str:
    if data.coverage_total > data.player_count:
        return f"{data.player_count} / {data.coverage_total}"
    return str(data.player_count)


def _gains_table(gains: list[tuple[str, int]]) -> str:
    rows = [
        f"<tr><td class=rank>{rank}</td><td>{_player_link(username)}</td>"
        f"<td class=num>{gain:,}</td></tr>"
        for rank, (username, gain) in enumerate(gains, start=1)
    ]
    header = '<th>#</th><th>Player</th><th class="num">XP</th>'
    return _table("past", header, rows, 3)


def _records_table(records: list[Record], span_name: str) -> str:
    single_date = span_name == "day"
    rows = []
    for rank, record in enumerate(records, start=1):
        if single_date:
            dates = f"<td class=date>{_date(record.end_epoch)}</td>"
        else:
            dates = (
                f"<td class=date>{_date(record.start_epoch)}</td>"
                f"<td class=date>{_date(record.end_epoch)}</td>"
            )
        rows.append(
            f"<tr><td class=rank>{rank}</td><td>{_player_link(record.username)}</td>"
            f"<td class=num>{record.gain:,}</td>{dates}</tr>"
        )
    if single_date:
        header = (
            "<th>#</th><th>Player</th><th class=\"num\">XP</th>"
            '<th class="num">Date</th>'
        )
        colspan, css = 4, "best day"
    else:
        header = (
            "<th>#</th><th>Player</th><th class=\"num\">XP</th>"
            '<th class="num">Start date</th>'
            '<th class="num">End date</th>'
        )
        colspan, css = 5, "best wide"
    return _table(css, header, rows, colspan)


def _coverage_bar(data: ReportData) -> str:
    total = max(1, data.coverage_total)
    segments: list[str] = []
    for index, length, present in _coverage_runs(data.coverage):
        start_epoch = data.coverage[index][0]
        next_index = index + length
        end_epoch = (
            data.coverage[next_index][0]
            if next_index < len(data.coverage)
            else data.now_epoch
        )
        first = _date(start_epoch)
        last = _date(end_epoch)
        when = first if first == last else f"{first} - {last}"
        ratio = min(1.0, present / total)
        pct = round(ratio * 100)
        label = f"{when}: {present}/{total} players ({pct}%)"
        if present:
            style = (
                f' style="flex:{length};'
                f'opacity:{0.15 + 0.85 * ratio:.2f}"'
            )
            segments.append(f'<span title="{_esc(label)}"{style}></span>')
        else:
            segments.append(
                f'<span class="zero" style="flex:{length}" '
                f'title="{_esc(label)}"></span>'
            )
    if not segments:
        segments.append('<span class="zero"></span>')
    first = (
        _date(data.coverage[0][0]) if data.coverage else _date(data.now_epoch)
    )
    last = _date(data.now_epoch)
    return (
        '<div class="coverage" role="img" '
        f'aria-label="data coverage from {first} to {last}">'
        + "".join(segments)
        + "</div>"
        f'<div class="range"><span>{first}</span><span>{last}</span></div>'
    )


def _js_literal(value: object) -> str:
    """JSON that is safe to embed in an inline <script> element."""
    text = json.dumps(value, ensure_ascii=True)
    for char, escape in (
        ("&", "\\u0026"),
        ("<", "\\u003c"),
        (">", "\\u003e"),
        ("\u2028", "\\u2028"),
        ("\u2029", "\\u2029"),
    ):
        text = text.replace(char, escape)
    return text


def _player_finder(data: ReportData) -> str:
    ordered = sorted(data.players, key=str.casefold)
    options = "".join(
        f'<option value="{_esc(name)}"></option>' for name in ordered
    )
    lookup = {name.casefold(): player_page_href(name) for name in ordered}
    return (
        '<form class="find" onsubmit="return findPlayer(this)">'
        '<input name="player" list="player-list" placeholder="player name" '
        'aria-label="player name" autocomplete="off" required>'
        f'<datalist id="player-list">{options}</datalist>'
        '<button type="submit">Go</button></form>'
        f"<script>const PLAYERS={_js_literal(lookup)};"
        "function findPlayer(form){"
        "const name=form.player.value.trim();"
        "const url=PLAYERS[name.toLowerCase()];"
        "if(!url){alert('unknown player: '+name);return false;}"
        "window.open(url,'_blank','noopener');return false;}</script>"
    )


def render_html(data: ReportData) -> str:
    metas = "\n".join(
        f'<p class="meta">{name}: {value}</p>'
        for name, value in (
            ("generated at", _esc(data.generated)),
            ("players logged", _players_line(data)),
            ("backfill interval", _hours(data.interval)),
            ("live interval", _hours(data.collect_interval)),
            ("priority interval", f"{DEFAULT_PRIORITY_INTERVAL_SECONDS // 60} minutes"),
            ("lookback", _years_months(data.lookback)),
        )
    )
    parts: list[str] = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>xp tracker - {_esc(_date(data.now_epoch))}</title>",
        f"<style>{_CSS}{SKILL_TABS_CSS}</style></head><body id=\"top\">",
        "<h1>XP Tracker</h1>",
        '<p class="sub"><a href="latest.txt">text report</a></p>',
        '<p class="sub"><a href="priority/index.html">priority tracking</a></p>',
        metas,
        '<p class="meta">data coverage:</p>',
        _coverage_bar(data),
        _player_finder(data),
    ]

    parts.append(skill_tabs_html())

    for index, skill in enumerate(SKILLS):
        parts.append(skill_panel_open(index, skill))
        parts.append(f"<h2>{_esc(skill)}</h2>")
        parts.append("<h3>Past</h3>")
        parts.append('<div class="tables">')
        for span_name in SPANS:
            parts.append(
                f"<div><h4>{span_name.capitalize()}</h4>"
                f"{_gains_table(data.gains[span_name].get(skill, []))}</div>"
            )
        parts.append("</div>")
        parts.append("<h3>Best</h3>")
        parts.append('<div class="tables">')
        for span_name in SPANS:
            parts.append(
                f"<div><h4>{span_name.capitalize()}</h4>"
                f"{_records_table(data.records[span_name].get(skill, []), span_name)}"
                "</div>"
            )
        parts.append("</div>")
        parts.append("</section>")

    parts.append(SKILL_TAB_SCRIPT)
    parts.append(
        '<a class="to-top" href="#top" aria-label="back to top">'
        "&#8593; Top</a>"
    )
    parts.append(f'<footer class="version">{_esc(data.version)}</footer>')
    parts.append("</body></html>")
    return "\n".join(parts) + "\n"


def build_report(
    conn: psycopg.Connection,
    now_epoch: int,
    interval: int,
    lookback: int,
    top_n: int,
    player_count: int,
    collect_interval: int | None = None,
) -> str:
    return render_text(
        build_report_data(
            conn, now_epoch, interval, lookback, top_n, player_count,
            collect_interval,
        )
    )
