"""Spend & agents report — makes autonomous work VISIBLE.

Bruno 2026-07-06: "I feel like I have an open faucet I don't even know where it's
at." The guardrails bound the faucet; this makes it visible. Two aggregations,
both read-only and cheap:

  • token_spend()   — from turns_ledger: input/output/cache tokens by model over
    a window, so "how much am I spending" is answerable at a glance.
  • agent_activity() — from orchestrator_tasks: per-agent task count, wall-clock
    minutes, and status breakdown, so "what did each agent actually do (and how
    long)" is answerable too.

Windows anchor on the data's own newest timestamp (not the wall clock), so a
clock/timezone offset in the ledger can't blank the report.
"""
from __future__ import annotations

import sqlite3
import time


def _conn():
    from lib.orchestration_engine import DB_PATH
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=8)


def token_spend(hours: int = 24) -> dict:
    """Token totals by model over the last `hours` of ledger activity."""
    try:
        c = _conn()
    except Exception as e:
        return {"error": str(e)[:120], "models": [], "total_in": 0, "total_out": 0}
    try:
        ref = c.execute("SELECT MAX(ts) FROM turns_ledger").fetchone()[0] or int(time.time())
        cut = ref - hours * 3600
        rows = c.execute(
            "SELECT model, SUM(input_tokens), SUM(output_tokens), "
            "SUM(cache_read_tokens), SUM(cache_write_tokens), COUNT(*) "
            "FROM turns_ledger WHERE ts > ? GROUP BY model "
            "ORDER BY (SUM(input_tokens)+SUM(output_tokens)) DESC", (cut,)).fetchall()
        models = []
        tin = tout = tcache = 0
        for m, i, o, cr, cw, n in rows:
            if not m or m == "<synthetic>":
                continue
            i, o, cr, cw = int(i or 0), int(o or 0), int(cr or 0), int(cw or 0)
            tin += i; tout += o; tcache += cr + cw
            models.append({"model": m, "in": i, "out": o,
                           "cache": cr + cw, "turns": int(n or 0)})
        return {"hours": hours, "models": models,
                "total_in": tin, "total_out": tout, "total_cache": tcache}
    finally:
        c.close()


def agent_activity(hours: int = 24) -> dict:
    """Per-agent task count, wall-clock minutes, and status breakdown."""
    try:
        c = _conn()
    except Exception as e:
        return {"error": str(e)[:120], "agents": []}
    try:
        ref = c.execute("SELECT MAX(created_at) FROM orchestrator_tasks").fetchone()[0] or int(time.time())
        cut = ref - hours * 3600
        rows = c.execute(
            "SELECT agent_name, status, created_at, updated_at FROM orchestrator_tasks "
            "WHERE created_at > ?", (cut,)).fetchall()
        agg: dict[str, dict] = {}
        for agent, status, ca, ua in rows:
            a = agg.setdefault(str(agent or "?"),
                               {"agent": str(agent or "?"), "tasks": 0,
                                "wall_min": 0.0, "status": {}})
            a["tasks"] += 1
            a["wall_min"] += max(0.0, (int(ua or 0) - int(ca or 0)) / 60.0)
            a["status"][str(status)] = a["status"].get(str(status), 0) + 1
        agents = sorted(agg.values(), key=lambda x: x["wall_min"], reverse=True)
        for a in agents:
            a["wall_min"] = round(a["wall_min"])
        return {"hours": hours, "agents": agents}
    finally:
        c.close()


def summary(hours: int = 24) -> dict:
    """Both halves in one call, for the Orchestrator panel + morning brief."""
    return {"tokens": token_spend(hours), "agents": agent_activity(hours),
            "generated_at": int(time.time())}
