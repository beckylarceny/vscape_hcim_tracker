"""HTML, escaping and filesystem primitives shared by every report."""

from __future__ import annotations

import html
import os
from functools import lru_cache
from pathlib import Path

from ..common.storage import as_datetime
from ..common.utility import SKILLS

_CSS_PATH = Path(__file__).with_name("report.css")

SITE_NAME = "vidyascape hcim xp tracker"


@lru_cache(maxsize=1)
def page_style() -> str:
    """Contents of report.css, read once per process."""
    return _CSS_PATH.read_text(encoding="utf-8")


def style_tag() -> str:
    return f"<style>{page_style()}</style>"


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def num(value: int) -> str:
    return f"{value:,}"


def short_number(value: float) -> str:
    value = max(0.0, float(value))
    for size, suffix in (
        (1_000_000_000, "B"), (1_000_000, "M"), (1_000, "k"),
    ):
        if value >= size:
            text = f"{value / size:.1f}".removesuffix(".0")
            return f"{text}{suffix}"
    if value >= 100:
        return f"{value:,.0f}"
    return f"{value:,.1f}".removesuffix(".0")


def date(epoch: int) -> str:
    return as_datetime(epoch).strftime("%Y-%m-%d")


def atomic_write(path: Path, text: str) -> None:
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, path)


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
        f'id="tab-{esc(skill)}" data-skill="{esc(skill)}" '
        f'aria-controls="skill-{esc(skill)}" '
        f'aria-selected="{"true" if index == 0 else "false"}">'
        f"{esc(skill)}</button>"
        for index, skill in enumerate(SKILLS)
    )
    return f'<div class="tabs" id="skill-tabs" role="tablist">{buttons}</div>'


def skill_panel_open(index: int, skill: str) -> str:
    hidden = "" if index == 0 else " hidden"
    return (
        f'<section id="skill-{esc(skill)}" class="tab-panel" '
        f'role="tabpanel" aria-labelledby="tab-{esc(skill)}"{hidden}>'
    )
