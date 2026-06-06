"""Crossref adapter — canonical DOI registry + paper metadata.

Free, no auth. The "polite pool" wants a `User-Agent: Egon/<v> (mailto:...)`
header; we honor it when egon-config.json has `crossref.email`.

Direct serves the three pillars (Bruno 2026-05-28):
  • Comprehensiveness — every DOI ever issued (~140M).
  • Heavy duty analysis — works/journals/funders/members endpoints support
    facet + filter queries for ad-hoc analysis.
  • Parsing/filtering — Crossref's filter language is precise; pairs with
    OpenAlex for cross-validation.

Docs: https://api.crossref.org/swagger-ui/index.html
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent.parent
BASE = "https://api.crossref.org"

META = {
    "id": "crossref",
    "label": "Crossref",
    "icon": "🔗",
    "kind": "reference_enrichment",
    "needs_auth": False,
    "destructive_actions": [],
    "read_only_default": True,
}


def _ua() -> dict:
    try:
        with (ROOT / "egon-config.json").open(encoding="utf-8") as f:
            email = (json.load(f).get("crossref") or {}).get("email")
        if email:
            return {"User-Agent": f"Egon/1.0 (mailto:{email})"}
    except Exception:
        pass
    return {"User-Agent": "Egon/1.0"}


def live_status(timeout: float = 5.0) -> dict:
    try:
        with httpx.Client(timeout=timeout, headers=_ua()) as c:
            r = c.get(f"{BASE}/works", params={"rows": 0})
        if r.status_code == 200:
            d = r.json() or {}
            total = (d.get("message") or {}).get("total-results")
            return {"status": "ok", "total_works": total,
                    "polite": _ua().get("User-Agent", "").find("mailto:") > 0}
        return {"status": "error", "error": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


def by_doi(doi: str) -> dict | None:
    doi = doi.strip().replace("https://doi.org/", "")
    try:
        with httpx.Client(timeout=10, headers=_ua()) as c:
            r = c.get(f"{BASE}/works/{doi}")
        if r.status_code != 200:
            return None
        m = (r.json() or {}).get("message") or {}
        return _flatten(m)
    except Exception:
        return None


def search(query: str, limit: int = 25,
           filter_str: str | None = None,
           sort: str = "score") -> list[dict]:
    params = {"query": query, "rows": min(limit, 100), "sort": sort}
    if filter_str:
        params["filter"] = filter_str
    try:
        with httpx.Client(timeout=20, headers=_ua()) as c:
            r = c.get(f"{BASE}/works", params=params)
        if r.status_code != 200:
            return []
        items = ((r.json() or {}).get("message") or {}).get("items") or []
        return [_flatten(m) for m in items]
    except Exception:
        return []


def _flatten(m: dict) -> dict:
    title_list = m.get("title") or [""]
    container = m.get("container-title") or [""]
    date_parts = (m.get("issued") or {}).get("date-parts") or [[None]]
    year = date_parts[0][0] if date_parts and date_parts[0] else None
    authors = []
    for a in (m.get("author") or [])[:20]:
        nm = " ".join(filter(None, [a.get("given"), a.get("family")]))
        if nm:
            authors.append(nm)
    return {
        "doi": m.get("DOI", ""),
        "title": title_list[0] if title_list else "",
        "year": year,
        "venue": container[0] if container else "",
        "type": m.get("type"),
        "publisher": m.get("publisher"),
        "authors": authors,
        "url": m.get("URL"),
        "is_referenced_by_count": m.get("is-referenced-by-count"),
        "references_count": m.get("references-count"),
        "abstract": (m.get("abstract") or "").replace("<jats:p>", "").replace("</jats:p>", "")[:2000],
    }
