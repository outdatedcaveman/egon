"""Phase 2 of the phone sweep: SAVE the classified tabs to their destinations,
mirrored to BOTH Zotero/Instapaper AND the Chrome 'Panop' bookmarks folder.

Rules (Bruno 2026-06-14):
  • articles/books/science_news -> Zotero (Panop/<col>) + bookmark
  • science_longform            -> Instapaper + bookmark
  • reject / unclassified       -> NOTHING (never saved, never closed)
  • A tab is only marked closeable after BOTH destinations confirm.
  • Safety: backup + per-item trace ledger; idempotent (resumable).
Closing happens in a separate step and ONLY for closeable tabs.
"""
from __future__ import annotations
import json, os, re, time, shutil
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
import requests

ROOT = Path(__file__).resolve().parents[1]
ST = ROOT / "state" / "panop"
BK = ST / "backups"
LEDGER = ST / "phone_save_ledger.jsonl"
CLOSEABLE = ST / "phone_closeable.json"
MASTER = json.loads((ST / "phone_master_verdicts.json").read_text(encoding="utf-8"))

ZCOL = {"articles": "GKSJSJMJ", "books": "B3XGDC4J", "science_news": "BRZ3UUIR"}
BFOLDER = {"articles": "Articles", "books": "Books", "science_news": "Science News",
           "science_longform": "Science Longform (read-in-place)"}
TRACK = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid",
         "gclid", "mc_cid", "mc_eid", "igshid", "_ga", "ref", "ref_src", "yclid", "msclkid",
         "spm", "share", "shared", "from", "source", "_hsenc", "_hsmi", "gad_source",
         "triedRedirect", "open", "publication_id", "post_id", "smid", "smtyp", "_gl"}
_REDIR = ("/redirect/", "/url?", "/l.php", "t.co/", "bit.ly/", "lnkd.in/", "kit-mail",
          "click.", "email.", "tracking.", "/ss/c/", "brevo", "hubspotlinks", "list-manage")


def _creds():
    env = {}
    for line in open(os.path.expanduser("~/Documents/Workspace/kms_auto_router/.env"), encoding="utf-8-sig"):
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1); env[k.strip()] = v.strip().strip('"').strip("'")
    pe = json.loads((ROOT / "panop_env.json").read_text(encoding="utf-8-sig"))
    return pe["zotero_api_key"], str(pe["zotero_user_id"]), env["INSTAPAPER_USERNAME"], env["INSTAPAPER_PASSWORD"]


def canon(u):
    try:
        p = urlparse(u)
        net = (p.netloc or "").lower()
        if net.startswith("m."): net = "www." + net[2:]
        path = (p.path or "").rstrip("/") or "/"
        qs = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=False) if k.lower() not in TRACK]
        qs.sort()
        return urlunparse(((p.scheme or "https").lower(), net, path, "", urlencode(qs), ""))
    except Exception:
        return u


def resolve(u):
    if not any(s in u.lower() for s in _REDIR):
        return canon(u)
    try:
        r = requests.head(u, allow_redirects=True, timeout=6)
        fin = getattr(r, "url", None) or u
        if r.status_code in (403, 405, 400):
            r = requests.get(u, allow_redirects=True, timeout=8, stream=True); fin = getattr(r, "url", None) or u
        return canon(fin if fin.startswith("http") else u)
    except Exception:
        return canon(u)


# ── bookmark direct write (Chrome must be closed) ───────────────────────────
import uuid
def _bm_stamp(): return str(int(time.time() * 1000000))
def _guid(): return str(uuid.uuid4())

def write_bookmark(data, url, title, folder):
    other = data.setdefault("roots", {}).setdefault("other", {})
    other.setdefault("children", [])
    panop = next((c for c in other["children"] if c.get("type") == "folder" and c.get("name") == "Panop"), None)
    if not panop:
        panop = {"children": [], "date_added": _bm_stamp(), "date_last_used": "0", "guid": _guid(), "name": "Panop", "type": "folder"}
        other["children"].append(panop)
    cat = next((c for c in panop["children"] if c.get("type") == "folder" and c.get("name", "").lower() == folder.lower()), None)
    if not cat:
        cat = {"children": [], "date_added": _bm_stamp(), "date_last_used": "0", "guid": _guid(), "name": folder, "type": "folder"}
        panop["children"].append(cat)
    if any(c.get("url") == url for c in cat.get("children", [])):
        return True
    cat["children"].append({"date_added": _bm_stamp(), "date_last_used": "0", "guid": _guid(),
                            "name": title or url, "type": "url", "url": url})
    return True


