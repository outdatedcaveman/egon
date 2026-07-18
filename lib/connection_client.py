"""Client for the EgonSearch worker — the ONE way to run a vault search.

Bruno 2026-07-12 (RAM re-architecture): connection_engine used to be imported
in-process by mobile_connect, the context broker, and egon_chat — all inside
the always-on mind_service, whose 789MB baseline spiked to ~1.4GB during search
bursts (the 8GB-box freeze class). Every caller now goes through this client:

    worker first  → http://127.0.0.1:8801/connect (scripts/search_worker.py,
                    supervised by egon_core; holds the embedder + turbovec once)
    fallback      → in-process connection_engine.connect, so search NEVER
                    breaks when the worker is down (fallback always).

Same signature and return shape as connection_engine.connect.
"""
from __future__ import annotations

WORKER_URL = "http://127.0.0.1:8801/connect"


def connect(text: str, limit: int = 18, semantic_search: bool = True,
            lexical_search: bool = False, *, timeout_s: float = 90.0,
            allow_fallback: bool = True) -> dict:
    try:
        import httpx
        r = httpx.post(WORKER_URL,
                       json={"text": text, "limit": limit,
                             "semantic_search": semantic_search,
                             "lexical_search": lexical_search},
                       timeout=max(0.2, float(timeout_s)))
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    if not allow_fallback:
        return {"connections": [], "status": "worker_unavailable"}
    from lib.connection_engine import connect as _connect
    return _connect(text, limit, semantic_search, lexical_search)
