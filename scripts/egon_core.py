"""Egon Core — the always-on supervisor for Egon's substrate services.

Bruno 2026-06-12: the mind died twice in one day and the Headroom proxy was
found down despite a directive that every agent route through it. Root cause:
each service was supervised by whichever app happened to start it (or nothing).
This is the fix — ONE supervised core, independent of the desktop UI, that
keeps the substrate alive and visibly healthy:

  • Mind service (:8000) — spawns scripts/mind_service.py (idempotent, has its
    own mutex) whenever /api/v1/mind/stats stops answering ok.
  • Headroom proxy (:8787) — via lib.headroom_proc.ensure_running (Antigravity's
    supervisor, reused; CREATE_NO_WINDOW, Python-degraded mode flag injected).
    Previously only alive while the Egon app was open — now always-on.
  • Semantic Connect index — lib.semantic_index.build(force=False) every 6 h
    (incremental by content hash, cheap after first build).
  • Health visibility — state/core_health.json rewritten every cycle with the
    live status of each unit + restart counts; logs/egon-core.log structured.

Lifecycle: started at login by the Startup launcher (the sanctioned always-on
exception to the no-daemons rule — it IS the coordination substrate). Single
instance enforced by kernel mutex. Check cycle every 30 s, restart backoff per
unit so a crash-looping service can't spin the CPU.

Run:  .venv\\Scripts\\pythonw.exe scripts\\egon_core.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    import lib.no_console  # noqa: F401
except Exception:
    pass

PYW = ROOT / ".venv" / "Scripts" / "pythonw.exe"
LOG = ROOT / "logs" / "egon-core.log"
HEALTH = ROOT / "state" / "core_health.json"

CHECK_EVERY_S = 30
INDEX_EVERY_S = 6 * 3600
RESTART_BACKOFF_S = 120          # per-unit: at most one restart per 2 min
HEAVY_MODE = os.environ.get("EGON_CORE_HEAVY_MODE", "manual").strip().lower()
HEAVY_IDLE_AFTER_S = int(os.environ.get("EGON_CORE_HEAVY_IDLE_AFTER_S", "1800"))

MIND_STATS = "http://127.0.0.1:8000/api/v1/mind/stats"
HEADROOM_HEALTH = "http://127.0.0.1:8787/health"
OLLAMA_TAGS = "http://127.0.0.1:11434/api/tags"
OLLAMA_EXE = Path(r"C:/Users/bruno/AppData/Local/Programs/Ollama/ollama.exe")


def log(level: str, event: str, **kw) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        line = (datetime.now().isoformat(timespec="seconds")
                + f" [{level}] event={event} "
                + " ".join(f"{k}={v}" for k, v in kw.items()))
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line.rstrip() + "\n")
    except Exception:
        pass


def _http_ok(url: str, timeout: float = 3.0) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = r.read(400).decode("utf-8", "replace")
        return r.status == 200, body
    except Exception as e:
        return False, f"{type(e).__name__}"


def _idle_seconds() -> float:
    """Seconds since last keyboard/mouse input. Windows-only; conservative."""
    if os.name != "nt":
        return 0.0
    try:
        import ctypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            return 0.0
        tick = ctypes.windll.kernel32.GetTickCount()
        return max(0.0, (tick - lii.dwTime) / 1000.0)
    except Exception:
        return 0.0


def _heavy_allowed() -> tuple[bool, str]:
    """Gate CPU/network-heavy background work.

    The always-on core must be a supervisor first. Heavy corpus work is useful,
    but it cannot run just because Egon is alive: it was freezing the PC while
    Bruno was actively using Inbox. Defaults to manual opt-in.
    """
    if HEAVY_MODE in ("1", "true", "yes", "always"):
        return True, "heavy mode always"
    if HEAVY_MODE in ("off", "0", "false", "no", "manual", ""):
        return False, "paused: set EGON_CORE_HEAVY_MODE=idle or always"
    if HEAVY_MODE == "idle":
        idle = _idle_seconds()
        if idle >= HEAVY_IDLE_AFTER_S:
            return True, f"idle {int(idle)}s"
        return False, f"paused: active user, idle {int(idle)}s/{HEAVY_IDLE_AFTER_S}s"
    return False, f"paused: unknown heavy mode {HEAVY_MODE!r}"


# ── units ────────────────────────────────────────────────────────────────────
class Unit:
    # Consecutive probe failures required before a unit counts as down.
    # 2026-06-11 post-mortem: 14 phantom "mind restarts" in one day — every
    # single one found the service already running. The probe (3s, one
    # strike) was timing out while the mind was merely BUSY (MiniLM encode
    # for /connect saturates the process for a few seconds). Busy ≠ dead.
    FAILS_REQUIRED = 3

    def __init__(self, name: str):
        self.name = name
        self.ok = False
        self.detail = ""
        self.restarts = 0
        self.last_restart = 0.0
        self.fails = 0

    def probe(self, ok: bool) -> bool:
        """Record a probe result; True only when down is CONFIRMED."""
        self.fails = 0 if ok else self.fails + 1
        return self.fails >= self.FAILS_REQUIRED

    def can_restart(self) -> bool:
        return time.time() - self.last_restart > RESTART_BACKOFF_S

    def mark_restart(self):
        self.restarts += 1
        self.last_restart = time.time()
        self.fails = 0

    def as_dict(self) -> dict:
        return {"ok": self.ok, "detail": self.detail, "restarts": self.restarts}


def check_mind(u: Unit) -> None:
    t0 = time.time()
    ok, body = _http_ok(MIND_STATS, timeout=8.0)
    ok = ok and '"status":"ok"' in body.replace(" ", "")
    ms = int((time.time() - t0) * 1000)
    confirmed_down = u.probe(ok)
    u.ok = ok or not confirmed_down   # busy-but-alive still counts as ok
    u.detail = (f"serving ({ms}ms)" if ok
                else f"slow/failed probe {u.fails}/{u.FAILS_REQUIRED}: {body}")
    if ms > 2000 or not ok:
        log("info", "mind_probe_slow", ms=ms, ok=ok, fails=u.fails)
    if not confirmed_down or not u.can_restart():
        return
    u.mark_restart()
    log("warn", "mind_down_restarting", attempt=u.restarts)
    try:
        # mind_service is idempotent (own mutex + ready checks); detached so it
        # outlives the core if the core itself is restarted.
        subprocess.Popen(
            [str(PYW), str(ROOT / "scripts" / "mind_service.py")],
            cwd=str(ROOT),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008),
        )
    except Exception as e:
        log("error", "mind_spawn_failed", error=str(e)[:160])


def check_headroom(u: Unit) -> None:
    ok, body = _http_ok(HEADROOM_HEALTH, timeout=6.0)
    confirmed_down = u.probe(ok)
    u.ok = ok or not confirmed_down
    u.detail = ("healthy" + (" (python-degraded)" if "disabled" in body else "")
                ) if ok else f"probe {u.fails}/{u.FAILS_REQUIRED}: {body}"
    if not confirmed_down or not u.can_restart():
        return
    u.mark_restart()
    log("warn", "headroom_down_restarting", attempt=u.restarts)
    try:
        from lib import headroom_proc
        headroom_proc.ensure_running(
            log_fn=lambda lvl, **kw: log(lvl, kw.pop("event", "headroom"), **kw))
    except Exception as e:
        log("error", "headroom_start_failed", error=str(e)[:160])


def check_ollama(u: Unit) -> None:
    """Keep the local synthesis brain (Ollama, qwen2.5:3b) serving. The model
    itself loads on demand and auto-unloads when idle, so a running server
    costs almost nothing. Bruno 2026-06-12 (#2: retrieval → answers)."""
    ok, _ = _http_ok(OLLAMA_TAGS, timeout=6.0)
    confirmed_down = u.probe(ok)
    u.ok = ok or not confirmed_down
    u.detail = "serving" if ok else f"probe {u.fails}/{u.FAILS_REQUIRED}"
    if not confirmed_down or not u.can_restart():
        return
    if not OLLAMA_EXE.exists():
        u.detail = "ollama not installed"
        return
    u.mark_restart()
    log("warn", "ollama_down_restarting", attempt=u.restarts)
    try:
        subprocess.Popen(
            [str(OLLAMA_EXE), "serve"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008
                           | 0x08000000),
        )
    except Exception as e:
        log("error", "ollama_spawn_failed", error=str(e)[:160])


_index_building = False
_index_last = 0.0


def check_index(u: Unit) -> None:
    """Refresh the semantic Connect index every INDEX_EVERY_S, off-thread."""
    global _index_building, _index_last
    meta = ROOT / "state" / "connect_index" / "meta.json"
    age = (time.time() - meta.stat().st_mtime) if meta.exists() else None
    u.ok = age is not None
    u.detail = (f"age={int(age // 60)}m" if age is not None else "not built")
    allowed, why = _heavy_allowed()
    if not allowed:
        u.detail += f" ({why})"
        return
    if _index_building:
        u.detail += " (refreshing)"
        return
    due = age is None or age > INDEX_EVERY_S
    if not due or time.time() - _index_last < 600:
        return
    _index_last = time.time()
    _index_building = True

    def _build():
        global _index_building
        try:
            # tier-2 first: extract text for pinned files (budgeted), so the
            # rebuild below embeds their content. lib/hydration_worker.
            from lib import hydration_worker
            hst = hydration_worker.process_queue()
            if hst.get("status") == "ok":
                log("info", "hydration_run", **{k: v for k, v in hst.items()
                                                if k != "status"})
        except Exception as e:
            log("warn", "hydration_failed", error=str(e)[:160])
        try:
            # files first so the fresh files_index.jsonl feeds this build
            # (big-play tier 1: Drive+PC filenames into the Connect engine)
            from lib import file_indexer
            fst = file_indexer.build()
            log("info", "files_index_refresh",
                files=fst.get("files"), secs=fst.get("seconds"))
        except Exception as e:
            log("warn", "files_index_failed", error=str(e)[:160])
        try:
            from lib import semantic_index
            st = semantic_index.build(force=False)
            log("info", "index_refresh", **{k: v for k, v in st.items()})
        except Exception as e:
            log("warn", "index_refresh_failed", error=str(e)[:160])
        try:
            # Obsidian full mirror — local writes, no API limit, so the whole
            # corpus stays instantiated every cycle. Notion fills separately
            # and incrementally via check_mirror. Bruno 2026-06-12.
            from lib import obsidian_mirror
            ost = obsidian_mirror.mirror_all()
            log("info", "obsidian_mirror", total=ost.get("total_written"))
        except Exception as e:
            log("warn", "obsidian_mirror_failed", error=str(e)[:160])
        finally:
            _index_building = False

    threading.Thread(target=_build, daemon=True, name="core-index").start()


DIGEST_JSON = ROOT / "state" / "daily_digest.json"
DIGEST_MD = ROOT / "state" / "daily_digest.md"
DIGEST_AFTER_HOUR = 8          # generate once per day, first cycle after 08:00
_digest_running = False


def check_digest(u: Unit) -> None:
    """Proactivity (strategy #3): once a day, run the introspection engine and
    assemble a digest Bruno never asked for but wants — what Egon noticed,
    what the other agents did in the last 24h, substrate health. Written to
    state/daily_digest.{json,md}; the Connect widget's tray toasts when a new
    one lands. Rule-based + read-only over mind.db: no LLM, no tokens."""
    global _digest_running
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        prev = json.loads(DIGEST_JSON.read_text(encoding="utf-8")).get("date")
    except Exception:
        prev = None
    u.ok = prev == today
    u.detail = f"last={prev or 'never'}"
    allowed, why = _heavy_allowed()
    if not allowed:
        u.detail += f" ({why})"
        return
    if _digest_running or prev == today or datetime.now().hour < DIGEST_AFTER_HOUR:
        return
    _digest_running = True

    def _build():
        global _digest_running
        try:
            _generate_digest(today)
            log("info", "digest_generated", date=today)
        except Exception as e:
            log("warn", "digest_failed", error=str(e)[:200])
        finally:
            _digest_running = False

    threading.Thread(target=_build, daemon=True, name="core-digest").start()


def _generate_digest(today: str) -> None:
    import sqlite3
    # 1) fresh introspection proposals (rule-based, cheap)
    proposals = []
    try:
        from lib.mind_introspection import run_introspection
        res = run_introspection()
        proposals = res.get("proposals", res) if isinstance(res, dict) else res
    except Exception as e:
        log("warn", "introspection_failed", error=str(e)[:160])
    # 2) what the other agents did in the last 24h (durable memories)
    agent_work = []
    try:
        con = sqlite3.connect(ROOT / "state" / "mind.db", timeout=10)
        con.row_factory = sqlite3.Row
        day_ago = int(time.time()) - 86400
        rows = con.execute(
            """SELECT m.id, m.kind, substr(m.content,1,200) AS preview,
                      COALESCE(a.name,'?') AS agent
               FROM memory m LEFT JOIN agents a ON a.id = m.attribution_agent_id
               WHERE m.created_at >= ? AND m.kind IN ('decision','note','plan')
               ORDER BY m.created_at DESC LIMIT 12""", (day_ago,)).fetchall()
        agent_work = [dict(r) for r in rows]
        con.close()
    except Exception:
        pass
    # 3) substrate health snapshot
    health = {}
    try:
        health = json.loads(HEALTH.read_text(encoding="utf-8")).get("units", {})
    except Exception:
        pass

    digest = {"date": today,
              "generated": datetime.now().isoformat(timespec="seconds"),
              "proposals": proposals if isinstance(proposals, list) else [],
              "agent_work_24h": agent_work,
              "substrate": {k: v.get("ok") for k, v in health.items()}}
    DIGEST_JSON.write_text(json.dumps(digest, indent=2, ensure_ascii=False),
                           encoding="utf-8")
    # human-readable twin
    lines = [f"# Egon Daily Digest — {today}", ""]
    props = digest["proposals"]
    lines.append(f"## Egon noticed ({len(props)} insight{'s' if len(props)!=1 else ''})")
    if props:
        for p in props[:10]:
            lines.append(f"- **{p.get('title','?')}** [{p.get('severity','info')}] — "
                         f"{p.get('description','')[:260]}")
    else:
        lines.append("- Nothing flagged by introspection in the last week.")
    lines.append("")
    lines.append(f"## What your agents did in the last 24h ({len(agent_work)})")
    if agent_work:
        for w in agent_work:
            lines.append(f"- [{w['agent']}] ({w['kind']}) {w['preview'][:160]}…")
    else:
        lines.append("- No durable memories written in the last 24h.")
    lines.append("")
    ok_units = [k for k, v in digest["substrate"].items() if v]
    bad_units = [k for k, v in digest["substrate"].items() if not v]
    lines.append("## Substrate")
    lines.append(f"- healthy: {', '.join(ok_units) or '—'}")
    if bad_units:
        lines.append(f"- ⚠ down: {', '.join(bad_units)}")
    DIGEST_MD.write_text("\n".join(lines), encoding="utf-8")


def write_health(units: dict[str, Unit]) -> None:
    try:
        HEALTH.parent.mkdir(parents=True, exist_ok=True)
        HEALTH.write_text(json.dumps({
            "updated": datetime.now().isoformat(timespec="seconds"),
            "core_pid": __import__("os").getpid(),
            "units": {n: u.as_dict() for n, u in units.items()},
        }, indent=2), encoding="utf-8")
    except Exception:
        pass


# Source snapshots — the store everything downstream reads (mirrors, Connect
# index, dashboards). Was only refreshed by the 06:00 in-app pass, which never
# fired unless the Egon UI happened to be open at 6AM — kindle went stale
# 2026-05-27 and instapaper (3,212 harvested items) NEVER got a snapshot.
# Bruno 2026-06-12: the always-on core owns freshness now. Daily, off-thread.
SNAPSHOTS_EVERY_S = 24 * 3600
_snap_running = False
_snap_last = 0.0


_SNAP_MARK = ROOT / "state" / "snapshots_last_run.json"


def check_snapshots(u: Unit) -> None:
    global _snap_running, _snap_last
    # Gate on OUR last full-refresh time (persisted), NOT the newest snapshot
    # file. 2026-06-13 bug: one fresh source (bookmarks) made the newest-file
    # age <48h, so the whole refresh was skipped — leaving zotero stale at the
    # old 5k snapshot for weeks. Now every source is re-snapshotted daily.
    last = 0.0
    try:
        last = float(json.loads(_SNAP_MARK.read_text(encoding="utf-8"))["ts"])
    except Exception:
        pass
    age_h = (time.time() - last) / 3600 if last else None
    u.ok = age_h is not None and age_h < 48
    u.detail = (f"all-sources age={age_h:.0f}h" if age_h is not None
                else "never") + (" (refreshing)" if _snap_running else "")
    allowed, why = _heavy_allowed()
    if not allowed:
        u.detail += f" ({why})"
        return
    due = last == 0.0 or time.time() - last > SNAPSHOTS_EVERY_S
    if _snap_running or not due or time.time() - _snap_last < 3600:
        return
    _snap_last = time.time()
    _snap_running = True

    def _run():
        global _snap_running
        try:
            import importlib
            from lib.snapshot_store import write_snapshot
            import scripts.pass_sources as _ps  # single source of truth
            pairs = _ps.SNAPSHOT_ADAPTERS
        except Exception:
            # fallback: inline list mirrors scripts/pass.py
            pairs = (
                ("chrome_bookmarks", "lib.adapters.chrome_bookmarks"),
                ("zotero", "lib.adapters.zotero_local"),
                ("letterboxd", "lib.adapters.letterboxd"),
                ("youtube_music", "lib.adapters.youtube"),
                ("notion_workspace", "lib.adapters.notion_workspace"),
                ("tvtime", "lib.adapters.tvtime"),
                ("kindle", "lib.adapters.kindle"),
                ("pocketcasts", "lib.adapters.pocketcasts"),
                ("paperpile", "lib.adapters.paperpile"),
                ("instapaper", "lib.adapters.instapaper"),
                ("youtube_history", "lib.adapters.youtube_history"),
                ("youtube_oauth", "lib.adapters.youtube_oauth"),
                ("trakt", "lib.adapters.trakt"),
            )
        try:
            # Export inbox first (state/inbox/): ANY vendor zip — Takeout
            # (YouTube/Fit/Gemini/My Activity), TV Time, Amazon DSAR — is
            # detected, extracted, parsed and merged before snapshotting.
            from lib import export_inbox
            eres = export_inbox.process()
            if eres.get("imported"):
                log("info", "exports_imported",
                    zips=len(eres["imported"]),
                    detail=str(eres["imported"])[:200])
        except Exception as e:
            log("warn", "export_inbox_failed", error=str(e)[:120])
        try:
            # legacy direct-file path (bare watch-history.json drops)
            from lib import youtube_takeout
            tres = youtube_takeout.import_takeout()
            if tres.get("status") == "ok":
                log("info", "takeout_imported", **{k: tres[k] for k in
                    ("file", "entries", "new") if k in tres})
        except Exception as e:
            log("warn", "takeout_failed", error=str(e)[:120])
        try:
            import importlib
            from lib.snapshot_store import write_snapshot
            done = failed = 0
            for source, modpath in pairs:
                try:
                    mod = importlib.import_module(modpath)
                    snap = mod.snapshot()
                    if snap and snap.get("status") == "ok" and snap.get("items"):
                        write_snapshot(source, snap)
                        done += 1
                except Exception as e:
                    failed += 1
                    log("warn", "snapshot_failed", source=source,
                        error=str(e)[:100])
            try:
                _SNAP_MARK.write_text(json.dumps({"ts": time.time()}),
                                      encoding="utf-8")
            except Exception:
                pass
            log("info", "snapshots_refreshed", ok=done, failed=failed)
            # TV Time -> Trakt: push the (auto-harvested) TV Time back-catalog
            # into Trakt, the durable home. Trakt dedups, so this is safe to
            # run daily; going forward Trakt auto-scrobbles. Once-for-all
            # (Bruno 2026-06-13). No-op unless Trakt is authed + TV Time data.
            try:
                from lib.adapters import trakt
                if trakt.live_status().get("status") == "ok":
                    pr = trakt.push_tvtime_history()
                    if pr.get("status") == "ok":
                        log("info", "tvtime_to_trakt",
                            matched=pr.get("matched"), unmatched=pr.get("unmatched"))
            except Exception as e:
                log("warn", "tvtime_to_trakt_failed", error=str(e)[:120])
        except Exception as e:
            log("warn", "snapshots_run_failed", error=str(e)[:160])
        finally:
            _snap_running = False

    threading.Thread(target=_run, name="egon-snapshots", daemon=True).start()


# Notion mirror increment cadence — every 5 min advance one bounded batch.
# Obsidian is fully mirrored by the index cycle (cheap local writes); Notion
# fills slowly to respect its API. Bruno 2026-06-12.
MIRROR_EVERY_S = int(os.environ.get("EGON_CORE_MIRROR_EVERY_S", "3600"))
MIRROR_BATCH = int(os.environ.get("EGON_CORE_MIRROR_BATCH", "25"))
NOTION_BODY_BATCH = int(os.environ.get("EGON_CORE_NOTION_BODY_BATCH", "5"))
_mirror_last = 0.0
_mirror_running = False


def check_mirror(u: Unit) -> None:
    global _mirror_last, _mirror_running
    state_file = ROOT / "state" / "mirror_runner.json"
    try:
        import json as _json
        cur = _json.loads(state_file.read_text(encoding="utf-8")).get(
            "notion_cursor", {}) if state_file.exists() else {}
        u.ok = True
        u.detail = "notion: " + ", ".join(
            f"{k}={v}" for k, v in list(cur.items())[:4]) if cur else "idle"
    except Exception:
        u.ok = True
        u.detail = "idle"
    allowed, why = _heavy_allowed()
    if not allowed:
        u.detail += f" ({why})"
        return
    if _mirror_running or time.time() - _mirror_last < MIRROR_EVERY_S:
        return
    _mirror_last = time.time()
    _mirror_running = True

    def _run():
        global _mirror_running
        try:
            # Bruno's manual Notion content: refresh page BODIES first (cached
            # by last_edited_time, ~60 pages/run) so the Obsidian mirror
            # carries full text, not stubs. #66, 2026-06-12.
            from lib import notion_body
            bres = notion_body.refresh(batch=NOTION_BODY_BATCH)
            if bres.get("fetched"):
                log("info", "notion_bodies", **{k: bres[k] for k in
                    ("fetched", "cached_total") if k in bres})
        except Exception as e:
            log("warn", "notion_bodies_failed", error=str(e)[:120])
        try:
            from lib import mirror_runner
            res = mirror_runner.run_notion_increment(batch=MIRROR_BATCH)
            log("info", "mirror_increment", pushed=res.get("pushed"),
                status=res.get("status"))
        except Exception as e:
            log("warn", "mirror_failed", error=str(e)[:160])
        finally:
            _mirror_running = False

    threading.Thread(target=_run, name="egon-mirror", daemon=True).start()


def main() -> int:
    # FAIL-SAFE single-instance guard: if the guard cannot run, EXIT rather than
    # proceed unguarded — two unguarded cores = two adb keepalive loops = the PC
    # crash on Chrome-open (2026-06-15 & -17). Never start unless provably alone.
    try:
        from lib.single_instance_mutex import claim_or_exit
        alone = claim_or_exit("Egon-Core-2026-06")
    except Exception as e:
        log("error", "core_guard_failed_exit", error=str(e)[:160])
        return 0
    if not alone:
        log("info", "core_already_running_exit")
        return 0

    log("info", "core_start")
    units = {"mind": Unit("mind"), "headroom": Unit("headroom"),
             "ollama": Unit("ollama"),
             "connect_index": Unit("connect_index"),
             "daily_digest": Unit("daily_digest"),
             "snapshots": Unit("snapshots"),
             "mirror": Unit("mirror")}
    while True:
        try:
            check_mind(units["mind"])
            check_headroom(units["headroom"])
            check_ollama(units["ollama"])
            check_index(units["connect_index"])
            check_digest(units["daily_digest"])
            check_snapshots(units["snapshots"])
            check_mirror(units["mirror"])
            write_health(units)
        except Exception as e:
            log("error", "core_cycle_error", error=str(e)[:200])
        time.sleep(CHECK_EVERY_S)


if __name__ == "__main__":
    raise SystemExit(main())
