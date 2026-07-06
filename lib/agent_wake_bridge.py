"""Wake bridge for orchestrator-controlled agent bodies.

Hermes is an in-process runner. Claude Code and Codex expose local
non-interactive CLIs, so the orchestrator can wake them by starting bounded
subprocesses with the delegated task contract. Antigravity exposes an agentapi
shim through its local language server; the bridge discovers that listener and
creates a native conversation handoff.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from lib.orchestration_engine import (
    ROOT,
    append_task_event,
    get_pending_task,
    get_tasks_status,
    is_auth_failure,
    is_quota_failure,
    record_agent_heartbeat,
    report_agent_failure,
    set_agent_cooldown,
    update_task_status,
)
from lib.orchestration_engine import DB_PATH

WAKE_DIR = ROOT / "state" / "agent_wake"
STATE_PATH = WAKE_DIR / "wake_state.json"
LOG_DIR = WAKE_DIR / "logs"
PROMPT_DIR = WAKE_DIR / "prompts"
RUNNER_AGENTS = ("claude-code", "codex", "antigravity")
QUEUE_ONLY_AGENTS: tuple[str, ...] = ()
WAKE_AGENTS = RUNNER_AGENTS + QUEUE_ONLY_AGENTS
MIN_RETRY_SECONDS = 180
ANTIGRAVITY_PROMPT_ARG_LIMIT = 18000
_ANTIGRAVITY_INFO_CACHE: dict[str, Any] = {"ts": 0, "items": []}


def _now() -> int:
    return int(time.time())


def _load_state() -> dict:
    try:
        if STATE_PATH.exists():
            body = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(body, dict):
                body.setdefault("tasks", {})
                body.setdefault("agents", {})
                return body
    except Exception:
        pass
    return {"tasks": {}, "agents": {}, "updated_at": 0}


def _save_state(state: dict) -> None:
    WAKE_DIR.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = _now()
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=True, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(STATE_PATH)


def _task_snapshot(task_id: int) -> dict | None:
    try:
        conn = sqlite3.connect(DB_PATH, timeout=4)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                """SELECT id, parent_prompt, agent_name, sub_task_desc, status, created_at, updated_at
                   FROM orchestrator_tasks WHERE id = ?""",
                (int(task_id),),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()
    except Exception:
        pass
    for task in get_tasks_status():
        if int(task.get("id") or 0) == int(task_id):
            return task
    return None


def _pending_tasks_by_agent() -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {agent: [] for agent in WAKE_AGENTS}
    try:
        conn = sqlite3.connect(DB_PATH, timeout=4)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """SELECT id, parent_prompt, agent_name, sub_task_desc, status, created_at, updated_at
                   FROM orchestrator_tasks
                   WHERE status = 'pending'
                   ORDER BY id ASC LIMIT 200"""
            ).fetchall()
        finally:
            conn.close()
        for row in rows:
            task = dict(row)
            agent = str(task.get("agent_name") or "").strip().lower()
            if agent in out:
                out[agent].append(task)
    except Exception:
        for task in get_tasks_status():
            agent = str(task.get("agent_name") or "").strip().lower()
            if agent in out and task.get("status") == "pending":
                out[agent].append(task)
    for tasks in out.values():
        tasks.sort(key=lambda t: int(t.get("id") or 0))
    return out


def _pid_running(pid: int | None) -> bool:
    if not pid:
        return False
    if sys.platform == "win32":
        try:
            res = subprocess.run(
                ["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=3,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return f'"{int(pid)}"' in (res.stdout or "")
        except Exception:
            return False
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False
    except Exception:
        return False


def _clip_file(path: str | None, limit: int = 4000) -> str:
    if not path:
        return ""
    try:
        p = Path(path)
        if not p.exists() or not p.is_file():
            return ""
        with p.open("rb") as f:
            if p.stat().st_size > limit:
                f.seek(max(0, p.stat().st_size - limit))
            data = f.read(limit)
        return data.decode("utf-8", errors="replace")[-limit:]
    except Exception:
        return ""


def _claude_credentials() -> dict[str, str]:
    """Env overrides so the orchestrator's HEADLESS `claude --print` authenticates.

    Bruno 2026-07-06: the on-disk OAuth token expired 2026-06-24, so every
    orchestrator-spawned claude-code 401'd (0 tokens) and ALL work dogpiled onto
    codex. The desktop app has a fresh in-memory token but the background service
    never sees it. Fix: inject a credential from egon-config.json.

    Cost-safe by design — prefers a SUBSCRIPTION token (`claude setup-token`,
    no per-token cost); only uses the paid API key if `llm.orchestrator_use_api_key`
    is explicitly true, so we never silently switch Bruno to metered billing.
    Returns {} when nothing is configured (claude-code simply stays unavailable,
    same as before — no surprise spend)."""
    try:
        import json as _json
        cfg = _json.loads((ROOT / "egon-config.json").read_text(encoding="utf-8"))
    except Exception:
        return {}
    llm = cfg.get("llm") or {}
    env_tok = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    tok = str(llm.get("claude_code_oauth_token") or env_tok or "").strip()
    if tok:
        return {"CLAUDE_CODE_OAUTH_TOKEN": tok}
    if llm.get("orchestrator_use_api_key"):
        key = str(llm.get("claude_api_key") or "").strip()
        if key.startswith("sk-ant-"):
            return {"ANTHROPIC_API_KEY": key}
    return {}


def _codex_executable() -> str | None:
    env_path = os.environ.get("CODEX_CLI_PATH")
    if env_path and Path(env_path).exists():
        return env_path
    local = Path.home() / "AppData" / "Local" / "OpenAI" / "Codex" / "bin"
    if local.exists():
        matches = sorted(local.glob("*/codex.exe"), key=lambda p: p.stat().st_mtime)
        if matches:
            return str(matches[-1])
    found = shutil.which("codex")
    if found and "WindowsApps" not in found:
        return found
    return found


def _antigravity_agentapi() -> str | None:
    env_path = os.environ.get("ANTIGRAVITY_AGENTAPI_PATH")
    if env_path and Path(env_path).exists():
        return env_path
    candidate = Path.home() / ".gemini" / "antigravity" / "bin" / "agentapi.bat"
    if candidate.exists():
        return str(candidate)
    found = shutil.which("agentapi")
    return found


def _antigravity_language_server_infos() -> list[dict[str, Any]]:
    if sys.platform != "win32":
        return []
    cached_at = int(_ANTIGRAVITY_INFO_CACHE.get("ts") or 0)
    if _now() - cached_at <= 15:
        return list(_ANTIGRAVITY_INFO_CACHE.get("items") or [])
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='language_server.exe'\" | "
        "Where-Object { $_.CommandLine -match 'antigravity' } | "
        "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
    )
    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return []
    raw = (res.stdout or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except Exception:
        return []
    if isinstance(parsed, dict):
        parsed = [parsed]
    out: list[dict[str, Any]] = []
    for item in parsed if isinstance(parsed, list) else []:
        try:
            pid = int(item.get("ProcessId"))
        except Exception:
            continue
        command = str(item.get("CommandLine") or "")
        token_match = re.search(r"--csrf_token\s+([0-9a-fA-F-]{20,})", command)
        out.append({
            "pid": pid,
            "csrf_token": token_match.group(1) if token_match else None,
            "command": command,
        })
    out = sorted(out, key=lambda item: int(item.get("pid") or 0))
    _ANTIGRAVITY_INFO_CACHE["ts"] = _now()
    _ANTIGRAVITY_INFO_CACHE["items"] = out
    return out


def _antigravity_language_server_pids() -> list[int]:
    return [int(item["pid"]) for item in _antigravity_language_server_infos() if item.get("pid")]


def _listening_local_ports_for_pid(pid: int) -> list[int]:
    try:
        res = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return []
    ports: list[int] = []
    pattern = re.compile(r"^\s*TCP\s+127\.0\.0\.1:(\d+)\s+\S+\s+LISTENING\s+(\d+)\s*$", re.I)
    for line in (res.stdout or "").splitlines():
        match = pattern.match(line)
        if not match:
            continue
        if int(match.group(2)) == int(pid):
            ports.append(int(match.group(1)))
    return sorted(set(ports), reverse=True)


def _antigravity_csrf_token() -> str | None:
    env_token = os.environ.get("ANTIGRAVITY_CSRF_TOKEN")
    if env_token:
        return env_token
    for info in _antigravity_language_server_infos():
        token = info.get("csrf_token")
        if token:
            return str(token)
    return None


def _antigravity_project_id() -> str:
    return os.environ.get("ANTIGRAVITY_PROJECT_ID") or str(ROOT)


def _probe_antigravity_address(
    agentapi: str,
    address: str,
    csrf_token: str | None = None,
    project_id: str | None = None,
) -> bool:
    env = os.environ.copy()
    env["ANTIGRAVITY_LS_ADDRESS"] = address
    if csrf_token:
        env["ANTIGRAVITY_CSRF_TOKEN"] = csrf_token
    if project_id:
        env["ANTIGRAVITY_PROJECT_ID"] = project_id
    if csrf_token:
        probe_cmd = [agentapi, "get-conversation-metadata", "00000000-0000-0000-0000-000000000000"]
    else:
        probe_cmd = [agentapi, "--help"]
    try:
        res = subprocess.run(
            probe_cmd,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=4,
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0,
        )
    except Exception:
        return False
    detail = f"{res.stdout or ''}\n{res.stderr or ''}".lower()
    if "antigravity_ls_address is not set" in detail:
        return False
    if csrf_token:
        return "trajectory not found" in detail or "conversation not found" in detail
    return "unknown command" in detail or "usage: agentapi" in detail or "available commands" in detail


def _antigravity_ls_address(
    agentapi: str | None = None,
    csrf_token: str | None = None,
    project_id: str | None = None,
) -> str | None:
    env_address = os.environ.get("ANTIGRAVITY_LS_ADDRESS")
    agentapi = agentapi or _antigravity_agentapi()
    if not agentapi:
        return None
    candidates: list[str] = []
    for pid in _antigravity_language_server_pids():
        for port in _listening_local_ports_for_pid(pid):
            candidates.append(f"http://127.0.0.1:{port}")
    candidates = list(dict.fromkeys(candidates))
    if env_address and env_address in candidates:
        return env_address
    if candidates and csrf_token and project_id:
        return candidates[0]
    for address in candidates:
        if _probe_antigravity_address(agentapi, address, csrf_token=csrf_token, project_id=project_id):
            return address
    if env_address and _probe_antigravity_address(agentapi, env_address, csrf_token=csrf_token, project_id=project_id):
        return env_address
    return candidates[0] if candidates else None


def _recent_antigravity_ports(limit: int = 12) -> list[int]:
    """Best-effort fallback for Antigravity restarts between process scans."""
    root = Path.home() / "AppData" / "Roaming" / "Antigravity"
    if not root.exists():
        return []
    ports: list[tuple[float, int]] = []
    pattern = re.compile(r"\b127\.0\.0\.1:(\d{4,5})\b")
    try:
        files = sorted(
            (p for p in root.rglob("*") if p.is_file() and p.stat().st_size < 2_000_000),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:80]
    except Exception:
        return []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")[-12000:]
        except Exception:
            continue
        for match in pattern.finditer(text):
            ports.append((path.stat().st_mtime, int(match.group(1))))
    seen: set[int] = set()
    out: list[int] = []
    for _, port in sorted(ports, reverse=True):
        if port in seen:
            continue
        seen.add(port)
        out.append(port)
        if len(out) >= limit:
            break
    return out


def _quota_summary(detail: str) -> str:
    text = str(detail or "")
    for line in text.splitlines():
        low = line.lower()
        if "rate_limit" in low or "out of extra usage" in low or "resets" in low or "429" in low:
            return line[:1000]
    return text[:1000]


def _agent_command(agent: str, prompt_path: Path, out_path: Path, err_path: Path) -> list[str] | None:
    if agent == "codex":
        codex = _codex_executable()
        if not codex:
            return None
        return [
            codex,
            "exec",
            "--json",
            "-C",
            str(ROOT),
            "-c",
            "approval_policy=\"never\"",
            "--sandbox",
            "danger-full-access",
            "-o",
            str(out_path.with_suffix(".last.txt")),
            "-",
        ]
    if agent == "claude-code":
        claude = shutil.which("claude")
        if not claude:
            candidate = Path.home() / ".local" / "bin" / "claude.exe"
            claude = str(candidate) if candidate.exists() else None
        if not claude:
            return None
        return [
            claude,
            "--print",
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            "acceptEdits",
            "--name",
            "egon-orchestrator",
        ]
    return None


def _task_capsule(agent: str, task: dict) -> str:
    """Shared-mind capsule for the task, embedded DIRECTLY in the wake prompt so
    every woken agent is deterministically fed from the consolidated mind —
    not merely asked to fetch it (Bruno 2026-07-03: 'you already ingest and
    feed from the all-around-complete mind now too, right?'). Best-effort."""
    try:
        from lib.mind_context_broker import build_context_capsule
        query = f"{task.get('parent_prompt') or ''} {task.get('sub_task_desc') or ''}"[:600]
        project = None
        try:
            from lib.egon_chat import _detect_project
            project = _detect_project(query)
        except Exception:
            pass
        cap = build_context_capsule(project=project, query=query,
                                    budget_chars=3000, limit_activity=5,
                                    limit_memory=5, include_graph=False,
                                    include_audit=False, agent=agent)
        if isinstance(cap, dict) and cap.get("status") == "ok":
            return (cap.get("briefing") or "").strip()[:3200]
    except Exception:
        pass
    return ""


def _runner_prompt(agent: str, task: dict) -> str:
    task_id = int(task["id"])
    capsule = _task_capsule(agent, task)
    capsule_block = (f"\nEGON SHARED-MIND CAPSULE (cross-agent context for this "
                     f"task — treat as ground truth):\n{capsule}\n" if capsule else "")
    return f"""You are {agent}, woken automatically by Egon's Orchestrator.

Task id: {task_id}
Parent prompt:
{task.get('parent_prompt') or ''}

Delegated task:
{task.get('sub_task_desc') or ''}
{capsule_block}
Contract:
1. The capsule above is your starting context; for MORE, call Egon's mind/context tool or GET /api/v1/mind/context/v2?project=egon&agent={agent}&query=<this task>.
2. Append a task event `started` before meaningful work.
3. Append progress/output events while working so the Orchestrator UI can show your written working response in near real time.
4. Before long steps, read /api/v1/mind/orchestrator/tasks/{task_id}/control and honor pause, stop/cancel, clarify, edit, and requeue.
5. When finished, call /api/v1/mind/orchestrator/complete with status completed or failed.
6. Write a durable memory with what changed, verification, and remaining risk if you learned or changed anything durable.

Use the existing repo and Egon mind contract. Do not start unrelated work.
"""


def _antigravity_prompt_arg(prompt: str, prompt_path: Path) -> str:
    if len(prompt) <= ANTIGRAVITY_PROMPT_ARG_LIMIT:
        return prompt
    head = prompt[:9000]
    tail = prompt[-6000:]
    return (
        f"{head}\n\n"
        f"[The full orchestrator handoff prompt is available at {prompt_path}. "
        "Open that file before doing project edits or marking the task complete.]\n\n"
        f"{tail}"
    )


def _antigravity_conversation_id(detail: str) -> str | None:
    try:
        data = json.loads(detail)
    except Exception:
        match = re.search(r'"conversationId"\s*:\s*"([^"]+)"', detail)
        return match.group(1) if match else None
    try:
        conv = data.get("response", {}).get("newConversation", {})
        value = conv.get("conversationId")
        return str(value) if value else None
    except Exception:
        return None


def _mark_queue_only(agent: str, task: dict, state: dict) -> dict:
    task_id = int(task["id"])
    tasks = state.setdefault("tasks", {})
    key = str(task_id)
    previous = tasks.get(key) or {}
    last_attempt = int(previous.get("last_attempt_at") or 0)
    if _now() - last_attempt < MIN_RETRY_SECONDS:
        return {"task_id": task_id, "agent": agent, "status": "waiting", "reason": "retry_suppressed"}
    entry = {
        "task_id": task_id,
        "agent": agent,
        "status": "queued_no_runner",
        "reason": "No discoverable non-interactive Antigravity runner/ANTIGRAVITY_LS_ADDRESS.",
        "last_attempt_at": _now(),
        "updated_at": _now(),
    }
    tasks[key] = entry
    state.setdefault("agents", {})[agent] = entry
    append_task_event(
        task_id,
        agent,
        "wake_queued",
        "Wake request queued, but this agent has no discoverable native runner in the current environment.",
        {"reason": entry["reason"]},
    )
    record_agent_heartbeat(agent, task_id, "wake_queued", entry["reason"])
    return dict(entry)


def _avail_ram_gb() -> float:
    """Available (not total!) physical RAM in GB — proper MEMORYSTATUSEX."""
    try:
        import ctypes

        class _MS(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_uint32), ("dwMemoryLoad", ctypes.c_uint32),
                        ("ullTotalPhys", ctypes.c_uint64), ("ullAvailPhys", ctypes.c_uint64),
                        ("ullTotalPageFile", ctypes.c_uint64), ("ullAvailPageFile", ctypes.c_uint64),
                        ("ullTotalVirtual", ctypes.c_uint64), ("ullAvailVirtual", ctypes.c_uint64),
                        ("ullAvailExtendedVirtual", ctypes.c_uint64)]

        ms = _MS()
        ms.dwLength = ctypes.sizeof(_MS)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))
        return ms.ullAvailPhys / 1e9
    except Exception:
        return 99.0


