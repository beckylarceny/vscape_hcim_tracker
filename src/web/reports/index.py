"""Site index linking the report sections."""

from __future__ import annotations

from pathlib import Path

from ..render.html import (
    SITE_TITLE,
    atomic_write,
    esc,
    site_header,
    style_tag,
)


def render_index(
    generated: str,
    player_count: int,
    priority_players: int,
    version: str = "software version unknown",
) -> str:
    """HTML home page with one link per report section."""
    sections = (
        (
            "general",
            "general tracking",
            "Skill rankings for the past day, week, month and year.",
        ),
        (
            "priority/",
            "priority tracking",
            f"{priority_players} players with 10-minute updates "
            "over the last 24 hours.",
        ),
        (
            "status",
            "status",
            "Collection schedules, data coverage, database and containers.",
        ),
    )
    links = "".join(
        f'<a class="section" href="{esc(href)}">'
        f"<h2>{esc(title)}</h2><p>{esc(description)}</p></a>"
        for href, title, description in sections
    )
    return "\n".join([
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{SITE_TITLE}</title>",
        f"{style_tag()}</head>",
        '<body id="top" class="home">',
        site_header(),
        "<h1>home</h1>",
        f'<p class="meta">generated at: {esc(generated)}</p>',
        f'<p class="meta">players tracked: {player_count:,}</p>',
        f'<div class="sections">{links}</div>',
        f'<footer class="version">{esc(version)}</footer>',
        "</body></html>",
    ]) + "\n"


def write_index_page(
    report_dir: Path,
    generated: str,
    player_count: int,
    priority_players: int,
    version: str = "software version unknown",
) -> Path:
    """Write index.html for the served report directory."""
    path = report_dir / "index.html"
    atomic_write(
        path,
        render_index(generated, player_count, priority_players, version),
    )
    return path
