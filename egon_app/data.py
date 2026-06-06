"""Data hooks for the native Qt UI.

Wraps the pure-logic `lib/` package so the UI never imports lib.* directly.
This keeps a single place to swap data sources later (e.g. live API vs cached
snapshot) without touching every page widget.

All functions are SAFE to call from the Qt main thread — they're either
cache-reads (microseconds) or kick a background daemon. No blocking I/O.
"""
from __future__ import annotations

from typing import Any
from pathlib import Path
import sys

# Ensure the egon root is on sys.path so we can import lib.*
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def last_pass() -> dict[str, Any]:
    """Cache-first read of the last-pass snapshot. Returns immediately."""
    from lib.state import load_last_pass
    return load_last_pass()


def ledger_config() -> dict[str, Any]:
    """User config — dark mode toggle, etc."""
    try:
        from lib.ledger import load_config
        return load_config() or {}
    except Exception:
        return {}


def trigger_pass(pass_kind: str = "daily") -> tuple[bool, str]:
    """Trigger a pass.

    For 'daily' (the default Run-pass-now button) we run the live adapter
    snapshot — fast (~10s), populates the UI immediately. The agent's heavier
    daily pass runs from scheduled task at 23:00. Other pass_kinds delegate
    to the existing actions module if available.
    """
    if pass_kind == "daily":
        try:
            from lib.snapshot import snapshot
            r = snapshot(write=True)
            n_ok = sum(1 for v in r["sources"].values()
                       if str(v.get("status", "")).lower() in ("ok", "alive"))
            if r.get("_write_error"):
                return False, f"snapshot probed {n_ok}/{len(r['sources'])} sources but could not write state"
            msg = f"snapshot ok: {n_ok}/{len(r['sources'])} sources in {r['duration_seconds']}s"
            if r.get("_write_warning"):
                msg += " (local saved; vault sync warning)"
            return True, msg
        except Exception as e:
            return False, f"snapshot failed: {e}"
    try:
        from lib.actions import trigger_pass as _trigger
        return _trigger(pass_kind)
    except Exception as e:
        return False, f"trigger_pass failed: {e}"


def force_refresh() -> None:
    """Refresh lib.state cache from disk immediately after a writer finishes."""
    try:
        from lib import state as _state
        fresh = _state._raw_load()
        with _state._CACHE["lock"]:
            _state._CACHE["data"] = fresh
            import time
            _state._CACHE["ts"] = time.time()
    except Exception:
        try:
            from lib import state as _state
            with _state._CACHE["lock"]:
                _state._CACHE["ts"] = 0.0
        except Exception:
            pass


def panop_status(timeout_s: float = 3.0) -> dict[str, Any]:
    """Direct HTTP status read from Panop subprocess. Never blocks the loop —
    caller must run this on a QThread if they want fresh data."""
    import requests
    try:
        r = requests.get("http://127.0.0.1:8000/api/v1/status", timeout=timeout_s)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        return {"_error": str(e)[:200]}
    return {"_error": f"http {r.status_code}"}
