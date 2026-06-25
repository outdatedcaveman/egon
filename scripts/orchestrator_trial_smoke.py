"""End-to-end smoke test for Egon's always-on orchestrator.

The script uses private smoke agent names so it does not steal or reroute real
Claude/Codex/Antigravity/Hermes work queued by Bruno.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

API = "http://127.0.0.1:8000/api/v1/mind"
SMOKE_AGENT = "__orchestrator_smoke__"
SMOKE_COOLDOWN_AGENT = "__orchestrator_smoke_quota__"


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
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def _step(name: str, ok: bool, detail: str = "", **extra) -> dict:
    return {"name": name, "ok": bool(ok), "detail": detail, **extra}


def _health(path: str, params: dict, timeout: float, attempts: int = 4) -> dict:
    last = {}
    for attempt in range(attempts):
        last = _request("GET", path, params=params, timeout=timeout)
        if last.get("status") != "refreshing":
            return last
        time.sleep(2.0 + attempt)
    return last


def main() -> int:
    steps: list[dict] = []
    task_id = None
    try:
        stats = _request("GET", "/stats")
        steps.append(_step("mind_stats", stats.get("status") == "ok", str(stats)[:180]))

        service = _request("GET", "/orchestrator/service/status")
        steps.append(_step("orchestrator_service", service.get("running") is True, str(service)[:220]))

        autonomy = _request("GET", "/orchestrator/autonomy/status")
        steps.append(_step("autonomy_status", autonomy.get("status") == "ok", str(autonomy.get("autonomy"))))

        from lib.orchestration_engine import create_task

        task = create_task(
            "__orchestrator_trial_smoke__",
            SMOKE_AGENT,
            "Smoke-test delegated task delivery, events, controls, and cleanup.",
            allow_unknown_agent=True,
        )
        task_id = int(task["id"])
        steps.append(_step("task_created", task_id > 0, f"task_id={task_id}"))

        capsule = _request("GET", "/context/v2", params={
            "project": "egon",
            "query": "orchestrator smoke delegated task",
            "agent": SMOKE_AGENT,
            "budget_chars": 1800,
            "limit_activity": 2,
            "limit_memory": 3,
            "include_graph": "false",
            "include_audit": "false",
        }, timeout=8.0)
        delegated = ((capsule.get("sections") or {}).get("delegated_task") or {})
        steps.append(_step(
            "delegated_task",
            capsule.get("status") == "ok" and int(delegated.get("id") or 0) == task_id,
            f"delegated={delegated.get('id')}",
        ))

        event = _request("POST", f"/orchestrator/tasks/{task_id}/events", body={
            "task_id": task_id,
            "agent_name": SMOKE_AGENT,
            "event_type": "progress",
            "content": "Smoke progress event visible through orchestrator timeline.",
            "payload": {"smoke": True},
        })
        steps.append(_step("event_append", event.get("status") == "ok", str(event)))

        for action in ("pause", "resume", "edit", "stop"):
            body = {"action": action, "note": f"smoke {action}"}
            if action == "edit":
                body["replacement_desc"] = "Edited smoke task text."
            control = _request("POST", f"/orchestrator/tasks/{task_id}/control", body=body)
            steps.append(_step(f"control_{action}", control.get("status") == "ok", str(control)))

        events = _request("GET", f"/orchestrator/tasks/{task_id}/events", params={"limit": 100})
        event_types = [e.get("event_type") for e in events.get("events") or []]
        required_events = {"created", "assigned", "progress", "control_pause", "control_resume", "control_edit", "control_stop"}
        steps.append(_step(
            "timeline_events",
            required_events.issubset(set(event_types)),
            ",".join(str(e) for e in event_types),
        ))

        provider = _request("GET", "/orchestrator/provider-hooks/status")
        steps.append(_step("provider_hooks_status", provider.get("status") == "ok", str(provider)[:220]))
        wake = _request("GET", "/orchestrator/wake/status")
        steps.append(_step(
            "wake_bridge_status",
            wake.get("status") == "ok" and "runner_agents" in wake,
            str(wake)[:220],
        ))

        failure = _request("POST", "/agents/failure", body={
            "agent_name": SMOKE_COOLDOWN_AGENT,
            "detail": "HTTP 429 rate limit quota exceeded during smoke test",
            "cooldown_seconds": 2,
        })
        steps.append(_step("quota_failure_routing", failure.get("status") == "cooldown", str(failure)))

        clear = _request("POST", "/agents/cooldown/clear", body={"agent_name": SMOKE_COOLDOWN_AGENT})
        steps.append(_step("quota_clear", clear.get("status") == "ok", str(clear)))

        scheduler = _request("GET", "/orchestrator/scheduler/status")
        steps.append(_step("scheduler_status", scheduler.get("status") == "ok", str(scheduler)[:220]))

        scorecard = _health("/scorecard", params={
            "project": "egon",
            "since_hours": 24,
            "capsule_budget_chars": 1000,
        }, timeout=12.0)
        steps.append(_step("scorecard", scorecard.get("status") == "ok", f"score={scorecard.get('score')}"))

        enforcement = _health("/enforcement/status", params={
            "project": "egon",
            "since_hours": 24,
        }, timeout=18.0)
        steps.append(_step("enforcement_status", enforcement.get("status") == "ok", f"score={enforcement.get('score')}"))
    except Exception as exc:
        steps.append(_step("exception", False, f"{type(exc).__name__}: {exc}"))
    finally:
        if task_id is not None:
            try:
                _request("POST", "/orchestrator/complete", body={"task_id": task_id, "status": "cancelled"})
            except Exception:
                pass

    ok = all(s["ok"] for s in steps)
    print(json.dumps({
        "status": "ok" if ok else "error",
        "passed": sum(1 for s in steps if s["ok"]),
        "total": len(steps),
        "steps": steps,
    }, indent=2, ensure_ascii=True))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
