"""Gemini in-process agent runner — a real Gemini agent for the Orchestrator.

Bruno 2026-07-06: "make Antigravity work with the orchestrator." Antigravity's
standalone language server is DEPRECATED by Google ("This version of Antigravity
is no longer supported" — verified in the conversation transcript), so it cannot
run headless/autonomously; it only works when Bruno drives the IDE. This runner
delivers the actual GOAL — Gemini as an orchestrator agent — via the Gemini API
that already works (lib/egon_chat, verified), sidestepping the blocked LS.

Scope, honestly: this is a REASONING/analysis/planning/review agent. It produces
written work (the response becomes the task output + a durable memory); it does
NOT edit files or run tools the way the agentic CLIs (codex/claude-code) do. For
file-editing Gemini work you'd need Google's agentic Gemini CLI (not installed).

Mirrors lib/hermes_runner: poll pending 'gemini' tasks → execute → complete.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request

from lib.orchestration_engine import (
    acknowledge_task_control,
    append_task_event,
    get_pending_task,
    get_task_control,
    record_agent_heartbeat,
    update_task_status,
    ROOT,
)

_RUNNER_LOCK = threading.Lock()
_MODEL = "gemini-2.5-flash"          # fast + cheap; the goal is diverse reasoning


def _assess_output(output: str) -> tuple[str, str]:
    """Reject acknowledgements and plans masquerading as completed work."""
    text = (output or "").strip()
    upper = text.upper()
    if "RESULT_STATUS: BLOCKED" in upper:
        return "blocked", "runner explicitly reported a blocker"
    if "RESULT_STATUS: COMPLETE" not in upper:
        return "invalid", "missing explicit RESULT_STATUS"
    if not any(marker in upper for marker in ("EVIDENCE:", "VERIFICATION:", "DELIVERABLE:")):
        return "invalid", "missing evidence, verification, or deliverable section"
    if len(text) < 180:
        return "invalid", "response too short to substantiate completion"
    return "complete", "explicit completion contract satisfied"


def _capsule(task: dict) -> str:
    """Shared-mind context for the task (best-effort, same as the CLI runners)."""
    try:
        from lib.mind_context_broker import build_context_capsule
        q = f"{task.get('parent_prompt') or ''} {task.get('sub_task_desc') or ''}"[:600]
        cap = build_context_capsule(project=None, query=q, budget_chars=2500,
                                    limit_activity=4, limit_memory=4,
                                    include_graph=False, include_audit=False,
                                    agent="gemini")
        if isinstance(cap, dict) and cap.get("status") == "ok":
            return (cap.get("briefing") or "").strip()[:2600]
    except Exception:
        pass
    return ""


def _save_memory(task_desc: str, output: str) -> None:
    payload = {"kind": "note",
               "content": f"Gemini completed task '{task_desc[:200]}'.\n\n{output[:1500]}",
               "tags": "egon,gemini,task"}
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:8000/api/v1/mind/memory",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=3.0).read()
    except Exception:
        pass


def execute_gemini_task(task: dict) -> None:
    """Run one 'gemini' task via the Gemini API; record output; mark complete."""
    task_id = task["id"]
    desc = task.get("sub_task_desc") or task.get("parent_prompt") or ""
    append_task_event(task_id, "gemini", "started", desc[:500])
    record_agent_heartbeat("gemini", task_id, "working", desc[:200])

    control = get_task_control(task_id)
    if control and control.get("action") in {"stop", "cancel"}:
        acknowledge_task_control(task_id, "gemini")
        append_task_event(task_id, "gemini", "cancelled", "stop/cancel was requested")
        update_task_status(task_id, "cancelled")
        return
    if control and control.get("action") == "pause":
        append_task_event(task_id, "gemini", "paused", "task is paused")
        return

    cap = _capsule(task)
    prompt = (
        "You are Gemini, an agent in Bruno's Egon orchestrator. Do the delegated "
        "task below and return your COMPLETE written result (analysis, plan, "
        "answer, review, or synthesis). You cannot edit files or run commands — "
        "if the task needs that, say so explicitly and produce the best written "
        "deliverable you can (e.g. the exact patch/plan to hand to a coding agent). "
        "End with exactly one auditable status contract. For finished work use "
        "'RESULT_STATUS: COMPLETE' followed by an 'EVIDENCE:', 'VERIFICATION:', "
        "or 'DELIVERABLE:' section. If required evidence/tools are unavailable, "
        "use 'RESULT_STATUS: BLOCKED' plus 'BLOCKER:' and 'HANDOFF:' sections. "
        "Do not say you are ready or merely restate the task."
        + (f"\n\nEGON SHARED-MIND CONTEXT:\n{cap}" if cap else "")
        + f"\n\nPARENT GOAL:\n{task.get('parent_prompt') or ''}"
        + f"\n\nDELEGATED TASK:\n{desc}"
    )
    try:
        from lib import egon_chat
        out = egon_chat.chat([{"role": "user", "content": prompt}],
                             provider="gemini", model=_MODEL,
                             inject_context=False, temperature=0.4, max_tokens=2000)
        out = (out or "").strip()
        if not out:
            append_task_event(task_id, "gemini", "output", "(empty response)")
            from lib.orchestration_engine import reassign_task_agent
            reassign_task_agent(task_id, "Gemini returned an empty response", target_agent="codex")
            return
        append_task_event(task_id, "gemini", "output", out[:12000])
        verdict, reason = _assess_output(out)
        if verdict != "complete":
            from lib.orchestration_engine import reassign_task_agent
            new_agent = reassign_task_agent(
                task_id,
                f"Gemini completion gate rejected output: {reason}",
                target_agent="codex",
            )
            append_task_event(
                task_id,
                "gemini",
                "quality_gate_rejected",
                f"{reason}; rerouted to {new_agent or 'no available agent'}",
                {"verdict": verdict, "to_agent": new_agent},
            )
            if not new_agent:
                update_task_status(task_id, "pending")
            return
        update_task_status(task_id, "completed")
        _save_memory(desc, out)
        record_agent_heartbeat("gemini", task_id, "completed", desc[:200])
    except Exception as e:
        append_task_event(task_id, "gemini", "wake_failed", f"{type(e).__name__}: {str(e)[:400]}")
        # transient (quota/network) → requeue so it retries; else fail
        if any(k in str(e).lower() for k in ("quota", "rate", "429", "timeout", "temporarily")):
            update_task_status(task_id, "pending")
        else:
            update_task_status(task_id, "failed")


def process_pending_tasks() -> None:
    if not _RUNNER_LOCK.acquire(blocking=False):
        return
    try:
        n = 0
        while n < 20:                        # bound per cycle
            task = get_pending_task("gemini")
            if not task:
                break
            n += 1
            try:
                execute_gemini_task(task)
            except Exception as e:
                print(f"[gemini_runner] error: {e}", flush=True)
    finally:
        _RUNNER_LOCK.release()


def trigger_gemini_runner() -> None:
    threading.Thread(target=process_pending_tasks, name="gemini-runner", daemon=True).start()


if __name__ == "__main__":
    process_pending_tasks()
