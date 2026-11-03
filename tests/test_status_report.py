"""Tests for the status page (no database needed)."""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from src.common.storage import CoverageSlice
from src.web.status.docker import (
    ContainerInfo,
    DockerStatus,
    container_rows,
    container_status,
    self_project,
)
from src.web.status.html import coverage_bar, render_status_page
from src.web.status.queries import (
    collect_status,
    coverage_runs,
    elapsed,
    interval_label,
    server_version,
    size,
    until,
)
from src.web.status.report import write_status_page
from src.web.status.text import render_status_text

NOW = 1_800_000_000
DAY = 86400
COLLECT = 6 * 3600
LOOKBACK = 365 * DAY
PRIORITY = 600

DOCKER = DockerStatus((
    ContainerInfo(
        "collector", "vscape_tracker-collector-1",
        "running", "Up 40 minutes", "vscape_tracker-app:latest",
    ),
    ContainerInfo(
        "db", "vscape_tracker-db-1",
        "running", "Up 41 minutes", "postgres:17-alpine",
    ),
    ContainerInfo(
        "web", "vscape_tracker-web-1",
        "running", "Up 39 minutes", "vscape_tracker-app:latest",
    ),
))


def stamp(epoch: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc)


class FakeResult:
    def __init__(self, rows) -> None:
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class FakeConn:
    def __init__(
        self,
        *,
        fail: bool = False,
        database: str = "xp tracker",
        host: str = "db",
        server_version: str | int = 170011,
        runs: list[tuple] | None = None,
    ) -> None:
        self.fail = fail
        self.runs = runs
        self.info = SimpleNamespace(
            server_version=server_version,
            dbname=database,
            host=host,
            port=5432,
        )

    def execute(self, sql, params=None):
        if self.fail and sql.strip() == "SELECT 1":
            raise RuntimeError("connection refused")
        if "pg_databasesize" in sql:
            return FakeResult([(1_500_000_000,)])
        if "pg_postmaster_start_time" in sql:
            return FakeResult([(stamp(NOW - 10 * DAY),)])
        if "FROM pg_class" in sql:
            return FakeResult([
                ("players", 100),
                ("snapshots", 1000),
                ("checkpoints", 2000),
                ("priority_snapshots", 3000),
                ("priority_checkpoints", 4000),
                ("runs", 5),
            ])
        if "count(*) FROM players" in sql:
            return FakeResult([(101,)])
        if "count(*) FROM runs" in sql:
            return FakeResult([(7,)])
        if "max(observed_at) FROM priority_checkpoints" in sql:
            return FakeResult([(stamp(NOW - 600),)])
        if "max(observed_at) FROM checkpoints" in sql:
            return FakeResult([(stamp(NOW - 3600),)])
        if "FROM runs" in sql:
            if self.runs is not None:
                return FakeResult(self.runs)
            return FakeResult([
                (5, stamp(NOW - 7200), stamp(NOW - 3600), "ok", 120, 1, 99),
                (4, stamp(NOW - DAY), None, "running", None, 0, None),
            ])
        if "DISTINCT observed_at FROM priority_snapshots" in sql:
            return FakeResult([(5, stamp(NOW - DAY), stamp(NOW))])
        if "DISTINCT observed_at FROM priority_checkpoints" in sql:
            return FakeResult([(9, stamp(NOW - DAY), stamp(NOW))])
        if "DISTINCT observed_at FROM snapshots" in sql:
            return FakeResult([
                (False, 4, stamp(NOW - 2 * DAY), stamp(NOW - DAY)),
                (True, 2, stamp(NOW - 3 * DAY), stamp(NOW - 3 * DAY)),
            ])
        if "DISTINCT observed_at FROM checkpoints" in sql:
            return FakeResult([
                (False, 10, stamp(NOW - 2 * DAY), stamp(NOW - DAY)),
                (True, 3, stamp(NOW - 3 * DAY), stamp(NOW - 3 * DAY)),
            ])
        return FakeResult([])


