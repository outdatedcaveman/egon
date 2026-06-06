"""Settings — Plan mode · Theme · Connections (unified) · Adapter health · Paths · Pricing."""
from __future__ import annotations

import json
from html import escape

from nicegui import ui

from pathlib import Path
from lib.ledger import load_config, save_config
from lib.pricing import PRICING
from views._async import lazy_panel



# -- Connections schema ------------------------------------------------------
# Each row: (icon, label, source_id, fields, helper_text, test_action, sync_action)
# fields = list of (config_path, ui_label, secret?, placeholder)
def _connections_spec(cfg: dict):
    return [
        {
            "icon": "📥", "label": "Instapaper", "id": "instapaper",
            "fields": [
                ("instapaper.username", "Email / username", False, "you@example.com"),
                ("instapaper.password", "Password",         True,  ""),
            ],
            "help": "Simple HTTP API · basic auth. Full reading-list reader needs OAuth (P5).",
            "test_import": "lib.adapters.instapaper",
            "test_fn":     "authenticate",
        },
        {
            "icon": "🎬", "label": "Letterboxd", "id": "letterboxd",
            "fields": [
                ("letterboxd.username",    "Username",                              False, "outdatedcaveman"),
                ("letterboxd.password",    "Password (auto-login attempt)",         True,  ""),
            ],
            "help": ("<b>Letterboxd has no public API.</b> The community workarounds (cookie scraping, "
                     "automated login) are all blocked by their WAF / reCAPTCHA Enterprise. "
                     "Only paths that actually work today: (a) we scrape the most-recent 72 films from your "
                     "public profile, (b) you drop a one-time export ZIP for the full corpus. "
                     "We use (a) when you have just a password; (b) is the unavoidable fallback for the full library."),
            "test_import": "lib.adapters.letterboxd",
            "test_fn":     "auto_login",
            "extra_uploader": {
                "config_key": "letterboxd.export_path",
                "label": "Letterboxd export ZIP (full corpus — Letterboxd offers no API)",
                "filename": "letterboxd-export.zip",
            },
        },
        {
            "icon": "🎵", "label": "YouTube + YouTube Music (READ-ONLY)", "id": "youtube_music",
            "fields": [],
            "help": ("<b>Real-time API access</b> via Google OAuth — same flow as Drive. "
                     "<b>Reuses your Drive OAuth client</b> automatically, so if you've already authorized Drive, "
                     "you just need to click <b>Authorize…</b> below — it'll add YouTube scopes to your existing grant. "
                     "Read-only: <code>youtube.readonly</code> only. Egon cannot like/unlike/subscribe/modify anything. "
                     "Pulls: liked songs+videos · your playlists · subscriptions."),
            "test_import": "lib.adapters.youtube", "test_fn": "live_status",
            "extra_authorize_yt": True,
            "supports_write_mode": True,
        },
        {
            "icon": "📖", "label": "Kindle (login)", "id": "kindle",
            "fields": [
                ("kindle.email",    "Amazon email",    False, ""),
                ("kindle.password", "Amazon password", True,  ""),
            ],
            "help": ("Amazon has NO public API. Click <b>Login to Kindle</b> below — a Chromium "
                     "window opens, you sign in once (incl. 2FA / CAPTCHA), Egon saves the session "
                     "locally (gitignored). After that, all syncs run headless and pull your full "
                     "highlights/notes from <code>read.amazon.com/notebook</code>."),
            "test_import": "lib.adapters.kindle", "test_fn": "live_status",
            "extra_browser_login": {
                "module": "lib.adapters.kindle",
                "label":  "Login to Kindle (opens browser)",
            },
        },
        {
            "icon": "📺", "label": "TV Time (login)", "id": "tvtime",
            "fields": [],
            "help": ("TV Time's mobile API no longer accepts plain password login (we confirmed: every "
                     "auth attempt returns 'You did not give the correct password' regardless of input). "
                     "Same Playwright path as Kindle/Paperpile: click <b>Login to TV Time</b> below, "
                     "Chromium opens, you sign in once, session cached. Subsequent syncs are headless."),
            "test_import": "lib.adapters.tvtime", "test_fn": "live_status",
            "extra_browser_login": {
                "module": "lib.adapters.tvtime",
                "label":  "Login to TV Time (opens browser)",
            },
        },
        {
            "icon": "☁️", "label": "Google Drive (READ-ONLY)", "id": "gdrive",
            "fields": [
                ("gdrive.client_id",     "OAuth client ID",     False, ""),
                ("gdrive.client_secret", "OAuth client secret", True,  ""),
            ],
            "help": ("<b>Read-only scopes only</b> — Egon cannot modify/share/delete anything in your Drive.<br/><br/>"
                     "<b>Setup (one-time, ~3 min — uses Google's <i>new</i> 2024+ console UI):</b><br/>"
                     "1. <a href='https://console.cloud.google.com' target='_blank' style='color:var(--accent);'>console.cloud.google.com</a> → New project (name: <code>egon</code>)<br/>"
                     "2. Menu → <b>APIs &amp; Services</b> → <b>Library</b> → enable <b>Google Drive API</b><br/>"
                     "3. Menu → <b>Google Auth Platform</b> → fill app name + your email<br/>"
                     "4. <b style='color:var(--ledger-txt);'>⚠ Critical:</b> in Google Auth Platform's left sidebar click "
                     "<b>Audience</b> → scroll to <b>Test users</b> → <b>+ Add users</b> → add your own email → Save. "
                     "(This is where the old 'OAuth consent screen → Test users' lives now.)<br/>"
                     "5. Same Google Auth Platform → <b>Clients</b> → Create client → <b>Desktop</b> → save client_id/secret<br/>"
                     "6. Paste above → <b>Save</b> → <b>Authorize…</b><br/><br/>"
                     "<i>403 'verification process' error = your email isn't on the Audience → Test users list. Go to step 4.</i>"),
            "test_import": "lib.adapters.gdrive", "test_fn": "live_status",
            "extra_authorize": True,
            "supports_write_mode": True,
        },
        {
            "icon": "📓", "label": "Notion", "id": "notion",
            "fields": [
                ("notion.token", "Integration token (uses claude-meta/.env by default)", True, ""),
            ],
            "help": "Already configured via <code>claude-meta/.env</code> — leave blank to reuse. "
                    "Get a new one at <code>notion.so/my-integrations</code> if you need a separate one for Egon.",
            "test_import": "lib.adapters.notion", "test_fn": "live_status",
        },
        # ---- new Google adapters (reuse Drive's OAuth client) ----
        {
            "icon": "📅", "label": "Google Calendar (READ-ONLY)", "id": "gcalendar",
            "fields": [],
            "help": "Reuses Drive's OAuth client. Pulls every event ±90/180 days from all your calendars.",
            "test_import": "lib.adapters.gcalendar", "test_fn": "live_status",
            "extra_authorize_module": "lib.adapters.gcalendar",
            "supports_write_mode": True,
        },
        {
            "icon": "📧", "label": "Gmail (READ-ONLY metadata)", "id": "gmail",
            "fields": [],
            "help": "Read-only metadata only (subject/from/to/date/snippet). No body content fetched. "
                    "Reuses Drive's OAuth client.",
            "test_import": "lib.adapters.gmail", "test_fn": "live_status",
            "extra_authorize_module": "lib.adapters.gmail",
            "supports_write_mode": True,
        },
        {
            "icon": "💪", "label": "Google Fit (READ-ONLY)", "id": "gfit",
            "fields": [],
            "help": "Steps · heart rate · weight · activity. Last 30 days. Reuses Drive's OAuth client.",
            "test_import": "lib.adapters.gfit", "test_fn": "live_status",
            "extra_authorize_module": "lib.adapters.gfit",
        },
        # ---- Zotero Web API ----
        {
            "icon": "📚", "label": "Zotero (full library via API)", "id": "zotero_web",
            "fields": [
                ("zotero.user_id", "User ID (numeric, from zotero.org/settings/keys)", False, ""),
                ("zotero.api_key", "API key (read-only)",                                 True,  ""),
            ],
            "help": ("Local SQLite only has what Zotero has synced locally. For your full ~200K library "
                     "use the Web API. Get keys: <a href='https://www.zotero.org/settings/keys' target='_blank' "
                     "style='color:var(--accent);'>zotero.org/settings/keys</a> → New Private Key → "
                     "Allow library access (read-only) → Save. The User ID is the long number at the top of that page."),
            "test_import": "lib.adapters.zotero_web", "test_fn": "live_status",
        },
        # ---- Paperpile (no public API — honest note) ----
        {
            "icon": "📑", "label": "Paperpile", "id": "paperpile",
            "fields": [
                ("paperpile.email",    "Paperpile email",                False, ""),
                ("paperpile.password", "Paperpile password",             True,  ""),
                ("paperpile.api_key",  "API key (if you have a Workspace plan)", True, ""),
            ],
            "help": ("<b>Paperpile has no public API for personal plans.</b> Click <b>Login to Paperpile</b> "
                     "below — Chromium opens, you sign in with Google once, session saved locally. "
                     "Subsequent syncs run headless. If you have a <i>Workspace</i> plan, paste the API key "
                     "above instead — that path skips the browser entirely."),
            "test_import": "lib.adapters.paperpile", "test_fn": "live_status",
            "extra_browser_login": {
                "module": "lib.adapters.paperpile",
                "label":  "Login to Paperpile (opens browser)",
            },
        },
        # ---- Mouseion (local Flask + SQLite) ----
        {
            "icon": "🐭", "label": "Mouseion", "id": "mouseion",
            "fields": [
                ("mouseion.path", "refs.db path (autodetected if at default)", False,
                 str(Path.home() / ".local" / "share" / "mouseion" / "refs.db")),
            ],
            "help": "Reads either the local Flask service (port 7274) if running, or the refs.db directly. "
                    "If you see 'DB empty', start Mouseion at least once to initialize it.",
            "test_import": "lib.adapters.mouseion", "test_fn": "live_status",
        },

    ]


