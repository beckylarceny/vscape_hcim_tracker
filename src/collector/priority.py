"""High-frequency collection for priority players."""

from __future__ import annotations

import logging
import threading
import time

from ..common import storage
from ..common.priority import refresh_priority_players, tracked_priority_names
from ..common.utility import (
    DEFAULT_PRIORITY_INTERVAL_SECONDS,
    DEFAULT_PRIORITY_RETENTION_SECONDS,
    SKILLS,
    TRACKED_ACCOUNT_CODES,
)
from . import api
from .collection import add_stats, zero_stats
from .rate import RateLimiter
from ..common.utility import next_slot

log = logging.getLogger("vscape_tracker.collector.priority")


def collect_priority_player(
    dsn: str,
    username: str,
    timestamp: int,
    limiter: RateLimiter,
) -> dict[str, int]:
    """Fetch one 10-minute priority checkpoint and store it."""
    stats = zero_stats()
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
        storage.upsert_players(conn, [(username, ironman)])
        if ironman not in TRACKED_ACCOUNT_CODES:
            log.info(
                "skipping priority %r: account type %d is not tracked",
                username, ironman,
            )
            return stats

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
    args, dsn: str, limiter: RateLimiter
) -> dict[str, int]:
    """Fetch the current 10-minute slot for every priority player, then prune."""
    totals = zero_stats()

    now = int(time.time())
    timestamp = (
        now // DEFAULT_PRIORITY_INTERVAL_SECONDS
    ) * DEFAULT_PRIORITY_INTERVAL_SECONDS

    conn = storage.connect(dsn)
    try:
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
        add_stats(totals, stats)

    log.info(
        "priority collection: %d players, %d requests, %d rows, %d failures",
        totals["players"], totals["requests"], totals["rows"],
        totals["failures"],
    )
    return totals


def _priority_loop(
    args,
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
        next_run = next_slot(
            now, DEFAULT_PRIORITY_INTERVAL_SECONDS, 5
        )
        stop.wait(max(1.0, next_run - now))


def start_priority_thread(
    args, dsn: str, limiter: RateLimiter
) -> threading.Event:
    """Prune and collect priority tracking players every 10 minutes."""
    stop = threading.Event()
    thread = threading.Thread(
        target=_priority_loop,
        args=(args, dsn, limiter, stop),
        name="vscape_tracker-priority",
        daemon=True,
    )
    thread.start()
    return stop
