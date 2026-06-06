"""Settings — full per-adapter configuration. Restores feature parity with the
old NiceGUI version: editable fields, helper text, Test / Authorize / Login /
Revoke buttons, Read/Write mode toggles, file uploaders.

Maxim (Bruno 2026-05-20): "no logic happening behind walls — everything must
be visible, accessible and actionable via the UI."
"""
from __future__ import annotations

from importlib import import_module
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QFrame, QComboBox,
    QPushButton, QScrollArea, QSizePolicy, QMessageBox, QLineEdit, QToolButton,
    QFileDialog, QPlainTextEdit,
)
from PySide6.QtGui import QFont

from egon_app import data


# ── full connections schema (1:1 with old views/settings.py) ───────────────
CONNECTIONS_SPEC = [
    {
        "id": "instapaper", "icon": "📥", "label": "Instapaper",
        "fields": [
            ("instapaper.username", "Email / username", False, "you@example.com"),
            ("instapaper.password", "Password",         True,  ""),
        ],
        "help": "Simple HTTP API · basic auth. Full reading-list reader needs OAuth (P5).",
        "test_module": "lib.adapters.instapaper", "test_fn": "authenticate",
    },
    {
        "id": "letterboxd", "icon": "🎬", "label": "Letterboxd",
        "fields": [
            ("letterboxd.username", "Username",                       False, "outdatedcaveman"),
            ("letterboxd.password", "Password (auto-login attempt)",  True,  ""),
        ],
        "help": ("Letterboxd has NO public API. Community workarounds (cookie scraping, "
                 "automated login) are blocked by their WAF / reCAPTCHA. Two real paths: "
                 "(a) we scrape your public profile's most-recent 72 films, "
                 "(b) you drop a one-time export ZIP for the full corpus."),
        "test_module": "lib.adapters.letterboxd", "test_fn": "auto_login",
        "extra_uploader": {
            "config_key": "letterboxd.export_path",
            "label":      "Letterboxd export ZIP (full corpus)",
            "filename":   "letterboxd-export.zip",
        },
    },
    {
        "id": "youtube", "icon": "🎵", "label": "YouTube + YouTube Music (read-only)",
        "fields": [],
        "help": ("Real-time API access via Google OAuth. Reuses your Drive OAuth client "
                 "automatically — if Drive is authorised, just click Authorize. Read-only: "
                 "youtube.readonly only. Pulls: liked songs+videos, your playlists, subscriptions."),
        "test_module": "lib.adapters.youtube", "test_fn": "live_status",
        "google_authorize_module": "lib.adapters.youtube",
        "supports_write_mode": True,
    },
    {
        "id": "kindle", "icon": "📖", "label": "Kindle (Amazon)",
        "fields": [
            ("kindle.region",      "Amazon region (com / com.br / co.uk / de / ...)", False, "com.br"),
            ("kindle.library_url", "Override URL (full library)", False, ""),
            ("kindle.export_path", "Path to Amazon data-export ZIP (optional)", False, ""),
        ],
        "help": ("Amazon has no public API. Three working paths, in order of "
                 "ease:\n"
                 " (a) Egon Chrome extension — click 'Pull library now' below. "
                 "Your real Chrome opens your Amazon library page, the extension "
                 "scrapes every owned book and POSTs to Egon. No anti-bot to dodge.\n"
                 " (b) Data-export ZIP — request at amazon.<your-tld>/hz/privacy-central/"
                 "data-requests. ~24h email, drop ZIP at kindle.export_path.\n"
                 " (c) Annotations only — click 'Login (notebook)' for the "
                 "Playwright path against /notebook (highlights, not full library).\n\n"
                 "Set kindle.region to your Amazon TLD (com.br for Brazil, "
                 "co.uk for UK, etc.). The 'Pull library now' button uses this "
                 "to construct the right URL."),
        "test_module": "lib.adapters.kindle", "test_fn": "live_status",
        "harvest_via_extension": {
            "url_fn": "lib.adapters.kindle._library_url",
            "endpoint": "http://127.0.0.1:8000/api/v1/kindle/library",
        },
    },
    {
        "id": "tvtime", "icon": "📺", "label": "TV Time (browser login)",
        "fields": [],
        "help": ("TV Time's mobile API no longer accepts plain passwords (we confirmed: "
                 "every attempt returns 'You did not give the correct password'). "
                 "Same Playwright path as Kindle/Paperpile: click Login, Chromium opens, "
                 "sign in once, session cached. Subsequent syncs are headless."),
        "test_module": "lib.adapters.tvtime", "test_fn": "live_status",
        "browser_login_module": "lib.adapters.tvtime",
    },
    {
        "id": "tmdb", "icon": "🎞️", "label": "TMDB (film metadata)",
        "fields": [
            ("tmdb.token",   "v4 Read Access Token (preferred)", True, ""),
            ("tmdb.api_key", "v3 API Key (alternative)",         True, ""),
        ],
        "help": ("Enriches your Letterboxd films with director, cast, genres, "
                 "runtime, synopsis, and clean posters — data Letterboxd doesn't "
                 "expose. Free key at themoviedb.org/settings/api (Application "
                 "URL can be http://localhost). Paste EITHER the v4 token OR the "
                 "v3 key. Results are cached to disk, so each film is fetched "
                 "from TMDB only once."),
        "test_module": "lib.adapters.tmdb", "test_fn": "live_status",
    },
    {
        "id": "pocketcasts", "icon": "🎧", "label": "Pocket Casts (podcasts)",
        "fields": [
            ("pocketcasts.email",    "Pocket Casts email", False, "you@example.com"),
            ("pocketcasts.password", "Pocket Casts password", True, ""),
        ],
        "help": ("Pocket Casts has no official public API, but its web-player API "
                 "is stable and uses a simple email/password → token login — no "
                 "OAuth, no captcha. Enter your credentials above and Save, then "
                 "Test. Pulls your subscribed podcasts (with cover art) and "
                 "listening history. Read-only — Egon never changes subscriptions, "
                 "queue, or playback."),
        "test_module": "lib.adapters.pocketcasts", "test_fn": "live_status",
    },
    {
        "id": "gdrive", "icon": "☁️", "label": "Google Drive (read-only)",
        "fields": [
            ("gdrive.client_id",     "OAuth client ID",     False, ""),
            ("gdrive.client_secret", "OAuth client secret", True,  ""),
        ],
        "help": ("Read-only scopes only — Egon cannot modify/share/delete anything.\n\n"
                 "Setup (one-time, ~3 min):\n"
                 " 1. console.cloud.google.com → New project (name: egon)\n"
                 " 2. Menu → APIs & Services → Library → enable Google Drive API\n"
                 " 3. Menu → Google Auth Platform → fill app name + your email\n"
                 " 4. ⚠ Critical: Google Auth Platform → Audience → Test users → "
                 "+ Add users → add your own email → Save\n"
                 " 5. Same page → Clients → Create client → Desktop → save id/secret\n"
                 " 6. Paste above → Save → Authorize"),
        "test_module": "lib.adapters.gdrive", "test_fn": "live_status",
        "google_authorize_module": "lib.adapters.gdrive",
        "supports_write_mode": True,
    },
    {
        "id": "gcalendar", "icon": "📅", "label": "Google Calendar (read-only)",
        "fields": [],
        "help": "Reuses Drive's OAuth client. Pulls every event ±90/180 days from all your calendars.",
        "test_module": "lib.adapters.gcalendar", "test_fn": "live_status",
        "google_authorize_module": "lib.adapters.gcalendar",
        "supports_write_mode": True,
    },
    {
        "id": "gmail", "icon": "📧", "label": "Gmail (read-only metadata)",
        "fields": [],
        "help": "Read-only metadata only (subject/from/to/date/snippet). No body content fetched.",
        "test_module": "lib.adapters.gmail", "test_fn": "live_status",
        "google_authorize_module": "lib.adapters.gmail",
        "supports_write_mode": True,
    },
    {
        "id": "gfit", "icon": "💪", "label": "Google Fit (read-only)",
        "fields": [],
        "help": "Steps · heart rate · weight · activity. Last 30 days.",
        "test_module": "lib.adapters.gfit", "test_fn": "live_status",
        "google_authorize_module": "lib.adapters.gfit",
    },
    {
        "id": "zotero_web", "icon": "📚", "label": "Zotero (full library via API)",
        "fields": [
            ("zotero.user_id", "User ID (numeric, from zotero.org/settings/keys)", False, ""),
            ("zotero.api_key", "API key (read-only)",                              True,  ""),
        ],
        "help": ("Local SQLite has only what Zotero synced locally. For your full library "
                 "use the Web API. Get keys at zotero.org/settings/keys → New Private Key → "
                 "Allow library access (read-only). User ID = the long number at the top."),
        "test_module": "lib.adapters.zotero_web", "test_fn": "live_status",
    },
    {
        "id": "paperpile", "icon": "📑", "label": "Paperpile",
        "fields": [
            ("paperpile.export_path", "Path to BibTeX/RIS export file", False, ""),
        ],
        "help": ("Paperpile's library loads via Firestore (Google Cloud) — it "
                 "is NOT in any capturable network call (confirmed: all API "
                 "traffic is auth/billing only), and reCAPTCHA blocks Playwright. "
                 "The reliable path is Paperpile's own export:\n"
                 " 1. In Paperpile, select all (Ctrl+A in the library)\n"
                 " 2. Export → BibTeX (or RIS) → save the file\n"
                 " 3. Put its path above → Save\n"
                 "Egon re-reads the file on every load, so re-export whenever "
                 "you've added refs and the new ones appear. The export keeps "
                 "every field (title/authors/year/journal/doi/url/keywords)."),
        "test_module": "lib.adapters.paperpile", "test_fn": "live_status",
    },
    {
        "id": "notion", "icon": "📓", "label": "Notion",
        "fields": [
            ("notion.token", "Integration token (claude-meta/.env reused if blank)", True, ""),
        ],
        "help": "Already configured via claude-meta/.env — leave blank to reuse. "
                "Get a new one at notion.so/my-integrations.",
        "test_module": "lib.adapters.notion", "test_fn": "live_status",
    },
    {
        "id": "mouseion", "icon": "🐭", "label": "Mouseion",
        "fields": [
            ("mouseion.path", "refs.db path (autodetected if at default)", False,
             str(Path.home() / ".local" / "share" / "mouseion" / "refs.db")),

        ],
        "help": "Reads either the local Flask service (port 7274) or refs.db directly.",
        "test_module": "lib.adapters.mouseion", "test_fn": "live_status",
    },
    {
        "id": "chrome_tabs", "icon": "🌐", "label": "Chrome tabs (desktop)",
        "fields": [],
        "help": ("Chrome 127+ silently blocks --remote-debugging-port (Google's "
                 "anti-malware mitigation). PERMANENT FIX: a tiny browser extension "
                 "that POSTs your tab list to Panop every 30 s (and on every tab "
                 "event). No DevTools port, no flags, works on any Chrome version.\n\n"
                 "Install (one-time):\n"
                 " 1. Click 'Install Chrome extension' below — it opens the folder.\n"
                 " 2. In Chrome: chrome://extensions/\n"
                 " 3. Toggle 'Developer mode' (top-right)\n"
                 " 4. Click 'Load unpacked' → select the egon_chrome_extension folder\n"
                 " 5. Done. Click the new Egon icon in the toolbar to see status.\n\n"
                 "All data stays on 127.0.0.1 — the extension only talks to Panop."),
        "test_module": "lib.adapters.chrome_tabs", "test_fn": "live_status",
        "open_extension_folder": True,
    },
]


