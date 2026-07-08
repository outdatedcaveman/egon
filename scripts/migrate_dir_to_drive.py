"""Move a COLD state directory to Google Drive and leave a junction behind.

Bruno 2026-07-07: cold data lives on Drive (frees the small local C:), but code
paths and Drive-down operation must keep working. So after the data is on Drive
we replace the local dir with a Windows directory JUNCTION pointing at Drive:
existing `STATE_DIR / <name>` references resolve transparently, and if Drive is
offline the readers (all os.walk-based, background embedding/indexing) simply
find nothing that cycle — core search/mind (local) are unaffected.

SAFE sequence, per dir — the local copy is NEVER removed until the Drive copy is
verified byte-for-byte-count:
  1. robocopy (mirror, resumable — skips already-uploaded files)
  2. verify: drive file-count >= local file-count AND drive bytes >= local bytes
  3. remove local dir, then mklink /J <local> <drive>
  4. verify the junction resolves and lists files

Usage:  python scripts/migrate_dir_to_drive.py exports mind_archive
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # egon root → import lib.*
from lib import drive
from lib.egon_paths import STATE_DIR

NO_WINDOW = 0x08000000


def _count_bytes(d: Path) -> tuple[int, int]:
    n = tot = 0
    for f in d.rglob("*"):
        try:
            if f.is_file():
                n += 1
                tot += f.stat().st_size
        except Exception:
            pass
    return n, tot


def _is_junction(p: Path) -> bool:
    """Reliable junction/symlink detection on Windows — os.readlink succeeds on a
    junction and raises OSError otherwise. Critical: we must NEVER `rmdir /S` a
    junction (it can follow into the Drive target), so this gates the swap."""
    try:
        os.readlink(str(p))
        return True
    except OSError:
        return False
    except Exception:
        return False


def migrate(name: str) -> dict:
    local = STATE_DIR / name
    if _is_junction(local):
        return {"dir": name, "status": "already_junction"}
    if not local.exists() or not local.is_dir():
        return {"dir": name, "status": "no_local_dir"}
    root = drive.drive_root()
    if root is None:
        return {"dir": name, "status": "skip_drive_down"}
    dst = root / name
    dst.mkdir(parents=True, exist_ok=True)

    # 1. copy (resumable mirror)
    r = subprocess.run(
        ["robocopy", str(local), str(dst), "/E", "/R:1", "/W:1",
         "/NFL", "/NDL", "/NJH", "/NJS", "/NP"],
        capture_output=True, text=True, creationflags=NO_WINDOW)
    if r.returncode >= 8:                     # 0-7 = success variants
        return {"dir": name, "status": "robocopy_failed", "rc": r.returncode}

    # 2. verify
    ln, lb = _count_bytes(local)
    dn, db = _count_bytes(dst)
    if not (dn >= ln and db >= lb):
        return {"dir": name, "status": "verify_mismatch",
                "local": [ln, lb], "drive": [dn, db]}

    # 3. swap: remove local, junction to Drive
    try:
        subprocess.run(["cmd", "/c", "rmdir", "/S", "/Q", str(local)],
                       capture_output=True, text=True, creationflags=NO_WINDOW)
        if local.exists():
            return {"dir": name, "status": "local_remove_failed"}
        mk = subprocess.run(["cmd", "/c", "mklink", "/J", str(local), str(dst)],
                            capture_output=True, text=True, creationflags=NO_WINDOW)
        if not _is_junction(local):
            return {"dir": name, "status": "junction_failed", "out": (mk.stdout + mk.stderr)[:200]}
    except Exception as e:
        return {"dir": name, "status": "swap_error", "error": str(e)[:200]}

    # 4. verify junction reads
    try:
        _ = list(local.iterdir())
    except Exception as e:
        return {"dir": name, "status": "junction_unreadable", "error": str(e)[:120]}
    return {"dir": name, "status": "migrated", "freed_local_mb": round(lb / 1e6),
            "drive": str(dst)}


def main(names: list[str]) -> None:
    import json
    for name in names:
        print(json.dumps(migrate(name), default=str), flush=True)


if __name__ == "__main__":
    main(sys.argv[1:] or ["exports", "mind_archive"])
