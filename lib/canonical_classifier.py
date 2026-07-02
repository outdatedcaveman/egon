"""Canonical classifier — Egon reads every bit of AI-generated work and files it.

Bruno's design: the mind is the ONE consolidated place holding the full chats +
data + harnesses from every AI. Projects should NOT come from messy app folder
names (that yields junk like `file_a`, `test_cooldown_routing`). Instead Egon
looks over each item's CONTENT and classifies it into a canonical project, tags
it, and records the decision — and that canonical structure is what grounds chat
and what every AI reads.

Hybrid classifier (Bruno's choice, 2026-07-01):
  1. Embed the item text and the canonical project profiles; cosine-rank.
  2. If the top match is clearly confident, assign it locally (no tokens).
  3. If it's ambiguous (or matches nothing well), a cloud model decides among the
     shortlist — or flags it `unfiled` / proposes a new project.

Decisions are stored ADDITIVELY in a new `canonical_assignments` table (the DB
stays the source of truth; nothing is deleted). A separate exporter materializes
the browsable canonical tree. Bruno 2026-07-01.
"""
from __future__ import annotations

import json
import sqlite3
import time
from functools import lru_cache

import numpy as np

from lib.mind_context_broker import DB_PATH

# Seed taxonomy of Bruno's REAL projects. Egon refines membership by classifying
# content into these (and may propose new ones via the LLM step). One-line
# profiles are embedded as the matching target.
CANONICAL_DEFS: dict[str, str] = {
    "egon": "Personal knowledge-management system and AI hub. PySide6 desktop app plus "
            "always-on Python services: the unified mind, data adapters, orchestrator, "
            "Connect search, mind service, embeddings, hydration.",
    "mouseion": "Reference/PDF library enrichment system. Ingests a document collection, "
                "fetches metadata from providers (crossref, openalex) under a shared "
                "network budget, dedups and completes entries.",
    "panop": "Knowledge capture: syncs browser bookmarks, Chrome tabs, and Zotero; "
             "captures pages; mirrors with Routster and the Inbox.",
    "routster": "Navigation and routing KMS. Destinations and endpoints; mirrors Panop.",
    "synesism": "Bruno's philosophical monist theory. Academic tomes, Lean/Coq "
                "formalization, media kit and divulgation.",
    "infohub": "Information hub / aggregation project.",
    "careerops": "Career operations and job-search automation; resumes, applications.",
    "claude-meta": "Claude Code self-improvement and meta-harness. Egon blueprint, agent "
                   "enforcement, cross-agent coordination.",
    "citizenship": "Portuguese citizenship DIY process (Lei 37/81) for Rogelia and Bruno. "
                   "Certidoes, dossier assembly, legal paperwork.",
    "ancestry": "Genealogy and ancestry research; family tree, records.",
    "tvtime": "TV Time to Trakt migration; watch-history harvest and sync.",
    "asympt": "Asympt project.",
    "flood": "Flood pitch/product project; pitch decks and review.",
    "double": "Double production project.",
}

