"""Egon Chat widget — a real streaming conversation surface (like claude.ai).

Bruno's primary high-quality work surface: he types, attaches images/documents,
picks the model + parameters, and Egon replies in real time — grounded in his
unified mind AND actual project repo source. Cloud-backed (lib/egon_chat); never
a local LLM (thrashes the 8GB box).

Parity features:
  • Provider + model pickers (top-tier default: Opus / GPT-5.5 / Gemini 2.5 Pro).
  • Attachments: images (+ paste a screenshot with Ctrl+V) and documents
    (PDF/docx/txt/code → extracted text). Multimodal content parts.
  • Parameters: temperature + max tokens.
  • Streaming, Enter-to-send, vault + repo grounding, one-directional.
Bruno 2026-07-01.
"""
from __future__ import annotations

import base64

from PySide6.QtCore import Qt, QObject, QThread, Signal, QBuffer, QIODevice
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QPlainTextEdit,
    QScrollArea, QFrame, QComboBox, QSizePolicy, QFileDialog, QApplication,
    QDoubleSpinBox, QSpinBox,
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
_CHIP_BG = "#222634"


class _StreamWorker(QObject):
    chunk = Signal(str)
    done = Signal()
    error = Signal(str)

    def __init__(self, messages, provider, model, temperature, max_tokens):
        super().__init__()
        self._messages = messages
        self._provider = provider
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        try:
            from lib import egon_chat
            got = False
            for piece in egon_chat.stream_chat(
                self._messages, provider=self._provider, model=self._model,
                inject_context=True, temperature=self._temperature,
                max_tokens=self._max_tokens,
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
            if "429" in msg or "quota" in msg.lower():
                msg = "rate-limited/quota — try another model or provider"
            self.error.emit(msg[:240])
        finally:
            self.done.emit()


class _Bubble(QFrame):
    def __init__(self, role: str, text: str = "", attach_note: str = ""):
        super().__init__()
        is_user = role == "user"
        bg = _USER_BG if is_user else _ASSIST_BG
        self.setStyleSheet(f"QFrame {{ background:{bg}; border:none; border-radius:10px; }}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(11, 8, 11, 8)
        lay.setSpacing(2)
        who = QLabel("You" if is_user else "Egon")
        who.setStyleSheet(f"color:{_ACCENT if is_user else _GOLD}; font-size:10px; font-weight:800;")
        lay.addWidget(who)
        if attach_note:
            an = QLabel(attach_note)
            an.setWordWrap(True)
            an.setStyleSheet(f"color:{_ACCENT}; font-size:11px;")
            lay.addWidget(an)
        self._label = QLabel(text)
        self._label.setWordWrap(True)
        self._label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._label.setStyleSheet(f"color:{_TEXT}; font-size:13px; background:transparent;")
        lay.addWidget(self._label)

    def append(self, text): self._label.setText(self._label.text() + text)
    def set_text(self, text): self._label.setText(text)
    def text(self): return self._label.text()


class ChatWidget(QWidget):
    def __init__(self, parent=None, title: str = "EGON CHAT"):
        super().__init__(parent)
        self._history: list[dict] = []
        self._attachments: list[dict] = []   # pending message parts (image/document)
        self._thread: QThread | None = None
        self._worker: _StreamWorker | None = None
        self._cur_bubble: _Bubble | None = None
        self._build(title)
        self._refresh_providers()

    # ── build ────────────────────────────────────────────────────────────────
    def _combo_css(self):
        return (f"QComboBox {{ background:{_PANEL_BG}; color:{_TEXT}; border:1px solid #22252a; "
                "border-radius:6px; padding:3px 8px; font-size:11px; }}"
                f"QComboBox QAbstractItemView {{ background:{_PANEL_BG}; color:{_TEXT}; "
                f"selection-background-color:{_ACCENT}; }}")

    def _build(self, title):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)
        card = QFrame()
        card.setStyleSheet(f"QFrame {{ background:{_BG_CARD}; border:none; border-radius:10px; }}")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(12, 10, 12, 12)
        lay.setSpacing(8)

        # header: title + provider/model + params + clear
        head = QHBoxLayout()
        head.setSpacing(6)
        t = QLabel(title)
        t.setStyleSheet(f"color:{_TEXT}; font-weight:800; font-size:12px;")
        head.addWidget(t)
        head.addStretch(1)
        self._provider = QComboBox(); self._provider.setStyleSheet(self._combo_css())
        self._provider.currentIndexChanged.connect(self._refresh_models)
        head.addWidget(self._provider)
        self._model = QComboBox(); self._model.setStyleSheet(self._combo_css())
        self._model.setMinimumWidth(150)
        head.addWidget(self._model)
        # parameters
        tlbl = QLabel("temp"); tlbl.setStyleSheet(f"color:{_MUTED}; font-size:10px;")
        head.addWidget(tlbl)
        self._temp = QDoubleSpinBox(); self._temp.setRange(0.0, 1.0); self._temp.setSingleStep(0.1)
        self._temp.setValue(0.7); self._temp.setFixedWidth(56)
        self._temp.setStyleSheet(self._combo_css().replace("QComboBox", "QDoubleSpinBox"))
        head.addWidget(self._temp)
        mlbl = QLabel("max"); mlbl.setStyleSheet(f"color:{_MUTED}; font-size:10px;")
        head.addWidget(mlbl)
        self._maxtok = QSpinBox(); self._maxtok.setRange(256, 32000); self._maxtok.setSingleStep(512)
        self._maxtok.setValue(4096); self._maxtok.setFixedWidth(74)
        self._maxtok.setStyleSheet(self._combo_css().replace("QComboBox", "QSpinBox"))
        head.addWidget(self._maxtok)
        clr = QPushButton("Clear"); clr.setCursor(Qt.PointingHandCursor)
        clr.setStyleSheet(f"QPushButton {{ background:#212328; color:{_TEXT}; border:none; "
                          "border-radius:6px; padding:4px 10px; font-size:11px; }}")
        clr.clicked.connect(self.clear)
        head.addWidget(clr)
        lay.addLayout(head)
        sub = QLabel("Grounded in your vault + project repos. Attach images/documents, "
                     "paste a screenshot (Ctrl+V), pick model & parameters.")
        sub.setStyleSheet(f"color:{_MUTED}; font-size:11px;")
        sub.setWordWrap(True)
        lay.addWidget(sub)

        # message thread
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet(
            "QScrollArea { border:none; background:transparent; }"
            f"QScrollBar:vertical {{ background:{_PANEL_BG}; width:9px; margin:0; }}"
            "QScrollBar::handle:vertical { background:#333842; border-radius:4px; min-height:28px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }")
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

        # attachment chips row (hidden until something is attached)
        self._chips_row = QHBoxLayout()
        self._chips_row.setSpacing(6)
        self._chips_host = QWidget()
        self._chips_host.setLayout(self._chips_row)
        self._chips_host.setVisible(False)
        lay.addWidget(self._chips_host)

        # input row
        row = QHBoxLayout(); row.setSpacing(8)
        self._attach_btn = QPushButton("📎")
        self._attach_btn.setToolTip("Attach image or document")
        self._attach_btn.setCursor(Qt.PointingHandCursor)
        self._attach_btn.setFixedWidth(40)
        self._attach_btn.setStyleSheet(
            f"QPushButton {{ background:#212328; color:{_TEXT}; border:none; "
            "border-radius:8px; padding:8px; font-size:15px; }}")
        self._attach_btn.clicked.connect(self._pick_files)
        row.addWidget(self._attach_btn)
        self._input = QPlainTextEdit()
        self._input.setPlaceholderText("Message Egon…  (Enter to send · Shift+Enter newline · Ctrl+V pastes an image)")
        self._input.setFixedHeight(64)
        self._input.setStyleSheet(
            f"QPlainTextEdit {{ background:{_PANEL_BG}; color:{_TEXT}; border:1px solid #22252a; "
            "border-radius:8px; padding:8px; font-size:13px; }}")
        self._input.installEventFilter(self)
        row.addWidget(self._input, 1)
        self._send = QPushButton("Send")
        self._send.setCursor(Qt.PointingHandCursor)
        self._send.setFixedWidth(84)
        self._send.setStyleSheet(
            f"QPushButton {{ background:{_GOLD}; color:#16181c; border:none; "
            "border-radius:8px; padding:8px; font-weight:800; font-size:13px; }}"
            f"QPushButton:disabled {{ background:#2a2c31; color:{_MUTED}; }}")
        self._send.clicked.connect(self._on_send)
        row.addWidget(self._send)
        lay.addLayout(row)
        outer.addWidget(card)

    # ── providers / models ─────────────────────────────────────────────────
    def _refresh_providers(self):
        try:
            from lib import egon_chat
            avail = egon_chat.available_providers()
        except Exception:
            avail = {}
        self._provider.blockSignals(True)
        self._provider.clear()
        first_ok = None
        for name in ("gemini", "claude", "openai"):
            ok = avail.get(name, False)
            self._provider.addItem(name + ("" if ok else "  (no key)"), userData=name)
            if ok and first_ok is None:
                first_ok = self._provider.count() - 1
        if first_ok is not None:
            self._provider.setCurrentIndex(first_ok)
        self._provider.blockSignals(False)
        self._refresh_models()

    def _refresh_models(self):
        try:
            from lib import egon_chat
            prov = self._provider.currentData() or "gemini"
            models = egon_chat.models_for(prov)
        except Exception:
            models = []
        self._model.clear()
        for m in models:
            self._model.addItem(m, userData=m)
        if models:
            self._model.setCurrentIndex(0)  # top-tier first

    # ── attachments ─────────────────────────────────────────────────────────
    def _pick_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Attach images or documents", "",
            "Supported (*.png *.jpg *.jpeg *.gif *.webp *.bmp *.pdf *.docx *.txt "
            "*.md *.json *.csv *.py *.js *.ts);;All files (*)")
        for p in paths or []:
            self._attach_path(p)

    def _attach_path(self, path: str):
        try:
            from lib import egon_chat
            part = egon_chat.attach_from_path(path)
        except Exception:
            part = None
        if part:
            self._add_attachment(part)

    def _add_attachment(self, part: dict):
        self._attachments.append(part)
        kind = "🖼" if part.get("type") == "image" else "📄"
        name = part.get("name") or part.get("type")
        chip = QLabel(f"{kind} {name}  ✕")
        chip.setCursor(Qt.PointingHandCursor)
        chip.setStyleSheet(f"QLabel {{ background:{_CHIP_BG}; color:{_TEXT}; border-radius:8px; "
                           "padding:3px 8px; font-size:11px; }}")
        chip._part = part  # type: ignore[attr-defined]

        def _remove(ev, c=chip, p=part):
            try:
                self._attachments.remove(p)
            except ValueError:
                pass
            c.deleteLater()
            if not self._attachments:
                self._chips_host.setVisible(False)
        chip.mousePressEvent = _remove
        self._chips_row.addWidget(chip)
        self._chips_host.setVisible(True)

    def _paste_image(self) -> bool:
        img = QApplication.clipboard().image()
        if img.isNull():
            return False
        buf = QBuffer()
        buf.open(QIODevice.WriteOnly)
        img.save(buf, "PNG")
        data = base64.b64encode(bytes(buf.data())).decode("ascii")
        self._add_attachment({"type": "image", "mime": "image/png",
                              "data": data, "name": "pasted.png"})
        return True

    # ── send / stream ────────────────────────────────────────────────────────
    def eventFilter(self, obj, event):
        if obj is self._input and event.type() == event.Type.KeyPress:
            k = event.key()
            mods = event.modifiers()
            if k in (Qt.Key_Return, Qt.Key_Enter) and not (mods & Qt.ShiftModifier):
                self._on_send()
                return True
            if k == Qt.Key_V and (mods & Qt.ControlModifier):
                if self._paste_image():   # image on clipboard → attach, don't paste bytes
                    return True
        return super().eventFilter(obj, event)

    def clear(self):
        if self._worker:
            self._worker.stop()
        self._history = []
        self._attachments = []
        while self._chips_row.count():
            it = self._chips_row.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self._chips_host.setVisible(False)
        while self._thread_lay.count():
            it = self._thread_lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self._empty = QLabel("No messages yet. Type below to start.")
        self._empty.setStyleSheet(f"color:{_MUTED}; font-size:12px; font-style:italic;")
        self._thread_lay.addWidget(self._empty)
        self._thread_lay.addStretch(1)

    def _add_bubble(self, role, text="", attach_note=""):
        if self._empty is not None:
            self._empty.deleteLater()
            self._empty = None
        b = _Bubble(role, text, attach_note)
        self._thread_lay.insertWidget(self._thread_lay.count() - 1, b)
        return b

    def _scroll_bottom(self):
        bar = self._scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _on_send(self):
        if self._thread is not None and self._thread.isRunning():
            return
        text = self._input.toPlainText().strip()
        if not text and not self._attachments:
            return
        provider = self._provider.currentData() or "gemini"
        model = self._model.currentData()
        atts = list(self._attachments)
        # build multimodal content: text + attachment parts
        if atts:
            content = ([{"type": "text", "text": text}] if text else []) + atts
        else:
            content = text
        self._history.append({"role": "user", "content": content})
        note = "  ".join(("🖼 " if a.get("type") == "image" else "📄 ")
                         + (a.get("name") or "") for a in atts)
        self._add_bubble("user", text, note)
        # reset input + attachments
        self._input.clear()
        self._attachments = []
        while self._chips_row.count():
            it = self._chips_row.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self._chips_host.setVisible(False)

        self._cur_bubble = self._add_bubble("assistant", "…")
        self._scroll_bottom()
        self._send.setEnabled(False)
        self._send.setText("…")
        self._thread = QThread(self)
        self._worker = _StreamWorker(list(self._history), provider, model,
                                     float(self._temp.value()), int(self._maxtok.value()))
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.chunk.connect(self._on_chunk)
        self._worker.error.connect(self._on_error)
        self._worker.done.connect(self._on_done)
        self._worker.done.connect(self._thread.quit)
        self._worker.done.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    def _on_chunk(self, piece):
        if self._cur_bubble is None:
            return
        if self._cur_bubble.text() == "…":
            self._cur_bubble.set_text("")
        self._cur_bubble.append(piece)
        self._scroll_bottom()

    def _on_error(self, msg):
        if self._cur_bubble is not None:
            cur = self._cur_bubble.text()
            self._cur_bubble.set_text((cur if cur not in ("…", "") else "") + f"\n⚠ {msg}")

    def _on_done(self):
        if self._cur_bubble is not None:
            reply = self._cur_bubble.text()
            if reply and not reply.strip().startswith("⚠"):
                self._history.append({"role": "assistant", "content": reply})
        self._cur_bubble = None
        self._send.setEnabled(True)
        self._send.setText("Send")
        self._scroll_bottom()
