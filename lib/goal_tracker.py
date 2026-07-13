"""Goal tracker — the orchestrator pursues OUTCOMES, not one-shot task waves.

Bruno 2026-07-04: his command was a goal ("Mouseion: ≥80% of entries with pdfs
and ≥80% completion") but each dispatch ran one wave and stopped — nobody
measured the number or kept going. This module closes that:

  MEASURE  — real metrics from the actual data (the live Mouseion refs.db the
             enrichment daemon writes, opened read-only/immutable; falls back to
             the Zotero desktop library only if refs.db is absent).
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

def _mouseion_db() -> Path:
    """The LIVE Mouseion/zoterpile reference DB the enrichment daemon writes.
    Override with EGON_MOUSEION_DB. (The repo's refs.db is an empty stub; the
    real store is under the per-user data dir.)"""
    import os
    env = os.environ.get("EGON_MOUSEION_DB")
    if env and Path(env).exists():
        return Path(env)
    for p in (Path.home() / ".local" / "share" / "mouseion" / "refs.db",
              Path.home() / ".local" / "share" / "zoterpile" / "refs.db"):
        if p.exists() and p.stat().st_size > 1_000_000:
            return p
    return Path.home() / ".local" / "share" / "mouseion" / "refs.db"


def measure_mouseion_8080() -> dict:
    """Enrichment state of the REAL Mouseion library (refs.db the daemon writes),
    NOT the Zotero desktop app. Bruno 2026-07-06: the metric had been reading
    ~/Zotero/zotero.sqlite — a different, more pessimistic corpus — so the goal
    loop and morning brief steered on the wrong gauge (33.9% vs the true 67.9%).

    Contract preserved: pct_pdf, pct_complete, total, with_pdf, complete.
    'complete' mirrors the Zotero predicate: title + authors + year +
    (url|doi) + (publisher|journal|container_title). Extras: avg_completeness
    (the daemon's own 0-1 score) and pending_queue (enrich_queue backlog), which
    make the wave prompts and brief actionable. Falls back to the Zotero
    measurement if refs.db is missing, so the loop never crashes."""
    db = _mouseion_db()
    if not db.exists():
        return _measure_zotero_desktop()
    try:
        c = sqlite3.connect(f"file:{db.as_posix()}?mode=ro&immutable=1",
                            uri=True, timeout=20)
    except Exception:
        return _measure_zotero_desktop()
    try:
        total = c.execute("SELECT COUNT(*) FROM refs").fetchone()[0] or 0
        # Honest "has a retrievable PDF" — a ref counts if it has a stored
        # filename, a local file, OR a Drive backup id. Counting only pdf_path
        # (a bare filename) under-reported 13.7% when the true coverage incl.
        # Drive-backed copies is ~20% (Bruno 2026-07-06 — the '46k→34k drop'
        # was this field-choice artifact, not a real loss).
        with_pdf = c.execute(
            "SELECT COUNT(*) FROM refs WHERE "
            "(pdf_path IS NOT NULL AND pdf_path!='') OR "
            "(pdf_local IS NOT NULL AND pdf_local!='') OR "
            "(pdf_drive_id IS NOT NULL AND pdf_drive_id!='')").fetchone()[0]
        complete = c.execute(
            "SELECT COUNT(*) FROM refs WHERE title IS NOT NULL AND title!='' "
            "AND authors IS NOT NULL AND authors!='' "
            "AND year IS NOT NULL AND year!='' "
            "AND ((url IS NOT NULL AND url!='') OR (doi IS NOT NULL AND doi!='')) "
            "AND ((publisher IS NOT NULL AND publisher!='') "
            "  OR (journal IS NOT NULL AND journal!='') "
            "  OR (container_title IS NOT NULL AND container_title!=''))"
        ).fetchone()[0]
        try:
            avg_c = c.execute("SELECT AVG(completeness) FROM refs").fetchone()[0]
        except Exception:
            avg_c = None
        try:
            pending = c.execute("SELECT COUNT(*) FROM enrich_queue "
                                "WHERE status='pending'").fetchone()[0]
        except Exception:
            pending = None
        return {"total": total,
                "pct_pdf": round(100 * with_pdf / max(total, 1), 2),
                "pct_complete": round(100 * complete / max(total, 1), 2),
                "with_pdf": with_pdf, "complete": complete,
                "avg_completeness": round(avg_c, 3) if avg_c is not None else None,
                "pending_queue": pending,
                "source": "mouseion_refs_db",
                "measured_at": int(time.time())}
    finally:
        c.close()


def _zotero_ro() -> sqlite3.Connection:
    db = Path.home() / "Zotero" / "zotero.sqlite"
    c = sqlite3.connect(f"file:{db.as_posix()}?mode=ro&immutable=1", uri=True,
                        timeout=20)
    c.row_factory = sqlite3.Row
    return c


def _measure_zotero_desktop() -> dict:
    """Fallback: the Zotero desktop library (used only if refs.db is absent).
    % of top-level items with a PDF, and % 'complete' (title + creators + year
    + [url or DOI] + [publisher or publicationTitle])."""
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
                "source": "zotero_desktop_fallback",
                "measured_at": int(time.time())}
    finally:
        c.close()


def _measure_llm(goal: dict) -> dict:
    """Generic self-evaluation for goals WITHOUT a programmatic metric (Bruno
    2026-07-04: 'I say what I want and the AI keeps doing work, evaluating it
    herself'). Evidence = this goal's tagged task events + verifier outcomes;
    a cheap model judges progress and names what's missing — which feeds the
    next wave's prompt."""
    from lib.orchestration_engine import DB_PATH
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=8)
    rows = conn.execute(
        "SELECT e.event_type, substr(e.content,1,220) AS c "
        "FROM orchestrator_task_events e JOIN orchestrator_tasks t "
        "ON t.id=e.task_id WHERE t.parent_prompt LIKE ? "
        "ORDER BY e.id DESC LIMIT 30", (f"%[goal:{goal['id']}%",)).fetchall()
    conn.close()
    evidence = "\n".join(f"[{r[0]}] {r[1]}" for r in reversed(rows))[-3500:]
    if not evidence.strip():
        return {"pct_progress": 0.0, "met": False,
                "missing": "no agent work attributed to this goal yet",
                "measured_at": int(time.time())}
    try:
        from lib import egon_chat
        prompt = (
            "You are evaluating progress on a standing goal from the evidence of "
            "agent work. Reply strict JSON: "
            '{"progress_pct": 0-100, "met": true|false, "missing": "<20 words>"}\n\n'
            f"GOAL / SUCCESS CRITERION:\n{goal.get('success') or goal['description']}\n\n"
            f"EVIDENCE (task events, oldest first):\n{evidence}")
        out = egon_chat.chat([{"role": "user", "content": prompt}],
                             provider="claude", model="claude-haiku-4-5-20251001",
                             inject_context=False, temperature=0.0, max_tokens=90)
        i, j = out.find("{"), out.rfind("}")
        d = json.loads(out[i:j + 1])
        return {"pct_progress": float(d.get("progress_pct") or 0),
                "met": bool(d.get("met")),
                "missing": str(d.get("missing") or "")[:200],
                "measured_at": int(time.time())}
    except Exception as e:
        return {"pct_progress": 0.0, "met": False,
                "missing": f"evaluator unavailable ({str(e)[:40]})",
                "measured_at": int(time.time())}


_METRICS = {"mouseion_8080": measure_mouseion_8080}


def goal_control(action: str, goal_id: str) -> str:
    """Deterministic chat commands: continue/pause/cancel goal <id>."""
    goals = _load_goals()
    for g in goals:
        if g["id"].lower() == goal_id.lower():
            if action == "continue":
                g["status"] = "active"
                g["wave_budget"] = int(g.get("waves") or 0) + MAX_WAVES
                msg = f"goal {g['id']} continued — {MAX_WAVES} more waves approved"
            elif action == "pause":
                g["status"] = "paused"
                msg = f"goal {g['id']} paused"
            elif action == "cancel":
                g["status"] = "cancelled"
                msg = f"goal {g['id']} cancelled (kept in the list, never deleted)"
            else:
                return f"unknown action {action}"
            _save_goals(goals)
            return msg
    return f"no goal named '{goal_id}' — known: " + ", ".join(x["id"] for x in goals)


def register_goal(goal_id: str, description: str, success: str) -> bool:
    """Add an LLM-judged goal (used by chat auto-registration)."""
    goals = _load_goals()
    if any(g["id"].lower() == goal_id.lower() for g in goals):
        return False
    goals.append({"id": goal_id, "metric": "llm", "status": "active",
                  "target": {"met": True}, "description": description[:600],
                  "success": success[:400], "waves": 1,
                  "last_wave_at": int(time.time()), "history": []})
    _save_goals(goals)
    return True


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
        import os
        tmp = GOALS.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(goals, indent=1), encoding="utf-8")
        os.replace(tmp, GOALS)   # atomic: chat process + core both write this
    except Exception:
        pass


