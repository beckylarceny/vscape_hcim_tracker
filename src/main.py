"""Entry point: collect hardcore ironman xp history and write a text report."""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import api, player_report, priority_report, report, storage, web
from .player_report import _atomic_write
from .config import (
    DEFAULT_BASE_URL,
    DEFAULT_COLLECT_INTERVAL_SECONDS,
    DEFAULT_INTERVAL_SECONDS,
    DEFAULT_LOOKBACK_SECONDS,
    DEFAULT_PRIORITY_INTERVAL_SECONDS,
    DEFAULT_PRIORITY_RETENTION_SECONDS,
    DEFAULT_PRIORITY_TOP,
    DEFAULT_RATE,
    DEFAULT_REPORT_INTERVAL_SECONDS,
    DEFAULT_RETRY_WINDOW_SECONDS,
    DEFAULT_TOP_N,
    DEFAULT_WORKERS,
    HCIM,
    SKILLS,
    build_dsn,
    parse_priority_players,
)

log = logging.getLogger("vscape_hcim_tracker")

_REPORT_LOCK = threading.Lock()

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(text: str) -> int:
    value = text.strip().lower()
    if value.isdigit():
        return int(value)
    if value and value[-1] in _UNITS:
        return int(float(value[:-1]) * _UNITS[value[-1]])
    raise argparse.ArgumentTypeError(f"invalid duration: {text!r}")


class RateLimiter:
    """Global spacing between HTTP requests, shared across worker threads."""

    def __init__(self, per_second: float) -> None:
        self._gap = 1.0 / per_second if per_second > 0 else 0.0
        self._lock = threading.Lock()
        self._next_at = 0.0

    def wait(self) -> None:
        if not self._gap:
            return
        with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next_at - now)
            self._next_at = max(now, self._next_at) + self._gap
        if delay:
            time.sleep(delay)


def _zero_stats() -> dict[str, int]:
    return {"requests": 0, "rows": 0, "failures": 0, "players": 0}


def _add_stats(totals: dict[str, int], stats: dict[str, int]) -> None:
    for key in totals:
        totals[key] += stats[key]


