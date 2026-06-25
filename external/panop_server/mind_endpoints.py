"""Mind endpoints — unified-mind storage and API.

The unified-mind layer for Egon. Per docs/UNIFIED_MIND_PLAN.md
(2026-05-28): every AI body (Claude Code, Codex, ChatGPT, Gemini,
Antigravity) shares one memory + project state + activity log via
this SQLite-backed REST surface.

Storage: state/mind.db (SQLite, WAL mode). Schema versioned via
PRAGMA user_version.

Surface (all rooted at /api/v1/mind/):
  POST agents/register        — idempotent upsert by name
  POST projects               — upsert by slug
  GET  projects               — list, newest-updated first
  POST sessions/start         — idempotent by (agent, external_id)
  POST sessions/end           — close + add summary
  POST activity               — append (session_id, kind, payload)
  GET  activity               — filter by project/agent/since
  POST memory                 — insert or update by id
  GET  memory                 — filter by kind/tags/q
  GET  context                — context broker (recent activity + relevant memory)
  GET  stats                  — counts for dashboard

Module-loaded; imports `app` from .main and registers routes on it.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path

from fastapi import Request
from fastapi.responses import FileResponse, HTMLResponse

from external.panop_server.main import app  # late-bound; main.py imports us at end


_ROOT = Path(__file__).resolve().parent.parent.parent
_DB_PATH = _ROOT / "state" / "mind.db"
_DB_LOCK = threading.RLock()
_HEALTH_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mind-health")
_HEALTH_CACHE: dict[str, dict] = {}
_HEALTH_CACHE_LOCK = threading.RLock()
_HEALTH_CACHE_PATH = _ROOT / "state" / "mind_health_cache.json"
_HEALTH_REFRESHING: set[str] = set()

SCHEMA_VERSION = 5

_SCHEMA = [
    "PRAGMA journal_mode = WAL",
    """CREATE TABLE IF NOT EXISTS agents (
        id INTEGER PRIMARY KEY,
        name TEXT UNIQUE NOT NULL,
        kind TEXT NOT NULL,
        created_at INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS projects (
        id INTEGER PRIMARY KEY,
        slug TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        description TEXT,
        status TEXT NOT NULL DEFAULT 'active',
        root_path TEXT,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY,
        agent_id INTEGER NOT NULL REFERENCES agents(id),
        project_id INTEGER REFERENCES projects(id),
        external_id TEXT,
        started_at INTEGER NOT NULL,
        ended_at INTEGER,
        summary TEXT,
        UNIQUE (agent_id, external_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions (project_id, started_at)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_agent ON sessions (agent_id, started_at)",
    """CREATE TABLE IF NOT EXISTS activity (
        id INTEGER PRIMARY KEY,
        session_id INTEGER NOT NULL REFERENCES sessions(id),
        ts INTEGER NOT NULL,
        kind TEXT NOT NULL,
        payload_json TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_activity_session ON activity (session_id, ts)",
    "CREATE INDEX IF NOT EXISTS idx_activity_ts ON activity (ts)",
    "CREATE INDEX IF NOT EXISTS idx_activity_kind ON activity (kind, ts)",
    """CREATE TABLE IF NOT EXISTS memory (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL,
        content TEXT NOT NULL,
        tags TEXT,
        attribution_agent_id INTEGER REFERENCES agents(id),
        attribution_session_id INTEGER REFERENCES sessions(id),
        related_memory_ids TEXT,
        last_reviewed INTEGER,
        interval_days INTEGER NOT NULL DEFAULT 0,
        ease_factor REAL NOT NULL DEFAULT 2.5,
        repetitions INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        superseded_by_memory_id INTEGER REFERENCES memory(id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_memory_kind ON memory (kind)",
    "CREATE INDEX IF NOT EXISTS idx_memory_updated ON memory (updated_at)",
    """CREATE TABLE IF NOT EXISTS files (
        id INTEGER PRIMARY KEY,
        project_id INTEGER REFERENCES projects(id),
        path TEXT UNIQUE NOT NULL,
        content_hash TEXT,
        last_editor_session_id INTEGER REFERENCES sessions(id),
        last_edited_at INTEGER,
        lease_session_id INTEGER REFERENCES sessions(id),
        lease_expires_at INTEGER
    )""",
    "CREATE INDEX IF NOT EXISTS idx_files_project ON files (project_id)",
    # FTS5 virtual table for memory content search
    "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(content, content='memory', content_rowid='id')",
    # Triggers to keep memory_fts synced
    """CREATE TRIGGER IF NOT EXISTS memory_ai AFTER INSERT ON memory BEGIN
         INSERT INTO memory_fts(rowid, content) VALUES (new.id, new.content);
       END""",
    """CREATE TRIGGER IF NOT EXISTS memory_ad AFTER DELETE ON memory BEGIN
         INSERT INTO memory_fts(memory_fts, rowid, content) VALUES ('delete', old.id, old.content);
       END""",
    """CREATE TRIGGER IF NOT EXISTS memory_au AFTER UPDATE ON memory BEGIN
         INSERT INTO memory_fts(memory_fts, rowid, content) VALUES ('delete', old.id, old.content);
         INSERT INTO memory_fts(rowid, content) VALUES (new.id, new.content);
       END""",
    """CREATE TABLE IF NOT EXISTS turns_ledger (
        id INTEGER PRIMARY KEY,
        session_id INTEGER NOT NULL REFERENCES sessions(id),
        ts INTEGER NOT NULL,
        model TEXT NOT NULL,
        input_tokens INTEGER NOT NULL,
        output_tokens INTEGER NOT NULL,
        cache_write_tokens INTEGER NOT NULL,
        cache_read_tokens INTEGER NOT NULL,
        tools TEXT,
        UNIQUE (session_id, ts, input_tokens, output_tokens)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_turns_ledger_ts ON turns_ledger (ts)",
    """CREATE TABLE IF NOT EXISTS orchestrator_tasks (
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
    )""",
    "CREATE INDEX IF NOT EXISTS idx_orchestrator_tasks_agent ON orchestrator_tasks (agent_name, status)",
    """CREATE TABLE IF NOT EXISTS agent_cooldowns (
        agent_name TEXT PRIMARY KEY,
        cooldown_until INTEGER NOT NULL,
        reason TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS orchestrator_task_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER,
        agent_name TEXT,
        event_type TEXT NOT NULL,
        content TEXT,
        payload_json TEXT,
        created_at INTEGER NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_orch_events_task ON orchestrator_task_events (task_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_orch_events_agent ON orchestrator_task_events (agent_name, id)",
    """CREATE TABLE IF NOT EXISTS orchestrator_task_controls (
        task_id INTEGER PRIMARY KEY,
        action TEXT NOT NULL,
        note TEXT,
        replacement_desc TEXT,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        acknowledged_at INTEGER
    )""",
    """CREATE TABLE IF NOT EXISTS orchestrator_agent_state (
        agent_name TEXT PRIMARY KEY,
        current_task_id INTEGER,
        status TEXT NOT NULL,
        detail TEXT,
        last_seen_at INTEGER NOT NULL
    )""",
]


def _now() -> int:
    return int(time.time())


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, isolation_level=None,
                           check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db() -> None:
    with _DB_LOCK:
        conn = _connect()
        try:
            cur = conn.cursor()
            for stmt in _SCHEMA:
                cur.execute(stmt)
            # One-time migration: populate memory_fts with existing memories
            cur.execute("""
                INSERT INTO memory_fts(rowid, content)
                SELECT id, content FROM memory
                WHERE id NOT IN (SELECT rowid FROM memory_fts)
            """)
            # Check and alter memory table schema dynamically if columns are missing
            cols = [r["name"] for r in cur.execute("PRAGMA table_info(memory)").fetchall()]
            if "last_reviewed" not in cols:
                cur.execute("ALTER TABLE memory ADD COLUMN last_reviewed INTEGER")
            if "interval_days" not in cols:
                cur.execute("ALTER TABLE memory ADD COLUMN interval_days INTEGER NOT NULL DEFAULT 0")
            if "ease_factor" not in cols:
                cur.execute("ALTER TABLE memory ADD COLUMN ease_factor REAL NOT NULL DEFAULT 2.5")
            if "repetitions" not in cols:
                cur.execute("ALTER TABLE memory ADD COLUMN repetitions INTEGER NOT NULL DEFAULT 0")
            if "superseded_by_memory_id" not in cols:
                cur.execute("ALTER TABLE memory ADD COLUMN superseded_by_memory_id INTEGER REFERENCES memory(id)")

            v = cur.execute("PRAGMA user_version").fetchone()[0]
            if v < SCHEMA_VERSION:
                cur.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        finally:
            conn.close()


_init_db()


def _upsert_agent(conn: sqlite3.Connection, name: str, kind: str) -> int:
    row = conn.execute("SELECT id FROM agents WHERE name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO agents (name, kind, created_at) VALUES (?, ?, ?)",
        (name, kind, _now()))
    return cur.lastrowid


def _upsert_project(conn: sqlite3.Connection, slug: str, name: str = "",
                    description: str = "", root_path: str | None = None) -> int:
    row = conn.execute("SELECT id FROM projects WHERE slug = ?", (slug,)).fetchone()
    if row:
        conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?",
                     (_now(), row["id"]))
        return row["id"]
    cur = conn.execute(
        """INSERT INTO projects (slug, name, description, status, root_path,
           created_at, updated_at) VALUES (?, ?, ?, 'active', ?, ?, ?)""",
        (slug, name or slug, description, root_path, _now(), _now()))
    return cur.lastrowid





# ── endpoints ──────────────────────────────────────────────────────────────

@app.post("/api/v1/mind/agents/register")
async def mind_register_agent(req: Request):
    try:
        body = await req.json()
        name = (body.get("name") or "").strip()
        kind = (body.get("kind") or "agent").strip()
        if not name:
            return {"status": "error", "error": "name required"}
        with _DB_LOCK:
            conn = _connect()
            try:
                aid = _upsert_agent(conn, name, kind)
            finally:
                conn.close()
        return {"status": "ok", "id": aid, "name": name}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.post("/api/v1/mind/projects")
async def mind_upsert_project(req: Request):
    try:
        body = await req.json()
        slug = (body.get("slug") or "").strip()
        if not slug:
            return {"status": "error", "error": "slug required"}
        with _DB_LOCK:
            conn = _connect()
            try:
                pid = _upsert_project(conn, slug, body.get("name") or "",
                                      body.get("description") or "",
                                      body.get("root_path"))
            finally:
                conn.close()
        return {"status": "ok", "id": pid, "slug": slug}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.get("/api/v1/mind/projects")
def mind_list_projects():
    try:
        with _DB_LOCK:
            conn = _connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM projects WHERE COALESCE(status,'active')='active' "
                    "ORDER BY updated_at DESC").fetchall()
            finally:
                conn.close()
        return {"status": "ok", "projects": [dict(r) for r in rows]}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.post("/api/v1/mind/orchestrator/dispatch")
async def mind_orchestrator_dispatch(req: Request):
    try:
        body = await req.json()
        prompt = (body.get("prompt") or "").strip()
        if not prompt:
            return {"status": "error", "error": "prompt required"}
        
        from lib.orchestration_engine import dispatch_prompt
        tasks = dispatch_prompt(prompt)
        
        # Trigger the Hermes runner asynchronously
        try:
            from lib.hermes_runner import trigger_hermes_runner
            trigger_hermes_runner()
        except Exception as e:
            print(f"[mind_endpoints] Failed to trigger hermes runner: {e}", flush=True)
        try:
            from lib.agent_wake_bridge import wake_pending_agents
            wake_pending_agents()
        except Exception as e:
            print(f"[mind_endpoints] Failed to trigger agent wake bridge: {e}", flush=True)
            
        return {"status": "ok", "tasks": tasks}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.get("/api/v1/mind/orchestrator/status")
def mind_orchestrator_status():
    try:
        from lib.orchestration_engine import (
            get_agent_routing_status,
            get_agents_cooldowns,
            get_tasks_status,
        )
        tasks = get_tasks_status()
        cooldowns = get_agents_cooldowns()
        routing = get_agent_routing_status()
        return {"status": "ok", "tasks": tasks, "cooldowns": cooldowns, "routing": routing}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.get("/api/v1/mind/orchestrator/service/status")
def mind_orchestrator_service_status():
    try:
        from lib.orchestrator_service import ensure_orchestrator_service, orchestrator_service_status

        ensure_orchestrator_service()
        return orchestrator_service_status()
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.get("/api/v1/mind/orchestrator/scheduler/status")
def mind_orchestrator_scheduler_status():
    try:
        from lib.orchestration_engine import get_scheduler_status
        return get_scheduler_status()
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.get("/api/v1/mind/orchestrator/mission-control")
def mind_orchestrator_mission_control(limit_events: int = 80):
    try:
        from lib.orchestration_engine import get_mission_control_status
        return get_mission_control_status(limit_events=limit_events)
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.get("/api/v1/mind/orchestrator/hermes")
def mind_orchestrator_hermes():
    """Hermes oversight snapshot for the console: stuck/failed/awaiting-veto
    tasks + masterlaw-screened proposals. Read-only; the console approves (requeue)
    or vetoes (cancel) via the existing task-control action endpoint."""
    try:
        from lib import hermes_monitor
        return {"status": "ok", **hermes_monitor.get_proposals()}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.get("/api/v1/mind/orchestrator/provider-hooks/status")
def mind_orchestrator_provider_hooks_status():
    try:
        from lib.provider_hooks import provider_hooks_status
        return provider_hooks_status()
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.get("/api/v1/mind/orchestrator/autonomy/status")
def mind_orchestrator_autonomy_status():
    try:
        from lib.orchestrator_service import autonomy_status, ensure_orchestrator_service

        ensure_orchestrator_service()
        return autonomy_status()
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.post("/api/v1/mind/orchestrator/autonomy/config")
async def mind_orchestrator_autonomy_config(req: Request):
    try:
        body = await req.json()
        from lib.orchestrator_service import ensure_orchestrator_service, update_autonomy_state

        ensure_orchestrator_service()
        return update_autonomy_state(**(body or {}))
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.post("/api/v1/mind/orchestrator/provider-hooks/scan")
def mind_orchestrator_provider_hooks_scan():
    try:
        from lib.provider_hooks import scan_provider_hooks
        return scan_provider_hooks()
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.get("/api/v1/mind/orchestrator/wake/status")
def mind_orchestrator_wake_status():
    try:
        from lib.agent_wake_bridge import wake_status
        return wake_status()
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.post("/api/v1/mind/orchestrator/wake/scan")
def mind_orchestrator_wake_scan():
    try:
        from lib.agent_wake_bridge import wake_pending_agents
        return wake_pending_agents()
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.get("/api/v1/mind/orchestrator/events")
def mind_orchestrator_events(task_id: int | None = None, since_id: int = 0, limit: int = 200):
    try:
        from lib.orchestration_engine import get_task_events
        return {"status": "ok", "events": get_task_events(task_id=task_id, since_id=since_id, limit=limit)}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.get("/api/v1/mind/orchestrator/tasks/{task_id}/events")
def mind_orchestrator_task_events(task_id: int, since_id: int = 0, limit: int = 200):
    try:
        from lib.orchestration_engine import get_task_events
        return {"status": "ok", "task_id": task_id, "events": get_task_events(task_id=task_id, since_id=since_id, limit=limit)}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.get("/api/v1/mind/orchestrator/tasks/{task_id}/control")
def mind_orchestrator_task_control(task_id: int):
    try:
        from lib.orchestration_engine import get_task_control
        return {"status": "ok", "task_id": task_id, "control": get_task_control(task_id)}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.post("/api/v1/mind/orchestrator/tasks/{task_id}/control")
async def mind_orchestrator_set_task_control(task_id: int, req: Request):
    try:
        body = await req.json()
        action = (body.get("action") or "").strip()
        note = (body.get("note") or body.get("clarification") or "").strip()
        replacement_desc = body.get("replacement_desc") or body.get("prompt")
        agent_name = (body.get("agent_name") or body.get("agent") or "").strip() or None
        from lib.orchestration_engine import set_task_control
        result = set_task_control(task_id, action, note=note, replacement_desc=replacement_desc, agent_name=agent_name)
        try:
            from lib.hermes_runner import trigger_hermes_runner
            trigger_hermes_runner()
        except Exception:
            pass
        try:
            from lib.agent_wake_bridge import wake_pending_agents
            wake_pending_agents()
        except Exception:
            pass
        return result
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.post("/api/v1/mind/orchestrator/tasks/{task_id}/control/ack")
async def mind_orchestrator_ack_task_control(task_id: int, req: Request):
    try:
        body = await req.json()
        agent_name = (body.get("agent_name") or body.get("agent") or "").strip() or None
        from lib.orchestration_engine import acknowledge_task_control
        return acknowledge_task_control(task_id, agent_name)
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.post("/api/v1/mind/orchestrator/tasks/{task_id}/events")
async def mind_orchestrator_append_task_event(task_id: int, req: Request):
    try:
        body = await req.json()
        agent_name = (body.get("agent_name") or body.get("agent") or "").strip()
        event_type = (body.get("event_type") or body.get("kind") or "progress").strip()
        content = str(body.get("content") or body.get("message") or "")
        payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
        from lib.orchestration_engine import append_task_event
        return append_task_event(task_id, agent_name, event_type, content, payload)
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.post("/api/v1/mind/agents/heartbeat")
async def mind_agent_heartbeat(req: Request):
    try:
        body = await req.json()
        agent_name = (body.get("agent_name") or body.get("agent") or "").strip()
        task_id = body.get("task_id")
        status = (body.get("status") or "active").strip()
        detail = str(body.get("detail") or body.get("message") or "")
        if not agent_name:
            return {"status": "error", "error": "agent_name required"}
        from lib.orchestration_engine import record_agent_heartbeat
        return record_agent_heartbeat(agent_name, int(task_id) if task_id is not None else None, status, detail)
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.post("/api/v1/mind/agents/cooldown")
async def mind_agent_cooldown(req: Request):
    try:
        body = await req.json()
        agent_name = (body.get("agent_name") or "").strip()
        cooldown_seconds = int(body.get("cooldown_seconds", 1800))
        reason = (body.get("reason") or "quota exceeded").strip()
        if not agent_name:
            return {"status": "error", "error": "agent_name required"}

        from lib.orchestration_engine import set_agent_cooldown
        cooldown = set_agent_cooldown(agent_name, cooldown_seconds, reason)
        try:
            from lib.hermes_runner import trigger_hermes_runner
            trigger_hermes_runner()
        except Exception:
            pass
        try:
            from lib.agent_wake_bridge import wake_pending_agents
            wake_pending_agents()
        except Exception:
            pass
        return {"status": "ok", **cooldown}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.post("/api/v1/mind/agents/cooldown/clear")
async def mind_agent_cooldown_clear(req: Request):
    try:
        body = await req.json()
        agent_name = (body.get("agent_name") or "").strip()
        if not agent_name:
            return {"status": "error", "error": "agent_name required"}

        from lib.orchestration_engine import clear_agent_cooldown
        clear_agent_cooldown(agent_name)
        try:
            from lib.hermes_runner import trigger_hermes_runner
            trigger_hermes_runner()
        except Exception:
            pass
        try:
            from lib.agent_wake_bridge import wake_pending_agents
            wake_pending_agents()
        except Exception:
            pass
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.post("/api/v1/mind/agents/failure")
async def mind_agent_failure(req: Request):
    try:
        body = await req.json()
        agent_name = (body.get("agent_name") or body.get("agent") or "").strip()
        detail = str(body.get("detail") or body.get("error") or "")
        cooldown_seconds = int(body.get("cooldown_seconds") or 1800)
        if not agent_name:
            return {"status": "error", "error": "agent_name required"}
        if not detail:
            return {"status": "error", "error": "detail required"}

        from lib.orchestration_engine import report_agent_failure
        result = report_agent_failure(agent_name, detail, cooldown_seconds)
        if result.get("status") == "cooldown":
            try:
                from lib.hermes_runner import trigger_hermes_runner
                trigger_hermes_runner()
            except Exception:
                pass
            try:
                from lib.agent_wake_bridge import wake_pending_agents
                wake_pending_agents()
            except Exception:
                pass
        return result
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.post("/api/v1/mind/orchestrator/complete")
async def mind_orchestrator_complete(req: Request):
    try:
        body = await req.json()
        task_id = body.get("task_id")
        status = body.get("status") or "completed"
        if task_id is None:
            return {"status": "error", "error": "task_id required"}
        
        from lib.orchestration_engine import update_task_status
        ok = update_task_status(int(task_id), status)
        if ok:
            return {"status": "ok"}
        return {"status": "error", "error": "failed to update task status"}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}



@app.post("/api/v1/mind/sessions/start")
async def mind_session_start(req: Request):
    try:
        body = await req.json()
        agent = (body.get("agent") or "").strip()
        external_id = body.get("external_id")
        project_slug = body.get("project")
        if not agent:
            return {"status": "error", "error": "agent required"}
        with _DB_LOCK:
            conn = _connect()
            try:
                aid = _upsert_agent(conn, agent, body.get("agent_kind") or "agent")
                pid = None
                if project_slug:
                    pid = _upsert_project(conn, project_slug, project_slug)
                if external_id:
                    row = conn.execute(
                        "SELECT id FROM sessions WHERE agent_id = ? AND external_id = ?",
                        (aid, external_id)).fetchone()
                    if row:
                        return {"status": "ok", "id": row["id"], "existed": True}
                started = int(body.get("started_at") or _now())
                cur = conn.execute(
                    """INSERT INTO sessions (agent_id, project_id, external_id,
                       started_at) VALUES (?, ?, ?, ?)""",
                    (aid, pid, external_id, started))
                return {"status": "ok", "id": cur.lastrowid}
            finally:
                conn.close()
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.post("/api/v1/mind/sessions/end")
async def mind_session_end(req: Request):
    try:
        body = await req.json()
        sid = body.get("session_id")
        if sid is None:
            return {"status": "error", "error": "session_id required"}
        summary = body.get("summary")
        ended = int(body.get("ended_at") or _now())
        with _DB_LOCK:
            conn = _connect()
            try:
                conn.execute(
                    "UPDATE sessions SET ended_at = ?, summary = ? WHERE id = ?",
                    (ended, summary, sid))
            finally:
                conn.close()
        return {"status": "ok", "id": sid}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.post("/api/v1/mind/activity")
async def mind_activity_append(req: Request):
    try:
        body = await req.json()
        sid = body.get("session_id")
        kind = (body.get("kind") or "").strip()
        if sid is None or not kind:
            return {"status": "error", "error": "session_id + kind required"}
        payload = body.get("payload") or {}
        ts = int(body.get("ts") or _now())
        with _DB_LOCK:
            conn = _connect()
            try:
                cur = conn.execute(
                    """INSERT INTO activity (session_id, ts, kind, payload_json)
                       VALUES (?, ?, ?, ?)""",
                    (sid, ts, kind, json.dumps(payload, ensure_ascii=False)))
                rid = cur.lastrowid
            finally:
                conn.close()
        return {"status": "ok", "id": rid}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.get("/api/v1/mind/activity")
def mind_activity_list(project: str | None = None,
                       agent: str | None = None,
                       since: int | None = None,
                       limit: int = 100):
    try:
        sql = """SELECT a.id, a.session_id, a.ts, a.kind, a.payload_json,
                        s.agent_id, ag.name as agent_name,
                        s.project_id, p.slug as project_slug
                 FROM activity a
                 JOIN sessions s ON s.id = a.session_id
                 JOIN agents ag ON ag.id = s.agent_id
                 LEFT JOIN projects p ON p.id = s.project_id
                 WHERE 1=1"""
        params: list = []
        if project:
            sql += " AND p.slug = ?"
            params.append(project)
        if agent:
            sql += " AND ag.name = ?"
            params.append(agent)
        if since is not None:
            sql += " AND a.ts >= ?"
            params.append(int(since))
        sql += " ORDER BY a.ts DESC LIMIT ?"
        params.append(min(max(int(limit), 1), 1000))
        with _DB_LOCK:
            conn = _connect()
            try:
                rows = conn.execute(sql, params).fetchall()
            finally:
                conn.close()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["payload"] = json.loads(d.pop("payload_json"))
            except Exception:
                d["payload"] = {}
            out.append(d)
        return {"status": "ok", "count": len(out), "activity": out}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.post("/api/v1/mind/memory")
async def mind_memory_upsert(req: Request):
    try:
        body = await req.json()
        mid = body.get("id")
        kind = (body.get("kind") or "fact").strip()
        content = (body.get("content") or "").strip()
        if not content:
            return {"status": "error", "error": "content required"}
        tags = body.get("tags") or ""
        if isinstance(tags, list):
            tags = ",".join(tags)
        a_agent = body.get("attribution_agent_id")
        a_session = body.get("attribution_session_id")
        related = body.get("related_memory_ids") or []
        if isinstance(related, list):
            related = ",".join(str(x) for x in related)
        superseded_by = body.get("superseded_by_memory_id")
        supersedes_id = body.get("supersedes_memory_id")
        with _DB_LOCK:
            conn = _connect()
            try:
                if mid:
                    conn.execute(
                        """UPDATE memory SET kind=?, content=?, tags=?,
                           attribution_agent_id=?, attribution_session_id=?,
                           related_memory_ids=?, superseded_by_memory_id=?, updated_at=? WHERE id=?""",
                        (kind, content, tags, a_agent, a_session,
                         related, superseded_by, _now(), mid))
                    out_id = mid
                else:
                    cur = conn.execute(
                        """INSERT INTO memory (kind, content, tags,
                           attribution_agent_id, attribution_session_id,
                           related_memory_ids, superseded_by_memory_id, created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (kind, content, tags, a_agent, a_session,
                         related, superseded_by, _now(), _now()))
                    out_id = cur.lastrowid
                if supersedes_id:
                    conn.execute(
                        "UPDATE memory SET superseded_by_memory_id = ?, updated_at = ? WHERE id = ?",
                        (out_id, _now(), supersedes_id))
            finally:
                conn.close()
        return {"status": "ok", "id": out_id}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.get("/api/v1/mind/memory")
def mind_memory_search(kind: str | None = None,
                       tags: str | None = None,
                       q: str | None = None,
                       include_superseded: bool = False,
                       limit: int = 50):
    try:
        sql = "SELECT * FROM memory WHERE 1=1"
        if not include_superseded:
            sql += " AND superseded_by_memory_id IS NULL"
        params: list = []
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        if tags:
            for t in [t.strip() for t in tags.split(",") if t.strip()]:
                sql += " AND tags LIKE ?"
                params.append(f"%{t}%")
        
        limit_val = min(max(int(limit), 1), 500)
        with _DB_LOCK:
            conn = _connect()
            try:
                if q:
                    try:
                        sql_fts = sql + " AND id IN (SELECT rowid FROM memory_fts WHERE memory_fts MATCH ?)"
                        sql_fts += " ORDER BY updated_at DESC LIMIT ?"
                        rows = conn.execute(sql_fts, params + [q, limit_val]).fetchall()
                    except sqlite3.OperationalError:
                        # Fallback to standard LIKE substring match on FTS5 syntax failure
                        sql_like = sql + " AND content LIKE ?"
                        sql_like += " ORDER BY updated_at DESC LIMIT ?"
                        rows = conn.execute(sql_like, params + [f"%{q}%", limit_val]).fetchall()
                else:
                    sql += " ORDER BY updated_at DESC LIMIT ?"
                    rows = conn.execute(sql, params + [limit_val]).fetchall()
            finally:
                conn.close()
        return {"status": "ok", "memory": [dict(r) for r in rows]}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.get("/api/v1/mind/memory/recall")
def mind_memory_recall():
    """Retrieve the next memory card due for spaced-repetition review.
    Falls back to a random concept or fact if none are due.
    """
    try:
        now_ts = _now()
        with _DB_LOCK:
            conn = _connect()
            try:
                # Find oldest due card (either never reviewed, or current time is past due date)
                sql = """SELECT * FROM memory 
                         WHERE (kind = 'fact' OR kind = 'concept') 
                           AND superseded_by_memory_id IS NULL
                           AND (last_reviewed IS NULL OR ? >= last_reviewed + interval_days * 86400)
                         ORDER BY last_reviewed ASC, created_at ASC LIMIT 1"""
                row = conn.execute(sql, (now_ts,)).fetchone()
                
                # Fallback to random if none are strictly due
                is_fallback = False
                if not row:
                    sql_fb = """SELECT * FROM memory 
                                WHERE (kind = 'fact' OR kind = 'concept') 
                                  AND superseded_by_memory_id IS NULL
                                ORDER BY RANDOM() LIMIT 1"""
                    row = conn.execute(sql_fb).fetchone()
                    if row:
                        is_fallback = True
                
                card = dict(row) if row else None
                return {"status": "ok", "card": card, "is_fallback": is_fallback}
            finally:
                conn.close()
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.post("/api/v1/mind/memory/{mid}/review")
async def mind_memory_review(mid: int, req: Request):
    """Submit a rating (0-5) for a card and compute its next review interval using SuperMemo-2."""
    try:
        body = await req.json()
        rating = body.get("rating")
        if rating is None or not isinstance(rating, int) or rating < 0 or rating > 5:
            return {"status": "error", "error": "rating must be an integer between 0 and 5"}
        
        with _DB_LOCK:
            conn = _connect()
            try:
                row = conn.execute("SELECT * FROM memory WHERE id = ?", (mid,)).fetchone()
                if not row:
                    return {"status": "error", "error": f"Memory {mid} not found"}
                
                interval = row["interval_days"] or 0
                repetitions = row["repetitions"] or 0
                ef = row["ease_factor"] or 2.5
                
                if rating < 3:
                    repetitions = 0
                    interval = 1
                else:
                    if repetitions == 0:
                        interval = 1
                    elif repetitions == 1:
                        interval = 6
                    else:
                        interval = int(round(interval * ef))
                    repetitions += 1
                
                ef = ef + (0.1 - (5 - rating) * (0.08 + (5 - rating) * 0.02))
                if ef < 1.3:
                    ef = 1.3
                
                now_ts = _now()
                conn.execute(
                    """UPDATE memory 
                       SET last_reviewed = ?, interval_days = ?, ease_factor = ?, repetitions = ?, updated_at = ?
                       WHERE id = ?""",
                    (now_ts, interval, ef, repetitions, now_ts, mid)
                )
                return {
                    "status": "ok",
                    "id": mid,
                    "last_reviewed": now_ts,
                    "interval_days": interval,
                    "ease_factor": ef,
                    "repetitions": repetitions
                }
            finally:
                conn.close()
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.get("/api/v1/mind/logo.png")
def get_logo():
    logo_path = Path(__file__).resolve().parent.parent.parent / "theme" / "logo.png"
    if logo_path.exists():
        return FileResponse(logo_path, media_type="image/png")
    return {"status": "error", "error": "logo not found"}



@app.get("/api/v1/mind/context")
def mind_context(project: str | None = None,
                 query: str | None = None,
                 limit_activity: int = 30,
                 limit_memory: int = 20):
    """Context broker. v1: keyword/tag matching + recent activity. v2
    will add embeddings + project-similarity weighting.

    Antigravity 2026-05-31: fixed memory filtering — now also matches on
    the project tag so `?project=egon` returns memory tagged with 'egon'
    even when the query string doesn't match content."""
    try:
        out = {"status": "ok", "project": project, "query": query,
               "recent_activity": [], "relevant_memory": [],
               "active_sessions": [], "structural_insights": []}
        with _DB_LOCK:
            conn = _connect()
            try:
                sql_a = """SELECT a.id, a.ts, a.kind, a.payload_json,
                                  ag.name as agent_name, p.slug as project_slug
                           FROM activity a
                           JOIN sessions s ON s.id = a.session_id
                           JOIN agents ag ON ag.id = s.agent_id
                           LEFT JOIN projects p ON p.id = s.project_id"""
                params_a: list = []
                if project:
                    sql_a += " WHERE p.slug = ?"
                    params_a.append(project)
                sql_a += " ORDER BY a.ts DESC LIMIT ?"
                params_a.append(int(limit_activity))
                for r in conn.execute(sql_a, params_a).fetchall():
                    d = dict(r)
                    try:
                        d["payload"] = json.loads(d.pop("payload_json"))
                    except Exception:
                        d["payload"] = {}
                    out["recent_activity"].append(d)

                # Memory: match on project tag AND/OR free-text query.
                # Previously only query was used, so ?project=egon returned
                # irrelevant memory unless the query text happened to appear
                # in the content. Now we OR-in the project tag match.
                sql_m = "SELECT * FROM memory WHERE 1=1"
                params_m: list = []
                conditions = []
                if project:
                    conditions.append("tags LIKE ?")
                    params_m.append(f"%{project}%")
                if query:
                    conditions.append("(content LIKE ? OR tags LIKE ?)")
                    params_m.extend([f"%{query}%", f"%{query}%"])
                if conditions:
                    # When both project and query are given, match memory that
                    # is relevant to the project OR matches the query — broader
                    # recall is better for context injection.
                    sql_m += " AND (" + " OR ".join(conditions) + ")"
                sql_m += " ORDER BY updated_at DESC LIMIT ?"
                params_m.append(int(limit_memory))
                out["relevant_memory"] = [
                    dict(r) for r in conn.execute(sql_m, params_m).fetchall()]

                sql_s = """SELECT s.id, s.started_at, s.external_id,
                                  ag.name as agent_name, p.slug as project_slug
                           FROM sessions s
                           JOIN agents ag ON ag.id = s.agent_id
                           LEFT JOIN projects p ON p.id = s.project_id
                           WHERE s.ended_at IS NULL"""
                params_s: list = []
                if project:
                    sql_s += " AND p.slug = ?"
                    params_s.append(project)
                sql_s += " ORDER BY s.started_at DESC LIMIT 20"
                out["active_sessions"] = [
                    dict(r) for r in conn.execute(sql_s, params_s).fetchall()]
            finally:
                conn.close()
        try:
            from lib.mind_graph import context_insights
            out["structural_insights"] = context_insights(
                project=project, query=query, limit=6)
        except Exception:
            out["structural_insights"] = []
        return out
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.get("/api/v1/mind/context/v2")
def mind_context_v2(project: str | None = None,
                    query: str | None = None,
                    budget_chars: int = 6000,
                    limit_activity: int = 8,
                    limit_memory: int = 8,
                    include_graph: bool = True,
                    include_audit: bool = True,
                    agent: str | None = None,
                    session_id: int | None = None):
    """Context Broker v2: compact shared-mind capsule for prompt injection."""
    try:
        from lib.mind_context_broker import build_context_capsule

        out = build_context_capsule(
            project=project,
            query=query,
            budget_chars=budget_chars,
            limit_activity=limit_activity,
            limit_memory=limit_memory,
            include_graph=include_graph,
            include_audit=include_audit,
            agent=agent,
        )
        if session_id is not None and out.get("status") == "ok":
            try:
                payload = {
                    "project": project,
                    "query": query,
                    "broker_version": out.get("version", "context-broker-v2"),
                    "activity_count": len(((out.get("sections") or {}).get("recent_activity")) or []),
                    "memory_count": len(((out.get("sections") or {}).get("durable_memory")) or []),
                    "structural_count": len(((out.get("sections") or {}).get("structural_insights")) or []),
                    "approx_tokens": (out.get("budget") or {}).get("approx_tokens"),
                    "source": "context_v2_endpoint",
                }
                with _DB_LOCK:
                    conn = _connect()
                    try:
                        conn.execute(
                            """INSERT INTO activity (session_id, ts, kind, payload_json)
                               VALUES (?, ?, 'mind_context', ?)""",
                            (int(session_id), _now(), json.dumps(payload, ensure_ascii=False)),
                        )
                    finally:
                        conn.close()
            except Exception:
                pass
        return out
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.post("/api/v1/mind/connect")
async def mind_connect(req: Request):
    """Connection Engine: POST {"text": <what you're writing>, "limit": 18}
    → ranked connections from Bruno's archives (Instapaper, Zotero, Paperpile,
    Kindle, Letterboxd, YouTube, bookmarks, Notion, …) + durable mind memory,
    each with provenance and the matched terms ("why"). 100% local, 0 LLM
    tokens. Bruno 2026-06-06 — the "click a button while writing" surface.
    Served by both the standalone mind service and Egon's in-process Panop."""
    try:
        import asyncio
        body = await req.json()
        from lib.connection_engine import connect as _connect_engine
        # Off-load to a worker thread: the blocking search must not freeze the
        # event loop, else /api/v1/mind/stats stalls and the supervisor restarts
        # this service mid-warmup (the flapping that made search cold). The
        # caller picks the engine: semantic_search (turbovec, ~1s, spans every
        # source) and lexical_search (full-corpus scan, ~30s) — the desktop/phone
        # pass lexical_search=False for a sub-second result. Bruno 2026-06-24.
        return await asyncio.to_thread(
            _connect_engine,
            str(body.get("text") or ""),
            int(body.get("limit") or 18),
            bool(body.get("semantic_search", True)),
            bool(body.get("lexical_search", True)),
        )
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.post("/api/v1/mind/synthesize")
async def mind_synthesize(req: Request):
    """Retrieval → answer. POST {"text": …} runs the Connection Engine, then a
    local LLM (Ollama qwen2.5:3b by default, see lib/synthesis.py) produces one
    grounded insight: strongest connection, tensions, what to open first.
    Returns the insight AND the connections. Only ever called on an explicit
    user action — synthesis is never automatic. Bruno 2026-06-12 (#2 of the
    strategy order: close the loop from links to answers)."""
    try:
        body = await req.json()
        text = str(body.get("text") or "")
        from lib.connection_engine import connect as _ce
        conn_res = _ce(text, limit=int(body.get("limit") or 14))
        if conn_res.get("status") != "ok":
            return conn_res
        from lib.synthesis import synthesize as _syn
        syn = _syn(text, conn_res.get("connections") or [])
        return {
            "status": "ok",
            "mode": conn_res.get("mode"),
            "terms": conn_res.get("terms"),
            "connections": conn_res.get("connections"),
            "synthesis": syn,      # {"status":"ok","insight":…} or "unavailable"
        }
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.get("/api/v1/mind/stats")
def mind_stats():
    """Dashboard counts. Used by the Mind tab in Egon's UI."""
    try:
        with _DB_LOCK:
            conn = _connect()
            try:
                stats = {
                    "agents": conn.execute("SELECT COUNT(*) c FROM agents").fetchone()["c"],
                    "projects": conn.execute("SELECT COUNT(*) c FROM projects").fetchone()["c"],
                    "sessions": conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"],
                    "activity": conn.execute("SELECT COUNT(*) c FROM activity").fetchone()["c"],
                    "memory": conn.execute("SELECT COUNT(*) c FROM memory").fetchone()["c"],
                    "files": conn.execute("SELECT COUNT(*) c FROM files").fetchone()["c"],
                    "schema_version": SCHEMA_VERSION,
                    "db_path": str(_DB_PATH),
                }
                # Top agents/projects by recent activity
                stats["top_agents_24h"] = [
                    dict(r) for r in conn.execute(
                        """SELECT ag.name as agent, COUNT(*) as activity_count
                           FROM activity a
                           JOIN sessions s ON s.id = a.session_id
                           JOIN agents ag ON ag.id = s.agent_id
                           WHERE a.ts >= ?
                           GROUP BY ag.name
                           ORDER BY activity_count DESC LIMIT 10""",
                        (_now() - 86400,)).fetchall()]
                stats["top_projects_24h"] = [
                    dict(r) for r in conn.execute(
                        """SELECT p.slug as project, COUNT(*) as activity_count
                           FROM activity a
                           JOIN sessions s ON s.id = a.session_id
                           JOIN projects p ON p.id = s.project_id
                           WHERE a.ts >= ?
                           GROUP BY p.slug
                           ORDER BY activity_count DESC LIMIT 10""",
                        (_now() - 86400,)).fetchall()]
            finally:
                conn.close()
        return {"status": "ok", **stats}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.get("/api/v1/mind/projects/summary")
def mind_projects_summary():
    """Batch project summary — returns per-project agent lists, 7d activity
    counts, and latest-activity preview in a SINGLE query. Replaces the
    N-queries-per-project pattern in the ProjectsPage UI.

    Antigravity 2026-05-31: added to eliminate one HTTP call per project on
    every 8s refresh cycle."""
    try:
        seven_days_ago = _now() - 7 * 86400
        with _DB_LOCK:
            conn = _connect()
            try:
                projects = conn.execute(
                    "SELECT * FROM projects WHERE COALESCE(status,'active')='active' "
                    "ORDER BY updated_at DESC LIMIT 50"
                ).fetchall()

                summaries = []
                for proj in projects:
                    pid = proj["id"]
                    slug = proj["slug"]

                    # Agents that have worked on this project
                    agents = [r["name"] for r in conn.execute(
                        """SELECT DISTINCT ag.name FROM sessions s
                           JOIN agents ag ON ag.id = s.agent_id
                           WHERE s.project_id = ?""", (pid,)).fetchall()]

                    # 7-day activity count
                    n_7d = conn.execute(
                        """SELECT COUNT(*) c FROM activity a
                           JOIN sessions s ON s.id = a.session_id
                           WHERE s.project_id = ? AND a.ts >= ?""",
                        (pid, seven_days_ago)).fetchone()["c"]

                    # Latest activity row
                    latest = conn.execute(
                        """SELECT a.ts, a.kind, a.payload_json, ag.name as agent_name
                           FROM activity a
                           JOIN sessions s ON s.id = a.session_id
                           JOIN agents ag ON ag.id = s.agent_id
                           WHERE s.project_id = ?
                           ORDER BY a.ts DESC LIMIT 1""",
                        (pid,)).fetchone()

                    last_ts = latest["ts"] if latest else proj["updated_at"]
                    last_kind = latest["kind"] if latest else None
                    last_agent = latest["agent_name"] if latest else None
                    try:
                        last_payload = json.loads(latest["payload_json"])[:200] if latest else ""
                    except Exception:
                        last_payload = str(latest["payload_json"])[:200] if latest else ""

                    summaries.append({
                        "slug": slug,
                        "name": proj["name"],
                        "description": proj["description"],
                        "status": proj["status"],
                        "root_path": proj["root_path"],
                        "agents": sorted(agents),
                        "activity_count_7d": n_7d,
                        "last_ts": last_ts,
                        "last_kind": last_kind,
                        "last_agent": last_agent,
                        "last_payload_preview": str(last_payload)[:200] if last_payload else "",
                        "updated_at": proj["updated_at"],
                    })
            finally:
                conn.close()

        # Sort by last activity timestamp (most recent first)
        summaries.sort(key=lambda x: x.get("last_ts") or 0, reverse=True)
        return {"status": "ok", "projects": summaries}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.post("/api/v1/mind/files/lease")
async def mind_file_lease(req: Request):
    try:
        import os
        body = await req.json()
        raw_path = body.get("path")
        sid = body.get("session_id")
        duration = int(body.get("duration_seconds") or body.get("ttl_seconds") or body.get("duration") or 60)
        duration = max(60, duration)
        if not raw_path or sid is None:
            return {"status": "error", "error": "path and session_id required"}
        
        # Normalize path to absolute path with forward slashes
        normalized_path = os.path.abspath(raw_path).replace("\\", "/")
        
        # Resolve project from path
        from lib.mind_project_resolver import canonical_slug
        project_slug = canonical_slug(normalized_path)
        
        with _DB_LOCK:
            conn = _connect()
            try:
                # Get project ID
                pid = None
                if project_slug:
                    pid = _upsert_project(conn, project_slug, project_slug)
                
                # Check current lease
                now_ts = _now()
                row = conn.execute(
                    "SELECT lease_session_id, lease_expires_at FROM files WHERE path = ?",
                    (normalized_path,)).fetchone()
                
                if row:
                    lease_session = row["lease_session_id"]
                    lease_expires = row["lease_expires_at"]
                    # If there's an active lease owned by another session
                    if (lease_session is not None and 
                        lease_expires is not None and 
                        lease_expires > now_ts and 
                        lease_session != sid):
                        # Fetch agent details for user-friendliness
                        agent_name = "unknown agent"
                        sess = conn.execute(
                            """SELECT ag.name FROM sessions s 
                               JOIN agents ag ON ag.id = s.agent_id 
                               WHERE s.id = ?""", (lease_session,)).fetchone()
                        if sess:
                            agent_name = sess["name"]
                        
                        # Log lock conflict activity
                        conn.execute(
                            """INSERT INTO activity (session_id, ts, kind, payload_json)
                               VALUES (?, ?, 'lock_conflict', ?)""",
                            (sid, now_ts, json.dumps({
                                "path": normalized_path,
                                "holding_agent": agent_name,
                                "holding_session": lease_session
                            }, ensure_ascii=False))
                        )
                        
                        return {
                            "status": "error",
                            "error": "locked",
                            "holding_agent": agent_name,
                            "holding_session": lease_session,
                            "expires_in": lease_expires - now_ts
                        }
                
                # Upsert file lease
                expires_at = now_ts + duration
                if row:
                    conn.execute(
                        """UPDATE files 
                           SET lease_session_id = ?, lease_expires_at = ?, project_id = ?, last_editor_session_id = ?, last_edited_at = ?
                           WHERE path = ?""",
                        (sid, expires_at, pid, sid, now_ts, normalized_path))
                else:
                    conn.execute(
                        """INSERT INTO files 
                           (project_id, path, last_editor_session_id, last_edited_at, lease_session_id, lease_expires_at)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (pid, normalized_path, sid, now_ts, sid, expires_at))
                
                return {
                    "status": "ok",
                    "path": normalized_path,
                    "project": project_slug,
                    "expires_at": expires_at,
                    "expires_in": duration
                }
            finally:
                conn.close()
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.post("/api/v1/mind/files/release")
async def mind_file_release(req: Request):
    try:
        import os
        body = await req.json()
        raw_path = body.get("path")
        sid = body.get("session_id")
        if sid is None:
            return {"status": "error", "error": "session_id required"}
        
        with _DB_LOCK:
            conn = _connect()
            try:
                if raw_path:
                    normalized_path = os.path.abspath(raw_path).replace("\\", "/")
                    row = conn.execute(
                        "SELECT lease_session_id FROM files WHERE path = ?",
                        (normalized_path,)).fetchone()
                    if row and row["lease_session_id"] == sid:
                        conn.execute(
                            "UPDATE files SET lease_session_id = NULL, lease_expires_at = NULL WHERE path = ?",
                            (normalized_path,))
                        return {"status": "ok"}
                    elif row and row["lease_session_id"] is not None:
                        return {"status": "error", "error": "not_owner", "owner": row["lease_session_id"]}
                    return {"status": "ok", "message": "no lease found"}
                else:
                    # Release all leases for this session
                    conn.execute(
                        "UPDATE files SET lease_session_id = NULL, lease_expires_at = NULL WHERE lease_session_id = ?",
                        (sid,))
                    return {"status": "ok", "message": "all leases released"}
            finally:
                conn.close()
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.get("/api/v1/mind/files/leases")
def mind_file_leases():
    try:
        now_ts = _now()
        with _DB_LOCK:
            conn = _connect()
            try:
                sql = """SELECT f.path, f.lease_expires_at, f.lease_session_id,
                                p.slug as project_slug, s.external_id as session_external_id,
                                ag.name as agent_name
                         FROM files f
                         LEFT JOIN sessions s ON s.id = f.lease_session_id
                         LEFT JOIN agents ag ON ag.id = s.agent_id
                         LEFT JOIN projects p ON p.id = f.project_id
                         WHERE f.lease_expires_at > ?"""
                rows = conn.execute(sql, (now_ts,)).fetchall()
                leases = []
                for r in rows:
                    d = dict(r)
                    d["expires_in"] = max(0, d["lease_expires_at"] - now_ts)
                    leases.append(d)
                return {"status": "ok", "leases": leases}
            finally:
                conn.close()
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.get("/api/v1/mind/introspection/proposals")
def mind_introspection_proposals():
    try:
        from lib.mind_introspection import analyze_mind
        props = analyze_mind()
    except Exception as e:
        props = []
        analyze_err = f"{type(e).__name__}: {str(e)[:120]}"
    else:
        analyze_err = None
    # Fold in the always-on Hermes oversight so the proactive feed surfaces
    # stuck/failed work and idle-quota opportunities the orchestrator found.
    try:
        from lib import hermes_monitor
        hp = hermes_monitor.get_proposals() or {}
        for p in (hp.get("proposals") or [])[:6]:
            tier = p.get("masterlaw_tier", "allow")
            props.append({
                "id": f"hermes_{p.get('kind','')}_{p.get('task_id','')}",
                "title": f"Hermes: resume {p.get('agent','agent')} work",
                "description": f"{p.get('desc','')} — {p.get('why','')}"
                               + (f"  [MASTERLAW: {tier}]" if tier and tier != "allow" else ""),
                "severity": "error" if tier == "block" else "warning",
                "category": "orchestrator",
                "project": "egon",
                "ts": 0,
            })
    except Exception:
        pass
    if not props and analyze_err:
        return {"status": "error", "error": analyze_err}
    return {"status": "ok", "proposals": props}


@app.post("/api/v1/mind/introspection/run")
def mind_introspection_run():
    try:
        from lib.mind_introspection import run_introspection
        res = run_introspection()
        return res
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.get("/api/v1/mind/concept_graph")
def mind_concept_graph(rebuild: bool = False, k: int = 200, sample: int | None = None):
    """Higher-order concepts clustered from the embedded vault + their morphisms
    (the data behind the Categorical Mind / CatColab graphic home). Returns the
    cached graph; rebuild=true forces a fresh clustering (heavy — prefer the
    idle-gated egon_core rebuild)."""
    try:
        from lib import concept_graph
        if rebuild:
            return concept_graph.build_concept_graph(k=k, sample=sample)
        g = concept_graph.load_concept_graph()
        if g is None:
            return {"status": "empty",
                    "detail": "concept graph not built yet — runs idle-gated via egon_core"}
        return g
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.get("/api/v1/mind/graph")
def mind_graph(project: str | None = None,
               query: str | None = None,
               limit_activity: int = 1500,
               include_graph: bool = False):
    """Build a typed high-order graph over agents, actions, memory, files,
    category objects, and morphisms. Always writes a Gephi-compatible GEXF
    artifact under state/mind_graph; include_graph=true returns full nodes and
    edges for UI/debug callers."""
    try:
        from lib.mind_graph import build_mind_graph
        res = build_mind_graph(project=project, query=query,
                               limit_activity=limit_activity)
        if not include_graph:
            res.pop("graph", None)
        return res
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.get("/api/v1/mind/graph/gephi")
def mind_graph_gephi(project: str | None = None,
                     query: str | None = None,
                     limit_activity: int = 1500):
    """Return the latest generated graph as a GEXF file for Gephi."""
    try:
        from lib.mind_graph import build_mind_graph
        res = build_mind_graph(project=project, query=query,
                               limit_activity=limit_activity)
        if res.get("status") != "ok":
            return res
        path = res.get("gephi_gexf_path")
        return FileResponse(path, media_type="application/gexf+xml",
                            filename=Path(path).name)
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.get("/api/v1/mind/audit")
def mind_audit(project: str | None = None,
               since_hours: int = 72,
               limit_sessions: int = 80):
    """Audit whether recent agent sessions followed the shared-mind contract."""
    try:
        from lib.mind_audit import audit_mind
        return audit_mind(project=project,
                          since_hours=since_hours,
                          limit_sessions=limit_sessions)
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}


def _cached_health_result(cache_key: str, compute, timeout_s: float = 8.0,
                          max_age_s: int = 300) -> dict:
    now = int(time.time())
    cached = _get_health_cache(cache_key, now, max_age_s)
    if cached:
        return cached

    def _store_done(future, key: str = cache_key) -> None:
        try:
            result = future.result()
            if not isinstance(result, dict):
                result = {"status": "error", "error": f"unexpected result type {type(result).__name__}"}
            _set_health_cache(key, result)
        except Exception as e:
            _set_health_cache(key, {
                "status": "error",
                "error": f"{type(e).__name__}: {str(e)[:200]}",
            })
        finally:
            with _HEALTH_CACHE_LOCK:
                _HEALTH_REFRESHING.discard(key)

    with _HEALTH_CACHE_LOCK:
        already_refreshing = cache_key in _HEALTH_REFRESHING
        if not already_refreshing:
            _HEALTH_REFRESHING.add(cache_key)
    if already_refreshing:
        stale = _get_health_cache(cache_key, now, max_age_s=None)
        if stale:
            stale.setdefault("cache", {})
            stale["cache"].update({"refreshing": True, "stale": True})
            return stale
        return {
            "status": "refreshing",
            "cache": {"hit": False, "refreshing": True},
        }

    future = _HEALTH_EXECUTOR.submit(compute)
    future.add_done_callback(_store_done)
    try:
        result = future.result(timeout=timeout_s)
        if not isinstance(result, dict):
            result = {"status": "error", "error": f"unexpected result type {type(result).__name__}"}
        _set_health_cache(cache_key, result)
        with _HEALTH_CACHE_LOCK:
            _HEALTH_REFRESHING.discard(cache_key)
        out = dict(result)
        out["cache"] = {"hit": False, "age_seconds": 0}
        return out
    except TimeoutError:
        stale = _get_health_cache(cache_key, now, max_age_s=None)
        if stale:
            stale.setdefault("cache", {})
            stale["cache"].update({"hit": True, "stale": True, "refresh": "timeout"})
            return stale
        return {
            "status": "refreshing",
            "message": f"{cache_key} refresh is still running",
            "cache": {"hit": False, "refreshing": True, "refresh": "timeout"},
        }


def _get_health_cache(cache_key: str, now: int | None = None,
                      max_age_s: int | None = 300) -> dict | None:
    now = now or int(time.time())
    with _HEALTH_CACHE_LOCK:
        cached = _HEALTH_CACHE.get(cache_key)
        if not cached:
            cached = _load_health_cache_from_disk().get(cache_key)
            if cached:
                _HEALTH_CACHE[cache_key] = cached
        if not cached:
            return None
        age = now - int(cached.get("_cached_at", 0))
        if max_age_s is not None and age > max_age_s:
            return None
        out = dict(cached.get("result") or {})
        out.setdefault("status", "ok")
        out["cache"] = {
            "hit": True,
            "age_seconds": age,
            "persistent": bool(cached.get("persistent")),
        }
        return out


def _set_health_cache(cache_key: str, result: dict) -> None:
    entry = {"_cached_at": int(time.time()), "result": result, "persistent": True}
    with _HEALTH_CACHE_LOCK:
        _HEALTH_CACHE[cache_key] = entry
        all_cache = _load_health_cache_from_disk()
        all_cache[cache_key] = entry
        _write_health_cache_to_disk(all_cache)


def _load_health_cache_from_disk() -> dict:
    try:
        if _HEALTH_CACHE_PATH.exists():
            body = json.loads(_HEALTH_CACHE_PATH.read_text(encoding="utf-8"))
            if isinstance(body, dict):
                return body
    except Exception:
        pass
    return {}


def _write_health_cache_to_disk(cache: dict) -> None:
    try:
        _HEALTH_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _HEALTH_CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=True, indent=2), encoding="utf-8")
        tmp.replace(_HEALTH_CACHE_PATH)
    except Exception:
        pass


@app.get("/api/v1/mind/scorecard")
def mind_scorecard(project: str | None = None,
                   since_hours: int = 168,
                   capsule_budget_chars: int = 3500,
                   refresh: bool = False):
    """Quantified meta-harness health and token-ROI scorecard."""
    try:
        from lib.mind_scorecard import build_mind_scorecard
        cache_key = f"scorecard:{project or ''}:{int(since_hours)}:{int(capsule_budget_chars)}"
        return _cached_health_result(
            cache_key,
            lambda: build_mind_scorecard(
                project=project,
                since_hours=since_hours,
                capsule_budget_chars=capsule_budget_chars,
            ),
            timeout_s=8.0,
            max_age_s=0 if refresh else 300,
        )
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.get("/api/v1/mind/enforcement/status")
def mind_enforcement_status(project: str | None = "egon",
                            since_hours: int = 168,
                            refresh: bool = False):
    """Check agent config and runtime coverage for unified-mind enforcement."""
    try:
        from lib.mind_enforcement import enforcement_status
        cache_key = f"enforcement:{project or ''}:{int(since_hours)}"
        return _cached_health_result(
            cache_key,
            lambda: enforcement_status(project=project, since_hours=since_hours),
            timeout_s=12.0,
            max_age_s=0 if refresh else 300,
        )
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.get("/api/v1/mind/activation/test")
@app.post("/api/v1/mind/activation/test")
def mind_activation_test(project: str = "egon",
                         query: str = "activation test",
                         run_mcp: bool = True):
    """Run an end-to-end activation test of the unified-mind harness."""
    try:
        from lib.mind_activation import run_activation_test
        return run_activation_test(
            project=project or "egon",
            query=query or "activation test",
            run_mcp=run_mcp,
        )
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.get("/api/v1/mind/activation/history")
def mind_activation_history(project: str = "egon",
                            limit: int = 20):
    """Return persisted activation-test history and score deltas."""
    try:
        from lib.mind_activation import activation_history
        return activation_history(project=project, limit=limit)
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.post("/api/v1/mind/ledger/turns")
async def mind_ledger_turns_append(req: Request):
    try:
        body = await req.json()
        sid = body.get("session_id")
        ts = body.get("ts")
        model = body.get("model")
        usage = body.get("usage") or {}
        tools = body.get("tools") or []
        if sid is None or ts is None or not model:
            return {"status": "error", "error": "session_id, ts, and model required"}
        
        in_t = usage.get("input_tokens", 0)
        out_t = usage.get("output_tokens", 0)
        cw_t = usage.get("cache_creation_input_tokens", 0)
        cr_t = usage.get("cache_read_input_tokens", 0)
        tools_str = ",".join(tools) if isinstance(tools, list) else str(tools)
        
        with _DB_LOCK:
            conn = _connect()
            try:
                # Check if this exact turn already exists to avoid duplicates
                row = conn.execute(
                    """SELECT id FROM turns_ledger 
                       WHERE session_id = ? AND ts = ? AND input_tokens = ? AND output_tokens = ?""",
                    (sid, ts, in_t, out_t)).fetchone()
                if not row:
                    conn.execute(
                        """INSERT INTO turns_ledger 
                           (session_id, ts, model, input_tokens, output_tokens, cache_write_tokens, cache_read_tokens, tools)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (sid, ts, model, in_t, out_t, cw_t, cr_t, tools_str))
            finally:
                conn.close()
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.get("/api/v1/mind/categorical")
def mind_categorical_reconcile():
    try:
        from lib.categorical_mind import scan_and_reconcile_categories
        res = scan_and_reconcile_categories()
        return res
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.post("/api/v1/mind/categorical/synthesize")
async def mind_categorical_synthesize(req: Request):
    try:
        body = await req.json()
        concept = (body.get("concept") or "").strip()
        if not concept:
            return {"status": "error", "error": "concept required"}
        from lib.categorical_synthesizer import synthesize_category
        res = synthesize_category(concept)
        return res
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.get("/dashboard", response_class=HTMLResponse)
def get_dashboard():
    dashboard_path = Path(__file__).resolve().parent / "dashboard.html"
    if not dashboard_path.exists():
        return HTMLResponse(content="<h1>Dashboard Template Not Found</h1>", status_code=404)
    return HTMLResponse(content=dashboard_path.read_text(encoding="utf-8"))


@app.get("/api/v1/classifier/lowconf")
def get_classifier_lowconf():
    lowconf_path = _ROOT / "state" / "panop" / "history_lowconf.json"
    if not lowconf_path.exists():
        return {"status": "ok", "candidates": []}
    try:
        data = json.loads(lowconf_path.read_text(encoding="utf-8"))
        return {"status": "ok", "candidates": data[:10]}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.post("/api/v1/classifier/review")
async def post_classifier_review(req: Request):
    try:
        body = await req.json()
        url = body.get("url")
        title = body.get("title")
        category = body.get("category")
        if not url or not category:
            return {"status": "error", "error": "url and category required"}

        from lib import kms_knn
        kms_knn.learn(title or "", url, category)

        # Trigger k-NN index rebuild asynchronously
        threading.Thread(target=kms_knn.build_index, daemon=True, name="knn-index-build").start()

        # Remove from history_lowconf.json
        lowconf_path = _ROOT / "state" / "panop" / "history_lowconf.json"
        if lowconf_path.exists():
            try:
                data = json.loads(lowconf_path.read_text(encoding="utf-8"))
                filtered = [item for item in data if item.get("url") != url]
                lowconf_path.write_text(json.dumps(filtered, indent=2, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass

        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}
