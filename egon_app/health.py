"""Tiny loopback health endpoint for the native desktop app.

The watchdog polls /health. The old NiceGUI app provided that route; the
native PySide app needs the same contract without pulling in a web framework.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

HOST = "127.0.0.1"
PORT_CANDIDATES = (8088, 8089, 8090, 8091)


def _last_pass_info() -> dict[str, Any] | None:
    try:
        from lib.state import LAST_PASS_CANDIDATES

        candidates = [p for p in LAST_PASS_CANDIDATES if p.exists() and p.stat().st_size > 0]
        if not candidates:
            return None
        newest = max(candidates, key=lambda p: p.stat().st_mtime)
        return {
            "path": str(newest),
            "mtime_age_s": round(datetime.now().timestamp() - newest.stat().st_mtime, 1),
            "size_kb": round(newest.stat().st_size / 1024, 1),
        }
    except Exception as e:
        return {"error": str(e)[:160]}


class _Handler(BaseHTTPRequestHandler):
    server_version = "EgonHealth/0.1"

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] != "/health":
            self.send_response(404)
            self.end_headers()
            return
        payload = {
            "ok": True,
            "app": "egon-native",
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "last_pass": _last_pass_info(),
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_health_server() -> tuple[str, int] | None:
    """Start a daemon HTTP server. Returns (host, port), or None if unavailable."""
    for port in PORT_CANDIDATES:
        try:
            server = ThreadingHTTPServer((HOST, port), _Handler)
        except OSError:
            continue
        thread = threading.Thread(
            target=server.serve_forever,
            daemon=True,
            name=f"egon-health-{port}",
        )
        thread.start()
        return HOST, port
    return None
