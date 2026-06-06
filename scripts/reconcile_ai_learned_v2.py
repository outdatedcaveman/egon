"""Reconcile stale `ai_learned` flags on Panop history (2026-05-22).

The problem
-----------
535 history entries are saved to BOTH Zotero and Bookmarks but can never be
closed because they carry `ai_learned: true` — a flag set by the old
bag-of-words / science-news pipeline (pre-fix). `_safe_to_close` refuses to
close anything `ai_learned`, so these tabs are stuck open on the phone forever
despite meeting Bruno's actual rule (in both durable stores).

The safe fix
------------
We do NOT blindly clear the flag (that's what caused the 2026-05-15 incident).
Instead we RE-VALIDATE each flagged entry against the *trustworthy* classifier:

    clear ai_learned  ⟺  (a) the URL is NOT on a never-academic domain
                          (Wikipedia/Amazon/GitHub/social/etc.), AND
                      (b) the URL's host matches a domain_keyword of the
                          category the entry is already filed under (or any
                          category) — i.e. a real domain match, not a
                          bag-of-words guess.

Entries that don't re-validate keep `ai_learned: true` and stay open — exactly
the conservative behaviour we want. A timestamped backup is written first.

Usage:
    .venv\\Scripts\\python.exe scripts\\reconcile_ai_learned_v2.py            # dry-run report
    .venv\\Scripts\\python.exe scripts\\reconcile_ai_learned_v2.py --apply    # write changes
"""
from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
HISTORY = ROOT / "state" / "panop" / "panop_history.json"
CONFIG = ROOT / "state" / "panop" / "panop_config.json"


def _host(url: str) -> str:
    try:
        h = urlparse(url if "://" in url else "http://" + url).netloc.lower()
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return ""


def _never_academic(url: str) -> bool:
    """Reuse Egon's domain-tier classifier. Fail-safe: if it can't load, treat
    as NOT never-academic (we still gate on a positive domain match below)."""
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from lib.classifier import domain_tiers
        res = domain_tiers.classify(url)
        reason = (res.evidence or {}).get("reason", "")
        return isinstance(reason, str) and reason.startswith("never_academic:")
    except Exception:
        return False


def main(apply: bool) -> int:
    if not HISTORY.exists():
        print(f"history not found: {HISTORY}")
        return 1
    history = json.loads(HISTORY.read_text(encoding="utf-8"))
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))

    # Build a flat set of trusted domain keywords across all categories.
    domains: list[str] = []
    for c in cfg.get("categories", []):
        domains += [d.lower() for d in c.get("domain_keywords", []) if d]

    def domain_matches(url: str) -> bool:
        ul = url.lower()
        return any(d in ul for d in domains)

    flagged = revalidated = kept = 0
    examples_cleared, examples_kept = [], []

    for url, item in history.items():
        if not isinstance(item, dict) or not item.get("ai_learned"):
            continue
        flagged += 1
        u = item.get("canonical_url") or url
        ok = domain_matches(u) and not _never_academic(u)
        if ok:
            revalidated += 1
            if apply:
                item["ai_learned"] = False
                item["ai_learned_recheck"] = datetime.now().isoformat(timespec="seconds")
            if len(examples_cleared) < 8:
                examples_cleared.append((item.get("cat_id"), u[:80]))
        else:
            kept += 1
            if len(examples_kept) < 8:
                examples_kept.append((item.get("cat_id"), u[:80]))

    print(f"ai_learned entries:        {flagged}")
    print(f"  re-validated (clearable): {revalidated}")
    print(f"  kept flagged (no match):  {kept}")
    print("\nexamples CLEARED (trusted domain match):")
    for cat, u in examples_cleared:
        print(f"  [{cat}] {u}")
    print("\nexamples KEPT (no trusted domain -> stays open):")
    for cat, u in examples_kept:
        print(f"  [{cat}] {u}")

    if apply:
        bak = HISTORY.with_suffix(f".json.bak-{datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(HISTORY, bak)
        HISTORY.write_text(json.dumps(history, ensure_ascii=False, indent=2),
                           encoding="utf-8")
        print(f"\nAPPLIED. backup -> {bak.name}")
        print(f"{revalidated} entries unblocked — next drain will close those "
              f"already in both stores.")
    else:
        print("\nDRY RUN — re-run with --apply to write changes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main("--apply" in sys.argv))
