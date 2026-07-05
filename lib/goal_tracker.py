"""Goal tracker — the orchestrator pursues OUTCOMES, not one-shot task waves.

Bruno 2026-07-04: his command was a goal ("Mouseion: ≥80% of entries with pdfs
and ≥80% completion") but each dispatch ran one wave and stopped — nobody
measured the number or kept going. This module closes that:

  MEASURE  — real metrics from the actual data (Zotero library SQLite, opened
             read-only/immutable so a running Zotero can't block or corrupt).
  JUDGE    — target met → goal achieved, reported 🎯 and retired.
  DRIVE    — target not met and no wave in flight → dispatch the next wave via
             the same /orchestrator/dispatch Bruno's chat uses, with the CURRENT
             numbers and deltas embedded so agents aim at what's actually
             missing. Cooldown + max-wave guard so it can never storm.
  REPORT   — every measurement lands in state/goals_status.json (Mission
             Control + phone Oversee render it) and material changes post to
             the current chat conversation.

Goals live in state/goals.json (editable; add more goals with other metrics).
Driven by egon_core `check_goals` in a guard-flagged thread.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from lib import egon_paths

GOALS = egon_paths.STATE_DIR / "goals.json"
STATUS = egon_paths.STATE_DIR / "goals_status.json"
_running = threading.Event()

WAVE_COOLDOWN_S = int(__import__("os").environ.get("EGON_GOAL_WAVE_COOLDOWN_S", str(6 * 3600)))
MAX_WAVES = int(__import__("os").environ.get("EGON_GOAL_MAX_WAVES", "20"))


# ── metrics ──────────────────────────────────────────────────────────────────

def _zotero_ro() -> sqlite3.Connection:
    db = Path.home() / "Zotero" / "zotero.sqlite"
    c = sqlite3.connect(f"file:{db.as_posix()}?mode=ro&immutable=1", uri=True,
                        timeout=20)
    c.row_factory = sqlite3.Row
    return c


def measure_mouseion_8080() -> dict:
    """% of top-level library items with a PDF, and % 'complete' (title +
    creators + year + [url or DOI] + [publisher or publicationTitle]).
    Field ids from fieldsCombined: title=1, date=6, url=13, publisher=23,
    publicationTitle=38, DOI=59 — resolved dynamically anyway."""
    c = _zotero_ro()
    try:
        f = {r["fieldName"]: r["fieldID"] for r in c.execute(
            "SELECT fieldID, fieldName FROM fieldsCombined")}
        base = ("FROM items i WHERE i.itemTypeID NOT IN (SELECT itemTypeID FROM "
                "itemTypes WHERE typeName IN ('attachment','note','annotation')) "
                "AND i.itemID NOT IN (SELECT itemID FROM deletedItems)")
        total = c.execute(f"SELECT COUNT(*) {base}").fetchone()[0]
        with_pdf = c.execute(
            "SELECT COUNT(DISTINCT ia.parentItemID) FROM itemAttachments ia "
            "JOIN items p ON p.itemID = ia.parentItemID "
            "WHERE ia.contentType='application/pdf' "
            "AND p.itemID NOT IN (SELECT itemID FROM deletedItems)").fetchone()[0]

        def has(fid: int) -> str:
            return ("EXISTS (SELECT 1 FROM itemData d WHERE d.itemID=i.itemID "
                    f"AND d.fieldID={int(fid)})")
        complete = c.execute(
            f"SELECT COUNT(*) {base} "
            f"AND {has(f['title'])} "
            "AND EXISTS (SELECT 1 FROM itemCreators ic WHERE ic.itemID=i.itemID) "
            f"AND {has(f['date'])} "
            f"AND ({has(f['url'])} OR {has(f['DOI'])}) "
            f"AND ({has(f['publisher'])} OR {has(f['publicationTitle'])})"
        ).fetchone()[0]
        return {"total": total,
                "pct_pdf": round(100 * with_pdf / max(total, 1), 2),
                "pct_complete": round(100 * complete / max(total, 1), 2),
                "with_pdf": with_pdf, "complete": complete,
                "measured_at": int(time.time())}
    finally:
        c.close()


_METRICS = {"mouseion_8080": measure_mouseion_8080}


# ── goal store ───────────────────────────────────────────────────────────────

_DEFAULT_GOALS = [{
    "id": "mouseion-8080",
    "metric": "mouseion_8080",
    "status": "active",
    "target": {"pct_pdf": 80.0, "pct_complete": 80.0},
    "description": ("Mouseion: at least 80% of library entries with PDFs and 80% "
                    "completion (title, authors, publisher, year, url or doi). "
                    "Bruno's standing order, 2026-07-03."),
    "waves": 0, "last_wave_at": 0, "history": [],
}]


def _load_goals() -> list[dict]:
    try:
        g = json.loads(GOALS.read_text(encoding="utf-8"))
        if isinstance(g, list) and g:
            return g
    except Exception:
        pass
    seeded = json.loads(json.dumps(_DEFAULT_GOALS))
    for g in seeded:
        # first wave waits one cooldown: the requeued/in-flight tasks from
        # Bruno's original (untagged) dispatch get their chance first
        g["last_wave_at"] = int(time.time())
    _save_goals(seeded)
    return seeded


def _save_goals(goals: list[dict]) -> None:
    try:
        GOALS.write_text(json.dumps(goals, indent=1), encoding="utf-8")
    except Exception:
        pass


# ── drive ────────────────────────────────────────────────────────────────────

def _goal_tasks_active(goal_id: str) -> int:
    """Waves are tagged in parent_prompt — count their unfinished tasks."""
    try:
        from lib.orchestration_engine import DB_PATH
        c = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=8)
        n = c.execute(
            "SELECT COUNT(*) FROM orchestrator_tasks WHERE parent_prompt LIKE ? "
            "AND status IN ('pending','assigned','paused','needs_clarification')",
            (f"%[goal:{goal_id}%",)).fetchone()[0]
        c.close()
        return n
    except Exception:
        return 0


def _dispatch_wave(goal: dict, m: dict) -> bool:
    prev = (goal.get("history") or [{}])[-1] if goal.get("history") else {}
    delta_pdf = round(m["pct_pdf"] - prev.get("pct_pdf", m["pct_pdf"]), 2)
    delta_c = round(m["pct_complete"] - prev.get("pct_complete", m["pct_complete"]), 2)
    wave = int(goal.get("waves") or 0) + 1
    prompt = (
        f"[goal:{goal['id']} wave {wave}] {goal['description']}\n"
        f"CURRENT MEASURED STATE (live from the Zotero library, "
        f"{m['total']:,} items): {m['pct_pdf']}% have PDFs "
        f"({m['with_pdf']:,}), {m['pct_complete']}% are metadata-complete "
        f"({m['complete']:,}). Change since last wave: PDFs {delta_pdf:+}pp, "
        f"completion {delta_c:+}pp.\n"
        "Continue closing the gap AT SCALE: prioritize batch pipelines over "
        "one-off fixes (bulk metadata enrichment via crossref/openalex, bulk "
        "PDF resolution via unpaywall/openalex OA links), respect the shared "
        "network budget, and report concrete counts processed in your task "
        "events so the next measurement can attribute progress.")
    try:
        import httpx
        r = httpx.post("http://127.0.0.1:8000/api/v1/mind/orchestrator/dispatch",
                       json={"prompt": prompt}, timeout=45)
        return r.status_code < 400
    except Exception:
        return False


def _post_chat(text: str) -> None:
    try:
        from lib import chat_store
        sid = chat_store.current_id()
        hist = chat_store.load(sid)
        hist.append({"role": "assistant", "content": text})
        chat_store.save(sid, hist)
    except Exception:
        pass


def evaluate() -> dict:
    """One pass over all active goals: measure → judge → drive → report."""
    goals = _load_goals()
    out = []
    for g in goals:
        if g.get("status") != "active":
            out.append({"id": g["id"], "status": g.get("status")})
            continue
        metric = _METRICS.get(g.get("metric"))
        if not metric:
            out.append({"id": g["id"], "error": "unknown metric"})
            continue
        try:
            m = metric()
        except Exception as e:
            out.append({"id": g["id"], "error": str(e)[:100]})
            continue
        tgt = g.get("target") or {}
        met = all(m.get(k, 0) >= v for k, v in tgt.items())
        note = ""
        if met:
            g["status"] = "achieved"
            note = "achieved"
            _post_chat(f"🎯 GOAL ACHIEVED — {g['id']}: "
                       f"{m['pct_pdf']}% PDFs / {m['pct_complete']}% complete "
                       f"(target {tgt.get('pct_pdf')}/{tgt.get('pct_complete')}).")
            try:
                from lib import push_notify
                push_notify.push("Egon 🎯 goal achieved",
                                 f"{g['id']}: {m['pct_pdf']}% / {m['pct_complete']}%",
                                 priority=4, tags="tada")
            except Exception:
                pass
        else:
            active = _goal_tasks_active(g["id"])
            since_wave = time.time() - float(g.get("last_wave_at") or 0)
            if active > 0:
                note = f"wave in flight ({active} tasks)"
            elif int(g.get("waves") or 0) >= MAX_WAVES:
                note = f"max waves ({MAX_WAVES}) reached — needs Bruno"
            elif since_wave < WAVE_COOLDOWN_S:
                note = f"cooldown ({int((WAVE_COOLDOWN_S - since_wave)/3600)}h)"
            elif _dispatch_wave(g, m):
                g["waves"] = int(g.get("waves") or 0) + 1
                g["last_wave_at"] = int(time.time())
                note = f"wave {g['waves']} dispatched"
                _post_chat(f"🎯 {g['id']}: {m['pct_pdf']}% PDFs / "
                           f"{m['pct_complete']}% complete (target "
                           f"{tgt.get('pct_pdf')}/{tgt.get('pct_complete')}) — "
                           f"wave {g['waves']} dispatched to the agents.")
                try:
                    from lib import push_notify
                    push_notify.push("Egon 🎯 wave dispatched",
                                     f"{g['id']} wave {g['waves']}: "
                                     f"{m['pct_pdf']}% / {m['pct_complete']}%")
                except Exception:
                    pass
            else:
                note = "dispatch failed (orchestrator unreachable)"
        hist = g.setdefault("history", [])
        if not hist or hist[-1].get("pct_pdf") != m["pct_pdf"] \
                or hist[-1].get("pct_complete") != m["pct_complete"]:
            hist.append(m)
            del hist[:-60]
        out.append({"id": g["id"], "measure": m, "note": note,
                    "waves": g.get("waves"), "target": tgt})
    _save_goals(goals)
    try:
        STATUS.write_text(json.dumps({"generated_at": int(time.time()),
                                      "goals": out}, indent=1), encoding="utf-8")
    except Exception:
        pass
    return {"goals": out}


def kick_async() -> bool:
    if _running.is_set():
        return False
    def _run():
        _running.set()
        try:
            evaluate()
        finally:
            _running.clear()
    threading.Thread(target=_run, name="goal-tracker", daemon=True).start()
    return True
