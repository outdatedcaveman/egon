"""Inbox page — dedicated Panop capture dashboard & history ledger."""
from __future__ import annotations

import json
import webbrowser
import os
import shutil
import re
from datetime import datetime
from typing import Any

from PySide6.QtCore import Qt, QTimer, QThread, Signal, QObject
from PySide6.QtGui import QColor, QBrush
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
    QTableWidget, QTableWidgetItem, QPushButton, QHeaderView, QSizePolicy,
    QMessageBox, QTabWidget, QGridLayout, QLineEdit, QTextEdit, QFileDialog,
    QComboBox, QCheckBox, QMenu
)

from egon_app import data

# ---------------------------------------------------------------------------
# Panop base URL
# ---------------------------------------------------------------------------
_PANOP_BASE = "http://127.0.0.1:8000/api/v1"

# ---------------------------------------------------------------------------
# Custom QSS Stylesheets (Panop dark theme with crimson accents)
# ---------------------------------------------------------------------------
_TAB_QSS = """
QTabWidget::pane {
    border: 1px solid #27272A;
    background: #030303;
    border-radius: 6px;
}
QTabBar::tab {
    background: #09090B;
    color: #A1A1AA;
    padding: 8px 14px;
    border: 1px solid #27272A;
    border-bottom: none;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    margin-right: 4px;
    font-size: 12px;
    font-family: 'Segoe UI', -apple-system, BlinkMacSystemFont, Roboto, sans-serif;
    font-weight: 500;
}
QTabBar::tab:selected {
    background: #030303;
    color: #EF4444;
    font-weight: 700;
    border-top: 2px solid #EF4444;
}
QTabBar::tab:hover {
    background: #18181B;
    color: #F4F4F5;
}
"""

_TABLE_QSS = """
QTableWidget {
    background: #030303;
    color: #E4E4E7;
    gridline-color: #18181B;
    border: 1px solid #27272A;
    border-radius: 6px;
    font-family: 'Segoe UI', -apple-system, BlinkMacSystemFont, Roboto, sans-serif;
    font-size: 13px;
}
QHeaderView::section {
    background: #09090B;
    color: #EF4444;
    padding: 6px;
    border: none;
    border-bottom: 1px solid #27272A;
    font-weight: 600;
    font-size: 12px;
}
QTableWidget::item {
    border-bottom: 1px solid #18181B;
    padding: 4px;
}
QTableWidget::item:selected {
    background: #27272A;
    color: #FFFFFF;
}
QTableWidget::item:hover {
    background: #18181B;
}
QScrollBar:vertical {
    border: none;
    background: #09090B;
    width: 6px;
    margin: 0px 0px 0px 0px;
}
QScrollBar::handle:vertical {
    background: #27272A;
    min-height: 15px;
    border-radius: 3px;
}
QScrollBar::handle:vertical:hover {
    background: #3F3F46;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    border: none;
    background: none;
}
"""

_INPUT_QSS = """
QLineEdit, QTextEdit {
    background: #09090B;
    color: #F4F4F5;
    border: 1px solid #27272A;
    border-radius: 6px;
    padding: 6px 10px;
    font-family: 'Segoe UI', sans-serif;
    font-size: 12px;
}
QLineEdit:focus, QTextEdit:focus {
    border-color: #EF4444;
}
"""

_COMBO_QSS = """
QComboBox {
    background: #09090B;
    color: #F4F4F5;
    border: 1px solid #27272A;
    border-radius: 6px;
    padding: 4px 8px;
    font-family: 'Segoe UI', sans-serif;
    font-size: 12px;
}
QComboBox::drop-down {
    border: none;
}
QComboBox QAbstractItemView {
    background: #09090B;
    color: #F4F4F5;
    selection-background-color: #27272A;
    border: 1px solid #27272A;
}
"""

# Colors for text formatting
_COLOR_MUTED = "#A1A1AA"
_COLOR_TEXT = "#F4F4F5"
_COLOR_RED = "#EF4444"
_COLOR_GREEN = "#10B981"
_COLOR_CYAN = "#06B6D4"
_COLOR_AMBER = "#F59E0B"


# ---------------------------------------------------------------------------
# Emojis Mapper
# ---------------------------------------------------------------------------
def get_emoji(c: str) -> str:
    cat = (c or "").lower()
    if 'article' in cat or 'pdf' in cat: return '📄'
    if 'book' in cat: return '📚'
    if 'shopping' in cat: return '🛒'
    if 'event' in cat: return '📅'
    if 'job' in cat: return '💼'
    if 'github' in cat or 'repo' in cat: return '💻'
    if 'scholar' in cat or 'reference' in cat or 'academic' in cat: return '🎓'
    if 'instapaper' in cat or 'read later' in cat or 'read it later' in cat: return '☕'
    if 'science' in cat or 'press release' in cat or 'news' in cat: return '🔬'
    if 'tool' in cat or 'app' in cat or 'service' in cat: return '🛠️'
    if 'video' in cat or 'watch' in cat: return '🎥'
    if 'podcast' in cat or 'audio' in cat: return '🎧'
    return '🔗'


# ---------------------------------------------------------------------------
# Shared pill widget
# ---------------------------------------------------------------------------
def _pill(label: str, val, ok: bool) -> QLabel:
    color_dot = _COLOR_GREEN if ok else _COLOR_RED
    lbl = QLabel()
    lbl.setTextFormat(Qt.RichText)
    lbl.setStyleSheet(
        "background: #09090B; padding: 4px 8px; border-radius: 10px; "
        "color: #F4F4F5; border: 1px solid #27272A; font-size: 11px;"
    )
    lbl.setText(f"<span style='color:{color_dot};'>●</span>  <b>{val}</b>  {label}")
    return lbl


# ---------------------------------------------------------------------------
# Helper function to create standard tables
# ---------------------------------------------------------------------------
def _make_table(columns: list[tuple[str, int]]) -> QTableWidget:
    tbl = QTableWidget(0, len(columns))
    tbl.setHorizontalHeaderLabels([c[0] for c in columns])
    hdr = tbl.horizontalHeader()
    hdr.setSectionResizeMode(QHeaderView.Interactive)
    hdr.setStretchLastSection(True)
    hdr.setMinimumSectionSize(48)
    for i, (_, w) in enumerate(columns):
        tbl.setColumnWidth(i, w)
    tbl.verticalHeader().setVisible(False)
    tbl.setEditTriggers(QTableWidget.NoEditTriggers)
    tbl.setSelectionBehavior(QTableWidget.SelectRows)
    tbl.setSelectionMode(QTableWidget.ExtendedSelection)
    tbl.setSortingEnabled(True)
    tbl.setStyleSheet(_TABLE_QSS)
    return tbl


# ═══════════════════════════════════════════════════════════════════════════
# Background HTTP workers (QThread)
# ═══════════════════════════════════════════════════════════════════════════
class _HttpWorker(QObject):
    """Runs a single httpx request off the main thread."""
    finished = Signal(dict)  # {"ok": bool, "data": ..., "error": str}

    def __init__(self, method: str, url: str, json_body: dict = None, timeout: float = 3.0):
        super().__init__()
        self._method = method
        self._url = url
        self._json_body = json_body
        self._timeout = timeout

    def run(self) -> None:
        try:
            import httpx
            with httpx.Client(timeout=self._timeout) as client:
                if self._method.upper() == "GET":
                    r = client.get(self._url)
                else:
                    if self._json_body:
                        r = client.post(self._url, json=self._json_body)
                    else:
                        r = client.post(self._url)
                if r.status_code < 400:
                    try:
                        body = r.json()
                    except Exception:
                        body = r.text
                    self.finished.emit({"ok": True, "data": body, "error": ""})
                else:
                    self.finished.emit({"ok": False, "data": None,
                                        "error": f"HTTP {r.status_code}: {r.text[:300]}"})
        except Exception as exc:
            self.finished.emit({"ok": False, "data": None, "error": str(exc)[:300]})


def _spawn_http(parent: QWidget, method: str, url: str,
                callback, json_body: dict = None, timeout: float = 3.0) -> QThread:
    """Fire an HTTP request in a QThread. *callback(result_dict)* is called
    on the main thread when done."""
    thread = QThread(parent)
    worker = _HttpWorker(method, url, json_body, timeout)
    worker.moveToThread(thread)
    
    # Store reference on the thread object to prevent garbage collection
    thread._worker = worker
    
    thread.started.connect(worker.run)
    worker.finished.connect(callback)
    worker.finished.connect(thread.quit)
    worker.finished.connect(worker.deleteLater)
    thread.finished.connect(thread.deleteLater)
    
    if hasattr(parent, "_threads") and isinstance(parent._threads, list):
        parent._threads.append(thread)
        thread.finished.connect(lambda: parent._threads.remove(thread) if thread in parent._threads else None)
        
    thread.start()
    return thread


