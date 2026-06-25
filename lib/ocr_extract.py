"""OCR extraction for scanned PDFs — PP-OCRv6 (PaddleOCR), CPU-only.

Why PP-OCRv6: released 2026-06-11, it reaches PP-OCRv5_server-level accuracy
(86.2% det / 83.2% rec) at 1.5M–34.5M params — small enough to run fast on
Bruno's 8GB CPU box, and it beats billion-scale VLMs on pure OCR. We render PDF
pages with PyMuPDF (already installed) and OCR the rasters; no GPU needed.

This is the fallback in the hydration pipeline: when a PDF yields little/no
embedded text (i.e. it's a scan), we OCR it so its CONTENT still becomes
searchable + embeddable — closing the last gap in the whole-vault embedding goal.

Graceful by design: if `paddleocr` isn't installed the module reports
unavailable and callers fall back to "" — nothing breaks. Install with:
    .venv\\Scripts\\pip install paddleocr paddlepaddle
Bruno 2026-06-25.
"""
from __future__ import annotations

import os
import threading
import time

_LOCK = threading.Lock()
_ENGINE = None
_TRIED = False
_RAM_FLOOR_GB = float(os.environ.get("EGON_OCR_RAM_FLOOR_GB", "0.7"))
_DPI = int(os.environ.get("EGON_OCR_DPI", "170"))
_MAX_PAGES = int(os.environ.get("EGON_OCR_MAX_PAGES", "25"))
_MAX_CHARS = 200_000


def _free_ram_gb() -> float:
    try:
        import psutil
        return psutil.virtual_memory().available / 1e9
    except Exception:
        return 99.0


def available() -> bool:
    """True if PaddleOCR can be imported (without forcing a model load)."""
    try:
        import paddleocr  # noqa: F401
        return True
    except Exception:
        return False


def _load():
    """Lazy singleton PP-OCR engine. Returns None if unavailable / RAM too tight."""
    global _ENGINE, _TRIED
    if _ENGINE is not None:
        return _ENGINE
    with _LOCK:
        if _ENGINE is not None:
            return _ENGINE
        if _TRIED:
            return None
        _TRIED = True
        if _free_ram_gb() < _RAM_FLOOR_GB:
            return None
        try:
            from paddleocr import PaddleOCR
            # Lean pipeline: skip doc-orientation/unwarping classifiers (extra
            # models, RAM) — scanned text PDFs are already upright. PaddleOCR 3.x
            # selects the latest PP-OCR (v6) detection+recognition models.
            # enable_mkldnn=False is REQUIRED: paddlepaddle 3.3.1's oneDNN backend
            # crashes on PP-OCRv6's det model (ConvertPirAttribute2RuntimeAttribute
            # NotImplementedError). Plain CPU inference works fine. Bruno 2026-06-25.
            _ENGINE = PaddleOCR(
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
                enable_mkldnn=False,
                lang=os.environ.get("EGON_OCR_LANG", "en"),
            )
        except Exception:
            _ENGINE = None
        return _ENGINE


def _ocr_image(engine, arr) -> list[str]:
    """Run OCR on a single page raster (numpy BGR ndarray); return text lines."""
    try:
        res = engine.predict(arr)
    except Exception:
        try:
            res = engine.ocr(arr)            # older API fallback
        except Exception:
            return []
    lines: list[str] = []
    for page in res or []:
        # PaddleOCR 3.x predict() -> dict-like with 'rec_texts'
        try:
            if isinstance(page, dict) and "rec_texts" in page:
                lines.extend(t for t in page["rec_texts"] if t)
                continue
            if hasattr(page, "get") and page.get("rec_texts"):
                lines.extend(t for t in page["rec_texts"] if t)
                continue
        except Exception:
            pass
        # legacy .ocr() -> [[box, (text, conf)], ...]
        try:
            for det in page:
                if isinstance(det, (list, tuple)) and len(det) >= 2:
                    txt = det[1][0] if isinstance(det[1], (list, tuple)) else det[1]
                    if txt:
                        lines.append(str(txt))
        except Exception:
            continue
    return lines


def ocr_pdf(path, max_pages: int = _MAX_PAGES, stop_check=None) -> str:
    """OCR a (scanned) PDF → plain text. Returns '' if OCR is unavailable, RAM is
    too tight, or nothing is read. Renders pages with PyMuPDF at _DPI."""
    engine = _load()
    if engine is None:
        return ""
    try:
        import fitz
        import numpy as np
    except Exception:
        return ""
    out: list[str] = []
    zoom = _DPI / 72.0
    try:
        doc = fitz.open(str(path))
    except Exception:
        return ""
    try:
        for i, page in enumerate(doc):
            if i >= max_pages:
                break
            if stop_check is not None and stop_check():
                break
            if _free_ram_gb() < _RAM_FLOOR_GB:
                break
            try:
                pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
                arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
                    pix.height, pix.width, pix.n)
                if pix.n == 4:                       # RGBA -> RGB
                    arr = arr[:, :, :3]
                arr = arr[:, :, ::-1]                # RGB -> BGR for PaddleOCR
                lines = _ocr_image(engine, arr)
                if lines:
                    out.append("\n".join(lines))
            except Exception:
                continue
            if sum(len(p) for p in out) > _MAX_CHARS:
                break
    finally:
        doc.close()
    return "\n".join(out)[:_MAX_CHARS]


if __name__ == "__main__":
    import sys
    print("paddleocr available:", available())
    if len(sys.argv) > 1:
        t0 = time.time()
        txt = ocr_pdf(sys.argv[1])
        print(f"OCR'd {len(txt)} chars in {time.time()-t0:.1f}s")
        print(txt[:800])
