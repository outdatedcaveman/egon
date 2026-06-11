"""Connect page — "what in my archives connects to what I'm writing?"

Bruno's ambition (2026-06-06): be writing something, press a button, and have
Egon surface things from your archives that connect to it — saved articles,
papers, books, films, videos, bookmarks, notes — plus links to previously
unknown things worth your attention. This is the laptop surface; the engine
(lib/connection_engine.py) is also exposed at POST /api/v1/mind/connect for the
phone / Chrome extension to hit.

Paste a paragraph (or a whole page) of what you're working on → Connect ✨ →
ranked connections grouped into "From your archives" and "From your mind",
each with the matched terms and a click-to-open link.
"""
from __future__ import annotations

import webbrowser

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QPlainTextEdit,
    QScrollArea, QFrame,
)

_BG = "#0E2630"
_BORDER = "#1F4858"
_ACCENT = "#7BC5C7"
_TEXT = "#F0E9D5"
_MUTED = "#9CA3AF"
_GOLD = "#D4A24C"

_SRC_ICON = {
    "instapaper": "📰", "zotero": "📚", "paperpile": "📄", "kindle": "📖",
    "letterboxd": "🎬", "youtube_music": "🎵", "pocketcasts": "🎧",
    "chrome_bookmarks": "🔖", "chrome_tabs": "🗂️", "notion_workspace": "🟦",
    "tvtime": "📺", "mind-memory": "🧠",
}


def _icon(src: str) -> str:
    return _SRC_ICON.get((src or "").lower(), "•")


class _Worker(QThread):
    done = Signal(dict, str)   # result, error

    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self._text = text

    def run(self):
        try:
            from lib.connection_engine import connect
            self.done.emit(connect(self._text, limit=18), "")
        except Exception as e:
            self.done.emit({}, f"{type(e).__name__}: {e}")


def _hit_row(hit: dict) -> QFrame:
    card = QFrame()
    card.setObjectName("hit")
    card.setStyleSheet(
        f"QFrame#hit {{ background:{_BG}; border:1px solid {_BORDER}; "
        f"border-radius:8px; }}")
    v = QVBoxLayout(card); v.setContentsMargins(12, 8, 12, 8); v.setSpacing(2)
    top = QHBoxLayout(); top.setSpacing(8)
    ic = QLabel(_icon(hit.get("source", ""))); ic.setStyleSheet("font-size:15px;")
    top.addWidget(ic)
    title = QLabel(hit.get("title", "")[:120])
    title.setStyleSheet(f"color:{_TEXT}; font-weight:600;")
    title.setWordWrap(True)
    top.addWidget(title, 1)
    url = hit.get("url")
    if url:
        b = QPushButton("Open")
        b.setStyleSheet(
            f"background:{_ACCENT}; color:#0E2630; border:none; border-radius:4px; "
            f"padding:3px 12px; font-weight:600;")
        b.clicked.connect(lambda _=False, u=url: webbrowser.open(u, new=2))
        top.addWidget(b)
    v.addLayout(top)
    sub = hit.get("snippet") or ""
    why = hit.get("why") or []
    meta = QLabel(f"<span style='color:{_MUTED}'>{hit.get('source','')}</span>"
                  + (f" · {sub[:90]}" if sub else "")
                  + (f"  <span style='color:{_GOLD}'>↳ {', '.join(why)}</span>" if why else ""))
    meta.setTextFormat(Qt.RichText); meta.setStyleSheet("font-size:11px;")
    meta.setWordWrap(True)
    v.addWidget(meta)
    return card


class ConnectPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 18, 24, 18); root.setSpacing(12)

        title = QLabel("Connect")
        title.setStyleSheet(f"color:{_TEXT}; font-size:22px; font-weight:700;")
        root.addWidget(title)
        sub = QLabel("Paste what you're writing or thinking about — Egon surfaces "
                     "connections from everything you've saved (articles, papers, "
                     "books, films, videos, bookmarks, notes) and your shared mind. "
                     "100% local, no tokens spent.")
        sub.setStyleSheet(f"color:{_MUTED};"); sub.setWordWrap(True)
        root.addWidget(sub)

        self._input = QPlainTextEdit()
        self._input.setPlaceholderText("Start typing or paste a paragraph…  (Ctrl+Enter to connect)")
        self._input.setStyleSheet(
            f"QPlainTextEdit {{ background:#102F3C; color:{_TEXT}; "
            f"border:1px solid {_BORDER}; border-radius:6px; padding:8px; "
            f"font-size:13px; }}")
        self._input.setFixedHeight(120)
        root.addWidget(self._input)

        row = QHBoxLayout()
        self._btn = QPushButton("✨ Connect")
        self._btn.setStyleSheet(
            f"QPushButton {{ background:{_GOLD}; color:#0E2630; border:none; "
            f"border-radius:6px; padding:8px 22px; font-weight:700; font-size:13px; }}")
        self._btn.clicked.connect(self._go)
        row.addWidget(self._btn)
        paste = QPushButton("Paste & Connect")
        paste.setStyleSheet(
            f"QPushButton {{ background:#16404F; color:{_TEXT}; border:1px solid {_BORDER}; "
            f"border-radius:6px; padding:8px 16px; }}")
        paste.clicked.connect(self._paste_and_go)
        row.addWidget(paste)
        self._status = QLabel("")
        self._status.setStyleSheet(f"color:{_MUTED};")
        row.addWidget(self._status); row.addStretch(1)
        root.addLayout(row)

        scroll = QScrollArea()
        scroll.setObjectName("cscroll")
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(
            f"QScrollArea#cscroll {{ border:1px solid {_BORDER}; border-radius:8px; "
            f"background:transparent; }}")
        self._host = QWidget()
        self._results = QVBoxLayout(self._host)
        self._results.setContentsMargins(10, 10, 10, 10); self._results.setSpacing(8)
        self._results.addStretch(1)
        scroll.setWidget(self._host)
        root.addWidget(scroll, 1)

    # Ctrl+Enter to connect
    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter) and (e.modifiers() & Qt.ControlModifier):
            self._go(); return
        super().keyPressEvent(e)

    def _paste_and_go(self):
        cb = QGuiApplication.clipboard().text()
        if cb.strip():
            self._input.setPlainText(cb)
        self._go()

    def _go(self):
        text = self._input.toPlainText().strip()
        if len(text) < 3:
            self._status.setText("type or paste a few words first")
            return
        self._btn.setEnabled(False)
        self._status.setText("connecting…")
        self._worker = _Worker(text, self)
        self._worker.done.connect(self._render)
        self._worker.start()

    def _clear(self):
        while self._results.count():
            it = self._results.takeAt(0)
            if it.widget():
                it.widget().deleteLater()

    def _render(self, result: dict, err: str):
        self._btn.setEnabled(True)
        self._clear()
        if err:
            self._status.setText(f"error: {err}")
            self._results.addStretch(1); return
        conns = result.get("connections", [])
        terms = result.get("terms", [])
        self._status.setText(f"{len(conns)} connections · key terms: {', '.join(terms[:8])}")
        archives = [c for c in conns if c.get("source") != "mind-memory"]
        mind = [c for c in conns if c.get("source") == "mind-memory"]
        if archives:
            h = QLabel("From your archives"); h.setStyleSheet(f"color:{_ACCENT}; font-weight:700; padding-top:4px;")
            self._results.addWidget(h)
            for c in archives:
                self._results.addWidget(_hit_row(c))
        if mind:
            h = QLabel("From your shared mind"); h.setStyleSheet(f"color:{_GOLD}; font-weight:700; padding-top:8px;")
            self._results.addWidget(h)
            for c in mind:
                self._results.addWidget(_hit_row(c))
        if not conns:
            self._results.addWidget(QLabel("No connections found — try more distinctive words."))
        self._results.addStretch(1)
