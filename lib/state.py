"""Read the agent's last_pass.json from the vault — overlay live adapter data on top.

Split:
- `last_pass.json` owns slow/smart fields: digest, classification, ledger, anomalies.
- Adapters own fast/fresh fields: queue counts, last activity, sparklines.

**Concurrency contract (rewritten 2026-05-20):** `load_last_pass()` is called
from inside NiceGUI's `@ui.page()` handler — i.e. on the asyncio loop's main
thread. Any blocking I/O here STALLS the whole UI. So we now:

  1. Cache the merged result in a module-level dict for `CACHE_TTL_S` seconds.
  2. Page handler reads the cache instantly — never touches disk/network/Drive.
  3. A background thread refreshes the cache. The refresh has a HARD TIMEOUT
     (`REFRESH_TIMEOUT_S`). If Drive or an adapter hangs, the thread is
     abandoned daemon-style; old cache stays valid until the next attempt.

This is what makes Egon not-wedge anymore. Even if Drive sync goes silent for
hours, the UI keeps rendering with the last known state.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

LOCAL_STATE = Path(__file__).resolve().parent.parent / "state"
LOCAL_LAST_PASS = LOCAL_STATE / "last_pass.json"
from lib.egon_paths import VAULT_STATE
LAST_PASS = VAULT_STATE / "last_pass.json"
LAST_PASS_CANDIDATES = (LOCAL_LAST_PASS, LAST_PASS)

CACHE_TTL_S = 60          # serve cached value for up to 60s
REFRESH_TIMEOUT_S = 8     # abandon refresh if it doesn't finish in 8s

_CACHE = {
    "data": {"schema_version": "0.0.0", "_missing": True, "sources": {}, "_cache_age_s": 0},
    "ts":   0.0,
    "refreshing": False,
    "lock": threading.RLock(),
}


def _raw_load() -> dict:
    """Read the most recent snapshot from disk.

    As of 2026-05-20 (snapshot writer landing) the file is authoritative —
    `lib.snapshot` populates the `sources` block for every adapter with a
    per-adapter hard timeout, so we no longer layer live probes on top here.
    Doing both was making `_raw_load` exceed the 8 s refresh window whenever
    a single adapter blocked, which is why the UI looked empty.
    """
    data = {"schema_version": "0.0.0", "_missing": True, "sources": {}}
    try:
        candidates = [
            p for p in LAST_PASS_CANDIDATES
            if p.exists() and p.stat().st_size > 0
        ]
        if candidates:
            newest = max(candidates, key=lambda p: p.stat().st_mtime)
            data = json.loads(newest.read_text(encoding="utf-8"))
            data["_state_path"] = str(newest)
    except Exception:
        data["_corrupt"] = True
    return data


def _refresh_with_timeout() -> None:
    """Run _raw_load() in a daemon thread with a hard wall-clock timeout.
    If the refresh doesn't complete in REFRESH_TIMEOUT_S, the thread is
    abandoned (it keeps running but we don't wait), and the cache is NOT
    updated this round. The previous cache stays valid until next attempt.
    """
    done = threading.Event()
    result = {"data": None}

    def _bg():
        try:
            result["data"] = _raw_load()
        except Exception as e:
            result["data"] = {"_refresh_error": str(e)[:200]}
        finally:
            done.set()

    threading.Thread(target=_bg, daemon=True, name="load_last_pass-refresh").start()
    if done.wait(timeout=REFRESH_TIMEOUT_S):
        if result["data"] is not None:
            with _CACHE["lock"]:
                _CACHE["data"] = result["data"]
                _CACHE["ts"] = time.time()
    # else: timed out — leave old cache alone


def _trigger_refresh_async():
    """Fire a background refresh if one isn't already in flight."""
    with _CACHE["lock"]:
        if _CACHE["refreshing"]:
            return
        _CACHE["refreshing"] = True

    def _run():
        try:
            _refresh_with_timeout()
        finally:
            with _CACHE["lock"]:
                _CACHE["refreshing"] = False

    threading.Thread(target=_run, daemon=True, name="load_last_pass-trigger").start()


def _local_raw_load() -> dict:
    """Fast, local-only load of the last-pass snapshot.
    Guaranteed not to block because it only touches the local C: drive.
    """
    data = {"schema_version": "0.0.0", "_missing": True, "sources": {}}
    try:
        if LOCAL_LAST_PASS.exists() and LOCAL_LAST_PASS.stat().st_size > 0:
            data = json.loads(LOCAL_LAST_PASS.read_text(encoding="utf-8"))
            data["_state_path"] = str(LOCAL_LAST_PASS)
    except Exception:
        data["_corrupt"] = True
    return data


def load_last_pass() -> dict:
    """Cache-first read. Always returns IMMEDIATELY (microseconds).
    If the cache is stale, kicks off a background refresh that won't block us.
    """
    now = time.time()
    trigger = False
    with _CACHE["lock"]:
        if _CACHE["ts"] == 0.0:
            # First load: do a fast, local-only read synchronously (safe and instant)
            try:
                _CACHE["data"] = _local_raw_load()
                _CACHE["ts"] = now
            except Exception:
                pass
            # Trigger background async refresh immediately to check Vault (G:) off-thread
            trigger = True
        else:
            age = now - _CACHE["ts"]
            if age > CACHE_TTL_S:
                trigger = True

    if trigger:
        _trigger_refresh_async()

    with _CACHE["lock"]:
        d = dict(_CACHE["data"])
        d["_cache_age_s"] = round(now - _CACHE["ts"], 1) if _CACHE["ts"] else None
    return d



def vault_relpath() -> str:
    return str(LAST_PASS)


def local_relpath() -> str:
    return str(LOCAL_LAST_PASS)
