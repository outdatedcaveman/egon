"""Egon launcher — minimal, robust.

Rewritten 2026-05-20 after discovering pywebview's embedded WebView2 control
was the root cause of unkillable kernel-stuck processes (the wedge pattern
Bruno hit repeatedly). The new launcher does NOT embed any browser:

  1. Start the NiceGUI backend as a subprocess (if not already up).
  2. Open the URL in the user's default browser (real Chrome/Edge/whatever).
  3. Exit. Backend keeps running independently.

No WebView2, no embedded UI, no kernel locks. If anything wedges, regular
process kill from Task Manager works.

Run via Launch Egon.vbs (silent) or Launch Egon.bat (debug console).
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP = ROOT / "egon.py"
HOST = "127.0.0.1"
# Port can be overridden via EGON_PORT env var; egon.py reads the same var.
# If the preferred port is wedged by a kernel-stuck process (rare but happens
# on Windows when WebView2 or similar grabs kernel locks), fall through to
# 8089 → 8090 → 8091 so the user is never blocked by a zombie holding the port.
PREFERRED_PORT = int(os.environ.get("EGON_PORT", "8088"))
PORT_CANDIDATES = [PREFERRED_PORT, 8089, 8090, 8091]
PORT = PREFERRED_PORT  # resolved in _ensure_backend
URL = f"http://{HOST}:{PORT}"
PID_FILE = ROOT / ".egon.pid"
CREATE_NO_WINDOW = 0x08000000
from lib.python_runtime import base_python, runtime_env


def _port_open(timeout: float = 0.5, port: int | None = None) -> bool:
    try:
        with socket.create_connection((HOST, port or PORT), timeout=timeout):
            return True
    except OSError:
        return False


def _port_bindable(port: int) -> bool:
    """True if we could bind a fresh listener on this port (i.e. it's free)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((HOST, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _wait_port(timeout: float = 30.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if _port_open():
            return True
        time.sleep(0.5)
    return False


def _ensure_backend() -> None:
    """Start the NiceGUI backend if it isn't already running on PORT.

    Resolves PORT/URL against PORT_CANDIDATES — if the preferred port is
    held but unresponsive (zombie), falls through to the next free port.
    """
    global PORT, URL
    # 1. If an Egon is already responsive on any candidate, attach.
    for p in PORT_CANDIDATES:
        if _port_open(timeout=0.3, port=p):
            PORT, URL = p, f"http://{HOST}:{p}"
            return
    # 2. Otherwise pick the first bindable port to spawn on.
    for p in PORT_CANDIDATES:
        if _port_bindable(p):
            PORT, URL = p, f"http://{HOST}:{p}"
            break
    else:
        print(f"[egon_launcher] no free port among {PORT_CANDIDATES}", flush=True)
        return
    os.environ["EGON_PORT"] = str(PORT)
    py = str(base_python(ROOT, windowed=True))
    # Detached subprocess — survives the launcher exiting.
    flags = CREATE_NO_WINDOW
    if sys.platform == "win32":
        flags |= subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008  # DETACHED_PROCESS
    proc = subprocess.Popen(
        [py, str(APP)],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=runtime_env(ROOT),
        creationflags=flags,
        close_fds=True,
    )
    try:
        PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    except Exception:
        pass
    if not _wait_port(timeout=30):
        # Backend didn't come up — log + exit; user can investigate
        print(f"[egon_launcher] backend failed to bind {URL} within 30s", flush=True)
        return


def main() -> int:
    _ensure_backend()
    # Open the URL in the user's real browser. No embedded WebView2.
    try:
        webbrowser.open(URL, new=2)
    except Exception as e:
        print(f"[egon_launcher] could not open browser: {e}", flush=True)
        print(f"[egon_launcher] visit {URL} manually", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
