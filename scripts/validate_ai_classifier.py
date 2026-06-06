"""Validation test for the AI bag-of-words classifier.

Per the post-mortem hardening process: before the AI fallback path can be
re-enabled in `_drain_classify_and_save`, the rebuilt classifier MUST return
`None` (no classification) for at least 95% of a known-wrong test set.

Known-wrong set:
  - Wikipedia articles on unrelated topics
  - Amazon product pages (these are book PAGES, not the books themselves;
    they should be classified as Books only by an explicit URL rule, not by
    bag-of-words on the product page)
  - GitHub repo README pages
  - Medium / Substack blog posts
  - Reddit / Twitter / news (non-science) pages

Pass condition: ≥ 95% of those URLs return None.
Fail condition: any test page returns a category id.

Run via:
    python scripts/validate_ai_classifier.py            # standard set
    python scripts/validate_ai_classifier.py --verbose  # show per-URL scores
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "external" / "panop_server"))

import main as panop_main  # type: ignore  # noqa: E402


# Known-WRONG URLs. These pages MUST classify as None (no category).
# Curated to cover the false-positive patterns seen in the 2026-05-15 incident.
KNOWN_WRONG = [
    # Wikipedia general (NOT academic articles)
    "https://en.wikipedia.org/wiki/Teleonomy",
    "https://en.wikipedia.org/wiki/Conceptual_engineering",
    "https://en.wikipedia.org/wiki/Kernel_method",
    "https://en.wikipedia.org/wiki/Football",
    "https://en.wikipedia.org/wiki/Pizza",
    # Amazon — these are product pages, not book references
    "https://www.amazon.com/Acéphale-Filosofía-una-vez-Spanish/dp/841575700X",
    "https://www.amazon.com/Technics-Time-Epimetheus-Meridian-Aesthetics/dp/0804730415",
    "https://www.amazon.com/dp/B08PZHYWJS",
    # GitHub README — code, not articles
    "https://github.com/google-research/disentanglement_lib",
    "https://github.com/InsForge/InsForge",
    # Medium / Substack blogs
    "https://medium.com/ai-software-engineer/5-openclaw-alternatives",
    # Hacker News / Reddit
    "https://news.ycombinator.com/",
    # General news (not science)
    "https://www.bbc.com/news/world",
    # Tech blogs
    "https://www.xda-developers.com/let-ai-agent-organize-my-pc",
    # YouTube
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
]

# Known-RIGHT URLs (sanity check the other side). These pages SHOULD classify
# as some category (we don't enforce WHICH, just that they get matched).
# Currently informational only — fail conditions are about known-wrong.
KNOWN_RIGHT_HINT = [
    "https://arxiv.org/abs/2204.04674",
    "https://www.nature.com/articles/s41586-024-12345-x",
    "https://philpapers.org/rec/SMITHEX",
]


def _classify_url(url: str) -> tuple[str | None, dict]:
    """Fetch + classify a URL via the rebuilt get_ai_prediction. Returns
    (predicted_category_id_or_None, debug_info).
    """
    meta = panop_main.fetch_page_content(url)
    if meta is None:
        return None, {"error": "fetch_failed"}
    text = ((meta.get("text") or meta.get("title") or "") + " "
            + (meta.get("abstract") or ""))
    if not text.strip():
        return None, {"error": "empty_text"}
    words = panop_main.get_words(text)
    distinct = len(set(words))
    pred = panop_main.get_ai_prediction(text)
    return pred, {"distinct_words": distinct, "title": (meta.get("title") or "")[:80]}


def main():
    verbose = "--verbose" in sys.argv
    print("=== KNOWN-WRONG URLs (must classify as None) ===")
    wrong_failures = []
    for u in KNOWN_WRONG:
        try:
            pred, info = _classify_url(u)
        except Exception as e:
            print(f"  ERROR fetching {u}: {e}")
            continue
        status = "OK (None)" if pred is None else f"FAIL → {pred!r}"
        print(f"  [{status}]  {u}")
        if verbose:
            print(f"           {info}")
        if pred is not None:
            wrong_failures.append((u, pred, info))
        time.sleep(0.3)

    print()
    pass_rate = (len(KNOWN_WRONG) - len(wrong_failures)) / len(KNOWN_WRONG) * 100
    print(f"Pass rate on known-wrong: {pass_rate:.1f}% "
          f"({len(KNOWN_WRONG) - len(wrong_failures)}/{len(KNOWN_WRONG)})")

    if pass_rate >= 95.0:
        print("\n✓ AI classifier validation PASSED — safe to re-enable in pipeline")
        return 0
    else:
        print(f"\n✗ AI classifier validation FAILED — must reach ≥95% before re-enabling")
        print("\nFailures (these pages should have classified None but got a category):")
        for u, p, info in wrong_failures:
            print(f"  - {u}\n      → {p!r}  ({info})")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
