"""App orchestrator — trigger your own apps from one interface.

Each app has:
- `status()` → port liveness check + cached config (path, detected port, ui_url)
- `actions` → one-click triggers

Ports are auto-discovered from each app's source code (no manual config).
Discovered values cached in egon-config.json["apps_cache"].
"""
from __future__ import annotations

import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from lib.lazy_httpx import httpx  # deferred ~2s import (2026-06-11 perf pass)

from lib.ledger import load_config, save_config
from lib.egon_paths import ROUTSTER_PATH, MOUSEION_PATH, PANOP_PATH


# -- Known apps + heuristics ------------------------------------------------

APP_DEFS = {
    "routster": {
        "label": "Routster",
        "icon":  "🦀",
        "paths": [ROUTSTER_PATH],
        "port_patterns": [
            r"const\s+PORT\s*=\s*(\d{4,5})",
            r"app\.listen\(\s*(\d{4,5})",
            r"PORT\s*[:=]\s*(\d{4,5})",
        ],
        "port_files":  ["main.js", "server.js", "backend/server.js", "backend/index.js"],
        "default_port": 4000,
        "launch_hint": "Launch_KMS_AutoRouter.vbs",
    },
    "mouseion": {
        "label": "Mouseion",
        "icon":  "🐭",
        "paths": [
            MOUSEION_PATH,
            MOUSEION_PATH,
        ],
        "port_patterns": [
            r"app\.run\([^)]*port\s*=\s*(\d{4,5})",
            r"--port[=\s]+(\d{4,5})",
            r"port\s*=\s*(\d{4,5})",
        ],
        "port_files":  ["app.py", "main.py", "server.py", "zoterpile.py", "run.py"],
        "default_port": 7274,
        "launch_hint": None,
    },
    "panop": {
        "label": "Panop",
        "icon":  "📱",
        "paths": [
            PANOP_PATH / "panop-server",
            PANOP_PATH,
            PANOP_PATH,
        ],
        "port_patterns": [
            r"uvicorn\.run\([^)]*port\s*=\s*(\d{4,5})",
            r"app\.run\([^)]*port\s*=\s*(\d{4,5})",
            r"PORT\s*[:=]\s*(\d{4,5})",
            r"--port[=\s]+(\d{4,5})",
        ],
        "port_files":  ["main.py", "app.py", "server.py", "panop.py"],
        "default_port": 8765,
        "launch_hint": None,
    },
}


# -- Discovery -------------------------------------------------------------

def _discover_port(app_id: str) -> tuple[int, str | None]:
    """Find this app's port by reading its source files. Returns (port, detection_path)."""
    spec = APP_DEFS[app_id]
    for root in spec["paths"]:
        if not root.exists():
            continue
        for rel in spec["port_files"]:
            f = root / rel
            if not f.exists():
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for pat in spec["port_patterns"]:
                m = re.search(pat, text)
                if m:
                    try:
                        return int(m.group(1)), str(f)
                    except ValueError:
                        continue
    return spec["default_port"], None


def _cached_or_discover(app_id: str) -> dict:
    """Use cached port if < 7 days old; else rediscover and save."""
    cfg = load_config()
    cache = cfg.get("apps_cache", {}).get(app_id, {})
    if cache.get("discovered_at"):
        try:
            ts = datetime.fromisoformat(cache["discovered_at"])
            if datetime.now() - ts < timedelta(days=7):
                return cache
        except ValueError:
            pass
    port, detection_path = _discover_port(app_id)
    spec = APP_DEFS[app_id]
    # find the install root that exists
    install_path = next((str(p) for p in spec["paths"] if p.exists()), None)
    fresh = {
        "port": port,
        "install_path": install_path,
        "detection_path": detection_path,
        "discovered_at": datetime.now().isoformat(),
    }
    cfg.setdefault("apps_cache", {})[app_id] = fresh
    save_config(cfg)
    return fresh


# -- liveness --------------------------------------------------------------

def _port_ok(port: int, path: str = "/") -> tuple[bool, str]:
    try:
        r = httpx.get(f"http://127.0.0.1:{port}{path}", timeout=2.0)
        return r.status_code < 500, f"HTTP {r.status_code}"
    except Exception as e:
        return False, str(e).split("\n")[0][:80]


def _open_url(url: str) -> tuple[bool, str]:
    import webbrowser
    webbrowser.open(url)
    return True, f"opened {url}"


# -- per-app status + actions ---------------------------------------------

def _app_status(app_id: str) -> dict:
    spec = APP_DEFS[app_id]
    info = _cached_or_discover(app_id)
    port = info["port"]
    install = info.get("install_path")
    detected_from = info.get("detection_path")
    ok, msg = _port_ok(port, "/")
    return {
        "id": app_id,
        "label": spec["label"],
        "icon":  spec["icon"],
        "port":  port,
        "running": ok,
        "detail": msg,
        "ui_url": f"http://127.0.0.1:{port}/",
        "install": install,
        "detected_from": detected_from,
        "launch_hint": spec.get("launch_hint"),
    }


def _launch_app(app_id: str) -> tuple[bool, str]:
    spec = APP_DEFS[app_id]
    info = _cached_or_discover(app_id)
    install = info.get("install_path")
    if not install:
        return False, f"{spec['label']} install not found in any of: {spec['paths']}"
    # Prefer a launch hint if specified
    hint = spec.get("launch_hint")
    if hint:
        launcher = Path(install) / hint
        if launcher.exists():
            try:
                subprocess.Popen(["wscript.exe", str(launcher)], close_fds=True)
                return True, f"launched {hint}"
            except Exception as e:
                return False, str(e)
    return False, f"no launch hint for {spec['label']} — start it manually"


# -- registry --------------------------------------------------------------

def _status(app_id):
    return lambda: _app_status(app_id)


def _actions(app_id):
    def build():
        info = _app_status(app_id)
        acts = {"open_ui": (f"Open {info['label']} UI", lambda u=info['ui_url']: _open_url(u))}
        if info.get("launch_hint"):
            acts["launch"] = (f"Launch {info['label']}", lambda aid=app_id: _launch_app(aid))
        # app-specific actions wired in once we know the API
        return acts
    return build


APPS = [
    {"id": "routster", "status": _status("routster"), "actions": _actions("routster")},
    {"id": "mouseion", "status": _status("mouseion"), "actions": _actions("mouseion")},
    {"id": "panop",    "status": _status("panop"),    "actions": _actions("panop")},
]
