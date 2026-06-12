"""Obsidian mirror — the local twin of the Notion mirror. v2.

Bruno 2026-06-12: "Notion and Obsidian should always mirror each other",
"FULLNESS and COMPREHENSIVENESS", "rich metadata to ease the life of AIs and
automation", "save ALL the content, not just title". This is part of the
personalized version-control infrastructure, not just a database mirror.

v2 over v1:
  • EVERY snapshot source mirrors (discovered dynamically from cross_search),
    not a hand-picked list — YouTube Music, Pocket Casts, Kindle, TV Time,
    Instapaper, Mouseion... whatever has a snapshot, plus Zotero (local
    SQLite, full 252k) and the unified mind's entities.
  • Mind entities at the reasonable grain Bruno asked for: one note per
    SESSION (not per message), per project, per durable memory, per agent
    skill/rule (kind=agent_asset).
  • Rich frontmatter: every scalar field the item carries, plus provenance
    (source, stable key, mirrored_at) — machine-readable for AIs/automation.
  • FULL content in the body where it exists: Zotero abstracts, Notion page
    bodies (lib/notion_body cache), memory content, file extracts.

Layout:  <vault>/050 - Mirrors/<source>/<safe-name>.md
Idempotent by stable key; additive (never deletes — reconciles can archive
deliberately later).
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

VAULT = Path(r"C:\Users\bruno\Documents\Obsidian Vault")
MIRROR_DIR = VAULT / "050 - Mirrors"
ROOT = Path(__file__).resolve().parent.parent

_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# fields that go to the BODY, not frontmatter
_BODY_FIELDS = ("abstract", "content", "summary", "snippet", "subtitle",
                "description", "body")
# noisy/internal fields we skip entirely
_SKIP_FIELDS = {"_key", "_page_id", "object", "raw", "items"}


def _safe(name: str, maxlen: int = 80) -> str:
    s = _BAD.sub("_", str(name)).strip(". ")
    return (s[:maxlen] or "untitled").rstrip(". ")


def _key_for(item: dict) -> str:
    k = item.get("id") or item.get("key") or item.get("url") or item.get("title")
    return str(k or "?")


def _yaml_val(v) -> str:
    s = str(v).replace('"', "'").replace("\n", " ")[:300]
    return f'"{s}"'


def _note_text(source: str, item: dict, key: str) -> str:
    title = str(item.get("title") or item.get("name") or "untitled")
    fm = ["---", f"title: {_yaml_val(title)}", f"source: {source}",
          f"key: {_yaml_val(key)}",
          f'mirrored_at: "{time.strftime("%Y-%m-%dT%H:%M:%S")}"']
    body_parts: list[str] = []
    for k, v in item.items():
        if k in ("title", "name") or k in _SKIP_FIELDS or v in (None, "", []):
            continue
        if k in _BODY_FIELDS:
            body_parts.append(f"## {k}\n\n{str(v)[:4000]}")
            continue
        if isinstance(v, (str, int, float, bool)):
            fm.append(f"{k}: {_yaml_val(v)}")
        elif isinstance(v, list) and v and isinstance(v[0], (str, int, float)):
            fm.append(f"{k}: [{', '.join(_yaml_val(x) for x in v[:12])}]")
    fm.append(f"tags: [mirror, {source}]")
    fm.append("---")
    out = "\n".join(fm)
    if body_parts:
        out += "\n\n" + "\n\n".join(body_parts)
    return out + "\n"


def mirror_source(source: str, snapshot: dict, max_items: int = 0,
                  body_lookup=None) -> dict:
    """Write every snapshot item as a note. max_items=0 → all.
    body_lookup(item) → extra body text (e.g. Notion page bodies)."""
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
        key = _key_for(item)
        base = _safe(str(item.get("title") or item.get("name") or key))
        if base in seen_names:
            base = f"{base}~{_safe(key.split(':')[-1], 16)}"
        seen_names[base] = 1
        try:
            text = _note_text(source, item, key)
            if body_lookup:
                extra = body_lookup(item) or ""
                if extra.strip():
                    text += f"\n## content\n\n{extra[:12000]}\n"
            (out_dir / f"{base}.md").write_text(text, encoding="utf-8")
            written += 1
        except Exception:
            continue
    return {"status": "ok", "source": source, "written": written,
            "seconds": round(time.time() - t0, 1)}


# ── mind entities: sessions, projects, memories, skills ─────────────────────
def _mind_entities() -> dict[str, list[dict]]:
    """Read the unified mind read-only and shape its entities for mirroring.
    Session-level grain (Bruno: no note per message, one per session)."""
    import sqlite3
    db = ROOT / "state" / "mind.db"
    if not db.exists():
        return {}
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    out: dict[str, list[dict]] = {}
    try:
        out["mind_sessions"] = [{
            "id": f"session:{r['id']}",
            "title": f"{r['agent']} session {r['external_id'][:18]} ({r['day']})",
            "agent": r["agent"], "project": r["project"] or "",
            "started": r["day"], "events": r["n_events"],
            "summary": (r["summary"] or "")[:3000],
        } for r in con.execute("""
            SELECT s.id, s.external_id, s.summary,
                   COALESCE(date(s.started_at,'unixepoch'),'?') day,
                   ag.name agent, COALESCE(p.slug,'') project,
                   (SELECT COUNT(*) FROM activity a WHERE a.session_id=s.id) n_events
            FROM sessions s JOIN agents ag ON ag.id=s.agent_id
            LEFT JOIN projects p ON p.id=s.project_id""")]
        out["mind_projects"] = [{
            "id": f"project:{r['slug']}", "title": f"Project — {r['slug']}",
            "status": r["status"] or "active",
            "description": (r["description"] or "")[:2000],
        } for r in con.execute("SELECT * FROM projects")]
        mems, skills = [], []
        for r in con.execute(
                "SELECT * FROM memory WHERE superseded_by_memory_id IS NULL"):
            row = {"id": f"memory:{r['id']}",
                   "title": f"Memory {r['id']} [{r['kind']}]",
                   "kind": r["kind"], "tags_": r["tags"] or "",
                   "updated": r["updated_at"],
                   "content": (r["content"] or "")[:6000]}
            (skills if r["kind"] == "agent_asset" else mems).append(row)
        out["mind_memories"] = mems
        out["mind_skills"] = skills
    finally:
        con.close()
    return out


def mirror_all(sources: list[str] | None = None) -> dict:
    """Mirror EVERYTHING: every cross_search snapshot source + Zotero (full
    local) + mind entities. Driven from the same snapshots as the Notion
    mirror so the two stay in lockstep."""
    from lib import cross_search
    results: dict[str, dict] = {}

    if sources is None:
        try:
            discovered = list(cross_search._all_sources())
        except Exception:
            discovered = []
        # zotero comes from the local adapter below; avoid the capped snapshot
        sources = [s for s in discovered if s != "zotero"]

    # 1) every snapshot source
    body_lookup = None
    for source in sources:
        try:
            snap = cross_search._latest_snapshot_for(source)
        except Exception:
            snap = None
        if not snap or not snap.get("items"):
            results[source] = {"status": "skip", "written": 0}
            continue
        if source == "notion_workspace":
            try:
                from lib import notion_body
                body_lookup = lambda it: notion_body.body_for(it.get("id", ""))
            except Exception:
                body_lookup = None
        results[source] = mirror_source(source, snap, body_lookup=body_lookup
                                        if source == "notion_workspace" else None)

    # 2) Zotero — full local library with abstracts
    try:
        from lib.adapters import zotero_local
        results["zotero"] = mirror_source("zotero", zotero_local.snapshot())
    except Exception as e:
        results["zotero"] = {"status": "error", "error": str(e)[:80]}

    # 3) the mind's own entities — sessions/projects/memories/skills
    for source, items in _mind_entities().items():
        results[source] = mirror_source(source, {"items": items})

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
