"""Database tests. Skipped unless VSCAPE_HCIM_TRACKER_TEST_DSN points at a disposable DB."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from src import main, report, storage
from src.config import SKILLS

DSN = os.environ.get("VSCAPE_HCIM_TRACKER_TEST_DSN")
T1 = 1_700_000_000
T2 = T1 + 86400


def connect_or_skip():
    if not DSN:
        raise unittest.SkipTest("set VSCAPE_HCIM_TRACKER_TEST_DSN to run database tests")
    try:
        conn = storage.connect(DSN)
    except Exception as error:  # noqa: BLE001 - missing server means skip
        raise unittest.SkipTest(f"cannot connect to {DSN}: {error}") from error
    storage.ensure_schema(conn)
    return conn


class DatabaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect_or_skip()
        self.addCleanup(self.conn.close)
        self.conn.execute(
            "TRUNCATE priority_checkpoints, priority_snapshots, "
            "checkpoints, snapshots, runs, players CASCADE"
        )
        self.conn.commit()

    def add_player(self, username: str = "alice", ironman: int = 1) -> None:
        storage.upsert_players(self.conn, [(username, ironman)])

    def add_snapshot(
        self, username: str, skill: str, epoch: int, xp: int
    ) -> None:
        storage.insert_snapshots(
            self.conn,
            [(username, skill, storage.as_datetime(epoch), xp)],
        )


class SchemaTest(DatabaseTest):
    def test_ensure_schema_is_idempotent(self) -> None:
        storage.ensure_schema(self.conn)
        storage.ensure_schema(self.conn)
        rows = self.conn.execute(
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public'
            """
        ).fetchall()
        names = {row[0] for row in rows}
        self.assertTrue(
            {
                "players", "snapshots", "checkpoints", "runs",
                "priority_snapshots", "priority_checkpoints",
            } <= names
        )


class PlayerTest(DatabaseTest):
    def test_upsert_inserts_and_updates(self) -> None:
        storage.upsert_players(self.conn, [("alice", 1), ("bob", 2)])
        storage.upsert_players(self.conn, [("alice", 3)])
        rows = self.conn.execute(
            "SELECT username, ironman FROM players ORDER BY username"
        ).fetchall()
        self.assertEqual([tuple(row) for row in rows], [("alice", 3), ("bob", 2)])

    def test_upsert_refreshes_last_seen(self) -> None:
        self.add_player()
        self.conn.execute(
            "UPDATE players SET last_seen = now() - interval '1 day'"
        )
        self.conn.commit()
        storage.upsert_players(self.conn, [("alice", 1)])
        row = self.conn.execute(
            "SELECT last_seen > now() - interval '1 minute' FROM players"
        ).fetchone()
        self.assertTrue(row[0])

    def test_canonical_username_is_case_insensitive(self) -> None:
        self.add_player("Cojultad")
        self.assertEqual(
            storage.canonical_username(self.conn, "cojultad"), "Cojultad"
        )
        self.assertIsNone(
            storage.canonical_username(self.conn, "nobody")
        )


