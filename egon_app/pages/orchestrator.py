"""Orchestrator page — multi-agent goal planner & task tracking.

Allows typing a single high-level prompt, decomposing it into specialized tasks
for agents (claude-code, antigravity, hermes, codex), and monitoring progress.
"""
from __future__ import annotations

import webbrowser
from PySide6.QtCore import Qt, QTimer, QThread, Signal, QObject
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QPlainTextEdit,
    QScrollArea, QFrame, QGridLayout, QSizePolicy, QProgressBar, QSplitter
)
from egon_app import theme
from egon_app.pages._chat_widget import ChatWidget

_API = "http://127.0.0.1:8000/api/v1/mind"

# Shared palette matching Egon theme
_BG_CARD = "#16181c"
_BORDER  = "#22252a"
_ACCENT  = "#5ac8fa"
_TEXT    = "#f5f5f7"
_MUTED   = "#76767f"
_GOLD    = "#ff9f0a"
_OK      = "#30d158"
_ERR     = "#ff453a"
_PENDING = "#E2A844"
_PANEL_BG = "#0c0d0f"

_AGENT_COLORS = {
    "claude-code": "#D77A56",
    "codex":       "#5ac8fa",
    "antigravity": "#9D7BC5",
    "hermes":      "#30d158",
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
        self._selected_task_id: int | None = None
        self._last_tasks: list[dict] = []
        self._last_cooldowns: dict = {}
        self._mission_agents: dict = {}
        self._last_mission_data: dict = {}
        self._agent_overview_cards: dict[str, dict] = {}
        self._build()
        
        # Poll status every 5 seconds when visible
        self._timer = QTimer(self)
        self._timer.setInterval(20000)   # 20s (was 5s) — 4x less polling churn on
        # a RAM-tight machine; combined with thread-prune + skip-if-busy. 2026-07-01
        self._timer.timeout.connect(self.refresh)

    def _button_style(self, fg: str = _TEXT, bg: str = "#212328", border: str = _BORDER) -> str:
        return (
            f"QPushButton {{ background:{bg}; color:{fg}; border:none; "
            "border-radius:5px; padding:4px 8px; font-weight:600; font-size:10px; }}"
            f"QPushButton:disabled {{ background:#15171a; color:{_MUTED}; border:none; }}"
        )

    def _chip(self, label: str, value: str, color: str = _MUTED) -> QLabel:
        chip = QLabel(f"{label} {value}")
        chip.setStyleSheet(
            f"QLabel {{ background:transparent; color:{color}; border:none; "
            "padding:0 4px 0 0; font-size:10px; font-weight:700; }}"
        )
        chip.setMinimumHeight(22)
        return chip

    def _build_command_panel(self) -> QFrame:
        panel = QFrame()
        panel.setStyleSheet(
            f"QFrame {{ background:{_BG_CARD}; border:none; border-radius:8px; }}"
        )
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(8)

        title = QLabel("COMMAND")
        title.setStyleSheet(f"color:{_TEXT}; font-weight:800; font-size:11px;")
        lay.addWidget(title)

        self._input = QPlainTextEdit()
        self._input.setPlaceholderText("Decompose and delegate a goal... (e.g., 'Check database stats, refactor synthesis, and run a workspace audit')")
        self._input.setStyleSheet(
            f"QPlainTextEdit {{ background:#0c0d0f; color:{_TEXT}; "
            f"border:none; border-radius:6px; padding:8px; "
            f"font-size:13px; }}"
        )
        self._input.setFixedHeight(82)
        lay.addWidget(self._input)

        row = QHBoxLayout()
        self._btn_dispatch = QPushButton("Decompose  Dispatch")
        self._btn_dispatch.setStyleSheet(
            f"QPushButton {{ background:{_GOLD}; color:#16181c; border:none; "
            f"border-radius:6px; padding:8px 22px; font-weight:800; font-size:13px; }}"
        )
        self._btn_dispatch.clicked.connect(self._dispatch)
        self._top_btn_dispatch = self._btn_dispatch
        row.addWidget(self._btn_dispatch)

        self._status = QLabel("Ready")
        self._status.setStyleSheet(f"color:{_MUTED};")
        self._top_status = self._status
        row.addWidget(self._status)

        self._progress = QProgressBar()
        self._progress.setStyleSheet(
            f"QProgressBar {{ background: {_BG_CARD}; border:none; border-radius: 4px; }}"
            f"QProgressBar::chunk {{ background: {_GOLD}; border-radius: 4px; }}"
        )
        self._progress.setFixedHeight(6)
        self._progress.setFixedWidth(120)
        self._progress.setTextVisible(False)
        self._progress.setVisible(False)
        self._top_progress = self._progress
        row.addWidget(self._progress)
        row.addStretch(1)
        lay.addLayout(row)
        return panel

    def _build_hermes_panel(self) -> QFrame:
        hermes_panel = QFrame()
        hermes_panel.setStyleSheet(
            f"QFrame {{ background:{_BG_CARD}; border:none; border-radius:8px; }}"
        )
        hp = QVBoxLayout(hermes_panel)
        hp.setContentsMargins(12, 10, 12, 10)
        hp.setSpacing(6)
        head = QHBoxLayout()
        ht = QLabel("HERMES OVERSIGHT  -  masterlaw-screened")
        ht.setStyleSheet(f"color:{_GOLD}; font-weight:800; font-size:12px;")
        head.addWidget(ht)
        head.addStretch(1)
        self._auto_btn = QPushButton("Autonomous dispatch: ...")
        self._auto_btn.setCursor(Qt.PointingHandCursor)
        self._auto_btn.clicked.connect(self._toggle_autonomy)
        self._top_auto_btn = self._auto_btn
        head.addWidget(self._auto_btn)
        hp.addLayout(head)
        self._hermes_summary = QLabel("...")
        self._hermes_summary.setStyleSheet(f"color:{_TEXT}; font-size:11px;")
        self._top_hermes_summary = self._hermes_summary
        hp.addWidget(self._hermes_summary)
        self._hermes_rows = QVBoxLayout()
        self._hermes_rows.setSpacing(4)
        self._top_hermes_rows = self._hermes_rows
        hp.addLayout(self._hermes_rows)
        self._auto_enabled = False
        return hermes_panel

    def _build_agent_overview_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("agentOverview")
        panel.setStyleSheet(
            f"QFrame#agentOverview {{ background:{_BG_CARD}; border:none; border-radius:8px; }}"
        )
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(12, 10, 12, 12)
        lay.setSpacing(10)

        head = QHBoxLayout()
        title = QLabel("AI COMMAND DECK")
        title.setStyleSheet(f"color:{_TEXT}; font-weight:800; font-size:12px;")
        subtitle = QLabel("Live per-agent work, quota state, latest signal, and controls")
        subtitle.setStyleSheet(f"color:{_MUTED}; font-size:11px;")
        head.addWidget(title)
        head.addWidget(subtitle)
        head.addStretch(1)
        self._agent_deck_summary = QLabel("Loading agents...")
        self._agent_deck_summary.setStyleSheet(
            f"QLabel {{ color:{_TEXT}; background:transparent; border:none; "
            "padding:0; font-size:10px; font-weight:800; }}"
        )
        head.addWidget(self._agent_deck_summary)
        lay.addLayout(head)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        agents = ("claude-code", "codex", "antigravity", "hermes")
        for idx, name in enumerate(agents):
            color = _AGENT_COLORS.get(name, _ACCENT)
            card = QFrame()
            card.setObjectName(f"agentOverviewCard_{name}")
            card.setMinimumHeight(178)
            card.setStyleSheet(
                f"QFrame {{ background:{_PANEL_BG}; border:none; border-radius:8px; }}"
            )
            cl = QVBoxLayout(card)
            cl.setContentsMargins(10, 9, 10, 9)
            cl.setSpacing(7)

            hdr = QHBoxLayout()
            dot = QLabel("o")
            dot.setStyleSheet(f"color:{color}; font-size:13px;")
            hdr.addWidget(dot)
            name_lbl = QLabel(name)
            name_lbl.setStyleSheet(f"color:{_TEXT}; font-weight:800; font-size:13px;")
            hdr.addWidget(name_lbl)
            hdr.addStretch(1)
            status = QLabel("IDLE")
            status.setStyleSheet(
                f"QLabel {{ color:{_MUTED}; background:transparent; border:none; "
                "padding:0; font-size:10px; font-weight:800; }}"
            )
            hdr.addWidget(status)
            cl.addLayout(hdr)

            action = QLabel("No active task")
            action.setWordWrap(True)
            action.setMinimumHeight(34)
            action.setStyleSheet(f"color:{_TEXT}; background:transparent; border:none; font-size:12px; font-weight:600;")
            cl.addWidget(action)

            progress = QProgressBar()
            progress.setRange(0, 100)
            progress.setValue(0)
            progress.setTextVisible(False)
            progress.setFixedHeight(6)
            progress.setStyleSheet(
                f"QProgressBar {{ background:#15171a; border:none; border-radius:3px; }}"
                f"QProgressBar::chunk {{ background:{color}; border-radius:3px; }}"
            )
            cl.addWidget(progress)

            chips = QHBoxLayout()
            chips.setSpacing(5)
            active_chip = self._chip("Active", "0", _ACCENT)
            done_chip = self._chip("Done", "0", _OK)
            fail_chip = self._chip("Fail", "0", _ERR)
            cancel_chip = self._chip("Cancel", "0", _MUTED)
            success_chip = self._chip("Success", "100%", _TEXT)
            for chip in (active_chip, done_chip, fail_chip, cancel_chip, success_chip):
                chips.addWidget(chip)
            chips.addStretch(1)
            cl.addLayout(chips)

            latest = QLabel("Latest: no event yet")
            latest.setWordWrap(True)
            latest.setMinimumHeight(32)
            latest.setStyleSheet(f"color:{_MUTED}; background:transparent; border:none; font-size:10px;")
            cl.addWidget(latest)

            controls = QHBoxLayout()
            controls.setSpacing(6)
            btn_events = QPushButton("Events")
            btn_pause = QPushButton("Pause")
            btn_edit = QPushButton("Edit")
            btn_clarify = QPushButton("Clarify")
            btn_stop = QPushButton("Stop")
            btn_cooldown = QPushButton("Cooldown")
            for btn in (btn_events, btn_pause, btn_edit, btn_clarify):
                btn.setStyleSheet(self._button_style())
                controls.addWidget(btn)
            btn_stop.setStyleSheet(self._button_style("#ffffff", _ERR, _ERR))
            controls.addWidget(btn_stop)
            btn_cooldown.setStyleSheet(self._button_style(_TEXT))
            controls.addWidget(btn_cooldown)
            controls.addStretch(1)
            cl.addLayout(controls)

            self._agent_overview_cards[name] = {
                "frame": card,
                "dot": dot,
                "status": status,
                "action": action,
                "progress": progress,
                "active": active_chip,
                "done": done_chip,
                "fail": fail_chip,
                "cancel": cancel_chip,
                "success": success_chip,
                "latest": latest,
                "events": btn_events,
                "pause": btn_pause,
                "edit": btn_edit,
                "clarify": btn_clarify,
                "stop": btn_stop,
                "cooldown": btn_cooldown,
            }
            grid.addWidget(card, idx // 2, idx % 2)
        lay.addLayout(grid)
        return panel

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        page_scroll = QScrollArea()
        page_scroll.setWidgetResizable(True)
        page_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        page_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        page_scroll.setStyleSheet(
            "QScrollArea { border:none; background:transparent; }"
            "QScrollBar:vertical { background:#0c0d0f; width:10px; margin:0; }"
            "QScrollBar::handle:vertical { background:#333842; border-radius:5px; min-height:32px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }"
        )
        content = QWidget()
        content.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        page_scroll.setWidget(content)
        outer.addWidget(page_scroll)

        root = QVBoxLayout(content)
        root.setContentsMargins(24, 18, 24, 18)
        root.setSpacing(12)

        # Header Row (Title + System Status Banner)
        header_lay = QHBoxLayout()
        title = QLabel("AI Orchestrator")
        title.setStyleSheet(f"color:{_TEXT}; font-size:22px; font-weight:700;")
        header_lay.addWidget(title)
        
        header_lay.addStretch(1)
        
        # System Status Banner
        self._banner = QFrame()
        self._banner.setStyleSheet(
            f"QFrame {{ background: {_BG_CARD}; border:none; border-radius: 12px; }}"
        )
        banner_lay = QHBoxLayout(self._banner)
        banner_lay.setContentsMargins(10, 4, 10, 4)
        banner_lay.setSpacing(6)
        
        self._banner_dot = QLabel("o")
        self._banner_dot.setStyleSheet(f"color: {_OK}; font-size: 14px;")
        banner_lay.addWidget(self._banner_dot)
        
        self._banner_text = QLabel("SYSTEM ACTIVE | Active Tasks: 0 | Core: ONLINE")
        self._banner_text.setStyleSheet(f"color: {_TEXT}; font-size: 11px; font-weight: 600;")
        banner_lay.addWidget(self._banner_text)
        
        header_lay.addWidget(self._banner)
        root.addLayout(header_lay)
        
        sub = QLabel("Talk to Egon directly below — it answers in real time, grounded in your "
                     "vault. The dashboard underneath is oversight: live agent status, Hermes "
                     "proposals (you veto), and the task queue.")
        sub.setStyleSheet(f"color:{_MUTED};")
        sub.setWordWrap(True)
        root.addWidget(sub)

        # ── The chat box Bruno asked for: Mission Control IS a conversation —
        # and (2026-07-02) THE command surface: orders typed here are detected
        # and dispatched to the orchestrator; the reply describes what queued.
        self._chat = ChatWidget(self, title="MISSION CONTROL  ·  talk or command")
        self._chat.setMinimumHeight(420)
        root.addWidget(self._chat)

        root.addWidget(self._build_hermes_panel())
        # COMMAND panel consolidated into the chat above (one input, not two —
        # Bruno 2026-07-02). Panel kept but hidden: its _dispatch plumbing still
        # backs task edit/clarify flows.
        _cmd_panel = self._build_command_panel()
        _cmd_panel.setVisible(False)
        root.addWidget(_cmd_panel)

        # Metrics Dashboard Row
        metrics_lay = QHBoxLayout()
        metrics_lay.setSpacing(12)
        
        self._metrics_cards = {}
        metrics_defs = [
            ("total", "DISPATCHED", _GOLD),
            ("completed", "COMPLETED", _OK),
            ("inflight", "IN-FLIGHT", _ACCENT),
            ("success_rate", "SUCCESS RATE", _TEXT),
        ]
        for key, label, val_color in metrics_defs:
            card = QFrame()
            card.setStyleSheet(
                f"QFrame {{ background: {_BG_CARD}; border:none; border-radius: 8px; }}"
            )
            card_lay = QVBoxLayout(card)
            card_lay.setContentsMargins(12, 10, 12, 10)
            card_lay.setSpacing(4)
            
            lbl = QLabel(label)
            lbl.setStyleSheet(f"color: {_MUTED}; font-size: 10px; font-weight: 700; letter-spacing: 0.5px;")
            card_lay.addWidget(lbl)
            
            val = QLabel("0")
            val.setStyleSheet(f"color: {val_color}; font-size: 20px; font-weight: 700;")
            card_lay.addWidget(val)
            
            self._metrics_cards[key] = val
            metrics_lay.addWidget(card)
            
        root.addLayout(metrics_lay)

        mission_panel = QFrame()
        mission_panel.setStyleSheet(
            f"QFrame {{ background:{_BG_CARD}; border:none; border-radius:8px; }}"
        )
        mission_lay = QVBoxLayout(mission_panel)
        mission_lay.setContentsMargins(12, 10, 12, 10)
        mission_lay.setSpacing(6)
        mission_title = QLabel("MISSION CONTROL")
        mission_title.setStyleSheet(f"color:{_TEXT}; font-weight:700; font-size:11px;")
        mission_lay.addWidget(mission_title)
        self._mission = QPlainTextEdit()
        self._mission.setReadOnly(True)
        self._mission.setFixedHeight(150)   # room for the recent-outcomes feed
        self._mission.setStyleSheet(
            f"QPlainTextEdit {{ background:#0c0d0f; color:{_TEXT}; "
            f"border:none; border-radius:6px; padding:7px; font-size:11px; }}"
        )
        self._mission.setPlaceholderText("Loading mission status...")
        mission_lay.addWidget(self._mission)
        root.addWidget(mission_panel)

        # ── SESSIONS — every AI's recent sessions, canonical project attached
        # (Bruno 2026-07-04: sessions on the console for better control) ──────
        sess_panel = QFrame()
        sess_panel.setStyleSheet(
            f"QFrame {{ background:{_BG_CARD}; border:none; border-radius:8px; }}")
        sess_lay = QVBoxLayout(sess_panel)
        sess_lay.setContentsMargins(12, 10, 12, 10)
        sess_lay.setSpacing(6)
        sess_head = QHBoxLayout()
        st = QLabel("SESSIONS  ·  all AIs, newest first")
        st.setStyleSheet(f"color:{_TEXT}; font-weight:800; font-size:11px;")
        sess_head.addWidget(st)
        sh = QLabel("click a session for its full summary")
        sh.setStyleSheet(f"color:{_MUTED}; font-size:10px;")
        sess_head.addWidget(sh)
        sess_head.addStretch(1)
        sess_lay.addLayout(sess_head)
        sess_scroll = QScrollArea()
        sess_scroll.setWidgetResizable(True)
        sess_scroll.setStyleSheet("QScrollArea { border:none; background:transparent; }")
        sess_scroll.setFixedHeight(150)
        self._sessions_host = QWidget()
        self._sessions_list = QVBoxLayout(self._sessions_host)
        self._sessions_list.setContentsMargins(0, 0, 0, 0)
        self._sessions_list.setSpacing(4)
        self._sessions_list.addStretch(1)
        sess_scroll.setWidget(self._sessions_host)
        sess_lay.addWidget(sess_scroll)
        root.addWidget(sess_panel)

        root.addWidget(self._build_agent_overview_panel())

        # Legacy command box was moved to the top command panel.
        legacy_command_box = QWidget()
        legacy_command_box.setVisible(False)
        row = QHBoxLayout(legacy_command_box)
        self._btn_dispatch = QPushButton("🪄 Decompose & Dispatch")
        self._btn_dispatch.setStyleSheet(
            f"QPushButton {{ background:{_GOLD}; color:#16181c; border:none; "
            f"border-radius:6px; padding:8px 22px; font-weight:700; font-size:13px; }}")
        self._btn_dispatch.clicked.connect(self._dispatch)
        row.addWidget(self._btn_dispatch)
        
        self._status = QLabel("Ready")
        self._status.setStyleSheet(f"color:{_MUTED};")
        row.addWidget(self._status)
        
        self._progress = QProgressBar()
        self._progress.setStyleSheet(
            f"QProgressBar {{ background: {_BG_CARD}; border:none; border-radius: 4px; }}"
            f"QProgressBar::chunk {{ background: {_GOLD}; border-radius: 4px; }}"
        )
        self._progress.setFixedHeight(6)
        self._progress.setFixedWidth(120)
        self._progress.setTextVisible(False)
        self._progress.setVisible(False)
        row.addWidget(self._progress)
        
        row.addStretch(1)
        root.addLayout(row)
        for _legacy_widget in (self._btn_dispatch, self._status, self._progress):
            _legacy_widget.setVisible(False)
        self._btn_dispatch = self._top_btn_dispatch
        self._status = self._top_status
        self._progress = self._top_progress

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
            # Floor the height so idle cards stay readable instead of collapsing
            # into thin bars when nothing is in flight (2026-06-25).
            card.setMinimumHeight(104)
            card.setStyleSheet(
                f"QFrame#agentCard {{ background:{_BG_CARD}; border:none; border-radius:8px; }}")
            card_lay = QVBoxLayout(card)
            card_lay.setContentsMargins(12, 10, 12, 10)
            card_lay.setSpacing(4)
            
            # Header Row (Agent Name + Status)
            hdr_lay = QHBoxLayout()
            agent_lbl = QLabel(name)
            agent_lbl.setStyleSheet(f"color:{_TEXT}; font-weight:700; font-size:13px;")
            hdr_lay.addWidget(agent_lbl)
            
            status_dot = QLabel("o")
            status_dot.setStyleSheet(f"color: {_MUTED}; font-size: 12px;")
            hdr_lay.addWidget(status_dot)
            
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

            # Double action buttons row
            btn_lay = QHBoxLayout()
            
            done_btn = QPushButton("Done")
            done_btn.setStyleSheet(
                f"QPushButton {{ background:{_OK}; color:#16181c; border:none; "
                f"border-radius:4px; padding:2px 8px; font-weight:600; font-size:10px; }}"
            )
            done_btn.setVisible(False)
            btn_lay.addWidget(done_btn)
            
            stop_btn = QPushButton("Stop")
            stop_btn.setStyleSheet(
                f"QPushButton {{ background:{_ERR}; color:#ffffff; border:none; "
                f"border-radius:4px; padding:2px 8px; font-weight:600; font-size:10px; }}"
            )
            stop_btn.setVisible(False)
            btn_lay.addWidget(stop_btn)
            
            cooldown_btn = QPushButton("Cooldown")
            cooldown_btn.setStyleSheet(
                f"QPushButton {{ background:#212328; color:{_TEXT}; border:none; "
                f"border-radius:4px; padding:2px 8px; font-weight:600; font-size:10px; }}"
            )
            btn_lay.addWidget(cooldown_btn)
            
            btn_lay.addStretch(1)
            card_lay.addLayout(btn_lay)

            self._agent_cards[name] = {
                "frame": card,
                "status_dot": status_dot,
                "status": status_lbl,
                "desc": desc_lbl,
                "btn_done": done_btn,
                "btn_stop": stop_btn,
                "btn_cooldown": cooldown_btn
            }
            
            row_idx = idx // 2
            col_idx = idx % 2
            self._grid.addWidget(card, row_idx, col_idx)

        grid_widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(grid_widget)
        left_layout.addStretch(1)

        # Right: Tasks Queue list
        queue_panel = QFrame()
        queue_panel.setStyleSheet(
            f"QFrame {{ background:{_BG_CARD}; border:none; border-radius:8px; }}"
        )
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

        timeline_title = QLabel("TASK TIMELINE")
        timeline_title.setStyleSheet(f"color:{_TEXT}; font-weight:700; font-size:11px;")
        queue_lay.addWidget(timeline_title)

        self._timeline = QPlainTextEdit()
        self._timeline.setReadOnly(True)
        self._timeline.setPlaceholderText("Select a task to inspect its live events.")
        self._timeline.setStyleSheet(
            f"QPlainTextEdit {{ background:#0c0d0f; color:{_TEXT}; "
            f"border:none; border-radius:6px; padding:7px; "
            f"font-size:11px; }}"
        )
        self._timeline.setMinimumHeight(150)
        queue_lay.addWidget(self._timeline)

        queue_panel.setMinimumWidth(320)
        queue_panel.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)

        # Draggable horizontal splitter between Agent Cards and Queue list
        content_splitter = QSplitter(Qt.Orientation.Horizontal)
        content_splitter.setStyleSheet(
            "QSplitter::handle { background: #22252a; width: 1px; }"
            "QSplitter::handle:hover { background: #ff9f0a; }"
        )
        content_splitter.addWidget(queue_panel)
        content_splitter.setCollapsible(0, False)
        content_splitter.setSizes([900])
        content_splitter.setMinimumHeight(360)
        
        # History Panel (Recent Activity History + Retry button)
        history_panel = QFrame()
        history_panel.setStyleSheet(
            f"QFrame {{ background:{_BG_CARD}; border:none; border-radius:8px; }}"
        )
        history_lay = QVBoxLayout(history_panel)
        history_lay.setContentsMargins(12, 12, 12, 12)
        history_lay.setSpacing(8)
        history_panel.setMinimumHeight(170)
        
        h_title = QLabel("RECENT ACTIVITY HISTORY")
        h_title.setStyleSheet(f"color:{_TEXT}; font-weight:700; font-size:11px;")
        history_lay.addWidget(h_title)
        
        h_scroll = QScrollArea()
        h_scroll.setWidgetResizable(True)
        h_scroll.setStyleSheet("QScrollArea { border:none; background:transparent; }")
        h_scroll.setMaximumHeight(150)
        
        self._history_host = QWidget()
        self._history_list = QVBoxLayout(self._history_host)
        self._history_list.setContentsMargins(0, 0, 0, 0)
        self._history_list.setSpacing(6)
        self._history_list.addStretch(1)
        h_scroll.setWidget(self._history_host)
        history_lay.addWidget(h_scroll, 1)
        
        # Vertical splitter for middle content and bottom history
        main_splitter = QSplitter(Qt.Orientation.Vertical)
        main_splitter.setStyleSheet(
            "QSplitter::handle { background: #22252a; height: 1px; }"
            "QSplitter::handle:hover { background: #ff9f0a; }"
        )
        main_splitter.addWidget(content_splitter)
        main_splitter.addWidget(history_panel)
        main_splitter.setCollapsible(0, False)
        main_splitter.setCollapsible(1, False)
        main_splitter.setSizes([500, 150])
        main_splitter.setMinimumHeight(560)
        
        # --- HERMES OVERSIGHT — always-on cross-AI monitor (proposes, you veto) ---
        hermes_panel = QFrame()
        hermes_panel.setStyleSheet(
            f"QFrame {{ background:{_BG_CARD}; border:none; border-radius:8px; }}")
        hp = QVBoxLayout(hermes_panel)
        hp.setContentsMargins(12, 10, 12, 10)
        hp.setSpacing(6)
        head = QHBoxLayout()
        ht = QLabel("HERMES OVERSIGHT  ·  masterlaw-screened")
        ht.setStyleSheet(f"color:{_GOLD}; font-weight:600; font-size:12px;")
        head.addWidget(ht)
        head.addStretch(1)
        # Master switch for autonomous dispatch (default OFF). Even ON, every
        # dispatch is masterlaw-screened and vetoable.
        self._auto_btn = QPushButton("Autonomous dispatch: …")
        self._auto_btn.setCursor(Qt.PointingHandCursor)
        self._auto_btn.clicked.connect(self._toggle_autonomy)
        head.addWidget(self._auto_btn)
        hp.addLayout(head)
        self._hermes_summary = QLabel("…")
        self._hermes_summary.setStyleSheet(f"color:{_TEXT}; font-size:11px;")
        hp.addWidget(self._hermes_summary)
        # dynamic per-proposal rows go here
        self._hermes_rows = QVBoxLayout()
        self._hermes_rows.setSpacing(4)
        hp.addLayout(self._hermes_rows)
        self._auto_enabled = False
        root.addWidget(hermes_panel)
        hermes_panel.setVisible(False)
        self._auto_btn = self._top_auto_btn
        self._hermes_summary = self._top_hermes_summary
        self._hermes_rows = self._top_hermes_rows

        root.addWidget(main_splitter, 1)

        # Initial status query
        self.refresh()

    def refresh(self) -> None:
        # Prune finished threads (the list was never cleaned → grew unbounded).
        # And if the previous batch is still in flight — mind_service is slow /
        # RAM-thrashing — SKIP this cycle instead of piling on 4 more HTTP
        # threads every 5s. Unbounded pile-up is what froze the GUI ("Not
        # Responding") over minutes, even while just typing. Bruno 2026-07-01.
        try:
            self._threads = [t for t in self._threads if t is not None and t.isRunning()]
        except Exception:
            self._threads = []
        if len(self._threads) >= 5:
            return
        self._threads.append(_spawn_http(
            self, "GET", f"{_API}/orchestrator/status", self._on_status_loaded
        ))
        self._threads.append(_spawn_http(
            self, "GET", f"{_API}/sessions?limit=12", self._on_sessions_loaded))
        self._threads.append(_spawn_http(
            self, "GET", f"{_API}/orchestrator/mission-control?limit_events=30",
            self._on_mission_loaded,
            timeout=8.0,
        ))
        self._threads.append(_spawn_http(
            self, "GET", f"{_API}/orchestrator/hermes", self._on_hermes_loaded))
        self._threads.append(_spawn_http(
            self, "GET", f"{_API}/orchestrator/autonomy/status", self._on_autonomy_loaded))

    def _on_sessions_loaded(self, res: dict) -> None:
        while self._sessions_list.count():
            it = self._sessions_list.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        data = (res.get("data") or {}) if res.get("ok") else {}
        sessions = data.get("sessions") or []
        if not sessions:
            lbl = QLabel("No sessions recorded yet.")
            lbl.setStyleSheet(f"color:{_MUTED}; font-size:11px; font-style:italic;")
            self._sessions_list.addWidget(lbl)
            self._sessions_list.addStretch(1)
            return
        import datetime as _dt
        for s in sessions:
            row = QFrame()
            row.setCursor(Qt.PointingHandCursor)
            row.setStyleSheet("QFrame { background:#0c0d0f; border:none; border-radius:6px; }"
                              "QFrame:hover { background:#14161b; }")
            rl = QHBoxLayout(row)
            rl.setContentsMargins(8, 4, 8, 4)
            rl.setSpacing(8)
            agent = s.get("agent") or "?"
            color = _AGENT_COLORS.get(agent, _AGENT_COLORS.get(f"{agent}-code", _ACCENT))
            when = ""
            if s.get("started_at"):
                when = _dt.datetime.fromtimestamp(s["started_at"]).strftime("%m-%d %H:%M")
            tag = QLabel(f"{agent}")
            tag.setStyleSheet(f"color:{color}; font-size:10px; font-weight:800;")
            tag.setFixedWidth(80)
            rl.addWidget(tag)
            proj = QLabel(s.get("project") or "")
            proj.setStyleSheet(f"color:{_GOLD}; font-size:10px;")
            proj.setFixedWidth(90)
            rl.addWidget(proj)
            goal = QLabel(self._one_line(s.get("goal") or s.get("external_id") or "", 120))
            goal.setStyleSheet(f"color:{_TEXT}; font-size:11px;")
            rl.addWidget(goal, 1)
            ts = QLabel(when)
            ts.setStyleSheet(f"color:{_MUTED}; font-size:10px;")
            rl.addWidget(ts)
            # click → full summary in the timeline box (control: inspect any
            # session's goal/actions without leaving the console)
            summary = s.get("summary") or "(no summary)"
            hdr = f"Session {s.get('external_id')} — {agent} / {s.get('project')}\n\n"
            row.mousePressEvent = (lambda ev, txt=hdr + summary:
                                   self._timeline.setPlainText(txt))
            self._sessions_list.addWidget(row)
        self._sessions_list.addStretch(1)

    def _on_autonomy_loaded(self, res: dict) -> None:
        d = (res.get("data") or {}) if res.get("ok") else {}
        autonomy = d.get("autonomy") or d
        on = bool(autonomy.get("enabled"))
        mode = str(autonomy.get("mode") or "supervise_only")
        self._auto_enabled = on
        label_mode = "supervise" if mode == "supervise_only" else mode
        self._auto_btn.setText(f"Autonomy: {'ON' if on else 'OFF'} ({label_mode})")
        self._auto_btn.setStyleSheet(
            f"QPushButton {{ background:{'#3a5f3a' if on else '#212328'}; "
            f"color:{'#b6f5b6' if on else _TEXT}; border:none; "
            f"border-radius:6px; padding:5px 10px; font-size:11px; font-weight:600; }}")

    def _toggle_autonomy(self) -> None:
        self._threads.append(_spawn_http(
            self, "POST", f"{_API}/orchestrator/autonomy/config",
            self._on_autonomy_loaded, json_body={"enabled": not self._auto_enabled}))

    def _on_hermes_loaded(self, res: dict) -> None:
        # clear old rows
        while self._hermes_rows.count():
            it = self._hermes_rows.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()
        if not res.get("ok"):
            self._hermes_summary.setText("Hermes: (offline — start egon_core)")
            return
        d = res.get("data") or {}
        self._hermes_summary.setText("Hermes sees: " + (d.get("summary") or "—"))
        props = d.get("proposals") or []
        if not props:
            self._hermes_summary.setText(
                (d.get("summary") or "—") + "   ·   nothing needs your call right now")
            return
        for p in props[:12]:
            self._hermes_rows.addWidget(self._make_proposal_row(p))

    def _make_proposal_row(self, p: dict) -> QFrame:
        tier = p.get("masterlaw_tier", "ok")
        tid = p.get("task_id")
        row = QFrame()
        row.setStyleSheet(f"QFrame {{ background:#0e0f12; border:none; border-radius:6px; }}")
        lay = QHBoxLayout(row)
        lay.setContentsMargins(8, 5, 8, 5)
        lay.setSpacing(6)
        mark = {"block": "BLOCK", "confirm": "ASK", "ok": "OK"}.get(tier, "-")
        txt = f"{mark} #{tid} → {p.get('agent')}: {p.get('why','')[:72]}"
        if tier != "ok":
            txt += f"   ({p.get('masterlaw_reason','')[:50]})"
        lbl = QLabel(txt)
        lbl.setStyleSheet(f"color:{_TEXT}; font-size:11px;")
        lbl.setWordWrap(True)
        lay.addWidget(lbl, 1)
        # Approve dispatches the task (requeue → pickup). BLOCKED proposals get no
        # approve button — the masterlaw forbids them; only veto.
        if tier != "block":
            appr = QPushButton("Approve")
            appr.setCursor(Qt.PointingHandCursor)
            appr.setStyleSheet(
                f"QPushButton {{ background:{_GOLD}; color:#16181c; border:none; "
                f"border-radius:5px; padding:4px 10px; font-size:11px; font-weight:600; }}")
            appr.clicked.connect(lambda _=False, t=tid: self._proposal_act(t, "requeue"))
            lay.addWidget(appr)
        veto = QPushButton("Veto")
        veto.setCursor(Qt.PointingHandCursor)
        veto.setStyleSheet(
            f"QPushButton {{ background:#3a2326; color:#f5b6b6; border:none; "
            f"border-radius:5px; padding:4px 10px; font-size:11px; }}")
        veto.clicked.connect(lambda _=False, t=tid: self._proposal_act(t, "cancel"))
        lay.addWidget(veto)
        return row

    def _proposal_act(self, task_id: int, action: str) -> None:
        self._threads.append(_spawn_http(
            self, "POST", f"{_API}/orchestrator/tasks/{task_id}/control",
            lambda res: self.refresh(), json_body={"action": action}))

    @staticmethod
    def _seen_text(seconds) -> str:
        if seconds is None:
            return "never"
        try:
            seconds = int(seconds)
        except Exception:
            return "unknown"
        if seconds < 90:
            return f"{seconds}s ago"
        minutes = seconds // 60
        if minutes < 90:
            return f"{minutes}m ago"
        hours = minutes // 60
        if hours < 48:
            return f"{hours}h ago"
        return f"{hours // 24}d ago"

    @staticmethod
    def _one_line(text: str, limit: int = 190) -> str:
        text = " ".join(str(text or "").split())
        return text if len(text) <= limit else text[: limit - 1] + "..."

    def _agent_counts(self, name: str) -> tuple[list[dict], dict, int, int]:
        active_statuses = {"pending", "assigned", "paused", "needs_clarification"}
        agent_tasks = [
            t for t in (self._last_tasks or [])
            if str(t.get("agent_name", "")).lower() == name
        ]
        counts = {k: 0 for k in (
            "pending", "assigned", "paused", "needs_clarification",
            "completed", "failed", "cancelled"
        )}
        for task in agent_tasks:
            status = str(task.get("status") or "")
            if status in counts:
                counts[status] += 1
        active_count = sum(counts[s] for s in active_statuses)
        finished_success_base = counts["completed"] + counts["failed"]
        success_rate = int((counts["completed"] / finished_success_base) * 100) if finished_success_base else 100
        return agent_tasks, counts, active_count, success_rate

    def _agent_card_notice(self, name: str, action: str) -> None:
        self._status.setText(f"{name}: no active task to {action}. Use the command box to assign new work.")
        self._timeline.setPlainText(
            f"{name} has no active task to {action}.\n"
            "Events remains available for the latest task when history exists; Cooldown can hold this AI out of routing."
        )

    def _render_mission_console(self) -> None:
        data = self._last_mission_data or {}
        summary = data.get("summary") or {}
        lines = [
            f"Active work {summary.get('active_work', 0)} | paused {summary.get('paused', 0)} | clarification {summary.get('needs_clarification', 0)} | leases {summary.get('open_leases', 0)}",
        ]
        stale = summary.get("stale_agents") or []
        cooldown = summary.get("cooldown_agents") or []
        if cooldown:
            lines.append("Cooldown: " + ", ".join(cooldown))
        if stale:
            active_stale = []
            for name in stale:
                info = (data.get("agents") or {}).get(name) or {}
                if info.get("current_task") or int(info.get("active_task_count") or 0) > 0:
                    active_stale.append(name)
            if active_stale:
                lines.append("Needs attention: stale active agents " + ", ".join(active_stale))
        wake = data.get("wake") or {}
        active_wake = wake.get("active_runners") or []
        queue_only = []
        for agent, item in (wake.get("agents") or {}).items():
            if (item or {}).get("status") == "queued_no_runner":
                queue_only.append(agent)
        if active_wake or queue_only:
            wake_bits = []
            if active_wake:
                wake_bits.append("runners " + ", ".join(
                    f"{w.get('agent')}#{w.get('task_id')}" for w in active_wake
                ))
            if queue_only:
                wake_bits.append("queued/no-runner " + ", ".join(sorted(queue_only)))
            lines.append("Wake: " + " | ".join(wake_bits))
        # 🎯 Goals: the measured outcome the orchestrator is pursuing
        try:
            import json as _json
            from pathlib import Path as _P
            gst = _json.loads((_P(__file__).resolve().parents[2] / "state" /
                               "goals_status.json").read_text(encoding="utf-8"))
            for g in gst.get("goals", []):
                m = g.get("measure") or {}
                t = g.get("target") or {}
                if m:
                    lines.append(
                        f"🎯 {g['id']}: {m.get('pct_pdf')}% PDFs / "
                        f"{m.get('pct_complete')}% complete (target "
                        f"{t.get('pct_pdf', '?')}/{t.get('pct_complete', '?')}) — "
                        f"{g.get('note', '')}")
        except Exception:
            pass
        # Visibility (Bruno 2026-07-04: "where's my visibility?"): what the
        # agents actually DID, right here above the fold — newest finished
        # tasks with their outcome, not just counters.
        done = [t for t in (self._last_tasks or [])
                if t.get("status") in ("completed", "failed")]
        for t in done[:4]:
            mark = "✓" if t.get("status") == "completed" else "✗"
            desc = " ".join(str(t.get("sub_task_desc") or "").split())[:90]
            ev = (t.get("latest_event") or {}).get("content") or ""
            ev = " ".join(str(ev).split())[:80]
            line = f"{mark} #{t.get('id')} {t.get('agent_name')}: {desc}"
            if ev:
                line += f"  →  {ev}"
            lines.append(line)
        if len(lines) == 1:
            lines.append("Use the AI Command Deck below for per-agent action, stats, events, and controls.")
        self._mission.setPlainText("\n".join(lines))

    def _render_agent_overview(self) -> None:
        if not self._agent_overview_cards:
            return
        active_statuses = {"pending", "assigned", "paused", "needs_clarification"}
        terminal_statuses = {"completed", "cancelled", "failed", "wake_exit", "status_completed", "status_cancelled"}
        mission_agents = self._mission_agents or {}
        cooldowns = self._last_cooldowns or {}
        active_agents = 0
        cooldown_agents = 0

        for name, card in self._agent_overview_cards.items():
            agent_tasks, counts, active_count, success_rate = self._agent_counts(name)
            info = mission_agents.get(name) or {}
            state = info.get("state") or {}
            current_task = info.get("current_task") or {}
            latest = info.get("latest_event") or {}
            cooldown = cooldowns.get(name) or info.get("cooldown")
            active_task = next((t for t in agent_tasks if t.get("status") in active_statuses), None)
            latest_task = agent_tasks[0] if agent_tasks else None
            control_task = active_task or current_task or latest_task
            task_id = (control_task or {}).get("id") or (control_task or {}).get("task_id")

            status = state.get("status") or ("cooldown" if cooldown else "idle")
            if not active_task and not current_task and status in terminal_statuses and not cooldown:
                status = "idle"
            if cooldown:
                status = "cooldown"
                cooldown_agents += 1
            if active_count:
                active_agents += 1

            if status == "cooldown":
                color = _GOLD
                status_label = "COOLDOWN"
            elif active_count:
                color = _ACCENT
                status_label = "ACTIVE"
            elif status in {"failed", "error"}:
                color = _ERR
                status_label = "ERROR"
            else:
                color = _MUTED
                status_label = "IDLE"

            action = (current_task or {}).get("sub_task_desc")
            if not action and active_task:
                action = active_task.get("sub_task_desc")
            if not action and cooldown:
                reason = cooldown.get("reason", "quota or manual cooldown") if isinstance(cooldown, dict) else "cooldown"
                action = f"Paused by cooldown: {reason}"
            if not action:
                action = "No active task"

            latest_content = latest.get("content")
            latest_kind = latest.get("event_type") or "event"
            if not latest_content and agent_tasks:
                task_latest = agent_tasks[0].get("latest_event") or {}
                latest_kind = task_latest.get("event_type") or latest_kind
                latest_content = task_latest.get("content")

            card["dot"].setStyleSheet(f"color:{color}; font-size:13px;")
            card["status"].setText(status_label)
            card["status"].setStyleSheet(
                f"QLabel {{ color:{color}; background:transparent; border:none; "
                "padding:0; font-size:10px; font-weight:800; }}"
            )
            card["action"].setText(self._one_line(action, 175))
            card["progress"].setValue(success_rate)
            card["active"].setText(f"Active {active_count}")
            card["done"].setText(f"Done {counts['completed']}")
            card["fail"].setText(f"Fail {counts['failed']}")
            card["cancel"].setText(f"Cancel {counts['cancelled']}")
            card["success"].setText(f"Success {success_rate}%")
            card["latest"].setText(
                f"Latest {latest_kind}: {self._one_line(latest_content, 190)}"
                if latest_content else f"Last seen: {self._seen_text(info.get('last_seen_seconds_ago'))}"
            )

            for key in ("events", "pause", "edit", "clarify", "stop"):
                try:
                    card[key].clicked.disconnect()
                except Exception:
                    pass
                card[key].setEnabled(True)
            try:
                card["cooldown"].clicked.disconnect()
            except Exception:
                pass

            if active_task and task_id:
                card["events"].clicked.connect(lambda _=False, tid=task_id: self._load_task_events(tid))
                if active_task and active_task.get("status") == "paused":
                    card["pause"].setText("Resume")
                    card["pause"].clicked.connect(lambda _=False, tid=task_id: self._control_task(tid, "resume"))
                else:
                    card["pause"].setText("Pause")
                    card["pause"].clicked.connect(lambda _=False, tid=task_id: self._control_task(tid, "pause", "manual pause"))
                card["edit"].clicked.connect(lambda _=False, tid=task_id: self._edit_task_from_input(tid))
                card["clarify"].clicked.connect(lambda _=False, tid=task_id: self._control_task(tid, "clarify", self._input.toPlainText().strip()))
                card["stop"].clicked.connect(lambda _=False, tid=task_id: self._control_task(tid, "stop", "manual stop"))
            else:
                card["pause"].setText("Pause")
                if task_id:
                    card["events"].clicked.connect(lambda _=False, tid=task_id: self._load_task_events(tid))
                else:
                    card["events"].clicked.connect(lambda _=False, n=name: self._agent_card_notice(n, "show events for"))
                card["pause"].clicked.connect(lambda _=False, n=name: self._toggle_cooldown(n, False))
                card["edit"].clicked.connect(lambda _=False, n=name: self._agent_card_notice(n, "edit"))
                card["clarify"].clicked.connect(lambda _=False, n=name: self._agent_card_notice(n, "clarify"))
                card["stop"].clicked.connect(lambda _=False, n=name: self._toggle_cooldown(n, False))

            if cooldown:
                card["cooldown"].setText("Resume")
                card["cooldown"].setStyleSheet(self._button_style(_OK, "#18251b", _OK))
                card["cooldown"].clicked.connect(lambda _=False, n=name: self._toggle_cooldown(n, True))
            else:
                card["cooldown"].setText("Cooldown")
                card["cooldown"].setStyleSheet(self._button_style(_TEXT))
                card["cooldown"].clicked.connect(lambda _=False, n=name: self._toggle_cooldown(n, False))

            card["frame"].setStyleSheet(
                f"QFrame {{ background:{_PANEL_BG}; border:none; border-radius:8px; }}"
            )

        idle_agents = len(self._agent_overview_cards) - active_agents - cooldown_agents
        self._agent_deck_summary.setText(
            f"{active_agents} active · {cooldown_agents} cooldown · {max(idle_agents, 0)} idle"
        )

    def _on_mission_loaded(self, res: dict) -> None:
        if not res.get("ok"):
            self._mission.setPlainText(f"Mission status failed: {res.get('error')}")
            return
        data = res.get("data") or {}
        self._last_mission_data = data
        self._mission_agents = data.get("agents") or {}
        self._render_mission_console()
        self._render_agent_overview()
        self._render_agent_breakdown()

    def _on_status_loaded(self, res: dict) -> None:
        if not res.get("ok"):
            self._status.setText(f"Status check failed: {res.get('error')}")
            self._banner_dot.setStyleSheet(f"color: {_ERR}; font-size: 14px;")
            self._banner_text.setText("SYSTEM OFFLINE | Core: UNREACHABLE")
            return

        tasks = res.get("data", {}).get("tasks", [])
        cooldowns = res.get("data", {}).get("cooldowns", {})
        self._last_tasks = tasks
        self._last_cooldowns = cooldowns
        self._render_mission_console()
        self._render_agent_overview()

        # ── Calculate metrics ──────────────────────────────────────────
        total_tasks = len(tasks)
        completed_tasks = sum(1 for t in tasks if t.get("status") == "completed")
        failed_tasks = sum(1 for t in tasks if t.get("status") == "failed")
        active_statuses = ("pending", "assigned", "paused", "needs_clarification")
        inflight_tasks = sum(1 for t in tasks if t.get("status") in active_statuses)

        total_finished = completed_tasks + failed_tasks
        success_rate = int((completed_tasks / total_finished) * 100) if total_finished > 0 else 100

        self._metrics_cards["total"].setText(str(total_tasks))
        self._metrics_cards["completed"].setText(str(completed_tasks))
        self._metrics_cards["inflight"].setText(str(inflight_tasks))
        self._metrics_cards["success_rate"].setText(f"{success_rate}%")
        self._render_agent_breakdown()

        # ── System Status Banner ───────────────────────────────────────
        if inflight_tasks > 0:
            self._banner_dot.setStyleSheet(f"color: {_GOLD}; font-size: 14px;")
            self._banner_text.setText(f"SYSTEM RUNNING | Active Tasks: {inflight_tasks} | Core: ONLINE")
        else:
            self._banner_dot.setStyleSheet(f"color: {_OK}; font-size: 14px;")
            self._banner_text.setText("SYSTEM ACTIVE | Active Tasks: 0 | Core: ONLINE")

        # ── Clear current queue display ────────────────────────────────
        while self._queue_list.count():
            item = self._queue_list.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        # ── Group tasks by agent ───────────────────────────────────────
        agent_newest: dict[str, dict | None] = {a: None for a in self._agent_cards.keys()}
        active_tasks: list[dict] = []

        for t in tasks:
            agent = t.get("agent_name", "").lower()
            if agent in agent_newest and agent_newest[agent] is None:
                if t.get("status") in active_statuses:
                    agent_newest[agent] = t
            if t.get("status") in active_statuses:
                active_tasks.append(t)

        # ── Update agent cards (cooldown-aware) ────────────────────────
        import datetime as _dt

        for name, card in self._agent_cards.items():
            t = agent_newest.get(name)
            cooldown = cooldowns.get(name)

            try:
                card["btn_cooldown"].clicked.disconnect()
            except Exception:
                pass

            if cooldown:
                until = cooldown.get("cooldown_until", 0)
                reason = cooldown.get("reason", "quota exceeded")
                until_str = _dt.datetime.fromtimestamp(until).strftime("%H:%M:%S")

                card["status_dot"].setStyleSheet(f"color: {_GOLD}; font-size:12px;")
                card["status"].setText("cooldown")
                card["status"].setStyleSheet(f"color:{_GOLD}; font-size:10px; font-weight:600;")
                card["desc"].setText(f"On cooldown until {until_str}\n({reason})")
                card["desc"].setStyleSheet(f"color:{_GOLD}; font-size:11px;")
                card["btn_done"].setVisible(False)
                card["btn_stop"].setVisible(False)
                card["btn_cooldown"].setText("Resume")
                card["btn_cooldown"].setStyleSheet(
                    f"QPushButton {{ background:#212328; color:{_OK}; border:none; "
                    f"border-radius:4px; padding:2px 8px; font-weight:600; font-size:10px; }}"
                )
                card["btn_cooldown"].clicked.connect(
                    lambda _=False, n=name: self._toggle_cooldown(n, True)
                )
            elif not t:
                card["status_dot"].setStyleSheet(f"color: {_MUTED}; font-size:12px;")
                card["status"].setText("idle")
                card["status"].setStyleSheet(f"color:{_MUTED}; font-size:10px; font-weight:600;")
                card["desc"].setText("No active task assigned")
                card["desc"].setStyleSheet(f"color:{_MUTED}; font-size:11px;")
                card["btn_done"].setVisible(False)
                card["btn_stop"].setVisible(False)
                card["btn_cooldown"].setText("Cooldown")
                card["btn_cooldown"].setStyleSheet(
                    f"QPushButton {{ background:#212328; color:{_TEXT}; border:none; "
                    f"border-radius:4px; padding:2px 8px; font-weight:600; font-size:10px; }}"
                )
                card["btn_cooldown"].clicked.connect(
                    lambda _=False, n=name: self._toggle_cooldown(n, False)
                )
            else:
                status = t.get("status", "idle")
                card["status"].setText(status)
                if status == "pending":
                    card["status_dot"].setStyleSheet(f"color: {_PENDING}; font-size:12px;")
                    card["status"].setStyleSheet(f"color:{_PENDING}; font-size:10px; font-weight:600;")
                elif status == "assigned":
                    card["status_dot"].setStyleSheet(f"color: {_ACCENT}; font-size:12px;")
                    card["status"].setStyleSheet(f"color:{_ACCENT}; font-size:10px; font-weight:600;")
                elif status == "completed":
                    card["status_dot"].setStyleSheet(f"color: {_OK}; font-size:12px;")
                    card["status"].setStyleSheet(f"color:{_OK}; font-size:10px; font-weight:600;")
                elif status == "failed":
                    card["status_dot"].setStyleSheet(f"color: {_ERR}; font-size:12px;")
                    card["status"].setStyleSheet(f"color:{_ERR}; font-size:10px; font-weight:600;")
                else:
                    card["status_dot"].setStyleSheet(f"color: {_MUTED}; font-size:12px;")
                    card["status"].setStyleSheet(f"color:{_MUTED}; font-size:10px; font-weight:600;")

                card["desc"].setText(t.get("sub_task_desc", ""))
                card["desc"].setStyleSheet(f"color:{_TEXT}; font-size:11px;")

                latest = t.get("latest_event") or {}
                if latest.get("content"):
                    card["desc"].setText(f"{t.get('sub_task_desc', '')}\n\nLatest: {latest.get('content', '')[:220]}")
                else:
                    card["desc"].setText(t.get("sub_task_desc", ""))

                if status in active_statuses:
                    card["btn_done"].setVisible(True)
                    card["btn_stop"].setVisible(True)
                    try:
                        card["btn_done"].clicked.disconnect()
                    except Exception:
                        pass
                    try:
                        card["btn_stop"].clicked.disconnect()
                    except Exception:
                        pass
                    card["btn_done"].clicked.connect(
                        lambda _=False, tid=t.get("id"): self._update_task(tid, "completed")
                    )
                    card["btn_stop"].clicked.connect(
                        lambda _=False, tid=t.get("id"): self._control_task(tid, "stop", "manual stop")
                    )
                else:
                    card["btn_done"].setVisible(False)
                    card["btn_stop"].setVisible(False)

                card["btn_cooldown"].setText("Cooldown")
                card["btn_cooldown"].setStyleSheet(
                    f"QPushButton {{ background:#212328; color:{_TEXT}; border:none; "
                    f"border-radius:4px; padding:2px 8px; font-weight:600; font-size:10px; }}"
                )
                card["btn_cooldown"].clicked.connect(
                    lambda _=False, n=name: self._toggle_cooldown(n, False)
                )

        # ── Queue list ─────────────────────────────────────────────────
        if not active_tasks:
            empty_lbl = QLabel("No active tasks in queue.")
            empty_lbl.setStyleSheet(f"color:{_MUTED}; font-size:11px; font-style:italic;")
            self._queue_list.addWidget(empty_lbl)
        else:
            for t in active_tasks:
                row = QFrame()
                row.setStyleSheet(f"background:#0c0d0f; border:none; border-radius:6px;")
                row_lay = QHBoxLayout(row)
                row_lay.setContentsMargins(8, 6, 8, 6)

                info = QLabel(f"<b>[{t.get('agent_name')}]</b> {t.get('sub_task_desc')}")
                info.setTextFormat(Qt.RichText)
                info.setStyleSheet(f"color:{_TEXT}; font-size:11px;")
                info.setWordWrap(True)
                row_lay.addWidget(info, 1)

                latest = t.get("latest_event") or {}
                if latest.get("content"):
                    event_lbl = QLabel(f"{latest.get('event_type', 'event')}: {latest.get('content', '')[:240]}")
                    event_lbl.setStyleSheet(f"color:{_MUTED}; font-size:10px;")
                    event_lbl.setWordWrap(True)
                    row_lay.addWidget(event_lbl, 1)

                btn_done = QPushButton("Done")
                btn_done.setStyleSheet(
                    f"QPushButton {{ background:{_OK}; color:#16181c; border:none; "
                    f"border-radius:4px; padding:2px 8px; font-weight:600; font-size:10px; }}"
                )
                btn_done.clicked.connect(
                    lambda _=False, tid=t.get("id"): self._update_task(tid, "completed")
                )
                row_lay.addWidget(btn_done)

                btn_events = QPushButton("Events")
                btn_events.setStyleSheet(
                    f"QPushButton {{ background:#212328; color:{_GOLD}; border:none; "
                    f"border-radius:4px; padding:2px 8px; font-weight:600; font-size:10px; }}"
                )
                btn_events.clicked.connect(
                    lambda _=False, tid=t.get("id"): self._load_task_events(tid)
                )
                row_lay.addWidget(btn_events)

                btn_pause = QPushButton("Pause" if t.get("status") != "paused" else "Resume")
                btn_pause.setStyleSheet(
                    f"QPushButton {{ background:#212328; color:{_TEXT}; border:none; "
                    f"border-radius:4px; padding:2px 8px; font-weight:600; font-size:10px; }}"
                )
                if t.get("status") == "paused":
                    btn_pause.clicked.connect(
                        lambda _=False, tid=t.get("id"): self._control_task(tid, "resume")
                    )
                else:
                    btn_pause.clicked.connect(
                        lambda _=False, tid=t.get("id"): self._control_task(tid, "pause", "manual pause")
                    )
                row_lay.addWidget(btn_pause)

                btn_clarify = QPushButton("Clarify")
                btn_clarify.setStyleSheet(
                    f"QPushButton {{ background:#212328; color:{_GOLD}; border:none; "
                    f"border-radius:4px; padding:2px 8px; font-weight:600; font-size:10px; }}"
                )
                btn_clarify.clicked.connect(
                    lambda _=False, tid=t.get("id"): self._control_task(tid, "clarify", self._input.toPlainText().strip())
                )
                row_lay.addWidget(btn_clarify)

                btn_edit = QPushButton("Edit")
                btn_edit.setStyleSheet(
                    f"QPushButton {{ background:#212328; color:{_TEXT}; border:none; "
                    f"border-radius:4px; padding:2px 8px; font-weight:600; font-size:10px; }}"
                )
                btn_edit.clicked.connect(
                    lambda _=False, tid=t.get("id"): self._edit_task_from_input(tid)
                )
                row_lay.addWidget(btn_edit)

                btn_stop = QPushButton("Stop")
                btn_stop.setStyleSheet(
                    f"QPushButton {{ background:{_ERR}; color:#ffffff; border:none; "
                    f"border-radius:4px; padding:2px 8px; font-weight:600; font-size:10px; }}"
                )
                btn_stop.clicked.connect(
                    lambda _=False, tid=t.get("id"): self._control_task(tid, "stop", "manual stop")
                )
                row_lay.addWidget(btn_stop)

                self._queue_list.addWidget(row)

        self._queue_list.addStretch(1)

        # ── Clear current history display ──────────────────────────────
        while self._history_list.count():
            item = self._history_list.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        # ── History list ───────────────────────────────────────────────
        history_tasks = [t for t in tasks if t.get("status") in ("completed", "failed", "cancelled")]
        if not history_tasks:
            empty_h_lbl = QLabel("No recent task history.")
            empty_h_lbl.setStyleSheet(f"color:{_MUTED}; font-size:11px; font-style:italic;")
            self._history_list.addWidget(empty_h_lbl)
        else:
            for t in history_tasks:
                row = QFrame()
                row.setStyleSheet(f"background:#0c0d0f; border:none; border-radius:6px;")
                row_lay = QHBoxLayout(row)
                row_lay.setContentsMargins(8, 6, 8, 6)

                status = t.get("status", "completed")
                status_color = _OK if status == "completed" else _ERR
                status_text = status.upper()

                status_pill = QLabel(status_text)
                status_pill.setStyleSheet(
                    f"QLabel {{ color:{status_color}; font-size:9px; font-weight:700; "
                    f"border:none; border-radius:4px; padding:1px 4px; }}"
                )
                row_lay.addWidget(status_pill)

                info = QLabel(f"<b>[{t.get('agent_name')}]</b> {t.get('sub_task_desc')}")
                info.setTextFormat(Qt.RichText)
                info.setStyleSheet(f"color:{_TEXT}; font-size:11px;")
                info.setWordWrap(True)
                row_lay.addWidget(info, 1)

                btn_retry = QPushButton("Retry")
                btn_retry.setStyleSheet(
                    f"QPushButton {{ background:#212328; color:{_TEXT}; border:none; "
                    f"border-radius:4px; padding:2px 8px; font-weight:600; font-size:10px; }}"
                )
                btn_retry.clicked.connect(
                    lambda _=False, tid=t.get("id"): self._update_task(tid, "pending")
                )
                row_lay.addWidget(btn_retry)

                btn_events = QPushButton("Events")
                btn_events.setStyleSheet(
                    f"QPushButton {{ background:#212328; color:{_GOLD}; border:none; "
                    f"border-radius:4px; padding:2px 8px; font-weight:600; font-size:10px; }}"
                )
                btn_events.clicked.connect(
                    lambda _=False, tid=t.get("id"): self._load_task_events(tid)
                )
                row_lay.addWidget(btn_events)

                self._history_list.addWidget(row)

        self._history_list.addStretch(1)
        if self._selected_task_id is not None:
            self._load_task_events(self._selected_task_id)

    def _render_agent_breakdown(self) -> None:
        if not hasattr(self, "_agent_breakdown"):
            return
        tasks = self._last_tasks or []
        cooldowns = self._last_cooldowns or {}
        mission_agents = self._mission_agents or {}
        agent_names = ("claude-code", "codex", "antigravity", "hermes")
        active_statuses = {"pending", "assigned", "paused", "needs_clarification"}

        def _seen_text(seconds) -> str:
            if seconds is None:
                return "never"
            try:
                seconds = int(seconds)
            except Exception:
                return "unknown"
            if seconds < 90:
                return f"{seconds}s ago"
            minutes = seconds // 60
            if minutes < 90:
                return f"{minutes}m ago"
            hours = minutes // 60
            if hours < 48:
                return f"{hours}h ago"
            return f"{hours // 24}d ago"

        def _one_line(text: str, limit: int = 190) -> str:
            text = " ".join(str(text or "").split())
            return text if len(text) <= limit else text[: limit - 1] + "…"

        lines: list[str] = []
        for name in agent_names:
            agent_tasks = [t for t in tasks if str(t.get("agent_name", "")).lower() == name]
            counts = {k: 0 for k in ("pending", "assigned", "paused", "needs_clarification",
                                     "completed", "failed", "cancelled")}
            for task in agent_tasks:
                status = str(task.get("status") or "")
                if status in counts:
                    counts[status] += 1

            active_count = sum(counts[s] for s in active_statuses)
            finished_success_base = counts["completed"] + counts["failed"]
            success_rate = int((counts["completed"] / finished_success_base) * 100) if finished_success_base else 100
            info = mission_agents.get(name) or {}
            state = info.get("state") or {}
            current_task = info.get("current_task") or {}
            latest = info.get("latest_event") or {}
            cooldown = cooldowns.get(name) or info.get("cooldown")

            state_status = state.get("status") or ("cooldown" if cooldown else "idle")
            if not current_task and state_status in {"completed", "cancelled", "failed", "wake_exit"} and not cooldown:
                state_status = "idle"
            if cooldown:
                state_status = "cooldown"

            action = current_task.get("sub_task_desc")
            if not action:
                active = next((t for t in agent_tasks if t.get("status") in active_statuses), None)
                action = active.get("sub_task_desc") if active else "no active task"

            total = len(agent_tasks)
            lines.append(
                f"{name}: {state_status} | last {_seen_text(info.get('last_seen_seconds_ago'))} | "
                f"active {active_count} (pending {counts['pending']}, assigned {counts['assigned']}, "
                f"paused {counts['paused']}, clarify {counts['needs_clarification']}) | "
                f"done {counts['completed']} / failed {counts['failed']} / cancelled {counts['cancelled']} | "
                f"success {success_rate}% | total {total}"
            )
            lines.append(f"  action: {_one_line(action, 230)}")
            latest_content = latest.get("content")
            latest_kind = latest.get("event_type") or "event"
            if latest_content:
                lines.append(f"  latest {latest_kind}: {_one_line(latest_content, 230)}")
            if cooldown:
                reason = cooldown.get("reason", "cooldown") if isinstance(cooldown, dict) else "cooldown"
                lines.append(f"  cooldown: {_one_line(reason, 180)}")
            lines.append("")

        self._agent_breakdown.setPlainText("\n".join(lines).rstrip())

    def _update_task(self, task_id: int, status: str) -> None:
        self._threads.append(_spawn_http(
            self, "POST", f"{_API}/orchestrator/complete",
            lambda res: self.refresh(),
            json_body={"task_id": task_id, "status": status}
        ))

    def _control_task(self, task_id: int, action: str, note: str = "") -> None:
        body = {"action": action, "note": note}
        self._threads.append(_spawn_http(
            self, "POST", f"{_API}/orchestrator/tasks/{task_id}/control",
            lambda res: self.refresh(),
            json_body=body
        ))

    def _load_task_events(self, task_id: int) -> None:
        if not task_id:
            return
        self._selected_task_id = int(task_id)
        self._timeline.setPlainText(f"Loading task {task_id} events...")
        self._threads.append(_spawn_http(
            self, "GET", f"{_API}/orchestrator/tasks/{int(task_id)}/events?limit=200",
            self._on_task_events_loaded,
            timeout=8.0,
        ))

    def _on_task_events_loaded(self, res: dict) -> None:
        if not res.get("ok"):
            self._timeline.setPlainText(f"Timeline failed: {res.get('error')}")
            return
        data = res.get("data") or {}
        events = data.get("events") or []
        if not events:
            self._timeline.setPlainText(f"Task {data.get('task_id') or self._selected_task_id}: no events yet.")
            return
        import datetime as _dt
        lines = []
        for event in events:
            try:
                ts = _dt.datetime.fromtimestamp(int(event.get("created_at") or 0)).strftime("%H:%M:%S")
            except Exception:
                ts = "--:--:--"
            agent = event.get("agent_name") or "system"
            kind = event.get("event_type") or "event"
            content = " ".join(str(event.get("content") or "").split())
            lines.append(f"{ts} [{agent}] {kind}")
            if content:
                lines.append(f"  {content}")
        self._timeline.setPlainText("\n".join(lines))

    def _edit_task_from_input(self, task_id: int) -> None:
        replacement = self._input.toPlainText().strip()
        if not replacement:
            self._status.setText("Type replacement task text in the command box first")
            return
        self._threads.append(_spawn_http(
            self, "POST", f"{_API}/orchestrator/tasks/{task_id}/control",
            lambda res: self.refresh(),
            json_body={"action": "edit", "replacement_desc": replacement, "note": "manual edit"}
        ))
        self._input.clear()

    def _toggle_cooldown(self, agent_name: str, currently_cooldown: bool) -> None:
        if currently_cooldown:
            self._threads.append(_spawn_http(
                self, "POST", f"{_API}/agents/cooldown/clear",
                lambda res: self.refresh(),
                json_body={"agent_name": agent_name}
            ))
        else:
            self._threads.append(_spawn_http(
                self, "POST", f"{_API}/agents/cooldown",
                lambda res: self.refresh(),
                json_body={"agent_name": agent_name, "cooldown_seconds": 1800, "reason": "manual cooldown"}
            ))

    def _dispatch(self) -> None:
        prompt = self._input.toPlainText().strip()
        if not prompt:
            self._status.setText("Type a command first")
            return
        
        self._btn_dispatch.setEnabled(False)
        self._status.setText("Decomposing task...")
        self._progress.setRange(0, 0)
        self._progress.setVisible(True)
        self._threads.append(_spawn_http(
            self, "POST", f"{_API}/orchestrator/dispatch",
            self._on_dispatch_done,
            timeout=45.0,
            json_body={"prompt": prompt}
        ))

    def _on_dispatch_done(self, res: dict) -> None:
        self._btn_dispatch.setEnabled(True)
        self._progress.setVisible(False)
        if not res.get("ok"):
            self._status.setText(f"Dispatch failed: {res.get('error')}")
            return
        
        self._input.clear()
        self._status.setText("Dispatched successfully!")
        self.refresh()
