"""Cross-encoder reranker — the precision (assertive) layer of the RAG.

model2vec retrieval is fast and broad but, being static, loses nuance on
compositional queries ("film noir CINEMATOGRAPHY", not just "film noir"). A
cross-encoder fixes that: it re-reads each top candidate's actual text jointly
with the query and scores true relevance — exactly where static embeddings fail.
It only runs on the ~50 retrieved candidates, so it's cheap per query.

Design for an 8GB CPU box (Bruno 2026-06-24):
  • lazy-loaded + cached; one ~280MB model (bge-reranker-base);
  • degrades GRACEFULLY — any failure (low RAM, model missing) returns the
    input order, so search never breaks;
  • a RAM floor prevents loading it when memory is tight;
  • disable with EGON_RERANK=0; swap model with EGON_RERANK_MODEL.
"""
from __future__ import annotations

import ctypes
import os
import threading

# Light by default (cross-encoder/ms-marco-MiniLM-L-6-v2, ~80MB) so it loads even
# on this RAM-tight 8GB box; bge-reranker-base is stronger if RAM allows (set
# EGON_RERANK_MODEL). Either way it only scores the ~50 retrieved candidates.
MODEL_NAME = os.environ.get("EGON_RERANK_MODEL", "cross-encoder/ms-marco-MiniLM-L-6-v2")
ENABLED = os.environ.get("EGON_RERANK", "1").lower() not in ("0", "false", "no", "off")
_RAM_FLOOR_GB = float(os.environ.get("EGON_RERANK_MIN_RAM_GB", "0.4"))

_model = None
_failed = False
_lock = threading.Lock()


def _free_ram_gb() -> float:
    try:
        class M(ctypes.Structure):
            _fields_ = [("a", ctypes.c_ulong), ("b", ctypes.c_ulong),
                        ("c", ctypes.c_ulonglong), ("d", ctypes.c_ulonglong),
                        ("e", ctypes.c_ulonglong), ("f", ctypes.c_ulonglong),
                        ("g", ctypes.c_ulonglong), ("h", ctypes.c_ulonglong),
                        ("i", ctypes.c_ulonglong)]
        m = M(); m.a = ctypes.sizeof(M)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        return m.d / 1024 ** 3
    except Exception:
        return 999.0


def _load():
    global _model, _failed
    if _model is not None or _failed:
        return _model
    with _lock:
        if _model is not None or _failed:
            return _model
        if _free_ram_gb() < _RAM_FLOOR_GB:
            return None          # try again later; don't mark permanently failed
        try:
            from sentence_transformers import CrossEncoder
            _model = CrossEncoder(MODEL_NAME, max_length=512)
        except Exception:
            _failed = True
            _model = None
        return _model


def is_ready() -> bool:
    return _model is not None


def warm_async() -> None:
    """Preload off the request path so the first rerank isn't slow."""
    if ENABLED:
        threading.Thread(target=_load, daemon=True, name="reranker-warmup").start()


def _cand_text(c: dict) -> str:
    return (str(c.get("title") or "") + ". " + str(c.get("snippet") or ""))[:512]


def rerank(query: str, candidates: list[dict], top_k: int) -> list[dict]:
    """Re-score `candidates` against `query` with the cross-encoder and return the
    top_k reordered. Falls back to the input order (truncated) on any failure, so
    callers can use it unconditionally."""
    if not ENABLED or not candidates or not (query or "").strip():
        return candidates[:top_k]
    m = _load()
    if m is None:
        return candidates[:top_k]
    try:
        pairs = [(query, _cand_text(c)) for c in candidates]
        scores = m.predict(pairs, batch_size=16, show_progress_bar=False)
        order = sorted(range(len(candidates)),
                       key=lambda i: float(scores[i]), reverse=True)
        out = []
        for i in order[:top_k]:
            c = dict(candidates[i])
            c["rerank_score"] = round(float(scores[i]), 3)
            out.append(c)
        return out
    except Exception:
        return candidates[:top_k]
