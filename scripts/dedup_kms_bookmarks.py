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
    # find ALL folders under any "KMS Output" parent (same May-28 dup bug hit them all)
    kms_roots = []
    def collect_kms(node, path=""):
        if node.get("type") == "folder":
            full = path + "/" + node.get("name", "")
            if node.get("name", "").strip() == "KMS Output":
                kms_roots.append(node)
            for c in node.get("children", []):
                collect_kms(c, full)
    for root in bm["roots"].values():
        if isinstance(root, dict):
            collect_kms(root)
    # every category subfolder under each KMS Output
    targets = []
    for kr in kms_roots:
        for sub in kr.get("children", []):
            if sub.get("type") == "folder":
                targets.append(sub)
    if not targets:
        print("no KMS Output subfolders found"); return

    total_before = total_after = 0
    plans = []
    for sub in targets:
        kids = sub.get("children", [])
        urls = [c for c in kids if c.get("type") == "url"]
        other = [c for c in kids if c.get("type") != "url"]
        seen, deduped = set(), []
        for c in urls:
            k = canon(c.get("url", ""))
            if k in seen:
                continue
            seen.add(k); deduped.append(c)
        total_before += len(urls); total_after += len(deduped)
        plans.append((sub, other, deduped, len(urls)))
        print(f"  {sub.get('name',''):20} {len(urls):>7} -> {len(deduped):>6} unique")
    print(f"TOTAL: {total_before} -> {total_after}  (removing {total_before-total_after} dups)")

    if not a.commit:
        print("DRY RUN — pass --commit to rewrite (Chrome must be closed; full backup first).")
        return

    BK.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    shutil.copy(BMFILE, BK / f"chrome_bookmarks_predeup_{stamp}.json")
    for sub, other, deduped, _ in plans:
        sub["children"] = other + deduped
    bm.pop("checksum", None)
    tmp = BMFILE.with_suffix(".egon_tmp")
    tmp.write_text(json.dumps(bm, ensure_ascii=False, indent=3), encoding="utf-8")
    tmp.replace(BMFILE)
    bak = BMFILE.with_name("Bookmarks.bak")
    if bak.exists():
        bak.replace(BK / f"chrome_Bookmarks.bak_{stamp}")
    print(f"DEDUPED all KMS Output folders -> {total_after} bookmarks "
          f"(removed {total_before-total_after} dups; backup chrome_bookmarks_predeup_{stamp}.json).")


if __name__ == "__main__":
    main()
