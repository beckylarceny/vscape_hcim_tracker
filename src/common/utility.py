"""Configuration loading and small helpers shared by the services."""

from __future__ import annotations

import argparse
import os
import pathlib
import urllib.parse

import yaml

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]

CONFIG_DIR_ENV = "VSCAPE_HCIM_TRACKER_CONFIG_DIR"
DEFAULT_CONFIG_DIR = _PROJECT_ROOT / "config"


def config_dir() -> pathlib.Path:
    override = os.environ.get(CONFIG_DIR_ENV)
    if override:
        return pathlib.Path(override).expanduser()
    return DEFAULT_CONFIG_DIR


def load_config(name: str) -> dict:
    """Read one of collector.yaml, web.yaml or db.yaml."""
    path = config_dir() / f"{name}.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


_COLLECTOR = load_config("collector")
_WEB = load_config("web")
_DB = load_config("db")

SKILLS: tuple[str, ...] = tuple(_COLLECTOR["skills"])
ACCOUNT_FILTER_CODES: dict[str, int] = dict(
    _COLLECTOR["account_filter_codes"]
)
HCIM = ACCOUNT_FILTER_CODES["HARDCORE_IRONMAN"]
PLAYER_HIGHSCORES_URL: str = _COLLECTOR["urls"]["player_highscores"]
PLAYER_TRACKER_URL: str = _COLLECTOR["urls"]["player_tracker"]

_COLLECTOR_DEFAULTS: dict = _COLLECTOR["defaults"]
DEFAULT_INTERVAL_SECONDS = int(_COLLECTOR_DEFAULTS["interval_seconds"])
DEFAULT_COLLECT_INTERVAL_SECONDS = int(
    _COLLECTOR_DEFAULTS["collect_interval_seconds"]
)
DEFAULT_RETRY_WINDOW_SECONDS = int(
    _COLLECTOR_DEFAULTS["retry_window_seconds"]
)
DATA_START_EPOCH = int(_COLLECTOR_DEFAULTS["data_start_epoch"])
DEFAULT_WORKERS = int(_COLLECTOR_DEFAULTS["workers"])
DEFAULT_RATE = float(_COLLECTOR_DEFAULTS["rate"])

_PRIORITY: dict = _COLLECTOR["priority"]
DEFAULT_PRIORITY_PLAYERS: tuple[str, ...] = tuple(_PRIORITY["players"])
DEFAULT_PRIORITY_INTERVAL_SECONDS = int(_PRIORITY["interval_seconds"])
DEFAULT_PRIORITY_RETENTION_SECONDS = int(_PRIORITY["retention_seconds"])
DEFAULT_PRIORITY_TOP = int(_PRIORITY["top"])

_WEB_DEFAULTS: dict = _WEB["defaults"]
SPANS: dict[str, int] = dict(_WEB["spans"])
DEFAULT_TOP_N = int(_WEB_DEFAULTS["top_n"])
DEFAULT_REPORT_INTERVAL_SECONDS = int(
    _WEB_DEFAULTS["report_interval_seconds"]
)
DEFAULT_BASE_URL = os.environ.get(
    "VSCAPE_HCIM_TRACKER_BASE_URL", _WEB_DEFAULTS["base_url"]
)


def _dedupe(names) -> tuple[str, ...]:
    result: list[str] = []
    for value in names:
        name = str(value).strip()
        if name and name not in result:
            result.append(name)
    return tuple(result)


def build_dsn(database: str | None = None) -> str | None:
    """Connection string from DSN, PG* variables or db.yaml."""
    if database is None:
        explicit = os.environ.get("VSCAPE_HCIM_TRACKER_DSN")
        if explicit:
            return explicit
    host = os.environ.get("PGHOST") or _DB.get("host")
    if not host:
        return None
    user = urllib.parse.quote(
        os.environ.get("PGUSER") or _DB.get("user", ""), safe=""
    )
    password = urllib.parse.quote(
        os.environ.get("PGPASSWORD", ""), safe=""
    )
    name = urllib.parse.quote(
        database or os.environ.get("PGDATABASE") or _DB.get("database", ""),
        safe="",
    )
    port = os.environ.get("PGPORT") or _DB.get("port", "5432")
    auth = f"{user}:{password}@" if user else ""
    return f"postgresql://{auth}{host}:{port}/{name}"


def _configured_priority_players() -> tuple[str, ...]:
    try:
        data = load_config("collector")["priority"]["players"]
    except (OSError, ValueError, KeyError, TypeError):
        return DEFAULT_PRIORITY_PLAYERS
    if not isinstance(data, list):
        return DEFAULT_PRIORITY_PLAYERS
    return _dedupe(data)


def parse_priority_players(values: list[str] | None) -> tuple[str, ...]:
    """Usernames from CLI values, the environment, or collector.yaml."""
    if values is None:
        raw = os.environ.get(
            "VSCAPE_HCIM_TRACKER_PRIORITY_PLAYERS", ""
        ).strip()
        if not raw:
            return _configured_priority_players()
        values = [raw]
    names: list[str] = []
    for value in values:
        names.extend(value.split(","))
    return _dedupe(names)


_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(text: str) -> int:
    value = text.strip().lower()
    if value.isdigit():
        return int(value)
    if value and value[-1] in _UNITS:
        return int(float(value[:-1]) * _UNITS[value[-1]])
    raise argparse.ArgumentTypeError(f"invalid duration: {text!r}")


def effective_lookback(now_epoch: int, lookback: int | None) -> int:
    """History to reconstruct, never before the known data start."""
    available = max(0, now_epoch - DATA_START_EPOCH)
    if lookback is None:
        return available
    return min(lookback, available)
