"""Notion full-workspace indexer — every page the integration has access to.

Uses /v1/search (paginated) to enumerate ALL pages + databases the Notion
token can see, NOT just the Egon root. Output snapshot.items contains:
  - title (extracted from properties.title or Name)
  - url   (canonical Notion URL)
  - id    (page id)
  - parent_type (workspace / page_id / database_id)
  - last_edited_time

Hits cross_search like any other source.
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from lib.lazy_httpx import httpx  # deferred ~2s import (2026-06-11 perf pass)

from lib.snapshot_store import latest_snapshot

META = {
    "id": "notion_workspace",
    "label": "Notion (entire workspace)",
    "icon": "📓",
    "kind": "database",
    "needs_auth": True,
    "destructive_actions": [],
    "read_only_default": True,
}

from lib.egon_paths import ENV_FILE as ENV_PATH


def _token() -> str | None:
    tok = os.environ.get("NOTION_TOKEN")
    if tok:
        return tok
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            if line.startswith("NOTION_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def _h() -> dict:
    return {"Authorization": f"Bearer {_token()}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json"}


def _title_of(obj: dict) -> str:
    """Extract a readable title from a page/database object."""
    props = obj.get("properties") or {}
    # databases have a top-level "title" array
    if obj.get("object") == "database":
        t = obj.get("title") or []
        return "".join(seg.get("plain_text", "") for seg in t) or "(untitled DB)"
    # pages: find the title-typed property
    for k, v in props.items():
        if isinstance(v, dict) and v.get("type") == "title":
            return "".join(seg.get("plain_text", "") for seg in v.get("title", [])) or "(untitled)"
    return "(untitled)"


def live_status() -> dict:
    if not _token():
        return {"status": "unconfigured", "error": "no NOTION_TOKEN (set in claude-meta/.env)"}
    try:
        r = httpx.post("https://api.notion.com/v1/search",
                       headers=_h(), json={"page_size": 1}, timeout=5)
        if r.status_code == 200:
            return {"status": "ok"}
        return {"status": "error", "error": f"HTTP {r.status_code}: {r.text[:120]}"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def snapshot() -> dict:
    if not _token():
        return {"status": "unconfigured", "error": "no NOTION_TOKEN"}
    items: list[dict] = []
    cursor = None
    pages_seen = 0
    try:
        with httpx.Client(headers=_h(), timeout=30) as c:
            while True:
                body = {"page_size": 100,
                        "sort": {"direction": "descending",
                                 "timestamp": "last_edited_time"}}
                if cursor:
                    body["start_cursor"] = cursor
                r = c.post("https://api.notion.com/v1/search", json=body)
                r.raise_for_status()
                data = r.json()
                for obj in data.get("results", []):
                    parent = obj.get("parent") or {}
                    items.append({
                        "id":               obj.get("id", ""),
                        "object":           obj.get("object", ""),
                        "title":            _title_of(obj),
                        "url":              obj.get("url", ""),
                        "parent_type":      parent.get("type", ""),
                        "archived":         bool(obj.get("archived")),
                        "last_edited_time": obj.get("last_edited_time", ""),
                        "created_time":     obj.get("created_time", ""),
                    })
                pages_seen += len(data.get("results", []))
                if not data.get("has_more"):
                    break
                cursor = data.get("next_cursor")
                # safety cap so we don't run forever on huge workspaces
                if pages_seen >= 5000:
                    break
    except Exception as e:
        return {"status": "error", "error": str(e)}

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
                "error": "click Sync now"}
    return {
        "status": snap.get("status", "ok"),
        "count": snap.get("count", 0),
        "last_synced": (snap.get("synced_at") or "")[:16],
    }
