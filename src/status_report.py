"""Static status page: database, schedules, snapshots and coverage."""

from __future__ import annotations

import os
import platform
import time
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    import psycopg

from . import report, styles
from .player_report import (
    _atomic_write,
    _date,
    _esc,
    _frequency_name,
    _years_months,
)
from .storage import as_datetime
from .utility import DEFAULT_PRIORITY_INTERVAL_SECONDS

PROCESS_STARTED_EPOCH = int(time.time())


class DatabaseStatus(NamedTuple):
    connected: bool
    latency_ms: float | None
    server_version: str | None
    database: str | None
    user: str | None
    host: str | None
    port: int | None
    size_bytes: int | None
    server_started_epoch: int | None
    error: str | None = None


class TableInfo(NamedTuple):
    name: str
    rows: int | None


class RunInfo(NamedTuple):
    id: int
    started_epoch: int
    finished_epoch: int | None
    status: str
    requests: int | None
    failures: int
    players: int | None


class FrequencyStatus(NamedTuple):
    name: str
    count: int
    first_epoch: int | None
    last_epoch: int | None


class StatusData(NamedTuple):
    generated: str
    now_epoch: int
    version: str
    started_epoch: int
    database: DatabaseStatus
    tables: tuple[TableInfo, ...]
    runs: tuple[RunInfo, ...]
    snapshots: tuple[FrequencyStatus, ...]
    fetches: tuple[FrequencyStatus, ...]
    coverage: list[tuple[int, int]]
    player_count: int
    coverage_total: int
    priority_players: int
    backfill_interval: int
    collect_interval: int
    priority_interval: int
    retry_window: int
    lookback: int
    report_interval: int
    newest_checkpoint_epoch: int | None
    newest_priority_epoch: int | None
    next_live_epoch: int
    next_priority_epoch: int


TABLE_NAMES = (
    "players",
    "snapshots",
    "checkpoints",
    "priority_snapshots",
    "priority_checkpoints",
    "runs",
)


def _epoch(value) -> int | None:
    return int(value.timestamp()) if value is not None else None


def _text(value) -> str | None:
    return None if value is None else str(value)


def _rows(conn, sql: str, params=None) -> list[tuple]:
    try:
        return conn.execute(sql, params).fetchall()
    except Exception:  # noqa: BLE001 - a broken query is not a broken page
        return []


def _row(conn, sql: str, params=None) -> tuple | None:
    try:
        return conn.execute(sql, params).fetchone()
    except Exception:  # noqa: BLE001
        return None


def _database_status(conn) -> DatabaseStatus:
    started = time.perf_counter()
    try:
        conn.execute("SELECT 1").fetchone()
        latency_ms = (time.perf_counter() - started) * 1000
    except Exception as error:  # noqa: BLE001
        return DatabaseStatus(
            False, None, None, None, None, None, None, None, None, str(error)
        )

    info = getattr(conn, "info", None)
    port = getattr(info, "port", None)
    size_row = _row(conn, "SELECT pg_database_size(current_database())")
    size_bytes = (
        int(size_row[0])
        if size_row and size_row[0] is not None
        else None
    )
    started_row = _row(conn, "SELECT pg_postmaster_start_time()")
    server_started_epoch = (
        _epoch(started_row[0]) if started_row else None
    )
    return DatabaseStatus(
        True,
        latency_ms,
        _text(getattr(info, "server_version", None)),
        _text(getattr(info, "dbname", None)),
        _text(getattr(info, "user", None)),
        _text(getattr(info, "host", None)),
        int(port) if port is not None else None,
        size_bytes,
        server_started_epoch,
    )


def _table_info(conn) -> tuple[TableInfo, ...]:
    rows = _rows(
        conn,
        """
        SELECT relname, GREATEST(reltuples, 0)::bigint
        FROM pg_class WHERE relname = ANY(%s)
        """,
        (list(TABLE_NAMES),),
    )
    counts = {row[0]: int(row[1]) for row in rows}
    return tuple(
        TableInfo(name, counts.get(name)) for name in TABLE_NAMES
    )


def _newest(conn, table: str) -> int | None:
    if table not in ("checkpoints", "priority_checkpoints"):
        raise ValueError(table)
    row = _row(conn, f"SELECT max(observed_at) FROM {table}")
    return _epoch(row[0]) if row else None


