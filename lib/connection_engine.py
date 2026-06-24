"""Connection Engine — "what in my archives connects to what I'm writing?"

Bruno 2026-06-06: the heart of the personalized-AI ambition. You're writing
something (laptop or phone), you press a button, and Egon surfaces things from
YOUR archives that connect to it: saved articles (Instapaper), papers (Zotero /
Paperpile), books (Kindle), films (Letterboxd), videos (YouTube), bookmarks,
Notion notes, and the unified mind's durable memories — ranked, deduped, with
the WHY (which of your words matched) attached.

Design choices:
  • 100% local + 0 LLM tokens for retrieval: the engine extracts salient terms
    from the input text and runs lexical ranking over every snapshot source
    (lib.cross_search) plus mind.db durable memory. Instant and free; semantic
    embeddings can be layered on later without changing the API.
  • Long input friendly: you can paste a whole paragraph/page. The engine
    weights rarer, longer terms so function words don't dominate.
  • Provenance always attached: every hit carries source, title, url, why.

Exposed at POST /api/v1/mind/connect (external/panop_server/mind_endpoints.py),
served by BOTH the standalone mind service and Egon's in-process Panop — so it
works even when the Egon UI is closed, and the Chrome extension / phone can
reach it over HTTP.

This implements the missing piece of the Intelligence Layer roadmap
(mind.db memory 321): the roadmap built agent introspection + token compaction;
this adds the user-facing "connect my writing to my archives" surface.
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "state" / "mind.db"

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9'’-]{2,}")

# Function words in EN + PT (Bruno writes in both). Kept deliberately broad so
# pasted prose doesn't drown the signal terms.
_STOP = frozenset("""
a an and are as at be but by for from has have had in is it its of on or such
that the their then there these they this to was were will with would not no
nor so if than too very can could should do does did done just also about into
over under again further once here when where why how all any both each few
more most other some only own same s t don now what which who whom your you we
our us i he she his her him they them
o a os as um uma uns umas e ou mas de do da dos das em no na nos nas por para
com sem sob sobre que quem qual quais quando onde como porque se não sim mais
menos muito pouco este esta isto esse essa isso aquele aquela aquilo seu sua
seus suas meu minha meus minhas nosso nossa eu tu ele ela nós vós eles elas
foi era ser estar tem têm há já ainda até desde entre
""".split())


def _enrich(connections: list[dict]) -> list[dict]:
    """Attach native-app deep links (`app_url`/`app_label`) so the phone can
    open each hit in Notion/Drive/YouTube/… instead of the browser. Best-effort:
    if the helper is unavailable for any reason, the web `url` still works."""
    try:
        from lib.deep_links import enrich
        return enrich(connections)
    except Exception:
        return connections


def _salient_terms(text: str, max_terms: int = 24) -> list[str]:
    """Pull the terms that carry the meaning of the input. Frequency-weighted,
    stopwords dropped, longer terms favoured (they're rarer and more topical)."""
    words = [w.lower() for w in _WORD_RE.findall(text or "")]
    counts = Counter(w for w in words if w not in _STOP)
    if not counts:
        return []
    ranked = sorted(counts.items(),
                    key=lambda kv: (kv[1] * (1.0 + min(len(kv[0]), 12) / 8.0)),
                    reverse=True)
    return [w for w, _ in ranked[:max_terms]]


def _matched(blob: str, terms: set[str]) -> list[str]:
    """Which terms appear in blob as whole words (no 'lei' inside 'leitura')."""
    toks = set(w.lower() for w in _WORD_RE.findall(blob or ""))
    return [t for t in terms if t in toks]


def _memory_hits(terms: list[str], limit: int = 8) -> list[dict[str, Any]]:
    """Rank mind.db durable memories against the salient terms."""
    if not DB_PATH.exists() or not terms:
        return []
    try:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=4)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT id, kind, content, tags FROM memory "
            "ORDER BY updated_at DESC LIMIT 1200").fetchall()
        con.close()
    except Exception:
        return []
    scored = []
    tset = set(terms)
    for r in rows:
        blob = (r["content"] or "") + " " + (r["tags"] or "")
        matched = _matched(blob, tset)
        if not matched:
            continue
        score = sum(2.0 if t in (r["tags"] or "").lower() else 1.0
                    for t in matched)
        scored.append({
            "source": "mind-memory",
            "title": f"memory {r['id']} [{r['kind']}]",
            "snippet": (r["content"] or "")[:220],
            "url": None,
            "score": round(score, 2),
            "why": matched[:6],
        })
    scored.sort(key=lambda h: h["score"], reverse=True)
    return scored[:limit]


