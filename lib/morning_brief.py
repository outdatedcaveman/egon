"""Morning briefing — Egon greets Bruno with the state of his world.

Improvement #1 (Bruno 2026-07-04): once a day, one proactive digest lands in
the chat (desktop + phone) and a summary push hits the pocket. What moved
overnight, what the agents did, what needs his call — zero clicks to know
everything. Deltas come from yesterday's snapshot (state/brief_snapshot.json).
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from lib import egon_paths

SNAP = egon_paths.STATE_DIR / "brief_snapshot.json"
STATE = egon_paths.STATE_DIR / "brief_state.json"


def _load(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _collect() -> dict:
    """Cheap, read-only facts. Anything unavailable is skipped, never fatal."""
    out: dict = {"ts": int(time.time())}
    # extraction progress
    try:
        from lib.egon_paths import FILE_EXTRACTS_DIR
        out["extracted"] = sum(1 for _ in FILE_EXTRACTS_DIR.rglob("*.txt"))
    except Exception:
        pass
    try:
        fi = egon_paths.STATE_DIR / "files_index.jsonl"
        out["files_known"] = sum(1 for _ in fi.open(encoding="utf-8", errors="ignore"))
    except Exception:
        pass
    # notion mirror
    try:
        from lib import mirror_runner
        out["notion_pct"] = mirror_runner.status().get("notion_pct")
    except Exception:
        pass
    # goals
    try:
        gst = _load(egon_paths.STATE_DIR / "goals_status.json")
        out["goals"] = [{"id": g["id"],
                         "pct_pdf": (g.get("measure") or {}).get("pct_pdf"),
                         "pct_complete": (g.get("measure") or {}).get("pct_complete"),
                         "note": g.get("note", "")}
                        for g in gst.get("goals", []) if g.get("measure")]
    except Exception:
        pass
    # agent work + attention items, last 24h
    try:
        from lib.orchestration_engine import DB_PATH
        c = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=8)
        day_ago = int(time.time()) - 86400
        out["tasks_done"] = c.execute(
            "SELECT COUNT(*) FROM orchestrator_tasks WHERE status='completed' "
            "AND updated_at > ?", (day_ago,)).fetchone()[0]
        out["tasks_failed"] = c.execute(
            "SELECT COUNT(*) FROM orchestrator_tasks WHERE status='failed' "
            "AND updated_at > ?", (day_ago,)).fetchone()[0]
        out["tasks_waiting"] = c.execute(
            "SELECT COUNT(*) FROM orchestrator_tasks WHERE status IN "
            "('pending','assigned','needs_clarification')").fetchone()[0]
        # yesterday's model spend from the ledger
        row = c.execute(
            "SELECT COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0) "
            "FROM turns_ledger WHERE ts > ?", (day_ago,)).fetchone()
        out["tokens_in"], out["tokens_out"] = int(row[0]), int(row[1])
        c.close()
    except Exception:
        pass
    # substrate health
    try:
        h = _load(egon_paths.STATE_DIR / "core_health.json")
        units = h.get("units") or h
        out["units_down"] = [n for n, u in units.items()
                             if isinstance(u, dict) and not u.get("ok")]
    except Exception:
        pass
    return out


def _fmt_delta(cur, prev, suffix="") -> str:
    if cur is None:
        return "?"
    if prev is None or prev == cur:
        return f"{cur:,}{suffix}" if isinstance(cur, int) else f"{cur}{suffix}"
    d = cur - prev
    sign = "+" if d >= 0 else ""
    return (f"{cur:,}{suffix} ({sign}{d:,})" if isinstance(cur, int)
            else f"{cur}{suffix} ({sign}{round(d, 2)})")


def build_brief() -> tuple[str, str]:
    """(full chat text, short push text). Also rolls the snapshot forward."""
    now = _collect()
    prev = _load(SNAP)
    lines = [f"☀️ Morning briefing — {time.strftime('%A, %d %b')}"]
    if "extracted" in now:
        lines.append(f"• Library text extraction: "
                     f"{_fmt_delta(now['extracted'], prev.get('extracted'))} of "
                     f"{now.get('files_known', 0):,} files")
    if now.get("notion_pct") is not None:
        lines.append(f"• Notion mirror: "
                     f"{_fmt_delta(now['notion_pct'], prev.get('notion_pct'), '%')}")
    prev_goals = {g["id"]: g for g in prev.get("goals", [])}
    for g in now.get("goals", []):
        pg = prev_goals.get(g["id"], {})
        lines.append(f"• 🎯 {g['id']}: "
                     f"{_fmt_delta(g.get('pct_pdf'), pg.get('pct_pdf'), '%')} PDFs / "
                     f"{_fmt_delta(g.get('pct_complete'), pg.get('pct_complete'), '%')} "
                     f"complete — {g.get('note', '')}")
    if "tasks_done" in now:
        lines.append(f"• Agents (24h): {now['tasks_done']} verified done · "
                     f"{now['tasks_failed']} failed · {now.get('tasks_waiting', 0)} queued")
    if now.get("tokens_out"):
        lines.append(f"• Model spend (24h): {now.get('tokens_in', 0):,} in / "
                     f"{now['tokens_out']:,} out tokens")
    down = now.get("units_down") or []
    lines.append("• Substrate: all services healthy" if not down
                 else f"• ⚠ Substrate: {', '.join(down)} DOWN")
    # Delivery truth (Bruno 2026-07-13): surface fixes that are on disk but NOT
    # live, so 'fixed but not deployed' can never stay invisible again.
    try:
        import sys as _sys, subprocess as _sp
        from lib.egon_paths import ROOT as _R
        r = _sp.run([_sys.executable, str(_R / "scripts" / "deploy_state.py")],
                    capture_output=True, text=True, timeout=40)
        summary = (r.stdout or "").strip().splitlines()[-1:] if r.stdout else []
        if summary and "not-live" in summary[0] and not summary[0].startswith("0 "):
            lines.append(f"• ⚠ Deploy: {summary[0]}")
    except Exception:
        pass
    full = "\n".join(lines)
    push = (f"{now.get('tasks_done', 0)} tasks done · extraction "
            f"{_fmt_delta(now.get('extracted'), prev.get('extracted'))} · "
            f"notion {now.get('notion_pct', '?')}% — full brief in Egon")
    try:
        SNAP.write_text(json.dumps(now), encoding="utf-8")
    except Exception:
        pass
    return full, push


def deliver() -> bool:
    full, push_text = build_brief()
    try:
        from lib import chat_store
        sid = chat_store.current_id()
        hist = chat_store.load(sid)
        hist.append({"role": "assistant", "content": full})
        chat_store.save(sid, hist)
    except Exception:
        pass
    try:
        from lib import push_notify
        push_notify.push("Egon ☀️ morning briefing", push_text, tags="sunrise")
    except Exception:
        pass
    try:
        STATE.write_text(json.dumps({"last_date": time.strftime("%Y-%m-%d")}),
                         encoding="utf-8")
    except Exception:
        pass
    return True


def due(hour: int = 8) -> bool:
    """True once per day, at/after the configured local hour."""
    if time.localtime().tm_hour < hour:
        return False
    return _load(STATE).get("last_date") != time.strftime("%Y-%m-%d")
