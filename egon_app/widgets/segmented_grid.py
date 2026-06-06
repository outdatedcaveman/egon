"""SegmentedGridWidget — one tab, multiple sub-views via a segmented selector.

Bruno 2026-05-22: "integrate navigation such that all of YouTube falls into a
single subwindow." Instead of separate top-level tabs for Liked / Playlists /
Subscriptions / Watch-history, this puts a compact segmented control at the
top of ONE tab and swaps the PosterGrid beneath it. Each segment lazy-loads
its own provider on first view.
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QStackedWidget, QButtonGroup,
)

from egon_app.widgets.poster_grid import PosterGridWidget

# (label, provider, shape, sort_fields, empty_message, stats_fn)
SegmentSpec = tuple


class SegmentedGridWidget(QWidget):
    def __init__(self, segments: list[dict], on_click: Callable | None = None, parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)

        # segmented control
        bar = QHBoxLayout()
        bar.setSpacing(0)
        bar.setContentsMargins(0, 0, 0, 0)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._stack = QStackedWidget()

        for i, seg in enumerate(segments):
            btn = QPushButton(seg["label"])
            btn.setCheckable(True)
            btn.setCursor(self.cursor())
            first = i == 0
            last = i == len(segments) - 1
            radius = (f"border-top-left-radius:6px;border-bottom-left-radius:6px;" if first else "") + \
                     (f"border-top-right-radius:6px;border-bottom-right-radius:6px;" if last else "")
            btn.setStyleSheet(
                "QPushButton { background:#0B1F28; color:#9CA3AF; border:1px solid #1F4858; "
                f"padding:6px 16px; font-size:12px; {radius} }}"
                "QPushButton:checked { background:#16404F; color:#F0E9D5; font-weight:600; "
                "border:1px solid #60A5A8; }")
            self._group.addButton(btn, i)
            bar.addWidget(btn)

            grid = PosterGridWidget(
                provider=seg["provider"],
                on_click=on_click,
                shape=seg.get("shape", "landscape"),
                sort_fields=seg.get("sort_fields"),
                stats_fn=seg.get("stats_fn"),
                empty_message=seg.get("empty_message", "no items yet"),
                cache_key=seg.get("cache_key"),
            )
            self._stack.addWidget(grid)

        bar.addStretch(1)
        v.addLayout(bar)
        v.addWidget(self._stack, 1)

        self._group.idClicked.connect(self._stack.setCurrentIndex)
        # default to first segment
        if segments:
            self._group.button(0).setChecked(True)
            self._stack.setCurrentIndex(0)
