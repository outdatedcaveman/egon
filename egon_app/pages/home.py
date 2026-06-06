"""Home page — landing dashboard. Native QWidget composition.

2026-05-22 redesign (Bruno: "ugly, misaligned, overly austere"):
  - Hero header: time-of-day greeting + last-pass summary
  - Stat strip: 4 headline metrics with big accent numbers
  - Source-health GRID (not a cramped vertical list): each source is a
    mini-card with a colour-coded status dot, item count, and detail line,
    laid out in a responsive lattice
  - Quick-actions row
All spacing/alignment is on an 8px rhythm; cards share one geometry.
"""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QFrame, QScrollArea, QPushButton,
)

from egon_app import data

# palette (shared with Media cards)
_BG_CARD = "#0E2630"
_BORDER  = "#1F4858"
_ACCENT  = "#7BC5C7"
_TEXT    = "#F0E9D5"
_MUTED   = "#9CA3AF"
_GOLD    = "#D4A24C"
_OK      = "#7FB069"
_WARN    = "#D4A24C"
_ERR     = "#D67A6A"


def _status_color(status: str) -> str:
    return {"ok": _OK, "alive": _OK, "ready": _OK,
            "warming": _WARN, "stale": _WARN, "unconfigured": _MUTED,
            "timeout": _ERR, "error": _ERR}.get(str(status).lower(), _MUTED)


def _stat_card(label: str, value: str, accent: str = _ACCENT, hint: str = "") -> QFrame:
    card = QFrame()
    card.setObjectName("statCard")
    card.setMinimumHeight(96)
    v = QVBoxLayout(card)
    v.setContentsMargins(18, 14, 18, 14)
    v.setSpacing(2)
    l = QLabel(label.upper())
    l.setObjectName("statCardLabel")
    v.addWidget(l)
    val = QLabel(value)
    val.setObjectName("statCardVal")
    val.setStyleSheet(f"color: {accent};")
    v.addWidget(val)
    if hint:
        h = QLabel(hint)
        h.setObjectName("statCardHint")
        h.setWordWrap(True)
        v.addWidget(h)
    v.addStretch(1)
    return card


def _source_card(name: str, info: dict) -> QFrame:
    status = (info.get("status", "—") if isinstance(info, dict) else "—")
    colour = _status_color(status)
    card = QFrame()
    card.setObjectName("srcCard")
    card.setMinimumHeight(72)
    v = QVBoxLayout(card)
    v.setContentsMargins(14, 10, 14, 10)
    v.setSpacing(3)

    top = QHBoxLayout(); top.setSpacing(8)
    dot = QLabel("●"); dot.setStyleSheet(f"color: {colour}; font-size: 12px;")
    top.addWidget(dot)
    n = QLabel(name)
    n.setObjectName("srcCardName")
    top.addWidget(n)
    top.addStretch(1)
    st = QLabel(str(status))
    st.setStyleSheet(f"color: {colour}; font-size: 11px;")
    top.addWidget(st)
    tw = QWidget(); tw.setLayout(top)
    v.addWidget(tw)

    # detail line — best available metric
    detail = ""
    if isinstance(info, dict):
        for k, fmt in (("total_items", "{:,} items"), ("count", "{:,} items"),
                       ("total_links", "{:,} links"), ("pages_mirrored", "{:,} pages"),
                       ("queue_count", "queue {}"), ("size_mb", "{} MB")):
            if info.get(k) not in (None, ""):
                try:
                    detail = fmt.format(info[k])
                except Exception:
                    detail = f"{info[k]}"
                break
        if not detail and info.get("error"):
            detail = str(info["error"])[:80]
        elif not detail and info.get("note"):
            detail = str(info["note"])[:80]
    d = QLabel(detail)
    d.setObjectName("srcCardDetail")
    d.setWordWrap(True)
    v.addWidget(d)
    return card


