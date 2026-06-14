"""Phase 3: close ONLY the verified-saved tabs (the closeable set) on the phone.
reject / unclassified / failures are NEVER closed.

At 2,000+ tabs Chrome's DevTools is overloaded and per-close calls time out, so
this runs as a PATIENT, RESUMABLE grind: low concurrency, generous timeouts,
re-foregrounds Chrome + re-forwards each round, and keeps going until every
closeable tab is closed (it accelerates as the tab count drops and Chrome frees
memory). Every close is traced; phone_tabs_full.json + the save ledger make all
closed tabs fully recoverable.
"""
from __future__ import annotations
import json, os, subprocess, time
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
import requests

ROOT = Path(__file__).resolve().parents[1]
ST = ROOT / "state" / "panop"
ADB = os.path.expanduser("~/AppData/Local/Android/Sdk/platform-tools/adb.exe")
CLOSE_LEDGER = ST / "phone_close_ledger.jsonl"
DEVICE = "192.168.0.9:5555"

closeable = list(dict.fromkeys(json.loads((ST / "phone_closeable.json").read_text(encoding="utf-8"))))
full = {t["id"]: t for t in json.loads((ST / "backups" / "phone_tabs_full.json").read_text(encoding="utf-8"))}


def _adb(*args, t=15):
    try: subprocess.run([ADB, *args], capture_output=True, timeout=t)
    except Exception: pass


def wake_and_forward():
    _adb("connect", DEVICE)
    _adb("shell", "input", "keyevent", "KEYCODE_WAKEUP")
    _adb("shell", "svc", "power", "stayon", "true")
    _adb("shell", "am", "start", "-n", "com.android.chrome/com.google.android.apps.chrome.Main")
    _adb("forward", "--remove", "tcp:9222")
    _adb("forward", "tcp:9222", "localabstract:chrome_devtools_remote")


def closed_so_far():
    s = set()
    if CLOSE_LEDGER.exists():
        for line in CLOSE_LEDGER.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
                if d.get("closed"): s.add(d["id"])
            except Exception: pass
    return s


def main():
    led = open(CLOSE_LEDGER, "a", encoding="utf-8")

    def close_one(tid):
        # ANY HTTP reply (200=closed, 404=already gone) means the tab is no
        # longer open -> done. Only a connection error/timeout is a real retry.
        try:
            r = requests.post(f"http://127.0.0.1:9222/json/close/{tid}", timeout=15)
            gone = r.status_code in (200, 201, 404)
        except Exception:
            return False
        if gone:
            led.write(json.dumps({"id": tid, "ts": datetime.now().isoformat(),
                                  "url": full.get(tid, {}).get("url", ""), "closed": True,
                                  "code": r.status_code}, ensure_ascii=False) + "\n")
            led.flush()
        return gone

    rounds, stale = 0, 0
    while True:
        rounds += 1
        done = closed_so_far()
        remaining = [t for t in closeable if t not in done]
        print(f"[round {rounds}] closed {len(done)}/{len(closeable)} | remaining {len(remaining)}", flush=True)
        if not remaining:
            print("ALL closeable tabs closed.", flush=True); break
        wake_and_forward()
        before = len(done)
        with ThreadPoolExecutor(max_workers=4) as ex:
            list(ex.map(close_one, remaining))
        gained = len(closed_so_far()) - before
        print(f"   round closed +{gained}", flush=True)
        stale = stale + 1 if gained == 0 else 0
        if stale >= 4:
            print(f"No progress for {stale} rounds — DevTools unresponsive; stopping. "
                  f"Re-run to resume.", flush=True); break
        time.sleep(2)
    led.close()
    fin = closed_so_far()
    print(f"\nCLOSED {len(fin)}/{len(closeable)} | still to close {len(closeable)-len(fin)}", flush=True)
    print("Left OPEN: 882 reject + unclassified (your manual queue).", flush=True)


if __name__ == "__main__":
    main()
