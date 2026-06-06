"""Tiny disk cache for UI data providers — stale-while-revalidate.

Bruno 2026-05-22: "Doesn't it have a cache? ... it shouldn't be empty while
loading the new ones. That serves for all databases."

Pattern the widgets use:
  1. On open: read the cached rows from disk and render IMMEDIATELY (instant,
     never blank — even on a cold app start, as long as it ran once before).
  2. Kick the real provider in a background thread.
  3. When it returns, overwrite the cache and re-render.

So reopening is instant, the view is never empty while refreshing, and small
changes just update in place.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

_CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "state" / "ui_cache"


def _path(key: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in key)
    return _CACHE_DIR / f"{safe}.json"


def read(key: str) -> tuple[list[dict], float]:
    """Return (rows, age_seconds). Empty list + inf age if no cache."""
    p = _path(key)
    try:
        if p.exists():
            blob = json.loads(p.read_text(encoding="utf-8"))
            rows = blob.get("rows") or []
            age = time.time() - blob.get("ts", 0)
            return rows, age
    except Exception:
        pass
    return [], float("inf")


def write(key: str, rows: list[dict]) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        # Cap what we persist so a 250k-row source doesn't write a 500MB file.
        # The browsing window + stats are what matter; full data reloads live.
        capped = rows[:5000]
        tmp = _path(key).with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"ts": time.time(), "rows": capped},
                                  ensure_ascii=False), encoding="utf-8")
        tmp.replace(_path(key))
    except Exception:
        pass
