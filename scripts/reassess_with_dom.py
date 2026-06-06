"""Re-assess stuck tabs by reading their LIVE DOM from the phone's Chrome.

The 2026-05-15 incident left ~626 history entries flagged ai_learned=True and
frozen — never closed, never re-evaluated. Among them: ~94 Amazon book pages
wrongly treated as junk, plus genuine articles on domains not in our lists.

This tool:
  1. connects to the phone over wireless ADB
  2. enumerates every open tab (target id + url via DevTools)
  3. reads each tab's live DOM (title, meta, ISBN/DOI, Amazon book signals) —
     bypassing Cloudflare/Amazon anti-bot because the phone already rendered it
  4. classifies via the layered classifier WITH the DOM signals
  5. for entries the classifier now confidently MATCHES:
       - re-push to Zotero (clean) + Chrome bookmarks
       - update the history record: correct category, ai_learned=False
  6. leaves genuinely-unclassifiable tabs untouched

DRY RUN by default. --commit to write.
"""
from __future__ import annotations

import lib.silent_subprocess  # noqa: F401  — suppress console windows

import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "external" / "panop_server"))

from lib import classifier, secrets                       # noqa: E402
from lib.classifier import dom_reader                      # noqa: E402
from lib.adapters import panop_capture as pc               # noqa: E402
import main as panop_main                                  # noqa: E402

HISTORY = ROOT / "state" / "panop" / "panop_history.json"
LOG = ROOT / "logs" / "reassess-2026-05-17.log"
ADB = pc.ADB_EXE

panop_main.ENV_FILE = str(ROOT / "external" / "panop_server" / "panop_env.json")
_real_get_env = panop_main.get_env
def _env():
    e = _real_get_env()
    e["zotero_api_key"] = secrets.get("zotero.api_key", "") or ""
    e["zotero_user_id"] = secrets.get("zotero.user_id", "") or ""
    return e
panop_main.get_env = _env


def _log(**kw):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(kw, ensure_ascii=False) + "\n")


def main():
    commit = "--commit" in sys.argv
    print(f"Mode: {'COMMIT' if commit else 'DRY RUN'}")

    # 1. connect
    ok, target = pc._ensure_connected()
    if not ok:
        print(f"phone unreachable: {target}"); return 1
    print(f"phone connected: {target}")
    pc._wake_and_open_chrome(target)
    import subprocess
    subprocess.run([str(ADB), "-s", target, "forward", "--remove", "tcp:9222"], capture_output=True)
    subprocess.run([str(ADB), "-s", target, "forward", "tcp:9222",
                    "localabstract:chrome_devtools_remote"], capture_output=True)
    time.sleep(2)

    # 2. enumerate tabs
    import requests
    try:
        tabs = requests.get("http://127.0.0.1:9222/json/list", timeout=30).json()
    except Exception as e:
        print(f"devtools unreachable: {e}"); return 1
    pages = [t for t in tabs if t.get("type") == "page" and t.get("id")]
    print(f"tabs enumerated: {len(pages)}")

    # 3. DOM-read in batches
    h = json.loads(HISTORY.read_text(encoding="utf-8"))
    dom_by_tid = {}
    BATCH = 20
    for i in range(0, len(pages), BATCH):
        batch_ids = [t["id"] for t in pages[i:i+BATCH]]
        got = dom_reader.read_tabs(batch_ids, log_fn=lambda e, **k: _log(event=e, **k))
        dom_by_tid.update(got)
        print(f"  DOM-read {min(i+BATCH,len(pages))}/{len(pages)}  (cumulative {len(dom_by_tid)})")
        time.sleep(0.5)
    print(f"DOM read OK for {len(dom_by_tid)}/{len(pages)} tabs")

    # 4. classify each tab that's a stuck history entry
    buckets = Counter()
    to_fix = []   # (url, history_item, new_cat, dom)
    for t in pages:
        tid = t["id"]
        url = (t.get("url") or "").strip()
        dom = dom_by_tid.get(tid) or {}
        if dom.get("url"):
            url = dom["url"]  # the DOM's location.href is most accurate
        if not url or not url.startswith(("http://", "https://")):
            continue
        canon = panop_main.canonicalize_url(url) or url
        item = h.get(canon) or h.get(url)
        # Only re-assess STUCK entries (ai_learned) — leave clean ones alone
        if not item or not item.get("ai_learned"):
            buckets["not_stuck_or_unknown"] += 1
            continue
        res = classifier.classify(url, page_meta={"dom": dom})
        if res.action == "match":
            buckets[f"match:{res.category}"] += 1
            to_fix.append((canon if item is h.get(canon) else url, item, res.category, dom))
        elif res.action == "review":
            buckets["review"] += 1
        else:
            reason = (res.evidence or {}).get("reason", "")
            buckets["abstain:" + (reason.split(":")[0] if reason else "?")] += 1

    print(f"\n=== Re-assessment of stuck tabs ===")
    for k, n in buckets.most_common():
        print(f"  {k:32} {n}")
    print(f"\n  → {len(to_fix)} stuck entries can now be correctly classified")

    print("\nSample of newly-classified (first 12):")
    for url, item, cat, dom in to_fix[:12]:
        print(f"  [{cat}] {(dom.get('title') or item.get('title') or '')[:55]:55} {url[:60]}")

    if not commit:
        print(f"\nDRY RUN — re-run with --commit to fix {len(to_fix)} entries.")
        return 0

    # 5. commit: re-push + update history
    print(f"\nCommitting {len(to_fix)} fixes…")
    cat_name = {"articles": "Articles", "books": "Books",
                "science_news": "Science News", "science_longform": "Science News"}
    fixed = failed = 0
    for i, (key, item, cat, dom) in enumerate(to_fix):
        name = cat_name.get(cat, "Articles")
        title = dom.get("title") or item.get("title") or key
        abstract = (dom.get("meta") or {}).get("citation_abstract", "") \
                   or (dom.get("meta") or {}).get("description", "") or item.get("abstract", "")
        doi = dom.get("doi") or item.get("doi") or None
        try:
            z_ok = panop_main.send_to_zotero(key, title, abstract, name, doi=doi)
            b_ok = panop_main.add_chrome_bookmark(key, title, name)
            item["category"] = name
            item["cat_id"] = cat if cat != "science_longform" else "science_news"
            item["ai_learned"] = False
            item["z_synced"] = bool(z_ok)
            item["b_synced"] = bool(b_ok)
            item["title"] = title
            item["reassessed"] = "2026-05-17"
            if z_ok and b_ok: fixed += 1
            else: failed += 1
        except Exception as e:
            failed += 1
            _log(event="fix_error", url=key[:120], error=str(e)[:200])
        if (i+1) % 40 == 0:
            HISTORY.write_text(json.dumps(h, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"  [{i+1}/{len(to_fix)}] fixed={fixed} failed={failed}")
        time.sleep(0.15)

    HISTORY.write_text(json.dumps(h, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nDONE: fixed={fixed} failed={failed}")
    _log(event="reassess_done", fixed=fixed, failed=failed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