class HomePage(QWidget):
    card_reviewed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget()
        scroll.setWidget(body)
        outer.addWidget(scroll)

        self._v = QVBoxLayout(body)
        self._v.setContentsMargins(32, 28, 32, 28)
        self._v.setSpacing(20)

        # hero
        self._greeting = QLabel()
        self._greeting.setStyleSheet(f"color: {_TEXT}; font-size: 26px; font-weight: 700;")
        self._v.addWidget(self._greeting)
        self._subhead = QLabel()
        self._subhead.setStyleSheet(f"color: {_MUTED}; font-size: 13px;")
        self._v.addWidget(self._subhead)

        # stat strip
        self._stats_grid = QGridLayout()
        self._stats_grid.setSpacing(14)
        self._v.addLayout(self._stats_grid)

        # Proactive Insights & Strategies
        self._insights_header = QLabel("Proactive Insights & Strategies")
        self._insights_header.setStyleSheet(f"color: {_TEXT}; font-size: 15px; font-weight: 600; margin-top: 4px;")
        self._v.addWidget(self._insights_header)

        self._insights_card = QFrame()
        self._insights_card.setStyleSheet(f"background-color: {_BG_CARD}; border: 1px solid {_BORDER}; border-radius: 6px;")
        self._insights_layout = QVBoxLayout(self._insights_card)
        self._insights_layout.setContentsMargins(16, 14, 16, 14)
        self._insights_layout.setSpacing(10)
        
        self._insights_list = QVBoxLayout()
        self._insights_list.setSpacing(8)
        self._insights_layout.addLayout(self._insights_list)
        self._v.addWidget(self._insights_card)

        # Bildung Active Recall (Spaced Repetition)
        self._recall_header = QLabel("Bildung Active Recall")
        self._recall_header.setStyleSheet(f"color: {_TEXT}; font-size: 15px; font-weight: 600; margin-top: 4px;")
        self._v.addWidget(self._recall_header)

        self._recall_card = QFrame()
        self._recall_card.setStyleSheet(f"background-color: {_BG_CARD}; border: 1px solid {_BORDER}; border-radius: 6px;")
        recall_layout = QVBoxLayout(self._recall_card)
        recall_layout.setContentsMargins(16, 14, 16, 14)
        recall_layout.setSpacing(12)

        # Question label
        self._q_label = QLabel()
        self._q_label.setWordWrap(True)
        self._q_label.setTextFormat(Qt.RichText)
        self._q_label.setStyleSheet(f"color: {_TEXT}; font-size: 14px; font-weight: 500;")
        recall_layout.addWidget(self._q_label)

        # Answer label (initially hidden)
        self._a_label = QLabel()
        self._a_label.setWordWrap(True)
        self._a_label.setTextFormat(Qt.RichText)
        self._a_label.setStyleSheet(f"color: {_GOLD}; font-size: 14px; border-top: 1px dashed {_BORDER}; padding-top: 8px;")
        self._a_label.hide()
        recall_layout.addWidget(self._a_label)

        # Metadata/Tags label
        self._meta_label = QLabel()
        self._meta_label.setStyleSheet("font-size: 11px;")
        recall_layout.addWidget(self._meta_label)

        # Button row containing Reveal and Rating buttons
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        # Reveal Answer Button
        self._reveal_btn = QPushButton("Reveal Answer")
        self._reveal_btn.setStyleSheet(
            f"background-color: {_BORDER}; color: {_TEXT}; font-weight: 600; "
            f"padding: 6px 16px; border-radius: 4px;"
        )
        self._reveal_btn.clicked.connect(self.reveal_answer)
        btn_row.addWidget(self._reveal_btn)

        # Rating buttons widget (initially hidden)
        self._rating_layout_widget = QWidget()
        rating_layout = QHBoxLayout(self._rating_layout_widget)
        rating_layout.setContentsMargins(0, 0, 0, 0)
        rating_layout.setSpacing(8)

        ratings = [
            ("Forgot", 0, _ERR),
            ("Hard", 2, _WARN),
            ("Good", 4, _OK),
            ("Easy", 5, _ACCENT),
        ]
        for label, val, color in ratings:
            btn = QPushButton(label)
            btn.setStyleSheet(
                f"background-color: {color}; color: #0E2A35; font-weight: 600; "
                f"padding: 6px 14px; border-radius: 4px;"
            )
            btn.clicked.connect(lambda checked=False, r_val=val: self.submit_review(r_val))
            rating_layout.addWidget(btn)
        rating_layout.addStretch(1)
        self._rating_layout_widget.hide()
        btn_row.addWidget(self._rating_layout_widget)

        btn_row.addStretch(1)
        recall_layout.addLayout(btn_row)

        self._v.addWidget(self._recall_card)

        self.card_reviewed.connect(self.on_card_reviewed)
        self.load_next_card()

        # source health header
        sh = QLabel("Source health")
        sh.setStyleSheet(f"color: {_TEXT}; font-size: 15px; font-weight: 600; margin-top: 4px;")
        self._v.addWidget(sh)
        self._sources_grid = QGridLayout()
        self._sources_grid.setSpacing(12)
        self._v.addLayout(self._sources_grid)

        self._v.addStretch(1)

        self._last_signature = None   # skip rebuilds when nothing changed
        self.refresh()
        self._timer = QTimer(self)
        self._timer.setInterval(30_000)   # was 15s; data layer caches at 60s anyway
        self._timer.timeout.connect(self.refresh)
        self._timer.start()

    def reveal_answer(self) -> None:
        self._reveal_btn.hide()
        self._a_label.show()
        self._rating_layout_widget.show()

    def load_next_card(self) -> None:
        res = _api_get("/memory/recall")
        self._current_card = None
        if not res or not res.get("card"):
            self._recall_card.hide()
            self._recall_header.hide()
            return

        self._recall_header.show()
        self._recall_card.show()

        card = res["card"]
        self._current_card = card

        content = card.get("content") or ""
        if ":" in content:
            q_text, a_text = content.split(":", 1)
            q_text = q_text.strip()
            a_text = a_text.strip()
        else:
            q_text = "Recall this concept/fact:"
            a_text = content.strip()

        self._q_label.setText(f"<b>Q:</b> {q_text}")
        self._a_label.setText(f"<b>A:</b> {a_text}")

        tags = card.get("tags") or "no tags"
        interval = card.get("interval_days", 0)
        reps = card.get("repetitions", 0)
        ef = card.get("ease_factor", 2.5)
        self._meta_label.setText(
            f"<span style='color: {_MUTED};'>Tags: {tags} | Interval: {interval}d | Reps: {reps} | EF: {ef:.2f}</span>"
        )

        self._a_label.hide()
        self._rating_layout_widget.hide()
        self._reveal_btn.show()

    def submit_review(self, rating_val: int) -> None:
        if not self._current_card:
            return
        card_id = self._current_card["id"]
        self._rating_layout_widget.setEnabled(False)

        def _bg():
            _api_post(f"/memory/{card_id}/review", {"rating": rating_val})
            self.card_reviewed.emit()

        import threading
        threading.Thread(target=_bg, daemon=True).start()

    def on_card_reviewed(self) -> None:
        self._rating_layout_widget.setEnabled(True)
        self.load_next_card()

    def refresh(self) -> None:
        # Fetch proactive insights
        res = _api_get("/introspection/proposals")
        proposals = (res or {}).get("proposals") or []
        
        while self._insights_list.count():
            item = self._insights_list.takeAt(0)
            w = item.widget()
            if w: w.deleteLater()
            
        if not res:
            empty = QLabel("● mind offline — start Egon's Panop to enable proactive insights")
            empty.setStyleSheet(f"color: {_ERR}; font-size: 12px; font-style: italic;")
            self._insights_list.addWidget(empty)
        elif not proposals:
            empty = QLabel("All systems running efficiently. No anomalies or lock conflicts detected.")
            empty.setStyleSheet(f"color: {_OK}; font-size: 12px; font-style: italic;")
            self._insights_list.addWidget(empty)
        else:
            for p in proposals[:5]:
                p_widget = QFrame()
                p_color = _WARN if p.get("severity") == "warning" else _OK if p.get("severity") == "info" else _ERR
                p_widget.setStyleSheet(
                    f"background-color: #16404F; border: 1px solid {_BORDER}; "
                    f"border-radius: 8px; padding: 10px;"
                )
                ph = QHBoxLayout(p_widget)
                ph.setContentsMargins(10, 8, 10, 8)
                ph.setSpacing(12)
                
                dot = QLabel("●")
                dot.setStyleSheet(f"color: {p_color}; font-size: 16px;")
                ph.addWidget(dot)
                
                pv = QVBoxLayout()
                pv.setSpacing(2)
                
                title = QLabel(f"<b>{p.get('title')}</b>")
                title.setTextFormat(Qt.RichText)
                title.setStyleSheet(f"color: {_TEXT}; font-size: 12px;")
                pv.addWidget(title)
                
                desc = QLabel(p.get("description"))
                desc.setStyleSheet(f"color: {_MUTED}; font-size: 11px;")
                desc.setWordWrap(True)
                pv.addWidget(desc)
                
                ph.addLayout(pv, stretch=1)
                
                proj = p.get("project")
                if proj and proj != "general":
                    badge = QLabel(proj.upper())
                    badge.setStyleSheet(
                        f"background-color: {_BORDER}; color: {_GOLD}; "
                        f"font-size: 9px; padding: 2px 6px; border-radius: 4px; font-weight: 600;"
                    )
                    ph.addWidget(badge)
                    
                self._insights_list.addWidget(p_widget)

        d = data.last_pass()
        sources = d.get("sources", {}) or {}

        # Skip the (expensive) widget rebuild when the underlying data is
        # unchanged — rebuilding 20+ cards every tick was needless churn.
        sig = (d.get("generated_at"), len(sources),
               tuple(sorted((k, str(v.get("status")) if isinstance(v, dict) else "")
                            for k, v in sources.items())))
        if sig == self._last_signature:
            return
        self._last_signature = sig
        generated = d.get("generated_at", "—")
        if isinstance(generated, str) and "T" in generated:
            generated = generated.replace("T", " ")[:16]

        # greeting
        hr = datetime.now().hour
        part = ("Good morning" if hr < 12 else
                "Good afternoon" if hr < 18 else "Good evening")
        self._greeting.setText(f"{part}, Bruno")
        n_ok = sum(1 for v in sources.values()
                   if isinstance(v, dict) and str(v.get("status", "")).lower() in ("ok", "alive", "ready"))
        self._subhead.setText(
            f"Last pass {generated}  ·  {len(sources)} sources, {n_ok} healthy")

        # total items across all sources
        total_items = 0
        for v in sources.values():
            if isinstance(v, dict):
                total_items += int(v.get("total_items") or v.get("count") or 0)

        # stat strip
        while self._stats_grid.count():
            it = self._stats_grid.takeAt(0)
            w = it.widget()
            if w: w.deleteLater()
        ledger = d.get("ledger") or {}
        stats = [
            ("Sources healthy", f"{n_ok}/{len(sources)}", _OK, "adapters reporting ok"),
            ("Items indexed",   f"{total_items:,}" if total_items else "—", _ACCENT, "across all sources"),
            ("Last pass",       str(generated), _TEXT, f"{d.get('duration_seconds','—')}s"),
            ("MTD tokens",      _fmt_tok(ledger.get("mtd_tokens")), _GOLD,
             f"${ledger.get('mtd_cost_usd','—')}" if ledger.get("mtd_cost_usd") else "this month"),
        ]
        for i, (lbl, val, accent, hint) in enumerate(stats):
            self._stats_grid.addWidget(_stat_card(lbl, val, accent, hint), 0, i)
            self._stats_grid.setColumnStretch(i, 1)

        # source health grid (responsive: 3 cols)
        while self._sources_grid.count():
            it = self._sources_grid.takeAt(0)
            w = it.widget()
            if w: w.deleteLater()
        if not sources:
            empty = QLabel("No source data yet — hit ⚡ Run pass now.")
            empty.setStyleSheet(f"color: {_MUTED}; padding: 12px;")
            self._sources_grid.addWidget(empty, 0, 0)
            return
        cols = 3
        for idx, (name, info) in enumerate(sorted(sources.items())):
            r, c = divmod(idx, cols)
            self._sources_grid.addWidget(_source_card(name, info), r, c)
        for c in range(cols):
            self._sources_grid.setColumnStretch(c, 1)


def _fmt_tok(n) -> str:
    try:
        n = float(n)
    except Exception:
        return "—"
    if n >= 1e9: return f"{n/1e9:.1f}B"
    if n >= 1e6: return f"{n/1e6:.1f}M"
    if n >= 1e3: return f"{n/1e3:.0f}K"
    return f"{int(n)}"


def _api_get(path: str, timeout: float = 1.5) -> dict | None:
    import httpx
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.get(f"http://127.0.0.1:8000/api/v1/mind{path}")
        if r.status_code == 200:
            return r.json()
    except Exception:
        return None
    return None


def _api_post(path: str, payload: dict, timeout: float = 1.5) -> dict | None:
    import httpx
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.post(f"http://127.0.0.1:8000/api/v1/mind{path}", json=payload)
        if r.status_code == 200:
            return r.json()
    except Exception:
        return None
    return None
