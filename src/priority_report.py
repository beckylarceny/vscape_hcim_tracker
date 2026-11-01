"""Priority player pages and per-player RSS feeds."""

from __future__ import annotations

import email.utils
import os
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    import psycopg

from .config import (
    DEFAULT_PRIORITY_INTERVAL_SECONDS,
    DEFAULT_PRIORITY_RETENTION_SECONDS,
    SKILLS,
)
from .player_report import (
    ChartData,
    _PLAYER_CSS,
    _chart_data,
    _chart_svg,
    _esc,
    _num,
    highscores_url,
    level_for_xp,
    player_slug,
    total_level,
)
from .storage import as_datetime

WINDOW_SECONDS = DEFAULT_PRIORITY_RETENTION_SECONDS


class Event(NamedTuple):
    epoch: int
    changes: tuple[tuple[str, int], ...]


class PriorityData(NamedTuple):
    username: str
    generated: str
    now_epoch: int
    current: dict[str, int]
    charts: dict[str, ChartData]


def _points_by_skill(
    conn: psycopg.Connection, username: str, start_epoch: int, end_epoch: int
) -> dict[str, list[tuple[int, int]]]:
    rows = conn.execute(
        """
        SELECT skill, observed_at, xp
        FROM priority_snapshots
        WHERE username = %s AND observed_at >= %s AND observed_at <= %s
        ORDER BY skill, observed_at
        """,
        (username, as_datetime(start_epoch), as_datetime(end_epoch)),
    ).fetchall()
    points: dict[str, list[tuple[int, int]]] = {}
    for skill, observed_at, xp in rows:
        points.setdefault(skill, []).append(
            (int(observed_at.timestamp()), int(xp))
        )
    return points


def collect_priority_data(
    conn: psycopg.Connection,
    username: str,
    now_epoch: int,
    generated: str,
    window: int = WINDOW_SECONDS,
) -> PriorityData:
    points_by_skill = _points_by_skill(
        conn,
        username,
        now_epoch - window - DEFAULT_PRIORITY_INTERVAL_SECONDS,
        now_epoch,
    )
    current = {
        skill: points[-1][1]
        for skill, points in points_by_skill.items()
        if points
    }
    charts = {
        skill: _chart_data(
            [epoch for epoch, _ in points_by_skill.get(skill, [])],
            points_by_skill.get(skill, []),
            window,
            now_epoch,
        )
        for skill in SKILLS
    }
    return PriorityData(
        username=username,
        generated=generated,
        now_epoch=now_epoch,
        current=current,
        charts=charts,
    )


def collect_events(
    conn: psycopg.Connection,
    username: str,
    now_epoch: int,
    window: int = WINDOW_SECONDS,
) -> list[Event]:
    """Changed xp values grouped by fetch time, newest first."""
    rows = conn.execute(
        """
        SELECT skill, observed_at, xp
        FROM priority_snapshots
        WHERE username = %s
        ORDER BY observed_at, skill
        """,
        (username,),
    ).fetchall()
    previous: dict[str, int] = {}
    changes: dict[int, list[tuple[str, int]]] = {}
    for skill, observed_at, xp in rows:
        xp = int(xp)
        if skill in previous and previous[skill] != xp:
            epoch = int(observed_at.timestamp())
            changes.setdefault(epoch, []).append(
                (skill, xp - previous[skill])
            )
        previous[skill] = xp
    order = {skill: index for index, skill in enumerate(SKILLS)}
    start = now_epoch - window
    events = [
        Event(epoch, tuple(sorted(items, key=lambda item: order.get(item[0], 0))))
        for epoch, items in changes.items()
        if epoch >= start
    ]
    events.sort(reverse=True)
    return events[:50]


def _gains_text(changes: tuple[tuple[str, int], ...]) -> str:
    return "\n".join(
        f"{skill}: {gain:+,} xp" for skill, gain in changes
    )


