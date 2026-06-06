"""Inciteful adapter — citation-graph exploration over OpenAlex.

Inciteful is a small, FOSS-spirited service that runs custom graph
queries over OpenAlex's citation graph. Its public API is open, free,
and stable. Perfect Mouseion citation-side companion to OpenAlex.

Endpoints (no auth):
  GET https://api.inciteful.xyz/graph?ids=<doi or openalex id>,...
  POST https://api.inciteful.xyz/query (graph query language)

Docs: https://inciteful.xyz/api
"""
from __future__ import annotations

import httpx

BASE = "https://api.inciteful.xyz"

META = {
    "id": "inciteful",
    "label": "Inciteful",
    "icon": "🌐",
    "kind": "reference_enrichment",
    "needs_auth": False,
    "destructive_actions": [],
    "read_only_default": True,
}


def live_status(timeout: float = 5.0) -> dict:
    try:
        # Probe the actual API host (not the landing page, which 301s to
        # the marketing site). follow_redirects=True future-proofs us if
        # they ever rearrange the docs.
        with httpx.Client(timeout=timeout, follow_redirects=True) as c:
            r = c.get(f"{BASE}/")
        if 200 <= r.status_code < 500:
            return {"status": "ok"}
        return {"status": "error", "error": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


def graph(ids: list[str], extension: int = 1) -> dict:
    """Build a citation graph around a list of seed DOIs / IDs.

    `extension` is how many hops out from the seeds (1 = direct cites
    and references, 2 = neighbors of neighbors). Inciteful caps at 2.
    """
    if not ids:
        return {"status": "error", "error": "no ids"}
    try:
        with httpx.Client(timeout=30) as c:
            r = c.get(f"{BASE}/graph",
                      params={"ids": ",".join(ids[:20]),
                              "extension": min(2, max(0, extension))},
                      headers={"Accept": "application/json"})
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}
    if r.status_code != 200:
        return {"status": "error", "error": f"HTTP {r.status_code}",
                "body": r.text[:200]}
    try:
        return {"status": "ok", **(r.json() or {})}
    except Exception:
        return {"status": "error", "error": "non-JSON body"}


def similar(ids: list[str], n: int = 20) -> list[dict]:
    """Surface the top-N most-similar papers to a seed set, ranked by
    co-citation weight."""
    g = graph(ids, extension=1)
    if g.get("status") != "ok":
        return []
    papers = g.get("papers") or g.get("nodes") or []
    if isinstance(papers, dict):
        papers = list(papers.values())
    # Inciteful tags seeds with `seed: True`; pull others, sort by score
    out = []
    for p in papers:
        if p.get("seed") or p.get("is_seed"):
            continue
        out.append({
            "id": p.get("id") or p.get("openalex_id"),
            "doi": p.get("doi"),
            "title": p.get("title", ""),
            "year": p.get("year"),
            "authors": p.get("authors") or [],
            "score": p.get("similarity") or p.get("score") or p.get("co_citation"),
        })
    out.sort(key=lambda x: (x.get("score") or 0), reverse=True)
    return out[:n]
