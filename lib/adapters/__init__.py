"""Source adapters — fast read-only views of each capture engine + storage.

Each adapter exports `live_status() -> dict` matching the `sources.<name>` slot in
last_pass.json. Failures return {"status": "error", "error": "..."} — never raise.

Submodules load lazily (PEP 562): importing lib.adapters used to eagerly pull
instapaper -> httpx (~1.7s) into every Egon boot even when only one adapter
was needed. `from lib.adapters import X` still works unchanged.
Bruno 2026-06-11 startup-perf pass.
"""
from __future__ import annotations

import importlib

__all__ = ["routster", "vault", "notion", "instapaper"]


def __getattr__(name: str):
    if name in __all__:
        return importlib.import_module(f".{name}", __name__)
    raise AttributeError(f"module 'lib.adapters' has no attribute {name!r}")
