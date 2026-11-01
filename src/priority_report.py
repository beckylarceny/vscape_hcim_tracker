"""Priority player pages and per-player RSS feeds."""

from __future__ import annotations

import email.utils
import urllib.parse
from bisect import bisect_left, bisect_right
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
    SKILL_TABS_CSS,
    SKILL_TAB_SCRIPT,
    _PLAYER_CSS,
    _atomic_write,
    _chart_svg,
    _current_line,
    _esc,
    _group_points,
    _thin,
    highscores_url,
    player_slug,
    skill_panel_open,
    skill_tabs_html,
)
from .storage import as_datetime, canonical_username

WINDOW_SECONDS = DEFAULT_PRIORITY_RETENTION_SECONDS
MAX_FEED_EVENTS = (
    DEFAULT_PRIORITY_RETENTION_SECONDS // DEFAULT_PRIORITY_INTERVAL_SECONDS
)


class Event(NamedTuple):
    epoch: int
    changes: tuple[tuple[str, int], ...]
    previous_epoch: int | None = None


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
    return _group_points(rows)


def _regular_points_by_skill(
    conn: psycopg.Connection, username: str, start_epoch: int, end_epoch: int
) -> dict[str, list[tuple[int, int]]]:
    rows = conn.execute(
        """
        SELECT skill, observed_at, xp
        FROM snapshots
        WHERE username = %s AND observed_at >= %s AND observed_at <= %s
        ORDER BY skill, observed_at
        """,
        (username, as_datetime(start_epoch), as_datetime(end_epoch)),
    ).fetchall()
    return _group_points(rows)


def _merged_points(
    priority: list[tuple[int, int]],
    regular: list[tuple[int, int]],
) -> dict[int, tuple[int, bool]]:
    """Priority samples plus regular samples only in uncovered periods."""
    chosen: dict[int, tuple[int, bool]] = {
        timestamp: (xp, False) for timestamp, xp in priority
    }
    times = sorted(chosen)
    for timestamp, xp in regular:
        if timestamp in chosen:
            continue
        if not times:
            chosen[timestamp] = (xp, True)
            continue
        if timestamp <= times[0] or timestamp >= times[-1]:
            chosen[timestamp] = (xp, True)
            continue
        index = bisect_left(times, timestamp)
        gap = times[index] - times[index - 1]
        if gap > DEFAULT_PRIORITY_INTERVAL_SECONDS * 1.5:
            chosen[timestamp] = (xp, True)
    return chosen


def _merged_chart(
    chosen: dict[int, tuple[int, bool]],
    start_epoch: int,
    end_epoch: int,
) -> ChartData:
    if not chosen:
        return ChartData(start_epoch, end_epoch, [], [])

    times = sorted(chosen)
    index = bisect_right(times, start_epoch) - 1
    regular_xp: set[int] = set()
    regular_gains: set[int] = set()
    if index >= 0:
        baseline_time = times[index]
        baseline_xp, baseline_regular = chosen[baseline_time]
        line = [(start_epoch, baseline_xp)]
        if baseline_regular:
            regular_xp.add(start_epoch)
        rest = times[index + 1:]
        previous_xp = baseline_xp
        previous_time = baseline_time
        previous_regular = baseline_regular
    else:
        line = []
        rest = times
        previous_xp = None
        previous_time = None
        previous_regular = None

    gains: list[tuple[int, float]] = []
    for timestamp in rest:
        if timestamp > end_epoch:
            break
        xp, is_regular = chosen[timestamp]
        line.append((timestamp, xp))
        if is_regular:
            regular_xp.add(timestamp)
        if previous_xp is not None and previous_time is not None:
            elapsed = timestamp - previous_time
            if elapsed > 0:
                rate = max(0, xp - previous_xp) * 3600 / elapsed
                gains.append((timestamp, rate))
                if is_regular or previous_regular:
                    regular_gains.add(timestamp)
        previous_xp = xp
        previous_time = timestamp
        previous_regular = is_regular

    if line and line[-1][0] < end_epoch:
        line.append((end_epoch, line[-1][1]))
        if previous_regular:
            regular_xp.add(end_epoch)

    return ChartData(
        start_epoch,
        end_epoch,
        _thin(line, 180),
        _thin(gains, 180),
        frozenset(regular_xp),
        frozenset(regular_gains),
    )


