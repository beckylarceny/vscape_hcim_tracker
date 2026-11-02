"""Status data: database queries, coverage slices and display facts."""

from __future__ import annotations

import platform
import time
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    import psycopg

from ..common.storage import CoverageSlice, as_datetime
from ..common.utility import DEFAULT_PRIORITY_INTERVAL_SECONDS, next_slot
from .format import frequency_name, hours, players_line, plural, years_months
from .html import date
from .status_docker import DockerStatus, container_status

PROCESS_STARTED_EPOCH = int(time.time())

TABLE_NAMES = (
    "players",
    "snapshots",
    "checkpoints",
    "priority_snapshots",
    "priority_checkpoints",
    "runs",
)

EXACT_TABLES = ("players", "runs")


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
    coverage: list[CoverageSlice]
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


def _epoch(value) -> int | None:
    return int(value.timestamp()) if value is not None else None


def _text(value) -> str | None:
    return None if value is None else str(value)


def server_version(value) -> str | None:
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
        server_version(getattr(info, "server_version", None)),
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
    priority_name = f"{frequency_name(priority_interval)} priority"
    live_name = frequency_name(collect_interval)
    backfill_name = frequency_name(backfill_interval)

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
    coverage: list[CoverageSlice],
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
        next_live_epoch=next_slot(now_epoch, collect_interval, 60),
        next_priority_epoch=next_slot(now_epoch, priority_interval, 5),
    )


def elapsed(seconds: int) -> str:
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    parts: list[str] = []
    if days:
        parts.append(plural(days, "day"))
    if hours:
        parts.append(plural(hours, "hour"))
    if minutes and not days:
        parts.append(plural(minutes, "minute"))
    if not parts:
        return "less than a minute"
    return " ".join(parts)


