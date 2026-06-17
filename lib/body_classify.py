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


from urllib.parse import parse_qsl, urlencode, unquote

# Redirect wrappers — the platform is NOT the content; the real link sits in a
# u=/url=/q= param. Bruno 2026-06-17: "beware redirect pages — facebook in
# particular; it's not the platform we want but the redirect links." Covers
# google.com/url, facebook l.php + flx/warn (the "leaving Facebook" interstitial),
# and the l./lm. mobile redirectors.
_REDIRECT_HOST_HINT = ("facebook.com", "l.facebook.com", "lm.facebook.com", "l.instagram.com",
                       "out.reddit.com", "href.li", "www.google.com", "google.com")
_REDIRECT_PATH_HINT = ("/l.php", "/flx/warn", "/url", "/away")
_TRACK_PARAMS = {"fbclid", "gclid", "utm_source", "utm_medium", "utm_campaign", "utm_term",
                 "utm_content", "mc_cid", "mc_eid", "igshid", "ref_src", "yclid", "msclkid",
                 "_hsenc", "_hsmi", "gad_source", "triedRedirect"}


def _clean_target(t):
    try:
        p = urlparse(t)
        qs = [(k, v) for k, v in parse_qsl(p.query) if k.lower() not in _TRACK_PARAMS]
        return p._replace(query=urlencode(qs)).geturl()
    except Exception:
        return t


def resolve_redirect(url):
    """If `url` is a known redirect wrapper, return its real (cleaned) target;
    else None. Used so a Facebook/Google interstitial is saved as the article it
    points to, not as the platform."""
    try:
        p = urlparse(url)
        host, path = (p.netloc or "").lower(), (p.path or "").lower()
        looks = (any(host.endswith(h) for h in _REDIRECT_HOST_HINT)
                 and any(h in path for h in _REDIRECT_PATH_HINT))
        q = dict(parse_qsl(p.query))
        for key in ("u", "url", "q"):
            v = q.get(key)
            if v and (looks or v.startswith("http")):
                tgt = unquote(v)
                if tgt.startswith("http") and urlparse(tgt).netloc.lower() != host:
                    return _clean_target(tgt)
    except Exception:
        pass
    return None


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


_HF_RESERVED = {"blog", "docs", "models", "organizations", "settings", "join", "login",
                "pricing", "tasks", "learn", "chat", "", "papers", "datasets", "spaces"}
_REF_HOSTS = ("plato.stanford.edu", "iep.utm.edu", "web.archive.org", "philpeople.org",
              "philpapers.org/profile", "hapoc.org")


def _object_type(url):
    """Deterministic object-type from URL structure (Bruno's taxonomy): the same
    host can hold different object kinds, so distinguish by path. Returns
    (category, confidence, source) or None. Works even when the body is walled."""
    p = urlparse(url); host = (p.netloc or "").lower(); path = (p.path or "").lower()
    segs = [s for s in path.split("/") if s]
    if host.endswith("huggingface.co"):
        if segs and segs[0] == "papers":
            return ("science_news", 0.8, "obj:hf_paper")          # announcement ABOUT a paper
        if segs and segs[0] in ("datasets", "spaces"):
            return ("data_tools", 0.85, f"obj:hf_{segs[0]}")
        if segs and segs[0] not in _HF_RESERVED:                  # {org}/{model} repo
            return ("data_tools", 0.8, "obj:hf_model")
    if (host.endswith("github.com") or host.endswith("gitlab.com")
            or host.endswith("bitbucket.org")) and len(segs) >= 2:
        return ("data_tools", 0.85, "obj:code_repo")
    if host.endswith("wikipedia.org") or any(h in host + path for h in _REF_HOSTS):
        return ("references", 0.85, "obj:reference_host")
    if host.endswith("play.google.com") and "/store/books" in path:
        return ("books", 0.85, "obj:play_books")
    if host.endswith("books.google.com") or "bbm.usp.br" in host:
        return ("books", 0.8, "obj:book_host")
    return None


