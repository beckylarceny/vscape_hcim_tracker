"""Command-line interface for the collector service."""

from __future__ import annotations

import argparse

from ..common.cli import add_common_arguments
from ..common.utility import DEFAULT_RATE, DEFAULT_WORKERS


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vscape_tracker.collector",
        description=(
            "Collect xp history for the configured account types from the "
            "highscores API into PostgreSQL."
        ),
    )
    add_common_arguments(parser)
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
        "--loop", action="store_true",
        help="run forever, collecting a new checkpoint every --collect-interval",
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="only process the first N players (testing)",
    )
    return parser
