"""One stylesheet, inlined into every generated page."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_CSS_PATH = Path(__file__).with_name("report.css")


@lru_cache(maxsize=1)
def page_style() -> str:
    """Contents of report.css, read once per process."""
    return _CSS_PATH.read_text(encoding="utf-8")


def style_tag() -> str:
    return f"<style>{page_style()}</style>"
