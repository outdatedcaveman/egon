"""Native ML classifier for the FULL KMS taxonomy, trained on Bruno's own
hand-curated bookmark folders (state/classifier/kms_training.jsonl). Token-free:
local MiniLM embeddings + weighted k-NN over the labelled exemplars. This is the
default brain in every surface (Inbox/Panop, Navigation/Routster); the powerful
AI is only the fallback/teacher. It learns continuously: AI labels and Bruno's
manual category overrides are appended to the training file and re-indexed.

k-NN (not centroids) on the TITLE is what separates an Amazon book ("The Selfish
Gene") from Amazon cutlery ("18-piece Steel Cutlery Set") even though both share
the amazon.com domain.
"""
from __future__ import annotations
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRAIN = ROOT / "state" / "classifier" / "kms_training.jsonl"
INDEX = ROOT / "state" / "classifier" / "kms_knn.npz"
LABELS = ROOT / "state" / "classifier" / "kms_knn_labels.json"

_model = None
_emb = None          # (N, dim) float32, L2-normalized
_labels = None       # list[str] length N

K = 15
CONFIDENT = 0.50     # weighted-vote share >= this AND top sim ok -> confident


def _load_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer("all-MiniLM-L6-v2")
    return _model


def _text(title, url=""):
    from urllib.parse import urlparse
    host = (urlparse(url or "").netloc or "").replace("www.", "")
    return f"{title} ({host})" if host else (title or "")


def build_index():
    import numpy as np
    rows = [json.loads(l) for l in TRAIN.read_text(encoding="utf-8").splitlines() if l.strip()]
    texts = [_text(r["title"], r.get("url", "")) for r in rows]
    labels = [r["category"] for r in rows]
    model = _load_model()
    emb = model.encode(texts, normalize_embeddings=True, batch_size=64, show_progress_bar=False)
    np.savez_compressed(INDEX, emb=emb.astype("float32"))
    LABELS.write_text(json.dumps(labels), encoding="utf-8")
    return len(labels)


def _load():
    global _emb, _labels
    if _emb is None:
        import numpy as np
        _emb = np.load(INDEX)["emb"]
        _labels = json.loads(LABELS.read_text(encoding="utf-8"))
    return _emb, _labels


def classify(title, url="", k=K):
    """Return {category, confidence, votes} via weighted k-NN on title+host."""
    import numpy as np
    emb, labels = _load()
    q = _load_model().encode(_text(title, url), normalize_embeddings=True).astype("float32")
    sims = emb @ q
    idx = np.argpartition(-sims, k)[:k]
    idx = idx[np.argsort(-sims[idx])]
    votes = {}
    for i in idx:
        s = float(sims[i])
        if s <= 0:
            continue
        votes[labels[i]] = votes.get(labels[i], 0.0) + s
    if not votes:
        return {"category": None, "confidence": 0.0, "votes": {}}
    total = sum(votes.values())
    cat = max(votes, key=votes.get)
    share = votes[cat] / total
    top_sim = float(sims[idx[0]])
    conf = round(share * min(1.0, top_sim / 0.6), 3)
    return {"category": cat, "confidence": conf, "share": round(share, 3),
            "top_sim": round(top_sim, 3), "votes": {k_: round(v, 2) for k_, v in votes.items()}}


def learn(title, url, category):
    """Append a new labelled example (AI verdict or Bruno's manual override) so
    the index improves on the next rebuild. Manual overrides should be added
    twice (higher weight) by the caller if desired."""
    with TRAIN.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"category": category, "title": title, "url": url,
                            "source": "feedback"}, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    print("building kNN index from", TRAIN)
    print("indexed", build_index(), "exemplars ->", INDEX)
