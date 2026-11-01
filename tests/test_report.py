"""Unit tests that do not need PostgreSQL."""

from __future__ import annotations

import unittest

from src.config import SKILLS, SPANS
from src.main import parse_duration
from src.report import (
    Record,
    ReportData,
    _coverage_runs,
    _hours,
    _scan_player_points,
    _years_months,
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
    def scan(self) -> dict[str, list]:
        heaps = {name: [] for name in SPANS}
        _scan_player_points(
            "tester", build_points(), list(SPANS.items()),
            NOW, LOOKBACK, INTERVAL, 5, heaps,
        )
        return heaps

    def test_window_lengths_match_spans(self) -> None:
        for name, span in SPANS.items():
            record = sorted(self.scan()[name], reverse=True)[0]
            self.assertEqual(record.end_epoch - record.start_epoch, span, name)


class RecordWindowTest(unittest.TestCase):
    def test_year_records_exist_when_now_is_unaligned(self) -> None:
        # write_report() passes time.time(), which is never on a checkpoint.
        interval = 86400
        lookback = 365 * 86400
        aligned = (1_800_000_000 // interval) * interval
        now = aligned + 1800
        points = [
            (t, t - aligned + lookback)
            for t in range(aligned - lookback, aligned + 1, interval)
        ]
        heaps = {name: [] for name in SPANS}
        _scan_player_points(
            "tester", points, list(SPANS.items()),
            now, lookback, 6 * 3600, 5, heaps,
        )
        self.assertTrue(heaps["year"])


class ZeroGainTest(unittest.TestCase):
    def test_flat_xp_produces_no_record_rows(self) -> None:
        points = [
            (NOW - LOOKBACK + step * INTERVAL, 500)
            for step in range(LOOKBACK // INTERVAL + 1)
        ]
        heaps = {name: [] for name in SPANS}
        _scan_player_points(
            "flat", points, list(SPANS.items()),
            NOW, LOOKBACK, INTERVAL, 5, heaps,
        )
        self.assertTrue(all(not heap for heap in heaps.values()))


class BestPerPlayerTest(unittest.TestCase):
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
        self.assertEqual(len(heaps["day"]), 1)
        self.assertEqual(heaps["day"][0].gain, 5_000_000)
        self.assertEqual(heaps["day"][0].username, "tester")


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
        self.assertIn("overall", text)
        self.assertLess(text.index("  PAST"), text.index("  BEST"))
        self.assertIn("    Day (24h)", text)
        self.assertIn("    Year (8760h)", text)

    def test_player_names_link_to_highscores(self) -> None:
        data = self.make_data()
        gains = {span: dict(skills) for span, skills in data.gains.items()}
        gains["day"]["overall"] = [("Fuck Jagex", 100)]
        page = render_html(data._replace(gains=gains))
        self.assertIn(
            'href="https://vidyaplace.org/highscores?player=Fuck%20Jagex"',
            page,
        )
        self.assertIn(">Fuck Jagex</a>", page)

    def test_coverage_bar_renders_runs(self) -> None:
        page = render_html(self.make_data())
        self.assertIn('class="coverage"', page)
        self.assertIn("1/2 players (50%)", page)
        self.assertIn('class="zero"', page)

    def test_coverage_uses_candidate_total(self) -> None:
        data = self.make_data()._replace(coverage_total=4)
        page = render_html(data)
        self.assertIn("2/4 players (50%)", page)
        self.assertIn("1/4 players (25%)", page)
        self.assertIn("players: 2 / 4", page)

    def test_best_tables_split_dates(self) -> None:
        page = render_html(self.make_data())
        self.assertIn(">Start date<", page)
        self.assertIn(">End date<", page)
        self.assertIn(">Date<", page)


class CoverageRunTest(unittest.TestCase):
    def test_adjacent_equal_coverage_is_merged(self) -> None:
        coverage = [
            (0, 5), (86400, 5), (172800, 2), (259200, 2), (345600, 0),
        ]
        self.assertEqual(
            _coverage_runs(coverage),
            [(0, 2, 5), (2, 2, 2), (4, 1, 0)],
        )

    def test_no_merge_when_coverage_changes(self) -> None:
        coverage = [(0, 1), (86400, 2), (172800, 1)]
        self.assertEqual(
            _coverage_runs(coverage),
            [(0, 1, 1), (1, 1, 2), (2, 1, 1)],
        )


class DurationFormatTest(unittest.TestCase):
    def test_hours(self) -> None:
        self.assertEqual(_hours(3600), "1 hour")
        self.assertEqual(_hours(21600), "6 hours")
        self.assertEqual(_hours(86400), "24 hours")
        self.assertEqual(_hours(1800), "0.5 hours")

    def test_years_and_months(self) -> None:
        self.assertEqual(_years_months(86400), "1 day")
        self.assertEqual(_years_months(20 * 86400), "20 days")
        self.assertEqual(_years_months(365 * 86400), "1 year")
        self.assertEqual(_years_months((365 + 30) * 86400), "1 year 1 month")
        self.assertEqual(
            _years_months((2 * 365 + 2 * 30) * 86400), "2 years 2 months"
        )


class DurationTest(unittest.TestCase):
    def test_units(self) -> None:
        self.assertEqual(parse_duration("6h"), 21600)
        self.assertEqual(parse_duration("365d"), 31536000)
        self.assertEqual(parse_duration("21600"), 21600)
        with self.assertRaises(Exception):
            parse_duration("nonsense")


if __name__ == "__main__":
    unittest.main()
