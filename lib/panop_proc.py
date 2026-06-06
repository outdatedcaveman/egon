"""Run Panop's FastAPI server IN-PROCESS inside Egon — no subprocess.

Bruno's hard rule (2026-05-27): nothing runs outside Egon. When Egon is
open, Panop is up; when Egon closes, Panop dies with it. No detached
subprocesses, no daemons, no scheduled tasks, no Startup-folder shortcuts.

How it works
------------
We import `external.panop_server.main:app` (a FastAPI app) and run it
under uvicorn in a **daemon thread** inside Egon's own Python process.
- Daemon thread → dies with Egon. Matches the rule exactly.
- Same surface as before: Panop is reachable at http://127.0.0.1:8000,
  Egon's existing `/panop/*` HTTP proxy keeps working unchanged.
- No process group, no DETACHED_PROCESS, no `subprocess.Popen` — there
  is no second OS process.

This file used to spawn Panop via `subprocess.Popen` with
`CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS` so the server outlived
Egon. That was the keystone violation of the new rule. See
`docs/RECONCILE_2026-05-27.md` for the full plan.
"""
from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PANOP_HOST = "127.0.0.1"
PANOP_PORT = 8000
PANOP_BOOT_TIMEOUT_S = 15
HEALTH_PATH = "/api/v1/status"

# Module-level handles to the running uvicorn server + its thread, so we
# can ask it to shut down on Egon exit.
_server_obj = None
_server_thread: threading.Thread | None = None


def _port_listening() -> bool:
    try:
        with socket.create_connection((PANOP_HOST, PANOP_PORT), timeout=0.5):
            return True
    except Exception:
        return False


def is_running() -> bool:
    """True if something is serving the Panop health endpoint on :8000.

    We use this both before bootstrap (so we don't double-bind if a stray
    subprocess from before this refactor is still around) and as a
    readiness check after bootstrap.
    """
    if not _port_listening():
        return False
    try:
        import requests
        r = requests.get(f"http://{PANOP_HOST}:{PANOP_PORT}{HEALTH_PATH}", timeout=2)
        return r.status_code == 200
    except Exception:
        # Port is bound by *something*; treat that as "running" — better
        # than racing to bind a second time.
        return True


def ensure_running(log_fn=None) -> bool:
    """Start Panop's FastAPI in a daemon thread inside this process.

    Idempotent: if Panop is already up (in this thread OR an external
    leftover binding :8000), returns True without doing anything.
    """
    global _server_obj, _server_thread

    if is_running():
        if log_fn: log_fn("info", event="panop_already_running")
        return True
    if _server_thread is not None and _server_thread.is_alive():
        # A live thread is not enough: uvicorn can be wedged or no longer
        # serving after a failed bind/startup. The supervisor calls this every
        # minute, so let it recover when the socket is actually down.
        if _port_listening():
            return True
        if log_fn: log_fn("warn", event="panop_thread_alive_port_down")
        try:
            if _server_obj is not None:
                _server_obj.should_exit = True
            _server_thread.join(timeout=1.0)
        except Exception:
            pass
        _server_obj = None
        _server_thread = None

    # Pin Panop's data directory to an ABSOLUTE canonical path BEFORE importing
    # its main module. Bruno 2026-05-29: Panop's OUTPUT_DIR() defaulted to the
    # RELATIVE "panop_output", so the in-process server read/wrote harvest files
    # under egon/panop_output while every other part of Egon (adapters,
    # snapshots) used egon/state/panop. The two dirs silently diverged and the
    # UI showed stale/empty harvests. The harvest-file path constants in
    # panop_server.main bind OUTPUT_DIR() at IMPORT time, so we must fix the
    # config before the import. We write the absolute path into panop_env.json
    # (ENV_FILE, read relative to cwd) once; idempotent on subsequent launches.
    try:
        import json as _json
        canon = str((ROOT / "state" / "panop").resolve())
        (ROOT / "state" / "panop").mkdir(parents=True, exist_ok=True)
        env_path = ROOT / "panop_env.json"   # panop_server.ENV_FILE, cwd-relative
        env = {}
        if env_path.exists():
            try:
                env = _json.loads(env_path.read_text(encoding="utf-8"))
            except Exception:
                env = {}
        if env.get("root_dir") != canon:
            env["root_dir"] = canon
            env_path.write_text(_json.dumps(env, indent=4), encoding="utf-8")
            if log_fn: log_fn("info", event="panop_root_pinned", root_dir=canon)
    except Exception as e:
        if log_fn: log_fn("warn", event="panop_root_pin_failed",
                          error=f"{type(e).__name__}: {str(e)[:160]}")

    # Lazy-import so a broken Panop install doesn't block Egon's UI load.
    try:
        from external.panop_server.main import app as panop_app
        import uvicorn
    except Exception as e:
        if log_fn: log_fn("error", event="panop_import_failed",
                          error=f"{type(e).__name__}: {str(e)[:240]}")
        return False

    try:
        config = uvicorn.Config(
            panop_app,
            host=PANOP_HOST,
            port=PANOP_PORT,
            # log_config=None is essential when embedding uvicorn inside Egon:
            # uvicorn's default dictConfig builds a StreamHandler on sys.stderr,
            # which is None under pythonw.exe AND collides with logging already
            # configured by Egon/Panop — that raised
            # "ValueError: Unable to configure formatter 'default'" and the
            # server thread died silently, so :8000 never bound. Passing None
            # skips uvicorn's logging setup entirely; it inherits Egon's.
            # Bruno 2026-05-29.
            log_config=None,
            log_level="warning",
            access_log=False,
            lifespan="on",
        )
        _server_obj = uvicorn.Server(config)
        # uvicorn.Server.run() drives an asyncio loop until should_exit
        # is set. Daemon=True so it terminates the instant Egon exits.
        _server_thread = threading.Thread(
            target=_server_obj.run,
            name="panop-uvicorn",
            daemon=True,
        )
        _server_thread.start()
        if log_fn: log_fn("info", event="panop_thread_started")
    except Exception as e:
        if log_fn: log_fn("error", event="panop_thread_start_failed",
                          error=f"{type(e).__name__}: {str(e)[:240]}")
        return False

    # Wait for the server to start accepting connections.
    deadline = time.time() + PANOP_BOOT_TIMEOUT_S
    while time.time() < deadline:
        if is_running():
            if log_fn: log_fn("info", event="panop_up_inprocess",
                              port=PANOP_PORT)
            return True
        time.sleep(0.3)

    if log_fn: log_fn("warn", event="panop_boot_timeout_inprocess")
    return False


def ensure_running_async(log_fn=None) -> None:
    """Fire-and-forget bootstrap so Egon's UI doesn't block on Panop.

    Same public name as the old subprocess version — callers don't change.
    """
    threading.Thread(
        target=ensure_running,
        args=(log_fn,),
        daemon=True,
        name="panop-bootstrap",
    ).start()


def stop(timeout_s: float = 4.0) -> None:
    """Ask the in-process uvicorn server to shut down. Called at Egon exit.

    Strictly best-effort: even if shutdown doesn't drain cleanly within
    `timeout_s`, the daemon thread will be torn down by the interpreter
    exit anyway (that's why it's a daemon thread). The clean path just
    lets `@app.on_event("shutdown")` hooks run.
    """
    global _server_obj, _server_thread
    if _server_obj is None:
        return
    try:
        _server_obj.should_exit = True
    except Exception:
        pass
    if _server_thread is not None:
        try:
            _server_thread.join(timeout=timeout_s)
        except Exception:
            pass
