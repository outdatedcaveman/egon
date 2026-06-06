"""Zotero Web API — pulls your entire online library (not just the local SQLite).

Setup: get your User ID + API key at https://www.zotero.org/settings/keys.
Permissions needed: "Allow library access" (read-only).
"""
from __future__ import annotations

import time
from datetime import datetime

import httpx

from lib import secrets
from lib.snapshot_store import latest_snapshot

META = {
    "id": "zotero_web",
    "label": "Zotero (full library via API)",
    "icon": "📚",
    "kind": "reference",
    "needs_auth": True,
    "destructive_actions": [],
    "read_only_default": True,
}

API = "https://api.zotero.org"


def _creds() -> tuple[str | None, str | None]:
    return secrets.get("zotero.user_id"), secrets.get("zotero.api_key")


def live_status() -> dict:
    uid, key = _creds()
    if not uid: return {"status": "unconfigured", "error": "Missing zotero.user_id"}
    if not key: return {"status": "unconfigured", "error": "Missing zotero.api_key"}
    try:
        r = httpx.get(f"{API}/users/{uid}/items", params={"limit": 1},
                      headers={"Zotero-API-Key": key,
                               "Zotero-API-Version": "3"}, timeout=15)
        if r.status_code == 200:
            total = int(r.headers.get("Total-Results", 0))
            return {"status": "ok", "total_items": total}
        if r.status_code == 403:
            return {"status": "error",
                    "error": (f"403 forbidden — API key invalid or doesn't have library-read permission. "
                              f"Re-check at zotero.org/settings/keys: the key must have "
                              f"'Allow library access' checked. Body: {r.text[:120]}")}
        if r.status_code == 404:
            return {"status": "error",
                    "error": (f"404 — User ID {uid} not found. Verify the numeric User ID at the top of "
                              f"zotero.org/settings/keys (not your username — the integer below)."
                              f" Body: {r.text[:80]}")}
        if r.status_code == 429:
            return {"status": "error",
                    "error": "429 — rate-limited. Wait a minute and retry."}
        return {"status": "error", "error": f"HTTP {r.status_code}: {r.text[:200]}"}
    except httpx.TimeoutException:
        return {"status": "error", "error": "Timed out reaching api.zotero.org"}
    except httpx.ConnectError as e:
        return {"status": "error", "error": f"Can't reach Zotero: {e}"}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


def snapshot() -> dict:
    uid, key = _creds()
    if not uid or not key:
        return {"status": "unconfigured", "error": "Missing credentials"}
    items: list[dict] = []
    headers = {"Zotero-API-Key": key, "Zotero-API-Version": "3"}
    start = 0
    limit = 100  # Zotero's max per request
    total_expected = None
    try:
        with httpx.Client(headers=headers, timeout=30) as c:
            while True:
                r = c.get(f"{API}/users/{uid}/items",
                          params={"start": start, "limit": limit,
                                  "format": "json", "include": "data"})
                if r.status_code != 200:
                    return {"status": "error",
                            "error": f"HTTP {r.status_code}: {r.text[:200]}"}
                batch = r.json()
                if not batch: break
                for it in batch:
                    d = it.get("data", {}) or {}
                    if d.get("itemType") in ("attachment", "note"): continue
                    items.append({
                        "id":      it.get("key"),
                        "title":   d.get("title", ""),
                        "doi":     d.get("DOI", ""),
                        "url":     d.get("url", ""),
                        "year":    (d.get("date") or "")[:4],
                        "type":    d.get("itemType", ""),
                        "added":   d.get("dateAdded", ""),
                        "creators": ", ".join(
                            f"{c.get('firstName','')} {c.get('lastName','')}".strip()
                            for c in d.get("creators", [])[:3]
                        ),
                    })
                if total_expected is None:
                    total_expected = int(r.headers.get("Total-Results", len(batch)))
                start += limit
                # Zotero asks for backoff; respect Retry-After if present
                ra = r.headers.get("Backoff") or r.headers.get("Retry-After")
                if ra:
                    try: time.sleep(min(float(ra), 5))
                    except: pass
                if start >= (total_expected or 0): break
                # safety cap so we don't loop forever on a buggy api
                if len(items) >= 50000: break
        return {"status": "ok", "synced_at": datetime.now().isoformat(),
                "count": len(items), "total_in_library": total_expected,
                "items": items}
    except Exception as e:
        return {"status": "error", "error": str(e)[:240]}


def items_list(limit: int = 100) -> list[dict]:
    s = latest_snapshot(META["id"])
    return s.get("items", [])[:limit] if s and s.get("status") == "ok" else []


# Adapter protocol uses `items` as the function name
def items(limit: int = 100) -> list[dict]:
    return items_list(limit)


def stats() -> dict:
    s = latest_snapshot(META["id"])
    if not s: return {"status": "no-snapshot", "count": 0, "last_synced": None}
    return {"status": s.get("status", "ok"), "count": s.get("count", 0),
            "last_synced": (s.get("synced_at") or "")[:16]}
