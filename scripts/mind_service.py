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


def main() -> int:
    _log("info", "starting", argv=" ".join(sys.argv[1:]))

    if _mind_ready():
        _log("info", "already_running")
        return 0

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
        threading.Thread(target=m_srv.run, name="egon-mobile-uvicorn",
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
        _log("info", "stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
