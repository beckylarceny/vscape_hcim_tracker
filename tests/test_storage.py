"""Database tests. Skipped unless VSCAPE_TRACKER_TEST_DSN points at a disposable DB."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from src.common import storage
from src.common.utility import SKILLS, build_dsn
from src.web import reporting
from src.web.reports import general as report

DSN = os.environ.get("VSCAPE_TRACKER_TEST_DSN") or build_dsn(
    "xp_tracker_test"
)
T1 = 1_700_000_000
T2 = T1 + 86400


def connect_or_skip():
    if not DSN:
        raise unittest.SkipTest("set VSCAPE_TRACKER_TEST_DSN to run database tests")
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
    def test_upsert_and_canonical(self) -> None:
        storage.upsert_players(self.conn, [("alice", 1), ("bob", 2)])
        storage.upsert_players(self.conn, [("alice", 3)])
        rows = self.conn.execute(
            "SELECT username, ironman FROM players ORDER BY username"
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in rows], [("alice", 3), ("bob", 2)]
        )

        self.conn.execute(
            "UPDATE players SET last_seen = now() - interval '1 day'"
        )
        self.conn.commit()
        storage.upsert_players(self.conn, [("alice", 3)])
        row = self.conn.execute(
            "SELECT last_seen > now() - interval '1 minute' FROM players "
            "WHERE username = 'alice'"
        ).fetchone()
        self.assertTrue(row[0])

        self.add_player("Cojultad")
        self.assertEqual(
            storage.canonical_username(self.conn, "cojultad"), "Cojultad"
        )
        self.assertIsNone(storage.canonical_username(self.conn, "nobody"))


class SnapshotTest(DatabaseTest):
    def test_snapshot_checkpoint_and_lookups(self) -> None:
        self.add_player()
        rows = [
            ("alice", skill, storage.as_datetime(T1), 100)
            for skill in SKILLS[:2]
        ]
        storage.insert_snapshots(self.conn, rows)
        storage.insert_snapshots(self.conn, rows)
        count = self.conn.execute("SELECT count(*) FROM snapshots").fetchone()[0]
        self.assertEqual(count, 2)

        self.add_snapshot("alice", "overall", T1, 999)
        self.assertEqual(
            self.conn.execute(
                "SELECT xp FROM snapshots WHERE skill = 'overall'"
            ).fetchone()[0],
            100,
        )

        storage.insert_checkpoints(self.conn, "alice", [T1, T2])
        storage.insert_checkpoints(self.conn, "alice", [T1, T2])
        count = self.conn.execute(
            "SELECT count(*) FROM checkpoints"
        ).fetchone()[0]
        self.assertEqual(count, 2)
        self.assertEqual(
            storage.fetched_checkpoint_times(self.conn, "alice", [T1, T2]),
            {T1, T2},
        )

        self.add_player("alice")
        self.add_player("bob")
        self.add_snapshot("alice", "overall", T1, 100)
        self.add_snapshot("alice", "overall", T2, 200)
        self.add_snapshot("alice", "attack", T1, 50)
        self.add_snapshot("bob", "overall", T2, 50)
        self.assertEqual(
            storage.latest_xp_by_skill(self.conn, "alice"),
            {"overall": 200, "attack": 50},
        )
        self.assertEqual(
            storage.top_players_by_xp(self.conn, "overall", 2),
            ["alice", "bob"],
        )
        self.assertEqual(
            storage.top_players_by_xp(self.conn, "overall", 0), []
        )
        self.assertEqual(
            storage.xp_at_or_before(self.conn, "overall", T2 - 1),
            {"alice": 100},
        )
        self.assertEqual(
            storage.xp_at_or_before(self.conn, "overall", T2),
            {"alice": 200, "bob": 50},
        )

        storage.insert_checkpoints(self.conn, "alice", [T1, T2])
        storage.insert_checkpoints(self.conn, "bob", [T1])
        buckets = storage.coverage_buckets(self.conn, T1, T2, 2)
        self.assertEqual(buckets[0], storage.CoverageSlice(T1, 2, 2))
        self.assertEqual(buckets[1], storage.CoverageSlice(T2, 1, 1))

        by_skill = storage.xp_by_skill_at_or_before(self.conn, T2)
        self.assertEqual(by_skill["overall"], {"alice": 200, "bob": 50})
        self.assertEqual(by_skill["attack"], {"alice": 50})
        self.assertEqual(
            storage.latest_xp_by_skill_many(self.conn, ["alice", "bob"]),
            {
                "alice": {"overall": 200, "attack": 50},
                "bob": {"overall": 50},
            },
        )
        self.assertEqual(storage.latest_xp_by_skill_many(self.conn, []), {})


class PriorityStorageTest(DatabaseTest):
    def test_priority_insert_latest_prune(self) -> None:
        self.add_player("alice")
        self.add_player("bob")
        row = ("alice", "overall", storage.as_datetime(T1), 100)
        storage.insert_priority_snapshots(self.conn, [row, row])
        storage.insert_priority_snapshots(self.conn, [
            ("alice", "overall", storage.as_datetime(T2), 200),
            ("bob", "overall", storage.as_datetime(T2), 20),
        ])
        storage.insert_priority_checkpoints(self.conn, "alice", [T1, T2])
        self.assertEqual(
            storage.latest_priority_xp_by_skill(self.conn, "alice"),
            {"overall": 200},
        )
        self.assertEqual(
            storage.priority_snapshot_players(self.conn), ["alice", "bob"]
        )

        storage.prune_priority_data(self.conn, T2)
        snapshots = self.conn.execute(
            "SELECT count(*) FROM priority_snapshots"
        ).fetchone()[0]
        checkpoints = self.conn.execute(
            "SELECT count(*) FROM priority_checkpoints"
        ).fetchone()[0]
        self.assertEqual((snapshots, checkpoints), (2, 1))


class ReportTest(DatabaseTest):
    def test_watermarks_and_report_output(self) -> None:
        self.add_player("alice")
        self.add_snapshot("alice", "overall", T1, 100)
        storage.insert_checkpoints(self.conn, "alice", [T1])
        before = reporting._player_watermarks(self.conn)

        storage.insert_checkpoints(self.conn, "alice", [T2])
        self.assertEqual(reporting._player_watermarks(self.conn), before)

        self.add_snapshot("alice", "overall", T2, 200)
        after = reporting._player_watermarks(self.conn)
        self.assertNotEqual(after, before)
        self.assertEqual(after["alice"][0], 2)

        storage.insert_priority_snapshots(
            self.conn, [("alice", "overall", storage.as_datetime(T1), 300)]
        )
        priority = reporting._player_watermarks(self.conn)
        self.assertNotEqual(priority, after)
        self.assertEqual(priority["alice"][2], 1)

        now = 1_800_000_000
        self.add_player("alice")
        self.add_player("bob")
        for username, before, after in (
            ("alice", 0, 1000),
            ("bob", 500, 500),
        ):
            self.add_snapshot(username, "overall", now - 86400, before)
            self.add_snapshot(username, "overall", now, after)

        text = report.build_report(
            self.conn, now, 86400, 31_536_000, 5, 2, 3600
        )
        overall = text.split("overall", 1)[1]
        day = overall.split("    Week", 1)[0]
        self.assertIn("1. alice", day)
        self.assertNotIn("bob", text)

        with tempfile.TemporaryDirectory() as directory:
            old = Path(directory, "report_20000101T000000Z.txt")
            old_html = Path(directory, "report_20000101T000000Z.html")
            stale = Path(directory, ".general.html.123.tmp")
            for path in (old, old_html, stale):
                path.write_text("old", encoding="utf-8")

            reporting.write_report(
                self.conn, Path(directory), now, 86400, 31_536_000, 5, 2,
                21600,
            )
            names = sorted(p.name for p in Path(directory).iterdir())
            self.assertEqual(
                names,
                [
                    "general.html", "index.html", "latest.txt", "players",
                    "priority", "status.html", "status.txt",
                ],
            )
            page = Path(directory, "general.html").read_text(encoding="utf-8")
            self.assertIn("alice", page)
            player_page = Path(directory, "players", "alice.html")
            player_html = player_page.read_text(encoding="utf-8")
            self.assertIn(
                "https://vidyaplace.org/highscores?player=alice", player_html
            )
            self.assertIn("Current xp", player_html)
            self.assertTrue(
                Path(directory, "players", "alice.txt").exists()
            )
            self.assertTrue(
                Path(directory, "priority", "index.html").exists()
            )


if __name__ == "__main__":
    unittest.main()
