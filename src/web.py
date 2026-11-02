"""Minimal static HTTP server for the generated HTML report."""

from __future__ import annotations

import functools
import http.server
import logging
import threading
from pathlib import Path

log = logging.getLogger("vscape_hcim_tracker.web")


class ReportRequestHandler(http.server.SimpleHTTPRequestHandler):
    """Serve the report directory, mapping / to the general HTML page."""

    def _redirect_root(self) -> None:
        if self.path in ("", "/", "/general", "/general/"):
            self.path = "/general.html"

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self._redirect_root()
        super().do_GET()

    def do_HEAD(self) -> None:  # noqa: N802 - http.server API
        self._redirect_root()
        super().do_HEAD()

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        log.debug("%s - %s", self.address_string(), format % args)


def start_server(
    directory: Path, port: int, host: str = "127.0.0.1"
) -> http.server.ThreadingHTTPServer:
    """Start serving `directory` in a daemon thread and return the server."""
    directory.mkdir(parents=True, exist_ok=True)
    handler = functools.partial(ReportRequestHandler, directory=str(directory))
    server = http.server.ThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(
        target=server.serve_forever, name="vscape_hcim_tracker-http", daemon=True
    )
    thread.start()
    return server
