"""Inbox page — dedicated Panop capture dashboard & history ledger."""
from __future__ import annotations

import json
import webbrowser
from egon_app.api import post_compat as __epost, get_compat as __eget
import os
import shutil
import re
import html
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urlparse

from PySide6.QtCore import Qt, QTimer, QThread, Signal, QObject, Slot
from PySide6.QtGui import QColor, QBrush
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
    QTableWidget, QTableWidgetItem, QPushButton, QHeaderView, QSizePolicy,
    QMessageBox, QTabWidget, QGridLayout, QLineEdit, QTextEdit, QFileDialog,
    QComboBox, QCheckBox, QMenu, QDialog, QFormLayout, QDialogButtonBox,
    QListWidget, QListWidgetItem, QSplitter
)

from egon_app import data

# ---------------------------------------------------------------------------
# Panop base URL
# ---------------------------------------------------------------------------
_PANOP_BASE = "http://127.0.0.1:8000/api/v1"
_ACTION_TIMEOUTS = {
    "Auto Connect Phone": 35.0,
    "Phone Reconnect": 20.0,
    "Diagnose USB": 30.0,
    "Pair Phone": 25.0,
    "Keep Phone Awake": 15.0,
    "Drain All Tabs": 10.0,
    "Fetch Now": 10.0,
}
_PHONE_TAB_RENDER_LIMIT = 120

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
            from egon_app.api import get_compat, post_compat
            if self._method.upper() == "GET":
                r = get_compat(self._url, timeout=self._timeout)
            else:
                r = post_compat(self._url, self._json_body,
                                timeout=self._timeout)
            if True:
                if r.status_code < 400:
                    try:
                        body = r.json()
                    except Exception:
                        body = r.text
                    body_status = ""
                    body_message = ""
                    if isinstance(body, dict):
                        body_status = str(body.get("status") or "").lower()
                        body_message = str(body.get("message") or body.get("error") or "")
                    if body_status in {"error", "failed", "failure"}:
                        self.finished.emit({
                            "ok": False,
                            "data": body,
                            "error": body_message or f"API returned status={body_status}",
                        })
                    else:
                        self.finished.emit({"ok": True, "data": body, "error": ""})
                else:
                    self.finished.emit({"ok": False, "data": None,
                                        "error": f"HTTP {r.status_code}: {r.text[:300]}"})
        except Exception as exc:
            self.finished.emit({"ok": False, "data": None, "error": str(exc)[:300]})


class _HttpCallbackProxy(QObject):
    """Delivers worker results on the GUI thread and contains slot errors."""
    done = Signal()

    def __init__(self, callback, status_label: QLabel | None = None):
        super().__init__()
        self._callback = callback
        self._status_label = status_label

    @Slot(dict)
    def handle(self, result: dict) -> None:
        try:
            self._callback(result)
        except Exception as exc:
            if self._status_label is not None:
                self._status_label.setText(f"Action failed in UI callback: {type(exc).__name__}: {exc}")
            print(f"[Inbox] UI callback failed: {type(exc).__name__}: {exc}")
        finally:
            self.done.emit()

def _spawn_http(parent: QWidget, method: str, url: str,
                callback, json_body: dict = None, timeout: float = 3.0) -> QThread:
    """Fire an HTTP request in a QThread. *callback(result_dict)* is called
    on the main thread when done."""
    thread = QThread(parent)
    worker = _HttpWorker(method, url, json_body, timeout)
    proxy = _HttpCallbackProxy(callback, getattr(parent, "_status_lbl", None))
    worker.moveToThread(thread)

    # Store reference on the thread object to prevent garbage collection
    thread._worker = worker
    thread._callback_proxy = proxy

    thread.started.connect(worker.run)
    worker.finished.connect(proxy.handle)
    worker.finished.connect(worker.deleteLater)
    proxy.done.connect(thread.quit)
    proxy.done.connect(proxy.deleteLater)
    thread.finished.connect(thread.deleteLater)


    thread.start()
    return thread


