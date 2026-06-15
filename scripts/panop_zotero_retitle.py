"""Re-title the real articles that were saved with a broken title (domain-as-
title, 'Untitled', or a block page). Re-fetch each URL, extract the real <title>,
and PATCH the Zotero item — ONLY when a genuine title is recovered (junk/blocked
fetches leave the item untouched). Reversible: backup + trace ledger; Zotero
keeps full version history.
"""
from __future__ import annotations
import json, os, re, time, glob, html as _html
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
import requests

ROOT = Path(__file__).resolve().parents[1]
BK = ROOT / "state" / "panop" / "backups"
LEDGER = ROOT / "state" / "panop" / "zotero_retitle_ledger.jsonl"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"}

_JUNK = ("just a moment", "checking your browser", "checking your connection",
         "attention required", "are you a robot", "verify you are human", "recaptcha",
         "captcha", "access denied", "403 forbidden", "404 not found", "just a sec",
         "you have been blocked", "rate limited", "enable javascript", "page not found",
         "bot verification", "security check", "are you human", "error", "redirecting")
_PLACE = {"", "untitled", "(no title)", "no title", "document", "loading", "home", "redirect"}


def _creds():
    pe = json.loads((ROOT / "panop_env.json").read_text(encoding="utf-8-sig"))
    return pe["zotero_api_key"], str(pe["zotero_user_id"])


def _host(u):
    from urllib.parse import urlparse
    h = (urlparse(u or "").netloc or "").lower()
    return h[4:] if h.startswith("www.") else h


def is_good_title(t, url):
    t = re.sub(r"\s+", " ", (t or "").strip())
    if not t or t.lower() in _PLACE or len(t) < 8:
        return None
    tl = t.lower()
    for p in _JUNK:
        if p in tl:
            return None
    tn = tl[4:] if tl.startswith("www.") else tl
    if re.fullmatch(r"[a-z0-9][a-z0-9.\-]*\.[a-z]{2,}", tn) or tn == _host(url):
        return None
    return t[:250]


def fetch_title(url):
    try:
        r = requests.get(url, headers=UA, timeout=8, allow_redirects=True)
        if r.status_code >= 400:
            return None
        m = re.search(r"<title[^>]*>(.*?)</title>", r.text[:200000], re.I | re.S)
        if not m:
            # fall back to og:title
            m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\'](.*?)["\']', r.text[:200000], re.I)
        if not m:
            return None
        return is_good_title(_html.unescape(m.group(1)).strip(), url)
    except Exception:
        return None


def main():
    import argparse
    ap = argparse.ArgumentParser(); ap.add_argument("--commit", action="store_true"); a = ap.parse_args()
    key, uid = _creds()
    H = {"Zotero-API-Key": key, "Zotero-API-Version": "3"}
    base = f"https://api.zotero.org/users/{uid}"
    work = json.loads(Path(sorted(glob.glob(str(BK / "zotero_panop_retitle_*.json")))[-1]).read_text(encoding="utf-8"))

    done = set()
    if LEDGER.exists():
        for line in LEDGER.read_text(encoding="utf-8").splitlines():
            try: done.add(json.loads(line)["key"])
            except Exception: pass
    items = [(k, v) for k, v in work.items() if k not in done and v.get("url")]
    print(f"to retitle: {len(items)} (done: {len(done)})")

    print("fetching titles…")
    with ThreadPoolExecutor(max_workers=16) as ex:
        titles = list(ex.map(lambda kv: fetch_title(kv[1]["url"]), items))
    recovered = [(k, v, t) for (k, v), t in zip(items, titles) if t]
    print(f"recovered real titles: {len(recovered)} / {len(items)}")
    for k, v, t in recovered[:8]:
        print(f"   {v.get('title','')[:22]:22} -> {t[:60]}")
    if not a.commit:
        print("\nDRY RUN — pass --commit to PATCH Zotero titles.")
        return

    # need current versions to PATCH — batch-fetch
    keys = [k for k, _, _ in recovered]
    ver = {}
    for i in range(0, len(keys), 50):
        chunk = keys[i:i+50]
        r = requests.get(f"{base}/items?itemKey={','.join(chunk)}&limit=50", headers=H, timeout=40)
        if r.status_code == 200:
            for it in r.json():
                ver[it["key"]] = it["version"]
        time.sleep(0.2)

    led = open(LEDGER, "a", encoding="utf-8")
    patched = fail = 0
    for i in range(0, len(recovered), 50):
        chunk = [(k, v, t) for k, v, t in recovered[i:i+50] if k in ver]
        payload = [{"key": k, "version": ver[k], "title": t} for k, v, t in chunk]
        r = requests.post(f"{base}/items", headers={**H, "Content-Type": "application/json"},
                          data=json.dumps(payload), timeout=60)
        succ = set()
        if r.status_code in (200, 201):
            succ = {int(j) for j in (r.json().get("successful") or {}).keys()}
        for j, (k, v, t) in enumerate(chunk):
            ok = j in succ
            patched += ok; fail += (not ok)
            led.write(json.dumps({"key": k, "old": v.get("title"), "new": t,
                                  "ok": ok, "ts": datetime.now().isoformat()}, ensure_ascii=False) + "\n")
        led.flush(); time.sleep(0.4)
    led.close()
    print(f"\nPATCHED {patched} titles (fail {fail}). Originals in {LEDGER}; Zotero keeps version history.")


if __name__ == "__main__":
    main()
