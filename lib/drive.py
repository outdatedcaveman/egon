"""Google Drive availability + resilient cold-storage roots.

Bruno 2026-07-07: "every file should ultimately be on Google Drive, not local
(if possible)" — BUT "make sure we can still operate most functions if Drive is
down." Those two together define the whole contract:

  • HOT data (mind.db SQLite, connect_index vectors) NEVER lives on Drive — it
    stays local so search/mind/chat work with Drive offline (and a live DB on a
    streaming Drive mount corrupts). It's backed UP to Drive on a schedule.
  • COLD data (snapshots, archives, exports, backups) lives on Drive to free the
    small local C:, with a LOCAL fallback so a Drive outage never crashes a write
    or hangs the app.

`G:\My Drive` is Google Drive for Desktop in STREAMING mode (verified: ~4GB local
cache backing 400GB+ cloud), so files there don't consume C:.

Everything here is defensive: a fast, cached availability probe; and helpers that
return the local path the moment Drive looks unavailable. Nothing blocks.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from lib.egon_paths import HOME, STATE_DIR

# Candidate Drive mounts, in preference order. First whose PARENT exists wins.
_DRIVE_CANDIDATES = [
    Path(os.environ.get("EGON_DRIVE_ROOT", "")) if os.environ.get("EGON_DRIVE_ROOT") else None,
    Path("G:/My Drive/EgonVault"),
    HOME / "Google Drive" / "EgonVault",
]
_DRIVE_CANDIDATES = [p for p in _DRIVE_CANDIDATES if p is not None]

_PROBE_TTL = 30.0            # seconds — cache the availability answer
_cache = {"ts": 0.0, "ok": False, "root": None}


def _probe(root: Path) -> bool:
    """Is this Drive root mounted AND writable right now? Fast, never hangs."""
    try:
        # The mount (drive letter / parent) must exist first — cheap.
        if not root.parent.exists():
            return False
        root.mkdir(parents=True, exist_ok=True)
        # Confirm writability with a tiny throwaway file (Drive can be mounted
        # read-only / paused). This is a local metadata op on a streaming mount.
        probe = root / ".egon_write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except Exception:
        return False


def drive_root() -> Path | None:
    """The live Drive vault root, or None if Drive is unavailable. Cached."""
    now = time.time()
    if now - _cache["ts"] < _PROBE_TTL:
        return _cache["root"] if _cache["ok"] else None
    ok, chosen = False, None
    for cand in _DRIVE_CANDIDATES:
        if _probe(cand):
            ok, chosen = True, cand
            break
    _cache.update({"ts": now, "ok": ok, "root": chosen})
    return chosen if ok else None


def is_available() -> bool:
    return drive_root() is not None


def cold_dir(name: str, *, local_fallback: Path | None = None) -> Path:
    """Return the directory for a COLD data category `name`.

    Prefers Drive (frees C:); falls back to a local path when Drive is down so
    writes/reads never fail. The local fallback defaults to state/<name>, which
    is also where a junction points once migrated — so callers get a valid,
    existing directory either way.
    """
    root = drive_root()
    if root is not None:
        try:
            d = root / name
            d.mkdir(parents=True, exist_ok=True)
            return d
        except Exception:
            pass
    fb = local_fallback if local_fallback is not None else (STATE_DIR / name)
    try:
        fb.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return fb