# ── drive ────────────────────────────────────────────────────────────────────

def _goal_tasks_active(goal_id: str) -> int:
    """Waves are tagged in parent_prompt — count their unfinished tasks."""
    try:
        from lib.orchestration_engine import DB_PATH
        c = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=8)
        # Only count tasks fresh enough to still BE the wave. Task #81 sat
        # 'pending' for 33h on a credential-less agent and starved the goal all
        # night (2026-07-12): the dispatcher saw "wave in flight" forever. A
        # wave task untouched for >6h is a dead wave — dispatch the next one.
        n = c.execute(
            "SELECT COUNT(*) FROM orchestrator_tasks WHERE parent_prompt LIKE ? "
            "AND status IN ('pending','assigned','paused','needs_clarification') "
            "AND created_at > strftime('%s','now') - 6*3600",
            (f"%[goal:{goal_id}%",)).fetchone()[0]
        c.close()
        return n
    except Exception:
        return 0


def _fmt_measure(m: dict) -> str:
    if "pct_pdf" in m:
        return f"{m['pct_pdf']}% PDFs / {m['pct_complete']}% complete"
    return f"progress {m.get('pct_progress', 0):.0f}%"


def _user_idle_s() -> float:
    """Seconds since last keyboard/mouse input (Windows). 0.0 if unknown, so a
    non-Windows / failure path is treated as 'user active' — never grinds."""
    try:
        import ctypes
        class _LI(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
        li = _LI(); li.cbSize = ctypes.sizeof(_LI)
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(li)):
            tick = ctypes.windll.kernel32.GetTickCount()
            return max(0.0, (tick - li.dwTime) / 1000.0)
    except Exception:
        pass
    return 0.0


