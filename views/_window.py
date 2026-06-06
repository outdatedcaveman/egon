"""Reusable "window" template — a tabbed view with one tab per source.

Every "window" page (Artifacts, Media, References, Databases) is just a list of
adapters rendered through this template. Keeps view code minimal.
"""
from __future__ import annotations

from html import escape
from typing import Callable

from nicegui import ui

from lib.snapshot_store import latest_snapshot, list_snapshots


def _stat_chip(label: str, value: str, color: str = "var(--text)") -> str:
    return (f'<span class="status-pill">'
            f'<span style="color:var(--muted);">{escape(label)}</span> '
            f'<b style="color:{color};">{escape(str(value))}</b></span>')


def render_window(
    title: str,
    subtitle: str,
    adapters: list,                       # list of adapter modules with META
    actions_help: str = "",
) -> None:
    """Render a top-level Egon window with tabs, one per adapter."""
    ui.html(f'<h1 class="page">{escape(title)}</h1>')
    ui.html(f'<p class="page-sub">{subtitle}</p>')

    # status strip across all adapters in this window
    chips = ""
    for a in adapters:
        meta = a.META
        try:
            st = a.live_status()
        except Exception as e:
            st = {"status": "error", "error": str(e)}
        ok = st.get("status") == "ok"
        color = "var(--success)" if ok else ("var(--muted)" if st.get("status") == "unconfigured" else "var(--danger)")
        chips += _stat_chip(f"{meta['icon']} {meta['label']}", st.get("status", "?"), color)
    ui.html(f'<div style="margin-bottom: 18px;">{chips}</div>')

    # tabs
    with ui.tabs().props("inline-label dense").style(
        "background: transparent; border-bottom: 1px solid var(--border); margin-bottom: 14px;"
    ) as tabs:
        for a in adapters:
            ui.tab(a.META["id"], label=f'{a.META["icon"]} {a.META["label"]}')

    with ui.tab_panels(tabs, value=adapters[0].META["id"]).style("background: transparent;"):
        for a in adapters:
            with ui.tab_panel(a.META["id"]):
                _render_adapter_panel(a)

    if actions_help:
        ui.html(f'<p class="page-sub" style="margin-top: 18px;">{actions_help}</p>')


