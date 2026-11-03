"""Plain-text status report."""

from __future__ import annotations

from ..render.html import SITE_TITLE
from .queries import (
    FrequencyStatus,
    RunInfo,
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

LABEL_WIDTH = 28
TABLE_INDENT = 28


def _text_facts(rows: list[tuple[str, str]]) -> list[str]:
    return [
        f"{label + ':':<{LABEL_WIDTH}}{value}" for label, value in rows
    ]


def _text_table(
    headings: tuple[str, ...],
    rows: list[tuple[str, ...]],
    numeric: tuple[int, ...] = (),
) -> list[str]:
    widths = [
        max([len(heading)] + [len(row[index]) for row in rows])
        for index, heading in enumerate(headings)
    ]
    widths[0] = max(TABLE_INDENT, widths[0])

    def line(values: tuple[str, ...]) -> str:
        cells: list[str] = []
        for index, (value, width) in enumerate(zip(values, widths)):
            if index in numeric:
                cells.append(f"{value:>{width}}")
            else:
                cells.append(f"{value:<{width}}")
        return "   ".join(cells).rstrip()

    return [line(headings)] + [line(row) for row in rows]


def _text_containers(containers) -> list[str]:
    rows = [
        (
            container.service,
            container.name,
            container.state,
            container.status,
            container.image,
        )
        for container in containers.containers
    ]
    if not rows:
        message = (
            f"docker api unavailable: {containers.error}"
            if containers.error
            else "no containers found"
        )
        return [message]
    return _text_table(
        ("service", "container", "state", "status", "image"), rows
    )


def _text_tables(data: StatusData) -> list[str]:
    return _text_table(
        ("table", "rows"),
        [
            (name, num_or_dash(rows))
            for name, rows in table_rows(data)
        ],
        (1,),
    )


def _text_frequency(
    entries: tuple[FrequencyStatus, ...],
) -> list[str]:
    rows = [
        (
            name,
            f"{count:,}",
            date_or_dash(first),
            date_or_dash(last),
        )
        for name, count, first, last in frequency_rows(entries)
    ]
    return _text_table(("", "count", "first", "last"), rows, (1,))


def _text_runs(runs: tuple[RunInfo, ...]) -> list[str]:
    rows = [
        (
            str(run.id),
            stamp(run.started_epoch),
            run.status,
            num_or_dash(run.requests),
            f"{run.failures:,}",
            num_or_dash(run.players),
        )
        for run in runs
    ]
    lines = _text_table(
        (
            "#",
            "started (UTC)",
            "status",
            "requests",
            "failures",
            "players",
        ),
        rows,
        (0, 3, 4, 5),
    )
    if not runs:
        lines.append("(no runs yet)")
    return lines


def _coverage_labels(data: StatusData) -> list[str]:
    return [label for _, _, _, label in coverage_segments(data)]


def render_status_text(data: StatusData) -> str:
    lines = [f"{SITE_TITLE.upper()} STATUS"]
    lines.extend(_text_facts([("generated at", data.generated)]))
    lines.append("")
    sections = (
        ("DATA COVERAGE", _coverage_labels(data)),
        ("SERVICE", _text_facts(service_rows(data))),
        ("CONTAINERS", _text_containers(data.containers)),
        ("DATABASE", _text_facts(database_rows(data))),
        ("TABLES", _text_tables(data)),
        ("BACKFILL", _text_facts(backfill_rows(data))),
        ("LIVE", _text_facts(live_rows(data))),
        ("PRIORITY", _text_facts(priority_rows(data))),
        ("PLAYERS", _text_facts(players_rows(data))),
        ("SNAPSHOTS", _text_frequency(data.snapshots)),
        ("CHECKPOINTS", _text_frequency(data.fetches)),
        ("RECENT RUNS", _text_runs(data.runs)),
    )
    for heading, rows in sections:
        lines.append(heading)
        lines.extend(rows)
        lines.append("")
    return "\n".join(lines) + "\n\n" + data.version + "\n"
