"""Quantified health and token-ROI scorecard for Egon's meta-harness."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "state" / "mind.db"


def build_mind_scorecard(project: str | None = None,
                         since_hours: int = 168,
                         capsule_budget_chars: int = 3500) -> dict[str, Any]:
    if not DB_PATH.exists():
        return {"status": "error", "error": "mind.db missing"}

    since_hours = max(1, min(int(since_hours), 24 * 90))
    since_ts = int(time.time()) - since_hours * 3600
    project_norm = (project or "").strip() or None

    session_stats = _session_stats(project_norm, since_ts)
    context_stats = _context_stats(project_norm, since_ts)
    memory_stats = _memory_stats(project_norm, since_ts)
    token_stats = _token_ledger_stats(project_norm, since_ts)
    raw_estimate = _estimate_v1_context_tokens(project_norm)
    capsule_probe = _capsule_probe(project_norm, capsule_budget_chars)
    audit = _audit(project_norm, since_hours)

    context_coverage = _ratio(session_stats["sessions_with_context"],
                              session_stats["session_count"])
    durable_coverage = _ratio(session_stats["documented_sessions"],
                              max(1, session_stats["meaningful_sessions"]))
    lease_coverage = _ratio(session_stats["edit_sessions_with_lease"],
                            max(1, session_stats["edit_sessions"]))
    v2_adoption = _ratio(context_stats["v2_context_events"],
                         max(1, context_stats["context_events"]))

    actual_capsule_tokens = context_stats.get("avg_capsule_tokens") or capsule_probe["approx_tokens"]
    token_roi = _ratio(max(0, raw_estimate["approx_tokens"] - actual_capsule_tokens),
                       max(1, raw_estimate["approx_tokens"]))
    score = _weighted_score({
        "compliance": (audit.get("compliance_rate") or 0) / 100,
        "context_coverage": context_coverage,
        "v2_adoption": v2_adoption,
        "durable_coverage": durable_coverage,
        "lease_coverage": lease_coverage if session_stats["edit_sessions"] else 1.0,
        "token_roi": token_roi,
        "fresh_memory": _ratio(memory_stats["recent_memory_count"],
                               max(1, memory_stats["memory_count"])),
    })

    recommendations = _recommendations(
        score=score,
        audit=audit,
        context_coverage=context_coverage,
        v2_adoption=v2_adoption,
        durable_coverage=durable_coverage,
        lease_coverage=lease_coverage,
        token_roi=token_roi,
        token_stats=token_stats,
        session_stats=session_stats,
    )

    return {
        "status": "ok",
        "version": "mind-scorecard-v1",
        "project": project_norm,
        "since_hours": since_hours,
        "generated_at": int(time.time()),
        "score": score,
        "grade": _grade(score),
        "metrics": {
            "compliance_rate": audit.get("compliance_rate"),
            "context_coverage": round(context_coverage * 100, 1),
            "v2_context_adoption": round(v2_adoption * 100, 1),
            "durable_memory_coverage": round(durable_coverage * 100, 1),
            "file_lease_coverage": round(lease_coverage * 100, 1) if session_stats["edit_sessions"] else 100.0,
            "estimated_token_roi": round(token_roi * 100, 1),
            "avg_capsule_tokens": actual_capsule_tokens,
            "estimated_v1_raw_tokens": raw_estimate["approx_tokens"],
            "capsule_probe_tokens": capsule_probe["approx_tokens"],
        },
        "sessions": session_stats,
        "context": context_stats,
        "memory": memory_stats,
        "tokens": token_stats,
        "audit": {
            "session_count": audit.get("session_count"),
            "passing_sessions": audit.get("passing_sessions"),
            "finding_count": len(audit.get("findings") or []),
            "top_findings": (audit.get("findings") or [])[:8],
        },
        "evidence": {
            "capsule_probe": capsule_probe,
            "raw_context_estimate": raw_estimate,
        },
        "recommendations": recommendations,
    }


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH.as_posix(), timeout=8)
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


def _ratio(num: float, den: float) -> float:
    return 0.0 if den <= 0 else max(0.0, min(1.0, num / den))


def _project_clause(alias: str = "p") -> str:
    return f" AND {alias}.slug = ?"


def _production_session_clause(alias: str = "s") -> str:
    return f" AND COALESCE({alias}.external_id, '') NOT LIKE 'mock-%'"


def _session_stats(project: str | None, since_ts: int) -> dict[str, Any]:
    with _connect() as conn:
        sql = """SELECT s.id, s.summary, s.ended_at, ag.name AS agent_name, p.slug AS project_slug
                 FROM sessions s
                 JOIN agents ag ON ag.id = s.agent_id
                 LEFT JOIN projects p ON p.id = s.project_id
                 WHERE s.started_at >= ?"""
        sql += _production_session_clause("s")
        params: list[Any] = [since_ts]
        if project:
            sql += _project_clause()
            params.append(project)
        sessions = conn.execute(sql, params).fetchall()

        stats = {
            "session_count": len(sessions),
            "active_sessions": 0,
            "stale_active_sessions": 0,
            "sessions_with_context": 0,
            "meaningful_sessions": 0,
            "documented_sessions": 0,
            "edit_sessions": 0,
            "edit_sessions_with_lease": 0,
        }
        now = int(time.time())
        for s in sessions:
            sid = int(s["id"])
            if s["ended_at"] is None:
                stats["active_sessions"] += 1
            acts = conn.execute(
                "SELECT kind FROM activity WHERE session_id = ?",
                (sid,),
            ).fetchall()
            kinds = [a["kind"] or "" for a in acts]
            memories = conn.execute(
                "SELECT COUNT(*) c FROM memory WHERE attribution_session_id = ?",
                (sid,),
            ).fetchone()["c"]
            if "mind_context" in kinds:
                stats["sessions_with_context"] += 1
            tool_count = sum(1 for k in kinds if k.startswith("tool_"))
            edit_session = any(any(w in k.lower() for w in ("edit", "write", "replace"))
                               for k in kinds if k.startswith("tool_"))
            if len(kinds) >= 5 or tool_count > 0:
                stats["meaningful_sessions"] += 1
            if memories or s["summary"]:
                stats["documented_sessions"] += 1
            if edit_session:
                stats["edit_sessions"] += 1
                if "file_lease" in kinds and "file_release" in kinds:
                    stats["edit_sessions_with_lease"] += 1
            if s["ended_at"] is None:
                row = conn.execute(
                    "SELECT MIN(ts) first_ts FROM activity WHERE session_id = ?",
                    (sid,),
                ).fetchone()
                first_ts = row["first_ts"] if row else None
                if first_ts and first_ts < now - 24 * 3600:
                    stats["stale_active_sessions"] += 1
    return stats


def _context_stats(project: str | None, since_ts: int) -> dict[str, Any]:
    with _connect() as conn:
        sql = """SELECT a.payload_json
                 FROM activity a
                 JOIN sessions s ON s.id = a.session_id
                 LEFT JOIN projects p ON p.id = s.project_id
                 WHERE a.ts >= ? AND a.kind = 'mind_context'"""
        sql += _production_session_clause("s")
        params: list[Any] = [since_ts]
        if project:
            sql += _project_clause()
            params.append(project)
        rows = conn.execute(sql, params).fetchall()

    token_values = []
    v2 = 0
    for r in rows:
        p = _payload(r["payload_json"])
        if p.get("broker_version") == "context-broker-v2":
            v2 += 1
        try:
            if p.get("approx_tokens") is not None:
                token_values.append(int(p["approx_tokens"]))
        except Exception:
            pass
    return {
        "context_events": len(rows),
        "v2_context_events": v2,
        "avg_capsule_tokens": int(sum(token_values) / len(token_values)) if token_values else None,
        "tracked_capsule_events": len(token_values),
    }


def _memory_stats(project: str | None, since_ts: int) -> dict[str, Any]:
    with _connect() as conn:
        params: list[Any] = []
        where = "WHERE 1=1"
        if project:
            where += " AND LOWER(COALESCE(tags, '')) LIKE ?"
            params.append(f"%{project.lower()}%")
        total = conn.execute(f"SELECT COUNT(*) c FROM memory {where}", params).fetchone()["c"]
        recent = conn.execute(
            f"SELECT COUNT(*) c FROM memory {where} AND updated_at >= ?",
            params + [since_ts],
        ).fetchone()["c"]
        kinds = [dict(r) for r in conn.execute(
            f"SELECT kind, COUNT(*) c FROM memory {where} GROUP BY kind ORDER BY c DESC",
            params,
        ).fetchall()]
    return {
        "memory_count": total,
        "recent_memory_count": recent,
        "by_kind": kinds,
    }


def _token_ledger_stats(project: str | None, since_ts: int) -> dict[str, Any]:
    with _connect() as conn:
        sql = """SELECT t.input_tokens, t.output_tokens, t.cache_write_tokens,
                        t.cache_read_tokens, t.tools
                 FROM turns_ledger t
                 JOIN sessions s ON s.id = t.session_id
                 LEFT JOIN projects p ON p.id = s.project_id
                 WHERE t.ts >= ?"""
        sql += _production_session_clause("s")
        params: list[Any] = [since_ts]
        if project:
            sql += _project_clause()
            params.append(project)
        rows = conn.execute(sql, params).fetchall()

    tools = 0
    total = {"input_tokens": 0, "output_tokens": 0,
             "cache_write_tokens": 0, "cache_read_tokens": 0}
    for r in rows:
        for key in total:
            total[key] += int(r[key] or 0)
        tools += len([t for t in (r["tools"] or "").split(",") if t.strip()])
    billable = total["input_tokens"] + total["output_tokens"] + total["cache_write_tokens"]
    return {
        "turn_count": len(rows),
        "tracked_tool_uses": tools,
        "input_tokens": total["input_tokens"],
        "output_tokens": total["output_tokens"],
        "cache_write_tokens": total["cache_write_tokens"],
        "cache_read_tokens": total["cache_read_tokens"],
        "estimated_billable_tokens": billable,
        "coverage_note": "Depends on agents ingesting per-turn usage into turns_ledger.",
    }


def _estimate_v1_context_tokens(project: str | None) -> dict[str, Any]:
    with _connect() as conn:
        act_sql = """SELECT a.id, a.ts, a.kind, a.payload_json,
                            ag.name AS agent_name, p.slug AS project_slug
                     FROM activity a
                     JOIN sessions s ON s.id = a.session_id
                     JOIN agents ag ON ag.id = s.agent_id
                     LEFT JOIN projects p ON p.id = s.project_id
                     WHERE 1=1"""
        act_sql += _production_session_clause("s")
        params: list[Any] = []
        if project:
            act_sql += _project_clause()
            params.append(project)
        act_sql += " ORDER BY a.ts DESC LIMIT 30"
        activity = []
        for r in conn.execute(act_sql, params).fetchall():
            d = dict(r)
            d["payload"] = _payload(d.pop("payload_json"))
            activity.append(d)

        mem_sql = "SELECT * FROM memory WHERE 1=1"
        mem_params: list[Any] = []
        if project:
            mem_sql += " AND LOWER(COALESCE(tags, '')) LIKE ?"
            mem_params.append(f"%{project.lower()}%")
        mem_sql += " ORDER BY updated_at DESC LIMIT 20"
        memory = [dict(r) for r in conn.execute(mem_sql, mem_params).fetchall()]

        sess_sql = """SELECT s.id, s.started_at, s.external_id, ag.name AS agent_name,
                             p.slug AS project_slug
                      FROM sessions s
                      JOIN agents ag ON ag.id = s.agent_id
                      LEFT JOIN projects p ON p.id = s.project_id
                      WHERE s.ended_at IS NULL"""
        sess_params: list[Any] = []
        if project:
            sess_sql += _project_clause()
            sess_params.append(project)
        sessions = [dict(r) for r in conn.execute(sess_sql, sess_params).fetchall()]

    raw = json.dumps({
        "recent_activity": activity,
        "relevant_memory": memory,
        "active_sessions": sessions,
    }, ensure_ascii=False)
    return {
        "approx_tokens": max(1, len(raw) // 4),
        "chars": len(raw),
        "activity_rows": len(activity),
        "memory_rows": len(memory),
        "active_sessions": len(sessions),
        "method": "Approximation: JSON chars / 4 for v1-style raw context rows.",
    }


def _capsule_probe(project: str | None, budget_chars: int) -> dict[str, Any]:
    try:
        from lib.mind_context_broker import build_context_capsule

        cap = build_context_capsule(
            project=project,
            query="scorecard token roi meta-harness audit",
            budget_chars=budget_chars,
            limit_activity=5,
            limit_memory=8,
            include_graph=True,
            include_audit=True,
        )
        budget = cap.get("budget") or {}
        return {
            "status": cap.get("status"),
            "approx_tokens": budget.get("approx_tokens") or 0,
            "briefing_chars": budget.get("briefing_chars") or 0,
            "memory_refs": (cap.get("refs") or {}).get("memory_ids") or [],
            "graph_gexf_path": (cap.get("refs") or {}).get("graph_gexf_path"),
        }
    except Exception as e:
        return {
            "status": "error",
            "approx_tokens": 0,
            "briefing_chars": 0,
            "error": f"{type(e).__name__}: {str(e)[:160]}",
        }


def _audit(project: str | None, since_hours: int) -> dict[str, Any]:
    try:
        from lib.mind_audit import audit_mind

        return audit_mind(project=project, since_hours=since_hours, limit_sessions=80)
    except Exception as e:
        return {
            "status": "error",
            "compliance_rate": 0,
            "session_count": 0,
            "passing_sessions": 0,
            "findings": [{
                "severity": "high",
                "code": "audit_unavailable",
                "message": f"{type(e).__name__}: {str(e)[:160]}",
            }],
        }


def _weighted_score(parts: dict[str, float]) -> int:
    weights = {
        "compliance": 0.25,
        "context_coverage": 0.20,
        "v2_adoption": 0.15,
        "durable_coverage": 0.15,
        "lease_coverage": 0.10,
        "token_roi": 0.10,
        "fresh_memory": 0.05,
    }
    score = sum(max(0.0, min(1.0, parts.get(k, 0.0))) * w
                for k, w in weights.items())
    return int(round(score * 100))


def _grade(score: int) -> str:
    if score >= 90:
        return "excellent"
    if score >= 75:
        return "healthy"
    if score >= 60:
        return "improving"
    if score >= 40:
        return "fragile"
    return "critical"


def _recommendations(score: int, audit: dict[str, Any],
                     context_coverage: float, v2_adoption: float,
                     durable_coverage: float, lease_coverage: float,
                     token_roi: float, token_stats: dict[str, Any],
                     session_stats: dict[str, Any]) -> list[dict[str, Any]]:
    recs: list[dict[str, Any]] = []
    if context_coverage < 0.9:
        recs.append({
            "priority": "high",
            "title": "Close mind_context coverage gaps",
            "why": "Agents can still act blind when session-start context is missing.",
        })
    if v2_adoption < 0.9:
        recs.append({
            "priority": "high",
            "title": "Restart stale MCP/hook hosts until Context Broker v2 adoption is near 100%",
            "why": "v2 is the main token-saving and shared-context path.",
        })
    if durable_coverage < 0.85:
        recs.append({
            "priority": "medium",
            "title": "Require durable memory or session summary after meaningful work",
            "why": "The harness only improves with each use if outcomes become reusable evidence.",
        })
    if session_stats["edit_sessions"] and lease_coverage < 0.95:
        recs.append({
            "priority": "medium",
            "title": "Tighten edit lease enforcement",
            "why": "File leases prevent agents from unknowingly overwriting each other.",
        })
    if token_stats["turn_count"] == 0:
        recs.append({
            "priority": "medium",
            "title": "Increase per-turn token ledger coverage",
            "why": "Token ROI is estimated until agents consistently write turns_ledger rows.",
        })
    if token_roi < 0.2:
        recs.append({
            "priority": "low",
            "title": "Tune capsule budget and ranking",
            "why": "The broker should usually avoid at least 20% of raw context tokens.",
        })
    if score >= 75 and not recs:
        recs.append({
            "priority": "low",
            "title": "Start trend tracking",
            "why": "The harness is healthy enough to optimize deltas over time.",
        })
    if not recs and audit.get("findings"):
        recs.append({
            "priority": "low",
            "title": "Review remaining low-severity audit findings",
            "why": "Small cleanup keeps the scorecard trustworthy.",
        })
    return recs[:6]


if __name__ == "__main__":
    print(json.dumps(build_mind_scorecard(project="egon"), indent=2, ensure_ascii=False))
