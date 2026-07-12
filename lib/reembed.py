"""Memory-streaming, resumable re-embedder.

Rebuilds the semantic Connect index with a NEW model into a *staging* directory,
without touching the live index — so search keeps working on the current index
the whole time, and we only swap atomically once the new one is built + verified.

Designed for an 8GB-RAM machine (failure-mode-safe, Bruno 2026-06-24):
  • vectors stream to a memory-mapped .npy in batches — the full matrix is NEVER
    held in RAM (the old build's `np.vstack(all)` was the OOM risk);
  • turbovec index is built incrementally in chunks from the memmap;
  • the corpus + texts are snapshotted once, so a run interrupted at idle's end
    RESUMES from where it stopped (no work lost, no 3.6h restart);
  • before every batch it checks free RAM and a stop_check, bailing cleanly when
    the user returns or memory runs low — it can never wedge the machine.

Swap is a separate, deliberate step (`swap_in`) run only after verification.
"""
from __future__ import annotations

import ctypes
import json
import os
import time
from pathlib import Path

import numpy as np

from lib import egon_paths

# Pluggable embedder. model2vec "potion-retrieval-32M" is the chosen backbone:
# static embeddings, ~7500 items/s on this CPU (125x bge-base), tiny RAM, so the
# whole vault re-embeds in minutes with zero OOM risk — the right tool for an
# 8GB machine. bge-base kept as a heavier-but-higher-ceiling alternative.
# Override with EGON_EMBED_MODEL. Bruno 2026-06-24.
MODELS = {
    # Bespoke student distilled from the bge-base teacher (lib/distill_student.py).
    # Quality ~ potion but 256-dim (half the index footprint — leaner on 8GB),
    # and it's OURS (re-distillable on Bruno's own corpus). The default.
    "egon-student-v1": {"type": "static",
        "name": str(egon_paths.STATE_DIR / "egon_student_v1"), "dim": 256,
        "query_prefix": "", "batch": 2000},
    "potion-retrieval-32M": {"type": "static",
        "name": "minishlab/potion-retrieval-32M", "dim": 512,
        "query_prefix": "", "batch": 2000},
    "bge-base": {"type": "st", "name": "BAAI/bge-base-en-v1.5", "dim": 768,
        "query_prefix": "Represent this sentence for searching relevant passages: ",
        "batch": 32},
}
_default_model = "egon-student-v1" if (egon_paths.STATE_DIR / "egon_student_v1").exists() \
    else "potion-retrieval-32M"
ACTIVE = os.environ.get("EGON_EMBED_MODEL", _default_model)
_CFG = MODELS.get(ACTIVE, MODELS["potion-retrieval-32M"])
MODEL_NAME = _CFG["name"]
DIM = _CFG["dim"]
BATCH = _CFG["batch"]


def _make_encoder():
    """Return an encode(texts)->float32 (N,DIM) normalized fn for the active
    model. Static (model2vec) and sentence-transformers share one interface."""
    if _CFG["type"] == "static":
        from model2vec import StaticModel
        sm = StaticModel.from_pretrained(MODEL_NAME)

        def enc(texts):
            v = sm.encode(list(texts)).astype(np.float32)
            nrm = np.linalg.norm(v, axis=1, keepdims=True)
            nrm[nrm == 0] = 1.0
            return v / nrm
        return enc
    from sentence_transformers import SentenceTransformer
    st = SentenceTransformer(MODEL_NAME)

    def enc(texts):
        return st.encode(list(texts), normalize_embeddings=True,
                         batch_size=BATCH, show_progress_bar=False).astype(np.float32)
    return enc

LIVE_DIR = Path(str(egon_paths.CONNECT_INDEX_DIR))
STAGING_DIR = Path(str(egon_paths.CONNECT_INDEX_DIR) + "_staging")
CORPUS_FILE = STAGING_DIR / "corpus.jsonl"     # snapshot: one {meta, text} per line
VEC_FILE = STAGING_DIR / "vectors.npy"
META_FILE = STAGING_DIR / "meta.json"
TURBO_FILE = STAGING_DIR / "turbo.idx"
PROGRESS_FILE = STAGING_DIR / "progress.json"
COMPLETE_FILE = STAGING_DIR / "COMPLETE.json"


def _read_complete(directory: Path) -> dict | None:
    try:
        p = directory / "COMPLETE.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    return None