def _runs(conn) -> tuple[RunInfo, ...]:
    rows = _rows(
        conn,
        """
        SELECT id, started_at, finished_at, status, request_count,
               failure_count, player_count
        FROM runs ORDER BY id DESC LIMIT 5
        """,
    )
    return tuple(
        RunInfo(
            int(row[0]),
            _epoch(row[1]) or 0,
            _epoch(row[2]),
            str(row[3]),
            int(row[4]) if row[4] is not None else None,
            int(row[5] or 0),
            int(row[6]) if row[6] is not None else None,
        )
        for row in rows
    )


def _grouped_times(
    conn, table: str, interval: int
) -> dict[bool, tuple[int, int | None, int | None]]:
    if table not in (
        "snapshots", "checkpoints", "priority_snapshots",
        "priority_checkpoints",
    ):
        raise ValueError(table)
    rows = _rows(
        conn,
        f"""
        SELECT mod(extract(epoch FROM observed_at)::bigint, %s) = 0
               AS backfill,
               count(*), min(observed_at), max(observed_at)
        FROM (SELECT DISTINCT observed_at FROM {table}) AS times
        GROUP BY backfill
        """,
        (max(1, interval),),
    )
    grouped: dict[bool, tuple[int, int | None, int | None]] = {
        True: (0, None, None),
        False: (0, None, None),
    }
    for backfill, count, first, last in rows:
        grouped[bool(backfill)] = (
            int(count), _epoch(first), _epoch(last)
        )
    return grouped


def _total_times(
    conn, table: str
) -> tuple[int, int | None, int | None]:
    if table not in ("priority_snapshots", "priority_checkpoints"):
        raise ValueError(table)
    row = _row(
        conn,
        f"""
        SELECT count(*), min(observed_at), max(observed_at)
        FROM (SELECT DISTINCT observed_at FROM {table}) AS times
        """,
    )
    if not row:
        return 0, None, None
    return int(row[0]), _epoch(row[1]), _epoch(row[2])


def _frequencies(
    conn,
    snapshot_table: str,
    checkpoint_table: str,
    priority_interval: int,
    collect_interval: int,
    backfill_interval: int,
) -> tuple[tuple[FrequencyStatus, ...], tuple[FrequencyStatus, ...]]:
    priority_name = f"{_frequency_name(priority_interval)} priority"
    live_name = _frequency_name(collect_interval)
    backfill_name = _frequency_name(backfill_interval)

    snapshots_grouped = _grouped_times(
        conn, snapshot_table, backfill_interval
    )
    checks_grouped = _grouped_times(
        conn, checkpoint_table, backfill_interval
    )
    # The priority tables have no grouped variant.
    snapshot_priority = _total_times(conn, "priority_snapshots")
    check_priority = _total_times(conn, "priority_checkpoints")

    def build(
        priority: tuple[int, int | None, int | None],
        grouped: dict[bool, tuple[int, int | None, int | None]],
    ) -> tuple[FrequencyStatus, ...]:
        return (
            FrequencyStatus(priority_name, *priority),
            FrequencyStatus(live_name, *grouped[False]),
            FrequencyStatus(backfill_name, *grouped[True]),
        )

    return build(snapshot_priority, snapshots_grouped), build(
        check_priority, checks_grouped
    )


