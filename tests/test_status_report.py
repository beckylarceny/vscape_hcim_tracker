"""Tests for the status page (no database needed)."""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.status_report import (
    _elapsed,
    _interval_label,
    _size,
    _until,
    collect_status,
    coverage_bar,
    coverage_runs,
    render_status_page,
    write_status_page,
)

NOW = 1_800_000_000
DAY = 86400
COLLECT = 6 * 3600
LOOKBACK = 365 * DAY
PRIORITY = 600


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
        user: str = "xp_tracker",
        server_version: str = "16.1",
    ) -> None:
        self.fail = fail
        self.info = SimpleNamespace(
            server_version=server_version,
            dbname=database,
            user=user,
            host=host,
            port=5432,
        )

    def execute(self, sql, params=None):
        if self.fail and sql.strip() == "SELECT 1":
            raise RuntimeError("connection refused")
        if "pg_database_size" in sql:
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
        if "max(observed_at) FROM priority_checkpoints" in sql:
            return FakeResult([(stamp(NOW - 600),)])
        if "max(observed_at) FROM checkpoints" in sql:
            return FakeResult([(stamp(NOW - 3600),)])
        if "FROM runs" in sql:
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
        coverage=[(NOW - 2 * DAY, 2), (NOW - DAY, 1), (NOW, 0)],
        player_count=2,
        coverage_total=2,
        priority_players=3,
    )
    kwargs.update(overrides)
    return collect_status(conn or FakeConn(), **kwargs)


class CoverageTest(unittest.TestCase):
    def test_bar_runs_tooltips_and_zero_segments(self) -> None:
        data = collect()
        page = render_status_page(data)
        bar = coverage_bar(data)
        self.assertIn('class="coverage"', bar)
        self.assertIn("(1 day): 1/2 players (50%)", bar)
        self.assertIn('data-tip="', bar)
        self.assertIn('class="zero"', bar)
        self.assertIn("min-width:1em", page)
        self.assertIn('id="coverage-tip"', page)
        self.assertIn("(hover: none)", page)
        self.assertNotIn('<span title="', page)

        empty = coverage_bar(collect(coverage=[]))
        self.assertIn('<span class="zero"></span>', empty)

        self.assertEqual(
            coverage_runs(
                [(0, 5), (86400, 5), (172800, 2), (259200, 2), (345600, 0)]
            ),
            [(0, 2, 5), (2, 2, 2), (4, 1, 0)],
        )
        self.assertEqual(
            coverage_runs([(0, 1), (86400, 2), (172800, 1)]),
            [(0, 1, 1), (1, 1, 2), (2, 1, 1)],
        )


class FormatTest(unittest.TestCase):
    def test_elapsed_until_and_size(self) -> None:
        self.assertEqual(_elapsed(2 * DAY + 4 * 3600), "2 days 4 hours")
        self.assertEqual(_elapsed(3 * 3600 + 12 * 60), "3 hours 12 minutes")
        self.assertEqual(_elapsed(30), "less than a minute")
        self.assertEqual(_until(12 * 60), "in 12 minutes")
        self.assertEqual(_until(10), "less than a minute")
        self.assertEqual(_size(2 * 1024 ** 3), "2.0 GB")
        self.assertEqual(_size(1500), "1.5 KB")
        self.assertEqual(_size(None), "unknown")

    def test_interval_label_pluralizes(self) -> None:
        self.assertEqual(_interval_label(60), "1 minute")
        self.assertEqual(_interval_label(3600), "1 hour")
        self.assertEqual(_interval_label(600), "10 minutes")
        self.assertEqual(_interval_label(21600), "6 hours")
        self.assertEqual(_interval_label(86400), "24 hours")


class PageTest(unittest.TestCase):
    def test_every_section_and_moved_metadata(self) -> None:
        page = render_status_page(collect())
        for heading in (
            "service", "database", "collection", "players", "snapshots",
            "data coverage", "recent runs",
        ):
            self.assertIn(f"<h2>{heading}</h2>", page)
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
        self.assertIn("estimated rows", page)
        self.assertIn("snapshots store only changed xp", page)
        self.assertIn('href="latest.html"', page)
        self.assertIn('href="latest.txt"', page)
        self.assertIn('href="priority/index.html"', page)
        self.assertIn('class="status"', page)

    def test_players_line_shows_partial_total(self) -> None:
        page = render_status_page(collect(coverage_total=4))
        self.assertIn("2 / 4", page)

    def test_report_interval_zero_is_not_on_a_timer(self) -> None:
        page = render_status_page(collect(report_interval=0))
        self.assertIn("<td>not on a timer</td>", page)
        self.assertNotIn("every 1 minute", page)

        negative = render_status_page(collect(report_interval=-60))
        self.assertIn("<td>not on a timer</td>", negative)

        timed = render_status_page(collect(report_interval=3600))
        self.assertIn("<td>every 1 hour</td>", timed)

    def test_next_slot_arithmetic(self) -> None:
        data = collect()
        self.assertEqual(
            data.next_live_epoch,
            (NOW // COLLECT + 1) * COLLECT + 60,
        )
        self.assertEqual(
            data.next_priority_epoch,
            (NOW // PRIORITY + 1) * PRIORITY + 5,
        )

    def test_database_text_is_escaped(self) -> None:
        conn = FakeConn(
            database="<script>alert(1)</script>",
            host="<host>",
            user="<user>",
            server_version="<ver>",
        )
        page = render_status_page(collect(conn))
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;host&gt;:5432", page)
        self.assertIn("&lt;user&gt;", page)

    def test_unreachable_database_degrades(self) -> None:
        page = render_status_page(collect(FakeConn(fail=True)))
        self.assertIn("unreachable: connection refused", page)
        self.assertIn("unknown", page)

    def test_frequency_counts_and_ranges(self) -> None:
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

    def test_write_status_page(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
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
            self.assertIn(
                "xp tracker status", path.read_text(encoding="utf-8")
            )


if __name__ == "__main__":
    unittest.main()
