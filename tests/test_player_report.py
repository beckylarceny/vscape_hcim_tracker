"""Tests for the per-player reports (no database needed)."""

from __future__ import annotations

import datetime as dt
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.common.utility import DATA_START_EPOCH, SKILLS, SPANS
from src.web import reporting
from src.web.render.chart import ChartData
from src.web.render.chart import chart_svg as _chart_svg
from src.web.render.chart import thin as _thin
from src.web.reports.player import (
    PlayerData,
    Record,
    _frequency_info,
    collect_player_data,
    highscores_url,
    level_for_xp,
    player_page_href,
    player_slug,
    render_player_page,
    render_player_text,
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
    def __init__(self, rows, checkpoints=(), priority=()) -> None:
        self.rows = rows
        self.priority = priority
        self.checkpoints = [
            dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc)
            for epoch in checkpoints
        ]

    def execute(self, sql, params=None):
        if "FROM checkpoints" in sql:
            return FakeResult([(checkpoint,) for checkpoint in self.checkpoints])
        if "DISTINCT observed_at" in sql:
            source = self.priority if "priority_snapshots" in sql else self.rows
            return FakeResult([(row[1],) for row in source])
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
    for skill in SKILLS:
        charts[skill]["all"] = charts[skill]["year"]
    return PlayerData(
        username=username,
        generated="2026-01-01 00:00 UTC",
        now_epoch=NOW,
        lookback=LOOKBACK,
        current=current,
        past=past,
        best=best,
        charts=charts,
    )


class SlugTest(unittest.TestCase):
    def test_slug_and_page_href(self) -> None:
        self.assertEqual(player_slug("Alice"), "Alice")
        self.assertEqual(player_slug("Fuck Jagex"), "Fuck_Jagex")
        self.assertEqual(player_slug("a/b?c"), "a_b_c")
        self.assertEqual(player_slug("..."), "player")
        self.assertEqual(
            player_page_href("Fuck Jagex"), "players/Fuck_Jagex"
        )


class RenderTest(unittest.TestCase):
    def test_header_current_xp_and_text(self) -> None:
        page = render_player_page(fake_data())
        self.assertIn(
            "<title>fuck jagex - vidyascape tracker</title>", page
        )
        self.assertIn(
            '<header class="site">vidyascape tracker</header>', page
        )
        self.assertIn("<h1>Fuck Jagex</h1>", page)
        self.assertIn(
            f'<a href="{highscores_url("Fuck Jagex")}" '
            'target="_blank" rel="noopener">Vidyascape highscores</a>',
            page,
        )
        link_row = re.search(
            r'<p class="sub links">(.*?)</p>', page
        ).group(1)
        self.assertIn("Vidyascape highscores", link_row)
        self.assertIn("text version", link_row)
        self.assertEqual(link_row.count('target="_blank"'), 2)
        self.assertIn("generated at: 2026-01-01 00:00 UTC", page)
        self.assertNotIn("data coverage:", page)
        self.assertNotIn('class="coverage"', page)
        self.assertNotIn('id="coverage-tip"', page)
        self.assertLess(
            page.index("generated at:"), page.index("<h2>Current xp</h2>")
        )
        self.assertIn('href="../status"', page)
        self.assertIn(
            'href="Fuck_Jagex.txt" target="_blank" rel="noopener">'
            "text version</a>",
            page,
        )
        self.assertIn('class="player"', page)

        text = render_player_text(fake_data(), version="software version abc")
        self.assertTrue(
            text.startswith("VIDYASCAPE TRACKER PLAYER REPORT\n")
        )
        self.assertIn("player:             Fuck Jagex", text)
        self.assertIn("current xp:", text)
        self.assertTrue(text.endswith("software version abc\n"))

        priority = render_player_page(fake_data(), is_priority=True)
        priority_row = re.search(
            r'<p class="sub links">(.*?)</p>', priority
        ).group(1)
        self.assertIn("priority report", priority_row)
        self.assertIn("text version", priority_row)
        self.assertEqual(priority_row.count('target="_blank"'), 3)
        self.assertNotIn("priority report", page)

        escaped = render_player_page(fake_data(username="<bob>"))
        self.assertIn("&lt;bob&gt;", escaped)
        self.assertNotIn("<bob>", escaped)

        version = "software version abc1234, updated 2026-11-01 13:00 UTC"
        tagged = render_player_page(fake_data(), version=version)
        self.assertIn(f'<footer class="version">{version}</footer>', tagged)
        self.assertLess(tagged.index('class="to-top"'), tagged.index(version))

        page = render_player_page(fake_data())
        self.assertIn("Current xp", page)
        self.assertIn("<th class=\"num\">Level</th>", page)
        self.assertIn('<td class="num">9</td>', page)
        self.assertIn("current xp: 1,000 (level 9)", page)
        # 23 real skills, each at 1,000 xp = level 9.
        self.assertIn(
            '<tr><td>overall</td><td class="num">207</td>', page
        )
        self.assertIn("current xp: 1,000 (total level 207)", page)
        self.assertIn('<div class="gain-tables">', page)
        self.assertIn("<td class=\"num\">100</td>", page)
        self.assertIn("<td class=\"num\">500</td>", page)


    def test_one_chart_per_skill_and_span(self) -> None:
        page = render_player_page(fake_data())
        self.assertEqual(
            page.count("<svg"), len(SKILLS) * (len(SPANS) + 1)
        )
        self.assertIn("overall day: total xp and xp/h", page)
        self.assertIn("overall all time: total xp and xp/h", page)
        self.assertIn('<figure class="wide">', page)
        self.assertIn(
            ".charts figure.wide {\n    grid-column: 1 / -1;\n}", page
        )
        self.assertIn('class="key gap-key">missing data</span>', page)
        self.assertIn("width: .95em;\n    height: .95em", page)
        self.assertIn('class="area"', page)
        self.assertIn('class="line"', page)

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
                | {"all": ChartData(NOW - LOOKBACK, NOW, [], [])}
                for skill in SKILLS
            },
        )
        page = render_player_page(empty)
        self.assertIn("no data", page)
        self.assertIn("(no data)", page)
        self.assertEqual(page.count("<svg"), 0)


