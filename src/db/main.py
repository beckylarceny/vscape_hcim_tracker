"""Create the tracker schema in PostgreSQL."""

from __future__ import annotations

import argparse
import logging

from ..common.storage import connect, ensure_schema
from ..common.utility import build_dsn

log = logging.getLogger("vscape_hcim_tracker.db")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vscape_hcim_tracker.db",
        description="Create or update the tracker database schema.",
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
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args(argv)
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

    conn = connect(args.dsn)
    try:
        ensure_schema(conn)
    finally:
        conn.close()
    log.info("database schema is ready")
    return 0
