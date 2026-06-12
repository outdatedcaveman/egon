"""YouTube watch history — snapshot adapter over the extension harvest.

Bruno 2026-06-12 ("where's youtube?"): the Chrome extension harvests watch
history (Google killed the API in 2016) into state/panop/
youtube_history_state.json, but nothing ever lifted it into the snapshot
store, so it was invisible to the mirrors, Connect index and dashboards.
This adapter is that bridge.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
STATE = ROOT / "state" / "panop" / "youtube_history_state.json"

META = {
    "id": "youtube_history",
    "label": "YouTube history",
    "icon": "▶️",
    "kind": "media",
    "needs_auth": False,
    "destructive_actions": [],
    "read_only_default": True,
}


def _read() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def live_status() -> dict:
    d = _read()
    items = d.get("items") or []
    if not items:
        return {"status": "unconfigured",
                "error": "no watch-history harvest yet — open youtube.com/feed/history "
                         "with the Egon extension installed"}
    age_h = (time.time() - STATE.stat().st_mtime) / 3600
    return {"status": "ok" if age_h < 72 else "stale",
            "total_items": len(items), "age_hours": round(age_h, 1)}


def snapshot() -> dict:
    d = _read()
    raw = d.get("items") or []
    items = []
    for it in raw:
        title = it.get("title") or ""
        if not title:
            continue
        items.append({
            "id": it.get("url") or title,
            "title": title[:300],
            "url": it.get("url") or "",
            "subtitle": " · ".join(p for p in (
                it.get("channel"), it.get("watched_at") or it.get("when"))
                if p)[:200],
            "kind": "watched_video",
            "channel": it.get("channel") or "",
        })
    return {"status": "ok" if items else "empty",
            "synced_at": datetime.now().isoformat(),
            "count": len(items), "items": items}


def items(limit: int = 5000) -> list[dict]:
    return (snapshot().get("items") or [])[:limit]