def _save_dotted(cfg: dict, path: str, value: str) -> None:
    """Save cfg['a']['b'] = value from 'a.b'."""
    parts = path.split(".")
    cur = cfg
    for p in parts[:-1]:
        if p not in cur or not isinstance(cur[p], dict):
            cur[p] = {}
        cur = cur[p]
    cur[parts[-1]] = value


def _get_dotted(cfg: dict, path: str, default: str = "") -> str:
    cur = cfg
    for p in path.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur if isinstance(cur, str) else default


def _test_chrome_debug() -> None:
    """Hit http://127.0.0.1:9222/json and report."""
    import httpx
    try:
        r = httpx.get("http://127.0.0.1:9222/json", timeout=2.0)
        if r.status_code == 200:
            tabs = [t for t in r.json() if t.get("type") == "page"]
            ui.notify(f"✓ Chrome debug port live · {len(tabs)} tabs open", type="positive")
        else:
            ui.notify(f"port responded but status {r.status_code}", type="warning")
    except Exception as e:
        ui.notify(f"✗ not reachable — close ALL Chrome (incl. tray) and relaunch via taskbar ({e})",
                  type="negative")


_STATUS_MOD_MAP = {
    "instapaper":       "lib.adapters.instapaper",
    "letterboxd":       "lib.adapters.letterboxd",
    "chrome_bookmarks": "lib.adapters.chrome_bookmarks",
    "zotero":           "lib.adapters.zotero_local",
    "zotero_web":       "lib.adapters.zotero_web",
    "notion":           "lib.adapters.notion",
    "notion_workspace": "lib.adapters.notion_workspace",
    "gdrive":           "lib.adapters.gdrive",
    "youtube_music":    "lib.adapters.youtube",
    "gcalendar":        "lib.adapters.gcalendar",
    "gmail":            "lib.adapters.gmail",
    "gfit":             "lib.adapters.gfit",
    "paperpile":        "lib.adapters.paperpile",
    "tvtime":           "lib.adapters.tvtime",
    "mouseion":         "lib.adapters.mouseion",
}


