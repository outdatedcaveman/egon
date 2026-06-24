"""Cross-platform search — one query, every snapshot.

Reads every snapshot JSON in egon/state/snapshots/<source>/<date>.json and
the vault mirror at G:/.../snapshots/<source>/. Each snapshot's `items` array
is flattened into searchable documents. Scoring is hybrid:

- exact substring match (highest weight)
- whitespace-tokenized overlap (TF-style)
- field-specific boosts (title > description > url)

No embeddings yet — pure lexical match. Snapshots are small (<10K items each),
so this is fast (<200 ms per query) and free. Vault still uses the heavier
MiniLM index via `query_vault.py`.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

LOCAL_ROOT = Path(__file__).resolve().parent.parent / "state" / "snapshots"
from lib.egon_paths import VAULT_SNAPSHOTS as VAULT_ROOT

# field weights for the score: which keys in an item contribute, and how heavily
_FIELD_WEIGHTS = {
    "title":             3.0,
    "name":              3.0,
    "url":               1.5,
    "description":       1.0,
    "doi":               2.0,
    "folder":            0.8,
    "year":              0.5,
    "rating":            0.3,
    "watched_date":      0.3,
    "added":             0.3,
}


# Parsed-snapshot cache keyed by (file path, mtime). The snapshots are large
# JSON (the whole archive corpus); re-reading + json.loads-ing all of them on
# EVERY search cost ~28s and made the phone Connect search slow even with the
# semantic index warm. Cache keeps them parsed in RAM and only re-reads a source
# when its snapshot file actually changes. Bruno 2026-06-24.
_SNAP_CACHE: dict[str, tuple[tuple[str, float], dict]] = {}


def _latest_snapshot_for(source: str) -> dict | None:
    """Most-recent snapshot file for `source`. Prefer local, fall back to vault.
    Cached by (path, mtime) so a warm process doesn't re-parse the corpus."""
    for root in (LOCAL_ROOT, VAULT_ROOT):
        d = root / source
        if not d.exists():
            continue
        files = sorted(d.glob("*.json"), reverse=True)
        for f in files:
            try:
                key = (str(f), f.stat().st_mtime)
                cached = _SNAP_CACHE.get(source)
                if cached is not None and cached[0] == key:
                    return cached[1]
                snap = json.loads(f.read_text(encoding="utf-8"))
                if snap.get("status") == "ok" and snap.get("items"):
                    result = {"source": source, "synced_at": snap.get("synced_at"),
                              "items": snap["items"]}
                    _SNAP_CACHE[source] = (key, result)
                    return result
            except Exception:
                continue
    return None


def _all_sources() -> list[str]:
    """Union of sources we have at least one snapshot for."""
    seen = set()
    for root in (LOCAL_ROOT, VAULT_ROOT):
        if not root.exists():
            continue
        for d in root.iterdir():
            if d.is_dir():
                seen.add(d.name)
    return sorted(seen)


_TOKEN_RE = re.compile(r"\w+")

# Stop words that are too common to contribute meaningful signal
_STOP_WORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from",
    "has", "have", "in", "is", "it", "its", "of", "on", "or", "such",
    "that", "the", "their", "then", "there", "these", "they", "this",
    "to", "was", "were", "will", "with", "would",
})


def _tokens(s: str, drop_stop: bool = True) -> set[str]:
    toks = {t.lower() for t in _TOKEN_RE.findall(s or "")}
    return (toks - _STOP_WORDS) if drop_stop else toks


def _score(item: dict, q_lower: str, q_tokens: set[str]) -> float:
    """Hybrid score: substring match + token overlap per weighted field."""
    score = 0.0
    for field, weight in _FIELD_WEIGHTS.items():
        val = item.get(field)
        if val is None:
            continue
        text = str(val).lower()
        if not text:
            continue
        # 1) exact substring match (strong signal)
        if q_lower in text:
            score += weight * 3.0
            if text.startswith(q_lower):
                score += weight  # bonus for prefix match
        # 2) token overlap (weaker, but catches multi-word queries)
        tokens = _tokens(text)
        overlap = len(q_tokens & tokens)
        if overlap:
            score += weight * (overlap / max(len(q_tokens), 1))
    return score


def search(query: str, sources: list[str] | None = None,
           limit: int = 50) -> list[dict]:
    """Search all (or selected) snapshots. Returns ranked list of:
        {source, score, item: <raw item>, synced_at}
    """
    q = (query or "").strip()
    if not q:
        return []
    q_lower = q.lower()
    q_tokens = _tokens(q)
    # if all query words are stop words, fall back to including them (degenerate queries)
    if not q_tokens:
        q_tokens = _tokens(q, drop_stop=False)
    # minimum score threshold: must beat the noise floor of a single stop-ish match
    MIN_SCORE = 0.5

    src_filter = set(sources) if sources else None
    results: list[dict] = []

    for source in _all_sources():
        if src_filter and source not in src_filter:
            continue
        snap = _latest_snapshot_for(source)
        if not snap:
            continue
        for item in snap["items"]:
            if not isinstance(item, dict):
                continue
            s = _score(item, q_lower, q_tokens)
            if s >= MIN_SCORE:
                results.append({
                    "source": source,
                    "score": round(s, 2),
                    "item": item,
                    "synced_at": snap.get("synced_at"),
                })

    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:limit]


def stats() -> dict:
    """Inventory: how many items we have per source."""
    out = {}
    for source in _all_sources():
        snap = _latest_snapshot_for(source)
        out[source] = len(snap["items"]) if snap else 0
    return out


# -- helpers for the UI ------------------------------------------------------

def pretty_title(item: dict) -> str:
    """Best title-like field across the heterogeneous schemas."""
    for k in ("title", "name", "headline", "doi", "url"):
        if item.get(k):
            return str(item[k])
    return "(untitled)"


def pretty_url(item: dict) -> str | None:
    for k in ("url", "external_url", "link"):
        if item.get(k):
            return str(item[k])
    return None


def pretty_subline(item: dict, source: str) -> str:
    """Source-aware subline shown under the title."""
    parts: list[str] = [source]
    if "folder" in item and item["folder"]:
        parts.append(f"📁 {item['folder']}")
    if "year" in item and item["year"]:
        parts.append(str(item["year"]))
    if "rating" in item and item["rating"]:
        parts.append(f"★ {item['rating']}")
    if "doi" in item and item["doi"]:
        parts.append(item["doi"])
    if "watched_date" in item and item["watched_date"]:
        parts.append(f"watched {item['watched_date']}")
    return " · ".join(parts)