# ── helpers ────────────────────────────────────────────────────────────────

def _get_dotted(cfg: dict, path: str, default: str = "") -> str:
    cur = cfg
    for p in path.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur if isinstance(cur, str) else default


def _set_dotted(cfg: dict, path: str, value: str) -> None:
    parts = path.split(".")
    cur = cfg
    for p in parts[:-1]:
        if p not in cur or not isinstance(cur[p], dict):
            cur[p] = {}
        cur = cur[p]
    cur[parts[-1]] = value


def _status_color(s: str) -> str:
    return {
        "ok": "#7FB069", "alive": "#7FB069",
        "unconfigured": "#9CA3AF", "warming": "#D4A24C", "stale": "#D4A24C",
        "timeout": "#D67A6A", "error": "#D67A6A",
    }.get(str(s).lower(), "#9CA3AF")


# ── worker that confirms cached-data counts off the UI thread ──────────────
# Several sources have a misleading live_status (Letterboxd's profile ping gets
# WAF-blocked; Kindle/Paperpile/Instapaper have no live list endpoint). This
# checks the FASTEST local source of truth for each and reports a count so the
# Settings badge can read 'ready · N items' instead of 'unconfigured'.

class _CountWorker(QThread):
    got_count = Signal(str, int)   # (source_id, count)

    def __init__(self, sids: list[str], parent=None):
        super().__init__(parent)
        self._sids = sids

    def _count_for(self, sid: str) -> int:
        try:
            if sid in ("kindle", "paperpile", "instapaper"):
                import httpx
                ep = {
                    "kindle":     "http://127.0.0.1:8000/api/v1/kindle/library",
                    "paperpile":  "http://127.0.0.1:8000/api/v1/paperpile/library",
                    "instapaper": "http://127.0.0.1:8000/api/v1/instapaper/library",
                }[sid]
                r = httpx.get(ep, timeout=2.0)
                return int((r.json() or {}).get("count") or 0)
            if sid == "letterboxd":
                from lib.adapters import letterboxd
                return len(letterboxd.items(5000))
            if sid == "mouseion":
                from lib.adapters import mouseion
                return len(mouseion.items(5000))
            if sid == "pocketcasts":
                from lib.adapters import pocketcasts
                return len(pocketcasts.podcasts())
            if sid in ("zotero_web", "zotero"):
                from lib.adapters import zotero_local
                return len(zotero_local.items(5000))
        except Exception:
            return 0
        return 0

    def run(self) -> None:
        for sid in self._sids:
            c = self._count_for(sid)
            if c > 0:
                self.got_count.emit(sid, c)