def _validate_index_dir(directory: Path) -> tuple[bool, str]:
    """Verify an index directory is self-consistent before it can be live.

    This intentionally checks the raw matrix even when turbovec exists because
    vectors.npy is also the incremental reuse store. Google Drive once removed
    that file after a swap, leaving an unusable "complete" live folder.
    """
    complete = _read_complete(directory)
    if not complete:
        return False, "missing COMPLETE.json"
    required = ("vectors.npy", "meta.json", "turbo.idx", "model.json")
    missing = [name for name in required if not (directory / name).exists()]
    if missing:
        return False, "missing " + ",".join(missing)
    try:
        meta = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
        vec = np.lib.format.open_memmap(directory / "vectors.npy", mode="r")
    except Exception as e:
        return False, f"unreadable index: {str(e)[:80]}"
    items = int(complete.get("items") or 0)
    dim = int(complete.get("dim") or 0)
    if len(meta) != items:
        return False, f"meta/items mismatch {len(meta)} != {items}"
    if len(vec.shape) != 2 or vec.shape[0] != items or vec.shape[1] != dim:
        return False, f"vectors shape mismatch {tuple(vec.shape)} != ({items}, {dim})"
    return True, "ok"


def _idle_seconds() -> float:
    """Seconds since the last keyboard/mouse input (Windows)."""
    try:
        class _LII(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
        lii = _LII(); lii.cbSize = ctypes.sizeof(_LII)
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            return max(0.0, (ctypes.windll.kernel32.GetTickCount() - lii.dwTime) / 1000.0)
    except Exception:
        pass
    return 0.0


def _free_ram_gb() -> float:
    try:
        class M(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        m = M(); m.dwLength = ctypes.sizeof(M)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        return m.ullAvailPhys / 1024 ** 3
    except Exception:
        return 999.0


def _snapshot_corpus(limit: int | None = None) -> int:
    """Freeze the corpus to disk once (meta + text per item) so re-embedding is
    resumable and consistent even if the live data changes mid-run. Returns N.
    `limit` caps the corpus (validation runs only)."""
    from lib import semantic_index as si
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    n = 0
    with CORPUS_FILE.open("w", encoding="utf-8") as f:
        import itertools
        # stream: peak RAM = ONE item (the triple-concat OOM-killed at ~1M items)
        for it in itertools.chain(si._archive_items(), si._memory_items(), si._file_items()):
            rec = {"m": {**si._slim(it), "h": si._hash(it["text"])}, "t": it["text"]}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
            if limit and n >= limit:
                break
    return n


def _load_meta_and_count() -> int:
    """Write meta.json from the corpus snapshot (once) and return N.
    Stream-written: accumulating ~1M meta dicts was a second OOM bomb."""
    n = 0
    with CORPUS_FILE.open(encoding="utf-8") as f,          META_FILE.open("w", encoding="utf-8") as mf:
        mf.write("[")
        for line in f:
            if n:
                mf.write(",")
            mf.write(json.dumps(json.loads(line)["m"], ensure_ascii=False))
            n += 1
        mf.write("]")
    return n


def _iter_texts(start: int):
    """Yield (row_index, text) from the corpus snapshot starting at `start`."""
    with CORPUS_FILE.open(encoding="utf-8") as f:
        for i, line in enumerate(f):
            if i < start:
                continue
            yield i, json.loads(line)["t"]


def reembed(stop_check=None, ram_floor_gb: float = 2.0,
            max_seconds: float | None = None, limit: int | None = None,
            idle_abort_s: float | None = None) -> dict:
    """Build/continue the staging index. Returns a status dict. Call repeatedly
    (idle-aware) — it resumes until COMPLETE, then builds turbo + marks done."""
    if COMPLETE_FILE.exists():
        return {"status": "already_complete"}
    STAGING_DIR.mkdir(parents=True, exist_ok=True)

    # Phase 1: snapshot corpus + size the memmap (once).
    if not CORPUS_FILE.exists() or not PROGRESS_FILE.exists():
        n = _snapshot_corpus(limit=limit)
        if n == 0:
            return {"status": "empty"}
        _load_meta_and_count()
        np.lib.format.open_memmap(VEC_FILE, mode="w+", dtype=np.float32, shape=(n, DIM)).flush()
        PROGRESS_FILE.write_text(json.dumps({"done": 0, "total": n}))

    prog = json.loads(PROGRESS_FILE.read_text())
    n, done = prog["total"], prog["done"]
    if done >= n:
        return _finalize(n)

    # Phase 2: stream-embed from `done` to N, writing straight into the memmap.
    encode = _make_encoder()
    vecs = np.lib.format.open_memmap(VEC_FILE, mode="r+")
    t0 = time.time()
    batch_idx, batch_txt = [], []

    def _flush_batch():
        nonlocal done
        if not batch_txt:
            return
        emb = encode(batch_txt)
        for k, row in enumerate(batch_idx):
            vecs[row] = emb[k].astype(np.float32)
        done = batch_idx[-1] + 1
        vecs.flush()
        PROGRESS_FILE.write_text(json.dumps({"done": done, "total": n}))
        batch_idx.clear(); batch_txt.clear()

    for row, text in _iter_texts(done):
        if stop_check and stop_check():
            _flush_batch(); return {"status": "paused", "done": done, "total": n}
        if idle_abort_s is not None and _idle_seconds() < idle_abort_s:
            _flush_batch(); return {"status": "user_active", "done": done, "total": n}
        if _free_ram_gb() < ram_floor_gb:
            _flush_batch(); return {"status": "low_ram", "done": done, "total": n}
        if max_seconds and (time.time() - t0) > max_seconds:
            _flush_batch(); return {"status": "time_budget", "done": done, "total": n}
        batch_idx.append(row); batch_txt.append(text or "")
        if len(batch_txt) >= BATCH:
            _flush_batch()
    _flush_batch()

    if done >= n:
        return _finalize(n)
    return {"status": "progress", "done": done, "total": n}


def _finalize(n: int) -> dict:
    """Build the turbovec index incrementally from the memmap, mark COMPLETE."""
    try:
        import turbovec
    except Exception as e:
        return {"status": "no_turbovec", "error": str(e)[:120]}
    mat = np.lib.format.open_memmap(VEC_FILE, mode="r")
    idx = turbovec.IdMapIndex(DIM)
    CH = 50_000                      # add in chunks so the full matrix never loads
    for s in range(0, n, CH):
        e = min(s + CH, n)
        ids = np.ascontiguousarray(np.arange(s, e), dtype=np.uint64)
        idx.add_with_ids(np.ascontiguousarray(mat[s:e], dtype=np.float32), ids)
    idx.prepare()
    idx.write(str(TURBO_FILE))
    # Couple the model to the index: semantic_index reads this on load to use the
    # right encoder + query instruction. bge-v1.5 wants the instruction on QUERIES.
    (STAGING_DIR / "model.json").write_text(json.dumps({
        "name": MODEL_NAME, "dim": DIM, "type": _CFG["type"],
        "query_prefix": _CFG["query_prefix"]}))
    COMPLETE_FILE.write_text(json.dumps({
        "model": MODEL_NAME, "dim": DIM, "items": n,
        "at": time.strftime("%Y-%m-%dT%H:%M:%S")}))
    ok, reason = _validate_index_dir(STAGING_DIR)
    if not ok:
        try:
            COMPLETE_FILE.unlink()
        except Exception:
            pass
        return {"status": "finalize_invalid", "error": reason}
    return {"status": "ok", "items": n}


def swap_in(timestamp: str) -> dict:
    """Atomically promote the COMPLETE staging index to live. Backs up the
    current live index first (renamed, never deleted) so the swap is reversible.
    `timestamp` names the backup (lib avoids wall-clock). Restart search after.

    Safety: verifies every required part is present before touching the live dir;
    if anything's missing it refuses and leaves the live index untouched."""
    if not COMPLETE_FILE.exists():
        return {"status": "not_complete"}
    ok, reason = _validate_index_dir(STAGING_DIR)
    if not ok:
        return {"status": "incomplete_staging", "error": reason}
    bak = Path(str(LIVE_DIR) + f"_bak_{timestamp}")
    restore = False
    if LIVE_DIR.exists():
        LIVE_DIR.rename(bak)                 # reversible: old index preserved
        restore = True
    try:
        STAGING_DIR.rename(LIVE_DIR)
        # drop the now-redundant build artifacts from the live dir (the corpus
        # snapshot is large; the index files are what search reads).
        for junk in ("corpus.jsonl", "progress.json"):
            try:
                (LIVE_DIR / junk).unlink()
            except Exception:
                pass
        ok, reason = _validate_index_dir(LIVE_DIR)
        if not ok:
            raise RuntimeError(reason)
        return {"status": "swapped", "live": str(LIVE_DIR), "backup": str(bak)}
    except Exception as e:
        failed = Path(str(LIVE_DIR) + f"_failed_{timestamp}")
        if LIVE_DIR.exists():
            LIVE_DIR.rename(failed)
        if restore and bak.exists():
            bak.rename(LIVE_DIR)
        return {"status": "swap_failed_rolled_back", "error": str(e)[:120],
                "live": str(LIVE_DIR), "backup": str(bak),
                "failed": str(failed) if failed.exists() else None}


def status() -> dict:
    if COMPLETE_FILE.exists():
        return {"state": "complete", **json.loads(COMPLETE_FILE.read_text())}
    if PROGRESS_FILE.exists():
        return {"state": "building", **json.loads(PROGRESS_FILE.read_text())}
    return {"state": "not_started"}