def _free_ram_gb() -> float:
    try:
        import ctypes
        class _MS(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_uint), ("dwMemoryLoad", ctypes.c_uint),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        ms = _MS(); ms.dwLength = ctypes.sizeof(_MS)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):
            return ms.ullAvailPhys / (1024 ** 3)
    except Exception:
        pass
    return 99.0


# How the mouseion goal is DRIVEN. Bruno 2026-07-13: the old agent-wave driver
# was theater — it asked claude-code/antigravity to "write enrichment code"
# every 6h, but the metric is moved by the running Mouseion DAEMON, not by
# agent code. Waves failed verification (metric flat) or stuck on dead
# antigravity, spending quota for nothing. The daemon (Mouseion.exe, now with
# book providers + Gemini) IS the engine; this driver's job is to keep it FED:
# when its queue drains and the library is still short of target, requeue a
# BOUNDED batch of incomplete refs — but only while the user is AWAY and RAM is
# healthy, so it never grinds the 8GB box while Bruno works.
_REQUEUE_BATCH = 4000
_REQUEUE_IDLE_S = 900          # 15 min idle = user away
_REQUEUE_RAM_FLOOR_GB = 1.5


def _drive_mouseion_daemon(goal: dict, m: dict) -> str:
    """Operational driver: feed the enrichment daemon instead of dispatching
    agent code-waves. Returns a human note. Never raises (goal loop must not
    crash) and never writes while the user is active."""
    pending = m.get("pending_queue")
    if pending is None:
        return "monitoring (queue size unknown)"
    if pending > 0:
        return f"daemon working ({pending:,} queued)"
    # Queue drained. Nothing to disrupt — but only refeed when Bruno is away
    # and the box has RAM headroom, so the daemon's grind never fights him.
    idle = _user_idle_s()
    if idle < _REQUEUE_IDLE_S:
        return "queue idle — will refeed incomplete refs when you step away"
    ram = _free_ram_gb()
    if ram < _REQUEUE_RAM_FLOOR_GB:
        return f"queue idle — deferring refeed (low RAM {ram:.1f}GB)"
    try:
        db = _mouseion_db()
        conn = sqlite3.connect(f"file:{db.as_posix()}", uri=True, timeout=8)
        try:
            conn.execute("PRAGMA busy_timeout=8000")
            cur = conn.execute(
                "UPDATE enrich_queue SET status='pending', last_attempt=NULL "
                "WHERE ref_id IN (SELECT eq.ref_id FROM enrich_queue eq "
                "JOIN refs r ON r.id = eq.ref_id "
                "WHERE eq.status='done' AND (r.completeness IS NULL OR r.completeness < 0.8) "
                "LIMIT ?)", (_REQUEUE_BATCH,))
            conn.commit()
            n = cur.rowcount
        finally:
            conn.close()
        if n > 0:
            return f"refed {n:,} incomplete refs to the daemon (idle {int(idle//60)}min)"
        return "nothing left to refeed — remaining refs are complete or unresolvable"
    except Exception as e:
        return f"refeed skipped ({str(e)[:40]})"


