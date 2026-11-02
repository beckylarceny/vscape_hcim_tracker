"""Static status page: database, schedules, snapshots and coverage."""

from __future__ import annotations

import http.client
import json
import os
import platform
import socket
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
from ..common.storage import as_datetime
from ..common.utility import DEFAULT_PRIORITY_INTERVAL_SECONDS

PROCESS_STARTED_EPOCH = int(time.time())

DOCKER_SOCKET = os.environ.get(
    "VSCAPE_HCIM_TRACKER_DOCKER_SOCKET", "/var/run/docker.sock"
)
COMPOSE_PROJECT = os.environ.get(
    "VSCAPE_HCIM_TRACKER_COMPOSE_PROJECT", "vscape_hcim_tracker"
)


class DatabaseStatus(NamedTuple):
    connected: bool
    latency_ms: float | None
    server_version: str | None
    database: str | None
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


class ContainerInfo(NamedTuple):
    service: str
    name: str
    state: str
    status: str
    image: str


class DockerStatus(NamedTuple):
    containers: tuple[ContainerInfo, ...]
    error: str | None = None


class StatusData(NamedTuple):
    generated: str
    now_epoch: int
    version: str
    started_epoch: int
    containers: DockerStatus
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

EXACT_TABLES = ("players", "runs")


def _epoch(value) -> int | None:
    return int(value.timestamp()) if value is not None else None


def _text(value) -> str | None:
    return None if value is None else str(value)


def _server_version(value) -> str | None:
    """Render psycopg's integer server_version as major.minor.patch."""
    if value is None:
        return None
    text = str(value)
    try:
        number = int(text)
    except ValueError:
        return text
    if number < 10000:
        return text
    major = number // 10000
    minor = number // 100 % 100
    patch = number % 100
    return f"{major}.{minor}.{patch}"


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


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float = 3.0) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(self.timeout)
            sock.connect(self.socket_path)
        except Exception:
            sock.close()
            raise
        self.sock = sock


def _docker_get(socket_path: str, path: str):
    connection = _UnixHTTPConnection(socket_path)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        body = response.read()
        if response.status != 200:
            raise OSError(f"docker api returned {response.status}")
        return json.loads(body)
    finally:
        connection.close()


def _container_list(payload) -> list[dict]:
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def _self_project(payload) -> str | None:
    hostname = socket.gethostname()
    for container in _container_list(payload):
        if str(container.get("Id", "")).startswith(hostname):
            labels = container.get("Labels") or {}
            project = labels.get("com.docker.compose.project")
            if project:
                return str(project)
    return None


def _container_rows(
    payload, project: str
) -> tuple[ContainerInfo, ...]:
    rows: list[ContainerInfo] = []
    for container in _container_list(payload):
        labels = container.get("Labels") or {}
        if labels.get("com.docker.compose.project") != project:
            continue
        names = container.get("Names") or []
        if names:
            name = str(names[0])
            if name.startswith("/"):
                name = name[1:]
        else:
            name = str(container.get("Id", ""))[:12]
        rows.append(
            ContainerInfo(
                str(labels.get("com.docker.compose.service", "")),
                name,
                str(container.get("State", "")),
                str(container.get("Status", "")),
                str(container.get("Image", "")),
            )
        )
    rows.sort(key=lambda row: (row.service, row.name))
    return tuple(rows)


def container_status(
    socket_path: str = DOCKER_SOCKET,
) -> DockerStatus:
    try:
        payload = _docker_get(socket_path, "/containers/json?all=1")
        project = _self_project(payload) or COMPOSE_PROJECT
        return DockerStatus(_container_rows(payload, project))
    except Exception as error:  # noqa: BLE001 - docker must not break the page
        return DockerStatus((), str(error))


