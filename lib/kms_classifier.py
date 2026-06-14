"""KMS master classifier / router — the single content-type brain shared by
Panop/Inbox and Routster/Navigation.

Bruno 2026-06-14: categories are about the NATURE of the content, not the
domain. The domain lists in panop config are only hints for the common cases;
the classifier must correctly recognise EVERY instance of a category (and only
those) even on brand-new domains. So the decision is made by a strong LLM
(Claude) reading the URL + title + abstract/text, with deterministic signals
(.pdf, arXiv-id, ISBN, DOI) as priors. One module, every surface.

Categories (exact judgment):
  articles          scholarly primary literature — peer-reviewed journal
                    articles, preprints (arXiv/bioRxiv/SSRN/PhilArchive/…),
                    working papers, conference papers, theses/dissertations,
                    lecture & course notes, technical reports. A PDF of a paper
                    is almost always this. NOT journalism, NOT blogs, NOT news.
  books             a book — monograph, textbook, edited volume (publisher book
                    page, ISBN, Goodreads, Amazon book, Springer/MIT/OUP books).
  science_longform  substantive long-form informative essays & quality
                    journalism meant to be read in full (Quanta, Aeon, Nautilus,
                    Noema, New Yorker, Atlantic, Asterisk, Asimov Press,
                    longreads, technical essays like Wolfram writings). Ideas /
                    deep explainers — not breaking news, not academic papers.
  science_news      science news pieces / press releases / university & journal
                    news (phys.org, ScienceDaily, EurekAlert, ScitechDaily,
                    Neuroscience News, Eurekalert). SECOND STAGE: if the page is
                    an AGGREGATOR / digest / newsletter / roundup that links to
                    multiple primary items, do NOT keep the digest — its real
                    payload is the underlying article/book links (is_aggregator).
  reject            does NOT belong in the KMS: general/sports/politics news,
                    social media, forums, video, shopping/product (non-book),
                    homepages & landing pages, marketing, search results,
                    login/paywall/error/bot-block stubs, non-substantive
                    personal blogs, apps/tools.

Public API:
  classify(item)               -> verdict dict
  classify_batch(items)        -> list[verdict] (one LLM call per ~12 items)
  deterministic_signals(item)  -> dict of priors (pdf/arxiv/doi/isbn)
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

CATEGORIES = ("articles", "books", "science_longform", "science_news", "reject")
_DEFAULT_MODEL = os.environ.get("KMS_CLASSIFIER_MODEL", "claude-sonnet-4-6")

ROOT = Path(__file__).resolve().parent.parent


# ── credentials (shared sources) ────────────────────────────────────────────
def _load_key() -> str | None:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"]
    # mouseion config.toml [llm] api_key (the de-facto shared LLM key)
    cfg = Path(os.path.expanduser("~/.config/mouseion/config.toml"))
    try:
        for line in cfg.read_text(encoding="utf-8").splitlines():
            m = re.match(r'\s*api_key\s*=\s*"(sk-ant-[^"]+)"', line)
            if m:
                return m.group(1)
    except Exception:
        pass
    # egon-config.json
    try:
        d = json.loads((ROOT / "egon-config.json").read_text(encoding="utf-8-sig"))
        k = (d.get("llm") or {}).get("api_key") or d.get("anthropic_api_key")
        if k:
            return k
    except Exception:
        pass
    return None


_client = None
def _get_client():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic(api_key=_load_key())
    return _client


# ── deterministic priors ────────────────────────────────────────────────────
_ARXIV_RE = re.compile(r"arxiv\.org/(abs|pdf)/\d{4}\.\d{4,5}", re.I)
_DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.I)
_ISBN_RE = re.compile(r"\b97[89][\d-]{10,}\b")


def deterministic_signals(item: dict) -> dict:
    url = (item.get("url") or "").lower()
    blob = " ".join(str(item.get(k) or "") for k in ("url", "title", "abstract", "extra")).lower()
    return {
        "is_pdf": url.endswith(".pdf") or ".pdf?" in url or "/pdf/" in url,
        "is_arxiv": bool(_ARXIV_RE.search(url)),
        "has_doi": bool(_DOI_RE.search(blob)),
        "has_isbn": bool(_ISBN_RE.search(blob)),
    }


_SYSTEM = """You are the classification brain of Bruno's personal knowledge-management system (KMS). You decide, for each web item, which ONE category it belongs to by the NATURE of its content — NOT by its domain. Domains are only hints; you must correctly recognise every instance of a category even on unfamiliar sites.

