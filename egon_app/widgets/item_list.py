"""Reusable data-browser widget — search · per-column sort/filter · stats ·
multi-select · export (CSV/JSON/RIS/BibTeX).

Bruno 2026-05-22 spec:
  - Stats bar: total entries · breakdown by type · last updated
  - Columns draggable (reorderable) AND resizable
  - Filter + order on every column (native sort on header click; filter box
    with a per-column scope dropdown)
  - Select-all button + multi-select
  - Export selected (or all filtered) to .csv/.json/.ris/.bib

A *provider* returns a list of dicts; columns map field-keys to headers.
Loads lazily on first show (perf).
"""
from __future__ import annotations

import csv
import io
import json
from typing import Any, Callable

from PySide6.QtCore import Qt, QTimer, QThread, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QFrame,
    QTableWidget, QTableWidgetItem, QPushButton, QHeaderView, QComboBox,
    QAbstractItemView, QMessageBox, QMenu, QFileDialog,
)

ColumnSpec = tuple[str, str, int]
ActionSpec = tuple[str, Callable[[list[dict]], None]]


# ── exporters ───────────────────────────────────────────────────────────────

def _export_csv(rows: list[dict], keys: list[str]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=keys, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({k: r.get(k, "") for k in keys})
    return buf.getvalue()


def _export_json(rows: list[dict], keys: list[str]) -> str:
    return json.dumps([{k: r.get(k, "") for k in keys} for r in rows],
                      indent=2, ensure_ascii=False)


def _export_ris(rows: list[dict], _keys) -> str:
    out = []
    for r in rows:
        out.append("TY  - JOUR")
        if r.get("title"):       out.append(f"TI  - {r['title']}")
        for a in str(r.get("authors") or r.get("author") or "").split(","):
            a = a.strip()
            if a: out.append(f"AU  - {a}")
        if r.get("year"):        out.append(f"PY  - {r['year']}")
        if r.get("publication"): out.append(f"JO  - {r['publication']}")
        if r.get("doi"):         out.append(f"DO  - {r['doi']}")
        if r.get("url"):         out.append(f"UR  - {r['url']}")
        if r.get("abstract"):    out.append(f"AB  - {r['abstract']}")
        for t in str(r.get("tags") or "").replace(";", ",").split(","):
            t = t.strip()
            if t: out.append(f"KW  - {t}")
        out.append("ER  - ")
        out.append("")
    return "\n".join(out)


def _export_bibtex(rows: list[dict], _keys) -> str:
    import re
    out = []
    for i, r in enumerate(rows):
        first_author = str(r.get("authors") or r.get("author") or "anon").split(",")[0]
        surname = re.sub(r"[^A-Za-z]", "", first_author.split()[0]) if first_author.split() else "anon"
        key = f"{surname.lower()}{r.get('year','') or i}"
        out.append(f"@article{{{key},")
        if r.get("title"):       out.append(f"  title = {{{r['title']}}},")
        authors = str(r.get("authors") or r.get("author") or "")
        if authors:
            out.append(f"  author = {{{' and '.join(a.strip() for a in authors.split(',') if a.strip())}}},")
        if r.get("year"):        out.append(f"  year = {{{r['year']}}},")
        if r.get("publication"): out.append(f"  journal = {{{r['publication']}}},")
        if r.get("doi"):         out.append(f"  doi = {{{r['doi']}}},")
        if r.get("url"):         out.append(f"  url = {{{r['url']}}},")
        out.append("}")
        out.append("")
    return "\n".join(out)


_EXPORTERS = {
    "CSV (.csv)":     ("csv",  _export_csv),
    "JSON (.json)":   ("json", _export_json),
    "RIS (.ris)":     ("ris",  _export_ris),
    "BibTeX (.bib)":  ("bib",  _export_bibtex),
}


class _LoadWorker(QThread):
    # rows, lib_stats(dict), err
    done = Signal(list, dict, str)

    def __init__(self, provider: Callable[[], list[dict]],
                 stats_provider: Callable[[], dict] | None = None, parent=None):
        super().__init__(parent)
        self._provider = provider
        self._stats_provider = stats_provider

    def run(self) -> None:
        try:
            rows = self._provider() or []
            lib = {}
            if self._stats_provider:
                try:
                    lib = self._stats_provider() or {}
                except Exception:
                    lib = {}
            self.done.emit(list(rows), lib, "")
        except Exception as e:
            self.done.emit([], {}, f"{type(e).__name__}: {e}"[:300])


class ItemListWidget(QWidget):
    def __init__(self,
                 provider: Callable[[], list[dict]],
                 columns: list[ColumnSpec],
                 actions: list[ActionSpec] | None = None,
                 empty_message: str = "no items yet",
                 type_field: str | None = None,
                 stats_provider: Callable[[], dict] | None = None,
                 cache_key: str | None = None,
                 parent=None):
        super().__init__(parent)
        self._provider = provider
        self._columns = columns
        self._keys = [c[0] for c in columns]
        self._actions = actions or []
        # disk cache key for stale-while-revalidate (instant reopen, never blank)
        self._cache_key = cache_key
        self._empty_message = empty_message
        # field used for the "by type" stats breakdown — auto-detect if None
        self._type_field = type_field or self._guess_type_field()
        # optional full-DB aggregate stats {total, by_type, last_updated} — used
        # so the stats bar reflects the WHOLE database, not just the loaded
        # browsing window. Bruno 2026-05-22.
        self._stats_provider = stats_provider
        self._lib_stats: dict = {}
        self._rows: list[dict] = []
        self._filtered: list[dict] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)

        # ── toolbar ──
        toolbar = QFrame()
        toolbar.setStyleSheet("QFrame { background: #16404F; border: 1px solid #1F4858; border-radius: 6px; }")
        tl = QHBoxLayout(toolbar)
        tl.setContentsMargins(8, 4, 8, 4)
        tl.setSpacing(8)

        self._search = QLineEdit()
        self._search.setPlaceholderText("filter")
        self._search.setMinimumWidth(180)
        self._search.setStyleSheet(
            "QLineEdit { background: #102F3C; color: #F0E9D5; border: 1px solid #1F4858; "
            "border-radius: 4px; padding: 4px 8px; }")
        self._search.textChanged.connect(self._apply_filter)
        tl.addWidget(self._search)

        tl.addWidget(QLabel("in:"))
        self._filter_col = QComboBox()
        self._filter_col.addItem("All columns", None)
        for key, label, _w in columns:
            self._filter_col.addItem(label, key)
        self._filter_col.currentIndexChanged.connect(self._apply_filter)
        tl.addWidget(self._filter_col)

        self._select_all_btn = QPushButton("Select all")
        self._select_all_btn.clicked.connect(self._table_select_all)
        tl.addWidget(self._select_all_btn)
        self._select_none_btn = QPushButton("Clear")
        self._select_none_btn.clicked.connect(lambda: self._table.clearSelection())
        tl.addWidget(self._select_none_btn)

        tl.addStretch(1)

        for label, fn in self._actions:
            b = QPushButton(label)
            b.setStyleSheet(
                "QPushButton { background: #60A5A8; color: white; padding: 4px 12px; "
                "border-radius: 3px; font-weight: 600; border: none; }"
                "QPushButton:hover { background: #7BC5C7; }")
            b.clicked.connect(lambda _=False, fn=fn: self._run_action(fn))
            tl.addWidget(b)

        # Export menu
        self._export_btn = QPushButton("Export ▾")
        self._export_btn.setStyleSheet(
            "QPushButton { background: #D4A24C; color: #102F3C; padding: 4px 12px; "
            "border-radius: 3px; font-weight: 700; border: none; }")
        menu = QMenu(self._export_btn)
        for label in _EXPORTERS:
            menu.addAction(label, lambda lbl=label: self._export(lbl))
        self._export_btn.setMenu(menu)
        tl.addWidget(self._export_btn)

        self._refresh_btn = QPushButton("Refresh")
        self._refresh_btn.clicked.connect(self.reload)
        tl.addWidget(self._refresh_btn)
        root.addWidget(toolbar)

        # ── stats bar ──
        self._stats = QLabel("loading…")
        self._stats.setStyleSheet("color: #9CA3AF; font-size: 11px; padding: 0 4px;")
        self._stats.setWordWrap(True)
        root.addWidget(self._stats)

        # ── table ──
        self._table = QTableWidget(0, len(columns))
        self._table.setHorizontalHeaderLabels([c[1] for c in columns])
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._table.setSortingEnabled(True)            # native per-column sort
        self._table.setStyleSheet(
            "QTableWidget { background: #102F3C; color: #F0E9D5; gridline-color: #1F4858; "
            "border: 1px solid #1F4858; border-radius: 6px; }"
            "QHeaderView::section { background: #16404F; color: #9CA3AF; padding: 6px; "
            "border: none; border-bottom: 1px solid #1F4858; font-weight: 600; }"
            "QTableWidget::item:selected { background: #1F5366; }")
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.Interactive)   # resizable
        hdr.setSectionsMovable(True)                        # DRAGGABLE/reorderable
        hdr.setStretchLastSection(True)
        hdr.setMinimumSectionSize(48)
        for i, (_k, _l, w) in enumerate(columns):
            self._table.setColumnWidth(i, 360 if w < 0 else w)
        root.addWidget(self._table, 1)

        self._loaded = False

    def _guess_type_field(self) -> str | None:
        for cand in ("kind", "type", "publication", "tags"):
            if cand in self._keys:
                return cand
        return None

    def showEvent(self, e):
        super().showEvent(e)
        if not self._loaded:
            self._loaded = True
            # STALE-WHILE-REVALIDATE: paint the disk-cached rows instantly so
            # the table is never blank on reopen, THEN refresh in background.
            if self._cache_key:
                from egon_app.widgets import _cache
                cached, _age = _cache.read(self._cache_key)
                if cached:
                    self._rows = cached
                    self._apply_filter()
            QTimer.singleShot(30, self.reload)

    # ------------------------------------------------------------------ data
    def reload(self) -> None:
        # Only show "loading…" when we have nothing on screen yet; otherwise
        # keep the (stale) rows visible and show a subtle refreshing hint.
        self._stats.setText("refreshing…" if self._rows else "loading…")
        self._refresh_btn.setEnabled(False)
        self._w = _LoadWorker(self._provider, self._stats_provider, parent=self)
        self._w.done.connect(self._on_loaded)
        self._w.start()

    def _on_loaded(self, rows: list[dict], lib_stats: dict, err: str) -> None:
        self._refresh_btn.setEnabled(True)
        if err:
            # On refresh error, KEEP showing whatever we already had (cache)
            # rather than blanking — only show error if we have nothing.
            if not self._rows:
                self._stats.setText(f"error: {err}")
                self._stats.setStyleSheet("color: #D67A6A; font-size: 11px; padding: 0 4px;")
                self._render([])
            return
        self._rows = rows
        self._lib_stats = lib_stats or {}
        if self._cache_key and rows:
            from egon_app.widgets import _cache
            _cache.write(self._cache_key, rows)
        self._stats.setStyleSheet("color: #9CA3AF; font-size: 11px; padding: 0 4px;")
        self._apply_filter()

    def _apply_filter(self, *_) -> None:
        q = (self._search.text() or "").strip().lower()
        col = self._filter_col.currentData()
        if not q:
            self._filtered = list(self._rows)
        elif col:
            self._filtered = [r for r in self._rows if q in str(r.get(col, "")).lower()]
        else:
            self._filtered = [r for r in self._rows
                              if any(q in str(v).lower() for v in r.values())]
        self._update_stats()
        self._render(self._filtered)

    def _update_stats(self) -> None:
        n = len(self._filtered)
        lib = self._lib_stats or {}
        true_total = lib.get("total")
        loaded = len(self._rows)

        # Headline count: prefer the full-DB total from the aggregate query.
        # Show the rendered window honestly when it's smaller than the DB.
        if true_total is not None:
            head = f"<b style='color:#F0E9D5;'>{true_total:,}</b> in library"
            q = (self._search.text() or "").strip()
            if q:
                head += f" · {n:,} match filter"
            elif loaded < true_total:
                head += f" · showing {loaded:,}"
            parts = [head]
        else:
            parts = [f"<b style='color:#F0E9D5;'>{n:,}</b>"
                     + (f" of {loaded:,}" if n != loaded else "") + " entries"]

        # breakdown by type — full-DB aggregate if available, else loaded sample
        by_type = lib.get("by_type")
        if by_type:
            parts.append(" · ".join(f"{k}: {v:,}" for k, v in
                                    list(by_type.items())[:6]))
        elif self._type_field:
            from collections import Counter
            c = Counter()
            for r in self._filtered:
                v = str(r.get(self._type_field, "") or "—").split(",")[0].strip()[:24]
                c[v] += 1
            if len(c) > 1:
                parts.append(" · ".join(f"{k}: {v}" for k, v in c.most_common(6)))

        # last updated
        last = lib.get("last_updated")
        if not last:
            dates = [str(r.get("added") or r.get("date") or "") for r in self._filtered]
            dates = [d for d in dates if d]
            last = max(dates)[:10] if dates else ""
        if last:
            parts.append(f"last updated {last}")
        self._stats.setText("  ·  ".join(parts))

    def _render(self, rows: list[dict]) -> None:
        self._table.setSortingEnabled(False)   # disable while bulk-inserting
        self._table.setRowCount(0)
        if not rows:
            self._table.insertRow(0)
            it = QTableWidgetItem(self._empty_message)
            it.setForeground(Qt.GlobalColor.gray)
            self._table.setItem(0, 0, it)
            if len(self._columns) > 1:
                self._table.setSpan(0, 0, 1, len(self._columns))
            self._table.setSortingEnabled(True)
            return
        cap = 5000
        for r_idx, row in enumerate(rows[:cap]):
            self._table.insertRow(r_idx)
            for c_idx, (key, _label, _w) in enumerate(self._columns):
                val = row.get(key, "")
                if isinstance(val, (list, tuple)):
                    val = ", ".join(str(x) for x in val)
                item = QTableWidgetItem(str(val))
                if c_idx == 0:
                    item.setData(Qt.UserRole, row)
                self._table.setItem(r_idx, c_idx, item)
        self._table.setSortingEnabled(True)

    # ------------------------------------------------------------------ selection
    def _selected_rows(self) -> list[dict]:
        rows = []
        for idx in self._table.selectionModel().selectedRows():
            cell = self._table.item(idx.row(), 0)
            if cell:
                d = cell.data(Qt.UserRole)
                if d:
                    rows.append(d)
        return rows

    def _table_select_all(self) -> None:
        self._table.selectAll()

    def _target_rows(self) -> list[dict]:
        """Selected rows, or all filtered if none selected."""
        sel = self._selected_rows()
        return sel if sel else list(self._filtered)

    # ------------------------------------------------------------------ actions / export
    def _run_action(self, fn: Callable[[list[dict]], None]) -> None:
        rows = self._selected_rows()
        if not rows:
            n = len(self._filtered)
            if n > 50:
                if QMessageBox.question(self, "Apply to all?",
                        f"No rows selected. Apply to all {n:,} filtered items?",
                        QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                    return
            rows = list(self._filtered)
        try:
            fn(rows)
        except Exception as e:
            QMessageBox.warning(self, "Action failed", f"{type(e).__name__}: {e}")

    def _export(self, label: str) -> None:
        ext, fn = _EXPORTERS[label]
        rows = self._target_rows()
        if not rows:
            QMessageBox.information(self, "Export", "Nothing to export.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, f"Export {len(rows)} items as {ext.upper()}",
            f"egon_export.{ext}", f"{label} (*.{ext});;All files (*.*)")
        if not path:
            return
        try:
            text = fn(rows, self._keys)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            QMessageBox.information(self, "Exported",
                f"{len(rows):,} items → {path}")
        except Exception as e:
            QMessageBox.warning(self, "Export failed", f"{type(e).__name__}: {e}")
