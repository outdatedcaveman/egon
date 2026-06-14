"""One-shot finalizer for Panop bookmark repair.

If Chrome is running, direct Bookmarks-file edits can be overwritten. This
script waits until desktop Chrome naturally exits, then runs the safe direct
repair pass and verifies that no Panop history row is missing bookmark proof.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "state" / "panop" / "panop_bookmark_finalize_watch.jsonl"
REPAIR = ROOT / "scripts" / "repair_panop_closed_saves.py"


def _log(event: str, **fields):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": datetime.now().isoformat(timespec="seconds"),
                            "event": event, **fields}, ensure_ascii=False) + "\n")


def _chrome_running() -> bool:
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "if (Get-Process chrome -ErrorAction SilentlyContinue) { 'yes' } else { 'no' }"],
            capture_output=True, text=True, timeout=10,
            creationflags=0x08000000,
        )
        return "yes" in (r.stdout or "").lower()
    except Exception:
        return True


def _run_repair(*args: str) -> tuple[int, str]:
    r = subprocess.run(
        [sys.executable, str(REPAIR), *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=900,
        creationflags=0x08000000,
    )
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def main() -> int:
    _log("watch_started")
    while _chrome_running():
        time.sleep(30)
    _log("chrome_closed_detected")
    code, out = _run_repair("--commit", "--sleep", "0.05")
    _log("repair_commit_finished", code=code, output=out[-4000:])
    vcode, vout = _run_repair()
    _log("repair_verify_finished", code=vcode, output=vout[-4000:])
    return 0 if code == 0 and vcode == 0 and '"needs_repair": 0' in vout else 1


if __name__ == "__main__":
    raise SystemExit(main())
