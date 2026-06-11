"""Egon Connect — OS floating widget (works without Chrome).

Bruno 2026-06-10: a desktop floating widget for the Connection Engine that reads
the actual TEXT CONTENT of whatever window you're in (via Windows UI Automation —
not a screenshot), anywhere on the laptop: Word, a code editor, Obsidian, a PDF
reader, a browser, Notes. Press the global hotkey and Egon surfaces connections
from all your archives + shared mind in a small always-on-top panel.

  • Global hotkey: Ctrl+Alt+Space (capture focused window + connect).
  • Capture priority: current text SELECTION (clipboard via Ctrl+C) > the
    focused edit/document control's text (UI Automation Value/Text pattern) >
    the foreground window's text. Real content, no OCR, no screenshot.
  • Talks to the always-on local mind: POST http://127.0.0.1:8000/api/v1/mind/connect.
  • Frameless, always-on-top, draggable, click an item to open it. You can also
    type/paste directly into the box.
  • Standalone process (pythonw, hidden) — independent of Egon's desktop app and
    of Chrome. Launch via scripts/connect_widget.py.

Run:  .venv\\Scripts\\pythonw.exe scripts\\connect_widget.py
"""
from __future__ import annotations

import json
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    import lib.no_console  # noqa: F401  (hide any child consoles)
except Exception:
    pass

from PySide6.QtCore import Qt, QObject, Signal, QTimer, QPoint
from PySide6.QtGui import QGuiApplication, QIcon
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QPlainTextEdit, QScrollArea, QFrame,
)

CONNECT_URL = "http://127.0.0.1:8000/api/v1/mind/connect"
HOTKEY = "ctrl+alt+space"

_SRC_ICON = {
    "instapaper": "📰", "zotero": "📚", "paperpile": "📄", "kindle": "📖",
    "letterboxd": "🎬", "youtube_music": "🎵", "pocketcasts": "🎧",
    "chrome_bookmarks": "🔖", "chrome_tabs": "🗂️", "notion_workspace": "🟦",
    "tvtime": "📺", "mind-memory": "🧠",
}


# ── content capture (UI Automation + clipboard) ──────────────────────────────
def _capture_focused_text() -> tuple[str, str]:
    """Return (text, how). Reads the focused window's CONTENT via UIA, with a
    clipboard-selection fallback. Runs in a worker thread (COM-safe)."""
    # 1) selection via clipboard (most precise — "what I highlighted")
    try:
        import keyboard
        prev = _clip_get()
        keyboard.send("ctrl+c")
        time.sleep(0.12)
        sel = _clip_get()
        if sel and sel.strip() and sel != prev and len(sel.strip()) >= 12:
            return sel.strip()[:6000], "selection"
        _clip_set(prev)   # restore clipboard if our copy grabbed nothing useful
    except Exception:
        pass
    # 2) focused control text via UI Automation
    try:
        import uiautomation as auto
        auto.SetGlobalSearchTimeout(0.6)
        ctrl = auto.GetFocusedControl()
        txt = ""
        for getter in ("GetValuePattern", "GetTextPattern"):
            try:
                pat = getattr(ctrl, getter)()
                txt = (pat.Value if getter == "GetValuePattern"
                       else pat.DocumentRange.GetText(8000)) or ""
                if txt.strip():
                    break
            except Exception:
                continue
        if not txt.strip():
            # walk up to the top window and grab its readable text
            try:
                win = ctrl.GetTopLevelControl()
                tp = win.GetTextPattern()
                txt = tp.DocumentRange.GetText(8000) or ""
            except Exception:
                pass
        if txt.strip():
            return txt.strip()[:6000], "focused window"
    except Exception:
        pass
    return "", "nothing"


def _clip_get() -> str:
    try:
        return QGuiApplication.clipboard().text() or ""
    except Exception:
        return ""


def _clip_set(s: str) -> None:
    try:
        QGuiApplication.clipboard().setText(s or "")
    except Exception:
        pass


def _connect(text: str) -> dict:
    try:
        body = json.dumps({"text": text, "limit": 18}).encode()
        req = urllib.request.Request(CONNECT_URL, data=body,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"status": "error", "error": f"mind not reachable: {e}"}


# ── widget ───────────────────────────────────────────────────────────────────
class Bridge(QObject):
    hotkey = Signal()


