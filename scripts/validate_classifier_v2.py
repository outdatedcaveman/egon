"""Validation harness for the new layered classifier.

For each test URL: fetch the page, run classifier.classify(), check that the
output matches the expected outcome.

Three buckets:
  KNOWN_WRONG    must end with action="abstain" — no MATCH allowed
  KNOWN_ARTICLES must end with action="match", category="articles"
  KNOWN_OTHER    must end with action="match" (any specific category we list)

Pass thresholds (tunable):
  ≥ 95% of KNOWN_WRONG should ABSTAIN (no auto-action on wrong-domain pages)
  ≥ 85% of KNOWN_ARTICLES should MATCH (some failures expected for paywalled
       sites that block scraping; the deciding layer is then domain_tier alone)
  ≥ 80% of KNOWN_OTHER should MATCH

Per-layer reporting helps tune thresholds:
  - How many wrongs caught by domain_tier (never list)
  - How many rights caught by hard_gates vs domain_tier vs embeddings
  - How many medium-confidence routed to "review" (acceptable, not failure)
"""
from __future__ import annotations

import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "external" / "panop_server"))

from lib import classifier  # noqa: E402
import main as panop_main  # noqa: E402

panop_main.ENV_FILE = str(ROOT / "external" / "panop_server" / "panop_env.json")


# ── Test corpus ────────────────────────────────────────────────────────────
# KNOWN_WRONG: URLs the classifier must NOT classify (action="abstain").
# Curated from the 2026-05-15 incident's false-positive patterns.
KNOWN_WRONG = [
    # Wikipedia general
    "https://en.wikipedia.org/wiki/Teleonomy",
    "https://en.wikipedia.org/wiki/Conceptual_engineering",
    "https://en.wikipedia.org/wiki/Kernel_method",
    "https://en.wikipedia.org/wiki/Football",
    "https://en.wikipedia.org/wiki/Pizza",
    "https://en.wikipedia.org/wiki/Linus%27s_law",
    "https://en.wikipedia.org/wiki/Heshen",
    "https://pt.wikipedia.org/wiki/Brasil",
    # Amazon product pages
    "https://www.amazon.com/Ac%C3%A9phale-Filosof%C3%ADa-una-vez-Spanish/dp/841575700X",
    "https://www.amazon.com/Technics-Time-Epimetheus-Meridian-Aesthetics/dp/0804730415",
    "https://www.amazon.com/dp/B08PZHYWJS",
    "https://www.amazon.com/Mythologies-Roland-Barthes/dp/0099529750",
    "https://www.amazon.com.br/Anti-Oedipus-Capitalism-Schizophrenia/dp/B01ABCDEF",
    # GitHub
    "https://github.com/google-research/disentanglement_lib",
    "https://github.com/InsForge/InsForge",
    "https://github.com/decoderesearch/SAELens",
    # Medium / Substack / blogs
    "https://medium.com/@hugolu87/openclaw-vs-claude-code-in-5-mins-1cf02124bc08",
    "https://blog.openai.com/some-blog-post",
    # Tech news (not science)
    "https://www.xda-developers.com/let-ai-agent-organize-my-pc",
    "https://www.theverge.com/some-tech-article",
    "https://arstechnica.com/gadgets/some-product-review",
    # General-purpose news
    "https://www.bbc.com/news/world",
    "https://www.cnn.com/some-news",
    "https://news.ycombinator.com/",
    # Social
    "https://twitter.com/elonmusk/status/123",
    "https://www.reddit.com/r/programming/comments/abc",
    "https://www.linkedin.com/in/someone",
    # Video / streaming
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://www.netflix.com/title/12345",
    # AI tools (NOT papers)
    "https://chat.openai.com/c/abc123",
    "https://claude.ai/chat/abc",
    "https://huggingface.co/google/gemma-2b",
    "https://paperswithcode.com/task/image-classification",
    # Stack family
    "https://stackoverflow.com/questions/12345",
    "https://superuser.com/questions/12345",
    # Search / utility
    "https://www.google.com/search?q=something",
    "https://translate.google.com/",
    "https://drive.google.com/file/d/abc",
    # Books-as-product (this is debatable — Amazon book pages might want
    # Books category, but per Bruno's reading, the obvious solution is to
    # let the user manually add commerce pages, not auto-classify)
    "https://www.amazon.com/Persuasive-Technology-Computers-Change-What/dp/0123740312",
    "https://store.steampowered.com/app/12345",
]

# KNOWN_ARTICLES: URLs that ARE academic papers. Classifier should match.
KNOWN_ARTICLES = [
    "https://arxiv.org/abs/2204.04674",
    "https://arxiv.org/abs/2310.06825",
    "https://arxiv.org/pdf/2204.04674.pdf",
    "https://www.biorxiv.org/content/10.1101/2024.01.01.001",
    "https://www.medrxiv.org/content/10.1101/2024.02.02.002",
    "https://philpapers.org/rec/SMITHEX",
    "https://www.nature.com/articles/s41586-024-12345-x",
    "https://www.science.org/doi/10.1126/science.abc",
    "https://www.cell.com/cell/fulltext/S0092-8674(24)00001-1",
    "https://pubmed.ncbi.nlm.nih.gov/12345678/",
    "https://pmc.ncbi.nlm.nih.gov/articles/PMC1234567/",
    "https://link.springer.com/article/10.1007/s12345-024-0123-4",
    "https://www.sciencedirect.com/science/article/pii/S0123456789012345",
    "https://onlinelibrary.wiley.com/doi/10.1002/abc.12345",
    "https://journals.plos.org/plosone/article?id=10.1371/journal.pone.1234567",
    "https://www.frontiersin.org/articles/10.3389/fnins.2024.12345/full",
    "https://www.pnas.org/doi/10.1073/pnas.2401234567",
    "https://www.thelancet.com/journals/lancet/article/PIIS0140-6736(24)00001-2/fulltext",
    "https://www.bmj.com/content/385/bmj.q123",
    "https://doi.org/10.1038/s41586-024-12345-x",
    "https://dx.doi.org/10.1126/science.abc123",
]

