"""Collector service entry point."""

from __future__ import annotations

import logging
import time

from ..common.priority import refresh_priority_players
from ..common.utility import parse_priority_players
from .cli import build_parser
from .priority import start_priority_thread
from .rate import RateLimiter
from .run import run_once
from .schedule import next_slot

log = logging.getLogger("vscape_hcim_tracker.collector")


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

    limiter = RateLimiter(args.rate)
    if args.loop:
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
        next_run = next_slot(now, args.collect_interval, 60)
        delay = max(1.0, next_run - now)
        log.info("sleeping %.0fs until the next checkpoint", delay)
        time.sleep(delay)


if __name__ == "__main__":
    raise SystemExit(main())
