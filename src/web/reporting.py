"""Build the static reports served by the web service."""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from pathlib import Path

from ..common import storage
from ..common.priority import (
    priority_index_names,
    refresh_priority_players,
    tracked_priority_names,
)
from ..common.utility import (
    DEFAULT_BASE_URL,
    DEFAULT_RETRY_WINDOW_SECONDS,
    TRACKED_ACCOUNT_CODES,
    effective_lookback,
)
from .render.html import atomic_write
from .reports import general, index, player, priority
from .status import report as status_report

log = logging.getLogger("vscape_tracker.web.reporting")

_REPORT_LOCK = threading.Lock()


def _remove_old_reports(report_dir: Path) -> None:
    """Remove old timestamped reports and temporary files."""
    for pattern in (
        "report_*.txt", "report_*.html", ".general.*.tmp", "players/.*.tmp",
    ):
        for path in report_dir.glob(pattern):
            path.unlink(missing_ok=True)


_PlayerMark = tuple[int, int, object, int, object]
_PLAYER_WATERMARKS: dict[Path, dict[str, _PlayerMark]] = {}
_PLAYER_PRIORITY_STATE: dict[Path, frozenset[str]] = {}

# Ranking data changes only when rows are written. Recomputing it every minute
# scans the whole snapshot history, so reuse the previous result until the
# table counts move. Window-boundary drift within an hour is immaterial.
_CACHE_TOLERANCE_SECONDS = 3600
_REPORT_CACHE: dict[
    tuple, tuple[tuple[int, int, int, int], int, general.ReportData]
] = {}
_REPORT_CACHE_SIZE = 4


