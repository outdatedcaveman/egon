"""Navigation — unified single-page Panop port with lazy-loaded data.

Render order:
1. Page header + 11 action buttons (instant)
2. Status pill bar (skeleton → fills in <500ms)
3. History table (skeleton → fills as Panop returns data)
4. Categories + Settings panels (skeleton → fills)
"""
from __future__ import annotations

from html import escape

from nicegui import ui

from lib import panop_client as panop
from views._async import lazy_panel, skeleton_panel


def _safe(fn, *args, **kw):
    try:
        return True, fn(*args, **kw)
    except Exception as e:
        return False, str(e)[:200]


def _act(method: str, path: str, label: str):
    def go(_=None):
        fn = panop.post if method == "POST" else panop.get
        ok, r = _safe(fn, path)
        ui.notify(f"{label}: {str(r)[:160]}",
                  type="positive" if ok else "negative", multi_line=True)
    return go


def _btn(label, action, primary=False, tooltip=""):
    el = ui.button(label, on_click=action).props(
        "unelevated dense" if primary else "unelevated outline dense")
    if primary:
        el.style("background: var(--accent); color: white;")
    else:
        el.style("color: var(--text-2); border: 1px solid var(--border);")
    if tooltip:
        el.tooltip(tooltip)
    return el


def _chip(text: str, kind: str = "") -> str:
    return f'<span class="chip {kind}">{escape(text)}</span>'


# -- async loaders (run on background thread) -----------------------------

def _load_status_data() -> dict:
    """Single shot — Panop's in-process calls, no socket hop, ~5ms."""
    return {
        "status": panop.status() or {},
        "hmeta":  panop.history_meta() or {},
        "bpend":  panop.bookmarks_pending() or {},
        "up":     panop.is_up(),
    }


def _load_history() -> list[dict]:
    res = panop.get("/api/v1/history?limit=300")
    return res if isinstance(res, list) else []


def _load_config_env() -> dict:
    return {"cfg": panop.get("/api/v1/config") or {}, "env": panop.env() or {}}


# -- renderers (run on UI thread after data lands) ------------------------

def _render_status_strip(data: dict) -> None:
    s = data["status"]
    hmeta = data["hmeta"]
    bpend = data["bpend"]
    adb = bool(s.get("adb_connected"))
    chrome = bool(s.get("chrome_running"))
    running = bool(s.get("running"))
    device = s.get("device_id") or "—"
    last_error = s.get("last_error") or ""

    phone_chip = _chip("📵 disconnected — Pair below", "warn") if not adb else _chip(f"📱 {device}", "sug")
    chrome_chip = _chip("Chrome running", "sug") if chrome else _chip("Chrome offline", "warn")
    sweep_chip = _chip("⟳ SWEEP RUNNING", "warn") if running else _chip("idle", "sug")

    history_total = hmeta.get("count", 0)
    z_synced = hmeta.get("z_synced", 0)
    b_synced = hmeta.get("b_synced", 0)
    bookmarks_pending = bpend.get("count", 0)

    ui.html(f'''
    <div style="display:flex; gap:8px; flex-wrap:wrap; margin-bottom:14px;">
      <span class="status-pill">Phone&nbsp;{phone_chip}</span>
      <span class="status-pill">{chrome_chip}</span>
      <span class="status-pill">Sweep&nbsp;{sweep_chip}</span>
      <span class="status-pill">History <b style="color:var(--text);">{history_total:,}</b></span>
      <span class="status-pill">Z synced <b style="color:var(--text);">{z_synced:,}</b></span>
      <span class="status-pill">B synced <b style="color:var(--text);">{b_synced:,}</b></span>
      <span class="status-pill">Bookmarks pending <b style="color:var(--text);">{bookmarks_pending:,}</b></span>
    </div>
    ''')
    if last_error:
        ui.html(f'<div class="flag">Last error from Panop: <code>{escape(last_error[:300])}</code></div>')