def until(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "less than a minute"
    return f"in {elapsed(seconds)}"


def size(size_bytes: int | None) -> str:
    if size_bytes is None:
        return "unknown"
    value = float(size_bytes)
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "bytes":
                return plural(int(value), "byte")
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def stamp(epoch: int | None) -> str:
    if epoch is None:
        return "unknown"
    return as_datetime(epoch).strftime("%Y-%m-%d %H:%M UTC")


def clock(epoch: int) -> str:
    return as_datetime(epoch).strftime("%H:%M UTC")


def interval_label(seconds: int) -> str:
    if seconds < 3600:
        return plural(max(1, seconds // 60), "minute")
    return hours(seconds)


def date_or_dash(epoch: int | None) -> str:
    return date(epoch) if epoch is not None else "-"


def num_or_dash(value: int | None) -> str:
    return "-" if value is None else f"{value:,}"


def connection_line(database: DatabaseStatus) -> str:
    if not database.connected:
        return f"unreachable: {database.error or 'unknown error'}"
    if database.latency_ms is None:
        return "connected"
    return f"connected, {database.latency_ms:.0f} ms"


def endpoint(database: DatabaseStatus) -> str:
    if not database.host:
        return "unknown"
    if database.port is None:
        return database.host
    return f"{database.host}:{database.port}"


def run_line(runs: tuple[RunInfo, ...]) -> str:
    if not runs:
        return "no runs yet"
    run = runs[0]
    if run.finished_epoch is None:
        duration = "running"
    else:
        duration = elapsed(run.finished_epoch - run.started_epoch)
    failures = f"{run.failures:,} failure" + (
        "" if run.failures == 1 else "s"
    )
    return (
        f"run {run.id}: {run.status}, "
        f"{num_or_dash(run.requests)} requests, "
        f"{failures}, {num_or_dash(run.players)} players, "
        f"{duration}"
    )


def last_run_ended(runs: tuple[RunInfo, ...], now_epoch: int) -> str:
    if not runs:
        return "unknown"
    finished = runs[0].finished_epoch
    if finished is None:
        return "running now"
    return f"{elapsed(now_epoch - finished)} ago"


def service_rows(data: StatusData) -> list[tuple[str, str]]:
    uptime = data.now_epoch - data.started_epoch
    rebuild = (
        "not on a timer"
        if data.report_interval <= 0
        else f"every {interval_label(data.report_interval)}"
    )
    return [
        ("uptime", elapsed(uptime)),
        ("started at", stamp(data.started_epoch)),
        (
            "process",
            f"python {platform.python_version()}, {platform.platform()}",
        ),
        ("report rebuild", rebuild),
    ]


def database_rows(data: StatusData) -> list[tuple[str, str]]:
    database = data.database
    server_started = database.server_started_epoch
    database_up = (
        "unknown"
        if server_started is None
        else f"{stamp(server_started)} "
        f"({elapsed(data.now_epoch - server_started)})"
    )
    return [
        ("connection", connection_line(database)),
        ("server version", database.server_version or "unknown"),
        ("database up since", database_up),
        ("database", database.database or "unknown"),
        ("host", endpoint(database)),
        ("size on disk", size(database.size_bytes)),
    ]


def backfill_rows(data: StatusData) -> list[tuple[str, str]]:
    return [
        (
            "backfill interval",
            interval_label(data.backfill_interval),
        ),
        ("retry window", years_months(data.retry_window)),
        ("lookback interval", years_months(data.lookback)),
        ("last run", run_line(data.runs)),
        ("last run ended", last_run_ended(data.runs, data.now_epoch)),
    ]


def live_rows(data: StatusData) -> list[tuple[str, str]]:
    newest = data.newest_checkpoint_epoch
    checkpoint_age = (
        f"{elapsed(data.now_epoch - newest)} ago"
        if newest is not None else "unknown"
    )
    return [
        ("live interval", interval_label(data.collect_interval)),
        (
            "next live fetch",
            f"at {clock(data.next_live_epoch)}, "
            f"{until(data.next_live_epoch - data.now_epoch)}",
        ),
        ("newest checkpoint", checkpoint_age),
    ]


def priority_rows(data: StatusData) -> list[tuple[str, str]]:
    newest_priority = data.newest_priority_epoch
    priority_age = (
        f"{elapsed(data.now_epoch - newest_priority)} ago"
        if newest_priority is not None else "unknown"
    )
    return [
        ("priority interval", interval_label(data.priority_interval)),
        (
            "next priority fetch",
            f"at {clock(data.next_priority_epoch)}, "
            f"{until(data.next_priority_epoch - data.now_epoch)}",
        ),
        ("newest priority checkpoint", priority_age),
    ]


def players_rows(data: StatusData) -> list[tuple[str, str]]:
    return [
        ("players logged", players_line(data.player_count, data.coverage_total)),
        ("priority players tracked", str(data.priority_players)),
    ]


def table_rows(data: StatusData) -> list[tuple[str, int | None]]:
    return [(table.name, table.rows) for table in data.tables]


def frequency_rows(
    entries: tuple[FrequencyStatus, ...],
) -> list[tuple[str, int, int | None, int | None]]:
    return [
        (entry.name, entry.count, entry.first_epoch, entry.last_epoch)
        for entry in entries
    ]


DENSITY_LABELS = {
    "hourly": "hourly checkpoints",
    "six-hour": "6-hour checkpoints",
    "daily": "daily checkpoints",
    "sparse": "sparse checkpoints",
}


def coverage_density(
    coverage: CoverageSlice, bucket_seconds: int
) -> str:
    """Fetch density class from checkpoints per player per day."""
    if not coverage.players:
        return "none"
    per_day = (
        coverage.checkpoints / coverage.players
        * 86400 / max(1, bucket_seconds)
    )
    if per_day >= 12:
        return "hourly"
    if per_day >= 3:
        return "six-hour"
    if per_day >= 1:
        return "daily"
    return "sparse"


def coverage_runs(
    coverage: list[CoverageSlice], bucket_seconds: int = 86400
) -> list[tuple[int, int, int, str]]:
    """(start index, length, player count, density) for equal runs."""
    runs: list[tuple[int, int, int, str]] = []
    for index, item in enumerate(coverage):
        density = coverage_density(item, bucket_seconds)
        if runs and runs[-1][2:] == (item.players, density):
            start, length, _, _ = runs[-1]
            runs[-1] = (start, length + 1, item.players, density)
        else:
            runs.append((index, 1, item.players, density))
    return runs


def coverage_segments(
    data: StatusData,
) -> list[tuple[int, int, str, str]]:
    """(length, player count, density, label) for each coverage run."""
    total = max(1, data.coverage_total)
    if len(data.coverage) > 1:
        bucket_seconds = (
            data.coverage[1].start_epoch - data.coverage[0].start_epoch
        )
    else:
        bucket_seconds = data.lookback
    segments: list[tuple[int, int, str, str]] = []
    runs = coverage_runs(data.coverage, bucket_seconds)
    for index, length, present, density in runs:
        start_epoch = data.coverage[index].start_epoch
        next_index = index + length
        end_epoch = (
            data.coverage[next_index].start_epoch
            if next_index < len(data.coverage)
            else data.now_epoch
        )
        first = date(start_epoch)
        last = date(end_epoch)
        when = first if first == last else f"{first} - {last}"
        duration = years_months(end_epoch - start_epoch)
        ratio = min(1.0, present / total)
        pct = round(ratio * 100)
        label = f"{when} ({duration}): {present}/{total} players ({pct}%)"
        if density in DENSITY_LABELS:
            label += f", {DENSITY_LABELS[density]}"
        segments.append((length, present, density, label))
    return segments
