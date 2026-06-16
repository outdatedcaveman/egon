"""Remove the unambiguous junk wrongly saved from history (youtube/x/social/
login domains + search-result pages) from the Zotero Panop tree. Real articles
with Cloudflare 'Just a moment' titles are NOT touched (they're re-titled
elsewhere). Reversible: full backup + Zotero trash (deleted=1), per-item log.
"""
from __future__ import annotations
import json, time
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
import requests

ROOT = Path(__file__).resolve().parents[1]
BK = ROOT / "state" / "panop" / "backups"
TRACK = {"utm_source","utm_medium","utm_campaign","utm_term","utm_content","fbclid","gclid",
         "mc_cid","mc_eid","igshid","_ga","ref","ref_src","yclid","msclkid","spm","share","shared",
         "from","source","_hsenc","_hsmi","gad_source","triedRedirect","r","open"}
ZCOLS = ["GKSJSJMJ", "B3XGDC4J", "BRZ3UUIR", "24A43HSI"]


def canon(u):
    try:
        p = urlparse(u); net = (p.netloc or "").lower()
        if net.startswith("m."): net = "www." + net[2:]
        path = (p.path or "").rstrip("/") or "/"
        qs = sorted((k, v) for k, v in parse_qsl(p.query) if k.lower() not in TRACK)
        return urlunparse(((p.scheme or "https").lower(), net, path, "", urlencode(qs), ""))
    except Exception:
        return u


def main():
    import argparse
    ap = argparse.ArgumentParser(); ap.add_argument("--commit", action="store_true"); a = ap.parse_args()
    pe = json.loads((ROOT / "panop_env.json").read_text(encoding="utf-8-sig"))
    H = {"Zotero-API-Key": pe["zotero_api_key"], "Zotero-API-Version": "3"}
    base = f"https://api.zotero.org/users/{pe['zotero_user_id']}"

    junk = json.loads((ROOT / "state" / "panop" / "history_harddelete.json").read_text(encoding="utf-8"))
    junk_canon = {canon(j["url"]) for j in junk}
    print(f"junk urls to match: {len(junk_canon)}")

    # fetch Panop items, match by canon url
    targets = {}   # key -> (version, title, url)
    allitems = []
    for ck in ZCOLS:
        start = 0
        while True:
            r = requests.get(f"{base}/collections/{ck}/items/top?limit=100&start={start}", headers=H, timeout=40)
            if r.status_code != 200: break
            b = r.json()
            if not b: break
            for it in b:
                d = it.get("data", {}); u = d.get("url")
                allitems.append(it)
                if u and canon(u) in junk_canon:
                    targets[it["key"]] = it["version"]
            if len(b) < 100: break
            start += len(b)
    print(f"matched Zotero items to trash: {len(targets)}")

    BK.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    (BK / f"zotero_junk_purge_backup_{stamp}.json").write_text(
        json.dumps([it for it in allitems if it["key"] in targets], ensure_ascii=False), encoding="utf-8")

    if not a.commit:
        print("DRY RUN — pass --commit to trash (reversible).")
        return
    keys = list(targets)
    trashed = 0
    for i in range(0, len(keys), 50):
        chunk = keys[i:i+50]
        payload = [{"key": k, "version": targets[k], "deleted": 1} for k in chunk]
        r = requests.post(f"{base}/items", headers={**H, "Content-Type": "application/json"},
                          data=json.dumps(payload), timeout=60)
        if r.status_code in (200, 201):
            trashed += len((r.json().get("successful") or {}))
        time.sleep(0.4)
    print(f"TRASHED {trashed}/{len(keys)} junk items (recoverable in Zotero Trash).")


if __name__ == "__main__":
    main()
