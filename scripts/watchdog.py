"""Egon watchdog — external process that auto-recovers from wedges.

Polls http://127.0.0.1:8088/health every WATCH_INTERVAL seconds. If two
consecutive checks fail, force-kills any Egon process and relaunches it
via the .vbs launcher (silent). Logs every restart to
egon/logs/watchdog-YYYY-MM.log.

Designed to run as a Windows scheduled task triggered "at log on" so it
auto-starts with the desktop session.

Single-instance: refuses to run a second copy via a .pid file.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Silence every subprocess this watchdog spawns (taskkill, the relauncher, ...)
# — same hard rule as the Egon app proper: no console flashes, ever.
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

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PID_FILE = ROOT / ".watchdog.pid"
LOG_FILE = ROOT / "logs" / f"watchdog-{datetime.now():%Y-%m}.log"
# After 2026-05-20 migration to native PySide6 app: launcher is the venv's
# pythonw.exe running the egon_app package. No more .vbs / .bat shim, no
# more browser window — direct module invocation, console hidden by virtue
# of pythonw.exe (not python.exe).
from lib.python_runtime import base_python, runtime_env  # noqa: E402

NATIVE_PY = base_python(ROOT, windowed=True)

HEALTH_URL = "http://127.0.0.1:8088/health"
# Fallback ports — launcher rotates through these if 8088 is wedged by a
# kernel-stuck process. Watchdog must check the same set to know Egon's alive.
HEALTH_URL_CANDIDATES = [
    "http://127.0.0.1:8088/health",
    "http://127.0.0.1:8089/health",
    "http://127.0.0.1:8090/health",
    "http://127.0.0.1:8091/health",
]
WATCH_INTERVAL = 60
HEALTH_TIMEOUT = 8
CONSECUTIVE_FAILS_TO_RESTART = 2
RESTART_COOLDOWN = 120          # don't restart again within this many seconds
MAX_RESTARTS_PER_HOUR = 4       # safety: stop trying after 4 restarts in 60min


def _log(level: str, **kw) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts": datetime.now().isoformat(timespec="seconds"),
             "level": level, **kw}
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _single_instance() -> bool:
    """Return True if we claimed the singleton slot."""
    if PID_FILE.exists():
        try:
            other = int(PID_FILE.read_text())
            # Best-effort check: if the pid exists and is python, abort
            try:
                os.kill(other, 0)
                return False
            except OSError:
                pass  # pid stale; fall through and claim
        except Exception:
            pass
    PID_FILE.write_text(str(os.getpid()))
    return True


def _release_singleton() -> None:
    try:
        PID_FILE.unlink()
    except Exception:
        pass


def _port_open() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 8088), timeout=3):
            return True
    except Exception:
        return False


def _health_ok() -> tuple[bool, str]:
    last_err = "no candidates reachable"
    for url in HEALTH_URL_CANDIDATES:
        try:
            r = requests.get(url, timeout=HEALTH_TIMEOUT)
            if r.status_code == 200 and r.json().get("ok"):
                return True, ""
            last_err = f"{url}: http {r.status_code}"
        except Exception as e:
            last_err = f"{url}: {str(e)[:80]}"
    return False, last_err


def _kill_egon_processes() -> int:
    """Kill any pythonw/python processes that look like Egon. Returns count killed."""
    killed = 0
    try:
        out = subprocess.check_output(
            ["wmic", "process", "where",
             "(name='pythonw.exe' or name='python.exe')",
             "get", "ProcessId,CommandLine", "/format:csv"],
            text=True, errors="replace", timeout=15,
        )
    except Exception as e:
        _log("warn", event="wmic_failed", error=str(e)[:120])
        return 0
    for line in out.splitlines():
        # Match BOTH the legacy launcher AND the native PySide6 app.
        if not any(token in line for token in ("egon.py", "egon_launcher", "egon_app.main")):
            continue
        parts = line.rsplit(",", 1)
        if len(parts) != 2: continue
        try: pid = int(parts[1].strip())
        except ValueError: continue
        # Try graceful first, then force
        for args in (
            ["taskkill", "/PID", str(pid)],
            ["taskkill", "/F", "/T", "/PID", str(pid)],
        ):
            try:
                subprocess.run(args, capture_output=True, timeout=10)
            except Exception:
                pass
        killed += 1
    return killed


def _launch_egon() -> None:
    """Spawn the native Egon app directly via pythonw.exe — no shell, no
    browser, no console flash."""
    if not NATIVE_PY.exists():
        _log("error", event="launcher_missing", path=str(NATIVE_PY))
        return
    try:
        subprocess.Popen(
            [str(NATIVE_PY), "-m", "egon_app.main"],
            cwd=str(ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=runtime_env(ROOT),
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP
                           | getattr(subprocess, "DETACHED_PROCESS", 0x00000008)),
        )
        _log("info", event="launched", target="egon_app.main")
    except Exception as e:
        _log("error", event="launch_failed", error=str(e)[:200])


def main() -> int:
    if not _single_instance():
        print("watchdog already running — exiting")
        return 1
    _log("info", event="watchdog_start", pid=os.getpid())

    fails = 0
    last_restart = 0.0
    restart_times: list[float] = []

    try:
        while True:
            ok, why = _health_ok()
            if ok:
                if fails > 0:
                    _log("info", event="recovered", after_fails=fails)
                fails = 0
            else:
                fails += 1
                _log("warn", event="health_fail", consec=fails, reason=why)
                now = time.time()
                # Prune restart history older than 1 hour
                restart_times = [t for t in restart_times if now - t < 3600]
                if fails >= CONSECUTIVE_FAILS_TO_RESTART:
                    if now - last_restart < RESTART_COOLDOWN:
                        _log("info", event="cooldown_skip")
                    elif len(restart_times) >= MAX_RESTARTS_PER_HOUR:
                        _log("error", event="too_many_restarts",
                             count=len(restart_times),
                             hint="something is fundamentally broken; pausing")
                        # back off — sleep 10 min before resuming polling
                        time.sleep(600)
                        restart_times = []
                    else:
                        _log("warn", event="restarting", consec_fails=fails)
                        killed = _kill_egon_processes()
                        time.sleep(3)
                        _launch_egon()
                        last_restart = now
                        restart_times.append(now)
                        fails = 0
                        _log("info", event="restart_complete", killed=killed)
                        time.sleep(20)  # give Egon time to boot before polling again
            time.sleep(WATCH_INTERVAL)
    except KeyboardInterrupt:
        _log("info", event="watchdog_stop", reason="keyboard_interrupt")
    finally:
        _release_singleton()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
