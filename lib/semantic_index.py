"""Semantic index for the Connection Engine.

Bruno 2026-06-10: lexical matching gave weak suggestions ("meaning" only found
things literally containing "meaning"). This adds true semantic search: every
item in your archives (Instapaper, Zotero, Paperpile, Kindle, Letterboxd,
YouTube, bookmarks, Notion, …) and every durable mind memory is embedded once
with a local MiniLM model (sentence-transformers, already installed) and cached.
A query is embedded and cosine-ranked against the cached matrix, so it finds
conceptually related material even when no words overlap (e.g. "neutral monism"
→ a Spinoza paper you never tagged "monism").

  • 100% local, no API/tokens. Model is all-MiniLM-L6-v2 (384-dim, ~80MB).
  • Index is built in the background and cached to state/connect_index/.
    Incremental by content hash — re-runs only embed new/changed items.
  • Cosine over the whole matrix is sub-10ms; the only cost is the one-time
    build (tens of seconds), done off the request path.

API:
    from lib import semantic_index as si
    si.ensure_built_async()          # kick a background build/refresh
    si.search("query text", top_k=40)  -> [{uid,source,title,url,snippet,score}]
    si.is_ready()                    -> bool
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "state" / "mind.db"
# Index location is configurable (EGON_CONNECT_INDEX_DIR) so the multi-GB
# vectors/meta/turbo can live on a Drive-synced folder instead of the system
# drive. Defaults to the local state dir. Bruno 2026-06-24.
try:
    from lib.egon_paths import CONNECT_INDEX_DIR as INDEX_DIR
    from lib.egon_paths import FILE_EXTRACTS_DIR as _EXTRACT_DIR
except Exception:
    INDEX_DIR = ROOT / "state" / "connect_index"
    _EXTRACT_DIR = ROOT / "state" / "file_extracts"
VEC_PATH = INDEX_DIR / "vectors.npy"
META_PATH = INDEX_DIR / "meta.json"

MODEL_NAME = "all-MiniLM-L6-v2"
_MAX_TEXT = 480           # chars fed to the encoder per item (title+snippet)


def _index_model() -> dict:
    """The model the CURRENT index was built with, read from INDEX_DIR/model.json.
    Couples the model to the index so swapping the index dir automatically
    switches the embedding model AND its query-instruction prefix (bge-style
    asymmetric retrieval). Falls back to the historical MiniLM default (no prefix)
    when the file is absent, so the old index keeps working unchanged."""
    try:
        p = INDEX_DIR / "model.json"
        if p.exists():
            d = json.loads(p.read_text(encoding="utf-8"))
            return {"name": d.get("name") or MODEL_NAME,
                    "type": d.get("type") or "st",
                    "query_prefix": d.get("query_prefix") or ""}
    except Exception:
        pass
    return {"name": MODEL_NAME, "type": "st", "query_prefix": ""}

_model = None
_model_key = None        # (name, type) the cached model was built for
_model_lock = threading.Lock()
_build_lock = threading.Lock()
_building = False

# in-memory cache of the loaded index
_vecs: np.ndarray | None = None
_meta: list[dict] | None = None
_meta_mtime: float = 0.0


# ── model ────────────────────────────────────────────────────────────────────
def _load_model():
    # Reload when the index's model changes (the model.json name/type differs from
    # what's cached) — so an autonomous index swap to a different embedder
    # (potion->student) takes effect without restarting the service.
    global _model, _model_key
    mi = _index_model()
    key = (mi.get("name"), mi.get("type"))
    if _model is not None and _model_key == key:
        return _model
    with _model_lock:
        if _model is not None and _model_key == key:
            return _model
        try:
            if mi.get("type") == "static":
                from model2vec import StaticModel
                _model = StaticModel.from_pretrained(mi["name"])
            else:
                from sentence_transformers import SentenceTransformer
                _model = SentenceTransformer(mi["name"])
            _model_key = key
        except Exception:
            _model = None
        return _model


def model_loaded() -> bool:
    """True once the local embedding model is already resident in this process."""
    return _model is not None


def warm_model_async() -> None:
    """Load the embedding model off the request path."""
    threading.Thread(target=_load_model, daemon=True,
                     name="semantic-index-model-warmup").start()


def _embed(texts: list[str], is_query: bool = False) -> np.ndarray | None:
    m = _load_model()
    if m is None:
        return None
    mi = _index_model()
    if is_query:
        # bge-style models retrieve better when the QUERY (not the passages) is
        # prefixed with an instruction; no-op for MiniLM/model2vec (empty prefix).
        pfx = mi.get("query_prefix") or ""
        if pfx:
            texts = [pfx + t for t in texts]
    if mi.get("type") == "static":
        # model2vec StaticModel: numpy out, no encode kwargs; normalize for cosine.
        v = np.asarray(m.encode(list(texts)), dtype=np.float32)
        nrm = np.linalg.norm(v, axis=1, keepdims=True)
        nrm[nrm == 0] = 1.0
        return v / nrm
    v = m.encode(texts, batch_size=64, normalize_embeddings=True,
                 show_progress_bar=False, convert_to_numpy=True)
    return v.astype(np.float32)


# ── corpus ───────────────────────────────────────────────────────────────────
def chunk_text(text: str, max_chars: int = 900, overlap_chars: int = 150) -> list[str]:
    """Splits text into chunks of max_chars with overlap_chars overlap, split on space/paragraph."""
    if not text:
        return []
    text = text.strip()
    if len(text) <= max_chars:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        end = start + max_chars
        if end >= len(text):
            chunks.append(text[start:].strip())
            break
        # Try to find a nice place to split (newline or space) within the last 150 characters of the chunk
        split_pos = text.rfind("\n", start + max_chars - 150, end)
        if split_pos == -1 or split_pos <= start:
            split_pos = text.rfind(" ", start + max_chars - 150, end)
        if split_pos != -1 and split_pos > start:
            end = split_pos
        chunks.append(text[start:end].strip())
        start = end - overlap_chars
        if start >= end:  # Prevent infinite loops if overlap is misconfigured
            start = end
    return [c for c in chunks if c]


def _item_full_text(it: dict, source: str) -> str:
    """Extracts rich text fields from a snapshot item for full text RAG."""
    parts = []
    title = it.get("title") or it.get("name") or ""
    if title:
        parts.append(title)
    for k in ("abstract", "content", "summary", "snippet", "subtitle", "description", "body"):
        val = it.get(k)
        if val and isinstance(val, str) and val.strip():
            parts.append(val.strip())
    for k in ("folder", "authors", "creator", "journal"):
        val = it.get(k)
        if val:
            parts.append(f"{k}: {val}")
    return "\n".join(parts)


# ── corpus ───────────────────────────────────────────────────────────────────
def _archive_items() -> list[dict]:
    """Every snapshot item across all sources, split into chunks if long."""
    from lib import cross_search
    out = []
    for source in cross_search._all_sources():
        snap = cross_search._latest_snapshot_for(source)
        if not snap:
            continue
        for it in (snap.get("items") or []):
            title = cross_search.pretty_title(it)
            if not title:
                continue
            url = cross_search.pretty_url(it)
            sub = cross_search.pretty_subline(it, source)
            parent_uid = "arc:" + hashlib.md5(
                ((url or "") + "|" + title).encode("utf-8", "ignore")).hexdigest()
            
            # Extract full text content
            full_text = _item_full_text(it, source)
            chunks = chunk_text(full_text, max_chars=900, overlap_chars=150)
            
            if len(chunks) <= 1:
                # Keep it simple if it's small (or empty)
                snippet_text = sub if sub else (full_text[:200] if full_text else "")
                out.append({
                    "uid": parent_uid,
                    "source": source,
                    "title": title[:200],
                    "url": url,
                    "snippet": snippet_text[:200],
                    "text": (title + " " + (snippet_text or ""))[:_MAX_TEXT]
                })
            else:
                for idx, chunk in enumerate(chunks):
                    out.append({
                        "uid": f"{parent_uid}:c{idx}",
                        "source": source,
                        "title": f"{title[:170]} (Part {idx + 1})",
                        "url": url,
                        "snippet": chunk,
                        "text": chunk
                    })
    return out


def _memory_items() -> list[dict]:
    if not DB_PATH.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=4)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT id, kind, content, tags FROM memory").fetchall()
        con.close()
    except Exception:
        return []
    out = []
    for r in rows:
        content = (r["content"] or "").strip()
        if not content:
            continue
        parent_uid = f"mem:{r['id']}"
        title = f"memory {r['id']} [{r['kind']}]"
        tags = (r["tags"] or "").strip()
        full_text = content + (" tags: " + tags if tags else "")
        chunks = chunk_text(full_text, max_chars=900, overlap_chars=150)
        
        if len(chunks) <= 1:
            out.append({
                "uid": parent_uid,
                "source": "mind-memory",
                "title": title,
                "url": None,
                "snippet": content[:200],
                "text": (content + " " + tags)[:_MAX_TEXT]
            })
        else:
            for idx, chunk in enumerate(chunks):
                out.append({
                    "uid": f"{parent_uid}:c{idx}",
                    "source": "mind-memory",
                    "title": f"{title} (Part {idx + 1})",
                    "url": None,
                    "snippet": chunk,
                    "text": chunk
                })
    return out


def _file_items() -> list[dict]:
    """Local + Drive files from state/files_index.jsonl (lib/file_indexer).
    Metadata-only tier by default, upgraded to full-text chunking if hydrated extract is on disk."""
    path = ROOT / "state" / "files_index.jsonl"
    if not path.exists():
        return []
    out = []
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    it = json.loads(line)
                except Exception:
                    continue
                name = it.get("name") or ""
                if not name:
                    continue
                file_path = it.get("path", "")
                pure = pathlib.PurePath(file_path)
                parents = " ".join(pure.parts[-4:-1])
                stem = pure.stem.replace("_", " ").replace("-", " ")
                ext = (it.get("ext") or pure.suffix).lower()
                digest = hashlib.md5(
                    file_path.encode("utf-8", "ignore")).hexdigest()
                url = "file:///" + file_path.replace("\\", "/")
                parent_uid = "file:" + digest

                meta_text = " ".join(part for part in (name, stem, parents, ext) if part)
                
                # Check for extracted text (same configurable dir the hydration
                # worker writes to — may live on Drive, not local state/).
                xp = _EXTRACT_DIR / f"{digest}.txt"
                file_text = ""
                if xp.exists():
                    try:
                        # Read up to 40,000 characters to keep vector size reasonable but capture full content
                        file_text = xp.read_text(encoding="utf-8", errors="replace")[:40000]
                    except Exception:
                        pass
                
                if file_text:
                    chunks = chunk_text(file_text, max_chars=900, overlap_chars=150)
                    for idx, chunk in enumerate(chunks):
                        out.append({
                            "uid": f"{parent_uid}:c{idx}",
                            "source": "files",
                            "title": f"{name[:170]} (Part {idx + 1})",
                            "url": url,
                            "snippet": chunk[:900],
                            "text": (meta_text + "\n" + chunk).strip()
                        })
                else:
                    out.append({
                        "uid": parent_uid,
                        "source": "files",
                        "title": name[:200],
                        "url": url,
                        "snippet": (it.get("path") or "")[-200:],
                        "text": meta_text[:_MAX_TEXT]
                    })
    except Exception:
        return []
    return out


def _hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8", "ignore")).hexdigest()[:12]


# ── build ────────────────────────────────────────────────────────────────────
def build(force: bool = False) -> dict:
    """(Re)build the index incrementally. Returns a small status dict."""
    global _building
    with _build_lock:
        if _building:
            return {"status": "busy"}
        _building = True
    try:
        INDEX_DIR.mkdir(parents=True, exist_ok=True)
        corpus = _archive_items() + _memory_items() + _file_items()
        if not corpus:
            return {"status": "empty"}
        # Load existing index for incremental reuse.
        prev_vecs, prev_by_uid = None, {}
        if not force and VEC_PATH.exists() and META_PATH.exists():
            try:
                prev_vecs = np.load(VEC_PATH, mmap_mode="r")
                prev_meta = json.loads(META_PATH.read_text(encoding="utf-8"))
                if len(prev_meta) != prev_vecs.shape[0]:
                    raise ValueError("vectors/meta row mismatch")
                for i, m in enumerate(prev_meta):
                    prev_by_uid[m["uid"]] = (i, m.get("h"))
            except Exception:
                prev_vecs, prev_by_uid = None, {}

        vectors, meta, to_embed, embed_idx = [], [], [], []
        for item in corpus:
            h = _hash(item["text"])
            prev = prev_by_uid.get(item["uid"])
            if prev is not None and prev[1] == h and prev_vecs is not None:
                vectors.append(prev_vecs[prev[0]])
                meta.append({**_slim(item), "h": h})
            else:
                embed_idx.append(len(meta))
                meta.append({**_slim(item), "h": h})
                vectors.append(None)
                to_embed.append(item["text"])

        if to_embed:
            embedded = _embed(to_embed)
            if embedded is None:
                return {"status": "no-model"}
            for j, pos in enumerate(embed_idx):
                vectors[pos] = embedded[j]

        mat = np.vstack([v for v in vectors if v is not None]).astype(np.float32)
        np.save(VEC_PATH, mat)
        META_PATH.write_text(json.dumps(meta), encoding="utf-8")
        _write_turbo(mat)
        try:
            _ensure_meta_db()   # build the lazy meta DB here (in the subprocess)
        except Exception:
            pass
        _invalidate()
        return {"status": "ok", "items": len(meta),
                "new": len(to_embed), "reused": len(meta) - len(to_embed)}
    finally:
        _building = False


def _slim(item: dict) -> dict:
    return {k: item[k] for k in ("uid", "source", "title", "url", "snippet")}


# ── turbovec engine (2026-06-12, Bruno's pick) ───────────────────────────────
# TurboQuant-quantized index: ~8x smaller than the float32 matrix and
# searched in ~15ms without loading 500MB into RAM. ids are ROW POSITIONS in
# meta.json (both files are written atomically together by build()). The
# numpy matrix stays on disk as the fallback engine and the incremental-
# reuse store; if turbovec is unavailable the old brute-force path runs.
TURBO_PATH = INDEX_DIR / "turbo.idx"


def _write_turbo(mat: "np.ndarray") -> None:
    try:
        import turbovec
        idx = turbovec.IdMapIndex(mat.shape[1])
        ids = np.ascontiguousarray(np.arange(len(mat)), dtype=np.uint64)
        idx.add_with_ids(np.ascontiguousarray(mat, dtype=np.float32), ids)
        idx.prepare()
        idx.write(str(TURBO_PATH))
    except Exception:
        # fallback engine (numpy matrix) still works; never fail the build
        pass


def ensure_built_async() -> None:
    """Kick a background build/refresh if not already running."""
    threading.Thread(target=lambda: build(force=False),
                     daemon=True, name="connect-index-build").start()


# ── query ────────────────────────────────────────────────────────────────────
def _invalidate():
    global _vecs, _meta, _meta_mtime, _turbo
    _vecs, _meta, _meta_mtime, _turbo = None, None, 0.0, None


_turbo = None


META_DB = INDEX_DIR / "meta.db"


class _LazyMeta:
    """Index-addressable metadata backed by SQLite. Only the rows in a result
    set are materialized — instead of holding all ~984k records as Python dicts
    in RAM (~2GB, the bulk of mind_service's footprint). Drop-in for the old
    list: supports len(), truthiness, and meta[i] -> dict. Bruno 2026-06-30."""
    _FIELDS = ("uid", "source", "title", "url", "snippet", "h")

    def __init__(self, db_path, n: int):
        import sqlite3
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._n = n

    def __len__(self):
        return self._n

    def __bool__(self):
        return self._n > 0

    def __getitem__(self, i: int) -> dict:
        row = self._db.execute(
            "SELECT uid, source, title, url, snippet, h FROM meta WHERE rowid=?",
            (int(i) + 1,)).fetchone()
        return dict(zip(self._FIELDS, row)) if row else {}


def _ensure_meta_db() -> int:
    """Build meta.db from meta.json when missing/stale; return the row count.
    meta.json is loaded ONCE here (transiently) only while (re)building — never
    on a read path. Called from build() (subprocess) so mind_service never has
    to load the full array."""
    import sqlite3
    mj_mtime = META_PATH.stat().st_mtime
    if META_DB.exists() and META_DB.stat().st_mtime >= mj_mtime:
        try:
            con = sqlite3.connect(str(META_DB))
            n = con.execute("SELECT count(*) FROM meta").fetchone()[0]
            con.close()
            return n
        except Exception:
            pass
    records = json.loads(META_PATH.read_text(encoding="utf-8"))
    tmp = META_DB.with_name("meta.db.tmp")
    if tmp.exists():
        tmp.unlink()
    con = sqlite3.connect(str(tmp))
    con.execute("CREATE TABLE meta (uid TEXT, source TEXT, title TEXT, "
                "url TEXT, snippet TEXT, h TEXT)")
    con.executemany(
        "INSERT INTO meta (rowid, uid, source, title, url, snippet, h) "
        "VALUES (?,?,?,?,?,?,?)",
        [(i + 1, r.get("uid"), r.get("source"), r.get("title"), r.get("url"),
          r.get("snippet"), r.get("h")) for i, r in enumerate(records)])
    con.commit()
    con.close()
    n = len(records)
    del records
    tmp.replace(META_DB)
    return n


def _load_meta():
    """Index-addressable metadata, SQLite-backed and lazy so the full ~984k
    records never sit in RAM. mtime-cached against meta.json."""
    global _meta, _meta_mtime, _vecs, _turbo
    if not META_PATH.exists():
        return None
    mtime = META_PATH.stat().st_mtime
    if _meta is not None and mtime == _meta_mtime:
        return _meta
    try:
        n = _ensure_meta_db()
        _meta = _LazyMeta(META_DB, n)
        _meta_mtime = mtime
        _vecs = None      # engines reload lazily against the new meta
        _turbo = None
        return _meta
    except Exception:
        return None


def _load_turbo(n_meta: int):
    """turbovec engine if present and consistent with meta; else None."""
    global _turbo
    if _turbo is not None:
        return _turbo
    if not TURBO_PATH.exists():
        return None
    try:
        import turbovec
        t = turbovec.IdMapIndex.load(str(TURBO_PATH))
        t.prepare()
        # consistency: the newest row id must exist (rows = meta positions)
        if n_meta and not t.contains(n_meta - 1):
            return None
        _turbo = t
        return t
    except Exception:
        return None


def _load_index():
    """Fallback engine: the raw float32 matrix. MEMORY-MAPPED, not resident —
    Bruno 2026-07-06: a plain np.load() here pinned the full 1.1GB in the _vecs
    module global for the life of mind_service the instant turbovec ever hiccuped,
    which is what drove the 1.4GB RAM and the freezes. mmap_mode='r' keeps the
    matrix on disk (OS pages in only the rows a brute-force pass touches, and
    reclaims them under pressure); the dot-product math is identical."""
    global _vecs
    meta = _load_meta()
    if meta is None or not VEC_PATH.exists():
        return None, None
    if _vecs is not None:
        return _vecs, meta
    try:
        _vecs = np.load(VEC_PATH, mmap_mode="r")
        if _vecs.shape[0] != len(meta):
            _vecs = None
            return None, None
        return _vecs, meta
    except Exception:
        return None, None


def _vector_shape() -> tuple[int, ...] | None:
    try:
        return tuple(np.lib.format.open_memmap(VEC_PATH, mode="r").shape)
    except Exception:
        return None


def is_ready() -> bool:
    meta = _load_meta()
    if not meta:
        return False
    shape = _vector_shape()
    if not shape or len(shape) != 2 or shape[0] != len(meta):
        return False
    if TURBO_PATH.exists() and _load_turbo(len(meta)) is None:
        return False
    return True


def search(query: str, top_k: int = 40, min_score: float = 0.18) -> list[dict]:
    """Rank the index against the query embedding. turbovec engine first
    (~15ms, no big matrix in RAM); numpy brute-force as fallback."""
    if not query.strip():
        return []
    meta = _load_meta()
    if not meta:
        return []
    qv = _embed([query[:_MAX_TEXT]], is_query=True)
    if qv is None:
        return []

    t = _load_turbo(len(meta))
    if t is not None:
        try:
            q = np.ascontiguousarray(qv[:1], dtype=np.float32)
            scores, ids = t.search(q, min(top_k, len(meta)))
            out = []
            for s, i in zip(scores[0], ids[0]):
                s = float(s)
                if s < min_score or int(i) >= len(meta):
                    continue
                out.append({**meta[int(i)], "score": round(s, 3)})
            if out:
                return out
        except Exception:
            pass    # fall through to numpy

    v, meta = _load_index()
    if v is None:
        return []
    sims = v @ qv[0]                      # vectors are L2-normalized → cosine
    n = min(top_k, len(meta))
    idx = np.argpartition(-sims, n - 1)[:n]
    idx = idx[np.argsort(-sims[idx])]
    out = []
    for i in idx:
        s = float(sims[i])
        if s < min_score:
            break
        m = meta[i]
        out.append({**m, "score": round(s, 3)})
    return out
