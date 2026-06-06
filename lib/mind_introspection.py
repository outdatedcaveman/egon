"""Introspection Engine — computes strategic and operational proposals for Bruno's KMS."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from lib.ledger import compute_ledger

_ROOT = Path(__file__).resolve().parent.parent
_DB_PATH = _ROOT / "state" / "mind.db"

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn

def analyze_mind() -> list[dict[str, Any]]:
    proposals = []
    now = int(time.time())
    seven_days_ago = now - 7 * 86400

    conn = _connect()
    try:
        # Rule 1: File Lock Contention / Hotspots
        # Query lock conflicts in the last 7 days
        conflicts = conn.execute(
            """SELECT ts, payload_json FROM activity 
               WHERE kind = 'lock_conflict' AND ts >= ?
               ORDER BY ts DESC""", (seven_days_ago,)).fetchall()
        
        file_conflicts: dict[str, list[dict]] = {}
        for row in conflicts:
            try:
                payload = json.loads(row["payload_json"])
                path = payload.get("path")
                if path:
                    file_conflicts.setdefault(path, []).append({
                        "ts": row["ts"],
                        "holding_agent": payload.get("holding_agent"),
                        "holding_session": payload.get("holding_session")
                    })
            except Exception:
                continue
        
        for path, list_conflicts in file_conflicts.items():
            fname = path.split("/")[-1]
            n = len(list_conflicts)
            agents = list(set(c["holding_agent"] for c in list_conflicts))
            proposals.append({
                "id": f"lock_conflict_{hash(path)}",
                "title": f"Lock Contention on {fname}",
                "description": f"File '{path}' experienced {n} lock conflict(s) in the past week involving agents: {', '.join(agents)}. "
                               f"Consider separating tasks or optimizing concurrent runs to prevent waiting.",
                "severity": "warning",
                "category": "concurrency",
                "project": path.split("/")[-2] if "/" in path else "general",
                "ts": list_conflicts[0]["ts"]
            })

        # Rule 2: Projects Needing Session Summaries
        # Find projects that have ended sessions with no summaries
        proj_no_sums = conn.execute(
            """SELECT p.slug, p.name, COUNT(s.id) as sessions_count
               FROM projects p
               JOIN sessions s ON s.project_id = p.id
               WHERE s.ended_at IS NOT NULL AND (s.summary IS NULL OR s.summary = '')
               GROUP BY p.id"""
        ).fetchall()
        
        for row in proj_no_sums:
            slug = row["slug"]
            name = row["name"]
            count = row["sessions_count"]
            proposals.append({
                "id": f"no_summary_{slug}",
                "title": f"Consolidate {name} Context",
                "description": f"{count} sessions on project '{slug}' ended without a summary. "
                               f"Consider writing a project-level progress note to help subsequent agent runs start with better context.",
                "severity": "info",
                "category": "project_health",
                "project": slug,
                "ts": now
            })

        # Rule 3: Idle Projects with Active Work
        # Look for projects with no sessions in the past 7 days, but have active files/leases or recent memory updates
        idle_projects = conn.execute(
            """SELECT p.slug, p.name, p.updated_at 
               FROM projects p
               WHERE p.id NOT IN (
                   SELECT DISTINCT project_id FROM sessions 
                   WHERE started_at >= ? AND project_id IS NOT NULL
               )""", (seven_days_ago,)).fetchall()
        
        for row in idle_projects:
            slug = row["slug"]
            name = row["name"]
            updated = row["updated_at"]
            # Check if there are memories or activity ever
            activity_count = conn.execute(
                """SELECT COUNT(*) c FROM activity a
                   JOIN sessions s ON s.id = a.session_id
                   WHERE s.project_id = (SELECT id FROM projects WHERE slug = ?)""", (slug,)).fetchone()["c"]
            if activity_count > 0:
                proposals.append({
                    "id": f"idle_project_{slug}",
                    "title": f"Idle Project: {name}",
                    "description": f"No agent activity on '{slug}' for over 7 days (last active: {time.strftime('%Y-%m-%d', time.localtime(updated))}). "
                                   f"Review if there are pending goals or tasks to resume.",
                    "severity": "info",
                    "category": "project_health",
                    "project": slug,
                    "ts": updated
                })

    except Exception as e:
        proposals.append({
            "id": "introspection_error",
            "title": "Introspection Analysis Error",
            "description": f"Failed to perform database analysis: {str(e)}",
            "severity": "error",
            "category": "system",
            "project": "general",
            "ts": now
        })
    finally:
        conn.close()

    # Rule 4: Token Usage Anomalies and Cache Inefficiency (via Ledger)
    try:
        ledger = compute_ledger(range_key="7d")
        # Check for global anomaly
        anomaly = ledger.get("anomaly")
        if anomaly:
            proposals.append({
                "id": "token_anomaly_global",
                "title": f"Token Burn Rate: {anomaly.get('level', 'Warning').title()}",
                "description": f"{anomaly.get('headline')} {anomaly.get('driver')}. {anomaly.get('suggestion')}",
                "severity": "warning" if anomaly.get("level") == "warn" else "info",
                "category": "token_efficiency",
                "project": "general",
                "ts": now
            })
        
        # Check individual sessions for low cache hits
        for sess in ledger.get("recent_sessions") or []:
            tot_tokens = sess.get("input", 0) + sess.get("output", 0) + sess.get("cache_read", 0) + sess.get("cache_write", 0)
            hit_pct = sess.get("hit_pct", 100)
            proj = sess.get("project", "").lower()
            if tot_tokens > 150000 and hit_pct < 20:
                proposals.append({
                    "id": f"cache_inefficiency_{sess.get('time')}_{proj}",
                    "title": f"Cache Inefficient Session on {sess.get('project')}",
                    "description": f"A session on '{sess.get('project')}' consumed {tot_tokens:,} tokens with only a {hit_pct}% cache hit rate. "
                                   f"Ensure files/notes are kept modular and standard prompt cache blocks are kept stable.",
                    "severity": "info",
                    "category": "token_efficiency",
                    "project": proj,
                    "ts": now
                })
    except Exception:
        pass

    # Sort proposals by severity (error -> warning -> info) and then by timestamp (newest first)
    severity_order = {"error": 0, "warning": 1, "info": 2}
    proposals.sort(key=lambda x: (severity_order.get(x["severity"], 9), -x["ts"]))
    return proposals

def run_introspection() -> dict[str, Any]:
    """Runs the analysis and upserts results into the mind's memories as kind='strategy'."""
    proposals = analyze_mind()
    conn = _connect()
    try:
        # Delete old introspection strategies to keep it clean
        conn.execute("DELETE FROM memory WHERE kind = 'strategy' AND tags LIKE '%introspection%'")
        
        # Insert current active proposals
        now = int(time.time())
        count = 0
        for p in proposals:
            content = f"[{p['severity'].upper()}] {p['title']}: {p['description']}"
            tags = f"introspection,strategy,{p['project']},{p['category']}"
            conn.execute(
                """INSERT INTO memory (kind, content, tags, created_at, updated_at)
                   VALUES ('strategy', ?, ?, ?, ?)""",
                (content, tags, now, now)
            )
            count += 1
        return {"status": "ok", "count": count, "proposals": proposals}
    except Exception as e:
        return {"status": "error", "error": str(e)}
    finally:
        conn.close()
