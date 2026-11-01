"""Shared configuration: skill list, API templates and time spans."""

from __future__ import annotations

import json
import os
import pathlib
import urllib.parse

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]

SKILLS: tuple[str, ...] = (
    "overall",
    "attack", "defence", "strength", "hitpoints", "ranged", "prayer", "magic",
    "cooking", "woodcutting", "fletching", "fishing", "firemaking", "crafting",
    "smithing", "mining", "herblore", "agility", "thieving", "slayer", "farming",
    "runecrafting", "hunter", "construction",
)
account_filter_code = {
    "NOFILTER": 0,
    "ALL_IRONMAN": 1,
    "HARDCORE_IRONMAN": 2,
    "ULTIMATE_IRONMAN": 3,
}
HCIM = account_filter_code["HARDCORE_IRONMAN"]
PLAYER_HIGHSCORES_URL = "https://vidyaplace.org/api/highscores/player/{username}"
PLAYER_TRACKER_URL = (
    "https://vidyaplace.org/api/tracker/player/{username}?time={seconds}"
)
SKILL_TRACKER_URL = (
    "https://vidyaplace.org/api/tracker/skill/{skill}/{page}"
    "?time={seconds}&ironman={ironman}"
)


def build_dsn(database: str | None = None) -> str | None:
    """Connection string from DSN or PG* variables, with URI-safe quoting."""
    if database is None:
        explicit = os.environ.get("VSCAPE_HCIM_TRACKER_DSN")
        if explicit:
            return explicit
    host = os.environ.get("PGHOST")
    if not host:
        return None
    user = urllib.parse.quote(os.environ.get("PGUSER", ""), safe="")
    password = urllib.parse.quote(
        os.environ.get("PGPASSWORD", ""), safe=""
    )
    name = urllib.parse.quote(
        database or os.environ.get("PGDATABASE", ""), safe=""
    )
    port = os.environ.get("PGPORT", "5432")
    auth = f"{user}:{password}@" if user else ""
    return f"postgresql://{auth}{host}:{port}/{name}"

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

# Priority players are fetched every 10 minutes and their high-frequency data
# is kept for one day. The list comes from priority_players.json, unless the
# environment or --priority overrides it.
DEFAULT_PRIORITY_INTERVAL_SECONDS = 10 * 60
DEFAULT_PRIORITY_RETENTION_SECONDS = 24 * 3600
DEFAULT_PRIORITY_PLAYERS = ("cojultad",)
PRIORITY_FILE_ENV = "VSCAPE_HCIM_TRACKER_PRIORITY_FILE"
DEFAULT_PRIORITY_FILE = _PROJECT_ROOT / "priority_players.json"


def priority_file_path() -> pathlib.Path:
    override = os.environ.get(PRIORITY_FILE_ENV)
    if override:
        return pathlib.Path(override).expanduser()
    return DEFAULT_PRIORITY_FILE


def _load_priority_file() -> tuple[str, ...]:
    try:
        data = json.loads(
            priority_file_path().read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return DEFAULT_PRIORITY_PLAYERS
    if isinstance(data, dict):
        data = data.get("players")
    if not isinstance(data, list):
        return DEFAULT_PRIORITY_PLAYERS
    names: list[str] = []
    for value in data:
        name = str(value).strip()
        if name and name not in names:
            names.append(name)
    return tuple(names)


def parse_priority_players(values: list[str] | None) -> tuple[str, ...]:
    """Usernames from CLI values, the environment, or the JSON file."""
    if values is None:
        raw = os.environ.get(
            "VSCAPE_HCIM_TRACKER_PRIORITY_PLAYERS", ""
        ).strip()
        if not raw:
            return _load_priority_file()
        values = [raw]
    names: list[str] = []
    for value in values:
        for name in value.split(","):
            name = name.strip()
            if name and name not in names:
                names.append(name)
    return tuple(names)


DEFAULT_BASE_URL = os.environ.get(
    "VSCAPE_HCIM_TRACKER_BASE_URL", "http://localhost:8000"
)
