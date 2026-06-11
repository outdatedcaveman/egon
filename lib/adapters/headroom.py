"""Headroom adapter — Context compression layer for AIs.

Probes the local Headroom proxy status on port 8787.
"""
from __future__ import annotations

import socket
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_PORT = 8787

META = {
    "id": "headroom",
    "label": "Headroom",
    "icon": "🧠",
    "kind": "llm_compressor",
    "needs_auth": False,
    "destructive_actions": [],
    "read_only_default": True,
}


def _proxy_alive(timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", DEFAULT_PORT), timeout=timeout):
            return True
    except Exception:
        return False


def _library_available() -> bool:
    try:
        import headroom  # type: ignore  # noqa: F401
        return True
    except Exception:
        return False


def live_status() -> dict:
    lib = _library_available()
    alive = _proxy_alive()
    if not alive:
        return {
            "status": "error",
            "error": "Headroom proxy not running on port 8787.",
            "library_installed": lib,
        }
    return {
        "status": "ok",
        "port": DEFAULT_PORT,
        "library_installed": lib,
        "proxy_url": f"http://127.0.0.1:{DEFAULT_PORT}",
        "note": "Compressing LLM context automatically.",
    }
