"""Egon Chat widget — a real streaming conversation surface (like claude.ai).

Bruno wants Mission Control to BE a chat box: he types, Egon replies in
descriptive text in real time, and the conversation continues. Backed by a
CLOUD model (lib/egon_chat) — never a local LLM (thrashes the 8GB box).

Design:
  • ONE-DIRECTIONAL. Types in → cloud model out. Vault context injected as data.
    Never dispatches agents or calls itself ("don't become schizo" — Bruno).
  • Streaming: a QThread pulls chunks from egon_chat.stream_chat and emits each
    to the UI thread, appended live to the current assistant bubble.
  • Reusable: mounted in the Orchestrator page; the same backend serves the phone.
Bruno 2026-07-01.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QObject, QThread, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QPlainTextEdit,
    QScrollArea, QFrame, QComboBox, QSizePolicy,
)

_BG_CARD = "#16181c"
_PANEL_BG = "#0c0d0f"
_TEXT = "#f5f5f7"
_MUTED = "#76767f"
_ACCENT = "#5ac8fa"
_GOLD = "#ff9f0a"
_USER_BG = "#1d2b33"
_ASSIST_BG = "#17191d"
_ERR = "#ff453a"


class _StreamWorker(QObject):
    chunk = Signal(str)
    done = Signal()
    error = Signal(str)

    def __init__(self, messages: list[dict], provider: str):
        super().__init__()
        self._messages = messages
        self._provider = provider
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        try:
            from lib import egon_chat
            got = False
            for piece in egon_chat.stream_chat(
                self._messages, provider=self._provider, inject_context=True
            ):
                if self._stop:
                    break
                if piece:
                    got = True
                    self.chunk.emit(piece)
            if not got and not self._stop:
                self.error.emit("(empty reply — the model returned nothing)")
        except Exception as exc:
            msg = str(exc)
            if "429" in msg:
                msg = "rate-limited (429) — try another provider from the dropdown"
            elif "no API key" in msg:
                msg = msg
            self.error.emit(msg[:240])
        finally:
            self.done.emit()


class _Bubble(QFrame):
    """One message bubble. Assistant bubbles get text appended live."""

    def __init__(self, role: str, text: str = ""):
        super().__init__()
        self._role = role
        is_user = role == "user"
        bg = _USER_BG if is_user else _ASSIST_BG
        self.setStyleSheet(
            f"QFrame {{ background:{bg}; border:none; border-radius:10px; }}"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(11, 8, 11, 8)
        lay.setSpacing(2)
        who = QLabel("You" if is_user else "Egon")
        who.setStyleSheet(
            f"color:{_ACCENT if is_user else _GOLD}; font-size:10px; font-weight:800;"
        )
        lay.addWidget(who)
        self._label = QLabel(text)
        self._label.setWordWrap(True)
        self._label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._label.setStyleSheet(f"color:{_TEXT}; font-size:13px; background:transparent;")
        lay.addWidget(self._label)

    def append(self, text: str) -> None:
        self._label.setText(self._label.text() + text)

    def set_text(self, text: str) -> None:
        self._label.setText(text)

    def text(self) -> str:
        return self._label.text()


class ChatWidget(QWidget):
    """A self-contained streaming chat. `history` is a list of {role, content}."""

    def __init__(self, parent=None, title: str = "EGON CHAT"):
        super().__init__(parent)
        self._history: list[dict] = []
        self._thread: QThread | None = None
        self._worker: _StreamWorker | None = None
        self._cur_bubble: _Bubble | None = None
        self._build(title)
        self._refresh_providers()

    def _build(self, title: str) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)

        card = QFrame()
        card.setStyleSheet(f"QFrame {{ background:{_BG_CARD}; border:none; border-radius:10px; }}")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(12, 10, 12, 12)
        lay.setSpacing(8)

        head = QHBoxLayout()
        t = QLabel(title)
        t.setStyleSheet(f"color:{_TEXT}; font-weight:800; font-size:12px;")
        head.addWidget(t)
        sub = QLabel("Ask Egon anything — grounded in your vault. Streams in real time.")
        sub.setStyleSheet(f"color:{_MUTED}; font-size:11px;")
        head.addWidget(sub)
        head.addStretch(1)
        self._provider = QComboBox()
        self._provider.setStyleSheet(
            f"QComboBox {{ background:{_PANEL_BG}; color:{_TEXT}; border:1px solid #22252a; "
            "border-radius:6px; padding:3px 8px; font-size:11px; }}"
            f"QComboBox QAbstractItemView {{ background:{_PANEL_BG}; color:{_TEXT}; "
            f"selection-background-color:{_ACCENT}; }}"
        )
        head.addWidget(self._provider)
        clr = QPushButton("Clear")
        clr.setCursor(Qt.PointingHandCursor)
        clr.setStyleSheet(
            f"QPushButton {{ background:#212328; color:{_TEXT}; border:none; "
            "border-radius:6px; padding:4px 10px; font-size:11px; }}"
        )
        clr.clicked.connect(self.clear)
        head.addWidget(clr)
        lay.addLayout(head)

        # message thread
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet(
            "QScrollArea { border:none; background:transparent; }"
            f"QScrollBar:vertical {{ background:{_PANEL_BG}; width:9px; margin:0; }}"
            "QScrollBar::handle:vertical { background:#333842; border-radius:4px; min-height:28px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }"
        )
        self._thread_host = QWidget()
        self._thread_lay = QVBoxLayout(self._thread_host)
        self._thread_lay.setContentsMargins(2, 2, 2, 2)
        self._thread_lay.setSpacing(8)
        self._empty = QLabel("No messages yet. Type below to start.")
        self._empty.setStyleSheet(f"color:{_MUTED}; font-size:12px; font-style:italic;")
        self._thread_lay.addWidget(self._empty)
        self._thread_lay.addStretch(1)
        self._scroll.setWidget(self._thread_host)
        self._scroll.setMinimumHeight(300)
        self._scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        lay.addWidget(self._scroll, 1)

        # input row
        row = QHBoxLayout()
        row.setSpacing(8)
        self._input = QPlainTextEdit()
        self._input.setPlaceholderText("Message Egon…  (Enter to send · Shift+Enter for newline)")
        self._input.setFixedHeight(64)
        self._input.setStyleSheet(
            f"QPlainTextEdit {{ background:{_PANEL_BG}; color:{_TEXT}; border:1px solid #22252a; "
            "border-radius:8px; padding:8px; font-size:13px; }}"
        )
        self._input.installEventFilter(self)
        row.addWidget(self._input, 1)
        self._send = QPushButton("Send")
        self._send.setCursor(Qt.PointingHandCursor)
        self._send.setFixedWidth(84)
        self._send.setStyleSheet(
            f"QPushButton {{ background:{_GOLD}; color:#16181c; border:none; "
            "border-radius:8px; padding:8px; font-weight:800; font-size:13px; }}"
            f"QPushButton:disabled {{ background:#2a2c31; color:{_MUTED}; }}"
        )
        self._send.clicked.connect(self._on_send)
        row.addWidget(self._send)
        lay.addLayout(row)

        outer.addWidget(card)

    def _refresh_providers(self) -> None:
        try:
            from lib import egon_chat
            avail = egon_chat.available_providers()
        except Exception:
            avail = {}
        self._provider.clear()
        first_ok = None
        for name in ("gemini", "claude", "openai"):
            ok = avail.get(name, False)
            label = name + ("" if ok else "  (no key)")
            self._provider.addItem(label, userData=name)
            if ok and first_ok is None:
                first_ok = self._provider.count() - 1
        if first_ok is not None:
            self._provider.setCurrentIndex(first_ok)

    # ── Enter-to-send ────────────────────────────────────────────────────────
    def eventFilter(self, obj, event):
        if obj is self._input and event.type() == event.Type.KeyPress:
            key = event.key()
            if key in (Qt.Key_Return, Qt.Key_Enter) and not (event.modifiers() & Qt.ShiftModifier):
                self._on_send()
                return True
        return super().eventFilter(obj, event)

    def clear(self) -> None:
        if self._worker:
            self._worker.stop()
        self._history = []
        while self._thread_lay.count():
            it = self._thread_lay.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()
        self._empty = QLabel("No messages yet. Type below to start.")
        self._empty.setStyleSheet(f"color:{_MUTED}; font-size:12px; font-style:italic;")
        self._thread_lay.addWidget(self._empty)
        self._thread_lay.addStretch(1)

    def _add_bubble(self, role: str, text: str = "") -> _Bubble:
        if self._empty is not None:
            self._empty.deleteLater()
            self._empty = None
        b = _Bubble(role, text)
        # insert before the trailing stretch
        self._thread_lay.insertWidget(self._thread_lay.count() - 1, b)
        return b

    def _scroll_bottom(self) -> None:
        bar = self._scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _on_send(self) -> None:
        if self._thread is not None and self._thread.isRunning():
            return  # a reply is streaming
        text = self._input.toPlainText().strip()
        if not text:
            return
        provider = self._provider.currentData() or "gemini"
        self._input.clear()
        self._history.append({"role": "user", "content": text})
        self._add_bubble("user", text)
        self._cur_bubble = self._add_bubble("assistant", "")
        self._cur_bubble.set_text("…")
        self._scroll_bottom()
        self._send.setEnabled(False)
        self._send.setText("…")

        self._thread = QThread(self)
        self._worker = _StreamWorker(list(self._history), provider)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.chunk.connect(self._on_chunk)
        self._worker.error.connect(self._on_error)
        self._worker.done.connect(self._on_done)
        self._worker.done.connect(self._thread.quit)
        self._worker.done.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    def _on_chunk(self, piece: str) -> None:
        if self._cur_bubble is None:
            return
        if self._cur_bubble.text() == "…":
            self._cur_bubble.set_text("")
        self._cur_bubble.append(piece)
        self._scroll_bottom()

    def _on_error(self, msg: str) -> None:
        if self._cur_bubble is not None:
            cur = self._cur_bubble.text()
            if cur in ("…", ""):
                self._cur_bubble.set_text(f"⚠ {msg}")
            else:
                self._cur_bubble.append(f"\n\n⚠ {msg}")
            self._cur_bubble._label.setStyleSheet(
                f"color:{_ERR}; font-size:13px; background:transparent;"
            ) if cur in ("…", "") else None

    def _on_done(self) -> None:
        if self._cur_bubble is not None:
            reply = self._cur_bubble.text()
            if reply and not reply.startswith("⚠"):
                self._history.append({"role": "assistant", "content": reply})
        self._cur_bubble = None
        self._send.setEnabled(True)
        self._send.setText("Send")
        self._scroll_bottom()
