"""Panop client — talks to Panop's SUBPROCESS via HTTP on port 8000.

Rewritten 2026-05-20 (post-wedge). Previously this lazy-imported Panop's main
module to call functions directly. That gave fast in-process calls but it
meant Panop's startup side-effects (background ADB-loop thread) ran inside
Egon's process. When that thread hung, Egon wedged.

Now Panop runs as a SEPARATE subprocess on :8000 (managed by lib.panop_proc).
Egon talks to it over HTTP with HARD TIMEOUTS — so a Panop hang results in a
503 here, not a wedge.

All public functions accept a `timeout` arg (default 3s) and gracefully
return None or an empty dict on failure rather than raising. Callers in view
code can render skeletons without worrying about exceptions.
"""
from __future__ import annotations

import time

PANOP_BASE = "http://127.0.0.1:8000"
DEFAULT_TIMEOUT = 3.0

# is_up cache: avoid the socket cost on hot path
_LAST_OK_CHECK: float = 0.0
_LAST_OK_VAL: bool = False
_OK_CACHE_TTL = 15.0


def _http_get(path: str, timeout: float = DEFAULT_TIMEOUT) -> dict | None:
    try:
        import requests
        r = requests.get(f"{PANOP_BASE}{path}", timeout=timeout)
        if r.status_code == 200:
            try: return r.json()
            except Exception: return None
    except Exception:
        return None
    return None


def _http_post(path: str, json_body=None, timeout: float = DEFAULT_TIMEOUT) -> dict | None:
    try:
        import requests
        r = requests.post(f"{PANOP_BASE}{path}", json=json_body, timeout=timeout)
        if r.status_code in (200, 201, 204):
            try: return r.json()
            except Exception: return {}
    except Exception:
        return None
    return None


def is_up(timeout: float = 2.0) -> bool:
    """True iff Panop's :8000 responds within `timeout` seconds. Cached 15s."""
    global _LAST_OK_CHECK, _LAST_OK_VAL
    now = time.time()
    if now - _LAST_OK_CHECK < _OK_CACHE_TTL:
        return _LAST_OK_VAL
    val = _http_get("/api/v1/status", timeout=timeout) is not None
    _LAST_OK_CHECK = now
    _LAST_OK_VAL = val
    return val


# -- public read-only API used by views ------------------------------------

def status(timeout: float = DEFAULT_TIMEOUT) -> dict | None:
    return _http_get("/api/v1/status", timeout=timeout)


def history_meta(timeout: float = DEFAULT_TIMEOUT) -> dict | None:
    return _http_get("/api/v1/history/meta", timeout=timeout)


def bookmarks_pending(timeout: float = DEFAULT_TIMEOUT) -> dict | None:
    return _http_get("/api/v1/bookmarks/pending", timeout=timeout)


def env(timeout: float = DEFAULT_TIMEOUT) -> dict | None:
    return _http_get("/api/v1/env", timeout=timeout)


def get(path: str, timeout: float = DEFAULT_TIMEOUT) -> dict | None:
    """Generic GET — used by views to expose extra endpoints."""
    return _http_get(path if path.startswith("/") else f"/{path}", timeout=timeout)


def post(path: str, json_body=None, timeout: float = DEFAULT_TIMEOUT) -> dict | None:
    """Generic POST — used by views to trigger actions."""
    return _http_post(path if path.startswith("/") else f"/{path}",
                      json_body=json_body, timeout=timeout)
