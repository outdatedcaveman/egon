"""Apply category verdicts to Panop Zotero suspects: move each item into the
correct sub-collection (Articles/Books/Science News/Science Longform) or trash
it (reject). Reversible — trash goes to Zotero Trash; moves are re-runnable.
Dry-run by default; --commit to act.

Reads:
  state/panop/backups/suspects.json   (index -> item key/version/cat/url/title)
  state/panop/backups/verdicts.json   (index -> target category | "reject")
"""
from __future__ import annotations
import argparse, json, time
from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parents[1]
BK = ROOT / "state" / "panop" / "backups"
CAT2NAME = {"articles": "Articles", "books": "Books",
            "science_news": "Science News",
            "science_longform": "Science Longform (read-in-place)"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--verdicts", default=str(BK / "verdicts.json"))
    ap.add_argument("--sleep", type=float, default=0.4)
    args = ap.parse_args()

    env = json.loads((ROOT / "panop_env.json").read_text(encoding="utf-8-sig"))
    H = {"Zotero-API-Key": env["zotero_api_key"], "Zotero-API-Version": "3"}
    base = f"https://api.zotero.org/users/{env['zotero_user_id']}"
    root = env["zotero_collection_key"]

    subs = requests.get(f"{base}/collections/{root}/collections?limit=50", headers=H, timeout=30).json()
    name2key = {c["data"]["name"]: c["key"] for c in subs}
    cat2key = {cat: name2key[nm] for cat, nm in CAT2NAME.items() if nm in name2key}

    suspects = json.loads((BK / "suspects.json").read_text(encoding="utf-8"))
    verdicts = {k: v for k, v in json.loads(Path(args.verdicts).read_text(encoding="utf-8")).items()
                if not k.startswith("_")}

    moves, trashes, noops = [], [], 0
    for idx_s, target in verdicts.items():
        it = suspects[int(idx_s)]
        if target == "reject":
            trashes.append(it)
        else:
            tgt_key = cat2key.get(target)
            cur = name2key.get(CAT2NAME.get(it["cat"], ""), "")  # current sub
            if tgt_key and tgt_key != cur:
                moves.append((it, target, tgt_key, cur))
            else:
                noops += 1

    from collections import Counter
    print(f"moves: {len(moves)}  {dict(Counter(m[1] for m in moves))}")
    print(f"trash (reject): {len(trashes)}")
    print(f"already-correct/noop: {noops}")
    if not args.commit:
        print("\nDRY RUN — sample:")
        for it, tgt, *_ in moves[:6]:
            print(f"  MOVE  {it['cat']:14}->{tgt:16} {(it['title'] or '')[:48]!r}")
        for it in trashes[:6]:
            print(f"  TRASH {(it['title'] or '')[:48]!r} | {it['url'][:48]}")
        return

    # apply moves: fetch current item, set collections=[root, target], POST
    done_m = done_t = fail = 0
    for it, target, tgt_key, cur in moves:
        try:
            r = requests.get(f"{base}/items/{it['key']}", headers=H, timeout=30)
            data = r.json()["data"]; ver = r.json()["version"]
            cols = set(data.get("collections") or [])
            cols.discard(cur); cols.add(tgt_key); cols.add(root) if root in cols else None
            payload = [{"key": it["key"], "version": ver, "collections": sorted(cols)}]
            w = requests.post(f"{base}/items", headers={**H, "Content-Type": "application/json"},
                              data=json.dumps(payload), timeout=40)
            done_m += 1 if w.status_code in (200, 201) and (w.json().get("successful")) else 0
            if not (w.status_code in (200, 201) and w.json().get("successful")):
                fail += 1
        except Exception:
            fail += 1
        time.sleep(args.sleep)
    # apply trash in batches of 50
    keys = [(it["key"], it["version"]) for it in trashes]
    for i in range(0, len(keys), 50):
        chunk = keys[i:i+50]
        payload = [{"key": k, "version": v, "deleted": 1} for k, v in chunk]
        w = requests.post(f"{base}/items", headers={**H, "Content-Type": "application/json"},
                          data=json.dumps(payload), timeout=60)
        if w.status_code in (200, 201):
            done_t += len(w.json().get("successful") or {})
        time.sleep(args.sleep)
    print(f"\nMOVED {done_m}/{len(moves)} | TRASHED {done_t}/{len(trashes)} | failed {fail}")


if __name__ == "__main__":
    main()
