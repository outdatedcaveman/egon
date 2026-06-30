"""Concept Graph — higher-order concepts derived from the embedded vault.

This is the substance behind the Categorical Mind (CatColab): instead of
documents/artifacts, it surfaces the *concepts* that the 776k-item embedding
space actually clusters into, and the relations (morphisms) between them.

Pipeline (all memory-safe on Bruno's 8GB machine):
  1. memmap state vectors.npy  (N x DIM float32, never fully loaded)
  2. MiniBatchKMeans streamed over batches  -> K concept centroids
  3. second streamed pass assigns rows + keeps the nearest members per concept
  4. concept labels mined from member titles (distinctive terms)
  5. edges = cosine similarity between centroids (the inter-concept morphisms)
  6. 2D layout via PCA of centroids (for the graphic home)

Output: state/concept_graph.json  — consumed by the Mind page's graphic view.

Run idle-gated (egon_core) since the meta-title load briefly needs ~1GB.
Bruno 2026-06-25.
"""
from __future__ import annotations

import json
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np

from lib.egon_paths import CONNECT_INDEX_DIR, STATE_DIR

VEC_FILE = CONNECT_INDEX_DIR / "vectors.npy"
META_FILE = CONNECT_INDEX_DIR / "meta.json"
OUT_FILE = STATE_DIR / "concept_graph.json"

_TOKEN_RE = re.compile(r"[a-z][a-z0-9'+-]{2,}")
# noise terms that say nothing about a *concept*
_STOP = set("""the a an and or of to in for on at by with from into over under as is are
was were be been being this that these those it its their his her our your my we you they
i he she them him us not no nor but so if then than too very can will just about www http
https com org net html php pdf html www2 amp utm ref via new old how what why when where who
home page index search results list view edit file files document documents note notes item
items untitled draft drafts copy chrome bookmark bookmarks tab tabs folder
sciencedirect springerlink springer jstor pnas arxiv biorxiv ssrn academia researchgate
elsevier wiley sage routledge taylor francis nature science scholar googleusercontent doi
vol issue journal journals review reviews annual proceedings online article articles paper
papers pdf epub amazon goodreads youtube wikipedia google drive docs gmail github gitlab
openrefine endnote remnote zotero notion obsidian mendeley paperpile instapaper pocket
abstract full text pmc ncbi nih org www2 url link www3 redirect login signin
moment just wait page online site home menu sign content download downloads view
loading please enable javascript browser cookies access denied forbidden error
untitled document welcome dashboard print share more info details click here""".split())


