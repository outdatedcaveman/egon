"""Standalone Egon mind service.

Runs the shared /api/v1/mind/* surface without the desktop UI. This is the
coordination substrate for agents: MCP tools, durable memory, file leases, and
pull ingestion can stay available even when the PySide window is closed.

The service deliberately keeps the existing :8000 contract by mounting the
same Panop FastAPI app that already owns the mind routes. It starts only the
mind ingestion loop, not Egon's UI-only routines.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOST = "127.0.0.1"
PORT = 8000
MIND_STATS_URL = f"http://{HOST}:{PORT}/api/v1/mind/stats"
LOG_PATH = ROOT / "logs" / "mind-service.log"
MOBILE_HOST = "127.0.0.1"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import lib.no_console  # noqa: F401  # keep Windows launches hidden
except Exception:
    pass


def _log(level: str, event: str, **data) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().isoformat(timespec="seconds")
        tail = " ".join(f"{k}={v}" for k, v in data.items())
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(f"{stamp} [{level}] event={event} {tail}\n".rstrip() + "\n")
    except Exception:
        pass


def _port_open(timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((HOST, PORT), timeout=timeout):
            return True
    except Exception:
        return False


def _tcp_open(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _mind_ready(timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(MIND_STATS_URL, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="replace"))
        return resp.status == 200 and body.get("status") == "ok"
    except Exception:
        return False


def _wait_ready(timeout_s: float = 20.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if _mind_ready():
            return True
        time.sleep(0.3)
    return False


def _start_mind_ingest() -> object | None:
    try:
        from lib.mind_ingest import MindIngestService

        svc = MindIngestService()
        svc.start()
        _log("info", "mind_ingest_started")
        return svc
    except Exception as e:
        _log("warn", "mind_ingest_start_failed",
             error=f"{type(e).__name__}: {str(e)[:200]}")
        return None


def _start_orchestrator_service() -> object | None:
    try:
        from lib.orchestrator_service import ensure_orchestrator_service

        svc = ensure_orchestrator_service()
        _log("info", "orchestrator_service_started")
        return svc
    except Exception as e:
        _log("warn", "orchestrator_service_start_failed",
             error=f"{type(e).__name__}: {str(e)[:200]}")
        return None


def _warm_context_stack() -> None:
    """Warm optional semantic helpers without delaying the mind API."""
    try:
        from lib import semantic_index

        semantic_index.warm_model_async()
        _log("info", "context_warmup_started")
    except Exception as e:
        _log("warn", "context_warmup_failed",
             error=f"{type(e).__name__}: {str(e)[:200]}")


def _warm_health_cache() -> None:
    """Seed operational health caches after the API is serving."""
    paths = [
        "/api/v1/mind/scorecard?project=egon&since_hours=24&capsule_budget_chars=1000",
        "/api/v1/mind/enforcement/status?project=egon&since_hours=24",
    ]
    for path in paths:
        try:
            url = f"http://{HOST}:{PORT}{path}"
            with urllib.request.urlopen(url, timeout=30.0) as resp:
                body = resp.read(300).decode("utf-8", errors="replace")
            _log("info", "health_cache_warmed", path=path, status=resp.status, body=body[:120])
        except Exception as e:
            _log("warn", "health_cache_warm_failed",
                 path=path, error=f"{type(e).__name__}: {str(e)[:200]}")


def _run_mobile_connect_only() -> int:
    """Serve Mobile Connect when another process already owns the mind API."""
    try:
        from lib.mobile_connect import build_app, write_url_file, MOBILE_PORT
        import uvicorn
    except Exception as e:
        _log("error", "mobile_connect_import_failed",
             error=f"{type(e).__name__}: {str(e)[:300]}")
        return 1
    if _tcp_open(MOBILE_HOST, MOBILE_PORT):
        _log("info", "mobile_connect_already_running", port=MOBILE_PORT)
        return 0
    try:
        url = write_url_file()
        _log("info", "mobile_connect_only_up", port=MOBILE_PORT, url=url)
        uvicorn.run(
            build_app(),
            host="0.0.0.0",
            port=MOBILE_PORT,
            log_config=None,
            log_level="warning",
            access_log=False,
        )
        return 0
    except Exception as e:
        _log("error", "mobile_connect_only_failed",
             error=f"{type(e).__name__}: {str(e)[:300]}")
        return 1


def _sweep_stale_instances() -> int:
    """DEFINITIVE single-instance (Bruno 2026-07-02: 'this should have been
    dealt with in a definitive manner — I can't have software that relies on an
    AI to check the plumbing'). The old design degraded a new spawn into a
    mobile-only sidecar whenever a prior instance held :8000 — which is exactly
    how THREE instances (one from the previous day, still serving OLD code)
    accumulated. New rule, deterministic: the NEWEST mind_service replaces any
    PRIOR mind_service processes — newest code always wins. Only processes
    whose command line contains 'mind_service' are touched; an Egon GUI serving
    :8000 in-process is respected (its own restart handles its reloads)."""
    import subprocess
    me = os.getpid()
    killed = 0
    try:
        from lib.mobile_connect import MOBILE_PORT
    except Exception:
        MOBILE_PORT = 8765
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "tcp"],
                             capture_output=True, text=True, timeout=10,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except Exception:
        return 0
    pids: set[int] = set()
    for line in out.splitlines():
        if "LISTENING" not in line:
            continue
        if f":{PORT} " in line or f":{MOBILE_PORT} " in line:
            try:
                pids.add(int(line.split()[-1]))
            except Exception:
                continue
    pids.discard(me)
    for pid in sorted(pids):
        try:
            res = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
                capture_output=True, text=True, timeout=12,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            cmdline = res.stdout or ""
            if "mind_service" not in cmdline:
                continue  # not ours — respect it
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            killed += 1
            _log("info", "stale_instance_replaced", pid=pid)
        except Exception as e:
            _log("warn", "stale_sweep_error", pid=pid, error=str(e)[:80])
    if killed:
        deadline = time.time() + 15
        while time.time() < deadline and (_port_open() or _tcp_open(MOBILE_HOST, MOBILE_PORT)):
            time.sleep(1)
    return killed


def main() -> int:
    _log("info", "starting", argv=" ".join(sys.argv[1:]))

    # Replace any prior mind_service instances FIRST — newest code wins.
    _sweep_stale_instances()

    if _mind_ready():
        # Something non-mind_service (e.g. the Egon GUI's in-process Panop)
        # legitimately owns the mind — serve only the phone surface beside it.
        _log("info", "mind_already_running")
        return _run_mobile_connect_only()

    force_start = os.environ.get("EGON_MIND_SERVICE_FORCE") == "1"
    if force_start:
        _log("warn", "singleton_bypass_requested")
        claimed = True
    else:
        try:
            from lib.single_instance_mutex import claim_or_exit
            claimed = claim_or_exit("Egon-Mind-Service-2026-06")
        except Exception:
            claimed = True
    if not claimed:
        _log("info", "singleton_already_claimed")
        if _wait_ready(timeout_s=25.0):
            _log("info", "singleton_peer_ready")
            return 0
        if _port_open():
            _log("warn", "singleton_peer_port_busy_without_mind")
            return 2
        _log("warn", "singleton_peer_not_ready")
        return 2

    if _port_open() and not _mind_ready():
        _log("warn", "port_8000_busy_without_mind")
        return 2

    try:
        from external.panop_server.main import app as panop_app
        import uvicorn
    except Exception as e:
        _log("error", "import_failed", error=f"{type(e).__name__}: {str(e)[:300]}")
        return 1

    ingest = _start_mind_ingest()
    orchestrator = _start_orchestrator_service()
    threading.Thread(target=_warm_context_stack, name="egon-context-warmup",
                     daemon=True).start()
    config = uvicorn.Config(
        panop_app,
        host=HOST,
        port=PORT,
        log_config=None,
        log_level="warning",
        access_log=False,
        lifespan="on",
    )
    server = uvicorn.Server(config)

    # Mobile Connect (strategy #4): a SEPARATE tiny token-guarded app on the
    # LAN (0.0.0.0:8765) so Bruno's phone can paste text and get connections +
    # synthesis. The full mind API above stays loopback-only — only /m,
    # /m/connect and /m/synthesize are exposed, all requiring the secret token
    # from egon-config.json. See lib/mobile_connect.py.
    try:
        from lib.mobile_connect import build_app, write_url_file, MOBILE_PORT
        m_cfg = uvicorn.Config(build_app(), host="0.0.0.0", port=MOBILE_PORT,
                               log_config=None, log_level="warning",
                               access_log=False)
        m_srv = uvicorn.Server(m_cfg)

        def _run_mobile() -> None:
            # Half-alive is forbidden: if the phone surface can't bind, the
            # whole service exits and egon_core respawns a clean one (whose
            # sweep clears whatever held the port). Deterministic plumbing —
            # never a mind on :8000 silently missing its :8765. 2026-07-02.
            try:
                m_srv.run()
            except Exception as e:
                _log("error", "mobile_bind_failed_fatal",
                     error=f"{type(e).__name__}: {str(e)[:200]}")
                os._exit(3)

        threading.Thread(target=_run_mobile, name="egon-mobile-uvicorn",
                         daemon=True).start()
        url = write_url_file()
        _log("info", "mobile_connect_up", port=MOBILE_PORT, url=url)
    except Exception as e:
        _log("warn", "mobile_connect_failed",
             error=f"{type(e).__name__}: {str(e)[:200]}")

    def _run() -> None:
        try:
            server.run()
        except Exception as e:
            _log("error", "uvicorn_failed",
                 error=f"{type(e).__name__}: {str(e)[:300]}")

    th = threading.Thread(target=_run, name="egon-mind-uvicorn", daemon=False)
    th.start()
    if _wait_ready():
        _log("info", "ready", port=PORT)
        threading.Thread(target=_warm_health_cache, name="egon-health-cache-warmup",
                         daemon=True).start()
    else:
        _log("warn", "ready_timeout", port=PORT)

    try:
        th.join()
    finally:
        if ingest is not None:
            try:
                ingest.stop()
            except Exception:
                pass
        if orchestrator is not None:
            try:
                orchestrator.stop()
            except Exception:
                pass
        _log("info", "stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
