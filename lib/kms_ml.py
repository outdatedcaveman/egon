"""KMS native ML classifier — the second tier under the master AI.

Bruno 2026-06-14: the master AI (lib/kms_classifier) is the ultimate arbiter,
but it costs tokens and needs a powerful LLM. This module is a lightweight,
fully-local learner that trains on BOTH the AI's labels and Bruno's manual
overrides. Over time its predictions should converge to the AI's, so:
  • it shrinks the spread vs the AI (track via agreement()),
  • it saves tokens (high-confidence ML predictions can skip the AI),
  • it is the fallback when no powerful LLM is available.

Design: embed (title + url + signals) with the LOCAL Ollama `nomic-embed-text`
(no API, already installed), then weighted-kNN over every stored label. Manual
overrides carry more weight than AI labels. Append-only label store →
incremental learning, no separate training step. Every write is traced.
"""
from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / "state" / "panop" / "kms_ml"
LABELS = STORE / "labels.jsonl"
OLLAMA = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
EMBED_MODEL = "nomic-embed-text"
CATEGORIES = ("articles", "books", "science_news", "science_longform", "reject")
_W = {"human": 3.0, "ai": 1.0}          # manual overrides weigh 3x

_cache = {"mtime": 0.0, "vecs": None, "labels": None, "weights": None}


def _feature_text(item: dict) -> str:
    from lib.kms_classifier import deterministic_signals
    sig = deterministic_signals(item)
    flags = " ".join(k for k, v in sig.items() if v)
    return f"{item.get('title','')}\n{item.get('url','')}\n{flags}".strip()


def _embed(text: str) -> list[float] | None:
    try:
        r = httpx.post(f"{OLLAMA}/api/embeddings",
                       json={"model": EMBED_MODEL, "prompt": text[:2000]}, timeout=30)
        if r.status_code == 200:
            return r.json().get("embedding")
    except Exception:
        pass
    return None


def record(item: dict, category: str, source: str = "ai") -> bool:
    """Append a labelled example (traced). source: 'ai' | 'human'."""
    if category not in CATEGORIES:
        return False
    emb = _embed(_feature_text(item))
    if not emb:
        return False
    STORE.mkdir(parents=True, exist_ok=True)
    row = {"ts": int(time.time()), "url": item.get("url"), "title": item.get("title"),
           "category": category, "source": source, "emb": emb}
    with LABELS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    _cache["mtime"] = 0.0  # invalidate
    return True


def _load():
    try:
        m = LABELS.stat().st_mtime
    except FileNotFoundError:
        _cache.update(mtime=0.0, vecs=[], labels=[], weights=[])
        return
    if m == _cache["mtime"] and _cache["vecs"] is not None:
        return
    vecs, labels, weights = [], [], []
    for line in LABELS.read_text(encoding="utf-8").splitlines():
        try:
            o = json.loads(line)
        except Exception:
            continue
        emb = o.get("emb")
        if not emb:
            continue
        n = math.sqrt(sum(x * x for x in emb)) or 1.0
        vecs.append([x / n for x in emb])             # pre-normalised
        labels.append(o["category"])
        weights.append(_W.get(o.get("source"), 1.0))
    _cache.update(mtime=m, vecs=vecs, labels=labels, weights=weights)


def predict(item: dict, k: int = 15) -> dict:
    """Weighted-kNN prediction from local labels. Returns category +
    confidence (0..1) + per-class vote weights. Empty store -> low confidence."""
    _load()
    vecs = _cache["vecs"]
    if not vecs:
        return {"category": None, "confidence": 0.0, "votes": {}, "n": 0}
    q = _embed(_feature_text(item))
    if not q:
        return {"category": None, "confidence": 0.0, "votes": {}, "n": 0, "error": "no_embedding"}
    nq = math.sqrt(sum(x * x for x in q)) or 1.0
    q = [x / nq for x in q]
    sims = []
    for i, v in enumerate(vecs):
        s = sum(a * b for a, b in zip(q, v))           # cosine (both normalised)
        sims.append((s, _cache["labels"][i], _cache["weights"][i]))
    sims.sort(key=lambda t: -t[0])
    top = sims[:k]
    votes: dict[str, float] = {}
    for s, lab, w in top:
        votes[lab] = votes.get(lab, 0.0) + max(0.0, s) * w
    total = sum(votes.values()) or 1.0
    best = max(votes, key=votes.get)
    return {"category": best, "confidence": round(votes[best] / total, 3),
            "votes": {c: round(votes.get(c, 0.0) / total, 3) for c in votes},
            "n": len(vecs)}


def agreement(sample: int = 200) -> dict:
    """How often the ML's own prediction matches the stored AI/human label —
    the convergence metric. Leave-one-out over a recent sample."""
    _load()
    labels = _cache["labels"]
    if len(labels) < 20:
        return {"n": len(labels), "agreement": None, "note": "need more labels"}
    import itertools
    vecs = _cache["vecs"]
    idxs = list(range(len(labels)))[-sample:]
    ok = 0
    for i in idxs:
        q = vecs[i]
        sims = sorted(((sum(a*b for a, b in zip(q, vecs[j])), labels[j])
                       for j in range(len(vecs)) if j != i), key=lambda t: -t[0])[:15]
        votes: dict[str, float] = {}
        for s, lab in sims:
            votes[lab] = votes.get(lab, 0.0) + max(0.0, s)
        if votes and max(votes, key=votes.get) == labels[i]:
            ok += 1
    return {"n": len(labels), "sampled": len(idxs),
            "agreement": round(ok / len(idxs), 3)}


def stats() -> dict:
    _load()
    from collections import Counter
    return {"labels": len(_cache["labels"] or []),
            "by_category": dict(Counter(_cache["labels"] or [])),
            "embed_model": EMBED_MODEL}