_ANTIGRAVITY_LS_EXE = (Path.home() / "AppData" / "Local" / "Programs" / "Antigravity"
                       / "resources" / "bin" / "language_server.exe")
# The Go language server is LIGHT (~200MB) — nothing like the Electron IDE.
_AUTOLAUNCH_RAM_FLOOR_GB = float(os.environ.get("EGON_ANTIGRAVITY_LAUNCH_RAM_GB", "1.0"))
_AUTOLAUNCH_COOLDOWN_S = int(os.environ.get("EGON_ANTIGRAVITY_LAUNCH_COOLDOWN_S", "300"))


def _ensure_antigravity_running(state: dict, task_id: int) -> tuple[bool, str]:
    """HEADLESS Antigravity (Bruno 2026-07-02: 'orchestrator shouldn't need to
    call up apps — use the CLIs and save RAM. Why isn't that true for
    Antigravity?'). It is now: language_server.exe supports --standalone
    --headless, so we spawn the LS DIRECTLY — no Electron IDE, ~200MB Go binary
    instead of ~1.5GB of app. Launch flags mirror what the IDE itself uses
    (from AppData/Roaming/Antigravity/logs/main.log). Our own --csrf_token
    appears on the spawned process's command line, so the existing discovery
    (_antigravity_language_server_infos / _antigravity_ls_address) picks it up
    with zero changes. Rate-limited; small RAM floor. Returns (ls_up, reason)."""
    if _antigravity_language_server_pids():
        return True, "already-running"
    if not _ANTIGRAVITY_LS_EXE.exists():
        return False, "language_server.exe not installed"
    avail = _avail_ram_gb()
    if avail < _AUTOLAUNCH_RAM_FLOOR_GB:
        return False, f"ram-floor ({avail:.1f}GB avail < {_AUTOLAUNCH_RAM_FLOOR_GB}GB)"
    last = float(state.get("antigravity_last_autolaunch") or 0)
    if _now() - last < _AUTOLAUNCH_COOLDOWN_S:
        return False, "launch-cooldown"
    state["antigravity_last_autolaunch"] = _now()
    _save_state(state)
    import uuid
    csrf = str(uuid.uuid4())
    cmd = [
        str(_ANTIGRAVITY_LS_EXE),
        "--standalone",
        "--headless=true",
        "--override_ide_name", "antigravity",
        "--subclient_type", "hub",
        "--override_user_agent_name", "antigravity",
        "--https_server_port", "0",
        "--csrf_token", csrf,
        "--app_data_dir", "antigravity",
        "--api_server_url", "https://generativelanguage.googleapis.com",
        "--cloud_code_endpoint", "https://daily-cloudcode-pa.googleapis.com",
    ]
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP | 0x08000000)
            if sys.platform == "win32" else 0,   # CREATE_NO_WINDOW — no console
            close_fds=True,
        )
    except Exception as exc:
        return False, f"ls-launch-error: {str(exc)[:80]}"
    state["antigravity_headless_ls_pid"] = proc.pid
    _save_state(state)
    append_task_event(task_id, "antigravity", "wake_info",
                      f"Spawned HEADLESS Antigravity language server (pid {proc.pid}, "
                      "~200MB — no IDE needed); waiting for it to bind.")
    boot_timeout = int(os.environ.get("EGON_ANTIGRAVITY_BOOT_TIMEOUT", "60"))
    deadline = _now() + boot_timeout
    while _now() < deadline:
        time.sleep(4)
        if proc.poll() is not None:
            return False, f"ls-exited rc={proc.returncode}"
        _ANTIGRAVITY_INFO_CACHE["ts"] = 0   # bust the 15s discovery cache
        if _listening_local_ports_for_pid(proc.pid):
            return True, "headless-ls-launched"
    return False, "ls-boot-timeout"


