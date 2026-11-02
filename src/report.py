"""Compute rankings and render the plain-text report."""

from __future__ import annotations

import datetime as dt
import json
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    import psycopg

from .utility import DEFAULT_PRIORITY_INTERVAL_SECONDS, SKILLS, SPANS
from .player_report import (
    Record,
    SKILL_TAB_SCRIPT,
    _bucket_count,
    _date,
    _esc,
    _years_months,
    player_page_href,
    scan_player_points as _scan_player_points,
    skill_panel_open,
    skill_tabs_html,
)
from .storage import as_datetime, coverage_buckets, xp_at_or_before
from . import styles


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
        ("lookback interval", _years_months(data.lookback)),
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
        )
    )
    parts: list[str] = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>xp tracker - {_esc(_date(data.now_epoch))}</title>",
        f"{styles.style_tag()}</head>",
        '<body id="top" class="rankings wide">',
        "<h1>XP Tracker</h1>",
        '<p class="sub"><a href="latest.txt">text report</a></p>',
        '<p class="sub"><a href="priority/index.html">'
        "priority tracking</a></p>",
        '<p class="sub"><a href="status.html">status</a></p>',
        metas,
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
