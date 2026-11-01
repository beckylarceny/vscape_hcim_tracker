"""Tests for priority player pages and RSS feeds (no database needed)."""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

from src.config import SKILLS
from src.player_report import ChartData
from src.priority_report import (
    Event,
    PriorityData,
    collect_events,
    collect_priority_data,
    render_feed,
    render_priority_page,
    write_priority_reports,
)

NOW = 1_800_000_000


class FakeResult:
    def __init__(self, rows) -> None:
        self.rows = rows

    def fetchall(self):
        return self.rows


class FakeConn:
    def __init__(self, rows) -> None:
        self.rows = rows

    def execute(self, sql, params=None):
        return FakeResult(self.rows)


def rows(*points) -> list[tuple[str, dt.datetime, int]]:
    return [
        (
            skill,
            dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc),
            xp,
        )
        for skill, epoch, xp in points
    ]


class DataTest(unittest.TestCase):
    def test_current_xp_and_one_chart_per_skill(self) -> None:
        conn = FakeConn(rows(
            ("overall", NOW - 1800, 1000),
            ("overall", NOW, 2000),
            ("attack", NOW, 50),
        ))
        data = collect_priority_data(conn, "alice", NOW, "generated")
        self.assertEqual(data.current["overall"], 2000)
        self.assertEqual(data.current["attack"], 50)
        self.assertEqual(len(data.charts), len(SKILLS))
        chart = data.charts["overall"]
        self.assertEqual(chart.xp[-1], (NOW, 2000))
        self.assertAlmostEqual(chart.gains[0][1], 2000.0)

    def test_missing_skill_has_an_empty_chart(self) -> None:
        conn = FakeConn(rows(("overall", NOW, 100)))
        data = collect_priority_data(conn, "alice", NOW, "generated")
        self.assertEqual(data.charts["attack"].xp, [])
        self.assertEqual(data.charts["attack"].gains, [])


class EventTest(unittest.TestCase):
    def test_changes_are_grouped_by_fetch_time(self) -> None:
        conn = FakeConn(rows(
            ("overall", NOW - 3600, 1000),
            ("overall", NOW - 1800, 1500),
            ("overall", NOW, 1800),
            ("attack", NOW - 1800, 100),
            ("attack", NOW, 120),
        ))
        events = collect_events(conn, "alice", NOW)
        self.assertEqual([event.epoch for event in events], [NOW, NOW - 1800])
        self.assertEqual(
            dict(events[0].changes), {"overall": 300, "attack": 20}
        )
        self.assertEqual(
            dict(events[1].changes), {"overall": 500}
        )

    def test_old_events_are_dropped(self) -> None:
        conn = FakeConn(rows(
            ("overall", NOW - 2 * 86400, 100),
            ("overall", NOW - 2 * 86400 + 1800, 200),
        ))
        self.assertEqual(collect_events(conn, "alice", NOW), [])


class FeedTest(unittest.TestCase):
    def test_feed_has_one_item_per_change(self) -> None:
        events = [
            Event(NOW, (("overall", 300), ("attack", 20))),
            Event(NOW - 1800, (("overall", 500),)),
        ]
        feed = render_feed(
            "A & B", "A_B", events, NOW, "https://example.test/"
        )
        self.assertIn('<?xml version="1.0" encoding="UTF-8"?>', feed)
        self.assertIn("<title>A &amp; B +300 xp</title>", feed)
        self.assertIn("overall: +300 xp", feed)
        self.assertIn("attack: +20 xp", feed)
        self.assertIn(f"A_B-{NOW}", feed)
        self.assertEqual(feed.count("<item>"), 2)
        self.assertIn(
            "https://example.test/priority/A_B.xml", feed
        )
        self.assertIn("https://example.test/priority/A_B.html", feed)

    def test_empty_feed_is_still_valid(self) -> None:
        feed = render_feed("alice", "alice", [], NOW, "http://localhost:8000")
        self.assertIn("<channel>", feed)
        self.assertNotIn("<item>", feed)


class PageTest(unittest.TestCase):
    def make_data(self) -> PriorityData:
        chart = ChartData(
            NOW - 86400,
            NOW,
            [(NOW - 86400, 400), (NOW, 1000)],
            [(NOW, 600)],
        )
        return PriorityData(
            username="alice",
            generated="2026-01-01 00:00 UTC",
            now_epoch=NOW,
            current={skill: 1000 for skill in SKILLS},
            charts={skill: chart for skill in SKILLS},
        )

    def test_one_big_chart_per_skill(self) -> None:
        page = render_priority_page(self.make_data())
        self.assertEqual(page.count("<svg"), len(SKILLS))
        self.assertIn('viewBox="0 0 720 200"', page)
        self.assertIn("priority report: 30-minute updates", page)
        self.assertIn('href="alice.xml"', page)
        self.assertIn('href="../players/alice.html"', page)
        self.assertIn('type="application/rss+xml"', page)

    def test_no_data_charts(self) -> None:
        data = self.make_data()
        empty = data._replace(
            current={},
            charts={
                skill: ChartData(NOW - 86400, NOW, [], [])
                for skill in SKILLS
            },
        )
        page = render_priority_page(empty)
        self.assertIn("(no data)", page)
        self.assertEqual(page.count("<svg"), 0)


class WriteTest(unittest.TestCase):
    def test_writes_pages_and_removes_stale_files(self) -> None:
        conn = FakeConn(rows(("overall", NOW - 1800, 1000)))
        with tempfile.TemporaryDirectory() as directory:
            report_dir = Path(directory)
            written = write_priority_reports(
                conn, report_dir, ("alice",), NOW, "generated",
                "http://localhost:8000",
            )
            self.assertEqual(written, 1)
            page = report_dir / "priority" / "alice.html"
            feed = report_dir / "priority" / "alice.xml"
            self.assertTrue(page.exists())
            self.assertTrue(feed.exists())
            self.assertIn(
                "alice priority - xp tracker",
                page.read_text(encoding="utf-8"),
            )

            stale = report_dir / "priority" / "old.html"
            stale.write_text("old", encoding="utf-8")
            write_priority_reports(
                conn, report_dir, (), NOW, "generated",
                "http://localhost:8000",
            )
            self.assertFalse(page.exists())
            self.assertFalse(feed.exists())
            self.assertFalse(stale.exists())


if __name__ == "__main__":
    unittest.main()