def _body_genre(html, text, host, tl, path, ogtype):
    """Genre from body when no strong object signal — conservative (only fires on
    reliable markers); the trained kNN + AI handle the fuzzy middle."""
    sn = _meta(html, "og:site_name").lower()
    if "encyclopedia" in sn or "encyclopedia" in tl or tl.endswith("wikipedia") or "- wiki" in tl:
        return ("references", 0.6, "genre:encyclopedic")
    if re.search(r"pip install|npm install|git clone|conda install|cargo add|docker run|\$ pip", html, re.I):
        return ("data_tools", 0.6, "genre:install_cmd")
    if ("/news/" in path or "/press" in path or _meta(html, "article:section").lower() in ("news", "press")):
        if ogtype == "article" or len(text) > 400:
            return ("science_news", 0.6, "genre:news_section")
    # authored long-read on a non-academic, non-news host -> content_longform
    if (ogtype == "article" and len(text) > 2500
            and (_meta(html, "article:author") or _meta(html, "author"))
            and not any(host.endswith(h) for h in _ACADEMIC_HOSTS)):
        return ("content_longform", 0.55, "genre:authored_longread")
    return None


def classify_by_body(url, want_text=False):
    """Return {category, confidence, source, title, abstract?, reason}. category
    may be None with source='needs_ai' (real body, ambiguous) or 'blocked'."""
    host = (urlparse(url).netloc or "").lower()
    path = (urlparse(url).path or "").lower()
    ot = _object_type(url)
    status, html = _fetch(url)
    title_m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    title = re.sub(r"\s+", " ", (title_m.group(1) if title_m else "")).strip()
    tl = title.lower()
    blocked = (status in (403, 429, 503) or not html
               or "just a moment" in tl or "request limit" in tl or "are you a robot" in tl
               or "checking your browser" in tl)

    if not blocked:
        # ── strong signals ────────────────────────────────────────────────
        # Object-type wins FIRST: it only fires for hosts where Bruno's category
        # is definitive regardless of page markup — SEP/Wikipedia carry citation
        # meta but are references (consult), not articles; an hf paper-page is
        # science_news not a paper; a repo is data_tools.
        if ot:
            return {"category": ot[0], "confidence": ot[1], "source": ot[2], "title": title}
        # citation meta = a real paper (for every other scholarly host).
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
        # genre from body (encyclopedic / install-guide / news-section / long-read)
        g = _body_genre(html, text, host, tl, path, ogtype)
        if g:
            return {"category": g[0], "confidence": g[1], "source": g[2], "title": title,
                    "abstract": _meta(html, "description", "og:description")}
        # real body but ambiguous → hand to AI with extracted content
        out = {"category": None, "confidence": 0.0, "source": "needs_ai", "title": title,
               "abstract": _meta(html, "description", "og:description")}
        if want_text:
            out["text"] = text[:4000]
        return out

    # ── blocked: fall back to URL structure ───────────────────────────────
    if _UTILITY_RE.search(url):
        return {"category": "reject", "confidence": 0.8, "source": "url:utility", "title": title}
    if ot:                                  # deterministic object-type survives a wall
        return {"category": ot[0], "confidence": ot[1], "source": ot[2], "title": title}
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
    # journals/preprints/archives surfaced from history (often CF-walled), 2026-06-17
    "philarchive.org", "cell.com", "journals.sagepub.com", "worldscientific.com",
    "journals.uchicago.edu", "siam.org", "epubs.siam.org", "jneurosci.org", "direct.mit.edu",
    "pubsonline.informs.org", "academia.edu", "quod.lib.umich.edu", "journals.asm.org",
    "pubs.acs.org", "authorea.com", "thelancet.com", "jamanetwork.com", "guilfordjournals.com",
    "cacm.acm.org", "link.aps.org", "nejm.org", "jov.arvojournals.org", "publications.aaahq.org",
    "escholarship.org", "openreview.net", "ui.adsabs.harvard.edu", "scienceconnect.io",
)
_SCINEWS_HOSTS = (
    "phys.org", "techxplore.com", "medicalxpress.com", "sciencedaily.com", "quantamagazine.org",
    "newscientist.com", "scientificamerican.com", "nautil.us", "aeon.co", "statnews.com",
    "arstechnica.com", "spectrum.ieee.org",
)
