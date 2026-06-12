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


def _item_hash(item: dict) -> str:
    import hashlib
    blob = "|".join(str(item.get(f, "")) for f in
                    ("title", "url", "subtitle", "author", "year", "kind"))
    return hashlib.md5(blob.encode("utf-8", "ignore")).hexdigest()[:10]


def reconcile_existing() -> dict:
    """One-time: import every page already in the Notion mirror DBs into the
    page map (key→{pid,hash}), matched to current snapshot items by title.
    After this, all existing pages are tracked, so the runner can use the
    fast no-scan insert path and never duplicate an orphan. Idempotent."""
    from lib import notion_mirror
    state = _load()
    pages_map = state.setdefault("notion_pages", {})
    report = {}
    for source in _SOURCES:
        if source not in notion_mirror.SCHEMAS:
            continue
        try:
            db_id = notion_mirror._find_or_create_source_db(source)
            title_to_id = notion_mirror._existing_keys(db_id)  # {title_lower: pid}
            snap = _snapshot_for(source)
            items = (snap or {}).get("items") or []
            schema = notion_mirror.SCHEMAS[source]
            pm = pages_map.setdefault(source, {})
            matched = 0
            for it in items:
                title = (schema["title_from"](it) or "").lower()
                pid = title_to_id.get(title)
                if pid:
                    pm[_item_key(it)] = {"pid": pid, "h": _item_hash(it)}
                    matched += 1
            report[source] = f"{matched} tracked / {len(title_to_id)} pages"
        except Exception as e:
            report[source] = f"error: {str(e)[:60]}"
    _save(state)
    return {"status": "ok", "by_source": report}


def run_notion_increment(batch: int = _NOTION_BATCH) -> dict:
    """Advance the Notion mirror by one bounded batch — TRUE UPSERT.

    Per source we persist a page map {key: {"pid": notion_page_id, "h":
    content_hash}}. An item is pending when:
      • its key is new (→ POST, create), or
      • its content hash changed since we last pushed (→ PATCH that page).
    So we never create a second page for an item we've already mirrored
    (Bruno 2026-06-12: "make Notion update instead of create new ones"), and
    edits flow as updates. Migrates the old key-set/positional state in place.
    """
    from lib import notion_mirror
    if not notion_mirror.EGON_PAGE_ID:
        return {"status": "no_root",
                "error": "notion.egon_page_id not set in egon-config.json"}
    state = _load()
    pages_map = state.setdefault("notion_pages", {})
    # one-time migration: old {source: [key,...]} set → {key: {pid:None,h:None}}
    old = state.get("notion_pushed")
    if old and not pages_map:
        for src, keys in old.items():
            pages_map[src] = {k: {"pid": None, "h": None} for k in keys}
        state.pop("notion_pushed", None)

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
        pm = pages_map.setdefault(source, {})

        # Reconcile any entries we pushed before we tracked page ids (the
        # migrated set): look up their existing Notion pages by title ONCE so
        # we PATCH them instead of creating duplicates. Runs only while no-pid
        # entries remain; cheap because those DBs are still small.
        if any(rec.get("pid") is None for rec in pm.values()):
            try:
                from lib import notion_mirror
                db_id = notion_mirror._find_or_create_source_db(source)
                title_to_id = notion_mirror._existing_keys(db_id)
                key_to_item = {_item_key(it): it for it in items}
                for k, rec in pm.items():
                    if rec.get("pid"):
                        continue
                    it = key_to_item.get(k)
                    if not it:
                        continue
                    title = (notion_mirror.SCHEMAS[source]["title_from"](it)
                             or "").lower()
                    pid = title_to_id.get(title)
                    if pid:
                        rec["pid"] = pid
            except Exception:
                pass
        pending = []
        for it in items:
            k = _item_key(it)
            rec = pm.get(k)
            if rec is None or rec.get("h") != _item_hash(it):
                pending.append(it)
        if not pending:
            report[source] = f"in sync ({len(pm)})"
            continue
        take = min(batch - spent, len(pending))
        window_items = []
        for it in pending[:take]:
            k = _item_key(it)
            d = dict(it)
            d["_key"] = k
            d["_page_id"] = (pm.get(k) or {}).get("pid")
            window_items.append(d)
        try:
            res = notion_mirror.mirror_to_notion(
                source, {"items": window_items}, max_items=take,
                assume_new=True)
            new_ids = res.get("ids") or {}
            for it in pending[:take]:
                k = _item_key(it)
                pid = new_ids.get(k) or (pm.get(k) or {}).get("pid")
                pm[k] = {"pid": pid, "h": _item_hash(it)}
            spent += take
            report[source] = (
                f"+{res.get('inserted',0)} new / {res.get('updated',0)} upd "
                f"({len(pm)}/{len(items)}, {len(pending)-take} pending)"
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
