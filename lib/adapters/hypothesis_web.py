"""Hypothes.is adapter — Bruno's web annotations.

Hypothes.is is open annotation across the web. Bruno bookmarked it under
Productivity and the Egon spirit fits it perfectly: annotations across
every paper, blog, and PDF Bruno reads becoming first-class data inside
Egon's References / Mind tabs.

Direct serves pillar 1 (comprehensiveness — your reading is a *first-
class* data source in Egon, alongside Kindle highlights, Instapaper,
etc.).

Auth: API token in `egon-config.json` → `hypothesis.token`. Graceful
fallback if missing (live_status returns 'unconfigured').

Docs: https://h.readthedocs.io/en/latest/api-reference/v1/
File name is `hypothesis_web.py` (not `hypothesis.py`) so it doesn't
shadow the popular `hypothesis` Python testing library.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent.parent
BASE = "https://hypothes.is/api"

META = {
    "id": "hypothesis",
    "label": "Hypothes.is",
    "icon": "✍️",
    "kind": "annotations",
    "needs_auth": True,
    "destructive_actions": ["delete_annotation"],
    "read_only_default": True,
}


def _token() -> str | None:
    try:
        with (ROOT / "egon-config.json").open(encoding="utf-8") as f:
            return (json.load(f).get("hypothesis") or {}).get("token")
    except Exception:
        return None


def _headers() -> dict:
    h = {"Accept": "application/json"}
    t = _token()
    if t:
        h["Authorization"] = f"Bearer {t}"
    return h


def live_status(timeout: float = 5.0) -> dict:
    tok = _token()
    if not tok:
        return {"status": "unconfigured",
                "error": "add `hypothesis.token` to egon-config.json (get one at "
                         "https://hypothes.is/account/developer )"}
    try:
        with httpx.Client(timeout=timeout, headers=_headers()) as c:
            r = c.get(f"{BASE}/profile")
        if r.status_code == 200:
            d = r.json() or {}
            return {"status": "ok",
                    "user": d.get("userid"),
                    "groups": len(d.get("groups", []))}
        return {"status": "error", "error": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


def annotations(limit: int = 200, user: str | None = None) -> list[dict]:
    """List recent annotations. If `user` omitted, uses the authenticated
    user from the profile endpoint."""
    if not _token():
        return []
    try:
        with httpx.Client(timeout=20, headers=_headers()) as c:
            if not user:
                p = c.get(f"{BASE}/profile").json()
                user = p.get("userid")
            params = {"limit": min(limit, 200), "order": "desc",
                      "sort": "updated"}
            if user:
                params["user"] = user
            r = c.get(f"{BASE}/search", params=params)
        if r.status_code != 200:
            return []
        rows = (r.json() or {}).get("rows") or []
        out = []
        for a in rows:
            target = (a.get("target") or [{}])[0]
            sel = target.get("selector") or []
            quote = ""
            for s in sel:
                if s.get("type") == "TextQuoteSelector":
                    quote = s.get("exact", "")
                    break
            out.append({
                "id": a.get("id"),
                "uri": a.get("uri"),
                "title": ((a.get("document") or {}).get("title") or [""])[0],
                "quote": quote[:600],
                "text": (a.get("text") or "")[:1000],
                "tags": a.get("tags") or [],
                "updated": a.get("updated"),
                "group": a.get("group"),
            })
        return out
    except Exception:
        return []
