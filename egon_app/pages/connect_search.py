"""Connect & Search — one home for everything retrieval.

Bruno 2026-06-12: "join the connect feature with the search window, it's
more thematically similar and they should be together — and have the button
to launch the floating context-window capturer there too."

Layout: a header row with the ✨ capturer launcher (the Ctrl+Alt+E
ask-about-this-screen overlay, spawned hidden via pythonw; the widget's own
kernel mutex makes double-launch a no-op), then two tabs hosting the
existing pages unchanged:
  ✨ Connect — paste text → ranked semantic connections + synthesis
  🔍 Search  — keyword search across the archive snapshots
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QTabWidget,
)

ROOT = Path(__file__).resolve().parent.parent.parent


class ConnectSearchPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(24, 18, 24, 12)
        v.setSpacing(10)

        head = QHBoxLayout()
        title = QLabel("✨ Connect & Search")
        title.setStyleSheet("color: #f5f5f7; font-size: 20px; font-weight: 700;")
        head.addWidget(title)
        head.addStretch(1)

        self._cap_status = QLabel("")
        self._cap_status.setStyleSheet("color: #76767f; font-size: 11px;")
        head.addWidget(self._cap_status)
        cap_btn = QPushButton("🖥️ Launch screen capturer (Ctrl+Alt+E)")
        cap_btn.setToolTip(
            "Starts the floating 'ask about this screen' overlay: press "
            "Ctrl+Alt+E anywhere, drag over any region, and its text is "
            "OCR'd and connected to your archives. Single-instance — safe "
            "to click again.")
        cap_btn.setStyleSheet(
            "QPushButton { background: #ff9f0a; color: #0c0d0f; padding: 6px 14px; "
            "border-radius: 4px; font-weight: 700; border: none; }"
            "QPushButton:hover { background: #E0B45E; }")
        cap_btn.clicked.connect(self._launch_capturer)
        head.addWidget(cap_btn)
        v.addLayout(head)

        tabs = QTabWidget()
        tabs.setStyleSheet(
            "QTabWidget::pane { border: 1px solid #22252a; border-radius: 6px; }"
            "QTabBar::tab { background: #0c0d0f; color: #76767f; padding: 7px 18px; "
            "border-top-left-radius: 6px; border-top-right-radius: 6px; font-weight: 600; }"
            "QTabBar::tab:selected { background: #212328; color: #f5f5f7; }")
        from egon_app.pages.connect import ConnectPage
        from egon_app.pages.search import SearchPage
        self._connect = ConnectPage()
        self._search = SearchPage()
        tabs.addTab(self._connect, "✨ Connect")
        tabs.addTab(self._search, "🔍 Search")
        v.addWidget(tabs, 1)

    def _launch_capturer(self) -> None:
        pyw = ROOT / ".venv" / "Scripts" / "pythonw.exe"
        script = ROOT / "scripts" / "connect_widget.py"
        try:
            subprocess.Popen(
                [str(pyw), str(script)], cwd=str(ROOT),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=0x08000008)  # no window, detached
            self._cap_status.setText(
                "capturer up — press Ctrl+Alt+E over anything")
        except Exception as e:
            self._cap_status.setText(f"launch failed: {e}")
