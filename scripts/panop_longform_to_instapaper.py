"""Migrate the Zotero 'Science Longform' collection to Instapaper.

Bruno's rule (2026-06-14): longform does NOT live in Zotero — it goes to
Instapaper (+ bookmarks). This empties the Zotero Science Longform collection
into Instapaper: add each item to Instapaper, and only after a confirmed add
trash the Zotero copy (reversible). Backup first. Dry-run by default.
"""
from __future__ import annotations
import argparse, json, time
from pathlib import Path
import requests, httpx

ROOT = Path(__file__).resolve().parents[1]
BK = ROOT / "state" / "panop" / "backups"
INSTA = "https://www.instapaper.com/api/add"


def _insta_creds():
    d = json.loads((ROOT / "egon-config.json").read_text(encoding="utf-8-sig"))
    ip = d.get("instapaper") or {}
    return ip.get("username"), ip.get("password")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--sleep", type=float, default=0.6)
    args = ap.parse_args()

    env = json.loads((ROOT / "panop_env.json").read_text(encoding="utf-8-sig"))
    H = {"Zotero-API-Key": env["zotero_api_key"], "Zotero-API-Version": "3"}
    base = f"https://api.zotero.org/users/{env['zotero_user_id']}"
    root = env["zotero_collection_key"]
    iu, ip = _insta_creds()
    if not iu or not ip:
        raise SystemExit("no instapaper creds in egon-config.json")

    subs = requests.get(f"{base}/collections/{root}/collections?limit=50", headers=H, timeout=30).json()
    lf_key = next((c["key"] for c in subs if c["data"]["name"].startswith("Science Longform")), None)
    if not lf_key:
        raise SystemExit("Science Longform collection not found")

    items, start = [], 0
    while True:
        r = requests.get(f"{base}/collections/{lf_key}/items/top?limit=100&start={start}", headers=H, timeout=40)
        b = r.json()
        if not b:
            break
        items += b
        if len(b) < 100:
            break
        start += len(b)
    print(f"Science Longform items: {len(items)}")
    BK.mkdir(parents=True, exist_ok=True)
    (BK / "zotero_longform_premigration.json").write_text(
        json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")

    if not args.commit:
        print("DRY RUN — would add each to Instapaper then trash the Zotero copy.")
        for it in items[:6]:
            d = it["data"]
            print("   ", (d.get("title") or "")[:50], "|", (d.get("url") or "")[:55])
        return

    added, trashed, fail = 0, 0, 0
    to_trash = []
    with httpx.Client(timeout=25) as c:
        for it in items:
            d = it["data"]
            url = d.get("url")
            if not url:
                continue
            try:
                r = c.post(INSTA, data={"username": iu, "password": ip, "url": url,
                                        "title": (d.get("title") or "")[:200]})
                if r.status_code in (200, 201):
                    added += 1
                    to_trash.append((it["key"], it["version"]))
                else:
                    fail += 1
            except Exception:
                fail += 1
            time.sleep(args.sleep)
    # trash the successfully-migrated Zotero copies
    for i in range(0, len(to_trash), 50):
        chunk = to_trash[i:i+50]
        payload = [{"key": k, "version": v, "deleted": 1} for k, v in chunk]
        w = requests.post(f"{base}/items", headers={**H, "Content-Type": "application/json"},
                          data=json.dumps(payload), timeout=60)
        if w.status_code in (200, 201):
            trashed += len(w.json().get("successful") or {})
        time.sleep(0.4)
    print(f"\nInstapaper added {added} | Zotero trashed {trashed} | failed {fail}")


if __name__ == "__main__":
    main()
