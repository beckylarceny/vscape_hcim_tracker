"""Unit tests that do not need PostgreSQL."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from src.common.utility import SKILLS, SPANS, parse_duration
from src.web import reporting
from src.web.format import hours as _hours
from src.web.format import years_months as _years_months
from src.web.report import (
    Record,
    ReportData,
    _scan_player_points,
    render_html,
    render_text,
)

INTERVAL = 6 * 3600
NOW = (1_800_000_000 // INTERVAL) * INTERVAL
LOOKBACK = 365 * 86400


def build_points(burst_after_seconds: int = 100 * 86400) -> list[tuple[int, int]]:
    points: list[tuple[int, int]] = []
    xp = 0
    timestamp = NOW - LOOKBACK
    burst_start = NOW - burst_after_seconds
    while timestamp <= NOW:
        points.append((timestamp, xp))
        if burst_start <= timestamp < burst_start + 86400:
            xp += 500_000
        xp += 1_000
        timestamp += INTERVAL
    return points


class RecordScanTest(unittest.TestCase):
    def test_window_lengths_and_unaligned_now(self) -> None:
        heaps = {name: [] for name in SPANS}
        _scan_player_points(
            "tester", build_points(), list(SPANS.items()),
            NOW, LOOKBACK, INTERVAL, 5, heaps,
        )
        for name, span in SPANS.items():
            record = sorted(heaps[name], reverse=True)[0]
            self.assertEqual(record.end_epoch - record.start_epoch, span, name)

        # write_report() passes time.time(), which is never on a checkpoint.
        interval = 86400
        aligned = (1_800_000_000 // interval) * interval
        points = [
            (t, t - aligned + LOOKBACK)
            for t in range(aligned - LOOKBACK, aligned + 1, interval)
        ]
        heaps = {name: [] for name in SPANS}
        _scan_player_points(
            "tester", points, list(SPANS.items()),
            aligned + 1800, LOOKBACK, 6 * 3600, 5, heaps,
        )
        self.assertTrue(heaps["year"])

    def test_player_appears_once_with_their_best_gain(self) -> None:
        points: list[tuple[int, int]] = []
        xp = 0
        for step in range(400):
            points.append((NOW - 400 * 86400 + step * 86400, xp))
            if step == 200:
                xp += 5_000_000
            elif step % 10 == 0:
                xp += 1_000

        heaps = {name: [] for name in SPANS}
        _scan_player_points(
            "tester", points, list(SPANS.items()),
            NOW, LOOKBACK, 86400, 5, heaps,
        )

        for name in SPANS:
            self.assertLessEqual(len(heaps[name]), 1, name)
        self.assertEqual(heaps["day"][0].gain, 5_000_000)
        self.assertEqual(heaps["day"][0].username, "tester")

        flat = [
            (NOW - LOOKBACK + step * INTERVAL, 500)
            for step in range(LOOKBACK // INTERVAL + 1)
        ]
        heaps = {name: [] for name in SPANS}
        _scan_player_points(
            "flat", flat, list(SPANS.items()),
            NOW, LOOKBACK, INTERVAL, 5, heaps,
        )
        self.assertTrue(all(not heap for heap in heaps.values()))


class HtmlReportTest(unittest.TestCase):
    def make_data(self) -> ReportData:
        gains = {name: {skill: [] for skill in SKILLS} for name in SPANS}
        records = {name: {skill: [] for skill in SKILLS} for name in SPANS}
        gains["day"]["overall"] = [("alice", 1234)]
        records["week"]["overall"] = [
            Record(gain=999, end_epoch=NOW, username="<bob>",
                   start_epoch=NOW - SPANS["week"])
        ]
        return ReportData(
            generated="2026-01-01 00:00 UTC",
            now_epoch=NOW,
            interval=86400,
            collect_interval=21600,
            lookback=365 * 86400,
            player_count=2,
            coverage_total=2,
            gains=gains,
            records=records,
            coverage=[(NOW - 2 * 86400, 2), (NOW - 86400, 1), (NOW, 0)],
        )

    def test_escapes_player_names_and_formats_gains(self) -> None:
        page = render_html(self.make_data())
        self.assertIn("alice", page)
        self.assertIn("1,234", page)
        self.assertIn("&lt;bob&gt;", page)
        self.assertNotIn("<bob>", page)

    def test_text_report_groups_by_skill_then_past_best(self) -> None:
        text = render_text(self.make_data())
        self.assertTrue(text.startswith("VIDYASCAPE HCIM XP TRACKER REPORT\n"))
        self.assertIn("overall", text)
        self.assertLess(text.index("  PAST"), text.index("  BEST"))
        self.assertIn("    Day (24h)", text)
        self.assertIn("    Year (8760h)", text)
        self.assertIn("players logged:", text)
        self.assertIn("backfill interval:", text)
        self.assertIn("live interval:", text)
        self.assertIn("priority interval:", text)
        self.assertIn("10 minutes", text)
        self.assertIn("1. alice", text)

    def test_player_names_link_to_player_pages_and_finder(self) -> None:
        data = self.make_data()
        gains = {span: dict(skills) for span, skills in data.gains.items()}
        gains["day"]["overall"] = [("Fuck Jagex", 100)]
        page = render_html(
            data._replace(gains=gains, players=("Alice", "Fuck Jagex"))
        )
        self.assertIn('href="players/Fuck_Jagex.html"', page)
        self.assertIn(">Fuck Jagex</a>", page)
        self.assertIn('<option value="Alice">', page)
        self.assertIn('"fuck jagex": "players/Fuck_Jagex.html"', page)
        self.assertIn("window.open(url,'_blank','noopener')", page)

    def test_rankings_page_has_only_generated_metadata(self) -> None:
        data = self.make_data()._replace(coverage_total=4)
        page = render_html(data)
        self.assertIn(
            "<title>general tracking - vidyascape hcim xp tracker</title>", page
        )
        self.assertIn("generated at: 2026-01-01 00:00 UTC", page)
        self.assertNotIn("players logged", page)
        self.assertNotIn("data coverage:", page)
        self.assertNotIn('class="coverage"', page)
        self.assertEqual(page.count("generated at:"), 1)
        self.assertIn('href="status.html"', page)
        self.assertIn('class="rankings wide"', page)

    def test_backlinks_sit_above_the_title(self) -> None:
        page = render_html(self.make_data())
        backlinks = (
            '<div class="backlinks"><p class="sub">'
            '<a href="priority/index.html">priority tracking</a> · '
            '<a href="status.html">status</a></p></div>'
        )
        self.assertIn(backlinks, page)
        self.assertLess(page.index('<div class="backlinks">'), page.index("<h1>"))

        text_version = '<p class="sub"><a href="latest.txt">text version</a></p>'
        self.assertIn(text_version, page)
        self.assertLess(page.index("<h1>"), page.index(text_version))
        self.assertLess(page.index(text_version), page.index("generated at"))

        self.assertNotIn("text report", page)
        self.assertNotIn(
            "</h1>\n<p class=\"sub\"><a href=\"priority/index.html\">", page
        )
        self.assertNotIn(
            "</h1>\n<p class=\"sub\"><a href=\"status.html\">", page
        )
        self.assertIn(
            ".backlinks{margin:-2rem 0 1.75rem;text-align:center}", page
        )
        self.assertIn(
            ".backlinks .sub{margin:0;padding:0;border:0;font-size:.9rem}",
            page,
        )

    def test_version_footer_and_text_line(self) -> None:
        import tempfile
        from pathlib import Path

        from src.web.report import read_version

        version = "software version abc1234, updated 2026-11-01 13:00 UTC"
        data = self.make_data()._replace(version=version)
        page = render_html(data)
        self.assertIn(f'<footer class="version">{version}</footer>', page)
        self.assertLess(
            page.index('class="to-top"'), page.index(version)
        )
        text = render_text(data)
        self.assertTrue(text.endswith(f"\n\n{version}\n"))

        with tempfile.TemporaryDirectory() as directory:
            report_dir = Path(directory)
            self.assertEqual(read_version(report_dir), "software version unknown")
            (report_dir / "version").write_text(
                "abc1234 2026-11-01 13:00 UTC\n", encoding="utf-8"
            )
            self.assertEqual(read_version(report_dir), version)

    def test_skills_are_tabs(self) -> None:
        page = render_html(self.make_data())
        self.assertIn(
            '<div class="tabs" id="skill-tabs" role="tablist">', page
        )
        self.assertIn('aria-selected="true">overall</button>', page)
        self.assertIn("function showSkill(name)", page)
        self.assertIn(
            '<section id="skill-overall" class="tab-panel" '
            'role="tabpanel" aria-labelledby="tab-overall">',
            page,
        )
        self.assertIn(
            '<section id="skill-attack" class="tab-panel" '
            'role="tabpanel" aria-labelledby="tab-attack" hidden>',
            page,
        )
        self.assertNotIn('<a href="#skill-overall">', page)


class ReportCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        reporting._REPORT_CACHE.clear()
        self.addCleanup(reporting._REPORT_CACHE.clear)

    def test_ranking_data_is_reused_until_table_counts_change(self) -> None:
        conn = MagicMock()
        conn.execute.return_value.fetchone.side_effect = [
            (371, 400, 600),
            (371, 400, 600),
            (371, 401, 600),
        ]
        data = ReportData(
            generated="2026-01-01 00:00 UTC",
            now_epoch=NOW,
            interval=86400,
            collect_interval=21600,
            lookback=LOOKBACK,
            player_count=371,
            coverage_total=371,
            gains={},
            records={},
            coverage=[],
        )
        with patch.object(
            reporting.report, "build_report_data", return_value=data
        ) as build:
            first = reporting.report_data(
                conn, NOW, 86400, LOOKBACK, 5, 371, 21600, 371
            )
            second = reporting.report_data(
                conn, NOW + 60, 86400, LOOKBACK, 5, 371, 21600, 371
            )
            third = reporting.report_data(
                conn, NOW + 120, 86400, LOOKBACK, 5, 371, 21600, 371
            )

        self.assertEqual(build.call_count, 2)
        self.assertEqual(first.now_epoch, NOW)
        self.assertEqual(second.now_epoch, NOW + 60)


class DurationTest(unittest.TestCase):
    def test_duration_parse_and_format(self) -> None:
        self.assertEqual(_hours(3600), "1 hour")
        self.assertEqual(_hours(21600), "6 hours")
        self.assertEqual(_hours(1800), "0.5 hours")
        self.assertEqual(_years_months(86400), "1 day")
        self.assertEqual(_years_months(365 * 86400), "1 year")
        self.assertEqual(_years_months((365 + 30) * 86400), "1 year 1 month")
        self.assertEqual(_years_months((2 * 365 + 2 * 30) * 86400),
                         "2 years 2 months")
        self.assertEqual(parse_duration("6h"), 21600)
        self.assertEqual(parse_duration("365d"), 31536000)
        self.assertEqual(parse_duration("21600"), 21600)
        with self.assertRaises(Exception):
            parse_duration("nonsense")


if __name__ == "__main__":
    unittest.main()
