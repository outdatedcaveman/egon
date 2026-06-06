"""Android Chrome tabs — reuses Panop's `/api/v1/tabs/inspect` endpoint.

Panop already does the heavy lifting: ADB + Chrome DevTools forwarded from the phone.
Egon just polls its API. Requires Panop server running (port autodetected — see
lib/orchestrator.py).
"""
from __future__ import annotations

from datetime import datetime

import httpx

from lib.ledger import load_config
from lib.snapshot_store import latest_snapshot

META = {
    "id": "android_tabs",
    "label": "Chrome Open Tabs (Android)",
    "icon": "📱",
    "kind": "artifact",
    "needs_auth": False,
    "destructive_actions": [],
    "read_only_default": True,
}


def _panop_port() -> int:
    """Read auto-discovered Panop port from egon-config.json (set by lib.orchestrator)."""
    cfg = load_config()
    return int(cfg.get("apps_cache", {}).get("panop", {}).get("port", 8000))


def _panop_url(path: str) -> str:
    return f"http://127.0.0.1:{_panop_port()}{path}"


def live_status() -> dict:
    try:
        r = httpx.get(_panop_url("/api/v1/status"), timeout=2.0)
        if r.status_code == 200:
            return {"status": "ok", "panop_port": _panop_port()}
        return {"status": "error", "error": f"Panop HTTP {r.status_code}"}
    except Exception as e:
        return {"status": "unconfigured",
                "error": f"Panop not reachable on port {_panop_port()}. Start Panop first (Apps tab)."}


def snapshot() -> dict:
    """Pull every open Android Chrome tab via Panop's inspect endpoint."""
    try:
        r = httpx.get(_panop_url("/api/v1/tabs/inspect"), timeout=15.0)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return {"status": "unconfigured", "error": f"Panop unreachable: {e}"}

    # Panop returns buckets {saved, ignored, pending, ...} or a flat list of tabs.
    # Defensive parsing — handle either shape.
    items: list[dict] = []
    if isinstance(data, dict):
        # buckets shape
        for bucket, tabs in data.items():
            if isinstance(tabs, list):
                for t in tabs:
                    if isinstance(t, dict):
                        items.append({
                            "title":  t.get("title", ""),
                            "url":    t.get("url", ""),
                            "bucket": bucket,
                            "id":     t.get("id", ""),
                        })
    elif isinstance(data, list):
        for t in data:
            if isinstance(t, dict):
                items.append({
                    "title": t.get("title", ""),
                    "url":   t.get("url", ""),
                    "bucket": t.get("bucket", ""),
                    "id":    t.get("id", ""),
                })

    return {
        "status": "ok",
        "synced_at": datetime.now().isoformat(),
        "count": len(items),
        "items": items,
    }


def items(limit: int = 100) -> list[dict]:
    snap = latest_snapshot(META["id"])
    if not snap or snap.get("status") != "ok":
        return []
    return snap.get("items", [])[:limit]


def stats() -> dict:
    snap = latest_snapshot(META["id"])
    if not snap:
        return {"status": "no-snapshot", "count": 0, "last_synced": None,
                "error": "click Sync now (needs Panop running)"}
    return {
        "status": snap.get("status", "ok"),
        "count": snap.get("count", 0),
        "last_synced": (snap.get("synced_at") or "")[:16],
    }
