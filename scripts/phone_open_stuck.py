"""Open the 'stuck' links (broken-title articles behind Cloudflare/paywalls that
couldn't be re-fetched from the PC) as tabs in the phone's authenticated Chrome,
where they load fine. Fires one VIEW intent per URL, paced. Traced + idempotent.
"""
from __future__ import annotations
import json, os, subprocess, time
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parents[1]
ST = ROOT / "state" / "panop"
ADB = os.path.expanduser("~/AppData/Local/Android/Sdk/platform-tools/adb.exe")
LEDGER = ST / "phone_open_ledger.jsonl"
DEVICE = "192.168.0.9:5555"


def _adb(*a, t=20):
    try:
        return subprocess.run([ADB, *a], capture_output=True, text=True, timeout=t)
    except Exception:
        return None


def main():
    urls = json.loads((ST / "stuck_links.json").read_text(encoding="utf-8"))
    done = set()
    if LEDGER.exists():
        for l in LEDGER.read_text(encoding="utf-8").splitlines():
            try: done.add(json.loads(l)["url"])
            except Exception: pass
    todo = [u for u in urls if u not in done]
    print(f"stuck links: {len(urls)} | already opened: {len(done)} | to open: {len(todo)}")

    _adb("connect", DEVICE)
    _adb("shell", "input", "keyevent", "KEYCODE_WAKEUP")
    _adb("shell", "svc", "power", "stayon", "true")
    _adb("shell", "monkey", "-p", "com.android.chrome", "-c", "android.intent.category.LAUNCHER", "1")
    time.sleep(3)

    led = open(LEDGER, "a", encoding="utf-8")
    opened = 0
    for i, u in enumerate(todo):
        r = _adb("shell", "am", "start", "-a", "android.intent.action.VIEW",
                 "-d", u, "com.android.chrome", t=15)
        ok = r is not None and "Error" not in (r.stderr or "")
        led.write(json.dumps({"url": u, "opened": bool(ok), "ts": datetime.now().isoformat()},
                             ensure_ascii=False) + "\n")
        led.flush()
        opened += ok
        if i % 25 == 0:
            _adb("shell", "input", "keyevent", "KEYCODE_WAKEUP")
            print(f"  opened {i+1}/{len(todo)}", flush=True)
        time.sleep(0.6)
    led.close()
    print(f"\nOPENED {opened}/{len(todo)} stuck links on the phone.")


if __name__ == "__main__":
    main()
