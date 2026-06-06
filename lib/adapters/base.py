"""Adapter base — common contract for every source.

Every adapter exports a module-level dict matching this shape:
    META = {
        "id": "chrome_bookmarks",
        "label": "Chrome Bookmarks",
        "icon": "🔖",
        "kind": "artifact" | "media" | "reference" | "database",
        "needs_auth": False,
        "destructive_actions": ["delete"],   # tokens for type-to-confirm
        "read_only_default": True,
    }
and these functions:
    snapshot() -> dict                # fresh full pull; usually called by daily pass
    live_status() -> dict             # quick liveness check (no full pull)
    items(limit=100) -> list[dict]    # for the table view; reads latest snapshot
    stats() -> dict                   # tiny summary numbers for the page header
    actions: dict[str, callable]      # supported actions; values are functions

If an adapter can't connect (no creds), every function returns {"status": "unconfigured", ...}.
NEVER raise from these functions — the UI catches errors but degrades gracefully.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Adapter(Protocol):
    META: dict

    def snapshot(self) -> dict: ...
    def live_status(self) -> dict: ...
    def items(self, limit: int = 100) -> list[dict]: ...
    def stats(self) -> dict: ...


def empty_stats() -> dict:
    return {"count": 0, "last_synced": None, "status": "unconfigured"}


def unconfigured(needs: str) -> dict:
    return {"status": "unconfigured", "error": f"configure: {needs}"}
