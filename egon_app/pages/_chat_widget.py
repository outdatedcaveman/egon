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
            # Consolidated surface (Bruno 2026-07-02): orders are auto-detected
            # and dispatched to the orchestrator; replies describe what queued.
            for piece in egon_chat.stream_chat_with_dispatch(
                self._messages, provider=self._provider, model=self._model,
                temperature=self._temperature, max_tokens=self._max_tokens,
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
        self._session_id: str = ""
        self._build(title)
        self._refresh_providers()
        self._load_persisted()
        # Separate conversations, shared with the phone via lib/chat_store —
        # watch the store and reload when another device changes it (never
        # mid-stream). Bruno 2026-07-04.
        from PySide6.QtCore import QTimer
        self._hist_mtime = self._hist_stat()
        self._sync_timer = QTimer(self)
        self._sync_timer.setInterval(10000)
        self._sync_timer.timeout.connect(self._maybe_reload_shared)
        self._sync_timer.start()

    def _streaming(self) -> bool:
        """True while a reply is in flight. MUST be exception-safe: the QThread
        deleteLater()s itself when finished, and touching the deleted wrapper
        raises RuntimeError — which, inside a Qt slot, was silently swallowed
        and made Send do NOTHING after the first reply (Bruno 2026-07-05)."""
        t = self._thread
        if t is None:
            return False
        try:
            return t.isRunning()
        except RuntimeError:      # wrapped C++ object already deleted
            self._thread = None
            return False

    def _hist_stat(self):
        try:
            from lib import chat_store
            return chat_store.mtime_signature()
        except Exception:
            return 0

    def _maybe_reload_shared(self):
        if self._streaming():
            return                      # streaming — don't yank the thread
        m = self._hist_stat()
        if m and m != self._hist_mtime:
            self._hist_mtime = m
            self._reset_thread_view()
            self._load_persisted()

    # ── conversations: separate sessions, one store shared with the phone
    # (lib/chat_store — no drift between surfaces). Bruno 2026-07-04. ────────
    def _persist(self):
        try:
            from lib import chat_store
            if self._session_id:
                chat_store.save(self._session_id, self._history)
            if hasattr(self, "_hist_mtime"):
                self._hist_mtime = self._hist_stat()   # own write ≠ reload
        except Exception:
            pass

    def _load_persisted(self):
        """Load the CURRENT session from the shared store and render it."""
        try:
            from lib import chat_store
            self._session_id = chat_store.current_id()
            hist = chat_store.load(self._session_id)
        except Exception:
            hist = []
        self._history = hist or []
        for m in self._history:
            role = "user" if m.get("role") == "user" else "assistant"
            c = m.get("content")
            if isinstance(c, list):
                text = " ".join(x.get("text", "") for x in c
                                if isinstance(x, dict) and x.get("type") == "text")
                names = [x.get("name", "") for x in c if isinstance(x, dict)
                         and x.get("type") in ("image", "audio", "video", "document")]
                note = "  ".join("📎 " + n for n in names if n)
                self._add_bubble(role, text, note)
            else:
                self._add_bubble(role, str(c or ""))
        self._scroll_bottom()
        self._refresh_sessions()

    def _refresh_sessions(self):
        if not hasattr(self, "_session_box"):
            return
        try:
            from lib import chat_store
            sessions = chat_store.list_sessions()
        except Exception:
            sessions = []
        self._session_box.blockSignals(True)
        self._session_box.clear()
        for s in sessions:
            self._session_box.addItem(s["title"][:44], userData=s["id"])
        idx = self._session_box.findData(self._session_id)
        if idx >= 0:
            self._session_box.setCurrentIndex(idx)
        self._session_box.blockSignals(False)

    def _switch_session(self, index: int):
        sid = self._session_box.itemData(index)
        if not sid or sid == self._session_id:
            return
        if self._worker:
            self._worker.stop()
        try:
            from lib import chat_store
            chat_store.set_current(sid)
        except Exception:
            return
        self._reset_thread_view()
        self._load_persisted()

    def _new_session(self):
        if self._worker:
            self._worker.stop()
        try:
            from lib import chat_store
            chat_store.new_session()
        except Exception:
            return
        self._reset_thread_view()
        self._load_persisted()
        self._input.setFocus()

    def _reset_thread_view(self):
        self._history = []
        while self._thread_lay.count():
            it = self._thread_lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self._empty = QLabel("No messages yet. Type below to start.")
        self._empty.setStyleSheet(f"color:{_MUTED}; font-size:12px; font-style:italic;")
        self._thread_lay.addWidget(self._empty)
        self._thread_lay.addStretch(1)

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

        # conversation row — its OWN line so it can't disappear next to the
        # model picker (Bruno 2026-07-04)
        conv_row = QHBoxLayout()
        conv_row.setSpacing(8)
        cl = QLabel("💬 Conversation")
        cl.setStyleSheet(f"color:{_GOLD}; font-weight:800; font-size:11px;")
        conv_row.addWidget(cl)
        self._session_box = QComboBox()
        self._session_box.setStyleSheet(
            f"QComboBox {{ background:{_PANEL_BG}; color:{_TEXT}; border:1px solid #2c3140; "
            "border-radius:6px; padding:5px 10px; font-size:12px; }}"
            f"QComboBox QAbstractItemView {{ background:{_PANEL_BG}; color:{_TEXT}; "
            f"selection-background-color:{_ACCENT}; }}")
        self._session_box.setMinimumWidth(340)
        self._session_box.currentIndexChanged.connect(self._switch_session)
        conv_row.addWidget(self._session_box, 1)
        newb = QPushButton("＋ New chat")
        newb.setCursor(Qt.PointingHandCursor)
        newb.setStyleSheet(
            f"QPushButton {{ background:#26292f; color:{_GOLD}; border:1px solid #2c3140; "
            "border-radius:6px; padding:5px 14px; font-size:11px; font-weight:700; }}")
        newb.clicked.connect(self._new_session)
        conv_row.addWidget(newb)
        lay.addLayout(conv_row)

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
        # Live auto-roll: whenever streamed chunks grow the thread, follow the
        # bottom (unless the user scrolled up) — see _follow_bottom.
        self._scroll.verticalScrollBar().rangeChanged.connect(self._follow_bottom)
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
            self, "Attach files (images, audio, video, documents)", "",
            "Supported (*.png *.jpg *.jpeg *.gif *.webp *.bmp "
            "*.mp3 *.wav *.m4a *.ogg *.flac *.mp4 *.mov *.webm "
            "*.pdf *.docx *.txt *.md *.json *.csv *.py *.js *.ts);;All files (*)")
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
        kind = {"image": "🖼", "audio": "🎵", "video": "🎬"}.get(
            part.get("type"), "📄")
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
        """'New chat': the old conversation stays in the sessions list (never
        deleted); a fresh one becomes current."""
        self._attachments = []
        while self._chips_row.count():
            it = self._chips_row.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self._chips_host.setVisible(False)
        self._new_session()

    def _add_bubble(self, role, text="", attach_note=""):
        if self._empty is not None:
            self._empty.deleteLater()
            self._empty = None
        b = _Bubble(role, text, attach_note)
        self._thread_lay.insertWidget(self._thread_lay.count() - 1, b)
        return b

    def _scroll_bottom(self):
        # Twice: now AND after Qt's deferred relayout — setValue(maximum())
        # before the appended text resizes the label scrolls to the OLD max,
        # which is why streaming replies never followed (Bruno 2026-07-12).
        from PySide6.QtCore import QTimer
        def _go():
            bar = self._scroll.verticalScrollBar()
            bar.setValue(bar.maximum())
        _go()
        QTimer.singleShot(0, _go)

    def _follow_bottom(self, _min: int, _max: int) -> None:
        """rangeChanged hook: auto-roll as streamed chunks grow the thread —
        but only when already near the bottom, so scrolling up to read
        history is never hijacked."""
        bar = self._scroll.verticalScrollBar()
        if _max - bar.value() <= bar.pageStep() * 2:
            bar.setValue(_max)

    def _on_send(self):
        if self._streaming():
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
        _ico = {"image": "🖼 ", "audio": "🎵 ", "video": "🎬 "}
        note = "  ".join(_ico.get(a.get("type"), "📄 ") + (a.get("name") or "")
                         for a in atts)
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
        self._thread = None      # deleteLater() will reap them; drop our refs
        self._worker = None
        self._send.setEnabled(True)
        self._send.setText("Send")
        self._scroll_bottom()
        self._persist()   # conversation survives app restarts
