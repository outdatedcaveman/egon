"""Backfill: push the 608 April Panop-history entries that have no Zotero/Bookmark
copy upstream (they were classified by an earlier Panop install before Zotero
API credentials were wired). Brings them into the current Panop > Articles/...
Zotero collection and the desktop Chrome bookmarks tree.

DRY RUN by default. Pass --commit to push.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_FILE = ROOT / "logs" / "backfill-april-2026-05-15.log"

# Import the vendored Panop module so we can call send_to_zotero + add_chrome_bookmark
sys.path.insert(0, str(ROOT / "external" / "panop_server"))
import main as panop_main  # type: ignore

# Wire env (Zotero creds, etc.) the same way panop_capture.py does
panop_main.ENV_FILE = str(ROOT / "external" / "panop_server" / "panop_env.json")
sys.path.insert(0, str(ROOT))
from lib import secrets   # noqa: E402

_real_get_env = panop_main.get_env
def _get_env_with_creds():
    e = _real_get_env()
    e["zotero_api_key"] = secrets.get("zotero.api_key", "") or ""
    e["zotero_user_id"] = secrets.get("zotero.user_id", "") or ""
    return e
panop_main.get_env = _get_env_with_creds


def _log(level: str, **kw):
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts": datetime.now().isoformat(timespec="seconds"), "level": level, **kw}
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def main():
    commit = "--commit" in sys.argv
    print(f"Mode: {'COMMIT' if commit else 'DRY RUN'}")

    history = panop_main.load_history()
    # Candidates: in history but unsynced to upstream
    candidates = [(u, it) for u, it in history.items()
                  if not it.get("z_synced") and not it.get("b_synced")
                  and it.get("category")]

    print(f"\nCandidates: {len(candidates)} entries unsynced to Zotero/Bookmarks")
    from collections import Counter
    by_cat = Counter(it.get("category", "?") for _, it in candidates)
    print(f"By category:")
    for c, n in by_cat.most_common():
        print(f"  {c:25}  {n}")

    if not commit:
        print(f"\nDRY RUN. Pass --commit to backfill {len(candidates)} entries.")
        return 0

    _log("info", event="backfill_start", count=len(candidates))
    print(f"\nBackfilling {len(candidates)} entries…")

    pushed = skipped = failed = 0
    for i, (url, it) in enumerate(candidates):
        title = it.get("title") or url
        abstract = it.get("abstract") or ""
        category = it.get("category")
        doi = it.get("doi") or None

        try:
            # send_to_zotero is idempotent — checks dedup cache
            z_ok = panop_main.send_to_zotero(url, title, abstract, category, doi=doi)
            b_ok = panop_main.add_chrome_bookmark(url, title, category)
            if z_ok:
                it["z_synced"] = True
            if b_ok:
                it["b_synced"] = True
            if z_ok and b_ok:
                pushed += 1
            else:
                failed += 1
                _log("warn", event="partial", url=url[:120], z=z_ok, b=b_ok)
        except Exception as e:
            failed += 1
            _log("warn", event="exception", url=url[:120], error=str(e)[:200])

        # Save history every 50 iterations
        if (i + 1) % 50 == 0:
            panop_main.save_history(history)
            print(f"  [{i+1}/{len(candidates)}] pushed={pushed} failed={failed}")
            _log("info", event="progress", done=i+1, pushed=pushed, failed=failed)

        time.sleep(0.15)  # be polite to Zotero API

    panop_main.save_history(history)
    print(f"\nDONE: pushed={pushed} failed={failed}")
    _log("info", event="done", pushed=pushed, failed=failed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
