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

# Notion budget per run. At ~1.33s/item (3 parallel workers) a 500-item
# run is ~11min; the runner's _mirror_running guard lets it span core
# cycles, so the fill is effectively continuous (~65k/day). Bruno wants the
# full 360k mirrored even if it takes days.
_NOTION_BATCH = 500


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


def _item_key(item: dict) -> str:
    return str(item.get("id") or item.get("key") or item.get("url")
               or item.get("title") or "?")


def run_notion_increment(batch: int = _NOTION_BATCH) -> dict:
    """Advance the Notion mirror by one bounded batch of UNPUSHED items.

    Keyed by stable item id, NOT a positional offset (2026-06-12 fix): new
    Zotero refs land at the top of the newest-first snapshot, so an index
    cursor silently skipped them. We persist the SET of already-pushed keys
    per source; any item whose key isn't in that set is pending, so brand-new
    references flow automatically the next time the runner fires.
    """
    from lib import notion_mirror
    if not notion_mirror.EGON_PAGE_ID:
        return {"status": "no_root",
                "error": "notion.egon_page_id not set in egon-config.json"}
    state = _load()
    pushed_map = state.setdefault("notion_pushed", {})
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
        pushed = set(pushed_map.get(source, []))
        pending = [it for it in items if _item_key(it) not in pushed]
        if not pending:
            report[source] = f"caught up ({len(pushed)})"
            continue
        take = min(batch - spent, len(pending))
        window = {"items": pending[:take]}
        try:
            res = notion_mirror.mirror_to_notion(source, window,
                                                 max_items=take, assume_new=True)
            advanced = res.get("inserted", 0) + res.get("updated", 0)
            pushed.update(_item_key(it) for it in pending[:take])
            pushed_map[source] = sorted(pushed)
            spent += take
            report[source] = (
                f"+{advanced} ({len(pushed)}/{len(items)}, "
                f"{len(pending) - take} pending)"
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
    pushed = {k: len(v) for k, v in
              (state.get("notion_pushed") or {}).items()}
    out = {"notion_pushed": pushed}
    try:
        from lib import obsidian_mirror
        out["obsidian"] = obsidian_mirror.stats()
    except Exception:
        pass
    return out
