"""Action triggers — subprocess wrappers around scripts/pass.py.

Imported by views. Returns (ok, message) so the UI can ui.notify() consistently.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Literal

ROOT = Path(__file__).resolve().parent.parent
PASS_PY = ROOT / "scripts" / "pass.py"
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"


def _python() -> str:
    return str(VENV_PY) if VENV_PY.exists() else sys.executable


def trigger_pass(kind: Literal["daily", "inbox", "mirror"] = "daily",
                 dry_run: bool = False) -> tuple[bool, str]:
    """Spawn pass.py in the background. Returns immediately."""
    if not PASS_PY.exists():
        return False, f"pass.py not found at {PASS_PY}"
    args = [_python(), str(PASS_PY), "--kind", kind]
    if dry_run:
        args.append("--dry-run")
    try:
        # Detach: don't block the UI worker. stdout/stderr go to logs/pass-YYYY-MM.log via the script.
        subprocess.Popen(
            args,
            cwd=str(ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        return True, f"{kind} pass queued"
    except Exception as e:
        return False, f"failed to launch: {e}"
