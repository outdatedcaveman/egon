"""Egon Connect v2 — "ask about this screen" overlay (Circle-to-Search style).

Bruno 2026-06-10 v2, after v1 feedback:
  • VISUAL: the hotkey freezes the screen into a dimmed fullscreen overlay of a
    live screenshot. Drag to select any region — it lights up with a gold
    marquee (like Google's "ask about this screen" / Circle to Search). Enter
    or double-click = whole screen. Esc always cancels.
  • CAPABLE: the selected region is OCR'd with Windows' built-in OCR engine
    (winocr — works on ANY pixels: PDFs, images, video frames, apps that
    expose no text API). The recognized text drops into an editable box, runs
    through the semantic Connection Engine (/api/v1/mind/connect), and you can
    refine the query and re-ask right there.
  • FIXED from v1: hotkey is now Ctrl+Alt+E (v1's Ctrl+Alt+Space collided with
    Claude); configurable via egon-config.json {"connect_widget":{"hotkey":…}}.
    The v1 hang ("capturing…" forever + dead ✕) was the worker thread touching
    the Qt clipboard — clipboard isn't thread-safe and it froze the event
    loop. v2 does screenshots on the Qt thread, OCR + HTTP strictly in worker
    threads, results marshaled back via Signals. The UI can never freeze.

Run:  .venv\\Scripts\\pythonw.exe scripts\\connect_widget.py
      (or double-click "Launch Connect Widget.vbs")
"""
from __future__ import annotations

import io
import json
import sys
import threading
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    import lib.no_console  # noqa: F401
except Exception:
    pass

from PySide6.QtCore import Qt, QObject, Signal, QRect, QPoint, QBuffer, QTimer
from PySide6.QtGui import QGuiApplication, QPainter, QColor, QPen, QPixmap, QCursor, QIcon
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QPlainTextEdit, QScrollArea, QFrame,
)

CONNECT_URL = "http://127.0.0.1:8000/api/v1/mind/connect"
SYNTH_URL = "http://127.0.0.1:8000/api/v1/mind/synthesize"
DEFAULT_HOTKEY = "ctrl+alt+e"

_SRC_ICON = {
    "instapaper": "📰", "zotero": "📚", "paperpile": "📄", "kindle": "📖",
    "letterboxd": "🎬", "youtube_music": "🎵", "pocketcasts": "🎧",
    "chrome_bookmarks": "🔖", "chrome_tabs": "🗂️", "notion_workspace": "🟦",
    "tvtime": "📺", "mind-memory": "🧠",
}


def _hotkey() -> str:
    try:
        cfg = json.loads((ROOT / "egon-config.json").read_text(encoding="utf-8"))
        return (cfg.get("connect_widget") or {}).get("hotkey") or DEFAULT_HOTKEY
    except Exception:
        return DEFAULT_HOTKEY


# ── worker-side helpers (NEVER touch Qt UI/clipboard from these) ─────────────
def _ocr_png(png_bytes: bytes) -> str:
    """Windows built-in OCR over raw PNG bytes. Worker-thread safe."""
    try:
        from PIL import Image
        import winocr
        img = Image.open(io.BytesIO(png_bytes))
        # Try English then Portuguese; keep whichever reads more text.
        best = ""
        for lang in ("en", "pt"):
            try:
                r = winocr.recognize_pil_sync(img, lang)
                t = (r.get("text") if isinstance(r, dict) else getattr(r, "text", "")) or ""
                if len(t) > len(best):
                    best = t
            except Exception:
                continue
        return best.strip()
    except Exception:
        return ""


def _connect_api(text: str) -> dict:
    try:
        body = json.dumps({"text": text[:6000], "limit": 16}).encode()
        req = urllib.request.Request(CONNECT_URL, data=body,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"status": "error", "error": f"mind not reachable ({e})"}


def _synthesize_api(text: str) -> dict:
    """Retrieval + local-LLM insight (qwen2.5:3b — small model, can take ~30s
    on first call while it loads into RAM). Explicit user action only."""
    try:
        body = json.dumps({"text": text[:6000], "limit": 14}).encode()
        req = urllib.request.Request(SYNTH_URL, data=body,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"status": "error", "error": f"synthesis failed ({e})"}


