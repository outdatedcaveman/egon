"""Sync page — Windows scheduled tasks + log tail + manual triggers.

The schtasks subprocess is slow (~5-10s on a fresh boot), so we run it on a
QThread to keep the UI responsive.
"""
from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QThread, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QPlainTextEdit, QMessageBox,
)

from egon_app import data

EGON_LOG = Path(__file__).resolve().parent.parent.parent / "logs"


class _SchTasksWorker(QThread):
    """Runs `schtasks /Query` off the GUI thread."""
    finished_with = Signal(list)  # emits list[dict]

    def run(self):
        out: list[dict] = []
        try:
            res = subprocess.run(
                ["schtasks", "/Query", "/FO", "CSV", "/V"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=15,
            )
            if res.returncode == 0:
                import csv
                seen = set()
                reader = csv.reader(res.stdout.splitlines())
                for cells in reader:
                    if not cells or len(cells) < 4:
                        continue
                    # search anywhere in cells for Egon / KMS
                    line_str = " ".join(cells)
                    if "KMS-" not in line_str and "Egon-" not in line_str:
                        continue
                    name = cells[1].lstrip("\\")
                    if name in seen:
                        continue
                    seen.add(name)
                    out.append({"name": name, "next_run": cells[2], "status": cells[3]})
        except Exception:
            pass
        self.finished_with.emit(out)


def _section_title(text: str) -> QLabel:
    l = QLabel(text)
    l.setStyleSheet("font-size: 14px; font-weight: 600; color: #F0E9D5; padding: 6px 0;")
    return l


class SyncPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 24, 28, 24)
        outer.setSpacing(14)

        title = QLabel("Sync")
        title.setStyleSheet("font-size: 22px; font-weight: 700; color: #F0E9D5;")
        outer.addWidget(title)
        sub = QLabel("Scheduled jobs · last runs · log tails · re-trigger.")
        sub.setStyleSheet("color: #9CA3AF;")
        outer.addWidget(sub)

        outer.addWidget(_section_title("Windows Scheduled Tasks (KMS-* / Egon-*)"))
        self._tasks_table = QTableWidget(0, 3)
        self._tasks_table.setHorizontalHeaderLabels(["Task", "Next run", "Status"])
        _th = self._tasks_table.horizontalHeader()
        _th.setSectionResizeMode(QHeaderView.Interactive)   # all columns draggable
        _th.setStretchLastSection(True)
        self._tasks_table.setColumnWidth(0, 360)
        self._tasks_table.setColumnWidth(1, 200)
        self._tasks_table.verticalHeader().setVisible(False)
        self._tasks_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._tasks_table.setStyleSheet(
            "QTableWidget { background: #102F3C; color: #F0E9D5; gridline-color: #1F4858; "
            "border: 1px solid #1F4858; border-radius: 6px; }"
            "QHeaderView::section { background: #16404F; color: #9CA3AF; padding: 6px; "
            "border: none; border-bottom: 1px solid #1F4858; font-weight: 600; }"
        )
        self._tasks_table.setMaximumHeight(220)
        outer.addWidget(self._tasks_table)

        # log tail
        ym = datetime.now().strftime("%Y-%m")
        self._log_path = EGON_LOG / f"pass-{ym}.log"
        outer.addWidget(_section_title(f"Last 30 lines · {self._log_path.name}"))
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setFont(QFont("Cascadia Mono", 10))
        self._log.setStyleSheet(
            "QPlainTextEdit { background: #0B1F28; color: #9CA3AF; border: 1px solid #1F4858; "
            "border-radius: 4px; padding: 8px; }"
        )
        self._log.setMaximumHeight(280)
        outer.addWidget(self._log, 1)

        # triggers
        outer.addWidget(_section_title("Manual triggers"))
        row = QHBoxLayout()
        for label, kind, primary in [
            ("Run daily pass now", "daily", True),
            ("Notion → vault mirror", "mirror", False),
            ("Inbox-only pass", "inbox", False),
        ]:
            b = QPushButton(label)
            if primary:
                b.setStyleSheet("background: #60A5A8; color: white; padding: 8px 16px; "
                                "border-radius: 4px; font-weight: 600;")
            b.clicked.connect(lambda _=False, k=kind: self._trigger(k))
            row.addWidget(b)
        row.addStretch(1)
        outer.addLayout(row)

        self.refresh()
        self._timer = QTimer(self)
        self._timer.setInterval(30_000)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()

    def refresh(self) -> None:
        # async schtasks
        self._tasks_worker = _SchTasksWorker()
        self._tasks_worker.finished_with.connect(self._on_tasks)
        self._tasks_worker.start()
        # log tail (cheap, just file read)
        try:
            if self._log_path.exists():
                with self._log_path.open(encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()[-30:]
                self._log.setPlainText("".join(lines))
            else:
                self._log.setPlainText("(no log yet)")
        except Exception as e:
            self._log.setPlainText(f"(error reading log: {e})")

    def _on_tasks(self, tasks: list) -> None:
        self._tasks_table.setRowCount(0)
        if not tasks:
            self._tasks_table.insertRow(0)
            self._tasks_table.setItem(0, 0, QTableWidgetItem("(none found)"))
            return
        for t in tasks:
            r = self._tasks_table.rowCount()
            self._tasks_table.insertRow(r)
            for c, val in enumerate([t["name"], t["next_run"], t["status"]]):
                item = QTableWidgetItem(str(val))
                if c == 2:
                    if "Ready" in str(val):
                        item.setForeground(Qt.GlobalColor.green)
                    elif "Running" in str(val):
                        item.setForeground(Qt.GlobalColor.cyan)
                    else:
                        item.setForeground(Qt.GlobalColor.yellow)
                self._tasks_table.setItem(r, c, item)

    def _trigger(self, kind: str) -> None:
        ok, msg = data.trigger_pass(kind)
        QMessageBox.information(self, f"{kind} pass", msg)
