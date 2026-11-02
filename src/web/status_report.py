"""Collect and write the status page and text report."""

from __future__ import annotations

from pathlib import Path

from .html import atomic_write
from .status_html import render_status_page
from .status_queries import collect_status
from .status_text import render_status_text


def write_status_page(conn, report_dir, **kwargs) -> Path:
    """Collect and write status.html and status.txt; return html's path."""
    data = collect_status(conn, **kwargs)
    page = render_status_page(data)
    text = render_status_text(data)
    directory = Path(report_dir)
    path = directory / "status.html"
    atomic_write(path, page)
    atomic_write(directory / "status.txt", text)
    return path
