"""Egon AI Orchestrator Engine — handles task decomposition, SQLite task queueing, and agent task injection."""
from __future__ import annotations

import json
import glob
import os
import sqlite3
import time
import urllib.request
from collections import deque
from pathlib import Path

from lib.synthesis import _config, _free_ram_gb

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "state" / "mind.db"
AGENTS = ("claude-code", "codex", "antigravity", "hermes")
DEFAULT_COOLDOWN_SECONDS = 1800
_FALLBACK_AGENTS = {
    "claude-code": ("codex", "antigravity", "hermes"),
    "codex": ("claude-code", "antigravity", "hermes"),
    "antigravity": ("codex", "claude-code", "hermes"),
    "hermes": ("codex", "antigravity", "claude-code"),
}
_AGENT_LOG_GLOBS = {
    "claude-code": [str(Path.home() / ".claude" / "projects" / "*" / "*.jsonl")],
    # Codex/Antigravity transcripts often contain source patches and planning
    # text about quota handling. They report live quota failures through
    # mind_agent_failure instead of passive transcript scraping.
    "codex": [],
    "antigravity": [],
    # Hermes reports quota-shaped command failures directly from the runner.
    "hermes": [],
}
_QUOTA_MARKERS = (
    "429",
    "529",
    "rate_limit",
    "rate limit",
    "rate-limited",
    "too many requests",
    "quota exceeded",
    "quota_exceeded",
    "usage limit",
    "resource_exhausted",
    "overloaded",
    "credits exhausted",
    "billing hard limit",
)
_ERROR_MARKERS = (
    "api_error",
    '"level":"error"',
    '"type":"error"',
    "ratelimiterror",
    "error",
    "exception",
    "failed",
)
_COOLDOWN_TABLE_SQL = """CREATE TABLE IF NOT EXISTS agent_cooldowns (
    agent_name TEXT PRIMARY KEY,
    cooldown_until INTEGER NOT NULL,
    reason TEXT
)"""
_TASKS_TABLE_SQL = """CREATE TABLE IF NOT EXISTS orchestrator_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_prompt TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    sub_task_desc TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'pending', 'assigned', 'completed', 'failed',
        'paused', 'needs_clarification', 'cancelled'
    )),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
)"""
_TASK_EVENTS_TABLE_SQL = """CREATE TABLE IF NOT EXISTS orchestrator_task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER,
    agent_name TEXT,
    event_type TEXT NOT NULL,
    content TEXT,
    payload_json TEXT,
    created_at INTEGER NOT NULL
)"""
_TASK_CONTROLS_TABLE_SQL = """CREATE TABLE IF NOT EXISTS orchestrator_task_controls (
    task_id INTEGER PRIMARY KEY,
    action TEXT NOT NULL,
    note TEXT,
    replacement_desc TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    acknowledged_at INTEGER
)"""
_AGENT_STATE_TABLE_SQL = """CREATE TABLE IF NOT EXISTS orchestrator_agent_state (
    agent_name TEXT PRIMARY KEY,
    current_task_id INTEGER,
    status TEXT NOT NULL,
    detail TEXT,
    last_seen_at INTEGER NOT NULL
)"""
_TASK_SCHEMA_CHECKED = False


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _normalize_agent_name(agent_name: str) -> str:
    return str(agent_name or "").strip().lower()


def _ensure_cooldown_table(conn: sqlite3.Connection) -> None:
    conn.execute(_COOLDOWN_TABLE_SQL)


def _ensure_task_table(conn: sqlite3.Connection) -> None:
    global _TASK_SCHEMA_CHECKED
    if _TASK_SCHEMA_CHECKED:
        return
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='orchestrator_tasks'"
    ).fetchone()
    if not row:
        conn.execute(_TASKS_TABLE_SQL)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_orchestrator_tasks_agent ON orchestrator_tasks (agent_name, status)")
        _TASK_SCHEMA_CHECKED = True
        return
    sql = row["sql"] or ""
    if "needs_clarification" not in sql or "cancelled" not in sql:
        conn.execute("ALTER TABLE orchestrator_tasks RENAME TO orchestrator_tasks_old")
        conn.execute(_TASKS_TABLE_SQL)
        conn.execute(
            """INSERT INTO orchestrator_tasks
               (id, parent_prompt, agent_name, sub_task_desc, status, created_at, updated_at)
               SELECT id, parent_prompt, agent_name, sub_task_desc, status, created_at, updated_at
               FROM orchestrator_tasks_old"""
        )
        conn.execute("DROP TABLE orchestrator_tasks_old")
    else:
        old = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='orchestrator_tasks_old'"
        ).fetchone()
        if old:
            conn.execute(
                """INSERT OR IGNORE INTO orchestrator_tasks
                   (id, parent_prompt, agent_name, sub_task_desc, status, created_at, updated_at)
                   SELECT id, parent_prompt, agent_name, sub_task_desc, status, created_at, updated_at
                   FROM orchestrator_tasks_old"""
            )
            conn.execute("DROP TABLE orchestrator_tasks_old")
    max_id = conn.execute("SELECT COALESCE(MAX(id), 0) m FROM orchestrator_tasks").fetchone()["m"]
    conn.execute("DELETE FROM sqlite_sequence WHERE name = 'orchestrator_tasks'")
    conn.execute("INSERT INTO sqlite_sequence(name, seq) VALUES ('orchestrator_tasks', ?)", (max_id,))
    conn.execute("CREATE INDEX IF NOT EXISTS idx_orchestrator_tasks_agent ON orchestrator_tasks (agent_name, status)")
    _TASK_SCHEMA_CHECKED = True


