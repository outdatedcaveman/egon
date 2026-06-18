"""Dedup the KMS Output/Articles bookmark folder: a May-28 runaway bulk-write
left 112,374 entries that are only ~2,387 unique URLs (each ~47x). Collapse to
one bookmark per unique (canonical) URL, keeping the first occurrence's title/
date. Chrome MUST be closed (raw Bookmarks-file write). Reversible: full backup
of the Bookmarks file first.

  python scripts/dedup_kms_bookmarks.py            # dry-run counts
  python scripts/dedup_kms_bookmarks.py --commit
"""
from __future__ import annotations
import sys, json, shutil, argparse, subprocess
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

ROOT = Path(__file__).resolve().parents[1]
BK = ROOT / "state" / "panop" / "backups"
BMFILE = next(Path.home().glob("AppData/Local/Google/Chrome/User Data/Default/Bookmarks"))
TARGET = "KMS Output/Articles"
TRACK = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid",
         "gclid", "mc_cid", "mc_eid", "ref", "ref_src"}


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
        out = subprocess.run(["powershell.exe", "-NoProfile", "-Command",
                              "(@(Get-Process chrome -ErrorAction SilentlyContinue)).Count"],
                             capture_output=True, text=True, timeout=20).stdout.strip()
        return out not in ("", "0")
    except Exception:
        return False


def find_folder(node, path=""):
    if node.get("type") == "folder":
        full = path + "/" + node.get("name", "")
        if full.endswith(TARGET):
            return node
        for c in node.get("children", []):
            r = find_folder(c, full)
            if r:
                return r
    return None


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--commit", action="store_true"); a = ap.parse_args()
    if a.commit and chrome_running():
        print("ABORT: Chrome is running — close it first (raw Bookmarks write would be clobbered).")
        return
    bm = json.loads(BMFILE.read_text(encoding="utf-8"))
    node = None
    for root in bm["roots"].values():
        if isinstance(root, dict):
            node = node or find_folder(root)
    if not node:
        print("KMS Output/Articles not found"); return

    kids = node.get("children", [])
    urls = [c for c in kids if c.get("type") == "url"]
    other = [c for c in kids if c.get("type") != "url"]   # preserve any subfolders
    seen, deduped = set(), []
    for c in urls:
        k = canon(c.get("url", ""))
        if k in seen:
            continue
        seen.add(k); deduped.append(c)
    print(f"{TARGET}: {len(urls)} url entries -> {len(deduped)} unique (subfolders kept: {len(other)})")

    if not a.commit:
        print("DRY RUN — pass --commit to rewrite (Chrome must be closed; full backup first).")
        return

    BK.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    shutil.copy(BMFILE, BK / f"chrome_bookmarks_predeup_{stamp}.json")
    node["children"] = other + deduped
    bm.pop("checksum", None)                              # let Chrome recompute
    tmp = BMFILE.with_suffix(".egon_tmp")
    tmp.write_text(json.dumps(bm, ensure_ascii=False, indent=3), encoding="utf-8")
    tmp.replace(BMFILE)
    # drop stale .bak so Chrome can't restore the bloated version
    bak = BMFILE.with_name("Bookmarks.bak")
    if bak.exists():
        bak.replace(BK / f"chrome_Bookmarks.bak_{stamp}")
    print(f"DEDUPED -> {len(deduped)} bookmarks (backup chrome_bookmarks_predeup_{stamp}.json). "
          f"Removed {len(urls)-len(deduped)} duplicates.")


if __name__ == "__main__":
    main()
