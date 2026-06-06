"""Routster supervisor — Egon-managed Electron subprocess.

Routster is an Electron/Node app, not Python, so it CANNOT live inside
Egon's process the way Panop now does. The next best fit for Bruno's
2026-05-27 rule ("nothing runs outside Egon") is a managed subprocess
that's:

  • spawned by Egon at startup,
  • supervised (we know its pid + Popen handle),
  • TERMINATED by Egon on `QApplication.aboutToQuit`,
  • idempotent — if Routster is already up on :4000 (because Bruno
    launched it directly, or Antigravity did) we don't double-spawn,

so when Egon closes, Routster closes with it. Crashes-during-Egon-life
fall back to Bruno relaunching Egon (we don't auto-respawn — the goal
is "Routster only when Egon is open," not "Routster always up").

The existing `lib/adapters/routster.py` already reads Routster's SQLite
at `%APPDATA%/routster/kms_local_data.sqlite` and POSTs to its HTTP API
on :4000. That adapter is the read/write surface; this file is the
lifecycle.

Antigravity rebuilt Routster.exe earlier today (mtime 2026-05-28 14:24).
We point at the canonical install via the `routster` block of
`egon-config.json` so we always launch what Antigravity / Bruno actually
shipped.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROUTSTER_HOST = "127.0.0.1"
ROUTSTER_PORT = 4000
BOOT_TIMEOUT_S = 20            # Electron + DB warmup can be slow on a cold boot

# Module-level lifecycle handles.
_proc: subprocess.Popen | None = None
_boot_thread: threading.Thread | None = None


# ── config / discovery ──────────────────────────────────────────────────────

def _load_routster_config() -> dict:
    """Read the `routster` block from egon-config.json.

    egon-config.json is gitignored as it can hold secrets; we tolerate it
    being absent and fall back to discovery.
    """
    for cfg_path in (ROOT / "egon-config.json",
                     ROOT / ".backups" / "egon-config_20260527_004500.json"):
        try:
            with cfg_path.open(encoding="utf-8") as f:
                cfg = json.load(f)
            r = cfg.get("routster")
            if not r or not isinstance(r, dict):
                r = cfg.get("apps_cache", {}).get("routster")
            # Only accept a config that ACTUALLY has the block; otherwise
            # fall through to the next candidate (the active config may
            # have been wiped clean by a settings reset, but the backup
            # remembers the install path Bruno set up).
            if isinstance(r, dict) and r:
                return r
        except Exception:
            continue
    return {}


def _find_routster_exe(install_path: str | None) -> Path | None:
    """Find the most recently-built Routster.exe. Prefer the install_path
    declared in egon-config.json; fall back to the well-known build dirs."""
    candidates: list[Path] = []
    if install_path:
        ip = Path(install_path)
        candidates += [
            ip / "dist-app" / "win-unpacked" / "Routster.exe",
            ip / "dist-new" / "Routster-win32-x64" / "Routster.exe",
        ]
    candidates += [
        Path(r"C:/Users/bruno/Documents/Workspace/kms_auto_router/dist-app/win-unpacked/Routster.exe"),
        Path(r"C:/Users/bruno/Documents/Workspace/kms_auto_router/dist-new/Routster-win32-x64/Routster.exe"),
    ]
    existing = [p for p in candidates if p.exists()]
    if not existing:
        return None
    # Pick the most recently modified — that's the build Antigravity/Bruno
    # last shipped.
    return max(existing, key=lambda p: p.stat().st_mtime)


# ── liveness probes ─────────────────────────────────────────────────────────

def _port_listening(timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((ROUTSTER_HOST, ROUTSTER_PORT), timeout=timeout):
            return True
    except Exception:
        return False


def is_running() -> bool:
    """True if something is serving Routster's HTTP API on :4000."""
    return _port_listening()


# ── start / stop ────────────────────────────────────────────────────────────

def ensure_running(log_fn=None) -> bool:
    """Spawn Routster.exe if it's not already up. Idempotent.

    Bruno 2026-05-29: gated by `routster.auto_start` in egon-config.json.
    Default is FALSE — Egon does NOT auto-launch Routster because Bruno
    doesn't always want it on his taskbar. Flip to true once + the
    supervisor takes over. If Routster's already running on :4000 from
    being launched manually, we still no-op (no double-spawn)."""
    global _proc

    if is_running():
        if log_fn: log_fn("info", event="routster_already_running",
                          port=ROUTSTER_PORT)
        return True

    cfg = _load_routster_config()
    if not cfg.get("auto_start", False):
        if log_fn: log_fn("info", event="routster_autostart_disabled",
                          hint="set egon-config.json.routster.auto_start=true to enable")
        return False
    install_path = cfg.get("install_path")
    exe = _find_routster_exe(install_path)
    if exe is None:
        if log_fn: log_fn("error", event="routster_exe_not_found",
                          install_path=install_path)
        return False

    cwd = str(exe.parent if not install_path else install_path)

    try:
        # Routster's main process is a Windows-subsystem Electron binary, so
        # CREATE_NO_WINDOW is a no-op for it (no console to hide). We don't
        # detach — we keep the Popen handle so we can terminate on aboutToQuit.
        # stdout/stderr → DEVNULL because we don't have a console to write
        # them to (Egon runs as pythonw).
        _proc = subprocess.Popen(
            [str(exe)],
            cwd=cwd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        if log_fn: log_fn("info", event="routster_spawned",
                          pid=_proc.pid, exe=str(exe))
    except Exception as e:
        if log_fn: log_fn("error", event="routster_spawn_failed",
                          error=f"{type(e).__name__}: {str(e)[:240]}")
        return False

    # Wait for Routster's HTTP API to come up.
    deadline = time.time() + BOOT_TIMEOUT_S
    while time.time() < deadline:
        if is_running():
            if log_fn: log_fn("info", event="routster_up",
                              port=ROUTSTER_PORT,
                              took_s=round(BOOT_TIMEOUT_S - (deadline - time.time()), 1))
            return True
        if _proc.poll() is not None:
            if log_fn: log_fn("error", event="routster_exited_during_boot",
                              returncode=_proc.returncode)
            return False
        time.sleep(0.4)

    if log_fn: log_fn("warn", event="routster_boot_timeout",
                      after_s=BOOT_TIMEOUT_S, pid=_proc.pid if _proc else None)
    return False


def ensure_running_async(log_fn=None) -> None:
    """Fire-and-forget so Egon's UI doesn't block while Electron boots."""
    global _boot_thread
    _boot_thread = threading.Thread(
        target=ensure_running, args=(log_fn,),
        daemon=True, name="routster-bootstrap",
    )
    _boot_thread.start()


def stop(timeout_s: float = 4.0) -> None:
    """Terminate the Routster subprocess. Called from QApplication.aboutToQuit.

    Strict best-effort: we own Routster's lifecycle only when WE spawned it.
    If `_proc` is None (Routster was already running when Egon started),
    we don't touch it — it's not ours to kill. That preserves the user's
    independent Routster session if they happened to launch it themselves.
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
