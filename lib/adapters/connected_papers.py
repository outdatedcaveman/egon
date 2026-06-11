"""Connected Papers adapter — visual citation neighborhoods.

Connected Papers offers a free graph API (rate-limited) that returns
the citation neighborhood around a seed paper: a small graph of
strongly related works, edges weighted by co-citation similarity.

For Mouseion: this is the "related work" surface used to expand a
reference set from one seed paper outward — the moment a paper enters
Bruno's library, we can pre-fetch the 10-30 most-related papers and
surface them in the References tab.

Endpoints (no auth, polite rate limits apply):
  GET https://api.connectedpapers.com/graph/v1/<paper_id>
  where <paper_id> is a Semantic Scholar paper ID or a DOI.

Docs: the API is undocumented but widely used by community tools; we
mirror the same shape the official client uses.
"""
from __future__ import annotations

from lib.lazy_httpx import httpx  # deferred ~2s import (2026-06-11 perf pass)

BASE = "https://api.connectedpapers.com"

META = {
    "id": "connected_papers",
    "label": "Connected Papers",
    "icon": "🕸️",
    "kind": "reference_enrichment",
    "needs_auth": False,
    "destructive_actions": [],
    "read_only_default": True,
}


def live_status(timeout: float = 5.0) -> dict:
    """Light probe — the official site responds with 200 even when the
    API is rate-limited, so getting any 2xx confirms reachability."""
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.get("https://www.connectedpapers.com")
        if r.status_code == 200:
            return {"status": "ok",
                    "note": "Graph API is rate-limited; treat each call as a budgeted op."}
        return {"status": "error", "error": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


def neighborhood(seed_id: str, kind: str = "auto") -> dict:
    """Fetch the citation neighborhood for a seed paper.

    `seed_id` can be a DOI (preferred), a Semantic Scholar paper ID
    (40-char hex), or an arXiv ID. `kind='auto'` lets the function
    detect, or pass `'doi'` / `'ss'` / `'arxiv'` explicitly.

    Returns:
        {
          "seed": {"id":..., "title":..., "year":..., "doi":...},
          "neighbors": [{"id":..., "title":..., "year":..., "similarity":...}, …],
        }

    Rate-limit hits return {"status":"rate_limited"}.
    """
    sid = seed_id.strip()
    # Convert DOI to the URL form the API expects.
    if kind == "auto":
        if sid.startswith("10."):
            kind = "doi"
        elif sid.startswith("arxiv:") or sid.startswith("arXiv:"):
            kind = "arxiv"
        else:
            kind = "ss"
    if kind == "doi":
        path = f"{BASE}/graph/v1/{sid}"
    else:
        path = f"{BASE}/graph/v1/{sid}"
    try:
        with httpx.Client(timeout=20) as c:
            r = c.get(path, headers={"Accept": "application/json"})
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}

    if r.status_code == 429:
        return {"status": "rate_limited",
                "retry_after": r.headers.get("Retry-After")}
    if r.status_code == 404:
        return {"status": "not_found", "seed_id": sid}
    if r.status_code != 200:
        return {"status": "error", "error": f"HTTP {r.status_code}",
                "body": r.text[:200]}
    try:
        j = r.json()
    except Exception:
        return {"status": "error", "error": "non-JSON body"}

    # Normalize the response — the API shape varies by year, so we
    # extract the bits we care about defensively.
    papers = j.get("nodes") or j.get("papers") or {}
    if isinstance(papers, dict):
        nodes = list(papers.values())
    else:
        nodes = papers
    seed_node = None
    for n in nodes:
        if n.get("isSelected") or n.get("is_seed") or n.get("id") == sid:
            seed_node = n
            break

    def shape(n: dict) -> dict:
        return {
            "id": n.get("id") or n.get("paperId"),
            "title": n.get("title") or "",
            "year": n.get("year"),
            "doi": (n.get("doi") or "").replace("https://doi.org/", ""),
            "authors": n.get("authors") or [],
            "citations": n.get("citationCount") or n.get("citations"),
            "similarity": n.get("similarity"),
        }

    return {
        "status": "ok",
        "seed": shape(seed_node) if seed_node else {"id": sid},
        "neighbors": [shape(n) for n in nodes if n is not seed_node][:50],
    }
