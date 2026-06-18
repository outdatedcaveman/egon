"""Act on the rescue audit: bring back everything flagged RESTORE / RESTORE?.
Reads state/panop/rescue_restore_candidates.json (produced by rescue_audit.py).

Two kinds of candidate:
  - Zotero-trash items (carry key+version)  -> un-trash in place (deleted:0),
    which returns them to whichever collection they were in. Reversible: re-trash.
  - history reject items (no key)           -> written to a re-save worklist
    (state/panop/rescue_history_resave.json) for the normal save pipeline; this
    script does NOT save them (that path writes to Zotero/Instapaper+bookmarks).

DRY by default. Pass --commit to un-trash. Re-fetches live versions first so a
stale version can't 412 the batch.

  python scripts/rescue_restore.py            # dry run — counts only
  python scripts/rescue_restore.py --commit   # un-trash the Zotero items
"""
from __future__ import annotations
import sys, json, time, argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import requests


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--include-maybe", action="store_true",
                    help="also restore RESTORE? (walled/ambiguous, kept out of caution). Default: yes")
    a = ap.parse_args()

    pe = json.loads((ROOT / "panop_env.json").read_text(encoding="utf-8-sig"))
    H = {"Zotero-API-Key": pe["zotero_api_key"], "Zotero-API-Version": "3"}
    base = f"https://api.zotero.org/users/{pe['zotero_user_id']}"

    cands = json.loads((ROOT / "state" / "panop" / "rescue_restore_candidates.json").read_text(encoding="utf-8"))
    want = {"RESTORE", "RESTORE?"}  # include-maybe is on by default; RESTORE? defaults to keep
    cands = [c for c in cands if c.get("verdict") in want]

    ztrash = [c for c in cands if c.get("src") == "zotero_trash" and c.get("key")]
    hist = [c for c in cands if c.get("src") != "zotero_trash"]
    print(f"restore candidates: {len(cands)}  (zotero-trash: {len(ztrash)}, history: {len(hist)})")

    # history re-save worklist (handled by the save pipeline, not here)
    (ROOT / "state" / "panop" / "rescue_history_resave.json").write_text(
        json.dumps(hist, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"history re-save worklist -> state/panop/rescue_history_resave.json ({len(hist)})")

    if not a.commit:
        print("\nDRY RUN — pass --commit to un-trash the Zotero items (reversible).")
        return

    # re-fetch live versions so the batch can't 412 on a stale version
    keys = [c["key"] for c in ztrash]
    live = {}
    for i in range(0, len(keys), 50):
        chunk = keys[i:i+50]
        # includeTrashed=1 is REQUIRED: the items we're restoring are IN the
        # trash, and /items excludes trashed items by default (empty result).
        r = requests.get(f"{base}/items?itemKey={','.join(chunk)}&includeTrashed=1&limit=50",
                         headers=H, timeout=40)
        if r.status_code == 200:
            for it in r.json():
                live[it["key"]] = it["version"]
        time.sleep(0.3)

    restored = 0
    for i in range(0, len(keys), 50):
        chunk = [k for k in keys[i:i+50] if k in live]
        if not chunk:
            continue
        payload = [{"key": k, "version": live[k], "deleted": 0} for k in chunk]
        r = requests.post(f"{base}/items", headers={**H, "Content-Type": "application/json"},
                          data=json.dumps(payload), timeout=60)
        if r.status_code in (200, 201):
            restored += len((r.json().get("successful") or {}))
        time.sleep(0.4)
    print(f"\nUN-TRASHED {restored}/{len(keys)} items back into their collections (re-trashable to undo).")


if __name__ == "__main__":
    main()
