"""Snapshot store — double-backup writer.

Every snapshot lands in TWO places:
1. Local:  egon/state/snapshots/<source>/<YYYY-MM-DD>.json
2. Vault:  G:/MetaVault/.../050-Resources/egon/snapshots/<source>/<YYYY-MM-DD>.json
           (Drive-synced → automatic offsite backup)

Atomic writes: tmp → fsync → rename. Both writes must succeed; if vault fails,
local write is kept and a warning is logged. We never delete prior snapshots —
they accumulate by date so you have a full audit trail.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

LOCAL_ROOT = Path(__file__).resolve().parent.parent / "state" / "snapshots"
from lib.egon_paths import VAULT_SNAPSHOTS as VAULT_ROOT

log = logging.getLogger("egon.snapshot_store")


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(path)


def write_snapshot(source: str, payload: dict, when: datetime | None = None) -> tuple[Path, Path | None]:
    """Write `payload` to both local and vault snapshot stores. Returns (local_path, vault_path).
    Vault path is None if the vault write failed (rare; logged)."""
    when = when or datetime.now()
    name = when.strftime("%Y-%m-%d") + ".json"
    local = LOCAL_ROOT / source / name
    vault = VAULT_ROOT / source / name

    _atomic_write(local, payload)

    vault_ok: Path | None = None
    try:
        _atomic_write(vault, payload)
        vault_ok = vault
    except OSError as e:
        log.warning("vault write failed for %s/%s: %s", source, name, e)

    # Keep only the most recent N dated snapshots per source. These were kept
    # forever (Bruno 2026-07-07: snapshots hit ~5GB, zotero alone 2.4GB across
    # 25 dailies) — a daily-refreshed source needs a short history, not months.
    _prune_source(LOCAL_ROOT / source)
    _prune_source(VAULT_ROOT / source)
    return local, vault_ok


import os as _os
_KEEP_SNAPSHOTS = int(_os.environ.get("EGON_SNAPSHOT_KEEP", "7"))


def _prune_source(source_dir: Path, keep: int = _KEEP_SNAPSHOTS) -> None:
    """Delete all but the newest `keep` dated *.json snapshots in a source dir."""
    try:
        files = sorted(source_dir.glob("*.json"), reverse=True)   # newest first by name (YYYY-MM-DD)
        for old in files[keep:]:
            try:
                old.unlink()
            except Exception:
                pass
    except Exception:
        pass


def latest_snapshot(source: str) -> dict | None:
    """Most recent successful/rich snapshot for `source`. Prefers local; falls back to vault."""
    for root in (LOCAL_ROOT, VAULT_ROOT):
        d = root / source
        if not d.exists():
            continue
        files = sorted(d.glob("*.json"), reverse=True)
        valid_candidates = []
        for f in files:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    continue
                if data.get("status") in ("error", "timeout", "unconfigured"):
                    continue
                
                # Check list lengths of main media collection keys
                item_count = 0
                for k in ("items", "films", "books", "shows"):
                    if k in data and isinstance(data[k], list):
                        item_count = max(item_count, len(data[k]))
                
                valid_candidates.append((data, item_count))
            except Exception:
                continue
                
        if valid_candidates:
            # If the newest valid snapshot is nearly empty (< 15 items) but we have a
            # historical snapshot that was rich (>= 15 items), fall back to the rich one
            # to preserve data visibility in the UI.
            newest_data, newest_cnt = valid_candidates[0]
            if newest_cnt < 15:
                richer = [c for c in valid_candidates if c[1] >= 15]
                if richer:
                    return richer[0][0]
            return newest_data
            
    return None


def list_snapshots(source: str) -> list[Path]:
    """All snapshot files we have for `source`, newest first."""
    seen: dict[str, Path] = {}
    for root in (LOCAL_ROOT, VAULT_ROOT):
        d = root / source
        if not d.exists():
            continue
        for f in d.glob("*.json"):
            seen.setdefault(f.name, f)
    return sorted(seen.values(), key=lambda p: p.name, reverse=True)
