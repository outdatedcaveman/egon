"""Daily pass service — fires Egon's adapter snapshot at 06:00 local
each day, but ONLY while Egon is open.

Bruno's standing rule (2026-05-27): the daily 06:00 routine is kept,
but never via Windows Scheduled Tasks — only via an in-app QTimer
that ticks while Egon's UI is open. If Egon is closed at 06:00,
nothing fires that day. That's the trade-off Bruno explicitly chose:
no background services that run when the master program isn't open.

Behavior:
  • At app boot, the service schedules a QTimer.singleShot to fire at
    the next local 06:00.
  • If Egon launches between 06:00 and 12:00 (i.e. missed today's
    06:00 by less than 6 hours), it fires once immediately as a
    catch-up, then schedules tomorrow's 06:00.
  • After firing, it reschedules itself for the next 06:00.
  • On QApplication.aboutToQuit, the timer is stopped and the worker
    thread is asked to finish.

The pass itself runs in a daemon thread (mirrors what `Run pass now`
does on the toolbar) so the UI never blocks. We call
`data.trigger_pass('daily')`, same entry point the button uses.
"""
from __future__ import annotations

import threading
from datetime import datetime, time, timedelta
from typing import Callable

from PySide6.QtCore import QObject, QTimer

DAILY_HOUR_LOCAL = 6           # 06:00 local
CATCHUP_WINDOW_HOURS = 6       # 06:00 → 12:00 = catch-up on first launch


def _seconds_to_next_run(now: datetime | None = None) -> tuple[int, bool]:
    """Returns (seconds_until_next_fire, catchup_now).

    catchup_now is True when we should fire immediately because Egon
    launched in the catch-up window AND we haven't run yet today.
    """
    now = now or datetime.now()
    today_target = now.replace(hour=DAILY_HOUR_LOCAL, minute=0,
                               second=0, microsecond=0)
    if now < today_target:
        # Haven't hit 06:00 yet today
        return int((today_target - now).total_seconds()), False
    # We're past today's 06:00. Are we within the catch-up window?
    delta_since = now - today_target
    if delta_since <= timedelta(hours=CATCHUP_WINDOW_HOURS):
        return 0, True
    # Outside catch-up window — schedule tomorrow's 06:00
    tomorrow = today_target + timedelta(days=1)
    return int((tomorrow - now).total_seconds()), False


def _seconds_to_tomorrow_06() -> int:
    now = datetime.now()
    today_target = now.replace(hour=DAILY_HOUR_LOCAL, minute=0,
                               second=0, microsecond=0)
    if now < today_target:
        return int((today_target - now).total_seconds())
    return int((today_target + timedelta(days=1) - now).total_seconds())


class DailyPassService(QObject):
    """Lifecycle wrapper. Public API: start(), stop(), is_running(),
    next_run_iso() (for the dashboard)."""

    def __init__(self, run_pass: Callable[[], tuple[bool, str]] | None = None,
                 parent: QObject | None = None):
        super().__init__(parent)
        # Allow injection for testability; default to the same path the
        # toolbar 'Run pass now' button uses.
        if run_pass is None:
            def _default():
                from egon_app import data
                return data.trigger_pass("daily")
            run_pass = _default
        self._run_pass = run_pass
        self._timer: QTimer | None = None
        self._next_fire_at: datetime | None = None
        self._running_pass = False
        self._stopped = False

    def is_running(self) -> bool:
        return self._timer is not None and self._timer.isActive()

    def next_run_iso(self) -> str | None:
        return self._next_fire_at.isoformat(timespec="seconds") \
            if self._next_fire_at else None

    def start(self) -> None:
        if self.is_running():
            return
        self._stopped = False
        self._schedule()

    def stop(self) -> None:
        self._stopped = True
        if self._timer is not None:
            try:
                self._timer.stop()
            except Exception:
                pass
            self._timer = None
        self._next_fire_at = None

    # ── internal ──────────────────────────────────────────────────────────

    def _schedule(self) -> None:
        if self._stopped:
            return
        secs, catchup = _seconds_to_next_run()
        self._next_fire_at = datetime.now() + timedelta(seconds=secs)
        # QTimer is millisecond-precision; cap at 24h32m so any clock
        # weirdness re-evaluates safely.
        secs = max(0, min(secs, 24 * 3600 + 30 * 60))
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._fire)
        self._timer.start(secs * 1000)
        if catchup and secs == 0:
            # Fire the catch-up immediately and reschedule for tomorrow.
            QTimer.singleShot(0, self._fire)

    def _fire(self) -> None:
        if self._stopped or self._running_pass:
            return
        self._running_pass = True

        def _run():
            try:
                self._run_pass()
            except Exception:
                pass
            # Notion push — write today's status back to the designated page.
            # Best-effort: failure here must not affect rescheduling. Reads
            # state/last_pass.json (just written by the pass above) to build
            # the bullet list. Bruno 2026-05-29.
            try:
                self._push_notion_status()
            except Exception:
                pass
            try:
                pass
            finally:
                self._running_pass = False
                # Re-schedule for the next 06:00 regardless of outcome.
                if not self._stopped:
                    # Move scheduling back to the Qt thread.
                    QTimer.singleShot(0, self._reschedule_tomorrow)

        threading.Thread(target=_run, daemon=True,
                         name="egon-daily-pass").start()

    def _push_notion_status(self) -> None:
        """Read state/last_pass.json and append today's summary to the
        designated Notion status page. Silently no-ops if disabled
        (no NOTION_TOKEN, no status_page_id, or already pushed today)."""
        import json
        from pathlib import Path

        last_pass_path = (Path(__file__).resolve().parent.parent.parent
                          / "state" / "last_pass.json")
        if not last_pass_path.exists():
            return
        try:
            with last_pass_path.open(encoding="utf-8") as f:
                lp = json.load(f)
        except Exception:
            return

        date_str = datetime.now().strftime("%Y-%m-%d")
        # Build a compact bullet list: one line per source with status/count.
        lines: list[str] = [
            f"Pass: {lp.get('reason','daily')}  "
            f"duration={lp.get('duration_s','?')}s  "
            f"items={lp.get('total_items','?')}",
        ]
        sources = lp.get("sources") or {}
        if isinstance(sources, dict):
            for name, info in sorted(sources.items()):
                if not isinstance(info, dict):
                    continue
                status = info.get("status", "?")
                count = (info.get("item_count")
                         or info.get("count")
                         or info.get("items")
                         or "")
                err = info.get("error") or ""
                line = f"{name}: {status}"
                if count != "":
                    line += f" ({count})"
                if err:
                    line += f" — {str(err)[:140]}"
                lines.append(line)

        try:
            from lib.adapters.notion import push_daily_status
            push_daily_status(date_str, lines)
        except Exception:
            pass

    def _reschedule_tomorrow(self) -> None:
        secs = _seconds_to_tomorrow_06()
        self._next_fire_at = datetime.now() + timedelta(seconds=secs)
        if self._timer is not None:
            try:
                self._timer.stop()
            except Exception:
                pass
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._fire)
        self._timer.start(secs * 1000)
