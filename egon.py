"""Egon — visual control plane. NiceGUI app on http://127.0.0.1:8088.

All views live in `views/`. The agent writes `last_pass.json` to the vault;
this app just reads + renders it. Buttons trigger Claude pass re-runs.
"""
from __future__ import annotations

import lib.silent_subprocess  # noqa: F401  — suppress console windows on Windows

import os as _os
import socket
from nicegui import ui

# Prevent indefinite socket hangs in third-party APIs
socket.setdefaulttimeout(45.0)

from lib.actions import trigger_pass
from lib.state import load_last_pass
from theme.tokens import ACCENT, GLOBAL_CSS
from views import (  # noqa: F401
    apps, artifacts, databases, home, inbox, ledger, media, memory,
    navigation, projects, references, search, settings, sync,
)

# -- Panop runs as a SEPARATE subprocess (port 8000); Egon proxies /panop/* --
# Rationale: when Panop was mounted in-process the old way (2026-05-20 wedge),
# its background ADB-loop thread could hang the entire Egon UI. Now Panop
# runs in its own Python interpreter — any hang in Panop is contained.
# Egon proxies /panop/<path> → http://127.0.0.1:8000/<path>.
def _bootstrap_panop():
    # Sentinel file disables auto-spawn (Bruno hit annoying-flashing windows;
    # delete .panop_disabled to re-enable).
    from pathlib import Path as _PP
    sentinel = _PP(__file__).resolve().parent / ".panop_disabled"
    if sentinel.exists():
        print(f"[panop_proc] disabled by sentinel {sentinel.name}", flush=True)
        return
    try:
        from lib import panop_proc
        panop_proc.ensure_running_async(
            log_fn=lambda level, **kw: print(f"[panop_proc] {level}: {kw}", flush=True)
        )
    except Exception as e:
        print(f"[panop_proc] bootstrap failed: {e}", flush=True)
_bootstrap_panop()


# -- pre-warm DISABLED 2026-05-20 -------------------------------------------
# Used to fire 15 parallel adapter probes at boot. Each probe could touch
# slow networks (Notion, Drive, Zotero web API). When Drive was slow these
# probes blocked, contributing to the wedge. View-on-demand is fast enough
# now that lazy_panel is wired everywhere.
print("[prewarm] disabled — adapters probe on first view-open instead", flush=True)


PORT = int(_os.environ.get("EGON_PORT", "8088"))
TITLE = "Egon"

# -- /health endpoint -----------------------------------------------------
# Tiny, no-I/O health check the watchdog polls every 60s. Reads only
# file mtimes (fast on local disk) — never touches network/Drive. Returns
# 200 quickly when Egon is alive; failure to respond is the watchdog's
# signal to restart.
from fastapi.responses import JSONResponse
from nicegui import app as _ng_app
from pathlib import Path as _Path
import os as _os

_HEALTH_ROOT = _Path(__file__).resolve().parent
_SNAP_DIR = _HEALTH_ROOT / "state" / "snapshots"
_LOCAL_LAST_PASS = _HEALTH_ROOT / "state" / "last_pass.json"
from lib.egon_paths import LAST_PASS as _LAST_PASS
_LAST_PASS_CANDIDATES = (_LOCAL_LAST_PASS, _LAST_PASS)


@_ng_app.get("/health")
def _health():
    from datetime import datetime, timezone
    snaps = {}
    if _SNAP_DIR.is_dir():
        for sub in _SNAP_DIR.iterdir():
            if not sub.is_dir():
                continue
            latest = None
            try:
                files = [f for f in sub.iterdir() if f.is_file()]
                if files:
                    latest = max(files, key=lambda f: f.stat().st_mtime)
            except Exception:
                pass
            if latest:
                age_h = (datetime.now().timestamp() - latest.stat().st_mtime) / 3600
                snaps[sub.name] = {
                    "latest": latest.name,
                    "size_kb": round(latest.stat().st_size / 1024, 1),
                    "age_h":   round(age_h, 1),
                    "stale":   age_h > 36,
                }
            else:
                snaps[sub.name] = {"latest": None, "stale": True}
    last_pass = None
    for candidate in sorted(
        (p for p in _LAST_PASS_CANDIDATES if p.exists() and p.stat().st_size > 0),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    ):
        try:
            mt = candidate.stat().st_mtime
            last_pass = {
                "path": str(candidate),
                "mtime_age_h": round((datetime.now().timestamp() - mt) / 3600, 1),
            }
            break
        except Exception:
            pass
    return JSONResponse({
        "ok":        True,
        "ts":        datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "snapshots": snaps,
        "last_pass": last_pass,
    })


# -- /panop/* HTTP proxy → http://127.0.0.1:8000/* --------------------------
# All calls to /panop/api/v1/... are forwarded to the Panop subprocess.
# Hard timeout so a hung Panop never wedges Egon. Returns 503 cleanly when
# Panop isn't reachable instead of blocking.
import httpx as _httpx
from fastapi import Request as _FastReq

_PANOP_BASE = "http://127.0.0.1:8000"
_PANOP_PROXY_TIMEOUT_S = 10


@_ng_app.api_route("/panop/{path:path}",
                   methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def _panop_proxy(path: str, request: _FastReq):
    url = f"{_PANOP_BASE}/{path}"
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in ("host", "content-length")}
    body = await request.body()
    try:
        async with _httpx.AsyncClient(timeout=_PANOP_PROXY_TIMEOUT_S) as cli:
            r = await cli.request(request.method, url,
                                  params=request.query_params,
                                  content=body, headers=headers)
            return JSONResponse(
                content=(r.json() if r.headers.get("content-type", "").startswith("application/json")
                         else {"_proxied": True, "body": r.text[:50000]}),
                status_code=r.status_code,
            )
    except _httpx.TimeoutException:
        return JSONResponse({"error": "panop_timeout"}, status_code=504)
    except _httpx.ConnectError:
        return JSONResponse({"error": "panop_unreachable",
                             "hint": "Panop subprocess not yet up; will start automatically"},
                            status_code=503)
    except Exception as e:
        return JSONResponse({"error": "proxy_error", "detail": str(e)[:200]},
                            status_code=502)

