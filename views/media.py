"""Media window — visual, polished. Posters · ratings · hearts · grid/list toggle.

Default view: poster grid. Toggle for list. Per-source tabs: Letterboxd · YouTube · YouTube Music · Kindle · TV Time · Pocket Casts.
"""
from __future__ import annotations

from html import escape
from nicegui import ui

from lib.snapshot_store import latest_snapshot
from views._cards import render_grid, render_list


SOURCES = [
    ("letterboxd",    "🎬", "Letterboxd"),
    ("youtube",       "📺", "YouTube"),
    ("youtube_music", "🎵", "YouTube Music"),
    ("kindle",        "📖", "Kindle"),
    ("tvtime",        "📺", "TV Time"),
    ("pocketcasts",   "🎧", "Pocket Casts"),
]


def _liked_filter(items: list[dict], only_liked: bool) -> list[dict]:
    return [i for i in items if i.get("liked")] if only_liked else items


def render(data: dict, **_) -> None:
    ui.html('<h1 class="page">🎬 Media</h1>')
    ui.html('<p class="page-sub">Letterboxd · YouTube · YouTube Music · Kindle · TV Time · Pocket Casts — '
            'daily snapshots, double-backed-up.</p>')

    with ui.tabs().props("inline-label dense").style(
        "background: transparent; border-bottom: 1px solid var(--border); margin-bottom: 14px;"
    ) as tabs:
        for sid, icon, label in SOURCES:
            ui.tab(sid, label=f"{icon} {label}")

    with ui.tab_panels(tabs, value="letterboxd").style("background: transparent;"):
        for sid, icon, label in SOURCES:
            with ui.tab_panel(sid):
                if sid == "youtube":
                    _render_youtube_subtabs()
                elif sid == "youtube_music":
                    _render_youtube_music_subtabs()
                elif sid == "pocketcasts":
                    _render_pocketcasts_subtabs()
                else:
                    _render_source(sid, icon, label)


def _check_snapshot(source: str, label: str) -> dict | None:
    snap = latest_snapshot(source)
    if not snap or snap.get("status") != "ok":
        ui.html(f'''
        <div class="panel"><div class="pbody">
          <p style="color: var(--muted); margin: 0; font-size: 13px;">
            <b>{escape(label)}</b> not yet synced — configure in
            <a href="/settings" style="color: var(--accent);">Settings</a> first.
          </p>
        </div></div>
        ''')
        return None
    return snap


def _fetch_youtube_history() -> list[dict]:
    try:
        import httpx
        r = httpx.get("http://127.0.0.1:8000/api/v1/youtube/history", timeout=2.0)
        if r.status_code == 200:
            data = r.json()
            if data.get("status") == "ok":
                return data.get("items", [])
    except Exception:
        pass
    return []


def _parse_seen_date(item: dict) -> tuple:
    """Parse watched, added, or acquired date into a sortable tuple."""
    import datetime as dt
    import re

    val = item.get("watched") or item.get("added") or item.get("acquired") or ""
    if not val:
        return (0, 0, 0)
        
    val_clean = str(val).strip()
    if not val_clean:
        return (0, 0, 0)
        
    now = dt.datetime.now()
    if val_clean.lower() == "today":
        return (now.year, now.month, now.day)
    if val_clean.lower() == "yesterday":
        yest = now - dt.timedelta(days=1)
        return (yest.year, yest.month, yest.day)
        
    # Check for weekday (e.g. "Monday", "Tuesday", etc.)
    days_of_week = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    if val_clean.lower() in days_of_week:
        target_idx = days_of_week.index(val_clean.lower())
        current_idx = now.weekday()
        days_ago = (current_idx - target_idx) % 7
        if days_ago == 0:
            days_ago = 7
        target_date = now - dt.timedelta(days=days_ago)
        return (target_date.year, target_date.month, target_date.day)

    # Try standard formats: "May 25, 2026" or "May 25 2026"
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y", "%Y-%m-%d"):
        try:
            d = dt.datetime.strptime(val_clean, fmt)
            return (d.year, d.month, d.day)
        except ValueError:
            pass

    # Regex search for month + day
    months = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
    months_full = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]
    
    for i, mname in enumerate(months):
        if mname in val_clean.lower():
            day_match = re.search(r'\b\d{1,2}\b', val_clean)
            if day_match:
                day = int(day_match.group(0))
                year_match = re.search(r'\b(20\d{2})\b', val_clean)
                year = int(year_match.group(0)) if year_match else now.year
                return (year, i + 1, day)
                
    for i, mname in enumerate(months_full):
        if mname in val_clean.lower():
            day_match = re.search(r'\b\d{1,2}\b', val_clean)
            if day_match:
                day = int(day_match.group(0))
                year_match = re.search(r'\b(20\d{2})\b', val_clean)
                year = int(year_match.group(0)) if year_match else now.year
                return (year, i + 1, day)

    # Check for ISO format
    if len(val_clean) >= 10 and val_clean[4] == "-" and val_clean[7] == "-":
        try:
            return (int(val_clean[:4]), int(val_clean[5:7]), int(val_clean[8:10]))
        except ValueError:
            pass

    # Extract first 4-digit number as year fallback
    m = re.search(r'\b(19|20)\d{2}\b', val_clean)
    if m:
        return (int(m.group(0)), 1, 1)

    return (0, 0, 0)