def _render_adapter_panel(adapter) -> None:
    meta = adapter.META
    try:
        stats = adapter.stats()
    except Exception as e:
        stats = {"status": "error", "error": str(e)}
    try:
        items = adapter.items(limit=50) if stats.get("status") == "ok" else []
    except Exception as e:
        items = []
        stats["error"] = str(e)

    # source metadata header
    n_snapshots = len(list_snapshots(meta["id"]))
    src_chips = (
        _stat_chip("count", str(stats.get("count", "—")))
        + _stat_chip("last sync", str(stats.get("last_synced") or "never"))
        + _stat_chip("snapshots", f"{n_snapshots} dates")
        + _stat_chip("status", stats.get("status", "?"),
                     color="var(--success)" if stats.get("status") == "ok" else "var(--muted)")
    )
    ui.html(f'<div style="margin-bottom: 12px;">{src_chips}</div>')

    if stats.get("status") == "unconfigured":
        ui.html(f"""
        <div class="panel"><div class="pbody">
          <p style="color: var(--muted); font-size: 13px; margin: 0;">
            <b>Not configured.</b> {escape(stats.get('error', ''))}
          </p>
          <p style="color: var(--muted); font-size: 12px; margin: 8px 0 0;">
            Add credentials in <a href="/settings" style="color: var(--accent);">Settings</a>,
            then click <b>Sync now</b> below to pull the first snapshot.
          </p>
        </div></div>
        """)
        ui.button("Sync now",
                  on_click=_make_sync_action(adapter)
                  ).props("unelevated outline").style(
            "margin-top: 10px; color: var(--text-2); border: 1px solid var(--border);"
        )
        return

    if stats.get("status") == "error":
        ui.html(f'<div class="flag"><b>Error:</b>&nbsp;{escape(str(stats.get("error", "?")))}</div>')

    # items table
    if items:
        cols = list(items[0].keys())[:6]  # cap to 6 columns
        head = "".join(f"<th>{escape(c)}</th>" for c in cols)
        rows = ""
        for it in items[:50]:
            cells_html = []
            for c in cols:
                v = it.get(c, "")
                # boolean → semantic icon (likes, starred, archived, liked, …)
                if isinstance(v, bool):
                    key = c.lower()
                    if any(s in key for s in ("liked", "favorite", "love")):
                        rendered = ('<span style="color:var(--danger);">♥</span>' if v
                                    else '<span style="color:var(--muted-soft);">♡</span>')
                    elif any(s in key for s in ("star", "starred")):
                        rendered = ('<span style="color:var(--ledger);">★</span>' if v
                                    else '<span style="color:var(--muted-soft);">☆</span>')
                    elif any(s in key for s in ("done", "complete", "archive")):
                        rendered = ('<span style="color:var(--success);">✓</span>' if v
                                    else '<span style="color:var(--muted-soft);">○</span>')
                    elif any(s in key for s in ("ok", "running", "online", "active", "alive")):
                        rendered = ('<span class="chip sug">on</span>' if v
                                    else '<span class="chip warn">off</span>')
                    else:
                        rendered = ('<span style="color:var(--success);">✓</span>' if v
                                    else '<span style="color:var(--muted-soft);">·</span>')
                else:
                    rendered = escape(str(v))[:120]
                cells_html.append(f'<td>{rendered}</td>')
            rows += f"<tr>{''.join(cells_html)}</tr>"
        ui.html(f"""
        <div class="panel">
          <div class="phead"><span class="ttl">Latest snapshot · {len(items)} items shown</span></div>
          <div class="pbody flush">
            <table class="stbl">
              <thead><tr>{head}</tr></thead>
              <tbody>{rows}</tbody>
            </table>
          </div>
        </div>
        """)
    else:
        ui.html('<div class="panel"><div class="pbody">'
                '<p style="color: var(--muted); font-size: 13px; margin: 0;">'
                'No items in latest snapshot. Click <b>Sync now</b> to pull.</p></div></div>')

    # actions row
    with ui.row().style("gap: 8px; margin-top: 14px; flex-wrap: wrap;"):
        ui.button("Sync now",
                  on_click=_make_sync_action(adapter)
                  ).props("unelevated").style("background: var(--accent); color: white;")
        ui.button("Export JSON",
                  on_click=_make_export_action(adapter)
                  ).props("unelevated outline").style("color: var(--text-2); border: 1px solid var(--border);")
        if stats.get("status") == "ok":
            ui.button("View raw snapshot",
                      on_click=lambda a=adapter: _show_raw(a)
                      ).props("unelevated outline").style("color: var(--text-2); border: 1px solid var(--border);")


def _make_sync_action(adapter) -> Callable:
    def _go():
        try:
            payload = adapter.snapshot()
            from lib.snapshot_store import write_snapshot
            local, vault = write_snapshot(adapter.META["id"], payload)
            ui.notify(
                f"✓ snapshot saved · local {'+ vault' if vault else '(vault failed)'}",
                type="positive" if vault else "warning"
            )
        except Exception as e:
            ui.notify(f"✗ {e}", type="negative")
    return _go


def _make_export_action(adapter) -> Callable:
    def _go():
        snap = latest_snapshot(adapter.META["id"])
        if not snap:
            ui.notify("no snapshot to export", type="warning")
            return
        from pathlib import Path
        from datetime import datetime
        out = Path.home() / "Downloads" / f"egon-{adapter.META['id']}-{datetime.now():%Y%m%d-%H%M}.json"
        import json
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(snap, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        ui.notify(f"✓ exported → {out}", type="positive")
    return _go


def _show_raw(adapter) -> None:
    snap = latest_snapshot(adapter.META["id"]) or {}
    import json
    text = json.dumps(snap, indent=2, ensure_ascii=False, default=str)[:5000]
    with ui.dialog() as dialog, ui.card().style("max-width: 90vw; min-width: 600px;"):
        ui.label(f"Raw snapshot · {adapter.META['label']}").style("font-weight: 600;")
        ui.html(f'<pre style="max-height: 60vh; overflow: auto; font-size: 11px;">{text}</pre>')
        ui.button("Close", on_click=dialog.close).props("unelevated").style("background: var(--accent); color: white;")
    dialog.open()
