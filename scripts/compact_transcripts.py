"""Transcript Compactor — compresses completed transcript JSONLs into DB summaries and archives files.

Avoids duplicate processing and frees up token ledger parsing overhead.
Uses local analysis (0 LLM tokens).
"""
from __future__ import annotations

import glob
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from lib.agent_state_guard import create_agent_restore_point  # noqa: E402

_DB_PATH = ROOT / "state" / "mind.db"
PROJECTS_GLOB = os.path.expanduser("~/.claude/projects/*/*.jsonl")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _iso_to_epoch(s: str) -> int | None:
    if not s:
        return None
    try:
        from datetime import datetime
        for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
                    "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                    "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
            try:
                return int(datetime.strptime(s, fmt).timestamp())
            except Exception:
                continue
    except Exception:
        pass
    return None


def get_user_content(e: dict) -> str:
    msg = e.get("message")
    if isinstance(msg, dict):
        content = msg.get("content")
        if isinstance(content, str):
            return content
        elif isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(item.get("text") or "")
            return " ".join(parts)
    content = e.get("content")
    if isinstance(content, str):
        return content
    return ""


def analyze_transcript(jsonl_path: Path) -> dict:
    user_prompts = []
    tool_counts = {}
    commands_run = []
    files_edited = []
    
    total_turns = 0
    total_input = 0
    total_output = 0
    total_cw = 0
    total_cr = 0
    
    try:
        with jsonl_path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                    
                kind = e.get("type") or e.get("role")
                
                # 1) User requests
                if kind == "user":
                    content = get_user_content(e)
                    if content:
                        user_prompts.append(content)
                        
                # 2) Assistant turns (tokens & tools)
                elif kind == "assistant":
                    total_turns += 1
                    msg = e.get("message") or {}
                    usage = msg.get("usage") or {}
                    if usage:
                        total_input += usage.get("input_tokens", 0) or 0
                        total_output += usage.get("output_tokens", 0) or 0
                        total_cw += usage.get("cache_creation_input_tokens", 0) or 0
                        total_cr += usage.get("cache_read_input_tokens", 0) or 0
                        
                    content = msg.get("content")
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "tool_use":
                                tool_name = block.get("name")
                                if tool_name:
                                    tool_counts[tool_name] = tool_counts.get(tool_name, 0) + 1
                                    # Inspect arguments
                                    inp = block.get("input") or {}
                                    if tool_name in ("Write", "Edit", "replace_file_content", "write_to_file", "multi_replace_file_content"):
                                        path = inp.get("path") or inp.get("TargetFile") or inp.get("path_to_write")
                                        if path:
                                            fname = Path(path).name
                                            if fname not in files_edited:
                                                files_edited.append(fname)
                                    elif tool_name in ("Bash", "PowerShell", "run_command"):
                                        cmd = inp.get("command") or inp.get("CommandLine")
                                        if cmd:
                                            cmd_str = str(cmd).strip().split("\n")[0][:80]
                                            if cmd_str not in commands_run:
                                                commands_run.append(cmd_str)
    except Exception as e:
        print(f"[FAIL] Error reading {jsonl_path.name}: {e}")
        
    return {
        "user_prompts": user_prompts,
        "tool_counts": tool_counts,
        "commands_run": commands_run[:10],
        "files_edited": files_edited[:10],
        "total_turns": total_turns,
        "total_input": total_input,
        "total_output": total_output,
        "total_cw": total_cw,
        "total_cr": total_cr,
    }


def generate_summary_string(analysis: dict) -> str:
    user_prompts = analysis["user_prompts"]
    tool_counts = analysis["tool_counts"]
    files_edited = analysis["files_edited"]
    commands_run = analysis["commands_run"]
    
    total_turns = analysis["total_turns"]
    total_input = analysis["total_input"]
    total_output = analysis["total_output"]
    total_cw = analysis["total_cw"]
    total_cr = analysis["total_cr"]
    
    summary_lines = []
    
    # Goal
    goal = ""
    if user_prompts:
        goal = user_prompts[0].strip()
        if len(goal) > 200:
            goal = goal[:197] + "..."
    if goal:
        summary_lines.append(f"Goal: {goal}")
    else:
        summary_lines.append("Goal: Unspecified request")
        
    # Stats
    hit_pct = round(100 * total_cr / (total_cr + total_input + total_cw)) if (total_cr + total_input + total_cw) > 0 else 0
    summary_lines.append(f"Stats: {total_turns} turns | Input: {total_input:,} | Output: {total_output:,} | Cache Hit: {hit_pct}%")
    
    # Tools
    if tool_counts:
        tools_str = ", ".join(f"{k} (x{v})" for k, v in sorted(tool_counts.items(), key=lambda x: x[1], reverse=True))
        summary_lines.append(f"Tools: {tools_str}")
        
    # Actions
    actions = []
    for f in files_edited:
        actions.append(f"Edited file: {f}")
    for c in commands_run:
        actions.append(f"Ran command: {c}")
        
    if actions:
        summary_lines.append("Actions:")
        for action in actions[:8]:
            summary_lines.append(f" - {action}")
            
    return "\n".join(summary_lines)