def _render_history(items: list[dict]) -> None:
    rows = ""
    for it in items[:300]:
        title = escape(str(it.get("title") or it.get("url", "(no title)"))[:90])
        url = escape(str(it.get("url", ""))[:140])
        when = escape(str(it.get("captured_at") or it.get("time") or "")[:16])
        cat = escape(str(it.get("category", "")))
        z_done = bool(it.get("zotero_synced") or it.get("z_synced"))
        b_done = bool(it.get("bookmark_pushed") or it.get("b_synced"))
        z_icon = ('<span title="In Zotero" style="color:var(--success);">Z✓</span>' if z_done
                  else '<span title="Not yet in Zotero" style="color:var(--muted-soft);">Z·</span>')
        b_icon = ('<span title="Pushed as bookmark" style="color:var(--success);">B✓</span>' if b_done
                  else '<span title="Not yet pushed" style="color:var(--muted-soft);">B·</span>')
        rows += (
            "<tr>"
            f'<td style="white-space:nowrap; color:var(--muted); font-size:11px;">{when}</td>'
            f'<td>{_chip(cat) if cat else ""}</td>'
            f'<td style="white-space:nowrap;">{z_icon}&nbsp;{b_icon}</td>'
            f'<td><b style="color:var(--text);">{title}</b><br/>'
            f'<span style="font-size:11px; color:var(--muted);">{url}</span></td>'
            "</tr>"
        )
    if not rows:
        rows = ('<tr><td colspan="4" style="padding:14px; color:var(--muted);">'
                'No history yet. Click <b>⚡ Fetch Now</b> or <b>⚡ Drain All Tabs</b> above.</td></tr>')

    ui.html(f'''
    <div class="panel">
      <div class="phead">
        <span class="ttl">📜 Historical Ledger · {len(items):,} entries</span>
        <span class="lnk">Z = synced to Zotero · B = pushed as Chrome bookmark</span>
      </div>
      <div class="pbody flush">
        <table class="stbl">
          <thead><tr><th>Captured</th><th>Category</th><th>Z / B</th><th>Item</th></tr></thead>
          <tbody>{rows}</tbody>
        </table>
      </div>
    </div>
    ''')


def _render_config_categories(data: dict) -> None:
    cats = data["cfg"].get("categories", []) if isinstance(data["cfg"], dict) else []
    env = data["env"]

    with ui.element("div").style("display:grid; grid-template-columns: 1fr 1fr; gap: 14px;"):
        # Categories
        cat_rows = ""
        for c in cats:
            if not isinstance(c, dict): continue
            name = escape(str(c.get("name", "")))
            domains = c.get("domains", [])
            doms = escape(", ".join(domains) if isinstance(domains, list) else str(domains))[:120]
            folder = escape(str(c.get("folder", c.get("bookmark_folder", ""))))
            cat_rows += (
                "<tr>"
                f'<td><b>{name}</b><br/><span style="color:var(--muted); font-size:11px;">{doms}</span></td>'
                f'<td style="text-align:right;">{_chip(folder) if folder else ""}</td>'
                "</tr>"
            )
        if not cat_rows:
            cat_rows = '<tr><td colspan="2" style="padding:14px; color:var(--muted);">No categories.</td></tr>'

        ui.html(f'''
        <div class="panel">
          <div class="phead">
            <span class="ttl">🏷️ Categories · {len(cats)}</span>
            <span class="lnk">routing rules</span>
          </div>
          <div class="pbody flush">
            <table class="stbl"><tbody>{cat_rows}</tbody></table>
          </div>
        </div>
        ''')

        # Settings editor
        with ui.element("div").classes("panel"):
            ui.html('<div class="phead"><span class="ttl">⚙️ System Settings · /api/v1/env</span>'
                    '<span class="lnk">live config</span></div>')
            with ui.element("div").classes("pbody"):
                if not env:
                    ui.html('<p style="color:var(--muted); margin:0;">Env not loaded.</p>')
                else:
                    inputs: dict = {}
                    for k, v in env.items():
                        if isinstance(v, bool):
                            el = ui.checkbox(k, value=v)
                        elif isinstance(v, (int, float)) and not isinstance(v, bool):
                            el = ui.number(label=k, value=v).props("outlined dense stack-label")
                        else:
                            is_secret = any(s in k.lower() for s in ("key", "token", "secret", "password"))
                            el = ui.input(
                                label=k, value=str(v) if v is not None else "",
                                password=is_secret, password_toggle_button=is_secret,
                            ).props("outlined dense stack-label")
                        el.style("margin-bottom: 6px;")
                        inputs[k] = el

                    def _save_settings(env=env, inputs=inputs):
                        payload = {}
                        for k, el in inputs.items():
                            val = el.value
                            orig = env.get(k)
                            if isinstance(orig, bool):
                                payload[k] = bool(val)
                            elif isinstance(orig, (int, float)) and not isinstance(orig, bool):
                                try: payload[k] = type(orig)(val) if val != "" else orig
                                except (ValueError, TypeError): payload[k] = orig
                            else:
                                payload[k] = val
                        ok, r = _safe(panop.post, "/api/v1/env", json_body=payload)
                        ui.notify(f"saved: {r}" if ok else f"✗ {r}",
                                  type="positive" if ok else "negative")

                    with ui.row().style("gap: 8px; margin-top: 8px;"):
                        _btn("Save settings", _save_settings, primary=True)
                        _btn("Reload", lambda: ui.navigate.reload())