class SnapshotTest(DatabaseTest):
    def test_insert_is_idempotent(self) -> None:
        self.add_player()
        rows = [
            ("alice", skill, storage.as_datetime(T1), 100)
            for skill in SKILLS[:2]
        ]
        storage.insert_snapshots(self.conn, rows)
        storage.insert_snapshots(self.conn, rows)
        count = self.conn.execute("SELECT count(*) FROM snapshots").fetchone()[0]
        self.assertEqual(count, 2)

    def test_existing_row_is_not_overwritten(self) -> None:
        self.add_player()
        self.add_snapshot("alice", "overall", T1, 100)
        self.add_snapshot("alice", "overall", T1, 999)
        xp = self.conn.execute("SELECT xp FROM snapshots").fetchone()[0]
        self.assertEqual(xp, 100)

    def test_fetched_checkpoint_times_returns_recorded_only(self) -> None:
        self.add_player()
        storage.insert_checkpoints(self.conn, "alice", [T1])
        fetched = storage.fetched_checkpoint_times(
            self.conn, "alice", [T1, T2]
        )
        self.assertEqual(fetched, {T1})

    def test_snapshots_alone_do_not_mark_a_checkpoint(self) -> None:
        self.add_player()
        for skill in SKILLS:
            self.add_snapshot("alice", skill, T1, 10)
        self.assertEqual(
            storage.fetched_checkpoint_times(self.conn, "alice", [T1]), set()
        )

    def test_insert_checkpoints_is_idempotent(self) -> None:
        self.add_player()
        storage.insert_checkpoints(self.conn, "alice", [T1, T2])
        storage.insert_checkpoints(self.conn, "alice", [T1, T2])
        count = self.conn.execute(
            "SELECT count(*) FROM checkpoints"
        ).fetchone()[0]
        self.assertEqual(count, 2)

    def test_latest_xp_by_skill_uses_newest_row(self) -> None:
        self.add_player()
        self.add_snapshot("alice", "overall", T1, 100)
        self.add_snapshot("alice", "overall", T2, 200)
        self.add_snapshot("alice", "attack", T1, 50)
        self.assertEqual(
            storage.latest_xp_by_skill(self.conn, "alice"),
            {"overall": 200, "attack": 50},
        )

    def test_xp_at_or_before_picks_latest_row(self) -> None:
        self.add_player("alice")
        self.add_player("bob")
        self.add_snapshot("alice", "overall", T1, 100)
        self.add_snapshot("alice", "overall", T2, 200)
        self.add_snapshot("bob", "overall", T2, 50)
        self.assertEqual(
            storage.xp_at_or_before(self.conn, "overall", T2 - 1),
            {"alice": 100},
        )
        self.assertEqual(
            storage.xp_at_or_before(self.conn, "overall", T2),
            {"alice": 200, "bob": 50},
        )

    def test_negative_xp_is_rejected(self) -> None:
        self.add_player()
        with self.assertRaises(Exception):
            self.add_snapshot("alice", "overall", T1, -1)
        self.conn.rollback()

    def test_unknown_player_is_rejected(self) -> None:
        with self.assertRaises(Exception):
            self.add_snapshot("ghost", "overall", T1, 1)
        self.conn.rollback()


class PriorityStorageTest(DatabaseTest):
    def test_insert_is_idempotent(self) -> None:
        self.add_player("alice")
        row = ("alice", "overall", storage.as_datetime(T1), 100)
        storage.insert_priority_snapshots(self.conn, [row, row])
        rows = self.conn.execute(
            "SELECT count(*) FROM priority_snapshots"
        ).fetchone()
        self.assertEqual(rows[0], 1)

    def test_latest_and_prune(self) -> None:
        self.add_player("alice")
        storage.insert_priority_snapshots(self.conn, [
            ("alice", "overall", storage.as_datetime(T1), 100),
            ("alice", "overall", storage.as_datetime(T2), 200),
        ])
        storage.insert_priority_checkpoints(self.conn, "alice", [T1, T2])
        self.assertEqual(
            storage.latest_priority_xp_by_skill(self.conn, "alice"),
            {"overall": 200},
        )

        storage.prune_priority_data(self.conn, T2)
        snapshots = self.conn.execute(
            "SELECT count(*) FROM priority_snapshots"
        ).fetchone()[0]
        checkpoints = self.conn.execute(
            "SELECT count(*) FROM priority_checkpoints"
        ).fetchone()[0]
        self.assertEqual((snapshots, checkpoints), (1, 1))


class CoverageTest(DatabaseTest):
    def test_counts_distinct_players_per_bucket(self) -> None:
        self.add_player("alice")
        self.add_player("bob")
        storage.insert_checkpoints(self.conn, "alice", [T1, T2])
        storage.insert_checkpoints(self.conn, "bob", [T1])

        buckets = storage.coverage_buckets(self.conn, T1, T2, 2)
        self.assertEqual(len(buckets), 2)
        self.assertEqual(buckets[0], (T1, 2))
        self.assertEqual(buckets[1][1], 1)

    def test_empty_table_is_all_zero(self) -> None:
        buckets = storage.coverage_buckets(self.conn, T1, T2, 3)
        self.assertEqual([count for _, count in buckets], [0, 0, 0])