def populate_turns_if_needed(conn: sqlite3.Connection, sid: int, jsonl_path: Path) -> int:
    """Ensure all assistant turns from the JSONL are populated in turns_ledger."""
    inserted = 0
    turns = []
    
    try:
        with jsonl_path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if e.get("type") != "assistant" and e.get("role") != "assistant":
                    continue
                
                msg = e.get("message") or {}
                usage = msg.get("usage") or {}
                model = msg.get("model")
                if usage and model:
                    ts_iso = e.get("timestamp")
                    ts = _iso_to_epoch(ts_iso) if ts_iso else int(time.time())
                    
                    content = msg.get("content")
                    tools = []
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "tool_use":
                                n = block.get("name")
                                if n:
                                    tools.append(n)
                    
                    turns.append({
                        "ts": ts,
                        "model": model,
                        "input_tokens": usage.get("input_tokens", 0) or 0,
                        "output_tokens": usage.get("output_tokens", 0) or 0,
                        "cache_write_tokens": usage.get("cache_creation_input_tokens", 0) or 0,
                        "cache_read_tokens": usage.get("cache_read_input_tokens", 0) or 0,
                        "tools": ",".join(tools)
                    })
    except Exception as e:
        print(f"[FAIL] Error reading turns from {jsonl_path.name}: {e}")
        return 0

    cursor = conn.cursor()
    for t in turns:
        row = cursor.execute(
            """SELECT id FROM turns_ledger 
               WHERE session_id = ? AND ts = ? AND input_tokens = ? AND output_tokens = ?""",
            (sid, t["ts"], t["input_tokens"], t["output_tokens"])).fetchone()
        if not row:
            cursor.execute(
                """INSERT INTO turns_ledger 
                   (session_id, ts, model, input_tokens, output_tokens, cache_write_tokens, cache_read_tokens, tools)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (sid, t["ts"], t["model"], t["input_tokens"], t["output_tokens"],
                 t["cache_write_tokens"], t["cache_read_tokens"], t["tools"])
            )
            inserted += 1
    return inserted


def main() -> int:
    if not _DB_PATH.exists():
        print(f"[FAIL] Database file does not exist at {_DB_PATH}")
        return 1
        
    # Find ended sessions in DB that lack summaries
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT s.id, s.external_id, p.slug 
               FROM sessions s 
               JOIN agents ag ON ag.id = s.agent_id
               LEFT JOIN projects p ON p.id = s.project_id
               WHERE s.ended_at IS NOT NULL AND (s.summary IS NULL OR s.summary = '') AND ag.name = 'claude-code'"""
        )
        sessions_to_compact = {row["external_id"]: dict(row) for row in cursor.fetchall()}
    except Exception as e:
        print(f"[FAIL] Failed to query database: {e}")
        conn.close()
        return 1
        
    if not sessions_to_compact:
        print("[INFO] No sessions without summaries found in the database.")
        conn.close()
        return 0
        
    print(f"[INFO] Found {len(sessions_to_compact)} sessions without summaries in DB.")
    
    # Scan transcript files on disk
    files = glob.glob(PROJECTS_GLOB)
    candidates: list[Path] = []
    now = time.time()
    for filepath in files:
        path = Path(filepath)
        uuid = path.stem
        if uuid not in sessions_to_compact:
            continue
        if (now - path.stat().st_mtime) < 900:
            continue
        candidates.append(path)

    if candidates:
        restore = create_agent_restore_point(
            "claude_transcript_compact",
            reason=(
                "preflight snapshot before compact_transcripts summarizes Claude "
                "sessions and copies live transcript archives"
            ),
            extra_files=[_DB_PATH, *candidates],
        )
        if not restore.get("ok"):
            print("[FAIL] Refusing to compact without a complete restore point.")
            print(json.dumps({
                "point_id": restore.get("point_id"),
                "missing_requested_files": restore.get("missing_requested_files"),
            }, ensure_ascii=False))
            conn.close()
            return 1
        print(f"[INFO] Restore point created before compaction: {restore.get('point_id')}")

    compacted_count = 0
    
    for path in candidates:
        uuid = path.stem
        db_session = sessions_to_compact[uuid]
        sid = db_session["id"]
        
        print(f"[INFO] Compacting session {uuid} (project: {db_session['slug'] or 'unknown'})...")
        
        # Populate turns in DB if not already present
        inserted_turns = populate_turns_if_needed(conn, sid, path)
        if inserted_turns > 0:
            print(f"[INFO] Backfilled {inserted_turns} turns into turns_ledger for {uuid}")
            
        analysis = analyze_transcript(path)
        summary_str = generate_summary_string(analysis)
        
        # Update database summary
        try:
            cursor.execute("UPDATE sessions SET summary = ? WHERE id = ?", (summary_str, sid))
            # Keep Claude's live transcript path intact. Claude Code's UI still
            # resolves recents via *.jsonl; renaming them makes sessions vanish.
            archived_path = path.with_suffix(".jsonl.archived")
            if not archived_path.exists():
                import shutil
                shutil.copy2(path, archived_path)
            if not path.exists():
                raise RuntimeError("live transcript disappeared during compaction")

            compacted_count += 1
            print(f"[SUCCESS] Compacted {uuid} and kept Claude transcript readable")
        except Exception as e:
            print(f"[FAIL] Failed to compact session {uuid}: {e}")
            conn.rollback()
            continue
            
    conn.commit()
    conn.close()
    
    print(f"[SUCCESS] Successfully compacted {compacted_count} sessions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
