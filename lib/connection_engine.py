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
        dedup_key = (url or "").strip().lower() or title.strip().lower()
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


def connect(text: str, limit: int = 18) -> dict[str, Any]:
    """The button. Give it what you're writing; get ranked connections from
    your archives + the shared mind, with provenance and matched terms."""
    text = (text or "").strip()
    if len(text) < 3:
        return {"status": "error", "error": "give me at least a few words"}
    terms = _salient_terms(text)
    if not terms:
        return {"status": "ok", "terms": [], "connections": [],
                "note": "no salient terms found in input"}
    archive = _archive_hits(terms, limit=limit)
    memory = _memory_hits(terms, limit=8)
    # Interleave: archives are the star; memories give agent/work context.
    connections = sorted(archive + memory,
                         key=lambda h: h["score"], reverse=True)
    return {
        "status": "ok",
        "terms": terms[:12],
        "count": len(connections),
        "connections": connections[:limit + 8],
    }
