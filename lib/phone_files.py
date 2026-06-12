"""Phone file indexer — Android tier of the file-explorer metaharness.

Lists knowledge-bearing files on the phone over wireless ADB (metadata only,
nothing is pulled) and writes state/files_index_phone.jsonl in the same shape
as lib/file_indexer.py rows, with root="phone:<dir>". The Artifacts page
merges both indexes into one provenance-agnostic table.

Scope: the standard user-content dirs, not the whole sdcard — a full -R walk
of /sdcard takes minutes and is mostly app cache noise.
Bruno 2026-06-12, Artifacts metaharness.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "state" / "files_index_phone.jsonl"
ADB = ROOT / "state" / "panop" / "platform-tools" / "platform-tools" / "adb.exe"
DEVICE = "192.168.0.9:5555"

_PHONE_DIRS = ["/sdcard/Download", "/sdcard/Documents", "/sdcard/Books",
               "/sdcard/DCIM", "/sdcard/Pictures/Screenshots"]
_EXTS = {".pdf", ".epub", ".md", ".txt", ".docx", ".doc", ".rtf",
         ".jpg", ".jpeg", ".png", ".csv"}

# `ls -llR` line:  -rw-rw---- 1 u0_a123 media_rw  1234567 2026-06-01 12:34 name.pdf
_LS_RE = re.compile(
    r"^\S+\s+\d+\s+\S+\s+\S+\s+(\d+)\s+(\d{4}-\d{2}-\d{2})\s+\d{2}:\d{2}\s+(.+)$")


def build(timeout_s: int = 120) -> dict:
    """Walk the phone dirs over adb; write the phone index. Metadata only."""
    if not ADB.exists():
        return {"status": "error", "error": "adb.exe not found"}
    t0 = time.time()
    items: list[dict] = []
    try:
        subprocess.run([str(ADB), "connect", DEVICE], capture_output=True,
                       timeout=15, creationflags=0x08000000)
        for base in _PHONE_DIRS:
            try:
                r = subprocess.run(
                    [str(ADB), "-s", DEVICE, "shell", "ls", "-llR", base],
                    capture_output=True, text=True, errors="replace",
                    timeout=timeout_s, creationflags=0x08000000)
            except subprocess.TimeoutExpired:
                continue
            cur_dir = base
            for line in (r.stdout or "").splitlines():
                line = line.rstrip()
                if line.endswith(":") and line.startswith("/"):
                    cur_dir = line[:-1]
                    continue
                m = _LS_RE.match(line)
                if not m:
                    continue
                size, day, name = int(m.group(1)), m.group(2), m.group(3)
                ext = os.path.splitext(name)[1].lower()
                if ext not in _EXTS:
                    continue
                items.append({
                    "path": f"{cur_dir}/{name}",
                    "name": name,
                    "ext": ext,
                    "size": size,
                    "mtime": int(time.mktime(time.strptime(day, "%Y-%m-%d"))),
                    "root": f"phone:{base}",
                })
    except Exception as e:
        if not items:
            return {"status": "error", "error": str(e)[:160]}

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT_PATH.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")
    os.replace(tmp, OUT_PATH)
    return {"status": "ok", "files": len(items),
            "seconds": round(time.time() - t0, 1)}
