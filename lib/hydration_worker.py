"""Tier-2 hydration worker — text extraction for pinned files.

The big play tier 2 (docs/FILES_INTEGRATION.md). Tier 1 indexed 44k files
by filename; this worker upgrades CHOSEN files to full-text embeddings:

  1. Bruno pins files in the Artifacts tab (📌) → state/hydration_queue.json.
  2. Each run, the worker takes pending entries within a per-run byte budget,
     extracts text (pypdf first _MAX_PAGES pages; .md/.txt read directly)
     into state/file_extracts/<uid>.txt, and marks the entry done.
  3. lib/semantic_index._file_items() appends the extract to the file's
     embedded text, so the next index refresh upgrades those vectors from
     filename-only to filename+content.

Budget rationale: Drive File Stream placeholders FORCE-DOWNLOAD on open.
Reading a pinned PDF hydrates it — that's the point of pinning — but the
per-run cap (default 200 MB) means a bulk pin can never silently pull tens
of GB. Failures are recorded on the queue entry, never retried in a loop.

Runs with the egon_core connect_index cycle (6h), before the index rebuild.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
QUEUE = ROOT / "state" / "hydration_queue.json"
# Configurable so extracts can live on a Drive-synced folder (they grow to GBs).
try:
    from lib.egon_paths import FILE_EXTRACTS_DIR as EXTRACT_DIR
except Exception:
    EXTRACT_DIR = ROOT / "state" / "file_extracts"

_MAX_PAGES = 12          # first pages carry title/abstract/intro — enough
_MAX_CHARS = 20_000      # per extract
_RUN_BUDGET_BYTES = int(200e6)
_TEXT_EXTS = {
    ".md", ".txt", ".tex", ".org", ".csv", ".rtf", ".json", ".jsonl",
    ".yaml", ".yml", ".xml", ".html", ".htm", ".log",
}
SUPPORTED_EXTS = _TEXT_EXTS | {".pdf", ".docx", ".pptx", ".epub", ".odt"}


def uid_for(path: str) -> str:
    """Must match lib/semantic_index._file_items uid derivation."""
    return hashlib.md5(path.encode("utf-8", "ignore")).hexdigest()


def _extract_pdf(path: Path) -> str:
    # PyMuPDF (fitz) is ~10x faster than pypdf for text extraction — the main
    # lever for getting the 175k-doc backlog done in weeks, not months. Falls
    # back to pypdf if fitz chokes on a file. Bruno 2026-06-30.
    parts: list[str] = []
    n_pages = 0
    try:
        import fitz
        doc = fitz.open(str(path))
        try:
            for page in doc:
                if n_pages >= _MAX_PAGES:
                    break
                n_pages += 1
                parts.append(page.get_text() or "")
                if sum(len(p) for p in parts) > _MAX_CHARS:
                    break
        finally:
            doc.close()
    except Exception:
        try:
            from pypdf import PdfReader
            reader = PdfReader(str(path))
            parts, n_pages = [], 0
            for page in reader.pages[:_MAX_PAGES]:
                n_pages += 1
                try:
                    parts.append(page.extract_text() or "")
                except Exception:
                    continue
                if sum(len(p) for p in parts) > _MAX_CHARS:
                    break
        except Exception:
            return ""
    text = "\n".join(parts)[:_MAX_CHARS]
    # Scanned PDF? OCR is OPT-IN only (EGON_OCR_FALLBACK=1). PaddleOCR balloons
    # to multiple GB across a batch of scans and was the #1 RAM hog on the 8GB
    # box — so the always-on loop never OCRs. Run OCR deliberately, small batches,
    # via a dedicated capped tool. Text PDFs still extract cheaply (pypdf above).
    # Bruno 2026-06-30.
    if os.environ.get("EGON_OCR_FALLBACK", "0") in ("1", "true", "yes"):
        if len(text.strip()) < max(120, 40 * max(n_pages, 1)):
            try:
                from lib import ocr_extract
                ocr_text = ocr_extract.ocr_pdf(path)
                if len(ocr_text.strip()) > len(text.strip()):
                    return ocr_text[:_MAX_CHARS]
            except Exception:
                pass
    return text


def _clean_markup(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _extract_zip_members(path: Path, member_prefixes: tuple[str, ...]) -> str:
    parts: list[str] = []
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            low = name.lower()
            if not any(low.startswith(prefix) for prefix in member_prefixes):
                continue
            if not low.endswith((".xml", ".html", ".htm", ".xhtml", ".txt")):
                continue
            try:
                raw = z.read(name).decode("utf-8", "replace")
            except Exception:
                continue
            cleaned = _clean_markup(raw)
            if cleaned:
                parts.append(cleaned)
            if sum(len(part) for part in parts) > _MAX_CHARS:
                break
    return "\n".join(parts)[:_MAX_CHARS]


def _extract(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".pdf":
        return _extract_pdf(path)
    if ext in _TEXT_EXTS:
        raw = path.read_text(encoding="utf-8", errors="replace")[:_MAX_CHARS]
        if ext in {".html", ".htm", ".xml"}:
            return _clean_markup(raw)[:_MAX_CHARS]
        return raw
    if ext == ".docx":
        try:
            return _extract_zip_members(path, ("word/",))
        except Exception:
            return ""
    if ext == ".pptx":
        try:
            return _extract_zip_members(path, ("ppt/slides/", "ppt/notesslides/"))
        except Exception:
            return ""
    if ext == ".epub":
        try:
            return _extract_zip_members(path, ("ops/", "oebps/", "text/", "xhtml/", "html/"))
        except Exception:
            return ""
    if ext == ".odt":
        try:
            return _extract_zip_members(path, ("content.xml",))
        except Exception:
            return ""
    return ""


def process_queue() -> dict:
    """Process pending pins within the byte budget. Returns a status dict."""
    try:
        queue = json.loads(QUEUE.read_text(encoding="utf-8"))
    except Exception:
        return {"status": "empty", "processed": 0}
    if not isinstance(queue, list) or not queue:
        return {"status": "empty", "processed": 0}

    EXTRACT_DIR.mkdir(parents=True, exist_ok=True)
    spent = 0
    done = failed = skipped = 0
    t0 = time.time()
    for entry in queue:
        if entry.get("status") in ("done", "failed"):
            continue
        if spent >= _RUN_BUDGET_BYTES:
            skipped += 1
            continue
        p = Path(entry.get("path", ""))
        if str(p).startswith("/sdcard"):
            entry["status"] = "failed"
            entry["error"] = "phone file — pull it to the PC first"
            failed += 1
            continue
        if not p.exists():
            entry["status"] = "failed"
            entry["error"] = "file not found"
            failed += 1
            continue
        try:
            size = p.stat().st_size
            text = _extract(p)          # hydrates Drive placeholders — intended
            spent += size
            if not text.strip():
                entry["status"] = "failed"
                entry["error"] = "no extractable text (scanned/encrypted?)"
                failed += 1
                continue
            (EXTRACT_DIR / f"{uid_for(str(p))}.txt").write_text(
                text, encoding="utf-8")
            entry["status"] = "done"
            entry["extracted_chars"] = len(text)
            done += 1
        except Exception as e:
            entry["status"] = "failed"
            entry["error"] = str(e)[:140]
            failed += 1

    QUEUE.write_text(json.dumps(queue, indent=2), encoding="utf-8")
    return {"status": "ok", "processed": done, "failed": failed,
            "deferred_over_budget": skipped,
            "bytes_hydrated_mb": round(spent / 1e6, 1),
            "seconds": round(time.time() - t0, 1)}
