"""Auto-hydration crawler for local Google Drive PDFs.

Tier 2 files integration (docs/FILES_INTEGRATION.md). Scans state/files_index.jsonl,
filters for Google Drive PDFs, checks if they are locally available (fully hydrated)
using Windows file attributes, and extracts their content (first 10 pages) into
state/file_extracts/ for semantic search indexing.
"""
from __future__ import annotations

import ctypes
import json
import os
import time
from pathlib import Path

from lib.hydration_worker import _extract, uid_for, EXTRACT_DIR

ROOT = Path(__file__).resolve().parent.parent
FILES_INDEX_PATH = ROOT / "state" / "files_index.jsonl"
MAX_EXTRACTS_PER_RUN = 30

FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000


def is_locally_available(path: str) -> bool:
    """Check if the cloud-backed file is fully available locally on Windows."""
    if os.name != "nt":
        return True
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(path)
        if attrs == -1:
            return False
        return not bool(attrs & FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS)
    except Exception:
        return False


def run_crawler() -> dict:
    """Crawls files index and extracts text for hydrated Drive PDFs."""
    if not FILES_INDEX_PATH.exists():
        return {"status": "error", "error": "files_index.jsonl not found"}

    t0 = time.time()
    EXTRACT_DIR.mkdir(parents=True, exist_ok=True)

    processed = 0
    scanned = 0
    errors = 0

    try:
        with FILES_INDEX_PATH.open(encoding="utf-8") as f:
            for line in f:
                if processed >= MAX_EXTRACTS_PER_RUN:
                    break
                try:
                    it = json.loads(line)
                except Exception:
                    continue

                path_str = it.get("path")
                if not path_str or not path_str.endswith(".pdf"):
                    continue

                # Filter to Google Drive / My Drive paths
                if "Google Drive" not in path_str and "My Drive" not in path_str:
                    continue

                p = Path(path_str)
                scanned += 1

                # Avoid triggering downloads: check if locally present
                if not is_locally_available(path_str):
                    continue

                digest = uid_for(path_str)
                extract_path = EXTRACT_DIR / f"{digest}.txt"

                # Extract if missing or outdated
                needs_extraction = False
                if not extract_path.exists():
                    needs_extraction = True
                else:
                    try:
                        if p.stat().st_mtime > extract_path.stat().st_mtime:
                            needs_extraction = True
                    except Exception:
                        pass

                if needs_extraction:
                    try:
                        text = _extract(p)
                        if text and text.strip():
                            extract_path.write_text(text, encoding="utf-8")
                            processed += 1
                    except Exception:
                        errors += 1

    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}

    return {
        "status": "ok",
        "scanned_drive_pdfs": scanned,
        "newly_extracted": processed,
        "errors": errors,
        "seconds": round(time.time() - t0, 2)
    }


if __name__ == "__main__":
    print(json.dumps(run_crawler(), indent=2))
