"""Notion mirror writer — slim DBs, batched, idempotent.

Lessons from the slow "Zotero Database" attempt this is replacing:
1. **Slim schema**: max 5 properties per DB row + title. No body content. No images. No relations.
2. **Batched upserts**: one HTTP call per item, but rate-limited (Notion: 3 req/sec).
   Daily pass batches up to ~500 items per source — slow but background.
3. **Idempotent**: lookup by stable key before insert — never duplicates, never deletes.
4. **One DB per source** under 🛰️ Egon / 050 Mirrors / — easy to drop one without
   breaking others.
5. **Off by default**: per-source toggle in egon-config.json. User opts in once they
   eyeball the schema and like it.

To enable for one source:
    egon-config.json:
      "mirror": { "notion": { "letterboxd": true } }
"""
from __future__ import annotations

import time
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from lib.lazy_httpx import httpx  # deferred ~2s import (2026-06-11 perf pass)

# Egon root in Notion — set NOTION_EGON_PAGE_ID to your own page id
EGON_PAGE_ID = os.environ.get("NOTION_EGON_PAGE_ID", "")
MIRRORS_PAGE_TITLE = "050 · Mirrors"
from lib.egon_paths import ENV_FILE as ENV_PATH


def _token() -> str | None:
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        if line.startswith("NOTION_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def _h() -> dict:
    tok = _token()
    return {"Authorization": f"Bearer {tok}", "Notion-Version": "2022-06-28",
            "Content-Type": "application/json"}


# Schema per source — TITLE first, then 4 more properties. That's the cap.
SCHEMAS: dict[str, dict] = {
    "letterboxd": {
        "title_prop":   "Title",
        "title_from":   lambda i: i.get("title") or i.get("slug", "?"),
        "key_from":     lambda i: i.get("slug") or i.get("title", ""),
        "properties": {
            "Title":   {"title": {}},
            "Year":    {"rich_text": {}},
            "Rating":  {"number": {"format": "number"}},
            "Liked":   {"checkbox": {}},
            "URL":     {"url": {}},
        },
        "values": lambda i: {
            "Year":   {"rich_text": [{"type": "text", "text": {"content": str(i.get("year") or "")}}]} ,
            "Rating": {"number": float(i["rating"]) if i.get("rating") else None},
            "Liked":  {"checkbox": bool(i.get("liked"))},
            "URL":    {"url": i.get("url") or None},
        },
    },
    "chrome_bookmarks": {
        "title_prop": "Title",
        "title_from": lambda i: i.get("title") or i.get("url", "?")[:80],
        "key_from":   lambda i: (i.get("url") or "")[:160],
        "properties": {
            "Title":  {"title": {}},
            "URL":    {"url": {}},
            "Folder": {"rich_text": {}},
            "Added":  {"rich_text": {}},
        },
        "values": lambda i: {
            "URL":    {"url": i.get("url") or None},
            "Folder": {"rich_text": [{"type": "text", "text": {"content": (i.get("folder") or "")[:200]}}]},
            "Added":  {"rich_text": [{"type": "text", "text": {"content": str(i.get("added") or "")[:30]}}]},
        },
    },
    "zotero": {
        "title_prop": "Title",
        "title_from": lambda i: i.get("title", "?"),
        "key_from":   lambda i: (i.get("doi") or f"zot:{i.get('id','')}").lower(),
        "properties": {
            "Title": {"title": {}},
            "DOI":   {"rich_text": {}},
            "Added": {"rich_text": {}},
        },
        "values": lambda i: {
            "DOI":   {"rich_text": [{"type": "text", "text": {"content": (i.get("doi") or "")[:100]}}]},
            "Added": {"rich_text": [{"type": "text", "text": {"content": str(i.get("added") or "")[:30]}}]},
        },
    },
}


# -- DB lifecycle ------------------------------------------------------------

def _find_or_create_mirrors_page() -> str:
    """Return page-id of the '050 · Mirrors' container page under Egon root."""
    r = httpx.get(f"https://api.notion.com/v1/blocks/{EGON_PAGE_ID}/children",
                  headers=_h(), timeout=15)
    if r.status_code == 200:
        for b in r.json().get("results", []):
            if b.get("type") == "child_page" and b.get("child_page", {}).get("title") == MIRRORS_PAGE_TITLE:
                return b["id"]
    # create
    r = httpx.post("https://api.notion.com/v1/pages", headers=_h(), timeout=15, json={
        "parent": {"page_id": EGON_PAGE_ID},
        "icon":   {"type": "emoji", "emoji": "🪞"},
        "properties": {"title": {"title": [{"type": "text", "text": {"content": MIRRORS_PAGE_TITLE}}]}},
    })
    r.raise_for_status()
    return r.json()["id"]


def _find_or_create_source_db(source: str) -> str:
    """Return database-id for the source's mirror DB under '050 · Mirrors'.
    Creates with the slim schema if missing."""
    mirrors_page = _find_or_create_mirrors_page()

    # search for an existing DB whose title matches
    title = source
    r = httpx.get(f"https://api.notion.com/v1/blocks/{mirrors_page}/children",
                  headers=_h(), timeout=15)
    if r.status_code == 200:
        for b in r.json().get("results", []):
            if b.get("type") == "child_database":
                t = b.get("child_database", {}).get("title", "")
                if t == title:
                    return b["id"]

    schema = SCHEMAS[source]
    r = httpx.post("https://api.notion.com/v1/databases", headers=_h(), timeout=20, json={
        "parent": {"page_id": mirrors_page, "type": "page_id"},
        "icon":   {"type": "emoji", "emoji": "🪞"},
        "title":  [{"type": "text", "text": {"content": title}}],
        "properties": schema["properties"],
    })
    r.raise_for_status()
    return r.json()["id"]


# -- upserts -----------------------------------------------------------------

def _existing_keys(db_id: str, key_prop: str = "_key") -> dict[str, str]:
    """Return {key: page_id} for every existing row in the DB.

    NOTE: we don't store a separate `_key` property — we use the title-equivalent
    derived from the schema's title_from() to dedup. So this function reads ALL
    rows and indexes by title.
    """
    out: dict[str, str] = {}
    cursor = None
    while True:
        body: dict[str, Any] = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        r = httpx.post(f"https://api.notion.com/v1/databases/{db_id}/query",
                       headers=_h(), json=body, timeout=20)
        if r.status_code != 200:
            return out
        data = r.json()
        for p in data.get("results", []):
            props = p.get("properties", {})
            title_prop = next((k for k, v in props.items() if v.get("type") == "title"), None)
            if title_prop:
                t = "".join(seg.get("plain_text", "") for seg in props[title_prop]["title"])
                out[t.lower()] = p["id"]
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return out


def mirror_to_notion(source: str, snapshot: dict, max_items: int = 500) -> dict:
    """Upsert every snapshot item to the source's Notion DB. Returns stats."""
    if source not in SCHEMAS:
        return {"status": "no_schema", "error": f"no slim schema for {source}"}

    schema = SCHEMAS[source]
    db_id = _find_or_create_source_db(source)
    existing = _existing_keys(db_id)

    inserted = updated = errors = 0
    items_to_process = snapshot.get("items", [])[:max_items]
    for item in items_to_process:
        try:
            title = schema["title_from"](item) or "(untitled)"
            values = schema["values"](item)
            properties = {
                schema["title_prop"]: {"title": [{"type": "text", "text": {"content": title[:200]}}]},
                **values,
            }
            existing_id = existing.get(title.lower())
            if existing_id:
                r = httpx.patch(f"https://api.notion.com/v1/pages/{existing_id}",
                                headers=_h(), json={"properties": properties}, timeout=20)
                if r.status_code == 200:
                    updated += 1
                else:
                    errors += 1
            else:
                r = httpx.post("https://api.notion.com/v1/pages",
                               headers=_h(), json={"parent": {"database_id": db_id},
                                                   "properties": properties}, timeout=20)
                if r.status_code == 200:
                    inserted += 1
                else:
                    errors += 1
            time.sleep(0.34)  # rate-limit: Notion allows 3 req/sec
        except Exception:
            errors += 1

    return {
        "status": "ok",
        "db_id": db_id,
        "inserted": inserted,
        "updated":  updated,
        "errors":   errors,
        "total":    len(items_to_process),
    }
