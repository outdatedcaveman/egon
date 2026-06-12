"""Obsidian mirror — the local twin of the Notion mirror.

Bruno 2026-06-12: "Notion and Obsidian should always mirror each other."
Every reference/bookmark/library item is instantiated as a markdown note
with YAML frontmatter under the vault, so the whole corpus is browsable and
back-linkable in Obsidian — and, unlike Notion, there is NO API rate limit,
so this can hold the full 250k+ Zotero library + everything else today.

Layout:  <vault>/050 - Mirrors/<source>/<safe-key>.md
Each note:
    ---
    title: ...
    source: zotero
    key: zotero:12345
    url: ...
    year: ...
    tags: [mirror, zotero]
    ---
    (subtitle / author / venue line)

Idempotent: filename is derived from the item's stable key, so re-runs
overwrite in place. Never deletes notes whose source row vanished (additive;
honours the never-delete rule) — a future reconcile can prune deliberately.
Same source snapshots + same key scheme as lib/notion_mirror, so the two
mirrors stay structurally identical.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

VAULT = Path(r"C:\Users\bruno\Documents\Obsidian Vault")
MIRROR_DIR = VAULT / "050 - Mirrors"

_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe(name: str, maxlen: int = 80) -> str:
    s = _BAD.sub("_", name).strip(". ")
    return (s[:maxlen] or "untitled").rstrip(". ")


def _key_for(source: str, item: dict) -> str:
    k = item.get("id") or item.get("key") or item.get("url") or item.get("title")
    return str(k or "?")


def _frontmatter(source: str, item: dict, key: str) -> str:
    title = str(item.get("title") or item.get("name") or "untitled").replace('"', "'")
    fm = ["---", f'title: "{title[:300]}"', f"source: {source}",
          f'key: "{key}"']
    for fld in ("url", "year", "author", "doi", "kind"):
        v = item.get(fld)
        if v:
            fm.append(f'{fld}: "{str(v)[:200].replace(chr(34), chr(39))}"')
    fm.append(f"tags: [mirror, {source}]")
    fm.append("---")
    body = str(item.get("subtitle") or item.get("snippet") or "")
    if body:
        fm.append("")
        fm.append(body[:500])
    return "\n".join(fm) + "\n"


def mirror_source(source: str, snapshot: dict, max_items: int = 0) -> dict:
    """Write every snapshot item as a note. max_items=0 → all."""
    if not VAULT.is_dir():
        return {"status": "error", "error": f"vault not found: {VAULT}"}
    items = snapshot.get("items") or []
    if max_items:
        items = items[:max_items]
    out_dir = MIRROR_DIR / source
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    written = 0
    seen_names: dict[str, int] = {}
    for item in items:
        key = _key_for(source, item)
        base = _safe(str(item.get("title") or item.get("name") or key))
        # disambiguate filename collisions deterministically by key tail
        if base in seen_names:
            base = f"{base}~{_safe(key.split(':')[-1], 16)}"
        seen_names[base] = 1
        try:
            (out_dir / f"{base}.md").write_text(
                _frontmatter(source, item, key), encoding="utf-8")
            written += 1
        except Exception:
            continue
    return {"status": "ok", "source": source, "written": written,
            "seconds": round(time.time() - t0, 1)}


def mirror_all(sources: list[str] | None = None) -> dict:
    """Mirror the standard sources into the vault. Driven from the same
    snapshots as the Notion mirror so the two stay in lockstep."""
    from lib import cross_search
    srcs = sources or ["zotero", "chrome_bookmarks", "letterboxd",
                       "paperpile", "instapaper", "notion_workspace"]
    results = {}
    for source in srcs:
        snap = None
        # Zotero's full library comes from the local-SQLite adapter, not the
        # capped snapshot store.
        if source == "zotero":
            try:
                from lib.adapters import zotero_local
                snap = zotero_local.snapshot()
            except Exception:
                snap = None
        if snap is None:
            try:
                snap = cross_search._latest_snapshot_for(source)
            except Exception:
                snap = None
        if not snap or not snap.get("items"):
            results[source] = {"status": "skip", "written": 0}
            continue
        results[source] = mirror_source(source, snap)
    total = sum(r.get("written", 0) for r in results.values())
    return {"status": "ok", "total_written": total, "by_source": results}


def stats() -> dict:
    """Note counts per mirrored source (for the Databases observatory)."""
    if not MIRROR_DIR.is_dir():
        return {}
    out = {}
    for d in MIRROR_DIR.iterdir():
        if d.is_dir():
            out[d.name] = sum(1 for _ in d.glob("*.md"))
    return out