_LAST_DEFER_EVENT: dict[int, int] = {}   # task_id -> ts; damp event spam
_REROUTE_AFTER_S = int(os.environ.get("EGON_ANTIGRAVITY_REROUTE_S", "900"))


def _first_defer_ts(task_id: int) -> int | None:
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=8)
        row = conn.execute(
            "SELECT MIN(created_at) FROM orchestrator_task_events "
            "WHERE task_id=? AND event_type='wake_deferred'", (task_id,)).fetchone()
        conn.close()
        return int(row[0]) if row and row[0] else None
    except Exception:
        return None


def _defer_antigravity(task_id: int, reason: str, detail: dict | None = None) -> dict:
    """Defer instead of hard-fail: the task STAYS pending, so wake_pending_agents
    retries it automatically on every tick — e.g. the moment Bruno opens
    Antigravity or RAM frees up. Hard-failing on a closed app was the bug that
    killed every antigravity dispatch (tasks #37-#46). The wake tick re-marks
    the task 'assigned' on every retry (get_pending_task does that), so we damp
    the event log to one wake_deferred per 15 min per task."""
    # Grace window expired? Antigravity has no headless mode (its language
    # server demands an IDE stdin handshake — verified 2026-07-03), so work must
    # NOT wait forever on a closed window: reroute to the next available agent
    # (codex/claude-code/hermes, cooldown-aware). Bruno: "work never stalls".
    first = _first_defer_ts(task_id)
    if first and _now() - first > _REROUTE_AFTER_S:
        try:
            from lib.orchestration_engine import reassign_task_agent
            new_agent = reassign_task_agent(
                task_id, f"antigravity unavailable {_REROUTE_AFTER_S // 60}min ({reason})")
        except Exception:
            new_agent = None
        if new_agent:
            _LAST_DEFER_EVENT.pop(task_id, None)
            return {"agent": "antigravity", "task_id": task_id,
                    "status": "rerouted", "reason": f"→ {new_agent}"}
    if _now() - _LAST_DEFER_EVENT.get(task_id, 0) > 900:
        _LAST_DEFER_EVENT[task_id] = _now()
        append_task_event(
            task_id, "antigravity", "wake_deferred",
            f"Antigravity runner unavailable ({reason}) — task stays queued; "
            "retries automatically, and reroutes to another agent after "
            f"{_REROUTE_AFTER_S // 60}min.",
            detail or {})
    update_task_status(task_id, "pending")
    record_agent_heartbeat("antigravity", task_id, "wake_deferred", reason)
    return {"agent": "antigravity", "task_id": task_id, "status": "deferred",
            "reason": reason}


