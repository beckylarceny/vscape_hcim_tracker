"""Collector scheduling helpers."""

from __future__ import annotations


def next_slot(now: float, interval: int, grace: int) -> float:
    """Next timestamp on the interval grid, plus a grace period."""
    return (int(now) // interval + 1) * interval + grace
