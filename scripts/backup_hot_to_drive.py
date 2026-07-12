"""Back up Egon's HOT local files to Google Drive — offsite protection without
putting them ON Drive (Bruno 2026-07-07: everything on Drive, but operate if
Drive is down).

The two files that MUST stay local (a live SQLite DB and an mmap'd vector matrix
would corrupt / crawl on a streaming Drive mount) get a consistent copy pushed to
`<Drive>/hot_backup/` on a schedule instead:

  • state/mind.db            → sqlite3 online-backup (consistent while in use)
  • state/connect_index/*    → plain copy (rewritten atomically by re-embed)

Keeps the last N dated backups on Drive and prunes the rest. Entirely gated on
Drive availability — a no-op (never an error) when Drive is down. Safe to run
from a scheduler or egon_core; single flighted.
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # egon root → import lib.*
from lib import drive
from lib.egon_paths import STATE_DIR, CONNECT_INDEX_DIR

KEEP = 3
_LOCK = threading.Lock()
_MIND_DB = STATE_DIR / "mind.db"
# Phone-app source (local-only git repo — token baked in, must never go to a
# public remote; Drive is Bruno's private account so a copy there is fine).
_EGON_ANDROID = Path.home() / "egon_android"
_ANDROID_SKIP = {"sdk", "jdk", "out", "obj"}


def _sqlite_backup(src: Path, dst: Path) -> bool:
    """Consistent copy of a possibly-live SQLite DB via the online backup API."""
    try:
        s = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True, timeout=30)
        try:
            d = sqlite3.connect(str(dst))
            try:
                s.backup(d)          # atomic, safe while the DB is being written
            finally:
                d.close()
        finally:
            s.close()
        return True
    except Exception:
        # Fallback: plain copy (best-effort; may be slightly inconsistent)
        try:
            shutil.copy2(src, dst)
            return True
        except Exception:
            return False


def _prune(dir_: Path, keep: int = KEEP) -> None:
    try:
        stamps = sorted([d for d in dir_.iterdir() if d.is_dir()], reverse=True)
        for old in stamps[keep:]:
            shutil.rmtree(old, ignore_errors=True)
    except Exception:
        pass


def run() -> dict:
    if not _LOCK.acquire(blocking=False):
        return {"status": "busy"}
    try:
        root = drive.drive_root()
        if root is None:
            return {"status": "skip", "reason": "drive unavailable"}
        base = root / "hot_backup"
        stamp = datetime.now().strftime("%Y-%m-%d")
        dest = base / stamp
        dest.mkdir(parents=True, exist_ok=True)

        done = []
        if _MIND_DB.exists():
            if _sqlite_backup(_MIND_DB, dest / "mind.db"):
                done.append("mind.db")
        # connect_index: copy the load-bearing files (skip nothing huge is fine —
        # this is the whole point, a Drive-side copy of the index).
        if CONNECT_INDEX_DIR.exists():
            ci = dest / "connect_index"
            ci.mkdir(exist_ok=True)
            for f in CONNECT_INDEX_DIR.glob("*"):
                if f.is_file():
                    try:
                        shutil.copy2(f, ci / f.name)
                        done.append(f"connect_index/{f.name}")
                    except Exception:
                        pass
        # egon_android source + its local git history (~KBs; toolchain skipped).
        if _EGON_ANDROID.exists():
            try:
                shutil.copytree(
                    _EGON_ANDROID, dest / "egon_android",
                    ignore=shutil.ignore_patterns(*_ANDROID_SKIP, "*.apk",
                                                  "*.idsig", "*.log",
                                                  "debug.keystore"),
                    dirs_exist_ok=True)
                done.append("egon_android/")
            except Exception:
                pass
        _prune(base)
        return {"status": "ok", "dest": str(dest), "backed_up": done,
                "ts": int(time.time())}
    finally:
        _LOCK.release()


if __name__ == "__main__":
    import json
    print(json.dumps(run(), indent=2, default=str))
