"""Tests for the per-player reports (no database needed)."""

from __future__ import annotations

import datetime as dt
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src import main
from src.config import SKILLS, SPANS
from src.player_report import (
    ChartData,
    PlayerData,
    Record,
    collect_player_data,
    highscores_url,
    level_for_xp,
    player_page_href,
    player_slug,
    render_player_page,
    total_level,
)

NOW = 1_800_000_000
DAY = 86400
LOOKBACK = 365 * DAY


class FakeResult:
    def __init__(self, rows) -> None:
        self.rows = rows

    def fetchall(self):
        return self.rows


class FakeConn:
    def __init__(self, rows, checkpoints=()) -> None:
        self.rows = rows
        self.checkpoints = [
            dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc)
            for epoch in checkpoints
        ]

    def execute(self, sql, params=None):
        if "FROM checkpoints" in sql:
            return FakeResult([(checkpoint,) for checkpoint in self.checkpoints])
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


def fake_data(username: str = "Fuck Jagex") -> PlayerData:
    current = {skill: 1000 for skill in SKILLS}
    past = {skill: {name: 100 for name in SPANS} for skill in SKILLS}
    best = {
        skill: {
            name: Record(
                gain=500, end_epoch=NOW, username=username,
                start_epoch=NOW - SPANS[name],
            )
            for name in SPANS
        }
        for skill in SKILLS
    }
    charts = {
        skill: {
            name: ChartData(
                start_epoch=NOW - SPANS[name],
                end_epoch=NOW,
                xp=[(NOW - SPANS[name], 400), (NOW, 1000)],
                gains=[(NOW, 600)],
            )
            for name in SPANS
        }
        for skill in SKILLS
    }
    return PlayerData(
        username=username,
        generated="2026-01-01 00:00 UTC",
        now_epoch=NOW,
        lookback=LOOKBACK,
        current=current,
        past=past,
        best=best,
        charts=charts,
        coverage=[
            (NOW - LOOKBACK, True),
            (NOW - LOOKBACK // 2, False),
            (NOW - DAY, True),
            (NOW, True),
        ],
    )


class SlugTest(unittest.TestCase):
    def test_slug_is_filesystem_safe(self) -> None:
        self.assertEqual(player_slug("Alice"), "Alice")
        self.assertEqual(player_slug("Fuck Jagex"), "Fuck_Jagex")
        self.assertEqual(player_slug("a/b?c"), "a_b_c")
        self.assertEqual(player_slug("..."), "player")
        self.assertEqual(player_slug(""), "player")

    def test_page_href_uses_the_slug(self) -> None:
        self.assertEqual(
            player_page_href("Fuck Jagex"), "players/Fuck_Jagex.html"
        )


class RenderTest(unittest.TestCase):
    def test_heading_and_highscores_subheading(self) -> None:
        page = render_player_page(fake_data())
        self.assertIn("<h1>Fuck Jagex</h1>", page)
        self.assertIn(
            '<h2 class="subheading"><a href="'
            f'{highscores_url("Fuck Jagex")}">Vidyascape highscores</a></h2>',
            page,
        )

    def test_current_xp_and_level_are_shown(self) -> None:
        page = render_player_page(fake_data())
        self.assertIn("Current xp", page)
        self.assertIn("<th class=\"num\">Level</th>", page)
        self.assertIn('<td class="num">9</td>', page)
        self.assertIn("current xp: 1,000 (level 9)", page)

    def test_past_and_best_are_separate_tables(self) -> None:
        page = render_player_page(fake_data())
        self.assertIn('<div class="gain-tables">', page)
        self.assertIn("<th class=\"num\">XP</th>", page)
        self.assertNotIn("Past XP", page)
        self.assertNotIn("Best XP", page)
        self.assertIn("<td class=\"num\">100</td>", page)
        self.assertIn("<td class=\"num\">500</td>", page)

    def test_overall_level_sums_other_skills(self) -> None:
        page = render_player_page(fake_data())
        # 23 real skills, each at 1,000 xp = level 9.
        self.assertIn(
            '<tr><td>overall</td><td class="num">207</td>', page
        )
        self.assertIn("current xp: 1,000 (total level 207)", page)

    def test_one_chart_per_skill_and_span(self) -> None:
        page = render_player_page(fake_data())
        self.assertEqual(
            page.count("<svg"), len(SKILLS) * len(SPANS)
        )
        self.assertIn("overall day: total xp and xp/h", page)
        self.assertIn('class="area"', page)
        self.assertIn('class="line"', page)
        self.assertIn('class="key area-key">xp/h</span>', page)

    def test_username_is_escaped(self) -> None:
        page = render_player_page(fake_data(username="<bob>"))
        self.assertIn("&lt;bob&gt;", page)
        self.assertNotIn("<bob>", page)

    def test_skill_tabs_are_below_the_current_table(self) -> None:
        page = render_player_page(fake_data())
        self.assertIn(
            '<div class="tabs" id="skill-tabs" role="tablist">', page
        )
        self.assertIn('data-skill="attack"', page)
        self.assertIn('aria-selected="true">overall</button>', page)
        self.assertNotIn('<nav class="skill-nav">', page)
        self.assertLess(
            page.index('<table class="current">'),
            page.index('id="skill-tabs"'),
        )
        self.assertLess(
            page.index('id="skill-tabs"'),
            page.index('<section id="skill-overall"'),
        )
        self.assertIn(".tabs{display:flex", page)
        self.assertIn(
            ".stats .tabs{margin-top:2.5rem;padding-top:1.25rem;"
            "border-top:1px solid #8884}",
            page,
        )
        self.assertIn(".tab-panel[hidden]{display:none}", page)

    def test_current_table_skills_are_not_links(self) -> None:
        page = render_player_page(fake_data())
        self.assertIn("<tr><td>attack</td>", page)
        self.assertNotIn('<tr><td><a href="#skill-attack">', page)

    def test_current_table_is_narrow(self) -> None:
        page = render_player_page(fake_data())
        self.assertIn(
            "table.current{width:auto;max-width:32rem;"
            "margin-left:auto;margin-right:auto}",
            page,
        )

    def test_all_tables_fit_their_content(self) -> None:
        page = render_player_page(fake_data())
        self.assertIn("table{width:auto;max-width:100%;", page)
        self.assertIn("margin:.25rem auto .75rem", page)

    def test_no_rounded_corners(self) -> None:
        page = render_player_page(fake_data())
        self.assertFalse(re.search(r"border-radius:\s*(?!0\b)\d", page))

    def test_all_text_is_lowercase(self) -> None:
        page = render_player_page(fake_data())
        self.assertIn("text-transform:lowercase", page)
        self.assertNotIn("text-transform:uppercase", page)

    def test_back_to_top_button(self) -> None:
        page = render_player_page(fake_data())
        self.assertIn('<body id="top">', page)
        self.assertIn('class="to-top" href="#top"', page)
        self.assertIn("position:fixed", page)

    def test_priority_link_is_shown_only_for_priority_players(self) -> None:
        page = render_player_page(fake_data(), is_priority=True)
        self.assertIn(
            '<p class="priority-link"><a href="../priority/Fuck_Jagex.html">'
            "priority report</a></p>",
            page,
        )
        normal = render_player_page(fake_data())
        self.assertNotIn("priority report", normal)

    def test_coverage_bar_is_rendered_at_the_top(self) -> None:
        page = render_player_page(fake_data())
        self.assertIn("data coverage:", page)
        self.assertIn('class="coverage"', page)
        self.assertIn('class="zero"', page)
        self.assertLess(
            page.index("data coverage:"), page.index("<h2>Current xp</h2>")
        )

    def test_all_rankings_is_a_top_backlink(self) -> None:
        page = render_player_page(fake_data())
        self.assertIn(
            '<div class="backlinks"><p class="sub">'
            '<a href="../latest.html">all rankings</a></p></div>',
            page,
        )
        self.assertLess(
            page.index('class="backlinks"'), page.index("<h1>")
        )
        self.assertIn(
            ".backlinks{margin:-2rem 0 1.75rem;text-align:center}", page
        )

    def test_headings_after_current_xp_are_centered(self) -> None:
        page = render_player_page(fake_data())
        self.assertIn(".stats h2,.stats h3,.stats h4{text-align:center}", page)
        self.assertLess(
            page.index('<div class="stats">'),
            page.index("<h2>Current xp</h2>"),
        )
        self.assertLess(
            page.index('class="subheading"'),
            page.index('<div class="stats">'),
        )

    def test_empty_history_renders_no_data(self) -> None:
        data = fake_data()
        empty = data._replace(
            current={},
            past={skill: {name: None for name in SPANS} for skill in SKILLS},
            best={skill: {name: None for name in SPANS} for skill in SKILLS},
            charts={
                skill: {
                    name: ChartData(NOW - SPANS[name], NOW, [], [])
                    for name in SPANS
                }
                for skill in SKILLS
            },
        )
        page = render_player_page(empty)
        self.assertIn("no data", page)
        self.assertIn("(no data)", page)
        self.assertEqual(page.count("<svg"), 0)


class ChartTest(unittest.TestCase):
    def test_gains_area_is_a_step_shape(self) -> None:
        from src.player_report import ChartData, _chart_svg

        chart = ChartData(
            start_epoch=NOW - 1000,
            end_epoch=NOW,
            xp=[(NOW - 1000, 0), (NOW, 3)],
            gains=[(NOW - 800, 10), (NOW - 400, 20), (NOW, 30)],
        )
        svg = _chart_svg(chart, "test")
        match = re.search(r'<polygon class="area" points="([^"]+)"', svg)
        self.assertIsNotNone(match)
        coords = [
            tuple(float(part) for part in point.split(","))
            for point in match.group(1).split()
        ]
        pairs = list(zip(coords, coords[1:]))
        self.assertTrue(any(
            first[0] == second[0] and first[1] != second[1]
            for first, second in pairs
        ))
        self.assertTrue(any(
            first[1] == second[1] and first[0] != second[0]
            for first, second in pairs
        ))

class ThinTest(unittest.TestCase):
    def test_long_series_keeps_endpoints(self) -> None:
        from src.player_report import _thin

        points = [(index, index * 10) for index in range(1000)]
        thinned = _thin(points, 180)
        self.assertEqual(len(thinned), 180)
        self.assertEqual(thinned[0], points[0])
        self.assertEqual(thinned[-1], points[-1])
        self.assertEqual(thinned, sorted(thinned))


class LevelTest(unittest.TestCase):
    def test_known_thresholds(self) -> None:
        self.assertEqual(level_for_xp(0), 1)
        self.assertEqual(level_for_xp(82), 1)
        self.assertEqual(level_for_xp(83), 2)
        self.assertEqual(level_for_xp(1154), 10)
        self.assertEqual(level_for_xp(13_034_430), 98)
        self.assertEqual(level_for_xp(13_034_431), 99)
        self.assertEqual(level_for_xp(200_000_000), 99)

    def test_negative_xp_is_level_one(self) -> None:
        self.assertEqual(level_for_xp(-5), 1)

    def test_total_level_excludes_overall(self) -> None:
        self.assertEqual(
            total_level({"overall": 999_999, "attack": 1154, "defence": 0}),
            11,
        )
        self.assertIsNone(total_level({}))


class CollectTest(unittest.TestCase):
    def collect(self, conn) -> PlayerData:
        return collect_player_data(
            conn, "alice", NOW, LOOKBACK, DAY, "2026-01-01 00:00 UTC"
        )

    def test_current_xp_and_past_gain(self) -> None:
        conn = FakeConn(rows(
            ("overall", NOW - 3 * DAY, 1000),
            ("overall", NOW - 2 * DAY, 2000),
            ("overall", NOW - DAY, 3000),
            ("overall", NOW, 4000),
        ))
        data = self.collect(conn)
        self.assertEqual(data.current["overall"], 4000)
        self.assertEqual(data.past["overall"]["day"], 1000)
        self.assertIsNone(data.past["overall"]["week"])

    def test_best_window_is_recorded(self) -> None:
        conn = FakeConn(rows(
            ("overall", NOW - 3 * DAY, 1000),
            ("overall", NOW - 2 * DAY, 2000),
            ("overall", NOW - DAY, 3000),
            ("overall", NOW, 4000),
        ))
        record = self.collect(conn).best["overall"]["day"]
        self.assertIsNotNone(record)
        self.assertEqual(record.gain, 1000)

    def test_chart_has_total_xp_and_per_point_gains(self) -> None:
        conn = FakeConn(rows(
            ("overall", NOW - 3 * DAY, 1000),
            ("overall", NOW - 2 * DAY, 2000),
            ("overall", NOW - DAY, 3000),
            ("overall", NOW, 4000),
        ))
        chart = self.collect(conn).charts["overall"]["day"]
        self.assertEqual(chart.start_epoch, NOW - DAY)
        self.assertEqual(chart.end_epoch, NOW)
        self.assertEqual(chart.xp[0], (NOW - DAY, 3000))
        self.assertEqual(chart.xp[-1], (NOW, 4000))
        self.assertEqual(chart.gains[0][0], NOW)
        self.assertAlmostEqual(chart.gains[0][1], 1000 / 24)

    def test_longer_window_includes_every_stored_point(self) -> None:
        conn = FakeConn(rows(
            ("overall", NOW - 3 * DAY, 1000),
            ("overall", NOW - 2 * DAY, 2000),
            ("overall", NOW - DAY, 3000),
            ("overall", NOW, 4000),
        ))
        chart = self.collect(conn).charts["overall"]["week"]
        self.assertEqual([xp for _, xp in chart.xp], [1000, 2000, 3000, 4000])
        self.assertEqual(len(chart.gains), 3)
        for _, rate in chart.gains:
            self.assertAlmostEqual(rate, 1000 / 24)

    def test_gain_rate_normalizes_datapoint_interval(self) -> None:
        conn = FakeConn(rows(
            ("overall", NOW - 30 * 3600, 0),
            ("overall", NOW - 24 * 3600, 100),
            ("overall", NOW, 500),
        ))
        rates = [
            rate for _, rate in self.collect(conn).charts["overall"]["week"].gains
        ]
        self.assertEqual(len(rates), 2)
        self.assertAlmostEqual(rates[0], 100 / 6)
        self.assertAlmostEqual(rates[1], 400 / 24)
        self.assertAlmostEqual(rates[0], rates[1])

    def test_total_xp_line_stays_flat_without_new_points(self) -> None:
        conn = FakeConn(rows(
            ("overall", NOW - 3 * DAY, 1000),
            ("overall", NOW - DAY, 2000),
        ))
        chart = self.collect(conn).charts["overall"]["week"]
        self.assertEqual(chart.xp[-1], (NOW, 2000))
        self.assertEqual(chart.xp[-2], (NOW - DAY, 2000))

    def test_total_xp_line_is_flat_with_one_point(self) -> None:
        conn = FakeConn(rows(("overall", NOW - DAY, 500)))
        chart = self.collect(conn).charts["overall"]["day"]
        self.assertEqual(chart.xp, [(NOW - DAY, 500), (NOW, 500)])

    def test_coverage_marks_days_with_checkpoints(self) -> None:
        conn = FakeConn([], checkpoints=[NOW - 3 * DAY, NOW - DAY, NOW])
        data = self.collect(conn)
        self.assertEqual(len(data.coverage), 365)
        covered = [flag for _, flag in data.coverage]
        self.assertEqual(sum(covered), 3)
        self.assertTrue(any(covered))
        self.assertFalse(all(covered))

    def test_skills_without_snapshots_are_absent(self) -> None:
        conn = FakeConn([])
        data = self.collect(conn)
        self.assertEqual(data.current, {})
        self.assertIsNone(data.past["overall"]["day"])
        chart = data.charts["overall"]["day"]
        self.assertEqual(chart.xp, [])
        self.assertEqual(chart.gains, [])


class WritePagesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = FakeConn(rows(
            ("overall", NOW - DAY, 100),
            ("overall", NOW, 200),
        ))

    def data(self, generated: str = "first") -> SimpleNamespace:
        return SimpleNamespace(
            players=("alice",),
            now_epoch=NOW,
            lookback=LOOKBACK,
            interval=DAY,
            generated=generated,
        )

    def test_initial_build_then_skip_when_watermark_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report_dir = Path(directory)
            main.write_player_pages(
                self.conn, report_dir, self.data(), {"alice": 1}, None
            )
            page = report_dir / "players" / "alice.html"
            before = page.read_text(encoding="utf-8")
            self.assertIn("first", before)

            written = main.write_player_pages(
                self.conn, report_dir, self.data("second"),
                {"alice": 1}, {"alice": 1},
            )
            self.assertEqual(written, 0)
            self.assertEqual(page.read_text(encoding="utf-8"), before)

    def test_changed_watermark_rebuilds_one_page(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report_dir = Path(directory)
            main.write_player_pages(
                self.conn, report_dir, self.data(), {"alice": 1}, None
            )
            written = main.write_player_pages(
                self.conn, report_dir, self.data("second"),
                {"alice": 2}, {"alice": 1},
            )
            self.assertEqual(written, 1)
            page = report_dir / "players" / "alice.html"
            self.assertIn("second", page.read_text(encoding="utf-8"))

    def test_removed_player_page_is_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report_dir = Path(directory)
            main.write_player_pages(
                self.conn, report_dir, self.data(), {"alice": 1}, None
            )
            page = report_dir / "players" / "alice.html"
            self.assertTrue(page.exists())
            data = self.data()
            data.players = ()
            main.write_player_pages(
                self.conn, report_dir, data, {}, {"alice": 1}
            )
            self.assertFalse(page.exists())


if __name__ == "__main__":
    unittest.main()
