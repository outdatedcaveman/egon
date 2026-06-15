"""Run egon's MASTER classifier (lib/classifier) over Routster's Chrome-history
scrape. Stage 1 = deterministic sieve (no page fetch): domain tiers + hard gates
separate the navigation noise (reject) from the saveable candidates and the
ambiguous middle that needs the AI arbiter / page text. Pure read; writes a
candidate worklist + a tally. Same engine Panop/Inbox use → no divergence.
"""
from __future__ import annotations
import json, sys
from pathlib import Path
from collections import Counter
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
HISTORY = Path.home() / "Desktop" / "Takeout" / "Chrome" / "History.json"
OUT = ROOT / "state" / "panop" / "routster_history_candidates.json"

import lib.classifier as C

bh = (json.loads(HISTORY.read_text(encoding="utf-8")).get("Browser History") or [])
# dedupe by url, keep a representative title
seen = {}
for e in bh:
    u = e.get("url")
    if not u or not u.startswith("http"):
        continue
    t = (e.get("title") or "").strip()
    if u not in seen or (t and not seen[u]):
        seen[u] = t
print(f"unique http URLs: {len(seen)}")

SAVE = {"articles", "books", "science_news", "science_longform"}
tally = Counter()
candidates = []     # action==match into a saveable category
abstain = []        # ambiguous middle (needs AI arbiter + page text)
for u, t in seen.items():
    try:
        r = C.classify(u, {"title": t})
    except Exception:
        tally["error"] += 1; continue
    if r.action == "match" and r.category in SAVE:
        tally[f"match:{r.category}"] += 1
        candidates.append({"url": u, "title": t, "category": r.category,
                           "confidence": round(r.confidence, 3), "layer": r.layer})
    elif r.action == "match":           # matched a non-save category (shouldn't happen)
        tally[f"match:{r.category}"] += 1
    elif r.action == "review":
        tally["review"] += 1
        abstain.append({"url": u, "title": t, "hint": r.category})
    else:
        # abstain: either clearly-rejectable noise or ambiguous-needs-AI.
        host = (urlparse(u).netloc or "").lower()
        reason = (r.evidence or {}).get("reason", "")
        if str(reason).startswith("never_academic"):
            tally["reject:domain"] += 1
        else:
            tally["abstain"] += 1
            abstain.append({"url": u, "title": t, "hint": None})

print("\n=== master-engine deterministic sieve ===")
for k, v in tally.most_common():
    print(f"  {k:24} {v}")
print(f"\n  clear saveable candidates: {len(candidates)}")
print(f"  ambiguous (need AI arbiter + text): {len(abstain)}")

OUT.write_text(json.dumps({"candidates": candidates, "ambiguous": abstain[:5000]},
                          ensure_ascii=False), encoding="utf-8")
print(f"\nworklist -> {OUT}")
