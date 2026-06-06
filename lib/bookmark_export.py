"""Bookmark exporter — Chrome/Edge Bookmarks → TSV.

Bruno asked (2026-05-28): "ideally all my saved links would be in this
format as well for us to run analyses." This module is the reusable
implementation behind that ask.

Inputs:
  • Any Chromium-family Bookmarks JSON (Chrome, Edge, Brave, Vivaldi…).
  • Optional path filter: dump one folder (e.g. "bookmark_bar > Data & Tools")
    or the whole tree.

Outputs:
  • TSV with columns: folder_path, title, url, date_added_iso,
    date_modified_iso, guid. Stable schema for downstream analysis.

Usage from CLI:
    python -m lib.bookmark_export                          # dump everything from default Chrome profile
    python -m lib.bookmark_export --browser=edge           # use Edge instead
    python -m lib.bookmark_export --folder="Data & Tools"  # only that folder (any depth)
    python -m lib.bookmark_export --out=path/to/out.tsv    # custom output

The CLI is a thin wrapper — import `walk_bookmarks` or `export_to_tsv`
directly from other Egon modules for programmatic access.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


# Standard locations per browser. We pick the first existing per browser
# (Default profile wins). Additional profiles (Profile 1, Profile 2…) are
# probed automatically.
_local_appdata = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
_BROWSER_ROOTS = {
    "chrome":  os.path.join(_local_appdata, "Google", "Chrome", "User Data"),
    "edge":    os.path.join(_local_appdata, "Microsoft", "Edge", "User Data"),
    "brave":   os.path.join(_local_appdata, "BraveSoftware", "Brave-Browser", "User Data"),
    "vivaldi": os.path.join(_local_appdata, "Vivaldi", "User Data"),
}



def find_bookmarks(browser: str = "chrome", profile: str = "Default") -> Path | None:
    """Find a browser's Bookmarks file. Returns None if not present."""
    root = _BROWSER_ROOTS.get(browser.lower())
    if not root:
        return None
    p = Path(root) / profile / "Bookmarks"
    return p if p.exists() else None


def list_profiles(browser: str = "chrome") -> list[str]:
    root = _BROWSER_ROOTS.get(browser.lower())
    if not root or not Path(root).exists():
        return []
    return sorted(d.name for d in Path(root).iterdir()
                  if d.is_dir() and (d / "Bookmarks").exists())


def _chrome_ts_to_iso(ts: str | int | None) -> str:
    """Chrome stores timestamps as microseconds since 1601-01-01 UTC. We
    convert to a stable ISO string (UTC). Empty/zero values become "".
    """
    try:
        ts = int(ts) if ts is not None else 0
    except Exception:
        return ""
    if ts <= 0:
        return ""
    # Chrome epoch: 1601-01-01; Unix epoch: 1970-01-01. Difference in
    # microseconds = 11644473600 * 1_000_000.
    unix_us = ts - 11644473600_000_000
    try:
        return datetime.fromtimestamp(unix_us / 1_000_000, tz=timezone.utc) \
                       .isoformat(timespec="seconds")
    except Exception:
        return ""


def walk_bookmarks(bookmarks_path: Path,
                   only_folder: str | None = None) -> Iterator[dict]:
    """Yield one dict per URL bookmark. If `only_folder` is given, yield
    only links whose folder path contains that segment (case-insensitive
    substring on the segment name, e.g. "Data & Tools" matches anywhere
    in the tree)."""
    with bookmarks_path.open(encoding="utf-8") as f:
        bm = json.load(f)
    needle = only_folder.lower() if only_folder else None

    def walk(node, path):
        if not isinstance(node, dict):
            return
        kind = node.get("type")
        if kind == "url":
            include = True
            if needle:
                include = any(needle in seg.lower() for seg in path)
            if include:
                yield {
                    "folder_path": " > ".join(path) if path else "",
                    "title": node.get("name", "") or "",
                    "url": node.get("url", "") or "",
                    "date_added_iso": _chrome_ts_to_iso(node.get("date_added")),
                    "date_modified_iso": _chrome_ts_to_iso(node.get("date_modified")),
                    "guid": node.get("guid", "") or "",
                }
        elif kind == "folder":
            new_path = path + [node.get("name", "")]
            for c in node.get("children", []) or []:
                yield from walk(c, new_path)

    for _root_key, root_val in (bm.get("roots") or {}).items():
        if isinstance(root_val, dict) and root_val.get("type") == "folder":
            yield from walk(root_val, [root_val.get("name", "")])


def export_to_tsv(bookmarks_path: Path, out_path: Path,
                  only_folder: str | None = None) -> int:
    """Write a TSV. Returns the row count."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out_path.open("w", encoding="utf-8", newline="") as f:
        f.write("folder_path\ttitle\turl\tdate_added_iso\tdate_modified_iso\tguid\n")
        for row in walk_bookmarks(bookmarks_path, only_folder=only_folder):
            # TSV requires no embedded tabs/newlines in fields; sanitise.
            vals = [str(row[k]).replace("\t", " ").replace("\n", " ").replace("\r", " ")
                    for k in ("folder_path", "title", "url",
                              "date_added_iso", "date_modified_iso", "guid")]
            f.write("\t".join(vals) + "\n")
            n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--browser", default="chrome",
                    choices=list(_BROWSER_ROOTS.keys()))
    ap.add_argument("--profile", default="Default")
    ap.add_argument("--folder", default=None,
                    help="Only dump links inside a folder name match (any depth)")
    ap.add_argument("--out", default=None,
                    help="Output TSV path; default writes to state/exports/")
    ap.add_argument("--all-profiles", action="store_true",
                    help="Dump every profile in the chosen browser, prefixed with profile name")
    args = ap.parse_args()

    egon_root = Path(__file__).resolve().parent.parent
    default_out_dir = egon_root / "state" / "exports"

    profiles = (list_profiles(args.browser) if args.all_profiles
                else [args.profile])
    if not profiles:
        print(f"no Bookmarks file found for browser={args.browser}")
        return 2

    total = 0
    for prof in profiles:
        path = find_bookmarks(args.browser, prof)
        if not path:
            print(f"  skip {prof}: no Bookmarks file")
            continue
        suffix = f"_{args.folder.lower().replace(' & ', '_').replace(' ', '_')}" if args.folder else "_all"
        out = Path(args.out) if args.out else \
              default_out_dir / f"bookmarks_{args.browser}_{prof.replace(' ', '_')}{suffix}.tsv"
        n = export_to_tsv(path, out, only_folder=args.folder)
        print(f"  {args.browser}/{prof}: {n} rows -> {out}")
        total += n
    print(f"\nTOTAL: {total} bookmarks across {len(profiles)} profile(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