def collect(conn: FakeConn | None = None, **overrides):
    kwargs = dict(
        generated="2026-01-01 00:00 UTC",
        now_epoch=NOW,
        version="software version abc1234",
        report_interval=60,
        backfill_interval=DAY,
        collect_interval=COLLECT,
        retry_window=7 * DAY,
        lookback=LOOKBACK,
        coverage=[
            CoverageSlice(NOW - 2 * DAY, 2, 48),
            CoverageSlice(NOW - DAY, 1, 4),
            CoverageSlice(NOW, 0, 0),
        ],
        player_count=2,
        coverage_total=2,
        priority_players=3,
    )
    kwargs.update(overrides)
    with mock.patch(
        "src.web.status.queries.container_status", return_value=DOCKER
    ):
        return collect_status(conn or FakeConn(), **kwargs)


class DockerTest(unittest.TestCase):
    def test_container_rows_project_and_errors(self) -> None:
        payload = [
            {
                "Id": "bbb",
                "Names": ["/vscape_tracker-web-1"],
                "State": "running",
                "Status": "Up 39 minutes",
                "Image": "app:latest",
                "Labels": {
                    "com.docker.compose.project": "vscape_tracker",
                    "com.docker.compose.service": "web",
                },
            },
            {
                "Id": "aaa",
                "Names": None,
                "State": "running",
                "Status": "Up 40 minutes",
                "Image": "app:latest",
                "Labels": {
                    "com.docker.compose.project": "vscape_tracker",
                    "com.docker.compose.service": "collector",
                },
            },
            {
                "Id": "ccc",
                "Names": ["/other-1"],
                "State": "running",
                "Status": "Up 1 hour",
                "Image": "other",
                "Labels": {
                    "com.docker.compose.project": "other",
                    "com.docker.compose.service": "other",
                },
            },
        ]
        self.assertEqual(
            container_rows(payload, "vscape_tracker"),
            (
                ContainerInfo(
                    "collector", "aaa", "running", "Up 40 minutes",
                    "app:latest",
                ),
                ContainerInfo(
                    "web", "vscape_tracker-web-1", "running",
                    "Up 39 minutes", "app:latest",
                ),
            ),
        )
        self.assertEqual(container_rows("not a list", "vscape_tracker"), ())

        payload = [
            {
                "Id": "abc123ffff",
                "Labels": {"com.docker.compose.project": "mine"},
            },
            {
                "Id": "def456ffff",
                "Labels": {"com.docker.compose.project": "other"},
            },
        ]
        with mock.patch(
            "src.web.status.docker.socket.gethostname", return_value="abc123"
        ):
            self.assertEqual(self_project(payload), "mine")
        with mock.patch(
            "src.web.status.docker.socket.gethostname", return_value="zzz"
        ):
            self.assertIsNone(self_project(payload))
        self.assertIsNone(self_project("not a list"))

        with mock.patch(
            "src.web.status.docker._docker_get",
            side_effect=OSError("no socket"),
        ):
            self.assertEqual(
                container_status(), DockerStatus((), "no socket")
            )


