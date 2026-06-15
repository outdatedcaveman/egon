"""Finish the phone sweep: close every tab that is verified-SAVED (to both
Zotero/Instapaper AND bookmarks) using its CURRENT DevTools id — the earlier
close failed for many because it used stale ids from the first snapshot. reject
/ unsaved tabs are never closed. Re-fetches the live list to map saved URLs ->
current ids, so it closes exactly the right tabs even after id drift.
"""
from __future__ import annotations
import json, os, subprocess, time
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
from concurrent.futures import ThreadPoolExecutor
import requests

ROOT = Path(__file__).resolve().parents[1]
ST = ROOT / "state" / "panop"
ADB = os.path.expanduser("~/AppData/Local/Android/Sdk/platform-tools/adb.exe")
LEDGER = ST / "phone_close_ledger.jsonl"
TRACK = {"utm_source","utm_medium","utm_campaign","utm_term","utm_content","fbclid","gclid",
         "mc_cid","mc_eid","igshid","_ga","ref","ref_src","yclid","msclkid","spm","share",
         "shared","from","source","_hsenc","_hsmi","gad_source","triedRedirect","r"}


def canon(u):
    try:
        p = urlparse(u); net = (p.netloc or "").lower()
        if net.startswith("m."): net = "www." + net[2:]
        path = (p.path or "").rstrip("/") or "/"
        qs = sorted((k, v) for k, v in parse_qsl(p.query) if k.lower() not in TRACK)
        return urlunparse(((p.scheme or "https").lower(), net, path, "", urlencode(qs), ""))
    except Exception:
        return u


def _adb(*a, t=15):
    try: subprocess.run([ADB, *a], capture_output=True, timeout=t)
    except Exception: pass


def wake():
    _adb("connect", "192.168.0.9:5555")
    _adb("shell", "input", "keyevent", "KEYCODE_WAKEUP")
    _adb("shell", "input", "keyevent", "KEYCODE_HOME")
    _adb("shell", "monkey", "-p", "com.android.chrome", "-c", "android.intent.category.LAUNCHER", "1")
    _adb("shell", "svc", "power", "stayon", "true")
    _adb("forward", "--remove", "tcp:9222")
    _adb("forward", "tcp:9222", "localabstract:chrome_devtools_remote")


def live_tabs():
    try:
        r = requests.get("http://127.0.0.1:9222/json/list", timeout=180)
        return [t for t in r.json() if str(t.get("url", "")).startswith("http")]
    except Exception:
        return None


def saved_urls():
    """Canon URLs confirmed saved to BOTH destinations (from the save ledger)."""
    s = set()
    led = ST / "phone_save_ledger.jsonl"
    if led.exists():
        for line in led.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
                if d.get("close"):                     # saved to both
                    s.add(canon(d.get("url", "")))
            except Exception:
                pass
    return s


def main():
    saved = saved_urls()
    print(f"saved-to-both URLs: {len(saved)}")
    led = open(LEDGER, "a", encoding="utf-8")
    rounds, stale = 0, 0
    while True:
        rounds += 1
        wake(); time.sleep(4)
        tabs = live_tabs()
        if tabs is None:
            print(f"[round {rounds}] DevTools not responding, retrying…"); time.sleep(5)
            stale += 1
            if stale >= 4: print("giving up — re-run to resume."); break
            continue
        # map saved+open tabs -> their CURRENT ids (close ALL dupes of a saved url)
        targets = [(t["id"], t.get("url", "")) for t in tabs if canon(t.get("url", "")) in saved]
        open_total = len(tabs)
        print(f"[round {rounds}] live tabs {open_total} | saved-and-open to close {len(targets)}", flush=True)
        if not targets:
            print("done — no saved tabs left open."); break

        def close_one(it):
            tid, url = it
            try:
                r = requests.post(f"http://127.0.0.1:9222/json/close/{tid}", timeout=15)
                gone = r.status_code in (200, 201, 404)
            except Exception:
                return False
            if gone:
                led.write(json.dumps({"id": tid, "url": url, "closed": True,
                                      "ts": datetime.now().isoformat(), "via": "finish"},
                                     ensure_ascii=False) + "\n"); led.flush()
            return gone
        with ThreadPoolExecutor(max_workers=4) as ex:
            closed = sum(ex.map(close_one, targets))
        print(f"   closed {closed}/{len(targets)}", flush=True)
        stale = 0
        time.sleep(2)
    led.close()
    print("reject/unsaved tabs left OPEN (your manual queue).")


if __name__ == "__main__":
    main()
