"""Siloed discovery watchers for candidate research items.

The watcher reads Bruno-curated interest terms from
state/persona_interests.json, queries free public OpenAlex and arXiv
endpoints, and writes candidates only to state/discovery_queue.json.
Approval is intentionally separate; nothing here imports into Zotero,
Mouseion, Paperpile, or any reference manager.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from lib import egon_paths

ROOT = egon_paths.EGON_ROOT
STATE_DIR = egon_paths.STATE_DIR
INTERESTS_PATH = STATE_DIR / "persona_interests.json"
QUEUE_PATH = STATE_DIR / "discovery_queue.json"

OPENALEX_WORKS = "https://api.openalex.org/works"
ARXIV_QUERY = "https://export.arxiv.org/api/query"

DEFAULT_DAYS_BACK = 30
DEFAULT_PER_QUERY = 8
MAX_QUERIES = 12
ABSTRACT_HEAD_CHARS = 900

_SPACE_RE = re.compile(r"\s+")
_DOI_RE = re.compile(r"(?i)\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b")
_ARXIV_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=True, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
    finally:
        try:
            Path(tmp).unlink(missing_ok=True)
        except Exception:
            pass


def _clean_text(value: Any, limit: int = 300) -> str:
    text = _SPACE_RE.sub(" ", str(value or "")).strip()
    return text[:limit].strip()


def _normalize_doi(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    raw = raw.replace("https://doi.org/", "").replace("http://doi.org/", "")
    match = _DOI_RE.search(raw)
    return match.group(0).lower().rstrip(".,;") if match else raw.lower().rstrip(".,;")


def _candidate_key(source: str, external_id: str, doi: str = "") -> str:
    doi = _normalize_doi(doi)
    if doi:
        return "doi:" + doi
    raw = f"{source}:{external_id}".strip(":")
    return raw.lower() if raw else "hash:" + hashlib.sha1(str(time.time_ns()).encode("ascii")).hexdigest()[:20]


def _abstract_head(text: Any) -> str:
    return _clean_text(text, ABSTRACT_HEAD_CHARS)


def _load_interest_terms(limit: int = MAX_QUERIES) -> list[str]:
    data = _read_json(INTERESTS_PATH, {})
    removed = {
        _clean_text(item).casefold()
        for item in data.get("removed", [])
        if _clean_text(item)
    }

    terms: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, dict):
            value = value.get("name") or value.get("title") or value.get("value")
        term = _clean_text(value, 120)
        if not term or term.casefold() in removed:
            return
        if term.casefold() in {t.casefold() for t in terms}:
            return
        terms.append(term)

    for term in data.get("pinned", []):
        add(term)
    for term in data.get("added", []):
        add(term)

    # If the overlay only has renames, the renamed values are still Bruno's
    # curated vocabulary and are safer than mining unrelated personal state.
    renames = data.get("renames") if isinstance(data.get("renames"), dict) else {}
    for term in renames.values():
        add(term)

    return terms[:limit]


def _request_json(url: str, params: dict[str, Any], timeout: float = 20.0) -> dict[str, Any]:
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    req = urllib.request.Request(
        url + ("?" + query if query else ""),
        headers={
            "Accept": "application/json",
            "User-Agent": "EgonDiscovery/1.0 (siloed local queue)",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    body = json.loads(raw)
    return body if isinstance(body, dict) else {}


def _request_xml(url: str, params: dict[str, Any], timeout: float = 20.0) -> ET.Element:
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    req = urllib.request.Request(
        url + ("?" + query if query else ""),
        headers={"User-Agent": "EgonDiscovery/1.0 (siloed local queue)"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return ET.fromstring(raw)


def _reconstruct_openalex_abstract(inv: dict[str, list[int]] | None) -> str:
    if not isinstance(inv, dict):
        return ""
    try:
        slots: dict[int, str] = {}
        for token, positions in inv.items():
            for pos in positions:
                slots[int(pos)] = token
        return " ".join(slots[i] for i in sorted(slots))
    except Exception:
        return ""


def _score(query: str, title: str, abstract: str, year: Any, source_bonus: int = 0) -> int:
    q_terms = {p.casefold() for p in re.findall(r"[A-Za-z0-9][A-Za-z0-9-]{2,}", query)}
    hay_title = title.casefold()
    hay_abs = abstract.casefold()
    hits = sum(1 for term in q_terms if term in hay_title or term in hay_abs)
    title_hits = sum(1 for term in q_terms if term in hay_title)
    try:
        recency = max(0, int(year or 0) - 2020)
    except Exception:
        recency = 0
    raw = 40 + hits * 8 + title_hits * 10 + min(recency, 8) + source_bonus
    return max(1, min(100, raw))


def _openalex_candidates(query: str, days_back: int, per_query: int) -> list[dict[str, Any]]:
    since = (datetime.utcnow() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    params = {
        "search": query,
        "per-page": max(1, min(25, per_query)),
        "sort": "publication_date:desc",
        "filter": f"from_publication_date:{since}",
    }
    body = _request_json(OPENALEX_WORKS, params=params)
    out: list[dict[str, Any]] = []
    for item in body.get("results") or []:
        if not isinstance(item, dict):
            continue
        external_id = _clean_text(item.get("id"), 200).replace("https://openalex.org/", "")
        doi = _normalize_doi(item.get("doi"))
        title = _clean_text(item.get("display_name") or item.get("title"), 500)
        abstract = _reconstruct_openalex_abstract(item.get("abstract_inverted_index"))
        year = item.get("publication_year")
        authors = [
            _clean_text((a.get("author") or {}).get("display_name"), 120)
            for a in (item.get("authorships") or [])[:8]
            if isinstance(a, dict)
        ]
        url = _clean_text(item.get("doi"), 300) or _clean_text(item.get("id"), 300)
        if url and not url.startswith(("http://", "https://")):
            url = "https://doi.org/" + url
        if not title or not external_id:
            continue
        key = _candidate_key("openalex", external_id, doi)
        out.append({
            "key": key,
            "id": external_id,
            "doi": doi,
            "title": title,
            "authors": [a for a in authors if a],
            "year": year,
            "abstract_head": _abstract_head(abstract),
            "source": "openalex",
            "source_url": url or f"https://openalex.org/{external_id}",
            "query": query,
            "relevance_score": _score(query, title, abstract, year, source_bonus=2),
            "found_at": int(time.time()),
        })
    return out


def _arxiv_candidates(query: str, days_back: int, per_query: int) -> list[dict[str, Any]]:
    params = {
        "search_query": "all:" + query,
        "start": 0,
        "max_results": max(1, min(25, per_query)),
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    root = _request_xml(ARXIV_QUERY, params=params)
    cutoff = datetime.utcnow() - timedelta(days=days_back)
    out: list[dict[str, Any]] = []
    for entry in root.findall("atom:entry", _ARXIV_NS):
        external_id = _clean_text(entry.findtext("atom:id", default="", namespaces=_ARXIV_NS), 300)
        title = _clean_text(entry.findtext("atom:title", default="", namespaces=_ARXIV_NS), 500)
        abstract = _clean_text(entry.findtext("atom:summary", default="", namespaces=_ARXIV_NS), 4000)
        published = _clean_text(entry.findtext("atom:published", default="", namespaces=_ARXIV_NS), 40)
        try:
            published_dt = datetime.fromisoformat(published.replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            published_dt = None
        if published_dt and published_dt < cutoff:
            continue
        year = published_dt.year if published_dt else None
        doi = _normalize_doi(entry.findtext("arxiv:doi", default="", namespaces=_ARXIV_NS))
        authors = [
            _clean_text(author.findtext("atom:name", default="", namespaces=_ARXIV_NS), 120)
            for author in entry.findall("atom:author", _ARXIV_NS)[:8]
        ]
        if not title or not external_id:
            continue
        key = _candidate_key("arxiv", external_id.rsplit("/", 1)[-1], doi)
        out.append({
            "key": key,
            "id": external_id.rsplit("/", 1)[-1],
            "doi": doi,
            "title": title,
            "authors": [a for a in authors if a],
            "year": year,
            "abstract_head": _abstract_head(abstract),
            "source": "arxiv",
            "source_url": external_id,
            "query": query,
            "relevance_score": _score(query, title, abstract, year),
            "found_at": int(time.time()),
        })
    return out


def _load_queue() -> dict[str, Any]:
    data = _read_json(QUEUE_PATH, {})
    if isinstance(data, list):
        data = {"version": 1, "candidates": data}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("version", 1)
    data.setdefault("candidates", [])
    return data


def run_watchers(
    *,
    force: bool = False,
    days_back: int = DEFAULT_DAYS_BACK,
    per_query: int = DEFAULT_PER_QUERY,
) -> dict[str, Any]:
    """Run discovery once per local day unless force=True.

    Returns a summary and writes all accepted candidates to QUEUE_PATH.
    """
    queue = _load_queue()
    today = datetime.now().strftime("%Y-%m-%d")
    if not force and queue.get("last_run_date") == today:
        return {
            "status": "skipped",
            "reason": "already-ran-today",
            "queue_path": str(QUEUE_PATH),
            "candidates": len(queue.get("candidates") or []),
        }

    queries = _load_interest_terms()
    existing: dict[str, dict[str, Any]] = {}
    for item in queue.get("candidates") or []:
        if not isinstance(item, dict):
            continue
        key = item.get("key") or _candidate_key(item.get("source", ""), item.get("id", ""), item.get("doi", ""))
        if key:
            item["key"] = key
            existing[key] = item

    errors: list[dict[str, str]] = []
    added = 0
    for query in queries:
        for source, fetcher in (("openalex", _openalex_candidates), ("arxiv", _arxiv_candidates)):
            try:
                candidates = fetcher(query, days_back, per_query)
            except Exception as exc:
                errors.append({
                    "source": source,
                    "query": query,
                    "error": f"{type(exc).__name__}: {str(exc)[:240]}",
                })
                continue
            for candidate in candidates:
                key = candidate.get("key") or _candidate_key(source, candidate.get("id", ""), candidate.get("doi", ""))
                candidate["key"] = key
                if key not in existing:
                    added += 1
                existing[key] = {**existing.get(key, {}), **candidate}

    candidates = list(existing.values())
    candidates.sort(key=lambda c: (int(c.get("relevance_score") or 0), int(c.get("found_at") or 0)), reverse=True)
    payload = {
        "version": 1,
        "last_run_date": today,
        "generated_at": int(time.time()),
        "queries": queries,
        "candidates": candidates,
        "errors": errors,
    }
    _atomic_write_json(QUEUE_PATH, payload)
    return {
        "status": "ok",
        "queue_path": str(QUEUE_PATH),
        "queries": len(queries),
        "added": added,
        "candidates": len(candidates),
        "errors": errors,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run siloed Egon discovery watchers.")
    parser.add_argument("--force", action="store_true", help="run even if watchers already ran today")
    parser.add_argument("--days-back", type=int, default=DEFAULT_DAYS_BACK)
    parser.add_argument("--per-query", type=int, default=DEFAULT_PER_QUERY)
    args = parser.parse_args(argv)
    result = run_watchers(force=args.force, days_back=args.days_back, per_query=args.per_query)
    print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True))
    return 0 if result.get("status") in {"ok", "skipped"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
