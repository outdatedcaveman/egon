"""Extract Bruno's hand-curated bookmark folders into a labeled training set for
the MASTER KMS classifier. His `KMS Output` + top-level folders ARE the ground
truth (150k+ links). Same Amazon domain lands in Books OR Shopping by content,
so we train on titles — exactly what lets the ML tell cutlery from a book.

Full taxonomy (folder -> canonical category):
  Articles / Panop·Articles / Journals & Book Series  -> articles
  Books / Panop·Books / Shopping·Books                 -> books
  Science News / Panop·Science News                    -> science_news
  Content & News / Science Longform / Quanta           -> content_longform   (read-later)
  References / Other References                          -> references
  Data & Tools                                          -> data_tools
  Shopping (non-book)                                   -> shopping
  Study & Work                                          -> study_work
  Opportunities                                         -> opportunities
  Curios                                                -> curios
Session/tab-dump folders (FreshStart, "Minha Sessão …", KMS Input) are SKIPPED —
they're mixed, not curated labels.
"""
from __future__ import annotations
import json, re
from pathlib import Path
from collections import Counter, defaultdict
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
BMK = Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "User Data" / "Default" / "Bookmarks"
OUT = ROOT / "state" / "classifier" / "kms_training.jsonl"
PER_CAT_CAP = 4000          # balance + keep embedding time sane

SKIP_FOLDER = re.compile(r"freshstart|minha sess|sess(ã|a)o|tabs backup|window \d|kms input|"
                         r"latest \(|tutti|session esteban|fev19|kurzweil", re.I)


def category_for(path_names):
    """Map a folder path (list of names, leaf last) to a canonical category."""
    p = [n.lower() for n in path_names]
    joined = " / ".join(p)
    if any(SKIP_FOLDER.search(n) for n in path_names):
        return None
    # most-specific first
    if "shopping" in p:
        return "books" if "books" in p else "shopping"
    if "panop" in p:
        if "articles" in p: return "articles"
        if "books" in p: return "books"
        if "science news" in p: return "science_news"
        if "longform" in joined: return "content_longform"
    if "science longform" in joined or "read-in-place" in joined or "quanta" in p:
        return "content_longform"
    if "content & news" in p: return "content_longform"
    if "science news" in p or "new scientist" in p: return "science_news"
    if "journals & book series" in p: return "articles"
    if "articles" in p: return "articles"
    if "books" in p: return "books"
    if "data & tools" in p: return "data_tools"
    if "references" in p or "other references" in p: return "references"
    if "study & work" in p: return "study_work"
    if "opportunities" in p: return "opportunities"
    if "curios" in p: return "curios"
    return None


def main():
    b = json.loads(BMK.read_text(encoding="utf-8"))
    rows = []
    def walk(node, path):
        if node.get("type") == "folder":
            nm = node.get("name", "")
            for c in node.get("children", []):
                walk(c, path + [nm])
        elif node.get("type") == "url":
            cat = category_for(path)
            if not cat:
                return
            url = node.get("url", ""); title = (node.get("name") or "").strip()
            if not url.startswith("http") or len(title) < 4:
                return
            rows.append((cat, title, url))
    for rk in ("bookmark_bar", "other", "synced"):
        r = b.get("roots", {}).get(rk)
        if r: walk(r, [])

    # dedupe by (cat,url); cap per category
    seen = set(); bycat = defaultdict(list)
    for cat, title, url in rows:
        k = (cat, url)
        if k in seen: continue
        seen.add(k); bycat[cat].append((title, url))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as f:
        kept = Counter()
        for cat, items in bycat.items():
            for title, url in items[:PER_CAT_CAP]:
                f.write(json.dumps({"category": cat, "title": title, "url": url}, ensure_ascii=False) + "\n")
                kept[cat] += 1
    print("training rows per category (capped):")
    for c, n in kept.most_common():
        print(f"  {c:18} {n}")
    print(f"total: {sum(kept.values())}  ->  {OUT}")


if __name__ == "__main__":
    main()
