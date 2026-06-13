"""Persona — the digital-double dashboard.

Bruno 2026-06-12/13: the behavioural foundation for an AI double. Health
stats + a CURATED, editable, sortable interests list. Media and reading
deliberately live in their own tabs (Media, References) — Persona is about
*identity*, not catalogues. A 'Generate summary' button asks the local LLM
to write a prose profile.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QFrame,
    QPushButton, QScrollArea, QComboBox, QLineEdit, QListWidget,
    QListWidgetItem, QAbstractItemView,
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


class PersonaPage(QWidget):
    _ready = Signal(dict)
    _prose_ready = Signal(dict)
    _interests_ready = Signal(list)

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
        self._gen.setStyleSheet(
            f"QPushButton {{ background:{_GOLD}; color:#102F3C; padding:7px 14px; "
            f"border-radius:4px; font-weight:700; border:none; }}")
        self._gen.clicked.connect(self._generate)
        head.addWidget(self._gen)
        outer.addLayout(head)

        sub = QLabel("Who you are, for an AI that reasons as you. Health from "
                     "your data; interests you curate. (Films & reading live "
                     "in the Media and References tabs.)")
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

        # health
        self._health_head = QLabel("🏃 Health & body")
        self._health_head.setStyleSheet(
            f"color:{_TEXT}; font-size:15px; font-weight:700;")
        self._v.addWidget(self._health_head)
        self._health_grid = QGridLayout(); self._health_grid.setSpacing(12)
        self._v.addLayout(self._health_grid)

        # interests — editable + sortable
        ih = QLabel("🧭 Interests  (double-click to rename · ★ pin · ✕ remove)")
        ih.setStyleSheet(f"color:{_TEXT}; font-size:15px; font-weight:700; "
                         f"margin-top:6px;")
        self._v.addWidget(ih)

        bar = QHBoxLayout()
        self._sort = QComboBox()
        self._sort.addItem("A → Z", "az")
        self._sort.addItem("Z → A", "za")
        self._sort.addItem("By source", "source")
        self._sort.addItem("Pinned first", "pinned")
        self._sort.currentIndexChanged.connect(self._reload_interests)
        self._sort.setStyleSheet(
            "QComboBox { background:#102F3C; color:#F0E9D5; border:1px solid "
            "#1F4858; border-radius:4px; padding:4px 8px; }")
        bar.addWidget(QLabel("Sort:"))
        bar.addWidget(self._sort)
        self._add = QLineEdit()
        self._add.setPlaceholderText("add an interest and press Enter…")
        self._add.returnPressed.connect(self._add_interest)
        self._add.setStyleSheet(
            "QLineEdit { background:#102F3C; color:#F0E9D5; border:1px solid "
            "#1F4858; border-radius:4px; padding:5px 8px; }")
        bar.addWidget(self._add, 1)
        self._count = QLabel("")
        self._count.setStyleSheet(f"color:{_MUTED}; font-size:11px;")
        bar.addWidget(self._count)
        self._v.addLayout(bar)

        self._list = QListWidget()
        self._list.setMinimumHeight(320)
        self._list.setEditTriggers(QAbstractItemView.DoubleClicked
                                   | QAbstractItemView.EditKeyPressed)
        self._list.setStyleSheet(
            f"QListWidget {{ background:#102F3C; color:{_TEXT}; border:1px solid "
            f"{_BORDER}; border-radius:6px; }} QListWidget::item {{ padding:5px "
            f"8px; border-bottom:1px solid #16323d; }} "
            f"QListWidget::item:selected {{ background:#1F5366; }}")
        self._list.itemChanged.connect(self._on_item_changed)
        self._list.itemClicked.connect(self._on_item_clicked)
        self._v.addWidget(self._list)
        self._v.addStretch(1)

        self._ready.connect(self._render_health)
        self._prose_ready.connect(self._render_prose)
        self._interests_ready.connect(self._render_interests)
        self._editing = False
        self._kick()

    # ── load ────────────────────────────────────────────────────────────────
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
        self._reload_interests()

    def _reload_interests(self) -> None:
        sort = self._sort.currentData() or "az"
        import threading

        def _bg():
            try:
                from lib import persona
                self._interests_ready.emit(persona.get_interests(sort))
            except Exception:
                self._interests_ready.emit([])
        threading.Thread(target=_bg, daemon=True).start()

    # ── render ──────────────────────────────────────────────────────────────
    def _render_health(self, p: dict) -> None:
        if p.get("error"):
            self._status.setText(f"load failed: {p['error']}")
            return
        self._status.setText("ready")
        while self._health_grid.count():
            it = self._health_grid.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        h = p.get("health", {})
        if not h.get("available"):
            self._health_grid.addWidget(QLabel(
                "No Fit data yet — Artifacts → Import a Google Takeout."), 0, 0)
            return
        cards = [
            (f"{h['lifetime_steps']:,}", "lifetime steps", _OK),
            (f"{h['lifetime_km']:,.0f} km", "distance walked", _OK),
            (f"{h['avg_daily_steps']:,}", "avg steps / day", _ACCENT),
            (f"{h['years']} yr", f"since {h['first_day']}", _MUTED),
            (f"{h['recent30_avg_steps']:,}", "last 30d avg / day", _ACCENT),
            (f"{h['best_day']['steps']:,}", f"best · {h['best_day']['date']}", _GOLD),
            (f"{h['lifetime_kcal']:,}", "kcal burned", _GOLD),
            (f"{h['days_tracked']:,}", "days recorded", _MUTED),
        ]
        for i, (val, lb, ac) in enumerate(cards):
            self._health_grid.addWidget(_stat(val, lb, ac), i // 4, i % 4)
            self._health_grid.setColumnStretch(i % 4, 1)

    def _render_interests(self, rows: list) -> None:
        self._editing = True
        self._list.clear()
        for r in rows:
            star = "★ " if r.get("pinned") else ""
            it = QListWidgetItem(f"{star}{r['name']}")
            it.setData(Qt.UserRole, r["name"])
            it.setData(Qt.UserRole + 1, r.get("source", ""))
            it.setFlags(it.flags() | Qt.ItemIsEditable)
            tag = {"Discover": _VIO, "YouTube": _ACCENT,
                   "you": _GOLD}.get(r.get("source"), _MUTED)
            it.setForeground(Qt.GlobalColor.white)
            it.setToolTip(f"source: {r.get('source','')} — click to pin/unpin, "
                          f"double-click to rename, Del to remove")
            self._list.addItem(it)
        self._count.setText(f"{len(rows)} interests")
        self._editing = False

    def _render_prose(self, res: dict) -> None:
        self._gen.setEnabled(True); self._gen.setText("✨ Generate summary")
        self._prose.setText("🪞  " + (res.get("summary") or ""))
        self._prose.show()

    # ── edit handlers ─────────────────────────────────────────────────────────
    def _add_interest(self) -> None:
        name = self._add.text().strip()
        if not name:
            return
        from lib import persona
        persona.add_interest(name)
        self._add.clear()
        self._reload_interests()

    def _on_item_changed(self, item: QListWidgetItem) -> None:
        if self._editing:
            return
        old = item.data(Qt.UserRole)
        new = item.text().lstrip("★ ").strip()
        if new and new != old:
            from lib import persona
            persona.rename_interest(old, new)
            self._reload_interests()

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        # single click toggles pin only on the star zone — keep it simple:
        # ctrl-click pins, plain click selects (rename = double-click, Del=remove)
        from PySide6.QtWidgets import QApplication
        mods = QApplication.keyboardModifiers()
        if mods & Qt.ControlModifier:
            from lib import persona
            persona.toggle_pin(item.data(Qt.UserRole))
            self._reload_interests()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Delete, Qt.Key_Backspace) and self._list.hasFocus():
            it = self._list.currentItem()
            if it:
                from lib import persona
                persona.remove_interest(it.data(Qt.UserRole))
                self._reload_interests()
                return
        super().keyPressEvent(e)

    # ── prose ─────────────────────────────────────────────────────────────────
    def _generate(self) -> None:
        self._gen.setEnabled(False); self._gen.setText("✨ thinking…")
        import threading

        def _bg():
            try:
                from lib import persona
                self._prose_ready.emit(persona.synthesize_prose())
            except Exception as e:
                self._prose_ready.emit({"summary": str(e)[:200]})
        threading.Thread(target=_bg, daemon=True).start()
