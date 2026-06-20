"""Mirror — snapshot data → Notion DB rows + Obsidian markdown pages.

This is the spine of the "second brain" plan: every snapshot we pull (films,
bookmarks, refs, tabs, ...) gets mirrored to:

1. **Notion** — one database per source under 🧠 KMS / 050-Resources / Mirrors / .
   Schema is derived from the snapshot items (title, url, date, rating, ...).
   Upsert by stable key (slug, doi, url) — never duplicates, never deletes.

2. **Obsidian vault** — one markdown page per item under
   G:/MetaVault/.../050-Resources/Mirrors/<source>/YYYY/MM/<slug>.md
   Front-matter has all the metadata; body links to the original.

Both writes are IDEMPOTENT and ADDITIVE. We never delete prior entries; if an
item disappears from a source, the mirror keeps it (with a `_removed: true`
note in front-matter). This is the second-brain guarantee.

Status: SCAFFOLD. The mapping functions are stubbed for each source so the
data structure is defined, but actual Notion/Obsidian writes are guarded behind
`enable_mirror: true` in egon-config.json (default OFF) — protects against
accidental flooding of Notion / vault before the user reviews the plan.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable

from lib import secrets

# vault location for mirrored pages
from lib.egon_paths import VAULT_MIRROR_ROOT

# Notion DB id mapping (one DB per source). The DBs themselves are created lazily
# the first time mirror is enabled for that source.
NOTION_DB_MAP: dict[str, str] = {}


# -- per-source field mappers -----------------------------------------------
# Each returns (stable_key, properties_dict) for one snapshot item.

def _letterboxd_map(item: dict) -> tuple[str, dict]:
    slug = item.get("slug") or item.get("title", "").lower().replace(" ", "-")
    return slug, {
        "Title":  item.get("title"),
        "Year":   item.get("year"),
        "Rating": item.get("rating"),
        "Liked":  item.get("liked"),
        "URL":    item.get("url"),
    }


def _chrome_bookmark_map(item: dict) -> tuple[str, dict]:
    return item.get("url", "")[:120], {
        "Title":   item.get("title"),
        "URL":     item.get("url"),
        "Folder":  item.get("folder"),
        "Added":   item.get("added"),
    }


def _zotero_map(item: dict) -> tuple[str, dict]:
    return (item.get("doi") or f"zot:{item.get('id','')}"), {
        "Title": item.get("title"),
        "DOI":   item.get("doi"),
        "Added": item.get("added"),
    }


def _unified_resource_map(item: dict) -> tuple[str, dict]:
    return item.get("id", ""), {
        "Title":  item.get("title"),
        "URL":    item.get("url"),
        "Detail": item.get("abstract"),
        "Kind":   item.get("kind"),
        "Year":   item.get("year"),
        "Key":    item.get("id"),
    }


MAPPERS: dict[str, Callable[[dict], tuple[str, dict]]] = {
    "letterboxd":       _letterboxd_map,
    "chrome_bookmarks": _chrome_bookmark_map,
    "zotero":           _zotero_map,
    "unified_resources": _unified_resource_map,
}


# -- enabled? ----------------------------------------------------------------

def is_enabled(source: str | None = None, target: str = "vault") -> bool:
    """Two-tier gate: vault writes default ON, Notion writes default OFF.

    Rationale:
    - Vault writes are local additive markdown files — cheap, safe, fast.
    - Notion writes are slow + flood the workspace + duplicate-risk (the
      "slow Zotero DB" lesson). Off by default; user must explicitly opt in.

    Override per source via egon-config.json:
        "mirror": {
            "vault":  { "letterboxd": false }   // disable vault for this source
            "notion": { "letterboxd": true }    // enable notion for this source
        }
    """
    # global kill switches
    if not secrets.get(f"mirror.{target}_enabled", True if target == "vault" else False):
        return False
    if source is None:
        return True
    # per-source toggle (default: True for vault, False for notion)
    default = (target == "vault")
    return bool(secrets.get(f"mirror.{target}.{source}", default))


# -- vault writer ------------------------------------------------------------

def _vault_page_path(source: str, key: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in key)[:60]
    when = datetime.now()
    return (VAULT_MIRROR_ROOT / source / f"{when:%Y}" / f"{when:%m}" / f"{safe}.md")


def write_vault_page(source: str, key: str, props: dict, body: str = "") -> Path:
    """Write one markdown page. Front-matter is YAML; body is free markdown."""
    path = _vault_page_path(source, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = ["---"]
    fm.append(f"source: {source}")
    fm.append(f"key: {key}")
    fm.append(f"mirrored_at: {datetime.now().isoformat()}")
    for k, v in props.items():
        if v is None:
            continue
        # naive YAML — fine for scalar values
        fm.append(f'{k.lower()}: {repr(v) if isinstance(v, (str, int, float, bool)) else v}')
    fm.append("---")
    fm.append("")
    fm.append(body or f"Mirrored from {source}.")
    path.write_text("\n".join(fm), encoding="utf-8")
    return path


# -- top-level entry point ---------------------------------------------------

def mirror_snapshot(source: str, snapshot: dict) -> dict:
    """Mirror every item in `snapshot["items"]` to vault (always, default ON)
    and optionally Notion (only when explicitly opted in per source)."""
    mapper = MAPPERS.get(source)
    if not mapper:
        return {"status": "no_mapper", "error": f"add MAPPERS['{source}'] in lib/mirror.py"}

    if not is_enabled(source, "vault") and not is_enabled(source, "notion"):
        return {"status": "disabled", "hint": "mirror disabled for this source"}

    written_vault = 0
    written_notion = 0
    errors: list[str] = []
    do_vault  = is_enabled(source, "vault")
    do_notion = is_enabled(source, "notion")
    for item in snapshot.get("items", []):
        try:
            key, props = mapper(item)
            if do_vault:
                write_vault_page(source, key, props)
                written_vault += 1
        except Exception as e:
            errors.append(str(e)[:120])

    # Notion mirror is batched, slow, and idempotent — run separately for the whole snapshot
    notion_result = None
    if do_notion:
        try:
            from lib.notion_mirror import mirror_to_notion
            notion_result = mirror_to_notion(source, snapshot)
            written_notion = notion_result.get("inserted", 0) + notion_result.get("updated", 0)
        except Exception as e:
            errors.append(f"notion: {e}"[:120])

    return {
        "status": "ok",
        "written_vault": written_vault,
        "written_notion": written_notion,
        "notion_detail":  notion_result,
        "errors":         errors[:5],
    }
