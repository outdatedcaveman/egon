"""Memory & rules — tabbed read-only viewer for ~/.claude/.../memory/."""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTabWidget, QPlainTextEdit, QFrame,
    QPushButton, QMessageBox, QComboBox,
)


def find_all_memory_dirs() -> list[tuple[str, Path]]:
    """Discover all folders inside ~/.claude/projects/ containing a 'memory' folder with markdown files."""
    base = Path.home() / ".claude" / "projects"
    results = []
    if base.exists():
        for p in base.iterdir():
            if p.is_dir():
                mem_dir = p / "memory"
                if mem_dir.exists() and any(mem_dir.glob("*.md")):
                    slug = p.name
                    # Try to decode the path representation used by Claude CLI
                    decoded = slug
                    if decoded.startswith("C--") or decoded.startswith("c--"):
                        decoded = "C:\\" + decoded[3:]
                    decoded = decoded.replace("-", "\\")
                    while "\\\\" in decoded:
                        decoded = decoded.replace("\\\\", "\\")
                    
                    results.append((decoded, mem_dir))
                    
    # Sort to prioritize "Claude-Code" or current workspace first
    def sort_key(item):
        name, path = item
        if "claude-code" in str(path).lower():
            return (0, name)
        if "egon" in str(path).lower():
            return (1, name)
        return (2, name)
        
    results.sort(key=sort_key)
    return results


class MemoryPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._mem_dirs = find_all_memory_dirs()
        self._current_mem_dir: Path | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 24, 28, 24)
        outer.setSpacing(10)

        # Title + Project selector row
        top_row = QHBoxLayout()
        title = QLabel("Memory & rules")
        title.setStyleSheet("font-size: 22px; font-weight: 700; color: #f5f5f7;")
        top_row.addWidget(title)
        top_row.addStretch(1)

        top_row.addWidget(QLabel("Project:"))
        self._project_cb = QComboBox()
        self._project_cb.setStyleSheet(
            "QComboBox { background: #212328; color: #f5f5f7; border: 1px solid #22252a; "
            "border-radius: 4px; padding: 4px 10px; min-width: 250px; }"
            "QComboBox::drop-down { border: none; }"
            "QComboBox QAbstractItemView { background: #0c0d0f; color: #f5f5f7; selection-background-color: #2a2d34; }"
        )
        for name, path in self._mem_dirs:
            self._project_cb.addItem(name, str(path))
        self._project_cb.currentIndexChanged.connect(self._on_project_changed)
        top_row.addWidget(self._project_cb)
        outer.addLayout(top_row)

        # Header layout for subtext + action button
        hdr = QHBoxLayout()
        self._sub_label = QLabel("Select a project above to view memory documents.")
        self._sub_label.setTextFormat(Qt.RichText)
        self._sub_label.setStyleSheet("color: #76767f;")
        hdr.addWidget(self._sub_label)
        hdr.addStretch(1)

        self._edit_btn = QPushButton("✎ Open in Editor")
        self._edit_btn.setStyleSheet(
            "QPushButton { background: #212328; color: #f5f5f7; border: 1px solid #22252a; "
            "border-radius: 4px; padding: 6px 14px; font-weight: 600; }"
            "QPushButton:hover { background: #2a2d34; }"
        )
        self._edit_btn.clicked.connect(self._open_in_editor)
        hdr.addWidget(self._edit_btn)
        outer.addLayout(hdr)

        self._tabs = QTabWidget()
        self._tabs.setStyleSheet(
            "QTabWidget::pane { border: 1px solid #22252a; background: #0c0d0f; border-radius: 4px; }"
            "QTabBar::tab { background: #0c0d0f; color: #76767f; padding: 6px 14px; "
            "border: 1px solid #22252a; border-bottom: none; }"
            "QTabBar::tab:selected { background: #212328; color: #f5f5f7; font-weight: 600; }"
        )
        outer.addWidget(self._tabs, 1)

        # Trigger initial selection
        if self._mem_dirs:
            self._project_cb.setCurrentIndex(0)
            self._on_project_changed(0)
        else:
            self._sub_label.setText("No Claude memory folders found under ~/.claude/projects/.")
            self._edit_btn.setEnabled(False)

    def _on_project_changed(self, index: int) -> None:
        if index < 0 or index >= len(self._mem_dirs):
            return
        name, mem_dir = self._mem_dirs[index]
        self._current_mem_dir = mem_dir
        self._sub_label.setText(f"Read-only viewer for <code>{mem_dir}</code>. Edit through your editor.")

        self._tabs.clear()
        files = sorted(mem_dir.glob("*.md"))
        if not files:
            empty = QLabel("No memory files found in this directory.")
            empty.setStyleSheet("color: #76767f; padding: 12px;")
            self._tabs.addTab(empty, "Empty")
            self._edit_btn.setEnabled(False)
            return

        for f in files:
            text_view = QPlainTextEdit()
            text_view.setReadOnly(True)
            text_view.setFont(QFont("Cascadia Mono", 10))
            text_view.setStyleSheet(
                "QPlainTextEdit { background: #0c0d0f; color: #f5f5f7; border: none; padding: 12px; }"
            )
            text_view.setProperty("file_path", str(f))
            try:
                text_view.setPlainText(f.read_text(encoding="utf-8"))
            except Exception as e:
                text_view.setPlainText(f"(error: {e})")
            self._tabs.addTab(text_view, f.stem)

        self._edit_btn.setEnabled(True)

    def _open_in_editor(self) -> None:
        widget = self._tabs.currentWidget()
        if widget:
            path_str = widget.property("file_path")
            if path_str:
                try:
                    os.startfile(path_str)
                except Exception as e:
                    QMessageBox.warning(self, "Failed to open", f"Could not open file: {e}")