def _database_status(conn) -> DatabaseStatus:
    started = time.perf_counter()
    try:
        conn.execute("SELECT 1").fetchone()
        latency_ms = (time.perf_counter() - started) * 1000
    except Exception as error:  # noqa: BLE001
        return DatabaseStatus(
            False, None, None, None, None, None, None, None, str(error)
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
        _server_version(getattr(info, "server_version", None)),
        _text(getattr(info, "dbname", None)),
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
    for name in EXACT_TABLES:
        row = _row(conn, f"SELECT count(*) FROM {name}")
        if row and row[0] is not None:
            counts[name] = int(row[0])
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
        containers=container_status(),
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


def _coverage_segments(
    data: StatusData,
) -> list[tuple[int, int, str]]:
    """(length, player count, label) for each coverage run."""
    total = max(1, data.coverage_total)
    segments: list[tuple[int, int, str]] = []
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
        segments.append((length, present, label))
    return segments


def coverage_bar(data: StatusData) -> str:
    total = max(1, data.coverage_total)
    segments: list[str] = []
    for length, present, label in _coverage_segments(data):
        ratio = min(1.0, present / total)
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
            f"<tr><th>{label}</th><td>{_esc(value)}</td></tr>"
            for label, value in rows
        )
        + "</tbody></table>"
    )


def _container_table(containers: DockerStatus) -> str:
    rows = "".join(
        f'<tr><th scope="row">{_esc(container.service)}</th>'
        f"<td>{_esc(container.name)}</td>"
        f"<td>{_esc(container.state)}</td>"
        f"<td>{_esc(container.status)}</td>"
        f"<td>{_esc(container.image)}</td></tr>"
        for container in containers.containers
    )
    if not rows:
        message = (
            f"docker api unavailable: {containers.error}"
            if containers.error
            else "no containers found"
        )
        rows = (
            f'<tr><td class=empty colspan="5">{_esc(message)}</td></tr>'
        )
    return (
        '<table class="grid containers"><thead><tr><th>service</th>'
        "<th>container</th><th>state</th><th>status</th>"
        "<th>image</th></tr></thead><tbody>"
        + rows
        + "</tbody></table>"
    )


