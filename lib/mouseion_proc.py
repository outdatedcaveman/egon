"""Mouseion enrichment — Egon-lifecycle managed subprocess.

Bruno 2026-07-06: the enrichment kept STALLING, and worse, an orchestrator
agent once launched it FACELESS — a mouseion process grinding CPU/RAM with the
app closed, which he explicitly forbade. Both problems have the same fix: tie
enrichment to Egon's lifecycle, exactly like Routster (lib/routster_proc.py):

  • spawned when Egon opens,
  • supervised (we hold the Popen handles),
  • TERMINATED on QApplication.aboutToQuit — so it dies WITH Egon,
  • idempotent — if :7274 is already serving (Bruno launched Mouseion himself)
    we do NOT double-spawn or later kill his session,
  • OPT-IN — `mouseion.auto_start` defaults FALSE. Nothing runs until Bruno
    flips it; no surprise process, no surprise RAM.

We do NOT reinvent the pipeline (Bruno: "improve the existing daemon, don't
reinvent"). We run Mouseion's OWN canonical launchers, unchanged:
  1. run_headless.py — Flask server on :7274 + the real enrichment/sync daemons,
     no GUI (the same app/daemons as the .exe; self-provisions its API key).
  2. supervisor.py  — the continuous DOI-recovery + PDF-fetch loop that drives
     over the :7274 API (raises completeness AND fetches PDFs).

Uses Mouseion's OWN venv interpreter (it has zoterpile's deps) and pythonw so no
console window ever flashes (Bruno's hard rule).
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
HOST = "127.0.0.1"
PORT = 7274
BOOT_TIMEOUT_S = 40           # Flask + 250k-row DB warmup on a cold start
_NO_WINDOW = 0x08000000       # CREATE_NO_WINDOW

# Lifecycle handles for the processes WE spawn (never touch a pre-existing one).
_server: subprocess.Popen | None = None
_supervisor: subprocess.Popen | None = None
_boot_thread: threading.Thread | None = None


def _default_install() -> Path:
    # Genericised (Path.home()) so the public repo carries no personal path.
    return Path.home() / "Desktop" / "mnt" / "outputs" / "zoterpile-main"


def _config() -> dict:
    try:
        cfg = json.loads((ROOT / "egon-config.json").read_text(encoding="utf-8"))
    except Exception:
        cfg = {}
    m = cfg.get("mouseion")
    if not isinstance(m, dict):
        m = {}
    # install_path may also live under apps_cache.mouseion (references_comparer)
    if not m.get("install_path"):
        ac = (cfg.get("apps_cache") or {}).get("mouseion") or {}
        if ac.get("install_path"):
            m = {**m, "install_path": ac["install_path"]}
    return m


def _install_path() -> Path:
    m = _config()
    p = m.get("install_path")
    return Path(p) if p else _default_install()


def _venv_python(install: Path) -> Path | None:
    """Mouseion's OWN venv interpreter (has its deps). pythonw = no console."""
    for name in ("pythonw.exe", "python.exe"):
        cand = install / ".venv" / "Scripts" / name
        if cand.exists():
            return cand
    return None


# ── liveness ────────────────────────────────────────────────────────────────

def _port_listening(timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((HOST, PORT), timeout=timeout):
            return True
    except Exception:
        return False


def is_running() -> bool:
    """True if something is serving Mouseion's API on :7274."""
    return _port_listening()


# ── start / stop ──────────────────────────────────────────────────────────────

def _spawn(pyexe: Path, script_or_args: list[str], install: Path,
           extra_env: dict | None = None) -> subprocess.Popen:
    env = os.environ.copy()
    env["PORT"] = str(PORT)
    env.setdefault("PYTHONUNBUFFERED", "1")
    if extra_env:
        env.update(extra_env)
    return subprocess.Popen(
        [str(pyexe), *script_or_args],
        cwd=str(install),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        creationflags=_NO_WINDOW,
        close_fds=True,
    )


def ensure_running(log_fn=None) -> bool:
    """Start Mouseion's headless server (+ supervisor) if not already up.

    Idempotent and OPT-IN. Returns True if :7274 ends up served."""
    global _server, _supervisor

    def _log(level, **kw):
        if log_fn:
            log_fn(level, **kw)

    if is_running():
        _log("info", event="mouseion_already_running", port=PORT)
        return True

    m = _config()
    if not m.get("auto_start", False):
        _log("info", event="mouseion_autostart_disabled",
             hint="set egon-config.json mouseion.auto_start=true to enable")
        return False

    install = _install_path()
    runner = install / "run_headless.py"
    if not runner.exists():
        _log("error", event="mouseion_runner_missing", path=str(runner))
        return False
    pyexe = _venv_python(install)
    if pyexe is None:
        _log("error", event="mouseion_venv_missing", install=str(install))
        return False

    try:
        _server = _spawn(pyexe, ["run_headless.py"], install)
        _log("info", event="mouseion_server_spawned", pid=_server.pid)
    except Exception as e:
        _log("error", event="mouseion_spawn_failed", error=f"{type(e).__name__}: {str(e)[:200]}")
        return False

    deadline = time.time() + BOOT_TIMEOUT_S
    up = False
    while time.time() < deadline:
        if is_running():
            up = True
            break
        if _server.poll() is not None:
            _log("error", event="mouseion_server_exited_during_boot",
                 returncode=_server.returncode)
            _server = None
            return False
        time.sleep(0.5)
    if not up:
        _log("warn", event="mouseion_boot_timeout", after_s=BOOT_TIMEOUT_S)
        return False
    _log("info", event="mouseion_up", port=PORT)

    # The supervisor (DOI recovery + PDF fetch-all) drives over the :7274 API.
    if m.get("run_supervisor", True) and (install / "supervisor.py").exists():
        try:
            _supervisor = _spawn(pyexe, ["supervisor.py"], install)
            _log("info", event="mouseion_supervisor_spawned", pid=_supervisor.pid)
        except Exception as e:
            _log("warn", event="mouseion_supervisor_failed",
                 error=f"{type(e).__name__}: {str(e)[:200]}")
    return True


def ensure_running_async(log_fn=None) -> None:
    """Fire-and-forget so Egon's UI never blocks on Mouseion's cold boot."""
    global _boot_thread
    _boot_thread = threading.Thread(
        target=ensure_running, args=(log_fn,),
        daemon=True, name="mouseion-bootstrap")
    _boot_thread.start()


def _terminate(proc: subprocess.Popen | None, timeout_s: float = 5.0) -> None:
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=2.0)
                except Exception:
                    pass
    except Exception:
        pass


def stop(timeout_s: float = 5.0) -> None:
    """Terminate the Mouseion processes WE spawned — on QApplication.aboutToQuit.

    Only ours: if :7274 was already up when Egon started (Bruno launched
    Mouseion himself), _server/_supervisor are None and we leave his session
    untouched. This is what makes 'enrichment only while Egon is open' true and
    kills the faceless-process problem for good."""
    global _server, _supervisor
    _terminate(_supervisor, timeout_s)
    _terminate(_server, timeout_s)
    _supervisor = None
    _server = None
