"""OpenAlex adapter — the 250M+ open academic graph.

Free, no auth required. The "polite pool" (faster + more stable) wants an
email address as a query param; we read it from `egon-config.json`'s
`openalex.email` slot but the adapter works without it too.

Direct serves the three pillars Bruno named (2026-05-28):
  • Comprehensiveness — every Crossref-registered paper + 250M+ open works.
  • Data analysis — group/aggregation queries on the works endpoint.
  • Parsing/filtering — first-class filter syntax over 50+ fields.

Docs: https://docs.openalex.org/
API base: https://api.openalex.org/
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent.parent
BASE = "https://api.openalex.org"

META = {
    "id": "openalex",
    "label": "OpenAlex",
    "icon": "📚",
    "kind": "reference_enrichment",
    "needs_auth": False,
    "destructive_actions": [],
    "read_only_default": True,
}


def _polite_params() -> dict:
    """Look up the polite-pool email from egon-config.json. Optional."""
    try:
        with (ROOT / "egon-config.json").open(encoding="utf-8") as f:
            email = (json.load(f).get("openalex") or {}).get("email")
        if email:
            return {"mailto": email}
    except Exception:
        pass
    return {}


def live_status(timeout: float = 5.0) -> dict:
    """Hit the root endpoint to confirm OpenAlex is reachable."""
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.get(f"{BASE}/works", params={**_polite_params(), "per-page": 1})
        if r.status_code == 200:
            d = r.json() or {}
            return {"status": "ok",
                    "works_count": d.get("meta", {}).get("count"),
                    "polite": bool(_polite_params())}
        return {"status": "error", "error": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


def search(query: str, limit: int = 25, filter_str: str | None = None) -> list[dict]:
    """Search works. `filter_str` is OpenAlex's filter syntax, e.g.
    `is_oa:true,publication_year:2020-2024`."""
    params = {**_polite_params(), "search": query,
              "per-page": min(limit, 200)}
    if filter_str:
        params["filter"] = filter_str
    try:
        with httpx.Client(timeout=20) as c:
            r = c.get(f"{BASE}/works", params=params)
        if r.status_code != 200:
            return []
        out = []
        for w in (r.json() or {}).get("results", []):
            out.append({
                "id": (w.get("id") or "").replace("https://openalex.org/", ""),
                "doi": (w.get("doi") or "").replace("https://doi.org/", ""),
                "title": (w.get("display_name") or w.get("title") or ""),
                "year": w.get("publication_year"),
                "venue": ((w.get("primary_location") or {}).get("source") or {}).get("display_name"),
                "authors": [a.get("author", {}).get("display_name", "")
                            for a in (w.get("authorships") or [])[:8]],
                "cited_by_count": w.get("cited_by_count"),
                "is_oa": w.get("open_access", {}).get("is_oa"),
                "oa_url": w.get("open_access", {}).get("oa_url"),
                "abstract": _reconstruct_abstract(w.get("abstract_inverted_index")),
                "concepts": [c.get("display_name", "")
                             for c in (w.get("concepts") or [])[:6]],
            })
        return out
    except Exception:
        return []


def by_doi(doi: str) -> dict | None:
    """Look up a single work by DOI. Returns the same shape as `search`."""
    doi = doi.strip().replace("https://doi.org/", "")
    try:
        with httpx.Client(timeout=10) as c:
            r = c.get(f"{BASE}/works/doi:{doi}", params=_polite_params())
        if r.status_code != 200:
            return None
        w = r.json()
        return {
            "id": (w.get("id") or "").replace("https://openalex.org/", ""),
            "doi": doi,
            "title": (w.get("display_name") or w.get("title") or ""),
            "year": w.get("publication_year"),
            "venue": ((w.get("primary_location") or {}).get("source") or {}).get("display_name"),
            "authors": [a.get("author", {}).get("display_name", "")
                        for a in (w.get("authorships") or [])[:20]],
            "cited_by_count": w.get("cited_by_count"),
            "is_oa": w.get("open_access", {}).get("is_oa"),
            "oa_url": w.get("open_access", {}).get("oa_url"),
            "abstract": _reconstruct_abstract(w.get("abstract_inverted_index")),
            "references_count": len(w.get("referenced_works") or []),
            "concepts": [c.get("display_name", "")
                         for c in (w.get("concepts") or [])[:10]],
        }
    except Exception:
        return None


def _reconstruct_abstract(inv: dict | None) -> str:
    """OpenAlex ships abstracts as inverted indices (token → positions).
    Reconstruct readable text."""
    if not inv or not isinstance(inv, dict):
        return ""
    try:
        slots: dict[int, str] = {}
        for token, positions in inv.items():
            for p in positions:
                slots[int(p)] = token
        return " ".join(slots[k] for k in sorted(slots))[:2000]
    except Exception:
        return ""
