"""Tests for priority player pages and RSS feeds (no database needed)."""

from __future__ import annotations

import datetime as dt
import re
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

    def fetchone(self):
        return self.rows[0] if self.rows else None


class FakeConn:
    def __init__(self, rows, canonical=None, regular=()) -> None:
        self.rows = rows
        self.canonical = canonical
        self.regular = regular

    def execute(self, sql, params=None):
        if "FROM players" in sql:
            rows = [(self.canonical,)] if self.canonical else []
            return FakeResult(rows)
        if "FROM snapshots" in sql:
            return FakeResult(self.regular)
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

    def test_regular_snapshots_fill_priority_gaps(self) -> None:
        priority = rows(
            ("overall", NOW - 3600, 1000),
            ("overall", NOW, 1100),
        )
        regular = rows(
            ("overall", NOW - 7200, 500),
            ("overall", NOW - 1800, 1050),
            ("overall", NOW - 600, 1080),
        )
        conn = FakeConn(priority, regular=regular)
        data = collect_priority_data(conn, "alice", NOW, "generated")
        chart = data.charts["overall"]
        self.assertEqual(data.current["overall"], 1100)
        self.assertIn(NOW - 7200, chart.regular_xp)
        self.assertIn(NOW - 1800, chart.regular_xp)
        self.assertIn(NOW - 600, chart.regular_xp)
        self.assertNotIn(NOW - 3600, chart.regular_xp)
        self.assertNotIn(NOW, chart.regular_xp)
        self.assertIn(NOW - 1800, chart.regular_gains)
        self.assertIn(NOW, chart.regular_gains)

    def test_regular_point_inside_priority_interval_is_ignored(self) -> None:
        priority = rows(
            ("overall", NOW - 600, 1000),
            ("overall", NOW, 1100),
        )
        regular = rows(("overall", NOW - 300, 1050))
        conn = FakeConn(priority, regular=regular)
        chart = collect_priority_data(conn, "alice", NOW, "generated").charts[
            "overall"
        ]
        self.assertNotIn(NOW - 300, chart.regular_xp)
        self.assertNotIn(NOW - 300, [stamp for stamp, _ in chart.xp])


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
        self.assertEqual(events[0].previous_epoch, NOW - 1800)
        self.assertEqual(events[1].previous_epoch, NOW - 3600)

    def test_previous_snapshot_can_come_from_regular_data(self) -> None:
        priority = rows(
            ("overall", NOW - 3600, 1000),
            ("overall", NOW - 1800, 1500),
            ("overall", NOW, 1800),
        )
        regular = rows(
            ("overall", NOW - 2400, 1200),
        )
        conn = FakeConn(priority, regular=regular)
        events = collect_events(conn, "alice", NOW)
        by_epoch = {event.epoch: event for event in events}
        self.assertEqual(
            by_epoch[NOW - 1800].previous_epoch, NOW - 2400
        )
        self.assertEqual(by_epoch[NOW].previous_epoch, NOW - 1800)

    def test_old_events_are_dropped(self) -> None:
        conn = FakeConn(rows(
            ("overall", NOW - 2 * 86400, 100),
            ("overall", NOW - 2 * 86400 + 1800, 200),
        ))
        self.assertEqual(collect_events(conn, "alice", NOW), [])


class FeedTest(unittest.TestCase):
    def test_feed_has_one_item_per_change(self) -> None:
        events = [
            Event(NOW, (("overall", 300), ("attack", 20)), NOW - 600),
            Event(NOW - 1800, (("overall", 500),), NOW - 3600),
        ]
        feed = render_feed(
            "A & B", "A_B", events, NOW, "https://example.test/"
        )
        self.assertIn('<?xml version="1.0" encoding="UTF-8"?>', feed)
        self.assertIn("<title>A &amp; B +300 xp</title>", feed)
        self.assertIn("overall: +300 xp", feed)
        self.assertIn("attack: +20 xp", feed)
        self.assertIn("last snapshot: 10 minutes ago", feed)
        self.assertIn("last snapshot: 30 minutes ago", feed)
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
        self.assertIn("priority report: 10-minute updates", page)
        self.assertIn(
            '<h2 class="subheading"><a href="alice.xml">rss feed</a></h2>',
            page,
        )
        self.assertEqual(page.count('class="subheading"'), 2)
        self.assertIn('href="../players/alice.html"', page)
        self.assertIn('type="application/rss+xml"', page)

    def test_time_axis_uses_hh_mm_ticks(self) -> None:
        page = render_priority_page(self.make_data())
        labels = re.findall(
            r'<text x="[^"]+" y="\d+" text-anchor="[^"]+">'
            r"(\d\d:\d\d)</text>",
            page,
        )
        self.assertEqual(len(labels), len(SKILLS) * 7)
        self.assertNotIn("2027-01-15<", page)

    def test_skill_links_under_the_header(self) -> None:
        page = render_priority_page(self.make_data())
        self.assertIn('<nav class="skill-nav">', page)
        self.assertIn(
            '<a href="#skill-overall">overall</a>', page
        )
        self.assertIn('<a href="#skill-attack">attack</a>', page)
        self.assertLess(
            page.index('class="skill-nav"'),
            page.index('<section id="skill-overall">'),
        )
        self.assertIn(".skill-nav{display:flex", page)

    def test_regular_data_is_marked_and_coloured(self) -> None:
        data = self.make_data()
        chart = ChartData(
            NOW - 86400,
            NOW,
            [(NOW - 86400, 400), (NOW - 3600, 800), (NOW, 1000)],
            [(NOW - 3600, 400), (NOW, 200)],
            frozenset({NOW - 3600}),
            frozenset({NOW - 3600}),
        )
        page = render_priority_page(
            data._replace(charts={skill: chart for skill in SKILLS})
        )
        self.assertIn("regular data", page)
        self.assertIn('class="line regular"', page)
        self.assertIn('class="area regular"', page)

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
            index = report_dir / "priority" / "index.html"
            self.assertTrue(page.exists())
            self.assertTrue(feed.exists())
            self.assertTrue(index.exists())
            self.assertIn(
                "alice priority - xp tracker",
                page.read_text(encoding="utf-8"),
            )
            self.assertIn(
                'href="alice.html"',
                index.read_text(encoding="utf-8"),
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
            self.assertEqual(
                index.read_text(encoding="utf-8").count(
                    "no priority players configured"
                ),
                1,
            )

    def test_configured_name_resolves_to_canonical_player(self) -> None:
        conn = FakeConn(
            rows(("overall", NOW - 1800, 1000)),
            canonical="Cojultad",
        )
        with tempfile.TemporaryDirectory() as directory:
            report_dir = Path(directory)
            write_priority_reports(
                conn, report_dir, ("cojultad",), NOW, "generated",
                "http://localhost:8000",
            )
            priority = report_dir / "priority"
            self.assertTrue((priority / "Cojultad.html").exists())
            self.assertTrue((priority / "Cojultad.xml").exists())
            self.assertFalse((priority / "cojultad.html").exists())


if __name__ == "__main__":
    unittest.main()
