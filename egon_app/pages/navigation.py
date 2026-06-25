"""Navigation page — Routster dashboard for link routing & capture."""
from __future__ import annotations

import os
import json
import webbrowser
import threading
from datetime import datetime

from PySide6.QtCore import Qt, QTimer, QThread, Signal, QObject
from PySide6.QtGui import QColor, QBrush
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTabWidget,
    QTableWidget, QTableWidgetItem, QPushButton, QHeaderView,
    QMessageBox, QLineEdit, QFileDialog, QFrame, QGridLayout,
    QComboBox, QSizePolicy, QSlider, QCheckBox, QStackedWidget,
    QListWidget, QGroupBox, QTextEdit, QDialog, QFormLayout,
    QDialogButtonBox, QAbstractItemView, QSpinBox
)

from lib.adapters import routster

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
    font-size: 11px;
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
    font-size: 11px;
}
QHeaderView::section {
    background: #09090B;
    color: #EF4444;
    padding: 6px;
    border: none;
    border-bottom: 1px solid #27272A;
    font-weight: 600;
    font-size: 11px;
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
    font-size: 11px;
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
    font-size: 11px;
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

_LIST_QSS = """
QListWidget {
    background: #09090B;
    color: #A1A1AA;
    border: 1px solid #27272A;
    border-radius: 6px;
    padding: 4px;
}
QListWidget::item {
    padding: 8px 10px;
    border-radius: 4px;
    margin-bottom: 2px;
    font-family: 'Segoe UI', sans-serif;
    font-size: 11px;
}
QListWidget::item:selected {
    background: #18181B;
    color: #EF4444;
    font-weight: 600;
}
QListWidget::item:hover:!selected {
    background: #18181B;
    color: #F4F4F5;
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

def _ts(val) -> str:
    """Convert a unix-epoch int to readable string, or '—'."""
    try:
        ts = int(val)
        if ts > 0:
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        pass
    return "—"

def _pill(label: str, val, ok: bool) -> QLabel:
    color_dot = _COLOR_GREEN if ok else _COLOR_RED
    lbl = QLabel()
    lbl.setTextFormat(Qt.RichText)
    lbl.setStyleSheet(
        "background: #09090B; padding: 4px 8px; border-radius: 10px; "
        "color: #F4F4F5; border: 1px solid #27272A; font-size: 10px;"
    )
    lbl.setText(f"<span style='color:{color_dot};'>●</span>  <b>{val}</b>  {label}")
    return lbl

def _make_table(columns: list[tuple[str, int]]) -> QTableWidget:
    """Factory for a themed table widget."""
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
    tbl.setSelectionMode(QTableWidget.ExtendedSelection)  # Enable multi-select
    tbl.setSortingEnabled(True)  # Enable header sorting
    tbl.setStyleSheet(_TABLE_QSS)
    return tbl

# ---------------------------------------------------------------------------
# Background HTTP workers (QThread)
# ---------------------------------------------------------------------------
class _HttpWorker(QObject):
    """Runs a single httpx request off the main thread."""
    finished = Signal(dict)  # {"ok": bool, "data": ..., "error": str}

    def __init__(self, method: str, url: str, json_body: dict = None, files: dict = None, timeout: float = 10.0):
        super().__init__()
        self._method = method
        self._url = url
        self._json_body = json_body
        self._files = files
        self._timeout = timeout

    def run(self) -> None:
        try:
            import httpx
            with httpx.Client(timeout=self._timeout) as client:
                if self._method.upper() == "GET":
                    r = client.get(self._url)
                elif self._method.upper() == "DELETE":
                    r = client.delete(self._url)
                elif self._method.upper() == "PUT":
                    r = client.put(self._url, json=self._json_body)
                elif self._method.upper() == "PATCH":
                    r = client.patch(self._url, json=self._json_body)
                else:  # POST
                    if self._files:
                        # For files, json_body contains data fields
                        r = client.post(self._url, data=self._json_body, files=self._files)
                    else:
                        r = client.post(self._url, json=self._json_body)
                
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

def _spawn_http(parent: QWidget, method: str, url: str, callback,
                json_body: dict = None, files: dict = None, timeout: float = 10.0) -> QThread:
    """Fire an HTTP request in a QThread. *callback(result_dict)* is called on the main thread when done."""
    thread = QThread(parent)
    worker = _HttpWorker(method, url, json_body, files, timeout)
    worker.moveToThread(thread)
    
    # Store reference on the thread object to prevent garbage collection
    thread._worker = worker
    
    thread.started.connect(worker.run)
    worker.finished.connect(callback)
    worker.finished.connect(thread.quit)
    worker.finished.connect(worker.deleteLater)
    thread.finished.connect(thread.deleteLater)
    thread.start()
    return thread

# ---------------------------------------------------------------------------
# Custom Dialog for Editing Link details
# ---------------------------------------------------------------------------
class EditLinkDialog(QDialog):
    def __init__(self, parent, title: str, url: str):
        super().__init__(parent)
        self.setWindowTitle("Edit Link Details")
        self.setStyleSheet("""
            QDialog { background: #09090B; border: 1px solid #27272A; border-radius: 6px; }
            QLabel { color: #A1A1AA; font-size: 11px; font-family: 'Segoe UI', sans-serif; }
        """)
        
        layout = QVBoxLayout(self)
        form = QFormLayout()
        
        self.title_input = QLineEdit(title)
        self.title_input.setStyleSheet(_INPUT_QSS)
        self.title_input.setMinimumWidth(380)
        
        self.url_input = QLineEdit(url)
        self.url_input.setStyleSheet(_INPUT_QSS)
        self.url_input.setMinimumWidth(380)
        
        form.addRow("Title:", self.title_input)
        form.addRow("URL:", self.url_input)
        layout.addLayout(form)
        
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        
        # Style buttons
        ok_btn = buttons.button(QDialogButtonBox.Ok)
        ok_btn.setStyleSheet("""
            QPushButton { background: #EF4444; color: white; padding: 4px 10px; border-radius: 4px; font-weight: 600; font-size: 11px; border: none; }
            QPushButton:hover { background: #B91C1C; }
        """)
        cancel_btn = buttons.button(QDialogButtonBox.Cancel)
        cancel_btn.setStyleSheet("""
            QPushButton { background: #18181B; color: #E4E4E7; padding: 4px 10px; border-radius: 4px; font-weight: 600; font-size: 11px; border: 1px solid #27272A; }
            QPushButton:hover { background: #27272A; }
        """)
        
        layout.addWidget(buttons)

# ---------------------------------------------------------------------------
# Navigation Page Widget
# ---------------------------------------------------------------------------
class NavigationPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background-color: #030303;")
        self._threads: list[QThread] = []
        
        # Cache containers
        self._raw_inbox_links: list[dict] = []
        self._raw_routes: list[dict] = []
        self._raw_logs: list[dict] = []
        self._raw_unsorted: list[dict] = []
        self._categories_list: list[str] = []
        self._settings_data: dict = {}
        self._selected_ids: set[str] = set()
        
        # Page layout
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(10)
        
        # Title
        title = QLabel("Navigation")
        title.setStyleSheet("font-size: 20px; font-weight: 700; color: #FFFFFF; font-family: 'Segoe UI', sans-serif;")
        root.addWidget(title)
        
        sub = QLabel(
            "Routster inside Egon — your link router. It watches captured "
            "links/bookmarks, auto-sorts them into categories via routing "
            "rules, and parks anything it can't place in the Unsorted Queue "
            "for you to triage. Tabs below: Inbox (the links queue), Active Flows, "
            "Action Logs, Deep Sweep, Unsorted, and System Settings."
        )
        sub.setStyleSheet("color: #A1A1AA; font-size: 11px; font-family: 'Segoe UI', sans-serif;")
        sub.setWordWrap(True)
        root.addWidget(sub)
        
        # Status pills row
        self._pills_row = QHBoxLayout()
        self._pills_row.setSpacing(8)
        root.addLayout(self._pills_row)
        
        # Status feedback label
        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet("color: #A1A1AA; font-size: 11px; font-family: 'Segoe UI', sans-serif;")
        self._status_lbl.setWordWrap(True)
        root.addWidget(self._status_lbl)
        
        # Main Tab Widget
        self._tabs = QTabWidget()
        self._tabs.setStyleSheet(_TAB_QSS)
        
        self._init_inbox_tab()
        self._init_flows_tab()
        self._init_logs_tab()
        self._init_sweep_tab()
        self._init_unsorted_tab()
        self._init_chrome_tabs_tab()
        self._init_settings_tab()
        
        self._tabs.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        root.addWidget(self._tabs, 1)
        
        # Load data & start QTimer
        self.refresh()
        self._timer = QTimer(self)
        self._timer.setInterval(20_000)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()

    # ---------------------------------------------------------------------------
    # Tab Initializers
    # ---------------------------------------------------------------------------
    
    def _init_inbox_tab(self) -> None:
        """Tab 1: Routster Inbox (Queue)"""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)
        
        # Ingestion QGroupBox
        ingest_box = QGroupBox("➕ Ingest Links / Notes / Files")
        ingest_box.setStyleSheet("""
            QGroupBox { border: 1px solid #27272A; border-left: 4px solid #EF4444; border-radius: 6px; margin-top: 6px; color: #FFFFFF; font-weight: bold; font-size: 11px; }
            QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 3px; }
        """)
        ingest_layout = QVBoxLayout(ingest_box)
        ingest_layout.setContentsMargins(10, 12, 10, 8)
        ingest_layout.setSpacing(6)
        
        self._ingest_text = QTextEdit()
        self._ingest_text.setPlaceholderText("Paste URL, academic DOI, or raw note text here... Or drag-and-drop a file.")
        self._ingest_text.setStyleSheet(_INPUT_QSS)
        self._ingest_text.setMaximumHeight(50)
        ingest_layout.addWidget(self._ingest_text)
        
        ingest_actions = QHBoxLayout()
        ingest_actions.setSpacing(6)
        
        self._chk_extract = QCheckBox("Extract & route individual links")
        self._chk_extract.setStyleSheet("QCheckBox { color: #A1A1AA; font-size: 11px; }")
        ingest_actions.addWidget(self._chk_extract)
        ingest_actions.addStretch(1)
        
        btn_pull_chrome = QPushButton("🔄 Pull from Chrome")
        btn_pull_chrome.setStyleSheet("""
            QPushButton { background: #18181B; color: #E4E4E7; border: 1px solid #27272A; border-radius: 4px; font-weight: 600; font-size: 10px; padding: 4px 10px; }
            QPushButton:hover { background: #27272A; color: white; }
        """)
        btn_pull_chrome.setFixedHeight(24)
        btn_pull_chrome.clicked.connect(self._pull_chrome_action)
        ingest_actions.addWidget(btn_pull_chrome)
        
        btn_choose_file = QPushButton("📁 Choose File")
        btn_choose_file.setStyleSheet("""
            QPushButton { background: #18181B; color: #E4E4E7; border: 1px solid #27272A; border-radius: 4px; font-weight: 600; font-size: 10px; padding: 4px 10px; }
            QPushButton:hover { background: #27272A; color: white; }
        """)
        btn_choose_file.setFixedHeight(24)
        btn_choose_file.clicked.connect(self._choose_file_action)
        ingest_actions.addWidget(btn_choose_file)
        
        btn_send_pipeline = QPushButton("🚀 Send to Pipeline")
        btn_send_pipeline.setStyleSheet("""
            QPushButton { background: #EF4444; color: white; border-radius: 4px; font-weight: 700; font-size: 10px; padding: 4px 12px; border: none; }
            QPushButton:hover { background: #B91C1C; }
        """)
        btn_send_pipeline.setFixedHeight(24)
        btn_send_pipeline.clicked.connect(self._send_pipeline_action)
        ingest_actions.addWidget(btn_send_pipeline)
        
        ingest_layout.addLayout(ingest_actions)
        layout.addWidget(ingest_box)
        
        # Toolbar Row 1: Filters
        filter_bar = QHBoxLayout()
        filter_bar.setSpacing(6)
        
        self._inbox_search = QLineEdit()
        self._inbox_search.setPlaceholderText("🔍 Search titles or URLs...")
        self._inbox_search.setStyleSheet(_INPUT_QSS)
        self._inbox_search.textChanged.connect(self._apply_inbox_filters)
        filter_bar.addWidget(self._inbox_search, 2)
        
        self._inbox_cat_filter = QComboBox()
        self._inbox_cat_filter.setStyleSheet(_COMBO_QSS)
        self._inbox_cat_filter.addItem("🔍 All Categories", "All")
        self._inbox_cat_filter.currentTextChanged.connect(self._apply_inbox_filters)
        filter_bar.addWidget(self._inbox_cat_filter, 1)
        
        self._inbox_source_filter = QComboBox()
        self._inbox_source_filter.setStyleSheet(_COMBO_QSS)
        self._inbox_source_filter.addItems(["🗂️ All Sources", "manual", "chrome-sync", "history-sweep", "bulk-extraction"])
        self._inbox_source_filter.currentTextChanged.connect(self._apply_inbox_filters)
        filter_bar.addWidget(self._inbox_source_filter, 1)
        
        conf_lbl_lay = QHBoxLayout()
        conf_lbl_lay.setSpacing(4)
        self._lbl_conf_val = QLabel("Min conf: 0%")
        self._lbl_conf_val.setStyleSheet("color: #A1A1AA; font-size: 10px;")
        
        self._inbox_conf_slider = QSlider(Qt.Horizontal)
        self._inbox_conf_slider.setRange(0, 100)
        self._inbox_conf_slider.setValue(0)
        self._inbox_conf_slider.setSingleStep(5)
        self._inbox_conf_slider.setMaximumWidth(120)
        self._inbox_conf_slider.valueChanged.connect(self._on_conf_slider_changed)
        conf_lbl_lay.addWidget(self._lbl_conf_val)
        conf_lbl_lay.addWidget(self._inbox_conf_slider)
        filter_bar.addLayout(conf_lbl_lay)
        
        layout.addLayout(filter_bar)
        
        # Toolbar Row 2: Mass actions
        mass_bar = QHBoxLayout()
        mass_bar.setSpacing(8)
        
        self._lbl_sel_count = QLabel("0 links selected")
        self._lbl_sel_count.setStyleSheet("color: #A1A1AA; font-size: 11px;")
        mass_bar.addWidget(self._lbl_sel_count)
        
        self._chk_select_all = QCheckBox("Select All")
        self._chk_select_all.setStyleSheet("QCheckBox { color: #A1A1AA; font-size: 11px; }")
        self._chk_select_all.stateChanged.connect(self._toggle_select_all)
        mass_bar.addWidget(self._chk_select_all)
        mass_bar.addStretch(1)
        
        self._mass_cat_combo = QComboBox()
        self._mass_cat_combo.setStyleSheet(_COMBO_QSS)
        self._mass_cat_combo.setPlaceholderText("🏷️ Match Category...")
        self._mass_cat_combo.currentIndexChanged.connect(self._mass_category_action)
        mass_bar.addWidget(self._mass_cat_combo)
        
        btn_mass_reclassify = QPushButton("🤖 Auto Re-classify")
        btn_mass_reclassify.setStyleSheet("""
            QPushButton { background: #18181B; color: #E4E4E7; border: 1px solid #27272A; border-radius: 4px; font-weight: 600; font-size: 10px; padding: 4px 10px; }
            QPushButton:hover { background: #27272A; color: white; }
        """)
        btn_mass_reclassify.setFixedHeight(24)
        btn_mass_reclassify.clicked.connect(self._mass_reclassify_action)
        mass_bar.addWidget(btn_mass_reclassify)
        
        btn_mass_delete = QPushButton("🗑️ Delete")
        btn_mass_delete.setStyleSheet("""
            QPushButton { background: #18181B; color: #EF4444; border: 1px solid #27272A; border-radius: 4px; font-weight: 600; font-size: 10px; padding: 4px 10px; }
            QPushButton:hover { background: #EF4444; color: white; }
        """)
        btn_mass_delete.setFixedHeight(24)
        btn_mass_delete.clicked.connect(self._mass_delete_action)
        mass_bar.addWidget(btn_mass_delete)
        
        self._btn_export = QPushButton("📤 Run Export Pipeline")
        self._btn_export.setStyleSheet("""
            QPushButton { background: #EF4444; color: white; border-radius: 4px; font-weight: 700; font-size: 10px; padding: 4px 12px; border: none; }
            QPushButton:hover { background: #B91C1C; }
        """)
        self._btn_export.setFixedHeight(24)
        self._btn_export.clicked.connect(self._export_queue_action)
        mass_bar.addWidget(self._btn_export)
        
        layout.addLayout(mass_bar)
        
        # Link Table
        self._inbox_table = _make_table([
            ("✓", 30), ("Type", 40), ("Title", 280), ("URL", 250),
            ("Integration/Category", 180), ("Confidence", 70), ("Actions", 60)
        ])
        # Allow checking columns
        self._inbox_table.itemChanged.connect(self._on_table_item_changed)
        self._inbox_table.itemDoubleClicked.connect(self._on_inbox_double_clicked)
        layout.addWidget(self._inbox_table, 1)
        
        self._tabs.addTab(tab, "📥 Inbox (Queue)")

    def _init_flows_tab(self) -> None:
        """Tab 2: Active Flows"""
        self._flows_table = _make_table([
            ("Category", 180), ("Order", 70), ("Connector", 200), ("Enabled", 80),
        ])
        self._tabs.addTab(self._flows_table, "⚡ Active Flows")

    def _init_logs_tab(self) -> None:
        """Tab 3: Action Logs"""
        self._logs_widget = QWidget()
        layout = QVBoxLayout(self._logs_widget)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)
        
        filter_bar = QHBoxLayout()
        filter_bar.setSpacing(6)
        
        self._logs_search = QLineEdit()
        self._logs_search.setPlaceholderText("🔍  Search logs (title, category, message)...")
        self._logs_search.setStyleSheet(_INPUT_QSS)
        self._logs_search.textChanged.connect(self._apply_logs_filters)
        filter_bar.addWidget(self._logs_search, 1)
        
        btn_clear = QPushButton("Clear")
        btn_clear.setStyleSheet("""
            QPushButton { background: #18181B; color: #E4E4E7; border: 1px solid #27272A; border-radius: 4px; font-size: 11px; padding: 4px 8px; }
            QPushButton:hover { background: #27272A; color: white; }
        """)
        btn_clear.setFixedHeight(24)
        btn_clear.clicked.connect(self._logs_search.clear)
        filter_bar.addWidget(btn_clear)
        layout.addLayout(filter_bar)
        
        self._logs_table = _make_table([
            ("Time", 130), ("Title", 250), ("Category", 120),
            ("Connector", 140), ("Message", 300),
        ])
        layout.addWidget(self._logs_table)
        self._tabs.addTab(self._logs_widget, "📜 Action Logs")

    def _init_sweep_tab(self) -> None:
        """Tab 4: Deep History Sweep"""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(10)
        
        title_lbl = QLabel("🧹 Deep History Sweep")
        title_lbl.setStyleSheet("font-size: 14px; font-weight: bold; color: #FFFFFF;")
        layout.addWidget(title_lbl)
        
        desc_lbl = QLabel(
            "Drop or select a Google Takeout History.json, raw Chrome History database file, or bookmark HTML file. "
            "Routster trains on your current bookmark folders to learn priority domains, runs the NLP classifier "
            "locally on your history, and stages confident priority matches in the Inbox queue for your review. "
            "Unsorted and Trash links are safely archived to the local SQLite database."
        )
        desc_lbl.setStyleSheet("color: #A1A1AA; font-size: 11px;")
        desc_lbl.setWordWrap(True)
        layout.addWidget(desc_lbl)
        
        # Threshold slider
        thresh_lay = QHBoxLayout()
        thresh_lay.setSpacing(8)
        self._lbl_threshold = QLabel("Wikipedia -> References strictness: 8")
        self._lbl_threshold.setStyleSheet("color: #F4F4F5; font-size: 11px;")
        
        self._sweep_threshold_slider = QSlider(Qt.Horizontal)
        self._sweep_threshold_slider.setRange(0, 20)
        self._sweep_threshold_slider.setValue(8)
        self._sweep_threshold_slider.setSingleStep(1)
        self._sweep_threshold_slider.setMaximumWidth(200)
        self._sweep_threshold_slider.valueChanged.connect(self._on_sweep_threshold_changed)
        
        thresh_lay.addWidget(self._lbl_threshold)
        thresh_lay.addWidget(self._sweep_threshold_slider)
        thresh_lay.addStretch(1)
        layout.addLayout(thresh_lay)
        
        # File selector panel
        file_box = QFrame()
        file_box.setStyleSheet("QFrame { background: #09090B; border: 1px solid #27272A; border-radius: 6px; padding: 20px; }")
        file_layout = QVBoxLayout(file_box)
        file_layout.setSpacing(10)
        file_layout.setAlignment(Qt.AlignCenter)
        
        self._lbl_sweep_filename = QLabel("Select a browser history or bookmarks export file:")
        self._lbl_sweep_filename.setStyleSheet("color: #F4F4F5; font-weight: bold; font-size: 11px;")
        file_layout.addWidget(self._lbl_sweep_filename)
        
        self._sweep_filepath = ""
        btn_choose = QPushButton("📂 Choose History File")
        btn_choose.setStyleSheet("""
            QPushButton { background: #EF4444; color: white; border-radius: 4px; font-weight: 700; font-size: 11px; padding: 6px 14px; border: none; }
            QPushButton:hover { background: #B91C1C; }
        """)
        btn_choose.setFixedSize(140, 28)
        btn_choose.clicked.connect(self._choose_sweep_file)
        file_layout.addWidget(btn_choose)
        
        layout.addWidget(file_box)
        
        # Execution control row
        exec_lay = QHBoxLayout()
        exec_lay.setSpacing(8)
        
        self._btn_run_sweep = QPushButton("🧹 Run Sweep")
        self._btn_run_sweep.setEnabled(False)
        self._btn_run_sweep.setStyleSheet("""
            QPushButton { background: #10B981; color: white; border-radius: 4px; font-weight: 700; font-size: 11px; padding: 6px 14px; border: none; }
            QPushButton:hover { background: #059669; }
            QPushButton:disabled { background: #1F2937; color: #4B5563; }
        """)
        self._btn_run_sweep.clicked.connect(self._run_sweep_action)
        exec_lay.addWidget(self._btn_run_sweep)
        
        btn_clear_sweep = QPushButton("♻️ Clear previous sweep results")
        btn_clear_sweep.setStyleSheet("""
            QPushButton { background: #18181B; color: #E4E4E7; border: 1px solid #27272A; border-radius: 4px; font-size: 11px; padding: 5px 12px; }
            QPushButton:hover { background: #27272A; color: white; }
        """)
        btn_clear_sweep.clicked.connect(self._clear_sweep_action)
        exec_lay.addWidget(btn_clear_sweep)
        exec_lay.addStretch(1)
        layout.addLayout(exec_lay)
        
        # Results frame
        self._sweep_result_box = QGroupBox("Sweep Results")
        self._sweep_result_box.setStyleSheet("""
            QGroupBox { border: 1px solid #27272A; border-radius: 6px; margin-top: 10px; color: #FFFFFF; font-weight: bold; font-size: 11px; }
            QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 3px; }
        """)
        self._sweep_result_box.setVisible(False)
        res_layout = QVBoxLayout(self._sweep_result_box)
        res_layout.setSpacing(6)
        
        self._lbl_sweep_summary = QLabel("")
        self._lbl_sweep_summary.setStyleSheet("color: #F4F4F5; font-size: 11px;")
        res_layout.addWidget(self._lbl_sweep_summary)
        
        self._lbl_sweep_buckets = QLabel("")
        self._lbl_sweep_buckets.setStyleSheet("color: #A1A1AA; font-size: 10px;")
        res_layout.addWidget(self._lbl_sweep_buckets)
        
        btn_view_queue = QPushButton("👀 Review staged links in Queue →")
        btn_view_queue.setStyleSheet("""
            QPushButton { background: #EF4444; color: white; border-radius: 4px; font-weight: bold; font-size: 11px; padding: 6px 12px; border: none; }
            QPushButton:hover { background: #B91C1C; }
        """)
        btn_view_queue.setFixedHeight(26)
        btn_view_queue.clicked.connect(lambda: self._tabs.setCurrentIndex(0))
        res_layout.addWidget(btn_view_queue)
        
        layout.addWidget(self._sweep_result_box)
        layout.addStretch(1)
        
        self._tabs.addTab(tab, "🧹 Deep Sweep")

    def _init_unsorted_tab(self) -> None:
        """Tab 5: Unsorted Queue"""
        self._unsorted_widget = QWidget()
        layout = QVBoxLayout(self._unsorted_widget)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)
        
        filter_bar = QHBoxLayout()
        filter_bar.setSpacing(6)
        
        self._unsorted_search = QLineEdit()
        self._unsorted_search.setPlaceholderText("🔍  Search backlog (title or URL)...")
        self._unsorted_search.setStyleSheet(_INPUT_QSS)
        self._unsorted_search.textChanged.connect(self._apply_unsorted_filters)
        filter_bar.addWidget(self._unsorted_search, 1)
        
        btn_clear = QPushButton("Clear")
        btn_clear.setStyleSheet("""
            QPushButton { background: #18181B; color: #E4E4E7; border: 1px solid #27272A; border-radius: 4px; font-size: 11px; padding: 4px 8px; }
            QPushButton:hover { background: #27272A; color: white; }
        """)
        btn_clear.setFixedHeight(24)
        btn_clear.clicked.connect(self._unsorted_search.clear)
        filter_bar.addWidget(btn_clear)
        layout.addLayout(filter_bar)
        
        self._unsorted_table = _make_table([
            ("Title", 250), ("URL", 320), ("Visits", 60),
            ("Last Visit", 130), ("Archived", 130),
        ])
        self._unsorted_table.itemDoubleClicked.connect(self._on_unsorted_double_clicked)
        layout.addWidget(self._unsorted_table)
        self._tabs.addTab(self._unsorted_widget, "🗃️ Unsorted")

    def _init_settings_tab(self) -> None:
        """Tab 6: System Settings"""
        tab = QWidget()
        layout = QHBoxLayout(tab)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(14)
        
        # Left side panel list
        self._settings_list = QListWidget()
        self._settings_list.setStyleSheet(_LIST_QSS)
        self._settings_list.setMaximumWidth(160)
        self._settings_list.addItems([
            "🎛️ General", "🧠 Classifier", "🔌 API & Webhooks",
            "⏱️ Triggers", "💾 Data & Storage", "🔧 Advanced", "ℹ️ About"
        ])
        self._settings_list.currentRowChanged.connect(self._on_settings_section_changed)
        layout.addWidget(self._settings_list)
        
        # Right side stacked widget
        self._settings_stack = QStackedWidget()
        self._settings_stack.setStyleSheet("background: #030303;")
        
        # 1. General Panel
        general_widget = QWidget()
        gen_lay = QFormLayout(general_widget)
        gen_lay.setContentsMargins(10, 10, 10, 10)
        
        title_gen = QLabel("🎛️ General Settings")
        title_gen.setStyleSheet("font-size: 13px; font-weight: bold; color: #EF4444; margin-bottom: 8px;")
        gen_lay.addRow(title_gen)
        
        self._set_default_cat = QLineEdit()
        self._set_default_cat.setStyleSheet(_INPUT_QSS)
        self._set_default_cat.editingFinished.connect(
            lambda: self._change_setting("general", "defaultCategory", self._set_default_cat.text())
        )
        gen_lay.addRow("Default Category:", self._set_default_cat)
        self._settings_stack.addWidget(general_widget)
        
        # 2. Classifier Panel
        classifier_widget = QWidget()
        class_lay = QFormLayout(classifier_widget)
        class_lay.setContentsMargins(10, 10, 10, 10)
        
        title_class = QLabel("🧠 Classifier Engine")
        title_class.setStyleSheet("font-size: 13px; font-weight: bold; color: #EF4444; margin-bottom: 8px;")
        class_lay.addRow(title_class)
        
        conf_lay = QHBoxLayout()
        self._set_conf_slider = QSlider(Qt.Horizontal)
        self._set_conf_slider.setRange(0, 100)
        self._set_conf_slider.setSingleStep(5)
        self._set_conf_slider.setMaximumWidth(180)
        self._lbl_set_conf = QLabel("45%")
        self._lbl_set_conf.setStyleSheet("color: #EF4444; font-weight: bold;")
        self._set_conf_slider.valueChanged.connect(self._on_settings_conf_changed)
        self._set_conf_slider.sliderReleased.connect(
            lambda: self._change_setting("classifier", "confidenceThreshold", self._set_conf_slider.value())
        )
        conf_lay.addWidget(self._set_conf_slider)
        conf_lay.addWidget(self._lbl_set_conf)
        conf_lay.addStretch(1)
        class_lay.addRow("Confidence Threshold:", conf_lay)
        
        self._set_fallback = QComboBox()
        self._set_fallback.setStyleSheet(_COMBO_QSS)
        self._set_fallback.addItem("Mark as 'Uncategorized'", "uncategorized")
        self._set_fallback.addItem("Assign to first eligible category", "first")
        self._set_fallback.addItem("Ask user (manual review)", "ask")
        self._set_fallback.currentIndexChanged.connect(
            lambda idx: self._change_setting("classifier", "fallbackBehavior", self._set_fallback.currentData())
        )
        class_lay.addRow("Fallback Behavior:", self._set_fallback)
        
        self._chk_keyword_hints = QCheckBox("Extract hints from filenames")
        self._chk_keyword_hints.setStyleSheet("QCheckBox { color: #A1A1AA; font-size: 11px; }")
        self._chk_keyword_hints.stateChanged.connect(
            lambda state: self._change_setting("classifier", "enableFilenameHints", state == Qt.Checked)
        )
        class_lay.addRow("Keyword Hints:", self._chk_keyword_hints)
        
        self._chk_adaptive_learning = QCheckBox("Learn from manual corrections")
        self._chk_adaptive_learning.setStyleSheet("QCheckBox { color: #A1A1AA; font-size: 11px; }")
        self._chk_adaptive_learning.stateChanged.connect(
            lambda state: self._change_setting("classifier", "enableAdaptiveLearning", state == Qt.Checked)
        )
        class_lay.addRow("Adaptive Learning:", self._chk_adaptive_learning)
        self._settings_stack.addWidget(classifier_widget)
        
        # 3. API Panel
        api_widget = QWidget()
        api_lay = QFormLayout(api_widget)
        api_lay.setContentsMargins(10, 10, 10, 10)
        
        title_api = QLabel("🔌 API & Webhooks")
        title_api.setStyleSheet("font-size: 13px; font-weight: bold; color: #EF4444; margin-bottom: 8px;")
        api_lay.addRow(title_api)
        
        self._set_webhook_url = QLineEdit()
        self._set_webhook_url.setStyleSheet(_INPUT_QSS)
        self._set_webhook_url.setReadOnly(True)
        api_lay.addRow("Webhook Endpoint:", self._set_webhook_url)
        
        self._set_api_secret = QLineEdit()
        self._set_api_secret.setEchoMode(QLineEdit.Password)
        self._set_api_secret.setStyleSheet(_INPUT_QSS)
        self._set_api_secret.editingFinished.connect(
            lambda: self._change_setting("api", "apiSecret", self._set_api_secret.text())
        )
        api_lay.addRow("API Secret Token:", self._set_api_secret)
        
        self._set_cors_origins = QLineEdit()
        self._set_cors_origins.setStyleSheet(_INPUT_QSS)
        self._set_cors_origins.editingFinished.connect(
            lambda: self._change_setting("api", "allowedOrigins", self._set_cors_origins.text())
        )
        api_lay.addRow("Allowed CORS Origins:", self._set_cors_origins)
        
        self._chk_auto_classify_web = QCheckBox("Auto-classify webhook items")
        self._chk_auto_classify_web.setStyleSheet("QCheckBox { color: #A1A1AA; font-size: 11px; }")
        self._chk_auto_classify_web.stateChanged.connect(
            lambda state: self._change_setting("api", "autoClassifyWebhook", state == Qt.Checked)
        )
        api_lay.addRow("Auto-classification:", self._chk_auto_classify_web)
        self._settings_stack.addWidget(api_widget)
        
        # 4. Triggers Panel
        triggers_widget = QWidget()
        trig_lay = QFormLayout(triggers_widget)
        trig_lay.setContentsMargins(10, 10, 10, 10)
        
        title_trig = QLabel("⏱️ Automation Triggers")
        title_trig.setStyleSheet("font-size: 13px; font-weight: bold; color: #EF4444; margin-bottom: 8px;")
        trig_lay.addRow(title_trig)
        
        self._set_poll_interval = QSpinBox()
        self._set_poll_interval.setRange(30, 3600)
        self._set_poll_interval.setStyleSheet("""
            QSpinBox { background: #09090B; color: #F4F4F5; border: 1px solid #27272A; border-radius: 6px; padding: 4px 8px; }
        """)
        self._set_poll_interval.valueChanged.connect(
            lambda val: self._change_setting("triggers", "pollingInterval", val)
        )
        trig_lay.addRow("Polling Interval (seconds):", self._set_poll_interval)
        self._settings_stack.addWidget(triggers_widget)
        
        # 5. Data & Storage Panel
        data_widget = QWidget()
        data_lay = QFormLayout(data_widget)
        data_lay.setContentsMargins(10, 10, 10, 10)
        
        title_data = QLabel("💾 Data & Storage")
        title_data.setStyleSheet("font-size: 13px; font-weight: bold; color: #EF4444; margin-bottom: 8px;")
        data_lay.addRow(title_data)
        
        self._set_db_path = QLineEdit()
        self._set_db_path.setStyleSheet(_INPUT_QSS)
        self._set_db_path.setReadOnly(True)
        data_lay.addRow("Database Path:", self._set_db_path)
        
        self._lbl_stats = QLabel("Inbox Links: — · Active Routes: — · Learned Rules: —")
        self._lbl_stats.setStyleSheet("color: #A1A1AA; font-size: 11px; font-weight: 600; margin-top: 10px; margin-bottom: 10px;")
        data_lay.addRow(self._lbl_stats)
        
        db_actions = QHBoxLayout()
        db_actions.setSpacing(6)
        
        btn_backup = QPushButton("📦 Export Database")
        btn_backup.setStyleSheet("""
            QPushButton { background: #10B981; color: white; border-radius: 4px; font-weight: 600; font-size: 10px; padding: 4px 10px; border: none; }
            QPushButton:hover { background: #059669; }
        """)
        btn_backup.setFixedHeight(24)
        btn_backup.clicked.connect(self._export_database)
        db_actions.addWidget(btn_backup)
        
        btn_clear_links = QPushButton("🗑️ Clear Links")
        btn_clear_links.setStyleSheet("""
            QPushButton { background: #18181B; color: #EF4444; border: 1px solid #27272A; border-radius: 4px; font-size: 10px; padding: 4px 10px; }
            QPushButton:hover { background: #27272A; }
        """)
        btn_clear_links.setFixedHeight(24)
        btn_clear_links.clicked.connect(lambda: self._clear_data_action("links"))
        db_actions.addWidget(btn_clear_links)
        
        btn_clear_rules = QPushButton("🧹 Reset Learned Rules")
        btn_clear_rules.setStyleSheet("""
            QPushButton { background: #18181B; color: #EF4444; border: 1px solid #27272A; border-radius: 4px; font-size: 10px; padding: 4px 10px; }
            QPushButton:hover { background: #27272A; }
        """)
        btn_clear_rules.setFixedHeight(24)
        btn_clear_rules.clicked.connect(lambda: self._clear_data_action("learned_rules"))
        db_actions.addWidget(btn_clear_rules)
        
        btn_clear_routes = QPushButton("⚠️ Clear Routes")
        btn_clear_routes.setStyleSheet("""
            QPushButton { background: #18181B; color: #EF4444; border: 1px solid #27272A; border-radius: 4px; font-size: 10px; padding: 4px 10px; }
            QPushButton:hover { background: #27272A; }
        """)
        btn_clear_routes.setFixedHeight(24)
        btn_clear_routes.clicked.connect(lambda: self._clear_data_action("routes"))
        db_actions.addWidget(btn_clear_routes)
        db_actions.addStretch(1)
        
        data_lay.addRow("Database Controls:", db_actions)
        
        recovery_lbl = QLabel("🔌 Recovery Actions")
        recovery_lbl.setStyleSheet("font-size: 11px; font-weight: bold; color: #F4F4F5; margin-top: 14px; margin-bottom: 4px;")
        data_lay.addRow(recovery_lbl)
        
        rec_actions = QHBoxLayout()
        rec_actions.setSpacing(6)
        
        btn_rec_failed = QPushButton("🔌 Recover Failed Exports")
        btn_rec_failed.setStyleSheet("""
            QPushButton { background: #18181B; color: #E4E4E7; border: 1px solid #27272A; border-radius: 4px; font-size: 10px; padding: 4px 10px; }
            QPushButton:hover { background: #27272A; color: white; }
        """)
        btn_rec_failed.setFixedHeight(24)
        btn_rec_failed.clicked.connect(lambda: self._recovery_action("failed"))
        rec_actions.addWidget(btn_rec_failed)
        
        btn_rec_z = QPushButton("Zotero Recovery")
        btn_rec_z.setStyleSheet("""
            QPushButton { background: #18181B; color: #E4E4E7; border: 1px solid #27272A; border-radius: 4px; font-size: 10px; padding: 4px 10px; }
            QPushButton:hover { background: #27272A; color: white; }
        """)
        btn_rec_z.setFixedHeight(24)
        btn_rec_z.clicked.connect(lambda: self._recovery_action("zotero"))
        rec_actions.addWidget(btn_rec_z)
        
        btn_rec_i = QPushButton("Instapaper Recovery")
        btn_rec_i.setStyleSheet("""
            QPushButton { background: #18181B; color: #E4E4E7; border: 1px solid #27272A; border-radius: 4px; font-size: 10px; padding: 4px 10px; }
            QPushButton:hover { background: #27272A; color: white; }
        """)
        btn_rec_i.setFixedHeight(24)
        btn_rec_i.clicked.connect(lambda: self._recovery_action("instapaper"))
        rec_actions.addWidget(btn_rec_i)
        rec_actions.addStretch(1)
        
        data_lay.addRow("Pipeline Recovery:", rec_actions)
        self._settings_stack.addWidget(data_widget)
        
        # 6. Advanced Panel
        advanced_widget = QWidget()
        adv_lay = QFormLayout(advanced_widget)
        adv_lay.setContentsMargins(10, 10, 10, 10)
        
        title_adv = QLabel("🔧 Advanced Settings")
        title_adv.setStyleSheet("font-size: 13px; font-weight: bold; color: #EF4444; margin-bottom: 8px;")
        adv_lay.addRow(title_adv)
        
        self._set_port = QLineEdit()
        self._set_port.setStyleSheet(_INPUT_QSS)
        self._set_port.setReadOnly(True)
        adv_lay.addRow("Server Port:", self._set_port)
        
        self._set_trig_dir = QLineEdit("./triggers/")
        self._set_trig_dir.setStyleSheet(_INPUT_QSS)
        self._set_trig_dir.setReadOnly(True)
        adv_lay.addRow("Trigger Directory:", self._set_trig_dir)
        
        self._set_conn_dir = QLineEdit("./connectors/")
        self._set_conn_dir.setStyleSheet(_INPUT_QSS)
        self._set_conn_dir.setReadOnly(True)
        adv_lay.addRow("Connector Directory:", self._set_conn_dir)
        
        btn_reset_onb = QPushButton("🔄 Reset Onboarding State")
        btn_reset_onb.setStyleSheet("""
            QPushButton { background: #18181B; color: #E4E4E7; border: 1px solid #27272A; border-radius: 4px; font-size: 10px; padding: 5px 12px; }
            QPushButton:hover { background: #27272A; color: white; }
        """)
        btn_reset_onb.clicked.connect(self._reset_onboarding_action)
        adv_lay.addRow("Onboarding Setup:", btn_reset_onb)
        self._settings_stack.addWidget(advanced_widget)
        
        # 7. About Panel
        about_widget = QWidget()
        about_lay = QVBoxLayout(about_widget)
        about_lay.setContentsMargins(14, 14, 14, 14)
        about_lay.setSpacing(10)
        about_lay.setAlignment(Qt.AlignCenter)
        
        title_about = QLabel("Routster Engine")
        title_about.setStyleSheet("font-size: 20px; font-weight: 800; color: #EF4444;")
        about_lay.addWidget(title_about)
        
        self._lbl_version = QLabel("Version: 1.2.0 · MIT License")
        self._lbl_version.setStyleSheet("color: #F4F4F5; font-size: 11px;")
        about_lay.addWidget(self._lbl_version)
        
        lbl_about_desc = QLabel(
            "Routster is a local-first automation pipeline designed for categorising, "
            "enriching, and routing scientific bibliography, PDFs, web contents, and "
            "files directly into Zotero, Notion, Instapaper, Obsidian, and Google Drive.\n"
            "Developed natively for the personal KMS orchestration ecosystem."
        )
        lbl_about_desc.setStyleSheet("color: #A1A1AA; font-size: 11px; text-align: center;")
        lbl_about_desc.setWordWrap(True)
        about_lay.addWidget(lbl_about_desc)
        
        btn_github = QPushButton("⭐ GitHub Repository")
        btn_github.setStyleSheet("""
            QPushButton { background: #18181B; color: #E4E4E7; border: 1px solid #27272A; border-radius: 4px; font-size: 10px; padding: 5px 12px; }
            QPushButton:hover { background: #27272A; color: white; }
        """)
        btn_github.setFixedSize(140, 24)
        btn_github.clicked.connect(lambda: webbrowser.open("https://github.com/outdatedcaveman/routster"))
        about_lay.addWidget(btn_github)
        
        self._settings_stack.addWidget(about_widget)
        
        layout.addWidget(self._settings_stack, 1)
        self._settings_list.setCurrentRow(0)
        self._tabs.addTab(tab, "⚙️ Settings")

    def _init_chrome_tabs_tab(self) -> None:
        """🌐 Chrome Open Tabs Tab"""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)
        
        filter_bar = QHBoxLayout()
        filter_bar.setSpacing(6)
        
        self._chrome_tabs_search = QLineEdit()
        self._chrome_tabs_search.setPlaceholderText("🔍 Search open tabs...")
        self._chrome_tabs_search.setStyleSheet(_INPUT_QSS)
        self._chrome_tabs_search.textChanged.connect(self._apply_chrome_tabs_filters)
        filter_bar.addWidget(self._chrome_tabs_search, 1)
        
        btn_clear = QPushButton("Clear")
        btn_clear.setStyleSheet("""
            QPushButton { background: #18181B; color: #E4E4E7; border: 1px solid #27272A; border-radius: 4px; font-size: 11px; padding: 4px 8px; }
            QPushButton:hover { background: #27272A; color: white; }
        """)
        btn_clear.setFixedHeight(24)
        btn_clear.clicked.connect(self._chrome_tabs_search.clear)
        filter_bar.addWidget(btn_clear)
        
        btn_refresh = QPushButton("🔄 Refresh")
        btn_refresh.setStyleSheet("""
            QPushButton { background: #18181B; color: #E4E4E7; border: 1px solid #27272A; border-radius: 4px; font-size: 11px; padding: 4px 8px; }
            QPushButton:hover { background: #27272A; color: white; }
        """)
        btn_refresh.setFixedHeight(24)
        btn_refresh.clicked.connect(self._refresh_chrome_tabs)
        filter_bar.addWidget(btn_refresh)
        
        layout.addLayout(filter_bar)
        
        action_bar = QHBoxLayout()
        action_bar.setSpacing(8)
        
        self._lbl_chrome_sel_count = QLabel("0 tabs selected")
        self._lbl_chrome_sel_count.setStyleSheet("color: #A1A1AA; font-size: 11px;")
        action_bar.addWidget(self._lbl_chrome_sel_count)
        
        self._chk_chrome_select_all = QCheckBox("Select All")
        self._chk_chrome_select_all.setStyleSheet("QCheckBox { color: #A1A1AA; font-size: 11px; }")
        self._chk_chrome_select_all.stateChanged.connect(self._toggle_chrome_select_all)
        action_bar.addWidget(self._chk_chrome_select_all)
        action_bar.addStretch(1)
        
        btn_send_inbox = QPushButton("📥 Ingest Selected to Triage Queue")
        btn_send_inbox.setStyleSheet("""
            QPushButton { background: #EF4444; color: white; border-radius: 4px; font-weight: 700; font-size: 10px; padding: 4px 12px; border: none; }
            QPushButton:hover { background: #B91C1C; }
        """)
        btn_send_inbox.setFixedHeight(24)
        btn_send_inbox.clicked.connect(self._send_chrome_tabs_to_inbox)
        action_bar.addWidget(btn_send_inbox)
        
        layout.addLayout(action_bar)
        
        self._chrome_tabs_table = _make_table([
            ("✓", 30), ("Title", 350), ("URL", 300), ("Active", 60), ("Pinned", 60)
        ])
        self._chrome_tabs_table.itemChanged.connect(self._on_chrome_table_item_changed)
        layout.addWidget(self._chrome_tabs_table, 1)
        
        self._tabs.addTab(tab, "🌐 Chrome Open Tabs")
        self._raw_chrome_tabs = []
        self._selected_chrome_urls = set()

    def _refresh_chrome_tabs(self) -> None:
        try:
            from lib.adapters import chrome_tabs
            snap = chrome_tabs.snapshot()
            if snap.get("status") == "ok":
                self._raw_chrome_tabs = snap.get("items") or []
            else:
                self._raw_chrome_tabs = []
        except Exception:
            self._raw_chrome_tabs = []
        self._apply_chrome_tabs_filters()

    def _apply_chrome_tabs_filters(self) -> None:
        self._chrome_tabs_table.blockSignals(True)
        self._chrome_tabs_table.setSortingEnabled(False)
        self._chrome_tabs_table.setRowCount(0)
        
        search = self._chrome_tabs_search.text().strip().lower()
        
        filtered = []
        for t in self._raw_chrome_tabs:
            title = str(t.get("title", "")).lower()
            url = str(t.get("url", "")).lower()
            if search and (search not in title and search not in url):
                continue
            filtered.append(t)
            
        self._chrome_tabs_table.setRowCount(len(filtered))
        for row_idx, t in enumerate(filtered):
            # Checkbox column
            chk_item = QTableWidgetItem()
            chk_item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            url = t.get("url", "")
            is_checked = url in self._selected_chrome_urls
            chk_item.setCheckState(Qt.Checked if is_checked else Qt.Unchecked)
            self._chrome_tabs_table.setItem(row_idx, 0, chk_item)
            
            # Title
            title_item = QTableWidgetItem(t.get("title", "untitled"))
            self._chrome_tabs_table.setItem(row_idx, 1, title_item)
            
            # URL
            url_item = QTableWidgetItem(url)
            self._chrome_tabs_table.setItem(row_idx, 2, url_item)
            
            # Active
            active_item = QTableWidgetItem("Yes" if t.get("active") else "No")
            self._chrome_tabs_table.setItem(row_idx, 3, active_item)
            
            # Pinned
            pinned_item = QTableWidgetItem("Yes" if t.get("pinned") else "No")
            self._chrome_tabs_table.setItem(row_idx, 4, pinned_item)
            
        self._chrome_tabs_table.setSortingEnabled(True)
        self._chrome_tabs_table.blockSignals(False)
        self._update_chrome_selection_label()

    def _toggle_chrome_select_all(self, state: int) -> None:
        self._chrome_tabs_table.blockSignals(True)
        checked = (state == 2)  # Qt.Checked is 2
        for r in range(self._chrome_tabs_table.rowCount()):
            chk_item = self._chrome_tabs_table.item(r, 0)
            if chk_item:
                chk_item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
                url_item = self._chrome_tabs_table.item(r, 2)
                if url_item:
                    url = url_item.text()
                    if checked:
                        self._selected_chrome_urls.add(url)
                    else:
                        self._selected_chrome_urls.discard(url)
        self._chrome_tabs_table.blockSignals(False)
        self._update_chrome_selection_label()

    def _on_chrome_table_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() == 0:
            row = item.row()
            url_item = self._chrome_tabs_table.item(row, 2)
            if url_item:
                url = url_item.text()
                if item.checkState() == Qt.Checked:
                    self._selected_chrome_urls.add(url)
                else:
                    self._selected_chrome_urls.discard(url)
            self._update_chrome_selection_label()

    def _update_chrome_selection_label(self) -> None:
        self._lbl_chrome_sel_count.setText(f"{len(self._selected_chrome_urls)} tabs selected")

    def _send_chrome_tabs_to_inbox(self) -> None:
        if not self._selected_chrome_urls:
            QMessageBox.information(self, "No Selections", "Please select one or more Chrome tabs first.")
            return
            
        # Map URL to title
        url_to_title = {}
        for t in self._raw_chrome_tabs:
            url_to_title[t.get("url", "")] = t.get("title", "")
            
        success_count = 0
        for url in list(self._selected_chrome_urls):
            title = url_to_title.get(url, "")
            res = routster.add_link(url, title)
            if isinstance(res, dict) and "error" not in res:
                success_count += 1
                self._selected_chrome_urls.discard(url)
                
        self._refresh_chrome_tabs()
        self._refresh_inbox_queue()
        QMessageBox.information(self, "Tabs Ingested", f"Successfully sent {success_count} tabs to the Routster Triage Queue.")

    # ---------------------------------------------------------------------------
    # Data Refresh & Sync
    # ---------------------------------------------------------------------------
    
    def refresh(self) -> None:
        self._refresh_pills()
        self._refresh_inbox_queue()
        self._refresh_flows()
        self._refresh_logs()
        self._refresh_unsorted()
        self._refresh_chrome_tabs()
        self._refresh_settings()

    def _refresh_pills(self) -> None:
        # Clear old pills
        while self._pills_row.count():
            it = self._pills_row.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()

        st = routster.live_status()
        is_ok = st.get("status") == "ok"
        last_activity = st.get("last_activity_iso", "—") or "—"
        if last_activity != "—":
            try:
                dt = datetime.fromisoformat(last_activity)
                last_activity = dt.strftime("%Y-%m-%d %H:%M")
            except (TypeError, ValueError):
                pass

        for label, val, ok in [
            ("Status",        "OK" if is_ok else "Error",          is_ok),
            ("Inbox queue",    st.get("total_links", "—"),          is_ok),
            ("Unsorted queue", st.get("unsorted_count", "—"),       is_ok),
            ("Routed (all)",   st.get("actions_total", "—"),        is_ok),
            ("Routed 24 h",    f"+{st.get('actions_24h', 0)}",      is_ok),
            ("Learned rules",  st.get("learned_rules", "—"),        is_ok),
            ("Last activity",  last_activity,                       is_ok),
        ]:
            self._pills_row.addWidget(_pill(label, val, ok))
        self._pills_row.addStretch(1)

    def _refresh_inbox_queue(self) -> None:
        """Fetch links from the links table directly from SQLite"""
        self._raw_inbox_links = routster.get_links()
        self._categories_list = routster.get_categories()
        
        # Populate Category Filter combobox dynamically
        self._inbox_cat_filter.blockSignals(True)
        cur_filter = self._inbox_cat_filter.currentData()
        self._inbox_cat_filter.clear()
        self._inbox_cat_filter.addItem("🔍 All Categories", "All")
        for cat in self._categories_list:
            self._inbox_cat_filter.addItem(f"{get_emoji(cat)} {cat}", cat)
        idx = self._inbox_cat_filter.findData(cur_filter)
        if idx >= 0:
            self._inbox_cat_filter.setCurrentIndex(idx)
        else:
            self._inbox_cat_filter.setCurrentIndex(0)
        self._inbox_cat_filter.blockSignals(False)

        # Populate mass change category combobox
        self._mass_cat_combo.blockSignals(True)
        self._mass_cat_combo.clear()
        self._mass_cat_combo.addItem("🏷️ Match Category...", "")
        for cat in self._categories_list:
            self._mass_cat_combo.addItem(cat, cat)
        self._mass_cat_combo.setCurrentIndex(0)
        self._mass_cat_combo.blockSignals(False)

        self._apply_inbox_filters()

    def _apply_inbox_filters(self) -> None:
        self._inbox_table.setSortingEnabled(False)
        self._inbox_table.setRowCount(0)
        
        search = self._inbox_search.text().strip().lower()
        cat_filter = self._inbox_cat_filter.currentData()
        source_filter = self._inbox_source_filter.currentText()
        min_conf = self._inbox_conf_slider.value()
        
        filtered = []
        for link in self._raw_inbox_links:
            title = str(link.get("title", "")).lower()
            url = str(link.get("url", "")).lower()
            category = link.get("category", "")
            source = link.get("source", "")
            confidence = link.get("confidence") or 0
            
            if search and (search not in title and search not in url):
                continue
            if cat_filter and cat_filter != "All" and category != cat_filter:
                continue
            if source_filter and "All Sources" not in source_filter and source != source_filter:
                continue
            if confidence < min_conf:
                continue
            filtered.append(link)

        for link in filtered:
            r = self._inbox_table.rowCount()
            self._inbox_table.insertRow(r)
            
            # 0. Checkbox
            chk_item = QTableWidgetItem()
            chk_item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            chk_item.setCheckState(Qt.Checked if link["id"] in self._selected_ids else Qt.Unchecked)
            self._inbox_table.setItem(r, 0, chk_item)
            
            # 1. Type
            type_item = QTableWidgetItem(get_emoji(link.get("category")))
            type_item.setTextAlignment(Qt.AlignCenter)
            self._inbox_table.setItem(r, 1, type_item)
            
            # 2. Title
            title_item = QTableWidgetItem(link.get("title", "—"))
            title_item.setData(Qt.ItemDataRole.UserRole, link["id"])
            self._inbox_table.setItem(r, 2, title_item)
            
            # 3. URL
            url_item = QTableWidgetItem(link.get("url", "—"))
            self._inbox_table.setItem(r, 3, url_item)
            
            # 4. Integration/Category ComboBox in cell
            combo = QComboBox()
            combo.setStyleSheet(_COMBO_QSS)
            combo.blockSignals(True)
            for cat in self._categories_list:
                combo.addItem(f"{get_emoji(cat)} {cat}", cat)
            
            cur_cat = link.get("category", "Uncategorized")
            c_idx = combo.findData(cur_cat)
            if c_idx >= 0:
                combo.setCurrentIndex(c_idx)
            else:
                combo.addItem(cur_cat, cur_cat)
                combo.setCurrentIndex(combo.count() - 1)
            combo.blockSignals(False)
            
            combo.currentIndexChanged.connect(
                lambda idx, lid=link["id"], cb=combo: self._on_row_category_changed(lid, cb.currentData())
            )
            self._inbox_table.setCellWidget(r, 4, combo)
            
            # 5. Confidence
            conf = link.get("confidence")
            conf_str = f"{conf}%" if conf is not None else "—"
            conf_item = QTableWidgetItem(conf_str)
            conf_item.setTextAlignment(Qt.AlignCenter)
            if conf is not None:
                if conf >= 75:
                    conf_item.setForeground(QBrush(QColor(_COLOR_GREEN)))
                elif conf >= 50:
                    conf_item.setForeground(QBrush(QColor(_COLOR_AMBER)))
                else:
                    conf_item.setForeground(QBrush(QColor(_COLOR_RED)))
            self._inbox_table.setItem(r, 5, conf_item)
            
            # 6. Action buttons (Edit & Delete)
            widget = QWidget()
            act_layout = QHBoxLayout(widget)
            act_layout.setContentsMargins(2, 2, 2, 2)
            act_layout.setSpacing(4)
            act_layout.setAlignment(Qt.AlignCenter)
            
            btn_edit = QPushButton("✏️")
            btn_edit.setToolTip("Edit details")
            btn_edit.setFixedSize(20, 20)
            btn_edit.setStyleSheet("QPushButton { border: none; background: transparent; } QPushButton:hover { background: #27272A; border-radius: 3px; }")
            btn_edit.clicked.connect(lambda _, lid=link["id"], title=link.get("title", ""), url=link.get("url", ""): self._edit_link_action(lid, title, url))
            act_layout.addWidget(btn_edit)
            
            btn_del = QPushButton("🗑️")
            btn_del.setToolTip("Delete link")
            btn_del.setFixedSize(20, 20)
            btn_del.setStyleSheet("QPushButton { border: none; background: transparent; color: #EF4444; } QPushButton:hover { background: #27272A; border-radius: 3px; }")
            btn_del.clicked.connect(lambda _, lid=link["id"]: self._delete_link_action(lid))
            act_layout.addWidget(btn_del)
            
            self._inbox_table.setCellWidget(r, 6, widget)
            
        self._inbox_table.setSortingEnabled(True)

    def _refresh_flows(self) -> None:
        self._raw_routes = routster.get_routes()
        self._flows_table.setSortingEnabled(False)
        self._flows_table.setRowCount(0)
        for row in self._raw_routes:
            r = self._flows_table.rowCount()
            self._flows_table.insertRow(r)
            enabled = "✓" if row.get("enabled") else "✗"
            cells = [
                str(row.get("category", "—")),
                str(row.get("action_order", "—")),
                str(row.get("connector_id", "—")),
                enabled,
            ]
            for c, val in enumerate(cells):
                item = QTableWidgetItem(val)
                if c == 3:
                    item.setForeground(
                        QBrush(QColor(_COLOR_GREEN)) if row.get("enabled") else QBrush(QColor(_COLOR_RED))
                    )
                self._flows_table.setItem(r, c, item)
        self._flows_table.setSortingEnabled(True)

    def _refresh_logs(self) -> None:
        self._raw_logs = routster.get_logs(limit=100)
        self._apply_logs_filters()

    def _apply_logs_filters(self) -> None:
        self._logs_table.setSortingEnabled(False)
        self._logs_table.setRowCount(0)
        search = self._logs_search.text().strip().lower()
        
        filtered = []
        for row in self._raw_logs:
            title = str(row.get("entity_title", "")).lower()
            category = str(row.get("category", "")).lower()
            message = str(row.get("message", "")).lower()
            if search and (search not in title and search not in category and search not in message):
                continue
            filtered.append(row)

        for row in filtered:
            r = self._logs_table.rowCount()
            self._logs_table.insertRow(r)
            cells = [
                _ts(row.get("timestamp")),
                str(row.get("entity_title", "—")),
                str(row.get("category", "—")),
                str(row.get("connector", "—")),
                str(row.get("message", "—")),
            ]
            for c, val in enumerate(cells):
                self._logs_table.setItem(r, c, QTableWidgetItem(val))
        self._logs_table.setSortingEnabled(True)

    def _refresh_unsorted(self) -> None:
        self._raw_unsorted = routster.get_unsorted(limit=100)
        self._apply_unsorted_filters()

    def _apply_unsorted_filters(self) -> None:
        self._unsorted_table.setSortingEnabled(False)
        self._unsorted_table.setRowCount(0)
        search = self._unsorted_search.text().strip().lower()
        
        filtered = []
        for row in self._raw_unsorted:
            title = str(row.get("title", "")).lower()
            url = str(row.get("url", "")).lower()
            if search and (search not in title and search not in url):
                continue
            filtered.append(row)

        for row in filtered:
            r = self._unsorted_table.rowCount()
            self._unsorted_table.insertRow(r)
            url_full = str(row.get("url", ""))
            url_display = (url_full[:60] + "…") if len(url_full) > 60 else url_full
            cells = [
                str(row.get("title", "—")),
                url_display,
                str(row.get("visits", 0)),
                _ts(row.get("last_visit")),
                _ts(row.get("archived_at")),
            ]
            for c, val in enumerate(cells):
                item = QTableWidgetItem(val)
                if c == 0:
                    item.setData(Qt.ItemDataRole.UserRole, url_full)
                if c == 1:
                    item.setToolTip(url_full)
                self._unsorted_table.setItem(r, c, item)
        self._unsorted_table.setSortingEnabled(True)

    def _refresh_settings(self) -> None:
        """Fetch general configuration in the background to update setting controls"""
        self._load_settings_in_background()

    def _load_settings_in_background(self) -> None:
        def callback(res):
            if res.get("ok"):
                self._settings_data = res.get("data", {})
                self._refresh_settings_stacked_widgets()
        _spawn_http(self, "GET", "http://localhost:4000/api/all-settings", callback)

    def _refresh_settings_stacked_widgets(self) -> None:
        if not self._settings_data:
            return
        
        # General
        gen = self._settings_data.get("general", {})
        self._set_default_cat.blockSignals(True)
        self._set_default_cat.setText(gen.get("defaultCategory", ""))
        self._set_default_cat.blockSignals(False)
        
        self._set_port.setText(str(gen.get("serverPort", "4000")))
        self._lbl_version.setText(f"Version: {gen.get('version', '1.2.0')} · MIT License")
        
        # Classifier
        cls = self._settings_data.get("classifier", {})
        self._set_conf_slider.blockSignals(True)
        self._set_conf_slider.setValue(int(cls.get("confidenceThreshold", 45)))
        self._set_conf_slider.blockSignals(False)
        self._lbl_set_conf.setText(f"{cls.get('confidenceThreshold', 45)}%")
        
        self._set_fallback.blockSignals(True)
        f_idx = self._set_fallback.findData(cls.get("fallbackBehavior", "uncategorized"))
        if f_idx >= 0:
            self._set_fallback.setCurrentIndex(f_idx)
        self._set_fallback.blockSignals(False)
        
        self._chk_keyword_hints.blockSignals(True)
        self._chk_keyword_hints.setChecked(bool(cls.get("enableFilenameHints", True)))
        self._chk_keyword_hints.blockSignals(False)
        
        self._chk_adaptive_learning.blockSignals(True)
        self._chk_adaptive_learning.setChecked(bool(cls.get("enableAdaptiveLearning", True)))
        self._chk_adaptive_learning.blockSignals(False)
        
        # API & Webhooks
        ap = self._settings_data.get("api", {})
        self._set_webhook_url.setText(ap.get("webhookUrl", "http://localhost:4000/api/open/ingest"))
        
        self._set_api_secret.blockSignals(True)
        self._set_api_secret.setText(ap.get("apiSecret", ""))
        self._set_api_secret.blockSignals(False)
        
        self._set_cors_origins.blockSignals(True)
        self._set_cors_origins.setText(ap.get("allowedOrigins", "*"))
        self._set_cors_origins.blockSignals(False)
        
        self._chk_auto_classify_web.blockSignals(True)
        self._chk_auto_classify_web.setChecked(bool(ap.get("autoClassifyWebhook", True)))
        self._chk_auto_classify_web.blockSignals(False)
        
        # Triggers
        trig = self._settings_data.get("triggers", {})
        self._set_poll_interval.blockSignals(True)
        self._set_poll_interval.setValue(int(trig.get("pollingInterval", 300)))
        self._set_poll_interval.blockSignals(False)
        
        # Data & Storage
        dat = self._settings_data.get("data", {})
        self._set_db_path.setText(dat.get("dbPath", ""))
        self._lbl_stats.setText(
            f"Inbox Links: {dat.get('totalLinks', 0)} · "
            f"Active Routes: {dat.get('totalRoutes', 0)} · "
            f"Learned Rules: {dat.get('learnedRules', 0)}"
        )

    # ---------------------------------------------------------------------------
    # Action Handlers
    # ---------------------------------------------------------------------------
    
    def _on_conf_slider_changed(self, val: int) -> None:
        self._lbl_conf_val.setText(f"Min conf: {val}%")
        self._apply_inbox_filters()

    def _on_sweep_threshold_changed(self, val: int) -> None:
        self._lbl_threshold.setText(f"Wikipedia -> References strictness: {val}")

    def _on_settings_conf_changed(self, val: int) -> None:
        self._lbl_set_conf.setText(f"{val}%")

    def _on_settings_section_changed(self, idx: int) -> None:
        if idx >= 0:
            self._settings_stack.setCurrentIndex(idx)

    def _toggle_select_all(self, state: int) -> None:
        self._inbox_table.blockSignals(True)
        checked = (state == Qt.Checked)
        
        for r in range(self._inbox_table.rowCount()):
            chk_item = self._inbox_table.item(r, 0)
            title_item = self._inbox_table.item(r, 2)
            if chk_item and title_item:
                lid = title_item.data(Qt.ItemDataRole.UserRole)
                chk_item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
                if checked:
                    self._selected_ids.add(lid)
                else:
                    self._selected_ids.discard(lid)
                    
        self._lbl_sel_count.setText(f"{len(self._selected_ids)} links selected")
        self._inbox_table.blockSignals(False)

    def _on_table_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() == 0:
            # Checkbox clicked
            r = item.row()
            title_item = self._inbox_table.item(r, 2)
            if title_item:
                lid = title_item.data(Qt.ItemDataRole.UserRole)
                if item.checkState() == Qt.Checked:
                    self._selected_ids.add(lid)
                else:
                    self._selected_ids.discard(lid)
                self._lbl_sel_count.setText(f"{len(self._selected_ids)} links selected")

    def _on_row_category_changed(self, link_id: str, category: str) -> None:
        self._status_lbl.setText(f"⏳ Updating category to '{category}'...")
        
        def callback(res):
            if res.get("ok"):
                self._status_lbl.setText("✅ Category updated successfully!")
                self._refresh_inbox_queue()
            else:
                self._status_lbl.setText(f"❌ Failed to update category: {res.get('error')}")
                
        _spawn_http(self, "PUT", f"http://localhost:4000/api/links/{link_id}", callback,
                    json_body={"category": category})

    def _on_inbox_double_clicked(self, item: QTableWidgetItem) -> None:
        # Double clicking title or URL column opens browser
        r = item.row()
        url_item = self._inbox_table.item(r, 3)
        if url_item:
            url = url_item.text()
            if url.startswith("http"):
                webbrowser.open(url)

    def _on_unsorted_double_clicked(self, item: QTableWidgetItem) -> None:
        title_item = self._unsorted_table.item(item.row(), 0)
        if title_item:
            url = title_item.data(Qt.ItemDataRole.UserRole)
            if url and url.startswith("http"):
                webbrowser.open(url)

    # ── CRUD & Mass Actions ────────────────────────────────────────────────
    
    def _edit_link_action(self, link_id: str, title: str, url: str) -> None:
        dlg = EditLinkDialog(self, title, url)
        if dlg.exec() == QDialog.Accepted:
            new_title = dlg.title_input.text().strip()
            new_url = dlg.url_input.text().strip()
            if not new_url:
                QMessageBox.warning(self, "Edit Link", "URL cannot be empty.")
                return
                
            self._status_lbl.setText("⏳ Updating link details...")
            
            def callback(res):
                if res.get("ok"):
                    self._status_lbl.setText("✅ Link details updated successfully!")
                    self.refresh()
                else:
                    self._status_lbl.setText(f"❌ Update failed: {res.get('error')}")
                    
            _spawn_http(self, "PUT", f"http://localhost:4000/api/links/{link_id}", callback,
                        json_body={"title": new_title, "url": new_url})

    def _delete_link_action(self, link_id: str) -> None:
        self._status_lbl.setText("⏳ Deleting link...")
        
        def callback(res):
            if res.get("ok"):
                self._status_lbl.setText("✅ Link successfully deleted.")
                self._selected_ids.discard(link_id)
                self.refresh()
            else:
                self._status_lbl.setText(f"❌ Delete failed: {res.get('error')}")
                
        _spawn_http(self, "DELETE", f"http://localhost:4000/api/links/{link_id}", callback)

    def _mass_category_action(self, idx: int) -> None:
        if idx <= 0 or not self._selected_ids:
            return
            
        category = self._mass_cat_combo.currentData()
        if not category:
            return
            
        self._status_lbl.setText(f"⏳ Categorising {len(self._selected_ids)} links to '{category}'...")
        
        def callback(res):
            if res.get("ok"):
                self._status_lbl.setText(f"✅ Successfully categorized {len(self._selected_ids)} links!")
                self._selected_ids.clear()
                self._chk_select_all.setChecked(False)
                self._lbl_sel_count.setText("0 links selected")
                self.refresh()
            else:
                self._status_lbl.setText(f"❌ Mass categorisation failed: {res.get('error')}")
                
        _spawn_http(self, "POST", "http://localhost:4000/api/links/mass-category", callback,
                    json_body={"itemIds": list(self._selected_ids), "category": category})

    def _mass_reclassify_action(self) -> None:
        if not self._selected_ids:
            QMessageBox.warning(self, "Mass Re-classify", "Please select at least one link to re-classify.")
            return
            
        self._status_lbl.setText(f"⏳ Re-classifying {len(self._selected_ids)} links...")
        
        def callback(res):
            if res.get("ok"):
                self._status_lbl.setText("✅ Links successfully re-classified!")
                self._selected_ids.clear()
                self._chk_select_all.setChecked(False)
                self._lbl_sel_count.setText("0 links selected")
                self.refresh()
            else:
                self._status_lbl.setText(f"❌ Auto re-classify failed: {res.get('error')}")
                
        _spawn_http(self, "POST", "http://localhost:4000/api/links/mass-reclassify", callback,
                    json_body={"itemIds": list(self._selected_ids)})

    def _mass_delete_action(self) -> None:
        if not self._selected_ids:
            QMessageBox.warning(self, "Mass Delete", "Please select at least one link to delete.")
            return
            
        ans = QMessageBox.warning(
            self, "Mass Delete", f"Are you sure you want to delete {len(self._selected_ids)} items?",
            QMessageBox.Yes | QMessageBox.No
        )
        if ans == QMessageBox.No:
            return
            
        self._status_lbl.setText(f"⏳ Deleting {len(self._selected_ids)} links...")
        
        def callback(res):
            if res.get("ok"):
                self._status_lbl.setText(f"✅ Successfully deleted {len(self._selected_ids)} links!")
                self._selected_ids.clear()
                self._chk_select_all.setChecked(False)
                self._lbl_sel_count.setText("0 links selected")
                self.refresh()
            else:
                self._status_lbl.setText(f"❌ Delete failed: {res.get('error')}")
                
        _spawn_http(self, "POST", "http://localhost:4000/api/links/mass-delete", callback,
                    json_body={"itemIds": list(self._selected_ids)})

    def _export_queue_action(self) -> None:
        if not self._selected_ids:
            QMessageBox.warning(self, "Export Queue", "Please select at least one link to run the export pipeline on.")
            return
            
        self._status_lbl.setText(f"⏳ Exporting {len(self._selected_ids)} links in the background...")
        
        def callback(res):
            if res.get("ok"):
                body = res.get("data", {})
                msg = body.get("message", "Sync completed successfully.")
                self._status_lbl.setText("✅ Export completed successfully!")
                QMessageBox.information(self, "Export Pipeline", msg)
                self._selected_ids.clear()
                self._chk_select_all.setChecked(False)
                self._lbl_sel_count.setText("0 links selected")
                self.refresh()
            else:
                self._status_lbl.setText(f"❌ Export failed: {res.get('error')}")
                QMessageBox.warning(self, "Export Pipeline", f"Export pipeline failed: {res.get('error')}")
                
        _spawn_http(self, "POST", "http://localhost:4000/api/export", callback,
                    json_body={"itemIds": list(self._selected_ids)}, timeout=30.0)

    # ── Universal Ingest Ingestion ─────────────────────────────────────────
    
    def _pull_chrome_action(self) -> None:
        self._status_lbl.setText("⏳ Extracting links from native Chrome bookmarks Sync...")
        
        def callback(res):
            if res.get("ok"):
                body = res.get("data", {})
                msg = body.get("message", "Chrome pull completed.")
                self._status_lbl.setText("✅ Pull Chrome Sync finished!")
                QMessageBox.information(self, "Chrome Sync", msg)
                self.refresh()
            else:
                self._status_lbl.setText(f"❌ Chrome Sync failed: {res.get('error')}")
                QMessageBox.warning(self, "Chrome Sync", f"Sync failed: {res.get('error')}")
                
        _spawn_http(self, "POST", "http://localhost:4000/api/sync-chrome", callback, timeout=20.0)

    def _choose_file_action(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Select Bookmarks File", "", "HTML/Text Files (*.html *.txt);;All Files (*.*)"
        )
        if not file_path:
            return
            
        parse_links = self._chk_extract.isChecked()
        self._status_lbl.setText("⏳ Uploading file to Routster pipeline...")
        
        def callback(res):
            if res.get("ok"):
                body = res.get("data", {})
                msg = body.get("message", "File upload successful.")
                self._status_lbl.setText("✅ File processed successfully!")
                QMessageBox.information(self, "File Ingestion", msg)
                self._ingest_text.clear()
                self.refresh()
            else:
                self._status_lbl.setText(f"❌ Ingestion failed: {res.get('error')}")
                QMessageBox.warning(self, "File Ingestion", f"Ingestion failed: {res.get('error')}")
                
        if parse_links or not file_path.lower().endswith(".html"):
            # Use general ingest endpoint
            _spawn_http(self, "POST", "http://localhost:4000/api/ingest", callback,
                        files={"file": (os.path.basename(file_path), open(file_path, "rb"))},
                        json_body={"parseLinks": "true" if parse_links else "false"}, timeout=30.0)
        else:
            # Upload Bookmarks HTML endpoint
            _spawn_http(self, "POST", "http://localhost:4000/api/upload-bookmarks", callback,
                        files={"file": (os.path.basename(file_path), open(file_path, "rb"), "text/html")}, timeout=30.0)

    def _send_pipeline_action(self) -> None:
        text = self._ingest_text.toPlainText().strip()
        if not text:
            QMessageBox.warning(self, "Ingestion", "Ingestion content field cannot be empty.")
            return
            
        parse_links = self._chk_extract.isChecked()
        is_url = text.startswith("http") or text.includes(".org") if hasattr(text, "includes") else (text.startswith("http") or ".org" in text)
        type_str = "url" if (is_url and not parse_links) else "text"
        
        self._status_lbl.setText("⏳ Sending item to Routster automation pipeline...")
        
        def callback(res):
            if res.get("ok"):
                body = res.get("data", {})
                msg = body.get("message", "Ingestion completed.")
                self._status_lbl.setText("✅ Ingestion successfully completed!")
                self._ingest_text.clear()
                self.refresh()
            else:
                self._status_lbl.setText(f"❌ Ingestion failed: {res.get('error')}")
                QMessageBox.warning(self, "Ingestion Error", f"Failed to ingest: {res.get('error')}")
                
        payload = {"type": type_str, "parseLinks": parse_links}
        if type_str == "url":
            payload["url"] = text
        else:
            payload["textContent"] = text
            
        _spawn_http(self, "POST", "http://localhost:4000/api/ingest", callback, json_body=payload, timeout=20.0)

    # ── Deep Sweep ─────────────────────────────────────────────────────────
    
    def _choose_sweep_file(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Select History Export File", "", "History Files (*.json *.db *.html);;All Files (*.*)"
        )
        if not file_path:
            return
        self._sweep_filepath = file_path
        self._lbl_sweep_filename.setText(f"Selected: {os.path.basename(file_path)}")
        self._btn_run_sweep.setEnabled(True)

    def _run_sweep_action(self) -> None:
        if not self._sweep_filepath:
            return
            
        threshold = self._sweep_threshold_slider.value()
        self._status_lbl.setText("⏳ Deep sweep running... Large history files may take a minute...")
        self._btn_run_sweep.setEnabled(False)
        
        def callback(res):
            self._btn_run_sweep.setEnabled(True)
            if res.get("ok"):
                body = res.get("data", {})
                stats = body.get("stats", {})
                self._status_lbl.setText("✅ Deep history sweep complete!")
                
                summary = (
                    f"Parsed <b>{stats.get('rawEntries', 0):,}</b> visits -> "
                    f"<b>{stats.get('uniqueUrls', 0):,}</b> unique links.<br/>"
                    f"Staged for review in Inbox: <b>{body.get('staged', 0):,}</b> links.<br/>"
                    f"Exclusions/already synced skipped: {body.get('skippedDuplicates', 0) + body.get('excludedSkipped', 0):,}"
                )
                self._lbl_sweep_summary.setText(summary)
                
                buckets = " · ".join(f"{k}: {v}" for k, v in stats.get("perBucket", {}).items())
                self._lbl_sweep_buckets.setText(f"Categorised: {buckets}")
                
                self._sweep_result_box.setVisible(True)
                self.refresh()
                QMessageBox.information(self, "History Sweep", "Deep sweep process complete! Review your staged matches in Tab 1.")
            else:
                self._status_lbl.setText(f"❌ Deep sweep failed: {res.get('error')}")
                QMessageBox.warning(self, "History Sweep", f"Sweep failed: {res.get('error')}")
                
        # Call upload endpoint
        _spawn_http(self, "POST", "http://localhost:4000/api/sweep-history", callback,
                    files={"file": (os.path.basename(self._sweep_filepath), open(self._sweep_filepath, "rb"))},
                    json_body={"threshold": str(threshold)}, timeout=60.0)

    def _clear_sweep_action(self) -> None:
        ans = QMessageBox.warning(
            self, "Clear Sweep Results",
            "Clear all staged links, unsorted records, and trash history from this sweep?\n"
            "Your learned rules and exclusions will not be affected.",
            QMessageBox.Yes | QMessageBox.No
        )
        if ans == QMessageBox.No:
            return
            
        self._status_lbl.setText("⏳ Clearing sweep database files...")
        
        def callback(res):
            if res.get("ok"):
                self._status_lbl.setText("✅ Sweep data cleared successfully!")
                self._sweep_result_box.setVisible(False)
                self._lbl_sweep_filename.setText("Select a browser history or bookmarks export file:")
                self._sweep_filepath = ""
                self._btn_run_sweep.setEnabled(False)
                self.refresh()
                QMessageBox.information(self, "Clear Sweep", "Database sweep results reset successfully.")
            else:
                self._status_lbl.setText(f"❌ Reset failed: {res.get('error')}")
                
        _spawn_http(self, "POST", "http://localhost:4000/api/clear-sweep", callback)

    # ── Settings Actions ───────────────────────────────────────────────────
    
    def _change_setting(self, section: str, key: str, value) -> None:
        self._status_lbl.setText(f"⏳ Saving setting '{key}'...")
        
        def callback(res):
            if res.get("ok"):
                self._status_lbl.setText(f"✅ Updated setting: {key}")
                self._load_settings_in_background()
            else:
                self._status_lbl.setText(f"❌ Failed to save setting: {res.get('error')}")
                
        _spawn_http(self, "PATCH", "http://localhost:4000/api/all-settings", callback,
                    json_body={"section": section, "key": key, "value": value})

    def _export_database(self) -> None:
        file_path, _ = QFileDialog.getSaveFileName(
            self, "Save Database Backup", "routster_backup.json", "JSON Files (*.json)"
        )
        if not file_path:
            return
            
        self._status_lbl.setText("⏳ Fetching database backup...")
        
        def callback(res):
            if res.get("ok"):
                try:
                    with open(file_path, "w", encoding="utf-8") as f:
                        json.dump(res["data"], f, indent=2)
                    self._status_lbl.setText("✅ Database exported successfully!")
                    QMessageBox.information(self, "Backup", f"Database backup saved successfully to:\n{file_path}")
                except Exception as e:
                    self._status_lbl.setText(f"❌ Failed to save backup: {e}")
                    QMessageBox.warning(self, "Backup", f"Failed to save backup: {e}")
            else:
                self._status_lbl.setText(f"❌ Failed to export database: {res.get('error')}")
                QMessageBox.warning(self, "Backup", f"Failed to export database: {res.get('error')}")
                
        _spawn_http(self, "GET", "http://localhost:4000/api/export-db", callback)

    def _clear_data_action(self, target: str) -> None:
        ans = QMessageBox.warning(
            self, "Clear Data",
            f"Are you sure you want to clear all {target}? This cannot be undone! Export first!",
            QMessageBox.Yes | QMessageBox.No
        )
        if ans == QMessageBox.No:
            return
            
        self._status_lbl.setText(f"⏳ Clearing {target}...")
        
        def callback(res):
            if res.get("ok"):
                self._status_lbl.setText(f"✅ Cleared {target} successfully!")
                QMessageBox.information(self, "Clear Data", f"Successfully cleared: {target}")
                self.refresh()
            else:
                self._status_lbl.setText(f"❌ Failed to clear {target}: {res.get('error')}")
                QMessageBox.warning(self, "Clear Data", f"Failed to clear: {res.get('error')}")
                
        _spawn_http(self, "POST", "http://localhost:4000/api/clear-data", callback, json_body={"target": target})

    def _recovery_action(self, kind: str) -> None:
        self._status_lbl.setText(f"⏳ Running {kind} recovery... Please wait...")
        
        def callback(res):
            if res.get("ok"):
                body = res.get("data", {})
                msg = body.get("message", "Recovery complete.")
                self._status_lbl.setText("✅ Recovery complete!")
                QMessageBox.information(self, "Pipeline Recovery", msg)
                self.refresh()
            else:
                self._status_lbl.setText(f"❌ Recovery failed: {res.get('error')}")
                QMessageBox.warning(self, "Pipeline Recovery", f"Recovery failed: {res.get('error')}")
                
        url = f"http://localhost:4000/api/recover-failed-exports"
        if kind == "zotero":
            url = f"http://localhost:4000/api/recover-to-zotero"
        elif kind == "instapaper":
            url = f"http://localhost:4000/api/recover-to-instapaper"
            
        _spawn_http(self, "POST", url, callback, timeout=40.0)

    def _reset_onboarding_action(self) -> None:
        self._status_lbl.setText("⏳ Resetting onboarding configuration state...")
        
        def callback(res):
            if res.get("ok"):
                self._status_lbl.setText("✅ Onboarding state reset! Routster will welcome you next boot.")
                QMessageBox.information(self, "Onboarding Setup", "Onboarding state reset! Restart Egon/Routster to re-run configuration.")
                self.refresh()
            else:
                self._status_lbl.setText(f"❌ Reset failed: {res.get('error')}")
                
        _spawn_http(self, "POST", "http://localhost:4000/api/app-state", callback, json_body={"onboarding_complete": False})

    # ── Logs & Unsorted Filters ────────────────────────────────────────────
    
    def _apply_logs_filters(self) -> None:
        self._apply_logs_filters_impl()

    def _apply_logs_filters_impl(self) -> None:
        self._logs_table.setSortingEnabled(False)
        self._logs_table.setRowCount(0)
        search = self._logs_search.text().strip().lower()
        
        filtered = []
        for row in self._raw_logs:
            title = str(row.get("entity_title", "")).lower()
            category = str(row.get("category", "")).lower()
            message = str(row.get("message", "")).lower()
            if search and (search not in title and search not in category and search not in message):
                continue
            filtered.append(row)

        for row in filtered:
            r = self._logs_table.rowCount()
            self._logs_table.insertRow(r)
            cells = [
                _ts(row.get("timestamp")),
                str(row.get("entity_title", "—")),
                str(row.get("category", "—")),
                str(row.get("connector", "—")),
                str(row.get("message", "—")),
            ]
            for c, val in enumerate(cells):
                self._logs_table.setItem(r, c, QTableWidgetItem(val))
        self._logs_table.setSortingEnabled(True)

    def _apply_unsorted_filters(self) -> None:
        self._apply_unsorted_filters_impl()

    def _apply_unsorted_filters_impl(self) -> None:
        self._unsorted_table.setSortingEnabled(False)
        self._unsorted_table.setRowCount(0)
        search = self._unsorted_search.text().strip().lower()
        
        filtered = []
        for row in self._raw_unsorted:
            title = str(row.get("title", "")).lower()
            url = str(row.get("url", "")).lower()
            if search and (search not in title and search not in url):
                continue
            filtered.append(row)

        for row in filtered:
            r = self._unsorted_table.rowCount()
            self._unsorted_table.insertRow(r)
            url_full = str(row.get("url", ""))
            url_display = (url_full[:60] + "…") if len(url_full) > 60 else url_full
            cells = [
                str(row.get("title", "—")),
                url_display,
                str(row.get("visits", 0)),
                _ts(row.get("last_visit")),
                _ts(row.get("archived_at")),
            ]
            for c, val in enumerate(cells):
                item = QTableWidgetItem(val)
                if c == 0:
                    item.setData(Qt.ItemDataRole.UserRole, url_full)
                if c == 1:
                    item.setToolTip(url_full)
                self._unsorted_table.setItem(r, c, item)
        self._unsorted_table.setSortingEnabled(True)
