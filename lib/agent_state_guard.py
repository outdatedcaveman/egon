"""Guardrails for agent-owned state that can cost Bruno tokens if damaged."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Iterable

from lib import restore_points

HOME = Path.home()
CLAUDE_PROJECTS = HOME / ".claude" / "projects"
CLAUDE_APPDATA = HOME / "AppData" / "Roaming" / "Claude"


def create_agent_restore_point(label: str,
                               reason: str,
                               extra_files: Iterable[Path] = ()) -> dict[str, Any]:
    """Create a restore point for agent state and fail if requested files were not captured."""
    requested = [Path(p) for p in extra_files if Path(p).exists()]
    meta = restore_points.create(label, reason=reason, extra_files=requested)
    captured = {
        str(Path(c["src"]).resolve()).lower()
        for c in meta.get("captured", [])
        if "src" in c and "error" not in c
    }
    missing = [
        str(p)
        for p in requested
        if str(p.resolve()).lower() not in captured
    ]
    meta["requested_extra_files"] = len(requested)
    meta["missing_requested_files"] = missing
    meta["ok"] = not missing
    return meta


def claude_session_state_health() -> dict[str, Any]:
    """Check for the exact class of failure that made Claude sessions disappear."""
    live_jsonl = list(CLAUDE_PROJECTS.glob("*/*.jsonl")) if CLAUDE_PROJECTS.exists() else []
    archived = list(CLAUDE_PROJECTS.glob("*/*.jsonl.archived")) if CLAUDE_PROJECTS.exists() else []
    archived_only = []
    for path in archived:
        live = Path(str(path)[:-len(".archived")])
        if not live.exists():
            archived_only.append(str(path))

    unavailable = []
    metadata_root = CLAUDE_APPDATA / "claude-code-sessions"
    if metadata_root.exists():
        for path in metadata_root.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix.lower() not in {"", ".json"}:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            if "transcriptUnavailable" not in text:
                continue
            try:
                body = json.loads(text)
            except Exception:
                continue
            if _has_transcript_unavailable(body):
                unavailable.append(str(path))

    ok = not archived_only and not unavailable
    return {
        "status": "ok" if ok else "error",
        "live_jsonl_count": len(live_jsonl),
        "archived_count": len(archived),
        "archived_only_count": len(archived_only),
        "transcript_unavailable_count": len(unavailable),
        "archived_only_examples": archived_only[:10],
        "transcript_unavailable_examples": unavailable[:10],
    }


def repair_claude_archived_only_transcripts() -> dict[str, Any]:
    """Restore missing live Claude JSONLs from their archived copies.

    This is intentionally copy-only: the .jsonl.archived files remain intact,
    and existing live transcripts are never overwritten.
    """
    archived = list(CLAUDE_PROJECTS.glob("*/*.jsonl.archived")) if CLAUDE_PROJECTS.exists() else []
    archived_only = []
    for path in archived:
        live = Path(str(path)[:-len(".archived")])
        if not live.exists():
            archived_only.append(path)
    if not archived_only:
        return {"status": "ok", "restored": 0, "restore_point": None}

    meta = create_agent_restore_point(
        "claude_archived_only_self_heal",
        reason="Restore missing live Claude transcripts from archived copies before enforcement check.",
        extra_files=archived_only,
    )
    if not meta.get("ok"):
        return {
            "status": "error",
            "error": "restore_point_incomplete",
            "missing_requested_files": meta.get("missing_requested_files") or [],
            "restore_point": meta.get("point_id"),
        }

    restored = []
    errors = []
    for archived_path in archived_only:
        live_path = Path(str(archived_path)[:-len(".archived")])
        try:
            if not live_path.exists():
                shutil.copy2(archived_path, live_path)
                restored.append(str(live_path))
        except Exception as e:
            errors.append({
                "path": str(live_path),
                "error": f"{type(e).__name__}: {str(e)[:160]}",
            })

    return {
        "status": "ok" if not errors else "error",
        "restored": len(restored),
        "restored_paths": restored[:20],
        "errors": errors,
        "restore_point": meta.get("point_id"),
    }


def _has_transcript_unavailable(value: Any) -> bool:
    if isinstance(value, dict):
        if value.get("transcriptUnavailable") is True:
            return True
        return any(_has_transcript_unavailable(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_transcript_unavailable(v) for v in value)
    return False
