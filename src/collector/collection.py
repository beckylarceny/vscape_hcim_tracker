"""Fetch xp history from the highscores API and store it."""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..common import storage
from ..common.utility import SKILLS, TRACKED_ACCOUNT_CODES
from . import api
from .rate import RateLimiter

log = logging.getLogger("vscape_tracker.collector")


def zero_stats() -> dict[str, int]:
    return {"requests": 0, "rows": 0, "failures": 0, "players": 0}


def add_stats(totals: dict[str, int], stats: dict[str, int]) -> None:
    for key in totals:
        totals[key] += stats[key]


def _fetch_current(name: str, limiter: RateLimiter):
    """Fetch one player's current xp; raises ApiError."""
    fetched = api.fetch_player_xp(name, wait=limiter.wait)
    if fetched is None:
        return None
    ironman, xp_now = fetched
    return name, ironman, xp_now


def _fetch_gains(name: str, timestamp: int, limiter: RateLimiter):
    age = max(0, int(time.time() - timestamp))
    return name, api.fetch_player_gains(name, age, wait=limiter.wait)


def collect_all(
    dsn: str,
    players: list[str],
    grid_times: list[int],
    workers: int,
    limiter: RateLimiter,
) -> dict[str, int]:
    """Backfill interval by interval, newest timestamp first.

    Every player is fetched for one timestamp before moving to the previous
    interval, so the database fills backwards in time and the newest coverage
    appears first.
    """
    totals = zero_stats()
    conn = storage.connect(dsn)
    try:
        current: dict[str, dict[str, int]] = {}
        seen: list[tuple[str, int]] = []
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {
                pool.submit(_fetch_current, name, limiter): name
                for name in players
            }
            for future in as_completed(futures):
                name = futures[future]
                totals["requests"] += 1
                try:
                    result = future.result()
                except Exception as error:  # noqa: BLE001
                    log.warning(
                        "highscores fetch for %r failed: %s", name, error
                    )
                    totals["failures"] += 1
                    continue
                if result is None:
                    log.warning("no highscores entry for %r", name)
                    totals["failures"] += 1
                    continue
                _, ironman, xp_now = result
                seen.append((name, ironman))
                if ironman not in TRACKED_ACCOUNT_CODES:
                    log.info(
                        "skipping %r: account type %d is not tracked",
                        name, ironman,
                    )
                    continue
                current[name] = xp_now

        if seen:
            storage.upsert_players(conn, sorted(seen))
        totals["players"] = len(current)
        if not current:
            return totals

        done = storage.checkpoint_times_by_player(conn)
        last_xp = storage.latest_xp_by_skill_many(conn, list(current))

        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            for index, timestamp in enumerate(grid_times, start=1):
                todo = [
                    name for name in current
                    if timestamp not in done.get(name, ())
                ]
                if not todo:
                    continue

                gained_by_player: dict[str, dict[str, int]] = {}
                futures = {
                    pool.submit(_fetch_gains, name, timestamp, limiter): name
                    for name in todo
                }
                for future in as_completed(futures):
                    name = futures[future]
                    totals["requests"] += 1
                    try:
                        _, gained = future.result()
                    except Exception as error:  # noqa: BLE001
                        log.warning(
                            "checkpoint %s for %r failed: %s",
                            timestamp, name, error,
                        )
                        totals["failures"] += 1
                        continue
                    if gained is None:
                        totals["failures"] += 1
                        continue
                    gained_by_player[name] = gained

                if not gained_by_player:
                    continue

                observed = storage.as_datetime(timestamp)
                rows: list[tuple] = []
                pairs: list[tuple[str, int]] = []
                for name, gained in gained_by_player.items():
                    xp_now = current[name]
                    previous = last_xp.setdefault(name, {})
                    for skill in SKILLS:
                        xp = max(0, xp_now[skill] - gained[skill])
                        if previous.get(skill) == xp:
                            # No change since the newer stored point.
                            continue
                        previous[skill] = xp
                        rows.append((name, skill, observed, xp))
                    pairs.append((name, timestamp))

                if rows:
                    storage.insert_snapshots(conn, rows)
                storage.insert_checkpoint_pairs(conn, pairs)
                totals["rows"] += len(rows)
                log.info(
                    "backfill %d/%d: %s, %d players, %d rows",
                    index, len(grid_times),
                    observed.strftime("%Y-%m-%d %H:%M"),
                    len(pairs), len(rows),
                )
        return totals
    finally:
        conn.close()
