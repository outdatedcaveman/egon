"""CLI for the restore-points system.

    python scripts/restore_point.py list
    python scripts/restore_point.py create <label> [--reason "..."]
    python scripts/restore_point.py show <point_id>
    python scripts/restore_point.py restore <point_id>           # DRY RUN
    python scripts/restore_point.py restore <point_id> --commit  # actually do it
    python scripts/restore_point.py prune [--keep N]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import restore_points as rp  # noqa: E402


def _print(o): print(json.dumps(o, indent=2, ensure_ascii=False))


def main():
    if len(sys.argv) < 2:
        print(__doc__); return 1
    cmd = sys.argv[1]

    if cmd == "list":
        pts = rp.list_points()
        if not pts:
            print("(no restore points)"); return 0
        for p in pts:
            sz = sum(c.get("size", 0) for c in p.get("captured", []))
            print(f"  {p['point_id']:55}  {p.get('label','?'):20}  {sz/1024:.0f} KB  {p.get('reason','')}")
        return 0

    if cmd == "create":
        if len(sys.argv) < 3:
            print("usage: create <label> [--reason \"…\"]"); return 1
        label = sys.argv[2]
        reason = ""
        if "--reason" in sys.argv:
            reason = sys.argv[sys.argv.index("--reason") + 1]
        meta = rp.create(label, reason=reason)
        print(f"created: {meta['point_id']}")
        for c in meta.get("captured", []):
            if "error" in c:
                print(f"  ! {c['src']}: {c['error']}")
            else:
                print(f"  + {Path(c['src']).name:35} ({c['size']} bytes)")
        return 0

    if cmd == "show":
        if len(sys.argv) < 3: print("usage: show <point_id>"); return 1
        pid = sys.argv[2]
        pts = {p["point_id"]: p for p in rp.list_points()}
        if pid not in pts: print(f"unknown: {pid}"); return 1
        _print(pts[pid])
        return 0

    if cmd == "restore":
        if len(sys.argv) < 3: print("usage: restore <point_id> [--commit]"); return 1
        pid = sys.argv[2]
        commit = "--commit" in sys.argv
        res = rp.restore(pid, dry_run=not commit)
        _print(res)
        if not commit:
            print("\n(dry run — re-run with --commit to actually restore)")
        return 0 if res.get("ok") else 1

    if cmd == "prune":
        keep = 30
        if "--keep" in sys.argv: keep = int(sys.argv[sys.argv.index("--keep") + 1])
        n = rp.prune(keep_n=keep)
        print(f"pruned {n} old restore points (kept newest {keep})")
        return 0

    print(__doc__); return 1


if __name__ == "__main__":
    raise SystemExit(main())
