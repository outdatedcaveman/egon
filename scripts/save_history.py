"""Save the fully-classified Chrome history to its destinations:
  articles/books/science_news -> Zotero (Panop tree) + bookmark
  content_longform            -> Instapaper + bookmark
  data_tools/references/shopping/opportunities/curios/study_work -> bookmark ONLY
Dedup vs existing Zotero; idempotent (resumable ledger); reversible (Zotero trash
/ Instapaper / bookmark backup). Chrome is usually running, so bookmarks are
delivered two Chrome-safe ways: (1) a Netscape HTML import file on the Desktop
(one click: Bookmarks > Import bookmarks > HTML) and (2) the Panop pending-
bookmark queue the extension drains via chrome.bookmarks. NEVER a raw file write
while Chrome runs (would be clobbered).
"""
from __future__ import annotations
import json, os, re, time, html
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
import requests

ROOT = Path(__file__).resolve().parents[1]
ST = ROOT / "state" / "panop"
CLASSIFIED = ST / "history_classified.json"
LEDGER = ST / "history_save_ledger.jsonl"
PENDING = ST / "panop_pending_bookmarks.json"
HTML_OUT = Path.home() / "Desktop" / "Panop_history_bookmarks_import.html"

ZCOL = {"articles": "GKSJSJMJ", "books": "B3XGDC4J", "science_news": "BRZ3UUIR"}
BFOLDER = {"articles": "Articles", "books": "Books", "science_news": "Science News",
           "content_longform": "Science Longform (read-in-place)", "data_tools": "Data & Tools",
           "references": "References", "shopping": "Shopping", "opportunities": "Opportunities",
           "curios": "Curios", "study_work": "Study & Work"}
SAVE = set(BFOLDER)
TRACK = {"utm_source","utm_medium","utm_campaign","utm_term","utm_content","fbclid","gclid","mc_cid",
         "mc_eid","igshid","_ga","ref","ref_src","yclid","msclkid","spm","share","shared","from","source",
         "_hsenc","_hsmi","gad_source","triedRedirect","open","token","r","isFreemail"}
_REDIR = ("/redirect/", "/url?", "/l.php", "t.co/", "bit.ly/", "lnkd.in/", "kit-mail", "click.",
          "/ss/c/", "hubspotlinks", "list-manage", "lm.facebook")


def _creds():
    pe = json.loads((ROOT / "panop_env.json").read_text(encoding="utf-8-sig"))
    env = {}
    for line in open(os.path.expanduser("~/Documents/Workspace/kms_auto_router/.env"), encoding="utf-8-sig"):
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1); env[k.strip()] = v.strip().strip('"').strip("'")
    return pe["zotero_api_key"], str(pe["zotero_user_id"]), env["INSTAPAPER_USERNAME"], env["INSTAPAPER_PASSWORD"]


