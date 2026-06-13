"""Artifacts — the universal file-explorer metaharness.

Bruno 2026-06-12: "make Artifacts the home to our own file explorer — one
that allows navigation and enriched action over ALL my files, regardless of
where they are (PC, Android, Drive, other programs)."

One provenance-agnostic table over every indexed file:
  • PC + Drive   — state/files_index.jsonl   (lib/file_indexer, 6h refresh)
  • Android      — state/files_index_phone.jsonl (lib/phone_files, on demand)

Deliberately NOT a Windows Explorer clone: no move/rename/delete (knowledge
management, not file management — and the never-delete rule stays
structural). The enriched actions are what Explorer can't do:
  🔗 Connect    — semantic neighbors of a file across ALL archives (Zotero,
                  bookmarks, mind memory, other files) via the mind engine.
  📌 Pin        — queue a Drive placeholder for tier-2 text extraction
                  (state/hydration_queue.json; see docs/FILES_INTEGRATION.md).
  Open / Reveal / Copy path — the basics, provenance-aware (phone rows
                  explain how to fetch instead of failing).
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QDialog, QListWidget,
    QListWidgetItem, QApplication, QMessageBox, QPushButton,
)

from egon_app.widgets.item_list import ItemListWidget

ROOT = Path(__file__).resolve().parent.parent.parent
INDEX_MAIN = ROOT / "state" / "files_index.jsonl"
INDEX_PHONE = ROOT / "state" / "files_index_phone.jsonl"
HYDRATION_QUEUE = ROOT / "state" / "hydration_queue.json"

_ROOT_LABELS = {
    "Google Drive": "☁️ Drive",
    "My Drive": "☁️ Drive",
    "EgonVault": "☁️ Vault",
    "Documents": "💻 PC",
}


def _root_label(root: str) -> str:
    if root.startswith("phone:"):
        return "📱 Phone"
    for frag, label in _ROOT_LABELS.items():
        if frag in root:
            return label
    return "💻 PC"


def _fmt_size(n) -> str:
    try:
        n = int(n)
    except Exception:
        return ""
    if n >= 1e9: return f"{n / 1e9:.1f} GB"
    if n >= 1e6: return f"{n / 1e6:.1f} MB"
    if n >= 1e3: return f"{n / 1e3:.0f} KB"
    return f"{n} B"


def _load_rows() -> list[dict]:
    rows: list[dict] = []
    for idx_path in (INDEX_MAIN, INDEX_PHONE):
        if not idx_path.exists():
            continue
        try:
            with idx_path.open(encoding="utf-8") as f:
                for line in f:
                    try:
                        it = json.loads(line)
                    except Exception:
                        continue
                    rows.append({
                        "name": it.get("name", ""),
                        "where": _root_label(it.get("root", "")),
                        "ext": (it.get("ext") or "").lstrip("."),
                        "size": _fmt_size(it.get("size")),
                        "modified": datetime.fromtimestamp(
                            it.get("mtime") or 0).strftime("%Y-%m-%d"),
                        "path": it.get("path", ""),
                        "_size_raw": it.get("size") or 0,
                        "_mtime": it.get("mtime") or 0,
                    })
        except Exception:
            continue
    rows.sort(key=lambda r: -r["_mtime"])
    return rows


def _stats() -> dict:
    rows = _load_rows()
    by = {}
    for r in rows:
        by[r["where"]] = by.get(r["where"], 0) + 1
    return {"total": len(rows), "by_type": by}


# ── actions ──────────────────────────────────────────────────────────────────
def _act_open(rows: list[dict]) -> None:
    for r in rows[:5]:
        p = r.get("path", "")
        if p.startswith("/sdcard"):
            QMessageBox.information(
                None, "Phone file",
                f"{r['name']} lives on the phone.\n\nFetch it with:\n"
                f"adb pull \"{p}\"\n\n(Or open it on the phone itself.)")
            continue
        try:
            os.startfile(p)  # noqa: S606 — user-initiated open
        except Exception as e:
            QMessageBox.warning(None, "Open failed", f"{p}\n\n{e}")


def _act_reveal(rows: list[dict]) -> None:
    for r in rows[:3]:
        p = r.get("path", "")
        if p.startswith("/sdcard"):
            continue
        try:
            subprocess.Popen(["explorer", "/select,", p],
                             creationflags=0x08000000)
        except Exception:
            pass


def _act_copy_paths(rows: list[dict]) -> None:
    QApplication.clipboard().setText("\n".join(r.get("path", "") for r in rows))


def _act_pin(rows: list[dict]) -> None:
    """Queue files for tier-2 hydration/text extraction."""
    queue = []
    try:
        queue = json.loads(HYDRATION_QUEUE.read_text(encoding="utf-8"))
    except Exception:
        pass
    known = {q.get("path") for q in queue}
    added = 0
    for r in rows:
        if r.get("path") and r["path"] not in known:
            queue.append({"path": r["path"], "pinned_at":
                          datetime.now().isoformat(timespec="seconds")})
            added += 1
    HYDRATION_QUEUE.parent.mkdir(parents=True, exist_ok=True)
    HYDRATION_QUEUE.write_text(json.dumps(queue, indent=2), encoding="utf-8")
    QMessageBox.information(
        None, "Pinned for extraction",
        f"{added} file(s) queued for tier-2 text extraction "
        f"({len(queue)} total in queue).\nThe extractor processes the queue "
        "with the 6h index refresh — see docs/FILES_INTEGRATION.md.")


class _ConnectDialog(QDialog):
    """Semantic neighbors of a file, across every archive."""

    from PySide6.QtCore import Signal as _Signal
    _ready = _Signal(list)   # worker thread → UI thread, auto-queued

    def __init__(self, filename: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"🔗 Connected to: {filename[:60]}")
        self.resize(680, 460)
        self.setStyleSheet("background: #0B1F28; color: #F0E9D5;")
        v = QVBoxLayout(self)
        self._status = QLabel("searching the archives…")
        self._status.setStyleSheet("color: #9CA3AF;")
        v.addWidget(self._status)
        self._list = QListWidget()
        self._list.setStyleSheet(
            "QListWidget { background: #102F3C; border: 1px solid #1F4858; "
            "border-radius: 6px; } QListWidget::item { padding: 7px; }"
            "QListWidget::item:selected { background: #1F5366; }")
        self._list.itemActivated.connect(self._open_item)
        v.addWidget(self._list, 1)

        self._ready.connect(self._fill)
        import threading

        def _bg():
            from egon_app.api import post_json
            stem = Path(filename).stem.replace("_", " ").replace("-", " ")
            res = post_json("http://127.0.0.1:8000/api/v1/mind/connect",
                            {"text": stem, "limit": 16}, timeout=30.0) or {}
            self._ready.emit(res.get("connections") or [])

        threading.Thread(target=_bg, daemon=True).start()

    def _open_item(self, item: QListWidgetItem) -> None:
        url = item.data(Qt.UserRole)
        if url:
            import webbrowser
            webbrowser.open(url)

    def _fill(self, conns: list) -> None:  # runs on the UI thread (queued)
        self._status.setText(
            f"{len(conns)} connections across your archives — double-click to open"
            if conns else "no strong connections found")
        emoji = {"files": "📁", "paperpile": "📄", "zotero": "📚",
                 "chrome_bookmarks": "🔖", "instapaper": "📰",
                 "mind-memory": "🧠", "letterboxd": "🎬",
                 "notion_workspace": "🟦", "youtube_music": "🎵"}
        for c in conns:
            label = (f"{emoji.get(c.get('source'), '•')} "
                     f"{(c.get('title') or '')[:84]}   "
                     f"[{c.get('source', '')}]")
            it = QListWidgetItem(label)
            it.setData(Qt.UserRole, c.get("url"))
            self._list.addItem(it)


def _act_connect(rows: list[dict]) -> None:
    if not rows:
        return
    dlg = _ConnectDialog(rows[0].get("name", ""))
    dlg.exec()


def _act_index_phone(rows: list[dict]) -> None:  # rows unused; toolbar action
    import threading

    def _bg():
        from lib import phone_files
        phone_files.build()

    threading.Thread(target=_bg, daemon=True).start()
    QMessageBox.information(
        None, "Phone indexing",
        "Indexing phone files over ADB in the background (Download, "
        "Documents, Books, DCIM, Screenshots — metadata only).\n"
        "Hit Refresh in ~30s to see them appear with 📱 provenance.")


class ArtifactsPage(QWidget):
    _import_done = Signal(dict)   # export-import worker → UI thread (queued)

    def __init__(self, parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(24, 18, 24, 18)
        v.setSpacing(10)

        headrow = QHBoxLayout()
        head = QLabel("🗂  Artifacts — every file, every device")
        head.setStyleSheet("color: #F0E9D5; font-size: 20px; font-weight: 700;")
        headrow.addWidget(head)
        headrow.addStretch(1)
        self._import_status = QLabel("")
        self._import_status.setStyleSheet("color: #9CA3AF; font-size: 11px;")
        headrow.addWidget(self._import_status)
        imp = QPushButton("📥 Import data export…")
        imp.setToolTip(
            "Drop in a Google Takeout, TV Time, or Amazon data-export zip. "
            "Egon detects the vendor, extracts it, and merges everything in "
            "(YouTube history, Fit, Discover, Gemini, Kindle, episodes…).")
        imp.setStyleSheet(
            "QPushButton { background: #D4A24C; color: #102F3C; padding: 7px 14px; "
            "border-radius: 4px; font-weight: 700; border: none; }"
            "QPushButton:hover { background: #E0B45E; }")
        imp.clicked.connect(self._import_export)
        headrow.addWidget(imp)
        v.addLayout(headrow)
        sub = QLabel(
            "PC · Drive · Phone in one table. 🔗 Connect finds what any file "
            "relates to across ALL your archives; 📌 Pin queues Drive "
            "placeholders for text extraction. 📥 Import pulls in vendor data "
            "exports (Takeout etc.). Newest first; filter to dig.")
        sub.setStyleSheet("color: #9CA3AF; font-size: 12px;")
        sub.setWordWrap(True)
        v.addWidget(sub)

        self._browser = ItemListWidget(
            provider=_load_rows,
            columns=[
                ("name", "Name", 360),
                ("where", "Where", 80),
                ("ext", "Type", 60),
                ("size", "Size", 80),
                ("modified", "Modified", 90),
                ("path", "Path", -1),
            ],
            actions=[
                ("🔗 Connect", _act_connect),
                ("Open", _act_open),
                ("Reveal", _act_reveal),
                ("Copy path", _act_copy_paths),
                ("📌 Pin", _act_pin),
                ("📱 Index phone", _act_index_phone),
            ],
            type_field="where",
            stats_provider=_stats,
            cache_key="artifacts_files",
            empty_message="no files indexed yet — the 6h core cycle builds the index",
        )
        v.addWidget(self._browser, 1)

    # ── data-export import (Takeout / TV Time / Amazon DSAR) ─────────────────
    def _import_export(self) -> None:
        from PySide6.QtWidgets import QFileDialog
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Choose data-export zip(s) — Takeout, TV Time, Amazon…",
            str(Path.home() / "Downloads"), "Export archives (*.zip)")
        if not paths:
            return
        self._import_status.setText("importing… (large exports take a minute)")
        try:
            self._import_done.disconnect()
        except Exception:
            pass
        self._import_done.connect(self._on_import_done)
        import threading, shutil

        def _bg():
            inbox = ROOT / "state" / "inbox"
            inbox.mkdir(parents=True, exist_ok=True)
            for p in paths:
                try:
                    dest = inbox / Path(p).name
                    if not dest.exists():
                        shutil.copy2(p, dest)
                except Exception:
                    pass
            try:
                from lib import export_inbox
                res = export_inbox.process()
            except Exception as e:
                res = {"status": "error", "error": str(e)[:200]}
            self._import_done.emit(res)

        threading.Thread(target=_bg, daemon=True).start()

    def _on_import_done(self, res: dict) -> None:
        imported = res.get("imported") or {}
        if res.get("status") == "error":
            self._import_status.setText(f"import failed: {res.get('error','')}")
            return
        if not imported:
            self._import_status.setText("nothing new to import (already absorbed)")
            return
        lines = []
        for zipname, rep in imported.items():
            vendor = rep.get("vendor", "?")
            counts = ", ".join(f"{k}: {v}" for k, v in rep.items()
                               if isinstance(v, int))
            lines.append(f"{zipname} [{vendor}] — {counts or 'extracted + indexed'}")
        self._import_status.setText("✓ imported — refresh in ~30s")
        QMessageBox.information(
            None, "Data export imported",
            "Absorbed into the mind, both mirrors, and the Connect index:\n\n"
            + "\n".join(lines)
            + "\n\nIt flows to Notion/Obsidian on the next mirror cycle.")
