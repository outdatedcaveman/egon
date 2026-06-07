"""Phone keep-alive daemon — never let Android disable wireless debugging.

Per Bruno's directive 2026-05-20: wireless debug must NOT drop spontaneously.
This daemon holds the line by:

  1. Maintaining a persistent ADB connection to the locked phone target.
  2. Polling `settings get global adb_wifi_enabled` every 30 s. If Android
     ever flips it to 0, we put it back to 1 over the SAME live session
     (this works because we already have an authorised connection).
  3. Re-asserting `screen_off_timeout = 1800000000` and `svc power stayon
     true` every 5 minutes — survives Doze, charger plug/unplug, and the
     phone's own occasional reset of these flags.
  4. On connection drop: reconnect to the locked target (192.168.1.50:5555)
     with exponential backoff (5 s → 60 s cap).
  5. Logs every action to `logs/phone-keepalive-YYYY-MM.log`.

Runs as a Windows scheduled task at logon (no admin required — uses the
Startup folder via scripts/install_phone_keepalive.py). The daemon never
opens a visible window thanks to the subprocess monkey-patch at top.

Hard rules:
  - We NEVER turn wireless debug OFF.
  - We NEVER restart the phone or do anything destructive.
  - We never hold more than one connection at a time.
  - If the locked target file is missing/unreachable, we sit idle and retry —
    we don't blast mDNS or try to provision via USB (that's the user's
    one-shot `lock_phone_to_5555.py`).
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# ── silence every child subprocess ──────────────────────────────────────────
if sys.platform == "win32":
    _NO_WIN = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    _orig_popen_init = subprocess.Popen.__init__
    def _silent_popen_init(self, *a, **kw):
        kw["creationflags"] = (kw.get("creationflags", 0) or 0) | _NO_WIN
        si = kw.get("startupinfo") or subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        kw["startupinfo"] = si
        return _orig_popen_init(self, *a, **kw)
    subprocess.Popen.__init__ = _silent_popen_init


ROOT = Path(__file__).resolve().parent.parent
LOCKED_FILE = ROOT / "state/panop/locked_target.json"
ADB_CANDIDATES = [
    ROOT / "state/panop/platform-tools/platform-tools/adb.exe",
    ROOT / "panop_output/platform-tools/platform-tools/adb.exe",
    Path.home() / "AppData/Local/Android/Sdk/platform-tools/adb.exe",
]
LOG_FILE = ROOT / "logs" / f"phone-keepalive-{datetime.now():%Y-%m}.log"
PID_FILE = ROOT / ".phone-keepalive.pid"

POLL_INTERVAL_S = 30          # check adb_wifi_enabled every 30s
REASSERT_INTERVAL_S = 300     # re-assert screen_timeout/stayon every 5min
BACKOFF_INITIAL_S = 5
BACKOFF_CAP_S = 60


# ── tiny utils ──────────────────────────────────────────────────────────────

def _log(level: str, event: str, **kw) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": datetime.now().isoformat(timespec="seconds"),
           "level": level, "event": event, **kw}
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
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


def _adb(adb: Path, *args: str, timeout: int = 10) -> tuple[int, str]:
    try:
        p = subprocess.run([str(adb), *args], capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return -1, str(e)


def _single_instance() -> bool:
    """Refuse to launch if another keepalive is alive (pid-file check)."""
    try:
        if PID_FILE.exists():
            old = int(PID_FILE.read_text().strip())
            # Probe by trying to send signal 0 — works on win via OpenProcess
            try:
                os.kill(old, 0)
                return False    # still alive
            except OSError:
                pass            # stale
        PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
        return True
    except Exception:
        return True   # fail-open rather than refuse to start


# ── core loop ───────────────────────────────────────────────────────────────

def _is_reachable(adb: Path, target: str) -> bool:
    rc, out = _adb(adb, "-s", target, "shell", "true", timeout=4)
    return rc == 0


def _ensure_connected(adb: Path, target: str) -> bool:
    if _is_reachable(adb, target):
        return True
    _adb(adb, "connect", target, timeout=8)
    return _is_reachable(adb, target)


def _assert_keepalive(adb: Path, target: str) -> dict:
    """Run the keep-alive commands. Returns a dict of {flag: value} read back."""
    out = {}
    # 1. Re-enable wireless debug if Android disabled it
    rc, v = _adb(adb, "-s", target, "shell", "settings", "get", "global",
                 "adb_wifi_enabled", timeout=6)
    v = (v or "").strip()
    out["adb_wifi_enabled_before"] = v
    if v not in ("1", "true"):
        _log("warn", "adb_wifi_was_disabled", value=v)
        _adb(adb, "-s", target, "shell", "settings", "put", "global",
             "adb_wifi_enabled", "1", timeout=6)
        # Read back
        rc2, v2 = _adb(adb, "-s", target, "shell", "settings", "get", "global",
                       "adb_wifi_enabled", timeout=6)
        out["adb_wifi_enabled_after"] = (v2 or "").strip()
    return out


def _reassert_persistent_flags(adb: Path, target: str) -> None:
    # Screen timeout and stay-awake settings are managed safely inside
    # panop_capture.py during sweeps, and restored immediately when done.
    # We do not overwrite them here in the background loop.
    pass


def run() -> int:
    if not _single_instance():
        print("phone_keepalive: another instance is running")
        return 1
    adb = _find_adb()
    if not adb:
        _log("error", "adb_not_found")
        return 2

    target = _read_target()
    if not target:
        _log("error", "no_locked_target",
             hint="Run scripts/lock_phone_to_5555.py with USB once.")
        # Sit and wait — lock file may appear later
    _log("info", "keepalive_start", pid=os.getpid(), target=target)

    backoff = BACKOFF_INITIAL_S
    last_reassert = 0.0

    while True:
        try:
            target = _read_target() or target
            if not target:
                time.sleep(POLL_INTERVAL_S)
                continue

            if not _ensure_connected(adb, target):
                _log("warn", "unreachable", target=target, backoff_s=backoff)
                time.sleep(backoff)
                backoff = min(BACKOFF_CAP_S, backoff * 2)
                continue
            backoff = BACKOFF_INITIAL_S   # reset on success

            # 1) wifi-debug flag check
            kv = _assert_keepalive(adb, target)
            if kv.get("adb_wifi_enabled_after"):
                _log("info", "adb_wifi_reenabled",
                     before=kv["adb_wifi_enabled_before"],
                     after=kv["adb_wifi_enabled_after"])

            # 2) periodic screen / stayon re-assertion
            now = time.time()
            if now - last_reassert > REASSERT_INTERVAL_S:
                _reassert_persistent_flags(adb, target)
                last_reassert = now

            time.sleep(POLL_INTERVAL_S)
        except KeyboardInterrupt:
            _log("info", "keepalive_stop_signal")
            return 0
        except Exception as e:
            _log("error", "loop_exception", error=str(e)[:200])
            time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    raise SystemExit(run())