def canon(u):
    try:
        p = urlparse(u); net = (p.netloc or "").lower()
        if net.startswith("m."): net = "www." + net[2:]
        path = (p.path or "").rstrip("/") or "/"
        qs = sorted((k, v) for k, v in parse_qsl(p.query) if k.lower() not in TRACK)
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
        return canon(fin if str(fin).startswith("http") else u)
    except Exception:
        return canon(u)


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", help="classified JSON {url:{category,title}} (default history_classified.json)")
    ap.add_argument("--ledger", help="alternate ledger file (default history_save_ledger.jsonl)")
    ap.add_argument("--dry", action="store_true", help="show the routing plan, write nothing")
    ARGS = ap.parse_args()
    src = Path(ARGS.input) if ARGS.input else CLASSIFIED
    global LEDGER
    if ARGS.ledger:
        LEDGER = Path(ARGS.ledger)

    zkey, zuid, iuser, ipass = _creds()
    ZH = {"Zotero-API-Key": zkey, "Zotero-API-Version": "3"}
    zbase = f"https://api.zotero.org/users/{zuid}"
    res = json.loads(src.read_text(encoding="utf-8"))
    items = [{"url": u, "title": v.get("title") or u, "cat": v["category"]}
             for u, v in res.items() if v["category"] in SAVE]

    done = set()
    if LEDGER.exists():
        for l in LEDGER.read_text(encoding="utf-8").splitlines():
            try: done.add(json.loads(l)["url"])
            except Exception: pass
    items = [it for it in items if it["url"] not in done]
    from collections import Counter
    dest = {"articles": "Zotero", "books": "Zotero", "science_news": "Zotero",
            "content_longform": "Instapaper"}
    plan = Counter(f"{i['cat']} -> {dest.get(i['cat'], 'bookmark')}" for i in items)
    print(f"to save: {len(items)} | {dict(Counter(i['cat'] for i in items))}")
    print(f"routing: {dict(plan)}")
    if ARGS.dry:
        print("DRY RUN — nothing written (pass without --dry to save; dedup applies at write).")
        return

    # resolve redirect-shaped urls
    print("resolving redirect URLs…")
    with ThreadPoolExecutor(max_workers=24) as ex:
        resolved = list(ex.map(lambda it: resolve(it["url"]), items))
    for it, r in zip(items, resolved):
        it["surl"] = r

    # existing Zotero URLs (dedup, the 3 zotero cats)
    existing = set()
    for ck in list(ZCOL.values()) + ["24A43HSI"]:
        start = 0
        while True:
            r = requests.get(f"{zbase}/collections/{ck}/items/top?limit=100&start={start}", headers=ZH, timeout=40)
            if r.status_code != 200: break
            b = r.json()
            if not b: break
            for it in b:
                uu = it.get("data", {}).get("url")
                if uu: existing.add(canon(uu))
            if len(b) < 100: break
            start += len(b)
    print(f"existing Zotero URLs: {len(existing)}")

    led = open(LEDGER, "a", encoding="utf-8")
    def trace(it, **kw):
        led.write(json.dumps({"url": it["url"], "surl": it["surl"], "cat": it["cat"], **kw}, ensure_ascii=False) + "\n")

    # 1) Zotero batches
    zbatch = [it for it in items if it["cat"] in ZCOL]
    seen = set(); zq = []
    for it in zbatch:
        su = it["surl"]
        if su in existing or su in seen:
            trace(it, zotero="dup"); continue
        seen.add(su)
        zq.append(it)
    z_ok = z_fail = 0
    for i in range(0, len(zq), 50):
        chunk = zq[i:i+50]
        payload = [{"itemType": "webpage", "title": it["title"][:250] or "Untitled", "url": it["surl"],
                    "date": datetime.now().strftime("%Y-%m-%d"), "tags": [{"tag": it["cat"]}],
                    "collections": [ZCOL[it["cat"]]]} for it in chunk]
        r = requests.post(f"{zbase}/items", headers={**ZH, "Content-Type": "application/json"}, data=json.dumps(payload), timeout=90)
        succ = set()
        if r.status_code in (200, 201):
            succ = {int(k) for k in (r.json().get("successful") or {}).keys()}
        for j, it in enumerate(chunk):
            ok = j in succ; z_ok += ok; z_fail += (not ok)
            trace(it, zotero=bool(ok))
        led.flush(); time.sleep(0.4)
    print(f"Zotero saved: {z_ok} (fail {z_fail})")

    # 2) Instapaper (content_longform)
    lf = [it for it in items if it["cat"] == "content_longform"]
    ip_ok = ip_fail = 0
    def ip_add(it):
        try:
            r = requests.post("https://www.instapaper.com/api/add", auth=(iuser, ipass),
                              data={"url": it["surl"], "title": it["title"][:300]}, timeout=20)
            return r.status_code in (200, 201)
        except Exception:
            return False
    with ThreadPoolExecutor(max_workers=4) as ex:
        for it, ok in zip(lf, ex.map(ip_add, lf)):
            ip_ok += ok; ip_fail += (not ok)
            trace(it, instapaper=bool(ok))
    print(f"Instapaper saved: {ip_ok} (fail {ip_fail})")

    # 3) bookmarks for ALL -> Netscape HTML (by folder) + pending queue
    bycat = {}
    for it in items:
        bycat.setdefault(it["cat"], []).append(it)
    lines = ['<!DOCTYPE NETSCAPE-Bookmark-file-1>', '<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">',
             '<TITLE>Bookmarks</TITLE>', '<H1>Bookmarks</H1>', '<DL><p>', '    <DT><H3>KMS Output</H3>', '    <DL><p>']
    for cat, its in bycat.items():
        lines.append(f'        <DT><H3>{html.escape(BFOLDER[cat])}</H3>')
        lines.append('        <DL><p>')
        for it in its:
            lines.append(f'            <DT><A HREF="{html.escape(it["surl"])}">{html.escape(it["title"][:300])}</A>')
        lines.append('        </DL><p>')
    lines += ['    </DL><p>', '</DL><p>']
    HTML_OUT.write_text("\n".join(lines), encoding="utf-8")

    # pending queue for the extension (chrome.bookmarks, Chrome-safe)
    q = []
    if PENDING.exists():
        try: q = json.loads(PENDING.read_text(encoding="utf-8"))
        except Exception: q = []
    have = {(x.get("url"), x.get("category")) for x in q}
    for it in items:
        k = (it["surl"], BFOLDER[it["cat"]])
        if k not in have:
            q.append({"url": it["surl"], "title": it["title"][:300], "category": BFOLDER[it["cat"]],
                      "parent": "KMS Output", "queued_at": datetime.now().isoformat()})
            have.add(k)
        trace(it, bookmark="queued")
    PENDING.write_text(json.dumps(q, ensure_ascii=False), encoding="utf-8")
    led.close()
    print(f"\nbookmarks: {len(items)} -> HTML {HTML_OUT}\n            + {len(q)} in extension queue")
    print("DONE. Import the HTML (Bookmarks > Import bookmarks and settings > HTML file) for the full tree,")
    print("or the extension drains the queue automatically while Chrome runs.")


if __name__ == "__main__":
    main()
