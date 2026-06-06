"""Chrome bookmarks — reads the local Bookmarks JSON file. No network. No auth."""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from lib.snapshot_store import latest_snapshot

META = {
    "id": "chrome_bookmarks",
    "label": "Chrome Bookmarks",
    "icon": "🔖",
    "kind": "artifact",
    "needs_auth": False,
    "destructive_actions": [],
    "read_only_default": True,
}

CHROME_PROFILES = [
    Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data" / "Default" / "Bookmarks",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data" / "Profile 1" / "Bookmarks",
]


def _bookmarks_file() -> Path | None:
    for p in CHROME_PROFILES:
        if p.exists():
            return p
    return None


def _walk(node: dict, parent_folder: str = "") -> list[dict]:
    out = []
    nodes = node.get("children") or []
    for n in nodes:
        if n.get("type") == "url":
            out.append({
                "title": n.get("name", "")[:200],
                "url":   n.get("url", ""),
                "folder": parent_folder,
                "added": n.get("date_added"),
            })
        elif n.get("type") == "folder":
            folder_name = n.get("name", "")
            full = f"{parent_folder}/{folder_name}" if parent_folder else folder_name
            out.extend(_walk(n, full))
    return out


def live_status() -> dict:
    f = _bookmarks_file()
    if not f:
        return {"status": "unconfigured", "error": "Chrome Bookmarks file not found"}
    return {"status": "ok", "path": str(f), "size_kb": round(f.stat().st_size / 1024, 1)}


def snapshot() -> dict:
    f = _bookmarks_file()
    if not f:
        return {"status": "unconfigured", "error": "Chrome Bookmarks file not found"}
    raw = json.loads(f.read_text(encoding="utf-8"))
    roots = raw.get("roots", {})
    items: list[dict] = []
    for root_key in ("bookmark_bar", "other", "synced"):
        if root_key in roots:
            items.extend(_walk(roots[root_key], root_key))
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
                "error": "click Sync now to pull first snapshot"}
    return {
        "status": snap.get("status", "ok"),
        "count": snap.get("count", 0),
        "last_synced": (snap.get("synced_at") or "")[:16],
    }