def _interval_label(seconds: int) -> str:
    if seconds < 3600:
        return _plural(max(1, seconds // 60), "minute")
    return report._hours(seconds)


def _date_or_dash(epoch: int | None) -> str:
    return _date(epoch) if epoch is not None else "-"


def _service_rows(data: StatusData) -> list[tuple[str, str]]:
    uptime = data.now_epoch - data.started_epoch
    rebuild = (
        "not on a timer"
        if data.report_interval <= 0
        else f"every {_interval_label(data.report_interval)}"
    )
    return [
        ("uptime", _elapsed(uptime)),
        ("started at", _stamp(data.started_epoch)),
        (
            "process",
            f"python {platform.python_version()}, {platform.platform()}",
        ),
        ("report rebuild", rebuild),
    ]


def _database_rows(data: StatusData) -> list[tuple[str, str]]:
    database = data.database
    server_started = database.server_started_epoch
    database_up = (
        "unknown"
        if server_started is None
        else f"{_stamp(server_started)} "
        f"({_elapsed(data.now_epoch - server_started)})"
    )
    return [
        ("connection", _connection_line(database)),
        ("server version", database.server_version or "unknown"),
        ("database up since", database_up),
        ("database", database.database or "unknown"),
        ("host", _endpoint(database)),
        ("size on disk", _size(database.size_bytes)),
    ]


def _collection_rows(data: StatusData) -> list[tuple[str, str]]:
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
    return [
        (
            "backfill interval",
            _interval_label(data.backfill_interval),
        ),
        ("live interval", _interval_label(data.collect_interval)),
        ("priority interval", _interval_label(data.priority_interval)),
        ("retry window", _years_months(data.retry_window)),
        ("lookback interval", _years_months(data.lookback)),
        ("last run", _run_line(data.runs)),
        ("last run ended", _last_run_ended(data.runs, data.now_epoch)),
        (
            "next live fetch",
            f"at {_clock(data.next_live_epoch)}, "
            f"{_until(data.next_live_epoch - data.now_epoch)}",
        ),
        (
            "next priority fetch",
            f"at {_clock(data.next_priority_epoch)}, "
            f"{_until(data.next_priority_epoch - data.now_epoch)}",
        ),
        ("newest checkpoint", checkpoint_age),
        ("newest priority checkpoint", priority_age),
    ]


def _players_rows(data: StatusData) -> list[tuple[str, str]]:
    return [
        ("players logged", report._players_line(data)),
        ("priority players tracked", str(data.priority_players)),
    ]


def _table_rows(data: StatusData) -> list[tuple[str, int | None]]:
    return [(table.name, table.rows) for table in data.tables]


def _frequency_rows(
    entries: tuple[FrequencyStatus, ...],
) -> list[tuple[str, int, int | None, int | None]]:
    return [
        (entry.name, entry.count, entry.first_epoch, entry.last_epoch)
        for entry in entries
    ]


def _frequency_table(
    entries: tuple[FrequencyStatus, ...], heading: str
) -> str:
    rows = "".join(
        f'<tr><th scope="row">{_esc(name)}</th>'
        f'<td class="num">{count:,}</td>'
        f'<td class="date">{_date_or_dash(first)}</td>'
        f'<td class="date">{_date_or_dash(last)}</td></tr>'
        for name, count, first, last in _frequency_rows(entries)
    )
    return (
        '<table class="grid"><thead><tr><th>interval</th>'
        f'<th class="num">{heading}</th><th class="date">first</th>'
        '<th class="date">last</th></tr></thead><tbody>'
        + rows
        + "</tbody></table>"
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


def _last_run_ended(
    runs: tuple[RunInfo, ...], now_epoch: int
) -> str:
    if not runs:
        return "unknown"
    finished = runs[0].finished_epoch
    if finished is None:
        return "running now"
    return f"{_elapsed(now_epoch - finished)} ago"


def render_status_page(data: StatusData) -> str:
    tables = "".join(
        f'<tr><th scope="row">{_esc(name)}</th>'
        f'<td class="num">{_num_or_dash(rows)}</td></tr>'
        for name, rows in _table_rows(data)
    )
    runs = "".join(
        f'<tr><th scope="row" class=rank>{run.id}</th>'
        f"<td>{_esc(_stamp(run.started_epoch))}</td>"
        f"<td>{_esc(run.status)}</td>"
        f'<td class="num">{_num_or_dash(run.requests)}</td>'
        f'<td class="num">{run.failures:,}</td>'
        f'<td class="num">{_num_or_dash(run.players)}</td></tr>'
        for run in data.runs
    )
    if not runs:
        runs = '<tr><td class=empty colspan="6">(no runs yet)</td></tr>'

    parts: list[str] = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>status - xp tracker</title>",
        styles.style_tag() + "</head>",
        '<body id="top" class="status">',
        '<div class="backlinks"><p class="sub">'
        '<a href="general.html">general tracking</a> · '
        '<a href="priority/index.html">priority tracking</a></p></div>',
        "<h1>xp tracker status</h1>",
        '<p class="sub"><a href="status.txt">text version</a></p>',
        f'<p class="meta">generated at: {_esc(data.generated)}</p>',
        "<h2>service</h2>",
        _fact_rows(_service_rows(data)),
        "<h3>containers</h3>",
        _container_table(data.containers),
        "<h2>database</h2>",
        _fact_rows(_database_rows(data)),
        "<h3>tables</h3>",
        '<table class="grid"><thead><tr><th>table</th>'
        '<th class="num">rows</th></tr></thead><tbody>'
        + tables
        + "</tbody></table>",
        '<p class="note">players and runs are counted exactly; snapshot '
        "and checkpoint tables use the planner estimate.</p>",
        "<h2>collection</h2>",
        _fact_rows(_collection_rows(data)),
        "<h2>players</h2>",
        _fact_rows(_players_rows(data)),
        "<h2>snapshots</h2>",
        _frequency_table(data.snapshots, "snapshots"),
        _frequency_table(data.fetches, "checkpoints"),
        '<p class="note">snapshots store only changed xp; checkpoints record '
        "every fetch.</p>",
        "<h2>data coverage</h2>",
        '<p class="note">each segment is an equal slice of the lookback '
        "window, fading with the share of players that hold a checkpoint "
        "there.</p>",
        coverage_bar(data),
        coverage_tip_html(),
        "<h2>recent runs</h2>",
        '<table class="grid runs"><thead><tr><th>#</th>'
        '<th>started (UTC)</th>'
        '<th>status</th><th class="num">requests</th>'
        '<th class="num">failures</th><th class="num">players</th>'
        "</tr></thead><tbody>" + runs + "</tbody></table>",
        '<a class="to-top" href="#top" aria-label="back to top">'
        "&#8593; Top</a>",
        f'<footer class="version">{_esc(data.version)}</footer>',
        "</body></html>",
    ]
    return "\n".join(parts) + "\n"


LABEL_WIDTH = 28
TABLE_INDENT = 28


def _text_facts(rows: list[tuple[str, str]]) -> list[str]:
    return [
        f"{label + ':':<{LABEL_WIDTH}}{value}" for label, value in rows
    ]


def _text_table(
    headings: tuple[str, ...],
    rows: list[tuple[str, ...]],
    numeric: tuple[int, ...] = (),
) -> list[str]:
    widths = [
        max([len(heading)] + [len(row[index]) for row in rows])
        for index, heading in enumerate(headings)
    ]
    widths[0] = max(TABLE_INDENT, widths[0])

    def line(values: tuple[str, ...]) -> str:
        cells: list[str] = []
        for index, (value, width) in enumerate(zip(values, widths)):
            if index in numeric:
                cells.append(f"{value:>{width}}")
            else:
                cells.append(f"{value:<{width}}")
        return "   ".join(cells).rstrip()

    return [line(headings)] + [line(row) for row in rows]


def _text_containers(containers: DockerStatus) -> list[str]:
    rows = [
        (
            container.service,
            container.name,
            container.state,
            container.status,
            container.image,
        )
        for container in containers.containers
    ]
    if not rows:
        message = (
            f"docker api unavailable: {containers.error}"
            if containers.error
            else "no containers found"
        )
        return [message]
    return _text_table(
        ("service", "container", "state", "status", "image"), rows
    )


def _text_tables(data: StatusData) -> list[str]:
    return _text_table(
        ("table", "rows"),
        [
            (name, _num_or_dash(rows))
            for name, rows in _table_rows(data)
        ],
        (1,),
    )


def _text_frequency(
    entries: tuple[FrequencyStatus, ...],
) -> list[str]:
    rows = [
        (
            name,
            f"{count:,}",
            _date_or_dash(first),
            _date_or_dash(last),
        )
        for name, count, first, last in _frequency_rows(entries)
    ]
    return _text_table(("", "count", "first", "last"), rows, (1,))


def _text_runs(runs: tuple[RunInfo, ...]) -> list[str]:
    rows = [
        (
            str(run.id),
            _stamp(run.started_epoch),
            run.status,
            _num_or_dash(run.requests),
            f"{run.failures:,}",
            _num_or_dash(run.players),
        )
        for run in runs
    ]
    lines = _text_table(
        (
            "#",
            "started (UTC)",
            "status",
            "requests",
            "failures",
            "players",
        ),
        rows,
        (0, 3, 4, 5),
    )
    if not runs:
        lines.append("(no runs yet)")
    return lines


def _coverage_labels(data: StatusData) -> list[str]:
    return [label for _, _, label in _coverage_segments(data)]


def render_status_text(data: StatusData) -> str:
    lines = ["XP TRACKER STATUS"]
    lines.extend(_text_facts([("generated at", data.generated)]))
    lines.append("")
    sections = (
        ("SERVICE", _text_facts(_service_rows(data))),
        ("CONTAINERS", _text_containers(data.containers)),
        ("DATABASE", _text_facts(_database_rows(data))),
        ("TABLES", _text_tables(data)),
        ("COLLECTION", _text_facts(_collection_rows(data))),
        ("PLAYERS", _text_facts(_players_rows(data))),
        ("SNAPSHOTS", _text_frequency(data.snapshots)),
        ("CHECKPOINTS", _text_frequency(data.fetches)),
        ("DATA COVERAGE", _coverage_labels(data)),
        ("RECENT RUNS", _text_runs(data.runs)),
    )
    for heading, rows in sections:
        lines.append(heading)
        lines.extend(rows)
        lines.append("")
    return "\n".join(lines) + "\n\n" + data.version + "\n"


def write_status_page(conn, report_dir, **kwargs) -> Path:
    """Collect and write status.html and status.txt; return html's path."""
    data = collect_status(conn, **kwargs)
    page = render_status_page(data)
    text = render_status_text(data)
    directory = Path(report_dir)
    path = directory / "status.html"
    _atomic_write(path, page)
    _atomic_write(directory / "status.txt", text)
    return path