def main():
    import argparse
    ap = argparse.ArgumentParser(); ap.add_argument("--commit", action="store_true"); a = ap.parse_args()
    zkey, zuid, iuser, ipass = _creds()
    ZH = {"Zotero-API-Key": zkey, "Zotero-API-Version": "3", "Content-Type": "application/json"}
    zbase = f"https://api.zotero.org/users/{zuid}"

    # already-saved ledger (idempotency)
    done = set()
    if LEDGER.exists():
        for line in LEDGER.read_text(encoding="utf-8").splitlines():
            try: done.add(json.loads(line)["id"])
            except Exception: pass

    save_items = [(tid, m) for tid, m in MASTER.items()
                  if m.get("category") in ("articles", "books", "science_news", "science_longform")
                  and tid not in done and m.get("url")]
    from collections import Counter
    print(f"to save: {len(save_items)} (already done: {len(done)}) | {dict(Counter(m['category'] for _,m in save_items))}")
    if not a.commit:
        print("DRY RUN — pass --commit to save.")
        return

    # existing Zotero URLs (dedup) — fetch current Panop tree
    existing = set()
    for ck in list(ZCOL.values()) + ["24A43HSI"]:
        start = 0
        while True:
            r = requests.get(f"{zbase}/collections/{ck}/items/top?limit=100&start={start}&format=json", headers={"Zotero-API-Key": zkey, "Zotero-API-Version": "3"}, timeout=40)
            if r.status_code != 200: break
            b = r.json()
            if not b: break
            for it in b:
                u = it.get("data", {}).get("url")
                if u: existing.add(canon(u))
            if len(b) < 100: break
            start += len(b)
    print(f"existing Zotero URLs (dedup set): {len(existing)}")

    # backup Chrome Bookmarks file
    prof = os.path.join(os.environ["USERPROFILE"], "AppData", "Local", "Google", "Chrome", "User Data", "Default")
    bpath = os.path.join(prof, "Bookmarks")
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    if os.path.exists(bpath):
        shutil.copy(bpath, BK / f"chrome_bookmarks_backup_{stamp}.json")
    bdata = json.load(open(bpath, encoding="utf-8")) if os.path.exists(bpath) else {"roots": {"other": {"children": []}}}

    # resolve URLs concurrently
    urls = {tid: m["url"] for tid, m in save_items}
    print("resolving final URLs…")
    with ThreadPoolExecutor(max_workers=24) as ex:
        resolved = dict(zip(urls, ex.map(resolve, urls.values())))

    # build per-category Zotero batches + instapaper + bookmarks
    closeable = []
    zbatch = []   # (tid, item-json, category)
    led = open(LEDGER, "a", encoding="utf-8")
    seen_run = set()
    ip_ok = ip_fail = bm_ok = 0

    def trace(tid, **kw):
        led.write(json.dumps({"id": tid, "ts": datetime.now().isoformat(), **kw}, ensure_ascii=False) + "\n")

    # longform -> Instapaper + bookmark (sequential, rate-limited)
    for tid, m in save_items:
        if m["category"] != "science_longform": continue
        url = resolved.get(tid) or m["url"]
        if url in seen_run: continue
        seen_run.add(url)
        title = m.get("title") or url
        try:
            r = requests.post("https://www.instapaper.com/api/add", auth=(iuser, ipass), data={"url": url, "title": title[:300]}, timeout=20)
            iok = r.status_code in (200, 201)
        except Exception:
            iok = False
        bok = write_bookmark(bdata, url, title, BFOLDER["science_longform"])
        ip_ok += iok; ip_fail += (not iok); bm_ok += bok
        if iok and bok:
            closeable.append(tid)
        trace(tid, cat="science_longform", url=url, instapaper=iok, bookmark=bok, close=bool(iok and bok))
        time.sleep(0.15)

    # articles/books/science_news -> Zotero (batched) + bookmark
    pending_z = [(tid, m) for tid, m in save_items if m["category"] in ZCOL]
    for tid, m in pending_z:
        url = resolved.get(tid) or m["url"]
        title = m.get("title") or url
        bok = write_bookmark(bdata, url, title, BFOLDER[m["category"]])
        bm_ok += bok
        if url in existing or url in seen_run:
            # already in Zotero (or dup within run): bookmark ensured, count closeable
            if bok: closeable.append(tid)
            trace(tid, cat=m["category"], url=url, zotero="dup", bookmark=bok, close=bok)
            continue
        seen_run.add(url)
        zbatch.append((tid, m["category"], {"itemType": "webpage", "title": title or "Untitled", "url": url,
                       "date": datetime.now().strftime("%Y-%m-%d"), "tags": [{"tag": m["category"]}],
                       "collections": [ZCOL[m["category"]]]}, bok))

    # write bookmarks file once (atomic), Chrome is closed
    bdata.pop("checksum", None)
    tmp = bpath + ".panop.tmp"
    with open(tmp, "w", encoding="utf-8") as f: json.dump(bdata, f, ensure_ascii=False)
    os.replace(tmp, bpath)
    bak = bpath + ".bak"
    if os.path.exists(bak):
        try: os.remove(bak)
        except Exception: pass
    print(f"bookmarks written ({bm_ok} entries ensured)")

    # POST Zotero in batches of 50
    z_ok = z_fail = 0
    for i in range(0, len(zbatch), 50):
        chunk = zbatch[i:i+50]
        payload = [c[2] for c in chunk]
        r = requests.post(f"{zbase}/items", headers=ZH, data=json.dumps(payload), timeout=60)
        succ = set()
        if r.status_code in (200, 201):
            body = r.json()
            succ = {int(k) for k in (body.get("successful") or {}).keys()}
        for j, (tid, cat, item, bok) in enumerate(chunk):
            ok = j in succ
            z_ok += ok; z_fail += (not ok)
            if ok and bok: closeable.append(tid)
            trace(tid, cat=cat, url=item["url"], zotero=ok, bookmark=bok, close=bool(ok and bok))
        time.sleep(0.4)

    led.close()
    json.dump(sorted(set(closeable)), open(CLOSEABLE, "w"), ensure_ascii=False)
    print(f"\nZotero saved: {z_ok} (fail {z_fail}) | Instapaper: {ip_ok} (fail {ip_fail}) | bookmarks: {bm_ok}")
    print(f"CLOSEABLE (saved to BOTH): {len(set(closeable))}  -> {CLOSEABLE}")
    print("reject/unclassified/failures: LEFT OPEN on phone.")


if __name__ == "__main__":
    main()
