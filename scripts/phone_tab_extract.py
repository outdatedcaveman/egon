"""STEP 1 of the phone sweep (Bruno's design): pull EVERY open tab's URL off the
phone to the PC — lightweight, so nothing heavy runs on Android. Uses CDP
Target.getTargets over the browser websocket (not /json/list, which serializes
title+favicon per tab and chokes past ~180s on thousands of tabs).

Writes state/panop/phone_tabs_<stamp>.json = [{targetId, url, title}] and a
stable 'phone_tabs_latest.json'. Mutates nothing on the phone.
"""
from __future__ import annotations
import json, subprocess, time, sys
from datetime import datetime
from pathlib import Path
import requests
import websocket  # websocket-client

ROOT = Path(__file__).resolve().parents[1]
ADB = ROOT / "panop_output" / "platform-tools" / "platform-tools" / "adb.exe"
OUT = ROOT / "state" / "panop"
NOWIN = 0x08000000


def _run(*a, timeout=20):
    return subprocess.run([str(ADB), *a], capture_output=True, text=True,
                          timeout=timeout, creationflags=NOWIN)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    devs = [l.split()[0] for l in (_run("devices").stdout or "").splitlines()[1:]
            if l.strip() and l.split()[-1] == "device"]
    print("adb devices:", devs or "NONE")
    if not devs:
        # try the locked wifi target from task #7
        _run("connect", "127.0.0.1:5555")
        time.sleep(1)
        devs = [l.split()[0] for l in (_run("devices").stdout or "").splitlines()[1:]
                if l.strip() and l.split()[-1] == "device"]
        if not devs:
            print("NO DEVICE — phone not reachable over adb. Is wifi-debug up?")
            return 2

    _run("forward", "--remove", "tcp:9222")
    _run("forward", "tcp:9222", "localabstract:chrome_devtools_remote")
    time.sleep(0.5)

    ver = requests.get("http://127.0.0.1:9222/json/version", timeout=15).json()
    print("browser:", ver.get("Browser"))
    ws_url = ver["webSocketDebuggerUrl"]

    # Chrome rejects ws upgrades whose Origin isn't allow-listed; omit it.
    ws = websocket.create_connection(ws_url, timeout=120, max_size=None,
                                     suppress_origin=True)
    ws.send(json.dumps({"id": 1, "method": "Target.getTargets"}))
    targets = []
    deadline = time.time() + 120
    while time.time() < deadline:
        msg = json.loads(ws.recv())
        if msg.get("id") == 1:
            targets = msg["result"]["targetInfos"]
            break
    ws.close()

    pages = [{"targetId": t["targetId"], "url": t.get("url", ""), "title": t.get("title", "")}
             for t in targets
             if t.get("type") == "page" and not t.get("url", "").startswith(
                 ("chrome://", "about:", "devtools://", "chrome-native://"))]
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    raw = OUT / f"phone_tabs_{stamp}.json"
    raw.write_text(json.dumps(pages, ensure_ascii=False, indent=1), encoding="utf-8")
    (OUT / "phone_tabs_latest.json").write_text(json.dumps(pages, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"total page targets: {len(targets)}  |  real tabs (excl chrome://): {len(pages)}")
    print(f"saved -> {raw}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
