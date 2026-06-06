"""Placeholder views — replaced one-by-one in later phases."""
from __future__ import annotations

from nicegui import ui


def render(data: dict, title: str, sub: str) -> None:
    ui.html(f'<h1 class="page">{title}</h1>')
    ui.html(f'<p class="page-sub">{sub}</p>')
    ui.html(
        '<div class="panel"><div class="pbody">'
        '<p style="color:var(--muted); font-size: 13px;">Coming in a later phase. '
        'Schema slot reserved in <code>last_pass.json</code>; agent will populate it.</p>'
        '</div></div>'
    )
