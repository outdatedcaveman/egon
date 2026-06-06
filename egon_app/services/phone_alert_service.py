"""Phone alert — native tray notification when the phone link needs you.

Bruno 2026-06-01: "will the program ALWAYS tell me when I need to do this?"
Yes. The keepalive writes state/panop/phone_status.json; this service polls it
and, the moment the link drops into a state Egon can't auto-heal (phone not
reachable AND not plugged in over USB), pops a native Windows toast via
QSystemTrayIcon — so you're told even when you're not looking at the Inbox tab.
It also gives Egon a tray presence; clicking it raises the window. Dies with
Egon like every other in-app service.
"""
from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QObject, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QSystemTrayIcon

ROOT = Path(__file__).resolve().parent.parent.parent
STATUS_FILE = ROOT / "state" / "panop" / "phone_status.json"
ICON = ROOT / "shell" / "egon.ico"
POLL_MS = 20_000


class PhoneAlertService(QObject):
    def __init__(self, app, parent: QObject | None = None):
        super().__init__(parent)
        self._app = app
        self._tray: QSystemTrayIcon | None = None
        self._timer: QTimer | None = None
        self._last_needs_action: bool | None = None

    def start(self) -> None:
        try:
            if not QSystemTrayIcon.isSystemTrayAvailable():
                return
            self._tray = QSystemTrayIcon(self)
            if ICON.exists():
                self._tray.setIcon(QIcon(str(ICON)))
            self._tray.setToolTip("Egon")
            self._tray.activated.connect(self._on_activated)
            self._tray.show()
            self._timer = QTimer(self)
            self._timer.setInterval(POLL_MS)
            self._timer.timeout.connect(self._check)
            self._timer.start()
            QTimer.singleShot(4000, self._check)   # first check after launch
        except Exception:
            pass

    def stop(self) -> None:
        try:
            if self._timer is not None:
                self._timer.stop()
            if self._tray is not None:
                self._tray.hide()
        except Exception:
            pass

    # ── internal ────────────────────────────────────────────────────────────
    def _read(self) -> dict | None:
        try:
            return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _check(self) -> None:
        d = self._read()
        if not d or self._tray is None:
            return
        needs = bool(d.get("needs_action"))
        # Toast only on the transition INTO needs_action (don't nag every poll).
        if needs and self._last_needs_action is not True:
            try:
                self._tray.showMessage(
                    "Egon — phone needs reconnecting",
                    d.get("message", "Plug your phone in via USB and enable "
                                     "USB debugging; Egon will reconnect."),
                    QSystemTrayIcon.MessageIcon.Warning, 20_000)
            except Exception:
                pass
        # Reflect state in the tray tooltip too.
        try:
            self._tray.setToolTip(
                "Egon — phone " + ("⚠ disconnected" if needs else "connected"))
        except Exception:
            pass
        self._last_needs_action = needs

    def _on_activated(self, reason) -> None:
        win = getattr(self._app, "_egon_main_window", None)
        if win is not None:
            try:
                win.show()
                win.raise_()
                win.activateWindow()
            except Exception:
                pass
