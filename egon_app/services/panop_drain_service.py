"""Panop drain service — runs the ORIGINAL Panop capture routine on a
schedule, entirely inside Egon.

Background
----------
Panop's phone-tab routine (drain → fetch → classify → export → clean) used to
fire from a Windows scheduled task that ran `scripts/run_panop_capture.ps1`,
i.e. `lib.adapters.panop_capture.run_capture()`. Bruno's 2026-05-27 rule
("nothing runs outside Egon") meant that task was disabled — and the only
remaining trigger, Panop's in-process `adb_loop`, waits `interval_hours` FROM
LAUNCH and never persists, so with Egon opening/closing it effectively never
fired. The drain silently stopped on 2026-05-28. This service restores it,
contained entirely within Egon.

What it runs (UNCHANGED — we only trigger it)
---------------------------------------------
`run_capture()` performs the exact original Panop routine, with every safety
restraint intact:
  • discovers the phone over wireless ADB (mDNS first, static-IP fallback),
  • wakes the screen + foregrounds Chrome,
  • `run_adb_sweep()`: reads open tabs, classifies each ONLY into the
    pre-defined categories, fetches metadata, creates a restore point, saves
    to bookmarks AND Zotero, and — only if `close_tabs_after_save` is enabled
    — closes a tab via the hard `_safe_to_close` gate (never closes a tab that
    isn't already saved AND categorized; never closes uncategorized tabs).
We do not touch any of that. This module is purely the scheduler.

Schedule
--------
  • Every `interval_hours` (Panop config, default 6 h) while Egon is open.
  • Catch-up on launch: if it's been ≥ interval since the last run, fire once
    shortly after launch (gives Panop/ADB a moment to settle).
  • Only while Egon is open; dies with Egon. Last-run time persists to
    state/panop_drain_last.json so catch-up survives restarts.
  • Never overlaps a run already in progress.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QTimer

_ROOT = Path(__file__).resolve().parent.parent.parent
_LAST_RUN_FILE = _ROOT / "state" / "panop_drain_last.json"

DEFAULT_INTERVAL_HOURS = 6
LAUNCH_CATCHUP_DELAY_S = 45      # let Panop bind :8000 + ADB settle first
MIN_INTERVAL_S = 30 * 60         # safety floor so a misconfig can't hammer the phone


class PanopDrainService(QObject):
    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._timer: QTimer | None = None
        self._running = False
        self._stopped = False

    # ── public API ─────────────────────────────────────────────────────────
    def start(self) -> None:
        self._stopped = False
        # Catch-up shortly after launch if a run is due.
        due_in = self._seconds_until_due()
        first = LAUNCH_CATCHUP_DELAY_S if due_in <= 0 else min(due_in, self._interval_s())
        self._arm(first)

    def stop(self) -> None:
        self._stopped = True
        if self._timer is not None:
            try:
                self._timer.stop()
            except Exception:
                pass
            self._timer = None

    def is_running(self) -> bool:
        return self._running

    # ── scheduling ─────────────────────────────────────────────────────────
    def _interval_s(self) -> int:
        """Panop's own interval_hours, clamped to a safe floor."""
        hours = DEFAULT_INTERVAL_HOURS
        try:
            from external.panop_server import main as _pm
            hours = float(_pm.get_env().get("interval_hours", DEFAULT_INTERVAL_HOURS))
        except Exception:
            pass
        return max(MIN_INTERVAL_S, int(hours * 3600))

    def _last_run_ts(self) -> float:
        try:
            return float(json.loads(_LAST_RUN_FILE.read_text(encoding="utf-8")).get("ts", 0))
        except Exception:
            return 0.0

    def _save_last_run(self) -> None:
        try:
            _LAST_RUN_FILE.parent.mkdir(parents=True, exist_ok=True)
            _LAST_RUN_FILE.write_text(
                json.dumps({"ts": time.time(),
                            "iso": datetime.now().isoformat(timespec="seconds")}),
                encoding="utf-8")
        except Exception:
            pass

    def _seconds_until_due(self) -> int:
        last = self._last_run_ts()
        if last <= 0:
            return 0  # never run → due now (catch-up)
        return int(self._interval_s() - (time.time() - last))

    def _arm(self, secs: int) -> None:
        if self._stopped:
            return
        secs = max(1, int(secs))
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._fire)
        self._timer.start(secs * 1000)

    def _arm_next(self) -> None:
        self._arm(self._interval_s())

    # ── firing ─────────────────────────────────────────────────────────────
    def _fire(self) -> None:
        if self._stopped or self._running:
            # Already draining (or shutting down) — reschedule and bail.
            if not self._stopped:
                self._arm_next()
            return
        self._running = True

        def _run():
            try:
                # Belt-and-braces: skip if Panop's own sweep is already running
                # (e.g. the in-process adb_loop happened to fire). Never overlap.
                try:
                    from external.panop_server import main as _pm
                    if getattr(_pm, "sweep_status", {}).get("running"):
                        return
                except Exception:
                    pass
                from lib.adapters.panop_capture import run_capture
                run_capture()
            except Exception:
                pass
            finally:
                self._save_last_run()
                self._running = False
                if not self._stopped:
                    QTimer.singleShot(0, self._arm_next)

        threading.Thread(target=_run, daemon=True, name="egon-panop-drain").start()
