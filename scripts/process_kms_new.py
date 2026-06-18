"""Phase A of the KMS Output unification: take the NEW items in the (deduped)
KMS Output bookmark folders that aren't yet in the Zotero Panop pile, classify
them body-first, and write the classified file save_history consumes. Covers the
Zotero-bound categories (Articles/Books/Science News). Read-only on bookmarks.

  python scripts/process_kms_new.py
  -> state/panop/kms_new_classified.json
  then: python scripts/save_history.py --input state/panop/kms_new_classified.json \
            --ledger state/panop/kms_new_ledger.jsonl
"""
from __future__ import annotations
import sys, json, requests
from pathlib import Path
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lib.body_classify import classify_by_body, resolve_redirect

ST = ROOT / "state" / "panop"
TRACK = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "gclid"}
# KMS Output category folders that mirror Zotero collections
ZBOUND = {"Articles", "Books", "Science News"}
SAVEABLE = {"articles", "books", "science_news", "content_longform", "references",
            "data_tools", "shopping", "opportunities", "curios", "study_work"}


def canon(u):
    try:
        p = urlparse(u); net = (p.netloc or "").lower()
        if net.startswith("m."): net = "www." + net[2:]
        path = (p.path or "").rstrip("/") or "/"
        qs = sorted((k, v) for k, v in parse_qsl(p.query) if k.lower() not in TRACK)
        return urlunparse(((p.scheme or "https").lower(), net, path, "", urlencode(qs), ""))
    except Exception:
        return u


def kms_items():
    bm = json.loads(next(Path.home().glob("AppData/Local/Google/Chrome/User Data/Default/Bookmarks")).read_text(encoding="utf-8"))
    out = {}
    def walk(node, under_kms=False, cat=None):
        if node.get("type") == "folder":
            nm = node.get("name", "").strip()
            ik = under_kms or nm == "KMS Output"
            c = nm if (under_kms and nm in ZBOUND) else cat
            for ch in node.get("children", []):
                walk(ch, ik, c)
        elif node.get("type") == "url" and cat in ZBOUND:
            out.setdefault(canon(node.get("url", "")), (node.get("url", ""), node.get("name", "")))
    for root in bm["roots"].values():
        if isinstance(root, dict):
            walk(root)
    return out


def main():
    pe = json.loads((ROOT / "panop_env.json").read_text(encoding="utf-8-sig"))
    H = {"Zotero-API-Key": pe["zotero_api_key"], "Zotero-API-Version": "3"}
    base = f"https://api.zotero.org/users/{pe['zotero_user_id']}"
    zall = set()
    for ck in ["GKSJSJMJ", "B3XGDC4J", "BRZ3UUIR", "S2IP249A", "2DDCVMKV", "QR7WM9FE",
               "DSA4TSUE", "SGSRJA3F", "WBTQEC5J"]:
        start = 0
        while True:
            r = requests.get(f"{base}/collections/{ck}/items/top?limit=100&start={start}", headers=H, timeout=40)
            b = r.json()
            if not b:
                break
            for it in b:
                u = it.get("data", {}).get("url")
                if u:
                    zall.add(canon(u))
            if len(b) < 100:
                break
            start += len(b)

    kms = kms_items()
    new = {c: v for c, v in kms.items() if c not in zall}
    print(f"KMS Output (Zotero-bound) unique: {len(kms)} | already in Zotero: {len(kms)-len(new)} | NEW: {len(new)}", flush=True)

    def classify_one(item):
        url, title = item
        u = resolve_redirect(url) or url
        v = classify_by_body(u)
        cat = v.get("category")
        if cat in (None, "reject", "blocked", "needs_ai"):
            host = urlparse(u).netloc.lower()
            cat = "articles" if any(h in host for h in ("arxiv", "doi", "philpapers", "ncbi", "journal", "academic")) else None
        return url, {"category": cat, "title": v.get("title") or title or url} if cat in SAVEABLE else None

    out, done = {}, 0
    with ThreadPoolExecutor(max_workers=12) as ex:
        for f in as_completed([ex.submit(classify_one, v) for v in new.values()]):
            url, rec = f.result()
            if rec:
                out[url] = rec
            done += 1
            if done % 50 == 0:
                print(f"  classified {done}/{len(new)}", flush=True)
    from collections import Counter
    print(f"saveable: {len(out)} | {dict(Counter(r['category'] for r in out.values()))}", flush=True)
    (ST / "kms_new_classified.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"-> {ST/'kms_new_classified.json'}", flush=True)


if __name__ == "__main__":
    main()
