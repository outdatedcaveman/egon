"""Token Ledger page — Pro/Max/API plan-aware native rendering.

Keeps the headline metrics, plan chip, and recent-turns table. Renders a
beautiful custom-drawn 30-day cumulative stacked token area chart.
"""
from __future__ import annotations

import math
from PySide6.QtCore import Qt, QTimer, QPointF, QRectF
from PySide6.QtGui import (
    QPainter, QColor, QPen, QBrush, QPolygonF, QFont, QLinearGradient,
)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QFrame,
    QPushButton, QComboBox, QTableWidget, QTableWidgetItem, QHeaderView,
    QSizePolicy,
)


def _fmt_tokens(n) -> str:
    try:
        n = float(n)
    except Exception:
        return "—"
    if n >= 1_000_000_000: return f"{n/1e9:.2f}B"
    if n >= 1_000_000:     return f"{n/1e6:.2f}M"
    if n >= 1_000:         return f"{n/1e3:.0f}K"
    return f"{int(n):,}"


def _fmt_money(n) -> str:
    try:
        n = float(n)
    except Exception:
        return "—"
    if n >= 1000: return f"${n:,.0f}"
    return f"${n:,.2f}"


def _metric(label: str, value: str, gold: bool = False) -> QFrame:
    f = QFrame()
    f.setObjectName("card")
    v = QVBoxLayout(f)
    v.setContentsMargins(18, 14, 18, 14)
    v.setSpacing(4)
    l1 = QLabel(label.upper())
    l1.setObjectName("metricLabel")
    v.addWidget(l1)
    l2 = QLabel(value)
    l2.setStyleSheet("font-size: 26px; font-weight: 700; color: "
                     + ("#D4A24C" if gold else "#7BC5C7"))
    v.addWidget(l2)
    return f


