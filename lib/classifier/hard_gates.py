"""Hard-gate classifier — Layer 2.

Two deterministic signals, either of which is enough on its own to confidently
classify a page as an academic article:

  A. **Metadata gate** — the page's HTML carries citation-style meta tags:
       citation_doi, citation_title, citation_author, citation_journal_title,
       dc.identifier=doi:..., prism.doi, og:type=article (only when also
       carrying any of the above)
     These tags are written by academic publishers' CMSes and are absent
     from blogs/news/products. Their presence is a near-zero-false-positive
     "this is a paper" signal.

  B. **URL-structure gate** — the URL path matches academic patterns:
       /doi/10..., /articles/..., /abs/..., /pdf/..., /content/..., /article/...,
       arxiv.org/abs/N, biorxiv/medrxiv/content/N, pubmed/N, isbn=...
     Used in conjunction with the domain tier — these patterns are most
     conclusive when the host is context-dependent (not already on an
     always/never list).
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from . import ClassificationResult

# Meta-tag names that conclusively indicate "this is an academic paper".
# Order matters: more-specific first.
CITATION_META_KEYS = (
    "citation_doi",
    "citation_title",
    "citation_journal_title",
    "citation_author",
    "citation_publication_date",
    "prism.doi",
    "prism.publicationname",
    "dc.identifier",     # only useful if value starts with "doi:"
    "bepress_citation_doi",
)

# URL substrings that strongly suggest an academic paper page.
URL_ACADEMIC_PATTERNS = (
    "/doi/10.", "doi.org/10.",
    "/articles/", "/article/",
    "/abs/", "/pdf/", "/fulltext",
    "arxiv.org/abs/", "arxiv.org/pdf/",
    "biorxiv.org/content/", "medrxiv.org/content/",
    "pubmed.ncbi.nlm.nih.gov/",
    "/preprints/",
    "ssrn.com/abstract",
)

# URL substrings that suggest a book page. NOTE: Amazon's "/dp/" is NOT here —
# it's used for EVERY Amazon product (electronics too), so it cannot classify
# books on its own. Amazon book detection requires the DOM `amazon_book`
# signal (see classify() below).
URL_BOOK_PATTERNS = (
    "/book/",
    "/books/",
    "books.google.com/books",
    "play.google.com/store/books",
    "isbn=", "/isbn/",
)


def _meta_signal(meta_dict: dict) -> tuple[bool, str | None]:
    """Returns (matched, which_key) if any citation meta tag is present + meaningful."""
    if not meta_dict:
        return False, None
    md = {k.lower(): (v or "") for k, v in meta_dict.items() if k}
    for k in CITATION_META_KEYS:
        v = md.get(k, "")
        if not v: continue
        if k == "dc.identifier":
            # only count it if it's a DOI form
            if v.lower().startswith("doi:") or re.search(r"\b10\.\d{4,}/", v):
                return True, k
            continue
        return True, k
    return False, None


def _url_signal(url: str) -> tuple[str | None, str | None]:
    """Returns (category_id, which_pattern) or (None, None)."""
    u = (url or "").lower()
    for pat in URL_ACADEMIC_PATTERNS:
        if pat in u:
            return "articles", pat
    for pat in URL_BOOK_PATTERNS:
        if pat in u:
            return "books", pat
    # Amazon product pages: per Bruno's 2026-05-19 directive, an Amazon
    # /dp/ or /gp/product/ page is treated as a Book. (Most of Bruno's
    # Amazon tabs are book pages; the occasional electronics page mis-shelved
    # into the Books collection is an easy manual fix, vs. losing real books.)
    if "amazon." in u and ("/dp/" in u or "/gp/product/" in u):
        return "books", "amazon_product"
    return None, None


def classify(url: str, page_meta: dict) -> ClassificationResult:
    """Runs the hard-gate checks; returns MATCH if any fires, else ABSTAIN.

    `page_meta` may carry a `dom` key holding the live DOM signals read from
    the phone's own Chrome tab (see lib/classifier/dom_reader.py). DOM signals
    are the most reliable evidence — they reflect the actually-rendered page,
    bypassing Cloudflare/Amazon anti-bot.
    """
    page_meta = page_meta or {}
    dom = page_meta.get("dom") or {}

    # A0. DOM signal — Amazon book detail page → Books (high confidence).
    # This is the ONLY way an Amazon page becomes a Book; Amazon URLs alone
    # never classify (electronics & books share the /dp/ URL shape).
    if dom.get("amazon_book"):
        return ClassificationResult.match(
            "books", confidence=0.95, layer="hard_gate",
            signal="dom_amazon_book",
        )

    # A1. DOM signal — DOI present in the rendered page → Articles.
    if dom.get("has_doi") or dom.get("doi"):
        return ClassificationResult.match(
            "articles", confidence=0.95, layer="hard_gate",
            signal="dom_doi", doi=dom.get("doi"),
        )

    # A2. DOM signal — ISBN present + the page is book-shaped → Books.
    if dom.get("has_isbn") and dom.get("isbn"):
        return ClassificationResult.match(
            "books", confidence=0.88, layer="hard_gate",
            signal="dom_isbn", isbn=dom.get("isbn"),
        )

    # A. Metadata gate (citation_* tags) — works on DOM meta or fetched meta
    meta_dict = dom.get("meta") or page_meta.get("_meta") or {}
    matched_meta, meta_key = _meta_signal(meta_dict)
    if matched_meta:
        return ClassificationResult.match(
            "articles", confidence=0.97, layer="hard_gate",
            signal="metadata", meta_key=meta_key,
        )

    # B. URL-structure gate
    cat, pat = _url_signal(url)
    if cat:
        return ClassificationResult.match(
            cat, confidence=0.92, layer="hard_gate",
            signal="url_pattern", pattern=pat,
        )

    return ClassificationResult.abstain(layer="hard_gate", reason="no_hard_signal")
