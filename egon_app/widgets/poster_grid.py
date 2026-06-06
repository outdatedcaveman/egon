"""Visual poster/art grid — for Media (films, music, shows).

Bruno's directive: media must be visual, pretty, complete and actionable.
This renders a responsive grid of cards, each with a lazily-downloaded
image plus a metadata block, a filter/sort toolbar above, and click-to-open.

Design language (2026-05-22 rewrite — Bruno called the first cut amateur):
  - Correct aspect ratios per medium: films 2:3 portrait, video 16:9
    landscape. Images are CENTER-CROPPED to fill (KeepAspectRatioByExpanding),
    never stretched.
  - Uniform card geometry so the grid is a clean lattice — fixed image box +
    fixed metadata block height regardless of content.
  - Typographic hierarchy: title (13px semibold, 2-line clamp), secondary
    line muted 11px, rating as gold ★ glyphs.
  - Rounded image corners, 1px hairline border, hover lift (accent border).
  - Async image loading with bounded concurrency (6) so a 300-card grid
    doesn't spawn 300 threads. Indirect URLs (Letterboxd) are resolved to a
    direct CDN image first.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt, QThread, Signal, QTimer, QRectF
from PySide6.QtGui import (
    QPixmap, QFont, QColor, QPainter, QPainterPath, QLinearGradient,
)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QFrame,
    QScrollArea, QLineEdit, QComboBox, QPushButton,
)

# ── palette ────────────────────────────────────────────────────────────────
_BG_CARD   = "#0E2630"
_BG_IMG    = "#16404F"
_BORDER    = "#1F4858"
_ACCENT    = "#60A5A8"
_TEXT      = "#F0E9D5"
_MUTED     = "#9CA3AF"
_GOLD      = "#E2B23C"


class _ImageWorker(QThread):
    done = Signal(str, bytes)   # url, raw bytes (empty on failure)

    def __init__(self, url: str, title: str = "", year: str = "", parent=None):
        super().__init__(parent)
        self._url = url
        self._title = title
        self._year = str(year) if year else ""

    def run(self) -> None:
        data = b""
        try:
            import httpx
            url = self._resolve(self._url, self._title, self._year)
            if url:
                r = httpx.get(url, timeout=8.0, follow_redirects=True,
                              headers={"User-Agent": "Mozilla/5.0 Egon"}, verify=False)
                ct = r.headers.get("content-type", "")
                if r.status_code == 200 and ct.startswith("image"):
                    data = r.content
        except Exception:
            data = b""
        self.done.emit(self._url, data)

    # cache resolved Letterboxd posters — backed by a disk file so we DON'T
    # re-fetch 300 film pages on every single visit to the Films tab. This was
    # a major perf drag. Bruno 2026-05-22.
    _resolve_cache: dict[str, str] | None = None
    _CACHE_FILE = (Path(__file__).resolve().parent.parent.parent
                   / "state" / "poster_url_cache.json")
    _cache_lock = threading.Lock()

    @classmethod
    def _load_resolve_cache(cls) -> dict:
        if cls._resolve_cache is None:
            try:
                cls._resolve_cache = json.loads(cls._CACHE_FILE.read_text(encoding="utf-8"))
            except Exception:
                cls._resolve_cache = {}
        return cls._resolve_cache

    @classmethod
    def _save_resolve_cache(cls) -> None:
        try:
            cls._CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            cls._CACHE_FILE.write_text(
                json.dumps(cls._resolve_cache or {}, ensure_ascii=False),
                encoding="utf-8")
        except Exception:
            pass

    @classmethod
    def _resolve(cls, url: str, title: str = "", year: str = "") -> str:
        """Resolve indirect poster URLs to a direct image URL.

        Letterboxd's `/film/<slug>/image-N/` path 403s — the real poster is
        on a.ltrbxd.com, discoverable via the film page's og:image tag.
        Disk-cached: each film page is fetched at most once, ever.
        """
        import re
        cache = cls._load_resolve_cache()
        if url in cache:
            return cache[url]

        # Pocket Casts FortiGuard WAF bypass using iTunes Search API
        if "pocketcasts" in url or "static.pocketcasts.com" in url:
            try:
                import urllib.parse
                import httpx
                term = urllib.parse.quote(title)
                r = httpx.get(f"https://itunes.apple.com/search?term={term}&entity=podcast", timeout=5.0)
                if r.status_code == 200:
                    results = r.json().get("results", [])
                    if results:
                        art_url = results[0].get("artworkUrl600") or results[0].get("artworkUrl100") or ""
                        if art_url:
                            with cls._cache_lock:
                                cache[url] = art_url
                                cls._save_resolve_cache()
                            return art_url
            except Exception:
                pass
            return url

        m = re.search(r"letterboxd\.com/film/([^/]+)/image-\d+", url)
        if not m:
            return url   # already direct (YouTube thumbs etc.)
        slug = m.group(1)

        # If TMDB is configured and we have a title, use TMDB to resolve/enrich the poster!
        # This is 100% reliable, fast, and builds the persistent TMDB cache.
        try:
            from lib.adapters import tmdb
            if tmdb.configured() and title:
                extra = tmdb.enrich(title, year)
                if extra and extra.get("poster"):
                    resolved = extra["poster"]
                    with cls._cache_lock:
                        cache[url] = resolved
                        cls._save_resolve_cache()
                    return resolved
        except Exception:
            pass

        # Fallback to Letterboxd page scraping if TMDB isn't configured or failed
        ua = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                             "AppleWebKit/537.36 (KHTML, like Gecko) "
                             "Chrome/130.0.0.0 Safari/537.36")}
        resolved = ""
        try:
            import httpx
            r = httpx.get(f"https://letterboxd.com/film/{slug}/",
                          timeout=8.0, follow_redirects=True, headers=ua, verify=False)
            if r.status_code == 200:
                # Check for year mismatch in <title> to handle disambiguation slugs
                title_match = re.search(r'<title>[^<]*?\((\d{4})\)[^<]*?</title>', r.text)
                page_year = title_match.group(1) if title_match else ""
                if year and page_year and page_year != year:
                    r2 = httpx.get(f"https://letterboxd.com/film/{slug}-{year}/",
                                   timeout=8.0, follow_redirects=True, headers=ua, verify=False)
                    if r2.status_code == 200:
                        r = r2
                
                mm = re.search(r'(https://a\.ltrbxd\.com/resized/film-poster/[^\s\x22\x27?]+\.jpg)', r.text)
                if not mm:
                    mm = re.search(r'"image"\s*:\s*"([^"]+)"', r.text)
                if not mm:
                    mm = re.search(r'og:image["\'\s]+content=["\']([^"\']+)', r.text)
                if not mm:
                    mm = re.search(r'(https://a\.ltrbxd\.com/resized/[^\s"\'?]+\.jpg)', r.text)
                if mm:
                    resolved = mm.group(1)
        except Exception:
            resolved = ""
        with cls._cache_lock:
            cache[url] = resolved
            cls._save_resolve_cache()
        return resolved


def _round_crop(src: QPixmap, w: int, h: int, radius: int = 6) -> QPixmap:
    """Center-crop src to fill w×h (no distortion), with rounded corners."""
    scaled = src.scaled(w, h, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
    # center crop
    x = max(0, (scaled.width() - w) // 2)
    y = max(0, (scaled.height() - h) // 2)
    cropped = scaled.copy(x, y, w, h)
    out = QPixmap(w, h)
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing, True)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, w, h), radius, radius)
    p.setClipPath(path)
    p.drawPixmap(0, 0, cropped)
    p.end()
    return out


def _placeholder(title: str, w: int, h: int) -> QPixmap:
    pm = QPixmap(w, h)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, w, h), 6, 6)
    p.setClipPath(path)
    # subtle vertical gradient tile
    g = QLinearGradient(0, 0, 0, h)
    g.setColorAt(0, QColor("#1B4D5E"))
    g.setColorAt(1, QColor("#102F3C"))
    p.fillRect(0, 0, w, h, g)
    p.setPen(QColor(_ACCENT))
    p.setFont(QFont("Segoe UI", int(h * 0.18), QFont.Bold))
    p.drawText(pm.rect(), Qt.AlignCenter, (title or "?").strip()[:1].upper())
    p.end()
    return pm


def _stars(rating) -> str:
    try:
        r = float(rating)
    except Exception:
        return ""
    full = int(r)
    half = (r - full) >= 0.5
    return "★" * full + ("½" if half else "")


class _Card(QFrame):
    """One media card. `shape` = 'portrait' (films) or 'landscape' (video)."""

    # geometry per shape — image box + total card size kept uniform
    GEO = {
        "portrait":  {"w": 180, "img_h": 270},   # film posters 2:3
        "landscape": {"w": 300, "img_h": 169},   # video thumbs 16:9 — enlarged
        "square":    {"w": 180, "img_h": 180},   # podcast / album art 1:1
    }

    def __init__(self, row: dict, shape: str, on_click, parent=None):
        super().__init__(parent)
        self._row = row
        self._on_click = on_click
        self._shape = shape
        geo = self.GEO[shape]
        self._iw, self._ih = geo["w"], geo["img_h"]

        self.setObjectName("mcard")
        self.setFixedWidth(self._iw + 16)
        self.setStyleSheet(
            f"QFrame#mcard {{ background: {_BG_CARD}; border: 1px solid {_BORDER}; "
            f"border-radius: 10px; }}"
            f"QFrame#mcard:hover {{ border: 1px solid {_ACCENT}; background: #12303B; }}"
        )
        self.setCursor(Qt.PointingHandCursor)

        v = QVBoxLayout(self)
        v.setContentsMargins(8, 8, 8, 10)
        v.setSpacing(6)

        self._img = QLabel()
        self._img.setFixedSize(self._iw, self._ih)
        self._img.setAlignment(Qt.AlignCenter)
        self._img.setPixmap(_placeholder(row.get("title", "?"), self._iw, self._ih))
        v.addWidget(self._img)

        # title — 2-line clamp via fixed height + word wrap
        title = QLabel(row.get("title", "(untitled)"))
        title.setWordWrap(True)
        title.setFixedHeight(34)
        title.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        title.setStyleSheet(f"color: {_TEXT}; font-weight: 600; font-size: 12px; "
                            f"line-height: 16px;")
        v.addWidget(title)

        # secondary line: year · channel/artist
        bits = []
        if row.get("year"):     bits.append(str(row["year"]))
        if row.get("subtitle"): bits.append(str(row["subtitle"]))
        sec = QLabel("  ·  ".join(bits))
        sec.setStyleSheet(f"color: {_MUTED}; font-size: 11px;")
        sec.setFixedHeight(15)
        sec.setTextFormat(Qt.PlainText)
        v.addWidget(sec)

        # rating / meta line
        rating = row.get("rating") or row.get("score")
        meta_bits = []
        stars = _stars(rating) if rating else ""
        meta_html = ""
        if stars:
            meta_html += f"<span style='color:{_GOLD};'>{stars}</span>"
        if row.get("liked"):
            meta_html += f" <span style='color:#D67A6A;'>♥</span>"
        for m in (row.get("meta") or [])[:2]:
            meta_bits.append(str(m))
        if meta_bits:
            extra = "  ·  ".join(meta_bits)
            meta_html += (("  ·  " if meta_html else "") +
                          f"<span style='color:{_MUTED};'>{extra}</span>")
        meta = QLabel(meta_html)
        meta.setTextFormat(Qt.RichText)
        meta.setStyleSheet("font-size: 11px;")
        meta.setFixedHeight(16)
        v.addWidget(meta)

    def set_image(self, data: bytes) -> None:
        if not data:
            return
        pm = QPixmap()
        if pm.loadFromData(data):
            self._img.setPixmap(_round_crop(pm, self._iw, self._ih))

    def mouseReleaseEvent(self, e) -> None:
        if e.button() == Qt.LeftButton and self._on_click:
            self._on_click(self._row)
        super().mouseReleaseEvent(e)


class _LoadWorker(QThread):
    done = Signal(list, str)

    def __init__(self, provider, parent=None):
        super().__init__(parent)
        self._provider = provider

    def run(self) -> None:
        try:
            rows = self._provider() or []
            self.done.emit(list(rows), "")
        except Exception as e:
            self.done.emit([], f"{type(e).__name__}: {e}"[:200])


class PosterGridWidget(QWidget):
    _MAX_CONCURRENT_IMG = 6

    def __init__(self, provider: Callable[[], list[dict]],
                 on_click: Callable[[dict], None] | None = None,
                 sort_fields: list[tuple[str, str]] | None = None,
                 empty_message: str = "no items yet",
                 shape: str = "portrait",
                 stats_fn: Callable[[list[dict]], str] | None = None,
                 cache_key: str | None = None,
                 parent=None):
        super().__init__(parent)
        self._provider = provider
        self._on_click = on_click or (lambda r: None)
        self._empty_message = empty_message
        self._shape = shape
        self._cache_key = cache_key
        self._cols_rendered = 0   # for responsive re-layout on resize
        # optional: computes a rich stats string from the loaded rows, shown
        # as a header strip above the grid. Bruno 2026-05-22 #37.
        self._stats_fn = stats_fn
        self._rows: list[dict] = []
        self._filtered: list[dict] = []
        self._cards: list[_Card] = []
        self._workers: list[_ImageWorker] = []
        self._img_queue: list = []
        self._active_workers = 0

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        bar = QFrame()
        bar.setStyleSheet(f"QFrame {{ background: {_BG_IMG}; border: 1px solid {_BORDER}; "
                          f"border-radius: 6px; }}")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(8, 4, 8, 4)
        self._search = QLineEdit()
        self._search.setPlaceholderText("filter")
        self._search.setStyleSheet(
            f"QLineEdit {{ background: {_BG_CARD}; color: {_TEXT}; border: 1px solid {_BORDER}; "
            f"border-radius: 4px; padding: 4px 8px; }}")
        self._search.textChanged.connect(self._apply)
        self._search.setMinimumWidth(200)
        bl.addWidget(self._search)
        bl.addWidget(QLabel("Sort:"))
        self._sort = QComboBox()
        for key, label in (sort_fields or [("title", "Title"), ("year", "Year"),
                                           ("rating", "Rating")]):
            self._sort.addItem(label, key)
        self._sort.currentIndexChanged.connect(self._apply)
        bl.addWidget(self._sort)
        self._dir = QPushButton("↓"); self._dir.setFixedWidth(30)
        self._desc = True
        self._dir.clicked.connect(self._toggle_dir)
        bl.addWidget(self._dir)
        self._count = QLabel("loading…")
        self._count.setStyleSheet(f"color: {_MUTED}; font-size: 11px;")
        bl.addWidget(self._count)
        bl.addStretch(1)
        self._refresh = QPushButton("Refresh")
        self._refresh.clicked.connect(self.reload)
        bl.addWidget(self._refresh)
        root.addWidget(bar)

        # optional stats strip (rich per-platform summary)
        self._stats_label = None
        if self._stats_fn:
            self._stats_label = QLabel("")
            self._stats_label.setTextFormat(Qt.RichText)
            self._stats_label.setWordWrap(True)
            self._stats_label.setStyleSheet(
                f"color: {_MUTED}; font-size: 12px; padding: 4px 6px;")
            root.addWidget(self._stats_label)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._host = QWidget()
        self._grid = QGridLayout(self._host)
        self._grid.setSpacing(16)
        self._grid.setContentsMargins(4, 4, 4, 4)
        self._grid.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
        self._scroll.setWidget(self._host)
        root.addWidget(self._scroll, 1)

        # LAZY LOAD: don't fetch on construction. A QTabWidget builds every
        # tab up-front, so eager loading fired all providers at once (the
        # slowdown Bruno noticed). We load only when the widget is first shown.
        self._loaded = False

    def showEvent(self, e):
        super().showEvent(e)
        if not self._loaded:
            self._loaded = True
            # stale-while-revalidate: render cached cards instantly, then refresh
            if self._cache_key:
                from egon_app.widgets import _cache
                cached, _age = _cache.read(self._cache_key)
                if cached:
                    self._rows = cached
                    self._apply()
            QTimer.singleShot(30, self.reload)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        # Responsive: if the window width now fits a different column count,
        # re-flow the existing cards. Bruno 2026-05-22: "responsive and adaptive".
        if self._rows and self._columns_for_width() != self._cols_rendered:
            self._render(self._filtered or self._rows)

    def reload(self) -> None:
        self._count.setText("refreshing…" if self._rows else "loading…")
        self._refresh.setEnabled(False)
        self._lw = _LoadWorker(self._provider, parent=self)
        self._lw.done.connect(self._on_loaded)
        self._lw.start()

    def _on_loaded(self, rows: list, err: str) -> None:
        self._refresh.setEnabled(True)
        if err:
            if not self._rows:           # keep stale cards on refresh error
                self._count.setText(f"error: {err}")
                self._rows = []
                self._apply()
            return
        self._rows = rows
        if self._cache_key and rows:
            from egon_app.widgets import _cache
            _cache.write(self._cache_key, rows)
        self._apply()

    def _toggle_dir(self) -> None:
        self._desc = not self._desc
        self._dir.setText("↓" if self._desc else "↑")
        self._apply()

    def _columns_for_width(self) -> int:
        card_w = _Card.GEO[self._shape]["w"] + 16 + 16
        avail = max(card_w, self._scroll.viewport().width() - 8)
        return max(1, avail // card_w)

    def _apply(self, *_) -> None:
        q = (self._search.text() or "").strip().lower()
        rows = self._rows
        if q:
            rows = [r for r in rows if any(q in str(v).lower() for v in r.values())]
        key = self._sort.currentData()
        if key:
            def _k(r):
                v = r.get(key, "")
                try: return (0, float(v))
                except Exception: return (1, str(v).lower())
            rows = sorted(rows, key=_k, reverse=self._desc)
        self._filtered = rows
        self._count.setText(f"{len(rows):,} of {len(self._rows):,}" if q
                            else f"{len(self._rows):,} items")
        if self._stats_label is not None and self._stats_fn:
            try:
                self._stats_label.setText(self._stats_fn(self._rows))
            except Exception:
                self._stats_label.setText("")
        self._render(rows)

    def _render(self, rows: list[dict]) -> None:
        for c in self._cards:
            c.deleteLater()
        self._cards = []
        for w in self._workers:
            try: w.quit()
            except Exception: pass
        self._workers = []
        self._img_queue = []
        self._active_workers = 0
        while self._grid.count():
            it = self._grid.takeAt(0)
            wdg = it.widget()
            if wdg: wdg.deleteLater()

        if not rows:
            lbl = QLabel(self._empty_message)
            lbl.setStyleSheet(f"color: #6B7280; padding: 24px; font-size: 13px;")
            lbl.setWordWrap(True)
            self._grid.addWidget(lbl, 0, 0)
            return

        cols = self._columns_for_width()
        self._cols_rendered = cols
        cap = 300
        for idx, row in enumerate(rows[:cap]):
            card = _Card(row, self._shape, self._on_click)
            r, c = divmod(idx, cols)
            self._grid.addWidget(card, r, c)
            self._cards.append(card)
            img_url = row.get("poster") or row.get("image") or row.get("art") or ""
            if img_url.startswith("http"):
                self._img_queue.append((card, img_url, row.get("title", ""), row.get("year", "")))
        if len(rows) > cap:
            self._count.setText(f"{len(rows):,} items (showing first {cap})")
        self._pump_image_queue()

    def _pump_image_queue(self) -> None:
        while (self._active_workers < self._MAX_CONCURRENT_IMG and self._img_queue):
            card, url, title, year = self._img_queue.pop(0)
            worker = _ImageWorker(url, title, year, parent=self)
            self._active_workers += 1
            def _on_done(u, data, card=card):
                try:
                    card.set_image(data)
                except RuntimeError:
                    pass
                self._active_workers -= 1
                self._pump_image_queue()
            worker.done.connect(_on_done)
            worker.start()
            self._workers.append(worker)