KNOWN_SCIENCE_NEWS = [
    "https://phys.org/news/2024-12-some-discovery.html",
    "https://www.sciencedaily.com/releases/2024/12/241201123456.htm",
    "https://www.psypost.org/2024/12/some-finding",
    "https://www.scientificamerican.com/article/some-news/",
    "https://www.newscientist.com/article/2401234-some-news/",
    "https://www.iflscience.com/some-science-news",
    "https://neurosciencenews.com/2024/some-news",
]

KNOWN_LONGFORM = [
    "https://www.quantamagazine.org/what-can-we-gain-by-losing-infinity-20260429/",
    "https://aeon.co/essays/some-essay",
    "https://nautil.us/some-piece",
]


def _classify_with_fetch(url: str) -> dict:
    """Fetch + classify a URL. Returns dict with result + timing + metadata signals."""
    t0 = time.time()
    try:
        meta = panop_main.fetch_page_content(url)
    except Exception as e:
        meta = None
    fetch_ms = int((time.time() - t0) * 1000)
    # Even when fetch fails we still run domain_tier + URL-pattern checks
    result = classifier.classify(url, page_meta=meta or {})
    return {
        "url": url,
        "category": result.category,
        "confidence": round(result.confidence, 3),
        "layer": result.layer,
        "action": result.action,
        "evidence": result.evidence,
        "fetch_ms": fetch_ms,
        "fetched_ok": meta is not None,
    }


def _run_bucket(name: str, urls: list, expected_action: str,
                expected_category: str | None = None) -> dict:
    """Run all URLs in a bucket, report stats."""
    print(f"\n=== {name} ({len(urls)} URLs) — expect action={expected_action}"
          f"{', category=' + expected_category if expected_category else ''} ===")
    results = []
    pass_count = 0
    for u in urls:
        r = _classify_with_fetch(u)
        ok = r["action"] == expected_action
        if expected_category and ok:
            ok = (r["category"] == expected_category) or (expected_action == "review")
        if ok: pass_count += 1
        status = "OK" if ok else "FAIL"
        print(f"  [{status}] action={r['action']:7} cat={r['category'] or '-':18} "
              f"layer={r['layer']:11} conf={r['confidence']:.2f}  {u[:70]}")
        results.append(r)
        time.sleep(0.25)
    pct = pass_count / len(urls) * 100 if urls else 0
    return {"name": name, "results": results, "pass": pass_count, "total": len(urls), "pct": pct}


def main():
    print(f"Classifier validation — {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Loaded modules: domain_tiers, hard_gates, embeddings")

    # Quick check whether embeddings layer is available
    from lib.classifier import embeddings as emb_mod
    has_model = emb_mod._load_model() is not None
    has_centroids = emb_mod._load_centroids()[0] is not None
    print(f"  embedding model available: {has_model}")
    print(f"  exemplar centroids built : {has_centroids}")

    buckets = [
        _run_bucket("KNOWN-WRONG",         KNOWN_WRONG,        "abstain"),
        _run_bucket("KNOWN-ARTICLES",      KNOWN_ARTICLES,     "match", "articles"),
        _run_bucket("KNOWN-SCIENCE-NEWS",  KNOWN_SCIENCE_NEWS, "match", "science_news"),
        _run_bucket("KNOWN-LONGFORM",      KNOWN_LONGFORM,     "match", "science_longform"),
    ]

    print(f"\n{'='*72}\nSummary:")
    for b in buckets:
        bar = "█" * int(b["pct"] / 5)
        print(f"  {b['name']:25} {b['pct']:5.1f}% ({b['pass']}/{b['total']})  {bar}")

    # Per-layer credit assignment for each bucket
    print(f"\nPer-bucket layer breakdown:")
    for b in buckets:
        layers = Counter(r["layer"] for r in b["results"])
        print(f"  {b['name']:25} layers: {dict(layers)}")

    # Pass/fail decision
    wrong = next(b for b in buckets if b["name"] == "KNOWN-WRONG")
    arts = next(b for b in buckets if b["name"] == "KNOWN-ARTICLES")
    print(f"\nGates:")
    print(f"  wrong rejection rate: {wrong['pct']:.1f}% (target ≥95%)  ←  PRIMARY safety gate")
    print(f"  articles match rate : {arts['pct']:.1f}% (target ≥85%)")
    primary_ok = wrong["pct"] >= 95.0
    print(f"\n  {'✓' if primary_ok else '✗'} Primary safety gate: "
          f"{'PASSED — classifier is safe to re-enable in drain pipeline' if primary_ok else 'FAILED — do NOT enable'}")
    return 0 if primary_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
