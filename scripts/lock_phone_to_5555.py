"""One-shot phone lock: switch the connected device into persistent TCP/IP
debugging mode on port 5555.

Why this exists
---------------
Android 11+ "Wireless debugging" (the one with pairing codes) deliberately
rotates the connection port every time the debug daemon restarts. That's the
default mode and the reason discovery keeps failing — there's no stable
target to connect to.

The OLDER `adb tcpip 5555` mechanism still works on modern Android and uses
a fixed port. It must be initiated over USB (Android refuses the request
over the network for security), but once set, the phone listens on
192.168.0.x:5555 until the next reboot — no more rotation.

How to use
----------
1. Plug your phone into the PC with a USB cable.
2. On the phone: Developer Options -> "USB debugging" must be ON.
3. Approve the RSA fingerprint dialog if it appears.
4. Run this script:        .venv\\Scripts\\python.exe scripts\\lock_phone_to_5555.py
5. When it prints  "LOCKED on <ip>:5555", you can unplug the cable.
6. Panop will from now on connect to <ip>:5555 directly — no mDNS, no
   rotating ports, no discovery delay.

What survives
-------------
- Survives screen sleep, wifi-debug toggle, Chrome going background.
- Does NOT survive a phone reboot. Re-run after every reboot.
- Does NOT survive disabling "USB debugging" in Developer Options.

After running, the connection lock is also persisted to
egon/state/panop/locked_target.json so phone_discovery can read it.
"""
from __future__ import annotations

import lib.silent_subprocess  # noqa: F401  — suppress console windows

import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ADB_CANDIDATES = [
    ROOT / "state/panop/platform-tools/platform-tools/adb.exe",
    ROOT / "panop_output/platform-tools/platform-tools/adb.exe",
    Path.home() / "AppData/Local/Android/Sdk/platform-tools/adb.exe",
    Path("adb.exe"),
]
LOCKED_FILE = ROOT / "state/panop/locked_target.json"


def _find_adb() -> Path:
    for c in ADB_CANDIDATES:
        if c.exists() if c.is_absolute() else True:
            try:
                subprocess.run([str(c), "version"], capture_output=True, timeout=5)
                return c
            except Exception:
                continue
    raise SystemExit("adb.exe not found. Install platform-tools or run Panop once to download it.")


def _adb(adb: Path, *args: str, timeout: int = 20) -> tuple[int, str, str]:
    p = subprocess.run([str(adb), *args], capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout or "", p.stderr or ""


def _usb_devices(adb: Path) -> list[str]:
    """Return list of USB-attached device serials (not network ones)."""
    _, out, _ = _adb(adb, "devices")
    serials = []
    for line in out.splitlines()[1:]:
        line = line.strip()
        if not line or "device" not in line.split():
            continue
        serial = line.split()[0]
        # Skip non-USB entries:
        # - Network serials look like "192.168.x.y:NNNN"
        # - mDNS service entries look like "adb-XXXX._adb-tls-connect._tcp"
        if ":" in serial and serial.count(".") == 3:
            continue
        if "_adb-tls" in serial or serial.endswith("._tcp") or "._tcp." in serial:
            continue
        serials.append(serial)
    return serials


def _device_ip(adb: Path, serial: str) -> str | None:
    """Read the phone's current wifi IP via `ip route`."""
    # try wlan0 first
    _, out, _ = _adb(adb, "-s", serial, "shell", "ip", "-4", "addr", "show", "wlan0", timeout=8)
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
    if m:
        return m.group(1)
    # fall back to `ip route get`
    _, out, _ = _adb(adb, "-s", serial, "shell", "ip", "route", "get", "1.1.1.1", timeout=8)
    m = re.search(r"src (\d+\.\d+\.\d+\.\d+)", out)
    return m.group(1) if m else None


def main() -> int:
    adb = _find_adb()
    print(f"[lock] adb at {adb}")

    serials = _usb_devices(adb)
    if not serials:
        print("[lock] No USB-connected device found. Plug in the cable, enable "
              "USB debugging in Developer Options, accept the RSA dialog, then "
              "re-run.")
        return 2
    if len(serials) > 1:
        print(f"[lock] Multiple USB devices: {serials}. Aborting — keep only one plugged in.")
        return 2

    serial = serials[0]
    print(f"[lock] device serial: {serial}")

    ip = _device_ip(adb, serial)
    if not ip:
        print("[lock] couldn't read phone IP. Is wifi on?")
        return 3
    print(f"[lock] phone wifi IP: {ip}")

    # The actual lock: switches the daemon into TCP/IP mode on port 5555
    rc, out, err = _adb(adb, "-s", serial, "tcpip", "5555")
    text = (out + err).strip()
    if rc != 0 and "restarting in TCP mode" not in text:
        print(f"[lock] tcpip command failed: {text}")
        return 4
    print(f"[lock] {text}")

    # The phone is briefly off-line as the daemon restarts; give it a moment.
    time.sleep(2)
    target = f"{ip}:5555"
    rc, out, err = _adb(adb, "connect", target)
    text = (out + err).strip()
    print(f"[lock] connect: {text}")

    # Confirm it's reachable
    _, devs_out, _ = _adb(adb, "devices")
    ok = any(target in line and "device" in line for line in devs_out.splitlines())
    if not ok:
        print(f"[lock] WARNING: {target} not visible in `adb devices`. "
              "Check that PC and phone are on the SAME wifi network.")
        print(devs_out)

    # Persist for phone_discovery to consume
    LOCKED_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCKED_FILE.write_text(json.dumps({
        "target": target,
        "method": "adb_tcpip_5555",
        "set_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "serial": serial,
        "ip": ip,
        "note": "Survives screen sleep / wifi-debug toggle. NOT a reboot. "
                "Re-run scripts/lock_phone_to_5555.py with USB after every "
                "phone reboot.",
    }, indent=2), encoding="utf-8")

    print(f"[lock] LOCKED on {target}")
    print(f"[lock] state saved -> {LOCKED_FILE}")
    print("[lock] You can unplug the USB cable now. Panop will use this "
          "target directly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
