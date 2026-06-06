"""Embedding-based classifier — Layer 3+5 (semantic + exemplar-personalized) +
Layer 4 (confidence/abstention).

Uses sentence-transformers (MiniLM) to embed the incoming page's text and
compute cosine similarity against per-category exemplar centroids precomputed
from the user's own past confirmed matches.

Gracefully degrades to abstain if:
  - sentence-transformers is not installed in the venv
  - centroids file is missing (run scripts/build_exemplar_centroids.py first)
  - the incoming page text is too thin

Confidence comes from BOTH:
  - absolute cosine similarity (top must exceed CONFIDENCE_THRESHOLD)
  - margin over second-best (must exceed MARGIN over the runner-up)
A medium-confidence result (between REVIEW_THRESHOLD and CONFIDENCE_THRESHOLD)
returns action="review" so it can be queued for human approval rather than
auto-acted upon.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from . import ClassificationResult

CENTROIDS_PATH = Path(__file__).resolve().parents[2] / "state" / "classifier" / "exemplar_centroids.npy"
META_PATH = Path(__file__).resolve().parents[2] / "state" / "classifier" / "exemplar_meta.json"

# Confidence thresholds, calibrated against the validation set.
# Tune these AFTER running validate_classifier.py.
CONFIDENCE_THRESHOLD = 0.55   # cosine ≥ this AND margin satisfied → MATCH
REVIEW_THRESHOLD     = 0.45   # cosine in [review, confidence) → REVIEW queue
MARGIN_MIN           = 0.05   # top must beat 2nd by at least this much
MIN_TEXT_CHARS       = 400    # below this the text is too thin to embed reliably

# Lazy globals
_model = None
_centroids = None       # numpy array shape (n_categories, dim)
_category_ids = None    # list of category id strings, parallel to _centroids


def _load_model():
    """Lazy-load the MiniLM model. Returns None if sentence-transformers is
    not installed."""
    global _model
    if _model is not None:
        return _model
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
    except ImportError:
        return None
    try:
        _model = SentenceTransformer("all-MiniLM-L6-v2")
        return _model
    except Exception:
        return None


def _load_centroids():
    """Lazy-load the precomputed centroids + category labels."""
    global _centroids, _category_ids
    if _centroids is not None:
        return _centroids, _category_ids
    if not CENTROIDS_PATH.exists() or not META_PATH.exists():
        return None, None
    try:
        import numpy as np  # type: ignore
        _centroids = np.load(CENTROIDS_PATH)
        meta = json.loads(META_PATH.read_text(encoding="utf-8"))
        _category_ids = meta.get("category_ids", [])
        return _centroids, _category_ids
    except Exception:
        return None, None


def classify(url: str, page_meta: dict) -> ClassificationResult:
    """Embed the page, compare to centroids, return MATCH / REVIEW / ABSTAIN."""
    # Build the text we'll embed
    title = (page_meta or {}).get("title", "") or ""
    abstract = (page_meta or {}).get("abstract", "") or ""
    text = (page_meta or {}).get("text", "") or ""
    # Prefer title+abstract (concentrated signal); fall back to first chunk of text
    composed = f"{title}\n\n{abstract}".strip()
    if len(composed) < MIN_TEXT_CHARS:
        composed = (composed + "\n\n" + text[:MIN_TEXT_CHARS * 4]).strip()
    if len(composed) < MIN_TEXT_CHARS:
        return ClassificationResult.abstain(layer="embedding",
                                            reason=f"text_too_thin:{len(composed)}")

    model = _load_model()
    if model is None:
        return ClassificationResult.abstain(layer="embedding",
                                            reason="model_unavailable")

    centroids, category_ids = _load_centroids()
    if centroids is None or not category_ids:
        return ClassificationResult.abstain(layer="embedding",
                                            reason="centroids_missing")

    try:
        import numpy as np  # type: ignore
        emb = model.encode(composed, normalize_embeddings=True)
        # centroids are already normalized in the build step
        sims = centroids @ emb  # cosine since both are unit-norm
        order = sims.argsort()[::-1]
        top_idx = int(order[0]); top_sim = float(sims[top_idx])
        second_sim = float(sims[order[1]]) if len(order) > 1 else 0.0
        margin = top_sim - second_sim
        cat = category_ids[top_idx]
        ev = {
            "top_category": cat, "top_sim": round(top_sim, 4),
            "second_sim": round(second_sim, 4), "margin": round(margin, 4),
            "thresholds": {"confidence": CONFIDENCE_THRESHOLD,
                           "review": REVIEW_THRESHOLD, "margin_min": MARGIN_MIN},
        }
        if top_sim >= CONFIDENCE_THRESHOLD and margin >= MARGIN_MIN:
            return ClassificationResult.match(cat, confidence=top_sim,
                                              layer="embedding", **ev)
        if top_sim >= REVIEW_THRESHOLD and margin >= MARGIN_MIN / 2:
            return ClassificationResult.review(cat, confidence=top_sim,
                                               layer="embedding", **ev)
        return ClassificationResult.abstain(layer="embedding",
                                            reason="below_threshold",
                                            **ev)
    except Exception as e:
        return ClassificationResult.abstain(layer="embedding",
                                            reason=f"exception:{str(e)[:80]}")