def build_grid(
    now_epoch: int,
    interval: int,
    collect_interval: int,
    lookback: int,
    retry_window: int,
    no_backfill: bool,
) -> list[int]:
    """Checkpoints to fetch, newest first.

    The recent retry window is always included, so a failed checkpoint is
    fetched again on the next run. The one-year backfill uses the coarse
    `interval` grid and is skipped with `no_backfill`.
    """
    current = (now_epoch // collect_interval) * collect_interval
    count = max(1, retry_window // collect_interval)
    times = {current - step * collect_interval for step in range(count)}
    if not no_backfill:
        anchor = (now_epoch // interval) * interval
        times.update(range(anchor, anchor - lookback - 1, -interval))
    return sorted(times, reverse=True)


def collect_player(
    dsn: str,
    username: str,
    grid_times: list[int],
    limiter: RateLimiter,
) -> dict[str, int]:
    """Fetch the xp history of one player and store it. Resumes automatically."""
    stats = _zero_stats()
    conn = storage.connect(dsn)
    pending: list[tuple] = []
    done: list[int] = []
    try:
        try:
            fetched = api.fetch_player_xp(username, wait=limiter.wait)
            stats["requests"] += 1
        except api.ApiError as error:
            log.warning("highscores fetch for %r failed: %s", username, error)
            stats["failures"] += 1
            return stats

        if fetched is None:
            log.warning("no highscores entry for %r", username)
            stats["failures"] += 1
            return stats

        ironman, xp_now = fetched
        if ironman != HCIM:
            # Only currently hardcore ironman accounts are recorded.
            log.info(
                "skipping %r: ironman status is %d, not hardcore", username, ironman
            )
            return stats

        storage.upsert_players(conn, [(username, ironman)])
        stats["players"] = 1

        existing = storage.fetched_checkpoint_times(conn, username, grid_times)
        missing = sorted(t for t in grid_times if t not in existing)
        if not missing:
            return stats

        last_xp = storage.latest_xp_by_skill(conn, username)
        for timestamp in missing:
            age = max(0, int(time.time() - timestamp))
            try:
                gained = api.fetch_player_gains(
                    username, age, wait=limiter.wait
                )
            except api.ApiError as error:
                log.warning(
                    "checkpoint %s for %r failed: %s", timestamp, username, error
                )
                stats["failures"] += 1
                continue
            stats["requests"] += 1
            if gained is None:
                stats["failures"] += 1
                continue

            observed = storage.as_datetime(timestamp)
            for skill in SKILLS:
                xp = xp_now[skill] - gained[skill]
                if xp < 0:
                    # Cannot happen unless the API is inconsistent; clamp.
                    xp = 0
                if last_xp.get(skill) == xp:
                    # No change since the last stored point; do not duplicate.
                    continue
                last_xp[skill] = xp
                pending.append((username, skill, observed, xp))
                stats["rows"] += 1
            done.append(timestamp)

            if len(pending) >= 5000:
                storage.insert_snapshots(conn, pending)
                storage.insert_checkpoints(conn, username, done)
                pending.clear()
                done.clear()

        return stats
    finally:
        try:
            if pending:
                storage.insert_snapshots(conn, pending)
            if done:
                storage.insert_checkpoints(conn, username, done)
        finally:
            conn.close()


def collect_all(
    dsn: str,
    players: list[str],
    grid_times: list[int],
    workers: int,
    limiter: RateLimiter,
) -> dict[str, int]:
    totals = _zero_stats()
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {
            pool.submit(collect_player, dsn, name, grid_times, limiter): name
            for name in players
        }
        for future in as_completed(futures):
            name = futures[future]
            done += 1
            try:
                stats = future.result()
            except Exception:  # noqa: BLE001 - one player must not kill the run
                log.exception("failed to collect %r", name)
                totals["failures"] += 1
                continue
            _add_stats(totals, stats)
            if done % 10 == 0 or done == len(players):
                log.info(
                    "progress %d/%d players, %d requests, %d rows, %d failures",
                    done, len(players), totals["requests"],
                    totals["rows"], totals["failures"],
                )
    return totals


def refresh_priority_players(args: argparse.Namespace) -> None:
    """Re-read the JSON list unless CLI values pinned it."""
    if getattr(args, "priority", None) is not None:
        return
    args.priority_players = parse_priority_players(None)


def collect_priority_player(
    dsn: str,
    username: str,
    timestamp: int,
    limiter: RateLimiter,
) -> dict[str, int]:
    """Fetch one 10-minute priority checkpoint and store it."""
    stats = _zero_stats()
    conn = storage.connect(dsn)
    try:
        canonical = storage.canonical_username(conn, username)
        if canonical:
            username = canonical

        try:
            fetched = api.fetch_player_xp(username, wait=limiter.wait)
            stats["requests"] += 1
        except api.ApiError as error:
            log.warning(
                "priority highscores fetch for %r failed: %s", username, error
            )
            stats["failures"] += 1
            return stats

        if fetched is None:
            log.warning("no highscores entry for priority player %r", username)
            stats["failures"] += 1
            return stats

        ironman, xp_now = fetched
        if ironman != HCIM:
            log.info(
                "skipping priority %r: ironman status is %d, not hardcore",
                username, ironman,
            )
            return stats

        storage.upsert_players(conn, [(username, ironman)])
        stats["players"] = 1

        age = max(0, int(time.time() - timestamp))
        try:
            gained = api.fetch_player_gains(
                username, age, wait=limiter.wait
            )
        except api.ApiError as error:
            log.warning(
                "priority checkpoint %s for %r failed: %s",
                timestamp, username, error,
            )
            stats["failures"] += 1
            return stats
        stats["requests"] += 1
        if gained is None:
            stats["failures"] += 1
            return stats

        observed = storage.as_datetime(timestamp)
        last_xp = storage.latest_priority_xp_by_skill(conn, username)
        rows: list[tuple] = []
        for skill in SKILLS:
            xp = xp_now[skill] - gained[skill]
            if xp < 0:
                xp = 0
            if last_xp.get(skill) == xp:
                continue
            rows.append((username, skill, observed, xp))
        if rows:
            storage.insert_priority_snapshots(conn, rows)
        storage.insert_priority_checkpoints(conn, username, [timestamp])
        stats["rows"] = len(rows)
        return stats
    finally:
        conn.close()


def collect_priority_once(
    args: argparse.Namespace, dsn: str, limiter: RateLimiter
) -> dict[str, int]:
    """Fetch the current 10-minute slot for every priority player, then prune."""
    totals = _zero_stats()

    now = int(time.time())
    timestamp = (
        now // DEFAULT_PRIORITY_INTERVAL_SECONDS
    ) * DEFAULT_PRIORITY_INTERVAL_SECONDS

    conn = storage.connect(dsn)
    try:
        storage.ensure_schema(conn)
        storage.prune_priority_data(
            conn, now - DEFAULT_PRIORITY_RETENTION_SECONDS
        )
        names = list(
            tracked_priority_names(
                conn, getattr(args, "priority_players", ())
            )
        )
    finally:
        conn.close()

    if not names:
        return totals

    for name in names:
        try:
            stats = collect_priority_player(
                dsn, name, timestamp, limiter
            )
        except Exception:  # noqa: BLE001 - one player must not kill the run
            log.exception("failed to collect priority player %r", name)
            totals["failures"] += 1
            continue
        _add_stats(totals, stats)

    log.info(
        "priority collection: %d players, %d requests, %d rows, %d failures",
        totals["players"], totals["requests"], totals["rows"],
        totals["failures"],
    )
    return totals


def _priority_loop(
    args: argparse.Namespace,
    dsn: str,
    limiter: RateLimiter,
    stop: threading.Event,
) -> None:
    while not stop.is_set():
        refresh_priority_players(args)
        try:
            collect_priority_once(args, dsn, limiter)
        except Exception:  # noqa: BLE001 - keep the loop alive
            log.exception("priority collection failed")
        now = time.time()
        next_slot = _next_slot(
            now, DEFAULT_PRIORITY_INTERVAL_SECONDS, 5
        )
        stop.wait(max(1.0, next_slot - now))


def start_priority_thread(
    args: argparse.Namespace, dsn: str, limiter: RateLimiter
) -> threading.Event:
    """Prune and collect priority tracking players every 10 minutes."""
    stop = threading.Event()
    thread = threading.Thread(
        target=_priority_loop,
        args=(args, dsn, limiter, stop),
        name="vscape_hcim_tracker-priority",
        daemon=True,
    )
    thread.start()
    return stop


def _next_slot(now: float, interval: int, grace: int) -> float:
    """Next timestamp on the interval grid, plus a grace period."""
    return (int(now) // interval + 1) * interval + grace


def _remove_old_reports(report_dir: Path) -> None:
    """Only latest.txt and latest.html are kept."""
    for pattern in (
        "report_*.txt", "report_*.html", ".latest.*.tmp", "players/.*.tmp",
    ):
        for path in report_dir.glob(pattern):
            path.unlink(missing_ok=True)


_PLAYER_WATERMARKS: dict[Path, dict[str, tuple[int, object]]] = {}
_PLAYER_PRIORITY_STATE: dict[Path, frozenset[str]] = {}


def tracked_priority_names(
    conn, players: tuple[str, ...] | list[str]
) -> tuple[str, ...]:
    """Top players by overall xp plus the manual list, in stored spelling."""
    names: list[str] = []
    candidates = list(
        storage.top_players_by_xp(conn, "overall", DEFAULT_PRIORITY_TOP)
    )
    candidates.extend(players)
    for username in candidates:
        canonical = storage.canonical_username(conn, username) or username
        if canonical not in names:
            names.append(canonical)
    return tuple(names)


def priority_index_names(
    conn, players: tuple[str, ...] | list[str]
) -> tuple[str, ...]:
    """Tracked players plus anyone still holding unpruned priority rows."""
    names = list(tracked_priority_names(conn, players))
    for username in storage.priority_snapshot_players(conn):
        if username not in names:
            names.append(username)
    return tuple(names)


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
        html_path = report_dir / "latest.html"
        _atomic_write(text_path, text)
        _atomic_write(html_path, page)
        _remove_old_reports(report_dir)

        key = report_dir.resolve()
        priority_names = tracked_priority_names(conn, priority_players)
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


def run_once(
    args: argparse.Namespace, dsn: str, limiter: RateLimiter | None = None
) -> None:
    interval = args.interval
    collect_interval = args.collect_interval
    lookback = args.lookback
    limiter = limiter or RateLimiter(args.rate)
    conn = storage.connect(dsn)
    try:
        storage.ensure_schema(conn)

        now = int(time.time())
        grid_times = build_grid(
            now, interval, collect_interval, lookback,
            args.retry_window, args.no_backfill,
        )

        if args.report_only:
            row = conn.execute("SELECT count(*) FROM players").fetchone()
            player_count = int(row[0])
            names: list[str] = []
        else:
            discovered = api.list_hcim_players(wait=limiter.wait)
            names = list(discovered)
            if args.limit:
                names = names[: args.limit]
            # Players are inserted only after their account type is verified.
            player_count = len(names)

        run_id = storage.start_run(
            conn, interval, collect_interval, lookback, player_count
        )
        log.info(
            "run %d: %d candidates, %d checkpoints, backfill %ds, "
            "collect %ds, lookback %ds",
            run_id, player_count, len(grid_times), interval,
            collect_interval, lookback,
        )

        totals = _zero_stats()
        status = "error"
        try:
            if not args.report_only:
                totals = collect_all(
                    dsn, names, grid_times, args.workers, limiter
                )
                player_count = totals["players"]
                log.info(
                    "collection finished: %d players, %d requests, "
                    "%d rows, %d failures",
                    totals["players"], totals["requests"],
                    totals["rows"], totals["failures"],
                )
            else:
                log.info("report-only mode: skipping collection")
            status = "ok" if totals["failures"] == 0 else "partial"

            if not args.collect_only:
                now = int(time.time())
                write_report(
                    conn, Path(args.report_dir), now, interval, lookback,
                    args.top, player_count, collect_interval,
                    coverage_total=len(names) if names else None,
                    priority_players=getattr(args, "priority_players", ()),
                    base_url=getattr(args, "base_url", DEFAULT_BASE_URL),
                )
        finally:
            storage.finish_run(
                conn, run_id, totals["requests"], totals["failures"],
                status, player_count,
            )
    finally:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vscape_hcim_tracker",
        description=(
            "Store hardcore ironman xp history in PostgreSQL and write "
            "plain-text and HTML ranking reports."
        ),
    )
    parser.add_argument(
        "--dsn",
        default=build_dsn(),
        help=(
            "PostgreSQL connection string "
            "(default: $VSCAPE_HCIM_TRACKER_DSN or PGHOST/PGUSER/PGPASSWORD)"
        ),
    )
    parser.add_argument(
        "--interval", type=parse_duration, default=DEFAULT_INTERVAL_SECONDS,
        help="backfill sampling step, e.g. 24h (default: 24h)",
    )
    parser.add_argument(
        "--collect-interval", type=parse_duration,
        default=DEFAULT_COLLECT_INTERVAL_SECONDS,
        help="cadence for new checkpoints in --loop mode, e.g. 6h (default: 6h)",
    )
    parser.add_argument(
        "--retry-window", type=parse_duration,
        default=DEFAULT_RETRY_WINDOW_SECONDS,
        help=(
            "recent checkpoints always re-checked, so failed fetches are "
            "retried (default: 7d)"
        ),
    )
    parser.add_argument(
        "--lookback", type=parse_duration, default=DEFAULT_LOOKBACK_SECONDS,
        help="how far back to reconstruct, e.g. 365d (default: 365d)",
    )
    parser.add_argument(
        "--workers", type=int, default=DEFAULT_WORKERS,
        help=f"concurrent player collectors (default: {DEFAULT_WORKERS})",
    )
    parser.add_argument(
        "--rate", type=float, default=DEFAULT_RATE,
        help=(
            "global HTTP requests per second, retries included, "
            f"0 = unlimited (default: {DEFAULT_RATE:g})"
        ),
    )
    parser.add_argument(
        "--no-backfill", action="store_true",
        help=(
            "only collect the recent retry window instead of reconstructing "
            "the full history"
        ),
    )
    parser.add_argument(
        "--report-only", action="store_true",
        help="rebuild the report from the database without fetching",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--collect-only", action="store_true",
        help="fetch into the database only; never write or serve reports",
    )
    mode.add_argument(
        "--serve-only", action="store_true",
        help="generate and serve reports from existing data; never fetch",
    )
    parser.add_argument(
        "--report-dir", default="reports",
        help="directory for the text and HTML reports (default: reports)",
    )
    parser.add_argument(
        "--report-interval", type=parse_duration,
        default=DEFAULT_REPORT_INTERVAL_SECONDS,
        help=(
            "regenerate the reports on this timer, independent of collection, "
            "0 = off (default: 1m)"
        ),
    )
    parser.add_argument(
        "--top", type=int, default=DEFAULT_TOP_N,
        help="entries per ranking (default: 5)",
    )
    parser.add_argument(
        "--priority", action="append", default=None,
        help=(
            "priority player username; repeat or comma-separate "
            "(default: $VSCAPE_HCIM_TRACKER_PRIORITY_PLAYERS)"
        ),
    )
    parser.add_argument(
        "--base-url", default=DEFAULT_BASE_URL,
        help=(
            "public URL used in RSS links "
            f"(default: {DEFAULT_BASE_URL})"
        ),
    )
    parser.add_argument(
        "--loop", action="store_true",
        help="run forever, collecting a new checkpoint every --collect-interval",
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="only process the first N players (testing)",
    )
    parser.add_argument(
        "--serve-port", type=int, default=0,
        help="serve the report directory on this port, 0 = off (default: 0)",
    )
    parser.add_argument(
        "--serve-host", default="127.0.0.1",
        help="interface for --serve-port (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser


def write_current_report(args: argparse.Namespace, dsn: str) -> None:
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
        write_report(
            conn, Path(args.report_dir), int(time.time()), args.interval,
            args.lookback, args.top, verified, args.collect_interval,
            coverage_total=candidates,
            priority_players=getattr(args, "priority_players", ()),
            base_url=getattr(args, "base_url", DEFAULT_BASE_URL),
        )
    finally:
        conn.close()


def _report_loop(
    args: argparse.Namespace, dsn: str, stop: threading.Event
) -> None:
    while not stop.wait(args.report_interval):
        refresh_priority_players(args)
        try:
            write_current_report(args, dsn)
        except Exception:  # noqa: BLE001 - keep the loop alive
            log.exception("periodic report failed")


def start_report_thread(
    args: argparse.Namespace, dsn: str
) -> threading.Event | None:
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


def _serve_forever(args: argparse.Namespace, dsn: str) -> int:
    """Report-only process: render on a timer and serve the files forever."""
    if not args.serve_port:
        log.error("--serve-only requires --serve-port")
        return 2

    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    _remove_old_reports(report_dir)

    try:
        write_current_report(args, dsn)
    except Exception:  # noqa: BLE001 - serving must still start
        log.exception("could not write the initial report")

    try:
        server = web.start_server(report_dir, args.serve_port, args.serve_host)
    except OSError as error:
        log.error(
            "cannot serve reports on %s:%d: %s",
            args.serve_host, args.serve_port, error,
        )
        return 2
    log.info(
        "serving reports on http://%s:%d/", args.serve_host, server.server_port
    )

    start_report_thread(args, dsn)
    if args.report_interval > 0:
        log.info("regenerating reports every %ds", args.report_interval)

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if not args.dsn:
        log.error(
            "no database configured: pass --dsn or set "
            "VSCAPE_HCIM_TRACKER_DSN / PGHOST"
        )
        return 2

    args.priority_players = parse_priority_players(args.priority)
    args.base_url = args.base_url.rstrip("/")

    if args.serve_only:
        return _serve_forever(args, args.dsn)

    if args.collect_only and args.serve_port:
        log.error("--collect-only cannot serve reports; use --serve-only")
        return 2

    report_dir = Path(args.report_dir)
    if not args.collect_only:
        report_dir.mkdir(parents=True, exist_ok=True)
        _remove_old_reports(report_dir)

    if args.serve_port:
        try:
            write_current_report(args, args.dsn)
        except Exception:  # noqa: BLE001 - serving must still start
            log.exception("could not write the initial report")
        try:
            server = web.start_server(
                report_dir, args.serve_port, args.serve_host
            )
        except OSError as error:
            log.error(
                "cannot serve reports on %s:%d: %s",
                args.serve_host, args.serve_port, error,
            )
            return 2
        log.info(
            "serving reports on http://%s:%d/",
            args.serve_host, server.server_port,
        )

    if not args.collect_only:
        start_report_thread(args, args.dsn)
        if args.report_interval > 0:
            log.info(
                "regenerating reports every %ds", args.report_interval
            )

    limiter = RateLimiter(args.rate)
    if args.loop and not args.report_only:
        start_priority_thread(args, args.dsn, limiter)

    while True:
        refresh_priority_players(args)
        try:
            run_once(args, args.dsn, limiter)
        except Exception:  # noqa: BLE001
            log.exception("run failed")
            if not args.loop:
                return 1
        if not args.loop:
            return 0

        now = time.time()
        next_run = _next_slot(now, args.collect_interval, 60)
        delay = max(1.0, next_run - now)
        log.info("sleeping %.0fs until the next checkpoint", delay)
        time.sleep(delay)


if __name__ == "__main__":
    sys.exit(main())
