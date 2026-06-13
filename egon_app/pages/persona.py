"""Persona — the digital-double dashboard.

Bruno 2026-06-12: gather the behavioural/personal data — fitness, interests,
media taste, reading — that defines who he is, as the foundation for an AI
double. Reads lib.persona (a lens over snapshots already in the store);
nothing new is collected here. A 'Generate digital-double summary' button
asks the local LLM to turn the numbers into a perceptive prose profile.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QFrame,
    QPushButton, QScrollArea, QSizePolicy,
)

_TEXT = "#F0E9D5"; _MUTED = "#9CA3AF"; _GOLD = "#D4A24C"; _ACCENT = "#7BC5C7"
_CARD = "#0E2630"; _BORDER = "#1F4858"; _OK = "#7FB069"; _VIO = "#9D7BD8"


def _stat(value: str, label: str, accent: str = _GOLD) -> QFrame:
    f = QFrame()
    f.setStyleSheet(f"QFrame {{ background:{_CARD}; border:1px solid {_BORDER}; "
                    f"border-radius:8px; }}")
    f.setMinimumHeight(86)
    v = QVBoxLayout(f); v.setContentsMargins(16, 12, 16, 12); v.setSpacing(2)
    val = QLabel(value); val.setStyleSheet(
        f"color:{accent}; font-size:23px; font-weight:800; border:none;")
    v.addWidget(val)
    lb = QLabel(label.upper()); lb.setStyleSheet(
        f"color:{_MUTED}; font-size:10px; font-weight:700; border:none;")
    lb.setWordWrap(True)
    v.addWidget(lb); v.addStretch(1)
    return f


def _chips(title: str, items: list[str], accent: str = _ACCENT) -> QFrame:
    f = QFrame()
    f.setStyleSheet(f"QFrame {{ background:{_CARD}; border:1px solid {_BORDER}; "
                    f"border-radius:8px; }}")
    v = QVBoxLayout(f); v.setContentsMargins(14, 12, 14, 12); v.setSpacing(8)
    h = QLabel(title); h.setStyleSheet(
        f"color:{_TEXT}; font-size:13px; font-weight:700; border:none;")
    v.addWidget(h)
    flow = QLabel("  ".join(f"<span style='background:{_BORDER}; color:{accent}; "
                            f"padding:2px 8px; border-radius:10px;'>{c}</span>"
                            for c in items[:40]) or
                  f"<span style='color:{_MUTED}'>no data yet</span>")
    flow.setTextFormat(Qt.RichText); flow.setWordWrap(True)
    flow.setStyleSheet("border:none;")
    v.addWidget(flow)
    return f


class PersonaPage(QWidget):
    _ready = Signal(dict)
    _prose_ready = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 18, 24, 12); outer.setSpacing(10)

        head = QHBoxLayout()
        t = QLabel("👤  Persona — your digital double")
        t.setStyleSheet(f"color:{_TEXT}; font-size:20px; font-weight:800;")
        head.addWidget(t)
        head.addStretch(1)
        self._status = QLabel("loading…")
        self._status.setStyleSheet(f"color:{_MUTED}; font-size:11px;")
        head.addWidget(self._status)
        self._gen = QPushButton("✨ Generate summary")
        self._gen.setToolTip("Ask the local LLM to write a prose profile from "
                             "your data (on-device, $0).")
        self._gen.setStyleSheet(
            f"QPushButton {{ background:{_GOLD}; color:#102F3C; padding:7px 14px; "
            f"border-radius:4px; font-weight:700; border:none; }}")
        self._gen.clicked.connect(self._generate)
        head.addWidget(self._gen)
        outer.addLayout(head)

        sub = QLabel("The behavioural foundation an AI can use to reason as you "
                     "— fitness, interests, taste, reading. Read-only lens over "
                     "everything Egon already holds.")
        sub.setStyleSheet(f"color:{_MUTED}; font-size:12px;")
        sub.setWordWrap(True); outer.addWidget(sub)

        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget(); scroll.setWidget(body); outer.addWidget(scroll, 1)
        self._v = QVBoxLayout(body); self._v.setSpacing(14)

        self._prose = QLabel("")
        self._prose.setWordWrap(True)
        self._prose.setStyleSheet(
            f"color:{_GOLD}; background:{_CARD}; border:1px solid {_VIO}; "
            f"border-radius:8px; padding:14px; font-size:14px;")
        self._prose.hide()
        self._v.addWidget(self._prose)

        self._sections = QVBoxLayout(); self._sections.setSpacing(14)
        self._v.addLayout(self._sections)
        self._v.addStretch(1)

        self._ready.connect(self._render)
        self._prose_ready.connect(self._render_prose)
        self._kick()

    def _kick(self) -> None:
        self._status.setText("reading your data…")
        import threading
        def _bg():
            try:
                from lib import persona
                self._ready.emit(persona.build_profile())
            except Exception as e:
                self._ready.emit({"error": str(e)[:200]})
        threading.Thread(target=_bg, daemon=True).start()

    def _clear(self) -> None:
        while self._sections.count():
            it = self._sections.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()
            elif it.layout():
                lay = it.layout()
                while lay.count():
                    c = lay.takeAt(0)
                    if c.widget():
                        c.widget().deleteLater()

    def _heading(self, txt: str) -> None:
        h = QLabel(txt)
        h.setStyleSheet(f"color:{_TEXT}; font-size:15px; font-weight:700; "
                        f"margin-top:4px;")
        self._sections.addWidget(h)

    def _grid(self, cards: list[QFrame], cols: int = 4) -> None:
        g = QGridLayout(); g.setSpacing(12)
        for i, c in enumerate(cards):
            g.addWidget(c, i // cols, i % cols)
            g.setColumnStretch(i % cols, 1)
        self._sections.addLayout(g)

    def _render(self, p: dict) -> None:
        if p.get("error"):
            self._status.setText(f"load failed: {p['error']}")
            return
        self._status.setText("ready")
        self._clear()
        h, i, m, r = p["health"], p["interests"], p["media"], p["reading"]

        # Health
        self._heading("🏃 Health & body")
        if h.get("available"):
            self._grid([
                _stat(f"{h['lifetime_steps']:,}", "lifetime steps", _OK),
                _stat(f"{h['lifetime_km']:,.0f} km", "distance walked", _OK),
                _stat(f"{h['avg_daily_steps']:,}", "avg steps / day", _ACCENT),
                _stat(f"{h['years']} yr", f"tracked since {h['first_day']}", _MUTED),
                _stat(f"{h['recent30_avg_steps']:,}", "last 30d avg / day", _ACCENT),
                _stat(f"{h['best_day']['steps']:,}", f"best day {h['best_day']['date']}", _GOLD),
                _stat(f"{h['lifetime_kcal']:,}", "kcal burned", _GOLD),
                _stat(f"{h['days_tracked']:,}", "days recorded", _MUTED),
            ])
        else:
            self._sections.addWidget(QLabel("No Fit data — import a Google "
                "Takeout (YouTube/Fit) via Artifacts → Import."))

        # Interests
        self._heading("🧭 Interests & intellectual taste")
        self._grid([
            _stat(f"{i.get('follows_count',0):,}", "Discover follows", _VIO),
            _stat(f"{i.get('subs_count',0):,}", "YouTube subscriptions", _VIO),
            _stat(f"{i.get('likes_count',0):,}", "Discover likes", _ACCENT),
            _stat(f"{i.get('not_interested_count',0):,}", "explicitly not-interested", _MUTED),
        ])
        if i.get("follows"):
            self._sections.addWidget(_chips("Follows", i["follows"], _VIO))
        if i.get("youtube_subs"):
            self._sections.addWidget(_chips("YouTube subscriptions", i["youtube_subs"], _ACCENT))

        # Media taste
        self._heading("🎬 Media taste")
        self._grid([
            _stat(f"{m['films_watched']:,}", "films logged", _GOLD),
            _stat(f"{m['music_tracks']:,}", "saved tracks", _ACCENT),
            _stat(f"{m['podcasts']:,}", "podcasts", _ACCENT),
            _stat(f"{m['tv_episodes']:,}", "TV episodes", _MUTED),
        ])
        if m.get("films_top"):
            self._sections.addWidget(_chips("Top-rated films", m["films_top"], _GOLD))

        # Reading
        self._heading("📚 Reading & knowledge")
        self._grid([
            _stat(f"{r['zotero_refs']:,}", "academic references", _OK),
            _stat(f"{r['kindle_items']:,}", "Kindle items", _GOLD),
            _stat(f"{r['instapaper']:,}", "saved articles", _ACCENT),
            _stat(f"{r['bookmarks']:,}", "bookmarks", _MUTED),
        ])

    def _generate(self) -> None:
        self._gen.setEnabled(False)
        self._gen.setText("✨ thinking…")
        import threading
        def _bg():
            try:
                from lib import persona
                self._prose_ready.emit(persona.synthesize_prose())
            except Exception as e:
                self._prose_ready.emit({"status": "error", "summary": str(e)[:200]})
        threading.Thread(target=_bg, daemon=True).start()

    def _render_prose(self, res: dict) -> None:
        self._gen.setEnabled(True)
        self._gen.setText("✨ Generate summary")
        self._prose.setText("🪞  " + (res.get("summary") or ""))
        self._prose.show()
