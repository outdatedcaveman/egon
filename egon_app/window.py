"""MainWindow — sidebar + content stack. Pure Qt widgets."""
from __future__ import annotations

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QFrame, QLabel,
    QPushButton, QStackedWidget, QButtonGroup, QSizePolicy, QStatusBar,
    QMessageBox,
)

from egon_app import data
from egon_app.theme import QSS
from egon_app.pages import (
    HomePage, InboxPage, NavigationPage, LedgerPage, SyncPage, MemoryPage, SettingsPage,
    ReferencesPage, MediaPage, SearchPage, MindPage, ProjectsPage, ConnectPage,
    make_generic_page,
)


# Nav definition: (slug, icon-char, label, is_ledger_emphasis)
NAV = [
    ("home",       "🏠", "Home",           False),
    ("connect",    "✨", "Connect & Search", False),
    ("inbox",      "📥", "Inbox",          False),
    ("artifacts",  "🗂", "Artifacts",      False),
    ("persona",    "👤", "Persona",        False),
    ("navigation", "🧭", "Navigation",     False),
    ("media",      "🎬", "Media",          False),
    ("references", "📚", "References",     False),
    ("databases",  "🗄", "Databases",      False),
    ("apps",       "🧰", "Apps",           False),
    ("projects",   "📁", "Projects",       False),
    ("sync",       "🔄", "Sync",           False),
    ("ledger",     "💰", "Token Ledger",   True),
    ("memory",     "🧠", "Memory & rules", False),
    ("mind",       "🌐", "Mind (shared)",  False),
    ("settings",   "⚙", "Settings",       False),
]






def _artifacts_page():
    from egon_app.pages.artifacts import ArtifactsPage
    return ArtifactsPage()


def _connect_search_page():
    from egon_app.pages.connect_search import ConnectSearchPage
    return ConnectSearchPage()


def _databases_page():
    from egon_app.pages.databases import DatabasesPage
    return DatabasesPage()


