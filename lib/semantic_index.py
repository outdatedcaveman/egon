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
INDEX_DIR = ROOT / "state" / "connect_index"
VEC_PATH = INDEX_DIR / "vectors.npy"
META_PATH = INDEX_DIR / "meta.json"

MODEL_NAME = "all-MiniLM-L6-v2"
_MAX_TEXT = 480           # chars fed to the encoder per item (title+snippet)

_model = None
_model_lock = threading.Lock()
_build_lock = threading.Lock()
_building = False

# in-memory cache of the loaded index
_vecs: np.ndarray | None = None
_meta: list[dict] | None = None
_meta_mtime: float = 0.0


# ── model ────────────────────────────────────────────────────────────────────
def _load_model():
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is not None:
            return _model
        try:
            from sentence_transformers import SentenceTransformer
            _model = SentenceTransformer(MODEL_NAME)
        except Exception:
            _model = None
        return _model


def _embed(texts: list[str]) -> np.ndarray | None:
    m = _load_model()
    if m is None:
        return None
    v = m.encode(texts, batch_size=64, normalize_embeddings=True,
                 show_progress_bar=False, convert_to_numpy=True)
    return v.astype(np.float32)


# ── corpus ───────────────────────────────────────────────────────────────────
def _archive_items() -> list[dict]:
    """Every snapshot item across all sources, as connect-shaped dicts."""
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
            uid = "arc:" + hashlib.md5(
                ((url or "") + "|" + title).encode("utf-8", "ignore")).hexdigest()
            out.append({"uid": uid, "source": source, "title": title[:200],
                        "url": url, "snippet": sub[:200],
                        "text": (title + " " + sub)[:_MAX_TEXT]})
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
        out.append({
            "uid": f"mem:{r['id']}",
            "source": "mind-memory",
            "title": f"memory {r['id']} [{r['kind']}]",
            "url": None,
            "snippet": content[:200],
            "text": (content + " " + (r["tags"] or ""))[:_MAX_TEXT],
        })
    return out


def _file_items() -> list[dict]:
    """Local + Drive files from state/files_index.jsonl (lib/file_indexer).
    Metadata-only tier: we embed filename + parent folders — rich enough for
    academic PDFs — and never touch file contents (Drive placeholders would
    force-download). Bruno 2026-06-12, the big play tier 1."""
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
                parents = " ".join(
                    pathlib.PurePath(it.get("path", "")).parts[-3:-1])
                stem = pathlib.PurePath(name).stem.replace("_", " ")
                out.append({
                    "uid": "file:" + hashlib.md5(
                        it.get("path", "").encode("utf-8", "ignore")).hexdigest(),
                    "source": "files",
                    "title": name[:200],
                    "url": "file:///" + it.get("path", "").replace("\\", "/"),
                    "snippet": (it.get("path") or "")[-200:],
                    "text": (stem + " " + parents)[:_MAX_TEXT],
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
                prev_vecs = np.load(VEC_PATH)
                prev_meta = json.loads(META_PATH.read_text(encoding="utf-8"))
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
        _invalidate()
        return {"status": "ok", "items": len(meta),
                "new": len(to_embed), "reused": len(meta) - len(to_embed)}
    finally:
        _building = False


def _slim(item: dict) -> dict:
    return {k: item[k] for k in ("uid", "source", "title", "url", "snippet")}


def ensure_built_async() -> None:
    """Kick a background build/refresh if not already running."""
    threading.Thread(target=lambda: build(force=False),
                     daemon=True, name="connect-index-build").start()


# ── query ────────────────────────────────────────────────────────────────────
def _invalidate():
    global _vecs, _meta, _meta_mtime
    _vecs, _meta, _meta_mtime = None, None, 0.0


def _load_index():
    global _vecs, _meta, _meta_mtime
    if not VEC_PATH.exists() or not META_PATH.exists():
        return None, None
    mtime = META_PATH.stat().st_mtime
    if _vecs is not None and mtime == _meta_mtime:
        return _vecs, _meta
    try:
        _vecs = np.load(VEC_PATH)
        _meta = json.loads(META_PATH.read_text(encoding="utf-8"))
        _meta_mtime = mtime
        return _vecs, _meta
    except Exception:
        return None, None


def is_ready() -> bool:
    v, m = _load_index()
    return v is not None and m is not None and len(m) > 0


def search(query: str, top_k: int = 40, min_score: float = 0.18) -> list[dict]:
    """Cosine-rank the cached index against the query embedding."""
    v, meta = _load_index()
    if v is None or not query.strip():
        return []
    qv = _embed([query[:_MAX_TEXT]])
    if qv is None:
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
