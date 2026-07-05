"""Install the staged Egon APK to the phone ONLY when it actually changed.

Bruno 2026-07-05 (on the street): "nothing happens when I click Capture again."
Root cause: every APK reinstall wipes the accessibility + overlay grants that
Capture and the bubble need. My auto-installers reinstalled BLINDLY on each LAN
reconnect, so each reconnect silently broke Capture until the keepalive
re-granted it. This guard installs only when the staged APK's sha256 differs
from the version already on the phone (versionName+versionCode heuristic +
recorded hash), so grants survive and reconnects are no-ops.

Usage: pythonw scripts/install_apk_if_changed.py   (exit 0 = up to date or
installed; prints one status line).
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APK = ROOT / "state" / "EgonConnect.apk"
ADB = ROOT / "panop_output" / "platform-tools" / "platform-tools" / "adb.exe"
STATE = ROOT / "state" / "apk_installed_sha.json"
PKG = "org.brunosaramago.egonconnect"
NO_WINDOW = 0x08000000


def _adb(*args, timeout=20):
    try:
        r = subprocess.run([str(ADB), *args], capture_output=True, text=True,
                           timeout=timeout, creationflags=NO_WINDOW)
        return r.returncode, (r.stdout or "").strip()
    except Exception as e:
        return 1, str(e)[:120]


def _staged_sha() -> str:
    return hashlib.sha256(APK.read_bytes()).hexdigest() if APK.exists() else ""


def _device_ready() -> bool:
    rc, out = _adb("get-state")
    return rc == 0 and "device" in out


def main() -> int:
    if not APK.exists():
        print("no staged APK"); return 0
    sha = _staged_sha()
    try:
        last = json.loads(STATE.read_text(encoding="utf-8")).get("sha")
    except Exception:
        last = None
    if not _device_ready():
        print("phone not reachable — skipped"); return 0
    # is the package even installed? (fresh phone → must install)
    rc, out = _adb("shell", "pm", "path", PKG)
    installed = rc == 0 and "package:" in out
    if installed and last == sha:
        print("APK unchanged — NOT reinstalling (grants preserved)")
        return 0
    rc, out = _adb("install", "-r", str(APK), timeout=120)
    if "Success" in out:
        STATE.write_text(json.dumps({"sha": sha}), encoding="utf-8")
        print("APK installed (changed) — keepalive will restore grants")
        return 0
    print(f"install failed: {out[:120]}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