def _archive_hits(terms: list[str], limit: int = 18) -> list[dict[str, Any]]:
    """Rank every snapshot archive (Instapaper, Zotero, Paperpile, Kindle,
    Letterboxd, YouTube, bookmarks, Notion, …) against the salient terms via
    lib.cross_search, then keep per-source diversity."""
    if not terms:
        return []
    from lib import cross_search
    query = " ".join(terms[:12])
    try:
        raw = cross_search.search(query)
    except Exception:
        return []
    hits, per_source, seen = [], Counter(), set()
    tset = set(terms)
    for r in raw:
        item, source = r.get("item") or {}, r.get("source") or "?"
        if per_source[source] >= 5:        # diversity: max 5 per archive
            continue
        title = cross_search.pretty_title(item)
        url = cross_search.pretty_url(item)
        dedup_key = (url or "").strip().lower() or (title or "").strip().lower()
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        matched = _matched(json.dumps(item, ensure_ascii=False), tset)
        per_source[source] += 1
        hits.append({
            "source": source,
            "title": title,
            "snippet": cross_search.pretty_subline(item, source),
            "url": url,
            "score": float(r.get("score") or 0),
            "why": matched[:6],
        })
        if len(hits) >= limit:
            break
    return hits


def _semantic_connect(text: str, terms: list[str], limit: int) -> list[dict] | None:
    """Embedding-based ranking over the cached index. Returns None if the index
    isn't built yet (caller falls back to lexical). Finds conceptually related
    material even with zero word overlap."""
    try:
        from lib import semantic_index as si
    except Exception:
        return None
    # NOTE: do NOT trigger an index (re)build from the query path. build()
    # rewrites ~1.7GB to disk (vectors+meta+turbo) and calls _invalidate() even
    # when nothing changed — that nukes the warm meta+turbo cache mid-search and
    # forced EVERY query to reload ~500MB (20-40s). egon_core already rebuilds
    # the index every 6h; queries just read the warm cache (turbovec ~0.4s).
    if not si.is_ready():
        return None
    hits = si.search(text, top_k=limit + 14)
    if not hits:
        return []
    tset = set(terms)
    per_source = {}
    out = []
    for h in hits:
        src = h.get("source") or "?"
        # light per-archive diversity so one big source can't dominate
        if src != "mind-memory":
            per_source[src] = per_source.get(src, 0) + 1
            if per_source[src] > 6:
                continue
        why = _matched((h.get("title", "") + " " + (h.get("snippet") or "")), tset)
        out.append({
            "source": src,
            "title": h.get("title", ""),
            "snippet": h.get("snippet") or "",
            "url": h.get("url"),
            "score": h.get("score", 0.0),
            "why": why[:6],
        })
    return out


