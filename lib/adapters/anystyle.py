"""AnyStyle adapter — bibliography string → structured CSL JSON.

AnyStyle is the open-source bibliography parser that powers Zotero's
"Add Item by Identifier" magic. The hosted instance at anystyle.io is
NOT a free public API (it returns 401 Not Authorized to /parse).

The right shape: AnyStyle is meant to be self-hosted via
`gem install anystyle-cli` plus a thin REST wrapper, or via the
`inukshuk/anystyle-server` Docker image. This adapter accepts a
configurable URL via `egon-config.json.anystyle.url` and degrades
gracefully when not configured (same pattern as `hypothesis_web`).

Direct serves pillar 3 (advanced parsing for content discovery): once
self-hosted, you can pipe any "References" section from a PDF, a
copy-pasted bibliography page, or an exported .txt list into clean CSL
records Egon routes into Zotero/Paperpile/Mouseion.

Setup options Bruno can pick on his side:
  1. Docker (fastest):
       docker run -p 4567:4567 inukshuk/anystyle-server
       Then add to egon-config.json:
         "anystyle": {"url": "http://127.0.0.1:4567"}
  2. Ruby gem (lighter):
       gem install anystyle-cli
       anystyle-cli parse --format=csl <input>
       Wrap with a tiny FastAPI shim — even simpler, lives inside Egon.
"""
from __future__ import annotations

import json
from pathlib import Path

from lib.lazy_httpx import httpx  # deferred ~2s import (2026-06-11 perf pass)

ROOT = Path(__file__).resolve().parent.parent.parent

META = {
    "id": "anystyle",
    "label": "AnyStyle",
    "icon": "✒️",
    "kind": "reference_parsing",
    "needs_auth": False,
    "destructive_actions": [],
    "read_only_default": True,
}


def _base_url() -> str | None:
    try:
        with (ROOT / "egon-config.json").open(encoding="utf-8") as f:
            return ((json.load(f).get("anystyle") or {}).get("url") or "").rstrip("/") or None
    except Exception:
        return None


def live_status(timeout: float = 4.0) -> dict:
    url = _base_url()
    if not url:
        return {"status": "unconfigured",
                "error": "Self-host AnyStyle and set egon-config.json.anystyle.url "
                         "(see lib/adapters/anystyle.py header for setup)."}
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.get(url)
        if 200 <= r.status_code < 500:
            return {"status": "ok", "url": url}
        return {"status": "error", "error": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


def parse(refs: list[str] | str, format: str = "csl") -> list[dict]:
    """Parse raw reference strings into structured records. Requires a
    self-hosted AnyStyle endpoint (see header). Falls back to echoing
    `_raw` when unconfigured so callers can still display before/after."""
    if isinstance(refs, str):
        refs = [r.strip() for r in refs.splitlines() if r.strip()]
    if not refs:
        return []
    url = _base_url()
    if not url:
        return [{"_raw": s, "_error": "anystyle endpoint unconfigured"} for s in refs]
    try:
        with httpx.Client(timeout=30) as c:
            r = c.post(f"{url}/parse",
                       data={"input": "\n".join(refs), "format": format},
                       headers={"Accept": "application/json"})
        if r.status_code != 200:
            return [{"_raw": s, "_error": f"HTTP {r.status_code}"} for s in refs]
        try:
            parsed = r.json()
        except Exception:
            return [{"_raw": s, "_error": "non-JSON response"} for s in refs]
        if not isinstance(parsed, list):
            parsed = []
        out = []
        for i, raw in enumerate(refs):
            rec = dict(parsed[i]) if i < len(parsed) and isinstance(parsed[i], dict) else {}
            rec["_raw"] = raw
            out.append(rec)
        return out
    except Exception as e:
        return [{"_raw": s, "_error": str(e)[:120]} for s in refs]