class RunTest(DatabaseTest):
    def test_run_lifecycle(self) -> None:
        run_id = storage.start_run(self.conn, 86400, 21600, 31_536_000, 2)
        row = self.conn.execute(
            "SELECT finished_at, interval_seconds, collect_interval_seconds, "
            "lookback_seconds, player_count, candidate_count, "
            "failure_count, status FROM runs WHERE id = %s",
            (run_id,),
        ).fetchone()
        self.assertEqual(
            tuple(row),
            (None, 86400, 21600, 31_536_000, 2, 2, 0, "running"),
        )
        storage.finish_run(self.conn, run_id, 42, 3, "partial", 1)
        row = self.conn.execute(
            "SELECT request_count, failure_count, status, player_count, "
            "finished_at IS NOT NULL FROM runs WHERE id = %s",
            (run_id,),
        ).fetchone()
        self.assertEqual(tuple(row), (42, 3, "partial", 1, True))


class ReportTest(DatabaseTest):
    def test_report_ranks_gains(self) -> None:
        now = 1_800_000_000
        self.add_player("alice")
        self.add_player("bob")
        for username, before, after in (
            ("alice", 0, 1000),
            ("bob", 0, 500),
        ):
            self.add_snapshot(username, "overall", now - 86400, before)
            self.add_snapshot(username, "overall", now, after)

        text = report.build_report(
            self.conn, now, 86400, 31_536_000, 5, 2, 3600
        )
        overall = text.split("overall", 1)[1]
        day = overall.split("    Week", 1)[0]
        self.assertIn("1. alice", day)
        self.assertIn("2. bob", day)
        self.assertLess(day.index("1. alice"), day.index("2. bob"))

    def test_zero_gain_players_are_omitted(self) -> None:
        now = 1_800_000_000
        self.add_player("alice")
        self.add_player("bob")
        self.add_snapshot("alice", "overall", now - 86400, 0)
        self.add_snapshot("alice", "overall", now, 1000)
        self.add_snapshot("bob", "overall", now - 86400, 500)
        self.add_snapshot("bob", "overall", now, 500)

        text = report.build_report(
            self.conn, now, 86400, 31_536_000, 5, 2, 21600
        )
        self.assertIn("alice", text)
        self.assertNotIn("bob", text)

    def test_write_report_keeps_only_latest_files(self) -> None:
        now = 1_800_000_000
        self.add_player("alice")
        self.add_snapshot("alice", "overall", now - 86400, 0)
        self.add_snapshot("alice", "overall", now, 1000)

        with tempfile.TemporaryDirectory() as directory:
            old = Path(directory, "report_20000101T000000Z.txt")
            old_html = Path(directory, "report_20000101T000000Z.html")
            stale = Path(directory, ".latest.html.123.tmp")
            for path in (old, old_html, stale):
                path.write_text("old", encoding="utf-8")

            main.write_report(
                self.conn, Path(directory), now, 86400, 31_536_000, 5, 1,
                21600,
            )
            names = sorted(p.name for p in Path(directory).iterdir())
            self.assertEqual(
                names,
                ["latest.html", "latest.txt", "players", "priority"],
            )
            self.assertTrue(Path(directory, "priority", "index.html").exists())

    def test_write_report_creates_latest_files(self) -> None:
        now = 1_800_000_000
        self.add_player("alice")
        self.add_snapshot("alice", "overall", now - 86400, 0)
        self.add_snapshot("alice", "overall", now, 1000)

        with tempfile.TemporaryDirectory() as directory:
            main.write_report(
                self.conn, Path(directory), now, 86400, 31_536_000, 5, 1,
                21600,
            )
            html_path = Path(directory, "latest.html")
            text_path = Path(directory, "latest.txt")
            self.assertTrue(html_path.exists())
            self.assertTrue(text_path.exists())
            self.assertTrue(
                Path(directory, "priority", "index.html").exists()
            )
            page = html_path.read_text(encoding="utf-8")
            self.assertIn("alice", page)
            player_page = Path(directory, "players", "alice.html")
            self.assertTrue(player_page.exists())
            player_html = player_page.read_text(encoding="utf-8")
            self.assertIn(
                "https://vidyaplace.org/highscores?player=alice", player_html
            )
            self.assertIn("Current xp", player_html)


if __name__ == "__main__":
    unittest.main()
