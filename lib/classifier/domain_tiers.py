"""Domain reputation tier classifier — Layer 1 (and final-fallback floor).

Loads a hand-curated JSON of always/never/context-dependent domains and
returns either an authoritative match or an authoritative reject. The
never-academic list short-circuits everything else.

Edit `egon/state/classifier/domain_tiers.json` to change the lists.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from . import ClassificationResult

CONFIG_PATH = Path(__file__).resolve().parents[2] / "state" / "classifier" / "domain_tiers.json"


@lru_cache(maxsize=1)
def _load() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _host(url: str) -> str:
    try:
        h = (urlparse(url).hostname or "").lower()
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return ""


def _host_matches(host: str, candidates: list[str]) -> bool:
    """Match host exactly or as a subdomain of any candidate."""
    if not host: return False
    host = host[4:] if host.startswith("www.") else host
    for c in candidates:
        c = c.lower()
        c = c[4:] if c.startswith("www.") else c
        if host == c: return True
        if host.endswith("." + c): return True
    return False


def classify(url: str) -> ClassificationResult:
    """Returns:
        REJECT (action=abstain, layer=domain_tier, evidence.reason=never)
            for hosts on never_academic
        MATCH (high confidence) for hosts on always_* lists
        ABSTAIN (layer=abstain) for everything else, signaling "let later
            layers try"
    """
    cfg = _load()
    if not cfg:
        return ClassificationResult.abstain(layer="domain_tier", reason="no_config")
    host = _host(url)
    if not host:
        return ClassificationResult.abstain(layer="domain_tier", reason="no_host")

    # 1. Never-academic: an authoritative ABSTAIN — caller should NOT pass
    # this URL down the chain to other classifier paths.
    if _host_matches(host, cfg.get("never_academic", [])):
        return ClassificationResult.abstain(layer="domain_tier",
                                            reason=f"never_academic:{host}")

    # 2. Always-* lists: an authoritative MATCH with high confidence
    if _host_matches(host, cfg.get("always_science_longform", [])):
        return ClassificationResult.match("science_longform", confidence=0.98,
                                          layer="domain_tier", host=host,
                                          tier="always_science_longform")
    if _host_matches(host, cfg.get("always_science_news", [])):
        return ClassificationResult.match("science_news", confidence=0.95,
                                          layer="domain_tier", host=host,
                                          tier="always_science_news")
    if _host_matches(host, cfg.get("always_academic_articles", [])):
        return ClassificationResult.match("articles", confidence=0.98,
                                          layer="domain_tier", host=host,
                                          tier="always_academic_articles")
    if _host_matches(host, cfg.get("always_books", [])):
        return ClassificationResult.match("books", confidence=0.95,
                                          layer="domain_tier", host=host,
                                          tier="always_books")

    # 3. Context-dependent → abstain, let other layers try
    return ClassificationResult.abstain(layer="domain_tier",
                                        reason=f"context_dependent:{host}")