# ── overlay ──────────────────────────────────────────────────────────────────
class Bridge(QObject):
    hotkey = Signal()
    ocr_done = Signal(str)        # recognized text
    connect_done = Signal(dict)   # engine result


class Overlay(QWidget):
    """Fullscreen frozen-screen overlay with rubber-band region select."""

    def __init__(self, bridge: Bridge):
        super().__init__()
        self.bridge = bridge
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setCursor(Qt.CrossCursor)
        self._shot: QPixmap | None = None
        self._dpr = 1.0
        self._origin: QPoint | None = None
        self._rect = QRect()
        self._panel = ResultPanel(self)
        self._panel.hide()
        self.bridge.ocr_done.connect(self._on_ocr)
        self.bridge.connect_done.connect(self._panel.render_result)

    # — lifecycle —
    def open_capture(self):
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        geo = screen.geometry()
        self._shot = screen.grabWindow(0)          # Qt thread: safe
        self._dpr = self._shot.devicePixelRatio() or 1.0
        self._origin, self._rect = None, QRect()
        self._panel.hide()
        self.setGeometry(geo)
        self.showFullScreen()
        self.raise_(); self.activateWindow()

    def close_overlay(self):
        self._panel.hide()
        self.hide()

    # — painting: dimmed screenshot + bright selection w/ gold marquee —
    def paintEvent(self, _):
        if self._shot is None:
            return
        p = QPainter(self)
        p.drawPixmap(self.rect(), self._shot)
        p.fillRect(self.rect(), QColor(8, 20, 26, 150))      # dim everything
        if not self._rect.isNull():
            src = QRect(int(self._rect.x() * self._dpr), int(self._rect.y() * self._dpr),
                        int(self._rect.width() * self._dpr), int(self._rect.height() * self._dpr))
            p.drawPixmap(self._rect, self._shot, src)         # undimmed region
            pen = QPen(QColor("#D4A24C")); pen.setWidth(2)
            p.setPen(pen); p.drawRect(self._rect)
        else:
            p.setPen(QColor("#F0E9D5"))
            f = p.font(); f.setPointSize(13); p.setFont(f)
            p.drawText(self.rect().adjusted(0, 40, 0, 0),
                       Qt.AlignHCenter | Qt.AlignTop,
                       "✨ Drag to select what to ask about · Enter = whole screen · Esc = cancel")

    # — input —
    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.close_overlay()
        elif e.key() in (Qt.Key_Return, Qt.Key_Enter):
            self._rect = self.rect().adjusted(0, 0, -1, -1)
            self.update(); self._finish_selection()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and not self._panel.isVisible():
            self._origin = e.position().toPoint()
            self._rect = QRect()
            self.update()

    def mouseMoveEvent(self, e):
        if self._origin is not None:
            self._rect = QRect(self._origin, e.position().toPoint()).normalized()
            self.update()

    def mouseDoubleClickEvent(self, e):
        self._rect = self.rect().adjusted(0, 0, -1, -1)
        self.update(); self._finish_selection()

    def mouseReleaseEvent(self, e):
        if self._origin is None:
            return
        self._origin = None
        if self._rect.width() < 12 or self._rect.height() < 12:
            self._rect = QRect(); self.update(); return
        self._finish_selection()

    # — capture → OCR → connect —
    def _finish_selection(self):
        if self._shot is None or self._rect.isNull():
            return
        src = QRect(int(self._rect.x() * self._dpr), int(self._rect.y() * self._dpr),
                    int(self._rect.width() * self._dpr), int(self._rect.height() * self._dpr))
        crop = self._shot.copy(src)
        buf = QBuffer(); buf.open(QBuffer.WriteOnly)
        crop.save(buf, "PNG")
        png = bytes(buf.data())
        buf.close()
        self._panel.show_loading(self._panel_anchor())
        threading.Thread(target=self._worker, args=(png,), daemon=True,
                         name="connect-ocr").start()

    def _panel_anchor(self) -> QPoint:
        # place the panel beside the selection, clamped on-screen
        pw, ph = self._panel.width(), self._panel.height()
        r, full = self._rect, self.rect()
        x = r.right() + 14
        if x + pw > full.right() - 8:
            x = max(8, r.left() - pw - 14)
        y = min(max(8, r.top()), full.bottom() - ph - 8)
        return QPoint(x, y)

    def _worker(self, png: bytes):
        text = _ocr_png(png)
        self.bridge.ocr_done.emit(text)
        if text:
            self.bridge.connect_done.emit(_connect_api(text))

    def _on_ocr(self, text: str):
        if not text:
            self._panel.render_result({"status": "error",
                "error": "Couldn't read any text in that region — try a tighter selection."})
            return
        self._panel.set_text(text)


