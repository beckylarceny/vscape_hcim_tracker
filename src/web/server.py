"""Minimal static HTTP server for the generated HTML report."""

from __future__ import annotations

import functools
import http.server
import logging
import threading
from pathlib import Path

log = logging.getLogger("vscape_hcim_tracker.web.server")


class ReportRequestHandler(http.server.SimpleHTTPRequestHandler):
    """Serve report pages with or without the .html suffix."""

    def _redirect_page_alias(self) -> bool:
        """Send /page/ to /page so relative report links stay correct."""
        base, _, query = self.path.partition("?")
        if base in ("", "/") or not base.endswith("/"):
            return False
        stem = base.rstrip("/")
        page = super().translate_path(stem + ".html")
        if not Path(page).is_file():
            return False
        location = stem + (f"?{query}" if query else "")
        self.send_response(301)
        self.send_header("Location", location)
        self.end_headers()
        return True

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        if self._redirect_page_alias():
            return
        super().do_GET()

    def do_HEAD(self) -> None:  # noqa: N802 - http.server API
        if self._redirect_page_alias():
            return
        super().do_HEAD()

    def translate_path(self, path: str) -> str:
        """Map clean URLs to generated HTML files."""
        base, _, _ = path.partition("?")
        if base in ("", "/"):
            base = "/index.html"
        elif base.endswith("/"):
            index = super().translate_path(base + "index.html")
            if Path(index).is_file():
                return index
            page = super().translate_path(base.rstrip("/") + ".html")
            if Path(page).is_file():
                return page
        else:
            candidate = super().translate_path(base + ".html")
            if Path(candidate).is_file():
                return candidate
        return super().translate_path(base)

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