def collect_priority_data(
    conn: psycopg.Connection,
    username: str,
    now_epoch: int,
    generated: str,
    window: int = WINDOW_SECONDS,
) -> PriorityData:
    start = now_epoch - window
    priority = _points_by_skill(
        conn, username, start - DEFAULT_PRIORITY_INTERVAL_SECONDS, now_epoch
    )
    regular = _regular_points_by_skill(
        conn, username, start - 86400, now_epoch
    )
    current: dict[str, int] = {}
    charts: dict[str, ChartData] = {}
    for skill in SKILLS:
        chosen = _merged_points(
            priority.get(skill, []), regular.get(skill, [])
        )
        times = sorted(chosen)
        if times:
            current[skill] = chosen[times[-1]][0]
        charts[skill] = _merged_chart(chosen, start, now_epoch)
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

    regular = conn.execute(
        """
        SELECT skill, observed_at, xp
        FROM snapshots
        WHERE username = %s AND observed_at >= %s
        ORDER BY observed_at, skill
        """,
        (username, as_datetime(now_epoch - window - 86400)),
    ).fetchall()
    snapshot_times = sorted(
        {int(row[1].timestamp()) for row in rows}
        | {int(row[1].timestamp()) for row in regular}
    )

    order = {skill: index for index, skill in enumerate(SKILLS)}
    start = now_epoch - window
    events = []
    for epoch, items in changes.items():
        if epoch < start:
            continue
        index = bisect_left(snapshot_times, epoch)
        previous_epoch = snapshot_times[index - 1] if index > 0 else None
        events.append(
            Event(
                epoch,
                tuple(sorted(items, key=lambda item: order.get(item[0], 0))),
                previous_epoch,
            )
        )
    events.sort(reverse=True)
    return events[:MAX_FEED_EVENTS]


def _plural(count: int, unit: str) -> str:
    return f"{count} {unit}" + ("s" if count != 1 else "")


def _ago(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "less than a minute"
    minutes = seconds // 60
    if minutes < 60:
        return _plural(minutes, "minute")
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        text = _plural(hours, "hour")
        if minutes:
            text += f" {_plural(minutes, 'minute')}"
        return text
    days, hours = divmod(hours, 24)
    text = _plural(days, "day")
    if hours:
        text += f" {_plural(hours, 'hour')}"
    return text


def _gains_text(changes: tuple[tuple[str, int], ...]) -> str:
    return " | ".join(
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
        if event.previous_epoch is None:
            since = "last snapshot: unknown"
        else:
            since = (
                "last snapshot: "
                f"{_ago(event.epoch - event.previous_epoch)} ago"
            )
        description = since + " | " + _gains_text(event.changes)
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
        f"<description>10-minute xp updates for {_esc(username)}</description>"
        f"<lastBuildDate>{last_build}</lastBuildDate>"
        f'<atom:link href="{_esc(feed_url)}" rel="self" '
        'type="application/rss+xml"/>'
        + "".join(items)
        + "</channel></rss>\n"
    )


_PRIORITY_CSS = """
figure.big{margin:.5rem 0 1.75rem;padding:.6rem .7rem .4rem}
figure.big figcaption{text-align:center}
svg .line.regular{stroke:#f59e0b;stroke-dasharray:4 3}
svg .area.regular{fill:#f59e0b33;stroke:#f59e0b33}
svg .dot.regular{fill:#f59e0b}
.key.regular-key::before{background:#f59e0b}
ul{max-width:44rem;margin:1rem auto;padding-left:1.4rem}
p.blurb{max-width:44rem;margin:1rem auto;color:#888;font-size:.9rem;
        line-height:1.5}
"""


def render_priority_page(
    data: PriorityData, version: str = "software version unknown"
) -> str:
    slug = player_slug(data.username)
    feed_href = f"{urllib.parse.quote(slug)}.xml"
    page_href = f"{urllib.parse.quote(slug)}.html"
    player_href = f"../players/{urllib.parse.quote(slug)}.html"
    flat = {
        skill
        for skill in SKILLS
        if not any(rate for _, rate in data.charts[skill].gains)
    }
    parts: list[str] = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{_esc(data.username.lower())} priority - xp tracker</title>",
        '<link rel="alternate" type="application/rss+xml" '
        f'title="{_esc(data.username)} priority xp" href="{_esc(feed_href)}">',
        f"<style>{_PLAYER_CSS}{SKILL_TABS_CSS}{_PRIORITY_CSS}</style></head>"
        '<body id="top">',
        '<div class="backlinks"><p class="sub">'
        '<a href="../latest.html">all rankings</a> · '
        '<a href="index.html">priority tracking</a> · '
        f'<a href="{_esc(player_href)}">player page</a></p></div>',
        f"<h1>{_esc(data.username)}</h1>",
        f'<h2 class="subheading"><a href="{_esc(highscores_url(data.username))}">'
        "vidyascape highscores</a></h2>",
        f'<h2 class="subheading"><a href="{_esc(feed_href)}">rss feed</a></h2>',
        f'<p class="meta">generated at: {_esc(data.generated)}</p>',
        '<p class="meta">priority report: 10-minute updates, last 24 hours</p>',
        skill_tabs_html(flat),
        '<div class="stats">',
    ]

    for index, skill in enumerate(SKILLS):
        value = data.current.get(skill)
        chart = data.charts[skill]
        regular_key = (
            '<span class="key regular-key">regular data</span>'
            if chart.regular_xp or chart.regular_gains else ""
        )
        parts.append(skill_panel_open(index, skill))
        parts.append(f"<h2>{_esc(skill)}</h2>")
        parts.append(
            '<p class="meta">current xp: '
            f"{_current_line(skill, value, data.current)}</p>"
        )
        label = f"{skill} last 24 hours: total xp and xp/h"
        chart_svg = _chart_svg(
            chart, label, width=720, height=200,
            time_axis=True, ticks=7, segmented_area=True,
        )
        parts.append(
            '<figure class="big"><figcaption><b>last 24 hours</b>'
            '<span class="key line-key">total xp</span>'
            '<span class="key area-key">xp/h</span>'
            f"{regular_key}</figcaption>"
            f"{chart_svg}"
            "</figure>"
        )
        parts.append("</section>")

    parts.append("</div>")
    parts.append(SKILL_TAB_SCRIPT)
    parts.append(
        '<a class="to-top" href="#top" aria-label="back to top">'
        "&#8593; Top</a>"
    )
    parts.append(f'<footer class="version">{_esc(version)}</footer>')
    parts.append("</body></html>")
    return "\n".join(parts) + "\n"