CATEGORIES:
- "articles": scholarly PRIMARY literature. Peer-reviewed journal articles, preprints (arXiv, bioRxiv, medRxiv, SSRN, PhilArchive, PhilSci, OSF), working papers, conference papers, theses/dissertations, lecture/course notes, technical reports, formal monograph-style scholarship. A PDF that is an academic paper is almost always "articles". It is scholarship/research, authored for an academic audience with citations — NOT journalism, NOT a blog, NOT a news story.
- "books": a BOOK. Monograph, textbook, edited volume, or a book product/landing page (publisher book page, ISBN present, Goodreads, Amazon book page, Springer/MIT/OUP/Cambridge/Harvard book).
- "science_longform": substantive LONG-FORM informative essays and high-quality explanatory journalism meant to be read in full — ideas, deep explainers, intellectual essays (Quanta, Aeon, Nautilus, Noema, Asterisk, Asimov Press, New Yorker, Atlantic features, Longreads, MIT Press Reader, serious technical essays e.g. Stephen Wolfram's writings). Deep and informative, but NOT a peer-reviewed paper and NOT breaking news.
- "science_news": a science NEWS item or press release — a short-to-medium news story reporting a finding (phys.org, ScienceDaily, ScitechDaily, EurekAlert, Neuroscience News, PsyPost, university/journal news pages). ALSO set is_aggregator=true if the page is a DIGEST / newsletter / roundup / index that mainly links out to multiple separate primary items (its value is the links it contains, not the page itself).
- "reject": does NOT belong in the KMS. General news, sports, politics, business/markets, social media (X, Reddit, Facebook, YouTube), forums, shopping/products that aren't books, homepages or section landing pages, search-result pages, marketing/SaaS pages, login/paywall/cookie/error/bot-block ("Just a moment", "403 Forbidden", "Access denied"), and non-substantive personal blogs.

DECISION RULES:
- Judge by content type. A NewScientist or BBC piece is news → science_news only if it's a science finding story, else reject (e.g. BBC football = reject).
- A bare homepage / domain-only title with no article path = reject.
- When the title is a generic site name but the URL path is clearly a real article, infer from the URL slug.
- Prefer the most specific correct category. If genuinely none fit, "reject".
- Be decisive but never force a fit: it is WRONG to file sports news as "articles".

Return ONLY a JSON array, one object per input item, same order, each:
{"id": <int>, "category": "articles"|"books"|"science_longform"|"science_news"|"reject", "fit": <bool, false iff category=="reject">, "is_aggregator": <bool>, "confidence": <0..1>, "reason": "<≤12 words>"}"""


def _build_user(items: list[dict]) -> str:
    lines = []
    for i, it in enumerate(items):
        sig = deterministic_signals(it)
        flags = ",".join(k for k, v in sig.items() if v) or "-"
        title = (it.get("title") or "")[:200]
        url = (it.get("url") or "")[:300]
        ab = (it.get("abstract") or it.get("text") or "")[:280]
        lines.append(
            f"[{i}] title={title!r}\n"
            f"    url={url}\n"
            f"    signals={flags}\n"
            f"    abstract={ab!r}"
        )
    return "Classify these items:\n\n" + "\n\n".join(lines)


def classify_batch(items: list[dict], model: str = _DEFAULT_MODEL,
                   retries: int = 3) -> list[dict]:
    """Classify up to ~15 items in one LLM call. Falls back to per-item
    deterministic guess only if the LLM is unreachable."""
    if not items:
        return []
    client = _get_client()
    user = _build_user(items)
    last_err = None
    for attempt in range(retries):
        try:
            resp = client.messages.create(
                model=model, max_tokens=2000, system=_SYSTEM,
                messages=[{"role": "user", "content": user}],
            )
            txt = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
            arr = json.loads(_extract_json(txt))
            by_id = {int(o["id"]): o for o in arr if "id" in o}
            out = []
            for i, it in enumerate(items):
                o = by_id.get(i) or {"category": "reject", "fit": False,
                                     "is_aggregator": False, "confidence": 0.0,
                                     "reason": "no verdict returned"}
                o.setdefault("is_aggregator", False)
                o["fit"] = bool(o.get("category") and o["category"] != "reject")
                out.append(o)
            return out
        except Exception as e:
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    # last-resort deterministic fallback (never silently mislabels as fit)
    return [{"category": "reject", "fit": False, "is_aggregator": False,
             "confidence": 0.0, "reason": f"LLM unavailable: {str(last_err)[:40]}"}
            for _ in items]


def classify(item: dict, model: str = _DEFAULT_MODEL) -> dict:
    return classify_batch([item], model=model)[0]


def _extract_json(txt: str) -> str:
    s = txt.find("[")
    e = txt.rfind("]")
    if s >= 0 and e > s:
        return txt[s:e + 1]
    return txt
