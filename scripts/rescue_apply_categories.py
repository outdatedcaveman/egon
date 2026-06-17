"""Apply Bruno's category corrections (rescue_recategorize.json) to the actual
Zotero collections, so his taxonomy signal is real in the library — not just in
training. Moves each recategorised item WITHIN the Panop subtree: drop it from
whatever Panop subcollection it's in, add it to the target one. Memberships
outside the Panop tree are left untouched.

Target collections (creates the missing ones under Panop = 24A43HSI):
  articles -> Articles · books -> Books · science_news -> Science News
  content_longform -> Science Longform (read-in-place)   [existing]
  references / data_tools / curios / opportunities / shopping -> created if absent

DRY by default; --commit creates collections + moves items (reversible: each
item's original collection list is logged to state/panop/backups/).

  python scripts/rescue_apply_categories.py
  python scripts/rescue_apply_categories.py --commit
"""
from __future__ import annotations
import sys, json, time, argparse
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import requests

PANOP = "24A43HSI"
# category -> (display name, existing key or None to create/lookup)
WANT = {
    "articles": "Articles", "books": "Books", "science_news": "Science News",
    "content_longform": "Science Longform (read-in-place)",
    "references": "References", "data_tools": "Data & Tools", "curios": "Curios",
    "opportunities": "Opportunities", "shopping": "Shopping",
}


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--commit", action="store_true"); a = ap.parse_args()
    pe = json.loads((ROOT / "panop_env.json").read_text(encoding="utf-8-sig"))
    H = {"Zotero-API-Key": pe["zotero_api_key"], "Zotero-API-Version": "3"}
    HW = {**H, "Content-Type": "application/json"}
    base = f"https://api.zotero.org/users/{pe['zotero_user_id']}"

    recat = json.loads((ROOT / "state" / "panop" / "rescue_recategorize.json").read_text(encoding="utf-8"))
    recat = [r for r in recat if r.get("key") and r.get("category") in WANT]

    # map existing Panop subcollections by name
    r = requests.get(f"{base}/collections/{PANOP}/collections?limit=100", headers=H, timeout=40)
    byname = {c["data"]["name"]: c["key"] for c in r.json()}
    panop_subtree = set(byname.values()) | {PANOP}

    cat_key, to_create = {}, []
    for cat, name in WANT.items():
        if name in byname:
            cat_key[cat] = byname[name]
        else:
            to_create.append((cat, name))

    from collections import Counter
    print(f"items to move (with keys): {len(recat)}")
    print(f"targets: {dict(Counter(r['category'] for r in recat))}")
    print(f"collections to CREATE under Panop: {[n for _, n in to_create] or 'none'}")

    if not a.commit:
        print("\nDRY RUN — pass --commit to create collections + move items (reversible).")
        return

    # create missing collections
    for cat, name in to_create:
        resp = requests.post(f"{base}/collections", headers=HW,
                             data=json.dumps([{"name": name, "parentCollection": PANOP}]), timeout=40)
        key = list((resp.json().get("successful") or {"0": {"key": None}}).values())[0]["key"]
        cat_key[cat] = key; panop_subtree.add(key)
        print(f"  created {name} -> {key}")
        time.sleep(0.3)

    # fetch current collections per item (batches of 50), then move
    keys = [r["key"] for r in recat]
    cur = {}
    for i in range(0, len(keys), 50):
        chunk = keys[i:i+50]
        rr = requests.get(f"{base}/items?itemKey={','.join(chunk)}&includeTrashed=1&limit=50", headers=H, timeout=40)
        for it in rr.json():
            cur[it["key"]] = {"version": it["version"], "collections": it["data"].get("collections", [])}
        time.sleep(0.3)

    bk = ROOT / "state" / "panop" / "backups" / f"recat_premove_{datetime.now():%Y%m%dT%H%M%S}.json"
    bk.write_text(json.dumps({k: v["collections"] for k, v in cur.items()}, ensure_ascii=False), encoding="utf-8")

    moved, skipped = 0, 0
    for r in recat:
        k, tgt = r["key"], cat_key[r["category"]]
        if k not in cur:
            skipped += 1; continue
        old = cur[k]["collections"]
        new = [c for c in old if c not in panop_subtree] + [tgt]
        if set(new) == set(old):
            skipped += 1; continue
        resp = requests.patch(f"{base}/items/{k}", headers={**HW, "If-Unmodified-Since-Version": str(cur[k]["version"])},
                              data=json.dumps({"collections": new}), timeout=40)
        if resp.status_code in (200, 204):
            moved += 1
        time.sleep(0.25)
    print(f"\nMOVED {moved} items to corrected collections | skipped {skipped} "
          f"(backup: {bk.name}, reversible).")


if __name__ == "__main__":
    main()