# thresholds on cosine similarity (normalized embeddings)
_CONFIDENT = 0.55   # >= this: assign locally, no LLM
_FLOOR = 0.28       # < this to every project: likely unfiled / new
_SHORTLIST = 4


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(DB_PATH), timeout=15)
    c.row_factory = sqlite3.Row
    return c


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS canonical_assignments (
               item_type TEXT NOT NULL,          -- 'session' | 'memory' | 'document'
               item_id   TEXT NOT NULL,
               canonical_project TEXT,            -- slug, or 'unfiled'
               confidence REAL,
               method TEXT,                       -- 'embedding' | 'llm' | 'floor'
               tags TEXT,                         -- json list
               rationale TEXT,
               ts INTEGER,
               PRIMARY KEY (item_type, item_id)
           )""")
    conn.commit()


def _norm(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v, axis=-1, keepdims=True)
    return v / np.clip(n, 1e-9, None)


@lru_cache(maxsize=1)
def _profiles() -> tuple[list[str], np.ndarray]:
    """(slugs, normalized profile vectors). Cached per process."""
    from lib.semantic_index import _embed
    slugs = list(CANONICAL_DEFS.keys())
    texts = [f"{s}: {CANONICAL_DEFS[s]}" for s in slugs]
    vecs = _embed(texts, is_query=False)
    if vecs is None:
        raise RuntimeError("embedder unavailable")
    return slugs, _norm(np.asarray(vecs, dtype=np.float32))


def _rank(text: str) -> list[tuple[str, float]]:
    from lib.semantic_index import _embed
    slugs, pvecs = _profiles()
    q = _embed([text[:4000]], is_query=True)
    if q is None:
        return []
    qv = _norm(np.asarray(q, dtype=np.float32))[0]
    sims = pvecs @ qv
    order = np.argsort(-sims)
    return [(slugs[i], float(sims[i])) for i in order]


def _llm_decide(text: str, shortlist: list[str]) -> tuple[str, str]:
    """Ask a cheap cloud model to pick the canonical project (or 'unfiled', or
    propose 'new:<slug>'). Returns (decision, rationale)."""
    try:
        from lib import egon_chat
        opts = ", ".join(shortlist) + ", unfiled"
        prompt = (
            "Classify this AI work session into ONE of Bruno's canonical projects.\n"
            f"Options: {opts}\n"
            "If it clearly belongs to a distinct project not listed, answer "
            "new:<short-slug>. Reply as strict JSON: "
            '{\"project\":\"<slug|unfiled|new:slug>\",\"why\":\"<8 words>\"}.\n\n'
            f"SESSION:\n{text[:2500]}"
        )
        # cheap + reliable model for high-volume classification
        out = egon_chat.chat([{"role": "user", "content": prompt}],
                             provider="claude", model="claude-haiku-4-5-20251001",
                             inject_context=False, temperature=0.0, max_tokens=120)
        s = out.strip()
        i, j = s.find("{"), s.rfind("}")
        obj = json.loads(s[i:j + 1]) if i >= 0 else {}
        return str(obj.get("project") or "unfiled"), str(obj.get("why") or "")
    except Exception as e:
        return "unfiled", f"llm-error:{str(e)[:40]}"


def classify_text(text: str, use_llm: bool = True) -> dict:
    """Classify one item's text → {canonical_project, confidence, method, tags,
    rationale}. Local embedding decides the confident majority; the LLM only
    arbitrates the ambiguous middle."""
    ranked = _rank(text or "")
    if not ranked:
        return {"canonical_project": "unfiled", "confidence": 0.0,
                "method": "floor", "tags": [], "rationale": "no-embedding"}
    top_slug, top_score = ranked[0]
    if top_score >= _CONFIDENT:
        return {"canonical_project": top_slug, "confidence": round(top_score, 3),
                "method": "embedding", "tags": [top_slug], "rationale": "high-sim"}
    if top_score < _FLOOR and not use_llm:
        return {"canonical_project": "unfiled", "confidence": round(top_score, 3),
                "method": "floor", "tags": [], "rationale": "below-floor"}
    if not use_llm:
        return {"canonical_project": top_slug, "confidence": round(top_score, 3),
                "method": "embedding", "tags": [top_slug], "rationale": "best-of-weak"}
    # The taxonomy is small (~13) — let the model see EVERY canonical project
    # (embedding-ranked, best first), not just the top-k, so it never proposes a
    # 'new:' project that already exists (e.g. genealogy → ancestry).
    all_ranked = [s for s, _ in ranked] or list(CANONICAL_DEFS.keys())
    decision, why = _llm_decide(text, all_ranked)
    tags = [] if decision in ("unfiled",) else [decision.replace("new:", "")]
    return {"canonical_project": decision, "confidence": round(top_score, 3),
            "method": "llm", "tags": tags, "rationale": why}


def _session_text(conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    """Best classification text for a session: its summary (Goal+Actions), else a
    few activity payloads."""
    if row["summary"]:
        return row["summary"]
    parts = []
    for a in conn.execute(
        "SELECT payload_json FROM activity WHERE session_id=? ORDER BY id LIMIT 12",
            (row["id"],)):
        try:
            p = json.loads(a["payload_json"])
            parts.append(json.dumps(p)[:400])
        except Exception:
            continue
    return "\n".join(parts)


def classify_sessions(limit: int | None = None, use_llm: bool = True,
                      only_unclassified: bool = True) -> dict:
    """Classify sessions into canonical projects; store in canonical_assignments.
    Returns a summary {counted, by_project, methods}."""
    conn = _conn()
    ensure_schema(conn)
    done = set()
    if only_unclassified:
        done = {r["item_id"] for r in conn.execute(
            "SELECT item_id FROM canonical_assignments WHERE item_type='session'")}
        # Second chance for 'unfiled': many were classified before their session
        # had any summary (e.g. Codex threads gained summaries only when the
        # exhaustive parser backfilled state_5 metadata). If the session NOW has
        # real text, requeue it — the upsert makes re-classification safe.
        retry = {r["item_id"] for r in conn.execute(
            """SELECT ca.item_id FROM canonical_assignments ca
               JOIN sessions s ON s.id = CAST(ca.item_id AS INTEGER)
               WHERE ca.item_type='session' AND ca.canonical_project='unfiled'
                 AND s.summary IS NOT NULL AND s.summary != ''""")}
        done -= retry
    rows = conn.execute("SELECT id, external_id, summary FROM sessions "
                        "ORDER BY id DESC").fetchall()
    by_project: dict[str, int] = {}
    methods: dict[str, int] = {}
    n = 0
    for row in rows:
        if limit and n >= limit:
            break
        sid = str(row["id"])
        if only_unclassified and sid in done:
            continue
        text = _session_text(conn, row)
        if not text.strip():
            continue
        res = classify_text(text, use_llm=use_llm)
        conn.execute(
            """INSERT INTO canonical_assignments
               (item_type,item_id,canonical_project,confidence,method,tags,rationale,ts)
               VALUES ('session',?,?,?,?,?,?,?)
               ON CONFLICT(item_type,item_id) DO UPDATE SET
                 canonical_project=excluded.canonical_project, confidence=excluded.confidence,
                 method=excluded.method, tags=excluded.tags, rationale=excluded.rationale,
                 ts=excluded.ts""",
            (sid, res["canonical_project"], res["confidence"], res["method"],
             json.dumps(res["tags"]), res["rationale"], int(time.time())))
        by_project[res["canonical_project"]] = by_project.get(res["canonical_project"], 0) + 1
        methods[res["method"]] = methods.get(res["method"], 0) + 1
        n += 1
        if n % 20 == 0:
            conn.commit()
    conn.commit()
    conn.close()
    return {"counted": n, "by_project": dict(sorted(by_project.items(),
            key=lambda x: -x[1])), "methods": methods}


def classify_memories(limit: int | None = None, use_llm: bool = True,
                      only_unclassified: bool = True) -> dict:
    """Classify durable memories into canonical projects (item_type='memory').
    Memories are the second content stream after sessions: rollout summaries,
    decisions, Codex thread metadata, Antigravity conversations — all filed by
    CONTENT, same hybrid pipeline. Cheap shortcut: when a memory's tags already
    name exactly one canonical project, trust the tag (method='tag') — no
    embedding or LLM spend needed."""
    conn = _conn()
    ensure_schema(conn)
    done = set()
    if only_unclassified:
        done = {r["item_id"] for r in conn.execute(
            "SELECT item_id FROM canonical_assignments WHERE item_type='memory'")}
    known = set(CANONICAL_DEFS.keys())
    rows = conn.execute(
        "SELECT id, kind, content, tags FROM memory "
        "WHERE superseded_by_memory_id IS NULL ORDER BY id DESC").fetchall()
    by_project: dict[str, int] = {}
    methods: dict[str, int] = {}
    n = 0
    for row in rows:
        if limit and n >= limit:
            break
        mid = str(row["id"])
        if only_unclassified and mid in done:
            continue
        content = (row["content"] or "").strip()
        if len(content) < 30:
            continue
        tags = {t.strip().lower() for t in (row["tags"] or "").split(",") if t.strip()}
        tag_hits = tags & known
        if len(tag_hits) == 1:
            res = {"canonical_project": next(iter(tag_hits)), "confidence": 1.0,
                   "method": "tag", "tags": sorted(tag_hits), "rationale": "tagged"}
        else:
            res = classify_text(content, use_llm=use_llm)
        conn.execute(
            """INSERT INTO canonical_assignments
               (item_type,item_id,canonical_project,confidence,method,tags,rationale,ts)
               VALUES ('memory',?,?,?,?,?,?,?)
               ON CONFLICT(item_type,item_id) DO UPDATE SET
                 canonical_project=excluded.canonical_project, confidence=excluded.confidence,
                 method=excluded.method, tags=excluded.tags, rationale=excluded.rationale,
                 ts=excluded.ts""",
            (mid, res["canonical_project"], res["confidence"], res["method"],
             json.dumps(res["tags"]), res["rationale"], int(time.time())))
        by_project[res["canonical_project"]] = by_project.get(res["canonical_project"], 0) + 1
        methods[res["method"]] = methods.get(res["method"], 0) + 1
        n += 1
        if n % 50 == 0:
            conn.commit()
    conn.commit()
    conn.close()
    return {"counted": n, "by_project": dict(sorted(by_project.items(),
            key=lambda x: -x[1])), "methods": methods}


if __name__ == "__main__":
    import sys
    lim = int(sys.argv[1]) if len(sys.argv) > 1 else None
    print(json.dumps(classify_sessions(limit=lim), indent=2, ensure_ascii=False))
    print(json.dumps(classify_memories(limit=lim), indent=2, ensure_ascii=False))
