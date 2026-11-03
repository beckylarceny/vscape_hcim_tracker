"""HTML status page and coverage bar."""

from __future__ import annotations

from ..render.html import (
    SITE_TITLE,
    date,
    esc,
    site_header,
    style_tag,
    tooltip_html,
)
from .queries import (
    FrequencyStatus,
    StatusData,
    backfill_rows,
    coverage_segments,
    database_rows,
    date_or_dash,
    frequency_rows,
    live_rows,
    num_or_dash,
    players_rows,
    priority_rows,
    service_rows,
    stamp,
    table_rows,
)


def _fact_rows(rows: list[tuple[str, str]]) -> str:
    return (
        '<table class="facts"><tbody>'
        + "".join(
            f"<tr><th>{label}</th><td>{esc(value)}</td></tr>"
            for label, value in rows
        )
        + "</tbody></table>"
    )


def _container_table(containers) -> str:
    rows = "".join(
        f'<tr><th scope="row">{esc(container.service)}</th>'
        f"<td>{esc(container.name)}</td>"
        f"<td>{esc(container.state)}</td>"
        f"<td>{esc(container.status)}</td>"
        f"<td>{esc(container.image)}</td></tr>"
        for container in containers.containers
    )
    if not rows:
        message = (
            f"docker api unavailable: {containers.error}"
            if containers.error
            else "no containers found"
        )
        rows = (
            f'<tr><td class=empty colspan="5">{esc(message)}</td></tr>'
        )
    return (
        '<table class="grid containers"><thead><tr><th>service</th>'
        "<th>container</th><th>state</th><th>status</th>"
        "<th>image</th></tr></thead><tbody>"
        + rows
        + "</tbody></table>"
    )


def _frequency_table(
    entries: tuple[FrequencyStatus, ...], heading: str
) -> str:
    rows = "".join(
        f'<tr><th scope="row">{esc(name)}</th>'
        f'<td class="num">{count:,}</td>'
        f'<td class="date">{date_or_dash(first)}</td>'
        f'<td class="date">{date_or_dash(last)}</td></tr>'
        for name, count, first, last in frequency_rows(entries)
    )
    return (
        '<table class="grid"><thead><tr><th>interval</th>'
        f'<th class="num">{heading}</th><th class="date">first</th>'
        '<th class="date">last</th></tr></thead><tbody>'
        + rows
        + "</tbody></table>"
    )


def coverage_bar(data: StatusData) -> str:
    total = max(1, data.coverage_total)
    segments: list[str] = []
    for length, present, density, label in coverage_segments(data):
        ratio = min(1.0, present / total)
        if present:
            css = f"density-{density}"
            style = (
                f' style="flex:{length};'
                f'opacity:{0.15 + 0.85 * ratio:.2f}"'
            )
            segments.append(
                f'<span class="{css}" data-tip="{esc(label)}"{style}></span>'
            )
        else:
            segments.append(
                f'<span class="zero" style="flex:{length}" '
                f'data-tip="{esc(label)}"></span>'
            )
    if not segments:
        segments.append('<span class="zero"></span>')
    first = (
        date(data.coverage[0].start_epoch)
        if data.coverage else date(data.now_epoch)
    )
    last = date(data.now_epoch)
    return (
        '<div class="coverage" role="img" '
        f'aria-label="data coverage from {first} to {last}">'
        + "".join(segments)
        + "</div>"
        f'<div class="range"><span>{first}</span><span>{last}</span></div>'
        + coverage_legend()
    )


def coverage_legend() -> str:
    """Colour key for the fetch density shown in the coverage bar."""
    return (
        '<div class="coverage-legend">'
        '<span class="density-hourly">hourly</span>'
        '<span class="density-six-hour">6-hour</span>'
        '<span class="density-daily">daily</span>'
        '<span class="density-sparse">sparse</span>'
        "</div>"
    )


def coverage_tip_html() -> str:
    return tooltip_html(".coverage span[data-tip]", "coverage-tip")


def render_status_page(data: StatusData) -> str:
    tables = "".join(
        f'<tr><th scope="row">{esc(name)}</th>'
        f'<td class="num">{num_or_dash(rows)}</td></tr>'
        for name, rows in table_rows(data)
    )
    runs = "".join(
        f'<tr><th scope="row" class=rank>{run.id}</th>'
        f"<td>{esc(stamp(run.started_epoch))}</td>"
        f"<td>{esc(run.status)}</td>"
        f'<td class="num">{num_or_dash(run.requests)}</td>'
        f'<td class="num">{run.failures:,}</td>'
        f'<td class="num">{num_or_dash(run.players)}</td></tr>'
        for run in data.runs
    )
    if not runs:
        runs = '<tr><td class=empty colspan="6">(no runs yet)</td></tr>'

    parts: list[str] = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>status - {SITE_TITLE}</title>",
        style_tag() + "</head>",
        '<body id="top" class="status">',
        '<div class="backlinks"><p class="sub">'
        '<a href="general">general tracking</a> · '
        '<a href="priority/">priority tracking</a></p></div>',
        site_header(),
        "<h1>status</h1>",
        '<p class="sub"><a href="status.txt">text version</a></p>',
        f'<p class="meta">generated at: {esc(data.generated)}</p>',
        "<h2>data coverage</h2>",
        '<p class="note">each segment is an equal slice of the lookback '
        "window, fading with the share of players that hold a checkpoint "
        "there.</p>",
        coverage_bar(data),
        coverage_tip_html(),
        "<h2>service</h2>",
        _fact_rows(service_rows(data)),
        "<h3>containers</h3>",
        _container_table(data.containers),
        "<h2>database</h2>",
        _fact_rows(database_rows(data)),
        "<h3>tables</h3>",
        '<table class="grid"><thead><tr><th>table</th>'
        '<th class="num">rows</th></tr></thead><tbody>'
        + tables
        + "</tbody></table>",
        '<p class="note">players and runs are counted exactly; snapshot '
        "and checkpoint tables use the planner estimate.</p>",
        "<h2>backfill</h2>",
        _fact_rows(backfill_rows(data)),
        "<h2>live</h2>",
        _fact_rows(live_rows(data)),
        "<h2>priority</h2>",
        _fact_rows(priority_rows(data)),
        "<h2>players</h2>",
        _fact_rows(players_rows(data)),
        "<h2>snapshots</h2>",
        _frequency_table(data.snapshots, "snapshots"),
        _frequency_table(data.fetches, "checkpoints"),
        '<p class="note">snapshots store only changed xp; checkpoints record '
        "every fetch.</p>",
        "<h2>recent runs</h2>",
        '<table class="grid runs"><thead><tr><th>#</th>'
        '<th>started (UTC)</th>'
        '<th>status</th><th class="num">requests</th>'
        '<th class="num">failures</th><th class="num">players</th>'
        "</tr></thead><tbody>" + runs + "</tbody></table>",
        '<a class="to-top" href="#top" aria-label="back to top">'
        "&#8593; Top</a>",
        f'<footer class="version">{esc(data.version)}</footer>',
        "</body></html>",
    ]
    return "\n".join(parts) + "\n"