def _dispatch_wave(goal: dict, m: dict) -> bool:
    wave = int(goal.get("waves") or 0) + 1
    if "pct_pdf" in m:      # programmatic metric (mouseion-style)
        prev = (goal.get("history") or [{}])[-1] if goal.get("history") else {}
        delta_pdf = round(m["pct_pdf"] - prev.get("pct_pdf", m["pct_pdf"]), 2)
        delta_c = round(m["pct_complete"] - prev.get("pct_complete", m["pct_complete"]), 2)
        queued = m.get("pending_queue")
        queue_txt = (f" enrich_queue backlog: {queued:,} refs still pending."
                     if queued else "")
        # NET-DRAIN accounting (Bruno 2026-07-12): the backlog doubled 84k→183k
        # in a day because strategies enqueue faster than the worker drains —
        # raw % hides that churn. Measure the queue DELTA between waves and,
        # when it grows without completion moving, force the wave to fix the
        # drain path instead of piling more on.
        prev_q = prev.get("pending_queue")
        if queued is not None and prev_q is not None:
            dq = queued - prev_q
            queue_txt += (f" Net queue drain since last wave: {-dq:+,} "
                          f"({prev_q:,} → {queued:,}).")
            if dq > 0 and delta_c < 0.5:
                queue_txt += (" WARNING: backlog GREW while completion barely "
                              "moved — enqueueing is outpacing the drain. Do "
                              "NOT add more items this wave: accelerate the "
                              "DRAIN (worker throughput, batch size, dead-item "
                              "pruning) or prune stale queue entries.")
        state = (f"CURRENT MEASURED STATE (live from the Mouseion refs.db, "
                 f"{m['total']:,} refs): {m['pct_pdf']}% have PDFs "
                 f"({m['with_pdf']:,}), {m['pct_complete']}% are metadata-complete "
                 f"({m['complete']:,}); avg completeness "
                 f"{m.get('avg_completeness', '?')}.{queue_txt} "
                 f"Change since last wave: PDFs {delta_pdf:+}pp, "
                 f"completion {delta_c:+}pp.\n"
                 "Continue closing the gap AT SCALE: prioritize batch pipelines "
                 "over one-off fixes (bulk metadata enrichment via crossref/"
                 "openalex, bulk PDF resolution via unpaywall/openalex OA links), "
                 "respect the shared network budget, and report concrete counts "
                 "processed in your task events so the next measurement can "
                 "attribute progress.")
    else:                    # LLM-judged goal
        state = (f"SELF-EVALUATION of prior waves: progress "
                 f"{m.get('pct_progress', 0):.0f}%. What is still missing: "
                 f"{m.get('missing', 'unknown')}.\n"
                 "Focus this wave on exactly what is missing; leave concrete "
                 "evidence in your task events so the next evaluation can "
                 "attribute progress.")
    prompt = f"[goal:{goal['id']} wave {wave}] {goal['description']}\n{state}"
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
        if g.get("metric") == "llm":
            m = _measure_llm(g)
            met = bool(m.get("met"))
        else:
            metric = _METRICS.get(g.get("metric"))
            if not metric:
                out.append({"id": g["id"], "error": "unknown metric"})
                continue
            try:
                m = metric()
            except Exception as e:
                out.append({"id": g["id"], "error": str(e)[:100]})
                continue
            tgt0 = g.get("target") or {}
            met = all(m.get(k, 0) >= v for k, v in tgt0.items()
                      if isinstance(v, (int, float)))
        tgt = g.get("target") or {}
        note = ""
        if met:
            g["status"] = "achieved"
            note = "achieved"
            _post_chat(f"🎯 GOAL ACHIEVED — {g['id']}: {_fmt_measure(m)}.")
            try:
                from lib import push_notify
                push_notify.push("Egon 🎯 goal achieved",
                                 f"{g['id']}: {_fmt_measure(m)}",
                                 priority=4, tags="tada")
            except Exception:
                pass
        elif g.get("driver") == "mouseion_daemon":
            # Operational driver: feed the running daemon, no agent code-waves.
            note = _drive_mouseion_daemon(g, m)
        else:
            active = _goal_tasks_active(g["id"])
            since_wave = time.time() - float(g.get("last_wave_at") or 0)
            budget = int(g.get("wave_budget") or MAX_WAVES)
            if active > 0:
                note = f"wave in flight ({active} tasks)"
            elif int(g.get("waves") or 0) >= budget:
                # APPROVAL GATE (Bruno 2026-07-04: 'proposing the changes for
                # me to only approve') — don't stop silently, ask once.
                if g.get("status") != "awaiting_approval":
                    g["status"] = "awaiting_approval"
                    _post_chat(
                        f"🎯 {g['id']} used its {budget}-wave budget "
                        f"({_fmt_measure(m)}). Reply 'continue goal {g['id']}' "
                        f"to approve {MAX_WAVES} more waves, or "
                        f"'pause goal {g['id']}'.")
                    try:
                        from lib import push_notify
                        push_notify.push("Egon 🎯 needs your call",
                                         f"{g['id']}: wave budget used — "
                                         "approve more in Egon chat", priority=4)
                    except Exception:
                        pass
                note = "awaiting your approval (wave budget used)"
            elif since_wave < WAVE_COOLDOWN_S:
                note = f"cooldown ({int((WAVE_COOLDOWN_S - since_wave)/3600)}h)"
            elif _dispatch_wave(g, m):
                g["waves"] = int(g.get("waves") or 0) + 1
                g["last_wave_at"] = int(time.time())
                note = f"wave {g['waves']} dispatched"
                _post_chat(f"🎯 {g['id']}: {_fmt_measure(m)} — "
                           f"wave {g['waves']} dispatched to the agents.")
                try:
                    from lib import push_notify
                    push_notify.push("Egon 🎯 wave dispatched",
                                     f"{g['id']} wave {g['waves']}: {_fmt_measure(m)}")
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
        import os
        tmp = STATUS.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"generated_at": int(time.time()),
                                   "goals": out}, indent=1), encoding="utf-8")
        os.replace(tmp, STATUS)
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
