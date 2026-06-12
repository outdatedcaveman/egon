"""Notion page-body fetcher — your manual Notion content, fully readable.

Bruno 2026-06-12 (#66 + "make sure we're saving ALL the content"): most of
his manual additions happen IN Notion, but the workspace snapshot only
carried titles+metadata, so the Obsidian mirror showed stubs. This fetches
each page's actual block content as markdown-ish text, cached on disk and
re-fetched ONLY when the page's last_edited_time changes.

Budgeted: _BATCH pages per run (newest-edited first), 3 req burst-safe.
Cache: state/notion_bodies/<page-id>.md with the last_edited_time in a
sidecar index, so unchanged pages cost zero API calls forever.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BODY_DIR = ROOT / "state" / "notion_bodies"
INDEX = BODY_DIR / "_index.json"
_BATCH = 60          # pages per run; ~2 req/page → about a minute of API
_MAX_BLOCKS = 200    # per page — covers real notes; truncates monsters


def _h():
    from lib.adapters.notion_workspace import _h as h
    return h()


def _rich(rt) -> str:
    return "".join(seg.get("plain_text", "") for seg in (rt or []))


def _block_text(b: dict) -> str:
    t = b.get("type")
    d = b.get(t) or {}
    txt = _rich(d.get("rich_text"))
    if t == "heading_1":
        return f"# {txt}"
    if t == "heading_2":
        return f"## {txt}"
    if t == "heading_3":
        return f"### {txt}"
    if t == "bulleted_list_item":
        return f"- {txt}"
    if t == "numbered_list_item":
        return f"1. {txt}"
    if t == "to_do":
        mark = "x" if d.get("checked") else " "
        return f"- [{mark}] {txt}"
    if t == "quote":
        return f"> {txt}"
    if t == "code":
        return f"```\n{txt}\n```"
    if t == "child_page":
        return f"→ subpage: {d.get('title', '')}"
    if t == "child_database":
        return f"→ database: {d.get('title', '')}"
    return txt


def fetch_page_body(page_id: str) -> str:
    """All text blocks of a page (one level; sub-blocks summarised)."""
    from lib.lazy_httpx import httpx
    lines: list[str] = []
    cursor = None
    fetched = 0
    while fetched < _MAX_BLOCKS:
        url = f"https://api.notion.com/v1/blocks/{page_id}/children?page_size=100"
        if cursor:
            url += f"&start_cursor={cursor}"
        r = httpx.get(url, headers=_h(), timeout=30)
        if r.status_code != 200:
            break
        d = r.json()
        for b in d.get("results", []):
            fetched += 1
            txt = _block_text(b)
            if txt and txt.strip():
                lines.append(txt)
        if not d.get("has_more"):
            break
        cursor = d.get("next_cursor")
        time.sleep(0.34)
    return "\n".join(lines)


def body_for(page_id: str) -> str:
    """Cached body text for a page id ('' if never fetched)."""
    p = BODY_DIR / f"{page_id}.md"
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        return ""


def refresh(batch: int = _BATCH) -> dict:
    """Fetch/refresh bodies for the most recently EDITED workspace pages whose
    cached last_edited_time is stale. Returns counts."""
    from lib import cross_search
    snap = None
    try:
        snap = cross_search._latest_snapshot_for("notion_workspace")
    except Exception:
        pass
    items = (snap or {}).get("items") or []
    if not items:
        return {"status": "no_snapshot", "fetched": 0}

    BODY_DIR.mkdir(parents=True, exist_ok=True)
    try:
        idx = json.loads(INDEX.read_text(encoding="utf-8"))
    except Exception:
        idx = {}

    fetched = skipped = errors = 0
    t0 = time.time()
    for it in items:                       # snapshot is newest-edited first
        if fetched >= batch:
            break
        pid = it.get("id") or ""
        if not pid or it.get("object") != "page" or it.get("archived"):
            continue
        edited = it.get("last_edited_time") or ""
        if idx.get(pid) == edited:
            skipped += 1
            continue
        try:
            body = fetch_page_body(pid)
            (BODY_DIR / f"{pid}.md").write_text(body, encoding="utf-8")
            idx[pid] = edited
            fetched += 1
            time.sleep(0.34)
        except Exception:
            errors += 1
    INDEX.write_text(json.dumps(idx, indent=1), encoding="utf-8")
    return {"status": "ok", "fetched": fetched, "cached_total": len(idx),
            "skipped_fresh": skipped, "errors": errors,
            "seconds": round(time.time() - t0, 1)}