def connect(text: str, limit: int = 18, semantic_search: bool = True,
            lexical_search: bool = True) -> dict[str, Any]:
    """The button. Give it what you're writing; get ranked connections from
    your archives + the shared mind. Hybrid (RRF fusion) ranking when semantic
    index is ready, lexical fallback otherwise; matched terms attached as 'why'.

    semantic_search=False skips the (currently brute-force, ~50s) embedding pass
    and returns the fast lexical-only result — used by the phone so it answers in
    <1s instead of timing out. Flip back to True once the turbovec query path
    lands (sub-second semantic). Bruno 2026-06-23."""
    text = (text or "").strip()
    if len(text) < 3:
        return {"status": "error", "error": "give me at least a few words"}
    terms = _salient_terms(text)

    # 1. Fetch semantic connections if ready (skipped for the fast lexical path)
    semantic = _semantic_connect(text, terms, limit=limit + 30) if semantic_search else None
    
    # 2. Fetch lexical connections (archives + memory). The archive scan
    # (cross_search) tokenizes the WHOLE corpus (~774k items) per query → ~30s,
    # which dominates the latency. The phone passes lexical_search=False to skip
    # it: the semantic turbovec index already spans every source (Drive,
    # Letterboxd, YouTube, Zotero, …) in ~1s, so the phone stays fast and still
    # draws from the entire database. Memory hits (sqlite) stay — they're cheap.
    lexical_archives = _archive_hits(terms, limit=limit + 30) if lexical_search else []
    lexical_memories = _memory_hits(terms, limit=20)
    lexical = sorted(lexical_archives + lexical_memories, key=lambda h: h["score"], reverse=True)

    if semantic is not None:
        # Hybrid Fusion using Reciprocal Rank Fusion (RRF)
        k = 60
        unique_items = {}
        
        # Rank mapping for semantic (already sorted)
        for rank, h in enumerate(semantic, 1):
            url = h.get("url")
            title = h.get("title")
            dkey = (url or "").strip().lower() or (title or "").strip().lower()
            if dkey not in unique_items:
                unique_items[dkey] = {
                    "source": h.get("source"),
                    "title": title,
                    "snippet": h.get("snippet"),
                    "url": url,
                    "why": h.get("why", []),
                    "semantic_rank": rank,
                    "semantic_score": h.get("score", 0.0),
                    "lexical_rank": None,
                    "lexical_score": 0.0,
                }

        # Rank mapping for lexical (already sorted)
        for rank, h in enumerate(lexical, 1):
            url = h.get("url")
            title = h.get("title")
            dkey = (url or "").strip().lower() or (title or "").strip().lower()
            if dkey in unique_items:
                item = unique_items[dkey]
                item["lexical_rank"] = rank
                item["lexical_score"] = h.get("score", 0.0)
                # Merge and deduplicate the matched terms
                item["why"] = list(dict.fromkeys(item["why"] + h.get("why", [])))
            else:
                unique_items[dkey] = {
                    "source": h.get("source"),
                    "title": title,
                    "snippet": h.get("snippet"),
                    "url": url,
                    "why": h.get("why", []),
                    "semantic_rank": None,
                    "semantic_score": 0.0,
                    "lexical_rank": rank,
                    "lexical_score": h.get("score", 0.0),
                }

        # Compute RRF score
        for item in unique_items.values():
            r_sem = item["semantic_rank"]
            r_lex = item["lexical_rank"]
            score_sem = 1.0 / (k + r_sem) if r_sem is not None else 0.0
            score_lex = 1.0 / (k + r_lex) if r_lex is not None else 0.0
            item["score"] = round(score_sem + score_lex, 5)

        # Sort combined list by RRF score descending
        combined = sorted(unique_items.values(), key=lambda x: x["score"], reverse=True)
        
        # Apply soft source diversity filter (max 6 items per archive)
        diversified = []
        per_source = {}
        for item in combined:
            src = item.get("source") or "?"
            if src != "mind-memory":
                per_source[src] = per_source.get(src, 0) + 1
                if per_source[src] > 6:
                    continue
            clean_item = {
                "source": src,
                "title": item["title"],
                "snippet": item["snippet"],
                "url": item["url"],
                "score": item["score"],
                "why": item["why"][:6],
            }
            diversified.append(clean_item)

        return {
            "status": "ok",
            "mode": "hybrid",
            "terms": terms[:12],
            "count": len(diversified),
            "connections": _enrich(diversified[:limit + 8]),
        }

    # Lexical fallback (index still building, or no model).
    if not terms:
        return {"status": "ok", "mode": "lexical", "terms": [], "connections": [],
                "note": "no salient terms found in input"}
                
    connections = sorted(lexical, key=lambda h: h["score"], reverse=True)
    return {
        "status": "ok",
        "mode": "lexical",
        "terms": terms[:12],
        "count": len(connections),
        "connections": _enrich(connections[:limit + 8]),
    }
