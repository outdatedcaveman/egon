"""Memory & rules — read-only viewer for ~/.claude/.../memory/."""
from __future__ import annotations

import os
from html import escape
from pathlib import Path

from nicegui import ui

# Override with EGON_MEMORY_DIR; defaults to the Claude Code projects root.
MEMORY = Path(os.environ.get("EGON_MEMORY_DIR", str(Path.home() / ".claude" / "projects")))


def render(data: dict) -> None:
    ui.html('<h1 class="page">Memory & rules</h1>')
    ui.html(f'<p class="page-sub">Read-only viewer for <code>{escape(str(MEMORY))}</code>. '
            'Edit through your editor; changes show on reload.</p>')

    if not MEMORY.exists():
        ui.html('<div class="panel"><div class="pbody">'
                '<p style="color:var(--danger);">Memory folder missing.</p></div></div>')
        return

    files = sorted(MEMORY.glob("*.md"))
    if not files:
        ui.html('<div class="panel"><div class="pbody">'
                '<p style="color:var(--muted);">No memory files yet.</p></div></div>')
        return

    # tabs
    with ui.tabs().props("inline-label dense").style("background:transparent; "
                       "border-bottom:1px solid var(--border); margin-bottom:14px;") as tabs:
        for f in files:
            ui.tab(f.stem)

    with ui.tab_panels(tabs, value=files[0].stem).style("background:transparent;"):
        for f in files:
            with ui.tab_panel(f.stem):
                try:
                    text = f.read_text(encoding="utf-8")
                except Exception as e:
                    text = f"(error: {e})"
                ui.html(f"""
                <div class="panel">
                  <div class="phead">
                    <span class="ttl">{escape(f.name)}</span>
                    <span class="lnk">{f.stat().st_size:,} bytes</span>
                  </div>
                  <div class="pbody">
                    <div style="font-family:'Source Sans Pro',sans-serif; font-size:13px;
                                line-height:1.5; color:var(--text); max-height:60vh; overflow:auto;
                                white-space:pre-wrap; word-break:break-word;">{escape(text)}</div>
                  </div>
                </div>
                """)
