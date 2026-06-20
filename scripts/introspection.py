"""Egon Introspection Engine — audits memory, dedups records, and summarizes activity."""
from __future__ import annotations

import json
import re
import sqlite3
import time
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "state" / "mind.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def tokenize(text: str) -> set[str]:
    """Tokenize memory content into clean lowercase words, filtering stop words."""
    words = re.findall(r"[a-z0-9_-]{3,}", text.lower())
    stop_words = {
        "the", "and", "for", "with", "that", "this", "you", "from", "have", "are",
        "was", "were", "been", "has", "had", "its", "their", "about", "your",
        "not", "but", "what", "which", "how", "will", "would", "can", "should"
    }
    return set(w for w in words if w not in stop_words)


def jaccard_similarity(set_a: set[str], set_b: set[str]) -> float:
    """Compute Jaccard similarity between two token sets."""
    union = len(set_a | set_b)
    if union == 0:
        return 0.0
    return len(set_a & set_b) / union


def prune_redundant_memories() -> dict[str, Any]:
    """Identify duplicate or subset-identical memories and mark them as superseded."""
    conn = _connect()
    try:
        # Get all active (non-superseded) memories (excluding strategies which are auto-generated summaries)
        rows = conn.execute(
            """SELECT id, kind, content, tags, updated_at 
               FROM memory 
               WHERE superseded_by_memory_id IS NULL AND kind != 'strategy'"""
        ).fetchall()
        
        memories = []
        for r in rows:
            mid = int(r["id"])
            content = r["content"] or ""
            tags = [t.strip().lower() for t in (r["tags"] or "").split(",") if t.strip()]
            memories.append({
                "id": mid,
                "kind": r["kind"],
                "content": content,
                "tags": tags,
                "updated_at": int(r["updated_at"]),
                "tokens": tokenize(content)
            })

        superseded_count = 0
        updates = []

        # Compare pairs
        for i in range(len(memories)):
            for j in range(i + 1, len(memories)):
                m_a = memories[i]
                m_b = memories[j]
                
                # Only check if they are of the same kind or closely related kinds
                if m_a["kind"] != m_b["kind"] and {m_a["kind"], m_b["kind"]} - {"fact", "note", "concept"}:
                    continue
                
                # Check Jaccard similarity
                sim = jaccard_similarity(m_a["tokens"], m_b["tokens"])
                if sim >= 0.82:
                    # Decide which one supersedes the other
                    # Usually, the one with more tokens contains more context/detail.
                    # If token length is similar, the newer one wins.
                    len_a = len(m_a["tokens"])
                    len_b = len(m_b["tokens"])
                    
                    if len_b > len_a * 1.2 or (len_b >= len_a and m_b["updated_at"] >= m_a["updated_at"]):
                        older = m_a
                        newer = m_b
                    else:
                        older = m_b
                        newer = m_a

                    # Verify containment or very high similarity
                    common_tokens = older["tokens"] & newer["tokens"]
                    containment_ratio = len(common_tokens) / max(1, len(older["tokens"]))
                    
                    if containment_ratio >= 0.85:
                        # Prepare update
                        merged_tags = sorted(list(set(older["tags"] + newer["tags"])))
                        updates.append({
                            "superseded_id": older["id"],
                            "by_id": newer["id"],
                            "merged_tags": ",".join(merged_tags),
                            "newer_id": newer["id"]
                        })

        # Apply updates atomically
        for u in updates:
            # Check if already updated by another comparison in this pass
            check = conn.execute(
                "SELECT superseded_by_memory_id FROM memory WHERE id = ?",
                (u["superseded_id"],)
            ).fetchone()
            if check and check["superseded_by_memory_id"] is not None:
                continue
                
            now = int(time.time())
            conn.execute(
                "UPDATE memory SET superseded_by_memory_id = ?, updated_at = ? WHERE id = ?",
                (u["by_id"], now, u["superseded_id"])
            )
            # Merge tags to the newer memory to preserve searchability
            conn.execute(
                "UPDATE memory SET tags = ?, updated_at = ? WHERE id = ?",
                (u["merged_tags"], now, u["newer_id"])
            )
            superseded_count += 1
            print(f"[introspection] Marked memory {u['superseded_id']} superseded by memory {u['by_id']}")

        conn.commit()
        return {"status": "ok", "pruned_memories": superseded_count}
    except Exception as e:
        print(f"[introspection] Memory pruning failed: {e}", flush=True)
        return {"status": "error", "error": str(e)}
    finally:
        conn.close()


