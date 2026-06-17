"""Apply Bruno's triage corrections (rescue_overrides.json from the review tool)
and TEACH the classifier from them — his manual judgments are the highest-value
training signal (the converging-to-master learning he asked for).

For each correction:
  - verdict 'discard' on a restore-candidate -> drop it from the restore set.
  - verdict 'keep'    on a discard item      -> add it to the restore set.
  - move_to set                              -> record target category for the
    categorisation phase (state/panop/rescue_recategorize.json).
  - every correction -> lib.kms_knn.learn(title, url, category)  (category =
    chosen taxonomy / move_to, or 'reject' for discards) so the native model
    stops repeating the mistake.

DRY by default; --commit rewrites rescue_restore_candidates.json (backed up) and
trains the model.

  python scripts/rescue_apply_overrides.py --file ~/Downloads/rescue_overrides.json
  python scripts/rescue_apply_overrides.py --file ... --commit
"""
from __future__ import annotations
import sys, json, argparse, shutil
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.rescue_audit import canon


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True, help="path to exported rescue_overrides.json")
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()

    ovs = json.loads(Path(a.file).expanduser().read_text(encoding="utf-8"))
    cand_path = ROOT / "state" / "panop" / "rescue_restore_candidates.json"
    cands = json.loads(cand_path.read_text(encoding="utf-8"))
    by_canon = {canon(c["url"]): c for c in cands}

    drop, add, recat, train = [], [], [], []
    for o in ovs:
        cu = canon(o["url"])
        verdict, move_to = o.get("verdict"), (o.get("move_to") or "")
        if verdict == "discard" and cu in by_canon:
            drop.append(cu)
            train.append((o.get("title", ""), o["url"], "reject"))
        elif verdict == "keep" and cu not in by_canon:
            add.append({"src": "zotero_trash" if o.get("source") == "library" else o.get("source", "history"),
                        "key": o.get("key") or None, "version": None, "title": o.get("title", ""),
                        "url": o["url"], "verdict": "RESTORE", "evidence": "bruno_override",
                        "rtitle": o.get("title", "")})
            train.append((o.get("title", ""), o["url"], move_to or "articles"))
        if move_to:
            recat.append({"url": o["url"], "key": o.get("key") or None, "category": move_to})
            train.append((o.get("title", ""), o["url"], move_to))

    new_cands = [c for c in cands if canon(c["url"]) not in set(drop)] + add
    print(f"corrections: {len(ovs)} | drop {len(drop)} | add {len(add)} | recategorise {len(recat)}")
    print(f"restore set: {len(cands)} -> {len(new_cands)} | training examples: {len(train)}")

    if not a.commit:
        print("\nDRY RUN — pass --commit to apply + train.")
        for o in ovs[:12]:
            print(f"  {o.get('engine','?')}->{o.get('verdict','?')}"
                  f"{(' ['+o['move_to']+']') if o.get('move_to') else ''}  {o.get('title','')[:54]}")
        return

    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    shutil.copy(cand_path, cand_path.with_suffix(f".{stamp}.bak"))
    cand_path.write_text(json.dumps(new_cands, ensure_ascii=False, indent=1), encoding="utf-8")
    if recat:
        rp = ROOT / "state" / "panop" / "rescue_recategorize.json"
        existing = json.loads(rp.read_text(encoding="utf-8")) if rp.exists() else []
        rp.write_text(json.dumps(existing + recat, ensure_ascii=False, indent=1), encoding="utf-8")

    taught = 0
    try:
        import lib.kms_knn as knn
        for title, url, cat in train:
            if title or url:
                knn.learn(title, url, cat); taught += 1
    except Exception as e:
        print(f"  (training skipped: {type(e).__name__}: {e})")
    print(f"applied. restore set rewritten (backup .{stamp}.bak). taught classifier {taught} examples.")
    print("re-run scripts/rescue_report.py to refresh the review report.")


if __name__ == "__main__":
    main()
