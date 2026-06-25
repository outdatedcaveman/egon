"""Search page — search all snapshots using lib.cross_search."""
from __future__ import annotations

import webbrowser
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QMessageBox,
)
from lib import cross_search


class _SearchWorker(QThread):
    done = Signal(list, str)  # results, error_msg

    def __init__(self, query: str, parent=None):
        super().__init__(parent)
        self._query = query

    def run(self):
        try:
            res = cross_search.search(self._query)
            self.done.emit(res, "")
        except Exception as e:
            self.done.emit([], str(e))


class SearchPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 24, 28, 24)
        outer.setSpacing(14)

        # Title
        title = QLabel("🔍  Search")
        title.setStyleSheet("font-size: 22px; font-weight: 700; color: #f5f5f7;")
        outer.addWidget(title)

        sub = QLabel("Query all snapshot databases simultaneously. Results are ranked by exact matches and token overlap.")
        sub.setStyleSheet("color: #76767f;")
        outer.addWidget(sub)

        # Search Bar
        bar = QHBoxLayout()
        self._input = QLineEdit()
        self._input.setPlaceholderText("Enter query...")
        self._input.setStyleSheet(
            "QLineEdit { background: #0c0d0f; color: #f5f5f7; border: 1px solid #22252a; "
            "border-radius: 4px; padding: 8px 12px; font-size: 13px; }"
            "QLineEdit:focus { border-color: #60A5A8; }"
        )
        self._input.returnPressed.connect(self._do_search)
        bar.addWidget(self._input, 1)

        self._btn = QPushButton("Search")
        self._btn.setStyleSheet(
            "QPushButton { background: #60A5A8; color: white; padding: 8px 20px; "
            "border-radius: 4px; font-weight: 600; border: none; }"
            "QPushButton:hover { background: #ff453a; }"
        )
        self._btn.clicked.connect(self._do_search)
        bar.addWidget(self._btn)
        outer.addLayout(bar)

        # Table
        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(["Source", "Title", "Score", "Match Details"])
        _hdr = self._table.horizontalHeader()
        _hdr.setSectionResizeMode(QHeaderView.Interactive)
        _hdr.setStretchLastSection(True)
        self._table.setColumnWidth(0, 120)
        self._table.setColumnWidth(1, 400)
        self._table.setColumnWidth(2, 60)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.doubleClicked.connect(self._on_row_double_clicked)
        self._table.setStyleSheet(
            "QTableWidget { background: #0c0d0f; color: #f5f5f7; gridline-color: #22252a; "
            "border: 1px solid #22252a; border-radius: 6px; }"
            "QHeaderView::section { background: #212328; color: #76767f; padding: 6px; "
            "border: none; border-bottom: 1px solid #22252a; font-weight: 600; }"
            "QTableWidget::item:selected { background: #2a2d34; }"
        )
        outer.addWidget(self._table, 1)

        self._status = QLabel("Ready")
        self._status.setStyleSheet("color: #76767f; font-size: 11px;")
        outer.addWidget(self._status)

        self._results: list[dict] = []

    def _do_search(self) -> None:
        q = self._input.text().strip()
        if not q:
            return
        self._btn.setEnabled(False)
        self._status.setText("Searching...")
        self._worker = _SearchWorker(q, self)
        self._worker.done.connect(self._on_search_done)
        self._worker.start()

    def _on_search_done(self, results: list, err: str) -> None:
        self._btn.setEnabled(True)
        if err:
            self._status.setText(f"Error: {err}")
            QMessageBox.warning(self, "Search failed", err)
            return
        self._results = results
        self._table.setRowCount(0)
        self._status.setText(f"Found {len(results)} matching results.")

        for r in results:
            row_idx = self._table.rowCount()
            self._table.insertRow(row_idx)

            item = r["item"]
            source = r["source"]
            score = str(r["score"])
            title = cross_search.pretty_title(item)
            subline = cross_search.pretty_subline(item, source)

            self._table.setItem(row_idx, 0, QTableWidgetItem(source.title()))
            self._table.setItem(row_idx, 1, QTableWidgetItem(title))
            self._table.setItem(row_idx, 2, QTableWidgetItem(score))
            self._table.setItem(row_idx, 3, QTableWidgetItem(subline))

    def _on_row_double_clicked(self, index) -> None:
        row = index.row()
        if 0 <= row < len(self._results):
            item = self._results[row]["item"]
            url = cross_search.pretty_url(item)
            if url:
                webbrowser.open(url, new=2)
            else:
                self._status.setText("No URL found for this item.")
