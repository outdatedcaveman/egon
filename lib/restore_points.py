"""Restore points: per-operation snapshots of all writable Egon state.

Goal: no automated operation can damage state Bruno can't recover. Every
destructive code path (Panop sweep, drain, Zotero pushes that touch many
items, scheduled-task changes) creates a restore point BEFORE acting.

Layout:
    egon/state/restore_points/
        2026-05-15T14-32-05_panop_drain/
            panop_history.json            ← state snapshot
            panop_ai_profiles.json
            panop_config.json
            panop_env.json
            scheduled_tasks.xml           ← Windows task export
            meta.json                     ← what triggered this point, what changed
        2026-05-15T15-12-44_zotero_purge/
            zotero_items_before.json      ← Zotero state pre-mutation
            meta.json
        journal.jsonl                     ← append-only list of every snapshot

API:
    rp = create("panop_drain", reason="nightly_run", extra_files=[...])
    list_points() -> list of dicts
    restore(point_id, dry_run=True) -> what would be restored
"""
from __future__ import annotations

import json
import shutil
import subprocess
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Iterable

EGON = Path(__file__).resolve().parents[1]
ROOT = EGON / "state" / "restore_points"
JOURNAL = ROOT / "journal.jsonl"

# Files that every snapshot captures (when they exist). Add to this list when
# new state files become user-data-relevant.
DEFAULT_SNAPSHOT_FILES = [
    EGON / "state" / "panop" / "panop_history.json",
    EGON / "state" / "panop" / "panop_ai_profiles.json",
    EGON / "state" / "panop" / "panop_config.json",
    EGON / "external" / "panop_server" / "panop_env.json",
    EGON / "egon-config.json",
]


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H-%M-%S")


def _journal_append(entry: dict) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    with JOURNAL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _export_scheduled_tasks(dest: Path) -> None:
    """Best-effort: dump current state of every KMS-* scheduled task."""
    try:
        r = subprocess.run(
            ["schtasks", "/Query", "/FO", "XML", "/V"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=20,
        )
        if r.returncode == 0 and r.stdout:
            dest.write_text(r.stdout, encoding="utf-8")
    except Exception:
        pass


def _snapshot_dest(point_dir: Path, src: Path, used: set[str]) -> Path:
    """Return a collision-proof snapshot path inside point_dir."""
    name = src.name
    if name in used:
        digest = hashlib.sha1(str(src).encode("utf-8", errors="replace")).hexdigest()[:10]
        name = f"{src.stem}.{digest}{src.suffix}"
    used.add(name)
    return point_dir / name


def create(label: str, reason: str = "", extra_files: Iterable[Path] = ()) -> dict:
    """Create a new restore point. Returns a dict describing it.

    label: short identifier (e.g. "panop_drain", "zotero_purge")
    reason: free-text why we're snapshotting now
    extra_files: paths beyond DEFAULT_SNAPSHOT_FILES to also include
    """
    ts = _ts()
    point_id = f"{ts}_{label}"
    point_dir = ROOT / point_id
    point_dir.mkdir(parents=True, exist_ok=True)

    captured = []
    used_names: set[str] = set()
    for src in list(DEFAULT_SNAPSHOT_FILES) + list(extra_files):
        try:
            src = Path(src)
            if not src.exists() or not src.is_file():
                continue
            dest = _snapshot_dest(point_dir, src, used_names)
            shutil.copy2(src, dest)
            captured.append({"src": str(src), "dest": str(dest),
                             "size": src.stat().st_size})
        except Exception as e:
            captured.append({"src": str(src), "error": str(e)[:200]})

    _export_scheduled_tasks(point_dir / "scheduled_tasks.xml")

    meta = {
        "point_id": point_id,
        "ts": datetime.now().isoformat(timespec="seconds"),
        "label": label,
        "reason": reason,
        "captured": captured,
    }
    (point_dir / "meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    _journal_append({"event": "created", **{k: meta[k] for k in ("point_id","ts","label","reason")}})
    return meta


def list_points() -> list[dict]:
    if not ROOT.exists():
        return []
    out = []
    for d in sorted(ROOT.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        m = d / "meta.json"
        if m.exists():
            try:
                out.append(json.loads(m.read_text(encoding="utf-8")))
            except Exception:
                out.append({"point_id": d.name, "_corrupt": True})
    return out


def restore(point_id: str, dry_run: bool = True) -> dict:
    """Copy each file in the named restore point back over the live state.

    Always creates a "pre-restore" restore point first so a restore is itself
    reversible. Default `dry_run=True` lists what WOULD happen.
    """
    point_dir = ROOT / point_id
    if not point_dir.is_dir():
        return {"ok": False, "error": f"unknown point_id: {point_id}"}

    m_path = point_dir / "meta.json"
    if not m_path.exists():
        return {"ok": False, "error": "meta.json missing"}
    meta = json.loads(m_path.read_text(encoding="utf-8"))

    plan = []
    for cap in meta.get("captured", []):
        if "src" not in cap or "dest" not in cap:
            continue
        live = Path(cap["src"])
        snap = Path(cap["dest"])
        if not snap.exists():
            plan.append({"src": str(snap), "dst": str(live), "action": "SKIP (snapshot missing)"})
            continue
        plan.append({"src": str(snap), "dst": str(live),
                     "action": "OVERWRITE" if live.exists() else "CREATE",
                     "size": snap.stat().st_size})

    if dry_run:
        return {"ok": True, "dry_run": True, "point_id": point_id, "plan": plan}

    # Take a safety snapshot before mutating
    safety = create("pre_restore", reason=f"automatic safety snapshot before restoring {point_id}")
    applied = 0
    for cap in meta.get("captured", []):
        try:
            live = Path(cap["src"]); snap = Path(cap["dest"])
            if snap.exists():
                live.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(snap, live)
                applied += 1
        except Exception as e:
            _journal_append({"event": "restore_partial_fail", "point_id": point_id,
                             "file": cap.get("src"), "error": str(e)[:200]})
    _journal_append({"event": "restored", "point_id": point_id, "applied": applied,
                     "safety_point_id": safety["point_id"]})
    return {"ok": True, "dry_run": False, "point_id": point_id,
            "applied": applied, "safety_point_id": safety["point_id"]}


def prune(keep_n: int = 30) -> int:
    """Keep the most recent N restore points; delete older ones. Returns
    count of points removed."""
    pts = list_points()
    if len(pts) <= keep_n:
        return 0
    removed = 0
    for p in pts[keep_n:]:
        try:
            shutil.rmtree(ROOT / p["point_id"])
            removed += 1
            _journal_append({"event": "pruned", "point_id": p["point_id"]})
        except Exception:
            pass
    return removed