def _norm(mat: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(mat, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return mat / n


def _load_titles(n: int) -> list[dict]:
    """Compact row-aligned [{title, source, uid}] — drops url/snippet to save
    RAM. meta.json is one big JSON array aligned with vectors.npy rows."""
    raw = json.loads(META_FILE.read_text(encoding="utf-8"))
    out = [{"title": (r.get("title") or "").strip()[:140],
            "source": r.get("source") or "", "uid": r.get("uid") or ""}
           for r in raw[:n]]
    del raw
    return out


_VOWELS = frozenset("aeiou")


def _wordish(tok: str) -> bool:
    """Reject hash/ID/URL-fragment tokens (ddb6a1, s0092, pii, abs) — keep only
    real words: alphabetic, length >= 4, containing a vowel."""
    return len(tok) >= 4 and tok.isalpha() and any(c in _VOWELS for c in tok)


def _label_from_titles(titles: list[str], global_df: Counter, n_docs: int) -> tuple[str, list[str]]:
    """Distinctive 2-4 word label for a concept, mined from member titles via a
    light tf-idf against the corpus-wide document frequency."""
    local = Counter()
    for t in titles:
        for tok in set(_TOKEN_RE.findall(t.lower())):
            if tok not in _STOP and _wordish(tok):
                local[tok] += 1
    if not local:
        return "(unlabeled)", []
    scored = []
    for tok, lf in local.items():
        df = global_df.get(tok, 1)
        idf = np.log((n_docs + 1) / (df + 1)) + 1.0
        scored.append((lf * idf, lf, tok))
    scored.sort(reverse=True)
    terms = [t for _, _, t in scored[:6]]
    label = " · ".join(terms[:3]) if terms else "(unlabeled)"
    return label, terms


def build_concept_graph(k: int = 160, sample: int | None = None,
                        members_per: int = 10, max_edges_per: int = 4,
                        edge_floor: float = 0.45) -> dict[str, Any]:
    """Cluster the embedded vault into `k` concepts + relations. `sample` caps
    rows for a fast preview; None = full corpus."""
    from sklearn.cluster import MiniBatchKMeans
    from sklearn.decomposition import PCA

    t0 = time.time()
    if not VEC_FILE.exists():
        return {"status": "error", "error": f"no vectors at {VEC_FILE}"}
    vecs = np.load(VEC_FILE, mmap_mode="r")
    n_total = vecs.shape[0]
    n = min(sample, n_total) if sample else n_total
    dim = vecs.shape[1]
    k = max(8, min(k, n // 50 or 8))

    # Pass 1 — stream MiniBatchKMeans over normalized batches.
    km = MiniBatchKMeans(n_clusters=k, batch_size=4096, n_init=3, max_iter=100)
    BATCH = 8192
    for i in range(0, n, BATCH):
        batch = _norm(np.asarray(vecs[i:min(i + BATCH, n)], dtype=np.float32))
        km.partial_fit(batch)
    centroids = _norm(km.cluster_centers_.astype(np.float32))

    # Pass 2 — assign rows; keep nearest members + sizes per concept.
    sizes = np.zeros(k, dtype=np.int64)
    best: list[list[tuple[float, int]]] = [[] for _ in range(k)]
    for i in range(0, n, BATCH):
        batch = _norm(np.asarray(vecs[i:min(i + BATCH, n)], dtype=np.float32))
        sims = batch @ centroids.T          # cosine (both normalized)
        lab = sims.argmax(axis=1)
        topsim = sims[np.arange(len(lab)), lab]
        for j, (c, s) in enumerate(zip(lab.tolist(), topsim.tolist())):
            sizes[c] += 1
            bucket = best[c]
            if len(bucket) < members_per:
                bucket.append((s, i + j))
            elif s > bucket[-1][0]:
                bucket.append((s, i + j))
                bucket.sort(reverse=True)
                bucket.pop()

    # Titles only for the member rows we kept (cheap targeted read).
    titles = _load_titles(n)
    n_docs = len(titles)
    global_df: Counter = Counter()
    for r in titles:
        for tok in set(_TOKEN_RE.findall(r["title"].lower())):
            if tok not in _STOP:
                global_df[tok] += 1

    # 2D layout for the graphic home.
    coords = PCA(n_components=2).fit_transform(centroids)
    cmin, cmax = coords.min(0), coords.max(0)
    span = np.where((cmax - cmin) == 0, 1, cmax - cmin)
    coords = (coords - cmin) / span        # 0..1

    concepts = []
    for c in range(k):
        members = sorted(best[c], reverse=True)
        mtitles = [titles[idx]["title"] for _, idx in members if titles[idx]["title"]]
        label, terms = _label_from_titles(mtitles, global_df, n_docs)
        srcs = Counter(titles[idx]["source"] for _, idx in members)
        concepts.append({
            "id": c, "label": label, "terms": terms, "size": int(sizes[c]),
            "x": round(float(coords[c][0]), 4), "y": round(float(coords[c][1]), 4),
            "sources": [s for s, _ in srcs.most_common(3)],
            "top_items": [{"title": titles[idx]["title"], "source": titles[idx]["source"],
                           "uid": titles[idx]["uid"]} for _, idx in members[:6]],
        })

    # Edges — the morphisms: strongest inter-concept similarities.
    sim = centroids @ centroids.T
    np.fill_diagonal(sim, -1.0)
    edges = []
    seen = set()
    for a in range(k):
        nbrs = np.argsort(sim[a])[::-1][:max_edges_per]
        for b in nbrs:
            w = float(sim[a][b])
            if w < edge_floor:
                continue
            key = (min(a, int(b)), max(a, int(b)))
            if key in seen:
                continue
            seen.add(key)
            edges.append({"a": key[0], "b": key[1], "weight": round(w, 3)})

    result = {
        "status": "ok",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "n_items": int(n), "n_concepts": k, "dim": int(dim),
        "seconds": round(time.time() - t0, 1),
        "concepts": concepts, "edges": edges,
    }
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return result


def load_concept_graph() -> dict[str, Any] | None:
    if OUT_FILE.exists():
        try:
            return json.loads(OUT_FILE.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=160)
    ap.add_argument("--sample", type=int, default=None)
    args = ap.parse_args()
    r = build_concept_graph(k=args.k, sample=args.sample)
    if r.get("status") == "ok":
        print(f"{r['n_concepts']} concepts over {r['n_items']:,} items in {r['seconds']}s")
        for c in sorted(r["concepts"], key=lambda c: -c["size"])[:18]:
            print(f"  [{c['size']:>6,}] {c['label']}  <{','.join(c['sources'][:2])}>")
        print(f"edges: {len(r['edges'])}")
    else:
        print(r)
