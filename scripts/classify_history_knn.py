"""Classify the FULL Chrome history with the master engine, TOKEN-FREE:
authoritative domain rules first (arxiv->articles etc.), then the bookmark-
trained k-NN for everything else (full taxonomy). No AI quota burned, so it
can't get cut mid-run. Outputs per-category results + a saveable worklist; the
low-confidence tail is flagged for an optional AI-arbiter top-up (paced).
"""
from __future__ import annotations
import json, sys
from pathlib import Path
from collections import Counter
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
HISTORY = Path.home() / "Desktop" / "Takeout" / "Chrome" / "History.json"
OUT = ROOT / "state" / "panop" / "history_classified.json"
LOWCONF = ROOT / "state" / "panop" / "history_lowconf.json"

import numpy as np
import lib.kms_knn as KN
import lib.classifier.domain_tiers as DT

SAVE = {"articles", "books", "science_news", "content_longform", "references",
        "data_tools", "shopping", "study_work", "opportunities", "curios"}
# categories Panop/Zotero already handle vs the new ones to reengineer back
CONF_SAVE = 0.45        # >= this from kNN (or any domain-rule hit) -> accept


def main():
    bh = json.loads(HISTORY.read_text(encoding="utf-8")).get("Browser History") or []
    seen = {}
    for e in bh:
        u = e.get("url")
        if not u or not u.startswith("http"):
            continue
        t = (e.get("title") or "").strip()
        if u not in seen or (t and not seen[u]):
            seen[u] = t
    items = list(seen.items())
    print(f"unique URLs: {len(items)}")

    # 1) authoritative domain rule first
    results = {}
    need_knn = []
    for u, t in items:
        r = DT.classify(u)
        if r.action == "match" and r.category in SAVE:
            results[u] = {"title": t, "category": r.category, "confidence": 0.97, "source": "domain"}
        else:
            reason = (r.evidence or {}).get("reason", "")
            if str(reason).startswith("never_academic"):
                # still let kNN see it — Medium/etc. live here but can be longform
                need_knn.append((u, t))
            else:
                need_knn.append((u, t))
    print(f"domain-rule matches: {len(results)} | to k-NN: {len(need_knn)}")

    # 2) k-NN for the rest — batch embed
    emb, labels = KN._load()
    model = KN._load_model()
    texts = [KN._text(t, u) for u, t in need_knn]
    B = 1000
    lab = np.array(labels)
    for i in range(0, len(texts), B):
        q = model.encode(texts[i:i+B], normalize_embeddings=True, batch_size=64,
                         show_progress_bar=False).astype("float32")
        sims = q @ emb.T                         # (b, N)
        kidx = np.argpartition(-sims, KN.K, axis=1)[:, :KN.K]
        for r in range(q.shape[0]):
            u, t = need_knn[i + r]
            row = sims[r]; ks = kidx[r]
            votes = {}
            for j in ks:
                s = float(row[j])
                if s > 0:
                    votes[lab[j]] = votes.get(lab[j], 0.0) + s
            if not votes:
                results[u] = {"title": t, "category": "reject", "confidence": 0.0, "source": "knn"}
                continue
            tot = sum(votes.values()); cat = max(votes, key=votes.get)
            share = votes[cat] / tot
            top = float(row[ks[np.argmax(row[ks])]])
            conf = round(share * min(1.0, top / 0.6), 3)
            results[u] = {"title": t, "category": cat if conf >= CONF_SAVE else "reject",
                          "knn_category": cat, "confidence": conf, "source": "knn"}
        print(f"  k-NN {min(i+B,len(texts))}/{len(texts)}", flush=True)

    tally = Counter(v["category"] for v in results.values())
    print("\n=== full-history classification (token-free) ===")
    for c, n in tally.most_common():
        print(f"  {c:18} {n}")
    saveable = sum(n for c, n in tally.items() if c in SAVE)
    print(f"\n  saveable (any category): {saveable}")
    # low-confidence non-reject for optional AI top-up
    low = [{"url": u, "title": v["title"], "knn": v.get("knn_category")}
           for u, v in results.items()
           if v["source"] == "knn" and 0.30 <= v["confidence"] < CONF_SAVE]
    OUT.write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")
    LOWCONF.write_text(json.dumps(low, ensure_ascii=False), encoding="utf-8")
    print(f"  low-confidence for AI top-up: {len(low)}")
    print(f"\nresults -> {OUT}")


if __name__ == "__main__":
    main()
