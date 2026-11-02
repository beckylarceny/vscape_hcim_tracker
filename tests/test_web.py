"""Tests for the static report server."""

from __future__ import annotations

import tempfile
import unittest
import urllib.request
from pathlib import Path

from src.web import server as web


class ServerTest(unittest.TestCase):
    def test_clean_urls_serve_generated_pages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            files = {
                "index.html": "home",
                "general.html": "general",
                "status.html": "status",
                "players/alice.html": "player",
                "priority/index.html": "priority index",
                "priority/alice.html": "priority player",
            }
            for name, body in files.items():
                path = Path(directory, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"<h1>{body}</h1>", encoding="utf-8")
            server = web.start_server(Path(directory), 0)
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            base = f"http://127.0.0.1:{server.server_port}"
            for path, expected in (
                ("/", "home"),
                ("/general", "general"),
                ("/general/", "general"),
                ("/general.html", "general"),
                ("/status", "status"),
                ("/status/", "status"),
                ("/players/alice", "player"),
                ("/players/alice/", "player"),
                ("/priority", "priority index"),
                ("/priority/", "priority index"),
                ("/priority/alice", "priority player"),
            ):
                with urllib.request.urlopen(base + path, timeout=10) as response:
                    body = response.read().decode("utf-8")
                    self.assertEqual(response.status, 200)
                self.assertIn(expected, body)


if __name__ == "__main__":
    unittest.main()
