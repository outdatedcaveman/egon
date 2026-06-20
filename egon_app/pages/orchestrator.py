"""Orchestrator page — multi-agent goal planner & task tracking.

Allows typing a single high-level prompt, decomposing it into specialized tasks
for agents (claude-code, antigravity, hermes, codex), and monitoring progress.
"""
from __future__ import annotations

import webbrowser
from PySide6.QtCore import Qt, QTimer, QThread, Signal, QObject
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QPlainTextEdit,
    QScrollArea, QFrame, QGridLayout, QSizePolicy
)
from egon_app import theme

_API = "http://127.0.0.1:8000/api/v1/mind"

# Shared palette matching Egon theme
_BG_CARD = "#0E2630"
_BORDER  = "#1F4858"
_ACCENT  = "#7BC5C7"
_TEXT    = "#F0E9D5"
_MUTED   = "#9CA3AF"
_GOLD    = "#D4A24C"
_OK      = "#7FB069"
_ERR     = "#D67A6A"
_PENDING = "#E2A844"

_AGENT_COLORS = {
    "claude-code": "#D77A56",
    "codex":       "#7BC5C7",
    "antigravity": "#9D7BC5",
    "hermes":      "#7FB069",
}

class _HttpWorker(QObject):
    finished = Signal(dict)

    def __init__(self, method: str, url: str, timeout: float = 8.0, json_body: dict | None = None):
        super().__init__()
        self._method = method
        self._url = url
        self._timeout = timeout
        self._json_body = json_body

    def run(self) -> None:
        try:
            from egon_app.api import get_compat, post_compat
            if self._method.upper() == "GET":
                r = get_compat(self._url, timeout=self._timeout)
            else:
                r = post_compat(self._url, self._json_body, timeout=self._timeout)
            if r.status_code < 400:
                try:
                    body = r.json()
                except Exception:
                    body = {"status": "ok", "response": r.text}
                self.finished.emit({"ok": True, "data": body, "error": ""})
            else:
                self.finished.emit({"ok": False, "data": None, "error": f"HTTP {r.status_code}"})
        except Exception as exc:
            self.finished.emit({"ok": False, "data": None, "error": str(exc)[:300]})

def _spawn_http(parent: QWidget, method: str, url: str,
                callback, timeout: float = 8.0, json_body: dict | None = None) -> QThread:
    thread = QThread(parent)
    worker = _HttpWorker(method, url, timeout, json_body)
    worker.moveToThread(thread)
    thread._worker = worker
    thread.started.connect(worker.run)
    worker.finished.connect(callback)
    worker.finished.connect(thread.quit)
    worker.finished.connect(worker.deleteLater)
    thread.finished.connect(thread.deleteLater)
    thread.start()
    return thread

class OrchestratorPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._threads: list[QThread] = []
        self._build()
        
        # Poll status every 5 seconds when visible
        self._timer = QTimer(self)
        self._timer.setInterval(5000)
        self._timer.timeout.connect(self.refresh)

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 18, 24, 18)
        root.setSpacing(12)

        # Header Title
        title = QLabel("AI Orchestrator")
        title.setStyleSheet(f"color:{_TEXT}; font-size:22px; font-weight:700;")
        root.addWidget(title)
        
        sub = QLabel("Type a high-level command to decompose it into sub-tasks for "
                     "active agent bodies (claude-code, antigravity, hermes, codex). "
                     "Sub-tasks are dynamically queued and injected into agents' context when they run.")
        sub.setStyleSheet(f"color:{_MUTED};")
        sub.setWordWrap(True)
        root.addWidget(sub)

        # Text input panel
        self._input = QPlainTextEdit()
        self._input.setPlaceholderText("Decompose and delegate a goal... (e.g., 'Check database stats, refactor synthesis, and run a workspace audit')")
        self._input.setStyleSheet(
            f"QPlainTextEdit {{ background:#102F3C; color:{_TEXT}; "
            f"border:1px solid {_BORDER}; border-radius:6px; padding:8px; "
            f"font-size:13px; }}")
        self._input.setFixedHeight(80)
        root.addWidget(self._input)

        # Buttons and dispatch state row
        row = QHBoxLayout()
        self._btn_dispatch = QPushButton("🪄 Decompose & Dispatch")
        self._btn_dispatch.setStyleSheet(
            f"QPushButton {{ background:{_GOLD}; color:#0E2630; border:none; "
            f"border-radius:6px; padding:8px 22px; font-weight:700; font-size:13px; }}")
        self._btn_dispatch.clicked.connect(self._dispatch)
        row.addWidget(self._btn_dispatch)
        
        self._status = QLabel("Ready")
        self._status.setStyleSheet(f"color:{_MUTED};")
        row.addWidget(self._status)
        row.addStretch(1)
        root.addLayout(row)

        # Content areas (Grid of Agent Cards + Scrollable Queue)
        content_layout = QHBoxLayout()
        content_layout.setSpacing(16)

        # Left: Agent status grid (2x2)
        grid_widget = QWidget()
        self._grid = QGridLayout(grid_widget)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(10)
        
        # Store references to cards
        self._agent_cards: dict[str, dict] = {}
        agents = ["claude-code", "antigravity", "hermes", "codex"]
        for idx, name in enumerate(agents):
            card = QFrame()
            card.setObjectName("agentCard")
            card.setStyleSheet(
                f"QFrame#agentCard {{ background:{_BG_CARD}; border:1px solid {_BORDER}; border-radius:8px; }}")
            card_lay = QVBoxLayout(card)
            card_lay.setContentsMargins(12, 10, 12, 10)
            card_lay.setSpacing(4)
            
            # Header Row (Agent Name + Status)
            hdr_lay = QHBoxLayout()
            agent_lbl = QLabel(name)
            agent_lbl.setStyleSheet(f"color:{_AGENT_COLORS.get(name, _TEXT)}; font-weight:700; font-size:13px;")
            hdr_lay.addWidget(agent_lbl)
            
            status_lbl = QLabel("idle")
            status_lbl.setStyleSheet(f"color:{_MUTED}; font-size:10px; font-weight:600; text-transform:uppercase;")
            hdr_lay.addWidget(status_lbl)
            hdr_lay.addStretch(1)
            card_lay.addLayout(hdr_lay)
            
            # Task body description
            desc_lbl = QLabel("No active task assigned")
            desc_lbl.setWordWrap(True)
            desc_lbl.setStyleSheet(f"color:{_MUTED}; font-size:11px;")
            card_lay.addWidget(desc_lbl, 1)

            # Resolve action button
            action_btn = QPushButton("Resolve")
            action_btn.setStyleSheet(
                f"QPushButton {{ background:#16404F; color:{_TEXT}; border:1px solid {_BORDER}; "
                f"border-radius:4px; padding:2px 8px; font-size:10px; }}")
            action_btn.setVisible(False)
            card_lay.addWidget(action_btn, 0, Qt.AlignmentFlag.AlignRight)

            self._agent_cards[name] = {
                "frame": card,
                "status": status_lbl,
                "desc": desc_lbl,
                "button": action_btn
            }
            
            row_idx = idx // 2
            col_idx = idx % 2
            self._grid.addWidget(card, row_idx, col_idx)

        grid_widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        content_layout.addWidget(grid_widget, 2)

        # Right: Tasks Queue list
        queue_panel = QFrame()
        queue_panel.setStyleSheet(
            f"QFrame {{ background:{_BG_CARD}; border:1px solid {_BORDER}; border-radius:8px; }}")
        queue_lay = QVBoxLayout(queue_panel)
        queue_lay.setContentsMargins(12, 12, 12, 12)
        queue_lay.setSpacing(8)
        
        q_title = QLabel("ACTIVE QUEUE")
        q_title.setStyleSheet(f"color:{_TEXT}; font-weight:700; font-size:11px;")
        queue_lay.addWidget(q_title)
        
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border:none; background:transparent; }")
        
        self._queue_host = QWidget()
        self._queue_list = QVBoxLayout(self._queue_host)
        self._queue_list.setContentsMargins(0, 0, 0, 0)
        self._queue_list.setSpacing(6)
        self._queue_list.addStretch(1)
        scroll.setWidget(self._queue_host)
        queue_lay.addWidget(scroll, 1)

        queue_panel.setMinimumWidth(320)
        queue_panel.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        content_layout.addWidget(queue_panel, 1)

        root.addLayout(content_layout, 1)
        
        # Initial status query
        self.refresh()

    def refresh(self) -> None:
        self._threads.append(_spawn_http(
            self, "GET", f"{_API}/orchestrator/status", self._on_status_loaded
        ))

    def _on_status_loaded(self, res: dict) -> None:
        if not res.get("ok"):
            self._status.setText(f"Status check failed: {res.get('error')}")
            return
        
        tasks = res.get("data", {}).get("tasks", [])
        
        # Clear out current queue display
        while self._queue_list.count():
            item = self._queue_list.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        # Group tasks by agent to find newest task for each card
        agent_newest: dict[str, dict | None] = {a: None for a in self._agent_cards.keys()}
        active_tasks = []

        for t in tasks:
            agent = t.get("agent_name", "").lower()
            if agent in agent_newest and agent_newest[agent] is None:
                agent_newest[agent] = t
            if t.get("status") in ("pending", "assigned"):
                active_tasks.append(t)

        # Update the cards
        for name, card in self._agent_cards.items():
            t = agent_newest.get(name)
            if not t:
                card["status"].setText("idle")
                card["status"].setStyleSheet(f"color:{_MUTED}; font-size:10px; font-weight:600;")
                card["desc"].setText("No active task assigned")
                card["desc"].setStyleSheet(f"color:{_MUTED}; font-size:11px;")
                card["button"].setVisible(False)
            else:
                status = t.get("status", "idle")
                card["status"].setText(status)
                
                # Colors based on status
                if status == "pending":
                    card["status"].setStyleSheet(f"color:{_PENDING}; font-size:10px; font-weight:600;")
                elif status == "assigned":
                    card["status"].setStyleSheet(f"color:{_ACCENT}; font-size:10px; font-weight:600;")
                elif status == "completed":
                    card["status"].setStyleSheet(f"color:{_OK}; font-size:10px; font-weight:600;")
                else:
                    card["status"].setStyleSheet(f"color:{_ERR}; font-size:10px; font-weight:600;")

                card["desc"].setText(t.get("sub_task_desc", ""))
                card["desc"].setStyleSheet(f"color:{_TEXT}; font-size:11px;")

                if status in ("pending", "assigned"):
                    card["button"].setVisible(True)
                    # Disconnect previous clicks before connecting a new one to avoid double clicks / wrong tasks
                    try:
                        card["button"].clicked.disconnect()
                    except Exception:
                        pass
                    card["button"].clicked.connect(lambda _=False, tid=t.get("id"): self._complete_task(tid))
                else:
                    card["button"].setVisible(False)

        # Update the queue list
        if not active_tasks:
            empty_lbl = QLabel("No active tasks in queue.")
            empty_lbl.setStyleSheet(f"color:{_MUTED}; font-size:11px; font-style:italic;")
            self._queue_list.addWidget(empty_lbl)
        else:
            for t in active_tasks:
                row = QFrame()
                row.setStyleSheet(f"background:#102F3C; border:1px solid {_BORDER}; border-radius:6px;")
                row_lay = QHBoxLayout(row)
                row_lay.setContentsMargins(8, 6, 8, 6)
                
                info = QLabel(f"<b>[{t.get('agent_name')}]</b> {t.get('sub_task_desc')}")
                info.setTextFormat(Qt.RichText)
                info.setStyleSheet(f"color:{_TEXT}; font-size:11px;")
                info.setWordWrap(True)
                row_lay.addWidget(info, 1)

                btn = QPushButton("Done")
                btn.setStyleSheet(
                    f"QPushButton {{ background:{_OK}; color:#0E2630; border:none; "
                    f"border-radius:4px; padding:2px 8px; font-weight:600; font-size:10px; }}")
                btn.clicked.connect(lambda _=False, tid=t.get("id"): self._complete_task(tid))
                row_lay.addWidget(btn)
                
                self._queue_list.addWidget(row)

        self._queue_list.addStretch(1)

    def _complete_task(self, task_id: int) -> None:
        self._threads.append(_spawn_http(
            self, "POST", f"{_API}/orchestrator/complete",
            lambda res: self.refresh(),
            json_body={"task_id": task_id, "status": "completed"}
        ))

    def _dispatch(self) -> None:
        prompt = self._input.toPlainText().strip()
        if not prompt:
            self._status.setText("Type a command first")
            return
        
        self._btn_dispatch.setEnabled(False)
        self._status.setText("Decomposing task...")
        self._threads.append(_spawn_http(
            self, "POST", f"{_API}/orchestrator/dispatch",
            self._on_dispatch_done,
            json_body={"prompt": prompt}
        ))

    def _on_dispatch_done(self, res: dict) -> None:
        self._btn_dispatch.setEnabled(True)
        if not res.get("ok"):
            self._status.setText(f"Dispatch failed: {res.get('error')}")
            return
        
        self._input.clear()
        self._status.setText("Dispatched successfully!")
        self.refresh()
