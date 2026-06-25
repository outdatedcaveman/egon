"""Generic page used by source-tab views (Artifacts, Media, References, etc).

Each of those views in the old NiceGUI app was a curated card-grid drawn
from `data.last_pass()['sources']` plus some category-specific filtering.
This widget covers that pattern with a configurable filter so we don't
copy-paste seven nearly-identical files.
"""
from __future__ import annotations

from typing import Callable, Iterable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QFrame,
    QScrollArea, QSizePolicy,
)

from egon_app import data


def _make_card(title: str, body_html: str) -> QFrame:
    f = QFrame()
    f.setObjectName("card")
    v = QVBoxLayout(f)
    v.setContentsMargins(16, 12, 16, 12)
    v.setSpacing(4)
    t = QLabel(title)
    t.setStyleSheet("font-size: 14px; font-weight: 600; color: #f5f5f7;")
    v.addWidget(t)
    b = QLabel(body_html)
    b.setTextFormat(Qt.RichText)
    b.setWordWrap(True)
    b.setStyleSheet("color: #76767f; font-size: 12px; line-height: 1.5;")
    v.addWidget(b)
    return f


class SourceListPage(QWidget):
    """Generic 'list of source cards' view.

    title:        h1 text
    subtitle:     descriptive line
    source_filter: function(source_id, info_dict) -> bool — which sources to show
    body_fmt:     function(source_id, info_dict) -> html string for the card body
    """

    def __init__(self, title: str, subtitle: str,
                 source_filter: Callable[[str, dict], bool],
                 body_fmt: Callable[[str, dict], str],
                 parent=None):
        super().__init__(parent)
        self._filter = source_filter
        self._body_fmt = body_fmt
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)
        body = QWidget()
        scroll.setWidget(body)
        v = QVBoxLayout(body)
        v.setContentsMargins(28, 24, 28, 24)
        v.setSpacing(14)

        t = QLabel(title)
        t.setStyleSheet("font-size: 22px; font-weight: 700; color: #f5f5f7;")
        v.addWidget(t)
        s = QLabel(subtitle)
        s.setStyleSheet("color: #76767f;")
        s.setWordWrap(True)
        v.addWidget(s)

        self._grid = QGridLayout()
        self._grid.setSpacing(12)
        v.addLayout(self._grid)
        v.addStretch(1)

        self.refresh()
        self._timer = QTimer(self)
        self._timer.setInterval(30_000)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()

    def refresh(self) -> None:
        while self._grid.count():
            it = self._grid.takeAt(0)
            w = it.widget()
            if w: w.deleteLater()
        d = data.last_pass()
        sources = d.get("sources", {})
        matching = [(sid, info) for sid, info in sorted(sources.items())
                    if self._filter(sid, info or {})]
        if not matching:
            empty = QLabel("No sources reporting in this category yet.")
            empty.setStyleSheet("color: #6B7280; padding: 10px;")
            self._grid.addWidget(empty, 0, 0)
            return
        for i, (sid, info) in enumerate(matching):
            self._grid.addWidget(_make_card(sid, self._body_fmt(sid, info or {})),
                                 i // 3, i % 3)


# -- preconfigured pages -----------------------------------------------------

# Keys we never surface — internal probe metadata, paths only useful in debug.
_HIDDEN_KEYS = {"status", "_probe_ms", "_cache_ts", "cache_age_s", "path",
                "scopes", "read_only", "mode", "schema_version", "spark_7d"}

# Keys we prioritise — render these first, in this order, with friendly labels.
_PRIORITY_KEYS: list[tuple[str, str]] = [
    ("total_items",      "items"),
    ("total_links",      "links"),
    ("pages_mirrored",   "pages mirrored"),
    ("inbox_count",      "inbox"),
    ("count",            "items"),
    ("queue_count",      "queued"),
    ("delta_24h",        "Δ 24h"),
    ("duplicates_flagged", "duplicates"),
    ("indexed_pages_min", "indexed pages"),
    ("size_mb",          "DB size (MB)"),
    ("size_kb",          "size (KB)"),
    ("username",         "user"),
    ("last_activity_iso","last activity"),
    ("last_seen",        "last seen"),
    ("last_sync",        "last sync"),
    ("scanned_at",       "scanned"),
    ("conflicts",        "conflicts"),
    ("tables",           "tables"),
    ("source",           "source"),
    ("panop_port",       "panop port"),
]


def _fmt_value(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (int, float)):
        if isinstance(v, float):
            return f"{v:,.1f}"
        return f"{v:,}"
    if isinstance(v, (list, tuple)):
        return f"{len(v)} entries" if len(v) > 3 else ", ".join(str(x) for x in v)
    s = str(v)
    return s[:80] + "…" if len(s) > 80 else s


def _generic_body(_sid: str, info: dict) -> str:
    if not isinstance(info, dict):
        return f"<span style='color:#6B7280;'>(unexpected data: {type(info).__name__})</span>"

    parts: list[str] = []
    status = info.get("status", "—")
    colour = {"ok": "#30d158", "alive": "#30d158",
              "unconfigured": "#76767f", "warming": "#ff9f0a",
              "stale": "#ff9f0a", "timeout": "#ff453a",
              "error": "#ff453a"}.get(str(status).lower(), "#76767f")
    parts.append(f"<span style='color:{colour};'>●</span>  <b>{status}</b>")

    # Prioritised keys first, then any leftovers we haven't hidden
    seen = set(_HIDDEN_KEYS) | {"error", "note"}
    for k, label in _PRIORITY_KEYS:
        if k in info and info[k] is not None:
            parts.append(f"{label}: <b style='color:#f5f5f7;'>{_fmt_value(info[k])}</b>")
            seen.add(k)
    for k, v in info.items():
        if k in seen or v is None:
            continue
        # Render any unlisted key still meaningful (skip ID-like or huge values)
        label = k.replace("_", " ")
        parts.append(f"{label}: <b style='color:#f5f5f7;'>{_fmt_value(v)}</b>")

    if "note" in info and info["note"]:
        parts.append(f"<span style='color:#6B7280; font-style:italic;'>{info['note'][:120]}</span>")
    if "error" in info and info["error"]:
        parts.append(f"<span style='color:#ff453a;'>{info['error'][:140]}</span>")
    return "<br>".join(parts)


# Category → (title, subtitle, [adapter_ids]).
# Lists match the actual adapter names in lib/adapters/ rather than aspirational
# IDs from the old NiceGUI app. Pages now populate immediately.
_GROUPS = {
    "artifacts":  ("Artifacts", "Vault + Chrome bookmarks — durable artefacts.",
                   ("vault", "chrome_bookmarks")),
    "media":      ("Media",     "Letterboxd · YouTube · TV Time.",
                   ("letterboxd", "youtube", "tvtime")),
    "references": ("References", "Zotero · Paperpile · Mouseion · Kindle · Instapaper.",
                   ("zotero_local", "zotero_web", "paperpile", "mouseion",
                    "kindle", "instapaper", "instapaper_full")),
    "databases":  ("Databases", "Notion workspace + Notion inbox.",
                   ("notion", "notion_workspace")),
    "apps":       ("Apps",      "Google ecosystem — Gmail · Calendar · Drive · Fit.",
                   ("gmail", "gcalendar", "gdrive", "gfit")),
    "projects":   ("Projects",  "Active pipelines: Routster · Mouseion · Panop.",
                   ("routster", "mouseion")),
    "navigation": ("Navigation", "Tabs + bookmarks across phone and laptop.",
                   ("android_tabs", "chrome_tabs", "chrome_bookmarks")),
    "search":     ("Search",    "Cross-source query — coming soon. All sources for now.",
                   None),
}


def make_page(slug: str) -> QWidget:
    cfg = _GROUPS.get(slug)
    if not cfg:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(40, 40, 40, 40)
        v.addWidget(QLabel(f"Unknown slug: {slug}"))
        return w

    title, subtitle, allow_ids = cfg
    if allow_ids is None:
        return SourceListPage(title, subtitle, lambda _s, _i: True, _generic_body)
    allow = set(allow_ids)
    return SourceListPage(title, subtitle,
                          lambda sid, _i: sid in allow,
                          _generic_body)
