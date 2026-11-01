"""Per-player HTML reports with inline SVG xp charts."""

from __future__ import annotations

import bisect
import heapq
import html
import re
import urllib.parse
from typing import TYPE_CHECKING, NamedTuple, TypeVar

if TYPE_CHECKING:
    import psycopg

from .config import SKILLS, SPANS
from .storage import as_datetime

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


class PlayerData(NamedTuple):
    username: str
    generated: str
    now_epoch: int
    lookback: int
    current: dict[str, int]
    past: dict[str, dict[str, int | None]]
    best: dict[str, dict[str, Record | None]]
    charts: dict[str, dict[str, ChartData]]
    coverage: list[tuple[int, bool]] = ()


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
    points: dict[str, list[tuple[int, int]]] = {}
    for skill, observed_at, xp in rows:
        points.setdefault(skill, []).append(
            (int(observed_at.timestamp()), int(xp))
        )
    return points


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


def _chart_data(
    epochs: list[int],
    points: list[tuple[int, int]],
    span: int,
    now_epoch: int,
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
        start, now_epoch, _thin(line, 180), _thin(gains, 180)
    )


def _coverage_buckets(
    conn: psycopg.Connection,
    username: str,
    start_epoch: int,
    end_epoch: int,
    lookback: int,
) -> list[tuple[int, bool]]:
    """Whether the player has a checkpoint in each day-sized bucket."""
    buckets = max(30, min(500, (lookback + 86399) // 86400))
    rows = conn.execute(
        """
        SELECT observed_at
        FROM checkpoints
        WHERE username = %s AND observed_at >= %s AND observed_at <= %s
        """,
        (username, as_datetime(start_epoch), as_datetime(end_epoch)),
    ).fetchall()
    present = [False] * buckets
    width = (end_epoch + 1 - start_epoch) / buckets
    for (observed_at,) in rows:
        index = int((int(observed_at.timestamp()) - start_epoch) / width)
        if 0 <= index < buckets:
            present[index] = True
    return [
        (int(start_epoch + index * width), present[index])
        for index in range(buckets)
    ]


def collect_player_data(
    conn: psycopg.Connection,
    username: str,
    now_epoch: int,
    lookback: int,
    slack: int,
    generated: str,
) -> PlayerData:
    """Load one player's stored history and derive every displayed value."""
    points_by_skill = _points_by_skill(
        conn, username, now_epoch - lookback - max(SPANS.values())
    )
    spans = list(SPANS.items())

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
            charts[skill][name] = _chart_data(
                epochs, points, span, now_epoch
            )

    coverage = _coverage_buckets(
        conn, username, now_epoch - lookback, now_epoch, lookback
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
        coverage=coverage,
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


_PLAYER_CSS = """
:root{color-scheme:light dark}
*{box-sizing:border-box}
body{font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;margin:0 auto;
     padding:2rem 1rem;max-width:64rem;text-transform:lowercase}
h1{font-size:1.9rem;margin:0 0 .35rem}
h2{font-size:1.5rem;margin:2.5rem 0 .5rem;border-bottom:1px solid #8884;
   padding-bottom:.3rem}
h3{font-size:1rem;margin:1.2rem 0 .3rem;text-transform:lowercase;
   letter-spacing:.04em;color:#888}
h4{font-size:.85rem;margin:.5rem 0 .2rem;color:#888;text-transform:lowercase;
   letter-spacing:.04em}
.stats h2,.stats h3,.stats h4{text-align:center}
.sub{margin:1rem 0 1.5rem;padding-bottom:.9rem;
     border-bottom:1px solid #8884}
.backlinks{margin:-2rem 0 1.75rem;text-align:center}
.backlinks .sub{margin:0;padding:0;border:0;font-size:.9rem}
.backlinks .sub + .sub{margin-top:.15rem}
.subheading{font-size:1rem;font-weight:400;margin:.2rem 0 .8rem;
            border:0;padding:0}
.priority-link{margin:.1rem 0 .9rem}
.meta{color:#888;font-size:.85rem;margin:0 0 .15rem}
.coverage{display:flex;gap:1px;height:14px;margin:.3rem 0 .1rem;
          background:#8882;overflow:hidden}
.coverage span{flex:1;background:#9079B6;min-width:1px}
.coverage span.zero{background:#8883}
.range{display:flex;justify-content:space-between;color:#888;font-size:.75rem}
table{width:auto;max-width:100%;border-collapse:collapse;
      margin:.25rem auto .75rem;table-layout:auto}
th,td{text-align:left;padding:.25rem .55rem;border-bottom:1px solid #8884}
th{font-weight:600}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;
              white-space:nowrap}
td.date,th.date{text-align:right;font-variant-numeric:tabular-nums;
                white-space:nowrap}
tr:nth-child(even) td{background:#8881}
table.current{width:auto;max-width:32rem;margin-left:auto;margin-right:auto}
table.past th:nth-child(1),table.past td:nth-child(1){width:45%}
table.best th:nth-child(1),table.best td:nth-child(1){width:25%}
.gain-tables{display:grid;align-items:start;
             grid-template-columns:repeat(2,minmax(0,1fr));
             gap:.5rem 1.5rem}
.charts{display:grid;align-items:start;
        grid-template-columns:repeat(2,minmax(0,1fr));
        gap:.75rem 1rem}
figure{min-width:0;margin:0;padding:.5rem .6rem .35rem;
       border:1px solid #8883;border-radius:0}
figcaption{font-size:.8rem;color:#888;margin-bottom:.25rem}
.key{font-size:.7rem;color:#888;margin-left:.45rem}
.key::before{content:"";display:inline-block;width:.55em;height:.55em;
             margin-right:.25em}
.line-key::before{background:#9079B6}
.area-key::before{background:#7BB59A}
svg{display:block;width:100%;height:auto}
svg .grid{stroke:#8884;stroke-width:.5}
svg .line{fill:none;stroke:#9079B6;stroke-width:1.5;
          vector-effect:non-scaling-stroke}
svg .area{fill:#7BB59A33;stroke:#7BB59A33;stroke-width:1;
          vector-effect:non-scaling-stroke}
svg .dot{fill:#9079B6}
svg text{fill:#888;font-size:9px}
svg text.xp{fill:#9079B6}
svg text.gain{fill:#7BB59A}
.empty{color:#888;margin:.2rem 0}
a{color:#7BB59A}
.to-top{position:fixed;right:1rem;bottom:1rem;z-index:10;padding:.45rem .7rem;
        background:#9079B6;color:#fff;font-size:.85rem;line-height:1;
        text-decoration:none;border:1px solid #8886;border-radius:0;
        box-shadow:0 1px 4px #0003}
.to-top:hover{background:#7B659C;color:#fff}
@media (max-width:52rem){
  .charts,.gain-tables{grid-template-columns:1fr}
}
@media (max-width:40rem){
  body{padding:1.25rem .85rem}
  .backlinks{margin-top:-1.25rem}
  table{font-size:.85em}
  th,td{padding:.2rem .4rem}
}
"""


def _coverage_runs(
    coverage: list[tuple[int, bool]]
) -> list[tuple[int, int, bool]]:
    """(start index, length, covered) for equal-state runs."""
    runs: list[tuple[int, int, bool]] = []
    for index, (_, covered) in enumerate(coverage):
        if runs and runs[-1][2] == covered:
            start, length, _ = runs[-1]
            runs[-1] = (start, length + 1, covered)
        else:
            runs.append((index, 1, covered))
    return runs


def _coverage_bar(data: PlayerData) -> str:
    if not data.coverage:
        return ""
    segments: list[str] = []
    for index, length, covered in _coverage_runs(data.coverage):
        start_epoch = data.coverage[index][0]
        next_index = index + length
        end_epoch = (
            data.coverage[next_index][0]
            if next_index < len(data.coverage)
            else data.now_epoch
        )
        first = _date(start_epoch)
        last = _date(end_epoch)
        when = first if first == last else f"{first} - {last}"
        state = "covered" if covered else "no data"
        label = f"{when}: {state}"
        if covered:
            segments.append(
                f'<span title="{_esc(label)}" style="flex:{length}"></span>'
            )
        else:
            segments.append(
                f'<span class="zero" title="{_esc(label)}" '
                f'style="flex:{length}"></span>'
            )
    first = _date(data.coverage[0][0])
    last = _date(data.now_epoch)
    return (
        '<p class="meta">data coverage:</p>'
        '<div class="coverage" role="img" '
        f'aria-label="checkpoint coverage from {first} to {last}">'
        + "".join(segments)
        + "</div>"
        f'<div class="range"><span>{first}</span><span>{last}</span></div>'
    )


SKILL_TABS_CSS = """
.tabs{display:flex;flex-wrap:wrap;gap:.3rem;margin:1rem 0 1.5rem}
.tab{font:inherit;font-size:.85rem;padding:.35rem .8rem;border:1px solid #8886;
     border-radius:0;background:transparent;color:inherit;cursor:pointer}
.tab.flat{color:#888}
.tab[aria-selected="true"]{background:#9079B6;color:#fff;border-color:#9079B6}
.stats .tabs{margin-top:2.5rem}
.tab-panel[hidden]{display:none}
.tab-panel>h2:first-child{margin-top:.6rem}
"""

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


def render_player_page(data: PlayerData, is_priority: bool = False) -> str:
    parts: list[str] = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{_esc(data.username.lower())} - xp tracker</title>",
        f"<style>{_PLAYER_CSS}{SKILL_TABS_CSS}</style></head>"
        '<body id="top">',
        '<div class="backlinks">'
        '<p class="sub"><a href="../latest.html">all rankings</a></p>'
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
        f'<p class="meta">generated: {_esc(data.generated)}</p>',
        _coverage_bar(data),
        '<div class="stats">',
        "<h2>Current xp</h2>",
        _current_table(data),
        skill_tabs_html(),
    ]

    for index, skill in enumerate(SKILLS):
        parts.append(skill_panel_open(index, skill))
        parts.append(f"<h2>{_esc(skill)}</h2>")
        value = data.current.get(skill)
        if value is None:
            shown = "no data"
        elif skill == "overall":
            shown = _num(value)
            overall_level = total_level(data.current)
            if overall_level is not None:
                shown += f" (total level {overall_level})"
        else:
            shown = (
                f"{_num(value)} (level {level_for_xp(value)})"
            )
        parts.append(f'<p class="meta">current xp: {shown}</p>')
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
                '<span class="key area-key">xp/h</span></figcaption>'
                f"{_chart_svg(data.charts[skill][name], label)}"
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
    parts.append("</body></html>")
    return "\n".join(parts) + "\n"