def generate_activity_summary() -> dict[str, Any]:
    """Aggregate recent activity into a structured strategic summary and log it in memory."""
    conn = _connect()
    try:
        now = int(time.time())
        seven_days_ago = now - 7 * 86400
        
        # 1. Gather sessions
        sessions = conn.execute(
            """SELECT s.id, ag.name as agent_name, p.slug as project_slug, s.started_at, s.summary
               FROM sessions s
               JOIN agents ag ON ag.id = s.agent_id
               LEFT JOIN projects p ON p.id = s.project_id
               WHERE s.started_at >= ?""", (seven_days_ago,)
        ).fetchall()
        
        # 2. Gather activity counts
        activity = conn.execute(
            """SELECT a.kind, a.payload_json, ag.name as agent_name, p.slug as project_slug
               FROM activity a
               JOIN sessions s ON s.id = a.session_id
               JOIN agents ag ON ag.id = s.agent_id
               LEFT JOIN projects p ON p.id = s.project_id
               WHERE a.ts >= ?""", (seven_days_ago,)
        ).fetchall()

        agent_counter = Counter([s["agent_name"] for s in sessions])
        project_counter = Counter([s["project_slug"] for s in sessions if s["project_slug"]])
        
        kinds = Counter([a["kind"] for a in activity])
        tools = Counter()
        lock_conflicts = 0
        errors = 0
        
        for a in activity:
            if a["kind"].startswith("tool_"):
                tools[a["kind"].replace("tool_", "")] += 1
            elif a["kind"] == "lock_conflict":
                lock_conflicts += 1
            elif a["kind"] == "error" or "error" in a["kind"]:
                errors += 1

        # Format markdown content
        summary_lines = [
            "# Egon Shared Activity Summary (Past 7 Days)",
            f"Generated at: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now))}",
            "",
            "## Session Distribution",
        ]
        for agent, count in agent_counter.most_common():
            summary_lines.append(f"- **{agent}**: {count} session(s)")
            
        summary_lines.append("\n## Top Active Projects")
        for proj, count in project_counter.most_common(5):
            summary_lines.append(f"- **{proj}**: {count} session(s)")
            
        summary_lines.append("\n## Top Executed Tools")
        for tool, count in tools.most_common(5):
            summary_lines.append(f"- ` {tool} `: {count} execution(s)")

        summary_lines.append("\n## Concurrency & System Health")
        summary_lines.append(f"- Lock Conflicts / Contention events: **{lock_conflicts}**")
        summary_lines.append(f"- Errors encountered: **{errors}**")
        
        content = "\n".join(summary_lines)
        tags = "introspection,activity-summary,strategy,general"
        
        # Check if an activity summary already exists today to avoid clutter
        today_start = int(time.mktime(time.strptime(time.strftime("%Y-%m-%d 00:00:00", time.localtime(now)), "%Y-%m-%d %H:%M:%S")))
        existing = conn.execute(
            "SELECT id FROM memory WHERE kind = 'strategy' AND tags LIKE '%activity-summary%' AND created_at >= ?",
            (today_start,)
        ).fetchone()
        
        if existing:
            conn.execute(
                "UPDATE memory SET content = ?, updated_at = ? WHERE id = ?",
                (content, now, existing["id"])
            )
            mid = existing["id"]
            print(f"[introspection] Updated existing activity summary memory {mid}")
        else:
            cur = conn.execute(
                """INSERT INTO memory (kind, content, tags, created_at, updated_at)
                   VALUES ('strategy', ?, ?, ?, ?)""",
                (content, tags, now, now)
            )
            mid = cur.lastrowid
            print(f"[introspection] Created new activity summary memory {mid}")
            
        conn.commit()
        return {"status": "ok", "summary_memory_id": mid}
    except Exception as e:
        print(f"[introspection] Activity summary generation failed: {e}", flush=True)
        return {"status": "error", "error": str(e)}
    finally:
        conn.close()


def run_periodic_introspection() -> dict[str, Any]:
    """Execute memory pruning and activity summarization in sequence."""
    print("[introspection] Starting introspection pass...")
    prune_res = prune_redundant_memories()
    summary_res = generate_activity_summary()
    
    # Run a quick database health check / cleanup
    try:
        conn = _connect()
        conn.execute("ANALYZE")
        conn.execute("VACUUM")
        conn.close()
        print("[introspection] Database optimized (ANALYZE + VACUUM completed).")
    except Exception as e:
        print(f"[introspection] DB vacuum error: {e}", flush=True)
        
    return {
        "status": "ok",
        "pruning": prune_res,
        "summarization": summary_res
    }


if __name__ == "__main__":
    res = run_periodic_introspection()
    print(json.dumps(res, indent=2))
