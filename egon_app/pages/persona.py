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
    QPushButton, QScrollArea, QComboBox, QLineEdit,
)

_TEXT = "#f5f5f7"; _MUTED = "#76767f"; _GOLD = "#ff9f0a"; _ACCENT = "#ff453a"
_CARD = "#16181c"; _BORDER = "#22252a"; _OK = "#30d158"; _VIO = "#9D7BD8"


class _InterestRow(QFrame):
    """One interest: ★ pin button · editable name field · source · ✕ remove."""
    def __init__(self, row: dict, on_pin, on_rename, on_remove):
        super().__init__()
        self._name = row["name"]
        self._on_rename = on_rename
        self.setObjectName("intRow")
        self.setStyleSheet(
            f"#intRow {{ background:transparent; border-bottom:1px solid "
            f"{_BORDER}; }}")
        h = QHBoxLayout(self); h.setContentsMargins(6, 3, 6, 3); h.setSpacing(8)

        pinned = row.get("pinned")
        star = QPushButton("★" if pinned else "☆")
        star.setFixedWidth(30)
        star.setCursor(Qt.PointingHandCursor)
        star.setToolTip("Pin / unpin")
        star.setStyleSheet(
            f"QPushButton {{ border:none; background:transparent; font-size:16px; "
            f"padding:0px; "
            f"color:{_GOLD if pinned else _MUTED}; }}"
            f"QPushButton:hover {{ color:{_GOLD}; }}")
        star.clicked.connect(lambda: on_pin(self._name))
        h.addWidget(star)

        self._field = QLineEdit(row["name"])
        self._field.setObjectName("intField")
        self._field.setFrame(False)
        self._field.setStyleSheet("")
        self._field.editingFinished.connect(self._commit)
        h.addWidget(self._field, 1)

        src = row.get("source", "")
        src_color = {"Discover": _VIO, "YouTube": _ACCENT,
                     "you": _GOLD}.get(src, _MUTED)
        badge = QLabel(src)
        badge.setStyleSheet(
            f"color:{src_color}; font-size:10px; background:{_BORDER}; "
            f"padding:2px 7px; border-radius:9px;")
        h.addWidget(badge)

        x = QPushButton("✕")
        x.setFixedWidth(28)
        x.setCursor(Qt.PointingHandCursor)
        x.setToolTip("Remove")
        x.setStyleSheet(
            f"QPushButton {{ border:none; background:transparent; color:{_MUTED}; "
            f"padding:0px; "
            f"font-size:14px; }} QPushButton:hover {{ color:#ff453a; }}")
        x.clicked.connect(lambda: on_remove(self._name))
        h.addWidget(x)

    def _commit(self):
        new = self._field.text().strip()
        if new and new != self._name:
            self._on_rename(self._name, new)


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
            f"QPushButton {{ background:{_GOLD}; color:#0c0d0f; padding:7px 14px; "
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
            f"color:{_GOLD}; background:{_CARD}; border:none; "
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
        ih = QLabel("🧭 Interests")
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
            "QComboBox { background:#0c0d0f; color:#f5f5f7; border:1px solid "
            "#22252a; border-radius:4px; padding:4px 8px; }")
        bar.addWidget(QLabel("Sort:"))
        bar.addWidget(self._sort)
        self._add = QLineEdit()
        self._add.setPlaceholderText("Type an interest to add...")
        self._add.returnPressed.connect(self._add_interest)
        self._add.setStyleSheet(
            "QLineEdit { background:#0c0d0f; color:#f5f5f7; border:1px solid "
            "#22252a; border-radius:4px; padding:5px 8px; }")
        bar.addWidget(self._add, 1)

        self._add_btn = QPushButton("Add")
        self._add_btn.setCursor(Qt.PointingHandCursor)
        self._add_btn.setStyleSheet(
            "QPushButton { background:#212328; color:#f5f5f7; border:1px solid #22252a; "
            "border-radius:4px; padding:5px 12px; font-weight:600; } "
            "QPushButton:hover { background:#ff453a; color:white; border-color:#ff453a; }")
        self._add_btn.clicked.connect(self._add_interest)
        bar.addWidget(self._add_btn)

        self._count = QLabel("")
        self._count.setStyleSheet(f"color:{_MUTED}; font-size:11px;")
        bar.addWidget(self._count)
        self._v.addLayout(bar)

        # scrollable container of editable rows (each with real ★ / ✕ buttons)
        listwrap = QFrame()
        listwrap.setStyleSheet(
            f"QFrame {{ background:#0c0d0f; border:none; "
            f"border-radius:6px; }}")
        lw = QVBoxLayout(listwrap); lw.setContentsMargins(2, 2, 2, 2)
        inner_scroll = QScrollArea(); inner_scroll.setWidgetResizable(True)
        inner_scroll.setFrameShape(QFrame.NoFrame)
        inner_scroll.setMinimumHeight(340)
        self._rows_host = QWidget()
        self._rows = QVBoxLayout(self._rows_host)
        self._rows.setContentsMargins(0, 0, 0, 0); self._rows.setSpacing(0)
        self._rows.addStretch(1)
        inner_scroll.setWidget(self._rows_host)
        lw.addWidget(inner_scroll)
        self._v.addWidget(listwrap)
        self._v.addStretch(1)

        self._ready.connect(self._render_health)
        self._prose_ready.connect(self._render_prose)
        self._interests_ready.connect(self._render_interests)
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
        # clear existing rows (keep the trailing stretch)
        while self._rows.count() > 1:
            it = self._rows.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        for r in rows:
            self._rows.insertWidget(
                self._rows.count() - 1,
                _InterestRow(r, self._pin, self._rename, self._remove))
        self._count.setText(f"{len(rows)} interests")

    def _render_prose(self, res: dict) -> None:
        self._gen.setEnabled(True); self._gen.setText("✨ Generate summary")
        self._prose.setText("🪞  " + (res.get("summary") or ""))
        self._prose.show()

    # ── edit handlers (real buttons / fields) ────────────────────────────────
    def _add_interest(self) -> None:
        name = self._add.text().strip()
        if not name:
            return
        from lib import persona
        persona.add_interest(name)
        self._add.clear()
        self._reload_interests()

    def _pin(self, name: str) -> None:
        from lib import persona
        persona.toggle_pin(name)
        self._reload_interests()

    def _rename(self, old: str, new: str) -> None:
        from lib import persona
        persona.rename_interest(old, new)
        self._reload_interests()

    def _remove(self, name: str) -> None:
        from lib import persona
        persona.remove_interest(name)
        self._reload_interests()

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
