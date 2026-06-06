"""Marginalia Search adapter — non-SEO, research-friendly web search.

Free, public API. Marginalia indexes "the small web" — academic pages,
personal essays, archives — the stuff Google increasingly buries. Direct
serves pillar 3 (parsing/filtering for content discovery) for Synesism
research and Mouseion citation-context lookup.

Docs: https://search.marginalia.nu/api
"""
from __future__ import annotations

import httpx

BASE = "https://api.marginalia.nu"

META = {
    "id": "marginalia",
    "label": "Marginalia Search",
    "icon": "🔍",
    "kind": "search",
    "needs_auth": False,
    "destructive_actions": [],
    "read_only_default": True,
}


def live_status(timeout: float = 5.0) -> dict:
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.get(f"{BASE}/public/search/test")
        if r.status_code in (200, 404, 400):
            # 400/404 expected on the test path; what matters is the host responds
            return {"status": "ok", "host_responding": True}
        return {"status": "error", "error": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


def search(query: str, limit: int = 20, profile: str = "no-js") -> list[dict]:
    """Search Marginalia. `profile` options: 'default' | 'no-js' |
    'modern' | 'corpo' | 'corpo-clean'. 'no-js' favors content-rich
    pages without JS bloat — best for research/Synesism workflows."""
    try:
        with httpx.Client(timeout=15) as c:
            r = c.get(f"{BASE}/public/search/{query}",
                      params={"count": min(limit, 100), "profile": profile})
        if r.status_code != 200:
            return []
        results = (r.json() or {}).get("results") or []
        return [{
            "url": r.get("url"),
            "title": r.get("title", ""),
            "description": r.get("description", "")[:500],
            "quality": r.get("quality"),
        } for r in results]
    except Exception:
        return []
