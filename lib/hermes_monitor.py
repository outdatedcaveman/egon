"""Hermes monitor — the lean, always-on oversight layer.

Watches the whole substrate that no single AI session can see and SURFACES what
matters to Bruno's console: orchestrator task health (stuck / abandoned /
failed / awaiting-veto), agent quota cooldowns + idle capacity, and the
cross-cutting opportunity Bruno cares about — when one AI's quota frees and it
sits idle while interrupted work elsewhere is unfinished.

It PROPOSES, it does not act. Autonomous dispatch is gated elsewhere; every
proposal here is masterlaw-screened so a forbidden action can never even be
suggested as runnable. 100% local SQLite reads + one small JSON write — no
model, no quota, negligible RAM/CPU. Bruno 2026-06-24.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from lib import egon_paths, masterlaw

DB_PATH = egon_paths.STATE_DIR / "mind.db"
PROPOSALS_FILE = egon_paths.STATE_DIR / "hermes_proposals.json"

STUCK_AFTER_S = 3600          # an assigned/in-progress task silent this long = stuck
ACTIVE_STATUSES = ("assigned", "in_progress", "running", "started")


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=6)
    c.row_factory = sqlite3.Row
    return c


def scan(now: float | None = None) -> dict:
    """Read-only sweep. Returns observations + masterlaw-screened proposals.
    Pure DB reads — safe to call frequently from the always-on core."""
    now = now or time.time()
    out = {
        "generated_at": int(now),
        "stuck_tasks": [], "failed_tasks": [], "awaiting_veto": [],
        "agent_cooldowns": [], "idle_quota_opportunities": [],
        "proposals": [], "summary": "",
    }
    if not DB_PATH.exists():
        out["summary"] = "mind.db missing"
        return out
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT id, agent_name, sub_task_desc, status, updated_at "
                "FROM orchestrator_tasks "
                "WHERE status NOT IN ('completed','cancelled') "
                "ORDER BY updated_at DESC LIMIT 200").fetchall()
            # agents currently quota-blocked (and when they free)
            cooldowns = {}
            try:
                cd = c.execute("SELECT agent_name, until_ts FROM orchestrator_agent_cooldowns").fetchall()
                cooldowns = {r["agent_name"]: r["until_ts"] for r in cd if r["until_ts"] and r["until_ts"] > now}
            except Exception:
                pass
        out["agent_cooldowns"] = [
            {"agent": a, "frees_in_min": int((t - now) / 60)} for a, t in cooldowns.items()]
        busy_or_blocked = set(cooldowns)
        for r in rows:
            item = {"task_id": r["id"], "agent": r["agent_name"],
                    "desc": (r["sub_task_desc"] or "")[:80], "status": r["status"],
                    "idle_min": int((now - (r["updated_at"] or now)) / 60)}
            st = (r["status"] or "").lower()
            if st in ACTIVE_STATUSES and item["idle_min"] >= STUCK_AFTER_S / 60:
                out["stuck_tasks"].append(item)
            elif st == "failed":
                out["failed_tasks"].append(item)
            elif st in ("needs_clarification", "paused"):
                out["awaiting_veto"].append(item)

        # The cross-AI opportunity: unfinished work (stuck/failed/pending) whose
        # agent is NOT on cooldown (quota is back) → propose resuming it.
        unfinished = out["stuck_tasks"] + out["failed_tasks"] + [
            {"task_id": r["id"], "agent": r["agent_name"],
             "desc": (r["sub_task_desc"] or "")[:80], "status": r["status"],
             "idle_min": int((now - (r["updated_at"] or now)) / 60)}
            for r in rows if (r["status"] or "").lower() == "pending"]
        for it in unfinished:
            if it["agent"] in busy_or_blocked:
                continue
            verdict = masterlaw.check_dispatch(it["desc"], it["agent"])
            out["idle_quota_opportunities"].append(it)
            out["proposals"].append({
                "kind": "resume_unfinished",
                "task_id": it["task_id"], "agent": it["agent"],
                "desc": masterlaw.redact(it["desc"]),
                "why": f"{it['status']} for {it['idle_min']}min; {it['agent']} not "
                       f"on cooldown — quota available to finish it",
                "masterlaw_tier": verdict["tier"],
                "masterlaw_reason": verdict["reason"],
                "auto_dispatchable": verdict["allowed"],   # only true if masterlaw-clean
            })
        out["summary"] = (
            f"{len(out['stuck_tasks'])} stuck · {len(out['failed_tasks'])} failed · "
            f"{len(out['awaiting_veto'])} awaiting your veto · "
            f"{len(out['proposals'])} proposals · "
            f"{len(out['agent_cooldowns'])} agents on cooldown")
    except Exception as e:
        out["summary"] = f"scan error: {str(e)[:120]}"
    return out


def run_once() -> dict:
    """Scan + persist proposals for the console to read. Returns the scan."""
    res = scan()
    try:
        PROPOSALS_FILE.parent.mkdir(parents=True, exist_ok=True)
        PROPOSALS_FILE.write_text(json.dumps(res, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
    except Exception:
        pass
    return res


def get_proposals() -> dict:
    """Console accessor — the latest Hermes oversight snapshot."""
    try:
        return json.loads(PROPOSALS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"summary": "no Hermes scan yet", "proposals": []}


if __name__ == "__main__":
    print(json.dumps(run_once(), indent=2, ensure_ascii=False))