def _render_items_view(source: str, items: list[dict], extra_lists: list[dict] = None) -> None:
    rated_items = [i for i in items if i.get("rating") is not None]
    avg = round(sum(i["rating"] for i in rated_items) / max(len(rated_items), 1), 2) if rated_items else 0
    liked_n = sum(1 for i in items if i.get("liked"))

    chips = (
        f'<span class="status-pill"><span style="color:var(--muted);">total</span> '
        f'<b style="color:var(--text);">{len(items):,}</b></span>'
    )
    if rated_items:
        chips += (
            f'<span class="status-pill"><span style="color:var(--muted);">rated</span> '
            f'<b style="color:var(--text);">{len(rated_items):,}</b></span>'
            f'<span class="status-pill"><span style="color:var(--muted);">average</span> '
            f'<b style="color:var(--ledger-txt);">★ {avg}</b></span>'
        )
    if liked_n:
        chips += (
            f'<span class="status-pill"><span style="color:var(--muted);">liked</span> '
            f'<b style="color:var(--danger);">♥ {liked_n}</b></span>'
        )
    if extra_lists:
        chips += (
            f'<span class="status-pill"><span style="color:var(--muted);">lists</span> '
            f'<b style="color:var(--text);">{len(extra_lists)}</b></span>'
        )

    ui.html(f'<div style="margin-bottom:14px;">{chips}</div>')

    state = {
        "view": "grid",
        "liked_only": False,
        "cols": 6,
        "items": items,
        "page": 1,
        "page_size": 240,
        "sort": "default"
    }
    container = ui.element('div')

    def _render():
        container.clear()
        filtered = _liked_filter(state["items"], state["liked_only"])
        
        # Apply sorting
        sort_val = state["sort"]
        if sort_val == "year_desc":
            filtered = sorted(filtered, key=lambda x: str(x.get("published") or x.get("year") or "")[:4], reverse=True)
        elif sort_val == "year_asc":
            filtered = sorted(filtered, key=lambda x: str(x.get("published") or x.get("year") or "")[:4])
        elif sort_val == "seen_desc":
            filtered = sorted(filtered, key=_parse_seen_date, reverse=True)
        elif sort_val == "seen_asc":
            filtered = sorted(filtered, key=_parse_seen_date)

        total_items = len(filtered)
        start = (state["page"] - 1) * state["page_size"]
        end = start + state["page_size"]
        show = filtered[start:end]
        pairs = [(source, it) for it in show]

        with container:
            if not show:
                ui.html('<div class="panel"><div class="pbody"><p style="color:var(--muted); margin:0;">No items match filters.</p></div></div>')
                return
            if state["view"] == "grid":
                ui.html(render_grid(pairs, cols=state["cols"]))
            else:
                ui.html(render_list(pairs))

            if total_items > state["page_size"]:
                max_page = (total_items + state["page_size"] - 1) // state["page_size"]
                with ui.row().style("gap: 12px; align-items: center; justify-content: center; margin-top: 20px; width: 100%;"):
                    prev_btn = ui.button("◀ Prev", on_click=lambda: _change_page(-1)).props("dense outline").style("color: var(--text-2); border: 1px solid var(--border);")
                    if state["page"] <= 1:
                        prev_btn.disable()
                    ui.label(f"Page {state['page']} of {max_page} ({total_items} items)").style("color: var(--muted); font-size: 13px;")
                    next_btn = ui.button("Next ▶", on_click=lambda: _change_page(1)).props("dense outline").style("color: var(--text-2); border: 1px solid var(--border);")
                    if state["page"] >= max_page:
                        next_btn.disable()

    def _change_page(delta):
        state["page"] += delta
        _render()

    with ui.row().style("gap: 8px; align-items: center; margin-bottom: 14px; flex-wrap: wrap;"):
        view_toggle = ui.toggle({"grid": "▦ Grid", "list": "☰ List"}, value="grid")
        
        liked_toggle = None
        if liked_n > 0:
            liked_toggle = ui.checkbox("♥ Liked only", value=False)
            
        cols_select = ui.select({4: "4 cols", 5: "5", 6: "6", 7: "7", 8: "8"}, value=6).props("dense").style("width: 100px;")

        # Sorting select
        sort_opts = {"default": "Default", "year_desc": "Year (Newest)", "year_asc": "Year (Oldest)"}
        if any("watched" in it or "added" in it or "acquired" in it for it in items):
            sort_opts["seen_desc"] = "Seen (Recent)"
            sort_opts["seen_asc"] = "Seen (Oldest)"
        sort_select = ui.select(sort_opts, value="default").props("dense").style("width: 140px;")

        def _on_view_change(e):
            state["view"] = e.value
            state["page"] = 1
            _render()
        def _on_liked_change(e):
            state["liked_only"] = bool(e.value)
            state["page"] = 1
            _render()
        def _on_cols_change(e):
            state["cols"] = int(e.value)
            _render()
        def _on_sort_change(e):
            state["sort"] = e.value
            state["page"] = 1
            _render()

        view_toggle.on_value_change(_on_view_change)
        if liked_toggle:
            liked_toggle.on_value_change(_on_liked_change)
        cols_select.on_value_change(_on_cols_change)
        sort_select.on_value_change(_on_sort_change)

    _render()

    if extra_lists:
        rows = ""
        for L in extra_lists[:50]:
            name = escape(str(L.get("name", "")))
            cnt = L.get("count") or len(L.get("items", []))
            url = L.get("url") or ""
            href = f' href="{escape(url)}" target="_blank"' if url else ""
            desc = escape(str(L.get("description") or "")[:200])
            rows += (
                f'<tr><td><a{href} style="color: var(--text); font-weight:500; text-decoration:none;">'
                f'{name}</a><br/><span style="color:var(--muted); font-size:11px;">{desc}</span></td>'
                f'<td style="text-align:right; color: var(--muted-soft);">{cnt} items</td></tr>'
            )
        ui.html(f'''
        <div class="panel" style="margin-top: 18px;">
          <div class="phead"><span class="ttl">📋 Lists · {len(extra_lists)}</span></div>
          <div class="pbody flush"><table class="stbl"><tbody>{rows}</tbody></table></div>
        </div>
        ''')


