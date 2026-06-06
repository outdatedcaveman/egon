"""Mind hook helper — invoked by Claude Code / Codex / Cursor hooks.

This is the bridge between an agent's hook system and Egon's mind API.
Each hook fires this script with a sub-command and pipes the event
JSON in via stdin; we POST it to localhost:8000/api/v1/mind/* and exit.

Hooks register one line in their `settings.json`. Example for Claude
Code (~/.claude/settings.local.json or ~/.claude/settings.json):

  {
    "hooks": {
      "Stop":              [{"hooks": [{"type": "command",
        "command": "python C:/Users/bruno/Claude Code/egon/scripts/mind_hook.py stop"}]}],
      "UserPromptSubmit":  [{"hooks": [{"type": "command",
        "command": "python C:/Users/bruno/Claude Code/egon/scripts/mind_hook.py prompt"}]}],
      "PostToolUse":       [{"hooks": [{"type": "command",
        "command": "python C:/Users/bruno/Claude Code/egon/scripts/mind_hook.py tool"}]}]
    }
  }

Sub-commands:
  stop     — POST sessions/end with the session summary (if present)
  prompt   — POST activity (kind=user_prompt) + emit hookSpecificOutput
             to inject mind context into the prompt (additionalContext).
  tool     — POST activity (kind=tool_<name>) with the tool input.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

MIND_API = os.environ.get("EGON_MIND_API", "http://127.0.0.1:8000/api/v1/mind")
AGENT_NAME = os.environ.get("EGON_HOOK_AGENT", "claude-code")
TIMEOUT_S = 3.0
ROOT = Path(__file__).resolve().parent.parent
SERVICE_SCRIPT = ROOT / "scripts" / "mind_service.py"
_LAST_AUTOSTART_AT = 0.0


def _read_event() -> dict:
    try:
        return json.load(sys.stdin)
    except Exception:
        return {}


def _post(path: str, body: dict) -> dict | None:
    try:
        r = requests.post(f"{MIND_API}{path}", json=body, timeout=TIMEOUT_S)
        if r.status_code == 200:
            return r.json()
    except Exception:
        if _start_mind_service() and _mind_ready(timeout=8.0):
            try:
                r = requests.post(f"{MIND_API}{path}", json=body, timeout=TIMEOUT_S)
                if r.status_code == 200:
                    return r.json()
            except Exception:
                return None
    return None


def _get(path: str, params: dict | None = None) -> dict | None:
    try:
        r = requests.get(f"{MIND_API}{path}", params=params or {}, timeout=TIMEOUT_S)
        if r.status_code == 200:
            return r.json()
    except Exception:
        if _start_mind_service() and _mind_ready(timeout=8.0):
            try:
                r = requests.get(f"{MIND_API}{path}", params=params or {}, timeout=TIMEOUT_S)
                if r.status_code == 200:
                    return r.json()
            except Exception:
                return None
    return None


def _mind_ready(timeout: float = 1.0) -> bool:
    try:
        r = requests.get(f"{MIND_API}/stats", timeout=timeout)
        if r.status_code != 200:
            return False
        body = r.json()
        return isinstance(body, dict) and body.get("status") == "ok"
    except Exception:
        return False


def _service_python() -> str:
    pyw = ROOT / ".venv" / "Scripts" / "pythonw.exe"
    if pyw.exists():
        return str(pyw)
    py = ROOT / ".venv" / "Scripts" / "python.exe"
    if py.exists():
        return str(py)
    return sys.executable


def _start_mind_service() -> bool:
    global _LAST_AUTOSTART_AT
    if _mind_ready() or not SERVICE_SCRIPT.exists():
        return True
    now = time.time()
    if now - _LAST_AUTOSTART_AT < 5:
        return True
    _LAST_AUTOSTART_AT = now

    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
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
            | 0x00000008
        )
    try:
        subprocess.Popen([_service_python(), str(SERVICE_SCRIPT)], **kwargs)
        return True
    except Exception:
        return False


def _ensure_session(event: dict) -> int | None:
    """Get-or-create a mind session for this Claude Code session, using
    the session_id from the event payload as the external_id."""
    session_uuid = event.get("session_id") or event.get("sessionId")
    if not session_uuid:
        return None
    project = _project_from_cwd(event.get("cwd"))
    r = _post("/sessions/start",
              {"agent": AGENT_NAME, "external_id": session_uuid,
               "project": project, "started_at": int(time.time())})
    return (r or {}).get("id")


def _project_from_cwd(cwd: str | None) -> str | None:
    """Resolve cwd to a canonical project slug. Importing
    lib.mind_project_resolver is lazy so the hook still works even if
    the user runs it from an environment without the egon source tree
    on sys.path."""
    if not cwd:
        return None
    try:
        import sys as _sys
        from pathlib import Path as _Path
        # Best-effort: put egon root on sys.path so the resolver is importable.
        egon_root = _Path(__file__).resolve().parent.parent
        if str(egon_root) not in _sys.path:
            _sys.path.insert(0, str(egon_root))
        from lib.mind_project_resolver import canonical_slug
        return canonical_slug(cwd)
    except Exception:
        # Fallback to old heuristic
        try:
            return Path(cwd).name.lower() or None
        except Exception:
            return None


# ── sub-commands ───────────────────────────────────────────────────────────

def _iso_to_epoch(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def _tool_names_from_content(content) -> list[str]:
    tools: list[str] = []
    if not isinstance(content, list):
        return tools
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            name = block.get("name")
            if name:
                tools.append(str(name))
    return tools


def _post_transcript_turns(sid: int, transcript_path: str | None) -> int:
    if not transcript_path:
        return 0
    path = Path(transcript_path)
    if not path.exists() or not path.is_file():
        return 0
    written = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    event = json.loads(line)
                except Exception:
                    continue
                if event.get("type") != "assistant" and event.get("role") != "assistant":
                    continue
                msg = event.get("message") or {}
                usage = msg.get("usage") or {}
                model = msg.get("model")
                if not usage or not model:
                    continue
                r = _post("/ledger/turns", {
                    "session_id": sid,
                    "ts": _iso_to_epoch(event.get("timestamp")) or int(time.time()),
                    "model": model,
                    "usage": usage,
                    "tools": _tool_names_from_content(msg.get("content")),
                })
                if r and r.get("status") == "ok":
                    written += 1
    except Exception:
        return written
    return written


def _is_file_edit_tool(tool_name: str) -> bool:
    name_lower = tool_name.lower()
    return name_lower in {
        "edit", "write", "todowrite", "notebookedit", 
        "replace_file_content", "multi_replace_file_content", "write_to_file", "write_file"
    }


def cmd_stop() -> int:
    event = _read_event()
    sid = _ensure_session(event)
    if sid is None:
        return 0
    ledger_rows = _post_transcript_turns(sid, event.get("transcript_path"))
    if ledger_rows:
        _post("/activity", {"session_id": sid, "kind": "token_ledger_ingest",
                            "payload": {"rows": ledger_rows,
                                        "source": event.get("transcript_path")},
                            "ts": int(time.time())})
    summary = (event.get("stop_hook_active")
               or event.get("response", "")[:1500]
               or event.get("transcript_path"))
    _post("/sessions/end",
          {"session_id": sid, "summary": str(summary)[:2000] if summary else None,
           "ended_at": int(time.time())})
    # Release all leases for this session
    _post("/files/release", {"session_id": sid})
    return 0


def cmd_prompt() -> int:
    event = _read_event()
    sid = _ensure_session(event)
    prompt = event.get("prompt") or event.get("user_message") or ""
    project = _project_from_cwd(event.get("cwd"))
    # 1. Log the prompt as activity
    if sid is not None:
        _post("/activity", {"session_id": sid, "kind": "user_prompt",
                            "payload": {"text_preview": str(prompt)[:1000]},
                            "ts": int(time.time())})
    # 2. Fetch shared context and inject it into the next model turn.
    #    Claude Code reads the JSON we emit on stdout; the
    #    hookSpecificOutput.additionalContext field prepends to the user
    #    message under a <shared-context> block.
    ctx = _get("/context/v2", {
        "project": project,
        "query": prompt[:300],
        "budget_chars": 5500,
        "limit_activity": 8,
        "limit_memory": 8,
    })
    if not ctx or ctx.get("status") != "ok":
        ctx = _get("/context", {"project": project, "query": prompt[:200]})
    if not ctx or ctx.get("status") != "ok":
        return 0
    if sid is not None:
        sections = ctx.get("sections") or {}
        _post("/activity", {"session_id": sid, "kind": "mind_context",
                            "payload": {
                                "project": project,
                                "query_preview": str(prompt)[:300],
                                "broker_version": ctx.get("version", "v1"),
                                "activity_count": len(sections.get("recent_activity") or ctx.get("recent_activity") or []),
                                "memory_count": len(sections.get("durable_memory") or ctx.get("relevant_memory") or []),
                                "structural_count": len(sections.get("structural_insights") or ctx.get("structural_insights") or []),
                                "approx_tokens": (ctx.get("budget") or {}).get("approx_tokens"),
                            },
                            "ts": int(time.time())})
    briefing = ctx.get("briefing")
    if briefing:
        additional = ("=== Shared mind context capsule (Egon Context Broker v2) ===\n"
                      + briefing)
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": additional,
        }}), flush=True)
        return 0

    snippets = []
    for a in (ctx.get("recent_activity") or [])[:6]:
        snippets.append(f"- [{a.get('agent_name')}] {a.get('kind')}: "
                        f"{json.dumps(a.get('payload') or {}, ensure_ascii=False)[:160]}")
    for m in (ctx.get("relevant_memory") or [])[:5]:
        snippets.append(f"- memory[{m.get('kind')}]: {(m.get('content') or '')[:200]}")
    if not snippets:
        return 0
    additional = ("=== Shared mind context (from other agent sessions) ===\n"
                  + "\n".join(snippets))
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": additional,
    }}), flush=True)
    return 0


def cmd_pretool() -> int:
    event = _read_event()
    tool_name = event.get("tool_name") or event.get("toolName") or "unknown"
    if not _is_file_edit_tool(tool_name):
        return 0
    
    tool_input = event.get("tool_input") or event.get("toolInput") or {}
    path = None
    for key in ["path", "filepath", "filename", "TargetFile", "file"]:
        if key in tool_input and isinstance(tool_input[key], str):
            path = tool_input[key]
            break
            
    if not path:
        return 0
        
    cwd = event.get("cwd") or os.getcwd()
    if not os.path.isabs(path):
        path = os.path.join(cwd, path)
    path = os.path.abspath(path).replace("\\", "/")
        
    sid = _ensure_session(event)
    if sid is None:
        return 0
        
    r = _post("/files/lease", {"path": path, "session_id": sid, "duration_seconds": 60})
    if r and r.get("status") == "error" and r.get("error") == "locked":
        holding_agent = r.get("holding_agent", "another agent")
        expires_in = r.get("expires_in", 0)
        sys.stderr.write(
            f"\n[Egon Lock Error] File '{path}' is currently locked by {holding_agent} "
            f"in session {r.get('holding_session')}.\n"
            f"Lock will expire in {expires_in} seconds.\n"
        )
        sys.stderr.flush()
        return 2  # Exit code 2 blocks tool execution in Claude Code
    if r and r.get("status") == "ok":
        _post("/activity", {"session_id": sid, "kind": "file_lease",
                            "payload": {"path": path, "tool": tool_name},
                            "ts": int(time.time())})
        
    return 0


def cmd_tool() -> int:
    event = _read_event()
    sid = _ensure_session(event)
    if sid is None:
        return 0
    tool_name = event.get("tool_name") or event.get("toolName") or "unknown"
    payload = {
        "tool": tool_name,
        "input_preview": _trim(event.get("tool_input") or {}),
        "response_preview": _trim(event.get("tool_response") or {}),
    }
    _post("/activity", {"session_id": sid,
                        "kind": f"tool_{tool_name}",
                        "payload": payload,
                        "ts": int(time.time())})
                        
    # Release lease on tool success/failure if it was a file edit tool
    if _is_file_edit_tool(tool_name):
        tool_input = event.get("tool_input") or event.get("toolInput") or {}
        path = None
        for key in ["path", "filepath", "filename", "TargetFile", "file"]:
            if key in tool_input and isinstance(tool_input[key], str):
                path = tool_input[key]
                break
        if path:
            cwd = event.get("cwd") or os.getcwd()
            if not os.path.isabs(path):
                path = os.path.join(cwd, path)
            path = os.path.abspath(path).replace("\\", "/")
            _post("/files/release", {"path": path, "session_id": sid})
            _post("/activity", {"session_id": sid, "kind": "file_release",
                                "payload": {"path": path, "tool": tool_name},
                                "ts": int(time.time())})
            
    return 0


def _trim(x, max_len: int = 500) -> str:
    try:
        s = json.dumps(x, ensure_ascii=False)
    except Exception:
        s = str(x)
    return s[:max_len]


# ── entrypoint ──────────────────────────────────────────────────────────────

CMDS = {"stop": cmd_stop, "prompt": cmd_prompt, "pretool": cmd_pretool, "tool": cmd_tool}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in CMDS:
        print("usage: mind_hook.py {stop|prompt|pretool|tool}", file=sys.stderr)
        return 2
    try:
        return CMDS[sys.argv[1]]()
    except Exception as e:
        # Never break the agent — log to stderr, exit 0 so the hook
        # doesn't block the user's session.
        print(f"mind_hook error: {e}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
