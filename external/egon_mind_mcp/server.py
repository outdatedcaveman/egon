"""Egon Mind MCP server.

A Model Context Protocol server that exposes Egon's unified mind
(SQLite-backed REST under /api/v1/mind/*) as MCP tools. Works in
Claude Desktop, Antigravity (Gemini IDE), Codex CLI, Cursor, Goose,
and any other MCP-capable agent.

Transport: stdio JSON-RPC 2.0 (newline-delimited messages).
Dependencies: stdlib only — runs under any Python 3.8+ on the system,
no venv required. The server makes HTTP calls to a running Egon Panop
on http://127.0.0.1:8000 (override via EGON_MIND_API env var).

Tools exposed:
  • mind_stats              — counts + top-agents-24h + top-projects-24h
  • mind_context            — recent activity + relevant memory + active sessions
  • mind_activity_list      — list activity (filterable)
  • mind_activity_append    — log new activity
  • mind_memory_search      — search memory by kind/tags/text
  • mind_memory_upsert      — add or update durable memory
  • mind_projects_list      — list registered projects
  • mind_register_agent     — register self (or another) as an agent

If Egon isn't running, every tool returns a clear `mind_offline` error
so the calling agent can handle it gracefully.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API_BASE = os.environ.get("EGON_MIND_API", "http://127.0.0.1:8000/api/v1/mind")
SERVER_NAME = "egon-mind"
SERVER_VERSION = "1.0.0"
PROTOCOL_VERSION = "2025-06-18"  # MCP spec date
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
SERVICE_SCRIPT = ROOT / "scripts" / "mind_service.py"
DEFAULT_API_BASE = "http://127.0.0.1:8000/api/v1/mind"
_LAST_AUTOSTART_AT = 0.0
_MCP_SESSION_ID = None


def _configure_stdio() -> None:
    """Force Unicode-capable stdio on Windows MCP hosts.

    Some clients launch Python with a legacy Windows code page. Returning a
    Context Broker capsule with arrows, emoji, or accented text can then raise a
    charmap encode error before the JSON-RPC response reaches the client.
    """
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def _log(msg: str) -> None:
    """Diagnostics go to stderr — stdout is reserved for JSON-RPC."""
    try:
        print(f"[egon-mind-mcp] {msg}", file=sys.stderr, flush=True)
    except Exception:
        pass


# ── HTTP helpers (stdlib only) ─────────────────────────────────────────────

def _raw_http(method: str, path: str, body: dict | None = None,
              params: dict | None = None, timeout: float = 1.5) -> tuple[int, dict | str]:
    url = API_BASE + path
    if params:
        q = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        if q:
            url += "?" + q
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(raw)
            except Exception:
                return resp.status, raw
    except urllib.error.URLError as e:
        return 0, f"mind_offline: {e}"
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def _service_python() -> str:
    from lib.python_runtime import base_python
    return str(base_python(ROOT, windowed=True))


def _autostart_enabled() -> bool:
    flag = os.environ.get("EGON_MIND_AUTOSTART", "1").strip().lower()
    if flag in {"0", "false", "no", "off"}:
        return False
    return API_BASE.rstrip("/") == DEFAULT_API_BASE


def _start_mind_service() -> bool:
    global _LAST_AUTOSTART_AT
    if not _autostart_enabled() or not SERVICE_SCRIPT.exists():
        return False

    now = time.time()
    if now - _LAST_AUTOSTART_AT < 5:
        return True
    _LAST_AUTOSTART_AT = now

    from lib.python_runtime import runtime_env
    env = runtime_env(ROOT)

    kwargs = {
        "cwd": str(ROOT),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "env": env,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | 0x00000008  # DETACHED_PROCESS
        )

    try:
        subprocess.Popen([_service_python(), str(SERVICE_SCRIPT)], **kwargs)
        _log("started standalone mind service")
        return True
    except Exception as e:
        _log(f"mind service autostart failed: {type(e).__name__}: {e}")
        return False


def _wait_for_mind_ready(timeout_s: float = 12.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        status, body = _raw_http("GET", "/stats", timeout=1.0)
        if status == 200 and isinstance(body, dict) and body.get("status") == "ok":
            return True
        time.sleep(0.4)
    return False


def _http(method: str, path: str, body: dict | None = None,
          params: dict | None = None, timeout: float = 1.5) -> tuple[int, dict | str]:
    status, body = _raw_http(method, path, body=body, params=params, timeout=timeout)
    if status != 0:
        return status, body
    if _start_mind_service() and _wait_for_mind_ready():
        return _raw_http(method, path, body=body, params=params, timeout=timeout)
    return status, body


def _mcp_session_id() -> int | None:
    global _MCP_SESSION_ID
    if _MCP_SESSION_ID is not None:
        return _MCP_SESSION_ID
    agent_name = os.environ.get("EGON_MCP_AGENT", "egon-mind-mcp")
    external_id = os.environ.get("EGON_MCP_SESSION", f"mcp-{os.getpid()}")
    status, body = _raw_http("POST", "/agents/register",
                             body={"name": agent_name, "kind": "mcp-server"},
                             timeout=1.5)
    if status != 200 or not isinstance(body, dict) or body.get("status") == "error":
        return None
    status, body = _raw_http("POST", "/sessions/start",
                             body={"agent": agent_name,
                                   "external_id": external_id,
                                   "project": os.environ.get("EGON_MCP_PROJECT"),
                                   "started_at": int(time.time())},
                             timeout=1.5)
    if status == 200 and isinstance(body, dict):
        _MCP_SESSION_ID = body.get("id")
    return _MCP_SESSION_ID


def _log_mcp_activity(kind: str, payload: dict) -> None:
    sid = _mcp_session_id()
    if sid is None:
        return
    _raw_http("POST", "/activity",
              body={"session_id": sid, "kind": kind,
                    "payload": payload, "ts": int(time.time())},
              timeout=1.5)


def _requested_agent(args: dict) -> str | None:
    agent = args.get("agent")
    if agent:
        return str(agent)
    env_agent = os.environ.get("EGON_MCP_AGENT")
    if env_agent and env_agent != "egon-mind-mcp":
        return env_agent
    return None


def _ok_or_error(status: int, body) -> dict:
    if status == 200 and isinstance(body, dict):
        return body
    if status == 0:
        return {"status": "error", "error": str(body),
                "hint": "The standalone mind service should auto-start; if it did not, run scripts/mind_service.py."}
    return {"status": "error", "http_status": status,
            "body": body if isinstance(body, str) else str(body)[:400]}


# ── tool implementations ──────────────────────────────────────────────────

def tool_mind_stats(_args: dict) -> dict:
    s, b = _http("GET", "/stats")
    return _ok_or_error(s, b)


def tool_mind_context(args: dict) -> dict:
    s, b = _http("GET", "/context/v2",
                 params={"project": args.get("project"),
                         "query": args.get("query"),
                         "limit_activity": args.get("limit_activity") or 30,
                         "limit_memory": args.get("limit_memory") or 20,
                         "budget_chars": args.get("budget_chars") or 6000,
                         "agent": _requested_agent(args)},
                 timeout=10.0)
    out = _ok_or_error(s, b)
    if out.get("status") != "ok":
        s, b = _http("GET", "/context",
                     params={"project": args.get("project"),
                             "query": args.get("query"),
                             "limit_activity": args.get("limit_activity") or 30,
                             "limit_memory": args.get("limit_memory") or 20,
                             "agent": _requested_agent(args)})
        out = _ok_or_error(s, b)
    if out.get("status") == "ok" and out.get("version") == "context-broker-v2":
        sections = out.get("sections") or {}
        out.setdefault("recent_activity", sections.get("recent_activity") or [])
        out.setdefault("relevant_memory", sections.get("durable_memory") or [])
        out.setdefault("active_sessions", sections.get("active_sessions") or [])
        out.setdefault("structural_insights", sections.get("structural_insights") or [])
    if out.get("status") == "ok":
        _log_mcp_activity("mind_context", {
            "project": args.get("project"),
            "query": args.get("query"),
            "broker_version": out.get("version", "v1"),
            "activity_count": len(out.get("recent_activity") or []),
            "memory_count": len(out.get("relevant_memory") or []),
            "structural_count": len(out.get("structural_insights") or []),
            "approx_tokens": (out.get("budget") or {}).get("approx_tokens"),
        })
    return out


def tool_mind_context_v2(args: dict) -> dict:
    s, b = _http("GET", "/context/v2",
                 params={"project": args.get("project"),
                         "query": args.get("query"),
                         "budget_chars": args.get("budget_chars") or 6000,
                         "limit_activity": args.get("limit_activity") or 8,
                         "limit_memory": args.get("limit_memory") or 8,
                         "include_graph": args.get("include_graph"),
                         "include_audit": args.get("include_audit"),
                         "agent": _requested_agent(args)},
                 timeout=10.0)
    out = _ok_or_error(s, b)
    if out.get("status") == "ok":
        _log_mcp_activity("mind_context", {
            "project": args.get("project"),
            "query": args.get("query"),
            "broker_version": out.get("version", "context-broker-v2"),
            "activity_count": len(((out.get("sections") or {}).get("recent_activity")) or []),
            "memory_count": len(((out.get("sections") or {}).get("durable_memory")) or []),
            "structural_count": len(((out.get("sections") or {}).get("structural_insights")) or []),
            "approx_tokens": (out.get("budget") or {}).get("approx_tokens"),
        })
    return out


def tool_mind_agent_failure(args: dict) -> dict:
    body = {
        "agent_name": args.get("agent_name") or args.get("agent") or _requested_agent(args),
        "detail": args.get("detail") or args.get("error") or "",
        "cooldown_seconds": args.get("cooldown_seconds") or 1800,
    }
    if not body["agent_name"] or not body["detail"]:
        return {"status": "error", "error": "agent_name and detail required"}
    s, b = _http("POST", "/agents/failure", body=body, timeout=2.0)
    return _ok_or_error(s, b)


def tool_mind_orchestrator_event(args: dict) -> dict:
    task_id = args.get("task_id")
    if task_id is None:
        return {"status": "error", "error": "task_id required"}
    body = {
        "agent_name": args.get("agent_name") or args.get("agent") or _requested_agent(args),
        "event_type": args.get("event_type") or args.get("kind") or "progress",
        "content": args.get("content") or args.get("message") or "",
        "payload": args.get("payload") or {},
    }
    s, b = _http("POST", f"/orchestrator/tasks/{int(task_id)}/events", body=body, timeout=2.0)
    return _ok_or_error(s, b)


def tool_mind_orchestrator_control(args: dict) -> dict:
    task_id = args.get("task_id")
    if task_id is None:
        return {"status": "error", "error": "task_id required"}
    if args.get("action"):
        body = {
            "action": args.get("action"),
            "note": args.get("note") or args.get("clarification") or "",
            "replacement_desc": args.get("replacement_desc") or args.get("prompt"),
            "agent_name": args.get("agent_name") or args.get("agent") or _requested_agent(args),
        }
        s, b = _http("POST", f"/orchestrator/tasks/{int(task_id)}/control", body=body, timeout=2.0)
    else:
        s, b = _http("GET", f"/orchestrator/tasks/{int(task_id)}/control", timeout=2.0)
    return _ok_or_error(s, b)


def tool_mind_orchestrator_events(args: dict) -> dict:
    task_id = args.get("task_id")
    params = {
        "since_id": args.get("since_id") or 0,
        "limit": args.get("limit") or 200,
    }
    if task_id is None:
        s, b = _http("GET", "/orchestrator/events", params=params, timeout=3.0)
    else:
        s, b = _http("GET", f"/orchestrator/tasks/{int(task_id)}/events", params=params, timeout=3.0)
    return _ok_or_error(s, b)


def tool_mind_agent_heartbeat(args: dict) -> dict:
    body = {
        "agent_name": args.get("agent_name") or args.get("agent") or _requested_agent(args),
        "task_id": args.get("task_id"),
        "status": args.get("status") or "active",
        "detail": args.get("detail") or args.get("message") or "",
    }
    if not body["agent_name"]:
        return {"status": "error", "error": "agent_name required"}
    s, b = _http("POST", "/agents/heartbeat", body=body, timeout=2.0)
    return _ok_or_error(s, b)


def tool_mind_orchestrator_scheduler(_args: dict) -> dict:
    s, b = _http("GET", "/orchestrator/scheduler/status", timeout=3.0)
    return _ok_or_error(s, b)


def tool_mind_orchestrator_mission(args: dict) -> dict:
    s, b = _http("GET", "/orchestrator/mission-control",
                 params={"limit_events": args.get("limit_events") or 80},
                 timeout=5.0)
    return _ok_or_error(s, b)


def tool_mind_orchestrator_autonomy(args: dict) -> dict:
    updates = {k: args[k] for k in (
        "enabled", "mode", "stuck_after_seconds",
        "auto_requeue_stuck", "wake_hermes", "wake_agents", "provider_hooks",
    ) if k in args}
    if updates:
        s, b = _http("POST", "/orchestrator/autonomy/config", body=updates, timeout=3.0)
    else:
        s, b = _http("GET", "/orchestrator/autonomy/status", timeout=3.0)
    return _ok_or_error(s, b)


def tool_mind_provider_hooks(args: dict) -> dict:
    if args.get("scan"):
        s, b = _http("POST", "/orchestrator/provider-hooks/scan", timeout=5.0)
    else:
        s, b = _http("GET", "/orchestrator/provider-hooks/status", timeout=3.0)
    return _ok_or_error(s, b)


def tool_mind_orchestrator_wake(args: dict) -> dict:
    if args.get("scan"):
        s, b = _http("POST", "/orchestrator/wake/scan", timeout=5.0)
    else:
        s, b = _http("GET", "/orchestrator/wake/status", timeout=3.0)
    return _ok_or_error(s, b)


def tool_mind_activity_list(args: dict) -> dict:
    s, b = _http("GET", "/activity",
                 params={"project": args.get("project"),
                         "agent": args.get("agent"),
                         "since": args.get("since"),
                         "limit": args.get("limit") or 50})
    return _ok_or_error(s, b)


def tool_mind_activity_append(args: dict) -> dict:
    body = {"session_id": args.get("session_id"),
            "kind": args.get("kind"),
            "payload": args.get("payload") or {}}
    if args.get("ts") is not None:
        body["ts"] = args["ts"]
    if not body["session_id"] or not body["kind"]:
        return {"status": "error", "error": "session_id + kind required"}
    s, b = _http("POST", "/activity", body=body)
    return _ok_or_error(s, b)


def tool_mind_memory_search(args: dict) -> dict:
    s, b = _http("GET", "/memory",
                 params={"kind": args.get("kind"),
                         "tags": args.get("tags"),
                         "q": args.get("q"),
                         "limit": args.get("limit") or 25})
    return _ok_or_error(s, b)


def tool_mind_memory_upsert(args: dict) -> dict:
    body = {"id": args.get("id"),
            "kind": args.get("kind") or "fact",
            "content": args.get("content"),
            "tags": args.get("tags") or [],
            "attribution_agent_id": args.get("attribution_agent_id"),
            "attribution_session_id": args.get("attribution_session_id"),
            "related_memory_ids": args.get("related_memory_ids") or []}
    if not body["content"]:
        return {"status": "error", "error": "content required"}
    s, b = _http("POST", "/memory", body=body)
    return _ok_or_error(s, b)


def tool_mind_projects_list(_args: dict) -> dict:
    s, b = _http("GET", "/projects")
    return _ok_or_error(s, b)


def tool_mind_register_agent(args: dict) -> dict:
    name = (args.get("name") or "").strip()
    if not name:
        return {"status": "error", "error": "name required"}
    s, b = _http("POST", "/agents/register",
                 body={"name": name, "kind": args.get("kind") or "agent"})
    return _ok_or_error(s, b)


def tool_mind_file_lease(args: dict) -> dict:
    body = {"path": args.get("path"),
            "session_id": args.get("session_id"),
            "duration_seconds": args.get("duration_seconds") or 60}
    if not body["path"] or body["session_id"] is None:
        return {"status": "error", "error": "path and session_id required"}
    s, b = _http("POST", "/files/lease", body=body)
    return _ok_or_error(s, b)


def tool_mind_file_release(args: dict) -> dict:
    body = {"session_id": args.get("session_id")}
    if args.get("path"):
        body["path"] = args["path"]
    if body["session_id"] is None:
        return {"status": "error", "error": "session_id required"}
    s, b = _http("POST", "/files/release", body=body)
    return _ok_or_error(s, b)


def tool_mind_file_leases(_args: dict) -> dict:
    s, b = _http("GET", "/files/leases")
    return _ok_or_error(s, b)


# ── tool registry + JSON schemas ──────────────────────────────────────────

TOOLS = [
    {
        "name": "mind_stats",
        "description": "Get unified-mind dashboard counts and 24h rollups (top agents, top projects).",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "fn": tool_mind_stats,
    },
    {
        "name": "mind_context",
        "description": "Get shared context (recent activity + relevant memory + active sessions) for a project and/or query. Use this at the start of a session to see what other agents have been doing.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Project slug (e.g. 'egon')"},
                "query":   {"type": "string", "description": "Free-text keywords to filter memory by"},
                "agent":   {"type": "string", "description": "Optional requesting agent name; enables delegated_task delivery"},
                "limit_activity": {"type": "integer", "default": 30},
                "limit_memory":   {"type": "integer", "default": 20},
                "budget_chars":   {"type": "integer", "default": 6000},
            },
        },
        "fn": tool_mind_context,
    },
    {
        "name": "mind_context_v2",
        "description": "Get a compact Context Broker v2 briefing capsule with ranked memory, recent activity, audit warnings, graph insights, and token-budget metadata.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Project slug (e.g. 'egon')"},
                "query": {"type": "string", "description": "Free-text query or current user request"},
                "agent": {"type": "string", "description": "Optional requesting agent name; enables delegated_task delivery"},
                "budget_chars": {"type": "integer", "default": 6000},
                "limit_activity": {"type": "integer", "default": 8},
                "limit_memory": {"type": "integer", "default": 8},
                "include_graph": {"type": "boolean", "default": True},
                "include_audit": {"type": "boolean", "default": True},
            },
        },
        "fn": tool_mind_context_v2,
    },
    {
        "name": "mind_agent_failure",
        "description": "Report an agent runtime failure. Quota or rate-limit shaped details automatically cool down that agent and reroute its pending/assigned orchestrator tasks.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_name": {"type": "string", "description": "Agent that hit the failure, e.g. claude-code, codex, antigravity, hermes"},
                "agent": {"type": "string", "description": "Alias for agent_name"},
                "detail": {"type": "string", "description": "Failure text, stderr, or API error detail"},
                "error": {"type": "string", "description": "Alias for detail"},
                "cooldown_seconds": {"type": "integer", "default": 1800},
            },
            "required": ["detail"],
        },
        "fn": tool_mind_agent_failure,
    },
    {
        "name": "mind_orchestrator_event",
        "description": "Append live progress/output/control evidence to an orchestrator task. Agents should call this while working and before final completion.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "agent_name": {"type": "string"},
                "agent": {"type": "string"},
                "event_type": {"type": "string", "description": "progress | output | decision | blocked | final | control_acknowledged"},
                "kind": {"type": "string", "description": "Alias for event_type"},
                "content": {"type": "string"},
                "message": {"type": "string", "description": "Alias for content"},
                "payload": {"type": "object"},
            },
            "required": ["task_id", "content"],
        },
        "fn": tool_mind_orchestrator_event,
    },
    {
        "name": "mind_orchestrator_control",
        "description": "Get or set a control action for an orchestrator task. Omit action to read current control; set action to pause, resume, stop, cancel, clarify, edit, or requeue.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "action": {"type": "string"},
                "note": {"type": "string"},
                "clarification": {"type": "string"},
                "replacement_desc": {"type": "string"},
                "prompt": {"type": "string", "description": "Alias for replacement_desc"},
                "agent_name": {"type": "string"},
                "agent": {"type": "string"},
            },
            "required": ["task_id"],
        },
        "fn": tool_mind_orchestrator_control,
    },
    {
        "name": "mind_orchestrator_events",
        "description": "Read recent orchestrator task events, optionally scoped to a task_id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "since_id": {"type": "integer", "default": 0},
                "limit": {"type": "integer", "default": 200},
            },
        },
        "fn": tool_mind_orchestrator_events,
    },
    {
        "name": "mind_agent_heartbeat",
        "description": "Report that an agent is alive, idle, polling, working, blocked, or finishing a specific orchestrator task.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_name": {"type": "string"},
                "agent": {"type": "string"},
                "task_id": {"type": "integer"},
                "status": {"type": "string"},
                "detail": {"type": "string"},
                "message": {"type": "string"},
            },
        },
        "fn": tool_mind_agent_heartbeat,
    },
    {
        "name": "mind_orchestrator_scheduler",
        "description": "Return orchestrator utilization status: active work, paused/clarification counts, cooldowns, stuck tasks, idle agents, and recent agent state.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "fn": tool_mind_orchestrator_scheduler,
    },
    {
        "name": "mind_orchestrator_mission",
        "description": "Return the shared mission-control view: agent states, current tasks, latest outputs, pending controls, cooldowns, leases, and recent orchestrator events.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit_events": {"type": "integer", "default": 80},
            },
        },
        "fn": tool_mind_orchestrator_mission,
    },
    {
        "name": "mind_orchestrator_autonomy",
        "description": "Inspect or update the always-on orchestrator autonomy loop. Safe mode requeues/reroutes stale work and wakes Hermes without inventing new tasks.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean"},
                "mode": {"type": "string", "description": "supervise_only or off"},
                "stuck_after_seconds": {"type": "integer"},
                "auto_requeue_stuck": {"type": "boolean"},
                "wake_hermes": {"type": "boolean"},
                "wake_agents": {"type": "boolean"},
                "provider_hooks": {"type": "boolean"},
            },
        },
        "fn": tool_mind_orchestrator_autonomy,
    },
    {
        "name": "mind_provider_hooks",
        "description": "Inspect or run native provider transcript hooks for Claude Code, Codex, and Antigravity. These hooks forward local transcript/protobuf activity and quota signals into the orchestrator.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "scan": {"type": "boolean", "default": False, "description": "When true, run one hook scan now; otherwise return status."},
            },
        },
        "fn": tool_mind_provider_hooks,
    },
    {
        "name": "mind_orchestrator_wake",
        "description": "Inspect or run the native wake bridge for orchestrator tasks. Starts Claude/Codex local runners when available and records queue-only handoffs for agents without a runner.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "scan": {"type": "boolean", "default": False, "description": "When true, run one wake scan now; otherwise return wake status."},
            },
        },
        "fn": tool_mind_orchestrator_wake,
    },
    {
        "name": "mind_activity_list",
        "description": "List recent activity rows across agents, filterable by project/agent/since.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "agent":   {"type": "string"},
                "since":   {"type": "integer", "description": "Unix timestamp"},
                "limit":   {"type": "integer", "default": 50},
            },
        },
        "fn": tool_mind_activity_list,
    },
    {
        "name": "mind_activity_append",
        "description": "Log a new activity row inside an open session. Use kinds like 'finding', 'decision', 'file_edit', 'hypothesis', 'note', 'error'.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "integer"},
                "kind":       {"type": "string"},
                "payload":    {"type": "object"},
                "ts":         {"type": "integer", "description": "Unix timestamp; defaults to now"},
            },
            "required": ["session_id", "kind"],
        },
        "fn": tool_mind_activity_append,
    },
    {
        "name": "mind_memory_search",
        "description": "Search the durable memory store. Returns rows matching kind/tags/free text. Use BEFORE committing to a course of action to check if past sessions already learned something relevant.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind":  {"type": "string", "description": "fact | preference | decision | skill | pattern"},
                "tags":  {"type": "string", "description": "Comma-separated tags to AND-match"},
                "q":     {"type": "string", "description": "Free-text content substring"},
                "limit": {"type": "integer", "default": 25},
            },
        },
        "fn": tool_mind_memory_search,
    },
    {
        "name": "mind_memory_upsert",
        "description": "Persist a durable fact / decision / preference / skill / pattern that future sessions of any agent should see. Cheap; default to writing memories whenever something non-obvious was learned.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id":      {"type": "integer", "description": "Omit to insert; provide to update"},
                "kind":    {"type": "string", "default": "fact"},
                "content": {"type": "string"},
                "tags":    {"type": "array", "items": {"type": "string"}},
                "related_memory_ids": {"type": "array", "items": {"type": "integer"}},
                "attribution_agent_id":   {"type": "integer"},
                "attribution_session_id": {"type": "integer"},
            },
            "required": ["content"],
        },
        "fn": tool_mind_memory_upsert,
    },
    {
        "name": "mind_projects_list",
        "description": "List every project the mind knows about (slug, name, description, root_path, status, timestamps).",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "fn": tool_mind_projects_list,
    },
    {
        "name": "mind_register_agent",
        "description": "Register self (or another agent) so future activity rows can attribute to a known agent id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "kind": {"type": "string", "default": "agent"},
            },
            "required": ["name"],
        },
        "fn": tool_mind_register_agent,
    },
    {
        "name": "mind_file_lease",
        "description": "Acquire a lock/lease on a file to coordinate edits across multiple agent sessions.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute file path to lock"},
                "session_id": {"type": "integer", "description": "Active session ID"},
                "duration_seconds": {"type": "integer", "default": 60, "description": "Lease duration in seconds"},
            },
            "required": ["path", "session_id"],
        },
        "fn": tool_mind_file_lease,
    },
    {
        "name": "mind_file_release",
        "description": "Release a file lease you previously acquired, or release all leases for this session if path is omitted.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute file path to release (optional)"},
                "session_id": {"type": "integer", "description": "Active session ID"},
            },
            "required": ["session_id"],
        },
        "fn": tool_mind_file_release,
    },
    {
        "name": "mind_file_leases",
        "description": "List all active file leases currently held by any agent.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "fn": tool_mind_file_leases,
    },
]


# ── JSON-RPC plumbing ─────────────────────────────────────────────────────

def _send(msg: dict) -> None:
    # ASCII JSON is accepted by every MCP client and avoids Windows code-page
    # crashes if a host ignores our stdio reconfiguration.
    sys.stdout.write(json.dumps(msg, ensure_ascii=True) + "\n")
    sys.stdout.flush()


def _result(req_id, result) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id, code: int, message: str, data=None) -> dict:
    err: dict = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def _handle_initialize(req_id, _params: dict) -> dict:
    return _result(req_id, {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
    })


def _handle_tools_list(req_id, _params: dict) -> dict:
    return _result(req_id, {
        "tools": [{"name": t["name"],
                   "description": t["description"],
                   "inputSchema": t["inputSchema"]} for t in TOOLS],
    })


def _handle_tools_call(req_id, params: dict) -> dict:
    name = (params or {}).get("name")
    args = (params or {}).get("arguments") or {}
    for t in TOOLS:
        if t["name"] == name:
            try:
                out = t["fn"](args)
            except Exception as e:
                out = {"status": "error",
                       "error": f"{type(e).__name__}: {e}"}
            return _result(req_id, {"content": [{
                "type": "text",
                "text": json.dumps(out, ensure_ascii=True, indent=2),
            }]})
    return _error(req_id, -32601, f"unknown tool: {name}")


def main() -> int:
    _configure_stdio()
    _log(f"starting; API_BASE={API_BASE}")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception as e:
            _send(_error(None, -32700, f"parse error: {e}"))
            continue
        req_id = req.get("id")
        method = req.get("method") or ""
        params = req.get("params") or {}
        try:
            if method == "initialize":
                _send(_handle_initialize(req_id, params))
            elif method == "initialized" or method == "notifications/initialized":
                # notification, no response
                pass
            elif method == "tools/list":
                _send(_handle_tools_list(req_id, params))
            elif method == "tools/call":
                _send(_handle_tools_call(req_id, params))
            elif method == "ping":
                _send(_result(req_id, {}))
            elif method == "shutdown":
                _send(_result(req_id, {}))
            elif method == "exit":
                return 0
            else:
                if req_id is not None:
                    _send(_error(req_id, -32601, f"method not found: {method}"))
        except Exception as e:
            _send(_error(req_id, -32603, f"internal error: {e}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