class CoverageTest(unittest.TestCase):
    def test_bar_runs_tooltips_and_zero_segments(self) -> None:
        data = collect()
        page = render_status_page(data)
        bar = coverage_bar(data)
        self.assertIn('class="coverage"', bar)
        self.assertIn("(1 day): 1/2 players (50%), 6-hour checkpoints", bar)
        self.assertIn('data-tip="', bar)
        self.assertIn('class="zero"', bar)
        self.assertIn('class="density-hourly"', bar)
        self.assertIn('class="coverage-legend"', bar)
        self.assertIn("repeating-linear-gradient(45deg", page)
        self.assertNotIn("repeating-linear-gradient(90deg", page)
        self.assertIn("min-width: 1em", page)
        self.assertIn('id="coverage-tip"', page)
        self.assertIn("(hover: none)", page)
        self.assertNotIn('<span title="', page)

        empty = coverage_bar(collect(coverage=[]))
        self.assertIn('<span class="zero"></span>', empty)

        self.assertEqual(
            coverage_runs([
                CoverageSlice(0, 5, 120),
                CoverageSlice(86400, 5, 20),
                CoverageSlice(172800, 2, 2),
                CoverageSlice(259200, 2, 2),
                CoverageSlice(345600, 0, 0),
            ]),
            [
                (0, 1, 5, "hourly"),
                (1, 1, 5, "six-hour"),
                (2, 2, 2, "daily"),
                (4, 1, 0, "none"),
            ],
        )
        self.assertEqual(
            coverage_runs([
                CoverageSlice(0, 1, 4),
                CoverageSlice(86400, 2, 2),
                CoverageSlice(172800, 1, 1),
            ]),
            [
                (0, 1, 1, "six-hour"),
                (1, 1, 2, "daily"),
                (2, 1, 1, "daily"),
            ],
        )

    def test_density_accounts_for_bucket_width(self) -> None:
        # A bucket wider than a day holds two or three daily checkpoints.
        # It must not fall below the daily class when it holds only two.
        wide = 186048  # 2.15 days, from a 646-day lookback in 300 buckets
        self.assertEqual(
            coverage_runs([
                CoverageSlice(0, 50, 100),
                CoverageSlice(wide, 50, 150),
                CoverageSlice(2 * wide, 50, 50),
            ], bucket_seconds=wide),
            [
                (0, 2, 50, "daily"),
                (2, 1, 50, "sparse"),
            ],
        )
        # Hourly and six-hour cadences keep their class in a wide bucket.
        self.assertEqual(
            coverage_runs([
                CoverageSlice(0, 10, 520),
                CoverageSlice(wide, 10, 80),
            ], bucket_seconds=wide),
            [
                (0, 1, 10, "hourly"),
                (1, 1, 10, "six-hour"),
            ],
        )


class FormatTest(unittest.TestCase):
    def test_format_helpers(self) -> None:
        self.assertEqual(elapsed(2 * DAY + 4 * 3600), "2 days 4 hours")
        self.assertEqual(elapsed(3 * 3600 + 12 * 60), "3 hours 12 minutes")
        self.assertEqual(elapsed(30), "less than a minute")
        self.assertEqual(until(12 * 60), "in 12 minutes")
        self.assertEqual(until(10), "less than a minute")
        self.assertEqual(size(2 * 1024 ** 3), "2.0 GB")
        self.assertEqual(size(1500), "1.5 KB")
        self.assertEqual(size(None), "unknown")

        self.assertEqual(interval_label(60), "1 minute")
        self.assertEqual(interval_label(3600), "1 hour")
        self.assertEqual(interval_label(600), "10 minutes")
        self.assertEqual(interval_label(21600), "6 hours")
        self.assertEqual(interval_label(86400), "24 hours")

        self.assertEqual(server_version(170011), "17.0.11")
        self.assertEqual(server_version(160004), "16.0.4")
        self.assertEqual(server_version("170011"), "17.0.11")
        self.assertEqual(server_version("17.11"), "17.11")
        self.assertIsNone(server_version(None))


