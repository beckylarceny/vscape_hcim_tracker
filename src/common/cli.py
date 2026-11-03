"""Command-line options shared by the collector and web services."""

from __future__ import annotations

import argparse

from .utility import (
    DEFAULT_COLLECT_INTERVAL_SECONDS,
    DEFAULT_INTERVAL_SECONDS,
    DEFAULT_RETRY_WINDOW_SECONDS,
    build_dsn,
    parse_duration,
)


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--dsn",
        default=build_dsn(),
        help=(
            "PostgreSQL connection string "
            "(default: $VSCAPE_TRACKER_DSN or PGHOST/PGUSER/PGPASSWORD)"
        ),
    )
    parser.add_argument(
        "--interval", type=parse_duration, default=DEFAULT_INTERVAL_SECONDS,
        help="backfill sampling step, e.g. 24h (default: 24h)",
    )
    parser.add_argument(
        "--collect-interval", type=parse_duration,
        default=DEFAULT_COLLECT_INTERVAL_SECONDS,
        help="live collection cadence, e.g. 1h (default: 1h)",
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
            "how far back to cover, e.g. 365d "
            "(default: everything since 2025-01-26)"
        ),
    )
    parser.add_argument(
        "--priority", action="append", default=None,
        help=(
            "priority player username; repeat or comma-separate "
            "(default: $VSCAPE_TRACKER_PRIORITY_PLAYERS)"
        ),
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
