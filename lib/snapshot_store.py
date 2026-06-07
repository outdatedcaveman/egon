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

    return local, vault_ok


def latest_snapshot(source: str) -> dict | None:
    """Most recent snapshot for `source`. Prefers local; falls back to vault if local missing."""
    for root in (LOCAL_ROOT, VAULT_ROOT):
        d = root / source
        if not d.exists():
            continue
        files = sorted(d.glob("*.json"), reverse=True)
        if files:
            try:
                return json.loads(files[0].read_text(encoding="utf-8"))
            except Exception:
                continue
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