def _persona_page():
    from egon_app.pages.persona import PersonaPage
    return PersonaPage()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Egon")
        self.resize(1480, 920)
        self.setMinimumSize(1080, 640)

        # Load theme config
        cfg = data.ledger_config()
        self._dark_mode = cfg.get("dark_mode", True)
        from egon_app import theme
        self.setStyleSheet(theme.get_stylesheet(self._dark_mode))

        central = QWidget()
        central.setObjectName("central")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_header())
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addWidget(self._build_sidebar())
        row.addWidget(self._build_stack(), 1)
        wrap = QWidget()
        wrap.setLayout(row)
        root.addWidget(wrap, 1)

        # Status bar
        sb = QStatusBar()
        self.setStatusBar(sb)
        self._sb_label = QLabel("ready")
        sb.addWidget(self._sb_label)
        sb_right = QLabel("v0.3 native")
        sb.addPermanentWidget(sb_right)

        # Default selection
        self._nav_buttons[0].setChecked(True)
        self._stack.setCurrentIndex(0)
        self._current_slug = NAV[0][0]   # track for refresh-timer gating

        # Auto-refresh the snapshot so Home never shows stale data. Bruno
        # 2026-05-22: connectors looked "unconfigured" only because
        # last_pass.json was a day old; the live adapters were fine. We now
        # regenerate it shortly after launch and every 30 min in a daemon
        # thread (writes to Drive, so off the UI thread).
        self._start_auto_snapshot()
        self._start_tmdb_warmer()

    def _start_auto_snapshot(self) -> None:
        import threading, time as _t
        def _loop():
            _t.sleep(3)               # let the UI settle first
            while True:
                try:
                    from lib.snapshot import snapshot
                    snapshot(write=True)
                    data.force_refresh()
                except Exception:
                    pass
                _t.sleep(1800)        # every 30 min
        threading.Thread(target=_loop, daemon=True, name="egon-auto-snapshot").start()

    def _start_tmdb_warmer(self) -> None:
        import threading, time as _t
        def _loop():
            _t.sleep(10)  # let the UI settle
            try:
                from lib.adapters import letterboxd
                from lib.adapters import tmdb
                if tmdb.configured():
                    films = letterboxd.items(5000)
                    cache = tmdb._load_cache()
                    uncached = []
                    for f in films:
                        title = f.get("title", "")
                        year = f.get("year", "")
                        if title:
                            ckey = f"{title.strip().lower()}|{str(year)[:4]}"
                            if ckey not in cache:
                                uncached.append((title, year))
                    
                    for t, y in uncached:
                        _t.sleep(0.2)  # 200ms delay
                        try:
                            tmdb.enrich(t, y)
                        except Exception:
                            pass
            except Exception:
                pass
        threading.Thread(target=_loop, daemon=True, name="egon-tmdb-warmer").start()

    # ------------------------------------------------------------------ UI
    def _build_header(self) -> QFrame:
        hdr = QFrame()
        hdr.setObjectName("headerBar")
        hdr.setFixedHeight(56)
        h = QHBoxLayout(hdr)
        h.setContentsMargins(16, 0, 16, 0)

        title = QLabel("🛰  Egon")
        title.setObjectName("headerTitle")
        h.addWidget(title)
        h.addStretch(1)

        d = data.last_pass()
        stats_text = (f"Last pass: {d.get('generated_at', '—')}  ·  "
                      f"{d.get('items_processed', '—')} items  ·  "
                      f"{d.get('duration_seconds', '—')}s")
        self._stats_label = QLabel(stats_text)
        self._stats_label.setObjectName("headerStats")
        h.addWidget(self._stats_label)

        h.addSpacing(12)
        # Theme toggle button
        self._theme_btn = QPushButton("☀️" if self._dark_mode else "🌙")
        self._theme_btn.setToolTip("Toggle light/dark theme")
        self._theme_btn.clicked.connect(self._on_toggle_theme)
        h.addWidget(self._theme_btn)

        h.addSpacing(6)
        btn = QPushButton("⚡ Run pass now")
        btn.setObjectName("runPassBtn")
        btn.clicked.connect(self._on_run_pass)
        h.addWidget(btn)
        return hdr

    def _build_sidebar(self) -> QFrame:
        sb = QFrame()
        sb.setObjectName("sidebar")
        sb.setFixedWidth(244)
        v = QVBoxLayout(sb)
        v.setContentsMargins(0, 14, 0, 0)
        v.setSpacing(2)

        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)
        self._nav_buttons: list[QPushButton] = []

        for idx, (slug, icon, label, is_ledger) in enumerate(NAV):
            btn = QPushButton(f"  {icon}  {label}")
            btn.setCheckable(True)
            btn.setObjectName("navItemLedger" if is_ledger else "navItem")
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda _=False, i=idx, s=slug: self._switch_to(i, s))
            self._nav_group.addButton(btn, idx)
            self._nav_buttons.append(btn)
            v.addWidget(btn)

        v.addStretch(1)

        footer = QLabel("v0.3 native\nstate in vault/050/egon")
        footer.setObjectName("sidebarFooter")
        v.addWidget(footer)
        return sb

    def _build_stack(self) -> QStackedWidget:
        # LAZY CONSTRUCTION (Bruno 2026-05-29 crash fix).
        # Previously every page was constructed eagerly here, and several
        # page __init__s do blocking network/disk I/O (their trailing
        # self.refresh()). That added up to ~15 s before win.show() ran —
        # the user saw a blank desktop, assumed Egon had crashed, and
        # double-clicked again (which hit the single-instance guard).
        #
        # Now we build ONLY the default Home page eagerly; every other page
        # is a cheap placeholder until the user first navigates to it, at
        # which point _ensure_page_built() swaps in the real widget. The
        # window therefore appears in ~1 s. First open of a heavy tab costs
        # its build time once — but that's a click the user made, with the
        # window already visible.
        self._stack = QStackedWidget()
        # Page index MUST match NAV order
        self._pages: dict[str, QWidget] = {}
        # Dedicated page widgets for the views that need bespoke logic;
        # everything else routes through `make_generic_page(slug)` which
        # renders a source-card grid from data.last_pass().
        self._page_classes = {
            "home":       HomePage,
            "inbox":      InboxPage,
            "navigation": NavigationPage,
            "ledger":     LedgerPage,
            "sync":       SyncPage,
            "memory":     MemoryPage,
            "settings":   SettingsPage,
            "references": ReferencesPage,
            "media":      MediaPage,
            "mind":       MindPage,
            "projects":   ProjectsPage,
            "connect":    _connect_search_page,
            "artifacts":  _artifacts_page,
            "databases":  _databases_page,
            "persona":    _persona_page,
        }
        self._idx_slug: dict[int, str] = {}
        self._built_slugs: set[str] = set()
        for idx, (slug, _icon, _label, _is_ledger) in enumerate(NAV):
            self._idx_slug[idx] = slug
            if idx == 0:
                # Build the landing page eagerly so the window has content.
                cls = self._page_classes.get(slug)
                page = cls() if cls else make_generic_page(slug)
                self._built_slugs.add(slug)
            else:
                # Lightweight placeholder; real widget built on first view.
                page = QWidget()
            self._pages[slug] = page
            self._stack.addWidget(page)
        return self._stack

    def _ensure_page_built(self, idx: int, slug: str) -> None:
        """Construct a page's real widget the first time it's shown, swapping
        out the placeholder. Keeps stack indices aligned with NAV order."""
        if slug in self._built_slugs:
            return
        cls = self._page_classes.get(slug)
        try:
            real = cls() if cls else make_generic_page(slug)
        except Exception:
            # A failed page build must never take the window down — but a
            # silent placeholder is just as bad (Bruno clicked Token Ledger,
            # saw "crash", and had no evidence to report). Show the traceback.
            import traceback
            from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget
            err = QWidget()
            lay = QVBoxLayout(err)
            head = QLabel(f"⚠️  The {slug} page failed to build")
            head.setStyleSheet("color: #D67A6A; font-size: 16px; font-weight: 700;")
            lay.addWidget(head)
            tb = QLabel(traceback.format_exc()[-1800:])
            tb.setStyleSheet("color: #9CA3AF; font-family: Consolas, monospace; font-size: 11px;")
            tb.setWordWrap(True)
            tb.setTextInteractionFlags(Qt.TextSelectableByMouse)
            lay.addWidget(tb)
            lay.addStretch(1)
            real = err
        placeholder = self._pages.get(slug)
        if placeholder is not None:
            self._stack.removeWidget(placeholder)
            placeholder.deleteLater()
        self._stack.insertWidget(idx, real)
        self._pages[slug] = real
        self._built_slugs.add(slug)

    # ------------------------------------------------------------------ events
    def _set_page_active(self, page, active: bool) -> None:
        """Start/stop a page's auto-refresh QTimer so only the VISIBLE page
        polls. Bruno 2026-05-29 efficiency pass: previously every page's timer
        (Mind 5s, Projects 8s × N HTTP calls, Navigation 20s, …) kept firing
        in the background forever, even when you were on a different tab —
        constant needless network + CPU. Now a page only refreshes while it's
        on screen, and re-refreshes immediately when you return to it."""
        if page is None:
            return
        from PySide6.QtCore import QTimer
        t = getattr(page, "_timer", None)
        try:
            if isinstance(t, QTimer):
                if active and not t.isActive():
                    t.start()
                elif not active and t.isActive():
                    t.stop()
        except Exception:
            pass

    def _switch_to(self, idx: int, slug: str) -> None:
        # Pause the page we're leaving so its timer stops polling off-screen.
        prev = getattr(self, "_current_slug", None)
        if prev and prev != slug:
            self._set_page_active(self._pages.get(prev), False)

        self._ensure_page_built(idx, slug)
        self._stack.setCurrentIndex(idx)
        self._sb_label.setText(f"page: {slug}")

        # Resume the page we're entering + refresh once so data updated while
        # it was paused appears immediately.
        page = self._pages.get(slug)
        self._set_page_active(page, True)
        if hasattr(page, "refresh"):
            try:
                page.refresh()
            except Exception:
                pass
        self._current_slug = slug

    def _on_run_pass(self) -> None:
        """Run live adapter snapshot off the UI thread, then refresh all pages."""
        self._sb_label.setText("running snapshot…")
        from PySide6.QtCore import QThread, Signal as _Sig

        class _Worker(QThread):
            done = _Sig(bool, str)
            def run(self_):
                ok, msg = data.trigger_pass("daily")
                self_.done.emit(ok, msg)

        self._pass_worker = _Worker()
        self._pass_worker.done.connect(self._on_pass_done)
        self._pass_worker.start()

    def _on_pass_done(self, ok: bool, msg: str) -> None:
        if not ok:
            QMessageBox.warning(self, "Pass failed", msg)
            self._sb_label.setText("pass failed")
            return
        # Invalidate the data-layer cache so every page reads fresh disk
        data.force_refresh()
        # Refresh every page that knows how to
        for p in self._pages.values():
            if hasattr(p, "refresh"):
                try: p.refresh()
                except Exception: pass
        # Update header stats
        d = data.last_pass()
        self._stats_label.setText(
            f"Last pass: {d.get('generated_at', '—')}  ·  "
            f"{d.get('items_processed', '—')} items  ·  "
            f"{d.get('duration_seconds', '—')}s"
        )
        self._sb_label.setText(f"snapshot done — {msg}")

    def _on_toggle_theme(self) -> None:
        self._dark_mode = not self._dark_mode
        from egon_app import theme
        self.setStyleSheet(theme.get_stylesheet(self._dark_mode))
        self._theme_btn.setText("☀️" if self._dark_mode else "🌙")
        try:
            from lib.ledger import load_config, save_config
            cfg = load_config() or {}
            cfg["dark_mode"] = self._dark_mode
            save_config(cfg)
        except Exception:
            pass
