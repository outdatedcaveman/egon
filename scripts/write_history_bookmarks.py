"""Write the classified-history bookmarks DIRECTLY into Chrome's Bookmarks file,
under Panop/<category> folders. Chrome MUST be closed (raw file write). Reuses
the resolved URLs from history_save_ledger.jsonl. Backs up the Bookmarks file
first; dedups within each folder. Reversible (the .bak backup).
"""
from __future__ import annotations
import json, os, time, uuid, shutil
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "state" / "panop" / "history_save_ledger.jsonl"
BK = ROOT / "state" / "panop" / "backups"
BFOLDER = {"articles": "Articles", "books": "Books", "science_news": "Science News",
           "content_longform": "Science Longform (read-in-place)", "data_tools": "Data & Tools",
           "references": "References", "shopping": "Shopping", "opportunities": "Opportunities",
           "curios": "Curios", "study_work": "Study & Work"}


def _chrome_running():
    import subprocess
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command",
                            "if (Get-Process chrome -ErrorAction SilentlyContinue) {'yes'} else {'no'}"],
                           capture_output=True, text=True, timeout=10)
        return "yes" in (r.stdout or "").lower()
    except Exception:
        return True


def main():
    if _chrome_running():
        raise SystemExit("Chrome is RUNNING — close it first (raw bookmark write would be clobbered).")
    # gather items from ledger (url -> surl, cat), dedup by surl+cat
    items = {}
    cls = json.loads((ROOT / "state" / "panop" / "history_classified.json").read_text(encoding="utf-8"))
    title_by = {u: (v.get("title") or u) for u, v in cls.items()}
    for l in LEDGER.read_text(encoding="utf-8").splitlines():
        try: o = json.loads(l)
        except Exception: continue
        cat = o.get("cat"); su = o.get("surl") or o.get("url")
        if cat in BFOLDER and su:
            items[(su, cat)] = title_by.get(o.get("url"), su)
    print(f"bookmark entries to write: {len(items)}")

    prof = os.path.join(os.environ["USERPROFILE"], "AppData", "Local", "Google", "Chrome", "User Data", "Default")
    bpath = os.path.join(prof, "Bookmarks")
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    BK.mkdir(parents=True, exist_ok=True)
    shutil.copy(bpath, BK / f"chrome_bookmarks_backup_{stamp}.json")
    data = json.load(open(bpath, encoding="utf-8"))

    def stampms(): return str(int(time.time() * 1000000))
    other = data.setdefault("roots", {}).setdefault("other", {}); other.setdefault("children", [])
    panop = next((c for c in other["children"] if c.get("type") == "folder" and c.get("name") == "Panop"), None)
    if not panop:
        panop = {"children": [], "date_added": stampms(), "guid": str(uuid.uuid4()), "name": "Panop", "type": "folder"}
        other["children"].append(panop)

    def folder(name):
        f = next((c for c in panop["children"] if c.get("type") == "folder" and c.get("name") == name), None)
        if not f:
            f = {"children": [], "date_added": stampms(), "guid": str(uuid.uuid4()), "name": name, "type": "folder"}
            panop["children"].append(f)
        return f

    added = 0
    seen_per = {}
    for (su, cat), title in items.items():
        f = folder(BFOLDER[cat])
        ex = seen_per.get(id(f))
        if ex is None:
            ex = {c.get("url") for c in f.get("children", []) if c.get("type") == "url"}
            seen_per[id(f)] = ex
        if su in ex:
            continue
        ex.add(su)
        f["children"].append({"date_added": stampms(), "guid": str(uuid.uuid4()),
                              "name": (title or su)[:300], "type": "url", "url": su})
        added += 1

    data.pop("checksum", None)
    tmp = bpath + ".panop.tmp"
    json.dump(data, open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
    os.replace(tmp, bpath)
    bak = bpath + ".bak"
    if os.path.exists(bak):
        try: os.remove(bak)
        except Exception: pass
    from collections import Counter
    print(f"wrote {added} new bookmarks under Panop/ ->", dict(Counter(BFOLDER[c] for (_, c) in items)))


if __name__ == "__main__":
    main()
