"""Source adapters — fast read-only views of each capture engine + storage.

Each adapter exports `live_status() -> dict` matching the `sources.<name>` slot in
last_pass.json. Failures return {"status": "error", "error": "..."} — never raise.
"""
from __future__ import annotations

from . import instapaper as instapaper
from . import notion as notion
from . import routster as routster
from . import vault as vault

__all__ = ["routster", "vault", "notion", "instapaper"]