def _status_chip(adapter_id: str) -> str:
    """Inline cached live_status chip for a source. Cached 60s so Settings stays snappy."""
    mod_name = _STATUS_MOD_MAP.get(adapter_id)
    if not mod_name:
        return '<span class="chip">stub</span>'
    from lib.status_cache import get_status
    s = get_status(adapter_id, mod_path=mod_name)
    status = s.get("status", "?")
    cls = ("sug" if status == "ok"
           else "warn" if status in ("unconfigured", "no-snapshot")
           else "")
    return f'<span class="chip {cls}">{escape(status)}</span>'


def _lazy_status_chip(adapter_id: str) -> None:
    """Inline ui.html that loads its chip in the background — used at render time
    so the Settings page structure appears instantly while chips stream in."""
    from views._async import lazy_chip
    lazy_chip(lambda aid=adapter_id: _status_chip(aid))


def render(data: dict) -> None:
    cfg = load_config()
    ui.html('<h1 class="page">Settings</h1>')
    ui.html('<p class="page-sub">Plan mode · theme · all source credentials · pricing.</p>')

    # ---- plan mode + theme ----
    with ui.card().style("padding: 14px 18px; margin-bottom: 14px;"):
        ui.label("Plan mode").style("font-weight:600; color: var(--text);")
        ui.label("Drives how the Token Ledger frames numbers.").style(
            "font-size:12px; color: var(--muted); margin-bottom: 8px;")
        plan_select = ui.toggle(
            {"pro": "Pro ($20/mo)", "max": "Max ($200/mo)", "api": "API metered"},
            value=cfg.get("plan_mode", "pro"),
        )

        def _save_plan(e):
            cfg["plan_mode"] = e.value
            save_config(cfg)
            ui.notify(f"plan mode → {e.value}")
        plan_select.on_value_change(_save_plan)

        ui.html('<div style="margin-top:14px; padding-top:14px; border-top:1px solid var(--border);"></div>')
        ui.label("Theme").style("font-weight:600; color: var(--text);")
        ui.label("Light is default; dark inverts the palette.").style(
            "font-size:12px; color: var(--muted); margin-bottom: 8px;")

        def _toggle_dark():
            cfg["dark_mode"] = not cfg.get("dark_mode", False)
            save_config(cfg)
            ui.run_javascript('document.documentElement.classList.toggle("dark");')
            ui.notify(f"dark mode {'on' if cfg['dark_mode'] else 'off'}")

        ui.button("Toggle dark mode", on_click=_toggle_dark).props("unelevated").style(
            "background: var(--accent); color: white;")

    # ---- Connections (unified) ----
    ui.html('<h2 style="font-size:18px; font-weight:600; margin: 14px 0 10px; color: var(--text);">'
            'Connections</h2>')
    ui.html('<p style="font-size:12px; color: var(--muted); margin: 0 0 12px;">'
            'All credentials in one place. Stored in <code>egon-config.json</code> (gitignored, never leaves disk). '
            'Auto-detected sources (Chrome Bookmarks, Zotero, Obsidian vault) appear with their status below.</p>')

    spec = _connections_spec(cfg)
    # Alphabetical by label (case-insensitive) — easier to scan a long list.
    spec = sorted(spec, key=lambda s: s["label"].lower())

    # Filter bar — typing in here hides any connector row whose label/id doesn't match.
    with ui.row().style("gap: 8px; align-items: center; margin: 0 0 10px; flex-wrap: wrap;"):
        filter_inp = ui.input(placeholder="filter connectors… (name, id, e.g. 'google', 'tv')").props(
            "outlined dense stack-label clearable"
        ).style("min-width: 320px;")
        ui.label(f"{len(spec)} connectors · sorted A→Z").style(
            "font-size: 11px; color: var(--muted);")

    filter_inp.on_value_change(lambda e: ui.run_javascript(
        f"(()=>{{const q=({json.dumps((e.value or '').lower())}).trim();"
        "document.querySelectorAll('[data-conn-search]').forEach(el=>{"
        "const hit = !q || el.dataset.connSearch.includes(q);"
        "el.style.display = hit ? '' : 'none';});})()"
    ))

    for src in spec:
        _render_connection_row(cfg, src)

    # ---- one-click Chrome debug port setup ----
    with ui.card().style("padding: 14px 18px; margin-bottom: 14px;"):
        ui.label("Chrome remote debugging").style("font-weight: 600; color: var(--text);")
        ui.label(
            "Adds --remote-debugging-port=9222 to your Chrome shortcuts so Egon can read open tabs. "
            "After running this: close ALL Chrome (incl. tray icon) → relaunch via taskbar."
        ).style("font-size: 12px; color: var(--muted); margin-bottom: 10px;")

        def _setup_chrome_debug():
            import json
            import subprocess
            from pathlib import Path
            script = Path(__file__).resolve().parent.parent / "scripts" / "chrome_debug_setup.ps1"
            try:
                r = subprocess.run(
                    ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                     "-File", str(script)],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=15,
                )
                if r.returncode != 0:
                    ui.notify(f"setup failed: {r.stderr[:200]}", type="negative")
                    return
                results = json.loads(r.stdout.strip()) if r.stdout.strip() else []
                if isinstance(results, dict):
                    results = [results]
                modified = sum(1 for x in results if x.get("status") == "modified")
                already  = sum(1 for x in results if x.get("status") == "already-set")
                errors   = [x for x in results if "error" in x.get("status", "")]
                msg = f"✓ {modified} modified, {already} already set"
                if errors:
                    msg += f", {len(errors)} need admin (Start Menu system shortcut)"
                ui.notify(msg, type="positive")
            except Exception as e:
                ui.notify(f"✗ {e}", type="negative")

        with ui.row().style("gap: 8px;"):
            ui.button("Set up Chrome debug port", on_click=_setup_chrome_debug).props("unelevated").style(
                "background: var(--accent); color: white;")
            ui.button("Test :9222",
                      on_click=lambda: _test_chrome_debug()
                      ).props("unelevated outline").style(
                "color: var(--text-2); border: 1px solid var(--border);")

    # ---- Notion ↔ Obsidian mirror panel (lazy: PowerShell Get-ScheduledTask = ~3-5s) ----
    lazy_panel(lambda: None, lambda _: _render_mirror_panel())

    # ---- auto-detected sources strip ----
    with ui.card().style("padding: 14px 18px; margin: 14px 0;"):
        ui.label("Auto-detected (no credentials needed)").style(
            "font-weight: 600; font-size: 13px; color: var(--text); margin-bottom: 8px;")
        with ui.row().style("gap: 6px; flex-wrap: wrap;"):
            for icon, name, aid in (
                ("🔖", "Chrome Bookmarks",     "chrome_bookmarks"),
                ("📚", "Zotero (local)",       "zotero"),
                ("🟣", "Obsidian Vault",       None),
                ("🦀", "Routster",             None),
                ("📓", "Notion (via .env)",    None),
            ):
                with ui.element("span").classes("status-pill"):
                    ui.html(f'{icon}&nbsp;<b>{escape(name)}</b>&nbsp;')
                    if aid:
                        _lazy_status_chip(aid)
                    else:
                        ui.html('<span class="chip sug">on disk</span>')

    # ---- pricing table ----
    pricing_rows = "".join(
        f'<tr><td><b style="color:var(--text);">{escape(model)}</b></td>'
        f'<td class="r num">${pi:.2f}</td><td class="r num">${po:.2f}</td>'
        f'<td class="r num">${pcw:.2f}</td><td class="r num">${pcr:.2f}</td></tr>'
        for model, (pi, po, pcw, pcr) in PRICING.items()
    )
    ui.html(f"""
    <div class="panel" style="margin-bottom:14px;">
      <div class="phead">
        <span class="ttl">Anthropic pricing · USD per million tokens</span>
        <span class="lnk">edit lib/pricing.py</span>
      </div>
      <div class="pbody flush">
        <table class="stbl">
          <thead><tr>
            <th>Model</th><th class="r">Input</th><th class="r">Output</th>
            <th class="r">Cache write</th><th class="r">Cache read</th>
          </tr></thead>
          <tbody>{pricing_rows}</tbody>
        </table>
      </div>
    </div>
    """)

    # ---- security info ----
    ui.html("""
    <div class="panel">
      <div class="phead"><span class="ttl">Security & Privacy</span></div>
      <div class="pbody">
        <ul style="margin: 0; padding-left: 20px; font-size: 13px; color: var(--text-2); line-height: 1.7;">
          <li><b>Local-only:</b> Egon binds <code>127.0.0.1:8088</code> only. Assertion in <code>egon.py</code> refuses any other host.</li>
          <li><b>Secrets:</b> all credentials in <code>egon-config.json</code> — gitignored. Env vars override.</li>
          <li><b>Double backup:</b> every snapshot written to both local <i>and</i> vault (Drive-synced).</li>
          <li><b>Snapshot history:</b> date-partitioned; never overwritten — full audit trail.</li>
          <li><b>External agents:</b> disabled by default. <code>lib/agent_gate.is_endpoint_enabled()</code> returns <code>False</code> hard-coded.</li>
        </ul>
      </div>
    </div>
    """)


