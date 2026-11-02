"""Per-player HTML reports with inline SVG xp charts."""

from __future__ import annotations

import bisect
import heapq
import html
import os
import re
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, TypeVar

if TYPE_CHECKING:
    import psycopg

from .utility import (
    DATA_START_EPOCH,
    DEFAULT_PRIORITY_INTERVAL_SECONDS,
    SKILLS,
    SPANS,
)
from .storage import as_datetime
from . import styles

PLAYER_HIGHSCORES_URL = "https://vidyaplace.org/highscores?player={username}"

_UNSAFE_SLUG = re.compile(r"[^A-Za-z0-9._-]+")

_Number = TypeVar("_Number", int, float)

# Total xp needed for levels 1-99, from the OSRS experience table:
# https://oldschool.runescape.wiki/w/Experience#Levels_1%E2%80%9399
XP_THRESHOLDS: tuple[int, ...] = (
    0, 83, 174, 276, 388, 512, 650, 801, 969, 1154,
    1358, 1584, 1833, 2107, 2411, 2746, 3115, 3523, 3973, 4470,
    5018, 5624, 6291, 7028, 7842, 8740, 9730, 10824, 12031, 13363,
    14833, 16456, 18247, 20224, 22406, 24815, 27473, 30408, 33648, 37224,
    41171, 45529, 50339, 55649, 61512, 67983, 75127, 83014, 91721, 101333,
    111945, 123660, 136594, 150872, 166636, 184040, 203254, 224466,
    247886, 273742, 302288, 333804, 368599, 407015, 449428, 496254,
    547953, 605032, 668051, 737627, 814445, 899257, 992895, 1096278,
    1210421, 1336443, 1475581, 1629200, 1798808, 1986068, 2192818,
    2421087, 2673114, 2951373, 3258594, 3597792, 3972294, 4385776,
    4842295, 5346332, 5902831, 6517253, 7195629, 7944614, 8771558,
    9684577, 10692629, 11805606, 13034431,
)


class Record(NamedTuple):
    gain: int
    end_epoch: int
    username: str
    start_epoch: int


class ChartData(NamedTuple):
    start_epoch: int
    end_epoch: int
    xp: list[tuple[int, int]]
    gains: list[tuple[int, float]]
    regular_xp: frozenset[int] = frozenset()
    regular_gains: frozenset[int] = frozenset()
    missing: tuple[tuple[int, int], ...] = ()


class Frequency(NamedTuple):
    name: str
    snapshots: int
    first_epoch: int | None
    last_epoch: int | None


class PlayerData(NamedTuple):
    username: str
    generated: str
    now_epoch: int
    lookback: int
    current: dict[str, int]
    past: dict[str, dict[str, int | None]]
    best: dict[str, dict[str, Record | None]]
    charts: dict[str, dict[str, ChartData]]
    frequencies: tuple[Frequency, ...] = ()


def player_slug(username: str) -> str:
    """Filesystem- and URL-safe file stem for one player."""
    slug = _UNSAFE_SLUG.sub("_", username).strip("._-")
    return slug or "player"


def player_page_href(username: str) -> str:
    slug = player_slug(username)
    return f"players/{urllib.parse.quote(slug, safe='')}.html"


def highscores_url(username: str) -> str:
    return PLAYER_HIGHSCORES_URL.format(
        username=urllib.parse.quote(username, safe="")
    )


def level_for_xp(xp: int) -> int:
    """OSRS level for a skill's total xp, capped at 99."""
    return bisect.bisect_right(XP_THRESHOLDS, max(0, xp))


def total_level(current: dict[str, int]) -> int | None:
    """Sum of every skill's level; `overall` is not itself a skill."""
    levels = [
        level_for_xp(xp)
        for skill, xp in current.items()
        if skill != "overall"
    ]
    return sum(levels) if levels else None


def _years_months(seconds: int) -> str:
    days = seconds // 86400
    years, remainder = divmod(days, 365)
    months = remainder // 30
    years += months // 12
    months %= 12
    parts: list[str] = []
    if years:
        parts.append(f"{years} {'year' if years == 1 else 'years'}")
    if months:
        parts.append(f"{months} {'month' if months == 1 else 'months'}")
    if not parts:
        return f"{days} {'day' if days == 1 else 'days'}"
    return " ".join(parts)


