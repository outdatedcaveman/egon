"""Shared snapshot runner for Egon.

This module coordinates running export inbox parsing and snapshot updates for
all adapters (Chrome Bookmarks, Zotero, Kindle, etc.) daily. It is designed to
be called both by egon_core.py (the desktop GUI) and mind_ingest.py (the background service).
"""
from __future__ import annotations

import ctypes
import importlib
import json
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SNAP_MARK = ROOT / "state" / "snapshots_last_run.json"
LOG_PATH = ROOT / "logs" / "snapshots.log"

_run_lock = threading.Lock()
_running = False

# Default snapshot adapters list from scripts/egon_core.py
ADAPTERS = (
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


def _log(level: str, event: str, **data) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().isoformat(timespec="seconds")
        tail = " ".join(f"{k}={v}" for k, v in data.items())
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(f"{stamp} [{level}] event={event} {tail}\n")
        print(f"[snapshots] {stamp} [{level}] {event} {tail}", flush=True)
    except Exception:
        pass


def _idle_seconds() -> float:
    """Seconds since last keyboard/mouse input on Windows."""
    if os.name != "nt":
        return 0.0
    try:
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


def is_heavy_allowed(caller: str = "core") -> tuple[bool, str]:
    """Check if CPU-heavy snapshots are allowed to run."""
    mode = os.environ.get("EGON_CORE_HEAVY_MODE", "manual").strip().lower()
    idle_after = int(os.environ.get("EGON_CORE_HEAVY_IDLE_AFTER_S", "1800"))

    if mode in ("1", "true", "yes", "always"):
        return True, "heavy mode always"
    
    # Headless caller (standalone mind service) fallback:
    # If set to manual/off but called headless, we auto-run it if the system has been idle for 15 mins (900s)
    # or if the last run is extremely stale (>36 hours).
    if mode in ("off", "0", "false", "no", "manual", ""):
        if caller == "headless":
            idle = _idle_seconds()
            if idle >= 900:  # 15 minutes
                return True, f"headless service: system idle {int(idle)}s"
            
            # Check last run age
            last = 0.0
            if SNAP_MARK.exists():
                try:
                    last = float(json.loads(SNAP_MARK.read_text(encoding="utf-8"))["ts"])
                except Exception:
                    pass
            age_h = (time.time() - last) / 3600 if last else None
            if age_h is None or age_h > 36:
                return True, f"headless service: failsafe stale age={age_h:.1f}h"
                
            return False, f"headless service: user active, idle {int(idle)}s/900s"
        return False, "paused: set EGON_CORE_HEAVY_MODE=idle or always"

    if mode == "idle":
        idle = _idle_seconds()
        if idle >= idle_after:
            return True, f"system idle {int(idle)}s"
        return False, f"paused: active user, idle {int(idle)}s/{idle_after}s"

    return False, f"paused: unknown heavy mode {mode!r}"


def run_snapshots_if_due(force: bool = False, caller: str = "core") -> dict:
    """Trigger Egon exports and snapshots if due (>24h elapsed) or forced.

    Runs in a background thread to prevent blocking.
    """
    global _running
    if _running:
        return {"status": "already_running"}

    last = 0.0
    if SNAP_MARK.exists():
        try:
            last = float(json.loads(SNAP_MARK.read_text(encoding="utf-8"))["ts"])
        except Exception:
            pass

    age_h = (time.time() - last) / 3600 if last else None
    due = force or last == 0.0 or (time.time() - last > 86400)

    if not due:
        return {"status": "skipped", "reason": f"last run was {age_h:.1f}h ago (limit 24h)"}

    # Check heavy allowed if not forced
    if not force:
        allowed, reason = is_heavy_allowed(caller)
        if not allowed:
            return {"status": "skipped", "reason": reason}

    # Start thread
    with _run_lock:
        if _running:
            return {"status": "already_running"}
        _running = True

    def _worker():
        global _running
        _log("info", "started", force=force, caller=caller)
        try:
            # 1. Process inbox exports
            try:
                from lib import export_inbox
                eres = export_inbox.process()
                if eres.get("imported"):
                    _log("info", "exports_imported", count=len(eres["imported"]))
            except Exception as e:
                _log("warn", "export_inbox_failed", error=str(e)[:120])

            # 2. Import YouTube Takeout
            try:
                from lib import youtube_takeout
                tres = youtube_takeout.import_takeout()
                if tres.get("status") == "ok":
                    _log("info", "takeout_imported", new=tres.get("new", 0))
            except Exception as e:
                _log("warn", "takeout_failed", error=str(e)[:120])

            # 3. Snapshot adapters
            from lib.snapshot_store import write_snapshot
            done = failed = 0
            
            pairs = ADAPTERS
            try:
                import scripts.pass_sources as _ps
                pairs = _ps.SNAPSHOT_ADAPTERS
            except Exception:
                pass

            for source, modpath in pairs:
                try:
                    mod = importlib.import_module(modpath)
                    snap = mod.snapshot()
                    if snap and snap.get("status") == "ok" and snap.get("items"):
                        write_snapshot(source, snap)
                        done += 1
                except Exception as e:
                    failed += 1
                    _log("warn", "snapshot_failed", source=source, error=str(e)[:100])

            # 4. Save last run timestamp
            try:
                SNAP_MARK.parent.mkdir(parents=True, exist_ok=True)
                SNAP_MARK.write_text(json.dumps({"ts": time.time()}), encoding="utf-8")
            except Exception:
                pass

            # 5. Push TV Time to Trakt
            try:
                from lib.adapters import trakt
                if trakt.live_status().get("status") == "ok":
                    pr = trakt.push_tvtime_history()
                    if pr.get("status") == "ok":
                        _log("info", "tvtime_to_trakt", matched=pr.get("matched"), unmatched=pr.get("unmatched"))
            except Exception as e:
                _log("warn", "tvtime_to_trakt_failed", error=str(e)[:120])

            _log("info", "completed", ok=done, failed=failed)

            # Automatically trigger semantic index rebuild asynchronously
            try:
                from lib import semantic_index as si
                _log("info", "trigger_index_rebuild")
                si.ensure_built_async()
            except Exception as e:
                _log("warn", "index_rebuild_trigger_failed", error=str(e)[:100])

        except Exception as e:
            _log("error", "run_failed", error=str(e)[:160])
        finally:
            _running = False

    t = threading.Thread(target=_worker, name="egon-snapshots-shared", daemon=True)
    t.start()
    return {"status": "started"}
