"""References page — searchable, sortable, multi-select data browser.

Each tab pulls real items from one source. Toolbar above the table:
filter, select-all, sort-by, refresh, batch actions. Actions take the
selected rows (or the whole filtered set when nothing is selected).

Bruno 2026-05-20: redesigned per directive:
  - No more per-row buttons (visually noisy)
  - Toolbar above the table, single Zotero tab (Zotero web/local share
    the same library so they were redundant)
  - Columns: Title · Authors · Year · DOI · URL · Publication · Added · Tags
"""
from __future__ import annotations

import webbrowser

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QTabWidget,
)

from egon_app.widgets import ItemListWidget
from egon_app.pages.references_comparer_view import UberComparerWidget


# ── per-source providers ───────────────────────────────────────────────────

def _zotero_items() -> list[dict]:
    """Single Zotero source — direct SQLite read from the local DB. Web
    and local share the same library so we just expose this one."""
    from lib.adapters import zotero_local
    return zotero_local.items(5000)


def _mouseion_items() -> list[dict]:
    from lib.adapters import mouseion
    return mouseion.items(5000)


def _zotero_stats() -> dict:
    """Full-DB aggregates so the stats bar shows the true ~250k total, not
    the 5k browsing window."""
    from lib.adapters import zotero_local
    return zotero_local.library_stats()


def _mouseion_stats() -> dict:
    from lib.adapters import mouseion
    return mouseion.library_stats()


def _instapaper_items() -> list[dict]:
    """Instapaper data comes from the Chrome extension's www.instapaper.com
    harvest. Their Simple API has no list endpoint; Full API needs OAuth.
    """
    from lib.adapters import instapaper
    return instapaper.items(5000)


def _kindle_items() -> list[dict]:
    try:
        from lib.adapters import kindle
        return kindle.items(5000)
    except Exception:
        return []


def _paperpile_items() -> list[dict]:
    """Export file first (full library), then extension harvest fallback."""
    from lib.adapters import paperpile
    return paperpile.items(20000)


# ── shared batch action helpers ─────────────────────────────────────────────

def _copy_text(text: str) -> None:
    QGuiApplication.clipboard().setText(text)


def _open_doi_for(rows: list[dict]) -> None:
    """Open every selected row's DOI in a new browser tab. Capped at 25
    so we don't open hundreds of tabs by accident."""
    n = 0
    for r in rows[:25]:
        doi = (r.get("doi") or "").strip()
        if not doi:
            continue
        if not doi.startswith("http"):
            doi = f"https://doi.org/{doi.lstrip('doi:')}"
        webbrowser.open(doi, new=2)
        n += 1


def _open_url_for(rows: list[dict]) -> None:
    n = 0
    for r in rows[:25]:
        for key in ("url", "html_url", "permalink", "link"):
            v = (r.get(key) or "").strip()
            if v.startswith("http"):
                webbrowser.open(v, new=2)
                n += 1
                break


def _copy_titles_for(rows: list[dict]) -> None:
    _copy_text("\n".join((r.get("title") or "") for r in rows))


def _copy_dois_for(rows: list[dict]) -> None:
    _copy_text("\n".join((r.get("doi") or "") for r in rows if r.get("doi")))


def _copy_bibtex_keys(rows: list[dict]) -> None:
    """Crude BibTeX-key copy: first-author-surname + year + first-title-word."""
    out: list[str] = []
    for r in rows:
        author = (r.get("authors") or "").split(",")[0].strip().split()[-1:] or ["unknown"]
        year = (r.get("year") or "n.d.")
        title_word = (r.get("title") or "").split()[:1] or ["item"]
        out.append(f"{author[0].lower()}{year}{title_word[0].lower()}")
    _copy_text("\n".join(out))


def _open_amazon_for(rows: list[dict]) -> None:
    try:
        from lib.adapters.kindle import secrets as _s
        region = (_s.get("kindle.region") or "com").strip().lower() or "com"
    except Exception:
        region = "com"
    base = "amazon.com" if region in ("com", "us") else f"amazon.{region}"
    for r in rows[:25]:
        asin = (r.get("asin") or "").strip()
        kind = (r.get("kind") or "").strip().lower()
        is_pdoc = kind in ("kindlepdoc", "personaldocument", "personal") or asin.startswith("pdoc_")
        if asin and not is_pdoc:
            webbrowser.open(f"https://www.{base}/dp/{asin}", new=2)


