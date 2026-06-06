"""Compliance audit for Egon's unified-mind enforcement contract."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "state" / "mind.db"


def _now() -> int:
    return int(time.time())


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=8)
    conn.row_factory = sqlite3.Row
    return conn


def _payload(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        body = json.loads(raw)
        return body if isinstance(body, dict) else {"value": body}
    except Exception:
        return {}


def audit_mind(project: str | None = None,
               since_hours: int = 72,
               limit_sessions: int = 80) -> dict[str, Any]:
    if not DB_PATH.exists():
        return {"status": "error", "error": "mind.db missing"}

    since = _now() - max(1, int(since_hours)) * 3600
    sessions: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []

    with _connect() as conn:
        sql = """SELECT s.*, ag.name AS agent_name, p.slug AS project_slug
                 FROM sessions s
                 JOIN agents ag ON ag.id = s.agent_id
                 LEFT JOIN projects p ON p.id = s.project_id
                 WHERE s.started_at >= ?"""
        params: list[Any] = [since]
        if project:
            sql += " AND p.slug = ?"
            params.append(project)
        sql += " ORDER BY s.started_at DESC LIMIT ?"
        params.append(int(limit_sessions))

        for s in conn.execute(sql, params).fetchall():
            sid = int(s["id"])
            acts = conn.execute(
                "SELECT kind, ts, payload_json FROM activity WHERE session_id = ?",
                (sid,),
            ).fetchall()
            memories = conn.execute(
                "SELECT id, kind, tags, updated_at FROM memory WHERE attribution_session_id = ?",
                (sid,),
            ).fetchall()
            leases = conn.execute(
                "SELECT path, lease_expires_at FROM files WHERE lease_session_id = ?",
                (sid,),
            ).fetchall()

            kinds = [a["kind"] or "" for a in acts]
            tool_kinds = [k for k in kinds if k.startswith("tool_")]
            edit_tools = [
                k for k in tool_kinds
                if any(word in k.lower() for word in ("edit", "write", "replace"))
            ]
            context_events = [a for a in acts if (a["kind"] or "") == "mind_context"]
            user_prompts = [a for a in acts if (a["kind"] or "") == "user_prompt"]
            documented = bool(memories) or bool(s["summary"])

            score = 100
            session_findings: list[str] = []
            if not context_events:
                score -= 30
                session_findings.append("missing_mind_context")
            if not acts:
                score -= 35
                session_findings.append("no_activity")
            if acts and not user_prompts and s["agent_name"] == "claude-code":
                score -= 10
                session_findings.append("no_user_prompt_activity")
            if edit_tools and not any(k in kinds for k in ("file_lease", "file_release")):
                score -= 20
                session_findings.append("edits_without_lease_evidence")
            if (len(acts) >= 5 or edit_tools) and not documented:
                score -= 25
                session_findings.append("missing_durable_memory_or_summary")
            if s["ended_at"] is None and s["started_at"] < _now() - 24 * 3600:
                score -= 10
                session_findings.append("stale_active_session")
            if leases:
                score -= 10
                session_findings.append("unreleased_file_lease")

            row = {
                "session_id": sid,
                "agent": s["agent_name"],
                "project": s["project_slug"],
                "external_id": s["external_id"],
                "started_at": s["started_at"],
                "ended_at": s["ended_at"],
                "activity_count": len(acts),
                "tool_count": len(tool_kinds),
                "memory_count": len(memories),
                "context_count": len(context_events),
                "score": max(0, score),
                "findings": session_findings,
            }
            sessions.append(row)

            for code in session_findings:
                findings.append({
                    "severity": _severity(code),
                    "code": code,
                    "session_id": sid,
                    "agent": s["agent_name"],
                    "project": s["project_slug"],
                    "message": _message(code, row),
                })

    findings.sort(key=lambda f: (_severity_rank(f["severity"]), f["agent"], f["session_id"]))
    passing = sum(1 for s in sessions if s["score"] >= 80)
    return {
        "status": "ok",
        "project": project,
        "since_hours": since_hours,
        "session_count": len(sessions),
        "passing_sessions": passing,
        "compliance_rate": round((passing / len(sessions)) * 100, 1) if sessions else 100.0,
        "findings": findings[:200],
        "sessions": sessions,
    }


def _severity(code: str) -> str:
    if code in {"no_activity", "missing_mind_context"}:
        return "high"
    if code in {"missing_durable_memory_or_summary", "edits_without_lease_evidence"}:
        return "medium"
    return "low"


def _severity_rank(severity: str) -> int:
    return {"high": 0, "medium": 1, "low": 2}.get(severity, 3)


def _message(code: str, row: dict[str, Any]) -> str:
    who = f"{row.get('agent')} session {row.get('session_id')}"
    if code == "missing_mind_context":
        return f"{who} has no recorded mind_context call."
    if code == "no_activity":
        return f"{who} has no activity rows."
    if code == "no_user_prompt_activity":
        return f"{who} has no user_prompt activity row."
    if code == "edits_without_lease_evidence":
        return f"{who} used edit/write tools without file lease evidence."
    if code == "missing_durable_memory_or_summary":
        return f"{who} did meaningful work without durable memory or session summary."
    if code == "stale_active_session":
        return f"{who} is still marked active after more than 24 hours."
    if code == "unreleased_file_lease":
        return f"{who} still holds a file lease."
    return f"{who} has audit finding {code}."


if __name__ == "__main__":
    print(json.dumps(audit_mind(), indent=2, ensure_ascii=False))
