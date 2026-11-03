"""HTML, escaping and filesystem primitives shared by every report."""

from __future__ import annotations

import html
import json
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


def tooltip_html(selector: str, tip_id: str) -> str:
    """Tooltip element and script for elements with a `data-tip` attribute."""
    selector_json = json.dumps(selector)
    tip_json = json.dumps(tip_id)
    return (
        f'<div class="tip" id="{esc(tip_id)}" role="tooltip" hidden></div>'
        "<script>(function(){"
        f"var tip=document.getElementById({tip_json});"
        "if(!tip){return;}"
        "function place(x,y){"
        "tip.style.left='0px';tip.style.top='0px';"
        "var box=tip.getBoundingClientRect(),pad=8;"
        "var left=Math.min(Math.max(pad,x-box.width/2),"
        "window.innerWidth-box.width-pad);"
        "var top=y-box.height-14;"
        "if(top<pad){top=y+18;}"
        "tip.style.left=left+'px';tip.style.top=top+'px';}"
        "function show(item,x,y){"
        "tip.textContent=item.getAttribute('data-tip');"
        "tip.hidden=false;place(x,y);}"
        "function hide(){tip.hidden=true;tip.removeAttribute('data-target');}"
        "var coarse=window.matchMedia&&"
        "window.matchMedia('(hover: none)').matches;"
        f"var items=document.querySelectorAll({selector_json});"
        "Array.prototype.forEach.call(items,function(item){"
        "if(coarse){"
        "item.addEventListener('click',function(event){"
        "event.preventDefault();"
        "var target=item.getAttribute('data-tip');"
        "var same=!tip.hidden&&"
        "tip.getAttribute('data-target')===target;"
        "if(same){hide();return;}"
        "tip.setAttribute('data-target',target);"
        "show(item,event.clientX,event.clientY);});"
        "}else{"
        "item.addEventListener('mouseenter',function(event){"
        "show(item,event.clientX,event.clientY);});"
        "item.addEventListener('mousemove',function(event){"
        "place(event.clientX,event.clientY);});"
        "item.addEventListener('mouseleave',hide);"
        "}});"
        "document.addEventListener('click',function(event){"
        "var target=event.target;"
        "if(!target.closest||"
        f"!target.closest({selector_json}))"
        "{hide();}});"
        "})();</script>"
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
