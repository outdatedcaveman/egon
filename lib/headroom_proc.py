"""Headroom supervisor — Egon-managed context compression proxy.

Spawns the `headroom proxy --port 8787` process silently on startup,
supervises its health, and terminates it on QApplication.aboutToQuit.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HEADROOM_HOST = "127.0.0.1"
HEADROOM_PORT = 8787
BOOT_TIMEOUT_S = 90   # Python-degraded mode boots slowly (~60s observed 2026-06-11)

# Module-level lifecycle handles.
_proc: subprocess.Popen | None = None
_boot_thread: threading.Thread | None = None


# ── config / discovery ──────────────────────────────────────────────────────

def _load_headroom_config() -> dict:
    """Read the `headroom` block from egon-config.json.

    Defaults to auto_start=True if the block or file is missing.
    """
    cfg_path = ROOT / "egon-config.json"
    if cfg_path.exists():
        try:
            with cfg_path.open(encoding="utf-8") as f:
                cfg = json.load(f)
            h = cfg.get("headroom")
            if isinstance(h, dict):
                return h
        except Exception:
            pass
    return {"auto_start": True}


def _find_headroom_exe() -> Path | None:
    """Find the headroom.exe executable inside Egon's virtual environment or on PATH."""
    # 1. Local virtualenv Scripts (Windows)
    exe_win = ROOT / ".venv" / "Scripts" / "headroom.exe"
    if exe_win.exists():
        return exe_win

    # 2. Local virtualenv bin (Unix/macOS)
    exe_unix = ROOT / ".venv" / "bin" / "headroom"
    if exe_unix.exists():
        return exe_unix

    # 3. System PATH fallback
    path_lookup = shutil.which("headroom")
    if path_lookup:
        return Path(path_lookup)

    return None


# ── liveness probes ─────────────────────────────────────────────────────────

def _port_listening(timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((HEADROOM_HOST, HEADROOM_PORT), timeout=timeout):
            return True
    except Exception:
        return False


def is_running() -> bool:
    """True if something is serving Headroom proxy on :8787."""
    return _port_listening()


# ── start / stop ────────────────────────────────────────────────────────────

def ensure_running(log_fn=None) -> bool:
    """Spawn headroom proxy if it's not already up. Idempotent.

    If Headroom's already running on :8787 (launched manually or by another agent),
    we no-op (no double-spawn).
    """
    global _proc

    if is_running():
        if log_fn:
            log_fn("info", event="headroom_already_running", port=HEADROOM_PORT)
        return True

    cfg = _load_headroom_config()
    if not cfg.get("auto_start", True):
        if log_fn:
            log_fn("info", event="headroom_autostart_disabled",
                   hint="set egon-config.json.headroom.auto_start=true to enable")
        return False

    exe = _find_headroom_exe()
    if exe is None:
        if log_fn:
            log_fn("error", event="headroom_exe_not_found")
        return False

    try:
        # Popen monkey-patch in main.py handles CREATE_NO_WINDOW on Windows.
        # We redirect stdout/stderr to DEVNULL as Egon runs as pythonw.
        env = os.environ.copy()
        env["HEADROOM_REQUIRE_RUST_CORE"] = "false"
        _proc = subprocess.Popen(
            [str(exe), "proxy", "--port", str(HEADROOM_PORT)],
            cwd=str(ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            close_fds=True,
        )
        if log_fn:
            log_fn("info", event="headroom_spawned", pid=_proc.pid, exe=str(exe))
    except Exception as e:
        if log_fn:
            log_fn("error", event="headroom_spawn_failed",
                   error=f"{type(e).__name__}: {str(e)[:240]}")
        return False

    # Wait for Headroom proxy to come up.
    deadline = time.time() + BOOT_TIMEOUT_S
    while time.time() < deadline:
        if is_running():
            if log_fn:
                log_fn("info", event="headroom_up", port=HEADROOM_PORT,
                       took_s=round(BOOT_TIMEOUT_S - (deadline - time.time()), 1))
            return True
        if _proc.poll() is not None:
            if log_fn:
                log_fn("error", event="headroom_exited_during_boot",
                       returncode=_proc.returncode)
            return False
        time.sleep(0.2)

    if log_fn:
        log_fn("warn", event="headroom_boot_timeout",
               after_s=BOOT_TIMEOUT_S, pid=_proc.pid if _proc else None)
    return False


def ensure_running_async(log_fn=None) -> None:
    """Fire-and-forget so Egon's UI startup does not block while Headroom boots."""
    global _boot_thread
    _boot_thread = threading.Thread(
        target=ensure_running, args=(log_fn,),
        daemon=True, name="headroom-bootstrap",
    )
    _boot_thread.start()


def stop(timeout_s: float = 4.0) -> None:
    """Terminate the Headroom proxy subprocess. Called from QApplication.aboutToQuit.

    We only kill it if Egon spawned it.
    """
    global _proc
    if _proc is None:
        return
    try:
        if _proc.poll() is None:
            _proc.terminate()
            try:
                _proc.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                _proc.kill()
                try:
                    _proc.wait(timeout=1.5)
                except Exception:
                    pass
    except Exception:
        pass
    finally:
        _proc = None