class PageTest(unittest.TestCase):
    def test_sections_rows_and_backlinks(self) -> None:
        page = render_status_page(collect())
        for heading in (
            "data coverage", "service", "database", "backfill", "live",
            "priority", "players", "snapshots", "recent runs",
        ):
            self.assertIn(f"<h2>{heading}</h2>", page)
        self.assertLess(
            page.index("<h2>data coverage</h2>"),
            page.index("<h2>service</h2>"),
        )
        self.assertLess(
            page.index('class="coverage"'), page.index("<h2>service</h2>")
        )
        for label in (
            "players logged", "backfill interval", "live interval",
            "priority interval", "lookback interval",
        ):
            self.assertIn(label, page)
        self.assertIn("generated at: 2026-01-01 00:00 UTC", page)
        self.assertIn("connected,", page)
        self.assertIn("database up since", page)
        self.assertIn("(10 days)", page)
        self.assertIn("newest priority checkpoint", page)
        self.assertIn("10 minutes ago", page)
        self.assertIn("every 1 minute", page)
        self.assertIn('<th class="num">rows</th>', page)
        self.assertNotIn("estimated rows", page)
        self.assertIn(
            "players and runs are counted exactly; snapshot and checkpoint "
            "tables use the planner estimate.",
            page,
        )
        self.assertIn("<td>17.0.11</td>", page)
        self.assertIn("snapshots store only changed xp", page)
        self.assertNotIn("pid ", page)
        self.assertNotIn("<th>user</th>", page)
        self.assertIn("python ", page)
        self.assertIn("class=\"status\"", page)

        page = render_status_page(collect())
        backfill = page.index("<h2>backfill</h2>")
        live = page.index("<h2>live</h2>")
        priority = page.index("<h2>priority</h2>")
        players = page.index("<h2>players</h2>")
        self.assertLess(backfill, page.index("backfill interval"))
        self.assertLess(page.index("last run ended"), live)
        self.assertLess(live, page.index("next live fetch"))
        self.assertLess(page.index("next live fetch"), priority)
        self.assertLess(priority, page.index("next priority fetch"))
        self.assertLess(page.index("next priority fetch"), players)

        page = render_status_page(collect())
        backlinks = (
            '<div class="backlinks"><p class="sub">'
            '<a href="general">general tracking</a> · '
            '<a href="priority/">priority tracking</a></p></div>'
        )
        self.assertIn(backlinks, page)
        self.assertLess(page.index('<div class="backlinks">'), page.index("<h1>"))

        text_version = '<p class="sub"><a href="status.txt">text version</a></p>'
        self.assertIn(text_version, page)
        self.assertLess(page.index("<h1>"), page.index(text_version))
        self.assertLess(page.index(text_version), page.index("generated at"))

        self.assertNotIn("text report", page)
        self.assertNotIn('href="latest.txt"', page)
        self.assertNotIn(
            "</h1>\n<p class=\"sub\"><a href=\"general\">", page
        )
        self.assertIn(
            ".backlinks {\n"
            "    margin: -2rem 0 1.75rem;\n"
            "    text-align: center;\n"
            "}",
            page,
        )
        self.assertIn(
            ".backlinks .sub {\n"
            "    margin: 0;\n"
            "    padding: 0;\n"
            "    border: 0;\n"
            "    font-size: .9rem;\n"
            "}",
            page,
        )


    def test_players_and_timers(self) -> None:
        page = render_status_page(collect(coverage_total=4))
        self.assertIn("2 / 4", page)

        page = render_status_page(collect(report_interval=0))
        self.assertIn("<td>not on a timer</td>", page)
        self.assertNotIn("every 1 minute", page)

        negative = render_status_page(collect(report_interval=-60))
        self.assertIn("<td>not on a timer</td>", negative)

        timed = render_status_page(collect(report_interval=3600))
        self.assertIn("<td>every 1 hour</td>", timed)

        data = collect()
        self.assertEqual(
            data.next_live_epoch,
            (NOW // COLLECT + 1) * COLLECT + 60,
        )
        self.assertEqual(
            data.next_priority_epoch,
            (NOW // PRIORITY + 1) * PRIORITY + 5,
        )


    def test_database_text_and_degrade(self) -> None:
        conn = FakeConn(
            database="<script>alert(1)</script>",
            host="<host>",
            server_version="<ver>",
        )
        page = render_status_page(collect(conn))
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;host&gt;:5432", page)

        page = render_status_page(collect(FakeConn(fail=True)))
        self.assertIn("unreachable: connection refused", page)
        self.assertIn("unknown", page)


    def test_last_run_and_runs_table(self) -> None:
        finished = FakeConn(runs=[
            (5, stamp(NOW - 1500), stamp(NOW - 21 * 60), "ok", 120, 1, 99),
        ])
        page = render_status_page(collect(finished))
        self.assertIn("last run ended</th><td>21 minutes ago</td>", page)

        running = FakeConn(runs=[
            (4, stamp(NOW - DAY), None, "running", None, 0, None),
        ])
        page = render_status_page(collect(running))
        self.assertIn("last run ended</th><td>running now</td>", page)

        page = render_status_page(collect(FakeConn(runs=[])))
        self.assertIn("last run ended</th><td>unknown</td>", page)

        page = render_status_page(collect())
        self.assertIn('<table class="grid runs">', page)
        self.assertIn(
            ".status table th:first-child,\n"
            ".status table td:first-child {\n"
            "    color: #888;",
            page,
        )
        self.assertIn("width: 1%;\n    white-space: nowrap", page)
        self.assertNotIn("width: 6rem", page)
        self.assertNotIn(".status table.runs", page)


    def test_containers_tables_and_text(self) -> None:
        page = render_status_page(collect())
        self.assertIn('<table class="grid containers">', page)
        self.assertIn(
            ".status table.containers th:nth-child(3),\n"
            ".status table.containers td:nth-child(3) {\n"
            "    width: 7rem;\n"
            "    white-space: nowrap;\n"
            "}",
            page,
        )
        self.assertIn("<td>running</td>", page)

        page = render_status_page(collect())
        self.assertIn('<th scope="row">6 hour</th>', page)
        self.assertIn('<th scope="row" class=rank>', page)
        self.assertIn(
            ".status table {\n"
            "    width: 100%;\n"
            "    max-width: 52rem;\n"
            "    margin: .25rem auto 1.1rem;\n"
            "}",
            page,
        )
        self.assertIn(
            ".status table th:first-child,\n"
            ".status table td:first-child {\n"
            "    color: #888",
            page,
        )
        self.assertNotIn("table.facts {", page)
        self.assertNotIn("table.grid {", page)

        page = render_status_page(collect())
        self.assertIn("<h3>containers</h3>", page)
        self.assertIn('<table class="grid containers">', page)
        self.assertLess(
            page.index("<h2>service</h2>"),
            page.index("<h3>containers</h3>"),
        )
        self.assertLess(
            page.index("<h3>containers</h3>"),
            page.index("<h2>database</h2>"),
        )
        for row in DOCKER.containers:
            self.assertIn(
                f'<tr><th scope="row">{row.service}</th>'
                f"<td>{row.name}</td><td>{row.state}</td>"
                f"<td>{row.status}</td><td>{row.image}</td></tr>",
                page,
            )
        self.assertIn(
            '<th>service</th><th>container</th><th>state</th>'
            "<th>status</th><th>image</th>",
            page,
        )

        data = collect()._replace(
            containers=DockerStatus((), "permission denied")
        )
        page = render_status_page(data)
        self.assertIn(
            "docker api unavailable: permission denied", page
        )
        self.assertIn('<td class=empty colspan="5">', page)
        self.assertNotIn("vscape_tracker-collector-1", page)

        empty = render_status_page(
            collect()._replace(containers=DockerStatus(()))
        )
        self.assertIn("no containers found", empty)

        data = collect()._replace(containers=DockerStatus((
            ContainerInfo("a<b", "c<d", "e<f", "g<h", "i<j"),
        )))
        page = render_status_page(data)
        self.assertIn("a&lt;b", page)
        self.assertNotIn("a<b", page)

        text = render_status_text(collect())
        self.assertIn("CONTAINERS", text)
        self.assertLess(text.index("SERVICE"), text.index("CONTAINERS"))
        self.assertLess(text.index("CONTAINERS"), text.index("DATABASE"))
        self.assertIn("vscape_tracker-collector-1", text)


    def test_table_alignment_and_header(self) -> None:
        page = render_status_page(collect())
        self.assertIn(
            ".status table {\n"
            "    width: 100%;\n"
            "    max-width: 52rem;\n"
            "    margin: .25rem auto 1.1rem;\n"
            "}",
            page,
        )

        page = render_status_page(collect())
        self.assertIn("<th>interval</th>", page)
        bold = (
            ".status table thead th:first-child {\n"
            "    font-weight: 600;\n"
            "}"
        )
        self.assertIn(bold, page)
        self.assertIn('<th scope="row">6 hour</th>', page)
        self.assertLess(
            page.index(
                ".status table th:first-child,\n"
                ".status table td:first-child {\n"
                "    color: #888;"
            ),
            page.index(bold),
        )


    def test_text_counts_and_ranges(self) -> None:
        data = collect()
        text = render_status_text(data)
        for heading in (
            "DATA COVERAGE", "SERVICE", "DATABASE", "TABLES", "BACKFILL",
            "LIVE", "PRIORITY", "PLAYERS", "SNAPSHOTS", "CHECKPOINTS",
            "RECENT RUNS",
        ):
            self.assertIn(heading, text)
        self.assertNotIn("COLLECTION", text)
        self.assertLess(text.index("DATA COVERAGE"), text.index("SERVICE"))
        self.assertNotIn("<", text)
        line = next(
            line for line in text.splitlines()
            if line.startswith("backfill interval:")
        )
        self.assertEqual(line[:18], "backfill interval:")
        self.assertEqual(line[28:], "24 hours")
        line = next(
            line for line in text.splitlines()
            if line.startswith("last run ended:")
        )
        self.assertEqual(line[28:], "1 hour ago")
        self.assertIn(
            f"{'newest priority checkpoint:':<28}10 minutes ago", text
        )
        self.assertIn("2/2 players (100%)", text)
        self.assertTrue(text.endswith("\n\n" + data.version + "\n"))

        data = collect()
        self.assertEqual(
            [entry.name for entry in data.snapshots],
            ["10 minute priority", "6 hour", "24 hour"],
        )
        self.assertEqual(
            [entry.count for entry in data.snapshots], [5, 4, 2]
        )
        self.assertEqual(
            [entry.count for entry in data.fetches], [9, 10, 3]
        )
        page = render_status_page(data)
        self.assertIn("10 minute priority", page)
        self.assertIn("24 hour", page)

        data = collect()
        rows = {table.name: table.rows for table in data.tables}
        self.assertEqual(rows["players"], 101)
        self.assertEqual(rows["runs"], 7)
        self.assertEqual(rows["snapshots"], 1000)
        self.assertEqual(rows["checkpoints"], 2000)
        self.assertEqual(rows["priority_snapshots"], 3000)
        self.assertEqual(rows["priority_checkpoints"], 4000)
        page = render_status_page(data)
        self.assertIn('<th scope="row">runs</th><td class="num">7</td>', page)
        self.assertIn('<th scope="row">players</th><td class="num">101</td>', page)


    def test_write_status_page(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "src.web.status.queries.container_status", return_value=DOCKER
        ):
            path = write_status_page(
                FakeConn(), Path(directory),
                generated="2026-01-01 00:00 UTC",
                now_epoch=NOW,
                version="software version abc1234",
                report_interval=60,
                backfill_interval=DAY,
                collect_interval=COLLECT,
                retry_window=7 * DAY,
                lookback=LOOKBACK,
                coverage=[],
                player_count=0,
                coverage_total=0,
                priority_players=0,
            )
            self.assertEqual(path.name, "status.html")
            page = path.read_text(encoding="utf-8")
            self.assertIn(
                '<header class="site">vidyascape tracker</header>', page
            )
            self.assertIn("<h1>status</h1>", page)
            text_path = Path(directory) / "status.txt"
            self.assertTrue(text_path.exists())
            text = text_path.read_text(encoding="utf-8")
            self.assertIn("VIDYASCAPE TRACKER STATUS", text)
            self.assertIn("RECENT RUNS", text)
            self.assertTrue(text.endswith("software version abc1234\n"))


if __name__ == "__main__":
    unittest.main()
