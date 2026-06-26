"""Budgeted automatic text extraction for indexed local files.

Tier 2 files integration (docs/FILES_INTEGRATION.md). Scans
state/files_index.jsonl, extracts text for supported formats that are already
safe to read locally, and writes state/file_extracts/<uid>.txt for semantic
search indexing. Cloud-backed Drive files are only opened when Windows marks
them locally available, so this crawler does not force-download placeholders.
"""
from __future__ import annotations

import ctypes
import json
import os
import shutil
import time
from pathlib import Path

from lib.hydration_worker import EXTRACT_DIR, SUPPORTED_EXTS, _extract, uid_for

ROOT = Path(__file__).resolve().parent.parent
FILES_INDEX_PATH = ROOT / "state" / "files_index.jsonl"
MAX_EXTRACTS_PER_RUN = 50
MAX_BYTES_PER_RUN = int(250e6)

# Opt-in cloud-paper-library hydration: when state/hydrate_cloud.json exists, the
# crawler ALSO extracts cloud-backed Drive PDFs (reading them force-downloads the
# placeholder). Hard-guarded by a disk floor so it can never fill the disk while
# unattended — it self-throttles and lets Drive's LRU cache recycle. Bruno 2026-06-26.
_CLOUD_HYDRATE_FLAG = ROOT / "state" / "hydrate_cloud.json"
_CLOUD_DISK_FLOOR_GB = float(os.environ.get("EGON_CLOUD_HYDRATE_FLOOR_GB", "18"))


def _free_gb(path: str) -> float:
    try:
        drive = os.path.splitdrive(os.path.abspath(path))[0] + os.sep
        return shutil.disk_usage(drive).free / 1e9
    except Exception:
        return 0.0

FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000
_CLOUD_MARKERS = ("google drive", "my drive", "egonvault")


def is_cloud_backed_path(path: str) -> bool:
    low = path.replace("\\", "/").lower()
    return any(marker in low for marker in _CLOUD_MARKERS)


def is_locally_available(path: str) -> bool:
    """Check if a cloud-backed file is fully available locally on Windows."""
    if os.name != "nt":
        return True
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(path)
        if attrs == -1:
            return False
        return not bool(attrs & FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS)
    except Exception:
        return False


def _extract_is_current(path: Path, extract_path: Path) -> bool:
    if not extract_path.exists():
        return False
    try:
        return extract_path.stat().st_mtime >= path.stat().st_mtime
    except Exception:
        return False


def run_crawler(max_extracts: int = MAX_EXTRACTS_PER_RUN,
                max_bytes: int = MAX_BYTES_PER_RUN,
                stop_check=None) -> dict:
    """Extract supported indexed files within a per-run byte/extract budget.

    max_extracts/max_bytes override the default per-run caps so an idle-aware
    driver can pull bigger batches when the PC is deeply idle. stop_check() — if
    given — is polled between files; returning True ends the batch early (used to
    bail the moment the user touches the machine). Bruno 2026-06-24."""
    if not FILES_INDEX_PATH.exists():
        return {"status": "error", "error": "files_index.jsonl not found"}

    t0 = time.time()
    EXTRACT_DIR.mkdir(parents=True, exist_ok=True)

    processed = 0
    scanned = 0
    candidates = 0
    cloud_deferred = 0
    over_budget = 0
    current = 0
    no_text = 0
    errors = 0
    spent = 0
    scanned_drive_pdfs = 0
    cloud_enabled = _CLOUD_HYDRATE_FLAG.exists()

    try:
        with FILES_INDEX_PATH.open(encoding="utf-8") as f:
            for line in f:
                if processed >= max_extracts:
                    break
                if stop_check is not None and stop_check():
                    break
                try:
                    it = json.loads(line)
                except Exception:
                    continue

                path_str = it.get("path") or ""
                ext = (it.get("ext") or Path(path_str).suffix).lower()
                if not path_str or ext not in SUPPORTED_EXTS:
                    continue

                scanned += 1
                if ext == ".pdf" and is_cloud_backed_path(path_str):
                    scanned_drive_pdfs += 1

                if is_cloud_backed_path(path_str) and not is_locally_available(path_str):
                    # Cloud paper library: extract its full text too, but ONLY when
                    # opted in AND disk stays safely above the floor on both the
                    # source drive and C: (reading force-downloads the file).
                    if (not cloud_enabled
                            or _free_gb(path_str) < _CLOUD_DISK_FLOOR_GB
                            or _free_gb(str(ROOT)) < _CLOUD_DISK_FLOOR_GB):
                        cloud_deferred += 1
                        continue
                    # opted-in + disk OK → fall through to download + extract

                p = Path(path_str)
                if not p.exists():
                    continue

                try:
                    size = p.stat().st_size
                except OSError:
                    errors += 1
                    continue
                if spent + size > max_bytes:
                    over_budget += 1
                    continue

                digest = uid_for(path_str)
                extract_path = EXTRACT_DIR / f"{digest}.txt"
                if _extract_is_current(p, extract_path):
                    current += 1
                    continue

                candidates += 1
                try:
                    text = _extract(p)
                    spent += size
                    if text and text.strip():
                        extract_path.write_text(text, encoding="utf-8")
                        processed += 1
                    else:
                        no_text += 1
                except Exception:
                    errors += 1

    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}

    return {
        "status": "ok",
        "scanned_indexed_files": scanned,
        "scanned_drive_pdfs": scanned_drive_pdfs,
        "candidates": candidates,
        "newly_extracted": processed,
        "already_current": current,
        "cloud_deferred": cloud_deferred,
        "deferred_over_budget": over_budget,
        "no_text": no_text,
        "errors": errors,
        "bytes_read_mb": round(spent / 1e6, 1),
        "seconds": round(time.time() - t0, 2),
    }


if __name__ == "__main__":
    print(json.dumps(run_crawler(), indent=2))
