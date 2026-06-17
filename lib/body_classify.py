"""Body-first classifier. Bruno 2026-06-15: neither the title nor the URL alone
is a reliable criterion (titles get Cloudflare-blocked; URLs are often
redirects/utility pages). So FETCH the page and judge from its BODY — citation
meta = a paper, product/price = shopping, ISBN/book schema = a book, a
contentless preference/whois/login page = reject — with title+URL only as
supporting hints. When the body can't be fetched (bot-wall/rate-limit), fall
back to URL structure; when the body is real but ambiguous, hand the extracted
text to the AI arbiter.
"""
from __future__ import annotations
import re
from urllib.parse import urlparse

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
       "Accept": "text/html,application/xhtml+xml"}

# URL-structure fallback (only used when the body can't be read).
_PAPER_HOST_PATH = (
    ("arxiv.org", "/abs/"), ("arxiv.org", "/pdf/"), ("philpapers.org", "/rec/"),
    ("philpapers.org", "/archive/"), ("ncbi.nlm.nih.gov", "/articles/pmc"),
    ("jstor.org", "/stable/"), ("academic.oup.com", "/article"), ("nature.com", "/articles/"),
    ("science.org", "/doi/"), ("biorxiv.org", "/content/"), ("pnas.org", "/doi/"),
    ("aeaweb.org", "/doi/"), ("psycnet.apa.org", "/"), ("ssrn.com", "/abstract"),
)
_UTILITY_RE = re.compile(r"/(unsubscribe|preference|preferences|preference-center|confirm|"
                         r"manage-subscription|email-settings|whois|login|signin|account|cart|checkout)"
                         r"|list-manage\.com|/page/confirm|watermark\d*\.", re.I)


def _fetch(url, timeout=10):
    try:
        import cloudscraper
        s = cloudscraper.create_scraper()
    except Exception:
        import requests
        s = requests.Session()
    try:
        r = s.get(url, headers=_UA, timeout=timeout)
        return r.status_code, (r.text or "")[:300_000]
    except Exception:
        return 0, ""


def _meta(html, *names):
    for n in names:
        m = re.search(r'<meta[^>]+(?:name|property)=["\']%s["\'][^>]+content=["\']([^"\']+)' % re.escape(n), html, re.I)
        if m:
            return m.group(1).strip()
    return ""


def classify_by_body(url, want_text=False):
    """Return {category, confidence, source, title, abstract?, reason}. category
    may be None with source='needs_ai' (real body, ambiguous) or 'blocked'."""
    host = (urlparse(url).netloc or "").lower()
    status, html = _fetch(url)
    title_m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    title = re.sub(r"\s+", " ", (title_m.group(1) if title_m else "")).strip()
    tl = title.lower()
    blocked = (status in (403, 429, 503) or not html
               or "just a moment" in tl or "request limit" in tl or "are you a robot" in tl
               or "checking your browser" in tl)

    if not blocked:
        # ── strong BODY signals ───────────────────────────────────────────
        if _meta(html, "citation_title", "citation_doi", "citation_journal_title", "citation_author"):
            return {"category": "articles", "confidence": 0.97, "source": "body:citation_meta",
                    "title": _meta(html, "citation_title") or title,
                    "abstract": _meta(html, "citation_abstract", "description", "og:description")}
        ogtype = _meta(html, "og:type").lower()
        # Book signals must be tested BEFORE product markup: Amazon/retailer BOOK
        # pages carry Product+price schema too, but ISBN / "Paperback" / "Kindle
        # Edition" / "Print length" mark them as books. Bruno's rule: an Amazon
        # book link -> Books, an Amazon cutlery link -> Shopping.
        book_sig = (ogtype == "book"
                    or re.search(r'"@type"\s*:\s*"Book"|itemtype=["\'][^"\']*schema.org/Book', html, re.I)
                    or re.search(r'\bISBN(?:[- ]?1[03])?\b|Kindle Edition|Print length|\bPaperback\b|'
                                 r'\bHardcover\b|Audible Audiobook|Mass Market Paperback|Publication date',
                                 html, re.I))
        if book_sig:
            return {"category": "books", "confidence": 0.9, "source": "body:book_signals", "title": title}
        if (ogtype == "product" or re.search(r'"@type"\s*:\s*"Product"|itemprop=["\']price|add to cart|add to basket', html, re.I)):
            return {"category": "shopping", "confidence": 0.85, "source": "body:product", "title": title}
        # contentless utility page → reject (short body, utility words, no article tags)
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text).strip()
        if (_UTILITY_RE.search(url) and not _meta(html, "citation_title")) or \
           (len(text) < 600 and any(w in tl for w in ("unsubscribe", "preference", "confirm", "whois", "sign in", "log in"))):
            return {"category": "reject", "confidence": 0.9, "source": "body:utility_page", "title": title}
        # real body but ambiguous → hand to AI with extracted content
        out = {"category": None, "confidence": 0.0, "source": "needs_ai", "title": title,
               "abstract": _meta(html, "description", "og:description")}
        if want_text:
            out["text"] = text[:4000]
        return out

    # ── blocked: fall back to URL structure ───────────────────────────────
    if _UTILITY_RE.search(url):
        return {"category": "reject", "confidence": 0.8, "source": "url:utility", "title": title}
    for h, p in _PAPER_HOST_PATH:
        if host.endswith(h) and p in (urlparse(url).path.lower()):
            return {"category": "articles", "confidence": 0.85, "source": "url:paper_path", "title": title}
    # A walled page (CF "Just a moment") on a host whose content is academic /
    # science-news by definition keeps that category — a blocked body must NOT
    # demote a real paper to UNSURE (that's how real papers got thrown out).
    if any(host.endswith(h) for h in _SCINEWS_HOSTS):
        return {"category": "science_news", "confidence": 0.7, "source": "url:scinews_host", "title": title}
    if any(host.endswith(h) for h in _ACADEMIC_HOSTS):
        return {"category": "articles", "confidence": 0.7, "source": "url:academic_host", "title": title}
    return {"category": None, "confidence": 0.0, "source": "blocked", "title": title}


# Hosts whose content is academic / science-news by definition — used to keep a
# walled (un-fetchable) page in the right bucket instead of demoting to UNSURE.
_ACADEMIC_HOSTS = (
    "arxiv.org", "philpapers.org", "ncbi.nlm.nih.gov", "pubmed.ncbi.nlm.nih.gov", "jstor.org",
    "nature.com", "science.org", "sciencedirect.com", "springer.com", "link.springer.com",
    "academic.oup.com", "biorxiv.org", "medrxiv.org", "pnas.org", "aeaweb.org", "pubs.aeaweb.org",
    "psycnet.apa.org", "ssrn.com", "papers.ssrn.com", "tandfonline.com", "onlinelibrary.wiley.com",
    "cambridge.org", "plato.stanford.edu", "iep.utm.edu", "semanticscholar.org", "researchgate.net",
    "ieeexplore.ieee.org", "dl.acm.org", "journals.aps.org", "iopscience.iop.org", "doi.org",
)
_SCINEWS_HOSTS = (
    "phys.org", "techxplore.com", "medicalxpress.com", "sciencedaily.com", "quantamagazine.org",
    "newscientist.com", "scientificamerican.com", "nautil.us", "aeon.co", "statnews.com",
    "arstechnica.com", "spectrum.ieee.org",
)
