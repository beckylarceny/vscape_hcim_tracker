"""Web service entry point: render reports and serve them forever."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from ..common.utility import parse_priority_players
from .cli import build_parser
from .reporting import (
    _remove_old_reports,
    start_report_thread,
    write_current_report,
)
from .server import start_server

log = logging.getLogger("vscape_hcim_tracker.web")


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
    args.base_url = args.base_url.rstrip("/")

    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    _remove_old_reports(report_dir)

    try:
        write_current_report(args, args.dsn)
    except Exception:  # noqa: BLE001 - serving must still start
        log.exception("could not write the initial report")

    try:
        server = start_server(report_dir, args.serve_port, args.serve_host)
    except OSError as error:
        log.error(
            "cannot serve reports on %s:%d: %s",
            args.serve_host, args.serve_port, error,
        )
        return 2
    log.info(
        "serving reports on http://%s:%d/", args.serve_host, server.server_port
    )

    start_report_thread(args, args.dsn)
    if args.report_interval > 0:
        log.info("regenerating reports every %ds", args.report_interval)

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