# ── worker for async adapter calls (test/authorize/login/snapshot) ─────────

class _AdapterWorker(QThread):
    done = Signal(bool, str)

    def __init__(self, module_path: str, fn_name: str, parent=None):
        super().__init__(parent)
        self._mod_path = module_path
        self._fn_name = fn_name

    def run(self):
        try:
            mod = import_module(self._mod_path)
            fn = getattr(mod, self._fn_name, None)
            if not callable(fn):
                self.done.emit(False, f"{self._mod_path}.{self._fn_name} not found")
                return
            r = fn()
            if isinstance(r, dict):
                ok = (r.get("status") in ("ok", "alive")
                      or r.get("authenticated") is True
                      or r.get("logged_in") is True)
                msg = (r.get("message") or r.get("error")
                       or ("ok" if ok else "failed"))
                self.done.emit(bool(ok), str(msg)[:300])
            else:
                self.done.emit(bool(r), "done" if r else "False")
        except Exception as e:
            self.done.emit(False, f"{type(e).__name__}: {e}"[:300])


# ── one adapter row, expandable ────────────────────────────────────────────

class _ConnectionCard(QFrame):
    """A single adapter card: header always visible, click to expand details."""

    def __init__(self, spec: dict, parent=None):
        super().__init__(parent)
        self._spec = spec
        self.setObjectName("card")
        self._inputs: dict[str, QLineEdit] = {}

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        # ── header (always visible) ──
        header = QFrame()
        header.setStyleSheet("QFrame:hover { background: #16404F; }")
        h = QHBoxLayout(header)
        h.setContentsMargins(14, 10, 14, 10)
        h.setSpacing(10)

        icon = QLabel(spec["icon"])
        icon.setStyleSheet("font-size: 18px;")
        h.addWidget(icon)

        title = QLabel(spec["label"])
        title.setStyleSheet("color: #F0E9D5; font-weight: 600; font-size: 13px;")
        h.addWidget(title, 1)

        self._status_lbl = QLabel("…")
        self._status_lbl.setMinimumWidth(110)
        self._status_lbl.setTextFormat(Qt.RichText)
        h.addWidget(self._status_lbl)

        self._toggle = QToolButton()
        self._toggle.setText("▾")
        self._toggle.setCheckable(True)
        self._toggle.setStyleSheet(
            "QToolButton { color: #9CA3AF; background: transparent; border: none; "
            "font-size: 14px; padding: 0 6px; }"
        )
        self._toggle.toggled.connect(self._on_toggle)
        h.addWidget(self._toggle)

        # make whole header clickable to expand
        header.mouseReleaseEvent = lambda _e: self._toggle.toggle()
        v.addWidget(header)

        # ── body (hidden by default) ──
        self._body = QFrame()
        self._body.setStyleSheet("QFrame { background: #0B1F28; border-top: 1px solid #1F4858; }")
        self._body.setVisible(False)
        bv = QVBoxLayout(self._body)
        bv.setContentsMargins(16, 12, 16, 14)
        bv.setSpacing(10)

        # help text
        if spec.get("help"):
            help_lbl = QLabel(spec["help"])
            help_lbl.setWordWrap(True)
            help_lbl.setStyleSheet("color: #9CA3AF; font-size: 12px; line-height: 1.5;")
            bv.addWidget(help_lbl)

        # field inputs
        if spec.get("fields"):
            from lib.ledger import load_config
            cfg = load_config() or {}
            grid = QGridLayout()
            grid.setContentsMargins(0, 0, 0, 0)
            grid.setSpacing(8)
            for i, (path, label, secret, placeholder) in enumerate(spec["fields"]):
                lab = QLabel(label)
                lab.setStyleSheet("color: #9CA3AF; font-size: 11px;")
                grid.addWidget(lab, i*2, 0, 1, 2)
                inp = QLineEdit()
                inp.setText(_get_dotted(cfg, path))
                inp.setPlaceholderText(placeholder)
                if secret:
                    inp.setEchoMode(QLineEdit.Password)
                inp.setStyleSheet(
                    "QLineEdit { background: #102F3C; color: #F0E9D5; "
                    "border: 1px solid #1F4858; border-radius: 3px; padding: 6px 8px; }"
                    "QLineEdit:focus { border-color: #60A5A8; }"
                )
                self._inputs[path] = inp
                grid.addWidget(inp, i*2 + 1, 0)
                if secret:
                    show_btn = QToolButton()
                    show_btn.setText("👁")
                    show_btn.setCheckable(True)
                    show_btn.toggled.connect(
                        lambda checked, inp=inp: inp.setEchoMode(
                            QLineEdit.Normal if checked else QLineEdit.Password))
                    show_btn.setStyleSheet(
                        "QToolButton { color: #9CA3AF; background: transparent; "
                        "border: none; padding: 4px 8px; }"
                    )
                    grid.addWidget(show_btn, i*2 + 1, 1)
                # File-path fields get a Browse… picker + auto-save on pick.
                # Bruno 2026-05-22: "where do I upload the export?" — this is it.
                elif path.endswith("_path") or "export" in path:
                    browse = QPushButton("Browse…")
                    browse.setStyleSheet(
                        "QPushButton { background: #16404F; color: #F0E9D5; "
                        "border: 1px solid #1F4858; border-radius: 3px; padding: 6px 12px; }"
                        "QPushButton:hover { background: #1F5366; }")
                    def _pick(_=False, inp=inp):
                        fn, _f = QFileDialog.getOpenFileName(
                            self, "Select file", "",
                            "References (*.bib *.ris *.txt);;All files (*.*)")
                        if fn:
                            inp.setText(fn)
                            self._save_fields()   # persist immediately
                    browse.clicked.connect(_pick)
                    grid.addWidget(browse, i*2 + 1, 1)
            bv.addLayout(grid)

        # mode toggle (Google services)
        if spec.get("supports_write_mode"):
            mode_row = QHBoxLayout()
            mode_row.setContentsMargins(0, 4, 0, 0)
            mode_lbl = QLabel("Access mode:")
            mode_lbl.setStyleSheet("color: #9CA3AF; font-size: 11px;")
            mode_row.addWidget(mode_lbl)
            self._mode_cb = QComboBox()
            self._mode_cb.addItem("🛡 Read-only", "read")
            self._mode_cb.addItem("✎ Read + Write", "readwrite")
            try:
                from lib import google_oauth
                cur = google_oauth.mode(spec["id"])
                idx = 0 if cur == "read" else 1
                self._mode_cb.setCurrentIndex(idx)
            except Exception:
                pass
            self._mode_cb.currentIndexChanged.connect(self._on_mode_change)
            mode_row.addWidget(self._mode_cb)
            mode_row.addStretch(1)
            wrap = QWidget(); wrap.setLayout(mode_row)
            bv.addWidget(wrap)

        # uploader (Letterboxd ZIP)
        if spec.get("extra_uploader"):
            up = spec["extra_uploader"]
            up_row = QHBoxLayout()
            up_lbl = QLabel(up["label"])
            up_lbl.setStyleSheet("color: #9CA3AF; font-size: 11px;")
            up_row.addWidget(up_lbl)
            from lib.ledger import load_config
            cfg2 = load_config() or {}
            current = _get_dotted(cfg2, up["config_key"]) or "(none)"
            self._uploader_path_lbl = QLabel(current)
            self._uploader_path_lbl.setStyleSheet("color: #F0E9D5; font-size: 11px;")
            up_row.addWidget(self._uploader_path_lbl, 1)
            up_btn = QPushButton("Choose file…")
            up_btn.clicked.connect(lambda _=False, u=up: self._choose_file(u))
            up_row.addWidget(up_btn)
            wrap = QWidget(); wrap.setLayout(up_row)
            bv.addWidget(wrap)

        # action buttons
        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 6, 0, 0)
        btn_row.setSpacing(6)
        if spec.get("fields"):
            b_save = QPushButton("Save")
            b_save.setStyleSheet(
                "QPushButton { background: #60A5A8; color: white; padding: 6px 14px; "
                "border-radius: 3px; font-weight: 600; border: none; }"
                "QPushButton:hover { background: #7BC5C7; }"
            )
            b_save.clicked.connect(self._save_fields)
            btn_row.addWidget(b_save)
        if spec.get("test_module"):
            b_test = QPushButton("Test")
            b_test.clicked.connect(self._test)
            btn_row.addWidget(b_test)
        if spec.get("google_authorize_module"):
            b_auth = QPushButton("Authorize…")
            b_auth.setStyleSheet(
                "QPushButton { background: #60A5A8; color: white; padding: 6px 14px; "
                "border-radius: 3px; font-weight: 600; border: none; }"
                "QPushButton:hover { background: #7BC5C7; }"
            )
            b_auth.clicked.connect(self._google_authorize)
            btn_row.addWidget(b_auth)
            b_revoke = QPushButton("Revoke")
            b_revoke.clicked.connect(self._revoke_token)
            btn_row.addWidget(b_revoke)
        if spec.get("browser_login_module"):
            b_login = QPushButton("Login (opens browser)")
            b_login.setStyleSheet(
                "QPushButton { background: #60A5A8; color: white; padding: 6px 14px; "
                "border-radius: 3px; font-weight: 600; border: none; }"
                "QPushButton:hover { background: #7BC5C7; }"
            )
            b_login.clicked.connect(self._browser_login)
            btn_row.addWidget(b_login)
        if spec.get("open_extension_folder"):
            b_ext = QPushButton("Install Chrome extension…")
            b_ext.setStyleSheet(
                "QPushButton { background: #60A5A8; color: white; padding: 6px 14px; "
                "border-radius: 3px; font-weight: 600; border: none; }"
                "QPushButton:hover { background: #7BC5C7; }"
            )
            b_ext.clicked.connect(self._open_extension_folder)
            btn_row.addWidget(b_ext)
        if spec.get("open_in_chrome_url"):
            # For services blocked by reCAPTCHA Enterprise / TLS fingerprinting
            # (Paperpile, Amazon Kindle). Opens the URL in the user's REAL
            # default browser via webbrowser.open — that's the only context
            # the bot defender will trust.
            b_ext_chrome = QPushButton("Open in my Chrome")
            b_ext_chrome.setStyleSheet(
                "QPushButton { background: #16404F; color: #F0E9D5; padding: 6px 14px; "
                "border-radius: 3px; font-weight: 600; border: 1px solid #1F4858; }"
                "QPushButton:hover { background: #1F5366; }"
            )
            b_ext_chrome.clicked.connect(self._open_in_real_chrome)
            btn_row.addWidget(b_ext_chrome)
        if spec.get("harvest_via_extension"):
            # One-click: open the right URL in your real Chrome AND poll the
            # extension's harvest endpoint until data arrives. Egon does the
            # work — Bruno just clicks the button.
            b_pull = QPushButton("Pull library now")
            b_pull.setStyleSheet(
                "QPushButton { background: #60A5A8; color: white; padding: 6px 14px; "
                "border-radius: 3px; font-weight: 600; border: none; }"
                "QPushButton:hover { background: #7BC5C7; }"
            )
            b_pull.clicked.connect(self._pull_library_via_extension)
            btn_row.addWidget(b_pull)
        b_sync = QPushButton("Sync now")
        b_sync.clicked.connect(self._sync_now)
        btn_row.addWidget(b_sync)
        btn_row.addStretch(1)
        wrap = QWidget(); wrap.setLayout(btn_row)
        bv.addWidget(wrap)

        v.addWidget(self._body)

    # ---------- internal ----------
    def _on_toggle(self, expanded: bool) -> None:
        self._toggle.setText("▴" if expanded else "▾")
        self._body.setVisible(expanded)

    def update_status(self, info: dict) -> None:
        status = info.get("status", "—")
        colour = _status_color(status)
        extra_keys = []
        for k in ("total_items", "total_links", "queue_count", "pages_mirrored",
                  "count", "size_mb", "username"):
            if k in info and info[k] is not None:
                extra_keys.append(f"{info[k]}")
                break
        suffix = f" · {extra_keys[0]}" if extra_keys else ""
        self._status_lbl.setText(
            f"<span style='color:{colour};'>●</span>  <b>{status}</b>{suffix}")

    def mark_ready_with_count(self, count: int) -> None:
        """Upgrade the badge to a green 'ready · N items' when we've confirmed
        cached data exists, even though the live API ping was unhealthy.
        Bruno 2026-05-22: the live ping (e.g. Letterboxd WAF block) was making
        sources with real data show 'unconfigured'. This corrects that."""
        if count and count > 0:
            self._status_lbl.setText(
                f"<span style='color:#7FB069;'>●</span>  <b>ready</b> · {count:,} items")

    def _save_fields(self) -> None:
        try:
            from lib.ledger import load_config, save_config
            cfg = load_config() or {}
            for path, inp in self._inputs.items():
                _set_dotted(cfg, path, (inp.text() or "").strip())
            save_config(cfg)
            QMessageBox.information(self, "Saved",
                f"{self._spec['label']} credentials saved to egon-config.json.")
        except Exception as e:
            QMessageBox.warning(self, "Save failed", str(e))

    def _on_mode_change(self, _idx: int) -> None:
        try:
            from lib.ledger import load_config, save_config
            cfg = load_config() or {}
            mode = self._mode_cb.currentData()
            cfg.setdefault(self._spec["id"], {})["mode"] = mode
            save_config(cfg)
            if mode == "readwrite":
                QMessageBox.warning(self, "Write mode enabled",
                    "Re-click Authorize to grant the new write scopes. "
                    "Egon never deletes without type-to-confirm.")
        except Exception as e:
            QMessageBox.warning(self, "Mode save failed", str(e))

    def _choose_file(self, up: dict) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, f"Select {up['filename']}", "", "ZIP archives (*.zip)")
        if path:
            try:
                from lib.ledger import load_config, save_config
                cfg = load_config() or {}
                _set_dotted(cfg, up["config_key"], path)
                save_config(cfg)
                self._uploader_path_lbl.setText(path)
                QMessageBox.information(self, "File set", f"Path saved: {path}")
            except Exception as e:
                QMessageBox.warning(self, "Save failed", str(e))

    def _test(self) -> None:
        m = self._spec["test_module"]; fn = self._spec.get("test_fn", "live_status")
        self._run_worker(m, fn, label="Test")

    def _google_authorize(self) -> None:
        m = self._spec["google_authorize_module"]
        self._run_worker(m, "start_auth_flow", label="Authorize")

    def _revoke_token(self) -> None:
        m = self._spec["google_authorize_module"]
        self._run_worker(m, "revoke", label="Revoke")

    def _browser_login(self) -> None:
        m = self._spec["browser_login_module"]
        QMessageBox.information(self, "Browser opening",
            f"A browser window will open for {self._spec['label']}. "
            "Sign in there. Close the window when done — session is saved locally.")
        self._run_worker(m, "start_auth_flow", label="Login")

    def _sync_now(self) -> None:
        m = self._spec.get("test_module", "")
        if not m:
            QMessageBox.information(self, "Sync", "No test module configured for this adapter.")
            return
        self._run_worker(m, "snapshot", label="Sync")

    # Minimum extension version that knows about all the current harvesters.
    # Bumped whenever we add a new site or fix a regex/selector in
    # external/egon_chrome_extension/. Pull-library refuses to run when the
    # installed extension is older — saves Bruno from "why is this broken"
    # cycles caused by Chrome silently keeping the old version cached.
    _MIN_EXT_VERSION = (1, 7, 4)

    @staticmethod
    def _parse_ver(v: str) -> tuple[int, ...]:
        try:
            return tuple(int(x) for x in str(v).split("."))
        except Exception:
            return (0,)

    def _check_extension_state(self) -> tuple[str, str | None, str | None]:
        """Return (status_text, installed_version_or_None, error_or_None).

        Reads /api/v1/chrome_tabs/state — the extension POSTs its own version
        on every push, so a recent payload tells us what's loaded.
        """
        try:
            import httpx
            r = httpx.get("http://127.0.0.1:8000/api/v1/chrome_tabs/state",
                          timeout=2.0)
            d = r.json() or {}
            if d.get("status") != "ok":
                return ("Extension NEVER pushed — not installed / Chrome not "
                        "running / Panop unreachable.", None,
                        "no_chrome_tabs_state")
            ext = d.get("extension") or {}
            ver = ext.get("version")
            if not ver:
                return ("Extension is OUTDATED — pushed without version field "
                        "(v1.1 or older). Reload it at chrome://extensions/.",
                        None, "missing_version_field")
            installed = self._parse_ver(ver)
            if installed < self._MIN_EXT_VERSION:
                need = ".".join(str(x) for x in self._MIN_EXT_VERSION)
                return (f"Extension v{ver} is outdated (need ≥ v{need}). "
                        f"Go to chrome://extensions/ and click the reload "
                        f"arrow under 'Egon — tabs + content harvester'.",
                        ver, "outdated_extension")
            received = d.get("received_at", "")
            return (f"Extension v{ver} (last push {received[:16]})",
                    ver, None)
        except Exception as e:
            return (f"Could not check extension: {e}", None, "exception")

    def _pull_library_via_extension(self) -> None:
        """One-click library pull. Verifies the Chrome extension is loaded
        AND up to date, then opens the right URL in the user's real Chrome
        and polls the extension's harvest endpoint every 2 s for up to 60 s.

        Why this matters: Bruno's request was 'pull it yourself, I don't know
        where my Kindle library lives'. This wires discovery + version-check
        + harvest end-to-end so a single click does everything OR tells you
        exactly what's wrong.
        """
        import importlib
        import webbrowser
        from PySide6.QtCore import QTimer

        # Pre-flight: extension must be loaded and at the minimum version.
        status_text, ver, err = self._check_extension_state()
        if err:
            need = ".".join(str(x) for x in self._MIN_EXT_VERSION)
            QMessageBox.warning(self, "Extension check failed",
                f"Pull library now requires the Egon Chrome extension at "
                f"v{need} or newer.\n\nCurrent state: {status_text}\n\n"
                f"Open chrome://extensions/, find 'Egon — tabs + content "
                f"harvester', click the reload arrow. Then try again.")
            return

        cfg = self._spec.get("harvest_via_extension") or {}
        # Resolve the URL — either a static string or a callable in lib/
        url = cfg.get("url_static", "")
        if not url and cfg.get("url_fn"):
            mod_path, _, fn_name = cfg["url_fn"].rpartition(".")
            try:
                url = getattr(importlib.import_module(mod_path), fn_name)()
            except Exception as e:
                QMessageBox.warning(self, "Pull failed",
                    f"Could not resolve URL: {e}")
                return
        endpoint = cfg.get("endpoint", "")
        if not url or not endpoint:
            QMessageBox.warning(self, "Pull failed",
                "Harvest spec is missing url + endpoint.")
            return

        # Snapshot the current count so we know when new data arrives.
        try:
            import httpx
            r = httpx.get(endpoint, timeout=2.0)
            self._pull_baseline_count = (r.json() or {}).get("count", 0)
        except Exception:
            self._pull_baseline_count = 0

        try:
            webbrowser.open(url, new=2)
        except Exception as e:
            QMessageBox.warning(self, "Pull failed", str(e))
            return

        # Poll for up to 60 s. We check every 2 s; as soon as we see a
        # count > baseline OR a fresh `received_at`, we report success.
        self._pull_endpoint = endpoint
        self._pull_attempts = 0
        self._pull_max_attempts = 30   # 30 × 2 s = 60 s
        self._pull_timer = QTimer(self)
        self._pull_timer.setInterval(2000)
        self._pull_timer.timeout.connect(self._poll_harvest)
        self._pull_timer.start()
        self._pull_started_at = __import__("time").time()
        QMessageBox.information(self, "Pulling library…",
            f"Opened {url} in your default browser.\n\n"
            "The Egon Chrome extension is now extracting your library. "
            "Egon will poll for the result for up to 60 seconds.\n\n"
            "Keep that tab open until the data lands — switch back to "
            "Egon to see the count update.")

    def _poll_harvest(self) -> None:
        import httpx
        self._pull_attempts += 1
        try:
            r = httpx.get(self._pull_endpoint, timeout=2.0)
            data = r.json() or {}
            new_count = data.get("count", 0)
            if new_count > self._pull_baseline_count:
                self._pull_timer.stop()
                self.refresh()
                QMessageBox.information(self, "Library pulled",
                    f"✓ Extension captured {new_count:,} items.\n"
                    "Open the relevant page (References / Media / Apps) "
                    "to see the data.")
                return
        except Exception:
            pass
        if self._pull_attempts >= self._pull_max_attempts:
            self._pull_timer.stop()
            QMessageBox.warning(self, "No harvest yet",
                "60 s elapsed with no data from the extension.\n\n"
                "Possible causes:\n"
                "  • Extension not reloaded after the recent v1.1 update — "
                "go to chrome://extensions/ and click the reload arrow.\n"
                "  • You're not signed in to the site in this Chrome.\n"
                "  • The page hasn't finished loading yet.\n\n"
                "The Chrome tab stays open; the harvest will still POST "
                "whenever it succeeds. Click 'Pull library now' again "
                "after the page loads to re-poll.")

    def _open_in_real_chrome(self) -> None:
        """Open the adapter's URL in the user's REAL default browser.

        Used for services (Paperpile / Amazon) whose bot defenders block ALL
        automation-driven browsers regardless of stealth measures. The user's
        normal Chrome has the right TLS fingerprint, plugin set, and history —
        they sail through. This is purely a navigation aid; data ingestion for
        these services has to come via export files or paid-tier APIs.
        """
        import webbrowser
        url = self._spec.get("open_in_chrome_url", "")
        if not url:
            return
        try:
            webbrowser.open(url, new=2)   # new=2 → new tab in default browser
            QMessageBox.information(self, "Opened in your browser",
                f"Opened {url} in your default browser.\n\n"
                "For data sync, see the help text above — Playwright is "
                "blocked for this service; only the noted alternatives work.")
        except Exception as e:
            QMessageBox.warning(self, "Open failed", str(e))

    def _open_extension_folder(self) -> None:
        """Open the Chrome-extension folder in File Explorer and walk Bruno
        through the chrome://extensions side."""
        import os, subprocess
        from pathlib import Path as _P
        ext = (_P(__file__).resolve().parent.parent.parent
               / "external" / "egon_chrome_extension")
        if not ext.exists():
            QMessageBox.warning(self, "Extension missing",
                f"Folder not found at {ext}")
            return
        try:
            os.startfile(str(ext))
        except Exception as e:
            QMessageBox.warning(self, "Open folder failed", str(e))
            return
        QMessageBox.information(self, "Install Chrome extension",
            "Folder opened in Explorer.\n\n"
            "Next steps in Chrome:\n"
            "  1. Open  chrome://extensions/\n"
            "  2. Toggle 'Developer mode' (top-right corner)\n"
            "  3. Click 'Load unpacked'\n"
            "  4. Select the egon_chrome_extension folder that just opened\n"
            "  5. Done — click the Egon icon in your toolbar to see status\n\n"
            "All data stays on 127.0.0.1.")

    def _run_worker(self, module: str, fn: str, label: str) -> None:
        self._w = _AdapterWorker(module, fn, parent=self)
        self._w.done.connect(lambda ok, msg, lbl=label: self._on_action_done(lbl, ok, msg))
        self._w.start()

    def _on_action_done(self, label: str, ok: bool, msg: str) -> None:
        if ok:
            QMessageBox.information(self, label, f"✓ {msg}")
        else:
            QMessageBox.warning(self, label, f"✗ {msg}")
        # Re-fetch status from last_pass to refresh chip
        d = data.last_pass()
        info = d.get("sources", {}).get(self._spec["id"], {})
        if info:
            self.update_status(info)