class ResultPanel(QFrame):
    """Floating result card inside the overlay."""

    def __init__(self, overlay: Overlay):
        super().__init__(overlay)
        self.overlay = overlay
        self.setFixedSize(380, 470)
        self.setStyleSheet("QFrame{background:#0B1F28; border:1px solid #1F4858; border-radius:10px;}")
        v = QVBoxLayout(self); v.setContentsMargins(12, 8, 12, 12); v.setSpacing(8)

        top = QHBoxLayout()
        t = QLabel("✨ Egon Connect"); t.setStyleSheet("color:#D4A24C; font-weight:700; border:none;")
        top.addWidget(t)
        self._status = QLabel(""); self._status.setStyleSheet("color:#9CA3AF; font-size:11px; border:none;")
        top.addWidget(self._status); top.addStretch(1)
        x = QPushButton("✕"); x.setFixedSize(22, 22)
        x.setStyleSheet("background:#16404F; color:#F0E9D5; border:none; border-radius:11px;")
        x.clicked.connect(self.overlay.close_overlay)
        top.addWidget(x)
        v.addLayout(top)

        self._text = QPlainTextEdit()
        self._text.setStyleSheet("QPlainTextEdit{background:#102F3C; color:#F0E9D5;"
                                 "border:1px solid #1F4858; border-radius:6px; padding:6px; font-size:12px;}")
        self._text.setFixedHeight(84)
        self._text.setPlaceholderText("recognized text appears here — edit it and re-ask")
        v.addWidget(self._text)

        btnrow = QHBoxLayout(); btnrow.setSpacing(6)
        ask = QPushButton("Ask again")
        ask.setStyleSheet("QPushButton{background:#D4A24C; color:#0E2630; border:none;"
                          "border-radius:6px; padding:6px; font-weight:700;}")
        ask.clicked.connect(self._re_ask)
        btnrow.addWidget(ask, 1)
        syn = QPushButton("🧠 Synthesize")
        syn.setToolTip("Local LLM (qwen2.5:3b) reads the matches and tells you "
                       "what connects, what contradicts, what to open first. "
                       "Runs on your machine; first call loads the model (~30s).")
        syn.setStyleSheet("QPushButton{background:#7BC5C7; color:#0E2630; border:none;"
                          "border-radius:6px; padding:6px; font-weight:700;}")
        syn.clicked.connect(self._synthesize)
        btnrow.addWidget(syn, 1)
        v.addLayout(btnrow)

        self._insight = QLabel("")
        self._insight.setWordWrap(True)
        self._insight.setVisible(False)
        self._insight.setStyleSheet(
            "color:#F0E9D5; background:#143038; border:1px solid #7BC5C7;"
            "border-radius:6px; padding:8px; font-size:12px;")
        v.addWidget(self._insight)

        self._scroll = QScrollArea(); self._scroll.setWidgetResizable(True)
        self._scroll.setStyleSheet("QScrollArea{border:1px solid #1F4858; border-radius:6px; background:transparent;}")
        self._host = QWidget(); self._host.setStyleSheet("background:transparent;")
        self._list = QVBoxLayout(self._host)
        self._list.setContentsMargins(6, 6, 6, 6); self._list.setSpacing(6)
        self._list.addStretch(1)
        self._scroll.setWidget(self._host)
        v.addWidget(self._scroll, 1)

    # — public —
    def show_loading(self, at: QPoint):
        self._clear(); self._text.setPlainText("")
        self._status.setText("reading region…")
        self.move(at); self.show(); self.raise_()

    def set_text(self, text: str):
        self._text.setPlainText(text[:1800])
        self._status.setText("connecting…")

    def render_result(self, res: dict):
        self._clear()
        if res.get("status") != "ok":
            self._status.setText("")
            err = QLabel(res.get("error", "no result")); err.setWordWrap(True)
            err.setStyleSheet("color:#D67A6A; border:none;")
            self._list.insertWidget(0, err)
            return
        conns = res.get("connections", [])
        self._status.setText(f"{res.get('mode','')} · {len(conns)} found")
        if not conns:
            self._list.insertWidget(0, QLabel("No connections found."))
            return
        for i, c in enumerate(conns):
            self._list.insertWidget(i, self._row(c))

    # — internals —
    def _synthesize(self):
        text = self._text.toPlainText().strip()
        if len(text) < 3:
            return
        self._insight.setVisible(True)
        self._insight.setText("🧠 thinking… (first call loads the model, ~30s)")
        self._status.setText("synthesizing…")

        def work():
            res = _synthesize_api(text)
            QTimer.singleShot(0, lambda: self._render_synthesis(res))
        threading.Thread(target=work, daemon=True, name="connect-synth").start()

    def _render_synthesis(self, res: dict):
        syn = (res or {}).get("synthesis") or {}
        if res.get("status") == "ok" and syn.get("status") == "ok":
            self._insight.setText("🧠 " + syn.get("insight", ""))
            self._status.setText(f"{res.get('mode','')} · synthesized ({syn.get('model','')})")
            # refresh the list too — synthesize re-ran retrieval
            self.render_result_keep_insight(res)
        else:
            err = syn.get("error") or res.get("error") or "synthesis unavailable"
            self._insight.setText("🧠 " + str(err))
            self._status.setText("")

    def render_result_keep_insight(self, res: dict):
        keep = self._insight.text()
        visible = self._insight.isVisible()
        self.render_result(res)
        self._insight.setText(keep)
        self._insight.setVisible(visible)

    def _re_ask(self):
        text = self._text.toPlainText().strip()
        if len(text) < 3:
            return
        self._status.setText("connecting…")
        threading.Thread(target=lambda: self.overlay.bridge.connect_done.emit(_connect_api(text)),
                         daemon=True).start()

    def _clear(self):
        while self._list.count() > 1:
            it = self._list.takeAt(0)
            if it.widget():
                it.widget().deleteLater()

    def _row(self, c: dict) -> QFrame:
        f = QFrame()
        f.setStyleSheet("QFrame{background:#0E2630; border:1px solid #1F4858; border-radius:6px;}")
        v = QVBoxLayout(f); v.setContentsMargins(8, 5, 8, 5); v.setSpacing(1)
        ic = _SRC_ICON.get(c.get("source", ""), "•")
        top = QHBoxLayout()
        title = QLabel(f"{ic} {c.get('title','')[:78]}")
        title.setStyleSheet("color:#F0E9D5; font-weight:600; font-size:12px; border:none;")
        title.setWordWrap(True)
        top.addWidget(title, 1)
        if c.get("url"):
            b = QPushButton("open"); b.setFixedHeight(20)
            b.setStyleSheet("background:#7BC5C7; color:#0E2630; border:none; border-radius:4px;"
                            "padding:1px 8px; font-size:11px;")
            b.clicked.connect(lambda _=False, u=c["url"]: (webbrowser.open(u, new=2),
                                                           self.overlay.close_overlay()))
            top.addWidget(b)
        v.addLayout(top)
        why = c.get("why") or []
        meta = QLabel(c.get("source", "") + (f"  ↳ {', '.join(why[:4])}" if why else ""))
        meta.setStyleSheet("color:#9CA3AF; font-size:10px; border:none;")
        v.addWidget(meta)
        return f


