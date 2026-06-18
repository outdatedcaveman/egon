"""Phase B of unification: merge the egon `Panop/<category>` bookmark folders
INTO the matching `KMS Output/<category>` folders, deduped, then remove the now-
empty Panop folders. KMS Output becomes the single bookmark base for BOTH
Inbox/Panop and Navigation/Routster (Bruno 2026-06-17: no separate bookmark dirs
per app). Chrome MUST be closed (raw Bookmarks write). Reversible: full backup.

  python scripts/merge_panop_into_kms.py            # dry-run plan
  python scripts/merge_panop_into_kms.py --commit
"""
from __future__ import annotations
import sys, json, shutil, argparse, subprocess
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

ROOT = Path(__file__).resolve().parents[1]
BK = ROOT / "state" / "panop" / "backups"
BMFILE = next(Path.home().glob("AppData/Local/Google/Chrome/User Data/Default/Bookmarks"))
TRACK = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "gclid"}


def canon(u):
    try:
        p = urlparse(u); net = (p.netloc or "").lower()
        if net.startswith("m."): net = "www." + net[2:]
        path = (p.path or "").rstrip("/") or "/"
        qs = sorted((k, v) for k, v in parse_qsl(p.query) if k.lower() not in TRACK)
        return urlunparse(((p.scheme or "https").lower(), net, path, "", urlencode(qs), ""))
    except Exception:
        return u


def chrome_running():
    try:
        o = subprocess.run(["powershell.exe", "-NoProfile", "-Command",
                            "(@(Get-Process chrome -ErrorAction SilentlyContinue)).Count"],
                           capture_output=True, text=True, timeout=20).stdout.strip()
        return o not in ("", "0")
    except Exception:
        return False


def find_named(node, name, path="", out=None):
    out = out if out is not None else []
    if node.get("type") == "folder":
        if node.get("name", "").strip() == name:
            out.append(node)
        for c in node.get("children", []):
            find_named(c, name, path, out)
    return out


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--commit", action="store_true"); a = ap.parse_args()
    if a.commit and chrome_running():
        print("ABORT: close Chrome first."); return
    bm = json.loads(BMFILE.read_text(encoding="utf-8"))

    kms_nodes = find_named(bm["roots"]["bookmark_bar"], "KMS Output") or \
        [n for r in bm["roots"].values() if isinstance(r, dict) for n in find_named(r, "KMS Output")]
    if not kms_nodes:
        print("KMS Output not found"); return
    kms = kms_nodes[0]                                   # the canonical KMS Output (bookmark bar)
    kms_subs = {s.get("name", "").strip(): s for s in kms.get("children", []) if s.get("type") == "folder"}

    panop_nodes = [n for r in bm["roots"].values() if isinstance(r, dict) for n in find_named(r, "Panop")]
    if not panop_nodes:
        print("no Panop bookmark folders to merge"); return

    def urls_of(folder):
        return [c for c in folder.get("children", []) if c.get("type") == "url"]

    moved, created = {}, []
    for pnode in panop_nodes:
        for pcat in [c for c in pnode.get("children", []) if c.get("type") == "folder"]:
            name = pcat.get("name", "").strip()
            dst = kms_subs.get(name)
            if dst is None:                              # create matching KMS Output subfolder
                dst = {"type": "folder", "name": name, "children": []}
                kms.setdefault("children", []).append(dst); kms_subs[name] = dst; created.append(name)
            existing = {canon(c.get("url", "")) for c in urls_of(dst)}
            add = [c for c in urls_of(pcat) if canon(c.get("url", "")) not in existing]
            if a.commit:
                dst.setdefault("children", []).extend(add)
            moved[name] = moved.get(name, 0) + len(add)

    print("Panop -> KMS Output merge plan:")
    for k, v in moved.items():
        print(f"  {k:18} +{v} new (rest were dups)")
    print(f"  collections created in KMS Output: {created or 'none'}")

    if not a.commit:
        print("\nDRY RUN — pass --commit (Chrome closed; full backup; removes empty Panop folders).")
        return

    # remove the Panop folders from their parents
    def strip_panop(node):
        if node.get("type") == "folder":
            node["children"] = [c for c in node.get("children", [])
                                if not (c.get("type") == "folder" and c.get("name", "").strip() == "Panop")]
            for c in node["children"]:
                strip_panop(c)
    for r in bm["roots"].values():
        if isinstance(r, dict):
            strip_panop(r)

    BK.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    shutil.copy(BMFILE, BK / f"chrome_bookmarks_premerge_{stamp}.json")
    bm.pop("checksum", None)
    tmp = BMFILE.with_suffix(".egon_tmp")
    tmp.write_text(json.dumps(bm, ensure_ascii=False, indent=3), encoding="utf-8")
    tmp.replace(BMFILE)
    bak = BMFILE.with_name("Bookmarks.bak")
    if bak.exists():
        bak.replace(BK / f"chrome_Bookmarks.bak_merge_{stamp}")
    print(f"MERGED Panop -> KMS Output ({sum(moved.values())} moved) and removed Panop folders. "
          f"Backup chrome_bookmarks_premerge_{stamp}.json")


if __name__ == "__main__":
    main()
