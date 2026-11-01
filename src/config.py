"""Shared configuration: skill list, API templates and time spans."""

from __future__ import annotations

import os
import pathlib
import sys


def _data_dir() -> pathlib.Path:
    """Directory holding data.py: env override, then nearest parent that has it."""
    override = os.environ.get("VSCAPE_HCIM_TRACKER_DATA_DIR")
    if override:
        return pathlib.Path(override).expanduser().resolve()
    here = pathlib.Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "data.py").is_file():
            return parent
    return here.parents[2]


_DATA_DIR = _data_dir()
if str(_DATA_DIR) not in sys.path:
    sys.path.insert(0, str(_DATA_DIR))

from data import (  # noqa: E402
    PLAYER_HIGHSCORES_URL,
    PLAYER_TRACKER_URL,
    SKILLS,
    account_filter_code,
)

HCIM = account_filter_code["HARDCORE_IRONMAN"]

# Fixed durations, in seconds, used for the "current top gains" and record scans.
SPANS: dict[str, int] = {
    "day": 86400,
    "week": 604800,
    "month": 2592000,
    "year": 31536000,
}

# Historical checkpoints are reconstructed on a coarse day grid.
DEFAULT_INTERVAL_SECONDS = 24 * 3600
# New checkpoints are appended on this cadence (also the loop sleep).
DEFAULT_COLLECT_INTERVAL_SECONDS = 6 * 3600
# Recent checkpoints are re-fetched if they failed or are missing.
DEFAULT_RETRY_WINDOW_SECONDS = 7 * 86400
DEFAULT_LOOKBACK_SECONDS = 365 * 86400
DEFAULT_TOP_N = 5
DEFAULT_REPORT_INTERVAL_SECONDS = 60
# The API returns 503 when several requests arrive at once; keep concurrency low.
DEFAULT_WORKERS = 2
DEFAULT_RATE = 4.0