# ═══════════════════════════════════════════════════════════════════════════
# Tab 0 — Unified Queue
# ═══════════════════════════════════════════════════════════════════════════
class _UnifiedQueueTab(QWidget):
    def __init__(self, parent: InboxPage):
        super().__init__(parent)
        self._parent = parent
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)

        # pill row
        self._pills_row = QHBoxLayout()
        self._pills_row.setSpacing(6)
        layout.addLayout(self._pills_row)

        # table
        self._table = _make_table([
            ("Source", 120), ("Title", 360), ("Age", 80), ("Suggested target", 220), ("Conf.", 60)
        ])
        self._table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout.addWidget(self._table, 1)

        # actions row
        actions = QHBoxLayout()
        actions.setSpacing(8)
        btn1 = QPushButton("Re-classify all")
        btn1.setStyleSheet(
            "QPushButton { background: #EF4444; color: white; padding: 6px 12px; "
            "border-radius: 4px; font-weight: 600; font-size: 12px; border: none; }"
            "QPushButton:hover { background: #B91C1C; }"
        )
        btn1.setFixedHeight(28)
        btn1.setCursor(Qt.PointingHandCursor)
        btn1.clicked.connect(self._reclassify)
        actions.addWidget(btn1)

        btn2 = QPushButton("Apply all confident (≥0.90)")
        btn2.setStyleSheet(
            "QPushButton { background: #18181B; color: #E4E4E7; border: 1px solid #27272A; "
            "padding: 6px 12px; border-radius: 4px; font-weight: 600; font-size: 12px; }"
            "QPushButton:hover { background: #27272A; color: white; }"
        )
        btn2.setFixedHeight(28)
        btn2.setCursor(Qt.PointingHandCursor)
        btn2.clicked.connect(self._apply_confident)
        actions.addWidget(btn2)
        actions.addStretch(1)
        layout.addLayout(actions)

    def refresh(self) -> None:
        d = data.last_pass() or {}
        src = d.get("sources") or {}
        items = d.get("inbox_preview") or []

        # rebuild pills
        while self._pills_row.count():
            it = self._pills_row.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()
        rt = src.get("routster") or {}
        nt = src.get("notion") or {}
        vm = src.get("vault") or {}
        mo = src.get("mouseion") or {}
        for name, val, ok in [
            ("Routster",        rt.get("queue_count", "—"), rt.get("status") == "ok"),
            ("Notion Inbox",    nt.get("queue_count", "—"), nt.get("status") == "ok"),
            ("Vault 001-Inbox", vm.get("inbox_count", "—"), vm.get("status") == "ok"),
            ("Mouseion dupes",  mo.get("duplicates_flagged", "—"), mo.get("status") == "ok"),
        ]:
            self._pills_row.addWidget(_pill(name, val, ok))
        self._pills_row.addStretch(1)

        # rebuild table
        self._table.blockSignals(True)
        self._table.setSortingEnabled(False)
        self._table.setRowCount(0)
        if not items:
            items = self._queue_summary_items(rt, nt, vm, mo)
        for it in items:
            if not isinstance(it, dict):
                continue
            r = self._table.rowCount()
            self._table.insertRow(r)
            
            try:
                conf = float(it.get("confidence", 0.0) or 0.0)
            except (TypeError, ValueError):
                conf = 0.0

            cells = [
                str(it.get("source") or "—"),
                str(it.get("title") or "—"),
                str(it.get("age") or "—"),
                str(it.get("suggested_target") or "—"),
                f"{conf:.2f}",
            ]
            for c, val in enumerate(cells):
                item = QTableWidgetItem(val)
                if c == 4:  # confidence column color
                    if conf >= 0.85:
                        item.setForeground(QBrush(QColor(_COLOR_CYAN)))
                    elif conf < 0.80:
                        item.setForeground(QBrush(QColor(_COLOR_AMBER)))
                self._table.setItem(r, c, item)
        self._table.setSortingEnabled(True)
        self._table.blockSignals(False)

    def _queue_summary_items(self, routster: dict, notion: dict,
                             vault: dict, mouseion: dict) -> list[dict]:
        """Fallback rows when the full agent-authored inbox preview is absent."""
        rows: list[dict] = []
        specs = [
            ("Routster", routster.get("queue_count"), "Review capture queue"),
            ("Notion", notion.get("queue_count"), "Review KMS inbox"),
            ("Vault", vault.get("inbox_count"), "Review 001-Inbox mirror"),
            ("Mouseion", mouseion.get("duplicates_flagged"), "Review duplicate candidates"),
        ]
        for source, count, target in specs:
            if count in (None, "-", "—", "\u2014"):
                continue
            try:
                numeric = int(count)
            except (TypeError, ValueError):
                numeric = 0
            if numeric <= 0:
                continue
            rows.append({
                "source": source,
                "title": f"{numeric:,} item{'s' if numeric != 1 else ''} waiting",
                "age": "live",
                "suggested_target": target,
                "confidence": 0.0,
            })
        return rows

    def _reclassify(self) -> None:
        ok, msg = data.trigger_pass("inbox")
        if not ok:
            QMessageBox.warning(self, "Re-classify", msg)

    def _apply_confident(self) -> None:
        QMessageBox.information(
            self, "Bulk apply",
            "Bulk-apply is queued to the agent — it will action on next pass.")


