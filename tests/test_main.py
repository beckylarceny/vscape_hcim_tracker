"""Unit tests for the collection loop and checkpoint grid."""

from __future__ import annotations

import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src import api, main, storage
from src.config import HCIM, SKILLS, parse_priority_players

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

    def test_retry_window_includes_previous_checkpoints(self) -> None:
        grid = main.build_grid(
            NOW + COLLECT, DAY, COLLECT, 365 * DAY, 7 * DAY, True
        )
        self.assertIn((NOW // COLLECT) * COLLECT, grid)


class CollectPlayerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = MagicMock()
        self.conn.close = MagicMock()
        self.limiter = main.RateLimiter(0)

    def collect(self, fetched, gains, grid=(T1, T2), last_xp=None):
        patches = [
            patch.object(main.storage, "connect", return_value=self.conn),
            patch.object(main.storage, "upsert_players"),
            patch.object(
                main.storage, "fetched_checkpoint_times", return_value=set()
            ),
            patch.object(
                main.storage, "latest_xp_by_skill",
                return_value=dict(last_xp or {}),
            ),
            patch.object(main.storage, "insert_snapshots"),
            patch.object(main.storage, "insert_checkpoints"),
            patch.object(main.api, "fetch_player_xp", return_value=fetched),
            patch.object(main.api, "fetch_player_gains", side_effect=gains),
        ]
        with patches[0], patches[1] as upsert, patches[2], patches[3], \
                patches[4] as insert, patches[5] as checkpoints, \
                patches[6], patches[7]:
            stats = main.collect_player(
                "dsn", "alice", list(grid), self.limiter
            )
        return stats, upsert, insert, checkpoints

    def test_non_hcim_account_is_not_recorded(self) -> None:
        stats, upsert, insert, checkpoints = self.collect(
            (1, xp(0)), [xp(0), xp(0)]
        )
        self.assertEqual(stats["players"], 0)
        upsert.assert_not_called()
        insert.assert_not_called()
        checkpoints.assert_not_called()

    def test_hcim_account_is_verified_then_recorded(self) -> None:
        stats, upsert, insert, checkpoints = self.collect(
            (HCIM, xp(1000)), [xp(10), xp(20)]
        )
        self.assertEqual(stats["players"], 1)
        self.assertEqual(stats["rows"], 2 * len(SKILLS))
        upsert.assert_called_once_with(self.conn, [("alice", HCIM)])
        self.assertEqual(insert.call_count, 1)
        self.assertEqual(len(insert.call_args.args[1]), 2 * len(SKILLS))
        self.assertEqual(
            sorted(checkpoints.call_args.args[2]), [T1, T2]
        )

    def test_unchanged_xp_is_not_stored(self) -> None:
        last = xp(1000)
        stats, _, insert, checkpoints = self.collect(
            (HCIM, xp(1000)), [xp(0), xp(0)], last_xp=last
        )
        self.assertEqual(stats["rows"], 0)
        insert.assert_not_called()
        self.assertEqual(
            sorted(checkpoints.call_args.args[2]), [T1, T2]
        )

    def test_only_changed_skills_are_stored(self) -> None:
        last = xp(1000)
        gains = xp(0)
        gains["overall"] = 5
        stats, _, insert, _ = self.collect(
            (HCIM, xp(1000)), [gains], grid=(T1,), last_xp=last
        )
        self.assertEqual(stats["rows"], 1)
        rows = insert.call_args.args[1]
        self.assertEqual([row[1] for row in rows], ["overall"])

    def test_failed_checkpoint_does_not_abort_the_player(self) -> None:
        stats, upsert, insert, checkpoints = self.collect(
            (HCIM, xp(1000)),
            [api.ApiError("503"), xp(10)],
            grid=(T1, T2),
        )
        self.assertEqual(stats["players"], 1)
        self.assertEqual(stats["failures"], 1)
        self.assertEqual(stats["rows"], len(SKILLS))
        rows = insert.call_args.args[1]
        self.assertTrue(all(row[2] == storage.as_datetime(T2) for row in rows))
        self.assertEqual(sorted(checkpoints.call_args.args[2]), [T2])

    def test_highscores_api_error_is_counted_not_raised(self) -> None:
        with patch.object(main.storage, "connect", return_value=self.conn), \
                patch.object(main.api, "fetch_player_xp",
                             side_effect=api.ApiError("503")):
            stats = main.collect_player(
                "dsn", "alice", [T1], self.limiter
            )
        self.assertEqual(stats["failures"], 1)
        self.assertEqual(stats["players"], 0)
        self.conn.close.assert_called_once()

    def test_pending_rows_are_flushed_when_later_fetch_fails(self) -> None:
        stats, _, insert, checkpoints = self.collect(
            (HCIM, xp(1000)),
            [xp(10), api.ApiError("503")],
            grid=(T1, T2),
        )
        self.assertEqual(stats["failures"], 1)
        self.assertEqual(insert.call_count, 1)
        self.assertEqual(len(insert.call_args.args[1]), len(SKILLS))
        self.assertEqual(sorted(checkpoints.call_args.args[2]), [T1])


class ModeTest(unittest.TestCase):
    def test_collect_only_and_serve_only_are_exclusive(self) -> None:
        with self.assertRaises(SystemExit):
            main.build_parser().parse_args(
                ["--collect-only", "--serve-only"]
            )

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


class ReportThreadTest(unittest.TestCase):
    def test_writes_on_its_own_timer(self) -> None:
        args = SimpleNamespace(report_interval=0.01)
        with patch.object(main, "write_current_report") as writer:
            stop = main.start_report_thread(args, "dsn")
            self.assertIsNotNone(stop)
            for _ in range(100):
                if writer.call_count:
                    break
                time.sleep(0.01)
            stop.set()
        self.assertGreaterEqual(writer.call_count, 1)


class PriorityListTest(unittest.TestCase):
    def test_cli_values_are_split_and_deduped(self) -> None:
        self.assertEqual(
            parse_priority_players(["a,b", " c ", "a"]),
            ("a", "b", "c"),
        )

    def test_environment_is_used_when_cli_is_omitted(self) -> None:
        with patch.dict(
            os.environ,
            {"VSCAPE_HCIM_TRACKER_PRIORITY_PLAYERS": "x, y"},
        ):
            self.assertEqual(parse_priority_players(None), ("x", "y"))


class PriorityCollectTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = MagicMock()
        self.conn.close = MagicMock()
        self.limiter = main.RateLimiter(0)

    def collect(self, fetched, gains, last_xp=None):
        with patch.object(main.storage, "connect", return_value=self.conn), \
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

    def test_unchanged_priority_xp_is_not_stored(self) -> None:
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

    def test_priority_api_error_is_counted(self) -> None:
        with patch.object(main.storage, "connect", return_value=self.conn), \
                patch.object(
                    main.api, "fetch_player_xp",
                    side_effect=api.ApiError("503"),
                ):
            stats = main.collect_priority_player(
                "dsn", "alice", T1, self.limiter
            )
        self.assertEqual(stats["failures"], 1)
        self.assertEqual(stats["players"], 0)

    def test_collect_once_uses_the_priority_list_and_prunes(self) -> None:
        args = SimpleNamespace(priority_players=("alice", "bob"))
        totals = {"requests": 2, "rows": 2, "failures": 0, "players": 1}
        with patch.object(main.storage, "connect", return_value=self.conn), \
                patch.object(main.storage, "ensure_schema"), \
                patch.object(main.storage, "prune_priority_data") as prune, \
                patch.object(
                    main, "collect_priority_player", return_value=totals
                ) as collect:
            result = main.collect_priority_once(args, "dsn", self.limiter)
        self.assertEqual(collect.call_count, 2)
        prune.assert_called_once()
        self.assertEqual(result["players"], 2)

    def test_priority_thread_starts_without_players_to_prune(self) -> None:
        args = SimpleNamespace(priority_players=())

        def fake_loop(thread_args, dsn, limiter, stop):
            stop.set()

        with patch.object(
            main, "_priority_loop", side_effect=fake_loop
        ) as loop:
            stop = main.start_priority_thread(args, "dsn", self.limiter)
            self.assertIsNotNone(stop)
            for _ in range(50):
                if loop.call_count:
                    break
                time.sleep(0.01)
        self.assertGreaterEqual(loop.call_count, 1)


if __name__ == "__main__":
    unittest.main()
