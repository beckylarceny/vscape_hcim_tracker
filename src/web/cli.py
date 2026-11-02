"""Command-line interface for the web service."""

from __future__ import annotations

import argparse

from ..common.cli import add_common_arguments
from ..common.utility import (
    DEFAULT_BASE_URL,
    DEFAULT_REPORT_INTERVAL_SECONDS,
    DEFAULT_TOP_N,
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
    add_common_arguments(parser)
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
    return parser