def _render_connection_row(cfg: dict, src: dict) -> None:
    # data-conn-search lets the filter input at the top of Settings hide non-matching rows.
    search_blob = f'{src.get("label","")} {src.get("id","")}'.lower()
    card = ui.card().style("padding: 12px 16px; margin-bottom: 8px;")
    card._props["data-conn-search"] = search_blob
    with card:
        # header row — status chip lazy-loads so the row appears instantly
        with ui.row().style("align-items: center; gap: 10px; margin-bottom: 8px;"):
            ui.html(f'<span style="font-size: 20px;">{src["icon"]}</span>')
            ui.label(src["label"]).style("font-weight: 600; color: var(--text); font-size: 14px;")
            _lazy_status_chip(src["id"])
            ui.space()

        # fields
        inputs = {}
        with ui.row().style("gap: 8px; align-items: center; flex-wrap: wrap;"):
            for path, label, secret, placeholder in src["fields"]:
                kw = {"label": label, "value": _get_dotted(cfg, path),
                      "placeholder": placeholder}
                if secret:
                    el = ui.input(**kw, password=True, password_toggle_button=True)
                else:
                    el = ui.input(**kw)
                # `stack-label` forces label always above input — eliminates the
                # label-overlapping-placeholder rendering you saw.
                el.props("outlined dense stack-label").style("min-width: 220px;")
                inputs[path] = el

            def _save(s=src, ins=inputs):
                latest = load_config()
                for p, el in ins.items():
                    _save_dotted(latest, p, (el.value or "").strip())
                save_config(latest)
                # Invalidate the status cache so the chip re-probes with fresh creds
                from lib.status_cache import invalidate
                invalidate(s["id"])
                ui.notify(f"✓ {s['label']} saved")

            def _test(s=src):
                if not s.get("test_import"):
                    ui.notify("no test for this source (manual export only)", type="info")
                    return
                try:
                    from importlib import import_module
                    mod = import_module(s["test_import"])
                    r = getattr(mod, s["test_fn"])()
                    if r.get("status") == "ok":
                        ui.notify(f"✓ {s['label']} reachable", type="positive")
                    else:
                        ui.notify(f"✗ {r.get('error', r.get('status', 'unknown'))}", type="negative")
                except Exception as e:
                    ui.notify(f"✗ {e}", type="negative")

            async def _sync(s=src):
                """Generic dispatch — uses _STATUS_MOD_MAP. Runs snapshot in a worker
                thread so Playwright-based adapters (Kindle, Paperpile) don't crash on
                NiceGUI's asyncio loop."""
                try:
                    import asyncio
                    from importlib import import_module
                    mod_name = _STATUS_MOD_MAP.get(s["id"])
                    if not mod_name:
                        ui.notify(f"no module mapped for {s['id']}", type="warning")
                        return
                    mod = import_module(mod_name)
                    if not hasattr(mod, "snapshot"):
                        ui.notify(f"{s['label']} adapter has no snapshot() yet", type="info")
                        return
                    ui.notify(f"⏳ syncing {s['label']}…", type="info")
                    # asyncio.to_thread escapes the running event loop — required
                    # for any adapter that uses Playwright's sync API internally.
                    snap = await asyncio.to_thread(mod.snapshot)
                    st = snap.get("status")
                    if st == "deferred":
                        ui.notify(f"⏳ {s['label']}: {snap.get('error', 'sync deferred to next session')}",
                                  type="info")
                        return
                    if st != "ok":
                        ui.notify(f"✗ {s['label']}: {snap.get('error', st)}",
                                  type="negative", multi_line=True)
                        return
                    from lib.snapshot_store import write_snapshot
                    local, vault = write_snapshot(s["id"], snap)
                    ui.notify(
                        f"✓ {s['label']} synced · {snap.get('count', 0):,} items · "
                        f"{'local + vault' if vault else 'local only (vault failed)'}",
                        type="positive" if vault else "warning"
                    )
                    # invalidate status cache so chip refreshes
                    from lib.status_cache import invalidate
                    invalidate(s["id"])
                except Exception as e:
                    ui.notify(f"✗ {type(e).__name__}: {e}", type="negative", multi_line=True)

            ui.button("Save", on_click=_save).props("unelevated dense").style(
                "background: var(--accent); color: white;")
            ui.button("Test", on_click=_test).props("unelevated outline dense").style(
                "color: var(--text-2); border: 1px solid var(--border);")
            ui.button("Sync now", on_click=_sync).props("unelevated outline dense").style(
                "color: var(--text-2); border: 1px solid var(--border);")

        # optional one-click OAuth authorize (Google Drive)
        if src.get("extra_authorize"):
            with ui.row().style("gap: 8px; align-items: center; margin-top: 10px;"):
                from lib.adapters import gdrive
                authed = gdrive.is_authorized()
                if authed:
                    ui.html('<span class="chip sug">authorized · read-only</span>')
                else:
                    ui.html('<span class="chip warn">not authorized</span>')

                def _authorize(s=src):
                    from lib.adapters import gdrive
                    r = gdrive.start_auth_flow()
                    if r.get("status") == "ok":
                        ui.notify("✓ Google Drive authorized (read-only)", type="positive")
                    else:
                        ui.notify(f"✗ {r.get('error', 'auth failed')}", type="negative")

                def _revoke():
                    from lib.adapters import gdrive
                    gdrive.revoke()
                    ui.notify("token revoked locally", type="positive")

                ui.button("Authorize…", on_click=_authorize).props("unelevated").style(
                    "background: var(--accent); color: white;")
                if authed:
                    ui.button("Revoke token", on_click=_revoke).props("unelevated outline").style(
                        "color: var(--text-2); border: 1px solid var(--border);")

        # Read / Read+Write toggle for Google adapters that support it
        if src.get("supports_write_mode"):
            from lib import google_oauth as g_oauth
            sid = src["id"]
            cur_mode = g_oauth.mode(sid)
            with ui.row().style("gap: 8px; align-items: center; margin-top: 10px;"):
                ui.label("Access mode:").style("color: var(--muted); font-size: 12px;")
                mode_toggle = ui.toggle(
                    {"read": "🛡 Read-only", "readwrite": "✎ Read + Write"},
                    value=cur_mode,
                ).props("dense")

                def _save_mode(e, sid=sid):
                    latest = load_config()
                    latest.setdefault(sid, {})["mode"] = e.value
                    save_config(latest)
                    if e.value == "readwrite":
                        ui.notify(
                            "⚠ Write mode enabled. Re-click Authorize to grant the new scopes. "
                            "Egon never deletes without your type-to-confirm.",
                            type="warning", multi_line=True, timeout=8,
                        )
                    else:
                        ui.notify("Read-only mode. Token still works; scope just limits Egon.", type="info")
                mode_toggle.on_value_change(_save_mode)

                if cur_mode == "readwrite":
                    ui.html('<span class="chip warn">WRITE MODE</span>')

        # generic "Login to X (opens browser)" — Kindle, Paperpile, anything Playwright-based
        if src.get("extra_browser_login"):
            mod_path = src["extra_browser_login"]["module"]
            label = src["extra_browser_login"]["label"]
            from importlib import import_module
            with ui.row().style("gap: 8px; align-items: center; margin-top: 10px;"):
                try:
                    mod = import_module(mod_path)
                    logged = mod.is_logged_in()
                except Exception:
                    mod = None; logged = False
                if logged:
                    ui.html('<span class="chip sug">logged in · session cached</span>')
                else:
                    ui.html('<span class="chip warn">not logged in</span>')

                async def _do_login(m=mod, lbl=label):
                    if not m:
                        ui.notify("adapter import failed", type="negative"); return
                    ui.notify(f"Opening browser… {lbl}. Sign in there, close the window when done.",
                              type="info", multi_line=True, timeout=12)
                    import asyncio
                    # Run Playwright in a worker thread to escape NiceGUI's event loop
                    r = await asyncio.to_thread(m.start_auth_flow)
                    if r.get("status") == "ok":
                        ui.notify("✓ session saved locally", type="positive")
                        from lib.status_cache import invalidate
                        invalidate(src["id"])
                    else:
                        ui.notify(f"✗ {r.get('error', 'login failed')}",
                                  type="negative", multi_line=True)

                def _do_revoke(m=mod):
                    if m:
                        m.revoke()
                    ui.notify("session revoked locally", type="positive")
                    from lib.status_cache import invalidate
                    invalidate(src["id"])

                ui.button(label, on_click=_do_login).props("unelevated").style(
                    "background: var(--accent); color: white;")
                if logged:
                    ui.button("Revoke session", on_click=_do_revoke).props(
                        "unelevated outline").style(
                        "color: var(--text-2); border: 1px solid var(--border);")

        # generic Authorize button (Calendar, Gmail, Fit, …)
        if src.get("extra_authorize_module"):
            mod_path = src["extra_authorize_module"]
            from importlib import import_module
            with ui.row().style("gap: 8px; align-items: center; margin-top: 10px;"):
                try:
                    mod = import_module(mod_path)
                    authed = mod.is_authorized()
                except Exception as e:
                    ui.html(f'<span class="chip warn">load error: {escape(str(e)[:50])}</span>')
                    authed = False
                    mod = None
                if mod:
                    if authed:
                        ui.html('<span class="chip sug">authorized · read-only</span>')
                    else:
                        ui.html('<span class="chip warn">not authorized</span>')

                    def _do_auth(m=mod, label=src["label"]):
                        r = m.start_auth_flow()
                        if r.get("status") == "ok":
                            ui.notify(f"✓ {label} authorized", type="positive")
                        else:
                            ui.notify(f"✗ {r.get('error', 'auth failed')}", type="negative")
                    def _do_rev(m=mod, label=src["label"]):
                        m.revoke()
                        ui.notify(f"{label} token revoked", type="positive")

                    ui.button("Authorize…", on_click=_do_auth).props("unelevated").style(
                        "background: var(--accent); color: white;")
                    if authed:
                        ui.button("Revoke token", on_click=_do_rev).props(
                            "unelevated outline"
                        ).style("color: var(--text-2); border: 1px solid var(--border);")

        # optional one-click OAuth authorize (YouTube — reuses Drive's client)
        if src.get("extra_authorize_yt"):
            with ui.row().style("gap: 8px; align-items: center; margin-top: 10px;"):
                from lib.adapters import youtube as yt
                authed = yt.is_authorized()
                if authed:
                    ui.html('<span class="chip sug">authorized · read-only</span>')
                else:
                    ui.html('<span class="chip warn">not authorized</span>')

                def _authorize_yt():
                    from lib.adapters import youtube as yt
                    r = yt.start_auth_flow()
                    if r.get("status") == "ok":
                        ui.notify("✓ YouTube authorized (read-only)", type="positive")
                    else:
                        ui.notify(f"✗ {r.get('error', 'auth failed')}", type="negative")

                def _revoke_yt():
                    from lib.adapters import youtube as yt
                    yt.revoke()
                    ui.notify("YouTube token revoked locally", type="positive")

                ui.button("Authorize…", on_click=_authorize_yt).props("unelevated").style(
                    "background: var(--accent); color: white;")
                if authed:
                    ui.button("Revoke token", on_click=_revoke_yt).props("unelevated outline").style(
                        "color: var(--text-2); border: 1px solid var(--border);")

        # optional file uploader (e.g. letterboxd export ZIP)
        up = src.get("extra_uploader")
        if up:
            with ui.row().style("gap: 8px; align-items: center; margin-top: 10px;"):
                cur = _get_dotted(cfg, up["config_key"])
                if cur:
                    ui.html(f'<span class="chip sug">file set</span> '
                            f'<code style="font-size: 11px; color: var(--muted);">{escape(cur)[-60:]}</code>')
                else:
                    ui.html(f'<span class="chip warn">no file</span>')

                async def _on_upload(e, src=src, up=up):
                    """NiceGUI 3.11: e.file is a FileUpload with async .save(path) and .name."""
                    from pathlib import Path
                    dest_dir = Path(__file__).resolve().parent.parent / "state" / "imports" / src["id"]
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    # Preserve original filename if available so the path looks right.
                    orig_name = getattr(e.file, "name", up["filename"]) or up["filename"]
                    dest = dest_dir / orig_name
                    try:
                        await e.file.save(dest)
                        latest = load_config()
                        _save_dotted(latest, up["config_key"], str(dest))
                        save_config(latest)
                        ui.notify(f"✓ saved → {dest.name}", type="positive")
                    except Exception as ex:
                        ui.notify(f"✗ {ex}", type="negative")

                ui.upload(label=up["label"], auto_upload=True, max_files=1,
                          on_upload=_on_upload).props("accept=.zip flat dense").style(
                    "max-width: 360px;"
                )

        if src.get("help"):
            ui.html(f'<div style="font-size: 11px; color: var(--muted); margin-top: 6px;">{src["help"]}</div>')