def _render_youtube_subtabs() -> None:
    snap = _check_snapshot("youtube_music", "YouTube")
    if not snap:
        return

    with ui.tabs().props("inline-label dense").style(
        "background: transparent; border-bottom: 1px solid var(--border); margin-bottom: 14px;"
    ) as subtabs:
        ui.tab("yt_liked", label="🎬 Liked Videos")
        ui.tab("yt_history", label="🕒 Watch History")
        ui.tab("yt_playlists", label="📋 Playlists")
        ui.tab("yt_subs", label="🔔 Subscriptions")

    with ui.tab_panels(subtabs, value="yt_liked").style("background: transparent;"):
        with ui.tab_panel("yt_liked"):
            liked_vids = [v for v in snap.get("items", []) if not v.get("is_music")]
            _render_items_view("youtube_music", liked_vids)
        with ui.tab_panel("yt_history"):
            history_items = _fetch_youtube_history()
            _render_items_view("youtube_music", history_items)
        with ui.tab_panel("yt_playlists"):
            _render_items_view("youtube_playlist", snap.get("playlists", []))
        with ui.tab_panel("yt_subs"):
            _render_items_view("youtube_subscription", snap.get("subscriptions", []))


def _render_youtube_music_subtabs() -> None:
    snap = _check_snapshot("youtube_music", "YouTube Music")
    if not snap:
        return

    with ui.tabs().props("inline-label dense").style(
        "background: transparent; border-bottom: 1px solid var(--border); margin-bottom: 14px;"
    ) as subtabs:
        ui.tab("ytm_liked", label="🎵 Liked Tracks")
        ui.tab("ytm_playlists", label="📋 Playlists")

    with ui.tab_panels(subtabs, value="ytm_liked").style("background: transparent;"):
        with ui.tab_panel("ytm_liked"):
            liked_tracks = [v for v in snap.get("items", []) if v.get("is_music")]
            _render_items_view("youtube_music", liked_tracks)
        with ui.tab_panel("ytm_playlists"):
            _render_items_view("youtube_playlist", snap.get("playlists", []))


def _render_pocketcasts_subtabs() -> None:
    snap = _check_snapshot("pocketcasts", "Pocket Casts")
    if not snap:
        return

    with ui.tabs().props("inline-label dense").style(
        "background: transparent; border-bottom: 1px solid var(--border); margin-bottom: 14px;"
    ) as subtabs:
        ui.tab("pc_subscribed", label="🎙️ Subscribed")
        ui.tab("pc_history", label="🕒 History")

    with ui.tab_panels(subtabs, value="pc_subscribed").style("background: transparent;"):
        with ui.tab_panel("pc_subscribed"):
            _render_items_view("pocketcasts", snap.get("items", []))
        with ui.tab_panel("pc_history"):
            _render_items_view("pocketcasts", snap.get("history", []))


def _render_source(source: str, icon: str, label: str) -> None:
    snap = latest_snapshot(source)
    if not snap or snap.get("status") != "ok":
        ui.html(f'''
        <div class="panel"><div class="pbody">
          <p style="color: var(--muted); margin: 0; font-size: 13px;">
            <b>{escape(label)}</b> not yet synced — configure in
            <a href="/settings" style="color: var(--accent);">Settings</a> first.
          </p>
        </div></div>
        ''')
        return

    items = list(snap.get("items", []))
    lists = snap.get("lists", [])

    _render_items_view(source, items, extra_lists=lists)
