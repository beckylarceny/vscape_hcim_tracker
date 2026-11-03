"""Priority player selection shared by collection and reporting."""

from __future__ import annotations

from . import storage
from .utility import (
    DEFAULT_PRIORITY_TOP,
    TRACKED_ACCOUNT_CODES,
    parse_priority_players,
)


def refresh_priority_players(args) -> None:
    """Re-read the JSON list unless CLI values pinned it."""
    if getattr(args, "priority", None) is not None:
        return
    args.priority_players = parse_priority_players(None)


def tracked_priority_names(
    conn, players: tuple[str, ...] | list[str]
) -> tuple[str, ...]:
    """Top players by overall xp plus the manual list, in stored spelling."""
    names: list[str] = []
    candidates = list(
        storage.top_players_by_xp(
            conn, "overall", DEFAULT_PRIORITY_TOP, TRACKED_ACCOUNT_CODES
        )
    )
    candidates.extend(players)
    for username in candidates:
        canonical = storage.canonical_username(conn, username) or username
        if canonical not in names:
            names.append(canonical)
    return tuple(names)


def priority_index_names(
    conn, players: tuple[str, ...] | list[str]
) -> tuple[str, ...]:
    """Tracked players plus anyone still holding unpruned priority rows."""
    names = list(tracked_priority_names(conn, players))
    for username in storage.priority_snapshot_players(
        conn, TRACKED_ACCOUNT_CODES
    ):
        if username not in names:
            names.append(username)
    return tuple(names)
