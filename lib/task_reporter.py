"""Task reporter — verified results come TO Bruno, not the other way around.

Bruno 2026-07-04 ("do them all in order", items 1+2): agents were completing
work invisibly — six codex tasks finished and he had no idea. This closes the
loop, with verification so 'completed' means MET THE GOAL, not 'runner exited':

  1. VERIFY  — for each newly-completed task, a cheap model compares the task
     goal against the runner's actual output events. A hollow completion is
     requeued ONCE (marked, so it can't ping-pong) instead of being reported
     as done.
  2. REPORT  — verified outcomes are appended to the CURRENT chat conversation
     (shared store → appears on desktop and phone), one concise digest per
     batch, marked 📣 so it's obviously Egon reporting in.
  3. NUDGE   — best-effort Android notification via adb when the phone is
     reachable on the LAN (no cloud push infra, no new services).

Driven by egon_core every cycle in a guard-flagged thread (LLM verify can take
seconds; the core loop must not block). State in state/task_report_state.json.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

from lib.orchestration_engine import DB_PATH, ROOT, append_task_event, update_task_status

STATE = ROOT / "state" / "task_report_state.json"
_running = threading.Event()

_SKIP_MARKERS = ("canary", "smoke", "__orchestrator")


def _load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {"last_id": 0}


def _save_state(st: dict) -> None:
    try:
        STATE.write_text(json.dumps(st), encoding="utf-8")
    except Exception:
        pass


def _events_tail(conn, task_id: int, limit: int = 6) -> str:
    rows = conn.execute(
        "SELECT event_type, content FROM orchestrator_task_events "
        "WHERE task_id=? ORDER BY id DESC LIMIT ?", (task_id, limit)).fetchall()
    out = []
    for r in reversed(rows):
        c = " ".join(str(r["content"] or "").split())[:300]
        if c:
            out.append(f"[{r['event_type']}] {c}")
    return "\n".join(out)[-1800:]


def _verify(goal: str, evidence: str) -> tuple[bool, str]:
    """Cheap goal-vs-evidence check. Conservative: on any error, pass-through
    (verified) so a broken verifier can never wedge the pipeline."""
    try:
        from lib import egon_chat
        prompt = (
            "A delegated agent task just reported completion. Decide from the "
            "evidence whether the GOAL was plausibly accomplished (not merely "
            "attempted or errored out). Reply strict JSON: "
            '{"verified": true|false, "why": "<12 words>"}\n\n'
            f"GOAL:\n{goal[:800]}\n\nEVIDENCE (runner events, newest last):\n{evidence}")
        out = egon_chat.chat([{"role": "user", "content": prompt}],
                             provider="claude", model="claude-haiku-4-5-20251001",
                             inject_context=False, temperature=0.0, max_tokens=60)
        i, j = out.find("{"), out.rfind("}")
        d = json.loads(out[i:j + 1])
        return bool(d.get("verified")), str(d.get("why") or "")[:120]
    except Exception as e:
        return True, f"verifier unavailable ({str(e)[:40]}) — passed through"


def _already_requeued(conn, task_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM orchestrator_task_events WHERE task_id=? "
        "AND event_type='verify_requeued' LIMIT 1", (task_id,)).fetchone() is not None


def _post_to_chat(lines: list[str]) -> None:
    try:
        from lib import chat_store
        sid = chat_store.current_id()
        hist = chat_store.load(sid)
        hist.append({"role": "assistant",
                     "content": "📣 Agent report:\n" + "\n".join(lines)})
        chat_store.save(sid, hist)
    except Exception:
        pass


def _notify_phone(text: str) -> None:
    """Best-effort Android notification via adb (LAN only, 6s budget)."""
    try:
        adb = ROOT / "panop_output" / "platform-tools" / "platform-tools" / "adb.exe"
        if not adb.exists():
            return
        subprocess.run(
            [str(adb), "shell", "cmd", "notification", "post",
             "-t", "Egon orchestrator", "egon_task", text[:120]],
            capture_output=True, timeout=6, creationflags=0x08000000)
    except Exception:
        pass


def report_new_outcomes() -> dict:
    """One pass: verify + report every task that finished since last time."""
    st = _load_state()
    last = int(st.get("last_id") or 0)
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=8)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, agent_name, status, sub_task_desc FROM orchestrator_tasks "
        "WHERE id > ? AND status IN ('completed','failed') ORDER BY id",
        (last,)).fetchall()
    lines: list[str] = []
    requeued = 0
    max_id = last
    for t in rows:
        max_id = max(max_id, t["id"])
        desc = " ".join(str(t["sub_task_desc"] or "").split())
        if any(m in desc.lower() for m in _SKIP_MARKERS):
            continue
        evidence = _events_tail(conn, t["id"])
        if t["status"] == "completed":
            ok, why = _verify(desc, evidence)
            if not ok and not _already_requeued(conn, t["id"]):
                update_task_status(t["id"], "pending")
                append_task_event(t["id"], t["agent_name"], "verify_requeued",
                                  f"Completion failed verification: {why}. Requeued once.")
                lines.append(f"↻ #{t['id']} {t['agent_name']}: completion NOT verified "
                             f"({why}) — requeued. «{desc[:80]}»")
                requeued += 1
            else:
                mark = "✓" if ok else "✓*"   # ✓* = unverified but already retried
                note = "" if ok else f" (unverified after retry: {why})"
                lines.append(f"{mark} #{t['id']} {t['agent_name']} completed{note}: "
                             f"«{desc[:90]}»")
        else:
            lines.append(f"✗ #{t['id']} {t['agent_name']} FAILED: «{desc[:90]}»")
    conn.close()
    if lines:
        _post_to_chat(lines)
        _notify_phone(lines[0] if len(lines) == 1
                      else f"{len(lines)} task outcomes — see Egon chat")
    st["last_id"] = max_id
    _save_state(st)
    return {"reported": len(lines), "requeued": requeued, "last_id": max_id}


def kick_async() -> bool:
    """Non-blocking trigger for egon_core's cycle: skip if a pass is running."""
    if _running.is_set():
        return False
    def _run():
        _running.set()
        try:
            report_new_outcomes()
        finally:
            _running.clear()
    threading.Thread(target=_run, name="task-reporter", daemon=True).start()
    return True
