"""LiteLLM adapter — one OSS proxy in front of every LLM provider.

Per the Egon-as-ecosystem direction (2026-05-28): every agent body
(Claude Code, Codex, ChatGPT, Gemini, …) calls LiteLLM as the single
proxy. Egon mirrors every request's metadata to the mind for analytics,
cost tracking, and cross-session memory.

Two modes, both supported here:

  1. **Library mode** — `litellm.completion(...)` in-process. Cheap, no
     network, but only this Python process sees it.

  2. **Proxy mode** — a running `litellm --port 4500` exposes an
     OpenAI-compatible API at `http://127.0.0.1:4500`. Every agent on
     the machine points at that URL → every call lands in one log.

The adapter exposes both surfaces. Install: `pip install litellm`. MIT.

Docs: https://docs.litellm.ai/
"""
from __future__ import annotations

import json
import socket
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_PROXY_HOST = "127.0.0.1"
DEFAULT_PROXY_PORT = 4500

META = {
    "id": "litellm",
    "label": "LiteLLM",
    "icon": "🔌",
    "kind": "llm_proxy",
    "needs_auth": False,
    "destructive_actions": [],
    "read_only_default": True,
}


def _config() -> dict:
    try:
        with (ROOT / "egon-config.json").open(encoding="utf-8") as f:
            return (json.load(f).get("litellm") or {})
    except Exception:
        return {}


def _proxy_url() -> str:
    cfg = _config()
    return cfg.get("proxy_url") or f"http://{DEFAULT_PROXY_HOST}:{DEFAULT_PROXY_PORT}"


def _library_available() -> bool:
    try:
        import litellm  # type: ignore  # noqa: F401
        return True
    except Exception:
        return False


def _proxy_alive(timeout: float = 0.4) -> bool:
    try:
        url = _proxy_url()
        # Quick TCP probe on host:port
        host_port = url.replace("http://", "").replace("https://", "").split("/")[0]
        if ":" in host_port:
            h, p = host_port.split(":", 1)
            p = int(p)
        else:
            h, p = host_port, 80
        with socket.create_connection((h, p), timeout=timeout):
            return True
    except Exception:
        return False


def live_status() -> dict:
    lib = _library_available()
    proxy = _proxy_alive()
    if not lib and not proxy:
        return {"status": "unconfigured",
                "error": "Install `pip install litellm` (library) OR run "
                         "`litellm --port 4500` (proxy). Adapter supports both."}
    return {"status": "ok",
            "library_available": lib,
            "proxy_alive": proxy,
            "proxy_url": _proxy_url() if proxy else None}


def proxy_models(timeout: float = 4.0) -> list[dict]:
    """Ask the running proxy what models it exposes (OpenAI shape)."""
    if not _proxy_alive():
        return []
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.get(f"{_proxy_url()}/v1/models")
        if r.status_code != 200:
            return []
        return (r.json() or {}).get("data") or []
    except Exception:
        return []
