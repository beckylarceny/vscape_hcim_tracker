"""Unit tests for the collection loop and checkpoint grid."""

from __future__ import annotations

import yaml
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src import api, main, storage
from src.utility import (
    DATA_START_EPOCH,
    HCIM,
    CONFIG_DIR_ENV,
    SKILLS,
    build_dsn,
    parse_priority_players,
)

COLLECT = 6 * 3600
DAY = 24 * 3600
NOW = 1_800_000_000
T1 = NOW - 6 * COLLECT
T2 = NOW - 5 * COLLECT


def xp(amount: int) -> dict[str, int]:
    return {skill: amount for skill in SKILLS}


class GridTest(unittest.TestCase):
    def test_backfill_uses_day_grid_and_recent_retry_window(self) -> None:
        grid = main.build_grid(
            NOW, DAY, COLLECT, 365 * DAY, 7 * DAY, no_backfill=False
        )
        current = (NOW // COLLECT) * COLLECT
        recent = {current - step * COLLECT for step in range(28)}
        self.assertEqual(grid, sorted(grid, reverse=True))
        self.assertEqual(grid[0], current)
        self.assertIn((NOW // DAY) * DAY, grid)
        self.assertTrue(recent <= set(grid))

    def test_no_backfill_only_covers_retry_window(self) -> None:
        grid = main.build_grid(
            NOW, DAY, COLLECT, 365 * DAY, 7 * DAY, no_backfill=True
        )
        current = (NOW // COLLECT) * COLLECT
        self.assertEqual(
            set(grid), {current - step * COLLECT for step in range(28)}
        )
        self.assertGreaterEqual(min(grid), NOW - 7 * DAY - COLLECT)

        later = main.build_grid(
            NOW + COLLECT, DAY, COLLECT, 365 * DAY, 7 * DAY, True
        )
        self.assertIn(current, later)


class RateLimiterTest(unittest.TestCase):
    def test_spaces_calls_and_zero_is_unlimited(self) -> None:
        with patch.object(
            main.time, "monotonic", side_effect=[0.0, 0.1]
        ), patch.object(main.time, "sleep") as sleep:
            limiter = main.RateLimiter(4)
            limiter.wait()
            limiter.wait()
        self.assertEqual(sleep.call_args_list[0].args[0], 0.15)

        with patch.object(main.time, "sleep") as sleep:
            main.RateLimiter(0).wait()
        sleep.assert_not_called()


class LookbackTest(unittest.TestCase):
    def test_default_starts_at_the_fixed_data_epoch(self) -> None:
        now = DATA_START_EPOCH + 100 * DAY
        self.assertEqual(main.effective_lookback(now, None), 100 * DAY)
        self.assertEqual(
            main.effective_lookback(now, 365 * DAY), 100 * DAY
        )
        self.assertEqual(
            main.effective_lookback(now, 10 * DAY), 10 * DAY
        )
        self.assertEqual(
            main.effective_lookback(DATA_START_EPOCH - 5, None), 0
        )


class CollectAllTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = MagicMock()
        self.conn.close = MagicMock()
        self.limiter = main.RateLimiter(0)
        self.calls: list[tuple[str, int]] = []

    def run_collect(
        self, players, grid, fetch_xp, fetch_gains,
        done=None, last=None,
    ):
        def gains(name, timestamp, limiter):
            self.calls.append((name, timestamp))
            return (name, fetch_gains(name, timestamp))

        with patch.object(main.storage, "connect", return_value=self.conn), \
                patch.object(main.storage, "ensure_schema"), \
                patch.object(main.storage, "upsert_players") as upsert, \
                patch.object(
                    main.storage, "checkpoint_times_by_player",
                    return_value=dict(done or {}),
                ), \
                patch.object(
                    main.storage, "latest_xp_by_skill",
                    side_effect=lambda conn, name: dict((last or {}).get(name, {})),
                ), \
                patch.object(
                    main.storage, "insert_snapshots"
                ) as insert, \
                patch.object(
                    main.storage, "insert_checkpoint_pairs"
                ) as pairs, \
                patch.object(
                    main.api, "fetch_player_xp",
                    side_effect=lambda name, wait=None: fetch_xp(name),
                ), \
                patch.object(main, "_fetch_gains", side_effect=gains):
            stats = main.collect_all(
                "dsn", list(players), list(grid), 1, self.limiter
            )
        return stats, upsert, insert, pairs

    def test_interval_major_newest_first(self) -> None:
        stats, upsert, insert, pairs = self.run_collect(
            ["alice", "bob"],
            [T2, T1],
            lambda name: (HCIM, xp(1000)),
            lambda name, timestamp: xp(10),
        )
        self.assertEqual(stats["players"], 2)
        self.assertEqual(stats["rows"], 2 * len(SKILLS))
        upsert.assert_called_once_with(
            self.conn, [("alice", HCIM), ("bob", HCIM)]
        )
        self.assertEqual(
            self.calls,
            [("alice", T2), ("bob", T2), ("alice", T1), ("bob", T1)],
        )
        self.assertEqual(
            [call.args[1] for call in pairs.call_args_list],
            [[("alice", T2), ("bob", T2)], [("alice", T1), ("bob", T1)]],
        )

    def test_verification_and_failures_are_isolated(self) -> None:
        def fetch_xp(name):
            if name == "bob":
                return None
            if name == "carol":
                raise api.ApiError("503")
            if name == "dave":
                return (1, xp(1000))
            return (HCIM, xp(1000))

        stats, upsert, insert, pairs = self.run_collect(
            ["alice", "bob", "carol", "dave"],
            [T2],
            fetch_xp,
            lambda name, timestamp: xp(10),
        )
        self.assertEqual(stats["players"], 1)
        self.assertEqual(stats["failures"], 2)
        upsert.assert_called_once_with(self.conn, [("alice", HCIM)])
        self.assertEqual(self.calls, [("alice", T2)])
        self.assertEqual(pairs.call_args.args[1], [("alice", T2)])

    def test_unchanged_xp_is_not_stored(self) -> None:
        stats, _, insert, pairs = self.run_collect(
            ["alice"],
            [T2],
            lambda name: (HCIM, xp(1000)),
            lambda name, timestamp: xp(0),
            last={"alice": xp(1000)},
        )
        self.assertEqual(stats["rows"], 0)
        insert.assert_not_called()
        self.assertEqual(pairs.call_args.args[1], [("alice", T2)])

        stats, _, insert, _ = self.run_collect(
            ["alice"],
            [T2],
            lambda name: (HCIM, xp(1000)),
            lambda name, timestamp: {**xp(0), "overall": 5},
            last={"alice": xp(1000)},
        )
        self.assertEqual(stats["rows"], 1)
        self.assertEqual(
            [row[1] for row in insert.call_args.args[1]], ["overall"]
        )

    def test_checkpoint_failure_is_retried_on_the_next_run(self) -> None:
        def gains(name, timestamp):
            if timestamp == T2:
                raise api.ApiError("503")
            return xp(10)

        stats, _, insert, pairs = self.run_collect(
            ["alice"],
            [T2, T1],
            lambda name: (HCIM, xp(1000)),
            gains,
        )
        self.assertEqual(stats["failures"], 1)
        self.assertEqual(
            self.calls, [("alice", T2), ("alice", T1)]
        )
        self.assertEqual(pairs.call_args.args[1], [("alice", T1)])

    def test_already_fetched_timestamps_are_skipped(self) -> None:
        stats, _, _, pairs = self.run_collect(
            ["alice", "bob"],
            [T2],
            lambda name: (HCIM, xp(1000)),
            lambda name, timestamp: xp(10),
            done={"alice": {T2}},
        )
        self.assertEqual(self.calls, [("bob", T2)])
        self.assertEqual(pairs.call_args.args[1], [("bob", T2)])


class ModeTest(unittest.TestCase):
    def test_collect_only_run_skips_report_generation(self) -> None:
        conn = MagicMock()
        args = SimpleNamespace(
            interval=86400,
            collect_interval=21600,
            lookback=365 * 86400,
            retry_window=7 * 86400,
            no_backfill=True,
            report_only=False,
            collect_only=True,
            report_dir="reports",
            top=5,
            workers=2,
            rate=4,
            limit=0,
        )
        totals = {"requests": 1, "rows": 1, "failures": 0, "players": 1}
        with patch.object(main.storage, "connect", return_value=conn), \
                patch.object(main.storage, "ensure_schema"), \
                patch.object(
                    main.api, "list_hcim_players", return_value={"a": 1}
                ), \
                patch.object(main, "collect_all", return_value=totals), \
                patch.object(main.storage, "start_run", return_value=1), \
                patch.object(main.storage, "finish_run") as finish, \
                patch.object(main, "write_report") as write:
            main.run_once(args, "dsn")

        write.assert_not_called()
        finish.assert_called_once()


class DsnTest(unittest.TestCase):
    def test_dsn_precedence_and_encoding(self) -> None:
        with patch.dict(
            os.environ,
            {"VSCAPE_HCIM_TRACKER_DSN": "postgresql:///x"},
            clear=True,
        ):
            self.assertEqual(build_dsn(), "postgresql:///x")

        with patch.dict(
            os.environ,
            {
                "PGHOST": "db",
                "PGPORT": "5432",
                "PGUSER": "u",
                "PGPASSWORD": "p@ss:word",
                "PGDATABASE": "xp_tracker",
            },
            clear=True,
        ):
            self.assertEqual(
                build_dsn(),
                "postgresql://u:p%40ss%3Aword@db:5432/xp_tracker",
            )

        with patch.dict(
            os.environ,
            {
                "VSCAPE_HCIM_TRACKER_DSN": "postgresql:///main",
                "PGHOST": "db",
                "PGUSER": "u",
                "PGPASSWORD": "p@ss",
                "PGDATABASE": "main",
            },
            clear=True,
        ):
            self.assertEqual(
                build_dsn("xp_tracker_test"),
                "postgresql://u:p%40ss@db:5432/xp_tracker_test",
            )

        # db.yaml supplies the defaults when the PG* variables are absent.
        with patch.dict(
            os.environ,
            {"PGHOST": "db", "PGPASSWORD": "p"},
            clear=True,
        ):
            self.assertEqual(
                build_dsn(),
                "postgresql://xp_tracker:p@db:5432/xp_tracker",
            )


class PriorityListTest(unittest.TestCase):
    def test_cli_values_are_split_and_deduped(self) -> None:
        self.assertEqual(
            parse_priority_players(["a,b", " c ", "a"]),
            ("a", "b", "c"),
        )
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                parse_priority_players(["alice,bob"]),
                ("alice", "bob"),
            )

    def test_environment_then_config_then_default(self) -> None:
        with patch.dict(
            os.environ,
            {"VSCAPE_HCIM_TRACKER_PRIORITY_PLAYERS": "x, y"},
        ):
            self.assertEqual(parse_priority_players(None), ("x", "y"))

        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(parse_priority_players(None), ())

        with patch.dict(
            os.environ,
            {CONFIG_DIR_ENV: "/nonexistent/config-dir"},
            clear=True,
        ):
            self.assertEqual(parse_priority_players(None), ())

    def test_collector_json_priority_players(self) -> None:
        cases = (
            ({"priority": {"players": ["alice", "bob", "alice"]}},
             ("alice", "bob")),
            ({"priority": {"players": []}}, ()),
            ({"priority": {}}, ()),
        )
        for payload, expected in cases:
            with tempfile.TemporaryDirectory() as directory:
                Path(directory, "collector.yaml").write_text(
                    yaml.safe_dump(payload), encoding="utf-8"
                )
                with patch.dict(
                    os.environ, {CONFIG_DIR_ENV: directory}, clear=True
                ):
                    self.assertEqual(parse_priority_players(None), expected)

    def test_refresh_reloads_file_unless_cli_pinned(self) -> None:
        args = SimpleNamespace(priority=None, priority_players=("old",))
        with patch.object(
            main, "parse_priority_players", return_value=("new",)
        ):
            main.refresh_priority_players(args)
        self.assertEqual(args.priority_players, ("new",))

        pinned = SimpleNamespace(priority=["fixed"], priority_players=("fixed",))
        with patch.object(main, "parse_priority_players") as parse:
            main.refresh_priority_players(pinned)
        parse.assert_not_called()
        self.assertEqual(pinned.priority_players, ("fixed",))


class PriorityCollectTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = MagicMock()
        self.conn.close = MagicMock()
        self.limiter = main.RateLimiter(0)

    def collect(self, fetched, gains, last_xp=None, canonical=None):
        with patch.object(main.storage, "connect", return_value=self.conn), \
                patch.object(
                    main.storage, "canonical_username",
                    return_value=canonical,
                ), \
                patch.object(main.storage, "upsert_players"), \
                patch.object(
                    main.storage, "latest_priority_xp_by_skill",
                    return_value=dict(last_xp or {}),
                ), \
                patch.object(
                    main.storage, "insert_priority_snapshots"
                ) as insert, \
                patch.object(
                    main.storage, "insert_priority_checkpoints"
                ) as checkpoints, \
                patch.object(
                    main.api, "fetch_player_xp", return_value=fetched
                ), \
                patch.object(
                    main.api, "fetch_player_gains", side_effect=gains
                ):
            stats = main.collect_priority_player(
                "dsn", "alice", T1, self.limiter
            )
        return stats, insert, checkpoints

    def test_hcim_priority_fetch_stores_changes(self) -> None:
        stats, insert, checkpoints = self.collect(
            (HCIM, xp(1000)), [xp(100)]
        )
        self.assertEqual(stats["players"], 1)
        self.assertEqual(stats["rows"], len(SKILLS))
        self.assertEqual(len(insert.call_args.args[1]), len(SKILLS))
        self.assertEqual(checkpoints.call_args.args[2], [T1])

        stats, insert, checkpoints = self.collect(
            (HCIM, xp(1000)), [xp(100)], canonical="Cojultad"
        )
        self.assertTrue(
            all(row[0] == "Cojultad" for row in insert.call_args.args[1])
        )
        self.assertEqual(checkpoints.call_args.args[1], "Cojultad")

        stats, insert, checkpoints = self.collect(
            (HCIM, xp(1000)), [xp(0)], last_xp=xp(1000)
        )
        self.assertEqual(stats["rows"], 0)
        insert.assert_not_called()
        self.assertEqual(checkpoints.call_args.args[2], [T1])

    def test_non_hcim_priority_player_is_skipped(self) -> None:
        stats, insert, checkpoints = self.collect((1, xp(1000)), [xp(0)])
        self.assertEqual(stats["players"], 0)
        self.assertEqual(stats["requests"], 1)
        insert.assert_not_called()
        checkpoints.assert_not_called()

        with patch.object(main.storage, "connect", return_value=self.conn), \
                patch.object(
                    main.storage, "canonical_username", return_value=None
                ), \
                patch.object(
                    main.api, "fetch_player_xp",
                    side_effect=api.ApiError("503"),
                ):
            stats = main.collect_priority_player(
                "dsn", "alice", T1, self.limiter
            )
        self.assertEqual(stats["failures"], 1)
        self.assertEqual(stats["players"], 0)

    def test_collect_once_uses_tracked_players_and_prunes(self) -> None:
        args = SimpleNamespace(priority_players=("alice", "bob"))
        totals = {"requests": 2, "rows": 2, "failures": 0, "players": 1}
        with patch.object(main.storage, "connect", return_value=self.conn), \
                patch.object(main.storage, "ensure_schema"), \
                patch.object(main.storage, "prune_priority_data") as prune, \
                patch.object(
                    main.storage, "top_players_by_xp",
                    return_value=["Cojultad"],
                ), \
                patch.object(
                    main.storage, "canonical_username", return_value=None
                ), \
                patch.object(
                    main, "collect_priority_player", return_value=totals
                ) as collect:
            result = main.collect_priority_once(args, "dsn", self.limiter)
        self.assertEqual(
            [call.args[1] for call in collect.call_args_list],
            ["Cojultad", "alice", "bob"],
        )
        prune.assert_called_once()
        self.assertEqual(result["players"], 3)
        self.assertEqual(result["failures"], 0)

        args = SimpleNamespace(priority_players=())
        with patch.object(main.storage, "connect", return_value=self.conn), \
                patch.object(main.storage, "ensure_schema"), \
                patch.object(main.storage, "prune_priority_data"), \
                patch.object(
                    main.storage, "top_players_by_xp",
                    return_value=["Cojultad"],
                ), \
                patch.object(
                    main.storage, "canonical_username", return_value=None
                ), \
                patch.object(
                    main, "collect_priority_player", return_value=totals
                ) as collect:
            main.collect_priority_once(args, "dsn", self.limiter)
        self.assertEqual(collect.call_args.args[1], "Cojultad")


if __name__ == "__main__":
    unittest.main()
