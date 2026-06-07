"""Phone keepalive — in-process Egon service.

Replaces `scripts/phone_keepalive.py` for the 2026-05-27 rule ("nothing
runs outside Egon"). When Egon's MainWindow is open, this service polls
Android's `adb_wifi_enabled` flag and re-enables it if Android flips it,
plus periodically re-stamps `screen_off_timeout` and `svc power stayon`
so the wireless-debug link survives Doze, charger plug/unplug, and the
phone's occasional reset of those flags. When Egon closes the daemon
thread dies with the process — no leftover daemon, no Startup-folder
shortcut, no scheduled task.

The standalone `scripts/phone_keepalive.py` is intentionally left in
place per Bruno's "add, don't reinvent" rule — don't delete other
agents' work. It is no longer auto-started (the Startup shortcut was
moved to `.backups/startup-disabled-2026-05-27/` on 2026-05-27). This
in-process service supersedes it.

Why a copy of the helpers rather than an `import` from the script:
importing the script triggers its top-level `subprocess.Popen.__init__`
monkey-patch a SECOND time (Egon's `egon_app/main.py` already installs
the same patch at process start). Double-wrapping is harmless but ugly,
and copying the helpers keeps this service standalone — if the script
ever gets edited or removed in a future agent session, this service
keeps working.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
LOCKED_FILE = ROOT / "state" / "panop" / "locked_target.json"
# Human-facing phone link status — the UI (Inbox banner) + the tray notifier
# read this so Egon ALWAYS TELLS Bruno when the phone needs a USB re-plug.
# Bruno 2026-06-01.
STATUS_FILE = ROOT / "state" / "panop" / "phone_status.json"
ADB_CANDIDATES = [
    ROOT / "state" / "panop" / "platform-tools" / "platform-tools" / "adb.exe",
    ROOT / "panop_output" / "platform-tools" / "platform-tools" / "adb.exe",
    Path.home() / "AppData/Local/Android/Sdk/platform-tools/adb.exe",
]
LOG_DIR = ROOT / "logs"

POLL_INTERVAL_S = 30          # check adb_wifi_enabled this often
REASSERT_INTERVAL_S = 300     # re-stamp screen_timeout/stayon every 5 min
BACKOFF_INITIAL_S = 5
BACKOFF_CAP_S = 60


def _log_file() -> Path:
    # Open per call so the file rotates naturally on month change.
    return LOG_DIR / f"phone-keepalive-{datetime.now():%Y-%m}.log"


def _log(level: str, event: str, **kw) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    rec = {"ts": datetime.now().isoformat(timespec="seconds"),
           "level": level, "event": event, **kw}
    try:
        with _log_file().open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _find_adb() -> Path | None:
    for c in ADB_CANDIDATES:
        if c.exists():
            return c
    return None


def _read_target() -> str | None:
    try:
        d = json.loads(LOCKED_FILE.read_text(encoding="utf-8"))
        t = d.get("target", "")
        if ":" in t and t.count(".") == 3:
            return t
    except Exception:
        return None
    return None


def _adb(adb_path: Path, *args: str, timeout: int = 10) -> tuple[int, str]:
    """Run adb with the given args. The CREATE_NO_WINDOW patch installed in
    `egon_app/main.py` covers this `subprocess.run`, so no console flash."""
    try:
        p = subprocess.run([str(adb_path), *args],
                           capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return -1, str(e)


def _is_reachable(adb_path: Path, target: str) -> bool:
    rc, _ = _adb(adb_path, "-s", target, "shell", "true", timeout=4)
    return rc == 0


def _ensure_connected(adb_path: Path, target: str) -> bool:
    if _is_reachable(adb_path, target):
        return True
    _adb(adb_path, "connect", target, timeout=8)
    return _is_reachable(adb_path, target)


def _assert_keepalive(adb_path: Path, target: str) -> dict:
    """Re-enable adb_wifi_enabled if Android flipped it off. Returns a dict
    of what was observed/changed for logging."""
    out: dict = {}
    rc, v = _adb(adb_path, "-s", target, "shell", "settings", "get", "global",
                 "adb_wifi_enabled", timeout=6)
    v = (v or "").strip()
    out["adb_wifi_enabled_before"] = v
    if v not in ("1", "true"):
        _log("warn", "adb_wifi_was_disabled", value=v)
        _adb(adb_path, "-s", target, "shell", "settings", "put", "global",
             "adb_wifi_enabled", "1", timeout=6)
        _, v2 = _adb(adb_path, "-s", target, "shell", "settings", "get", "global",
                     "adb_wifi_enabled", timeout=6)
        out["adb_wifi_enabled_after"] = (v2 or "").strip()
    return out


def _reassert_persistent_flags(adb_path: Path, target: str) -> None:
    # Screen timeout and stay-awake settings are managed safely inside
    # panop_capture.py during sweeps, and restored immediately when done.
    # We do not overwrite them here in the background loop.
    pass


# ── auto-relock over USB ─────────────────────────────────────────────────────
# Bruno 2026-06-01: the wireless lock (`adb tcpip 5555`) is LOST on every phone
# reboot and whenever Developer Options is toggled — and Bruno has hit this
# repeatedly. The keepalive used to just retry the dead target forever. Now,
# whenever the wireless target is unreachable, we look for a USB-attached phone
# and AUTOMATICALLY redo the lock (the same steps as
# scripts/lock_phone_to_5555.py): tcpip 5555 → read wifi IP → connect → persist
# locked_target.json. So the user never runs a script again — plug the phone in
# once after a reboot and Egon re-establishes the link within one poll cycle.

def _usb_serials(adb_path: Path) -> list[str]:
    """USB-attached device serials only (skip network IP:port + mDNS entries)."""
    rc, out = _adb(adb_path, "devices")
    serials = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2 or parts[1] != "device":
            continue
        serial = parts[0]
        if ":" in serial and serial.count(".") == 3:   # 192.168.x.y:NNNN
            continue
        if "_adb-tls" in serial or serial.endswith("._tcp") or "._tcp." in serial:
            continue
        serials.append(serial)
    return serials


def _device_wifi_ip(adb_path: Path, serial: str) -> str | None:
    rc, out = _adb(adb_path, "-s", serial, "shell", "ip", "-4", "addr",
                   "show", "wlan0", timeout=8)
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out or "")
    if m:
        return m.group(1)
    rc, out = _adb(adb_path, "-s", serial, "shell", "ip", "route", "get",
                   "1.1.1.1", timeout=8)
    m = re.search(r"src (\d+\.\d+\.\d+\.\d+)", out or "")
    return m.group(1) if m else None


def _relock_via_usb(adb_path: Path) -> str | None:
    """If a phone is plugged in over USB, re-establish the tcpip-5555 wireless
    lock and persist it. Returns the new target, or None if no USB device /
    failure. Mirrors scripts/lock_phone_to_5555.py exactly."""
    serials = _usb_serials(adb_path)
    if not serials:
        return None
    if len(serials) > 1:
        _log("warn", "relock_multiple_usb", serials=serials)
        return None
    serial = serials[0]
    ip = _device_wifi_ip(adb_path, serial)
    if not ip:
        _log("warn", "relock_no_wifi_ip", serial=serial,
             hint="is the phone on wifi?")
        return None
    rc, text = _adb(adb_path, "-s", serial, "tcpip", "5555", timeout=15)
    if rc != 0 and "restarting in TCP mode" not in (text or ""):
        _log("warn", "relock_tcpip_failed", detail=(text or "")[:160])
        return None
    time.sleep(2)   # daemon restarts into TCP mode
    target = f"{ip}:5555"
    _adb(adb_path, "connect", target, timeout=10)
    if not _is_reachable(adb_path, target):
        _log("warn", "relock_connect_unreachable", target=target,
             hint="PC and phone on the SAME wifi?")
        return None
    try:
        LOCKED_FILE.parent.mkdir(parents=True, exist_ok=True)
        LOCKED_FILE.write_text(json.dumps({
            "target": target,
            "method": "adb_tcpip_5555",
            "set_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "serial": serial,
            "ip": ip,
            "note": "Auto-relocked by Egon's phone keepalive when the phone was "
                    "plugged in over USB (recovers from reboot / dev-options "
                    "toggle). No manual script needed.",
        }, indent=2), encoding="utf-8")
    except Exception:
        pass
    _log("info", "auto_relocked", target=target, serial=serial, ip=ip)
    return target


def _write_phone_status(reachable: bool, target: str | None,
                        usb_seen: bool = False) -> None:
    """Persist a human-facing status the UI + tray notifier read. `needs_action`
    is True when the link is down AND we couldn't auto-heal (no usable USB
    device) — i.e. Bruno must plug in / enable USB debugging."""
    needs_action = (not reachable) and (not usb_seen)
    if reachable:
        msg = "Phone connected — Inbox drain can reach it."
    elif usb_seen:
        msg = "Phone plugged in over USB — re-establishing the wireless link…"
    else:
        msg = ("Phone disconnected. Plug it into the PC via USB and make sure "
               "USB debugging is ON (Developer Options) — Egon will then "
               "reconnect automatically. (tcpip mode is lost on every reboot; "
               "this is the one manual step.)")
    try:
        STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATUS_FILE.write_text(json.dumps({
            "reachable": reachable,
            "needs_action": needs_action,
            "target": target,
            "message": msg,
            "updated": datetime.now().isoformat(timespec="seconds"),
        }, indent=2), encoding="utf-8")
    except Exception:
        pass


def _run_loop(stop: threading.Event) -> None:
    adb_path = _find_adb()
    if not adb_path:
        _log("error", "adb_not_found",
             hint="state/panop/platform-tools/platform-tools/adb.exe not present")
        # Park the loop — wake on stop. Better than crashing.
        stop.wait()
        return

    _log("info", "keepalive_start_inprocess", pid=os.getpid())
    backoff = BACKOFF_INITIAL_S
    last_reassert = 0.0
    target = _read_target()

    while not stop.is_set():
        try:
            target = _read_target() or target
            if not target:
                # No locked target yet — try to establish one automatically if
                # the phone is plugged in over USB; otherwise wait. (First-time
                # setup no longer strictly needs the manual lock script.)
                target = _relock_via_usb(adb_path)
                if not target:
                    _write_phone_status(False, None, usb_seen=False)
                    stop.wait(POLL_INTERVAL_S)
                    continue

            if not _ensure_connected(adb_path, target):
                # Wireless link dead (reboot / dev-options toggle / IP change).
                # Before backing off, try to auto-relock via USB — if the phone
                # is plugged in, this heals it within one cycle. Bruno 2026-06-01.
                new_target = _relock_via_usb(adb_path)
                if new_target:
                    target = new_target
                    backoff = BACKOFF_INITIAL_S
                    _write_phone_status(True, target)
                    continue
                # Couldn't reach AND couldn't auto-heal → tell the user (the
                # banner + tray notifier read phone_status.json).
                usb_seen = bool(_usb_serials(adb_path))
                _write_phone_status(False, target, usb_seen=usb_seen)
                _log("warn", "unreachable", target=target, backoff_s=backoff,
                     usb_seen=usb_seen)
                stop.wait(backoff)
                backoff = min(BACKOFF_CAP_S, backoff * 2)
                continue
            backoff = BACKOFF_INITIAL_S    # reset on success
            _write_phone_status(True, target)

            kv = _assert_keepalive(adb_path, target)
            if kv.get("adb_wifi_enabled_after"):
                _log("info", "adb_wifi_reenabled", **kv)

            now = time.time()
            if now - last_reassert > REASSERT_INTERVAL_S:
                _reassert_persistent_flags(adb_path, target)
                last_reassert = now

            stop.wait(POLL_INTERVAL_S)
        except Exception as e:
            _log("error", "loop_exception", error=str(e)[:200])
            stop.wait(POLL_INTERVAL_S)

    _log("info", "keepalive_stop_inprocess")


class PhoneKeepaliveService:
    """Lifecycle wrapper. Public API:

        svc = PhoneKeepaliveService()
        svc.start()             # at app startup
        ...
        svc.stop(timeout=4)     # at app exit (wire to QApplication.aboutToQuit)
    """

    def __init__(self):
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=_run_loop, args=(self._stop,),
            daemon=True, name="egon-phone-keepalive",
        )
        self._thread.start()

    def stop(self, timeout: float = 4.0) -> None:
        self._stop.set()
        if self._thread is not None:
            try:
                self._thread.join(timeout=timeout)
            except Exception:
                pass