# ── page ───────────────────────────────────────────────────────────────────

# Shared column schema for academic-reference sources. Tweak once, every tab
# benefits.
_REF_COLUMNS = [
    ("title",       "Title",       -1),
    ("authors",     "Authors",     220),
    ("year",        "Year",         60),
    ("publication", "Publication", 200),
    ("doi",         "DOI",         170),
    ("url",         "URL",         200),
    ("added",       "Added",       100),
    ("tags",        "Tags",        180),
]

_REF_ACTIONS = [
    ("Open DOIs",       _open_doi_for),
    ("Open URLs",       _open_url_for),
    ("Copy titles",     _copy_titles_for),
    ("Copy DOIs",       _copy_dois_for),
    ("Copy BibTeX keys", _copy_bibtex_keys),
]


class ReferencesPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 24, 28, 24)
        outer.setSpacing(10)

        title = QLabel("References")
        title.setStyleSheet("font-size: 22px; font-weight: 700; color: #f5f5f7;")
        outer.addWidget(title)
        sub = QLabel("Search, sort, multi-select, batch-open or batch-copy across "
                     "Zotero · Paperpile · Kindle · Mouseion. "
                     "Click a row to select; Ctrl-click and Shift-click for ranges; "
                     "or use the 'All filtered' checkbox.")
        sub.setStyleSheet("color: #76767f;")
        sub.setWordWrap(True)
        outer.addWidget(sub)

        tabs = QTabWidget()
        tabs.setStyleSheet(
            "QTabWidget::pane { border: 1px solid #22252a; background: #0c0d0f; border-radius: 4px; }"
            "QTabBar::tab { background: #0c0d0f; color: #76767f; padding: 6px 14px; "
            "border: 1px solid #22252a; border-bottom: none; }"
            "QTabBar::tab:selected { background: #212328; color: #f5f5f7; font-weight: 600; }"
        )
        outer.addWidget(tabs, 1)

        tabs.addTab(UberComparerWidget(), "⚖️ Uber-Comparer")

        tabs.addTab(ItemListWidget(
            provider=_zotero_items,
            columns=_REF_COLUMNS,
            actions=_REF_ACTIONS,
            stats_provider=_zotero_stats,
            cache_key="ref_zotero",
            empty_message="Zotero SQLite not found at the default path.",
        ), "Zotero")

        tabs.addTab(ItemListWidget(
            provider=_paperpile_items,
            columns=_REF_COLUMNS,
            actions=_REF_ACTIONS,
            cache_key="ref_paperpile",
            empty_message=("No Paperpile harvest yet. Settings → Paperpile → "
                           "Pull library now. Needs the Egon Chrome extension v1.3+."),
        ), "Paperpile")

        tabs.addTab(ItemListWidget(
            provider=_kindle_items,
            cache_key="ref_kindle",
            columns=[
                ("title",  "Title",  -1),
                ("author", "Author", 280),
                ("kind",   "Type",    90),
                ("asin",   "ASIN",   140),
            ],
            actions=[
                ("Open on Amazon", _open_amazon_for),
                ("Copy titles",     _copy_titles_for),
            ],
            empty_message=("No Kindle harvest yet. Settings → Kindle → Pull "
                           "library now (needs the Egon Chrome extension v1.3+)."),
        ), "Kindle")

        tabs.addTab(ItemListWidget(
            provider=_mouseion_items,
            columns=_REF_COLUMNS,
            actions=_REF_ACTIONS,
            stats_provider=_mouseion_stats,
            cache_key="ref_mouseion",
            empty_message="Mouseion has no items. Is the Flask service on port 7274 running?",
        ), "Mouseion")

        # Instapaper moved to the Media page (Bruno 2026-05-29) — it's a
        # saved-articles reading list, grouped now with the other content
        # you consume rather than with academic references.