# ── settings page ───────────────────────────────────────────────────────────

class SettingsPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
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

        title = QLabel("Settings")
        title.setStyleSheet("font-size: 22px; font-weight: 700; color: #F0E9D5;")
        v.addWidget(title)
        sub = QLabel("Plan mode · per-adapter configuration · system state · maintenance.")
        sub.setStyleSheet("color: #9CA3AF;")
        v.addWidget(sub)

        # ── Chrome extension status banner ──
        # Tells the user at-a-glance whether the helper extension is loaded
        # and at the required version. Without this, "Pull library now"
        # silently no-ops if the user hasn't reloaded the extension after an
        # update. Bruno 2026-05-20.
        self._ext_banner = QFrame()
        self._ext_banner.setObjectName("card")
        eb = QHBoxLayout(self._ext_banner)
        eb.setContentsMargins(16, 10, 16, 10)
        self._ext_status_lbl = QLabel("checking Chrome extension…")
        self._ext_status_lbl.setStyleSheet("color: #F0E9D5; font-weight: 600;")
        self._ext_status_lbl.setTextFormat(Qt.RichText)
        self._ext_status_lbl.setWordWrap(True)
        eb.addWidget(self._ext_status_lbl, 1)
        b_recheck = QPushButton("Recheck")
        b_recheck.clicked.connect(self._refresh_ext_status)
        eb.addWidget(b_recheck)
        b_open_ext_page = QPushButton("Open chrome://extensions")
        b_open_ext_page.clicked.connect(self._open_chrome_extensions)
        eb.addWidget(b_open_ext_page)
        v.addWidget(self._ext_banner)

        # ── plan mode ──
        card_plan = self._card("Plan mode")
        v.addWidget(card_plan)
        self._plan_cb = QComboBox()
        self._plan_cb.addItems(["pro", "max", "api"])
        self._plan_cb.setCurrentText(data.ledger_config().get("plan_mode", "pro"))
        self._plan_cb.currentTextChanged.connect(self._on_plan_change)
        card_plan.layout().addWidget(self._row("Claude plan", self._plan_cb,
            "Drives Token Ledger headline (tokens for pro/max, $ for api)"))

        # ── connections (the meat) ──
        card_conn = self._card("Connections")
        v.addWidget(card_conn)
        sub2 = QLabel(f"{len(CONNECTIONS_SPEC)} connectors · click any row to expand. "
                      "All credentials in egon-config.json (gitignored, never leaves disk).")
        sub2.setStyleSheet("color: #6B7280; font-size: 11px; padding: 0 16px 6px;")
        sub2.setWordWrap(True)
        card_conn.layout().addWidget(sub2)
        self._cards: dict[str, _ConnectionCard] = {}
        for spec in sorted(CONNECTIONS_SPEC, key=lambda s: s["label"].lower()):
            cc = _ConnectionCard(spec)
            self._cards[spec["id"]] = cc
            card_conn.layout().addWidget(cc)

        # ── system state ──
        card_sys = self._card("System state")
        v.addWidget(card_sys)
        self._sys_text = QPlainTextEdit()
        self._sys_text.setReadOnly(True)
        self._sys_text.setFont(QFont("Cascadia Mono", 9))
        self._sys_text.setMaximumHeight(220)
        self._sys_text.setStyleSheet(
            "QPlainTextEdit { background: #0B1F28; color: #9CA3AF; "
            "border: 1px solid #1F4858; border-radius: 4px; padding: 8px; }"
        )
        card_sys.layout().addWidget(self._sys_text)

        # ── maintenance ──
        card_act = self._card("Maintenance")
        v.addWidget(card_act)
        ar = QHBoxLayout()
        ar.setContentsMargins(16, 6, 16, 6)
        for label, fn in [
            ("Sync ALL libraries now",   self._sync_everything),
            ("Run snapshot now",         self._snapshot),
            ("Reload Panop subprocess",  self._reload_panop),
            ("Show phone keepalive log", self._show_keepalive_log),
            ("Open egon-config.json",    self._open_config),
        ]:
            b = QPushButton(label)
            b.clicked.connect(fn)
            ar.addWidget(b)
        ar.addStretch(1)
        wrap = QWidget(); wrap.setLayout(ar)
        card_act.layout().addWidget(wrap)

        v.addStretch(1)
        self.refresh()

    # ---------- card layout helpers ----------
    def _card(self, title: str) -> QFrame:
        f = QFrame(); f.setObjectName("card")
        vv = QVBoxLayout(f); vv.setContentsMargins(0, 0, 0, 12); vv.setSpacing(0)
        l = QLabel(title); l.setObjectName("cardTitle")
        vv.addWidget(l)
        return f

    def _row(self, label: str, widget: QWidget, hint: str = "") -> QWidget:
        w = QFrame()
        h = QHBoxLayout(w); h.setContentsMargins(16, 8, 16, 8); h.setSpacing(14)
        l = QLabel(label); l.setMinimumWidth(160)
        l.setStyleSheet("color: #F0E9D5; font-weight: 600;")
        h.addWidget(l); h.addWidget(widget, 1)
        if hint:
            hh = QLabel(hint); hh.setStyleSheet("color: #6B7280; font-size: 11px;")
            h.addWidget(hh)
        return w

    # ---------- chrome extension banner ----------
    def _refresh_ext_status(self) -> None:
        """Poll Panop's chrome_tabs/state, surface the version + last push."""
        try:
            import httpx
            r = httpx.get("http://127.0.0.1:8000/api/v1/chrome_tabs/state",
                          timeout=2.0)
            d = r.json() or {}
        except Exception as e:
            self._ext_banner_set("red", f"Cannot reach Panop on :8000 ({e})")
            return
        if d.get("status") != "ok":
            self._ext_banner_set("red",
                "Egon Chrome extension is NOT installed (or has never pushed). "
                "Click 'Open chrome://extensions' → Load unpacked → "
                "select <code>external/egon_chrome_extension</code>.")
            return
        ext = d.get("extension") or {}
        ver = ext.get("version")
        if not ver:
            self._ext_banner_set("amber",
                "Extension is loaded but on an old version (no version field "
                "in payload). Reload it at chrome://extensions/ to pick up "
                "v1.2.1+.")
            return
        from egon_app.pages.settings import _ConnectionCard as _CC
        installed = _CC._parse_ver(ver)
        if installed < _CC._MIN_EXT_VERSION:
            need = ".".join(str(x) for x in _CC._MIN_EXT_VERSION)
            self._ext_banner_set("amber",
                f"Extension v{ver} is OUTDATED — need ≥ v{need}. "
                f"chrome://extensions/ → click the reload arrow under "
                f"'Egon — tabs + content harvester'.")
            return
        received = d.get("received_at", "")
        count = d.get("count", "—")
        self._ext_banner_set("green",
            f"Egon extension v{ver} ✓ — last push {received[:16]} "
            f"({count} tabs seen).")

    def _ext_banner_set(self, level: str, html: str) -> None:
        bg = {"green": "#16404F", "amber": "#3A2E1B", "red": "#3A1B1B"}.get(level, "#16404F")
        border = {"green": "#1F4858", "amber": "#7A5A2E", "red": "#7A2E2E"}.get(level, "#1F4858")
        dot = {"green": "#7FB069", "amber": "#D4A24C", "red": "#D67A6A"}.get(level, "#9CA3AF")
        self._ext_banner.setStyleSheet(
            f"QFrame#card {{ background: {bg}; border: 1px solid {border}; "
            f"border-radius: 6px; }}"
        )
        self._ext_status_lbl.setText(f"<span style='color:{dot};'>●</span>  {html}")

    def _open_chrome_extensions(self) -> None:
        import webbrowser
        webbrowser.open("chrome://extensions/", new=2)

    # ---------- refresh ----------
    def refresh(self) -> None:
        self._refresh_ext_status()
        d = data.last_pass()
        sources = d.get("sources", {})
        unhealthy = []
        for sid, card in self._cards.items():
            info = sources.get(sid, {}) or {"status": "—"}
            card.update_status(info)
            if str(info.get("status", "")).lower() not in ("ok", "alive", "ready"):
                unhealthy.append(sid)
        self._refresh_system_state(d)
        # Background: for sources whose live ping was unhealthy, check whether
        # cached data actually exists and upgrade the badge to 'ready · N'.
        if unhealthy:
            self._count_worker = _CountWorker(unhealthy, parent=self)
            self._count_worker.got_count.connect(self._on_count)
            self._count_worker.start()

    def _on_count(self, sid: str, count: int) -> None:
        card = self._cards.get(sid)
        if card and count > 0:
            card.mark_ready_with_count(count)

    def _refresh_system_state(self, last_pass: dict) -> None:
        lines = []
        # snapshot meta
        lines.append(f"snapshot:    generated_at={last_pass.get('generated_at', '—')}")
        lines.append(f"             duration={last_pass.get('duration_seconds', '—')}s · "
                     f"items={last_pass.get('items_processed', '—')}")
        # panop
        try:
            ps = data.panop_status(timeout_s=1.5)
            lines.append(f"panop:       adb={ps.get('adb_connected')} · "
                         f"device={ps.get('device_id')} · "
                         f"chrome_running={ps.get('chrome_running')} · "
                         f"tabs_seen={ps.get('tabs_seen')}")
        except Exception as e:
            lines.append(f"panop:       UNREACHABLE ({e})")
        # phone keepalive log tail
        try:
            from datetime import datetime
            log = (Path(__file__).resolve().parent.parent.parent
                   / "logs" / f"phone-keepalive-{datetime.now():%Y-%m}.log")
            if log.exists():
                tail = log.read_text(encoding="utf-8").splitlines()[-3:]
                for t in tail:
                    lines.append(f"keepalive:   {t}")
            else:
                lines.append("keepalive:   (no log yet)")
        except Exception as e:
            lines.append(f"keepalive:   log err: {e}")
        # locked phone target
        try:
            import json as _json
            lock = (Path(__file__).resolve().parent.parent.parent
                    / "state/panop/locked_target.json")
            if lock.exists():
                d2 = _json.loads(lock.read_text(encoding="utf-8"))
                lines.append(f"phone lock:  {d2.get('target')} · "
                             f"set={d2.get('set_at')} · serial={d2.get('serial')}")
            else:
                lines.append("phone lock:  (none — run scripts/lock_phone_to_5555.py)")
        except Exception as e:
            lines.append(f"phone lock:  err {e}")
        self._sys_text.setPlainText("\n".join(lines))

    # ---------- actions ----------
    def _on_plan_change(self, new_plan: str) -> None:
        try:
            from lib.ledger import load_config, save_config
            cfg = load_config() or {}
            cfg["plan_mode"] = new_plan
            save_config(cfg)
        except Exception as e:
            QMessageBox.warning(self, "Save failed", str(e))

    def _snapshot(self) -> None:
        ok, msg = data.trigger_pass("daily")
        data.force_refresh()
        self.refresh()
        QMessageBox.information(self, "Snapshot", msg)

    def _sync_everything(self) -> None:
        """Signal the Chrome extension to run a full hands-off library sync.

        POSTs a timestamp to Panop's /api/v1/sync/request. The extension
        polls that endpoint every 30 s; when it sees the new timestamp it
        opens each library page in a background tab, harvests, and closes
        it — Kindle, Paperpile, Instapaper, all of it. No tabs to open by
        hand. Also runs the local adapter snapshot immediately.
        """
        import httpx
        try:
            r = httpx.post("http://127.0.0.1:8000/api/v1/sync/request", timeout=4.0)
            ok = r.status_code == 200
        except Exception as e:
            ok = False
            err = str(e)[:160]
        # Local snapshot right away (covers Zotero/Mouseion/Google/etc.)
        data.trigger_pass("daily")
        data.force_refresh()
        self.refresh()
        if ok:
            QMessageBox.information(self, "Sync everything",
                "Signal sent. The Chrome extension will open each library in a "
                "background tab, harvest it, and close it — within ~30 s, "
                "provided Chrome is running with the Egon extension (v1.4+).\n\n"
                "Local sources (Zotero, Mouseion, Google, Vault) refreshed now.")
        else:
            QMessageBox.warning(self, "Sync everything",
                f"Couldn't reach Panop to send the extension signal ({err}).\n"
                "Local sources were still refreshed. Is Panop running on :8000?")

    def _reload_panop(self) -> None:
        try:
            from lib import panop_proc
            panop_proc.ensure_running(log_fn=lambda l, **k: None)
            QMessageBox.information(self, "Panop", "Reload triggered.")
            self.refresh()
        except Exception as e:
            QMessageBox.warning(self, "Panop reload", str(e))

    def _show_keepalive_log(self) -> None:
        from datetime import datetime
        log = (Path(__file__).resolve().parent.parent.parent
               / "logs" / f"phone-keepalive-{datetime.now():%Y-%m}.log")
        if not log.exists():
            QMessageBox.information(self, "Keepalive log", f"No log yet at {log}.")
            return
        try:
            lines = log.read_text(encoding="utf-8").splitlines()[-30:]
            QMessageBox.information(self, "Phone keepalive — last 30 events", "\n".join(lines))
        except Exception as e:
            QMessageBox.warning(self, "Log read", str(e))

    def _open_config(self) -> None:
        import os, subprocess
        p = Path(__file__).resolve().parent.parent.parent / "egon-config.json"
        if not p.exists():
            QMessageBox.information(self, "egon-config.json",
                f"File doesn't exist yet at {p}. Save credentials in any "
                "connection row to create it.")
            return
        try:
            os.startfile(str(p))
        except Exception as e:
            QMessageBox.warning(self, "Open config", str(e))
