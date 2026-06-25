"""Chrome open tabs (desktop) — via the Egon Chrome extension.

Rewritten 2026-05-20. Old approach used Chrome DevTools Protocol on :9222 —
which Chrome 127+ silently disables unless `--user-data-dir` is also set
(Google's security mitigation against malware reading the live session).
That path was fragile and broke on every Chrome update.

New approach: a tiny browser extension (external/egon_chrome_extension/)
POSTs the current tab list to Panop every 30 s (and on every tab event).
This adapter just reads the most recently received payload from Panop's
`/api/v1/chrome_tabs/state` endpoint. Works on any Chrome version, no
flags needed.

Install the extension:
  1. chrome://extensions/
  2. Toggle "Developer mode" (top-right)
  3. "Load unpacked" → select egon/external/egon_chrome_extension/
  4. Pin it (optional). Click the icon for a status popup.

For ANDROID tabs, see `lib/adapters/android_tabs.py`.
"""
from __future__ import annotations

from datetime import datetime

from lib.lazy_httpx import httpx  # deferred ~2s import (2026-06-11 perf pass)

from lib.snapshot_store import latest_snapshot

META = {
    "id": "chrome_tabs",
    "label": "Chrome Open Tabs (desktop)",
    "icon": "🌐",
    "kind": "artifact",
    "needs_auth": False,
    "destructive_actions": [],
    "read_only_default": True,
}

# 2026-06-12: the /chrome_tabs/state route was removed in the 2026-05-26
# panop_server rewrite (lives only in .backups now); the live surface is
# /api/v1/tabs/inspect. The adapter sat "unconfigured" ever since.
PANOP_STATE_URL = "http://127.0.0.1:8000/api/v1/chrome_tabs/state"
_STALE_AFTER_S = 90   # if extension hasn't pushed in this long, mark stale


def _read_state() -> dict | None:
    try:
        r = httpx.get(PANOP_STATE_URL, timeout=8.0)   # inspect walks all tabs
        if r.status_code == 200:
            return r.json()
    except Exception:
        return None
    return None


def live_status() -> dict:
    d = _read_state()
    if not d:
        return {"status": "unconfigured",
                "error": ("Panop unreachable on :8000 — start Egon first, "
                          "then install the Egon Chrome extension "
                          "(see lib/adapters/chrome_tabs.py docstring).")}
    if d.get("status") == "no_data":
        return {"status": "unconfigured",
                "error": ("No tabs received yet. Install the Egon Chrome extension "
                          "from external/egon_chrome_extension/ — chrome://extensions "
                          "→ Developer mode → Load unpacked.")}
    if d.get("status") == "error":
        return {"status": "error", "error": d.get("error", "")}

    # Reported tabs — check freshness
    received_at = d.get("received_at") or ""
    age_s = None
    try:
        rec = datetime.fromisoformat(received_at)
        age_s = (datetime.now() - rec).total_seconds()
    except Exception:
        pass
    tabs_open = d.get("count", len(d.get("tabs", [])))
    if age_s is not None and age_s > _STALE_AFTER_S:
        return {"status": "stale", "tabs_open": tabs_open,
                "received_at": received_at, "age_s": int(age_s),
                "error": "Extension hasn't pushed in >90s — is Chrome open?"}
    return {"status": "ok", "tabs_open": tabs_open,
            "received_at": received_at,
            "age_s": int(age_s) if age_s is not None else None}


def snapshot() -> dict:
    """Return current tabs in the standard snapshot shape."""
    d = _read_state()
    if not d or d.get("status") != "ok":
        return {"status": "unconfigured",
                "error": (d or {}).get("error",
                                       "no chrome_tabs data — install the extension")}
    tabs = d.get("tabs", []) or []
    items_out = [
        {"title": t.get("title", ""), "url": t.get("url", ""),
         "id":    t.get("id", ""),    "window_id": t.get("windowId"),
         "active": t.get("active", False), "pinned": t.get("pinned", False)}
        for t in tabs
    ]
    return {
        "status": "ok",
        "synced_at": d.get("received_at", datetime.now().isoformat()),
        "count": len(items_out),
        "items": items_out,
    }


def items(limit: int = 100) -> list[dict]:
    snap = latest_snapshot(META["id"])
    if not snap or snap.get("status") != "ok":
        # Fall back to live read so the UI works pre-first-sync
        live = snapshot()
        if live.get("status") == "ok":
            return live.get("items", [])[:limit]
        return []
    return snap.get("items", [])[:limit]


def stats() -> dict:
    snap = latest_snapshot(META["id"])
    if not snap:
        live = snapshot()
        if live.get("status") == "ok":
            return {"status": "ok", "count": live.get("count", 0),
                    "last_synced": (live.get("synced_at") or "")[:16]}
        return {"status": "no-snapshot", "count": 0, "last_synced": None,
                "error": "install the Egon Chrome extension"}
    return {
        "status": snap.get("status", "ok"),
        "count":  snap.get("count", 0),
        "last_synced": (snap.get("synced_at") or "")[:16],
    }