# ═══════════════════════════════════════════════════════════════════════════
# Tab 1 — Phone Tabs (Queue)
# ═══════════════════════════════════════════════════════════════════════════
class _PhoneTabsTab(QWidget):
    def __init__(self, parent: InboxPage):
        super().__init__(parent)
        self._parent = parent
        self._raw_phone_tabs: list[dict] = []
        self._selected_urls: set[str] = set()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)

        # Ingestion panel
        self._ingest_box = QFrame()
        self._ingest_box.setObjectName("ingestPanel")
        self._ingest_box.setStyleSheet(
            "QFrame#ingestPanel { background: #09090B; border: 1px solid #27272A; "
            "border-left: 4px solid #EF4444; border-radius: 6px; padding: 10px; }"
        )
        ingest_layout = QGridLayout(self._ingest_box)
        ingest_layout.setSpacing(8)

        lbl_man = QLabel("<b>KMS Link / File Ingestion (Panop Pipeline)</b>")
        lbl_man.setStyleSheet("color: #FFFFFF; font-size: 13px; font-family: 'Segoe UI', sans-serif;")
        lbl_man.setTextFormat(Qt.TextFormat.RichText)
        ingest_layout.addWidget(lbl_man, 0, 0, 1, 3)

        self._manual_url_input = QLineEdit()
        self._manual_url_input.setPlaceholderText("Link URL to ingest...")
        self._manual_url_input.setStyleSheet(_INPUT_QSS)
        ingest_layout.addWidget(self._manual_url_input, 1, 0, 1, 2)

        self._manual_title_input = QLineEdit()
        self._manual_title_input.setPlaceholderText("Link Title (optional)...")
        self._manual_title_input.setStyleSheet(_INPUT_QSS)
        ingest_layout.addWidget(self._manual_title_input, 2, 0, 1, 2)

        self._manual_file_input = QLineEdit()
        self._manual_file_input.setPlaceholderText("Select files to copy directly to category folder...")
        self._manual_file_input.setStyleSheet(_INPUT_QSS)
        self._manual_file_input.setReadOnly(True)
        ingest_layout.addWidget(self._manual_file_input, 3, 0, 1, 1)

        btn_browse = QPushButton("📁 Choose File(s)")
        btn_browse.setStyleSheet(
            "QPushButton { background: #18181B; color: #E4E4E7; border: 1px solid #27272A; "
            "border-radius: 4px; font-weight: 600; font-size: 12px; }"
            "QPushButton:hover { background: #27272A; color: white; }"
        )
        btn_browse.setFixedHeight(28)
        btn_browse.setCursor(Qt.PointingHandCursor)
        btn_browse.clicked.connect(self._choose_files)
        ingest_layout.addWidget(btn_browse, 3, 1, 1, 1)

        self._manual_cat_combo = QComboBox()
        self._manual_cat_combo.setStyleSheet(_COMBO_QSS)
        self._manual_cat_combo.setFixedHeight(28)
        ingest_layout.addWidget(self._manual_cat_combo, 1, 2, 1, 1)

        btn_ingest = QPushButton("➕ Ingest to Panop")
        btn_ingest.setStyleSheet(
            "QPushButton { background: #EF4444; color: white; padding: 6px 12px; "
            "border-radius: 4px; font-weight: 600; font-size: 12px; border: none; }"
            "QPushButton:hover { background: #B91C1C; }"
            "QPushButton:pressed { background: #991B1B; }"
        )
        btn_ingest.setFixedHeight(56)
        btn_ingest.setCursor(Qt.PointingHandCursor)
        btn_ingest.clicked.connect(self._manual_ingest)
        ingest_layout.addWidget(btn_ingest, 2, 2, 2, 1)

        layout.addWidget(self._ingest_box)

        # Filters toolbar
        filter_bar = QHBoxLayout()
        filter_bar.setSpacing(6)

        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("🔍  Search open tabs by title or URL...")
        self._search_input.setStyleSheet(_INPUT_QSS)
        self._search_input.textChanged.connect(self._apply_filters)
        filter_bar.addWidget(self._search_input, 2)

        self._category_combo = QComboBox()
        self._category_combo.setStyleSheet(_COMBO_QSS)
        self._category_combo.addItem("All Predictions")
        self._category_combo.currentTextChanged.connect(self._apply_filters)
        filter_bar.addWidget(self._category_combo, 1)

        self._btn_toggle_ingest = QPushButton("➕ Ingest Links")
        self._btn_toggle_ingest.setStyleSheet(
            "QPushButton { background: #18181B; color: #E4E4E7; border: 1px solid #27272A; "
            "border-radius: 6px; padding: 4px 10px; font-weight: 600; font-size: 12px; }"
            "QPushButton:hover { background: #27272A; color: white; }"
        )
        self._btn_toggle_ingest.setFixedHeight(28)
        self._btn_toggle_ingest.setCursor(Qt.PointingHandCursor)
        self._btn_toggle_ingest.clicked.connect(self._toggle_ingest_panel)
        filter_bar.addWidget(self._btn_toggle_ingest)

        layout.addLayout(filter_bar)

        # Selection toolbar
        select_bar = QHBoxLayout()
        select_bar.setSpacing(8)

        self._chk_select_all = QCheckBox("Select All")
        self._chk_select_all.setStyleSheet("QCheckBox { color: #A1A1AA; font-size: 12px; }")
        self._chk_select_all.stateChanged.connect(self._toggle_select_all)
        select_bar.addWidget(self._chk_select_all)

        self._lbl_sel_count = QLabel("0 tabs selected")
        self._lbl_sel_count.setStyleSheet("color: #A1A1AA; font-size: 12px;")
        select_bar.addWidget(self._lbl_sel_count)

        select_bar.addStretch(1)

        btn_save_selected = QPushButton("📥 Save & Close Checked")
        btn_save_selected.setStyleSheet(
            "QPushButton { background: #EF4444; color: white; padding: 4px 10px; "
            "border-radius: 4px; font-weight: 700; font-size: 12px; border: none; }"
            "QPushButton:hover { background: #B91C1C; }"
        )
        btn_save_selected.setFixedHeight(24)
        btn_save_selected.clicked.connect(self._save_selected_tabs)
        select_bar.addWidget(btn_save_selected)

        btn_close_selected = QPushButton("🗑️ Close Checked")
        btn_close_selected.setStyleSheet(
            "QPushButton { background: #18181B; color: #EF4444; padding: 4px 10px; "
            "border-radius: 4px; font-weight: 600; font-size: 12px; border: 1px solid #27272A; }"
            "QPushButton:hover { background: #EF4444; color: white; border-color: #EF4444; }"
        )
        btn_close_selected.setFixedHeight(24)
        btn_close_selected.clicked.connect(self._close_selected_tabs)
        select_bar.addWidget(btn_close_selected)

        layout.addLayout(select_bar)

        # Phone tabs table
        self._phone_table = _make_table([
            ("✓", 30), ("Type", 40), ("Title", 280), ("URL", 280),
            ("Predicted Category", 160), ("Status / Reason", 220), ("Actions", 160)
        ])
        self._phone_table.itemChanged.connect(self._on_table_item_changed)
        self._phone_table.itemDoubleClicked.connect(self._on_table_double_clicked)
        self._phone_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout.addWidget(self._phone_table, 1)

        # Start with ingestion panel hidden
        self._ingest_box.setVisible(False)

    def _toggle_ingest_panel(self) -> None:
        vis = not self._ingest_box.isVisible()
        self._ingest_box.setVisible(vis)
        if vis:
            self._btn_toggle_ingest.setText("➖ Hide Ingest")
            self._btn_toggle_ingest.setStyleSheet(
                "QPushButton { background: #EF4444; color: white; border: none; "
                "border-radius: 6px; padding: 4px 10px; font-weight: 600; font-size: 12px; }"
                "QPushButton:hover { background: #B91C1C; }"
            )
        else:
            self._btn_toggle_ingest.setText("➕ Ingest Links")
            self._btn_toggle_ingest.setStyleSheet(
                "QPushButton { background: #18181B; color: #E4E4E7; border: 1px solid #27272A; "
                "border-radius: 6px; padding: 4px 10px; font-weight: 600; font-size: 12px; }"
                "QPushButton:hover { background: #27272A; color: white; }"
            )

    def populate_categories(self, categories: list[dict]) -> None:
        self._manual_cat_combo.clear()
        for cat in categories:
            self._manual_cat_combo.addItem(f"{get_emoji(cat['name'])} {cat['name']}", cat['id'])
        
        self._category_combo.blockSignals(True)
        current = self._category_combo.currentText()
        self._category_combo.clear()
        self._category_combo.addItem("Only Matched Predictions")
        self._category_combo.addItem("All Predictions")
        for cat in categories:
            self._category_combo.addItem(cat['name'])
        idx = self._category_combo.findText(current)
        if idx >= 0:
            self._category_combo.setCurrentIndex(idx)
        else:
            self._category_combo.setCurrentIndex(0)
        self._category_combo.blockSignals(False)

        # Set helpful tooltip listing predefined categories
        self._category_combo.setToolTip(
            "Predefined categories from panop_config.json:\n" +
            "\n".join(f"• {c['name']} (dest: {c['dest_folder']})" for c in categories)
        )

    def load_tabs(self, tabs: list[dict]) -> None:
        self._raw_phone_tabs = tabs
        self._apply_filters()

    def _apply_filters(self) -> None:
        self._phone_table.blockSignals(True)
        self._phone_table.setSortingEnabled(False)
        self._phone_table.setRowCount(0)

        search = self._search_input.text().strip().lower()
        cat_filter = self._category_combo.currentText()

        categories_list = self._parent._categories_list or []

        filtered = []
        for tab in self._raw_phone_tabs:
            if not isinstance(tab, dict):
                continue
            title = str(tab.get("title") or "").lower()
            url = str(tab.get("url") or "").lower()
            if search and (search not in title and search not in url):
                continue

            status_str = str(tab.get("status") or "")
            pred_cat_name = "Uncategorized"
            if status_str.startswith("match:"):
                pred_cat_name = status_str.split(":", 1)[1]
            elif status_str == "needs_body_check":
                pred_cat_name = "Articles"  # articles is default for body required

            if cat_filter == "Only Matched Predictions":
                if status_str in ("no_match", "chrome_internal", "discarded", "") or pred_cat_name == "Uncategorized":
                    continue
            elif cat_filter != "All Predictions" and pred_cat_name != cat_filter:
                continue

            filtered.append((tab, pred_cat_name))

        for tab, pred_cat_name in filtered:
            r = self._phone_table.rowCount()
            self._phone_table.insertRow(r)

            url = str(tab.get("url") or "")
            title = str(tab.get("title") or "—")
            tid = str(tab.get("id") or "")
            status = str(tab.get("status") or "no_match")
            reason = str(tab.get("reason") or "—")

            # 0. Checkbox
            chk_item = QTableWidgetItem()
            chk_item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            chk_item.setCheckState(Qt.Checked if url in self._selected_urls else Qt.Unchecked)
            self._phone_table.setItem(r, 0, chk_item)

            # 1. Type Emoji
            type_item = QTableWidgetItem(get_emoji(pred_cat_name))
            type_item.setTextAlignment(Qt.AlignCenter)
            self._phone_table.setItem(r, 1, type_item)

            # 2. Title
            title_item = QTableWidgetItem(title)
            self._phone_table.setItem(r, 2, title_item)

            # 3. URL
            url_item = QTableWidgetItem(url)
            self._phone_table.setItem(r, 3, url_item)

            # 4. Predicted Category dropdown
            combo = QComboBox()
            combo.setStyleSheet(_COMBO_QSS)
            combo.blockSignals(True)
            for c in categories_list:
                combo.addItem(f"{get_emoji(c['name'])} {c['name']}", c['id'])
            combo.addItem("Uncategorized", "uncategorized")

            # Find matching category ID
            cur_cat_id = "uncategorized"
            for c in categories_list:
                if c["name"] == pred_cat_name:
                    cur_cat_id = c["id"]
                    break
            
            c_idx = combo.findData(cur_cat_id)
            if c_idx >= 0:
                combo.setCurrentIndex(c_idx)
            else:
                combo.setCurrentIndex(combo.count() - 1)
            combo.blockSignals(False)
            self._phone_table.setCellWidget(r, 4, combo)

            # 5. Status / Reason
            reason_item = QTableWidgetItem(reason)
            if status == "saved":
                reason_item.setForeground(QBrush(QColor(_COLOR_GREEN)))
            elif status.startswith("match:"):
                reason_item.setForeground(QBrush(QColor(_COLOR_CYAN)))
            elif status == "needs_body_check":
                reason_item.setForeground(QBrush(QColor(_COLOR_AMBER)))
            elif status == "chrome_internal":
                reason_item.setForeground(QBrush(QColor(_COLOR_MUTED)))
            self._phone_table.setItem(r, 5, reason_item)

            # 6. Actions row
            actions_widget = QWidget()
            actions_lay = QHBoxLayout(actions_widget)
            actions_lay.setContentsMargins(2, 2, 2, 2)
            actions_lay.setSpacing(4)
            
            save_btn = QPushButton("Save & Close")
            save_btn.setStyleSheet(
                "QPushButton { background: #EF4444; color: white; padding: 2px 6px; "
                "border-radius: 4px; font-size: 10px; font-weight: bold; border: none; }"
                "QPushButton:hover { background: #B91C1C; }"
            )
            save_btn.setFixedHeight(20)
            save_btn.setCursor(Qt.PointingHandCursor)
            save_btn.clicked.connect(lambda _=False, u=url, t=title, cb=combo: self._save_and_close_tab(u, t, cb.currentData()))
            actions_lay.addWidget(save_btn)
            
            close_btn = QPushButton("Close")
            close_btn.setStyleSheet(
                "QPushButton { background: #18181B; color: #E4E4E7; border: 1px solid #27272A; "
                "padding: 2px 6px; border-radius: 4px; font-size: 10px; }"
                "QPushButton:hover { background: #27272A; }"
            )
            close_btn.setFixedHeight(20)
            close_btn.setCursor(Qt.PointingHandCursor)
            close_btn.clicked.connect(lambda _=False, u=url: self._close_tab_without_saving(u))
            actions_lay.addWidget(close_btn)
            
            self._phone_table.setCellWidget(r, 6, actions_widget)

        self._phone_table.setSortingEnabled(True)
        self._phone_table.blockSignals(False)
        self._update_selected_label()

    def _on_table_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() == 0:
            url_item = self._phone_table.item(item.row(), 3)
            if url_item:
                url = url_item.text()
                if item.checkState() == Qt.Checked:
                    self._selected_urls.add(url)
                else:
                    self._selected_urls.discard(url)
                self._update_selected_label()

    def _toggle_select_all(self, state: int) -> None:
        self._phone_table.blockSignals(True)
        self._phone_table.setSortingEnabled(False)
        checked = (state == Qt.Checked.value or state == Qt.Checked)
        
        for r in range(self._phone_table.rowCount()):
            chk_item = self._phone_table.item(r, 0)
            url_item = self._phone_table.item(r, 3)
            if chk_item and url_item:
                url = url_item.text()
                chk_item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
                if checked:
                    self._selected_urls.add(url)
                else:
                    self._selected_urls.discard(url)
                    
        self._phone_table.setSortingEnabled(True)
        self._phone_table.blockSignals(False)
        self._update_selected_label()

    def _update_selected_label(self) -> None:
        count = len(self._selected_urls)
        self._lbl_sel_count.setText(f"{count} tab{'s' if count != 1 else ''} selected")
        self._chk_select_all.blockSignals(True)
        if count == 0:
            self._chk_select_all.setCheckState(Qt.Unchecked)
        elif count == self._phone_table.rowCount():
            self._chk_select_all.setCheckState(Qt.Checked)
        else:
            self._chk_select_all.setCheckState(Qt.PartiallyChecked)
        self._chk_select_all.blockSignals(False)

    def _on_table_double_clicked(self, item: QTableWidgetItem) -> None:
        url_item = self._phone_table.item(item.row(), 3)
        if url_item:
            webbrowser.open(url_item.text())

    def _choose_files(self) -> None:
        file_paths, _ = QFileDialog.getOpenFileNames(
            self, "Select Files to Upload via Panop Pipeline", "", "All Files (*.*)"
        )
        if file_paths:
            self._manual_file_input.setText("; ".join(file_paths))

    def _manual_ingest(self) -> None:
        url = self._manual_url_input.text().strip()
        title = self._manual_title_input.text().strip()
        files = self._manual_file_input.text().strip()
        cat_id = self._manual_cat_combo.currentData()
        
        if not url and not files:
            QMessageBox.warning(self, "Manual Ingestion", "Please enter a URL or select a file to ingest.")
            return
            
        if url:
            self._parent._status_lbl.setText("⏳ Ingesting URL to Panop...")
            api_url = f"{_PANOP_BASE}/history/add"
            payload = {"url": url, "title": title, "category_id": cat_id}
            
            def callback(res):
                if res.get("ok"):
                    data = res.get("data", {})
                    if data.get("status") == "ok":
                        self._parent._status_lbl.setText(f"✅ Ingested URL successfully under category '{data.get('category')}'. Syncing in background...")
                        self._manual_url_input.clear()
                        self._manual_title_input.clear()
                        self._parent.refresh()
                    else:
                        self._parent._status_lbl.setText(f"❌ Ingestion failed: {data.get('message', 'unknown error')}")
                else:
                    self._parent._status_lbl.setText(f"❌ Ingestion HTTP error: {res.get('error')}")
            
            t = _spawn_http(self, "POST", api_url, callback, json_body=payload)
            self._parent._threads.append(t)
            t.finished.connect(lambda: self._parent._threads.remove(t) if t in self._parent._threads else None)
            
        if files:
            file_paths = [f.strip() for f in files.split("; ") if f.strip()]
            dest_folder = None
            for c in self._parent._categories_list:
                if c["id"] == cat_id:
                    dest_folder = c["dest_folder"]
                    break
            if not dest_folder:
                dest_folder = "Uncategorized"
                
            self._parent._status_lbl.setText("⏳ Copying files to category folder...")
            
            import threading
            def _copy_worker():
                try:
                    from pathlib import Path
                    root = Path(__file__).resolve().parents[2]
                    target_dir = root / "state" / "panop" / dest_folder
                    target_dir.mkdir(parents=True, exist_ok=True)
                    copied = []
                    for fp in file_paths:
                        if os.path.exists(fp):
                            dest = target_dir / Path(fp).name
                            shutil.copy2(fp, dest)
                            copied.append(dest.name)
                    
                    def success():
                        self._parent._status_lbl.setText(f"✅ Copied {len(copied)} file(s) directly to category folder: state/panop/{dest_folder}")
                        self._manual_file_input.clear()
                        QMessageBox.information(
                            self, "File Upload",
                            f"Successfully uploaded {len(copied)} file(s) to folder:\n"
                            f"state/panop/{dest_folder}\n\n" + "\n".join(copied)
                        )
                        self._parent.refresh()
                    QTimer.singleShot(0, success)
                except Exception as e:
                    QTimer.singleShot(0, lambda: self._parent._status_lbl.setText(f"❌ File copy failed: {e}"))
                    
            threading.Thread(target=_copy_worker, daemon=True).start()

    def _save_and_close_tab(self, url: str, title: str, category_id: str) -> None:
        tid = ""
        for tab in self._raw_phone_tabs:
            if tab.get("url") == url:
                tid = tab.get("id", "")
                break
        
        self._parent._status_lbl.setText(f"⏳ Saving '{title}' to Zotero/Bookmarks...")
        
        import threading
        import httpx
        def _worker():
            try:
                r = httpx.post(f"{_PANOP_BASE}/history/add", json={
                    "url": url, "title": title, "category_id": category_id
                }, timeout=15.0)
                if r.status_code == 200 and r.json().get("status") == "ok":
                    if tid:
                        try: httpx.post(f"http://127.0.0.1:9222/json/close/{tid}", timeout=5.0)
                        except Exception: pass
                    def success():
                        self._parent._status_lbl.setText(f"✅ Saved and closed tab successfully: {title}")
                        self._parent.refresh()
                    QTimer.singleShot(0, success)
                else:
                    msg = r.json().get("message", "unknown error")
                    QTimer.singleShot(0, lambda: self._parent._status_lbl.setText(f"❌ Save failed: {msg}"))
            except Exception as e:
                QTimer.singleShot(0, lambda: self._parent._status_lbl.setText(f"❌ Save failed: {e}"))
                
        threading.Thread(target=_worker, daemon=True).start()

    def _close_tab_without_saving(self, url: str) -> None:
        tid = ""
        title = url
        for tab in self._raw_phone_tabs:
            if tab.get("url") == url:
                tid = tab.get("id", "")
                title = tab.get("title", url)
                break
        if not tid:
            return
            
        self._parent._status_lbl.setText(f"⏳ Closing tab: {title}...")
        
        import threading
        import httpx
        def _worker():
            try:
                r = httpx.post(f"http://127.0.0.1:9222/json/close/{tid}", timeout=5.0)
                if r.status_code == 200:
                    def success():
                        self._parent._status_lbl.setText(f"✅ Closed tab on phone: {title}")
                        self._parent.refresh()
                    QTimer.singleShot(0, success)
                else:
                    QTimer.singleShot(0, lambda: self._parent._status_lbl.setText(f"❌ Close failed: HTTP {r.status_code}"))
            except Exception as e:
                QTimer.singleShot(0, lambda: self._parent._status_lbl.setText(f"❌ Close failed: {e}"))
                
        threading.Thread(target=_worker, daemon=True).start()

    def _save_selected_tabs(self) -> None:
        if not self._selected_urls:
            QMessageBox.warning(self, "Save Tabs", "No tabs selected.")
            return
            
        # Collect info from table
        targets = []
        for r in range(self._phone_table.rowCount()):
            url_item = self._phone_table.item(r, 3)
            title_item = self._phone_table.item(r, 2)
            chk_item = self._phone_table.item(r, 0)
            combo = self._phone_table.cellWidget(r, 4)
            
            if url_item and chk_item and chk_item.checkState() == Qt.Checked:
                url = url_item.text()
                title = title_item.text() if title_item else url
                category_id = combo.currentData() if combo else ""
                
                tid = ""
                for tab in self._raw_phone_tabs:
                    if tab.get("url") == url:
                        tid = tab.get("id", "")
                        break
                targets.append({"url": url, "title": title, "category_id": category_id, "tid": tid})
                
        if not targets:
            return
            
        self._parent._status_lbl.setText(f"⏳ Saving {len(targets)} selected tab(s) to Zotero/Bookmarks...")
        
        import threading
        import httpx
        
        def _save_worker():
            success_count = 0
            fail_count = 0
            for item in targets:
                try:
                    r = httpx.post(f"{_PANOP_BASE}/history/add", json={
                        "url": item["url"], "title": item["title"], "category_id": item["category_id"]
                    }, timeout=15.0)
                    
                    if r.status_code == 200 and r.json().get("status") == "ok":
                        success_count += 1
                        if item["tid"]:
                            try:
                                httpx.post(f"http://127.0.0.1:9222/json/close/{item['tid']}", timeout=5.0)
                            except Exception:
                                pass
                    else:
                        fail_count += 1
                except Exception:
                    fail_count += 1
            
            def done():
                self._parent._status_lbl.setText(f"✅ Saved {success_count} tab(s) successfully. Failed: {fail_count}.")
                self._selected_urls.clear()
                self._parent.refresh()
            QTimer.singleShot(0, done)
            
        threading.Thread(target=_save_worker, daemon=True).start()

    def _close_selected_tabs(self) -> None:
        if not self._selected_urls:
            QMessageBox.warning(self, "Close Tabs", "No tabs selected.")
            return
            
        confirm = QMessageBox.question(
            self, "Close Tabs",
            f"Are you sure you want to close the {len(self._selected_urls)} selected tab(s) on your phone without saving them?",
            QMessageBox.Yes | QMessageBox.No
        )
        if confirm != QMessageBox.Yes:
            return
            
        tids = []
        for url in self._selected_urls:
            for tab in self._raw_phone_tabs:
                if tab.get("url") == url and tab.get("id"):
                    tids.append(tab.get("id"))
                    
        self._parent._status_lbl.setText(f"⏳ Closing {len(tids)} tab(s) on phone...")
        
        import threading
        import httpx
        def _close_worker():
            closed = 0
            for tid in tids:
                try:
                    r = httpx.post(f"http://127.0.0.1:9222/json/close/{tid}", timeout=5.0)
                    if r.status_code == 200:
                        closed += 1
                except Exception:
                    pass
            def done():
                self._parent._status_lbl.setText(f"✅ Closed {closed} tab(s) on phone.")
                self._selected_urls.clear()
                self._parent.refresh()
            QTimer.singleShot(0, done)
            
        threading.Thread(target=_close_worker, daemon=True).start()


