"""Toggle Egon's phone 'banking mode' pause.

Why
---
Egon's phone keepalive normally keeps Android Wireless Debugging ON so the
Inbox drain can reach the phone. Banking apps (Nubank and friends) run
anti-fraud checks that REFUSE TO LAUNCH while debugging is on — so while Egon
holds debugging on, those apps won't open.

Egon already auto-detects a banking app in the foreground and backs off, but
this script is the manual override: turn banking mode ON before a long banking
session (or for an app Egon doesn't know about) and Egon will stop re-enabling
Wireless Debugging until you turn it OFF again.

Usage
-----
    python scripts/phone_banking_mode.py on      # pause — let banking apps open
    python scripts/phone_banking_mode.py off     # resume the normal phone link
    python scripts/phone_banking_mode.py toggle
    python scripts/phone_banking_mode.py status

The flag is just the presence of state/panop/phone_link_paused.json, which the
keepalive checks every cycle — no Egon restart needed.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAUSE_FILE = ROOT / "state" / "panop" / "phone_link_paused.json"
STATUS_FILE = ROOT / "state" / "panop" / "phone_status.json"
ADB_CANDIDATES = [
    ROOT / "state" / "panop" / "platform-tools" / "platform-tools" / "adb.exe",
    ROOT / "panop_output" / "platform-tools" / "platform-tools" / "adb.exe",
    Path.home() / "AppData/Local/Android/Sdk/platform-tools/adb.exe",
]
EGON_CONNECT_PACKAGE = "org.brunosaramago.egonconnect"
EGON_A11Y_SERVICE = f"{EGON_CONNECT_PACKAGE}/{EGON_CONNECT_PACKAGE}.EgonA11yService"


def _find_adb() -> Path | None:
    for candidate in ADB_CANDIDATES:
        if candidate.exists():
            return candidate
    return None


def _adb(adb: Path, *args: str, timeout: int = 8) -> str:
    try:
        p = subprocess.run([str(adb), *args], capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace")
        return (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return str(e)


def _devices(adb: Path) -> list[str]:
    out = _adb(adb, "devices", timeout=8)
    devices: list[str] = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == "device":
            devices.append(parts[0])
    return devices


def _disable_egon_connect_surface(adb: Path, dev: str) -> list[str]:
    logs: list[str] = []
    cur = _adb(adb, "-s", dev, "shell", "settings", "get", "secure",
               "enabled_accessibility_services", timeout=6).strip()
    parts = [p for p in cur.split(":") if p and p != "null" and p != EGON_A11Y_SERVICE]
    new = ":".join(parts) if parts else "null"
    _adb(adb, "-s", dev, "shell", "settings", "put", "secure",
         "enabled_accessibility_services", new, timeout=6)
    if not parts:
        _adb(adb, "-s", dev, "shell", "settings", "put", "secure",
             "accessibility_enabled", "0", timeout=6)
    logs.append(f"{dev}: Egon accessibility disabled")

    for op in ("SYSTEM_ALERT_WINDOW", "ACCESS_RESTRICTED_SETTINGS"):
        _adb(adb, "-s", dev, "shell", "appops", "set", EGON_CONNECT_PACKAGE,
             op, "ignore", timeout=6)
        logs.append(f"{dev}: {op}=ignore")

    _adb(adb, "-s", dev, "shell", "am", "force-stop", EGON_CONNECT_PACKAGE,
         timeout=6)
    logs.append(f"{dev}: Egon Connect force-stopped")
    return logs


def _write_status() -> None:
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATUS_FILE.write_text(json.dumps({
        "reachable": False,
        "needs_action": False,
        "paused": True,
        "paused_reason": "manual banking mode",
        "target": None,
        "message": "Phone background access paused (banking mode). User-requested phone actions can still acquire a short Egon access lease.",
        "updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }, indent=2), encoding="utf-8")


def _teardown_phone_link() -> list[str]:
    logs: list[str] = []
    adb = _find_adb()
    if not adb:
        return ["adb not found"]
    for dev in _devices(adb):
        logs.extend(_disable_egon_connect_surface(adb, dev))
        _adb(adb, "-s", dev, "shell", "settings", "put", "global",
             "adb_wifi_enabled", "0", timeout=6)
        readback = _adb(adb, "-s", dev, "shell", "settings", "get", "global",
                        "adb_wifi_enabled", timeout=6).strip()
        logs.append(f"{dev}: adb_wifi_enabled={readback or 'unknown'}")
    logs.append(_adb(adb, "disconnect", timeout=6).strip() or "disconnect requested")
    logs.append(_adb(adb, "kill-server", timeout=6).strip() or "adb server stopped")
    try:
        subprocess.run(["taskkill", "/F", "/IM", "adb.exe"], capture_output=True,
                       timeout=6)
        logs.append("adb.exe processes killed")
    except Exception as e:
        logs.append(f"taskkill adb failed: {e}")
    return logs


def _on() -> None:
    PAUSE_FILE.parent.mkdir(parents=True, exist_ok=True)
    PAUSE_FILE.write_text(json.dumps({
        "paused": True,
        "set_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "note": "Banking mode: Egon pauses background phone automation so it "
                "does not keep touching ADB. Explicit user-requested phone "
                "actions can still acquire short access leases.",
    }, indent=2), encoding="utf-8")
    _write_status()


def _off() -> None:
    try:
        PAUSE_FILE.unlink()
    except FileNotFoundError:
        pass


def main(argv: list[str]) -> int:
    cmd = (argv[0] if argv else "status").lower()
    if cmd in ("on", "pause", "bank"):
        _on(); print("banking mode ON — Egon will let banking apps open")
    elif cmd in ("off", "resume", "unpause"):
        _off(); print("banking mode OFF — normal phone link resumes")
    elif cmd == "toggle":
        if PAUSE_FILE.exists():
            _off(); print("banking mode OFF — normal phone link resumes")
        else:
            _on(); print("banking mode ON — Egon will let banking apps open")
    elif cmd == "status":
        print("banking mode is", "ON" if PAUSE_FILE.exists() else "OFF")
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
