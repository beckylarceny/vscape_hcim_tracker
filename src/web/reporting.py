"""Build the static reports served by the web service."""

from __future__ import annotations

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
    effective_lookback,
)
from . import player_report, priority_report, report, status_report
from .player_report import _atomic_write

log = logging.getLogger("vscape_hcim_tracker.web.reporting")

_REPORT_LOCK = threading.Lock()


def _remove_old_reports(report_dir: Path) -> None:
    """Only latest.txt and general.html are kept."""
    for pattern in (
        "report_*.txt", "report_*.html", ".general.*.tmp", "players/.*.tmp",
    ):
        for path in report_dir.glob(pattern):
            path.unlink(missing_ok=True)


_PLAYER_WATERMARKS: dict[Path, dict[str, tuple[int, object]]] = {}
_PLAYER_PRIORITY_STATE: dict[Path, frozenset[str]] = {}


def _player_watermarks(conn) -> dict[str, tuple[int, object]]:
    """Checkpoint count and newest checkpoint per player.

    Any new checkpoint, including a retry that fills an old gap, changes the
    count, so the matching page is rebuilt on the next report pass.
    """
    rows = conn.execute(
        """
        SELECT p.username, count(c.observed_at), max(c.observed_at)
        FROM players p
        LEFT JOIN checkpoints c ON c.username = p.username
        GROUP BY p.username
        """
    ).fetchall()
    return {
        username: (int(count), newest)
        for username, count, newest in rows
    }


def write_player_pages(
    conn, report_dir: Path, data,
    watermarks: dict[str, tuple[int, object]],
    previous: dict[str, tuple[int, object]] | None,
    priority_names: tuple[str, ...] = (),
    force: bool = False,
    version: str = "software version unknown",
) -> int:
    """Write one HTML report per player whose checkpoint history changed."""
    directory = report_dir / "players"
    directory.mkdir(parents=True, exist_ok=True)
    priority = set(priority_names)
    entries = [
        (username, f"{player_report.player_slug(username)}.html")
        for username in data.players
    ]
    expected = {name for _, name in entries}
    stale = [
        username
        for username, name in entries
        if force
        or previous is None
        or previous.get(username) != watermarks.get(username)
        or not (directory / name).exists()
    ]

    names = dict(entries)
    written = 0
    for username in stale:
        name = names[username]
        try:
            page_data = player_report.collect_player_data(
                conn, username, data.now_epoch, data.lookback,
                data.interval, data.generated, data.collect_interval,
            )
            page = player_report.render_player_page(
                page_data, is_priority=username in priority, version=version
            )
        except Exception:  # noqa: BLE001 - one page must not break the rest
            log.exception("could not build the player report for %r", username)
            continue
        _atomic_write(directory / name, page)
        written += 1
        if written % 50 == 0:
            log.info(
                "wrote %d/%d player reports", written, len(stale)
            )
    for path in directory.glob("*.html"):
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
        data = report.build_report_data(
            conn, now_epoch, interval, lookback, top_n, player_count,
            collect_interval, coverage_total,
        )
        data = data._replace(version=report.read_version(report_dir))
        text = report.render_text(data)
        page = report.render_html(data)

        report_dir.mkdir(parents=True, exist_ok=True)
        text_path = report_dir / "latest.txt"
        html_path = report_dir / "general.html"
        _atomic_write(text_path, text)
        _atomic_write(html_path, page)
        _remove_old_reports(report_dir)

        key = report_dir.resolve()
        priority_names = tracked_priority_names(conn, priority_players)
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

        count = priority_report.write_priority_reports(
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
        storage.ensure_schema(conn)
        row = conn.execute("SELECT count(*) FROM players").fetchone()
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
        name="vscape_hcim_tracker-report",
        daemon=True,
    )
    thread.start()
    return stop
