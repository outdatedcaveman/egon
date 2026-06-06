"""Mem0 adapter — embedded, self-improving agent memory.

Mem0 (mem0ai on PyPI) is the lightweight memory layer for AI agents.
Stores facts/preferences/observations, retrieves them by semantic
similarity, auto-improves over time. Pure Python — no separate
service. Apache-2.0.

Role in the Egon unified-mind plan: Mem0 holds the per-agent memory
that gets retrieved per session (e.g., "what did Claude learn last
session that this Codex session should know?"). Letta is the heavier
server-style cousin; Mem0 is the embedded option that works without
running anything extra.

Install: `pip install mem0ai`. The adapter gracefully reports
unconfigured if the library isn't installed, so Egon doesn't crash on
import.

Docs: https://docs.mem0.ai/
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

META = {
    "id": "mem0",
    "label": "Mem0",
    "icon": "🧠",
    "kind": "agent_memory",
    "needs_auth": False,
    "destructive_actions": ["delete_memory"],
    "read_only_default": True,
}


def _config() -> dict:
    try:
        with (ROOT / "egon-config.json").open(encoding="utf-8") as f:
            return (json.load(f).get("mem0") or {})
    except Exception:
        return {}


def _client():
    """Lazily import + construct a Memory client. Returns None if the
    library isn't installed (so the adapter degrades gracefully)."""
    try:
        from mem0 import Memory  # type: ignore
    except Exception:
        return None
    try:
        cfg = _config().get("config") or {}
        return Memory.from_config(cfg) if cfg else Memory()
    except Exception:
        return None


def live_status() -> dict:
    try:
        import mem0  # type: ignore
    except Exception:
        return {"status": "unconfigured",
                "error": "pip install mem0ai (and an embedding/LLM backend it can use)"}
    cli = _client()
    if cli is None:
        return {"status": "error", "error": "mem0 library imported but client init failed"}
    return {"status": "ok", "version": getattr(mem0, "__version__", "?")}


def add(text: str, user_id: str = "bruno",
        metadata: dict | None = None) -> dict:
    cli = _client()
    if cli is None:
        return {"status": "error", "error": "mem0 not installed"}
    try:
        res = cli.add(text, user_id=user_id, metadata=metadata or {})
        return {"status": "ok", "result": res}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


def search(query: str, user_id: str = "bruno",
           limit: int = 10) -> list[dict]:
    cli = _client()
    if cli is None:
        return []
    try:
        hits = cli.search(query, user_id=user_id, limit=limit)
        # Mem0 returns a list of dicts with id, memory, score, metadata.
        return [{"id": h.get("id"), "memory": h.get("memory"),
                 "score": h.get("score"),
                 "metadata": h.get("metadata") or {}} for h in (hits or [])]
    except Exception:
        return []
