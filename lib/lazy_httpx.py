"""Lazy httpx shim — `from lib.lazy_httpx import httpx`, use as normal.

httpx costs ~1.7-2.3s to import (it drags in rich/pygments via its CLI
module) and 27+ modules imported it at module level, so Egon's boot paid
that price several times over before any window appeared. This proxy delays
the real import until the first attribute access (first actual HTTP call).

2026-06-11 startup-perf pass. Found via `python -X importtime`:
egon_app.pages.navigation -> lib.adapters.routster -> httpx was 2.3s of a
4.2s cold import of egon_app.main.
"""
from __future__ import annotations

import importlib
from typing import Any


class _LazyHttpx:
    _mod = None

    def __getattr__(self, name: str) -> Any:
        if _LazyHttpx._mod is None:
            _LazyHttpx._mod = importlib.import_module("httpx")
        return getattr(_LazyHttpx._mod, name)


httpx = _LazyHttpx()