def _start_antigravity_runner(task: dict, state: dict) -> dict:
    agent = "antigravity"
    task_id = int(task["id"])
    prompt = _runner_prompt(agent, task)
    PROMPT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    prompt_path = PROMPT_DIR / f"task-{task_id}-{agent}.md"
    out_path = LOG_DIR / f"task-{task_id}-{agent}.jsonl"
    err_path = LOG_DIR / f"task-{task_id}-{agent}.err.log"
    prompt_path.write_text(prompt, encoding="utf-8")

    agentapi = _antigravity_agentapi()
    if not agentapi:
        # agentapi ships with the app — a missing runner means the app isn't
        # installed/running; defer so the task survives until it is.
        return _defer_antigravity(task_id, "agentapi_missing")
    # The app must be RUNNING for its language server to answer. If it's not,
    # try to launch it (RAM-gated, rate-limited); otherwise defer — never
    # hard-fail a task just because the IDE window was closed. 2026-07-02.
    ls_up, why = _ensure_antigravity_running(state, task_id)
    if not ls_up:
        return _defer_antigravity(task_id, why, {"agentapi": agentapi})
    csrf_token = _antigravity_csrf_token()
    project_id = _antigravity_project_id()
    address = _antigravity_ls_address(agentapi, csrf_token=csrf_token, project_id=project_id)
    if not address:
        return _defer_antigravity(
            task_id, "address_missing (app up, LS port not discovered yet)",
            {"agentapi": agentapi})

    model = os.environ.get("EGON_ANTIGRAVITY_MODEL", "pro").strip() or "pro"
    cmd = [
        agentapi,
        "new-conversation",
        f"--model={model}",
        _antigravity_prompt_arg(prompt, prompt_path),
    ]
    env = os.environ.copy()
    env["ANTIGRAVITY_LS_ADDRESS"] = address
    if csrf_token:
        env["ANTIGRAVITY_CSRF_TOKEN"] = csrf_token
    env["ANTIGRAVITY_PROJECT_ID"] = project_id
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env.setdefault("EGON_MCP_AGENT", agent)
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    update_task_status(task_id, "assigned")
    append_task_event(
        task_id,
        agent,
        "wake_started",
        "Starting native Antigravity agentapi handoff.",
        {
            "runner": agentapi,
            "address": address,
            "model": model,
            "project_id": project_id,
            "stdout_path": str(out_path),
            "stderr_path": str(err_path),
            "handoff_mode": "launch_only",
        },
    )
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            creationflags=creationflags,
        )
        timeout = int(os.environ.get("EGON_ANTIGRAVITY_LAUNCH_TIMEOUT", "90"))
        stdout_text, stderr_text = proc.communicate(timeout=max(10, timeout))
        out_path.write_text(stdout_text or "", encoding="utf-8")
        err_path.write_text(stderr_text or "", encoding="utf-8")
    except subprocess.TimeoutExpired as exc:
        try:
            proc.kill()
        except Exception:
            pass
        out_path.write_text(exc.stdout or "", encoding="utf-8", errors="replace")
        err_path.write_text((exc.stderr or "") + "\nTimed out waiting for Antigravity agentapi handoff.", encoding="utf-8", errors="replace")
        append_task_event(task_id, agent, "wake_failed", "Timed out waiting for Antigravity agentapi handoff.")
        update_task_status(task_id, "failed")
        return {"agent": agent, "task_id": task_id, "status": "failed", "reason": "handoff_timeout"}
    except Exception as exc:
        append_task_event(task_id, agent, "wake_failed", f"{type(exc).__name__}: {str(exc)[:500]}")
        update_task_status(task_id, "failed")
        return {"agent": agent, "task_id": task_id, "status": "failed", "reason": f"{type(exc).__name__}: {exc}"}

    detail = ((stdout_text or "") + "\n" + (stderr_text or "")).strip()
    conversation_id = _antigravity_conversation_id(detail)
    entry = {
        "task_id": task_id,
        "agent": agent,
        "status": "handoff_created" if conversation_id else "handoff_error",
        "handoff_mode": "launch_only",
        "pid": proc.pid,
        "returncode": proc.returncode,
        "runner": agentapi,
        "model": model,
        "address": address,
        "project_id": project_id,
        "conversation_id": conversation_id,
        "prompt_path": str(prompt_path),
        "stdout_path": str(out_path),
        "stderr_path": str(err_path),
        "started_at": _now(),
        "updated_at": _now(),
    }
    state.setdefault("tasks", {})[str(task_id)] = entry
    state.setdefault("agents", {})[agent] = entry
    if is_quota_failure(detail):
        report_agent_failure(agent, _quota_summary(detail))
        entry["status"] = "quota_rerouted"
        entry["final_task_status"] = "rerouted_or_cooldown"
        state.setdefault("tasks", {})[str(task_id)] = entry
        state.setdefault("agents", {})[agent] = entry
        append_task_event(
            task_id,
            agent,
            "wake_exit",
            detail,
            {"pid": proc.pid, "returncode": proc.returncode, "final_status": "rerouted_or_cooldown"},
        )
    elif is_auth_failure(detail):
        set_agent_cooldown(agent, 1800, "auth failure (401) — waiting for "
                           "credentials/quota to recover")
        update_task_status(task_id, "pending")
        entry["status"] = "auth_requeued"
        entry["final_task_status"] = "requeued_auth"
        state.setdefault("tasks", {})[str(task_id)] = entry
        state.setdefault("agents", {})[agent] = entry
        append_task_event(
            task_id, agent, "wake_auth_failed",
            "Antigravity handoff hit a 401 authentication failure — agent "
            "cooled down 30min, task requeued (not failed).",
            {"pid": proc.pid, "returncode": proc.returncode})
    elif conversation_id:
        append_task_event(
            task_id,
            agent,
            "wake_handoff_created",
            detail or "Antigravity native handoff created a conversation.",
            {
                "pid": proc.pid,
                "returncode": proc.returncode,
                "conversation_id": conversation_id,
                "final_status": "assigned",
            },
        )
        record_agent_heartbeat(agent, task_id, "wake_handoff_created", f"conversation_id={conversation_id}")
    else:
        update_task_status(task_id, "failed")
        entry["final_task_status"] = "failed"
        state.setdefault("tasks", {})[str(task_id)] = entry
        state.setdefault("agents", {})[agent] = entry
        append_task_event(
            task_id,
            agent,
            "wake_exit",
            detail or "Antigravity agentapi handoff exited without a conversation id.",
            {"pid": proc.pid, "returncode": proc.returncode, "final_status": "failed"},
        )
    return dict(entry)


def _start_runner(agent: str, state: dict) -> dict:
    task = get_pending_task(agent)
    if not task:
        return {"agent": agent, "status": "idle"}
    if agent == "antigravity":
        return _start_antigravity_runner(task, state)
    task_id = int(task["id"])
    prompt = _runner_prompt(agent, task)
    PROMPT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    prompt_path = PROMPT_DIR / f"task-{task_id}-{agent}.md"
    out_path = LOG_DIR / f"task-{task_id}-{agent}.jsonl"
    err_path = LOG_DIR / f"task-{task_id}-{agent}.err.log"
    prompt_path.write_text(prompt, encoding="utf-8")
    cmd = _agent_command(agent, prompt_path, out_path, err_path)
    if not cmd:
        append_task_event(
            task_id,
            agent,
            "wake_failed",
            f"No local non-interactive runner found for {agent}.",
        )
        update_task_status(task_id, "failed")
        return {"agent": agent, "task_id": task_id, "status": "failed", "reason": "runner_missing"}

    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env.setdefault("EGON_MCP_AGENT", agent)
    if agent == "claude-code":
        # Headless claude-code needs an explicit credential — the background
        # service can't see the desktop app's in-memory token, and the on-disk
        # one expires. No credential configured → skip the spawn entirely so it
        # doesn't 401 and burn a wake slot (Bruno 2026-07-06).
        creds = _claude_credentials()
        if not creds:
            append_task_event(
                task_id, agent, "wake_deferred",
                "claude-code has no headless credential — set llm."
                "claude_code_oauth_token (run `claude setup-token`) in "
                "egon-config.json. Task stays queued; not failed.")
            update_task_status(task_id, "pending")
            record_agent_heartbeat(agent, task_id, "wake_deferred", "no headless credential")
            return {"agent": agent, "task_id": task_id, "status": "deferred",
                    "reason": "no_headless_credential"}
        env.update(creds)
    creationflags = 0
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        stdin = prompt_path.open("r", encoding="utf-8")
        stdout = out_path.open("w", encoding="utf-8", errors="replace")
        stderr = err_path.open("w", encoding="utf-8", errors="replace")
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            creationflags=creationflags,
        )
        stdin.close()
        stdout.close()
        stderr.close()
    except Exception as exc:
        append_task_event(task_id, agent, "wake_failed", f"{type(exc).__name__}: {str(exc)[:500]}")
        update_task_status(task_id, "failed")
        return {"agent": agent, "task_id": task_id, "status": "failed", "reason": f"{type(exc).__name__}: {exc}"}

    entry = {
        "task_id": task_id,
        "agent": agent,
        "status": "running",
        "pid": proc.pid,
        "runner": cmd[0],
        "prompt_path": str(prompt_path),
        "stdout_path": str(out_path),
        "stderr_path": str(err_path),
        "started_at": _now(),
        "updated_at": _now(),
    }
    state.setdefault("tasks", {})[str(task_id)] = entry
    state.setdefault("agents", {})[agent] = entry
    update_task_status(task_id, "assigned")
    append_task_event(
        task_id,
        agent,
        "wake_started",
        f"Started native {agent} runner pid={proc.pid}.",
        {"pid": proc.pid, "runner": cmd[0], "stdout_path": str(out_path), "stderr_path": str(err_path)},
    )
    record_agent_heartbeat(agent, task_id, "wake_started", f"pid={proc.pid}")
    return dict(entry)


def poll_wake_processes(state: dict | None = None) -> dict:
    state = state or _load_state()
    changed = False
    checked = 0
    finished = 0
    for key, entry in list((state.get("tasks") or {}).items()):
        if entry.get("status") != "running":
            continue
        checked += 1
        task_id = int(entry.get("task_id") or key)
        agent = entry.get("agent")
        if _pid_running(entry.get("pid")):
            continue
        task = _task_snapshot(task_id) or {}
        stdout_tail = _clip_file(entry.get("stdout_path"))
        stderr_tail = _clip_file(entry.get("stderr_path"))
        detail = (stdout_tail + "\n" + stderr_tail).strip()[-4000:]
        final_status = task.get("status")
        if final_status not in {"completed", "failed", "cancelled"}:
            if is_quota_failure(detail):
                report_agent_failure(agent, _quota_summary(detail))
                final_status = "rerouted_or_cooldown"
            elif is_auth_failure(detail):
                # 401s are transient (expired OAuth / quota lockout) — task #43
                # was killed by one. Cool the agent down and REQUEUE the task so
                # it runs once credentials recover. Bruno 2026-07-02.
                set_agent_cooldown(agent, 1800, "auth failure (401) — waiting "
                                   "for credentials/quota to recover")
                update_task_status(task_id, "pending")
                append_task_event(
                    task_id, agent, "wake_auth_failed",
                    "Runner hit a 401 authentication failure — agent cooled "
                    "down 30min, task requeued (not failed).",
                    {"pid": entry.get("pid")})
                final_status = "requeued_auth"
            elif entry.get("handoff_mode") == "launch_only":
                final_status = final_status or "assigned"
                append_task_event(
                    task_id,
                    agent,
                    "wake_handoff_created",
                    detail or f"{agent} native handoff process exited after creating the conversation.",
                    {"pid": entry.get("pid"), "final_status": final_status},
                )
                entry["status"] = "handoff_created"
                entry["final_task_status"] = final_status
                entry["updated_at"] = _now()
                state.setdefault("agents", {})[agent] = dict(entry)
                changed = True
                finished += 1
                continue
            else:
                update_task_status(task_id, "failed")
                final_status = "failed"
        append_task_event(
            task_id,
            agent,
            "wake_exit",
            detail or f"{agent} native runner exited.",
            {"pid": entry.get("pid"), "final_status": final_status},
        )
        entry["status"] = "exited"
        entry["final_task_status"] = final_status
        entry["updated_at"] = _now()
        state.setdefault("agents", {})[agent] = dict(entry)
        changed = True
        finished += 1
    if changed:
        _save_state(state)
    return {"status": "ok", "checked": checked, "finished": finished}


def wake_pending_agents(max_per_tick: int = 2) -> dict:
    state = _load_state()
    poll = poll_wake_processes(state)
    started: list[dict] = []
    queued: list[dict] = []
    pending = _pending_tasks_by_agent()

    slots = max(0, int(max_per_tick))
    for agent in RUNNER_AGENTS:
        if slots <= 0:
            break
        if not pending.get(agent):
            continue
        agent_state = (state.get("agents") or {}).get(agent) or {}
        if agent_state.get("status") == "running" and _pid_running(agent_state.get("pid")):
            continue
        result = _start_runner(agent, state)
        if result.get("status") == "running":
            slots -= 1
        started.append(result)

    for agent in QUEUE_ONLY_AGENTS:
        for task in pending.get(agent, [])[:1]:
            queued.append(_mark_queue_only(agent, task, state))

    _save_state(state)
    return {
        "status": "ok",
        "started": started,
        "queued": queued,
        "poll": poll,
        "state": wake_status(state),
    }


def wake_status(state: dict | None = None) -> dict:
    state = state or _load_state()
    tasks = state.get("tasks") or {}
    agents = state.get("agents") or {}
    for entry in list(tasks.values()) + list(agents.values()):
        if not isinstance(entry, dict) or entry.get("status") != "exited":
            continue
        try:
            task = _task_snapshot(int(entry.get("task_id") or 0)) or {}
        except Exception:
            task = {}
        task_status = task.get("status")
        if task_status in {"completed", "failed", "cancelled"}:
            entry["final_task_status"] = task_status
    active = []
    for entry in tasks.values():
        if entry.get("status") == "running" and _pid_running(entry.get("pid")):
            active.append(entry)
    return {
        "status": "ok",
        "wake_dir": str(WAKE_DIR),
        "agents": agents,
        "tasks": tasks,
        "active_runners": active,
        "runner_agents": list(RUNNER_AGENTS),
        "queue_only_agents": list(QUEUE_ONLY_AGENTS),
        "updated_at": state.get("updated_at"),
    }
