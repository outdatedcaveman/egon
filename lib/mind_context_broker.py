"""Compact context broker for Egon's unified mind.

v1 returned raw recent activity and memory rows. This module builds a bounded
briefing capsule: enough shared context for an agent to act coherently without
spending scarce prompt budget on database noise.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
import os
from pathlib import Path
from typing import Any

from lib import semantic_index as si

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "state" / "mind.db"

TOKEN_RE = re.compile(r"[A-Za-z0-9_./:-]{3,}")
DEFAULT_BUDGET_CHARS = 6000


def _vault_matches(query: str | None, limit: int = 6) -> list[dict[str, Any]]:
    """Top reranked hits from Bruno's WHOLE vault (Zotero, bookmarks, Letterboxd,
    documents, …) for the agent's query — via the model2vec + cross-encoder
    engine. This is what lets every AI session draw on the entire knowledge base,
    not just the mind DB. Semantic-only (fast) + graceful empty on any failure."""
    if not (query or "").strip():
        return []
    try:
        from lib.connection_engine import connect
        res = connect(str(query), limit=limit, semantic_search=True,
                      lexical_search=False)
        out = []
        for c in (res.get("connections") or [])[:limit]:
            out.append({
                "source": c.get("source"),
                "title": (c.get("title") or "")[:140],
                "snippet": (c.get("snippet") or "")[:200],
                "url": c.get("url"),
            })
        return out
    except Exception:
        return []


def build_context_capsule(project: str | None = None,
                          query: str | None = None,
                          budget_chars: int = DEFAULT_BUDGET_CHARS,
                          limit_memory: int = 8,
                          limit_activity: int = 8,
                          include_graph: bool = True,
                          include_audit: bool = True,
                          agent: str | None = None) -> dict[str, Any]:
    """Return a compact, directly injectable shared-mind capsule."""
    if not DB_PATH.exists():
        return {"status": "error", "error": "mind.db missing"}

    budget = _bounded_int(budget_chars, 1800, 20000)
    query_tokens = _tokens(query)
    project_norm = (project or "").strip().lower()

    # Session-start context must be reliably fast. Semantic ranking is used
    # only after the embedding model is already warm; lexical ranking remains
    # the bounded cold-start path.
    use_semantic = (
        not bool(agent)
        and query
        and (si.model_loaded() or os.environ.get("EGON_CONTEXT_ALLOW_COLD_SEMANTIC") == "1")
    )
    memories = _ranked_memory(
        project_norm,
        query_tokens,
        limit=max(1, limit_memory),
        query=query,
        use_semantic=use_semantic,
    )
    # Whole-vault retrieval (model2vec + reranker) — only when the engine is warm
    # so session-start stays fast. Gives agents Bruno's entire knowledge base.
    vault = _vault_matches(query, limit=6) if use_semantic else []
    activity = _recent_activity(project_norm, limit=max(1, limit_activity))
    sessions = _active_sessions(project_norm)
    audit = _audit(project_norm, enabled=include_audit)
    graph = _graph(project_norm, query, enabled=include_graph)

    sections = {
        "what_matters_now": _what_matters_now(memories, activity, audit, graph),
        "vault_matches": vault,
        "durable_memory": memories,
        "recent_activity": activity,
        "active_sessions": sessions,
        "structural_insights": graph.get("insights", []),
        "audit_warnings": audit.get("warnings", []),
        "token_discipline": [
            "Use the capsule first; fetch raw rows only when an ID below is directly relevant.",
            "Prefer concrete file/action evidence over re-reading broad history.",
            "Write a durable memory when a decision, invariant, failure mode, or reusable pattern is learned.",
        ],
    }

    if agent:
        sections["orchestrator_control_contract"] = [
            "If a delegated_task is present, treat it as the active task from Bruno's orchestrator.",
            "Before starting, append a task event with event_type='started'.",
            "While working, append progress/output/decision events so the Orchestrator UI can show written working responses in near real time.",
            "Poll the task control endpoint/tool before long steps; honor pause, stop/cancel, clarify, edit, and requeue controls.",
            "When finished, update the task status and write durable shared memory summarizing what changed, verification, and remaining risk.",
        ]
        try:
            from lib.orchestration_engine import get_pending_task
            delegated = get_pending_task(agent)
            if delegated:
                sections["delegated_task"] = delegated
        except Exception as e:
            print(f"[mind_context_broker] failed to check delegated tasks: {e}", flush=True)

    briefing = _render_briefing(project, query, sections, graph)
    briefing = _fit_budget(briefing, budget)

    refs = {
        "memory_ids": [m["id"] for m in memories],
        "vault_match_urls": [v["url"] for v in vault if v.get("url")],
        "activity_ids": [a["id"] for a in activity],
        "active_session_ids": [s["id"] for s in sessions],
        "audit_codes": sorted({w["code"] for w in audit.get("warnings", [])}),
        "graph_gexf_path": graph.get("gephi_gexf_path"),
    }
    return {
        "status": "ok",
        "version": "context-broker-v2",
        "project": project,
        "query": query,
        "generated_at": int(time.time()),
        "budget": {
            "requested_chars": budget,
            "briefing_chars": len(briefing),
            "approx_tokens": max(1, len(briefing) // 4),
        },
        "briefing": briefing,
        "sections": sections,
        "refs": refs,
    }


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=8)
    conn.row_factory = sqlite3.Row
    return conn


def _bounded_int(value: Any, low: int, high: int) -> int:
    try:
        n = int(value)
    except Exception:
        n = DEFAULT_BUDGET_CHARS
    return max(low, min(high, n))


def _tokens(text: str | None) -> set[str]:
    if not text:
        return set()
    return {t.lower() for t in TOKEN_RE.findall(text) if len(t) >= 3}


def _safe_json(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        body = json.loads(raw)
        return body if isinstance(body, dict) else {"value": body}
    except Exception:
        return {}


def _clip(text: Any, limit: int = 360) -> str:
    s = " ".join(str(text or "").split())
    if len(s) <= limit:
        return s
    return s[: max(0, limit - 1)].rstrip() + "..."


def _ranked_memory(
    project: str,
    tokens: set[str],
    limit: int,
    query: str | None = None,
    use_semantic: bool = True,
) -> list[dict[str, Any]]:
    rows: list[sqlite3.Row]
    
    # Semantic search matches
    semantic_ids = []
    semantic_scores = {}
    if query and use_semantic:
        try:
            if si.is_ready():
                hits = si.search(query, top_k=limit * 2)
                for h in hits:
                    if h.get("source") == "mind-memory" and h.get("uid", "").startswith("mem:"):
                        try:
                            mid = int(h["uid"].split(":")[1])
                            semantic_ids.append(mid)
                            semantic_scores[mid] = h.get("score", 0.0)
                        except Exception:
                            continue
        except Exception as e:
            print(f"[mind_context_broker] semantic search fail: {e}", flush=True)

    with _connect() as conn:
        clauses = ["superseded_by_memory_id IS NULL"]
        params: list[Any] = []
        match_clauses = []
        if project:
            match_clauses.append("LOWER(COALESCE(tags, '')) LIKE ?")
            params.append(f"%{project}%")
        if tokens:
            token_clauses = []
            for token in list(tokens)[:8]:
                token_clauses.append("(LOWER(content) LIKE ? OR LOWER(COALESCE(tags, '')) LIKE ?)")
                params.extend([f"%{token}%", f"%{token}%"])
            match_clauses.append("(" + " OR ".join(token_clauses) + ")")
        if semantic_ids:
            placeholders = ",".join("?" for _ in semantic_ids)
            match_clauses.append(f"id IN ({placeholders})")
            params.extend(semantic_ids)
        
        sql = "SELECT * FROM memory"
        if match_clauses:
            clauses.append("(" + " OR ".join(match_clauses) + ")")
        sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY updated_at DESC LIMIT 120"
        rows = conn.execute(sql, params).fetchall()

        # Score primary records
        now = int(time.time())
        primary_map = {}
        related_ids_to_fetch = set()
        parent_scores = {}
        
        for r in rows:
            rid = int(r["id"])
            haystack = f"{r['kind']} {r['content']} {r['tags'] or ''}".lower()
            score = 0
            if project and project in (r["tags"] or "").lower():
                score += 8
            score += sum(2 for t in tokens if t in haystack)
            if rid in semantic_scores:
                score += int(semantic_scores[rid] * 12)
            score += {"decision": 5, "preference": 4, "pattern": 4, "skill": 3, "fact": 2}.get(r["kind"], 1)
            age_days = max(0, (now - int(r["updated_at"] or 0)) // 86400)
            score += max(0, 6 - age_days)
            
            primary_map[rid] = (score, int(r["updated_at"] or 0), r)

        # Identify incoming relations (where an external memory points to one of our primary matches)
        incoming_rows = []
        if primary_map:
            clauses_incoming = []
            params_incoming = []
            for pid in primary_map.keys():
                clauses_incoming.append("related_memory_ids = ?")
                clauses_incoming.append("related_memory_ids LIKE ?")
                clauses_incoming.append("related_memory_ids LIKE ?")
                clauses_incoming.append("related_memory_ids LIKE ?")
                params_incoming.extend([str(pid), f"{pid},%", f"%,{pid}", f"%,{pid},%"])
            
            # Group by OR and query
            if len(params_incoming) > 0 and len(params_incoming) <= 980:
                sql_incoming = f"SELECT * FROM memory WHERE superseded_by_memory_id IS NULL AND ({' OR '.join(clauses_incoming)})"
                incoming_rows = conn.execute(sql_incoming, params_incoming).fetchall()

        # Cache already fetched incoming rows
        incoming_cache = {int(r["id"]): r for r in incoming_rows}

        # Populating related set from incoming relations
        for rid, r in incoming_cache.items():
            if rid in primary_map:
                continue
            # Inherit max parent score
            rel_str = r["related_memory_ids"] or ""
            parent_score = 0
            for x in rel_str.split(","):
                x = x.strip()
                if x.isdigit():
                    parent_id = int(x)
                    if parent_id in primary_map:
                        parent_score = max(parent_score, primary_map[parent_id][0])
            related_ids_to_fetch.add(rid)
            parent_scores[rid] = max(parent_scores.get(rid, 0), parent_score)
            
        # Populating related set from outgoing relations (direct fields on matches)
        for rid, (_, _, r) in primary_map.items():
            rel_str = r["related_memory_ids"] or ""
            for x in rel_str.split(","):
                x = x.strip()
                if x.isdigit():
                    rel_id = int(x)
                    if rel_id != rid and rel_id not in primary_map:
                        related_ids_to_fetch.add(rel_id)
                        parent_scores[rel_id] = max(parent_scores.get(rel_id, 0), primary_map[rid][0])

        # Fetch 1-hop related records that we don't have in cache
        related_rows = []
        ids_to_query = [rid for rid in related_ids_to_fetch if rid not in incoming_cache]
        if ids_to_query:
            placeholders = ",".join("?" for _ in ids_to_query)
            rel_sql = f"SELECT * FROM memory WHERE id IN ({placeholders}) AND superseded_by_memory_id IS NULL"
            related_rows = conn.execute(rel_sql, ids_to_query).fetchall()
            
        # Combine queried rows and cached incoming rows
        all_related_rows = list(related_rows) + [r for rid, r in incoming_cache.items() if rid in related_ids_to_fetch]
            
        related_map = {}
        for r in all_related_rows:
            rid = int(r["id"])
            haystack = f"{r['kind']} {r['content']} {r['tags'] or ''}".lower()
            own_score = 0
            if project and project in (r["tags"] or "").lower():
                own_score += 8
            own_score += sum(2 for t in tokens if t in haystack)
            if rid in semantic_scores:
                own_score += int(semantic_scores[rid] * 12)
            own_score += {"decision": 5, "preference": 4, "pattern": 4, "skill": 3, "fact": 2}.get(r["kind"], 1)
            age_days = max(0, (now - int(r["updated_at"] or 0)) // 86400)
            own_score += max(0, 6 - age_days)
            
            # Inherit 50% score from parent
            inherited = int(parent_scores.get(rid, 0) * 0.5)
            final_score = max(own_score, inherited)
            related_map[rid] = (final_score, int(r["updated_at"] or 0), r)
            
        # Combine all candidates
        all_candidates = []
        for rid, (score, updated_at, r) in primary_map.items():
            all_candidates.append((score, updated_at, r))
        for rid, (score, updated_at, r) in related_map.items():
            all_candidates.append((score, updated_at, r))
            
        # Sort and take top `limit`
        all_candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
        
        out = []
        for score, _, r in all_candidates[:limit]:
            out.append({
                "id": int(r["id"]),
                "kind": r["kind"],
                "score": score,
                "updated_at": r["updated_at"],
                "tags": [t.strip() for t in (r["tags"] or "").split(",") if t.strip()],
                "content": _clip(r["content"], 420),
            })
        return out


def _recent_activity(project: str, limit: int) -> list[dict[str, Any]]:
    with _connect() as conn:
        sql = """SELECT a.id, a.ts, a.kind, a.payload_json,
                        ag.name AS agent_name, p.slug AS project_slug
                 FROM activity a
                 JOIN sessions s ON s.id = a.session_id
                 JOIN agents ag ON ag.id = s.agent_id
                 LEFT JOIN projects p ON p.id = s.project_id"""
        params: list[Any] = []
        if project:
            sql += " WHERE p.slug = ?"
            params.append(project)
        sql += " ORDER BY a.ts DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()

    out = []
    for r in rows:
        payload = _safe_json(r["payload_json"])
        preview = payload.get("text_preview") or payload.get("query_preview") or payload.get("input_preview") or payload
        out.append({
            "id": int(r["id"]),
            "ts": int(r["ts"]),
            "kind": r["kind"],
            "agent": r["agent_name"],
            "project": r["project_slug"],
            "summary": _clip(preview, 220),
        })
    return out


def _active_sessions(project: str) -> list[dict[str, Any]]:
    with _connect() as conn:
        sql = """SELECT s.id, s.started_at, s.external_id,
                        ag.name AS agent_name, p.slug AS project_slug
                 FROM sessions s
                 JOIN agents ag ON ag.id = s.agent_id
                 LEFT JOIN projects p ON p.id = s.project_id
                 WHERE s.ended_at IS NULL"""
        params: list[Any] = []
        if project:
            sql += " AND p.slug = ?"
            params.append(project)
        sql += " ORDER BY s.started_at DESC LIMIT 12"
        rows = conn.execute(sql, params).fetchall()

    now = int(time.time())
    return [{
        "id": int(r["id"]),
        "agent": r["agent_name"],
        "project": r["project_slug"],
        "external_id": _clip(r["external_id"], 120),
        "age_minutes": max(0, (now - int(r["started_at"])) // 60),
    } for r in rows]


def _audit(project: str, enabled: bool) -> dict[str, Any]:
    if not enabled:
        return {"warnings": []}
    try:
        from lib.mind_audit import audit_mind

        result = audit_mind(project=project or None, since_hours=168, limit_sessions=30)
    except Exception as e:
        return {"warnings": [{"severity": "low", "code": "audit_unavailable",
                              "message": f"{type(e).__name__}: {str(e)[:120]}"}]}
    warnings = []
    for f in (result.get("findings") or [])[:8]:
        if f.get("severity") in {"high", "medium"}:
            warnings.append({
                "severity": f.get("severity"),
                "code": f.get("code"),
                "agent": f.get("agent"),
                "session_id": f.get("session_id"),
                "message": _clip(f.get("message"), 220),
            })
    return {
        "compliance_rate": result.get("compliance_rate"),
        "session_count": result.get("session_count"),
        "warnings": warnings,
    }


def _graph(project: str, query: str | None, enabled: bool) -> dict[str, Any]:
    if not enabled:
        return {"insights": []}
    try:
        from lib.mind_graph import build_mind_graph

        result = build_mind_graph(project=project or None,
                                  query=query,
                                  limit_activity=250)
    except Exception as e:
        return {"insights": [{"kind": "graph_unavailable",
                              "title": f"{type(e).__name__}: {str(e)[:120]}",
                              "score": 0}]}
    insights = []
    for item in (result.get("insights") or [])[:5]:
        node = item.get("node") or {}
        insights.append({
            "kind": item.get("kind"),
            "title": _clip(item.get("title"), 160),
            "score": item.get("score"),
            "node_kind": node.get("kind"),
            "node_id": node.get("id"),
            "path": node.get("path"),
        })
    return {
        "node_count": result.get("node_count"),
        "edge_count": result.get("edge_count"),
        "gephi_gexf_path": result.get("gephi_gexf_path"),
        "insights": insights,
    }


def _what_matters_now(memories: list[dict[str, Any]],
                      activity: list[dict[str, Any]],
                      audit: dict[str, Any],
                      graph: dict[str, Any]) -> list[str]:
    notes: list[str] = []
    if memories:
        notes.append(f"Start from {len(memories)} ranked durable memories before broad repo exploration.")
    if activity:
        latest = activity[0]
        notes.append(f"Latest recorded activity is {latest['agent']}:{latest['kind']} (activity {latest['id']}).")
    warnings = audit.get("warnings") or []
    if warnings:
        notes.append(f"Audit has {len(warnings)} high/medium enforcement warning(s); fix missing context/docs before expanding work.")
    if graph.get("insights"):
        top = graph["insights"][0]
        notes.append(f"Graph focus: {top.get('title')} ({top.get('kind')}).")
    if not notes:
        notes.append("No strong prior signal was found; proceed with a small evidence pass and document durable findings.")
    return notes


def _render_briefing(project: str | None,
                     query: str | None,
                     sections: dict[str, Any],
                     graph: dict[str, Any]) -> str:
    lines = [
        "EGON SHARED MIND CAPSULE v2",
        f"Project: {project or 'unspecified'}",
        f"Query: {_clip(query, 220) if query else 'none'}",
        "",
    ]
    if sections.get("delegated_task"):
        task = sections["delegated_task"]
        lines.extend([
            "==================================================",
            "!!! EGON DELEGATED TASK !!!",
            f"You have been assigned this sub-task from the Orchestrator:",
            f"  Description: {task['sub_task_desc']}",
            f"  Parent Prompt Context: {task['parent_prompt']}",
            f"Please prioritize completing this task. When done, write a memory with the outcome.",
            "==================================================",
            ""
        ])

    lines.append("What matters now:")
    lines.extend(f"- {item}" for item in sections["what_matters_now"])

    if sections["durable_memory"]:
        lines.append("")
        lines.append("Ranked durable memory:")
        for m in sections["durable_memory"]:
            lines.append(f"- memory {m['id']} [{m['kind']}, score {m['score']}]: {m['content']}")

    if sections.get("vault_matches"):
        lines.append("")
        lines.append("Relevant from your vault (full-corpus, reranked):")
        for v in sections["vault_matches"]:
            tail = f" — {v['snippet']}" if v.get("snippet") else ""
            url = f" <{v['url']}>" if v.get("url") else ""
            lines.append(f"- [{v.get('source')}] {v.get('title')}{url}{tail}")

    if sections["recent_activity"]:
        lines.append("")
        lines.append("Recent activity:")
        for a in sections["recent_activity"]:
            lines.append(f"- activity {a['id']} [{a['agent']}:{a['kind']}]: {a['summary']}")

    if sections["active_sessions"]:
        lines.append("")
        lines.append("Active sessions:")
        for s in sections["active_sessions"]:
            lines.append(f"- session {s['id']} [{s['agent']}, {s['age_minutes']}m old]: {s['external_id']}")

    if sections["structural_insights"]:
        lines.append("")
        lines.append("Structural insights:")
        for item in sections["structural_insights"]:
            extra = f" path={item['path']}" if item.get("path") else ""
            lines.append(f"- {item.get('kind')}: {item.get('title')} score={item.get('score')}{extra}")

    if sections["audit_warnings"]:
        lines.append("")
        lines.append("Audit warnings:")
        for w in sections["audit_warnings"]:
            lines.append(f"- {w['severity']} {w['code']} session={w['session_id']}: {w['message']}")

    lines.append("")
    lines.append("Token discipline:")
    lines.extend(f"- {item}" for item in sections["token_discipline"])
    if graph.get("gephi_gexf_path"):
        lines.append("")
        lines.append(f"Gephi artifact: {graph['gephi_gexf_path']}")
    return "\n".join(lines)


def _fit_budget(text: str, budget_chars: int) -> str:
    if len(text) <= budget_chars:
        return text
    suffix = "\n[Capsule clipped to requested budget. Ask /context/v2 with a larger budget_chars for more.]"
    keep = max(0, budget_chars - len(suffix))
    return text[:keep].rstrip() + suffix


if __name__ == "__main__":
    print(json.dumps(build_context_capsule(project="egon", query="context broker"),
                     indent=2, ensure_ascii=False))
