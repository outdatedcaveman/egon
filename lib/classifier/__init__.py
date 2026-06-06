"""Egon's layered tab classifier.

Six independent layers, fired in this order. Each layer can MATCH (return a
category with confidence), ABSTAIN (return None — let the next layer try),
or REJECT (return None and skip remaining layers — used by domain_tiers to
hard-block known-non-academic URLs).

    [domain_tiers]      hard never/always/context floor
    [hard_gates]        citation_doi/title meta tags + URL structure
    [embedding match]   cosine similarity vs per-category exemplar centroids
    [abstention]        wraps the embedding layer's output: confidence + margin
    [review queue]      medium-confidence results route here (no auto-action)

Public API:
    classify(url, page_meta=None) -> ClassificationResult

  ClassificationResult.category  is the matched category id or None
  ClassificationResult.confidence is 0.0-1.0
  ClassificationResult.layer      tells you which layer made the decision
  ClassificationResult.action    is one of: "match" | "abstain" | "review"
  ClassificationResult.evidence  is a dict of signals used

The classifier NEVER mutates state — it's a pure function. Callers decide
what to DO with the result (save to Zotero, queue for review, ignore).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ClassificationResult:
    category: str | None        # category id, e.g. "articles" / "books" / "science_news"
    confidence: float           # 0.0 (no signal) to 1.0 (certain)
    layer: str                  # which layer decided: "domain_tier" / "hard_gate" / "embedding" / "abstain"
    action: str                 # "match" | "abstain" | "review" — what the caller should do
    evidence: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def abstain(cls, layer: str = "abstain", reason: str = "", **evidence):
        ev = dict(evidence)
        if reason:
            ev["reason"] = reason
        return cls(category=None, confidence=0.0, layer=layer, action="abstain",
                   evidence=ev)

    @classmethod
    def review(cls, category: str, confidence: float, layer: str, **evidence):
        return cls(category=category, confidence=confidence, layer=layer,
                   action="review", evidence=evidence)

    @classmethod
    def match(cls, category: str, confidence: float, layer: str, **evidence):
        return cls(category=category, confidence=confidence, layer=layer,
                   action="match", evidence=evidence)


def classify(url: str, page_meta: dict | None = None) -> ClassificationResult:
    """Top-level classifier. Pure function over (url, page_meta).
    Page_meta is the dict returned by Panop's `fetch_page_content` — has
    `_meta` (HTML meta tags), `title`, `text`, `abstract`, `article_links`.
    """
    from . import domain_tiers, hard_gates, embeddings

    # Layer 1: domain reputation tier
    res = domain_tiers.classify(url)
    # A MATCH from the always_* lists is authoritative — return directly.
    if res.action == "match":
        return res
    # A "never_academic" hit MUST short-circuit: do not let later layers
    # accidentally re-classify a banned domain (this was the original bug —
    # the /dp/ URL pattern would match amazon.com/.../dp/ even though
    # amazon.com is explicitly on never_academic).
    reason = (res.evidence or {}).get("reason", "")
    if isinstance(reason, str) and reason.startswith("never_academic:"):
        return res  # final abstain, never override

    # Layer 2: metadata + URL hard gates
    res = hard_gates.classify(url, page_meta or {})
    if res.action == "match":
        return res

    # Layer 3: embedding similarity (only if exemplar centroids exist on disk)
    res = embeddings.classify(url, page_meta or {})
    return res
