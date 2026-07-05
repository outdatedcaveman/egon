"""PySide6 View for References Uber-Comparer tab."""
from __future__ import annotations

import time
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QFrame,
    QTableWidget, QTableWidgetItem, QPushButton, QHeaderView, QComboBox,
    QAbstractItemView, QMessageBox, QMenu, QCheckBox, QProgressBar,
)
from lib import references_comparer


class ComparerLoadWorker(QThread):
    done = Signal(list, dict, str)
    progress = Signal(int, int, str)

    def run(self) -> None:
        def pg_cb(curr, tot, msg):
            self.progress.emit(curr, tot, msg)
        try:
            # Load all items for comparative view (uncapped/high limit)
            groups, stats = references_comparer.build_comparer_index(350000, progress_callback=pg_cb)
            self.done.emit(groups, stats, "")
        except Exception as e:
            self.done.emit([], {}, str(e))


class ComparerActionWorker(QThread):
    done = Signal(dict, str)
    progress = Signal(int, int, str)

    def __init__(self, action_type: str, groups: list[dict], merge_sources: bool = True, parent=None):
        super().__init__(parent)
        self.action_type = action_type
        self.groups = groups
        self.merge_sources = merge_sources

    def run(self) -> None:
        def pg_cb(curr, tot, msg):
            self.progress.emit(curr, tot, msg)
        try:
            if self.action_type == "zot_to_m":
                to_push = [g for g in self.groups if g["zotero_item"] and not g["mouseion_item"]]
                if not to_push:
                    self.done.emit({"status": "no_match"}, "")
                    return
                success, fail = references_comparer.push_to_mouseion(to_push, progress_callback=pg_cb)
                self.done.emit({"status": "ok", "success": success, "fail": fail, "target": "Mouseion"}, "")
            elif self.action_type == "m_to_z":
                to_push = [g for g in self.groups if g["mouseion_item"] and not g["zotero_item"]]
                if not to_push:
                    self.done.emit({"status": "no_match"}, "")
                    return
                success, fail = references_comparer.push_to_zotero(to_push, progress_callback=pg_cb)
                self.done.emit({"status": "ok", "success": success, "fail": fail, "target": "Zotero"}, "")
            elif self.action_type == "pp_to_z":
                to_push = [g for g in self.groups if g["paperpile_item"] and not g["zotero_item"]]
                if not to_push:
                    self.done.emit({"status": "no_match"}, "")
                    return
                success, fail = references_comparer.push_to_zotero(to_push, progress_callback=pg_cb)
                self.done.emit({"status": "ok", "success": success, "fail": fail, "target": "Zotero"}, "")
            elif self.action_type == "pp_to_m":
                to_push = [g for g in self.groups if g["paperpile_item"] and not g["mouseion_item"]]
                if not to_push:
                    self.done.emit({"status": "no_match"}, "")
                    return
                success, fail = references_comparer.push_to_mouseion(to_push, progress_callback=pg_cb)
                self.done.emit({"status": "ok", "success": success, "fail": fail, "target": "Mouseion"}, "")
            elif self.action_type == "to_pp":
                to_push = [g for g in self.groups if (g["zotero_item"] or g["mouseion_item"]) and not g["paperpile_item"]]
                if not to_push:
                    self.done.emit({"status": "no_match"}, "")
                    return
                path = references_comparer.push_to_paperpile(to_push)
                self.done.emit({"status": "export", "path": path, "count": len(to_push)}, "")
            elif self.action_type == "consolidate":
                res = references_comparer.run_consolidation(self.groups, merge_sources=self.merge_sources, progress_callback=pg_cb)
                self.done.emit({"status": "consolidate", "results": res}, "")
            elif self.action_type == "consolidate_zotero":
                res = references_comparer.run_consolidation(self.groups, merge_sources=self.merge_sources, target_client="zotero", progress_callback=pg_cb)
                self.done.emit({"status": "consolidate", "results": res}, "")
            elif self.action_type == "consolidate_mouseion":
                res = references_comparer.run_consolidation(self.groups, merge_sources=self.merge_sources, target_client="mouseion", progress_callback=pg_cb)
                self.done.emit({"status": "consolidate", "results": res}, "")
            elif self.action_type == "consolidate_paperpile":
                res = references_comparer.run_consolidation(self.groups, merge_sources=self.merge_sources, target_client="paperpile", progress_callback=pg_cb)
                self.done.emit({"status": "consolidate", "results": res}, "")
        except Exception as e:
            self.done.emit({}, str(e))


class UberComparerWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._groups: list[dict] = []
        self._stats_data: dict = {}
        self._checked_groups: dict = {} # Map id(group) -> group
        self._loader: ComparerLoadWorker | None = None
        self._action_worker: ComparerActionWorker | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(6)

        # Toolbar Frame
        toolbar = QFrame()
        toolbar.setStyleSheet("QFrame { background: #16404F; border: 1px solid #1F4858; border-radius: 6px; }")
        toolbar_layout = QVBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(8, 6, 8, 6)
        toolbar_layout.setSpacing(6)

        # Row 1: Search, Filter, Selection controls
        row1 = QHBoxLayout()
        row1.setContentsMargins(0, 0, 0, 0)
        row1.setSpacing(8)

        self._search = QLineEdit()
        self._search.setPlaceholderText("filter title / authors / DOI…")
        self._search.setMinimumWidth(220)
        self._search.setStyleSheet(
            "QLineEdit { background: #102F3C; color: #F0E9D5; border: 1px solid #1F4858; "
            "border-radius: 4px; padding: 4px 8px; }")
        self._search.textChanged.connect(self._apply_filters)
        row1.addWidget(self._search)

        row1.addWidget(QLabel("filter:"))
        self._status_filter = QComboBox()
        self._status_filter.addItem("All entries", "")
        self._status_filter.addItem("Missing in Zotero", "missing_zotero")
        self._status_filter.addItem("Missing in Paperpile", "missing_paperpile")
        self._status_filter.addItem("Missing in Mouseion", "missing_mouseion")
        self._status_filter.addItem("In Zotero", "in_zotero")
        self._status_filter.addItem("In Paperpile", "in_paperpile")
        self._status_filter.addItem("In Mouseion", "in_mouseion")
        self._status_filter.addItem("Only in Zotero", "only_zotero")
        self._status_filter.addItem("Only in Paperpile", "only_paperpile")
        self._status_filter.addItem("Only in Mouseion", "only_mouseion")
        self._status_filter.addItem("Incomplete (< 75%)", "incomplete")
        self._status_filter.currentIndexChanged.connect(self._apply_filters)
        row1.addWidget(self._status_filter)

        self._select_all_btn = QPushButton("Select All")
        self._select_all_btn.setStyleSheet(
            "QPushButton { background: #1F5366; color: white; padding: 4px 10px; "
            "border-radius: 3px; border: none; font-weight: 600; }"
            "QPushButton:hover { background: #286C85; }")
        self._select_all_btn.clicked.connect(self._select_all)
        row1.addWidget(self._select_all_btn)

        self._deselect_all_btn = QPushButton("Deselect All")
        self._deselect_all_btn.setStyleSheet(
            "QPushButton { background: #1F5366; color: white; padding: 4px 10px; "
            "border-radius: 3px; border: none; font-weight: 600; }"
            "QPushButton:hover { background: #286C85; }")
        self._deselect_all_btn.clicked.connect(self._deselect_all)
        row1.addWidget(self._deselect_all_btn)

        row1.addStretch(1)

        self._refresh_btn = QPushButton("Refresh")
        self._refresh_btn.setStyleSheet(
            "QPushButton { background: #1F5366; color: white; padding: 4px 12px; "
            "border-radius: 3px; font-weight: 600; border: none; }"
            "QPushButton:hover { background: #286C85; }")
        self._refresh_btn.clicked.connect(self.reload)
        row1.addWidget(self._refresh_btn)

        toolbar_layout.addLayout(row1)

        # Row 2: Sync and Consolidation Actions
        row2 = QHBoxLayout()
        row2.setContentsMargins(0, 0, 0, 0)
        row2.setSpacing(8)

        self._push_z_m_btn = QPushButton("Push Zotero ➔ Mouseion")
        self._push_z_m_btn.setStyleSheet(
            "QPushButton { background: #60A5A8; color: white; padding: 4px 12px; "
            "border-radius: 3px; font-weight: 600; border: none; }"
            "QPushButton:hover { background: #7BC5C7; }")
        self._push_z_m_btn.clicked.connect(lambda: self._run_action("zot_to_m"))
        row2.addWidget(self._push_z_m_btn)

        self._push_m_z_btn = QPushButton("Push Mouseion ➔ Zotero")
        self._push_m_z_btn.setStyleSheet(
            "QPushButton { background: #60A5A8; color: white; padding: 4px 12px; "
            "border-radius: 3px; font-weight: 600; border: none; }"
            "QPushButton:hover { background: #7BC5C7; }")
        self._push_m_z_btn.clicked.connect(lambda: self._run_action("m_to_z"))
        row2.addWidget(self._push_m_z_btn)

        self._more_btn = QPushButton("More Actions ▾")
        self._more_btn.setStyleSheet(
            "QPushButton { background: #1F5366; color: white; padding: 4px 12px; "
            "border-radius: 3px; font-weight: 600; border: none; }"
            "QPushButton:hover { background: #286C85; }")
        
        more_menu = QMenu(self._more_btn)
        more_menu.setStyleSheet(
            "QMenu { background: #16404F; color: #F0E9D5; border: 1px solid #1F4858; }"
            "QMenu::item:selected { background: #1F5366; }"
        )
        more_menu.addAction("Push Paperpile ➔ Zotero", lambda: self._run_action("pp_to_z"))
        more_menu.addAction("Push Paperpile ➔ Mouseion", lambda: self._run_action("pp_to_m"))
        more_menu.addAction("Export Push to Paperpile (RIS)", lambda: self._run_action("to_pp"))
        more_menu.addSeparator()
        more_menu.addAction("Consolidate Zotero Entries Only", lambda: self._run_action("consolidate_zotero"))
        more_menu.addAction("Consolidate Mouseion Entries Only", lambda: self._run_action("consolidate_mouseion"))
        more_menu.addAction("Export Paperpile Consolidations Only", lambda: self._run_action("consolidate_paperpile"))
        self._more_btn.setMenu(more_menu)
        row2.addWidget(self._more_btn)

        row2.addStretch(1)

        self._merge_cb = QCheckBox("Merge sources")
        self._merge_cb.setChecked(True)
        self._merge_cb.setStyleSheet("QCheckBox { color: #F0E9D5; }")
        row2.addWidget(self._merge_cb)

        self._consolidate_btn = QPushButton("Consolidate Metadata")
        self._consolidate_btn.setStyleSheet(
            "QPushButton { background: #D4A24C; color: #102F3C; padding: 4px 12px; "
            "border-radius: 3px; font-weight: 700; border: none; }"
            "QPushButton:hover { background: #E5B25D; }")
        self._consolidate_btn.clicked.connect(lambda: self._run_action("consolidate"))
        row2.addWidget(self._consolidate_btn)

        toolbar_layout.addLayout(row2)

        layout.addWidget(toolbar)

        # Stats bar
        self._stats_lbl = QLabel("Comparative index not loaded. Click 'Refresh' to scan and compile the comparison matrix across Zotero, Paperpile, and Mouseion.")
        self._stats_lbl.setStyleSheet("color: #9CA3AF; font-size: 11px; padding: 0 4px;")
        self._stats_lbl.setWordWrap(True)
        layout.addWidget(self._stats_lbl)

        # Progress Panel (hidden by default)
        self._progress_panel = QFrame()
        self._progress_panel.setStyleSheet("QFrame { background: #16404F; border: 1px solid #1F4858; border-radius: 6px; }")
        progress_layout = QHBoxLayout(self._progress_panel)
        progress_layout.setContentsMargins(8, 6, 8, 6)
        progress_layout.setSpacing(10)
        
        self._progress_lbl = QLabel("Starting operation...")
        self._progress_lbl.setStyleSheet("color: #F0E9D5; font-size: 12px;")
        progress_layout.addWidget(self._progress_lbl, 1)
        
        self._progress_bar = QProgressBar()
        self._progress_bar.setStyleSheet("""
            QProgressBar {
                border: 1px solid #1F4858;
                border-radius: 4px;
                background: #102F3C;
                text-align: center;
                color: #F0E9D5;
                font-weight: bold;
                height: 18px;
            }
            QProgressBar::chunk {
                background-color: #D4A24C;
                border-radius: 2px;
            }
        """)
        self._progress_bar.setMinimumWidth(300)
        progress_layout.addWidget(self._progress_bar)
        
        self._progress_panel.setVisible(False)
        layout.addWidget(self._progress_panel)

        # Table
        self._columns = [
            ("select", "", 30),
            ("title", "Title", 350),
            ("authors", "Authors", 220),
            ("year", "Year", 60),
            ("doi", "DOI", 170),
            ("zotero", "Zotero", 80),
            ("paperpile", "Paperpile", 80),
            ("mouseion", "Mouseion", 80),
            ("richest", "Richest Source", 160)
        ]
        self._table = QTableWidget(0, len(self._columns))
        self._table.setHorizontalHeaderLabels([c[1] for c in self._columns])
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._table.setSortingEnabled(True)
        self._table.setStyleSheet(
            "QTableWidget { background: #102F3C; color: #F0E9D5; gridline-color: #1F4858; "
            "border: 1px solid #1F4858; border-radius: 6px; }"
            "QHeaderView::section { background: #16404F; color: #9CA3AF; padding: 6px; "
            "border: none; border-bottom: 1px solid #1F4858; font-weight: 600; }"
            "QTableWidget::item:selected { background: #1F5366; }")
        
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.Interactive)
        hdr.setSectionsMovable(True)
        hdr.setStretchLastSection(True)
        for i, (_k, _l, w) in enumerate(self._columns):
            self._table.setColumnWidth(i, w)
            
        layout.addWidget(self._table, 1)

        # Connect check state change and context menu signals
        self._table.itemChanged.connect(self._on_item_changed)
        self._table.setContextMenuPolicy(Qt.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._show_context_menu)

    def showEvent(self, e):
        super().showEvent(e)
        # Bypassed auto-reload to avoid automatic CPU/concurrency locks on page load.
        # The user must click the Refresh button explicitly to start compile runs.

    def reload(self) -> None:
        self._stats_lbl.setText("re-building consolidated index in background...")
        try:
            if self._loader and self._loader.isRunning():
                return
        except RuntimeError:      # deleted wrapper — same class of bug as the
            self._loader = None   # chat Send deadlock (2026-07-05 audit)
        
        self._progress_lbl.setText("Rebuilding index...")
        self._progress_bar.setRange(0, 4)
        self._progress_bar.setValue(0)
        self._progress_panel.setVisible(True)
        
        self._loader = ComparerLoadWorker(self)
        self._loader.progress.connect(self._on_action_progress)
        self._loader.done.connect(self._on_loaded)
        self._loader.start()

    def _on_loaded(self, groups: list, stats: dict, err: str) -> None:
        self._loader = None
        self._progress_panel.setVisible(False)
        if err:
            self._stats_lbl.setText(f"Error loading comparative index: {err}")
            return
        
        self._groups = groups
        self._stats_data = stats
        self._checked_groups.clear() # Reset selection on reload
        self._update_stats()
        self._apply_filters()

    def _on_action_progress(self, current: int, total: int, message: str) -> None:
        self._progress_lbl.setText(message)
        if total > 0:
            self._progress_bar.setRange(0, total)
            self._progress_bar.setValue(current)
        else:
            self._progress_bar.setRange(0, 0)

    def _update_stats(self) -> None:
        if not self._stats_data:
            self._stats_lbl.setText("loading consolidated index…")
            return
        stats = self._stats_data
        stats_text = (
            f"Consolidated Index: {stats['total_unique']:,} unique entries | "
            f"Zotero: {stats['zotero_count']:,} | "
            f"Paperpile: {stats['paperpile_count']:,} | "
            f"Mouseion: {stats['mouseion_count']:,} | "
            f"Perfect Match (in all 3): {stats['perfect_match']:,} | "
            f"Load duration: {stats['duration']}s"
        )
        if self._checked_groups:
            stats_text += f" | {len(self._checked_groups):,} selected"
        self._stats_lbl.setText(stats_text)

    def _get_filtered_groups(self) -> list[dict]:
        q = self._search.text().lower().strip()
        status_filter = self._status_filter.currentData() or self._status_filter.currentText()
        if status_filter == "All entries":
            status_filter = ""

        filtered = []
        for it in self._groups:
            # Query pre-cached lowercase search blob to avoid string formatting/lower() overhead on GUI thread
            search_blob = it.get("search_blob") or ""
            if q and q not in search_blob:
                continue
                
            in_z = bool(it["zotero_item"])
            in_p = bool(it["paperpile_item"])
            in_m = bool(it["mouseion_item"])
            is_inc = it["best_score"] < 75
            
            if status_filter == "missing_zotero" and in_z:
                continue
            elif status_filter == "missing_paperpile" and in_p:
                continue
            elif status_filter == "missing_mouseion" and in_m:
                continue
            elif status_filter == "in_zotero" and not in_z:
                continue
            elif status_filter == "in_paperpile" and not in_p:
                continue
            elif status_filter == "in_mouseion" and not in_m:
                continue
            elif status_filter == "only_zotero" and (not in_z or in_p or in_m):
                continue
            elif status_filter == "only_paperpile" and (not in_p or in_z or in_m):
                continue
            elif status_filter == "only_mouseion" and (not in_m or in_z or in_p):
                continue
            elif status_filter == "incomplete" and not is_inc:
                continue
                
            filtered.append(it)
        return filtered

    def _select_all(self) -> None:
        filtered = self._get_filtered_groups()
        for it in filtered:
            self._checked_groups[id(it)] = it
        self._update_stats()
        self._apply_filters()

    def _deselect_all(self) -> None:
        self._checked_groups.clear()
        self._update_stats()
        self._apply_filters()

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != 0:
            return
        group = item.data(Qt.UserRole)
        if not group:
            return
        if item.checkState() == Qt.Checked:
            self._checked_groups[id(group)] = group
        else:
            self._checked_groups.pop(id(group), None)
        self._update_stats()

    def _apply_filters(self) -> None:
        self._table.setSortingEnabled(False)
        self._table.blockSignals(True)
        self._table.setRowCount(0)
        
        filtered = self._get_filtered_groups()
        
        row_idx = 0
        for it in filtered:
            self._table.insertRow(row_idx)
            
            # Checkbox (Col 0)
            cb_item = QTableWidgetItem()
            cb_item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            is_checked = id(it) in self._checked_groups
            cb_item.setCheckState(Qt.Checked if is_checked else Qt.Unchecked)
            cb_item.setData(Qt.UserRole, it)
            self._table.setItem(row_idx, 0, cb_item)

            # Title (Col 1)
            title = it["title"] or ""
            t_item = QTableWidgetItem(title)
            t_item.setData(Qt.UserRole, it)
            self._table.setItem(row_idx, 1, t_item)
            
            # Authors (Col 2)
            authors = it["authors"] or ""
            a_item = QTableWidgetItem(authors)
            self._table.setItem(row_idx, 2, a_item)
            
            # Year (Col 3)
            year = it["year"] or ""
            y_item = QTableWidgetItem(year)
            y_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self._table.setItem(row_idx, 3, y_item)
            
            # DOI (Col 4)
            doi = it["doi"] or ""
            d_item = QTableWidgetItem(doi)
            self._table.setItem(row_idx, 4, d_item)
            
            # Presence icons (Cols 5, 6, 7)
            in_z = bool(it["zotero_item"])
            in_p = bool(it["paperpile_item"])
            in_m = bool(it["mouseion_item"])

            z_item = QTableWidgetItem("✓" if in_z else "✗")
            z_item.setTextAlignment(Qt.AlignCenter)
            z_item.setForeground(Qt.green if in_z else Qt.red)
            self._table.setItem(row_idx, 5, z_item)
            
            p_item = QTableWidgetItem("✓" if in_p else "✗")
            p_item.setTextAlignment(Qt.AlignCenter)
            p_item.setForeground(Qt.green if in_p else Qt.red)
            self._table.setItem(row_idx, 6, p_item)
            
            m_item = QTableWidgetItem("✓" if in_m else "✗")
            m_item.setTextAlignment(Qt.AlignCenter)
            m_item.setForeground(Qt.green if in_m else Qt.red)
            self._table.setItem(row_idx, 7, m_item)
            
            # Richest source / completeness (Col 8)
            r_text = f"{it['best_score']}% ({it['best_source']})"
            r_item = QTableWidgetItem(r_text)
            self._table.setItem(row_idx, 8, r_item)
            
            row_idx += 1
            if row_idx >= 1500: # Limit rows shown for table scroll performance
                break
                
        self._table.blockSignals(False)
        self._table.setSortingEnabled(True)

    def _selected_groups(self) -> list[dict]:
        # Return checked groups first if any
        if self._checked_groups:
            return list(self._checked_groups.values())

        # Fallback to highlighted selection
        sel_ranges = self._table.selectedRanges()
        if not sel_ranges:
            out = []
            for r in range(self._table.rowCount()):
                item = self._table.item(r, 1) # title column is now 1
                if item:
                    g = item.data(Qt.UserRole)
                    if g: out.append(g)
            return out
            
        out = []
        for rng in sel_ranges:
            for r in range(rng.topRow(), rng.bottomRow() + 1):
                item = self._table.item(r, 1) # title column is now 1
                if item:
                    g = item.data(Qt.UserRole)
                    if g: out.append(g)
        return out

    def _show_context_menu(self, pos) -> None:
        target_groups = self._selected_groups()
        if not target_groups:
            return

        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu { background: #16404F; color: #F0E9D5; border: 1px solid #1F4858; }"
            "QMenu::item:selected { background: #1F5366; }"
        )
        
        has_z = any(g["zotero_item"] for g in target_groups)
        has_m = any(g["mouseion_item"] for g in target_groups)
        has_p = any(g["paperpile_item"] for g in target_groups)

        act_all = menu.addAction("Consolidate Selected (All Clients)")
        act_all.triggered.connect(lambda: self._run_action("consolidate"))
        
        menu.addSeparator()

        if has_z:
            act_z = menu.addAction("Update Selected in Zotero Only")
            act_z.triggered.connect(lambda: self._run_action("consolidate_zotero"))
        if has_m:
            act_m = menu.addAction("Update Selected in Mouseion Only")
            act_m.triggered.connect(lambda: self._run_action("consolidate_mouseion"))
        if has_p:
            act_p = menu.addAction("Export Updates for Paperpile Only")
            act_p.triggered.connect(lambda: self._run_action("consolidate_paperpile"))

        menu.exec(self._table.viewport().mapToGlobal(pos))

    def _run_action(self, action_type: str) -> None:
        target_groups = self._selected_groups()
        if not target_groups:
            QMessageBox.information(self, "Action", "No entries available to act on.")
            return

        # Confirm actions for all if no selection active and count > 100
        is_explicit = bool(self._checked_groups) or bool(self._table.selectedRanges())
        if not is_explicit and len(target_groups) > 100:
            res = QMessageBox.question(
                self, "Confirm Bulk Action",
                f"No explicit selection active. Run '{action_type}' on all {len(target_groups)} visible entries?",
                QMessageBox.Yes | QMessageBox.No
            )
            if res != QMessageBox.Yes:
                return

        # Safeguard: Warn the user on extremely large consolidation runs (>5000 items)
        if len(target_groups) > 5000:
            res = QMessageBox.warning(
                self, "High Volume Bulk Action",
                f"You are about to run '{action_type}' on {len(target_groups)} entries. "
                "This bulk operation executes database writes in chunks and may take several minutes.\n\n"
                "Are you sure you want to proceed? (It is recommended to filter and select smaller chunks.)",
                QMessageBox.Yes | QMessageBox.No
            )
            if res != QMessageBox.Yes:
                return

        # Disable UI buttons during action
        self._set_buttons_enabled(False)
        self._stats_lbl.setText("Running background operation...")
        
        self._progress_lbl.setText("Starting operation...")
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_panel.setVisible(True)
        
        self._action_worker = ComparerActionWorker(
            action_type, target_groups, merge_sources=self._merge_cb.isChecked(), parent=self
        )
        self._action_worker.progress.connect(self._on_action_progress)
        self._action_worker.done.connect(self._on_action_done)
        self._action_worker.start()

    def _set_buttons_enabled(self, enabled: bool) -> None:
        self._push_z_m_btn.setEnabled(enabled)
        self._push_m_z_btn.setEnabled(enabled)
        self._more_btn.setEnabled(enabled)
        self._consolidate_btn.setEnabled(enabled)
        self._refresh_btn.setEnabled(enabled)
        self._select_all_btn.setEnabled(enabled)
        self._deselect_all_btn.setEnabled(enabled)

    def _on_action_done(self, res: dict, err: str) -> None:
        self._action_worker = None
        self._set_buttons_enabled(True)
        self._progress_panel.setVisible(False)
        
        if err:
            QMessageBox.critical(self, "Action Failed", f"Operation failed: {err}")
            self.reload()
            return

        status = res.get("status")
        if status == "no_match":
            QMessageBox.information(self, "Action Complete", "No entries matched this operation direction.")
        elif status == "ok":
            QMessageBox.information(
                self, "Action Complete",
                f"Successfully synced to {res['target']}! (Success: {res['success']}, Failed: {res['fail']})"
            )
        elif status == "export":
            QMessageBox.information(
                self, "Action Complete",
                f"Exported {res['count']} entries to RIS file for Paperpile:\n{res['path']}\n\n"
                "Please import this file inside your Paperpile application to complete the sync."
            )
        elif status == "consolidate":
            details = res["results"]
            z = details.get("zotero", {})
            m = details.get("mouseion", {})
            p = details.get("paperpile", {})
            
            msg = "Consolidation complete!\n\n"
            if z:
                msg += f"• Zotero: Updated {z.get('success', 0)} items ({z.get('fail', 0)} failed)\n"
            if m:
                msg += f"• Mouseion: Updated {m.get('success', 0)} items ({m.get('fail', 0)} failed)\n"
            if p and p.get("total_exported"):
                msg += f"• Paperpile: Exported {p['total_exported']} updates to RIS file:\n  {p['ris_file']}\n"
                
            QMessageBox.information(self, "Action Complete", msg)
            
        self.reload()
