"""HTTP access to the vidyaplace highscore and xp tracker API."""

from __future__ import annotations

import json
import logging
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from .utility import HCIM, PLAYER_HIGHSCORES_URL, PLAYER_TRACKER_URL, SKILLS

log = logging.getLogger(__name__)

API_BASE = "https://vidyaplace.org/api"
USER_AGENT = "xp-tracker/1.0"

# The backend answers with 503 under concurrent load, so these are worth retrying.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

Waiter = Callable[[], None]


class ApiError(RuntimeError):
    """Raised when a request keeps failing after all retries."""


def _retry_delay(attempt: int, retry_after: float | None) -> float:
    delay = min(60.0, 2.0 * 2 ** (attempt - 1)) + random.random()
    if retry_after is not None:
        delay = max(delay, retry_after)
    return delay


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _get_json(
    url: str,
    *,
    wait: Waiter | None = None,
    timeout: float = 30.0,
    retries: int = 5,
) -> Any:
    """Fetch a JSON document. Returns None for "not found" style responses.

    `wait` is called before every attempt, including retries, so the shared
    rate limiter also applies to retry bursts.
    """
    last_error: Exception | None = None
    retry_after: float | None = None
    for attempt in range(retries):
        if attempt:
            time.sleep(_retry_delay(attempt, retry_after))
        retry_after = None
        if wait is not None:
            wait()
        request = urllib.request.Request(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            last_error = error
            if error.code not in RETRYABLE_STATUS:
                raise ApiError(f"HTTP {error.code} for {url}") from error
            retry_after = _parse_retry_after(error.headers.get("Retry-After"))
            log.debug("HTTP %s for %s", error.code, url)
            continue
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last_error = error
            log.debug("network error for %s: %s", url, error)
            continue

        if not body or body.strip() == b"null":
            return None
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            # The API answers "Not found" as plain text for unknown players.
            return None

    raise ApiError(f"request failed after {retries} attempts: {url} ({last_error})")


def _quote(username: str) -> str:
    return urllib.parse.quote(username, safe="")


def list_hcim_players(wait: Waiter | None = None) -> dict[str, int]:
    """Return {username: current overall xp} for every tracked hardcore ironman."""
    meta = _get_json(
        f"{API_BASE}/highscores/pages/overall?ironman={HCIM}", wait=wait
    )
    pages = int(meta["Pages"]) if meta else 0
    players: dict[str, int] = {}
    for page in range(1, pages + 1):
        rows = _get_json(
            f"{API_BASE}/highscores/skill/overall/{page}?ironman={HCIM}",
            wait=wait,
        )
        if not rows:
            continue
        for row in rows:
            if int(row.get("ironman") or 0) != HCIM:
                continue
            players[row["username"]] = int(row.get("overall_xp") or 0)
    log.info("discovered %d hardcore ironman players", len(players))
    return players


def fetch_player_xp(
    username: str, wait: Waiter | None = None
) -> tuple[int, dict[str, int]] | None:
    """Return (ironman status, {skill: xp}) for one player, or None if unknown."""
    data = _get_json(
        PLAYER_HIGHSCORES_URL.format(username=_quote(username)), wait=wait
    )
    if not data:
        return None
    xp = {skill: int(data.get(f"{skill}_xp") or 0) for skill in SKILLS}
    return int(data.get("ironman") or 0), xp


def fetch_player_gains(
    username: str, seconds: int, wait: Waiter | None = None
) -> dict[str, int] | None:
    """Return {skill: xp gained during the last `seconds` seconds}."""
    url = PLAYER_TRACKER_URL.format(
        username=_quote(username), seconds=int(seconds)
    )
    data = _get_json(url, wait=wait)
    if not data:
        return None
    return {skill: int(data.get(f"{skill}_diff") or 0) for skill in SKILLS}
