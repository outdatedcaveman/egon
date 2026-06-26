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

# Sources to mirror, in priority order: mind sources and curated items first,
# and Zotero last to prevent it from starving smaller, high-priority databases.
_PRIORITY = ["paperpile", "chrome_bookmarks", "letterboxd",
             "instapaper", "notion_workspace", "unified_resources"]
_MIND_SOURCES = ["mind_sessions", "mind_projects", "mind_memories",
                 "mind_skills"]


def _all_sources() -> list[str]:
    extra: list[str] = []
    try:
        from lib import cross_search
        extra = [s for s in cross_search._all_sources()
                 if s not in _PRIORITY and s != "zotero"]
    except Exception:
        pass
    # chrome_tabs is EPHEMERAL (open tabs churn by the minute): mirroring it
    # to Notion would spend the whole API budget on stale updates. It stays
    # in the Obsidian mirror (free local writes); everything durable goes to
    # both. "Efficiently yet thoroughly" — Bruno 2026-06-12.
    extra = [x for x in extra if x != "chrome_tabs"]
    return _MIND_SOURCES + _PRIORITY + sorted(extra) + ["zotero"]

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


_mind_cache: dict = {}


def _snapshot_for(source: str) -> dict | None:
    if source == "zotero":
        try:
            from lib.adapters import zotero_local
            return zotero_local.snapshot()
        except Exception:
            return None
    if source.startswith("mind_"):
        global _mind_cache
        if not _mind_cache:
            try:
                from lib.obsidian_mirror import _mind_entities
                _mind_cache = _mind_entities()
            except Exception:
                _mind_cache = {}
        items = _mind_cache.get(source) or []
        return {"items": items} if items else None
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


# In-process keyed-snapshot cache. egon_core runs Notion catchup every cycle;
# without this, each batch RE-LOADS + RE-HASHES the entire source snapshot
# (Zotero = 258k items) just to find the next 500 to push — pure CPU/RAM waste
# that made "always-on" expensive. We cache (items + key→hash) per source for a
# TTL, so the heavy pass runs once per window and each batch is then ~free.
# Bruno 2026-06-25.
_KEYED_CACHE: dict[str, tuple] = {}
_KEYED_TTL = 900.0  # seconds
# Network full-DB reconcile only when this many entries still lack a page id,
# and at most once per source per process (avoids re-scanning a 30k-page DB to
# match a handful of stragglers — the bug that hung catchup).
_RECONCILE_MIN = 200
_RECONCILED: set = set()


def _keyed_snapshot(source: str):
    """Return (items, {key: hash}) for a source, cached in-process for _KEYED_TTL."""
    hit = _KEYED_CACHE.get(source)
    if hit and (time.time() - hit[0]) < _KEYED_TTL:
        return hit[1], hit[2]
    snap = _snapshot_for(source)
    items = (snap or {}).get("items") or []
    khash = {_item_key(it): _item_hash(it) for it in items}
    _KEYED_CACHE[source] = (time.time(), items, khash)
    return items, khash


def reconcile_existing() -> dict:
    """One-time: import every page already in the Notion mirror DBs into the
    page map (key→{pid,hash}), matched to current snapshot items by title.
    After this, all existing pages are tracked, so the runner can use the
    fast no-scan insert path and never duplicate an orphan. Idempotent."""
    from lib import notion_mirror
    state = _load()
    pages_map = state.setdefault("notion_pages", {})
    report = {}
    for source in _all_sources():
        # generic schema covers every source now
        try:
            db_id = notion_mirror._find_or_create_source_db(source)
            title_to_id = notion_mirror._existing_keys(db_id)  # {title_lower: pid}
            snap = _snapshot_for(source)
            items = (snap or {}).get("items") or []
            schema = (notion_mirror.SCHEMAS.get(source) or notion_mirror.GENERIC_SCHEMA)
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
    # Guarantee the bulk backfill (zotero — ~222k still to push) a share of
    # every batch. Otherwise last-priority ordering + small-DB hash churn (e.g.
    # paperpile's perpetually-"pending" items) eats the whole budget and zotero
    # never advances. Non-bulk sources spend at most SMALL_CAP combined; zotero
    # gets the rest. Bruno 2026-06-26.
    _BULK = {"zotero"}
    SMALL_CAP = max(40, batch // 5)
    small_spent = 0
    for source in _all_sources():
        if spent >= batch:
            break
        items, khash = _keyed_snapshot(source)
        if not items:
            report[source] = "no items"
            continue
        pm = pages_map.setdefault(source, {})

        # Reconcile entries pushed before we tracked page ids: match them to
        # existing Notion pages by title so we PATCH instead of duplicating.
        # `_existing_keys` reads the WHOLE source DB over the network, so it is
        # only worth it when a meaningful chunk is still un-pid'd (a fresh
        # migration). For a handful of stragglers it would re-scan tens of
        # thousands of pages every batch — the hang that stalled catchup. Skip
        # below the threshold (those few just re-create; negligible dup risk).
        # Also reconcile each source at most once per process. Bruno 2026-06-25.
        no_pid = sum(1 for rec in pm.values() if rec.get("pid") is None)
        if no_pid > _RECONCILE_MIN and source not in _RECONCILED:
            _RECONCILED.add(source)
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
                    title = ((notion_mirror.SCHEMAS.get(source) or notion_mirror.GENERIC_SCHEMA)["title_from"](it)
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
            if rec is None or rec.get("h") != khash.get(k):
                pending.append(it)
        if not pending:
            report[source] = f"in sync ({len(pm)})"
            continue
        if source in _BULK:
            avail = batch - spent
        else:
            avail = min(batch - spent, SMALL_CAP - small_spent)
        if avail <= 0:
            report[source] = f"deferred ({len(pending)} pending)"
            continue
        take = min(avail, len(pending))
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
            if source not in _BULK:
                small_spent += take
            report[source] = (
                f"+{res.get('inserted',0)} new / {res.get('updated',0)} upd "
                f"({len(pm)}/{len(items)}, {len(pending)-take} pending)"
                + (f" {res.get('errors')} err" if res.get("errors") else ""))
        except Exception as e:
            # Skip a failing source — never let it halt the loop before the bulk
            # backfill (zotero) gets its turn. Bruno 2026-06-26.
            report[source] = f"error: {str(e)[:80]}"
            continue
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