# -- entrypoint ------------------------------------------------------------

def render(data: dict, **_) -> None:
    up = panop.is_up()  # cached, ~instant
    status_chip = _chip("backend mounted in-process", "sug") if up else _chip("starting…", "warn")

    # ---- header (instant) ----
    ui.html(f'''
    <h1 class="page">🧭 Navigation {status_chip}</h1>
    <p class="page-sub">Panop\'s engine, ported into Egon — single page, all controls.</p>
    ''')

    # ---- 11 action buttons (instant) ----
    with ui.row().style("gap: 8px; flex-wrap: wrap; margin-bottom: 14px;"):
        _btn("✦ Check Connection", _act("GET", "/api/v1/status", "check"), primary=True)
        _btn("⚡ Merge Duplicates", _act("POST", "/api/v1/history/merge", "merge"))
        _btn("Z Resync All", _act("POST", "/api/v1/history/sync", "z-resync"))
        _btn("B Resync All", _act("POST", "/api/v1/bookmarks/reset-flags", "b-resync"))
        _btn("📱 Close Synced Tabs", _act("POST", "/api/v1/tabs/close-synced", "close-synced"))
        _btn("🔍 Inspect Tabs", _act("GET", "/api/v1/tabs/inspect", "inspect"))
        _btn("⚡ Drain All Tabs", _act("POST", "/api/v1/tabs/drain", "drain"))
        _btn("💤 Keep Phone Awake", _act("POST", "/api/v1/phone/keep_awake", "keep-awake"))
        _btn("📵 Phone reconnect", _act("POST", "/api/v1/phone/reconnect", "reconnect"))
        _btn("🤝 Pair Phone", _act("POST", "/api/v1/phone/pair", "pair"))
        _btn("⚡ Fetch Now", _act("POST", "/api/v1/fetch_now", "fetch"))

    # ---- status strip (lazy) ----
    lazy_panel(_load_status_data, _render_status_strip,
               skeleton=skeleton_panel("Reading phone + sweep status", lines=2))

    # ---- history table (lazy) ----
    lazy_panel(_load_history, _render_history,
               skeleton=skeleton_panel("Loading historical ledger", lines=8))

    # ---- categories + settings (lazy) ----
    lazy_panel(_load_config_env, _render_config_categories,
               skeleton=skeleton_panel("Loading config + categories", lines=4))