class ChartTest(unittest.TestCase):
    def test_chart_gaps_and_gain_shape(self) -> None:
        from src.web.reports.player import _missing_ranges

        self.assertEqual(_missing_ranges([], 0, 100, 10), ((0, 100),))
        self.assertEqual(_missing_ranges([50], 0, 100, 100), ())
        self.assertEqual(
            _missing_ranges([10, 80], 0, 100, 10),
            ((10, 80), (80, 100)),
        )

        chart = ChartData(
            start_epoch=NOW - 1000,
            end_epoch=NOW,
            xp=[(NOW - 1000, 0), (NOW, 3)],
            gains=[(NOW, 1)],
            missing=((NOW - 800, NOW - 400),),
        )
        svg = _chart_svg(chart, "gaps")
        self.assertIn('class="gap"', svg)

        from src.web.reports.player import _missing_ranges

        six_hourly = [0, 21600, 43200, 64800, 86400]
        self.assertEqual(_missing_ranges(six_hourly, 0, DAY, 2 * DAY), ())
        skipped = [0, 21600, 64800, 86400]
        self.assertEqual(
            _missing_ranges(skipped, 0, DAY, 2 * DAY),
            ((21600, 64800),),
        )

        # A plain 24 hour cadence is not partial coverage.
        daily = [index * DAY for index in range(5)]
        self.assertEqual(_missing_ranges(daily, 0, 4 * DAY, 2 * DAY), ())

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

        points = [(index, index * 10) for index in range(1000)]
        thinned = _thin(points, 180)
        self.assertEqual(len(thinned), 180)
        self.assertEqual(thinned[0], points[0])
        self.assertEqual(thinned[-1], points[-1])


