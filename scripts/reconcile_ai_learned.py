"""Reconcile the ~1,227 ai_learned history entries from the 2026-05-15 incident.

Each was classified by the faulty bag-of-words AI fallback, then had its Zotero
copy trashed. Many are GENUINE articles the old AI happened to get right; the
rest are junk (Wikipedia, Amazon, GitHub, ...).

This script re-classifies each one with the NEW validated layered classifier:

  - new classifier MATCHES (domain_tier / hard_gate, high confidence)
        → genuine. On --commit: re-push to Zotero clean, clear ai_learned,
          set the correct category. Now _safe_to_close will allow closing it.
  - new classifier ABSTAINS (never_academic OR just no signal)
        → leave it. ai_learned stays True, tab stays open on phone.

DRY RUN by default — reports the bucket breakdown. Pass --commit to act.

Only domain_tier + hard_gate (URL-pattern) signals are used here — no page
fetch — so it's fast and deterministic. Entries that need a fetch to decide
are left for the normal drain pipeline.
"""
from __future__ import annotations

import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HISTORY = ROOT / "state" / "panop" / "panop_history.json"
LOG = ROOT / "logs" / "reconcile-2026-05-16.log"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "external" / "panop_server"))
from lib import classifier, secrets   # noqa: E402
import main as panop_main             # noqa: E402

panop_main.ENV_FILE = str(ROOT / "external" / "panop_server" / "panop_env.json")
_real_get_env = panop_main.get_env
def _env_with_creds():
    e = _real_get_env()
    e["zotero_api_key"] = secrets.get("zotero.api_key", "") or ""
    e["zotero_user_id"] = secrets.get("zotero.user_id", "") or ""
    return e
panop_main.get_env = _env_with_creds


def _log(**kw):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(kw, ensure_ascii=False) + "\n")


def main():
    commit = "--commit" in sys.argv
    print(f"Mode: {'COMMIT' if commit else 'DRY RUN'}")

    h = json.loads(HISTORY.read_text(encoding="utf-8"))
    ai_entries = [(u, it) for u, it in h.items() if it.get("ai_learned")]
    print(f"ai_learned entries to reconcile: {len(ai_entries)}\n")

    confirmed = []     # (url, item, new_category)
    junk = []          # (url, item, reason)
    uncertain = []     # (url, item)

    for url, it in ai_entries:
        res = classifier.classify(url, page_meta={})
        if res.action == "match":
            confirmed.append((url, it, res.category))
        elif res.action == "abstain":
            reason = (res.evidence or {}).get("reason", "")
            if isinstance(reason, str) and reason.startswith("never_academic:"):
                junk.append((url, it, reason))
            else:
                uncertain.append((url, it))
        else:  # review
            uncertain.append((url, it))

    print(f"=== Reconciliation buckets ===")
    print(f"  CONFIRMED genuine (will reconcile): {len(confirmed)}")
    cat_break = Counter(c for _, _, c in confirmed)
    for c, n in cat_break.most_common():
        print(f"      {c}: {n}")
    print(f"  JUNK (never-academic, leave open): {len(junk)}")
    print(f"  UNCERTAIN (no URL signal, leave for drain): {len(uncertain)}")
    print()

    # show samples
    print("Sample CONFIRMED (first 8):")
    for u, it, c in confirmed[:8]:
        print(f"  [{c}] {u[:85]}")
    print("\nSample JUNK (first 8):")
    for u, it, r in junk[:8]:
        print(f"  {u[:85]}")
    print()

    if not commit:
        print(f"DRY RUN — nothing changed. Re-run with --commit to reconcile {len(confirmed)} entries.")
        return 0

    # COMMIT: re-push confirmed entries to Zotero, clear ai_learned
    _log(event="reconcile_start", confirmed=len(confirmed), junk=len(junk), uncertain=len(uncertain))
    print(f"Reconciling {len(confirmed)} confirmed entries…")
    fixed = failed = 0
    for i, (url, it, new_cat) in enumerate(confirmed):
        # Map category id -> display name
        cat_name = {"articles": "Articles", "books": "Books",
                    "science_news": "Science News",
                    "science_longform": "Science News"}.get(new_cat, "Articles")
        title = it.get("title") or url
        abstract = it.get("abstract") or ""
        doi = it.get("doi") or None
        try:
            z_ok = panop_main.send_to_zotero(url, title, abstract, cat_name, doi=doi)
            b_ok = panop_main.add_chrome_bookmark(url, title, cat_name)
            it["ai_learned"] = False
            it["category"] = cat_name
            it["cat_id"] = new_cat if new_cat != "science_longform" else "science_news"
            it["z_synced"] = bool(z_ok)
            it["b_synced"] = bool(b_ok)
            it["reconciled"] = "2026-05-16"
            if z_ok and b_ok:
                fixed += 1
            else:
                failed += 1
        except Exception as e:
            failed += 1
            _log(event="reconcile_error", url=url[:120], error=str(e)[:200])
        if (i + 1) % 50 == 0:
            HISTORY.write_text(json.dumps(h, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"  [{i+1}/{len(confirmed)}] fixed={fixed} failed={failed}")
        time.sleep(0.15)

    HISTORY.write_text(json.dumps(h, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nDONE: reconciled={fixed} failed={failed}")
    print(f"  {len(junk)} junk entries left flagged (tabs stay open)")
    print(f"  {len(uncertain)} uncertain entries left for the drain to handle")
    _log(event="reconcile_done", fixed=fixed, failed=failed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
