"""Cached live_status() calls — prevents Settings/Home from killing the WebSocket.

Every adapter's live_status() result is cached for 60 seconds. The page renders
instantly using cached values; a background thread refreshes them on a schedule.

This fixes the "Connection lost. Trying to reconnect…" notification that fires
when NiceGUI's WebSocket times out waiting for slow API probes.
"""
from __future__ import annotations

import threading
import time
from importlib import import_module
from typing import Callable

# (adapter_id) -> (timestamp, result_dict)
_CACHE: dict[str, tuple[float, dict]] = {}
_TTL = 60.0
_LOCK = threading.Lock()


def get_status(adapter_id: str, mod_path: str | None = None,
               loader: Callable[[], dict] | None = None) -> dict:
    """Return cached live_status() for `adapter_id`, refreshing if stale.

    Either pass `mod_path` (we'll import + call live_status) or `loader` (a
    custom fn that returns the dict).
    """
    now = time.time()
    with _LOCK:
        cached = _CACHE.get(adapter_id)
        if cached and (now - cached[0] < _TTL):
            return cached[1]

    def _compute() -> dict:
        try:
            if loader is not None:
                return loader()
            if not mod_path:
                return {"status": "error", "error": "no loader configured"}
            mod = import_module(mod_path)
            return mod.live_status()
        except Exception as e:
            return {"status": "error", "error": str(e)[:120]}

    result = _compute()
    with _LOCK:
        _CACHE[adapter_id] = (now, result)
    return result


def invalidate(adapter_id: str | None = None) -> None:
    """Force a re-probe on next get_status."""
    with _LOCK:
        if adapter_id is None:
            _CACHE.clear()
        else:
            _CACHE.pop(adapter_id, None)