# ═══════════════════════════════════════════════════════════════════════════
# Tab 2 — Saved History
# ═══════════════════════════════════════════════════════════════════════════
class _SavedHistoryTab(QWidget):
    def __init__(self, parent: InboxPage):
        super().__init__(parent)
        self._parent = parent
        self._history_items: list[dict] = []
        self._selected_urls: set[str] = set()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)

        # Filters toolbar
        filter_bar = QHBoxLayout()
        filter_bar.setSpacing(6)

        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("🔍  Search history by title or URL...")
        self._search_input.setStyleSheet(_INPUT_QSS)
        self._search_input.textChanged.connect(self._apply_filters)
        filter_bar.addWidget(self._search_input, 2)

        self._category_combo = QComboBox()
        self._category_combo.setStyleSheet(_COMBO_QSS)
        self._category_combo.addItem("All Categories")
        self._category_combo.currentTextChanged.connect(self._apply_filters)
        filter_bar.addWidget(self._category_combo, 1)

        self._status_combo = QComboBox()
        self._status_combo.setStyleSheet(_COMBO_QSS)
        self._status_combo.addItems(["All Statuses", "Synced", "Pending", "Zotero Only", "Bookmark Only"])
        self._status_combo.currentTextChanged.connect(self._apply_filters)
        filter_bar.addWidget(self._status_combo, 1)

        layout.addLayout(filter_bar)

        # Selection toolbar
        select_bar = QHBoxLayout()
        select_bar.setSpacing(8)

        self._chk_select_all = QCheckBox("Select All")
        self._chk_select_all.setStyleSheet("QCheckBox { color: #A1A1AA; font-size: 12px; }")
        self._chk_select_all.stateChanged.connect(self._toggle_select_all)
        select_bar.addWidget(self._chk_select_all)

        self._lbl_sel_count = QLabel("0 items selected")
        self._lbl_sel_count.setStyleSheet("color: #A1A1AA; font-size: 12px;")
        select_bar.addWidget(self._lbl_sel_count)

        select_bar.addStretch(1)

        btn_sync_selected = QPushButton("🔄 Sync Selected")
        btn_sync_selected.setStyleSheet(
            "QPushButton { background: #EF4444; color: white; padding: 4px 10px; "
            "border-radius: 4px; font-weight: 700; font-size: 12px; border: none; }"
            "QPushButton:hover { background: #B91C1C; }"
        )
        btn_sync_selected.setFixedHeight(24)
        btn_sync_selected.clicked.connect(self._sync_selected_history)
        select_bar.addWidget(btn_sync_selected)

        btn_delete_selected = QPushButton("🗑️ Delete Selected")
        btn_delete_selected.setStyleSheet(
            "QPushButton { background: #18181B; color: #EF4444; padding: 4px 10px; "
            "border-radius: 4px; font-weight: 600; font-size: 12px; border: 1px solid #27272A; }"
            "QPushButton:hover { background: #EF4444; color: white; border-color: #EF4444; }"
        )
        btn_delete_selected.setFixedHeight(24)
        btn_delete_selected.clicked.connect(self._delete_selected_history)
        select_bar.addWidget(btn_delete_selected)

        layout.addLayout(select_bar)

        # History table
        self._history_table = _make_table([
            ("✓", 30), ("Time", 140), ("Title", 340), ("Category", 140), ("Status", 120)
        ])
        self._history_table.itemChanged.connect(self._on_table_item_changed)
        self._history_table.itemDoubleClicked.connect(self._on_table_double_clicked)
        self._history_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout.addWidget(self._history_table, 1)

    def load_history(self, items: list[dict]) -> None:
        self._history_items = items
        
        # Populate category filter ComboBox
        cats_set = set()
        for entry in items:
            if isinstance(entry, dict):
                c = entry.get("category")
                if c:
                    cats_set.add(str(c))
        cats = sorted(list(cats_set))
        self._category_combo.blockSignals(True)
        current = self._category_combo.currentText()
        self._category_combo.clear()
        self._category_combo.addItem("All Categories")
        for c in cats:
            self._category_combo.addItem(c)
        idx = self._category_combo.findText(current)
        if idx >= 0:
            self._category_combo.setCurrentIndex(idx)
        else:
            self._category_combo.setCurrentIndex(0)
        self._category_combo.blockSignals(False)

        self._apply_filters()

    def _apply_filters(self) -> None:
        self._history_table.blockSignals(True)
        self._history_table.setSortingEnabled(False)
        self._history_table.setRowCount(0)

        search = self._search_input.text().strip().lower()
        cat = self._category_combo.currentText()
        status_filter = self._status_combo.currentText()

        filtered = []
        for entry in self._history_items:
            if not isinstance(entry, dict):
                continue
            title = str(entry.get("title") or "").lower()
            url = str(entry.get("url") or "").lower()
            if search and (search not in title and search not in url):
                continue

            entry_cat = str(entry.get("category") or "")
            if cat != "All Categories" and entry_cat != cat:
                continue

            z = bool(entry.get("z_synced"))
            b = bool(entry.get("b_synced"))
            if z and b:
                status = "Synced"
            elif z:
                status = "Zotero Only"
            elif b:
                status = "Bookmark Only"
            else:
                status = "Pending"

            if status_filter != "All Statuses" and status != status_filter:
                continue

            filtered.append((entry, status))

        for entry, status in filtered:
            r = self._history_table.rowCount()
            self._history_table.insertRow(r)

            url = str(entry.get("url") or "")
            title = str(entry.get("title") or "—")
            date_str = str(entry.get("date") or "—")

            # 0. Checkbox
            chk_item = QTableWidgetItem()
            chk_item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            chk_item.setCheckState(Qt.Checked if url in self._selected_urls else Qt.Unchecked)
            self._history_table.setItem(r, 0, chk_item)

            # 1. Date Time
            self._history_table.setItem(r, 1, QTableWidgetItem(date_str))

            # 2. Title
            title_item = QTableWidgetItem(title)
            title_item.setData(Qt.ItemDataRole.UserRole, url)
            self._history_table.setItem(r, 2, title_item)

            # 3. Category
            self._history_table.setItem(r, 3, QTableWidgetItem(entry.get("category", "—")))

            # 4. Status
            status_item = QTableWidgetItem(status)
            if status == "Synced":
                status_item.setForeground(QBrush(QColor(_COLOR_GREEN)))
            elif status in ("Zotero Only", "Bookmark Only"):
                status_item.setForeground(QBrush(QColor(_COLOR_CYAN)))
            else:
                status_item.setForeground(QBrush(QColor(_COLOR_AMBER)))
            self._history_table.setItem(r, 4, status_item)

        self._history_table.setSortingEnabled(True)
        self._history_table.blockSignals(False)
        self._update_selected_label()

    def _on_table_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() == 0:
            title_item = self._history_table.item(item.row(), 2)
            if title_item:
                url = title_item.data(Qt.ItemDataRole.UserRole)
                if url:
                    if item.checkState() == Qt.Checked:
                        self._selected_urls.add(url)
                    else:
                        self._selected_urls.discard(url)
                    self._update_selected_label()

    def _toggle_select_all(self, state: int) -> None:
        self._history_table.blockSignals(True)
        self._history_table.setSortingEnabled(False)
        checked = (state == Qt.Checked.value or state == Qt.Checked)
        
        for r in range(self._history_table.rowCount()):
            chk_item = self._history_table.item(r, 0)
            title_item = self._history_table.item(r, 2)
            if chk_item and title_item:
                url = title_item.data(Qt.ItemDataRole.UserRole)
                if url:
                    chk_item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
                    if checked:
                        self._selected_urls.add(url)
                    else:
                        self._selected_urls.discard(url)
                    
        self._history_table.setSortingEnabled(True)
        self._history_table.blockSignals(False)
        self._update_selected_label()

    def _update_selected_label(self) -> None:
        count = len(self._selected_urls)
        self._lbl_sel_count.setText(f"{count} item{'s' if count != 1 else ''} selected")
        self._chk_select_all.blockSignals(True)
        if count == 0:
            self._chk_select_all.setCheckState(Qt.Unchecked)
        elif count == self._history_table.rowCount():
            self._chk_select_all.setCheckState(Qt.Checked)
        else:
            self._chk_select_all.setCheckState(Qt.PartiallyChecked)
        self._chk_select_all.blockSignals(False)

    def _on_table_double_clicked(self, item: QTableWidgetItem) -> None:
        title_item = self._history_table.item(item.row(), 2)
        if title_item:
            url = title_item.data(Qt.ItemDataRole.UserRole)
            if url:
                webbrowser.open(url)

    def _sync_selected_history(self) -> None:
        if not self._selected_urls:
            QMessageBox.warning(self, "Sync History", "No history items selected.")
            return
            
        self._parent._status_lbl.setText(f"⏳ Syncing {len(self._selected_urls)} history item(s)...")
        
        import threading
        import httpx
        
        targets = []
        for url in self._selected_urls:
            item = None
            for entry in self._history_items:
                if entry.get("url") == url:
                    item = entry
                    break
            if item:
                targets.append((url, item))
                
        def _worker():
            succeeded = 0
            failed = 0
            for url, item in targets:
                z_need = not item.get("z_synced")
                b_need = not item.get("b_synced")
                
                try:
                    if z_need:
                        r = httpx.post(f"{_PANOP_BASE}/history/sync_single?url={url}&type=zotero", timeout=15.0)
                        if r.status_code == 200 and r.json().get("status") == "ok":
                            succeeded += 1
                        else:
                            failed += 1
                    if b_need:
                        r = httpx.post(f"{_PANOP_BASE}/history/sync_single?url={url}&type=bookmark", timeout=15.0)
                        if r.status_code == 200 and r.json().get("status") == "ok":
                            succeeded += 1
                        else:
                            failed += 1
                except Exception:
                    failed += 1
            def done():
                self._parent._status_lbl.setText(f"✅ Sync done. Succeeded: {succeeded} sync ops. Failed: {failed}.")
                self._selected_urls.clear()
                self._parent.refresh()
            QTimer.singleShot(0, done)
            
        threading.Thread(target=_worker, daemon=True).start()

    def _delete_selected_history(self) -> None:
        if not self._selected_urls:
            QMessageBox.warning(self, "Delete History", "No history items selected.")
            return
            
        confirm = QMessageBox.question(
            self, "Delete History",
            f"Are you sure you want to delete the {len(self._selected_urls)} selected history item(s)?\n"
            "This will also delete any downloaded files associated with them.",
            QMessageBox.Yes | QMessageBox.No
        )
        if confirm != QMessageBox.Yes:
            return
            
        self._parent._status_lbl.setText("⏳ Deleting history items...")
        
        import threading
        import httpx
        urls = list(self._selected_urls)
        def _worker():
            try:
                r = httpx.post(f"{_PANOP_BASE}/history/delete", json={"urls": urls}, timeout=15.0)
                if r.status_code == 200 and r.json().get("status") == "ok":
                    def success():
                        self._parent._status_lbl.setText(f"✅ Deleted {len(urls)} history item(s).")
                        self._selected_urls.clear()
                        self._parent.refresh()
                    QTimer.singleShot(0, success)
                else:
                    QTimer.singleShot(0, lambda: self._parent._status_lbl.setText(f"❌ Delete failed: {r.text}"))
            except Exception as e:
                QTimer.singleShot(0, lambda: self._parent._status_lbl.setText(f"❌ Delete failed: {e}"))
                
        threading.Thread(target=_worker, daemon=True).start()