class LevelTest(unittest.TestCase):
    def test_known_thresholds(self) -> None:
        self.assertEqual(level_for_xp(0), 1)
        self.assertEqual(level_for_xp(82), 1)
        self.assertEqual(level_for_xp(83), 2)
        self.assertEqual(level_for_xp(1154), 10)
        self.assertEqual(level_for_xp(13_034_431), 99)
        self.assertEqual(level_for_xp(200_000_000), 99)
        self.assertEqual(level_for_xp(-5), 1)
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

    def test_collected_values_and_charts(self) -> None:
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
        record = data.best["overall"]["day"]
        self.assertIsNotNone(record)
        self.assertEqual(record.gain, 1000)

        info = {item.name: item for item in data.frequencies}
        self.assertEqual(info["1 hour"].snapshots, 4)
        self.assertEqual(info["24 hour"].snapshots, 0)
        self.assertEqual(info["10 minute priority"].snapshots, 0)
        page = render_player_page(data)
        self.assertIn("generated at: 2026-01-01", page)
        self.assertIn("1 hour snapshots: 4, 2027-01-12 to 2027-01-15", page)
        self.assertIn("24 hour snapshots: 0", page)
        self.assertIn("10 minute priority snapshots: 0", page)

        mixed = _frequency_info(
            [172800, 172800 + 21600, 172800 + 43200],
            [172800 + 600],
            21600,
            86400,
        )
        self.assertEqual(
            [(item.name, item.snapshots) for item in mixed],
            [("10 minute priority", 1), ("6 hour", 2), ("24 hour", 1)],
        )

        conn = FakeConn(rows(
            ("overall", NOW - 3 * DAY, 1000),
            ("overall", NOW - 2 * DAY, 2000),
            ("overall", NOW - DAY, 3000),
            ("overall", NOW, 4000),
        ))
        data = self.collect(conn)
        chart = data.charts["overall"]["day"]
        self.assertEqual(chart.xp[0], (NOW - DAY, 3000))
        self.assertEqual(chart.xp[-1], (NOW, 4000))
        self.assertAlmostEqual(chart.gains[0][1], 1000 / 24)
        self.assertEqual(
            data.charts["overall"]["all"].start_epoch, DATA_START_EPOCH
        )
        self.assertLessEqual(len(data.charts["overall"]["all"].xp), 160)

        week = self.collect(conn).charts["overall"]["week"]
        self.assertEqual([xp for _, xp in week.xp], [1000, 2000, 3000, 4000])
        self.assertEqual(len(week.gains), 3)
        for _, rate in week.gains:
            self.assertAlmostEqual(rate, 1000 / 24)

        # Rates use the real point interval, not the nominal one.
        conn = FakeConn(rows(
            ("overall", NOW - 30 * 3600, 0),
            ("overall", NOW - 24 * 3600, 100),
            ("overall", NOW, 500),
        ))
        week = self.collect(conn).charts["overall"]["week"]
        rates = [rate for _, rate in week.gains]
        self.assertEqual(len(rates), 2)
        self.assertAlmostEqual(rates[0], 100 / 6)
        self.assertAlmostEqual(rates[1], 400 / 24)

        # Sparse series keep the last value until the end of the window.
        conn = FakeConn(rows(
            ("overall", NOW - 3 * DAY, 1000),
            ("overall", NOW - DAY, 2000),
        ))
        chart = self.collect(conn).charts["overall"]["week"]
        self.assertEqual(chart.xp[-2:], [(NOW - DAY, 2000), (NOW, 2000)])

        conn = FakeConn(rows(("overall", NOW - DAY, 500)))
        chart = self.collect(conn).charts["overall"]["day"]
        self.assertEqual(chart.xp, [(NOW - DAY, 500), (NOW, 500)])


    def test_missing_data_and_empty_skills(self) -> None:
        # xp only changes twice, but the last 10 days and 30-40 days ago were
        # fetched daily; the 20 days in between were never fetched.
        checkpoints = sorted(
            [NOW - day * DAY for day in range(11)]
            + [NOW - day * DAY for day in range(30, 41)]
        )
        conn = FakeConn(
            rows(
                ("overall", NOW - 20 * DAY, 1000),
                ("overall", NOW, 2000),
            ),
            checkpoints=checkpoints,
        )
        data = self.collect(conn)

        # Idle time with checkpoints is not missing data.
        self.assertEqual(data.charts["overall"]["day"].missing, ())
        self.assertEqual(data.charts["overall"]["week"].missing, ())
        self.assertEqual(
            data.charts["overall"]["month"].missing,
            ((NOW - 30 * DAY, NOW - 10 * DAY),),
        )
        self.assertEqual(
            data.charts["overall"]["year"].missing,
            (
                (NOW - 365 * DAY, NOW - 40 * DAY),
                (NOW - 30 * DAY, NOW - 10 * DAY),
            ),
        )
        self.assertEqual(
            data.charts["overall"]["all"].missing,
            (
                (DATA_START_EPOCH, NOW - 40 * DAY),
                (NOW - 30 * DAY, NOW - 10 * DAY),
            ),
        )

        # No checkpoints at all: the whole window is uncollected.
        empty = self.collect(FakeConn([]))
        self.assertEqual(
            empty.charts["overall"]["day"].missing, ((NOW - DAY, NOW),)
        )

        data = self.collect(FakeConn([]))
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
            collect_interval=6 * 3600,
            generated=generated,
        )

    def test_page_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report_dir = Path(directory)
            reporting.write_player_pages(
                self.conn, report_dir, self.data(), {"alice": 1}, None
            )
            page = report_dir / "players" / "alice.html"
            text = report_dir / "players" / "alice.txt"
            before = page.read_text(encoding="utf-8")
            self.assertIn("first", before)
            self.assertIn("first", text.read_text(encoding="utf-8"))

            written = reporting.write_player_pages(
                self.conn, report_dir, self.data("second"),
                {"alice": 1}, {"alice": 1},
            )
            self.assertEqual(written, 0)
            self.assertEqual(page.read_text(encoding="utf-8"), before)

            written = reporting.write_player_pages(
                self.conn, report_dir, self.data("second"),
                {"alice": 2}, {"alice": 1},
            )
            self.assertEqual(written, 1)
            self.assertIn("second", page.read_text(encoding="utf-8"))
            self.assertIn("second", text.read_text(encoding="utf-8"))

            data = self.data()
            data.players = ()
            reporting.write_player_pages(
                self.conn, report_dir, data, {}, {"alice": 1}
            )
            self.assertFalse(page.exists())
            self.assertFalse(text.exists())


if __name__ == "__main__":
    unittest.main()
