"""Real-agent orchestrator trial harness.

Queues safe canary tasks for actual agent identities and observes whether the
real bodies pick them up through Egon's mind/orchestrator contract. Unlike the
smoke test, this script does not impersonate Claude/Codex/Antigravity/Hermes by
calling context as those agents.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

API = "http://127.0.0.1:8000/api/v1/mind"
DEFAULT_AGENTS = ("claude-code", "codex", "antigravity", "hermes")
TRIAL_PREFIX = "__real_agent_trial__"


def _request(method: str, path: str, body: dict | None = None,
             params: dict | None = None, timeout: float = 12.0) -> dict:
    url = API + path
    if params:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        if query:
            url += "?" + query
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}


def _trial_task_text(agent: str, run_id: str) -> str:
    if agent == "hermes":
        return (
            f"{TRIAL_PREFIX} {run_id} for hermes. Safe command canary only; "
            "do not edit project files.\n"
            f"hermes-command: python -c \"print('REAL_AGENT_TRIAL {run_id} hermes ok')\""
        )
    return (
        f"{TRIAL_PREFIX} {run_id} for {agent}. This is a safe orchestrator "
        "contract trial. Do not edit project files. Use Egon's mind/orchestrator "
        "tools or REST API to append events: started, progress, control_acknowledged "
        "if a control appears, and final. Read your task control endpoint before "
        "finishing. Mark the task completed when done."
    )


def _create_tasks(agents: list[str], run_id: str) -> dict[str, int]:
    from lib.orchestration_engine import create_task

    out: dict[str, int] = {}
    for agent in agents:
        task = create_task(
            f"{TRIAL_PREFIX} {run_id}",
            agent,
            _trial_task_text(agent, run_id),
        )
        out[agent] = int(task["id"])
    try:
        from lib.hermes_runner import trigger_hermes_runner

        trigger_hermes_runner()
    except Exception:
        pass
    return out


def _events(task_id: int) -> list[dict]:
    from lib.orchestration_engine import get_task_events

    try:
        return get_task_events(task_id=task_id, limit=200)
    except Exception as exc:
        return [{
            "task_id": task_id,
            "agent_name": None,
            "event_type": "trial_harness_error",
            "content": f"Could not read task events: {type(exc).__name__}: {exc}",
            "payload": {},
        }]


def _status(task_id: int) -> dict:
    from lib.orchestration_engine import DB_PATH

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """SELECT id, parent_prompt, agent_name, sub_task_desc, status, created_at, updated_at
               FROM orchestrator_tasks WHERE id = ?""",
            (int(task_id),),
        ).fetchone()
        if not row:
            return {}
        task = dict(row)
        latest = conn.execute(
            """SELECT id, event_type, content, created_at, agent_name
               FROM orchestrator_task_events
               WHERE task_id = ? ORDER BY id DESC LIMIT 1""",
            (int(task_id),),
        ).fetchone()
        task["latest_event"] = dict(latest) if latest else None
        control = conn.execute(
            "SELECT * FROM orchestrator_task_controls WHERE task_id = ?",
            (int(task_id),),
        ).fetchone()
        task["control"] = dict(control) if control else None
        return task
    except Exception as exc:
        return {"id": int(task_id), "status": "unknown", "error": f"{type(exc).__name__}: {exc}"}
    finally:
        conn.close()
    return {}


def _assess(agent: str, task_id: int) -> dict:
    events = _events(task_id)
    event_types = [e.get("event_type") for e in events]
    task = _status(task_id)
    evidence_events = [
        e for e in events
        if e.get("event_type") not in {
            "created",
            "control_resume",
            "status_cancelled",
            "trial_cleanup",
            "trial_harness_error",
        }
    ]
    control_ack = any(e.get("event_type") == "control_acknowledged" for e in events)
    completed = task.get("status") == "completed" or any(e == "status_completed" for e in event_types)
    assigned = "assigned" in event_types or task.get("status") == "assigned"
    return {
        "agent": agent,
        "task_id": task_id,
        "task_status": task.get("status"),
        "assigned": assigned,
        "event_count": len(events),
        "event_types": event_types,
        "real_activity": bool(evidence_events),
        "control_acknowledged": control_ack,
        "completed": completed,
        "latest_event": events[-1] if events else None,
    }


def _send_control_ping(task_id: int, note: str) -> None:
    from lib.orchestration_engine import set_task_control

    set_task_control(task_id, "clarify", note)


def _resume(task_id: int) -> None:
    try:
        from lib.orchestration_engine import set_task_control

        set_task_control(task_id, "resume", "trial control window closed")
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--observe-seconds", type=int, default=180)
    parser.add_argument("--agents", default=",".join(DEFAULT_AGENTS))
    parser.add_argument("--control-ping-after", type=int, default=45)
    parser.add_argument("--cleanup", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--wake-scan", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()

    agents = [a.strip() for a in args.agents.split(",") if a.strip()]
    run_id = f"{int(time.time())}"
    task_ids = _create_tasks(agents, run_id)
    if args.wake_scan:
        try:
            from lib.agent_wake_bridge import wake_pending_agents

            wake_pending_agents()
        except Exception as exc:
            print(json.dumps({
                "status": "wake_scan_error",
                "error": f"{type(exc).__name__}: {exc}",
            }, ensure_ascii=True), file=sys.stderr)
    started = time.time()
    control_sent: set[int] = set()
    control_resumed: set[int] = set()
    samples: list[dict] = []
    monitor_errors: list[str] = []

    final: list[dict] = []
    try:
        try:
            while time.time() - started < max(10, args.observe_seconds):
                elapsed = int(time.time() - started)
                assessments = [_assess(agent, tid) for agent, tid in task_ids.items()]
                samples.append({"elapsed": elapsed, "agents": assessments})

                for item in assessments:
                    tid = int(item["task_id"])
                    if (
                        args.control_ping_after > 0
                        and elapsed >= args.control_ping_after
                        and item.get("assigned")
                        and not item.get("completed")
                        and tid not in control_sent
                    ):
                        _send_control_ping(
                            tid,
                            f"REAL_AGENT_TRIAL_CONTROL_ACK {run_id}: acknowledge this control, then continue.",
                        )
                        control_sent.add(tid)
                    if (
                        tid in control_sent
                        and tid not in control_resumed
                        and elapsed >= args.control_ping_after + 25
                        and not item.get("completed")
                    ):
                        _resume(tid)
                        control_resumed.add(tid)

                if all(a.get("completed") for a in assessments):
                    break
                time.sleep(5)
        except Exception as exc:
            monitor_errors.append(f"{type(exc).__name__}: {exc}")
    finally:
        final = [_assess(agent, tid) for agent, tid in task_ids.items()]
        if args.cleanup:
            from lib.orchestration_engine import append_task_event, update_task_status

            for agent, tid in task_ids.items():
                try:
                    assessment = _assess(agent, tid)
                    if assessment.get("completed"):
                        if assessment.get("task_status") != "completed":
                            update_task_status(tid, "completed")
                        continue
                    else:
                        append_task_event(
                            tid,
                            agent,
                            "trial_cleanup",
                            "Real-agent trial harness cleaned up unfinished canary task.",
                        )
                        update_task_status(tid, "cancelled")
                except Exception:
                    pass
            final = [_assess(agent, tid) for agent, tid in task_ids.items()]

    summary = {
        "status": "ok",
        "run_id": run_id,
        "observe_seconds": int(time.time() - started),
        "task_ids": task_ids,
        "agents": final,
        "pass": {
            "any_real_activity": any(a["real_activity"] for a in final),
            "all_assigned": all(a["assigned"] for a in final),
            "all_real_activity": all(a["real_activity"] for a in final),
            "all_completed": all(a["completed"] for a in final),
            "all_control_acknowledged_when_pinged": all(
                (int(a["task_id"]) not in control_sent) or a["control_acknowledged"]
                for a in final
            ),
        },
        "control_ping_task_ids": sorted(control_sent),
        "sample_count": len(samples),
        "monitor_errors": monitor_errors,
        "wake_scan": bool(args.wake_scan),
    }
    print(json.dumps(summary, indent=2, ensure_ascii=True))
    return 0 if summary["pass"]["any_real_activity"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
