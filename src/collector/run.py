"""One collection run: discover players and fill the checkpoint grid."""

from __future__ import annotations

import logging
import time

from ..common import storage
from ..common.utility import DEFAULT_RETRY_WINDOW_SECONDS, effective_lookback
from . import api
from .collection import collect_all, zero_stats
from .grid import build_grid
from .rate import RateLimiter

log = logging.getLogger("vscape_tracker.collector.run")


def run_once(
    args, dsn: str, limiter: RateLimiter | None = None
) -> None:
    interval = args.interval
    collect_interval = args.collect_interval
    now = int(time.time())
    lookback = effective_lookback(now, args.lookback)
    limiter = limiter or RateLimiter(args.rate)
    conn = storage.connect(dsn)
    try:
        grid_times = build_grid(
            now, interval, collect_interval, lookback,
            getattr(args, "retry_window", DEFAULT_RETRY_WINDOW_SECONDS),
            args.no_backfill,
        )

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

        totals = zero_stats()
        status = "error"
        try:
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
            status = "ok" if totals["failures"] == 0 else "partial"
        finally:
            storage.finish_run(
                conn, run_id, totals["requests"], totals["failures"],
                status, player_count,
            )
    finally:
        conn.close()
