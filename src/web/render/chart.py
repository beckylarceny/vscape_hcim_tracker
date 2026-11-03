"""Inline SVG xp charts shared by the player and priority pages."""

from __future__ import annotations

from typing import NamedTuple, TypeVar

from ...common.storage import as_datetime
from .html import date, esc, short_number

_Number = TypeVar("_Number", int, float)

CHART_POINTS = 90
# Checkpoints are fetched at least once a day, so a longer break between them
# means that stretch of the window was never collected.
MISSING_AFTER_SECONDS = 36 * 3600
WIDE_CHART_POINTS = 160


class StackedBar(NamedTuple):
    start_epoch: int
    end_epoch: int
    segments: tuple[tuple[str, float], ...]


class ChartData(NamedTuple):
    start_epoch: int
    end_epoch: int
    xp: list[tuple[int, int]]
    gains: list[tuple[int, float]]
    regular_xp: frozenset[int] = frozenset()
    regular_gains: frozenset[int] = frozenset()
    missing: tuple[tuple[int, int], ...] = ()
    stacked: tuple[StackedBar, ...] = ()


def thin(
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


def _bar_tip(
    skill: str, rate: float, start_epoch: int, end_epoch: int
) -> str:
    """Skill, xp gain, rate and UTC window for one stacked bar segment."""
    elapsed = max(0, end_epoch - start_epoch)
    gain = rate * elapsed / 3600
    first = as_datetime(start_epoch)
    last = as_datetime(end_epoch)
    if first.date() == last.date():
        when = f"{first:%H:%M}-{last:%H:%M}"
    else:
        when = f"{first:%m-%d %H:%M}-{last:%m-%d %H:%M}"
    return (
        f"{skill}: +{short_number(gain)} xp, "
        f"{short_number(rate)} xp/h ({when} UTC)"
    )


def chart_svg(
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
    stack_totals = [
        sum(rate for _, rate in bar.segments) for bar in chart.stacked
    ]
    xp_min = min(xp_values) if xp_values else 0
    xp_max = max(xp_values) if xp_values else 0
    gain_max = max(gain_values + stack_totals, default=0)
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
        f'aria-label="{esc(label)}">',
        f"<title>{esc(label)}</title>",
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

    if chart.stacked:
        for bar in chart.stacked:
            bar_start = x(max(bar.start_epoch, chart.start_epoch))
            bar_end = x(bar.end_epoch)
            if bar_end <= bar_start:
                continue
            bottom = baseline
            for skill, rate in bar.segments:
                if rate <= 0:
                    continue
                segment_height = plot_height * rate / gain_span
                top_y = bottom - segment_height
                tip = _bar_tip(skill, rate, bar.start_epoch, bar.end_epoch)
                parts.append(
                    f'<rect class="bar skill-{esc(skill)}" '
                    f'data-tip="{esc(tip)}" '
                    f'x="{bar_start:.1f}" y="{top_y:.1f}" '
                    f'width="{bar_end - bar_start:.1f}" '
                    f'height="{segment_height:.1f}"/>'
                )
                bottom = top_y
    elif chart.gains:
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
        f'text-anchor="end">{esc(short_number(xp_max))}</text>'
    )
    parts.append(
        f'<text class="xp" x="{left - 3}" y="{baseline + 4}" '
        f'text-anchor="end">{esc(short_number(xp_min))}</text>'
    )
    parts.append(
        f'<text class="gain" x="{width - right + 3}" y="{top + 4}">'
        f"{esc(short_number(gain_max))}</text>"
    )
    parts.append(
        f'<text class="gain" x="{width - right + 3}" y="{baseline + 4}">'
        "0</text>"
    )
    if not time_axis:
        parts.append(
            f'<text x="{left}" y="{height - 3}">'
            f"{date(chart.start_epoch)}</text>"
        )
        parts.append(
            f'<text x="{width - right}" y="{height - 3}" '
            f'text-anchor="end">{date(chart.end_epoch)}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)