class StackedAreaChartWidget(QWidget):
    """Custom-drawn stacked area chart for token metrics."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(240)
        self._data = {}

    def setData(self, data: dict) -> None:
        self._data = data
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        rect = self.rect()
        # Draw card container frame
        painter.setPen(QPen(QColor("#1F4858"), 1))
        painter.setBrush(QBrush(QColor("#102F3C")))
        painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 6, 6)

        if not self._data or "cache_reads" not in self._data:
            painter.setPen(QColor("#9CA3AF"))
            painter.setFont(QFont("Segoe UI", 10))
            painter.drawText(rect, Qt.AlignCenter, "No token chart data available.")
            painter.end()
            return

        cr = self._data.get("cache_reads", [])
        cw = self._data.get("cache_writes", [])
        inp = self._data.get("input", [])
        out = self._data.get("output", [])
        labels = self._data.get("labels", [])

        n_points = len(cr)
        if n_points == 0:
            painter.setPen(QColor("#9CA3AF"))
            painter.setFont(QFont("Segoe UI", 10))
            painter.drawText(rect, Qt.AlignCenter, "No cumulative data points yet.")
            painter.end()
            return

        # Stack values per point (cumulative bottom-up)
        stacked_vals = []
        for i in range(n_points):
            v_cr = cr[i]
            v_cw = cw[i]
            v_in = inp[i]
            v_out = out[i]
            stacked_vals.append([
                v_cr,
                v_cr + v_cw,
                v_cr + v_cw + v_in,
                v_cr + v_cw + v_in + v_out
            ])

        max_val = max((sv[3] for sv in stacked_vals), default=1.0)
        if max_val <= 0:
            max_val = 1.0

        # Round max value up to a clean grid tick
        order = 10 ** int(math.log10(max_val) if max_val > 0 else 0)
        if order < 1:
            order = 1
        max_val = math.ceil(max_val / (order / 2)) * (order / 2)

        # Plot margins
        left_m = 65
        right_m = 25
        top_m = 35
        bottom_m = 40

        plot_w = rect.width() - left_m - right_m
        plot_h = rect.height() - top_m - bottom_m

        # Draw grid and Y axis labels
        painter.setPen(QPen(QColor("#1F4858"), 1, Qt.DotLine))
        painter.setFont(QFont("Segoe UI", 9))
        y_ticks = 4
        for i in range(y_ticks + 1):
            y_val = max_val * i / y_ticks
            y_pos = top_m + plot_h - (plot_h * i / y_ticks)
            # grid line
            painter.drawLine(left_m, y_pos, left_m + plot_w, y_pos)
            # label
            painter.setPen(QColor("#9CA3AF"))
            painter.drawText(QRectF(5, y_pos - 8, left_m - 12, 16),
                             Qt.AlignRight | Qt.AlignVCenter, f"{y_val:.1f}M")
            painter.setPen(QPen(QColor("#1F4858"), 1, Qt.DotLine))

        # Draw X axis labels (first, middle, last)
        x_indices = [0, n_points // 2, n_points - 1] if n_points > 2 else list(range(n_points))
        for idx in x_indices:
            x_pos = left_m + (plot_w * idx / (n_points - 1)) if n_points > 1 else left_m
            date_str = labels[idx] if idx < len(labels) else ""
            painter.setPen(QColor("#9CA3AF"))
            painter.drawText(QRectF(x_pos - 45, top_m + plot_h + 8, 90, 20),
                             Qt.AlignCenter, date_str)

        # Stacked layers colors
        colors = [
            QColor("#7FB069"),  # Cache Reads (Green)
            QColor("#60A5A8"),  # Cache Writes (Teal)
            QColor("#94A3B8"),  # Input (Slate)
            QColor("#D4A24C"),  # Output (Gold)
        ]

        y_base = top_m + plot_h

        # Draw stacked layers from top to bottom (overlap paint)
        for layer_idx in range(3, -1, -1):
            poly = QPolygonF()
            poly.append(QPointF(left_m, y_base))  # start at baseline left

            for i in range(n_points):
                x = left_m + (plot_w * i / (n_points - 1)) if n_points > 1 else left_m
                val = stacked_vals[i][layer_idx]
                y = y_base - (plot_h * val / max_val)
                poly.append(QPointF(x, y))

            poly.append(QPointF(left_m + plot_w, y_base))  # end at baseline right

            # Draw polygon
            painter.setPen(Qt.NoPen)
            color = colors[layer_idx]
            color.setAlpha(170)  # opacity blend
            painter.setBrush(QBrush(color))
            painter.drawPolygon(poly)

        # Draw Legend
        legend_labels = ["Output", "Input", "Cache Writes", "Cache Reads"]
        lx = left_m + 15
        ly = 12
        for i, lbl in enumerate(legend_labels):
            col = colors[3 - i]
            # color block
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(col))
            painter.drawRect(lx, ly, 10, 10)
            # label
            painter.setPen(QColor("#F0E9D5"))
            painter.drawText(lx + 15, ly + 9, lbl)
            lx += 115

        painter.end()


class LedgerPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 24, 28, 24)
        outer.setSpacing(14)

        # title + range selector
        top = QHBoxLayout()
        title = QLabel("💰  Token Ledger")
        title.setStyleSheet("font-size: 22px; font-weight: 700; color: #F0E9D5;")
        top.addWidget(title)
        top.addStretch(1)
        top.addWidget(QLabel("Range:"))
        self._range_cb = QComboBox()
        self._range_cb.addItems(["7d", "30d", "90d", "all"])
        self._range_cb.setCurrentText("30d")
        self._range_cb.currentTextChanged.connect(lambda _: self.refresh())
        top.addWidget(self._range_cb)
        outer.addLayout(top)

        sub = QLabel("Live computation from .claude session JSONLs. "
                     "Pro/Max headline = tokens; API headline = $$.")
        sub.setStyleSheet("color: #9CA3AF;")
        outer.addWidget(sub)

        # metrics grid
        self._grid = QGridLayout()
        self._grid.setSpacing(12)
        outer.addLayout(self._grid)

        # Token stacked area chart
        self._chart = StackedAreaChartWidget()
        outer.addWidget(self._chart)

        # recent turns table
        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(["Date", "Session", "Turns", "Tokens", "API equiv $"])
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setStyleSheet(
            "QTableWidget { background: #102F3C; color: #F0E9D5; gridline-color: #1F4858; "
            "border: 1px solid #1F4858; border-radius: 6px; }"
            "QHeaderView::section { background: #16404F; color: #9CA3AF; padding: 6px; "
            "border: none; border-bottom: 1px solid #1F4858; font-weight: 600; }"
        )
        outer.addWidget(self._table, 1)

        self.refresh()
        self._timer = QTimer(self)
        self._timer.setInterval(60_000)  # ledger is slow to compute; refresh sparingly
        self._timer.timeout.connect(self.refresh)
        self._timer.start()

    def refresh(self) -> None:
        try:
            from lib.ledger import compute_ledger, load_config
        except Exception as e:
            self._show_error(f"Ledger module missing: {e}")
            return
        try:
            cfg = load_config() or {}
            plan = cfg.get("plan_mode", "pro")
            L = compute_ledger(plan_mode=plan, range_key=self._range_cb.currentText()) or {}
        except Exception as e:
            self._show_error(f"compute_ledger failed: {e}")
            return

        is_pro = plan in ("pro", "max")
        v = L.get("verification", {}) or {}
        totals = L.get("totals", {}) or L

        # rebuild metrics
        while self._grid.count():
            it = self._grid.takeAt(0)
            w = it.widget()
            if w: w.deleteLater()

        cards = []
        cards.append(("Plan", plan.upper(), True))
        cards.append(("Sessions", str(v.get("sessions_ever", "—")), False))
        cards.append(("Turns", str(v.get("total_turns_ever", "—")), False))
        if is_pro:
            cards.append(("Tokens (range)", _fmt_tokens(totals.get("total_tokens")), False))
            cards.append(("API equiv $", _fmt_money(totals.get("api_equivalent_usd")), True))
            cards.append(("Cache savings", _fmt_money(totals.get("cache_savings_usd")), True))
        else:
            cards.append(("Spend (range)", _fmt_money(totals.get("usd_spent")), True))
            cards.append(("Input tokens", _fmt_tokens(totals.get("input_tokens")), False))
            cards.append(("Output tokens", _fmt_tokens(totals.get("output_tokens")), False))

        for i, (lbl, val, gold) in enumerate(cards):
            self._grid.addWidget(_metric(lbl, val, gold), i // 3, i % 3)

        # Update chart
        self._chart.setData(L.get("stacked_30d", {}))

        # recent turns
        recent = L.get("recent_sessions", []) or L.get("sessions", []) or []
        self._table.setRowCount(0)
        for s in recent[:50]:
            r = self._table.rowCount()
            self._table.insertRow(r)
            for c, val in enumerate([
                str(s.get("date", "—"))[:16],
                str(s.get("session_id", s.get("name", "—")))[:48],
                str(s.get("turns", "—")),
                _fmt_tokens(s.get("tokens", s.get("total_tokens"))),
                _fmt_money(s.get("api_equivalent_usd", s.get("usd"))),
            ]):
                self._table.setItem(r, c, QTableWidgetItem(val))

    def _show_error(self, msg: str) -> None:
        while self._grid.count():
            it = self._grid.takeAt(0)
            w = it.widget()
            if w: w.deleteLater()
        err = QLabel(msg)
        err.setStyleSheet("color: #D67A6A; padding: 10px;")
        err.setWordWrap(True)
        self._grid.addWidget(err, 0, 0, 1, 3)
