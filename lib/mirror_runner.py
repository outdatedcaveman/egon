"""Mirror runner — keeps Notion and Obsidian mirroring each other.

Bruno 2026-06-12: "Notion and Obsidian should always mirror each other" and
"ALL my info instantiated as entities."

Two mirrors, two very different cost profiles:
  • Obsidian — local markdown writes, no rate limit. Full instantiation of
    the entire corpus (250k+ Zotero + everything) is done in one pass by
    lib.obsidian_mirror. Re-run is cheap.
  • Notion — REST API at a few requests/sec, and the dedup read of an
    existing DB is O(all rows). A naive full mirror of 250k items is a
    MULTI-DAY job and would hammer rate limits. So Notion fills
    INCREMENTALLY: newest-first, a bounded batch per run, with a persistent
    per-source cursor in state/mirror_runner.json so we never re-push or
    re-scan what's already mirrored. Over many runs it converges to full,
    then just carries new items.

This runner is what egon_core calls on a slow cadence. A one-shot full
Obsidian mirror is mirror_obsidian_full().
"""
from __future__ import annotations

import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state" / "mirror_runner.json"

# Sources to mirror, in priority order. Zotero first (the big one).
_SOURCES = ["zotero", "paperpile", "chrome_bookmarks", "letterboxd",
            "instapaper", "notion_workspace"]

# Notion budget per run — keep well under rate limits and leave the API
# responsive for everything else. ~150 writes ≈ a minute of API time.
_NOTION_BATCH = 150


def _load() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(d: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(d, indent=2), encoding="utf-8")


def _snapshot_for(source: str) -> dict | None:
    if source == "zotero":
        try:
            from lib.adapters import zotero_local
            return zotero_local.snapshot()
        except Exception:
            return None
    try:
        from lib import cross_search
        return cross_search._latest_snapshot_for(source)
    except Exception:
        return None


def mirror_obsidian_full() -> dict:
    """One-shot: instantiate the whole corpus as vault notes. Cheap; no API."""
    from lib import obsidian_mirror
    return obsidian_mirror.mirror_all()


def run_notion_increment(batch: int = _NOTION_BATCH) -> dict:
    """Advance the Notion mirror by one bounded, newest-first batch across
    sources whose cursor hasn't caught up. Persistent cursor; never re-pushes.
    """
    from lib import notion_mirror
    if not notion_mirror.EGON_PAGE_ID:
        return {"status": "no_root",
                "error": "notion.egon_page_id not set in egon-config.json"}
    state = _load()
    cursors = state.setdefault("notion_cursor", {})
    spent = 0
    report = {}
    for source in _SOURCES:
        if spent >= batch:
            break
        snap = _snapshot_for(source)
        items = (snap or {}).get("items") or []
        if not items:
            report[source] = "no items"
            continue
        done = int(cursors.get(source, 0))
        if done >= len(items):
            report[source] = f"caught up ({done})"
            continue
        take = min(batch - spent, len(items) - done)
        window = {"items": items[done:done + take]}
        try:
            # The window is small, so mirror_to_notion's per-call existing-keys
            # read stays bounded; dedup still protects against double-insert.
            res = notion_mirror.mirror_to_notion(source, window, max_items=take, assume_new=True)
            advanced = res.get("inserted", 0) + res.get("updated", 0)
            cursors[source] = done + take
            spent += take
            report[source] = (f"+{advanced} ({cursors[source]}/{len(items)})"
                              + (f" {res.get('errors')} err" if res.get("errors") else ""))
        except Exception as e:
            report[source] = f"error: {str(e)[:80]}"
            break
    _save(state)
    return {"status": "ok", "pushed": spent, "by_source": report,
            "ts": int(time.time())}


def status() -> dict:
    """Mirror progress for the Databases observatory."""
    state = _load()
    cur = state.get("notion_cursor", {})
    out = {"notion_cursor": cur}
    try:
        from lib import obsidian_mirror
        out["obsidian"] = obsidian_mirror.stats()
    except Exception:
        pass
    return out
