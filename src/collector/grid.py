"""Checkpoint grid for backfill and retry collection."""

from __future__ import annotations


def build_grid(
    now_epoch: int,
    interval: int,
    collect_interval: int,
    lookback: int,
    retry_window: int,
    no_backfill: bool,
) -> list[int]:
    """Checkpoints to fetch, newest first.

    The recent retry window is always included, so a failed checkpoint is
    fetched again on the next run. The one-year backfill uses the coarse
    `interval` grid and is skipped with `no_backfill`.
    """
    current = (now_epoch // collect_interval) * collect_interval
    count = max(1, retry_window // collect_interval)
    times = {current - step * collect_interval for step in range(count)}
    if not no_backfill:
        anchor = (now_epoch // interval) * interval
        times.update(range(anchor, anchor - lookback - 1, -interval))
    return sorted(times, reverse=True)