def _render_mirror_panel() -> None:
    """Notion ↔ Obsidian mirror: scope manager + per-scope direction + backup status."""
    import subprocess
    import sys
    from pathlib import Path

    # Load the scope config via the shared module
    user_home = Path.home()
    meta_scripts = user_home / "Claude Code" / "claude-meta" / "scripts"
    if str(meta_scripts) not in sys.path:
        sys.path.insert(0, str(meta_scripts))
    try:
        from _mirror_scopes import (load_config as load_scopes, save_config as save_scopes,
                                     Scope, NotionFilter, ObsidianFilter, kms_section_ids)
        scope_cfg = load_scopes()
        kms_ids = kms_section_ids()
    except Exception as e:
        ui.html(f'<div class="panel"><div class="pbody"><p style="color:var(--danger);">'
                f'Mirror scopes config failed to load: {escape(str(e))}</p></div></div>')
        return

    META_ENV = user_home / "Claude Code" / "claude-meta" / ".env"
    STATE_PATH = user_home / "Claude Code" / "claude-meta" / "logs" / "mirror_state.json"
    FWD_LOG_DIR = user_home / "Claude Code" / "claude-meta" / "logs" / "sync"


    # --- read current status ---
    pages_mirrored = 0
    last_fwd_log = "—"
    if STATE_PATH.exists():
        try:
            state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            pages_mirrored = len(state)
        except Exception:
            pass
    if FWD_LOG_DIR.exists():
        logs = sorted(FWD_LOG_DIR.glob("notion-mirror-*.log"), reverse=True)
        if logs:
            last_fwd_log = logs[0].name

    # Detect WRITE_BACK_ENABLED in claude-meta/.env
    writeback_on = False
    if META_ENV.exists():
        for line in META_ENV.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("WRITE_BACK_ENABLED=1"):
                writeback_on = True
                break

    # Detect scheduled task next-run
    next_run = "—"
    last_run = "—"
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-ScheduledTask -TaskName 'KMS-Sync-Notion-Mirror' | "
             "Get-ScheduledTaskInfo | "
             "Select-Object @{n='lr';e={$_.LastRunTime.ToString('yyyy-MM-dd HH:mm')}}, "
             "@{n='nr';e={$_.NextRunTime.ToString('yyyy-MM-dd HH:mm')}} | "
             "ConvertTo-Json -Compress)"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            info = json.loads(r.stdout.strip())
            last_run = info.get("lr", "—")
            next_run = info.get("nr", "—")
    except Exception:
        pass

    # Backup status (count restore points, latest date)
    local_bk = Path(scope_cfg.backup.local_dir)
    drive_bk = Path(scope_cfg.backup.drive_dir)
    local_pts = sorted([p.name for p in local_bk.iterdir() if p.is_dir()],
                       reverse=True) if local_bk.exists() else []
    drive_pts = sorted([p.name for p in drive_bk.iterdir() if p.is_dir()],
                       reverse=True) if drive_bk.exists() else []

    DIR_OPTS = {"bi": "↔ Bidirectional", "n_to_o": "→ Notion → Obsidian",
                "o_to_n": "← Obsidian → Notion", "off": "✕ Disabled"}

    card = ui.card().style("padding: 14px 18px; margin: 14px 0;")
    with card:
        ui.html('<div style="display:flex; align-items:center; gap:10px; margin-bottom: 6px;">'
                '<span style="font-size: 20px;">🪞</span>'
                '<b style="color: var(--text); font-size: 14px;">Notion ↔ Obsidian mirror</b>'
                f'<span class="chip sug">{pages_mirrored:,} Notion pages mirrored</span>'
                f'<span class="chip{" sug" if writeback_on else ""}">'
                f'global write-back: {"ON" if writeback_on else "off"}</span>'
                '</div>')
        ui.html(
            f'<div style="font-size: 12px; color: var(--muted); margin-bottom: 12px; line-height: 1.6;">'
            f'<b>Schedule:</b> daily at 03:00 (task <code>KMS-Sync-Notion-Mirror</code>). '
            f'Last: {escape(last_run)} · next: {escape(next_run)} · log: <code>{escape(last_fwd_log)}</code>. '
            f'<br><b>How scopes work:</b> each scope filters Notion AND Obsidian, with its own direction. '
            f'The first matching scope wins for a given page. Default <code>full</code> scope covers everything.'
            f'</div>'
        )

        # --- scope table ---
        scope_container = ui.element("div")

        def _redraw_scopes():
            scope_container.clear()
            # Refresh from disk so we always reflect saved state
            cfg = load_scopes()
            with scope_container:
                rows_html = []
                for s in cfg.scopes:
                    nf = s.notion
                    notion_desc = (
                        "all" if nf.scope == "all"
                        else f"subtree<br><code style='font-size:10px;'>{escape(nf.root_id[:8])}…</code>"
                        if nf.scope == "subtree"
                        else f"page<br><code style='font-size:10px;'>{escape(nf.root_id[:8])}…</code>"
                        if nf.scope == "page"
                        else f"exclude<br><code style='font-size:10px;'>{escape(nf.root_id[:8])}…</code>"
                    )
                    obs_desc = escape(s.obsidian.subpath) if s.obsidian.subpath else "(full vault)"
                    if s.obsidian.exclude_subpaths:
                        obs_desc += f" <span style='color:var(--muted);'>· excl {len(s.obsidian.exclude_subpaths)}</span>"
                    en_chip = ('<span class="chip sug">enabled</span>' if s.enabled
                               else '<span class="chip warn">disabled</span>')
                    dir_chip = f'<span class="chip">{escape(DIR_OPTS.get(s.direction, s.direction))}</span>'
                    rows_html.append(
                        "<tr>"
                        f'<td><b style="color:var(--text);">{escape(s.name)}</b><br>'
                        f'<code style="font-size:10px; color:var(--muted);">{escape(s.id)}</code></td>'
                        f'<td>{notion_desc}</td>'
                        f'<td>{obs_desc}</td>'
                        f'<td>{dir_chip}</td>'
                        f'<td>{en_chip}</td>'
                        f'<td data-scope-id="{escape(s.id, quote=True)}"></td>'
                        "</tr>"
                    )
                table_html = f"""
                <div class="panel" style="margin-bottom: 10px;">
                  <div class="pbody flush">
                    <table class="stbl" style="font-size: 12px;">
                      <thead><tr>
                        <th>Scope</th><th>Notion source</th><th>Obsidian dest</th>
                        <th>Direction</th><th>State</th><th>Actions</th>
                      </tr></thead>
                      <tbody>{"".join(rows_html)}</tbody>
                    </table>
                  </div>
                </div>
                """
                ui.html(table_html)

                # Per-row actions: NiceGUI buttons that target each scope. We render
                # them as a horizontal action strip per scope below the table since
                # mounting interactive elements inside the html-table is awkward.
                for s in cfg.scopes:
                    with ui.row().style("gap: 6px; align-items: center; margin-bottom: 6px; "
                                        "padding: 4px 8px; background: var(--panel-2); border-radius: 4px;"):
                        ui.label(f"› {s.name}").style("font-size: 11px; color: var(--muted); min-width:200px;")
                        dir_sel = ui.select(DIR_OPTS, value=s.direction).props("dense outlined").style(
                            "min-width: 180px;")
                        en_toggle = ui.switch("on", value=s.enabled).props("dense")

                        def _on_dir(e, sid=s.id):
                            c = load_scopes()
                            for sc in c.scopes:
                                if sc.id == sid:
                                    sc.direction = e.value
                                    break
                            save_scopes(c)
                            ui.notify(f"{sid}: direction → {e.value}", type="positive")
                        def _on_en(e, sid=s.id):
                            c = load_scopes()
                            for sc in c.scopes:
                                if sc.id == sid:
                                    sc.enabled = bool(e.value)
                                    break
                            save_scopes(c)
                            ui.notify(f"{sid}: {'enabled' if e.value else 'disabled'}", type="positive")
                        def _del(sid=s.id):
                            if sid == "full":
                                ui.notify("can't delete the default `full` scope", type="negative")
                                return
                            c = load_scopes()
                            c.scopes = [sc for sc in c.scopes if sc.id != sid]
                            save_scopes(c)
                            ui.notify(f"deleted scope `{sid}`", type="positive")
                            _redraw_scopes()
                        dir_sel.on_value_change(_on_dir)
                        en_toggle.on_value_change(_on_en)
                        if s.id != "full":
                            ui.button("delete", on_click=_del).props("flat dense").style(
                                "color: var(--danger); font-size: 11px;")

        _redraw_scopes()

        # --- add-scope form ---
        with ui.expansion("➕ Add new scope", icon="add").classes("w-full").style(
                "background: var(--panel-2); border-radius: 4px; margin-top: 8px;"):
            with ui.row().style("gap: 8px; flex-wrap: wrap; align-items: center; padding: 8px;"):
                new_id = ui.input(label="Scope id (a-z, no spaces)", value="").props(
                    "outlined dense stack-label").style("min-width: 180px;")
                new_name = ui.input(label="Display name", value="").props(
                    "outlined dense stack-label").style("min-width: 220px;")
                new_dir = ui.select(DIR_OPTS, value="n_to_o", label="Direction").props(
                    "outlined dense").style("min-width: 180px;")

            with ui.row().style("gap: 8px; flex-wrap: wrap; align-items: center; padding: 0 8px 8px;"):
                # Notion source picker — quick presets from KMS or custom page id
                preset_opts = {"all": "All of Notion", "custom": "Custom page/subtree id"}
                for title, pid in (kms_ids or {}).items():
                    preset_opts[pid] = f"Subtree: {title}"
                new_preset = ui.select(preset_opts, value="all", label="Notion source").props(
                    "outlined dense").style("min-width: 260px;")
                new_custom_id = ui.input(label="Custom Notion id (if Custom)", value="").props(
                    "outlined dense stack-label").style("min-width: 240px;")
                new_subpath = ui.input(label="Obsidian subpath (blank = full vault)", value="").props(
                    "outlined dense stack-label").style("min-width: 260px;")

                def _add_scope():
                    sid = (new_id.value or "").strip().lower().replace(" ", "_")
                    if not sid or not new_name.value:
                        ui.notify("id + name required", type="negative")
                        return
                    c = load_scopes()
                    if any(s.id == sid for s in c.scopes):
                        ui.notify(f"scope `{sid}` already exists", type="negative")
                        return
                    preset = new_preset.value
                    if preset == "all":
                        nf = NotionFilter(scope="all")
                    elif preset == "custom":
                        nf = NotionFilter(scope="subtree", root_id=(new_custom_id.value or "").strip())
                    else:
                        nf = NotionFilter(scope="subtree", root_id=preset)
                    of = ObsidianFilter(subpath=(new_subpath.value or "").strip())
                    c.scopes.append(Scope(id=sid, name=new_name.value, direction=new_dir.value,
                                           enabled=True, notion=nf, obsidian=of))
                    save_scopes(c)
                    ui.notify(f"added scope `{sid}`", type="positive")
                    _redraw_scopes()

                ui.button("Add scope", on_click=_add_scope).props("unelevated").style(
                    "background: var(--accent); color: white;")

        # --- actions row ---
        with ui.row().style("gap: 8px; flex-wrap: wrap; margin-top: 12px;"):
            def _toggle_writeback():
                """Flip global WRITE_BACK_ENABLED in claude-meta/.env (gates ALL O→N pushes)."""
                try:
                    lines = META_ENV.read_text(encoding="utf-8").splitlines() if META_ENV.exists() else []
                    found = False
                    new_lines = []
                    new_state = not writeback_on
                    for line in lines:
                        if line.strip().startswith("WRITE_BACK_ENABLED="):
                            new_lines.append(f"WRITE_BACK_ENABLED={'1' if new_state else '0'}")
                            found = True
                        else:
                            new_lines.append(line)
                    if not found:
                        new_lines.append(f"WRITE_BACK_ENABLED={'1' if new_state else '0'}")
                    META_ENV.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
                    ui.notify(f"global write-back {'ON' if new_state else 'OFF'} — reload page",
                              type="positive")
                except Exception as e:
                    ui.notify(f"✗ {e}", type="negative")

            ui.button("Disable global write-back" if writeback_on else "Enable global write-back",
                      on_click=_toggle_writeback).props("unelevated").style(
                "background: var(--accent); color: white;" if not writeback_on
                else "background: var(--panel-2); color: var(--text); border: 1px solid var(--border);")

            def _run_mirror_now():
                from lib.actions import trigger_pass
                trigger_pass("mirror")
                ui.notify("mirror pass queued (forward + reverse per scope, then backup)",
                          type="positive")
            ui.button("Run mirror now", on_click=_run_mirror_now).props("unelevated outline").style(
                "color: var(--text-2); border: 1px solid var(--border);")

        # --- backup status ---
        ui.html(
            f'<div style="margin-top: 14px; padding-top: 12px; border-top: 1px solid var(--border);">'
            f'<b style="color: var(--text); font-size: 13px;">💾 Parallel restore points</b>'
            f'<div style="font-size: 12px; color: var(--muted); margin-top: 4px; line-height: 1.6;">'
            f'Local: <code>{escape(scope_cfg.backup.local_dir)}</code> · '
            f'<b style="color:var(--text);">{len(local_pts)}</b> points'
            f'{(", latest " + escape(local_pts[0])) if local_pts else ""}<br>'
            f'Drive: <code>{escape(scope_cfg.backup.drive_dir)}</code> · '
            f'<b style="color:var(--text);">{len(drive_pts)}</b> points'
            f'{(", latest " + escape(drive_pts[0])) if drive_pts else ""}<br>'
            f'Retention: <b style="color:var(--text);">{scope_cfg.backup.retention_days} days</b>. '
            f'Hardlinks dedupe unchanged files. Restore via '
            f'<code>py claude-meta/scripts/mirror_backup.py --restore YYYY-MM-DD --commit</code>.'
            f'</div></div>'
        )
