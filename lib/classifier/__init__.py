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


# GENUINE non-content — never a saveable item, hard-reject before any ML.
# Search-result pages, auth/mail/calendar surfaces. NOT social/video (those
# need judgement — a YouTube lecture can be longform, an X thread a reference).
_HARD_REJECT_HOSTS = {
    "accounts.google.com", "mail.google.com", "calendar.google.com", "drive.google.com",
    "docs.google.com", "outlook.live.com", "outlook.office.com", "web.whatsapp.com",
    "messenger.com", "translate.google.com",
}
# Social / video: do NOT blanket-reject and do NOT let the k-NN force them into a
# scholarly bucket (it has no exemplars for them). Route to JUDGEMENT — the AI
# arbiter decides content_longform / references / reject by the actual content.
# Bruno 2026-06-15: "youtube and x could be a longform or reference; exercise
# judgement, don't throw out entire domains."
_JUDGEMENT_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "music.youtube.com",
    "x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com",
    "facebook.com", "www.facebook.com", "m.facebook.com", "instagram.com",
    "tiktok.com", "reddit.com", "www.reddit.com", "old.reddit.com", "linkedin.com",
    "t.co", "pinterest.com",
}


def _hard_reject(url: str) -> bool:
    from urllib.parse import urlparse
    u = (url or "").lower()
    host = (urlparse(u).netloc or "")
    if host in _HARD_REJECT_HOSTS:
        return True
    if ("google.com/search" in u or "google.com/url?" in u or "bing.com/search" in u
            or "/search?q=" in u or (host.endswith(".google.com") and "/search" in u)):
        return True
    return False


def _needs_judgement(url: str) -> bool:
    from urllib.parse import urlparse
    return (urlparse((url or "").lower()).netloc or "") in _JUDGEMENT_HOSTS


def classify(url: str, page_meta: dict | None = None) -> ClassificationResult:
    """Top-level classifier. Pure function over (url, page_meta).
    Page_meta is the dict returned by Panop's `fetch_page_content` — has
    `_meta` (HTML meta tags), `title`, `text`, `abstract`, `article_links`.
    """
    from . import domain_tiers, hard_gates, embeddings
    from lib.body_classify import resolve_redirect, _object_type

    # Layer 0 (pre): redirect wrapper — classify the REAL target, not the
    # platform. A facebook l.php/flx-warn or google/url link points elsewhere.
    # Bruno 2026-06-17: "facebook entries are the redirect links, not the platform."
    tgt = resolve_redirect(url)
    if tgt and tgt != url:
        return classify(tgt, page_meta)

    # Layer 0a: hard reject — genuine non-content (search/auth/mail).
    if _hard_reject(url):
        return ClassificationResult.abstain(layer="hard_reject", reason="never_academic:hard_reject")

    # Layer 0b: deterministic object-type — the same host splits by what the page
    # IS (hf paper-page->science_news vs model-repo->data_tools; SEP/Wikipedia/
    # PhilPeople->references; code repo->data_tools; google/play books->books).
    # Bruno's taxonomy, encoded from his 172 category corrections (2026-06-17).
    ot = _object_type(url)
    if ot:
        return ClassificationResult.match(ot[0], confidence=ot[1], layer="object_type", source=ot[2])

    # Layer 0c: social/video need JUDGEMENT, not a forced k-NN match. Route to
    # review so the AI arbiter decides (a YouTube lecture may be longform, an X
    # thread a reference); the k-NN must never label these as articles/books.
    if _needs_judgement(url):
        return ClassificationResult.review("content_longform", confidence=0.0,
                                           layer="needs_judgement", reason="social_video_needs_ai")

    # Layer 1: domain reputation tier
    res = domain_tiers.classify(url)
    # A MATCH from the always_* lists is authoritative — return directly.
    if res.action == "match":
        return res
    # NOTE: the old "never_academic short-circuit" was removed 2026-06-15. It
    # was correct when 'articles' was the only positive class, but the full
    # taxonomy now has legitimate categories on those very domains: github ->
    # data_tools, wikipedia -> references, medium/substack -> content_longform.
    # So a never_academic domain must NOT short-circuit — it flows to the kNN,
    # which decides the right category (or 'reject' for true noise).

    # Layer 2: metadata + URL hard gates
    res = hard_gates.classify(url, page_meta or {})
    if res.action == "match":
        return res

    # Layer 3: native ML — full-taxonomy k-NN trained on Bruno's own bookmark
    # folders (lib/kms_knn). This is the default brain for every surface; it
    # covers articles/books/science_news/content_longform/references/data_tools/
    # shopping/study_work/opportunities/curios and tells e.g. an Amazon book
    # from Amazon cutlery by title. Bruno 2026-06-15.
    try:
        import lib.kms_knn as _knn
        title = (page_meta or {}).get("title", "") or ""
        if title:
            k = _knn.classify(title, url)
            cat, conf = k.get("category"), k.get("confidence", 0.0)
            if cat and cat != "reject":
                if conf >= 0.50:
                    return ClassificationResult.match(cat, confidence=conf, layer="kms_knn",
                                                      share=k.get("share"), votes=k.get("votes"))
                if conf >= 0.35:
                    return ClassificationResult.review(cat, confidence=conf, layer="kms_knn",
                                                       votes=k.get("votes"))
    except Exception:
        pass

    # Layer 4 (fallback): legacy embedding centroids, if present
    res = embeddings.classify(url, page_meta or {})
    return res