def _timestamp() -> str:
    return dt.datetime.now(tz=dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _data_version(conn) -> tuple[int, int, int, int]:
    row = conn.execute(
        """
        SELECT (SELECT count(*) FROM players),
               (SELECT count(*) FROM players WHERE ironman = ANY(%s)),
               (SELECT count(*) FROM snapshots),
               (SELECT count(*) FROM checkpoints)
        """,
        (list(TRACKED_ACCOUNT_CODES),),
    ).fetchone()
    return (int(row[0]), int(row[1]), int(row[2]), int(row[3]))


def report_data(
    conn,
    now_epoch: int,
    interval: int,
    lookback: int,
    top_n: int,
    player_count: int,
    collect_interval: int,
    coverage_total: int | None = None,
) -> general.ReportData:
    """Ranking data, recomputed only when the stored rows or parameters change."""
    key = (
        interval, lookback, top_n, player_count, collect_interval, coverage_total,
    )
    version = _data_version(conn)
    cached = _REPORT_CACHE.get(key)
    if (
        cached is not None
        and cached[0] == version
        and 0 <= now_epoch - cached[1] <= _CACHE_TOLERANCE_SECONDS
    ):
        return cached[2]._replace(
            now_epoch=now_epoch, generated=_timestamp()
        )

    data = general.build_report_data(
        conn, now_epoch, interval, lookback, top_n, player_count,
        collect_interval, coverage_total,
    )
    if len(_REPORT_CACHE) >= _REPORT_CACHE_SIZE:
        _REPORT_CACHE.pop(next(iter(_REPORT_CACHE)))
    _REPORT_CACHE[key] = (version, now_epoch, data)
    return data


def _player_watermarks(conn) -> dict[str, _PlayerMark]:
    """Snapshot count and newest snapshot per player, regular and priority.

    Only stored xp changes matter. Checkpoints record fetches, so rebuilding
    on them would rewrite every inactive player's page on every collection
    run. A retry that fills an old gap without new xp leaves the coverage
    shading until that player's next gain.
    """
    rows = conn.execute(
        """
        WITH regular AS (
            SELECT username, count(*) AS rows, max(observed_at) AS newest
            FROM snapshots
            GROUP BY username
        ), priority AS (
            SELECT username, count(*) AS rows, max(observed_at) AS newest
            FROM priority_snapshots
            GROUP BY username
        )
        SELECT p.username, p.ironman,
               coalesce(r.rows, 0), r.newest,
               coalesce(q.rows, 0), q.newest
        FROM players p
        LEFT JOIN regular r ON r.username = p.username
        LEFT JOIN priority q ON q.username = p.username
        WHERE p.ironman = ANY(%s)
        """,
        (list(TRACKED_ACCOUNT_CODES),),
    ).fetchall()
    return {
        username: (
            int(ironman),
            int(regular_rows), regular_newest,
            int(priority_rows), priority_newest,
        )
        for (
            username, ironman, regular_rows, regular_newest,
            priority_rows, priority_newest,
        ) in rows
    }


def write_player_pages(
    conn, report_dir: Path, data,
    watermarks: dict[str, _PlayerMark],
    previous: dict[str, _PlayerMark] | None,
    priority_names: tuple[str, ...] = (),
    force: bool = False,
    version: str = "software version unknown",
) -> int:
    """Write HTML and text reports for each player whose stored xp changed."""
    directory = report_dir / "players"
    directory.mkdir(parents=True, exist_ok=True)
    priority = set(priority_names)
    entries = [
        (username, player.player_slug(username))
        for username in data.players
    ]
    expected = {
        f"{stem}{suffix}"
        for _, stem in entries
        for suffix in (".html", ".txt")
    }
    stale = [
        username
        for username, stem in entries
        if force
        or previous is None
        or previous.get(username) != watermarks.get(username)
        or not (directory / f"{stem}.html").exists()
        or not (directory / f"{stem}.txt").exists()
    ]

    names = dict(entries)
    written = 0
    for username in stale:
        stem = names[username]
        try:
            page_data = player.collect_player_data(
                conn, username, data.now_epoch, data.lookback,
                data.interval, data.generated, data.collect_interval,
            )
            page = player.render_player_page(
                page_data, is_priority=username in priority, version=version
            )
            text = player.render_player_text(
                page_data, is_priority=username in priority, version=version
            )
        except Exception:  # noqa: BLE001 - one page must not break the rest
            log.exception("could not build the player report for %r", username)
            continue
        atomic_write(directory / f"{stem}.html", page)
        atomic_write(directory / f"{stem}.txt", text)
        written += 1
        if written % 50 == 0:
            log.info(
                "wrote %d/%d player reports", written, len(stale)
            )
    for pattern in ("*.html", "*.txt"):
        for path in directory.glob(pattern):
            if path.name not in expected:
                path.unlink(missing_ok=True)
    return written


def write_report(
    conn,
    report_dir: Path,
    now_epoch: int,
    interval: int,
    lookback: int,
    top_n: int,
    player_count: int,
    collect_interval: int,
    coverage_total: int | None = None,
    priority_players: tuple[str, ...] = (),
    base_url: str = DEFAULT_BASE_URL,
    report_interval: int = 0,
    retry_window: int = DEFAULT_RETRY_WINDOW_SECONDS,
) -> Path:
    """Write the text and HTML reports from one shared computation."""
    with _REPORT_LOCK:
        data = report_data(
            conn, now_epoch, interval, lookback, top_n, player_count,
            collect_interval, coverage_total,
        )
        data = data._replace(version=general.read_version(report_dir))
        text = general.render_text(data)
        page = general.render_html(data)

        report_dir.mkdir(parents=True, exist_ok=True)
        text_path = report_dir / "latest.txt"
        html_path = report_dir / "general.html"
        atomic_write(text_path, text)
        atomic_write(html_path, page)
        _remove_old_reports(report_dir)

        key = report_dir.resolve()
        priority_names = tracked_priority_names(conn, priority_players)
        index.write_index_page(
            report_dir, data.generated, data.player_count,
            len(priority_names), data.version,
        )
        try:
            status_report.write_status_page(
                conn, report_dir,
                generated=data.generated,
                now_epoch=data.now_epoch,
                version=data.version,
                report_interval=report_interval,
                backfill_interval=data.interval,
                collect_interval=data.collect_interval,
                retry_window=retry_window,
                lookback=data.lookback,
                coverage=data.coverage,
                player_count=data.player_count,
                coverage_total=data.coverage_total,
                priority_players=len(priority_names),
            )
        except Exception:  # noqa: BLE001 - status must not block rankings
            log.exception("could not write status.html")

        priority_key = frozenset(priority_names)
        force = _PLAYER_PRIORITY_STATE.get(key) != priority_key
        _PLAYER_PRIORITY_STATE[key] = priority_key

        watermarks = _player_watermarks(conn)
        previous = _PLAYER_WATERMARKS.get(key)
        if previous != watermarks or force:
            count = write_player_pages(
                conn, report_dir, data, watermarks, previous,
                priority_names=priority_names, force=force,
                version=data.version,
            )
            _PLAYER_WATERMARKS[key] = watermarks
            if count:
                log.info(
                    "wrote %d player reports to %s", count,
                    report_dir / "players",
                )

        count = priority.write_priority_reports(
            conn, report_dir,
            priority_index_names(conn, priority_players),
            data.now_epoch, data.generated, base_url,
            version=data.version,
        )
        if count:
            log.debug(
                "wrote %d priority reports to %s", count,
                report_dir / "priority",
            )
    log.info("wrote reports to %s and %s", text_path, html_path)
    return html_path


def write_current_report(args, dsn: str) -> None:
    """Render the page from existing data, e.g. before the first run."""
    conn = storage.connect(dsn)
    try:
        row = conn.execute(
            "SELECT count(*) FROM players WHERE ironman = ANY(%s)",
            (list(TRACKED_ACCOUNT_CODES),),
        ).fetchone()
        verified = int(row[0])
        row = conn.execute(
            "SELECT max(candidate_count) FROM runs"
        ).fetchone()
        candidates = int(row[0]) if row and row[0] else verified
        now = int(time.time())
        write_report(
            conn, Path(args.report_dir), now, args.interval,
            effective_lookback(now, args.lookback), args.top, verified,
            args.collect_interval,
            coverage_total=candidates,
            priority_players=getattr(args, "priority_players", ()),
            base_url=getattr(args, "base_url", DEFAULT_BASE_URL),
            report_interval=getattr(args, "report_interval", 0),
            retry_window=getattr(
                args, "retry_window", DEFAULT_RETRY_WINDOW_SECONDS
            ),
        )
    finally:
        conn.close()


def _report_loop(args, dsn: str, stop: threading.Event) -> None:
    while not stop.wait(args.report_interval):
        refresh_priority_players(args)
        try:
            write_current_report(args, dsn)
        except Exception:  # noqa: BLE001 - keep the loop alive
            log.exception("periodic report failed")


def start_report_thread(args, dsn: str) -> threading.Event | None:
    """Regenerate the page on a fixed timer, independent of collection."""
    if args.report_interval <= 0:
        return None
    stop = threading.Event()
    thread = threading.Thread(
        target=_report_loop,
        args=(args, dsn, stop),
        name="vscape_tracker-report",
        daemon=True,
    )
    thread.start()
    return stop
