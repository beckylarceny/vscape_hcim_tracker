"""Tests for the static report server."""

from __future__ import annotations

import tempfile
import unittest
import urllib.request
from pathlib import Path

from src.web import server as web


class ServerTest(unittest.TestCase):
    def test_root_serves_general_html(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "general.html").write_text(
                "<h1>hello</h1>", encoding="utf-8"
            )
            server = web.start_server(Path(directory), 0)
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            url = f"http://127.0.0.1:{server.server_port}/"
            with urllib.request.urlopen(url, timeout=10) as response:
                body = response.read().decode("utf-8")
                self.assertEqual(response.status, 200)
            self.assertIn("hello", body)


if __name__ == "__main__":
    unittest.main()