def render_priority_index(
    players: list[str], version: str = "software version unknown"
) -> str:
    items = "".join(
        f'<li><a href="{urllib.parse.quote(player_slug(name))}.html">'
        f"{_esc(name)}</a> · "
        f'<a href="{urllib.parse.quote(player_slug(name))}.xml">rss</a></li>'
        for name in players
    )
    if not items:
        items = "<li>no players tracked right now.</li>"
    return "\n".join([
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>priority tracking - xp tracker</title>",
        f"<style>{_PLAYER_CSS}{_PRIORITY_CSS}</style>"
        '</head><body id="top">',
        '<div class="backlinks"><p class="sub">'
        '<a href="../latest.html">all rankings</a></p></div>',
        "<h1>priority tracking</h1>",
        '<p class="blurb">the top 5 players by overall xp, and any players '
        "added to the priority list file, are fetched every 10 minutes. "
        "each page and rss feed covers the last 24 hours, with regular "
        "snapshots filling the gaps in orange. players who leave the list "
        "stay here until their high-frequency data is pruned.</p>",
        f"<ul>{items}</ul>",
        '<a class="to-top" href="#top" aria-label="back to top">'
        "&#8593; Top</a>",
        f'<footer class="version">{_esc(version)}</footer>',
        "</body></html>",
    ]) + "\n"


def write_priority_reports(
    conn: psycopg.Connection,
    report_dir: Path,
    players: tuple[str, ...] | list[str],
    now_epoch: int,
    generated: str,
    base_url: str,
    version: str = "software version unknown",
) -> int:
    directory = report_dir / "priority"
    directory.mkdir(parents=True, exist_ok=True)
    expected: set[str] = {"index.html"}
    written = 0
    seen: set[str] = set()
    resolved: list[str] = []
    for username in players:
        canonical = canonical_username(conn, username) or username
        if canonical in seen:
            continue
        seen.add(canonical)
        resolved.append(canonical)
        slug = player_slug(canonical)
        stem = urllib.parse.quote(slug)
        data = collect_priority_data(conn, canonical, now_epoch, generated)
        events = collect_events(conn, canonical, now_epoch)
        _atomic_write(
            directory / f"{stem}.html",
            render_priority_page(data, version),
        )
        _atomic_write(
            directory / f"{stem}.xml",
            render_feed(canonical, slug, events, now_epoch, base_url),
        )
        expected.add(f"{stem}.html")
        expected.add(f"{stem}.xml")
        written += 1
    _atomic_write(
        directory / "index.html", render_priority_index(resolved, version)
    )
    for pattern in ("*.html", "*.xml", ".*.tmp"):
        for path in directory.glob(pattern):
            if path.name not in expected:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
    return written
