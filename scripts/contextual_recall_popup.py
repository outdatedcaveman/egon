from __future__ import annotations

import ctypes
import html
import json
import sys
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QVBoxLayout, QWidget

ROOT = Path(__file__).resolve().parents[1]
API = "http://127.0.0.1:8000/api/v1/mind/context/v2"


def _foreground_title() -> str:
    if sys.platform != "win32":
        return ""
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value.strip()
    except Exception:
        return ""


def _clipboard_text(app: QApplication) -> str:
    try:
        return app.clipboard().text()[:1200]
    except Exception:
        return ""


def _mind_recall(query: str) -> dict:
    url = f"{API}?{urlencode({'project': 'egon', 'query': query, 'budget_chars': 3500})}"
    with urlopen(url, timeout=8) as r:
        return json.loads(r.read().decode("utf-8"))


def _pick_item(data: dict) -> tuple[str, str, str]:
    sections = data.get("sections") if isinstance(data.get("sections"), dict) else {}
    memories = sections.get("durable_memory") if isinstance(sections.get("durable_memory"), list) else []
    activities = sections.get("recent_activity") if isinstance(sections.get("recent_activity"), list) else []
    item = memories[0] if memories else (activities[0] if activities else {})
    title = str(item.get("kind") or item.get("title") or "Relevant context").replace("_", " ").title()
    body = str(item.get("content") or item.get("summary") or data.get("briefing") or "No recall item returned.")
    source = f"memory {item.get('id')}" if item.get("id") else "mind context"
    return title, body, source


def main() -> int:
    if any(arg in {"-h", "--help", "/?"} for arg in sys.argv[1:]):
        print("Usage: contextual_recall_popup.py")
        print("Opens a small always-on-top Egon Context Recall popup.")
        return 0

    app = QApplication(sys.argv)
    title = _foreground_title()
    clip = _clipboard_text(app)
    query = (
        "Surface one useful, non-obvious saved data point for Bruno's current context. "
        f"Active window: {title}. Clipboard excerpt: {clip}"
    )
    try:
        data = _mind_recall(query)
        recall_title, body, source = _pick_item(data)
    except Exception as e:
        recall_title = "Egon Mind Unavailable"
        body = f"Could not retrieve contextual recall: {type(e).__name__}: {e}"
        source = "local launcher"

    win = QWidget()
    win.setWindowTitle("Egon Context Recall")
    win.setWindowFlags(win.windowFlags() | Qt.WindowStaysOnTopHint)
    win.resize(620, 360)
    layout = QVBoxLayout(win)
    layout.setContentsMargins(18, 16, 18, 16)
    layout.setSpacing(12)

    h = QLabel(f"<b>{html.escape(recall_title)}</b>")
    h.setWordWrap(True)
    layout.addWidget(h)

    b = QLabel(html.escape(body[:1800]).replace("\n", "<br/>"))
    b.setWordWrap(True)
    b.setTextFormat(Qt.RichText)
    layout.addWidget(b, stretch=1)

    meta = QLabel(f"Source: {html.escape(source)}")
    meta.setStyleSheet("color: #777;")
    layout.addWidget(meta)

    close = QPushButton("Close")
    close.clicked.connect(win.close)
    layout.addWidget(close)

    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
