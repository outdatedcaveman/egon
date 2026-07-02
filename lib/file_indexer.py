"""File indexer — brings Bruno's Drive + PC files into the Connection Engine.

THE BIG PLAY, tier 1 (Bruno 2026-06-12: "we'll move into integrating my
files from Drive and the PC into the flow"). Survey findings that shaped
this design:

  * ~/Google Drive               — 43,476 PDFs / 57 GB: the paper+book
    library. These are Drive File Stream CLOUD PLACEHOLDERS: os.walk sees
    names/sizes for free, but READING CONTENT FORCE-DOWNLOADS the file.
  * ~/Documents                  — ~30k files, mostly code trees; only
    curated extensions are worth indexing.
  * My Drive / EgonVault         — placeholder roots, nearly empty locally.

Therefore: TIER 1 (this module) indexes METADATA ONLY — path, name, parent
folders, ext, size, mtime. Filenames of academic PDFs are semantically rich
("Gärdenfors - Geometry of Meaning.pdf"), so this alone makes the whole
library surface in Connect/bubble results for $0 and zero downloads.

TIER 2 (later, opt-in): budgeted text extraction — first pages of PDFs that
are already hydrated locally or explicitly requested, capped MB/day, feeding
fuller text into the same uid. Never bulk-hydrates the cloud.

Output: state/files_index.jsonl — one {"path","name","ext","size","mtime",
"root"} per line. lib/semantic_index.py reads it as the 'files' source.
Refresh: egon_core 'connect_index' unit cycle (6h) or on demand.

Config (egon-config.json, all optional):
  files_index.roots       — list of dirs (defaults below)
  files_index.exts        — extensions to include (defaults below)
  files_index.max_files   — safety cap per root (default 120_000)
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "state" / "files_index.jsonl"

_HOME = Path.home()
_DEFAULT_ROOTS = [
    str(ROOT / "state" / "exports"),   # extracted vendor data exports
    str(_HOME / "Google Drive"),
    str(_HOME / "My Drive"),
    str(_HOME / "EgonVault"),
    str(_HOME / "Documents"),
    # Reference-manager PDF attachments — the actual article full text, not just
    # metadata. Zotero stores one folder per attachment under storage/; Paperpile
    # syncs PDFs to a Drive folder (cloud placeholders → cloud-hydration guard).
    str(_HOME / "Zotero" / "storage"),
    r"G:\My Drive\Paperpile",
    # Exhaustive-mind extracts: full text pulled out of every AI's raw stores
    # (Antigravity conversations, Claude prompt history/plans/tasks, …) by
    # lib/mind_exhaustive. Indexing them here feeds the whole-vault embedding
    # pipeline, so EVERYTHING the AIs produced is searchable. Bruno 2026-07-01.
    str(ROOT / "state" / "mind_archive" / "_extracts"),
] + [r for r in os.environ.get("EGON_EXTRA_FILE_ROOTS", "").split(os.pathsep) if r.strip()]
# Knowledge-bearing formats only — code trees and binaries stay out.
_DEFAULT_EXTS = {".pdf", ".md", ".txt", ".docx", ".doc", ".epub", ".rtf",
                 ".tex", ".odt", ".pptx", ".csv", ".org", ".json",
                 ".jsonl", ".yaml", ".yml", ".xml", ".html", ".htm",
                 ".log"}
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv",
              "AppData", ".obsidian", ".trash", "$RECYCLE.BIN",
              # Fit raw export dirs — daily metrics become clean entities via
              # the google_fit snapshot; the raw per-day/sample files (10k+)
              # would flood Artifacts. 2026-06-13.
              "All Data", "Daily activity metrics"}


def _config() -> dict:
    try:
        return (json.loads((ROOT / "egon-config.json").read_text(
            encoding="utf-8")).get("files_index") or {})
    except Exception:
        return {}


def build(force: bool = False) -> dict:
    """Crawl the roots, write files_index.jsonl. Metadata only — never opens
    file contents, so Drive placeholders are never hydrated."""
    cfg = _config()
    roots = cfg.get("roots") or _DEFAULT_ROOTS
    exts = set(e.lower() for e in (cfg.get("exts") or _DEFAULT_EXTS))
    cap = int(cfg.get("max_files") or 120_000)

    t0 = time.time()
    items: list[dict] = []
    per_root: dict[str, int] = {}
    for root in roots:
        rp = Path(root)
        if not rp.is_dir():
            continue
        n0 = len(items)
        for dirpath, dirnames, filenames in os.walk(rp):
            dirnames[:] = [d for d in dirnames
                           if not d.startswith(".") and d not in _SKIP_DIRS]
            for fn in filenames:
                ext = os.path.splitext(fn)[1].lower()
                if ext not in exts:
                    continue
                full = os.path.join(dirpath, fn)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                items.append({
                    "path": full,
                    "name": fn,
                    "ext": ext,
                    "size": st.st_size,
                    "mtime": int(st.st_mtime),
                    "root": str(rp),
                })
                if len(items) - n0 >= cap:
                    break
            if len(items) - n0 >= cap:
                break
        per_root[str(rp)] = len(items) - n0

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT_PATH.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")
    os.replace(tmp, OUT_PATH)
    return {"status": "ok", "files": len(items), "per_root": per_root,
            "seconds": round(time.time() - t0, 1)}


def live_status() -> dict:
    """Adapter-shaped status for the sweep."""
    if not OUT_PATH.exists():
        return {"status": "unconfigured",
                "error": "files index not built yet — runs with the 6h "
                         "connect-index refresh, or call file_indexer.build()"}
    try:
        n = sum(1 for _ in OUT_PATH.open(encoding="utf-8"))
        age_h = (time.time() - OUT_PATH.stat().st_mtime) / 3600
        return {"status": "ok" if age_h < 48 else "stale",
                "total_items": n, "age_hours": round(age_h, 1)}
    except Exception as e:
        return {"status": "error", "error": str(e)[:120]}