class ConnectWidget(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Egon Connect")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self.resize(400, 460)
        ico = ROOT / "shell" / "egon.ico"
        if ico.exists():
            self.setWindowIcon(QIcon(str(ico)))
        self._drag = None

        root = QVBoxLayout(self); root.setContentsMargins(0, 0, 0, 0); root.setSpacing(0)
        bar = QFrame(); bar.setStyleSheet("background:#16404F;")
        bl = QHBoxLayout(bar); bl.setContentsMargins(12, 8, 8, 8)
        t = QLabel("✨ Egon Connect"); t.setStyleSheet("color:#D4A24C; font-weight:700;")
        bl.addWidget(t); bl.addStretch(1)
        self._mode = QLabel(""); self._mode.setStyleSheet("color:#9CA3AF; font-size:11px;")
        bl.addWidget(self._mode)
        x = QPushButton("✕"); x.setFixedSize(24, 24)
        x.setStyleSheet("background:transparent; color:#9CA3AF; border:none; font-size:14px;")
        x.clicked.connect(self.hide); bl.addWidget(x)
        bar.mousePressEvent = self._bar_press
        bar.mouseMoveEvent = self._bar_move
        root.addWidget(bar)

        body = QWidget(); body.setStyleSheet("background:#0B1F28;")
        bv = QVBoxLayout(body); bv.setContentsMargins(12, 10, 12, 12); bv.setSpacing(8)
        hint = QLabel(f"Press {HOTKEY.upper()} anywhere to capture what you're "
                      "reading/writing — or type below.")
        hint.setStyleSheet("color:#9CA3AF; font-size:11px;"); hint.setWordWrap(True)
        bv.addWidget(hint)
        self._input = QPlainTextEdit()
        self._input.setStyleSheet("QPlainTextEdit{background:#102F3C; color:#F0E9D5;"
                                  "border:1px solid #1F4858; border-radius:6px; padding:6px;}")
        self._input.setFixedHeight(70)
        bv.addWidget(self._input)
        go = QPushButton("Connect")
        go.setStyleSheet("QPushButton{background:#D4A24C; color:#0E2630; border:none;"
                         "border-radius:6px; padding:7px; font-weight:700;}")
        go.clicked.connect(lambda: self._run_text(self._input.toPlainText()))
        bv.addWidget(go)
        self._scroll = QScrollArea(); self._scroll.setWidgetResizable(True)
        self._scroll.setStyleSheet("border:1px solid #1F4858; border-radius:6px; background:transparent;")
        self._host = QWidget(); self._results = QVBoxLayout(self._host)
        self._results.setContentsMargins(8, 8, 8, 8); self._results.setSpacing(6)
        self._results.addStretch(1)
        self._scroll.setWidget(self._host)
        bv.addWidget(self._scroll, 1)
        root.addWidget(body, 1)

        self.bridge = Bridge()
        self.bridge.hotkey.connect(self._on_hotkey)

    # drag the frameless window by its title bar
    def _bar_press(self, e):
        if e.button() == Qt.LeftButton:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def _bar_move(self, e):
        if self._drag is not None and (e.buttons() & Qt.LeftButton):
            self.move(e.globalPosition().toPoint() - self._drag)

    def _on_hotkey(self):
        # show near the cursor, then capture + connect off the UI thread
        self._place_near_cursor()
        self.show(); self.raise_(); self.activateWindow()
        self._mode.setText("capturing…")
        threading.Thread(target=self._capture_and_connect, daemon=True).start()

    def _place_near_cursor(self):
        try:
            c = QGuiApplication.primaryScreen().availableGeometry()
            p = self.cursor().pos()
            x = min(max(p.x() - 200, c.left() + 8), c.right() - self.width() - 8)
            y = min(max(p.y() + 16, c.top() + 8), c.bottom() - self.height() - 8)
            self.move(QPoint(x, y))
        except Exception:
            pass

    def _capture_and_connect(self):
        text, how = _capture_focused_text()
        if not text:
            QTimer.singleShot(0, lambda: self._render({"status": "error",
                "error": "Couldn't read the window's text. Select some text and try again, or type below."}, "—"))
            return
        QTimer.singleShot(0, lambda: self._input.setPlainText(text[:1500]))
        res = _connect(text)
        QTimer.singleShot(0, lambda: self._render(res, how))

    def _run_text(self, text):
        text = (text or "").strip()
        if len(text) < 3:
            return
        self._mode.setText("connecting…")
        def work():
            res = _connect(text)
            QTimer.singleShot(0, lambda: self._render(res, "typed"))
        threading.Thread(target=work, daemon=True).start()

    def _clear(self):
        while self._results.count():
            it = self._results.takeAt(0)
            if it.widget():
                it.widget().deleteLater()

    def _render(self, res: dict, how: str):
        self._clear()
        if res.get("status") != "ok":
            self._mode.setText("")
            lbl = QLabel(res.get("error", "no result")); lbl.setStyleSheet("color:#D67A6A;")
            lbl.setWordWrap(True); self._results.addWidget(lbl); self._results.addStretch(1)
            return
        conns = res.get("connections", [])
        self._mode.setText(f"{res.get('mode','')} · {how} · {len(conns)}")
        if not conns:
            self._results.addWidget(QLabel("No connections found."))
            self._results.addStretch(1); return
        for c in conns:
            self._results.addWidget(self._row(c))
        self._results.addStretch(1)

    def _row(self, c: dict) -> QFrame:
        f = QFrame(); f.setStyleSheet("QFrame{background:#0E2630; border:1px solid #1F4858; border-radius:6px;}")
        v = QVBoxLayout(f); v.setContentsMargins(8, 6, 8, 6); v.setSpacing(1)
        ic = _SRC_ICON.get(c.get("source", ""), "•")
        top = QHBoxLayout()
        title = QLabel(f"{ic} {c.get('title','')[:80]}")
        title.setStyleSheet("color:#F0E9D5; font-weight:600;"); title.setWordWrap(True)
        top.addWidget(title, 1)
        if c.get("url"):
            b = QPushButton("open"); b.setStyleSheet("background:#7BC5C7; color:#0E2630;"
                "border:none; border-radius:4px; padding:2px 8px;")
            b.clicked.connect(lambda _=False, u=c["url"]: webbrowser.open(u, new=2))
            top.addWidget(b)
        v.addLayout(top)
        why = c.get("why") or []
        meta = QLabel(f"{c.get('source','')}"
                      + (f"  ↳ {', '.join(why)}" if why else ""))
        meta.setStyleSheet("color:#9CA3AF; font-size:10px;")
        v.addWidget(meta)
        return f


def main() -> int:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)   # live in the background; hotkey re-shows
    w = ConnectWidget()
    w._place_near_cursor()

    # global hotkey on a background thread → marshal to the Qt thread via signal
    def hk():
        try:
            import keyboard
            keyboard.add_hotkey(HOTKEY, lambda: w.bridge.hotkey.emit())
            keyboard.wait()       # keep the listener alive
        except Exception:
            pass
    threading.Thread(target=hk, daemon=True).start()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