# ═══════════════════════════════════════════════════════════════════════════
# Main page — QTabWidget wrapper
# ═══════════════════════════════════════════════════════════════════════════
class InboxPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background-color: #030303;")
        self._threads: list[QThread] = []
        self._categories_list: list[dict] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 8, 12, 8)
        outer.setSpacing(6)

        # Header: Title + Status pills row inline
        header_layout = QHBoxLayout()
        header_layout.setSpacing(12)

        title = QLabel("Inbox / Panop")
        title.setStyleSheet("font-size: 18px; font-weight: 700; color: #FFFFFF; font-family: 'Segoe UI', sans-serif;")
        title.setToolTip(
            "Dedicated dashboard for Panop phone-tab-drain capture subsystem.\n"
            "Scrape links from your phone's Android Chrome, auto-categorize, sync to Zotero & Bookmarks, and close tabs."
        )
        header_layout.addWidget(title)

        # --- status pills ---
        self._pills_row = QHBoxLayout()
        self._pills_row.setSpacing(6)
        header_layout.addLayout(self._pills_row)
        header_layout.addStretch(1)
        outer.addLayout(header_layout)

        # --- status feedback label (now inline in the button bar, initialized here) ---
        self._status_lbl = QLabel("ready")
        self._status_lbl.setStyleSheet("color: #A1A1AA; font-size: 11px; font-family: 'Segoe UI', sans-serif;")
        self._status_lbl.setTextFormat(Qt.TextFormat.RichText)

        # --- action toolbar (compact, single horizontal row) ---
        btn_frame = QFrame()
        btn_frame.setStyleSheet(
            "QFrame { background: #09090B; border: 1px solid #27272A; "
            "border-radius: 6px; padding: 4px; }"
        )
        btn_frame.setMaximumHeight(42)
        
        btn_layout = QHBoxLayout(btn_frame)
        btn_layout.setContentsMargins(6, 4, 6, 4)
        btn_layout.setSpacing(6)

        # 1. Fetch Now
        btn_fetch = QPushButton("⚡ Fetch Now")
        btn_fetch.setCursor(Qt.PointingHandCursor)
        btn_fetch.setStyleSheet(
            "QPushButton { background: #EF4444; color: white; padding: 4px 12px; "
            "border-radius: 4px; font-weight: 700; font-size: 12px; border: none; }"
            "QPushButton:hover { background: #B91C1C; }"
        )
        btn_fetch.setFixedHeight(26)
        btn_fetch.clicked.connect(self._fetch_now_action)
        btn_layout.addWidget(btn_fetch)

        # 2. Drain All Tabs
        btn_drain = QPushButton("📥 Drain All Tabs")
        btn_drain.setCursor(Qt.PointingHandCursor)
        btn_drain.setStyleSheet(
            "QPushButton { background: #EF4444; color: white; padding: 4px 12px; "
            "border-radius: 4px; font-weight: 700; font-size: 12px; border: none; }"
            "QPushButton:hover { background: #B91C1C; }"
        )
        btn_drain.setFixedHeight(26)
        btn_drain.clicked.connect(self._make_action_handler("Drain All Tabs", "POST", "/tabs/drain"))
        btn_layout.addWidget(btn_drain)

        # 3. Cancel Drain
        btn_cancel_drain = QPushButton("🛑 Cancel Drain")
        btn_cancel_drain.setCursor(Qt.PointingHandCursor)
        btn_cancel_drain.setStyleSheet(
            "QPushButton { background: #18181B; color: #E4E4E7; border: 1px solid #27272A; "
            "padding: 4px 12px; border-radius: 4px; font-weight: 600; font-size: 12px; }"
            "QPushButton:hover { background: #27272A; color: white; }"
        )
        btn_cancel_drain.setFixedHeight(26)
        btn_cancel_drain.clicked.connect(self._make_action_handler("Cancel Drain", "POST", "/tabs/drain/cancel"))
        btn_layout.addWidget(btn_cancel_drain)

        # 4. Merge Duplicates
        btn_merge = QPushButton("♻️ Merge Duplicates")
        btn_merge.setCursor(Qt.PointingHandCursor)
        btn_merge.setStyleSheet(
            "QPushButton { background: #18181B; color: #E4E4E7; border: 1px solid #27272A; "
            "padding: 4px 12px; border-radius: 4px; font-weight: 600; font-size: 12px; }"
            "QPushButton:hover { background: #27272A; color: white; }"
        )
        btn_merge.setFixedHeight(26)
        btn_merge.clicked.connect(self._make_action_handler("Merge Duplicates", "POST", "/history/merge"))
        btn_layout.addWidget(btn_merge)

        # 5. Sync Operations Dropdown
        btn_sync_ops = QPushButton("Sync Operations ▾")
        btn_sync_ops.setCursor(Qt.PointingHandCursor)
        btn_sync_ops.setStyleSheet(
            "QPushButton { background: #18181B; color: #E4E4E7; border: 1px solid #27272A; "
            "padding: 4px 12px; border-radius: 4px; font-weight: 600; font-size: 12px; }"
            "QPushButton:hover { background: #27272A; color: white; }"
        )
        btn_sync_ops.setFixedHeight(26)
        sync_menu = QMenu(btn_sync_ops)
        sync_menu.setStyleSheet("QMenu { background-color: #09090B; color: #E4E4E7; border: 1px solid #27272A; }")
        
        act_z_resync = sync_menu.addAction("Zotero Resync All")
        act_z_resync.triggered.connect(self._make_action_handler("Z Resync All", "POST", "/reconcile"))
        
        act_b_resync = sync_menu.addAction("Bookmarks Resync All")
        act_b_resync.triggered.connect(self._make_action_handler("B Resync All", "POST", "/bookmarks/flush"))
        
        act_sync_all = sync_menu.addAction("Sync All Pending")
        act_sync_all.triggered.connect(self._make_action_handler("Sync All Pending", "POST", "/history/sync"))
        
        btn_sync_ops.setMenu(sync_menu)
        btn_layout.addWidget(btn_sync_ops)

        # 6. Phone Admin Dropdown
        btn_phone_admin = QPushButton("Phone Admin ▾")
        btn_phone_admin.setCursor(Qt.PointingHandCursor)
        btn_phone_admin.setStyleSheet(
            "QPushButton { background: #18181B; color: #E4E4E7; border: 1px solid #27272A; "
            "padding: 4px 12px; border-radius: 4px; font-weight: 600; font-size: 12px; }"
            "QPushButton:hover { background: #27272A; color: white; }"
        )
        btn_phone_admin.setFixedHeight(26)
        phone_menu = QMenu(btn_phone_admin)
        phone_menu.setStyleSheet("QMenu { background-color: #09090B; color: #E4E4E7; border: 1px solid #27272A; }")
        
        act_check = phone_menu.addAction("Check Connection")
        act_check.triggered.connect(self._make_action_handler("Check Connection", "GET", "/status"))
        
        act_reconnect = phone_menu.addAction("Phone Reconnect")
        act_reconnect.triggered.connect(self._make_action_handler("Phone Reconnect", "POST", "/phone/reconnect"))
        
        act_pair = phone_menu.addAction("Pair Phone (ADB)")
        act_pair.triggered.connect(self._make_action_handler("Pair Phone", "POST", "/phone/pair"))
        
        act_keep = phone_menu.addAction("Keep Phone Awake")
        act_keep.triggered.connect(self._make_action_handler("Keep Phone Awake", "POST", "/phone/keep_awake"))
        
        act_diag = phone_menu.addAction("Diagnose USB")
        act_diag.triggered.connect(self._make_action_handler("Diagnose USB", "POST", "/phone/usb_diagnose"))
        
        btn_phone_admin.setMenu(phone_menu)
        btn_layout.addWidget(btn_phone_admin)

        # Status feedback label inside the toolbar to save vertical space
        btn_layout.addWidget(self._status_lbl, 1)

        # 7. Refresh
        btn_ref = QPushButton("🔄 Refresh")
        btn_ref.setCursor(Qt.PointingHandCursor)
        btn_ref.setStyleSheet(
            "QPushButton { background: #18181B; color: #E4E4E7; border: 1px solid #27272A; "
            "padding: 4px 12px; border-radius: 4px; font-weight: 600; font-size: 12px; }"
            "QPushButton:hover { background: #27272A; color: white; }"
        )
        btn_ref.setFixedHeight(26)
        btn_ref.clicked.connect(self.refresh)
        btn_layout.addWidget(btn_ref)

        outer.addWidget(btn_frame)

        # --- diagnostics panel ---
        self._diag_panel = QFrame()
        self._diag_panel.setStyleSheet(
            "QFrame { background: #09090B; border: 1px solid #27272A; "
            "border-left: 4px solid #EF4444; border-radius: 6px; padding: 6px 12px; }"
        )
        self._diag_panel.setMinimumHeight(48)
        self._diag_panel.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        
        diag_layout = QHBoxLayout(self._diag_panel)
        diag_layout.setContentsMargins(8, 4, 8, 4)
        diag_layout.setSpacing(10)

        self._diag_icon = QLabel("ℹ️")
        self._diag_icon.setStyleSheet("font-size: 14px;")
        diag_layout.addWidget(self._diag_icon)
        
        text_layout = QVBoxLayout()
        text_layout.setSpacing(1)
        text_layout.setContentsMargins(0, 0, 0, 0)
        
        self._diag_title = QLabel("Connection Diagnostics")
        self._diag_title.setStyleSheet("font-size: 12px; font-weight: 600; color: #F4F4F5;")
        self._diag_title.setTextFormat(Qt.TextFormat.RichText)
        text_layout.addWidget(self._diag_title)
        
        self._diag_detail = QLabel("No diagnostics run yet. Use 'Diagnose USB' in Phone Admin dropdown to troubleshoot connection.")
        self._diag_detail.setStyleSheet("color: #A1A1AA; font-size: 11px;")
        self._diag_detail.setWordWrap(True)
        self._diag_detail.setTextFormat(Qt.TextFormat.RichText)
        text_layout.addWidget(self._diag_detail)
        
        diag_layout.addLayout(text_layout, 1)
        outer.addWidget(self._diag_panel)

        # --- Tab widget ---
        self._tabs = QTabWidget()
        self._tabs.setStyleSheet(_TAB_QSS)
        outer.addWidget(self._tabs, 1)

        self._queue_tab = _UnifiedQueueTab(self)
        self._tabs.addTab(self._queue_tab, "📥 Unified Queue")

        self._phone_tab = _PhoneTabsTab(self)
        self._tabs.addTab(self._phone_tab, "📱 Phone Tabs (Queue)")

        self._history_tab = _SavedHistoryTab(self)
        self._tabs.addTab(self._history_tab, "📜 Saved History")

        # Load category config
        self._load_local_categories_list()

        # Timers
        self._timer = QTimer(self)
        self._timer.setInterval(15_000)
        self._timer.timeout.connect(self._on_timer_timeout)
        self._timer.start()

        # Initial refresh
        self.refresh()

    def _load_local_categories_list(self) -> None:
        try:
            from pathlib import Path
            root = Path(__file__).resolve().parents[2]
            config_path = root / "state" / "panop" / "panop_config.json"
            if config_path.exists():
                with open(config_path, "r", encoding="utf-8") as f:
                    self._categories_list = json.load(f).get("categories", [])
        except Exception:
            pass
        if not self._categories_list:
            self._categories_list = [
                {"id": "articles", "name": "Articles", "dest_folder": "Android Articles"},
                {"id": "books", "name": "Books", "dest_folder": "Android Books"},
                {"id": "science_news", "name": "Science News", "dest_folder": "Android Science News"},
                {"id": "science_longform", "name": "Science Longform (read-in-place)", "dest_folder": "Android Science Longform"}
            ]
        self._phone_tab.populate_categories(self._categories_list)

    def refresh(self) -> None:
        self._queue_tab.refresh()
        self.refresh_status()
        self.refresh_phone_tabs()
        self.refresh_history()

    def _on_timer_timeout(self) -> None:
        self.refresh_status()
        self._queue_tab.refresh()

    # -- status pills refresh -----------------------------------------------
    def refresh_status(self) -> None:
        t = _spawn_http(self, "GET", f"{_PANOP_BASE}/status", self._on_status_result)
        self._threads.append(t)
        t.finished.connect(lambda: self._threads.remove(t) if t in self._threads else None)

    def _on_status_result(self, result: dict) -> None:
        # clear pills
        while self._pills_row.count():
            it = self._pills_row.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()

        if not result or not result.get("ok"):
            self._pills_row.addWidget(_pill("Panop", "offline", False))
            self._pills_row.addStretch(1)
            self._update_diagnostics_display("error", "Panop server is offline. Check if Egon background processes are running.", False)
            return

        d = result.get("data") or {}
        if not isinstance(d, dict):
            d = {}
        adb = bool(d.get("adb_connected"))
        
        # Sync the diagnostics panel with the live ADB status
        if not adb:
            if self._diag_detail.text().startswith("No diagnostics") or "successfully" in self._diag_detail.text():
                self._update_diagnostics_display("error", "Phone is not connected. Click 'Diagnose USB' in Phone Admin to automatically configure connection.", False)
        else:
            dev_id = d.get("device_id") or "Connected Device"
            self._update_diagnostics_display("ok", f"Phone is connected successfully!\nDevice ID: {dev_id}\n\nYou are ready to sweep tabs and bookmarks.", True)
            
        chrome = bool(d.get("chrome_running"))
        sweep = bool(d.get("sweep_running"))
        tabs = d.get("tabs_seen") if d.get("tabs_seen") is not None else "—"
        bmarks = d.get("bookmarks_pending") if d.get("bookmarks_pending") is not None else "—"
        last_sweep = d.get("last_sweep") if d.get("last_sweep") is not None else "—"

        try:
            bmarks_is_zero = int(bmarks) == 0
        except (ValueError, TypeError):
            bmarks_is_zero = False

        for label, val, ok in [
            ("ADB Connected",      "Yes" if adb else "No",         adb),
            ("Chrome Running",     "Yes" if chrome else "No",      chrome),
            ("Sweep Running",      "Yes" if sweep else "No",       sweep),
            ("Active Tabs",        str(tabs),                      True),
            ("Bookmarks Pending",  str(bmarks),                    bmarks_is_zero),
            ("Last Sweep",         str(last_sweep),                last_sweep != "—"),
        ]:
            self._pills_row.addWidget(_pill(label, val, ok))
        self._pills_row.addStretch(1)

    # -- phone tabs inspect -------------------------------------------------
    def refresh_phone_tabs(self) -> None:
        self._status_lbl.setText("⏳ Fetching open tabs from phone's Android Chrome...")
        t = _spawn_http(self, "GET", f"{_PANOP_BASE}/tabs/inspect?wake=false", self._on_phone_tabs_result, timeout=180.0)
        self._threads.append(t)
        t.finished.connect(lambda: self._threads.remove(t) if t in self._threads else None)

    def _on_phone_tabs_result(self, result: dict) -> None:
        if not result or not result.get("ok"):
            self._status_lbl.setText(f"❌ Failed to fetch phone tabs: {result.get('error') if result else 'no response'}")
            return
        
        data_dict = result.get("data") or {}
        if not isinstance(data_dict, dict):
            data_dict = {}
        if data_dict.get("status") == "error":
            self._status_lbl.setText(f"❌ {data_dict.get('message')}")
            return
            
        tabs = data_dict.get("tabs") or []
        if not isinstance(tabs, list):
            tabs = []
        woken = data_dict.get("woken") or 0
        self._status_lbl.setText(f"✅ Found {len(tabs)} tabs on phone. Woken: {woken}.")
        self._phone_tab.load_tabs(tabs)

    # -- history ledger refresh ---------------------------------------------
    def refresh_history(self) -> None:
        t = _spawn_http(self, "GET", f"{_PANOP_BASE}/history?limit=250", self._on_history_result)
        self._threads.append(t)
        t.finished.connect(lambda: self._threads.remove(t) if t in self._threads else None)

    def _on_history_result(self, result: dict) -> None:
        result = result if isinstance(result, dict) else {}
        if not result.get("ok"):
            return
        raw_data = result.get("data")
        if not isinstance(raw_data, (list, dict)):
            raw_data = []
        
        items = []
        if isinstance(raw_data, dict):
            if "items" in raw_data or "history" in raw_data:
                items = raw_data.get("items", raw_data.get("history", []))
                if not isinstance(items, list):
                    items = []
            else:
                for url, info in raw_data.items():
                    if isinstance(info, dict):
                        item_copy = dict(info)
                        item_copy["url"] = url
                        items.append(item_copy)
                try:
                    items.sort(key=lambda x: str(x.get("date", "")), reverse=True)
                except Exception:
                    pass
        elif isinstance(raw_data, list):
            items = raw_data

        self._history_tab.load_history(items)

    # -- action button factory ---------------------------------------------
    def _make_action_handler(self, label: str, method: str, path: str):
        def handler():
            self._status_lbl.setText(f"⏳  {label}…")
            url = f"{_PANOP_BASE}{path}"
            t = _spawn_http(self, method, url,
                            lambda res, _l=label: self._on_action_result(_l, res))
            self._threads.append(t)
            t.finished.connect(lambda: self._threads.remove(t) if t in self._threads else None)
        return handler

    def _on_action_result(self, label: str, result: dict) -> None:
        if not result:
            self._status_lbl.setText(f"❌  {label}: no response")
            return
        if result.get("ok"):
            body = result.get("data")
            if body is None:
                body = ""
            if isinstance(body, dict):
                status = str(body.get("status") or "ok")
                if label == "Drain All Tabs":
                    msg = "Drain loop started. Saving and closing phone tabs..."
                elif "message" in body and body["message"] is not None:
                    msg = str(body["message"])
                elif "status" in body and body["status"] is not None:
                    msg = f"Status: {body['status']}"
                else:
                    msg = ", ".join(f"{k}: {v}" for k, v in body.items())
                connected = bool(body.get("connected"))
            else:
                status = "ok"
                msg = str(body)[:500]
                connected = False
            first_line = msg.splitlines()[0] if msg else "Done"
            self._status_lbl.setText(f"✅  {label}: {first_line}")
            
            # Reactive visual updates
            if label in ("Drain All Tabs", "Cancel Drain", "Merge Duplicates", "Z Resync All", "B Resync All", "Sync All Pending"):
                QTimer.singleShot(1500, self.refresh)
            
            if label in ("Diagnose USB", "Check Connection", "Phone Reconnect"):
                self._update_diagnostics_display(status, msg, connected)
        else:
            err = result.get("error") or "unknown error"
            self._status_lbl.setText(f"❌  {label}: {err}")
            if label in ("Diagnose USB", "Check Connection", "Phone Reconnect"):
                self._update_diagnostics_display("error", f"Connection failed: {err}", False)

    def _fetch_now_action(self) -> None:
        self._status_lbl.setText("⏳ Fetching now (running sweep on server)...")
        
        def callback(res):
            res = res if isinstance(res, dict) else {}
            if res.get("ok"):
                self._status_lbl.setText("✅ Sweep triggered on server. Loading phone tabs list...")
                QTimer.singleShot(2000, self.refresh)
            else:
                err = str(res.get("error") or "Unknown error")
                self._status_lbl.setText(f"❌ Fetch trigger failed: {err}")
                
        t = _spawn_http(self, "POST", f"{_PANOP_BASE}/fetch_now", callback)
        self._threads.append(t)
        t.finished.connect(lambda: self._threads.remove(t) if t in self._threads else None)

    def _update_diagnostics_display(self, status: str, message: str, connected: bool) -> None:
        status = str(status or "").lower()
        if connected:
            color = _COLOR_GREEN
            border_left_color = _COLOR_GREEN
            status_text = "Connected"
            icon = "🟢"
            # Auto-hide the diagnostics panel when connected successfully
            self._diag_panel.setVisible(False)
        elif status == "warning":
            color = _COLOR_AMBER
            border_left_color = _COLOR_AMBER
            status_text = "Action Required"
            icon = "🟡"
            self._diag_panel.setVisible(True)
        else:
            color = _COLOR_RED
            border_left_color = _COLOR_RED
            status_text = "Disconnected"
            icon = "🔴"
            self._diag_panel.setVisible(True)

        self._diag_panel.setStyleSheet(
            f"QFrame {{ background: #09090B; border: 1px solid #27272A; "
            f"border-left: 4px solid {border_left_color}; "
            f"border-radius: 6px; padding: 6px 12px; }}"
        )
        
        self._diag_icon.setText(icon)
        self._diag_title.setText(f"Connection Status: <span style='color:{color};'>{status_text}</span>")

        html_msg = str(message or "").replace("\n", "<br/>")
        html_msg = re.sub(r"(\d+\.)", r"<b>\1</b>", html_msg)
        self._diag_detail.setText(html_msg)
