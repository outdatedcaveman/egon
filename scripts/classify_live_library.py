"""THOROUGH re-classification of the ENTIRE live Zotero Panop tree, body-first.
Bruno 2026-06-17: do the whole job once. Every item in every Panop subcollection
is re-judged by its page BODY (lib.body_classify: object-type + citation/book/
product + genre + journal-host fallback) to get its CORRECT category and a real
title. Output is a resumable checkpoint; apply_live_library.py then moves items
to the right collection, retitles the broken ones, and trashes true junk —
reversibly.

Resumable: appends one JSON line per item to state/panop/live_reclassify.jsonl;
re-running skips already-done keys. Concurrent + checkpointed so a crash never
loses work.

  python scripts/classify_live_library.py
"""
from __future__ import annotations
import sys, json, threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import requests
from lib.body_classify import classify_by_body, resolve_redirect

ST = ROOT / "state" / "panop"
CKPT = ST / "live_reclassify.jsonl"

# every Panop subcollection -> the category it currently represents
COLLS = {
    "GKSJSJMJ": "articles", "B3XGDC4J": "books", "BRZ3UUIR": "science_news",
    "S2IP249A": "content_longform", "2DDCVMKV": "references", "QR7WM9FE": "data_tools",
    "DSA4TSUE": "curios", "SGSRJA3F": "opportunities", "WBTQEC5J": "shopping",
}


def load_all(H, base):
    items = {}
    for ck, cat in COLLS.items():
        start = 0
        while True:
            r = requests.get(f"{base}/collections/{ck}/items/top?limit=100&start={start}", headers=H, timeout=40)
            if r.status_code != 200 or not r.json():
                break
            b = r.json()
            for it in b:
                k = it["key"]; d = it.get("data", {})
                rec = items.setdefault(k, {"key": k, "version": it["version"],
                                           "title": d.get("title", "") or "", "url": d.get("url", "") or "",
                                           "collections": d.get("collections", []), "cur_cats": []})
                rec["cur_cats"].append(cat)
            if len(b) < 100:
                break
            start += len(b)
    return items


def main():
    pe = json.loads((ROOT / "panop_env.json").read_text(encoding="utf-8-sig"))
    H = {"Zotero-API-Key": pe["zotero_api_key"], "Zotero-API-Version": "3"}
    base = f"https://api.zotero.org/users/{pe['zotero_user_id']}"

    print("loading live Panop items…", flush=True)
    items = load_all(H, base)
    print(f"live items: {len(items)}", flush=True)

    done = set()
    if CKPT.exists():
        for line in CKPT.read_text(encoding="utf-8").splitlines():
            try:
                done.add(json.loads(line)["key"])
            except Exception:
                pass
    todo = [it for k, it in items.items() if k not in done and it["url"].startswith("http")]
    print(f"already done: {len(done)} | to classify: {len(todo)}", flush=True)

    lock = threading.Lock()
    fh = CKPT.open("a", encoding="utf-8")

    def work(it):
        url = resolve_redirect(it["url"]) or it["url"]
        v = classify_by_body(url)
        return {"key": it["key"], "url": it["url"], "surl": url,
                "old_title": it["title"], "cur_cats": it["cur_cats"],
                "new_cat": v.get("category"), "new_title": v.get("title") or "",
                "source": v.get("source"), "conf": v.get("confidence", 0.0)}

    n = 0
    with ThreadPoolExecutor(max_workers=12) as ex:
        futs = {ex.submit(work, it): it for it in todo}
        for f in as_completed(futs):
            try:
                rec = f.result()
            except Exception as e:
                it = futs[f]
                rec = {"key": it["key"], "url": it["url"], "old_title": it["title"],
                       "cur_cats": it["cur_cats"], "new_cat": None, "source": f"error:{type(e).__name__}"}
            with lock:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n"); fh.flush()
            n += 1
            if n % 200 == 0:
                print(f"  classified {n}/{len(todo)}", flush=True)
    fh.close()
    print(f"DONE — {n} newly classified, checkpoint {CKPT}", flush=True)


if __name__ == "__main__":
    main()