def _summarize_connection_status(body: dict) -> tuple[str, str, bool]:
    status = str(body.get("status") or "ok")
    connected = bool(body.get("connected", body.get("adb_connected", False)))
    adb = bool(body.get("adb_connected", connected))
    chrome = bool(body.get("chrome_running"))
    device = body.get("device_id") or "none"
    tabs = body.get("tabs_seen", 0)
    matched = body.get("tabs_matched", 0)
    pending = body.get("bookmarks_pending", 0)
    last_run = body.get("last_run") or "never"
    last_fetch = body.get("last_tab_fetch_at") or "never"

    if body.get("message"):
        msg = str(body["message"])
    elif body.get("last_error"):
        msg = f"Last sweep error: {body.get('last_error')}"
    else:
        live = "live" if adb and chrome else "not live"
        msg = (
            f"Phone link is {live}. Device: {device}. "
            f"Last capture: {tabs} tabs, {matched} matched, {pending} bookmark writes pending. "
            f"Last sweep: {last_run}; last tab fetch: {last_fetch}."
        )
    return status, msg, connected or adb


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
        self._filtered_phone_tabs: list[tuple[dict, str]] = []
        self._visible_filtered_count = 0
        self._selected_urls: set[str] = set()
        self._sort_column: int | None = None
        self._sort_desc = False
        self._sort_mode = "smart"

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

        self._scope_combo = QComboBox()
        self._scope_combo.setStyleSheet(_COMBO_QSS)
        self._scope_combo.addItem("Show: Ready for Sweep", "Ready for Sweep")
        self._scope_combo.addItem("Show: All Loaded Rows", "All Loaded Rows")
        self._scope_combo.currentIndexChanged.connect(self._apply_filters)
        self._scope_combo.setToolTip("Which rows are shown: ready, saved, unmatched, needs review, or all loaded rows.")
        filter_bar.addWidget(self._scope_combo, 1)

        self._category_combo = QComboBox()
        self._category_combo.setStyleSheet(_COMBO_QSS)
        self._category_combo.addItem("Category: Any", "Any Category")
        self._category_combo.currentIndexChanged.connect(self._apply_filters)
        self._category_combo.setToolTip("Category filter. Use this after choosing the row state in the Show menu.")
        filter_bar.addWidget(self._category_combo, 1)

        self._sort_combo = QComboBox()
        self._sort_combo.setStyleSheet(_COMBO_QSS)
        self._sort_combo.addItem("Sort: Smart Priority", "smart")
        self._sort_combo.addItem("Sort: Unsaved First", "unsaved")
        self._sort_combo.addItem("Sort: Needs Decision", "decision")
        self._sort_combo.addItem("Sort: Page Type", "kind")
        self._sort_combo.addItem("Sort: Site", "site")
        self._sort_combo.addItem("Sort: Category", "category")
        self._sort_combo.addItem("Sort: Title", "title")
        self._sort_combo.addItem("Sort: Clicked Column", "column")
        self._sort_combo.currentIndexChanged.connect(self._on_sort_mode_changed)
        self._sort_combo.setToolTip("Sorts the full filtered set before the first 120 rows are rendered.")
        filter_bar.addWidget(self._sort_combo, 1)

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

        btn_load_tabs = QPushButton("Load Phone Tabs")
        btn_load_tabs.setStyleSheet(
            "QPushButton { background: #18181B; color: #E4E4E7; border: 1px solid #27272A; "
            "border-radius: 6px; padding: 4px 10px; font-weight: 600; font-size: 12px; }"
            "QPushButton:hover { background: #27272A; color: white; }"
        )
        btn_load_tabs.setFixedHeight(28)
        btn_load_tabs.setCursor(Qt.PointingHandCursor)
        btn_load_tabs.clicked.connect(self._parent.refresh_phone_tabs)
        filter_bar.addWidget(btn_load_tabs)

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

        self._bulk_cat_combo = QComboBox()
        self._bulk_cat_combo.setStyleSheet(_COMBO_QSS)
        self._bulk_cat_combo.setFixedHeight(24)
        select_bar.addWidget(self._bulk_cat_combo)

        btn_apply_category = QPushButton("Set Checked Category")
        btn_apply_category.setStyleSheet(
            "QPushButton { background: #18181B; color: #E4E4E7; padding: 4px 10px; "
            "border-radius: 4px; font-weight: 600; font-size: 12px; border: 1px solid #27272A; }"
            "QPushButton:hover { background: #27272A; color: white; }"
        )
        btn_apply_category.setFixedHeight(24)
        btn_apply_category.clicked.connect(self._apply_checked_category)
        select_bar.addWidget(btn_apply_category)

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
            ("✓", 30), ("Type", 105), ("Title", 300), ("URL", 260),
            ("Predicted Category", 170), ("Status / Reason", 240), ("Actions", 150)
        ])
        self._phone_table.itemChanged.connect(self._on_table_item_changed)
        self._phone_table.itemDoubleClicked.connect(self._on_table_double_clicked)
        self._phone_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._phone_table.setSortingEnabled(False)
        self._phone_table.horizontalHeader().setSectionsClickable(True)
        self._phone_table.horizontalHeader().setSortIndicatorShown(True)
        self._phone_table.horizontalHeader().sectionClicked.connect(self._on_header_clicked)
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
        self._bulk_cat_combo.clear()
        self._bulk_cat_combo.addItem("Uncategorized / Do not sweep", "uncategorized")
        for cat in categories:
            self._manual_cat_combo.addItem(f"{get_emoji(cat['name'])} {cat['name']}", cat['id'])
            self._bulk_cat_combo.addItem(f"{get_emoji(cat['name'])} {cat['name']}", cat['id'])

        self._rebuild_filter_controls(self._current_scope_key(), self._current_category_key())

        # Set helpful tooltip listing predefined categories
        self._category_combo.setToolTip(
            "Predefined categories from panop_config.json:\n" +
            "\n".join(f"• {c['name']} (dest: {c['dest_folder']})" for c in categories)
        )

    def _tab_category_name(self, tab: dict) -> str:
        status_str = str(tab.get("status") or "")
        pred_cat_name = str(tab.get("category") or "").strip() or "Uncategorized"
        if status_str.startswith("match:"):
            pred_cat_name = status_str.split(":", 1)[1]
        elif status_str in {"needs_body_check", "body_required"}:
            pred_cat_name = "Needs body check"
        return pred_cat_name

    def _is_article_like_tab(self, tab: dict, category_name: str | None = None) -> bool:
        cat_name = (category_name or self._tab_category_name(tab)).lower()
        if cat_name in {"articles", "science news", "science longform (read-in-place)"}:
            return True
        text = f"{tab.get('title') or ''} {tab.get('url') or ''}".lower()
        patterns = (
            r"/article", r"/articles/", r"/doi/", r"\bdoi\b", r"\.pdf(?:\?|$)", r"arxiv\.org/",
            r"biorxiv\.org/", r"medrxiv\.org/", r"pubmed", r"pmc\.ncbi", r"nature\.com/",
            r"science\.org/", r"pnas\.org/", r"cell\.com/", r"sciencedirect\.com/", r"springer\.com/",
            r"link\.springer\.com/", r"wiley\.com/", r"tandfonline\.com/", r"jstor\.org/",
            r"muse\.jhu\.edu/", r"philarchive\.org/", r"osf\.io/", r"academic\.oup\.com/",
            r"cambridge\.org/", r"projecteuclid\.org/", r"frontiersin\.org/", r"mdpi\.com/",
            r"plos\.org/", r"royalsocietypublishing\.org/", r"liebertpub\.com/", r"sagepub\.com/",
            r"theatlantic\.com/", r"newyorker\.com/", r"aeon\.co/", r"nautil\.us/", r"quantamagazine\.org/",
        )
        return any(re.search(pattern, text) for pattern in patterns)

    def _current_scope_key(self) -> str:
        data = self._scope_combo.currentData()
        if data:
            return str(data)
        text = self._scope_combo.currentText()
        if text in ("All Predictions", "Only Matched Predictions", "Article-like / Reading", ""):
            return "Ready for Sweep"
        return text

    def _current_category_key(self) -> str:
        data = self._category_combo.currentData()
        if data:
            return str(data)
        text = self._category_combo.currentText()
        if text.startswith("Category: "):
            text = text.removeprefix("Category: ").strip()
        return text or "Any Category"

    def _filter_counts(self) -> dict:
        stats = {
            "all": len(self._raw_phone_tabs),
            "ready": 0,
            "needs_attention": 0,
            "possible_mistakes": 0,
            "saved": 0,
            "unmatched": 0,
            "cat_total": {},
            "cat_ready": {},
            "cat_saved": {},
        }
        for tab in self._raw_phone_tabs:
            if not isinstance(tab, dict):
                continue
            status_str = str(tab.get("status") or "")
            cat_name = self._tab_category_name(tab)
            is_ready = status_str.startswith("match:")
            if is_ready:
                stats["ready"] += 1
                stats["cat_ready"][cat_name] = stats["cat_ready"].get(cat_name, 0) + 1
            if status_str == "saved":
                stats["saved"] += 1
                stats["cat_saved"][cat_name] = stats["cat_saved"].get(cat_name, 0) + 1
            if status_str in {"needs_body_check", "body_required"} or (status_str == "no_match" and self._is_article_like_tab(tab, cat_name)):
                stats["needs_attention"] += 1
            if self._is_possible_mistake(tab, cat_name):
                stats["possible_mistakes"] += 1
            if status_str in ("no_match", "chrome_internal", "discarded", "") or cat_name == "Uncategorized":
                stats["unmatched"] += 1
            stats["cat_total"][cat_name] = stats["cat_total"].get(cat_name, 0) + 1
        return stats

    def _rebuild_filter_controls(self, preferred_scope: str | None = None, preferred_category: str | None = None) -> None:
        stats = self._filter_counts()
        categories = self._parent._categories_list or []
        preferred_scope = preferred_scope or "Ready for Sweep"
        preferred_category = preferred_category or "Any Category"

        self._scope_combo.blockSignals(True)
        self._scope_combo.clear()
        self._scope_combo.addItem(f"Show: Ready for Sweep ({stats['ready']})", "Ready for Sweep")
        self._scope_combo.addItem(f"Show: Needs Review ({stats['needs_attention']})", "Needs Rule / Classifier Attention")
        self._scope_combo.addItem(f"Show: Possible Mistakes ({stats['possible_mistakes']})", "Possible Mistakes")
        self._scope_combo.addItem(f"Show: All Loaded ({stats['all']})", "All Loaded Rows")
        self._scope_combo.addItem(f"Show: Already Saved ({stats['saved']})", "Already Saved")
        self._scope_combo.addItem(f"Show: Unmatched ({stats['unmatched']})", "Unmatched / Uncategorized")
        idx = self._scope_combo.findData(preferred_scope)
        self._scope_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._scope_combo.blockSignals(False)

        self._category_combo.blockSignals(True)
        self._category_combo.clear()
        self._category_combo.addItem("Category: Any", "Any Category")
        for cat in categories:
            name = str(cat.get("name") or "").strip()
            if not name:
                continue
            total = stats["cat_total"].get(name, 0)
            ready = stats["cat_ready"].get(name, 0)
            saved = stats["cat_saved"].get(name, 0)
            if total:
                self._category_combo.addItem(f"Category: {name} ({total} total, {ready} new, {saved} saved)", name)
            else:
                self._category_combo.addItem(f"Category: {name}", name)
        idx = self._category_combo.findData(preferred_category)
        self._category_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._category_combo.blockSignals(False)

    def load_tabs(self, tabs: list[dict]) -> None:
        self._raw_phone_tabs = tabs
        self._selected_urls.clear()
        self._rebuild_filter_controls(self._current_scope_key(), self._current_category_key())
        self._apply_filters()

    def _expanded_tab_url(self, url: str) -> str:
        url = (url or "").strip()
        try:
            parsed = urlparse(url)
            query = parse_qs(parsed.query)
            for key in ("url", "u", "q", "target", "to"):
                values = query.get(key) or []
                for value in values:
                    if value and value.startswith(("http://", "https://")):
                        return unquote(value)
        except Exception:
            pass
        return url

    def _tab_domain(self, url: str) -> str:
        resolved = self._expanded_tab_url(url)
        try:
            host = (urlparse(resolved).netloc or "").lower()
        except Exception:
            host = ""
        if host.startswith("www."):
            host = host[4:]
        return host

    def _generic_title_rank(self, title: str) -> int:
        low = (title or "").strip().lower()
        generic = (
            "", "untitled", "new tab", "just a moment...", "just a moment",
            "403 forbidden", "access denied", "verification required",
            "project muse -- verification required!", "one moment, please...",
        )
        if low in generic:
            return 1
        if low.startswith("google.com/url?") or low.startswith("http"):
            return 1
        return 0

    def _tab_text(self, tab: dict, pred_cat_name: str = "") -> str:
        return (
            f"{tab.get('title') or ''} {tab.get('url') or ''} {pred_cat_name} "
            f"{tab.get('reason') or ''} {tab.get('decision_source') or ''}"
        ).lower()

    def _tab_kind(self, tab: dict, pred_cat_name: str) -> str:
        status = str(tab.get("status") or "")
        url = str(tab.get("url") or "")
        domain = self._tab_domain(url)
        text = self._tab_text(tab, pred_cat_name)
        cat = (pred_cat_name or "").lower()
        if status == "saved":
            return "Saved"
        if status in {"discarded", "chrome_internal"}:
            return "System"
        if "book" in cat or "goodreads.com" in domain or "/books/" in text or "isbn" in text:
            return "Book"
        if any(x in domain for x in ("youtube.com", "youtu.be", "vimeo.com")):
            return "Video"
        if any(x in domain for x in ("github.com", "gitlab.com", "huggingface.co", "pypi.org", "npmjs.com")):
            return "Code"
        if any(x in domain for x in ("amazon.", "mercadolivre.", "shopee.", "aliexpress.", "magazineluiza.")):
            return "Shopping"
        if any(x in domain for x in ("arxiv.org", "biorxiv.org", "medrxiv.org", "pubmed", "pmc.ncbi", "doi.org")):
            return "Paper"
        if any(x in text for x in ("/doi/", "doi:", ".pdf", "/article", "/articles/", "journal", "abstract")):
            return "Article"
        if any(x in domain for x in ("aeon.co", "theatlantic.com", "newyorker.com", "nautil.us", "inference-review.com", "iai.tv")):
            return "Longform"
        if "science" in cat:
            return "Science"
        if cat == "uncategorized":
            return "Uncategorized"
        return pred_cat_name or "Page"

    def _quality_label(self, tab: dict, pred_cat_name: str) -> str:
        status = str(tab.get("status") or "")
        reason = str(tab.get("reason") or "")
        reason_low = reason.lower()
        decision_source = str(tab.get("decision_source") or "")
        cat = (pred_cat_name or "").lower()
        kind = self._tab_kind(tab, pred_cat_name)
        title = str(tab.get("title") or "")

        if status in {"discarded", "chrome_internal"}:
            return "Internal / suspended"
        if status == "saved":
            return "Already saved"
        if decision_source == "exact_user_correction":
            return "User corrected"
        if decision_source == "learned_domain_rule":
            return "Learned rule"
        if status in {"needs_body_check", "body_required"}:
            return "Needs body check"
        if status == "no_match" and self._is_article_like_tab(tab, pred_cat_name):
            return "Likely reading, not classified"
        if status == "no_match":
            return "Unmatched"
        if status.startswith("match:"):
            if self._generic_title_rank(title):
                return "Weak title; verify"
            if "smart classifier" in reason_low or "learned override" in reason_low:
                return "Smart guess"
            if kind == "Book" and "book" not in cat:
                return "Possible category mismatch"
            if kind in {"Paper", "Article", "Longform"} and not any(x in cat for x in ("article", "science", "longform", "read", "paper")):
                return "Possible category mismatch"
            if kind == "Code" and not any(x in cat for x in ("code", "software", "tool", "github")):
                return "Possible category mismatch"
            if kind == "Shopping" and not any(x in cat for x in ("shopping", "buy", "product")):
                return "Possible category mismatch"
            return "Ready"
        return "Needs review"

    def _is_possible_mistake(self, tab: dict, pred_cat_name: str) -> bool:
        return self._quality_label(tab, pred_cat_name) in {
            "Needs body check",
            "Likely reading, not classified",
            "Weak title; verify",
            "Smart guess",
            "Possible category mismatch",
        }

    def _status_rank(self, tab: dict, pred_cat_name: str) -> int:
        status = str(tab.get("status") or "")
        if self._is_possible_mistake(tab, pred_cat_name):
            return 0
        if status.startswith("match:"):
            return 1
        if status in {"needs_body_check", "body_required"}:
            return 2
        if status == "no_match" and self._is_article_like_tab(tab, pred_cat_name):
            return 3
        if status == "no_match":
            return 4
        if status == "saved":
            return 5
        if status in {"discarded", "chrome_internal"}:
            return 6
        return 7

    def _kind_rank(self, kind: str) -> int:
        order = {
            "Paper": 0,
            "Article": 1,
            "Longform": 2,
            "Science": 3,
            "Book": 4,
            "Code": 5,
            "Video": 6,
            "Shopping": 7,
            "Uncategorized": 8,
            "Saved": 9,
            "System": 10,
        }
        return order.get(kind, 20)

    def _title_key(self, tab: dict) -> tuple:
        title = str(tab.get("title") or "")
        url = str(tab.get("url") or "")
        return (self._generic_title_rank(title), title.lower(), self._tab_domain(url), self._expanded_tab_url(url).lower())

    def _smart_sort_key(self, row: tuple[dict, str]) -> tuple:
        tab, pred_cat_name = row
        kind = self._tab_kind(tab, pred_cat_name)
        return (
            self._status_rank(tab, pred_cat_name),
            self._kind_rank(kind),
            self._tab_domain(str(tab.get("url") or "")),
            self._title_key(tab),
        )

    def _mode_sort_key(self, row: tuple[dict, str]) -> tuple:
        tab, pred_cat_name = row
        mode = self._sort_mode or "smart"
        kind = self._tab_kind(tab, pred_cat_name)
        if mode == "unsaved":
            return (1 if str(tab.get("status") or "") == "saved" else 0, self._smart_sort_key(row))
        if mode == "decision":
            return (self._status_rank(tab, pred_cat_name), self._kind_rank(kind), self._title_key(tab))
        if mode == "kind":
            return (self._kind_rank(kind), kind.lower(), self._smart_sort_key(row))
        if mode == "site":
            return (self._tab_domain(str(tab.get("url") or "")), self._smart_sort_key(row))
        if mode == "category":
            return ((pred_cat_name or "").lower(), self._smart_sort_key(row))
        if mode == "title":
            return self._title_key(tab)
        return self._smart_sort_key(row)

    def _phone_tab_sort_key(self, row: tuple[dict, str], column: int) -> tuple:
        tab, pred_cat_name = row
        status = str(tab.get("status") or "")
        if column == 0:
            return (0 if str(tab.get("url") or "") in self._selected_urls else 1, self._smart_sort_key(row))
        if column == 1:
            kind = self._tab_kind(tab, pred_cat_name)
            return (self._kind_rank(kind), kind.lower(), self._smart_sort_key(row))
        if column == 2:
            return self._title_key(tab)
        if column == 3:
            return (self._tab_domain(str(tab.get("url") or "")), self._expanded_tab_url(str(tab.get("url") or "")).lower())
        if column == 4:
            return ((pred_cat_name or "").lower(), self._smart_sort_key(row))
        if column == 5:
            return (self._status_rank(tab, pred_cat_name), status.lower(), str(tab.get("reason") or "").lower())
        return self._smart_sort_key(row)

    def _filtered_url_set(self) -> set[str]:
        return {str(tab.get("url") or "") for tab, _cat in self._filtered_phone_tabs if str(tab.get("url") or "")}

    def _current_action_urls(self) -> set[str]:
        return self._selected_urls.intersection(self._filtered_url_set())

    def _category_id_for_name(self, category_name: str) -> str:
        for c in self._parent._categories_list or []:
            if c.get("name") == category_name:
                return str(c.get("id") or "uncategorized")
        return "uncategorized"

    def _on_header_clicked(self, column: int) -> None:
        if column == self._sort_column:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_column = column
            self._sort_desc = False
        self._sort_mode = "column"
        self._sort_combo.blockSignals(True)
        column_idx = self._sort_combo.findData("column")
        self._sort_combo.setCurrentIndex(column_idx if column_idx >= 0 else -1)
        self._sort_combo.blockSignals(False)
        order = Qt.DescendingOrder if self._sort_desc else Qt.AscendingOrder
        self._phone_table.horizontalHeader().setSortIndicator(column, order)
        self._apply_filters()

    def _on_sort_mode_changed(self, *_args) -> None:
        data = self._sort_combo.currentData()
        if not data:
            return
        if str(data) == "column":
            return
        self._sort_mode = str(data)
        self._sort_column = None
        self._sort_desc = False
        self._phone_table.horizontalHeader().setSortIndicatorShown(False)
        self._phone_table.horizontalHeader().setSortIndicatorShown(True)
        self._apply_filters()

    def _phone_tab_matches_filters(self, tab: dict, pred_cat_name: str, search: str, scope_filter: str, category_filter: str) -> bool:
        title = str(tab.get("title") or "")
        url = str(tab.get("url") or "")
        status_str = str(tab.get("status") or "")
        reason = str(tab.get("reason") or "")
        quality = self._quality_label(tab, pred_cat_name)
        kind = self._tab_kind(tab, pred_cat_name)
        source = str(tab.get("decision_source") or "")
        haystack = f"{title} {url} {pred_cat_name} {status_str} {reason} {quality} {kind} {source}".lower()
        if search and search not in haystack:
            return False

        if scope_filter == "Ready for Sweep":
            if not status_str.startswith("match:"):
                return False
        elif scope_filter == "Needs Rule / Classifier Attention":
            if status_str not in {"needs_body_check", "body_required"} and (status_str != "no_match" or not self._is_article_like_tab(tab, pred_cat_name)):
                return False
        elif scope_filter == "Possible Mistakes":
            if not self._is_possible_mistake(tab, pred_cat_name):
                return False
        elif scope_filter == "Already Saved":
            if status_str != "saved":
                return False
        elif scope_filter == "Unmatched / Uncategorized":
            if status_str not in ("no_match", "chrome_internal", "discarded", "") and pred_cat_name != "Uncategorized":
                return False
        elif scope_filter != "All Loaded Rows":
            return False

        if category_filter != "Any Category" and pred_cat_name != category_filter:
            return False
        return True

    def _apply_filters(self, *_args) -> None:
        self._phone_table.blockSignals(True)
        self._phone_table.setSortingEnabled(False)
        self._phone_table.setRowCount(0)

        search = self._search_input.text().strip().lower()
        scope_filter = self._current_scope_key()
        category_filter = self._current_category_key()

        categories_list = self._parent._categories_list or []

        filtered = []
        for tab in self._raw_phone_tabs:
            if not isinstance(tab, dict):
                continue
            pred_cat_name = self._tab_category_name(tab)
            if self._phone_tab_matches_filters(tab, pred_cat_name, search, scope_filter, category_filter):
                filtered.append((tab, pred_cat_name))

        if self._sort_column is not None:
            filtered.sort(key=lambda row: self._phone_tab_sort_key(row, self._sort_column), reverse=self._sort_desc)
        else:
            filtered.sort(key=self._mode_sort_key)
        self._filtered_phone_tabs = filtered
        self._visible_filtered_count = len(filtered)
        render_rows = filtered[:_PHONE_TAB_RENDER_LIMIT]
        for tab, pred_cat_name in render_rows:
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

            # 1. Type / page kind. Keep it as a plain item rather than a
            # widget so the table remains fast with thousands of tabs.
            kind = self._tab_kind(tab, pred_cat_name)
            type_item = QTableWidgetItem(f"{get_emoji(pred_cat_name)} {kind}")
            type_item.setTextAlignment(Qt.AlignCenter)
            type_item.setToolTip(f"Page type: {kind}\nDomain: {self._tab_domain(url)}")
            self._phone_table.setItem(r, 1, type_item)

            # 2. Title
            title_item = QTableWidgetItem(title)
            self._phone_table.setItem(r, 2, title_item)

            # 3. URL
            url_item = QTableWidgetItem(url)
            self._phone_table.setItem(r, 3, url_item)

            # 4. Predicted Category. Keep rows lightweight; per-row combo
            # widgets across hundreds of phone tabs freeze the Qt GUI.
            cur_cat_id = "uncategorized"
            for c in categories_list:
                if c["name"] == pred_cat_name:
                    cur_cat_id = c["id"]
                    break
            cat_item = QTableWidgetItem(f"{get_emoji(pred_cat_name)} {pred_cat_name}")
            cat_item.setData(Qt.UserRole, cur_cat_id)
            cat_item.setToolTip(
                f"Predicted category: {pred_cat_name}\n"
                "To correct it, check the row, choose a category above, then use Set Checked Category."
            )
            self._phone_table.setItem(r, 4, cat_item)

            # 5. Status / Reason
            quality = self._quality_label(tab, pred_cat_name)
            display_reason = reason
            if quality not in {"Ready", "Already saved"}:
                display_reason = f"{quality}: {reason}"
            reason_item = QTableWidgetItem(display_reason)
            reason_item.setToolTip(
                f"Decision quality: {quality}\n"
                f"Status: {status}\n"
                f"Kind: {kind}\n"
                f"Category: {pred_cat_name}\n"
                f"Domain: {self._tab_domain(url)}\n"
                f"Source: {tab.get('decision_source') or 'not reported'}\n"
                f"Confidence: {tab.get('decision_confidence') if tab.get('decision_confidence') is not None else 'not reported'}\n"
                f"Reason: {reason}"
            )
            if status == "saved":
                reason_item.setForeground(QBrush(QColor(_COLOR_GREEN)))
            elif self._is_possible_mistake(tab, pred_cat_name):
                reason_item.setForeground(QBrush(QColor(_COLOR_AMBER)))
            elif status.startswith("match:"):
                reason_item.setForeground(QBrush(QColor(_COLOR_CYAN)))
            elif status in {"needs_body_check", "body_required"}:
                reason_item.setForeground(QBrush(QColor(_COLOR_AMBER)))
            elif status == "chrome_internal":
                reason_item.setForeground(QBrush(QColor(_COLOR_MUTED)))
            self._phone_table.setItem(r, 5, reason_item)

            # 6. Actions row
            action_item = QTableWidgetItem("Check row, then use Save & Close Checked or Close Checked")
            action_item.setForeground(QBrush(QColor(_COLOR_MUTED)))
            self._phone_table.setItem(r, 6, action_item)

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
        checked = (state == Qt.Checked.value or state == Qt.Checked)
        filtered_urls = self._filtered_url_set()

        if checked:
            self._selected_urls.update(filtered_urls)
        else:
            self._selected_urls.difference_update(filtered_urls)

        for r in range(self._phone_table.rowCount()):
            chk_item = self._phone_table.item(r, 0)
            if chk_item:
                chk_item.setCheckState(Qt.Checked if checked else Qt.Unchecked)

        self._phone_table.blockSignals(False)
        self._update_selected_label()

    def _apply_checked_category(self) -> None:
        active_urls = self._current_action_urls()
        if not active_urls:
            QMessageBox.warning(self, "Set Category", "No tabs selected.")
            return
        cat_id = self._bulk_cat_combo.currentData()
        is_uncategorized = str(cat_id or "").lower() == "uncategorized"
        cat = None if is_uncategorized else next((c for c in (self._parent._categories_list or []) if c.get("id") == cat_id), None)
        if not is_uncategorized and not cat:
            QMessageBox.warning(self, "Set Category", "Choose a category first.")
            return
        name = "Uncategorized" if is_uncategorized else cat.get("name", "Uncategorized")
        changed = 0
        changed_urls = set()
        override_items = []
        for tab in self._raw_phone_tabs:
            url = str(tab.get("url") or "")
            if url in active_urls:
                previous_category_name = self._tab_category_name(tab)
                previous_category_id = str(tab.get("cat_id") or self._category_id_for_name(previous_category_name))
                previous_status = str(tab.get("status") or "")
                previous_reason = str(tab.get("reason") or "")
                previous_quality = self._quality_label(tab, previous_category_name)
                tab["category"] = name
                tab["cat_id"] = cat_id
                if is_uncategorized:
                    tab["status"] = "no_match"
                    tab["reason"] = "User marked as Uncategorized / do not sweep."
                else:
                    tab["status"] = f"match:{name}"
                    tab["reason"] = f"User corrected to '{name}'."
                changed_urls.add(url)
                override_items.append({
                    "url": url,
                    "title": str(tab.get("title") or ""),
                    "category_id": str(cat_id or "uncategorized"),
                    "previous_category_id": previous_category_id,
                    "previous_category_name": previous_category_name,
                    "previous_status": previous_status,
                    "previous_reason": previous_reason,
                    "decision_quality": previous_quality,
                    "reason": tab["reason"],
                })
                changed += 1
        if is_uncategorized:
            self._selected_urls.difference_update(changed_urls)
        if override_items:
            payload = {"items": override_items}
            self._parent._status_lbl.setText(f"Recording {len(override_items)} classifier correction(s)...")
            def _correction_callback(res):
                if not res.get("ok"):
                    self._parent._status_lbl.setText(f"Correction learning failed: {res.get('error') or 'unknown error'}")
                    return
                data = res.get("data") or {}
                learned_domains = data.get("learned_domains", 0)
                exact = data.get("exact_overrides", 0)
                self._parent._status_lbl.setText(
                    f"Learned {data.get('count', len(override_items))} correction(s). "
                    f"{exact} exact override(s), {learned_domains} learned domain rule(s)."
                )
            t = _spawn_http(
                self,
                "POST",
                f"{_PANOP_BASE}/classifier/overrides",
                _correction_callback,
                json_body=payload,
                timeout=15.0,
            )
            self._parent._threads.append(t)
            t.finished.connect(lambda: self._parent._threads.remove(t) if t in self._parent._threads else None)
        self._rebuild_filter_controls(self._current_scope_key(), self._current_category_key())
        self._apply_filters()
        if is_uncategorized:
            self._parent._status_lbl.setText(f"Marked {changed} checked tab(s) as Uncategorized. They are excluded from Ready for Sweep.")
        else:
            self._parent._status_lbl.setText(f"Set {changed} checked tab(s) to {name}. Future fetches will use this correction.")

    def _update_selected_label(self) -> None:
        filtered_urls = self._filtered_url_set()
        count = len(self._selected_urls.intersection(filtered_urls))
        total = len(filtered_urls)
        visible = self._phone_table.rowCount()
        shown_suffix = ""
        if self._visible_filtered_count > visible:
            shown_suffix = f" | showing first {visible} of {self._visible_filtered_count}"
        self._lbl_sel_count.setText(f"{count} of {total} selected | {visible} visible{shown_suffix}")
        self._chk_select_all.blockSignals(True)
        if count == 0:
            self._chk_select_all.setCheckState(Qt.Unchecked)
        elif total > 0 and count == total:
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
        if str(category_id or "").lower() == "uncategorized":
            self._parent._status_lbl.setText("Uncategorized tabs are excluded from Save & Close. Choose a real category first.")
            return
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
                r = __epost(f"{_PANOP_BASE}/history/add", {
                    "url": url, "title": title, "category_id": category_id
                }, timeout=15.0)
                if r.status_code == 200 and r.json().get("status") == "ok":
                    if tid:
                        try: __epost(f"http://127.0.0.1:9222/json/close/{tid}", timeout=5.0)
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
                r = __epost(f"http://127.0.0.1:9222/json/close/{tid}", timeout=5.0)
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
        active_urls = self._current_action_urls()
        if not active_urls:
            QMessageBox.warning(self, "Save Tabs", "No tabs selected.")
            return

        targets = []
        seen_urls = set()
        for tab, pred_cat_name in self._filtered_phone_tabs:
            url = str(tab.get("url") or "")
            if not url or url not in active_urls or url in seen_urls:
                continue
            seen_urls.add(url)
            title = str(tab.get("title") or url)
            category_id = str(tab.get("cat_id") or self._category_id_for_name(pred_cat_name))
            if category_id.lower() == "uncategorized":
                continue
            targets.append({"url": url, "title": title, "category_id": category_id, "tid": str(tab.get("id") or "")})

        if not targets:
            self._parent._status_lbl.setText("No saveable tabs selected. Uncategorized tabs are excluded from Save & Close.")
            return

        self._parent._status_lbl.setText(f"⏳ Saving {len(targets)} selected tab(s) to Zotero/Bookmarks...")

        import threading
        import httpx

        def _save_worker():
            success_count = 0
            fail_count = 0
            for item in targets:
                try:
                    r = __epost(f"{_PANOP_BASE}/history/add", {
                        "url": item["url"], "title": item["title"], "category_id": item["category_id"]
                    }, timeout=15.0)

                    if r.status_code == 200 and r.json().get("status") == "ok":
                        success_count += 1
                        if item["tid"]:
                            try:
                                __epost(f"http://127.0.0.1:9222/json/close/{item['tid']}", timeout=5.0)
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
        active_urls = self._current_action_urls()
        if not active_urls:
            QMessageBox.warning(self, "Close Tabs", "No tabs selected.")
            return

        confirm = QMessageBox.question(
            self, "Close Tabs",
            f"Are you sure you want to close the {len(active_urls)} selected tab(s) on your phone without saving them?",
            QMessageBox.Yes | QMessageBox.No
        )
        if confirm != QMessageBox.Yes:
            return

        tids = []
        for tab, _pred_cat_name in self._filtered_phone_tabs:
            url = str(tab.get("url") or "")
            tid = tab.get("id")
            if url in active_urls and tid:
                tids.append(tid)

        self._parent._status_lbl.setText(f"⏳ Closing {len(tids)} tab(s) on phone...")

        import threading
        import httpx
        def _close_worker():
            closed = 0
            for tid in tids:
                try:
                    r = __epost(f"http://127.0.0.1:9222/json/close/{tid}", timeout=5.0)
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
                        r = __epost(f"{_PANOP_BASE}/history/sync_single?url={url}&type=zotero", timeout=15.0)
                        if r.status_code == 200 and r.json().get("status") == "ok":
                            succeeded += 1
                        else:
                            failed += 1
                    if b_need:
                        r = __epost(f"{_PANOP_BASE}/history/sync_single?url={url}&type=bookmark", timeout=15.0)
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
                r = __epost(f"{_PANOP_BASE}/history/delete", {"urls": urls}, timeout=15.0)
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
class _CategoriesRulesTab(QWidget):
    def __init__(self, parent: "InboxPage"):
        super().__init__(parent)
        self._parent = parent
        self._config: dict[str, Any] = {}
        self._categories: list[dict[str, Any]] = []
        self._loading = False
        self._active_row = -1

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)

        self._summary = QLabel("0 categories")
        self._summary.setStyleSheet("color: #D4D4D8; font-size: 12px; font-weight: 600;")
        toolbar.addWidget(self._summary, 1)

        for text, slot in [
            ("Add Category", self._add_category),
            ("Delete Selected", self._delete_selected),
            ("Reload Rules", self.reload),
            ("Save Rules", self.save),
        ]:
            btn = QPushButton(text)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setFixedHeight(28)
            btn.setStyleSheet(
                "QPushButton { background: #18181B; color: #E4E4E7; border: 1px solid #27272A; "
                "border-radius: 4px; padding: 4px 10px; font-weight: 600; font-size: 12px; }"
                "QPushButton:hover { background: #27272A; color: white; }"
            )
            btn.clicked.connect(slot)
            toolbar.addWidget(btn)
        layout.addLayout(toolbar)

        split = QSplitter(Qt.Horizontal)
        split.setStyleSheet(
            "QSplitter::handle { background: #22252a; width: 1px; }"
            "QSplitter::handle:hover { background: #ff453a; }"
        )
        layout.addWidget(split, 1)

        left = QFrame()
        left.setStyleSheet("QFrame { background: #050505; border: 1px solid #27272A; border-radius: 6px; }")
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(8, 8, 8, 8)
        left_layout.setSpacing(6)
        left_title = QLabel("Category Buckets")
        left_title.setStyleSheet("color: #EF4444; font-size: 12px; font-weight: 700;")
        left_layout.addWidget(left_title)
        self._list = QListWidget()
        self._list.setStyleSheet(
            "QListWidget { background: #09090B; border: 1px solid #27272A; color: #F4F4F5; "
            "font-size: 12px; outline: 0; }"
            "QListWidget::item { padding: 8px; border-bottom: 1px solid #18181B; }"
            "QListWidget::item:selected { background: #27272A; color: white; border-left: 3px solid #EF4444; }"
        )
        self._list.currentRowChanged.connect(self._on_selected_category)
        left_layout.addWidget(self._list, 1)
        split.addWidget(left)

        right = QFrame()
        right.setStyleSheet("QFrame { background: #050505; border: 1px solid #27272A; border-radius: 6px; }")
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(10, 10, 10, 10)
        right_layout.setSpacing(8)

        self._detail_title = QLabel("Select a category")
        self._detail_title.setStyleSheet("color: #F4F4F5; font-size: 15px; font-weight: 700;")
        right_layout.addWidget(self._detail_title)

        form = QGridLayout()
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(6)
        self._id_edit = self._line_edit()
        self._name_edit = self._line_edit()
        self._dest_edit = self._line_edit()
        self._mode_combo = QComboBox()
        self._mode_combo.setStyleSheet(_COMBO_QSS)
        self._mode_combo.addItems(["ANY", "ALL"])
        self._keep_open = QCheckBox("Keep source tab open after capture")
        self._keep_open.setStyleSheet("color: #E4E4E7; font-size: 12px;")

        for row, (label, widget) in enumerate([
            ("ID", self._id_edit),
            ("Name", self._name_edit),
            ("Destination folder", self._dest_edit),
            ("Body match mode", self._mode_combo),
        ]):
            form.addWidget(self._form_label(label), row, 0)
            form.addWidget(widget, row, 1)
        form.addWidget(self._keep_open, 4, 1)
        right_layout.addLayout(form)

        rules_layout = QHBoxLayout()
        rules_layout.setSpacing(8)
        self._domains_edit = self._rules_box("Domain keywords")
        self._body_required_edit = self._rules_box("Body required")
        self._body_forbidden_edit = self._rules_box("Body forbidden")
        for title, editor in [
            ("Domain keywords", self._domains_edit),
            ("Body required", self._body_required_edit),
            ("Body forbidden", self._body_forbidden_edit),
        ]:
            box = QVBoxLayout()
            lab = self._form_label(title)
            box.addWidget(lab)
            box.addWidget(editor)
            rules_layout.addLayout(box, 1)
        right_layout.addLayout(rules_layout, 1)
        split.addWidget(right)
        split.setSizes([330, 980])

        self._status = QLabel("Rules are loaded from Panop config.")
        self._status.setStyleSheet("color: #A1A1AA; font-size: 11px;")
        layout.addWidget(self._status)

        for widget in [self._id_edit, self._name_edit, self._dest_edit]:
            widget.textChanged.connect(self._commit_current)
        for widget in [self._domains_edit, self._body_required_edit, self._body_forbidden_edit]:
            widget.textChanged.connect(self._commit_current)
        self._mode_combo.currentTextChanged.connect(self._commit_current)
        self._keep_open.stateChanged.connect(self._commit_current)

    def load_config(self, config: dict[str, Any]) -> None:
        self._loading = True
        self._config = dict(config or {})
        cats = self._config.get("categories") or []
        if not isinstance(cats, list):
            cats = []
        self._categories = [dict(cat) for cat in cats if isinstance(cat, dict)]
        self._list.clear()
        for cat in self._categories:
            self._list.addItem(self._list_item_for(cat))
        self._summary.setText(self._summary_text())
        if self._categories:
            self._active_row = 0
            self._list.setCurrentRow(0)
            self._load_editor(self._categories[0])
        else:
            self._active_row = -1
            self._clear_editor()
        self._loading = False
        self._status.setText(f"Loaded {len(self._categories)} categories.")

    def reload(self) -> None:
        self._parent.refresh_config()

    def save(self) -> None:
        self._commit_current()
        config = dict(self._config or {})
        config["categories"] = self._categories

        def callback(result: dict) -> None:
            if result.get("ok"):
                self._status.setText("Saved category rules.")
                self._parent._panop_config = config
                self._parent._categories_list = config.get("categories") or []
                self._parent._phone_tab.populate_categories(self._parent._categories_list)
                self._parent.refresh_status()
            else:
                self._status.setText(f"Save failed: {result.get('error') or 'unknown error'}")

        t = _spawn_http(self._parent, "POST", f"{_PANOP_BASE}/config", callback, json_body=config, timeout=15.0)
        self._parent._threads.append(t)
        t.finished.connect(lambda: self._parent._threads.remove(t) if t in self._parent._threads else None)

    def _add_category(self) -> None:
        idx = len(self._categories) + 1
        cat = {
            "id": f"category_{idx}",
            "name": f"Category {idx}",
            "dest_folder": f"Android Category {idx}",
            "domain_keywords": [],
            "body_required": [],
            "body_forbidden": [],
            "body_required_mode": "ANY",
        }
        self._categories.append(cat)
        self._list.addItem(self._list_item_for(cat))
        self._summary.setText(self._summary_text())
        self._list.setCurrentRow(len(self._categories) - 1)

    def _delete_selected(self) -> None:
        row = self._list.currentRow()
        if row < 0 or row >= len(self._categories):
            return
        removed = self._categories.pop(row)
        self._list.takeItem(row)
        self._summary.setText(self._summary_text())
        self._status.setText(f"Deleted unsaved category: {removed.get('name') or removed.get('id')}")
        if self._categories:
            self._active_row = min(row, len(self._categories) - 1)
            self._list.setCurrentRow(self._active_row)
        else:
            self._active_row = -1
            self._clear_editor()

    def _on_selected_category(self, row: int) -> None:
        if self._loading:
            return
        self._commit_current()
        if 0 <= row < len(self._categories):
            self._active_row = row
            self._load_editor(self._categories[row])

    def _load_editor(self, cat: dict[str, Any]) -> None:
        self._loading = True
        self._detail_title.setText(str(cat.get("name") or "Untitled category"))
        self._id_edit.setText(str(cat.get("id") or ""))
        self._name_edit.setText(str(cat.get("name") or ""))
        self._dest_edit.setText(str(cat.get("dest_folder") or ""))
        mode = str(cat.get("body_required_mode") or "ANY").upper()
        idx = self._mode_combo.findText(mode)
        self._mode_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._keep_open.setChecked(bool(cat.get("keep_open_after_extract")))
        self._domains_edit.setPlainText(self._join_rules(cat.get("domain_keywords")))
        self._body_required_edit.setPlainText(self._join_rules(cat.get("body_required")))
        self._body_forbidden_edit.setPlainText(self._join_rules(cat.get("body_forbidden")))
        self._loading = False

    def _clear_editor(self) -> None:
        self._loading = True
        self._detail_title.setText("No category selected")
        for widget in [self._id_edit, self._name_edit, self._dest_edit,
                       self._domains_edit, self._body_required_edit, self._body_forbidden_edit]:
            if isinstance(widget, QTextEdit):
                widget.clear()
            else:
                widget.clear()
        self._mode_combo.setCurrentIndex(0)
        self._keep_open.setChecked(False)
        self._loading = False

    def _commit_current(self) -> None:
        if self._loading:
            return
        row = self._active_row
        if row < 0 or row >= len(self._categories):
            return
        cat = self._categories[row]
        cat_id = self._id_edit.text().strip()
        name = self._name_edit.text().strip()
        if not cat_id and name:
            cat_id = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
            self._id_edit.setText(cat_id)
        cat.update({
            "id": cat_id,
            "name": name,
            "dest_folder": self._dest_edit.text().strip() or name,
            "domain_keywords": self._split_rules(self._domains_edit.toPlainText()),
            "body_required": self._split_rules(self._body_required_edit.toPlainText()),
            "body_forbidden": self._split_rules(self._body_forbidden_edit.toPlainText()),
            "body_required_mode": self._mode_combo.currentText().upper(),
            "keep_open_after_extract": self._keep_open.isChecked(),
        })
        self._detail_title.setText(name or "Untitled category")
        item = self._list.item(row)
        if item:
            fresh = self._list_item_for(cat)
            item.setText(fresh.text())
            item.setToolTip(fresh.toolTip())
        self._summary.setText(self._summary_text())

    def _list_item_for(self, cat: dict[str, Any]) -> QListWidgetItem:
        domains = len(cat.get("domain_keywords") or [])
        body_required = len(cat.get("body_required") or [])
        body_forbidden = len(cat.get("body_forbidden") or [])
        keep = "keep open" if cat.get("keep_open_after_extract") else "close allowed"
        name = str(cat.get("name") or cat.get("id") or "Untitled")
        item = QListWidgetItem(
            f"{name}\n{domains} domains · {body_required} required · {body_forbidden} forbidden · {keep}"
        )
        item.setToolTip(str(cat.get("dest_folder") or "No destination folder"))
        return item

    def _summary_text(self) -> str:
        domain_count = sum(len(c.get("domain_keywords") or []) for c in self._categories)
        body_count = sum(len(c.get("body_required") or []) + len(c.get("body_forbidden") or []) for c in self._categories)
        return f"{len(self._categories)} categories · {domain_count} domain rules · {body_count} body rules"

    def _line_edit(self) -> QLineEdit:
        edit = QLineEdit()
        edit.setStyleSheet(_INPUT_QSS)
        return edit

    def _rules_box(self, placeholder: str) -> QTextEdit:
        edit = QTextEdit()
        edit.setPlaceholderText(f"{placeholder}: one per line, or comma-separated")
        edit.setStyleSheet(_INPUT_QSS)
        edit.setMinimumHeight(260)
        return edit

    @staticmethod
    def _form_label(text: str) -> QLabel:
        lab = QLabel(text)
        lab.setStyleSheet("color: #A1A1AA; font-size: 11px; font-weight: 600;")
        return lab

    @staticmethod
    def _join_rules(value: Any) -> str:
        if isinstance(value, list):
            return "\n".join(str(v) for v in value if str(v).strip())
        return str(value or "")

    @staticmethod
    def _split_rules(value: str) -> list[str]:
        parts = re.split(r"[\n,]+", value or "")
        return [p.strip() for p in parts if p.strip()]


class InboxPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background-color: #030303;")
        self._threads: list[QThread] = []
        self._phone_tabs_loading = False
        self._categories_list: list[dict] = []
        self._panop_config: dict[str, Any] = {}
        self._env_config: dict[str, Any] = {}
        self._env_checks: dict[str, QCheckBox] = {}

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

        # 1b. Fetch — Articles / Books / Science News ONLY (save + close only those)
        btn_fetch_abs = QPushButton("📚 A/B/SciNews Only")
        btn_fetch_abs.setCursor(Qt.PointingHandCursor)
        btn_fetch_abs.setStyleSheet(
            "QPushButton { background: #2563EB; color: white; padding: 4px 12px; "
            "border-radius: 4px; font-weight: 700; font-size: 12px; border: none; }"
            "QPushButton:hover { background: #1D4ED8; }"
        )
        btn_fetch_abs.setFixedHeight(26)
        btn_fetch_abs.setToolTip("Fetch + clean ONLY Articles, Books and Science News — "
                                 "closes only those tabs; everything else stays open on the phone.")
        btn_fetch_abs.clicked.connect(self._fetch_restricted_action)
        btn_layout.addWidget(btn_fetch_abs)

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

        btn_auto_phone = QPushButton("Auto Connect Phone")
        btn_auto_phone.setCursor(Qt.PointingHandCursor)
        btn_auto_phone.setStyleSheet(
            "QPushButton { background: #18181B; color: #E4E4E7; border: 1px solid #27272A; "
            "padding: 4px 12px; border-radius: 4px; font-weight: 700; font-size: 12px; }"
            "QPushButton:hover { background: #27272A; color: white; }"
        )
        btn_auto_phone.setFixedHeight(26)
        btn_auto_phone.clicked.connect(self._make_action_handler("Auto Connect Phone", "POST", "/phone/reconnect"))
        btn_layout.addWidget(btn_auto_phone)

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

        act_reconnect = phone_menu.addAction("Auto Connect / Repair")
        act_reconnect.triggered.connect(self._make_action_handler("Auto Connect Phone", "POST", "/phone/reconnect"))

        act_pair = phone_menu.addAction("Pair with Code (first time)")
        act_pair.triggered.connect(self._pair_phone_action)

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

        # --- safety controls ---
        safety_frame = QFrame()
        safety_frame.setStyleSheet(
            "QFrame { background: #09090B; border: 1px solid #27272A; "
            "border-radius: 6px; padding: 3px; }"
            "QCheckBox { color: #E4E4E7; font-size: 11px; padding: 2px 6px; }"
            "QCheckBox::indicator { width: 14px; height: 14px; }"
        )
        safety_frame.setMaximumHeight(34)
        safety_layout = QHBoxLayout(safety_frame)
        safety_layout.setContentsMargins(8, 3, 8, 3)
        safety_layout.setSpacing(10)
        safety_title = QLabel("Safety")
        safety_title.setStyleSheet("color: #F4F4F5; font-weight: 700; font-size: 11px;")
        safety_layout.addWidget(safety_title)
        for key, label, tip in [
            ("require_manual_vetting_before_close", "Require vetting before close", "When on, Egon refuses all automatic already-synced tab closing."),
            ("close_tabs_after_save", "Auto-close after save", "Allow Panop close paths after successful save. Kept blocked while vetting is required."),
            ("enable_autonomous_sweep", "Scheduled sweeps", "Allow unattended Panop sweeps while Egon is running."),
            ("resolve_terminal_redirects", "Use true destination URLs", "Unwrap and follow redirect-like links before classifying or saving."),
        ]:
            chk = QCheckBox(label)
            chk.setToolTip(tip)
            chk.toggled.connect(lambda checked, _key=key: self._set_env_flag(_key, checked))
            self._env_checks[key] = chk
            safety_layout.addWidget(chk)
        safety_layout.addStretch(1)
        outer.addWidget(safety_frame)

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

        self._categories_tab = _CategoriesRulesTab(self)
        self._tabs.addTab(self._categories_tab, "Categories / Rules")

        # Load category config
        self._load_local_categories_list()

        # Timers
        self._timer = QTimer(self)
        self._timer.setInterval(15_000)
        self._timer.timeout.connect(self._on_timer_timeout)
        self._timer.start()

        # Initial refresh
        self.refresh()


    def closeEvent(self, event) -> None:
        try:
            self._timer.stop()
        except Exception:
            pass
        for thread in list(self._threads):
            try:
                if thread.isRunning():
                    thread.requestInterruption()
                    thread.quit()
                    if not thread.wait(750):
                        thread.terminate()
                        thread.wait(750)
            except RuntimeError:
                pass
            except Exception as exc:
                print(f"[Inbox] failed to stop worker thread: {type(exc).__name__}: {exc}")
        self._threads.clear()
        super().closeEvent(event)

    def _load_local_categories_list(self) -> None:
        try:
            from pathlib import Path
            root = Path(__file__).resolve().parents[2]
            config_path = root / "state" / "panop" / "panop_config.json"
            if config_path.exists():
                with open(config_path, "r", encoding="utf-8") as f:
                    self._panop_config = json.load(f)
                    self._categories_list = self._panop_config.get("categories", [])
        except Exception:
            pass
        if not self._categories_list:
            self._categories_list = [
                {"id": "articles", "name": "Articles", "dest_folder": "Android Articles"},
                {"id": "books", "name": "Books", "dest_folder": "Android Books"},
                {"id": "science_news", "name": "Science News", "dest_folder": "Android Science News"},
                {"id": "science_longform", "name": "Science Longform (read-in-place)", "dest_folder": "Android Science Longform"}
            ]
            self._panop_config = {"categories": self._categories_list}
        self._phone_tab.populate_categories(self._categories_list)
        self._categories_tab.load_config(self._panop_config or {"categories": self._categories_list})

    def refresh(self) -> None:
        self._queue_tab.refresh()
        self.refresh_status()
        self.refresh_history()
        self.refresh_config()
        self.refresh_env()

    def _on_timer_timeout(self) -> None:
        self.refresh_status()
        self._queue_tab.refresh()

    def refresh_config(self) -> None:
        t = _spawn_http(self, "GET", f"{_PANOP_BASE}/config", self._on_config_result, timeout=10.0)
        self._threads.append(t)
        t.finished.connect(lambda: self._threads.remove(t) if t in self._threads else None)

    def _on_config_result(self, result: dict) -> None:
        if not result or not result.get("ok"):
            return
        config = result.get("data") or {}
        if not isinstance(config, dict):
            return
        cats = config.get("categories") or []
        if not isinstance(cats, list):
            cats = []
        self._panop_config = config
        self._categories_list = cats
        self._phone_tab.populate_categories(self._categories_list)
        self._categories_tab.load_config(config)

    def refresh_env(self) -> None:
        t = _spawn_http(self, "GET", f"{_PANOP_BASE}/env", self._on_env_result, timeout=6.0)
        self._threads.append(t)
        t.finished.connect(lambda: self._threads.remove(t) if t in self._threads else None)

    def _on_env_result(self, result: dict) -> None:
        if not result or not result.get("ok"):
            return
        env = result.get("data") or {}
        if not isinstance(env, dict):
            return
        self._env_config = env
        defaults = {
            "require_manual_vetting_before_close": True,
            "close_tabs_after_save": False,
            "enable_autonomous_sweep": False,
            "resolve_terminal_redirects": True,
        }
        for key, chk in self._env_checks.items():
            chk.blockSignals(True)
            chk.setChecked(bool(env.get(key, defaults.get(key, False))))
            chk.blockSignals(False)

    def _set_env_flag(self, key: str, checked: bool) -> None:
        if key == "close_tabs_after_save" and checked and self._env_config.get("require_manual_vetting_before_close", True):
            self._status_lbl.setText("Auto-close is still blocked while manual vetting is required.")
        self._env_config[key] = bool(checked)
        self._status_lbl.setText(f"Updating safety setting: {key} = {bool(checked)}")

        def callback(res):
            if res and res.get("ok"):
                body = res.get("data")
                if isinstance(body, dict) and isinstance(body.get("env"), dict):
                    self._on_env_result({"ok": True, "data": body["env"]})
                self._status_lbl.setText("Safety settings updated.")
            else:
                self._status_lbl.setText(f"Failed to update safety setting: {(res or {}).get('error', 'no response')}")
                QTimer.singleShot(500, self.refresh_env)

        t = _spawn_http(self, "POST", f"{_PANOP_BASE}/env", callback, json_body={key: bool(checked)}, timeout=8.0)
        self._threads.append(t)
        t.finished.connect(lambda: self._threads.remove(t) if t in self._threads else None)

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
        chrome = bool(d.get("chrome_running"))
        sweep = bool(d.get("running"))
        tabs = d.get("tabs_seen") if d.get("tabs_seen") is not None else "-"
        matched = d.get("tabs_matched") if d.get("tabs_matched") is not None else "-"
        bmarks = d.get("bookmarks_pending") if d.get("bookmarks_pending") is not None else "-"
        last_sweep = d.get("last_run") if d.get("last_run") is not None else "-"
        last_fetch = d.get("last_tab_fetch_at") if d.get("last_tab_fetch_at") is not None else "-"

        # Sync the diagnostics panel with the live ADB status
        if not adb:
            if self._diag_detail.text().startswith("No diagnostics") or "successfully" in self._diag_detail.text():
                if last_fetch != "-":
                    msg = (
                        f"Phone is not currently connected. Last capture is preserved: "
                        f"{tabs} tabs seen, {matched} matched at {last_fetch}. "
                        "Click Auto Connect Phone or plug in the phone with USB debugging authorized."
                    )
                else:
                    msg = "Phone is not connected. Click Auto Connect Phone, or plug in the phone with USB debugging authorized and Egon will remember it."
                self._update_diagnostics_display("warning", msg, False)
        else:
            dev_id = d.get("device_id") or "Connected Device"
            if chrome:
                self._update_diagnostics_display("ok", f"Phone and Chrome are connected.\nDevice ID: {dev_id}\n\nYou are ready to load, sweep, or drain tabs.", True)
            else:
                self._update_diagnostics_display(
                    "warning",
                    f"Phone is connected, but Android Chrome tabs are not visible yet.\n"
                    f"Device ID: {dev_id}\n\nOpen Chrome on the phone or click Auto Connect Phone again; Egon will rebuild the DevTools bridge.",
                    False,
                )

        try:
            bmarks_is_zero = int(bmarks) == 0
        except (ValueError, TypeError):
            bmarks_is_zero = False

        if sweep:
            self._status_lbl.setText(f"Sweep running: {tabs} targets seen, {matched} matched so far.")
        elif d.get("last_error"):
            self._status_lbl.setText(f"Last sweep failed: {d.get('last_error')}")
        elif last_sweep != "-":
            self._status_lbl.setText(f"Last sweep finished: {tabs} targets seen, {matched} matched.")

        for label, val, ok in [
            ("ADB Connected",      "Yes" if adb else "No",         adb),
            ("Chrome Running",     "Yes" if chrome else "No",      chrome),
            ("Sweep Running",      "Yes" if sweep else "No",       sweep),
            ("Last Capture",       str(tabs),                      last_fetch != "-"),
            ("Matched",            str(matched),                   True),
            ("Bookmarks Pending",  str(bmarks),                    bmarks_is_zero),
            ("Last Sweep",         str(last_sweep),                last_sweep != "-"),
        ]:
            self._pills_row.addWidget(_pill(label, val, ok))
        self._pills_row.addStretch(1)

    # -- phone tabs inspect -------------------------------------------------
    def refresh_phone_tabs(self, *_args) -> None:
        if self._phone_tabs_loading:
            return
        self._phone_tabs_loading = True
        self._status_lbl.setText("Classifying phone tabs before sweep...")
        t = _spawn_http(
            self,
            "GET",
            f"{_PANOP_BASE}/tabs/inspect?wake=false&view=all&limit=0",
            self._on_phone_tabs_result,
            timeout=180.0,
        )
        self._threads.append(t)
        t.finished.connect(lambda: self._threads.remove(t) if t in self._threads else None)

    def _on_phone_tabs_result(self, result: dict) -> None:
        self._phone_tabs_loading = False
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
        total = data_dict.get("total")
        buckets = data_dict.get("buckets") if isinstance(data_dict.get("buckets"), dict) else {}
        matched = buckets.get("matched", 0)
        body_required = buckets.get("body_required", 0)
        saved = buckets.get("saved", 0)
        no_match = buckets.get("no_match", 0)
        ready = int(matched or 0) + int(body_required or 0)
        if data_dict.get("snapshot"):
            snapshot_at = data_dict.get("snapshot_at") or "previous run"
            self._status_lbl.setText(
                f"Classified {total or len(tabs)} tabs from last capture ({snapshot_at}); "
                f"showing {ready} ready for sweep by default ({saved} already saved, {no_match} unmatched hidden). Reconnect phone for live refresh."
            )
        else:
            self._status_lbl.setText(
                f"Classified {total or len(tabs)} phone tabs; showing {ready} ready for sweep by default "
                f"({saved} already saved, {no_match} unmatched hidden). Woken: {woken}."
            )
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


    def _pair_phone_action(self, *_args) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Pair Phone (ADB)")
        dialog.setModal(True)
        dialog.setStyleSheet("QDialog { background: #09090B; color: #E4E4E7; }")

        layout = QVBoxLayout(dialog)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)

        host_input = QLineEdit()
        host_input.setPlaceholderText("192.168.0.2")
        host_input.setStyleSheet(_INPUT_QSS)

        port_input = QLineEdit()
        port_input.setPlaceholderText("Pairing port")
        port_input.setStyleSheet(_INPUT_QSS)

        code_input = QLineEdit()
        code_input.setPlaceholderText("Pairing code")
        code_input.setStyleSheet(_INPUT_QSS)

        form.addRow("Host", host_input)
        form.addRow("Port", port_input)
        form.addRow("Code", code_input)
        layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        if dialog.exec() != QDialog.Accepted:
            self._status_lbl.setText("Pair Phone cancelled")
            return

        host = host_input.text().strip()
        port_text = port_input.text().strip()
        code = code_input.text().strip()
        if not host or not port_text or not code:
            self._status_lbl.setText("Pair Phone: host, port and code are all required")
            self._update_diagnostics_display("error", "Pair Phone requires host, pairing port, and pairing code.", False)
            return
        try:
            port = int(port_text)
        except ValueError:
            self._status_lbl.setText("Pair Phone: port must be numeric")
            self._update_diagnostics_display("error", "Pair Phone port must be numeric.", False)
            return

        self._status_lbl.setText("Pairing phone...")
        query = urlencode({"host": host, "port": port, "code": code})
        url = f"{_PANOP_BASE}/phone/pair?{query}"
        timeout = _ACTION_TIMEOUTS.get("Pair Phone", 25.0)
        t = _spawn_http(self, "POST", url,
                        lambda res: self._on_action_result("Pair Phone", res),
                        timeout=timeout)
        self._threads.append(t)
        t.finished.connect(lambda: self._threads.remove(t) if t in self._threads else None)

    # -- action button factory ---------------------------------------------
    def _make_action_handler(self, label: str, method: str, path: str):
        def handler(*_args):
            self._status_lbl.setText(f"⏳  {label}…")
            url = f"{_PANOP_BASE}{path}"
            timeout = _ACTION_TIMEOUTS.get(label, 8.0 if method.upper() == "POST" else 3.0)
            t = _spawn_http(self, method, url,
                            lambda res, _l=label: self._on_action_result(_l, res),
                            timeout=timeout)
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
                if label in ("Check Connection", "Diagnose USB", "Phone Reconnect", "Auto Connect Phone"):
                    status, msg, connected = _summarize_connection_status(body)
                elif label == "Drain All Tabs":
                    status = str(body.get("status") or "ok")
                    msg = "Drain loop started. Saving and closing phone tabs..."
                elif "message" in body and body["message"] is not None:
                    status = str(body.get("status") or "ok")
                    msg = str(body["message"])
                elif "status" in body and body["status"] is not None:
                    status = str(body.get("status") or "ok")
                    msg = f"Status: {body['status']}"
                else:
                    status = str(body.get("status") or "ok")
                    visible = {k: v for k, v in body.items() if k not in {"last_tab_urls", "tabs"}}
                    msg = ", ".join(f"{k}: {v}" for k, v in visible.items())
                if label not in ("Check Connection", "Diagnose USB", "Phone Reconnect", "Auto Connect Phone"):
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

            if label in ("Diagnose USB", "Check Connection", "Phone Reconnect", "Auto Connect Phone"):
                self._update_diagnostics_display(status, msg, connected)
                if connected:
                    QTimer.singleShot(500, self.refresh_status)
                    QTimer.singleShot(900, lambda: self._tabs.setCurrentIndex(1))
                    QTimer.singleShot(1000, self.refresh_phone_tabs)
        else:
            err = result.get("error") or "unknown error"
            self._status_lbl.setText(f"❌  {label}: {err}")
            if label in ("Diagnose USB", "Check Connection", "Phone Reconnect", "Auto Connect Phone"):
                body = result.get("data") if isinstance(result, dict) else None
                msg = f"Connection failed: {err}"
                if isinstance(body, dict):
                    details = body.get("message") or body.get("hint") or body.get("connect_log")
                    if details:
                        msg = str(details)
                self._update_diagnostics_display("error", msg, False)

    def _fetch_now_action(self) -> None:
        self._status_lbl.setText("⏳ Fetching now (running sweep on server)...")

        def callback(res):
            res = res if isinstance(res, dict) else {}
            if res.get("ok"):
                self._status_lbl.setText("Sweep triggered. Waiting for completion before loading the phone queue...")
                QTimer.singleShot(1200, lambda: self._poll_sweep_after_fetch(0))
            else:
                err = str(res.get("error") or "Unknown error")
                self._status_lbl.setText(f"❌ Fetch trigger failed: {err}")

        t = _spawn_http(self, "POST", f"{_PANOP_BASE}/fetch_now", callback)
        self._threads.append(t)
        t.finished.connect(lambda: self._threads.remove(t) if t in self._threads else None)

    def _fetch_restricted_action(self) -> None:
        """Restrict the routine to Articles/Books/Science News, then fetch. Only
        those three get saved + closed; everything else stays open. Bruno 2026-06-18."""
        self._status_lbl.setText("⏳ Restricting to Articles / Books / Science News, then fetching…")

        def after_restrict(_res):
            # Whatever the restrict call returns, kick off the (now-limited) sweep.
            self._fetch_now_action()

        t = _spawn_http(self, "POST",
                        f"{_PANOP_BASE}/restrict_categories?categories=articles,books,science_news",
                        after_restrict)
        self._threads.append(t)
        t.finished.connect(lambda: self._threads.remove(t) if t in self._threads else None)

    def _poll_sweep_after_fetch(self, attempt: int = 0) -> None:
        def callback(res):
            res = res if isinstance(res, dict) else {}
            body = res.get("data") if isinstance(res.get("data"), dict) else {}
            if not res.get("ok"):
                if attempt < 8:
                    QTimer.singleShot(2000, lambda: self._poll_sweep_after_fetch(attempt + 1))
                else:
                    self._status_lbl.setText("Sweep status did not answer; refresh manually if the queue stays empty.")
                return

            running = bool(body.get("running"))
            tabs_seen = body.get("tabs_seen", 0)
            matched = body.get("tabs_matched", 0)
            if running and attempt < 90:
                self._status_lbl.setText(
                    f"Sweep still running... {tabs_seen} tabs seen, {matched} matched so far."
                )
                QTimer.singleShot(2000, lambda: self._poll_sweep_after_fetch(attempt + 1))
                return

            if running:
                self._status_lbl.setText(
                    "Sweep is still running after 3 minutes. You can keep working; the queue will load when refreshed."
                )
                self.refresh()
                return

            self._status_lbl.setText(
                f"Sweep finished: {tabs_seen} tabs seen, {matched} matched. Loading the actionable phone queue..."
            )
            self.refresh()
            self.refresh_phone_tabs()

        t = _spawn_http(self, "GET", f"{_PANOP_BASE}/status", callback, timeout=8.0)
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

        html_msg = html.escape(str(message or "")).replace("\n", "<br/>")
        html_msg = re.sub(r"(\d+\.)", r"<b>\1</b>", html_msg)
        self._diag_detail.setText(html_msg)