def _bucket_count(lookback: int) -> int:
    """One coverage bucket per day, clamped to a readable bar."""
    return max(30, min(300, (lookback + 86399) // 86400))


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


def scan_player_points(
    username: str,
    points: list[tuple[int, int]],
    spans: list[tuple[str, int]],
    now_epoch: int,
    lookback: int,
    slack: int,
    top_n: int,
    heaps: dict[str, list[Record]],
) -> None:
    """Best window of each span, sliding across the whole lookback period.

    Stored points are sparse: a skill's xp is treated as constant between two
    stored points. A window therefore needs only one stored point at or before
    its start, and one at its end.
    """
    if not points:
        return
    oldest_start = now_epoch - lookback - slack
    pointers = {name: 0 for name, _ in spans}
    best: dict[str, Record | None] = {name: None for name, _ in spans}
    for end_epoch, end_xp in points:
        if end_epoch > now_epoch:
            continue
        for name, span in spans:
            start_epoch = end_epoch - span
            if start_epoch < oldest_start:
                continue
            pointer = pointers[name]
            while (
                pointer + 1 < len(points)
                and points[pointer + 1][0] <= start_epoch
            ):
                pointer += 1
            pointers[name] = pointer
            if points[pointer][0] > start_epoch:
                # No stored baseline at or before the window start.
                continue
            gain = end_xp - points[pointer][1]
            if gain <= 0:
                # Players sitting at zero xp do not get a record row.
                continue
            current = best[name]
            if current is None or gain > current.gain:
                best[name] = Record(
                    gain=gain,
                    end_epoch=end_epoch,
                    username=username,
                    start_epoch=start_epoch,
                )

    # One row per player: only their best window for each span.
    for name, _ in spans:
        record = best[name]
        if record is None:
            continue
        heap = heaps[name]
        if len(heap) < top_n:
            heapq.heappush(heap, record)
        elif record > heap[0]:
            heapq.heapreplace(heap, record)


def _date(epoch: int) -> str:
    return as_datetime(epoch).strftime("%Y-%m-%d")


def _atomic_write(path: Path, text: str) -> None:
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, path)


def _group_points(rows) -> dict[str, list[tuple[int, int]]]:
    points: dict[str, list[tuple[int, int]]] = {}
    for skill, observed_at, xp in rows:
        points.setdefault(skill, []).append(
            (int(observed_at.timestamp()), int(xp))
        )
    return points


def _points_by_skill(
    conn: psycopg.Connection, username: str, earliest: int
) -> dict[str, list[tuple[int, int]]]:
    rows = conn.execute(
        """
        SELECT skill, observed_at, xp
        FROM snapshots
        WHERE username = %s AND observed_at >= %s
        ORDER BY skill, observed_at
        """,
        (username, as_datetime(earliest)),
    ).fetchall()
    return _group_points(rows)


def _xp_at(
    epochs: list[int], points: list[tuple[int, int]], timestamp: int
) -> int | None:
    index = bisect.bisect_right(epochs, timestamp) - 1
    if index < 0:
        return None
    return points[index][1]


def _past_gain(
    epochs: list[int], points: list[tuple[int, int]], span: int, now_epoch: int
) -> int | None:
    latest = _xp_at(epochs, points, now_epoch)
    earlier = _xp_at(epochs, points, now_epoch - span)
    if latest is None or earlier is None:
        return None
    return max(0, latest - earlier)


def _thin(
    points: list[tuple[int, _Number]], limit: int
) -> list[tuple[int, _Number]]:
    """Evenly reduce a series while keeping its endpoints."""
    if len(points) <= limit:
        return points
    step = len(points) / (limit - 1)
    thinned = [points[0]]
    for index in range(1, limit - 1):
        thinned.append(points[int(round(index * step))])
    thinned.append(points[-1])
    return thinned


CHART_POINTS = 90
# Checkpoints are fetched at least once a day, so a longer break between them
# means that stretch of the window was never collected.
MISSING_AFTER_SECONDS = 36 * 3600
WIDE_CHART_POINTS = 160


def _chart_data(
    epochs: list[int],
    points: list[tuple[int, int]],
    span: int,
    now_epoch: int,
    max_points: int = CHART_POINTS,
) -> ChartData:
    """Total xp and xp/hour between stored points for the last `span`."""
    start = now_epoch - span
    if not points:
        return ChartData(start, now_epoch, [], [])

    index = bisect.bisect_right(epochs, start) - 1
    if index >= 0:
        # Include the point at or before the window start, pinned to the edge.
        line = [(start, points[index][1])]
        rest = points[index + 1:]
        previous_xp = points[index][1]
        previous_time = points[index][0]
    else:
        line = []
        rest = points
        previous_xp = None
        previous_time = None

    gains: list[tuple[int, float]] = []
    for timestamp, xp in rest:
        if timestamp > now_epoch:
            break
        line.append((timestamp, xp))
        if previous_xp is not None and previous_time is not None:
            elapsed = timestamp - previous_time
            if elapsed > 0:
                rate = max(0, xp - previous_xp) * 3600 / elapsed
                gains.append((timestamp, rate))
        previous_xp = xp
        previous_time = timestamp

    if line and line[-1][0] < now_epoch:
        # No later observations: hold the last known xp flat to the right edge.
        line.append((now_epoch, line[-1][1]))

    return ChartData(
        start, now_epoch, _thin(line, max_points), _thin(gains, max_points)
    )


def _snapshot_times(
    conn: psycopg.Connection, username: str, earliest: int, table: str
) -> list[int]:
    """Distinct times at which this player has snapshots in one table."""
    if table not in ("snapshots", "priority_snapshots"):
        raise ValueError(table)
    rows = conn.execute(
        f"""
        SELECT DISTINCT observed_at
        FROM {table}
        WHERE username = %s AND observed_at >= %s
        ORDER BY observed_at
        """,
        (username, as_datetime(earliest)),
    ).fetchall()
    return [int(row[0].timestamp()) for row in rows]


def _checkpoint_times(
    conn: psycopg.Connection, username: str, start_epoch: int, end_epoch: int
) -> list[int]:
    """Times at which this player's xp was actually fetched."""
    rows = conn.execute(
        """
        SELECT observed_at
        FROM checkpoints
        WHERE username = %s AND observed_at >= %s AND observed_at <= %s
        ORDER BY observed_at
        """,
        (username, as_datetime(start_epoch), as_datetime(end_epoch)),
    ).fetchall()
    return [int(row[0].timestamp()) for row in rows]


def _frequency_name(seconds: int) -> str:
    if seconds < 3600:
        return f"{seconds // 60} minute"
    return f"{seconds // 3600:g} hour"


def _frequency_info(
    times: list[int],
    priority_times: list[int],
    live_interval: int,
    backfill_interval: int,
) -> tuple[Frequency, ...]:
    """Snapshot count and covered date range per fetching frequency."""
    live: list[int] = []
    backfill: list[int] = []
    for timestamp in times:
        target = backfill if timestamp % backfill_interval == 0 else live
        target.append(timestamp)

    entries = (
        (
            f"{_frequency_name(DEFAULT_PRIORITY_INTERVAL_SECONDS)} priority",
            priority_times,
        ),
        (_frequency_name(live_interval), live),
        (_frequency_name(backfill_interval), backfill),
    )
    return tuple(
        Frequency(
            name,
            len(stamps),
            stamps[0] if stamps else None,
            stamps[-1] if stamps else None,
        )
        for name, stamps in entries
    )


def _missing_ranges(
    times: list[int], start_epoch: int, end_epoch: int, limit: int
) -> tuple[tuple[int, int], ...]:
    """Stretches of the window with fewer checkpoints than expected.

    The window edges use `limit`. Interior gaps are compared with the smallest
    nearby gap, so a skipped fetch inside a 6 hour cadence is flagged while a
    normal 24 hour backfill day is not.
    """
    if end_epoch <= start_epoch:
        return ()
    inside = [t for t in times if start_epoch <= t <= end_epoch]
    if not inside:
        return ((start_epoch, end_epoch),)
    gaps = [later - earlier for earlier, later in zip(inside, inside[1:])]
    ranges: list[tuple[int, int]] = []
    if inside[0] - start_epoch > limit:
        ranges.append((start_epoch, inside[0]))
    for index, gap in enumerate(gaps):
        local = min(gaps[max(0, index - 1): index + 2])
        if gap > limit or gap > 1.5 * local:
            ranges.append((inside[index], inside[index + 1]))
    if end_epoch - inside[-1] > limit:
        ranges.append((inside[-1], end_epoch))
    return tuple(ranges)


def collect_player_data(
    conn: psycopg.Connection,
    username: str,
    now_epoch: int,
    lookback: int,
    slack: int,
    generated: str,
    live_interval: int = 6 * 3600,
) -> PlayerData:
    """Load one player's stored history and derive every displayed value."""
    points_by_skill = _points_by_skill(conn, username, 0)
    spans = list(SPANS.items())
    all_start = min(DATA_START_EPOCH, now_epoch)
    missing_limit = max(MISSING_AFTER_SECONDS, 2 * live_interval)
    checkpoint_times = _checkpoint_times(
        conn, username, all_start, now_epoch
    )

    current: dict[str, int] = {}
    past: dict[str, dict[str, int | None]] = {}
    best: dict[str, dict[str, Record | None]] = {}
    charts: dict[str, dict[str, ChartData]] = {}

    for skill in SKILLS:
        points = points_by_skill.get(skill, [])
        epochs = [epoch for epoch, _ in points]

        latest = _xp_at(epochs, points, now_epoch)
        if latest is not None:
            current[skill] = latest

        heaps: dict[str, list[Record]] = {name: [] for name, _ in spans}
        scan_player_points(
            username, points, spans, now_epoch, lookback, slack, 1, heaps
        )

        past[skill] = {}
        best[skill] = {}
        charts[skill] = {}
        for name, span in spans:
            past[skill][name] = _past_gain(
                epochs, points, span, now_epoch
            )
            best[skill][name] = heaps[name][0] if heaps[name] else None
            chart = _chart_data(epochs, points, span, now_epoch)
            charts[skill][name] = chart._replace(
                missing=_missing_ranges(
                    checkpoint_times,
                    chart.start_epoch, now_epoch, missing_limit,
                )
            )
        all_span = max(1, now_epoch - all_start)
        all_chart = _chart_data(
            epochs, points, all_span, now_epoch, WIDE_CHART_POINTS
        )
        charts[skill]["all"] = all_chart._replace(
            missing=_missing_ranges(
                checkpoint_times, all_start, now_epoch, missing_limit,
            )
        )

    frequencies = _frequency_info(
        _snapshot_times(conn, username, now_epoch - lookback, "snapshots"),
        _snapshot_times(
            conn, username, now_epoch - lookback, "priority_snapshots"
        ),
        live_interval,
        slack,
    )

    return PlayerData(
        username=username,
        generated=generated,
        now_epoch=now_epoch,
        lookback=lookback,
        current=current,
        past=past,
        best=best,
        charts=charts,
        frequencies=frequencies,
    )


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _num(value: int) -> str:
    return f"{value:,}"


def _short_number(value: float) -> str:
    value = max(0.0, float(value))
    for size, suffix in ((1_000_000_000, "B"), (1_000_000, "M"),
                         (1_000, "k")):
        if value >= size:
            text = f"{value / size:.1f}".removesuffix(".0")
            return f"{text}{suffix}"
    if value >= 100:
        return f"{value:,.0f}"
    return f"{value:,.1f}".removesuffix(".0")


def _chart_svg(
    chart: ChartData,
    label: str,
    width: int = 360,
    height: int = 118,
    *,
    time_axis: bool = False,
    ticks: int = 2,
    segmented_area: bool = False,
) -> str:
    if not chart.xp and not chart.gains:
        return '<p class="empty">(no data)</p>'

    left, right, top, bottom = 46, 46, 10, 18
    plot_width = width - left - right
    plot_height = height - top - bottom
    window = max(1, chart.end_epoch - chart.start_epoch)

    def x(timestamp: int) -> float:
        ratio = (timestamp - chart.start_epoch) / window
        return left + plot_width * min(1.0, max(0.0, ratio))

    xp_values = [value for _, value in chart.xp]
    gain_values = [value for _, value in chart.gains]
    xp_min = min(xp_values) if xp_values else 0
    xp_max = max(xp_values) if xp_values else 0
    gain_max = max(gain_values) if gain_values else 0
    xp_span = xp_max - xp_min
    gain_span = gain_max or 1

    def y_xp(value: int) -> float:
        if not xp_span:
            return top + plot_height / 2
        ratio = (value - xp_min) / xp_span
        return top + plot_height * (1 - ratio)

    def y_gain(value: float) -> float:
        return top + plot_height * (1 - value / gain_span)

    baseline = top + plot_height
    parts = [
        f'<svg viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="{_esc(label)}">',
        f"<title>{_esc(label)}</title>",
    ]
    for gap_start, gap_end in chart.missing:
        gap_left = x(gap_start)
        gap_right = x(gap_end)
        if gap_right > gap_left:
            parts.append(
                f'<rect class="gap" x="{gap_left:.1f}" y="{top:.1f}" '
                f'width="{gap_right - gap_left:.1f}" '
                f'height="{plot_height:.1f}"/>'
            )
    for step in range(3):
        line_y = top + plot_height * step / 2
        parts.append(
            f'<line class="grid" x1="{left}" y1="{line_y:.1f}" '
            f'x2="{width - right}" y2="{line_y:.1f}"/>'
        )

    if time_axis:
        count = max(2, ticks)
        for index in range(count):
            ratio = index / (count - 1)
            tick_x = left + plot_width * ratio
            timestamp = int(chart.start_epoch + window * ratio)
            parts.append(
                f'<line class="grid" x1="{tick_x:.1f}" y1="{top:.1f}" '
                f'x2="{tick_x:.1f}" y2="{baseline:.1f}"/>'
            )
            anchor = (
                "start" if index == 0
                else "end" if index == count - 1
                else "middle"
            )
            parts.append(
                f'<text x="{tick_x:.1f}" y="{height - 3}" '
                f'text-anchor="{anchor}">'
                f"{as_datetime(timestamp).strftime('%H:%M')}</text>"
            )

    if chart.gains:
        if segmented_area:
            previous_time = chart.start_epoch
            for timestamp, value in chart.gains:
                start_x = x(previous_time)
                end_x = x(timestamp)
                if end_x > start_x:
                    top_y = y_gain(value)
                    css = (
                        "area regular"
                        if timestamp in chart.regular_gains
                        else "area"
                    )
                    area = (
                        f"{start_x:.1f},{baseline:.1f} "
                        f"{start_x:.1f},{top_y:.1f} "
                        f"{end_x:.1f},{top_y:.1f} "
                        f"{end_x:.1f},{baseline:.1f}"
                    )
                    parts.append(
                        f'<polygon class="{css}" points="{area}"/>'
                    )
                previous_time = max(previous_time, timestamp)
        else:
            first_time, first_gain = chart.gains[0]
            area = [
                f"{x(first_time):.1f},{baseline:.1f}",
                f"{x(first_time):.1f},{y_gain(first_gain):.1f}",
            ]
            previous_gain = first_gain
            for timestamp, value in chart.gains[1:]:
                # Hold the previous gain until this point, then step.
                area.append(f"{x(timestamp):.1f},{y_gain(previous_gain):.1f}")
                area.append(f"{x(timestamp):.1f},{y_gain(value):.1f}")
                previous_gain = value
            area.append(f"{x(chart.gains[-1][0]):.1f},{baseline:.1f}")
            parts.append(f'<polygon class="area" points="{" ".join(area)}"/>')

    if len(chart.xp) > 1:
        if chart.regular_xp:
            for (t1, v1), (t2, v2) in zip(chart.xp, chart.xp[1:]):
                css = (
                    "line regular"
                    if t1 in chart.regular_xp or t2 in chart.regular_xp
                    else "line"
                )
                parts.append(
                    f'<line class="{css}" x1="{x(t1):.1f}" '
                    f'y1="{y_xp(v1):.1f}" x2="{x(t2):.1f}" '
                    f'y2="{y_xp(v2):.1f}"/>'
                )
        else:
            points = " ".join(
                f"{x(timestamp):.1f},{y_xp(value):.1f}"
                for timestamp, value in chart.xp
            )
            parts.append(f'<polyline class="line" points="{points}"/>')
    elif chart.xp:
        timestamp, value = chart.xp[0]
        css = "dot regular" if timestamp in chart.regular_xp else "dot"
        parts.append(
            f'<circle class="{css}" cx="{x(timestamp):.1f}" '
            f'cy="{y_xp(value):.1f}" r="2"/>'
        )

    parts.append(
        f'<text class="xp" x="{left - 3}" y="{top + 4}" '
        f'text-anchor="end">{_esc(_short_number(xp_max))}</text>'
    )
    parts.append(
        f'<text class="xp" x="{left - 3}" y="{baseline + 4}" '
        f'text-anchor="end">{_esc(_short_number(xp_min))}</text>'
    )
    parts.append(
        f'<text class="gain" x="{width - right + 3}" y="{top + 4}">'
        f"{_esc(_short_number(gain_max))}</text>"
    )
    parts.append(
        f'<text class="gain" x="{width - right + 3}" y="{baseline + 4}">'
        "0</text>"
    )
    if not time_axis:
        parts.append(
            f'<text x="{left}" y="{height - 3}">'
            f"{_date(chart.start_epoch)}</text>"
        )
        parts.append(
            f'<text x="{width - right}" y="{height - 3}" '
            f'text-anchor="end">{_date(chart.end_epoch)}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _current_table(data: PlayerData) -> str:
    rows: list[str] = []
    overall_level = total_level(data.current)
    for skill in SKILLS:
        value = data.current.get(skill)
        shown = _num(value) if value is not None else "-"
        if skill == "overall":
            level = str(overall_level) if overall_level is not None else "-"
        elif value is None:
            level = "-"
        else:
            level = str(level_for_xp(value))
        rows.append(
            f"<tr><td>{_esc(skill)}</td>"
            f'<td class="num">{level}</td>'
            f'<td class="num">{shown}</td></tr>'
        )
    return (
        '<table class="current"><thead><tr><th>Skill</th>'
        '<th class="num">Level</th><th class="num">XP</th></tr></thead>'
        "<tbody>" + "".join(rows) + "</tbody></table>"
    )


def _past_table(data: PlayerData, skill: str) -> str:
    rows: list[str] = []
    for name in SPANS:
        past = data.past[skill][name]
        shown = _num(past) if past is not None else "-"
        rows.append(
            f'<tr><td>{_esc(name.capitalize())}</td>'
            f'<td class="num">{shown}</td></tr>'
        )
    return (
        '<table class="past"><thead><tr><th>Span</th>'
        '<th class="num">XP</th></tr></thead><tbody>'
        + "".join(rows)
        + "</tbody></table>"
    )


def _best_table(data: PlayerData, skill: str) -> str:
    rows: list[str] = []
    for name in SPANS:
        record = data.best[skill][name]
        if record is None:
            cells = (
                '<td class="num">-</td><td class="date">-</td>'
                '<td class="date">-</td>'
            )
        else:
            cells = (
                f'<td class="num">{_num(record.gain)}</td>'
                f'<td class="date">{_date(record.start_epoch)}</td>'
                f'<td class="date">{_date(record.end_epoch)}</td>'
            )
        rows.append(
            f"<tr><td>{_esc(name.capitalize())}</td>{cells}</tr>"
        )
    return (
        '<table class="best"><thead><tr><th>Span</th>'
        '<th class="num">XP</th><th class="num">Start</th>'
        '<th class="num">End</th></tr></thead><tbody>'
        + "".join(rows)
        + "</tbody></table>"
    )


SKILL_TAB_SCRIPT = (
    "<script>"
    "const TABS=document.querySelectorAll('[role=tab]');"
    "const PANELS=document.querySelectorAll('.tab-panel');"
    "function showSkill(name){"
    "TABS.forEach(function(tab){"
    "tab.setAttribute('aria-selected',"
    "tab.dataset.skill===name?'true':'false');});"
    "PANELS.forEach(function(panel){"
    "panel.hidden=panel.id!=='skill-'+name;});"
    "if(location.hash!=='#skill-'+name){"
    "history.replaceState(null,'','#skill-'+name);}}"
    "TABS.forEach(function(tab){"
    "tab.addEventListener('click',function(){"
    "showSkill(tab.dataset.skill);"
    "const bar=document.getElementById('skill-tabs');"
    "if(bar.getBoundingClientRect().top<0){bar.scrollIntoView();}"
    "});});"
    "var initial=(location.hash||'').replace('#skill-','');"
    "if(document.getElementById('skill-'+initial)){showSkill(initial);}"
    "</script>"
)


def skill_tabs_html(flat: frozenset[str] | set[str] = frozenset()) -> str:
    buttons = "".join(
        f'<button class="tab{" flat" if skill in flat else ""}" '
        f'type="button" role="tab" '
        f'id="tab-{_esc(skill)}" data-skill="{_esc(skill)}" '
        f'aria-controls="skill-{_esc(skill)}" '
        f'aria-selected="{"true" if index == 0 else "false"}">'
        f"{_esc(skill)}</button>"
        for index, skill in enumerate(SKILLS)
    )
    return f'<div class="tabs" id="skill-tabs" role="tablist">{buttons}</div>'


def skill_panel_open(index: int, skill: str) -> str:
    hidden = "" if index == 0 else " hidden"
    return (
        f'<section id="skill-{_esc(skill)}" class="tab-panel" '
        f'role="tabpanel" aria-labelledby="tab-{_esc(skill)}"{hidden}>'
    )


def _frequency_meta(data: PlayerData) -> str:
    lines: list[str] = []
    for info in data.frequencies:
        if info.first_epoch is None:
            lines.append(
                f'<p class="meta">{_esc(info.name)} snapshots: 0</p>'
            )
            continue
        first = _date(info.first_epoch)
        last = _date(info.last_epoch)
        when = first if first == last else f"{first} to {last}"
        lines.append(
            f'<p class="meta">{_esc(info.name)} snapshots: '
            f"{info.snapshots}, {when}</p>"
        )
    return "".join(lines)


def render_player_page(
    data: PlayerData,
    is_priority: bool = False,
    version: str = "software version unknown",
) -> str:
    parts: list[str] = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{_esc(data.username.lower())} - xp tracker</title>",
        f"{styles.style_tag()}</head>",
        '<body id="top" class="player">',
        '<div class="backlinks">'
        '<p class="sub"><a href="../general.html">general tracking</a> · '
        '<a href="../status.html">status</a></p>'
        "</div>",
        f"<h1>{_esc(data.username)}</h1>",
        f'<h2 class="subheading"><a href="{_esc(highscores_url(data.username))}">'
        "Vidyascape highscores</a></h2>",
        *(
            [
                '<p class="priority-link"><a href="../priority/'
                f'{urllib.parse.quote(player_slug(data.username))}.html">'
                "priority report</a></p>"
            ]
            if is_priority else []
        ),
        f'<p class="meta">generated at: {_esc(data.generated)}</p>',
        _frequency_meta(data),
        '<div class="stats">',
        "<h2>Current xp</h2>",
        _current_table(data),
        skill_tabs_html(),
    ]

    for index, skill in enumerate(SKILLS):
        parts.append(skill_panel_open(index, skill))
        parts.append(f"<h2>{_esc(skill)}</h2>")
        value = data.current.get(skill)
        parts.append(
            '<p class="meta">current xp: '
            f"{_current_line(skill, value, data.current)}</p>"
        )
        parts.append("<h3>Gains</h3>")
        parts.append('<div class="gain-tables">')
        parts.append(
            f"<div><h4>Past</h4>{_past_table(data, skill)}</div>"
        )
        parts.append(
            f"<div><h4>Best</h4>{_best_table(data, skill)}</div>"
        )
        parts.append("</div>")
        parts.append("<h3>Charts</h3>")
        parts.append('<div class="charts">')
        for name in SPANS:
            label = f"{skill} {name}: total xp and xp/h"
            parts.append(
                f"<figure><figcaption><b>{_esc(name)}</b>"
                '<span class="key line-key">total xp</span>'
                '<span class="key area-key">xp/h</span>'
                '<span class="key gap-key">missing data</span>'
                "</figcaption>"
                f"{_chart_svg(data.charts[skill][name], label)}"
                "</figure>"
            )
        parts.append(
            f'<figure class="wide"><figcaption><b>all time</b>'
            '<span class="key line-key">total xp</span>'
            '<span class="key area-key">xp/h</span>'
            '<span class="key gap-key">missing data</span>'
            "</figcaption>"
            f"{_chart_svg(data.charts[skill]['all'], f'{skill} all time: total xp and xp/h')}"
            "</figure>"
        )
        parts.append("</div>")
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