def _next_slot(now_epoch: int, interval: int, offset: int) -> int:
    if interval <= 0:
        return now_epoch + offset
    return (now_epoch // interval + 1) * interval + offset


def collect_status(
    conn,
    *,
    generated: str,
    now_epoch: int,
    version: str,
    report_interval: int,
    backfill_interval: int,
    collect_interval: int,
    retry_window: int,
    lookback: int,
    coverage: list[tuple[int, int]],
    player_count: int,
    coverage_total: int,
    priority_players: int,
) -> StatusData:
    """Read everything the status page shows, degrading to unknown."""
    priority_interval = DEFAULT_PRIORITY_INTERVAL_SECONDS
    snapshots, fetches = _frequencies(
        conn,
        "snapshots",
        "checkpoints",
        priority_interval,
        collect_interval,
        backfill_interval,
    )
    return StatusData(
        generated=generated,
        now_epoch=now_epoch,
        version=version,
        started_epoch=PROCESS_STARTED_EPOCH,
        database=_database_status(conn),
        tables=_table_info(conn),
        runs=_runs(conn),
        snapshots=snapshots,
        fetches=fetches,
        coverage=coverage,
        player_count=player_count,
        coverage_total=coverage_total,
        priority_players=priority_players,
        backfill_interval=backfill_interval,
        collect_interval=collect_interval,
        priority_interval=priority_interval,
        retry_window=retry_window,
        lookback=lookback,
        report_interval=report_interval,
        newest_checkpoint_epoch=_newest(conn, "checkpoints"),
        newest_priority_epoch=_newest(conn, "priority_checkpoints"),
        next_live_epoch=_next_slot(now_epoch, collect_interval, 60),
        next_priority_epoch=_next_slot(now_epoch, priority_interval, 5),
    )


def _plural(count: int, unit: str) -> str:
    return f"{count} {unit}" + ("s" if count != 1 else "")


def _elapsed(seconds: int) -> str:
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    parts: list[str] = []
    if days:
        parts.append(_plural(days, "day"))
    if hours:
        parts.append(_plural(hours, "hour"))
    if minutes and not days:
        parts.append(_plural(minutes, "minute"))
    if not parts:
        return "less than a minute"
    return " ".join(parts)


def _until(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "less than a minute"
    return f"in {_elapsed(seconds)}"


def _size(size_bytes: int | None) -> str:
    if size_bytes is None:
        return "unknown"
    value = float(size_bytes)
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "bytes":
                return _plural(int(value), "byte")
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def _stamp(epoch: int | None) -> str:
    if epoch is None:
        return "unknown"
    return as_datetime(epoch).strftime("%Y-%m-%d %H:%M UTC")


def _clock(epoch: int) -> str:
    return as_datetime(epoch).strftime("%H:%M UTC")


def coverage_runs(
    coverage: list[tuple[int, int]]
) -> list[tuple[int, int, int]]:
    """(start index, length, player count) for equal-count runs."""
    runs: list[tuple[int, int, int]] = []
    for index, (_, present) in enumerate(coverage):
        if runs and runs[-1][2] == present:
            start, length, _ = runs[-1]
            runs[-1] = (start, length + 1, present)
        else:
            runs.append((index, 1, present))
    return runs


def coverage_bar(data: StatusData) -> str:
    total = max(1, data.coverage_total)
    segments: list[str] = []
    for index, length, present in coverage_runs(data.coverage):
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
        duration = _years_months(end_epoch - start_epoch)
        ratio = min(1.0, present / total)
        pct = round(ratio * 100)
        label = f"{when} ({duration}): {present}/{total} players ({pct}%)"
        if present:
            style = (
                f' style="flex:{length};'
                f'opacity:{0.15 + 0.85 * ratio:.2f}"'
            )
            segments.append(f'<span data-tip="{_esc(label)}"{style}></span>')
        else:
            segments.append(
                f'<span class="zero" style="flex:{length}" '
                f'data-tip="{_esc(label)}"></span>'
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


def coverage_tip_html() -> str:
    return (
        '<div class="tip" id="coverage-tip" role="tooltip" hidden></div>'
        "<script>(function(){"
        "var tip=document.getElementById('coverage-tip');"
        "if(!tip){return;}"
        "function place(x,y){"
        "tip.style.left='0px';tip.style.top='0px';"
        "var box=tip.getBoundingClientRect(),pad=8;"
        "var left=Math.min(Math.max(pad,x-box.width/2),"
        "window.innerWidth-box.width-pad);"
        "var top=y-box.height-14;"
        "if(top<pad){top=y+18;}"
        "tip.style.left=left+'px';tip.style.top=top+'px';}"
        "function show(span,x,y){"
        "tip.textContent=span.getAttribute('data-tip');"
        "tip.hidden=false;place(x,y);}"
        "function hide(){tip.hidden=true;tip.removeAttribute('data-target');}"
        "var coarse=window.matchMedia&&"
        "window.matchMedia('(hover: none)').matches;"
        "var spans=document.querySelectorAll('.coverage span[data-tip]');"
        "Array.prototype.forEach.call(spans,function(span){"
        "if(coarse){"
        "span.addEventListener('click',function(event){"
        "event.preventDefault();"
        "var target=span.getAttribute('data-tip');"
        "var same=!tip.hidden&&"
        "tip.getAttribute('data-target')===target;"
        "if(same){hide();return;}"
        "tip.setAttribute('data-target',target);"
        "show(span,event.clientX,event.clientY);});"
        "}else{"
        "span.addEventListener('mouseenter',function(event){"
        "show(span,event.clientX,event.clientY);});"
        "span.addEventListener('mousemove',function(event){"
        "place(event.clientX,event.clientY);});"
        "span.addEventListener('mouseleave',hide);"
        "}});"
        "document.addEventListener('click',function(event){"
        "var target=event.target;"
        "if(!target.closest||"
        "!target.closest('.coverage span[data-tip]')){hide();}});"
        "})();</script>"
    )


def _connection_line(database: DatabaseStatus) -> str:
    if not database.connected:
        return f"unreachable: {database.error or 'unknown error'}"
    if database.latency_ms is None:
        return "connected"
    return f"connected, {database.latency_ms:.0f} ms"


def _endpoint(database: DatabaseStatus) -> str:
    if not database.host:
        return "unknown"
    if database.port is None:
        return database.host
    return f"{database.host}:{database.port}"


def _fact_rows(rows: list[tuple[str, str]]) -> str:
    return (
        '<table class="facts"><tbody>'
        + "".join(
            f"<tr><th>{label}</th><td>{value}</td></tr>"
            for label, value in rows
        )
        + "</tbody></table>"
    )


def _interval_label(seconds: int) -> str:
    if seconds < 3600:
        return _plural(max(1, seconds // 60), "minute")
    return report._hours(seconds)


def _date_or_dash(epoch: int | None) -> str:
    return _date(epoch) if epoch is not None else "-"


def _frequency_table(entries: tuple[FrequencyStatus, ...]) -> str:
    rows = "".join(
        f"<tr><td>{_esc(entry.name)}</td>"
        f'<td class="num">{entry.count:,}</td>'
        f'<td class="date">{_date_or_dash(entry.first_epoch)}</td>'
        f'<td class="date">{_date_or_dash(entry.last_epoch)}</td></tr>'
        for entry in entries
    )
    return (
        '<table class="grid"><thead><tr><th>interval</th>'
        '<th class="num">snapshots</th><th class="date">first</th>'
        '<th class="date">last</th></tr></thead><tbody>'
        + rows
        + "</tbody></table>"
    )


def _fetch_table(entries: tuple[FrequencyStatus, ...]) -> str:
    return _frequency_table(entries).replace(
        '<th class="num">snapshots</th>',
        '<th class="num">checkpoints</th>',
    )


def _num_or_dash(value: int | None) -> str:
    return "-" if value is None else f"{value:,}"


def _run_line(runs: tuple[RunInfo, ...]) -> str:
    if not runs:
        return "no runs yet"
    run = runs[0]
    if run.finished_epoch is None:
        duration = "running"
    else:
        duration = _elapsed(run.finished_epoch - run.started_epoch)
    failures = f"{run.failures:,} failure" + (
        "" if run.failures == 1 else "s"
    )
    return (
        f"run {run.id}: {run.status}, "
        f"{_num_or_dash(run.requests)} requests, "
        f"{failures}, {_num_or_dash(run.players)} players, "
        f"{duration}"
    )


def render_status_page(data: StatusData) -> str:
    database = data.database
    uptime = data.now_epoch - data.started_epoch
    tables = "".join(
        f"<tr><td>{_esc(table.name)}</td>"
        f'<td class="num">'
        f"{_num_or_dash(table.rows)}</td></tr>"
        for table in data.tables
    )
    runs = "".join(
        f"<tr><td class=rank>{run.id}</td>"
        f"<td>{_esc(_stamp(run.started_epoch))}</td>"
        f"<td>{_esc(run.status)}</td>"
        f'<td class="num">{_num_or_dash(run.requests)}</td>'
        f'<td class="num">{run.failures:,}</td>'
        f'<td class="num">{_num_or_dash(run.players)}</td></tr>'
        for run in data.runs
    )
    if not runs:
        runs = '<tr><td class=empty colspan="6">(no runs yet)</td></tr>'

    newest = data.newest_checkpoint_epoch
    checkpoint_age = (
        f"{_elapsed(data.now_epoch - newest)} ago"
        if newest is not None else "unknown"
    )
    newest_priority = data.newest_priority_epoch
    priority_age = (
        f"{_elapsed(data.now_epoch - newest_priority)} ago"
        if newest_priority is not None else "unknown"
    )
    server_started = database.server_started_epoch
    database_up = (
        "unknown"
        if server_started is None
        else f"{_stamp(server_started)} "
        f"({_elapsed(data.now_epoch - server_started)})"
    )
    parts: list[str] = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>status - xp tracker</title>",
        styles.style_tag() + "</head>",
        '<body id="top" class="status">',
        "<h1>xp tracker status</h1>",
        '<p class="sub"><a href="latest.html">rankings</a> · '
        '<a href="latest.txt">text report</a> · '
        '<a href="priority/index.html">priority tracking</a></p>',
        f'<p class="meta">generated at: {_esc(data.generated)}</p>',
        "<h2>service</h2>",
        _fact_rows([
            ("uptime", _esc(_elapsed(uptime))),
            ("started at", _esc(_stamp(data.started_epoch))),
            (
                "process",
                _esc(
                    f"pid {os.getpid()}, python {platform.python_version()}, "
                    f"{platform.platform()}"
                ),
            ),
            (
                "report rebuild",
                _esc(
                    "not on a timer"
                    if data.report_interval <= 0
                    else f"every {_interval_label(data.report_interval)}"
                ),
            ),
        ]),
        "<h2>database</h2>",
        _fact_rows([
            ("connection", _esc(_connection_line(database))),
            ("server version", _esc(database.server_version or "unknown")),
            ("database up since", _esc(database_up)),
            ("database", _esc(database.database or "unknown")),
            ("host", _esc(_endpoint(database))),
            ("user", _esc(database.user or "unknown")),
            ("size on disk", _esc(_size(database.size_bytes))),
        ]),
        "<h3>tables</h3>",
        '<table class="grid"><thead><tr><th>table</th>'
        '<th class="num">estimated rows</th></tr></thead><tbody>'
        + tables
        + "</tbody></table>",
        "<h2>collection</h2>",
        _fact_rows([
            (
                "backfill interval",
                _esc(_interval_label(data.backfill_interval)),
            ),
            (
                "live interval",
                _esc(_interval_label(data.collect_interval)),
            ),
            (
                "priority interval",
                _esc(_interval_label(data.priority_interval)),
            ),
            ("retry window", _esc(_years_months(data.retry_window))),
            ("lookback interval", _esc(_years_months(data.lookback))),
            ("last run", _esc(_run_line(data.runs))),
            (
                "next live fetch",
                _esc(
                    f"at {_clock(data.next_live_epoch)}, "
                    f"{_until(data.next_live_epoch - data.now_epoch)}"
                ),
            ),
            (
                "next priority fetch",
                _esc(
                    f"at {_clock(data.next_priority_epoch)}, "
                    f"{_until(data.next_priority_epoch - data.now_epoch)}"
                ),
            ),
            ("newest checkpoint", _esc(checkpoint_age)),
            ("newest priority checkpoint", _esc(priority_age)),
        ]),
        "<h2>players</h2>",
        _fact_rows([
            ("players logged", _esc(report._players_line(data))),
            ("priority players tracked", str(data.priority_players)),
        ]),
        "<h2>snapshots</h2>",
        _frequency_table(data.snapshots),
        _fetch_table(data.fetches),
        '<p class="note">snapshots store only changed xp; checkpoints record '
        "every fetch.</p>",
        "<h2>data coverage</h2>",
        '<p class="note">each segment is an equal slice of the lookback '
        "window, fading with the share of players that hold a checkpoint "
        "there.</p>",
        coverage_bar(data),
        coverage_tip_html(),
        "<h2>recent runs</h2>",
        '<table class="grid"><thead><tr><th>#</th><th>started (UTC)</th>'
        '<th>status</th><th class="num">requests</th>'
        '<th class="num">failures</th><th class="num">players</th>'
        "</tr></thead><tbody>" + runs + "</tbody></table>",
        '<a class="to-top" href="#top" aria-label="back to top">'
        "&#8593; Top</a>",
        f'<footer class="version">{_esc(data.version)}</footer>',
        "</body></html>",
    ]
    return "\n".join(parts) + "\n"


def write_status_page(conn, report_dir, **kwargs) -> Path:
    """Collect and write status.html; return its path."""
    data = collect_status(conn, **kwargs)
    page = render_status_page(data)
    path = Path(report_dir) / "status.html"
    _atomic_write(path, page)
    return path