def _ensure_control_tables(conn: sqlite3.Connection) -> None:
    _ensure_task_table(conn)
    conn.execute(_TASK_EVENTS_TABLE_SQL)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_orch_events_task ON orchestrator_task_events (task_id, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_orch_events_agent ON orchestrator_task_events (agent_name, id)")
    conn.execute(_TASK_CONTROLS_TABLE_SQL)
    conn.execute(_AGENT_STATE_TABLE_SQL)


def _active_cooldowns(conn: sqlite3.Connection) -> dict[str, dict]:
    _ensure_cooldown_table(conn)
    now = int(time.time())
    conn.execute("DELETE FROM agent_cooldowns WHERE cooldown_until <= ?", (now,))
    rows = conn.execute(
        "SELECT agent_name, cooldown_until, reason FROM agent_cooldowns WHERE cooldown_until > ?",
        (now,),
    ).fetchall()
    return {
        r["agent_name"]: {
            "cooldown_until": r["cooldown_until"],
            "reason": r["reason"],
        }
        for r in rows
    }


def _choose_fallback_agent(original_agent: str, conn: sqlite3.Connection) -> str | None:
    original_agent = _normalize_agent_name(original_agent)
    cooldowns = _active_cooldowns(conn)
    for candidate in _FALLBACK_AGENTS.get(original_agent, AGENTS):
        candidate = _normalize_agent_name(candidate)
        if candidate and candidate != original_agent and candidate not in cooldowns:
            return candidate
    return None


def _reroute_desc(desc: str, original_agent: str, reason: str) -> str:
    desc = str(desc or "").strip()
    if desc.startswith("[Rerouted from "):
        return desc
    short_reason = str(reason or "unavailable").strip()[:80]
    return f"[Rerouted from {original_agent}: {short_reason}] {desc}"


def _json_dump(payload: dict | None) -> str | None:
    if not payload:
        return None
    try:
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    except Exception:
        return json.dumps({"unserializable": str(payload)[:500]}, ensure_ascii=True)


def _json_load(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        body = json.loads(raw)
        return body if isinstance(body, dict) else {"value": body}
    except Exception:
        return {}


def _append_task_event(
    conn: sqlite3.Connection,
    task_id: int | None,
    agent_name: str | None,
    event_type: str,
    content: str = "",
    payload: dict | None = None,
) -> int:
    _ensure_control_tables(conn)
    now = int(time.time())
    cur = conn.execute(
        """INSERT INTO orchestrator_task_events
           (task_id, agent_name, event_type, content, payload_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            int(task_id) if task_id is not None else None,
            _normalize_agent_name(agent_name or "") or None,
            str(event_type or "note").strip() or "note",
            str(content or "")[:12000],
            _json_dump(payload),
            now,
        ),
    )
    return int(cur.lastrowid)


def append_task_event(
    task_id: int | None,
    agent_name: str | None,
    event_type: str,
    content: str = "",
    payload: dict | None = None,
) -> dict:
    conn = _connect()
    try:
        event_id = _append_task_event(conn, task_id, agent_name, event_type, content, payload)
        if agent_name:
            _record_agent_heartbeat(conn, agent_name, task_id, event_type, content)
        conn.commit()
        return {"status": "ok", "event_id": event_id}
    finally:
        conn.close()


def _record_agent_heartbeat(
    conn: sqlite3.Connection,
    agent_name: str,
    task_id: int | None = None,
    status: str = "active",
    detail: str = "",
) -> None:
    _ensure_control_tables(conn)
    agent_name = _normalize_agent_name(agent_name)
    if not agent_name:
        return
    conn.execute(
        """INSERT INTO orchestrator_agent_state
           (agent_name, current_task_id, status, detail, last_seen_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(agent_name) DO UPDATE SET
             current_task_id=excluded.current_task_id,
             status=excluded.status,
             detail=excluded.detail,
             last_seen_at=excluded.last_seen_at""",
        (
            agent_name,
            int(task_id) if task_id is not None else None,
            str(status or "active")[:80],
            str(detail or "")[:500],
            int(time.time()),
        ),
    )


def record_agent_heartbeat(
    agent_name: str,
    task_id: int | None = None,
    status: str = "active",
    detail: str = "",
) -> dict:
    conn = _connect()
    try:
        _record_agent_heartbeat(conn, agent_name, task_id, status, detail)
        conn.commit()
        return {"status": "ok"}
    finally:
        conn.close()


def _latest_task_controls(conn: sqlite3.Connection) -> dict[int, dict]:
    _ensure_control_tables(conn)
    rows = conn.execute("SELECT * FROM orchestrator_task_controls").fetchall()
    return {int(r["task_id"]): dict(r) for r in rows}


def _reroute_tasks_from_agent(
    conn: sqlite3.Connection,
    agent_name: str,
    reason: str,
    include_assigned: bool = True,
) -> int:
    agent_name = _normalize_agent_name(agent_name)
    statuses = ("pending", "assigned") if include_assigned else ("pending",)
    placeholders = ",".join("?" for _ in statuses)
    rows = conn.execute(
        f"""SELECT id, sub_task_desc
            FROM orchestrator_tasks
            WHERE agent_name = ? AND status IN ({placeholders})
            ORDER BY id ASC""",
        (agent_name, *statuses),
    ).fetchall()
    rerouted = 0
    now = int(time.time())
    for row in rows:
        fallback = _choose_fallback_agent(agent_name, conn)
        if not fallback:
            continue
        conn.execute(
            """UPDATE orchestrator_tasks
               SET agent_name = ?, sub_task_desc = ?, status = 'pending', updated_at = ?
               WHERE id = ?""",
            (
                fallback,
                _reroute_desc(row["sub_task_desc"], agent_name, reason),
                now,
                row["id"],
            ),
        )
        _append_task_event(
            conn,
            row["id"],
            fallback,
            "rerouted",
            f"Rerouted from {agent_name} to {fallback}: {reason}",
            {"from_agent": agent_name, "to_agent": fallback, "reason": str(reason or "")[:500]},
        )
        rerouted += 1
    return rerouted


def _set_agent_cooldown_until(
    conn: sqlite3.Connection,
    agent_name: str,
    cooldown_until: int,
    reason: str,
) -> None:
    agent_name = _normalize_agent_name(agent_name)
    if not agent_name:
        raise ValueError("agent_name required")
    _ensure_cooldown_table(conn)
    conn.execute(
        """INSERT OR REPLACE INTO agent_cooldowns (agent_name, cooldown_until, reason)
           VALUES (?, ?, ?)""",
        (agent_name, int(cooldown_until), str(reason or "quota exceeded").strip() or "quota exceeded"),
    )


def set_agent_cooldown(
    agent_name: str,
    cooldown_seconds: int = 1800,
    reason: str = "quota exceeded",
) -> dict:
    """Persist an agent cooldown and return the stored state."""
    seconds = max(1, int(cooldown_seconds))
    until = int(time.time()) + seconds
    conn = _connect()
    try:
        reason = str(reason or "quota exceeded").strip() or "quota exceeded"
        _set_agent_cooldown_until(conn, agent_name, until, reason)
        rerouted = _reroute_tasks_from_agent(conn, agent_name, reason, include_assigned=True)
        conn.commit()
        return {
            "agent_name": _normalize_agent_name(agent_name),
            "cooldown_until": until,
            "reason": reason,
            "rerouted_tasks": rerouted,
        }
    finally:
        conn.close()


def clear_agent_cooldown(agent_name: str) -> bool:
    """Clear any active cooldown for an agent."""
    agent_name = _normalize_agent_name(agent_name)
    if not agent_name:
        raise ValueError("agent_name required")
    conn = _connect()
    try:
        _ensure_cooldown_table(conn)
        conn.execute("DELETE FROM agent_cooldowns WHERE agent_name = ?", (agent_name,))
        conn.commit()
        return True
    finally:
        conn.close()


def _tail_lines(path: str, max_lines: int = 50) -> list[str]:
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return list(deque(f, maxlen=max_lines))


def _line_has_quota_signal(line: str) -> bool:
    low = str(line or "").lower()
    has_quota = any(marker in low for marker in _QUOTA_MARKERS)
    if not has_quota:
        return False
    if any(marker in low for marker in ("429", "529", "resource_exhausted", "too many requests")):
        return True
    return any(marker in low for marker in _ERROR_MARKERS)


def _line_has_runtime_quota_signal(line: str) -> bool:
    low = str(line or "").lower()
    stripped = str(line or "").strip()
    if stripped.startswith("{"):
        try:
            event = json.loads(stripped)
            if isinstance(event, dict):
                event_type = str(event.get("type") or "").lower()
                subtype = str(event.get("subtype") or "").lower()
                level = str(event.get("level") or "").lower()
                status = str(event.get("status") or event.get("status_code") or "")
                if (
                    event_type not in {"error", "api_error"}
                    and subtype != "api_error"
                    and level != "error"
                    and status not in {"429", "529"}
                ):
                    return False
        except Exception:
            pass
    runtime_markers = (
        '"subtype":"api_error"',
        '"level":"error"',
        '"type":"error"',
        "api_error",
        "ratelimiterror",
        "insufficient_quota",
        "resource_exhausted",
        "status_code\":429",
        "\"status\":429",
        "http 429",
    )
    return _line_has_quota_signal(line) and any(marker in low for marker in runtime_markers)


def is_quota_failure(text: str) -> bool:
    """Return True when text looks like a real API quota/rate-limit failure."""
    if not text:
        return False
    for line in str(text).splitlines():
        if _line_has_quota_signal(line):
            return True
    return False


def _recent_agent_log_files(agent_name: str, max_files: int = 5) -> list[str]:
    paths: list[str] = []
    for pattern in _AGENT_LOG_GLOBS.get(agent_name, []):
        matches = glob.glob(pattern, recursive=True)
        for match in matches:
            try:
                p = Path(match)
                if not p.is_file():
                    continue
                if agent_name == "antigravity" and p.suffix.lower() not in {".md", ".json", ".jsonl", ".txt", ""}:
                    continue
                paths.append(str(p))
            except Exception:
                continue
    paths.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return paths[:max_files]


def detect_agent_cooldowns(
    conn: sqlite3.Connection,
    agents: list[str] | tuple[str, ...] | None = None,
) -> dict[str, dict]:
    """Detect recent quota/rate-limit failures from local agent logs.

    This is deliberately conservative: it requires quota/rate-limit language
    plus an error-shaped marker, so ordinary planning text mentioning quotas
    does not cool down an agent by accident.
    """
    detected: dict[str, dict] = {}
    now = int(time.time())
    for agent in agents or AGENTS:
        agent = _normalize_agent_name(agent)
        if not agent:
            continue
        for filepath in _recent_agent_log_files(agent):
            try:
                mtime = os.path.getmtime(filepath)
                if now - mtime > 7200:
                    continue
                for line in _tail_lines(filepath, max_lines=80):
                    if not _line_has_runtime_quota_signal(line):
                        continue
                    reason = f"Quota / rate limit detected from {Path(filepath).name}"
                    cooldown_until = max(int(mtime) + DEFAULT_COOLDOWN_SECONDS, now + 600)
                    _set_agent_cooldown_until(conn, agent, cooldown_until, reason)
                    rerouted = _reroute_tasks_from_agent(conn, agent, reason, include_assigned=True)
                    conn.commit()
                    detected[agent] = {
                        "cooldown_until": cooldown_until,
                        "reason": reason,
                        "rerouted_tasks": rerouted,
                    }
                    print(
                        f"[cooldown] Auto-detected {agent} rate limit. "
                        f"Cooldown until {cooldown_until}; rerouted={rerouted}",
                        flush=True,
                    )
                    raise StopIteration
            except StopIteration:
                break
            except Exception as e:
                print(f"[cooldown] Error scanning {agent} log {filepath}: {e}", flush=True)
    return detected


def report_agent_failure(
    agent_name: str,
    detail: str,
    cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
) -> dict:
    """Record a runtime failure. Quota-shaped failures cool down and reroute."""
    agent_name = _normalize_agent_name(agent_name)
    if not agent_name:
        raise ValueError("agent_name required")
    if not is_quota_failure(detail):
        return {"status": "ignored", "agent_name": agent_name, "quota_detected": False}
    reason = "quota exceeded"
    for line in str(detail or "").splitlines():
        if _line_has_quota_signal(line):
            reason = line.strip()[:180] or reason
            break
    cooldown = set_agent_cooldown(agent_name, cooldown_seconds, reason)
    return {"status": "cooldown", "quota_detected": True, **cooldown}


def _route_agent_for_dispatch(agent_name: str, desc: str, conn: sqlite3.Connection) -> tuple[str, str]:
    agent_name = _normalize_agent_name(agent_name)
    cooldowns = _active_cooldowns(conn)
    if agent_name not in cooldowns:
        return agent_name, desc
    fallback = _choose_fallback_agent(agent_name, conn)
    if not fallback:
        return agent_name, desc
    reason = cooldowns[agent_name].get("reason") or "cooldown"
    return fallback, _reroute_desc(desc, agent_name, reason)


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
        # NEVER load a model without a safety buffer above its footprint —
        # loading qwen even the 0.5B (0.8GB) when only ~1GB is free pushes the
        # 8GB box into pagefile thrash and FREEZES the whole machine (incl. the
        # GUI) for minutes. Below the buffer we use the instant rule-based
        # decomposition instead — no model, no freeze. Bruno 2026-07-01.
        _SAFETY_GB = 1.5
        chain = [(cfg["model"], 3.0), ("qwen2.5:1.5b", 1.6), ("qwen2.5:0.5b", 0.8)]
        for model, need_gb in chain:
            if free_gb is None or free_gb < need_gb + _SAFETY_GB:
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
        _ensure_control_tables(conn)
        detect_agent_cooldowns(conn)
        now = int(time.time())
        from lib import masterlaw
        for t in tasks:
            routed_agent, routed_task = _route_agent_for_dispatch(t["agent"], t["task"], conn)
            t["agent"] = routed_agent
            t["task"] = routed_task
            # MASTERLAW screen — fail-closed. A task that looks like irreversible
            # deletion, PII egress, or an access-control change is NOT made
            # dispatchable; it lands as needs_clarification for Bruno's veto.
            verdict = masterlaw.check_dispatch(routed_task, routed_agent)
            init_status = "pending" if verdict["allowed"] else "needs_clarification"
            t["blocked"] = not verdict["allowed"]
            cur = conn.execute(
                """INSERT INTO orchestrator_tasks (parent_prompt, agent_name, sub_task_desc, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (parent_prompt, routed_agent, routed_task, init_status, now, now)
            )
            t["id"] = int(cur.lastrowid)
            _append_task_event(
                conn,
                t["id"],
                routed_agent,
                "created" if verdict["allowed"] else "masterlaw_blocked",
                routed_task if verdict["allowed"] else verdict["reason"],
                {"parent_prompt": parent_prompt, "masterlaw": verdict["code"]},
            )
        conn.commit()
    except Exception as e:
        print(f"[orchestrator] Dispatch failed: {e}", flush=True)
    finally:
        conn.close()
    return tasks


def create_task(parent_prompt: str, agent_name: str, sub_task_desc: str,
                status: str = "pending", allow_unknown_agent: bool = False) -> dict:
    """Create one orchestrator task without running decomposition."""
    agent_name = _normalize_agent_name(agent_name)
    if not allow_unknown_agent and agent_name not in AGENTS:
        raise ValueError(f"unsupported agent: {agent_name}")
    status = str(status or "pending").strip().lower()
    if status not in {"pending", "assigned", "paused", "needs_clarification"}:
        raise ValueError("unsupported initial status")
    conn = _connect()
    try:
        _ensure_control_tables(conn)
        now = int(time.time())
        routed_agent, routed_desc = _route_agent_for_dispatch(agent_name, sub_task_desc, conn)
        cur = conn.execute(
            """INSERT INTO orchestrator_tasks
               (parent_prompt, agent_name, sub_task_desc, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (parent_prompt, routed_agent, routed_desc, status, now, now),
        )
        tid = int(cur.lastrowid)
        _append_task_event(conn, tid, routed_agent, "created", routed_desc, {"parent_prompt": parent_prompt})
        conn.commit()
        return {
            "id": tid,
            "parent_prompt": parent_prompt,
            "agent": routed_agent,
            "task": routed_desc,
            "status": status,
        }
    finally:
        conn.close()


def is_agent_on_cooldown(agent_name: str, conn: sqlite3.Connection) -> bool:
    try:
        agent_name = _normalize_agent_name(agent_name)
        _ensure_cooldown_table(conn)
        now = int(time.time())
        row = conn.execute(
            "SELECT cooldown_until FROM agent_cooldowns WHERE agent_name = ?",
            (agent_name,)
        ).fetchone()
        if row and row["cooldown_until"] > now:
            return True
    except Exception:
        pass
    return False


def detect_claude_cooldown(conn: sqlite3.Connection) -> None:
    detect_agent_cooldowns(conn, agents=("claude-code",))


def get_agents_cooldowns() -> dict[str, dict]:
    conn = _connect()
    try:
        cooldowns = _active_cooldowns(conn)
        conn.commit()
        return cooldowns
    except Exception:
        return {}
    finally:
        conn.close()


def get_agent_routing_status() -> dict[str, dict]:
    conn = _connect()
    try:
        cooldowns = _active_cooldowns(conn)
        _ensure_control_tables(conn)
        state_rows = conn.execute("SELECT * FROM orchestrator_agent_state").fetchall()
        agent_state = {r["agent_name"]: dict(r) for r in state_rows}
        conn.commit()
        return {
            agent: {
                "available": agent not in cooldowns,
                "cooldown": cooldowns.get(agent),
                "fallback_order": list(_FALLBACK_AGENTS.get(agent, ())),
                "state": agent_state.get(agent),
            }
            for agent in AGENTS
        }
    except Exception:
        return {
            agent: {
                "available": True,
                "cooldown": None,
                "fallback_order": list(_FALLBACK_AGENTS.get(agent, ())),
                "state": None,
            }
            for agent in AGENTS
        }
    finally:
        conn.close()


def _get_task_control(conn: sqlite3.Connection, task_id: int) -> dict | None:
    _ensure_control_tables(conn)
    row = conn.execute(
        "SELECT * FROM orchestrator_task_controls WHERE task_id = ?",
        (int(task_id),),
    ).fetchone()
    return dict(row) if row else None


def get_task_control(task_id: int) -> dict | None:
    conn = _connect()
    try:
        return _get_task_control(conn, task_id)
    finally:
        conn.close()


def set_task_control(
    task_id: int,
    action: str,
    note: str = "",
    replacement_desc: str | None = None,
    agent_name: str | None = None,
) -> dict:
    action = str(action or "").strip().lower()
    if action not in {"pause", "resume", "stop", "cancel", "clarify", "edit", "requeue"}:
        raise ValueError("unsupported control action")
    conn = _connect()
    try:
        _ensure_control_tables(conn)
        now = int(time.time())
        row = conn.execute("SELECT * FROM orchestrator_tasks WHERE id = ?", (int(task_id),)).fetchone()
        if not row:
            return {"status": "error", "error": "task not found"}

        status_update = None
        desc_update = None
        if action == "pause":
            status_update = "paused"
        elif action in {"resume", "requeue"}:
            status_update = "pending"
        elif action in {"stop", "cancel"}:
            status_update = "cancelled"
        elif action == "edit":
            status_update = "pending"
            desc_update = str(replacement_desc or "").strip()
            if not desc_update:
                return {"status": "error", "error": "replacement_desc required for edit"}
        elif action == "clarify":
            status_update = "needs_clarification"

        if desc_update is not None:
            conn.execute(
                "UPDATE orchestrator_tasks SET sub_task_desc = ?, status = ?, updated_at = ? WHERE id = ?",
                (desc_update, status_update, now, int(task_id)),
            )
        elif status_update:
            conn.execute(
                "UPDATE orchestrator_tasks SET status = ?, updated_at = ? WHERE id = ?",
                (status_update, now, int(task_id)),
            )

        if action in {"resume", "requeue"}:
            conn.execute("DELETE FROM orchestrator_task_controls WHERE task_id = ?", (int(task_id),))
        else:
            conn.execute(
                """INSERT INTO orchestrator_task_controls
                   (task_id, action, note, replacement_desc, created_at, updated_at, acknowledged_at)
                   VALUES (?, ?, ?, ?, ?, ?, NULL)
                   ON CONFLICT(task_id) DO UPDATE SET
                     action=excluded.action,
                     note=excluded.note,
                     replacement_desc=excluded.replacement_desc,
                     updated_at=excluded.updated_at,
                     acknowledged_at=NULL""",
                (int(task_id), action, str(note or "")[:2000], replacement_desc, now, now),
            )
        event_agent = agent_name or row["agent_name"]
        _append_task_event(
            conn,
            int(task_id),
            event_agent,
            f"control_{action}",
            note or desc_update or action,
            {"action": action, "replacement_desc": replacement_desc},
        )
        conn.commit()
        return {"status": "ok", "task_id": int(task_id), "action": action, "task_status": status_update}
    finally:
        conn.close()


def acknowledge_task_control(task_id: int, agent_name: str | None = None) -> dict:
    conn = _connect()
    try:
        _ensure_control_tables(conn)
        now = int(time.time())
        conn.execute(
            "UPDATE orchestrator_task_controls SET acknowledged_at = ? WHERE task_id = ?",
            (now, int(task_id)),
        )
        _append_task_event(conn, int(task_id), agent_name, "control_acknowledged", "Control acknowledged")
        conn.commit()
        return {"status": "ok", "task_id": int(task_id)}
    finally:
        conn.close()


def get_task_events(task_id: int | None = None, since_id: int = 0, limit: int = 200) -> list[dict]:
    conn = _connect()
    try:
        _ensure_control_tables(conn)
        params: list[object] = [int(since_id)]
        where = "WHERE id > ?"
        if task_id is not None:
            where += " AND task_id = ?"
            params.append(int(task_id))
        params.append(max(1, min(500, int(limit))))
        rows = conn.execute(
            f"""SELECT * FROM orchestrator_task_events
                {where}
                ORDER BY id DESC LIMIT ?""",
            params,
        ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["payload"] = _json_load(item.pop("payload_json", None))
            out.append(item)
        out.reverse()
        return out
    finally:
        conn.close()


def get_scheduler_status(stuck_after_seconds: int = 1800) -> dict:
    """Return a compact utilization view for the always-on orchestrator loop."""
    conn = _connect()
    try:
        _ensure_control_tables(conn)
        now = int(time.time())
        cooldowns = _active_cooldowns(conn)
        counts = {}
        for r in conn.execute(
            "SELECT status, COUNT(*) c FROM orchestrator_tasks GROUP BY status"
        ).fetchall():
            counts[r["status"]] = int(r["c"])

        stuck_rows = conn.execute(
            """SELECT id, agent_name, sub_task_desc, status, updated_at
               FROM orchestrator_tasks
               WHERE status = 'assigned' AND updated_at < ?
               ORDER BY updated_at ASC LIMIT 20""",
            (now - max(60, int(stuck_after_seconds)),),
        ).fetchall()
        state_rows = conn.execute("SELECT * FROM orchestrator_agent_state").fetchall()
        agent_state = {r["agent_name"]: dict(r) for r in state_rows}
        idle_agents = [
            agent for agent in AGENTS
            if agent not in cooldowns
            and not conn.execute(
                "SELECT 1 FROM orchestrator_tasks WHERE agent_name = ? AND status IN ('pending','assigned') LIMIT 1",
                (agent,),
            ).fetchone()
        ]
        return {
            "status": "ok",
            "counts": counts,
            "cooldowns": cooldowns,
            "stuck_tasks": [dict(r) for r in stuck_rows],
            "idle_agents": idle_agents,
            "agent_state": agent_state,
            "active_work": int(counts.get("pending", 0)) + int(counts.get("assigned", 0)),
            "needs_clarification": int(counts.get("needs_clarification", 0)),
            "paused": int(counts.get("paused", 0)),
            "ts": now,
        }
    finally:
        conn.close()


def get_mission_control_status(limit_events: int = 80) -> dict:
    """Return one operator-facing view of orchestration state.

    This deliberately aggregates existing durable tables instead of inventing a
    parallel status model. It is the "what is happening right now?" surface for
    Bruno and for agents checking one another's work.
    """
    conn = _connect()
    try:
        _ensure_control_tables(conn)
        now = int(time.time())
        cooldowns = _active_cooldowns(conn)
        controls = _latest_task_controls(conn)

        task_rows = conn.execute(
            """SELECT id, parent_prompt, agent_name, sub_task_desc, status, created_at, updated_at
               FROM orchestrator_tasks
               WHERE status IN ('pending','assigned','paused','needs_clarification')
               ORDER BY updated_at DESC LIMIT 100"""
        ).fetchall()
        active_tasks = [dict(r) for r in task_rows]
        tasks_by_agent: dict[str, list[dict]] = {}
        for task in active_tasks:
            tid = int(task["id"])
            latest = conn.execute(
                """SELECT id, event_type, content, created_at, agent_name, payload_json
                   FROM orchestrator_task_events
                   WHERE task_id = ?
                   ORDER BY id DESC LIMIT 1""",
                (tid,),
            ).fetchone()
            task["latest_event"] = _event_dict(latest) if latest else None
            task["control"] = controls.get(tid)
            tasks_by_agent.setdefault(task["agent_name"], []).append(task)

        state_rows = conn.execute("SELECT * FROM orchestrator_agent_state").fetchall()
        agent_state = {r["agent_name"]: dict(r) for r in state_rows}
        agents: dict[str, dict] = {}
        for agent in AGENTS:
            state = agent_state.get(agent)
            cooldown = cooldowns.get(agent)
            current_tasks = tasks_by_agent.get(agent, [])
            current_task = current_tasks[0] if current_tasks else None
            last_seen = int((state or {}).get("last_seen_at") or 0)
            stale_seconds = now - last_seen if last_seen else None
            latest_event = None
            if current_task and current_task.get("latest_event"):
                latest_event = current_task["latest_event"]
            else:
                row = conn.execute(
                    """SELECT id, task_id, agent_name, event_type, content, payload_json, created_at
                       FROM orchestrator_task_events
                       WHERE agent_name = ?
                       ORDER BY id DESC LIMIT 1""",
                    (agent,),
                ).fetchone()
                latest_event = _event_dict(row) if row else None
            agents[agent] = {
                "available": agent not in cooldowns,
                "cooldown": cooldown,
                "state": state,
                "last_seen_seconds_ago": stale_seconds,
                "current_task": current_task,
                "active_task_count": len(current_tasks),
                "pending_controls": [
                    t["control"] for t in current_tasks
                    if t.get("control") and not t["control"].get("acknowledged_at")
                ],
                "latest_event": latest_event,
            }

        event_rows = conn.execute(
            """SELECT id, task_id, agent_name, event_type, content, payload_json, created_at
               FROM orchestrator_task_events
               ORDER BY id DESC LIMIT ?""",
            (max(1, min(300, int(limit_events))),),
        ).fetchall()
        recent_events = [_event_dict(r) for r in event_rows]
        recent_events.reverse()

        leases = []
        try:
            lease_rows = conn.execute(
                """SELECT path, lease_expires_at, lease_session_id
                   FROM files
                   WHERE lease_session_id IS NOT NULL AND lease_expires_at > ?
                   ORDER BY lease_expires_at ASC LIMIT 100""",
                (now,),
            ).fetchall()
            for r in lease_rows:
                item = dict(r)
                item["expires_in"] = max(0, int(item["lease_expires_at"] or 0) - now)
                leases.append(item)
        except Exception:
            leases = []

        counts = {}
        for r in conn.execute(
            "SELECT status, COUNT(*) c FROM orchestrator_tasks GROUP BY status"
        ).fetchall():
            counts[r["status"]] = int(r["c"])
        try:
            from lib.agent_wake_bridge import wake_status

            wake = wake_status()
        except Exception as e:
            wake = {"status": "error", "error": f"{type(e).__name__}: {str(e)[:160]}"}

        return {
            "status": "ok",
            "ts": now,
            "agents": agents,
            "tasks": {
                "counts": counts,
                "active": active_tasks,
            },
            "recent_events": recent_events,
            "leases": leases,
            "wake": wake,
            "summary": {
                "active_work": int(counts.get("pending", 0)) + int(counts.get("assigned", 0)),
                "paused": int(counts.get("paused", 0)),
                "needs_clarification": int(counts.get("needs_clarification", 0)),
                "cooldown_agents": [a for a, c in cooldowns.items() if c],
                "active_wake_runners": len(wake.get("active_runners") or []),
                "stale_agents": [
                    a for a, info in agents.items()
                    if info.get("last_seen_seconds_ago") is not None
                    and int(info["last_seen_seconds_ago"]) > 300
                ],
                "open_leases": len(leases),
            },
        }
    finally:
        conn.close()


def _event_dict(row: sqlite3.Row | None) -> dict | None:
    if not row:
        return None
    item = dict(row)
    item["payload"] = _json_load(item.pop("payload_json", None))
    return item


def rebalance_stuck_tasks(stuck_after_seconds: int = 1800) -> dict:
    """Move stale assigned tasks back to useful work queues.

    If the assigned agent is on cooldown, the task is rerouted to the next
    available fallback. Otherwise it is requeued for the same agent so the
    next context poll can pick it back up without Bruno pressing "go on".
    """
    conn = _connect()
    try:
        _ensure_control_tables(conn)
        now = int(time.time())
        cooldowns = _active_cooldowns(conn)
        cutoff = now - max(60, int(stuck_after_seconds))
        rows = conn.execute(
            """SELECT id, agent_name, sub_task_desc, updated_at
               FROM orchestrator_tasks
               WHERE status = 'assigned' AND updated_at < ?
               ORDER BY updated_at ASC LIMIT 50""",
            (cutoff,),
        ).fetchall()
        requeued = 0
        rerouted = 0
        touched: list[dict] = []
        for row in rows:
            tid = int(row["id"])
            agent = _normalize_agent_name(row["agent_name"])
            target_agent = agent
            desc = row["sub_task_desc"]
            event_type = "auto_requeued"
            payload = {"from_agent": agent, "reason": "stale_assigned"}
            if agent in cooldowns:
                fallback = _choose_fallback_agent(agent, conn)
                if not fallback:
                    continue
                target_agent = fallback
                desc = _reroute_desc(desc, agent, cooldowns[agent].get("reason") or "cooldown")
                event_type = "auto_rerouted"
                payload["to_agent"] = fallback
                payload["cooldown"] = cooldowns[agent]
                rerouted += 1
            else:
                requeued += 1
            conn.execute(
                """UPDATE orchestrator_tasks
                   SET agent_name = ?, sub_task_desc = ?, status = 'pending', updated_at = ?
                   WHERE id = ?""",
                (target_agent, desc, now, tid),
            )
            conn.execute("DELETE FROM orchestrator_task_controls WHERE task_id = ?", (tid,))
            _append_task_event(
                conn,
                tid,
                target_agent,
                event_type,
                f"Autonomy moved stale task back to pending for {target_agent}.",
                payload,
            )
            touched.append({"task_id": tid, "from_agent": agent, "to_agent": target_agent})
        conn.commit()
        return {
            "status": "ok",
            "requeued": requeued,
            "rerouted": rerouted,
            "tasks": touched,
            "ts": now,
        }
    finally:
        conn.close()


def get_pending_task(agent_name: str) -> dict | None:
    """Retrieve the next pending task for an agent and mark it as 'assigned'.
    Dynamically routes tasks if the target agent is on cooldown.
    """
    conn = _connect()
    try:
        agent_name = _normalize_agent_name(agent_name)
        _ensure_cooldown_table(conn)
        _ensure_control_tables(conn)
        detect_agent_cooldowns(conn)
        _record_agent_heartbeat(conn, agent_name, None, "polling", "checking for delegated work")

        # Check if requesting agent itself is on cooldown
        if is_agent_on_cooldown(agent_name, conn):
            conn.commit()
            return None

        # Check for tasks directly assigned to caller agent
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
            _append_task_event(conn, tid, agent_name, "assigned", row["sub_task_desc"])
            _record_agent_heartbeat(conn, agent_name, tid, "assigned", row["sub_task_desc"])
            conn.commit()
            return {
                "id": tid,
                "parent_prompt": row["parent_prompt"],
                "sub_task_desc": row["sub_task_desc"],
                "control": _get_task_control(conn, tid),
                "contract": {
                    "progress_events_required": True,
                    "check_controls_url": f"/api/v1/mind/orchestrator/tasks/{tid}/control",
                    "events_url": f"/api/v1/mind/orchestrator/tasks/{tid}/events",
                    "final_status_required": True,
                    "shared_mind_required": True,
                },
            }

        # If caller agent has no tasks, can we reassign a task from an agent on cooldown?
        now = int(time.time())
        cooldown_agents_rows = conn.execute(
            "SELECT agent_name FROM agent_cooldowns WHERE cooldown_until > ?",
            (now,)
        ).fetchall()
        cooldown_agents = [r["agent_name"] for r in cooldown_agents_rows if r["agent_name"] != agent_name]
        
        if cooldown_agents:
            placeholders = ",".join("?" for _ in cooldown_agents)
            row = conn.execute(
                f"""SELECT id, parent_prompt, agent_name, sub_task_desc 
                   FROM orchestrator_tasks 
                   WHERE agent_name IN ({placeholders}) AND status IN ('pending', 'assigned')
                   ORDER BY id ASC LIMIT 1""",
                tuple(cooldown_agents)
            ).fetchone()
            
            if row:
                tid = row["id"]
                original_agent = row["agent_name"]
                rerouted_desc = _reroute_desc(row["sub_task_desc"], original_agent, "cooldown")
                conn.execute(
                    """UPDATE orchestrator_tasks 
                       SET agent_name = ?, sub_task_desc = ?, status = 'assigned', updated_at = ?
                       WHERE id = ?""",
                    (agent_name, rerouted_desc, now, tid)
                )
                _append_task_event(
                    conn,
                    tid,
                    agent_name,
                    "rerouted_assigned",
                    rerouted_desc,
                    {"from_agent": original_agent, "to_agent": agent_name},
                )
                _record_agent_heartbeat(conn, agent_name, tid, "assigned", rerouted_desc)
                conn.commit()
                print(f"[orchestrator] Rerouted task {tid} from {original_agent} (on cooldown) to {agent_name}", flush=True)
                return {
                    "id": tid,
                    "parent_prompt": row["parent_prompt"],
                    "sub_task_desc": rerouted_desc,
                    "control": _get_task_control(conn, tid),
                    "contract": {
                        "progress_events_required": True,
                        "check_controls_url": f"/api/v1/mind/orchestrator/tasks/{tid}/control",
                        "events_url": f"/api/v1/mind/orchestrator/tasks/{tid}/events",
                        "final_status_required": True,
                        "shared_mind_required": True,
                    },
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
        _ensure_control_tables(conn)
        rows = conn.execute(
            """SELECT id, parent_prompt, agent_name, sub_task_desc, status, created_at, updated_at 
               FROM orchestrator_tasks 
               ORDER BY id DESC LIMIT 50"""
        ).fetchall()
        tasks = [dict(r) for r in rows]
        controls = _latest_task_controls(conn)
        for task in tasks:
            tid = int(task["id"])
            task["control"] = controls.get(tid)
            latest = conn.execute(
                """SELECT id, event_type, content, created_at, agent_name
                   FROM orchestrator_task_events
                   WHERE task_id = ?
                   ORDER BY id DESC LIMIT 1""",
                (tid,),
            ).fetchone()
            if latest:
                task["latest_event"] = dict(latest)
            else:
                task["latest_event"] = None
            count = conn.execute(
                "SELECT COUNT(*) c FROM orchestrator_task_events WHERE task_id = ?",
                (tid,),
            ).fetchone()
            task["event_count"] = int(count["c"] if count else 0)
        return tasks
    except Exception:
        return []
    finally:
        conn.close()


def update_task_status(task_id: int, status: str) -> bool:
    """Update task status (completed, failed, etc.)."""
    conn = _connect()
    try:
        _ensure_control_tables(conn)
        now = int(time.time())
        row = conn.execute(
            "SELECT agent_name, sub_task_desc FROM orchestrator_tasks WHERE id = ?",
            (int(task_id),),
        ).fetchone()
        conn.execute(
            "UPDATE orchestrator_tasks SET status = ?, updated_at = ? WHERE id = ?",
            (status, now, task_id)
        )
        if status in {"completed", "failed", "cancelled"}:
            conn.execute("DELETE FROM orchestrator_task_controls WHERE task_id = ?", (int(task_id),))
        _append_task_event(
            conn,
            int(task_id),
            row["agent_name"] if row else None,
            f"status_{status}",
            row["sub_task_desc"] if row else status,
            {"status": status},
        )
        if row:
            _record_agent_heartbeat(conn, row["agent_name"], int(task_id), status, row["sub_task_desc"])
        conn.commit()
        return True
    except Exception:
        return False
    finally:
        conn.close()
