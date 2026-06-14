"""One-time training: compute per-category exemplar centroids from Panop's
panop_history.json so the embedding classifier can rank pages against
Bruno's actual past picks (not generic prompts).

Filters used to keep only TRUSTED exemplars:
  - z_synced AND b_synced AND ai_learned=False AND extracted_from=None
  - = entries that classified via explicit domain rule, NOT from any AI
    fallback or Science News redirect
  - in Zotero NOT trashed (we fetch the live list to exclude any that we
    moved to Trash today)

For each exemplar:
  - Build text = title + abstract (or local JSON's metadata)
  - Embed via MiniLM
  - Group by category, compute mean embedding (centroid), L2-normalize

Output:
  state/classifier/exemplar_centroids.npy   (np.float32 [n_categories, 384])
  state/classifier/exemplar_meta.json       (category_ids + per-category count)

Run via: python scripts/build_exemplar_centroids.py
"""
from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HISTORY = ROOT / "state" / "panop" / "panop_history.json"
OUT_NPY = ROOT / "state" / "classifier" / "exemplar_centroids.npy"
OUT_META = ROOT / "state" / "classifier" / "exemplar_meta.json"

sys.path.insert(0, str(ROOT))
from lib import secrets   # noqa: E402

MIN_EXEMPLARS_PER_CAT = 10  # categories with fewer get skipped (centroid unreliable)


def _fetch_zotero_trashed_urls() -> set:
    """Pull URLs of items currently in Zotero Trash so we don't include them
    as positive exemplars."""
    try:
        import httpx
    except Exception:
        return set()
    uid = secrets.get("zotero.user_id"); key = secrets.get("zotero.api_key")
    if not uid or not key:
        return set()
    H = {"Zotero-API-Key": key, "Zotero-API-Version": "3"}
    trashed = set()
    start = 0
    while True:
        try:
            r = httpx.get(f"https://api.zotero.org/users/{uid}/items/trash",
                          headers=H, params={"limit": 100, "start": start},
                          timeout=20)
        except Exception:
            break
        if r.status_code != 200: break
        batch = r.json()
        if not batch: break
        for it in batch:
            u = it.get("data", {}).get("url")
            if u: trashed.add(u)
        start += 100
        time.sleep(0.2)
        if len(batch) < 100: break
    return trashed


def main():
    if not HISTORY.exists():
        print(f"Panop history not found: {HISTORY}"); return 1

    print("Loading history…")
    h = json.loads(HISTORY.read_text(encoding="utf-8"))

    print("Fetching Zotero Trash to exclude…")
    trashed = _fetch_zotero_trashed_urls()
    print(f"  Trashed URLs to exclude: {len(trashed)}")

    # Filter to trusted exemplars
    exemplars = defaultdict(list)
    for url, it in h.items():
        if not it.get("z_synced") or not it.get("b_synced"):
            continue
        if it.get("ai_learned"): continue
        if it.get("extracted_from"): continue
        if it.get("cat_id") in (None, "", "uncategorized"): continue
        if url in trashed: continue
        title = (it.get("title") or "").strip()
        abstract = (it.get("abstract") or "").strip()
        if not title and not abstract: continue
        text = f"{title}\n\n{abstract}".strip()
        if len(text) < 30: continue
        exemplars[it["cat_id"]].append(text)

    # Bruno 2026-06-14: also learn from the AI-arbiter labels (the master AI is
    # the ultimate arbiter; the native ML must converge toward it). The phone
    # sweep produced verdicts for ALL categories INCLUDING reject + longform,
    # which the old history-only build lacked. Future Bruno manual overrides
    # land in the same file and get picked up on the next rebuild.
    VERDICTS = ROOT / "state" / "panop" / "phone_master_verdicts.json"
    if VERDICTS.exists():
        try:
            mv = json.loads(VERDICTS.read_text(encoding="utf-8"))
            added = 0
            for _id, m in mv.items():
                cat = m.get("category"); title = (m.get("title") or "").strip()
                if not cat or len(title) < 8:
                    continue
                # title carries the signal; append host as a weak extra cue
                from urllib.parse import urlparse
                host = urlparse(m.get("url") or "").netloc.replace("www.", "")
                exemplars[cat].append(f"{title} ({host})" if host else title)
                added += 1
            print(f"Merged {added} AI-arbiter exemplars from phone verdicts.")
        except Exception as e:
            print(f"verdict merge skipped: {e}")

    print(f"\nExemplar counts per category:")
    for c, lst in exemplars.items():
        print(f"  {c:25}  {len(lst)}")

    # Drop categories with too few exemplars
    final = {c: lst for c, lst in exemplars.items() if len(lst) >= MIN_EXEMPLARS_PER_CAT}
    if not final:
        print(f"\nNo category has >= {MIN_EXEMPLARS_PER_CAT} trusted exemplars. Bailing."); return 1
    dropped = set(exemplars.keys()) - set(final.keys())
    if dropped: print(f"\nDropping categories with too few exemplars: {dropped}")

    # Load model
    print("\nLoading MiniLM (this can take 30-60s on first run)…")
    try:
        from sentence_transformers import SentenceTransformer
        import numpy as np
    except ImportError as e:
        print(f"sentence-transformers / numpy not installed: {e}"); return 1
    model = SentenceTransformer("all-MiniLM-L6-v2")

    # Embed
    cat_ids = sorted(final.keys())
    centroids = []
    per_cat_count = {}
    for cid in cat_ids:
        texts = final[cid]
        print(f"  embedding {len(texts)} exemplars for '{cid}'…")
        embs = model.encode(texts, normalize_embeddings=True,
                            show_progress_bar=False, batch_size=32)
        centroid = embs.mean(axis=0)
        # L2-normalize the centroid so we can use dot product as cosine
        norm = np.linalg.norm(centroid)
        if norm > 0: centroid = centroid / norm
        centroids.append(centroid)
        per_cat_count[cid] = len(texts)

    centroids = np.stack(centroids).astype("float32")
    OUT_NPY.parent.mkdir(parents=True, exist_ok=True)
    np.save(OUT_NPY, centroids)

    meta = {
        "built_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "category_ids": cat_ids,
        "exemplars_per_category": per_cat_count,
        "total_exemplars": sum(per_cat_count.values()),
        "embedding_dim": int(centroids.shape[1]),
        "model": "all-MiniLM-L6-v2",
        "min_exemplars_per_cat": MIN_EXEMPLARS_PER_CAT,
    }
    OUT_META.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n✓ wrote {OUT_NPY} ({centroids.shape[0]} categories × {centroids.shape[1]} dims)")
    print(f"✓ wrote {OUT_META}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