def render_feed(
    username: str,
    slug: str,
    events: list[Event],
    now_epoch: int,
    base_url: str,
) -> str:
    base = base_url.rstrip("/")
    page_url = f"{base}/priority/{urllib.parse.quote(slug)}.html"
    feed_url = f"{base}/priority/{urllib.parse.quote(slug)}.xml"
    last_build = email.utils.formatdate(
        events[0].epoch if events else now_epoch, usegmt=True
    )

    items: list[str] = []
    for event in events:
        changes = dict(event.changes)
        overall = changes.get("overall")
        if overall is None:
            title = f"{username} xp update"
        else:
            title = f"{username} {overall:+,} xp"
        description = _gains_text(event.changes)
        items.append(
            "<item>"
            f"<title>{_esc(title)}</title>"
            f"<link>{_esc(page_url)}</link>"
            f'<guid isPermaLink="false">{_esc(slug)}-{event.epoch}</guid>'
            f"<pubDate>{email.utils.formatdate(event.epoch, usegmt=True)}</pubDate>"
            f"<description>{_esc(description)}</description>"
            "</item>"
        )

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" '
        'xmlns:atom="http://www.w3.org/2005/Atom">\n'
        "<channel>"
        f"<title>{_esc(username)} priority xp</title>"
        f"<link>{_esc(page_url)}</link>"
        f"<description>30-minute xp updates for {_esc(username)}</description>"
        f"<lastBuildDate>{last_build}</lastBuildDate>"
        f'<atom:link href="{_esc(feed_url)}" rel="self" '
        'type="application/rss+xml"/>'
        + "".join(items)
        + "</channel></rss>\n"
    )


_PRIORITY_CSS = """
figure.big{margin:.5rem 0 1.75rem;padding:.6rem .7rem .4rem}
figure.big figcaption{text-align:center}
"""


def _current_line(
    skill: str, value: int | None, current: dict[str, int]
) -> str:
    if value is None:
        return "no data"
    if skill == "overall":
        total = total_level(current)
        if total is None:
            return _num(value)
        return f"{_num(value)} (total level {total})"
    return f"{_num(value)} (level {level_for_xp(value)})"


def render_priority_page(data: PriorityData) -> str:
    slug = player_slug(data.username)
    feed_href = f"{urllib.parse.quote(slug)}.xml"
    page_href = f"{urllib.parse.quote(slug)}.html"
    player_href = f"../players/{urllib.parse.quote(slug)}.html"
    parts: list[str] = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{_esc(data.username.lower())} priority - xp tracker</title>",
        '<link rel="alternate" type="application/rss+xml" '
        f'title="{_esc(data.username)} priority xp" href="{_esc(feed_href)}">',
        f"<style>{_PLAYER_CSS}{_PRIORITY_CSS}</style></head>"
        '<body id="top">',
        f"<h1>{_esc(data.username)}</h1>",
        f'<h2 class="subheading"><a href="{_esc(highscores_url(data.username))}">'
        "vidyascape highscores</a></h2>",
        f'<p class="meta">generated: {_esc(data.generated)}</p>',
        '<p class="meta">priority report: 30-minute updates, last 24 hours</p>',
        '<p class="sub">'
        '<a href="../latest.html">all rankings</a> · '
        f'<a href="{_esc(player_href)}">player page</a> · '
        f'<a href="{_esc(feed_href)}">rss</a></p>',
        '<div class="stats">',
    ]

    for skill in SKILLS:
        value = data.current.get(skill)
        parts.append(f'<section id="skill-{_esc(skill)}">')
        parts.append(f"<h2>{_esc(skill)}</h2>")
        parts.append(
            '<p class="meta">current xp: '
            f"{_current_line(skill, value, data.current)}</p>"
        )
        label = f"{skill} last 24 hours: total xp and xp/h"
        parts.append(
            '<figure class="big"><figcaption><b>last 24 hours</b>'
            '<span class="key line-key">total xp</span>'
            '<span class="key area-key">xp/h</span></figcaption>'
            f"{_chart_svg(data.charts[skill], label, width=720, height=200)}"
            "</figure>"
        )
        parts.append("</section>")

    parts.append("</div>")
    parts.append(
        '<a class="to-top" href="#top" aria-label="back to top">'
        "&#8593; Top</a>"
    )
    parts.append("</body></html>")
    return "\n".join(parts) + "\n"


def _atomic_write(path: Path, text: str) -> None:
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, path)


def write_priority_reports(
    conn: psycopg.Connection,
    report_dir: Path,
    players: tuple[str, ...] | list[str],
    now_epoch: int,
    generated: str,
    base_url: str,
) -> int:
    directory = report_dir / "priority"
    if not players and not directory.exists():
        return 0
    directory.mkdir(parents=True, exist_ok=True)
    expected: set[str] = set()
    written = 0
    for username in players:
        slug = player_slug(username)
        stem = urllib.parse.quote(slug)
        data = collect_priority_data(conn, username, now_epoch, generated)
        events = collect_events(conn, username, now_epoch)
        _atomic_write(
            directory / f"{stem}.html",
            render_priority_page(data),
        )
        _atomic_write(
            directory / f"{stem}.xml",
            render_feed(username, slug, events, now_epoch, base_url),
        )
        expected.add(f"{stem}.html")
        expected.add(f"{stem}.xml")
        written += 1
    for pattern in ("*.html", "*.xml", ".*.tmp"):
        for path in directory.glob(pattern):
            if path.name not in expected:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
    return written
