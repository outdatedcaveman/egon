"""Egon AI Orchestrator Engine — handles task decomposition, SQLite task queueing, and agent task injection."""
from __future__ import annotations

import json
import sqlite3
import time
import urllib.request
from pathlib import Path

from lib.synthesis import _config, _free_ram_gb

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "state" / "mind.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _chat_decomposition(prompt: str, cfg: dict, timeout: float = 25.0) -> str | None:
    system_content = (
        "You are Egon's Orchestrator agent. Decompose a high-level request into concrete sub-tasks "
        "for these specialized agents: 'claude-code' (writes/edits code, runs tests), "
        "'antigravity' (high-level architecture, research, planning, mockups), "
        "'hermes' (background tasks, database checks, scripts).\n"
        "Output ONLY a raw JSON array of objects. Each object must have 'agent' (name string) and "
        "'task' (instruction string). No markdown, no triple backticks, no conversational preamble."
    )
    body = json.dumps({
        "model": cfg["model"],
        "max_tokens": 300,
        "temperature": 0.1,
        "messages": [
            {"role": "system", "content": system_content},
            {"role": "user", "content": f"Decompose this request: {prompt}"},
        ],
    }).encode()
    req = urllib.request.Request(
        cfg["endpoint"] + "/chat/completions", data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {cfg['api_key']}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
        return (data.get("choices") or [{}])[0].get("message", {}).get("content")
    except Exception:
        return None


def decompose_prompt(prompt: str) -> list[dict]:
    """Decompose high-level prompt into agent-specific sub-tasks using local Qwen.
    Falls back to a dual-agent heuristic if local LLM is offline.
    """
    cfg = _config()
    raw_response = None
    
    # Try local LLM chain
    try:
        free_gb = _free_ram_gb()
        # ollama is running or can load
        chain = [(cfg["model"], 3.0), ("qwen2.5:1.5b", 1.6), ("qwen2.5:0.5b", 0.8)]
        for model, need_gb in chain:
            if free_gb is not None and free_gb < need_gb:
                continue
            raw_response = _chat_decomposition(prompt, dict(cfg, model=model))
            if raw_response:
                break
    except Exception:
        pass

    if raw_response:
        try:
            # Clean up potential markdown formatting block wrapper
            clean = raw_response.strip()
            if clean.startswith("```json"):
                clean = clean.split("```json", 1)[1]
            if clean.startswith("```"):
                clean = clean.split("```", 1)[1]
            if "```" in clean:
                clean = clean.rsplit("```", 1)[0]
            clean = clean.strip()
            
            tasks = json.loads(clean)
            if isinstance(tasks, list):
                # Validate schema
                valid_tasks = []
                for t in tasks:
                    if isinstance(t, dict) and "agent" in t and "task" in t:
                        agent = str(t["agent"]).strip().lower()
                        if agent in ("claude-code", "antigravity", "hermes", "codex"):
                            valid_tasks.append({
                                "agent": agent,
                                "task": str(t["task"]).strip()
                            })
                if valid_tasks:
                    return valid_tasks
        except Exception:
            pass

    # Heuristic fallback if LLM offline or returned invalid format
    print("[orchestrator] LLM offline or invalid response. Using dual-agent fallback.")
    fallback = []
    prompt_lower = prompt.lower()
    
    # Simple rule-based classification
    if any(k in prompt_lower for k in ("code", "write", "edit", "test", "fix", "implement", "refactor")):
        fallback.append({"agent": "claude-code", "task": f"Implement the requested changes/features: {prompt}"})
        fallback.append({"agent": "antigravity", "task": f"Define the implementation plan and verify output: {prompt}"})
    elif any(k in prompt_lower for k in ("run", "check", "audit", "clean", "db", "ingest", "snapshot")):
        fallback.append({"agent": "hermes", "task": f"Run background audit / cleanup script: {prompt}"})
        fallback.append({"agent": "antigravity", "task": f"Verify system status and inspect logs: {prompt}"})
    else:
        # Default dual planning & execution assignment
        fallback.append({"agent": "antigravity", "task": f"Research design and create implementation plan: {prompt}"})
        fallback.append({"agent": "claude-code", "task": f"Write codebase changes and execute tests: {prompt}"})
        
    return fallback


def dispatch_prompt(parent_prompt: str) -> list[dict]:
    """Decompose prompt and insert tasks into SQLite db as pending."""
    tasks = decompose_prompt(parent_prompt)
    conn = _connect()
    try:
        now = int(time.time())
        for t in tasks:
            conn.execute(
                """INSERT INTO orchestrator_tasks (parent_prompt, agent_name, sub_task_desc, status, created_at, updated_at)
                   VALUES (?, ?, ?, 'pending', ?, ?)""",
                (parent_prompt, t["agent"], t["task"], now, now)
            )
        conn.commit()
    except Exception as e:
        print(f"[orchestrator] Dispatch failed: {e}", flush=True)
    finally:
        conn.close()
    return tasks


def get_pending_task(agent_name: str) -> dict | None:
    """Retrieve the next pending task for an agent and mark it as 'assigned'."""
    conn = _connect()
    try:
        row = conn.execute(
            """SELECT id, parent_prompt, sub_task_desc 
               FROM orchestrator_tasks 
               WHERE agent_name = ? AND status = 'pending' 
               ORDER BY id ASC LIMIT 1""",
            (agent_name,)
        ).fetchone()
        
        if row:
            tid = row["id"]
            now = int(time.time())
            conn.execute(
                "UPDATE orchestrator_tasks SET status = 'assigned', updated_at = ? WHERE id = ?",
                (now, tid)
            )
            conn.commit()
            return {
                "id": tid,
                "parent_prompt": row["parent_prompt"],
                "sub_task_desc": row["sub_task_desc"]
            }
    except Exception as e:
        print(f"[orchestrator] Get pending task failed: {e}", flush=True)
    finally:
        conn.close()
    return None


def get_tasks_status() -> list[dict]:
    """Retrieve all tasks grouped by status or parent prompt."""
    conn = _connect()
    try:
        rows = conn.execute(
            """SELECT id, parent_prompt, agent_name, sub_task_desc, status, created_at, updated_at 
               FROM orchestrator_tasks 
               ORDER BY id DESC LIMIT 50"""
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        conn.close()


def update_task_status(task_id: int, status: str) -> bool:
    """Update task status (completed, failed, etc.)."""
    conn = _connect()
    try:
        now = int(time.time())
        conn.execute(
            "UPDATE orchestrator_tasks SET status = ?, updated_at = ? WHERE id = ?",
            (status, now, task_id)
        )
        conn.commit()
        return True
    except Exception:
        return False
    finally:
        conn.close()
