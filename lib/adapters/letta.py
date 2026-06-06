"""Letta adapter — stateful agent server (formerly MemGPT).

Letta runs as a separate process and exposes a REST + WebSocket API. It
gives stateful agents with long-term memory, tool use, and persistence
out of the box — exactly the "memory cortex" the Egon unified-mind plan
needs.

Install (any of):
  pip install letta             # then `letta server` to run
  docker run -p 8283:8283 letta-image
  uvx letta server              # uv-managed venv

Default port: 8283. Egon doesn't manage the Letta process — Letta is
big enough to deserve its own lifecycle. The adapter probes it.

Docs: https://docs.letta.com/
"""
from __future__ import annotations

import json
import socket
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8283

META = {
    "id": "letta",
    "label": "Letta",
    "icon": "🧬",
    "kind": "agent_server",
    "needs_auth": False,
    "destructive_actions": ["delete_agent", "delete_memory"],
    "read_only_default": True,
}


def _config() -> dict:
    try:
        with (ROOT / "egon-config.json").open(encoding="utf-8") as f:
            return (json.load(f).get("letta") or {})
    except Exception:
        return {}


def _base_url() -> str:
    cfg = _config()
    return cfg.get("url") or f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"


def _port_open(timeout: float = 0.4) -> bool:
    try:
        url = _base_url()
        host_port = url.replace("http://", "").replace("https://", "").split("/")[0]
        if ":" in host_port:
            h, p = host_port.split(":", 1); p = int(p)
        else:
            h, p = host_port, 80
        with socket.create_connection((h, p), timeout=timeout):
            return True
    except Exception:
        return False


def live_status(timeout: float = 4.0) -> dict:
    if not _port_open():
        return {"status": "unconfigured",
                "error": "Letta server not running. `pip install letta && letta server`"}
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.get(f"{_base_url()}/v1/health")
        if r.status_code == 200:
            d = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            return {"status": "ok", **d}
        # Some Letta builds answer 404 on /v1/health but still serve /v1/agents
        if r.status_code == 404:
            r2 = httpx.get(f"{_base_url()}/v1/agents", timeout=4)
            if r2.status_code in (200, 401):
                return {"status": "ok", "note": "server up; no /v1/health endpoint"}
        return {"status": "error", "error": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


def list_agents() -> list[dict]:
    if not _port_open():
        return []
    try:
        with httpx.Client(timeout=8) as c:
            r = c.get(f"{_base_url()}/v1/agents")
        if r.status_code != 200:
            return []
        agents = r.json()
        if not isinstance(agents, list):
            agents = (agents or {}).get("agents") or []
        return [{
            "id": a.get("id"),
            "name": a.get("name", ""),
            "created_at": a.get("created_at"),
            "memory_blocks": len((a.get("memory") or {}).get("blocks") or []),
        } for a in agents]
    except Exception:
        return []
