"""Are.na adapter — research-as-network channels.

Are.na is a quiet, network-shaped knowledge tool: blocks (notes, images,
links, text) grouped into channels, channels connected to other
channels. Closest in spirit to what Synesism wants for source
curation. Free tier exists; a personal access token from
https://dev.are.na/oauth/applications unlocks personal/private channels.

Egon ties:
  • References — channels of curated paper links sit alongside Zotero
    and Paperpile.
  • Synesism — each argument or topic gets a channel; sub-arguments
    are connected channels.
  • Routster — alternative bookmark organization model to compare
    against the auto-categorizer.

Public endpoints work without auth (read-only on public channels);
private channels need a token in `egon-config.json.arena.token`.

Docs: https://dev.are.na/documentation/channels
"""
from __future__ import annotations

import json
from pathlib import Path

from lib.lazy_httpx import httpx  # deferred ~2s import (2026-06-11 perf pass)

ROOT = Path(__file__).resolve().parent.parent.parent
BASE = "https://api.are.na/v2"

META = {
    "id": "arena",
    "label": "Are.na",
    "icon": "🔗",
    "kind": "knowledge_network",
    "needs_auth": False,   # public read works; auth unlocks private
    "destructive_actions": ["delete_block", "delete_channel"],
    "read_only_default": True,
}


def _token() -> str | None:
    try:
        with (ROOT / "egon-config.json").open(encoding="utf-8") as f:
            return (json.load(f).get("arena") or {}).get("token")
    except Exception:
        return None


def _headers() -> dict:
    h = {"Accept": "application/json"}
    t = _token()
    if t:
        h["Authorization"] = f"Bearer {t}"
    return h


def live_status(timeout: float = 5.0) -> dict:
    try:
        with httpx.Client(timeout=timeout, headers=_headers()) as c:
            r = c.get(f"{BASE}/search/channels", params={"q": "test", "per": 1})
        if r.status_code == 200:
            return {"status": "ok", "authed": bool(_token())}
        return {"status": "error", "error": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


def search_channels(query: str, limit: int = 20) -> list[dict]:
    try:
        with httpx.Client(timeout=15, headers=_headers()) as c:
            r = c.get(f"{BASE}/search/channels",
                      params={"q": query, "per": min(limit, 100)})
        if r.status_code != 200:
            return []
        ch = (r.json() or {}).get("channels") or []
        return [{
            "id": c_.get("id"),
            "slug": c_.get("slug"),
            "title": c_.get("title", ""),
            "user": (c_.get("user") or {}).get("slug"),
            "length": c_.get("length"),
            "status": c_.get("status"),
            "updated_at": c_.get("updated_at"),
        } for c_ in ch]
    except Exception:
        return []


def channel(slug: str, per: int = 50) -> dict:
    """Fetch a channel's metadata + its blocks."""
    try:
        with httpx.Client(timeout=20, headers=_headers()) as c:
            r = c.get(f"{BASE}/channels/{slug}",
                      params={"per": min(per, 100)})
        if r.status_code != 200:
            return {"status": "error", "error": f"HTTP {r.status_code}"}
        d = r.json() or {}
        contents = d.get("contents") or []
        return {
            "status": "ok",
            "title": d.get("title", ""),
            "user": (d.get("user") or {}).get("slug"),
            "length": d.get("length"),
            "blocks": [{
                "id": b.get("id"),
                "class": b.get("class"),
                "title": b.get("title", ""),
                "content": (b.get("content") or "")[:1500],
                "url": (b.get("source") or {}).get("url") if b.get("source") else None,
                "image": (b.get("image") or {}).get("display", {}).get("url"),
            } for b in contents],
        }
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}