# ---- nav definition ----
NAV = [
    ("home",        "🏠", "Home",            False),
    ("inbox",       "📥", "Inbox",           False),
    ("artifacts",   "🗂️", "Artifacts",       False),
    ("navigation",  "🧭", "Navigation",      False),
    ("media",       "🎬", "Media",           False),
    ("references",  "📚", "References",      False),
    ("databases",   "🗄️", "Databases",       False),
    ("apps",        "🧰", "Apps",            False),
    ("projects",    "📁", "Projects",        False),
    ("search",      "🔍", "Search",          False),
    ("sync",        "🔄", "Sync",            False),
    ("ledger",      "💰", "Token Ledger",    True),
    ("memory",      "🧠", "Memory & rules",  False),
    ("settings",    "⚙️", "Settings",        False),
]

VIEWS = {
    "home":     lambda data, **_: home.render(data),
    "ledger":   lambda data, **kw: ledger.render(data, range_key=kw.get("range", "30d")),
    "inbox":    lambda data, **_: inbox.render(data),
    "sync":     lambda data, **_: sync.render(data),
    "memory":   lambda data, **_: memory.render(data),
    "settings": lambda data, **_: settings.render(data),
    "projects":   lambda data, **_: projects.render(data),
    "search":     lambda data, **_: search.render(data),
    "artifacts":  lambda data, **_: artifacts.render(data),
    "media":      lambda data, **_: media.render(data),
    "references": lambda data, **_: references.render(data),
    "databases":  lambda data, **_: databases.render(data),
    "apps":       lambda data, **_: apps.render(data),
    "navigation": lambda data, **_: navigation.render(data),
}


@ui.page("/{slug}")
@ui.page("/")
def page(slug: str = "home", range: str = "30d"):
    if slug not in VIEWS:
        slug = "home"

    ui.add_head_html(GLOBAL_CSS)

    # apply persisted dark-mode preference, if any
    from lib.ledger import load_config
    if load_config().get("dark_mode"):
        ui.add_head_html('<script>document.documentElement.classList.add("dark");</script>')

    data = load_last_pass()
    last_pass_at = data.get("generated_at", "—")
    items = data.get("items_processed", "—")
    duration = data.get("duration_seconds", "—")

    # ---- header ----
    with ui.header().style("padding: 0 24px; height: 56px;").classes("items-center"):
        ui.label("🛰️  Egon").style("font-weight: 600; font-size: 16px;")
        ui.space()
        ui.label(f"Last pass: {last_pass_at}  ·  {items} items  ·  {duration}s").style(
            "font-size: 12px; color: var(--muted, #6b7280);"
        )

        # dark-mode toggle
        def _toggle_dark():
            from lib.ledger import load_config, save_config
            cfg = load_config()
            cfg["dark_mode"] = not cfg.get("dark_mode", False)
            save_config(cfg)
            ui.run_javascript('document.documentElement.classList.toggle("dark");')
            ui.notify(f"dark mode {'on' if cfg['dark_mode'] else 'off'}")

        ui.button("☀ / 🌙", on_click=_toggle_dark).props(
            "flat dense"
        ).style("color: var(--text-2, #374151); padding: 0 10px; min-width: 0;").tooltip("Toggle dark mode")

        def _run_pass():
            ok, msg = trigger_pass("daily")
            ui.notify(msg, type="positive" if ok else "negative")
        ui.button("⚡ Run pass now", on_click=_run_pass).props(
            "unelevated dense"
        ).style(f"background: var(--accent, {ACCENT}); color: white; padding: 0 14px; border-radius: 6px;")

    # ---- left drawer ----
    with ui.left_drawer(value=True, fixed=True).style(
        "padding: 16px 0; width: 244px; display: flex; flex-direction: column; overflow: hidden;"
    ):
        # scrollable nav region
        with ui.element("div").style("flex: 1; overflow-y: auto; padding-bottom: 8px;"):
            for s, icon, label, is_ledger in NAV:
                sel = (s == slug)
                cls = "nav-item"
                if sel:
                    cls += " sel ledger" if is_ledger else " sel"
                ui.html(f'<a href="/{s}" class="{cls}"><span>{icon}</span><span>{label}</span></a>')
        # footer pinned at the bottom via flex
        ui.html(
            '<div style="font-size: 11px; color: var(--muted-soft); padding: 10px 18px; '
            'border-top: 1px solid var(--border); margin: 0; line-height: 1.55;">'
            'v0.2 · localhost:8088<br/>state in vault/050/egon</div>'
        )

    # ---- main view ----
    # Navigation injects its iframe directly into <body> via JS (escapes Quasar wrappers).
    # All other views sit inside the standard padded main wrapper.
    with ui.element("main").style("padding: 28px 40px; max-width: 1380px; margin: 0 auto;"):
        VIEWS[slug](data, range=range)


if __name__ in {"__main__", "__mp_main__"}:
    HOST = "127.0.0.1"
    assert HOST in ("127.0.0.1", "localhost"), (
        f"SECURITY: refusing to bind to non-loopback host {HOST!r}. "
        "Egon is local-only by design — your KMS data must never be reachable from the network."
    )
    ui.run(host=HOST, port=PORT, title=TITLE, dark=False, reload=False, show=False)
