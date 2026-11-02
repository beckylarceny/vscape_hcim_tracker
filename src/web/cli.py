"""Command-line interface for the web service."""

from __future__ import annotations

import argparse

from ..common.utility import (
    DEFAULT_BASE_URL,
    DEFAULT_COLLECT_INTERVAL_SECONDS,
    DEFAULT_INTERVAL_SECONDS,
    DEFAULT_REPORT_INTERVAL_SECONDS,
    DEFAULT_RETRY_WINDOW_SECONDS,
    DEFAULT_TOP_N,
    build_dsn,
    parse_duration,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vscape_hcim_tracker.web",
        description=(
            "Render hardcore ironman xp reports from PostgreSQL and serve "
            "them over HTTP."
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
        help="backfill sampling step used in report labels (default: 24h)",
    )
    parser.add_argument(
        "--collect-interval", type=parse_duration,
        default=DEFAULT_COLLECT_INTERVAL_SECONDS,
        help="live collection cadence used in report labels (default: 6h)",
    )
    parser.add_argument(
        "--retry-window", type=parse_duration,
        default=DEFAULT_RETRY_WINDOW_SECONDS,
        help="retry window used in status labels (default: 7d)",
    )
    parser.add_argument(
        "--lookback", type=parse_duration, default=None,
        help=(
            "reporting window, e.g. 365d "
            "(default: everything since 2025-01-26)"
        ),
    )
    parser.add_argument(
        "--report-dir", default="reports",
        help="directory for the text and HTML reports (default: reports)",
    )
    parser.add_argument(
        "--report-interval", type=parse_duration,
        default=DEFAULT_REPORT_INTERVAL_SECONDS,
        help=(
            "regenerate the reports on this timer, "
            "0 = only at startup (default: 1m)"
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
        "--serve-port", type=int, default=8000,
        help="serve the report directory on this port (default: 8000)",
    )
    parser.add_argument(
        "--serve-host", default="127.0.0.1",
        help="interface to bind (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser
