"""Hermes Autonomous Task Runner — executes background tasks delegated by the Orchestrator.

Translates natural language sub-task instructions into local executable scripts,
runs them silently without console popups, logs execution outputs, and records results.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

# Side-effect import to silences all subprocess.Popen calls on Windows
try:
    import lib.silent_subprocess  # noqa: F401
except ImportError:
    pass

from lib.orchestration_engine import (
    acknowledge_task_control,
    append_task_event,
    get_pending_task,
    get_task_control,
    is_quota_failure,
    report_agent_failure,
    record_agent_heartbeat,
    update_task_status,
    ROOT,
)

LOG_FILE = ROOT / "logs" / "hermes-tasks.log"
_RUNNER_LOCK = threading.Lock()


def translate_task(task_desc: str) -> str:
    """Translate natural language subtask description to local command line.
    Uses rule-based heuristics and local Qwen LLM as fallback.
    """
    desc_lower = task_desc.lower()

    explicit_marker = "hermes-command:"
    if explicit_marker in desc_lower:
        start = desc_lower.index(explicit_marker) + len(explicit_marker)
        command = task_desc[start:].strip().splitlines()[0].strip().strip("`")
        if command:
            return command
    
    # 1. Rule-based classification
    if "audit" in desc_lower or "introspection" in desc_lower:
        return "python scripts/introspection.py"
    if "snapshot" in desc_lower:
        return "python scripts/pass.py --kind snapshots"
    if "mirror" in desc_lower:
        return "python scripts/pass.py --kind mirror"
    if "clean" in desc_lower or "db" in desc_lower or "cleanup" in desc_lower:
        return "python scripts/panop_zotero_cleanup.py"

    # 2. Local LLM Translation
    try:
        from lib.synthesis import _config
        cfg = _config()
        system_content = (
            "You are Egon's command translation engine. Translate the following natural language task description "
            "into a single Windows PowerShell command or Python script execution that should be run locally.\n"
            "Allowed python scripts are under scripts/ (e.g., scripts/introspection.py, scripts/pass.py, etc.).\n"
            "Output ONLY the exact command. No markdown, no backticks, no conversational preamble."
        )
        
        body = json.dumps({
            "model": cfg["model"],
            "max_tokens": 150,
            "temperature": 0.1,
            "messages": [
                {"role": "system", "content": system_content},
                {"role": "user", "content": f"Task: {task_desc}"},
            ],
        }).encode()
        
        req = urllib.request.Request(
            cfg["endpoint"] + "/chat/completions", data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {cfg['api_key']}"})
        
        with urllib.request.urlopen(req, timeout=12.0) as r:
            data = json.loads(r.read())
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content")
        if content:
            cmd = content.strip().replace("`", "")
            if cmd.startswith("```"):
                cmd = cmd.split("\n")[1].strip()
            # Clean single-line command
            cmd_lines = [l.strip() for l in cmd.splitlines() if l.strip()]
            if cmd_lines:
                cmd = cmd_lines[0]
            if cmd and len(cmd) > 5 and not any(k in cmd for k in ("sorry", "unable", "cannot", "assist")):
                return cmd
    except Exception as e:
        print(f"[hermes_runner] Qwen translation error: {e}", flush=True)

    # 3. Default fallback if LLM is offline or returns conversation
    return "python scripts/introspection.py"


def run_command_silently(command: str, task_id: int | None = None) -> tuple[int, str]:
    """Execute command silently under Windows to suppress popups."""
    # Prepend sys.executable for python commands to keep virtualenv active
    exec_cmd = command
    if exec_cmd.startswith("python "):
        exec_cmd = f'"{sys.executable}" ' + exec_cmd[7:]
    elif exec_cmd.startswith("pythonw "):
        exec_cmd = f'"{sys.executable}" ' + exec_cmd[8:]

    creationflags = 0
    if sys.platform == "win32":
        creationflags = 0x08000000  # CREATE_NO_WINDOW

    try:
        proc = subprocess.Popen(
            exec_cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )
        started = time.time()
        while proc.poll() is None:
            if task_id is not None:
                control = get_task_control(task_id)
                action = (control or {}).get("action")
                if action in {"stop", "cancel"}:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                    try:
                        stdout, stderr = proc.communicate(timeout=8)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                        stdout, stderr = proc.communicate()
                    return -2, f"Execution cancelled by orchestrator control: {action}\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}"
                if action == "pause":
                    append_task_event(task_id, "hermes", "paused", "Hermes saw pause control before process completion")
            if time.time() - started > 300:
                try:
                    proc.kill()
                except Exception:
                    pass
                stdout, stderr = proc.communicate()
                return -1, f"Execution timed out after 300s\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}"
            time.sleep(1.0)
        stdout, stderr = proc.communicate()
        return proc.returncode, f"STDOUT:\n{stdout}\nSTDERR:\n{stderr}"
    except Exception as e:
        return -1, f"Execution failed: {str(e)}"


def save_synthesis_memory(task_desc: str, command: str, success: bool, output: str) -> None:
    """Save a durable memory describing the task completion/failure."""
    status_str = "completed" if success else "failed"
    # Take first 800 chars of output as preview
    preview = output[:800] + ("..." if len(output) > 800 else "")
    content = f"Hermes {status_str} task '{task_desc}' by running '{command}'.\n\nExecution log preview:\n{preview}"
    
    # Try to resolve hermes agent ID first to send to the REST API
    agent_id = None
    try:
        import sqlite3
        from lib.orchestration_engine import DB_PATH
        conn = sqlite3.connect(DB_PATH, timeout=5)
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM agents WHERE name = 'hermes'")
            row = cur.fetchone()
            if row:
                agent_id = row[0]
            else:
                # Insert hermes agent if missing
                now = int(time.time())
                cur.execute("INSERT OR IGNORE INTO agents (name, kind, created_at) VALUES ('hermes', 'agent', ?)", (now,))
                cur.execute("SELECT id FROM agents WHERE name = 'hermes'")
                row = cur.fetchone()
                if row:
                    agent_id = row[0]
                conn.commit()
        finally:
            conn.close()
    except Exception:
        pass

    payload = {
        "kind": "note",
        "content": content,
        "tags": "egon,hermes,task"
    }
    if agent_id:
        payload["attribution_agent_id"] = agent_id

    # 1. Try REST API
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:8000/api/v1/mind/memory",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=3.0) as r:
            res = json.loads(r.read())
            if res.get("status") == "ok":
                return
    except Exception:
        pass

    # 2. Try Direct SQLite fallback
    try:
        import sqlite3
        from lib.orchestration_engine import DB_PATH
        conn = sqlite3.connect(DB_PATH, timeout=10)
        try:
            now = int(time.time())
            cur = conn.cursor()
            if not agent_id:
                cur.execute("SELECT id FROM agents WHERE name = 'hermes'")
                row = cur.fetchone()
                agent_id = row[0] if row else None
                if not agent_id:
                    cur.execute("INSERT OR IGNORE INTO agents (name, kind, created_at) VALUES ('hermes', 'agent', ?)", (now,))
                    cur.execute("SELECT id FROM agents WHERE name = 'hermes'")
                    row = cur.fetchone()
                    agent_id = row[0] if row else None

            cur.execute(
                """INSERT INTO memory (kind, content, tags, attribution_agent_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                ("note", content, "egon,hermes,task", agent_id, now, now)
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"[hermes_runner] DB write fallback failed: {e}", flush=True)


def log_task_run(task_id: int, task_desc: str, command: str, code: int, output: str) -> None:
    """Write execution logs to logs/hermes-tasks.log."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    entry = (
        f"==================================================\n"
        f"[{ts}] Task ID: {task_id}\n"
        f"Description: {task_desc}\n"
        f"Command: {command}\n"
        f"Exit Code: {code}\n"
        f"--------------------------------------------------\n"
        f"{output.strip()}\n"
        f"==================================================\n\n"
    )
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(entry)


def execute_hermes_task(task: dict) -> None:
    """Execute a single task: translate, run, log, status-update, write memory."""
    task_id = task["id"]
    task_desc = task["sub_task_desc"]
    
    print(f"[hermes_runner] Processing task {task_id}: {task_desc}", flush=True)
    append_task_event(task_id, "hermes", "started", task_desc)
    record_agent_heartbeat("hermes", task_id, "working", task_desc)
    control = get_task_control(task_id)
    if control and control.get("action") in {"stop", "cancel"}:
        acknowledge_task_control(task_id, "hermes")
        append_task_event(task_id, "hermes", "cancelled", "Hermes skipped task because stop/cancel was already requested")
        update_task_status(task_id, "cancelled")
        return
    if control and control.get("action") == "pause":
        append_task_event(task_id, "hermes", "paused", "Hermes skipped paused task")
        return
    command = translate_task(task_desc)
    print(f"[hermes_runner] Translated command: {command}", flush=True)
    append_task_event(task_id, "hermes", "command", command)
    
    # Run
    code, output = run_command_silently(command, task_id=task_id)
    success = (code == 0)
    quota_failure = (not success) and is_quota_failure(output)
    
    # Log
    log_task_run(task_id, task_desc, command, code, output)
    
    # Update SQLite task status
    if quota_failure:
        append_task_event(task_id, "hermes", "quota_failure", output[:3000])
        report_agent_failure("hermes", output)
    elif code == -2:
        append_task_event(task_id, "hermes", "cancelled", output[:3000])
        update_task_status(task_id, "cancelled")
    else:
        append_task_event(task_id, "hermes", "output", output[:12000], {"exit_code": code})
        update_task_status(task_id, "completed" if success else "failed")
    
    # Synthesis memory
    save_synthesis_memory(task_desc, command, success, output)
    if quota_failure:
        print(f"[hermes_runner] Task {task_id} hit quota/rate limit; rerouted away from hermes", flush=True)
    else:
        print(f"[hermes_runner] Task {task_id} completed with status: {'success' if success else 'failed'}", flush=True)


def process_pending_tasks() -> None:
    """Find all pending hermes tasks and execute them sequentially."""
    if not _RUNNER_LOCK.acquire(blocking=False):
        return
    try:
        while True:
            task = get_pending_task("hermes")
            if not task:
                break
            try:
                execute_hermes_task(task)
            except Exception as e:
                print(f"[hermes_runner] Error running task: {e}", flush=True)
    finally:
        _RUNNER_LOCK.release()


def trigger_hermes_runner() -> None:
    """Trigger pending task processor in a background daemon thread."""
    t = threading.Thread(target=process_pending_tasks, name="hermes-runner", daemon=True)
    t.start()


if __name__ == "__main__":
    print("[hermes_runner] Running standalone polling cycle...")
    process_pending_tasks()