def _setup_tray(app, overlay) -> object | None:
    """Ambient delivery surface (strategy #3 proactivity): tray icon that
    toasts when the core publishes a new daily digest, with a menu to open the
    digest, trigger a capture, or quit. The widget is always-on, so this works
    with both Chrome and the Egon app closed."""
    from PySide6.QtWidgets import QSystemTrayIcon, QMenu
    from PySide6.QtGui import QAction
    import os as _os
    if not QSystemTrayIcon.isSystemTrayAvailable():
        return None
    tray = QSystemTrayIcon(app)
    # Distinct icon, NOT shell/egon.ico: this widget sat in the tray wearing
    # the same icon as the Egon app, which read as a duplicate-instance bug
    # (Bruno 2026-06-11). Gold disc + dark spark = the Connect surface.
    from PySide6.QtGui import QPixmap, QPainter, QColor, QFont
    pm = QPixmap(64, 64)
    pm.fill(QColor(0, 0, 0, 0))
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#D4A24C"))
    p.setPen(QColor("#0B1F28"))
    p.drawEllipse(2, 2, 60, 60)
    f = QFont(); f.setPixelSize(40); f.setBold(True)
    p.setFont(f)
    p.drawText(pm.rect(), 0x84, "✦")   # AlignCenter, four-pointed star
    p.end()
    tray.setIcon(QIcon(pm))
    tray.setToolTip("Egon Connect — Ctrl+Alt+E to ask about the screen")

    digest_md = ROOT / "state" / "daily_digest.md"
    digest_json = ROOT / "state" / "daily_digest.json"

    def open_digest():
        if digest_md.exists():
            try:
                _os.startfile(str(digest_md))
            except Exception:
                pass

    menu = QMenu()
    a1 = QAction("📰 Open daily digest"); a1.triggered.connect(open_digest)
    a2 = QAction("✨ Capture screen (Ctrl+Alt+E)")
    a2.triggered.connect(overlay.open_capture)
    a3 = QAction("Quit Egon Connect"); a3.triggered.connect(app.quit)
    for a in (a1, a2, a3):
        menu.addAction(a)
    tray.setContextMenu(menu)
    tray._egon_actions = (a1, a2, a3)          # keep refs alive
    tray.activated.connect(
        lambda r: open_digest() if r == QSystemTrayIcon.ActivationReason.Trigger else None)
    tray.show()

    # toast once per NEW digest
    state = {"last": None}
    try:
        state["last"] = json.loads(digest_json.read_text(encoding="utf-8")).get("date")
    except Exception:
        pass

    def poll():
        try:
            d = json.loads(digest_json.read_text(encoding="utf-8"))
        except Exception:
            return
        if d.get("date") and d["date"] != state["last"]:
            state["last"] = d["date"]
            n = len(d.get("proposals") or [])
            w = len(d.get("agent_work_24h") or [])
            try:
                tray.showMessage("📰 Egon — your daily digest is ready",
                                 f"{n} insight(s) · {w} agent update(s). "
                                 "Click the tray icon to read.",
                                 QSystemTrayIcon.MessageIcon.Information, 15000)
            except Exception:
                pass

    timer = QTimer(tray)
    timer.setInterval(5 * 60 * 1000)
    timer.timeout.connect(poll)
    timer.start()
    QTimer.singleShot(10_000, poll)
    return tray


def main() -> int:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    ico = ROOT / "shell" / "egon.ico"
    if ico.exists():
        app.setWindowIcon(QIcon(str(ico)))

    bridge = Bridge()
    overlay = Overlay(bridge)
    bridge.hotkey.connect(overlay.open_capture)
    app._egon_tray = _setup_tray(app, overlay)   # keep ref alive

    hotkey = _hotkey()

    def hk_listener():
        try:
            import keyboard
            keyboard.add_hotkey(hotkey, lambda: bridge.hotkey.emit(),
                                suppress=False, trigger_on_release=True)
            keyboard.wait()
        except Exception:
            pass
    threading.Thread(target=hk_listener, daemon=True, name="connect-hotkey").start()

    # tiny first-run toast so Bruno knows it's armed
    QTimer.singleShot(800, lambda: None)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
