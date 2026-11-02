"""Command-line interface for the collector service."""

from __future__ import annotations

import argparse

from ..common.utility import (
    DEFAULT_COLLECT_INTERVAL_SECONDS,
    DEFAULT_INTERVAL_SECONDS,
    DEFAULT_RATE,
    DEFAULT_RETRY_WINDOW_SECONDS,
    DEFAULT_WORKERS,
    build_dsn,
    parse_duration,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vscape_hcim_tracker.collector",
        description=(
            "Collect hardcore ironman xp history from the highscores API "
            "into PostgreSQL."
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
        "--lookback", type=parse_duration, default=None,
        help=(
            "how far back to reconstruct, e.g. 365d "
            "(default: everything since 2025-01-26)"
        ),
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
        "--priority", action="append", default=None,
        help=(
            "priority player username; repeat or comma-separate "
            "(default: $VSCAPE_HCIM_TRACKER_PRIORITY_PLAYERS)"
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
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser
