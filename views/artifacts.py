"""Artifacts window — rich grid/list views for Chrome Bookmarks · Instapaper.

Uses the shared media-card components for visual consistency with Media.
"""
from __future__ import annotations

from html import escape

from nicegui import ui

from lib.snapshot_store import latest_snapshot
from views._cards import render_grid, render_list


SOURCES = [
    ("chrome_bookmarks", "🔖", "Chrome Bookmarks"),
    ("instapaper",       "📥", "Instapaper"),
    ("instapaper_full",  "📖", "Instapaper Reading List"),
    ("chrome_tabs",      "🌐", "Chrome Open Tabs"),
    ("android_tabs",     "📱", "Android Open Tabs"),
]


def render(data: dict, **_) -> None:
    ui.html('<h1 class="page">🗂️ Artifacts</h1>')
    ui.html('<p class="page-sub">Saved content: Chrome bookmarks · Instapaper · open tabs (desktop + Android). '
            'All snapshots are double-backed-up.</p>')

    with ui.tabs().props("inline-label dense").style(
        "background: transparent; border-bottom: 1px solid var(--border); margin-bottom: 14px;"
    ) as tabs:
        for sid, icon, label in SOURCES:
            ui.tab(sid, label=f"{icon} {label}")

    with ui.tab_panels(tabs, value="chrome_bookmarks").style("background: transparent;"):
        for sid, icon, label in SOURCES:
            with ui.tab_panel(sid):
                _render_source(sid, icon, label)


def _render_source(source: str, icon: str, label: str) -> None:
    snap = latest_snapshot(source)
    if not snap or snap.get("status") != "ok":
        ui.html(f'''
        <div class="panel"><div class="pbody">
          <p style="color: var(--muted); margin: 0; font-size: 13px;">
            <b>{escape(label)}</b> not yet synced — configure in
            <a href="/settings" style="color: var(--accent);">Settings</a> first,
            then click Sync now.
          </p>
        </div></div>
        ''')
        return

    items = list(snap.get("items", []))

    # ---- stats strip ----
    starred_n = sum(1 for i in items if i.get("starred") or i.get("liked"))
    folders = {i.get("folder", "") for i in items if i.get("folder")}
    chips = (
        f'<span class="status-pill"><span style="color:var(--muted);">total</span> '
        f'<b style="color:var(--text);">{len(items):,}</b></span>'
    )
    if starred_n:
        chips += (f'<span class="status-pill"><span style="color:var(--muted);">starred</span> '
                  f'<b style="color:var(--danger);">♥ {starred_n}</b></span>')
    if folders:
        chips += (f'<span class="status-pill"><span style="color:var(--muted);">folders</span> '
                  f'<b style="color:var(--text);">{len(folders):,}</b></span>')
    ui.html(f'<div style="margin-bottom:14px;">{chips}</div>')

    # ---- controls ----
    state = {
        "view": "grid" if source != "chrome_bookmarks" else "list",  # bookmarks too dense for grid by default
        "starred_only": False,
        "cols": 6,
        "items": items,
        "search": "",
    }
    container = ui.element('div')

    def _filtered() -> list[dict]:
        out = state["items"]
        if state["starred_only"]:
            out = [i for i in out if (i.get("starred") or i.get("liked"))]
        q = state["search"].strip().lower()
        if q:
            out = [i for i in out
                   if q in (i.get("title", "") + " " + i.get("url", "") + " " + i.get("folder", "")).lower()]
        return out[:240]

    def _render():
        container.clear()
        show = _filtered()
        pairs = [(source, it) for it in show]
        with container:
            if not show:
                ui.html('<div class="panel"><div class="pbody">'
                        '<p style="color:var(--muted); margin:0;">No items match filters.</p></div></div>')
                return
            if state["view"] == "grid":
                ui.html(render_grid(pairs, cols=state["cols"]))
            else:
                ui.html(render_list(pairs))

    with ui.row().style("gap: 8px; align-items: center; margin-bottom: 14px; flex-wrap: wrap;"):
        search_inp = ui.input(placeholder="filter title / URL / folder…").props(
            "outlined dense stack-label clearable"
        ).style("min-width: 280px;")
        view_toggle = ui.toggle({"grid": "▦ Grid", "list": "☰ List"}, value=state["view"])
        if any(i.get("starred") or i.get("liked") for i in items):
            starred_toggle = ui.checkbox("♥ Starred only", value=False)
            def _on_star(e):
                state["starred_only"] = bool(e.value)
                _render()
            starred_toggle.on_value_change(_on_star)
        cols_select = ui.select({4: "4", 5: "5", 6: "6", 7: "7", 8: "8"}, value=6).props("dense").style("width: 80px;")

        def _on_search(e):
            state["search"] = e.value or ""
            _render()
        def _on_view_change(e):
            state["view"] = e.value
            _render()
        def _on_cols(e):
            state["cols"] = int(e.value)
            _render()

        search_inp.on_value_change(_on_search)
        view_toggle.on_value_change(_on_view_change)
        cols_select.on_value_change(_on_cols)

    _render()
