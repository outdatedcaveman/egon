"""Mind page — unified-mind dashboard & Categorical Mind (CatColab).

Shows the shared activity feed across every connected agent, the top-agents rollup,
top-projects rollup, memory search, and the Applied Category Theory (ACT) modeling tab.
Auto-refreshes every 5 s while the page is visible.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

import httpx
from PySide6.QtCore import Qt, QTimer, QThread, Signal, QObject
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QFrame, QScrollArea, QPushButton, QLineEdit, QSizePolicy,
    QMessageBox, QTabWidget, QListWidget, QSplitter,
)

_API = "http://127.0.0.1:8000/api/v1/mind"

# Shared palette
_BG_CARD = "#0E2630"
_BORDER  = "#1F4858"
_ACCENT  = "#7BC5C7"
_TEXT    = "#F0E9D5"
_MUTED   = "#9CA3AF"
_GOLD    = "#D4A24C"
_OK      = "#7FB069"
_WARN    = "#D4A24C"
_ERR     = "#D67A6A"

# Agent → colour mapping
_AGENT_COLOR = {
    "claude-code": "#D77A56",
    "codex":       "#7BC5C7",
    "antigravity": "#9D7BC5",
    "chatgpt":     "#7FB069",
    "gemini":      "#D4A24C",
}

_TAB_QSS = """
QTabWidget::pane { border: 1px solid #1F4858; background: #0E2630; }
QTabBar::tab { background: #16404F; color: #9CA3AF; padding: 8px 16px;
    border: 1px solid #1F4858; border-bottom: none;
    border-top-left-radius: 6px; border-top-right-radius: 6px;
    margin-right: 2px; }
QTabBar::tab:selected { background: #0E2630; color: #F0E9D5; font-weight: 600; }
"""


def _fmt_age(ts: int | None) -> str:
    if not ts:
        return "—"
    delta = int(datetime.now().timestamp()) - int(ts)
    if delta < 60:    return f"{delta}s ago"
    if delta < 3600:  return f"{delta // 60}m ago"
    if delta < 86400: return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def _safe_json(d: Any) -> str:
    try:
        return json.dumps(d, ensure_ascii=False)
    except Exception:
        return str(d)


def _signed(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return ""
    if value == 0:
        return " (+0)"
    sign = "+" if value > 0 else ""
    return f" ({sign}{value})"


def _lock_pill(lease: dict) -> QFrame:
    path = lease.get("path") or ""
    fname = path.split("/")[-1]
    agent = lease.get("agent_name") or "unknown"
    expires_in = lease.get("expires_in", 0)
    color = _AGENT_COLOR.get(agent, _MUTED)
    
    pill = QFrame()
    pill.setStyleSheet(
        f"background-color: #16404F; border: 1px solid {_BORDER}; "
        f"border-radius: 12px; padding: 4px 10px;"
    )
    h = QHBoxLayout(pill)
    h.setContentsMargins(4, 2, 4, 2)
    h.setSpacing(6)
    
    icon = QLabel("🔒")
    icon.setStyleSheet("font-size: 11px;")
    h.addWidget(icon)
    
    text = QLabel(f"<b>{fname}</b> held by <span style='color:{color};'>{agent}</span> ({expires_in}s)")
    text.setTextFormat(Qt.RichText)
    text.setStyleSheet(f"color: {_TEXT}; font-size: 11px;")
    text.setToolTip(path)
    h.addWidget(text)
    
    return pill


def _api_get(path: str, params: dict | None = None,
             timeout: float = 1.5) -> dict | None:
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.get(f"{_API}{path}", params=params or {})
        if r.status_code == 200:
            res = r.json()
            if isinstance(res, dict):
                return res
    except Exception:
        return None
    return None


# ── background thread HTTP workers ─────────────────────────────────────────

class _HttpWorker(QObject):
    finished = Signal(dict)

    def __init__(self, method: str, url: str, timeout: float = 8.0, json_body: dict | None = None):
        super().__init__()
        self._method = method
        self._url = url
        self._timeout = timeout
        self._json_body = json_body

    def run(self) -> None:
        try:
            with httpx.Client(timeout=self._timeout) as client:
                if self._method.upper() == "GET":
                    r = client.get(self._url)
                else:
                    if self._json_body:
                        r = client.post(self._url, json=self._json_body)
                    else:
                        r = client.post(self._url)
                if r.status_code < 400:
                    try:
                        body = r.json()
                    except Exception:
                        body = r.text
                    self.finished.emit({"ok": True, "data": body, "error": ""})
                else:
                    self.finished.emit({"ok": False, "data": None,
                                        "error": f"HTTP {r.status_code}: {r.text[:300]}"})
        except Exception as exc:
            self.finished.emit({"ok": False, "data": None, "error": str(exc)[:300]})


def _spawn_http(parent: QWidget, method: str, url: str,
                callback, timeout: float = 8.0, json_body: dict | None = None) -> QThread:
    thread = QThread(parent)
    worker = _HttpWorker(method, url, timeout, json_body)
    worker.moveToThread(thread)
    thread._worker = worker
    thread.started.connect(worker.run)
    worker.finished.connect(callback)
    worker.finished.connect(thread.quit)
    worker.finished.connect(worker.deleteLater)
    thread.finished.connect(thread.deleteLater)
    thread.start()
    return thread


# ── small visual primitives ────────────────────────────────────────────────

def _stat_card(label: str, value: str, hint: str = "",
               accent: str = _ACCENT) -> QFrame:
    card = QFrame()
    card.setObjectName("statCard")
    card.setMinimumHeight(96)
    v = QVBoxLayout(card)
    v.setContentsMargins(18, 14, 18, 14)
    v.setSpacing(2)
    lbl = QLabel(label.upper()); lbl.setObjectName("statCardLabel")
    v.addWidget(lbl)
    val = QLabel(value)
    val.setObjectName("statCardVal")
    val.setStyleSheet(f"color: {accent};")
    v.addWidget(val)
    if hint:
        h = QLabel(hint); h.setObjectName("statCardHint")
        h.setWordWrap(True)
        v.addWidget(h)
    v.addStretch(1)
    return card


def _bar_row(label: str, value: int, max_value: int,
             accent: str = _ACCENT) -> QFrame:
    row = QFrame()
    row.setMinimumHeight(28)
    h = QHBoxLayout(row); h.setContentsMargins(2, 2, 2, 2); h.setSpacing(8)
    nl = QLabel(label); nl.setMinimumWidth(120)
    nl.setStyleSheet(f"color: {_TEXT};")
    h.addWidget(nl)
    pct = int(100 * value / max_value) if max_value > 0 else 0
    bar = QFrame()
    bar.setFixedHeight(8)
    bar.setStyleSheet(f"background-color: {_BORDER}; border-radius: 4px;")
    inner_h = QHBoxLayout(bar); inner_h.setContentsMargins(0, 0, 0, 0)
    inner = QFrame()
    inner.setFixedHeight(8)
    inner.setStyleSheet(f"background-color: {accent}; border-radius: 4px;")
    inner.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    inner_h.addWidget(inner, stretch=max(pct, 1))
    inner_h.addStretch(max(100 - pct, 1))
    h.addWidget(bar, stretch=1)
    vl = QLabel(str(value)); vl.setMinimumWidth(40)
    vl.setAlignment(Qt.AlignmentFlag.AlignRight)
    vl.setStyleSheet(f"color: {_MUTED};")
    h.addWidget(vl)
    return row


def _activity_row(item: dict) -> QFrame:
    agent = item.get("agent_name") or "?"
    project = item.get("project_slug") or "—"
    kind = item.get("kind") or "?"
    payload = item.get("payload") or {}
    ts = item.get("ts")
    color = _AGENT_COLOR.get(agent, _MUTED)

    row = QFrame()
    row.setObjectName("activityRow")
    row.setMinimumHeight(36)
    h = QHBoxLayout(row); h.setContentsMargins(10, 6, 10, 6); h.setSpacing(10)

    pill = QLabel(agent)
    pill.setStyleSheet(
        f"background-color: {color}; color: #0E2630; "
        f"padding: 2px 8px; border-radius: 8px; font-weight: 600;")
    pill.setMinimumWidth(90); pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
    h.addWidget(pill)

    proj = QLabel(project)
    proj.setStyleSheet(f"color: {_GOLD};")
    proj.setMinimumWidth(80)
    h.addWidget(proj)

    k = QLabel(kind)
    k.setStyleSheet(f"color: {_ACCENT};")
    k.setMinimumWidth(100)
    h.addWidget(k)

    body = QLabel(_safe_json(payload)[:140])
    body.setStyleSheet(f"color: {_TEXT};")
    body.setWordWrap(False)
    body.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
    h.addWidget(body, stretch=1)

    age = QLabel(_fmt_age(ts))
    age.setStyleSheet(f"color: {_MUTED};")
    age.setAlignment(Qt.AlignmentFlag.AlignRight)
    age.setMinimumWidth(60)
    h.addWidget(age)

    return row


# ── Tab 1: Activity & Stats ────────────────────────────────────────────────

class _ActivityStatsTab(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._threads: list[QThread] = []
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(14)

        # Stat strip
        self._stat_strip = QHBoxLayout(); self._stat_strip.setSpacing(12)
        root.addLayout(self._stat_strip)

        self._scorecard = QFrame()
        self._scorecard.setStyleSheet(f"background-color: {_BG_CARD}; border: 1px solid {_BORDER}; border-radius: 6px;")
        score_layout = QVBoxLayout(self._scorecard)
        score_layout.setContentsMargins(14, 12, 14, 12)
        score_layout.setSpacing(8)
        score_title_row = QHBoxLayout(); score_title_row.setSpacing(10)
        score_title = QLabel("META-HARNESS SCORECARD")
        score_title.setStyleSheet(f"color: {_MUTED}; font-size: 11px; font-weight: 600;")
        score_title_row.addWidget(score_title)
        self._activation_btn = QPushButton("Run activation")
        self._activation_btn.setToolTip("Run the end-to-end harness activation test.")
        self._activation_btn.clicked.connect(self._run_activation_test)
        score_title_row.addWidget(self._activation_btn)
        self._score_grade = QLabel("waiting for scorecard")
        self._score_grade.setAlignment(Qt.AlignmentFlag.AlignRight)
        self._score_grade.setStyleSheet(f"color: {_MUTED}; font-size: 11px;")
        score_title_row.addWidget(self._score_grade, stretch=1)
        score_layout.addLayout(score_title_row)
        self._score_grid = QGridLayout()
        self._score_grid.setContentsMargins(0, 0, 0, 0)
        self._score_grid.setHorizontalSpacing(12)
        self._score_grid.setVerticalSpacing(6)
        score_layout.addLayout(self._score_grid)
        self._score_reco = QLabel("")
        self._score_reco.setWordWrap(True)
        self._score_reco.setStyleSheet(f"color: {_MUTED}; font-size: 11px;")
        score_layout.addWidget(self._score_reco)
        self._activation_summary = QLabel("Activation history not loaded yet.")
        self._activation_summary.setWordWrap(True)
        self._activation_summary.setStyleSheet(f"color: {_MUTED}; font-size: 11px;")
        score_layout.addWidget(self._activation_summary)
        root.addWidget(self._scorecard)

        # Two-column section: top agents (left) + top projects (right)
        cols = QHBoxLayout(); cols.setSpacing(12)
        self._agents_card = self._make_chart_card("Top agents (24h)")
        self._projects_card = self._make_chart_card("Top projects (24h)")
        cols.addWidget(self._agents_card[0], stretch=1)
        cols.addWidget(self._projects_card[0], stretch=1)
        root.addLayout(cols)

        # Locks Card
        self._locks_card = QFrame()
        self._locks_card.setStyleSheet(f"background-color: {_BG_CARD}; border: 1px solid {_BORDER}; border-radius: 6px;")
        locks_layout = QVBoxLayout(self._locks_card)
        locks_layout.setContentsMargins(14, 12, 14, 12)
        locks_layout.setSpacing(6)
        
        locks_title = QLabel("ACTIVE FILE LOCKS")
        locks_title.setStyleSheet(f"color: {_MUTED}; font-size: 11px; font-weight: 600;")
        locks_layout.addWidget(locks_title)
        
        self._locks_list = QWidget()
        self._locks_list_layout = QHBoxLayout(self._locks_list)
        self._locks_list_layout.setContentsMargins(0, 0, 0, 0)
        self._locks_list_layout.setSpacing(8)
        self._locks_list_layout.addStretch(1)
        locks_layout.addWidget(self._locks_list)
        
        root.addWidget(self._locks_card)

        # Memory search row
        mem_row = QHBoxLayout(); mem_row.setSpacing(8)
        mem_lbl = QLabel("Memory search:")
        mem_lbl.setStyleSheet(f"color: {_MUTED};")
        mem_row.addWidget(mem_lbl)
        self._mem_input = QLineEdit()
        self._mem_input.setPlaceholderText("type to filter memory by content / tags / kind …")
        self._mem_input.returnPressed.connect(self._run_memory_search)
        mem_row.addWidget(self._mem_input, stretch=1)
        search_btn = QPushButton("Search")
        search_btn.clicked.connect(self._run_memory_search)
        mem_row.addWidget(search_btn)
        root.addLayout(mem_row)

        self._mem_results = QLabel("(no search yet)")
        self._mem_results.setStyleSheet(f"color: {_MUTED};")
        self._mem_results.setWordWrap(True)
        root.addWidget(self._mem_results)

        # Activity feed (scrollable)
        feed_label = QLabel("Recent activity (newest first)")
        feed_label.setStyleSheet(f"color: {_TEXT}; font-weight: 600;")
        root.addWidget(feed_label)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMinimumHeight(240)
        scroll.setStyleSheet(f"background-color: {_BG_CARD}; border: 1px solid {_BORDER}; border-radius: 6px;")
        self._feed_host = QWidget()
        self._feed_layout = QVBoxLayout(self._feed_host)
        self._feed_layout.setContentsMargins(8, 8, 8, 8)
        self._feed_layout.setSpacing(4)
        self._feed_layout.addStretch(1)
        scroll.setWidget(self._feed_host)
        root.addWidget(scroll, stretch=1)

    def _make_chart_card(self, title: str) -> tuple[QFrame, QVBoxLayout]:
        card = QFrame()
        card.setStyleSheet(f"background-color: {_BG_CARD}; border: 1px solid {_BORDER}; border-radius: 6px;")
        v = QVBoxLayout(card); v.setContentsMargins(14, 12, 14, 12); v.setSpacing(6)
        t = QLabel(title); t.setStyleSheet(f"color: {_TEXT}; font-weight: 600;")
        v.addWidget(t)
        return card, v

    def refresh(self, stats: dict) -> None:
        # Stat strip — clear and re-fill
        while self._stat_strip.count():
            item = self._stat_strip.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()
        cards = [
            ("agents", stats.get("agents", 0), _ACCENT),
            ("projects", stats.get("projects", 0), _GOLD),
            ("sessions", stats.get("sessions", 0), _ACCENT),
            ("activity", stats.get("activity", 0), _ACCENT),
            ("memory", stats.get("memory", 0), _GOLD),
            ("files", stats.get("files", 0), _MUTED),
        ]
        for label, val, accent in cards:
            self._stat_strip.addWidget(_stat_card(label, str(val), accent=accent))
        self._stat_strip.addStretch(1)

        # Top agents / projects bars
        self._fill_chart(self._agents_card[1],
                         stats.get("top_agents_24h") or [],
                         key="agent")
        self._fill_chart(self._projects_card[1],
                         stats.get("top_projects_24h") or [],
                         key="project")

        scorecard = _api_get("/scorecard", {"project": "egon", "since_hours": 168}, timeout=8.0)
        self._refresh_scorecard(scorecard or {})
        activation = _api_get("/activation/history", {"project": "egon", "limit": 5}, timeout=8.0)
        self._refresh_activation_history(activation or {})

        # Active file locks
        leases_resp = _api_get("/files/leases")
        leases = (leases_resp or {}).get("leases") or []
        
        while self._locks_list_layout.count() > 1:
            it = self._locks_list_layout.takeAt(0)
            if it and it.widget():
                it.widget().deleteLater()
                
        if not leases:
            empty = QLabel("No active file locks held by any agent.")
            empty.setStyleSheet(f"color: {_MUTED}; font-style: italic; font-size: 11px;")
            self._locks_list_layout.insertWidget(0, empty)
        else:
            for lease in leases:
                self._locks_list_layout.insertWidget(self._locks_list_layout.count() - 1, _lock_pill(lease))

        # Activity feed
        feed = _api_get("/activity", {"limit": 60})
        items = (feed or {}).get("activity") or []
        while self._feed_layout.count():
            it = self._feed_layout.takeAt(0)
            if it and it.widget():
                it.widget().deleteLater()
        if not items:
            empty = QLabel("(no activity yet — open a Claude/Codex/Antigravity "
                           "session or wait for the mind ingestion poll to land)")
            empty.setStyleSheet(f"color: {_MUTED};")
            empty.setWordWrap(True)
            self._feed_layout.addWidget(empty)
        else:
            for it in items[:60]:
                self._feed_layout.addWidget(_activity_row(it))
        self._feed_layout.addStretch(1)

    def _fill_chart(self, layout: QVBoxLayout, rows: list[dict],
                    key: str) -> None:
        while layout.count() > 1:
            it = layout.takeAt(1)
            if it and it.widget():
                it.widget().deleteLater()
        
        valid_rows = []
        if isinstance(rows, list):
            valid_rows = [r for r in rows if isinstance(r, dict)]

        if not valid_rows:
            empty = QLabel("(no activity in the last 24h)")
            empty.setStyleSheet(f"color: {_MUTED};")
            layout.addWidget(empty)
            return

        try:
            max_v = max(int(r.get("activity_count") or 0) for r in valid_rows) or 1
        except (ValueError, TypeError):
            max_v = 1

        for r in valid_rows:
            label = str(r.get(key) or "—")
            try:
                count = int(r.get("activity_count") or 0)
            except (ValueError, TypeError):
                count = 0
            accent = _AGENT_COLOR.get(label, _ACCENT) if key == "agent" else _ACCENT
            layout.addWidget(_bar_row(label, count, max_v, accent=accent))

    def _refresh_scorecard(self, scorecard_data: dict) -> None:
        while self._score_grid.count():
            it = self._score_grid.takeAt(0)
            if it and it.widget():
                it.widget().deleteLater()
        if not isinstance(scorecard_data, dict) or scorecard_data.get("status") != "ok":
            self._score_grade.setText("scorecard unavailable")
            self._score_grade.setStyleSheet(f"color: {_ERR}; font-size: 11px;")
            self._score_reco.setText("The scorecard endpoint is offline or still loading.")
            return

        try:
            score = int(scorecard_data.get("score") or 0)
        except (ValueError, TypeError):
            score = 0
        grade = scorecard_data.get("grade") or "unknown"
        accent = _OK if score >= 75 else (_WARN if score >= 50 else _ERR)
        self._score_grade.setText(f"{score}/100 - {grade}")
        self._score_grade.setStyleSheet(f"color: {accent}; font-size: 12px; font-weight: 700;")
        metrics = scorecard_data.get("metrics") if isinstance(scorecard_data.get("metrics"), dict) else {}
        rows = [
            ("Compliance", f"{metrics.get('compliance_rate', 0)}%"),
            ("Context", f"{metrics.get('context_coverage', 0)}%"),
            ("V2 adoption", f"{metrics.get('v2_context_adoption', 0)}%"),
            ("Doc coverage", f"{metrics.get('durable_memory_coverage', 0)}%"),
            ("Token ROI", f"{metrics.get('estimated_token_roi', 0)}%"),
            ("Capsule avg", f"{metrics.get('avg_capsule_tokens', 0)} tok"),
        ]
        for idx, (label, value) in enumerate(rows):
            box = QFrame()
            box.setStyleSheet(f"background-color: #16404F; border: 1px solid {_BORDER}; border-radius: 6px;")
            v = QVBoxLayout(box)
            v.setContentsMargins(10, 7, 10, 7)
            v.setSpacing(1)
            k = QLabel(label.upper())
            k.setStyleSheet(f"color: {_MUTED}; font-size: 10px;")
            v.addWidget(k)
            val = QLabel(str(value))
            val.setStyleSheet(f"color: {_TEXT}; font-size: 15px; font-weight: 700;")
            v.addWidget(val)
            self._score_grid.addWidget(box, idx // 3, idx % 3)

        recs = scorecard_data.get("recommendations") or []
        if recs and isinstance(recs, list) and isinstance(recs[0], dict):
            first = recs[0]
            self._score_reco.setText(f"Next: {first.get('title')} - {first.get('why')}")
        else:
            self._score_reco.setText("No immediate scorecard recommendations.")

    def _refresh_activation_history(self, history_data: dict) -> None:
        if not isinstance(history_data, dict) or history_data.get("status") != "ok":
            self._activation_summary.setText("Activation history unavailable.")
            self._activation_summary.setStyleSheet(f"color: {_ERR}; font-size: 11px;")
            return
        latest = history_data.get("latest") if isinstance(history_data.get("latest"), dict) else {}
        if not latest:
            self._activation_summary.setText("No activation tests have been recorded yet.")
            self._activation_summary.setStyleSheet(f"color: {_MUTED}; font-size: 11px;")
            return
        delta = history_data.get("delta") if isinstance(history_data.get("delta"), dict) else {}
        metrics_delta = delta.get("metrics") if isinstance(delta.get("metrics"), dict) else {}
        score = latest.get("activation_score", "?")
        passed = latest.get("passed", 0)
        failed = latest.get("failed", 0)
        scorecard = latest.get("scorecard_score", "?")
        enforcement = latest.get("enforcement_score", "?")
        
        latest_metrics = latest.get("metrics") if isinstance(latest.get("metrics"), dict) else {}
        v2 = latest_metrics.get("v2_context_adoption", "?")
        lease = latest_metrics.get("file_lease_coverage", "?")
        roi = latest_metrics.get("estimated_token_roi", "?")
        
        score_delta = _signed(delta.get("scorecard_score"))
        enf_delta = _signed(delta.get("enforcement_score"))
        v2_delta = _signed(metrics_delta.get("v2_context_adoption"))
        lease_delta = _signed(metrics_delta.get("file_lease_coverage"))
        self._activation_summary.setStyleSheet(f"color: {_TEXT}; font-size: 11px;")
        self._activation_summary.setText(
            f"Activation {score}/100 ({passed} pass, {failed} fail). "
            f"Scorecard {scorecard}{score_delta}; enforcement {enforcement}{enf_delta}. "
            f"V2 {v2}%{v2_delta}; leases {lease}%{lease_delta}; ROI {roi}%."
        )

    def _run_activation_test(self) -> None:
        self._activation_btn.setEnabled(False)
        self._activation_btn.setText("Running...")
        self._activation_summary.setText("Running activation test...")
        url = f"{_API}/activation/test?project=egon&query=dashboard%20activation&run_mcp=true"
        t = _spawn_http(self, "GET", url, self._on_activation_done, timeout=80.0)
        self._threads.append(t)
        t.finished.connect(lambda: self._threads.remove(t) if t in self._threads else None)

    def _on_activation_done(self, result: dict) -> None:
        self._activation_btn.setEnabled(True)
        self._activation_btn.setText("Run activation")
        if not result or not result.get("ok"):
            self._activation_summary.setText(f"Activation failed: {result.get('error') if result else 'no response'}")
            self._activation_summary.setStyleSheet(f"color: {_ERR}; font-size: 11px;")
            return
        res_data = result.get("data") or {}
        failed = res_data.get("failed", 0)
        score = res_data.get("score", 0)
        if failed:
            self._activation_summary.setStyleSheet(f"color: {_ERR}; font-size: 11px;")
            self._activation_summary.setText(f"Activation {score}/100 with {failed} failed step(s).")
        else:
            self._activation_summary.setStyleSheet(f"color: {_OK}; font-size: 11px;")
            self._activation_summary.setText(f"Activation passed: {score}/100.")
        self.refresh()

    def _run_memory_search(self) -> None:
        q = (self._mem_input.text() or "").strip()
        params: dict = {"limit": 25}
        if q:
            params["q"] = q
        r = _api_get("/memory", params)
        items = (r or {}).get("memory") or []
        if not items:
            self._mem_results.setText("(no memory rows matched)")
            self._mem_results.setStyleSheet(f"color: {_MUTED};")
            return
        lines = []
        for m in items[:10]:
            tags = m.get("tags") or ""
            kind = m.get("kind") or "?"
            content = (m.get("content") or "")[:160]
            lines.append(f"  • [{kind}] {content}   tags={tags}")
        self._mem_results.setText("\n".join(lines))
        self._mem_results.setStyleSheet(f"color: {_TEXT};")


# ── Tab 2: Categorical Mind (CatColab) ─────────────────────────────────────

class _CategoricalMindTab(QWidget):
    """The Category-theoretic conceptual modeling (CatColab) dashboard."""
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._threads: list[QThread] = []  # prevent GC
        self._categories_data: list[dict] = []
        self._functors_data: list[str] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(12)

        # Control Bar
        ctrl_bar = QHBoxLayout()
        self._scan_btn = QPushButton("⚡ Scan & Map Analogies")
        self._scan_btn.setStyleSheet("background: #60A5A8; color: white; padding: 6px 12px; font-weight: 600;")
        self._scan_btn.clicked.connect(self._on_scan_clicked)
        ctrl_bar.addWidget(self._scan_btn)

        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet("color: #9CA3AF; font-size: 12px;")
        ctrl_bar.addWidget(self._status_lbl, 1)
        layout.addLayout(ctrl_bar)

        # Control Bar 2: Synthesis Bar
        synth_bar = QHBoxLayout()
        self._concept_input = QLineEdit()
        self._concept_input.setPlaceholderText("Describe a system/concept in natural language to model & compare (e.g. 'a database replication cluster')...")
        self._concept_input.setStyleSheet(
            "QLineEdit { background: #102F3C; color: #F0E9D5; border: 1px solid #1F4858; "
            "border-radius: 4px; padding: 6px; font-size: 12px; }"
        )
        self._concept_input.returnPressed.connect(self._on_synth_clicked)
        synth_bar.addWidget(self._concept_input, 1)

        self._synth_btn = QPushButton("⚡ Translate & Model")
        self._synth_btn.setStyleSheet(
            "QPushButton { background: #D4A24C; color: #0E2630; padding: 6px 12px; "
            "border-radius: 4px; font-weight: 600; font-size: 12px; }"
            "QPushButton:disabled { background: #5A4E39; color: #9CA3AF; }"
        )
        self._synth_btn.clicked.connect(self._on_synth_clicked)
        synth_bar.addWidget(self._synth_btn)
        layout.addLayout(synth_bar)

        # Main splitter (Categories vs Functors)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setStyleSheet("QSplitter::handle { background: #1F4858; height: 1px; width: 1px; }")
        layout.addWidget(splitter, 1)

        # Left Column: Categories List & Detail
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(10)

        cat_lbl = QLabel("Parsed Categories")
        cat_lbl.setStyleSheet("font-size: 13px; font-weight: 600; color: #7BC5C7;")
        left_layout.addWidget(cat_lbl)

        self._cat_list = QListWidget()
        self._cat_list.setStyleSheet(
            "QListWidget { background: #102F3C; color: #F0E9D5; border: 1px solid #1F4858; border-radius: 6px; padding: 6px; }"
            "QListWidget::item { padding: 4px 6px; border-radius: 4px; }"
            "QListWidget::item:selected { background: #1F5366; color: white; }"
            "QListWidget::item:hover { background: #16404F; }"
        )
        self._cat_list.itemSelectionChanged.connect(self._on_category_selected)
        left_layout.addWidget(self._cat_list, 2)

        self._cat_detail = QFrame()
        self._cat_detail.setStyleSheet(
            "QFrame { background: #0E2630; border: 1px solid #1F4858; border-radius: 6px; padding: 12px; }")
        cat_detail_layout = QVBoxLayout(self._cat_detail)
        cat_detail_layout.setContentsMargins(8, 8, 8, 8)
        
        self._cat_detail_title = QLabel("Select a category to view details")
        self._cat_detail_title.setStyleSheet("font-size: 12px; font-weight: 600; color: #7BC5C7;")
        self._cat_detail_title.setTextFormat(Qt.TextFormat.RichText)
        cat_detail_layout.addWidget(self._cat_detail_title)

        cat_scroll = QScrollArea()
        cat_scroll.setWidgetResizable(True)
        cat_scroll.setStyleSheet("border: none; background: transparent;")
        self._cat_detail_text = QLabel("")
        self._cat_detail_text.setStyleSheet("color: #F0E9D5; font-size: 11px;")
        self._cat_detail_text.setWordWrap(True)
        self._cat_detail_text.setTextFormat(Qt.TextFormat.RichText)
        cat_scroll.setWidget(self._cat_detail_text)
        cat_detail_layout.addWidget(cat_scroll, 1)
        
        left_layout.addWidget(self._cat_detail, 3)
        splitter.addWidget(left_widget)

        # Right Column: Functors List & Detail
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(10)

        func_lbl = QLabel("Discovered Analogies (Functors)")
        func_lbl.setStyleSheet("font-size: 13px; font-weight: 600; color: #D4A24C;")
        right_layout.addWidget(func_lbl)

        self._func_list = QListWidget()
        self._func_list.setStyleSheet(
            "QListWidget { background: #102F3C; color: #F0E9D5; border: 1px solid #1F4858; border-radius: 6px; padding: 6px; }"
            "QListWidget::item { padding: 4px 6px; border-radius: 4px; }"
            "QListWidget::item:selected { background: #1F5366; color: white; }"
            "QListWidget::item:hover { background: #16404F; }"
        )
        self._func_list.itemSelectionChanged.connect(self._on_functor_selected)
        right_layout.addWidget(self._func_list, 2)

        self._func_detail = QFrame()
        self._func_detail.setStyleSheet(
            "QFrame { background: #0E2630; border: 1px solid #1F4858; border-radius: 6px; padding: 12px; }")
        func_detail_layout = QVBoxLayout(self._func_detail)
        func_detail_layout.setContentsMargins(8, 8, 8, 8)

        self._func_detail_title = QLabel("Select an analogy to view mapping")
        self._func_detail_title.setStyleSheet("font-size: 12px; font-weight: 600; color: #D4A24C;")
        self._func_detail_title.setTextFormat(Qt.TextFormat.RichText)
        func_detail_layout.addWidget(self._func_detail_title)

        func_scroll = QScrollArea()
        func_scroll.setWidgetResizable(True)
        func_scroll.setStyleSheet("border: none; background: transparent;")
        self._func_detail_text = QLabel("")
        self._func_detail_text.setStyleSheet("color: #F0E9D5; font-size: 11px;")
        self._func_detail_text.setWordWrap(True)
        self._func_detail_text.setTextFormat(Qt.TextFormat.RichText)
        func_scroll.setWidget(self._func_detail_text)
        func_detail_layout.addWidget(func_scroll, 1)
        right_layout.addWidget(self._func_detail, 3)
        splitter.addWidget(right_widget)

    def load_data(self) -> None:
        self._status_lbl.setText("⏳ Querying categories…")
        # Fetch current data from categorical endpoint (cached result is fast)
        t = _spawn_http(self, "GET", "http://127.0.0.1:8000/api/v1/mind/categorical",
                        self._on_data_loaded, timeout=8.0)
        self._threads.append(t)
        t.finished.connect(lambda: self._threads.remove(t) if t in self._threads else None)

    def _on_data_loaded(self, result: dict) -> None:
        self._status_lbl.setText("")
        if not result or not result.get("ok"):
            self._status_lbl.setText(f"❌ Failed to fetch categories: {result.get('error') if result else 'no response'}")
            return
        
        categorical_data = result.get("data") or {}
        if not isinstance(categorical_data, dict):
            categorical_data = {}
        categories = categorical_data.get("categories") or []
        if not isinstance(categories, list):
            categories = []
        functors = categorical_data.get("functors") or []
        if not isinstance(functors, list):
            functors = []

        self._categories_data = categories
        self._functors_data = functors

        # Update categories list
        selected_cat = self._cat_list.currentItem().text() if self._cat_list.currentItem() else None
        self._cat_list.clear()
        for cat in categories:
            if isinstance(cat, dict):
                self._cat_list.addItem(cat.get("name", "Unnamed"))
        if selected_cat:
            items = self._cat_list.findItems(selected_cat, Qt.MatchFlag.MatchExact)
            if items:
                self._cat_list.setCurrentItem(items[0])

        # Update functors list
        selected_func = self._func_list.currentItem().text() if self._func_list.currentItem() else None
        self._func_list.clear()
        for func in functors:
            first_line = func.splitlines()[0] if isinstance(func, str) and func else "Functor"
            self._func_list.addItem(first_line)
        if selected_func:
            items = self._func_list.findItems(selected_func, Qt.MatchFlag.MatchExact)
            if items:
                self._func_list.setCurrentItem(items[0])

    def _on_scan_clicked(self) -> None:
        self._status_lbl.setText("⏳ Scanning and mapping category analogies…")
        self._scan_btn.setEnabled(False)
        t = _spawn_http(self, "GET", "http://127.0.0.1:8000/api/v1/mind/categorical",
                        self._on_scan_finished, timeout=12.0)
        self._threads.append(t)
        t.finished.connect(lambda: self._threads.remove(t) if t in self._threads else None)

    def _on_scan_finished(self, result: dict) -> None:
        self._scan_btn.setEnabled(True)
        result = result if isinstance(result, dict) else {}
        if result.get("ok"):
            self._status_lbl.setText("✅ Category scan completed and analogies mapped!")
            self._on_data_loaded(result)
        else:
            err = str(result.get("error") or "Unknown error")
            self._status_lbl.setText(f"❌ Scan failed: {err}")

    def _on_synth_clicked(self) -> None:
        concept = self._concept_input.text().strip()
        if not concept:
            return
        self._status_lbl.setText("⏳ Translating and modeling concept using LLM...")
        self._synth_btn.setEnabled(False)
        self._concept_input.setEnabled(False)
        t = _spawn_http(self, "POST", "http://127.0.0.1:8000/api/v1/mind/categorical/synthesize",
                        self._on_synth_finished, timeout=45.0, json_body={"concept": concept})
        self._threads.append(t)
        t.finished.connect(lambda: self._threads.remove(t) if t in self._threads else None)

    def _on_synth_finished(self, result: dict) -> None:
        self._synth_btn.setEnabled(True)
        self._concept_input.setEnabled(True)
        result = result if isinstance(result, dict) else {}
        if result.get("ok"):
            synth_data = result.get("data")
            if not isinstance(synth_data, dict):
                synth_data = {}
            if synth_data.get("status") == "ok":
                self._status_lbl.setText("✅ Concept modeled and analogies mapped successfully!")
                self._concept_input.clear()
                reconcile_data = synth_data.get("reconcile")
                if not isinstance(reconcile_data, dict):
                    reconcile_data = {}
                self._on_data_loaded({"ok": True, "data": reconcile_data})
            else:
                err_msg = str(synth_data.get("error") or "Unknown backend error")
                self._status_lbl.setText(f"❌ Synthesis failed: {err_msg}")
                QMessageBox.warning(self, "Synthesis Error", err_msg)
        else:
            err = str(result.get("error") or "Network connection failed")
            self._status_lbl.setText(f"❌ Network error: {err}")
            QMessageBox.warning(self, "Network Error", err)

    def _on_category_selected(self) -> None:
        item = self._cat_list.currentItem()
        if not item:
            self._cat_detail_title.setText("Select a category to view details")
            self._cat_detail_text.setText("")
            return
        cat_name = item.text()
        cat_data = next((c for c in self._categories_data if c.get("name") == cat_name), None)
        if not cat_data:
            return
            
        self._cat_detail_title.setText(f"Category: <span style='color:#7BC5C7;'><b>{cat_name}</b></span>")
        
        objs = cat_data.get("objects", [])
        mors = cat_data.get("morphisms", [])
        
        lines = []
        lines.append("<b>Objects (Concepts):</b>")
        lines.append(f"  ● {', '.join(objs)}" if objs else "  <i>None</i>")
        lines.append("")
        lines.append("<b>Morphisms (Relationships):</b>")
        if mors:
            for mor in mors:
                dom = mor.get("dom")
                codom = mor.get("codom")
                labels = mor.get("labels", [])
                for lbl in labels:
                    lines.append(f"  ⚡ {dom} ➔ {codom} <span style='color:#7BC5C7;'>({lbl})</span>")
        else:
            lines.append("  <i>None</i>")
            
        self._cat_detail_text.setText("<br/>".join(lines))

    def _on_functor_selected(self) -> None:
        item = self._func_list.currentItem()
        if not item:
            self._func_detail_title.setText("Select an analogy to view mapping")
            self._func_detail_text.setText("")
            return
        row = self._func_list.currentRow()
        if row < 0 or row >= len(self._functors_data):
            return
        func_text = self._functors_data[row]
        lines = func_text.splitlines()
        if not lines:
            return
            
        self._func_detail_title.setText(f"Analogy Mapping: <span style='color:#D4A24C;'><b>{lines[0].split(':', 1)[0]}</b></span>")
        
        body_lines = []
        for line in lines[1:]:
            line_str = line.strip()
            if line_str.startswith("●"):
                body_lines.append(f"<span style='color:#7FB069;'>●</span> {line_str[1:].strip()}")
            elif line_str.startswith("⚡"):
                body_lines.append(f"<span style='color:#D4A24C;'>⚡</span> {line_str[1:].strip()}")
            else:
                body_lines.append(line_str)
                
        self._func_detail_text.setText("<br/>".join(body_lines))


# ── Main MindPage QTabWidget wrapper ───────────────────────────────────────

class MindPage(QWidget):
    REFRESH_MS = 8000  # Poll status/stats every 8s

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._build()
        self._timer = QTimer(self)
        self._timer.setInterval(self.REFRESH_MS)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self.refresh()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(14)

        # Header
        hdr = QHBoxLayout(); hdr.setSpacing(10)
        title = QLabel("Unified Mind")
        title.setStyleSheet(f"color: {_TEXT}; font-size: 22px; font-weight: 700;")
        hdr.addWidget(title)
        self._status = QLabel("—")
        self._status.setStyleSheet(f"color: {_MUTED};")
        hdr.addWidget(self._status)
        hdr.addStretch(1)
        
        rebuild_btn = QPushButton("🔄 Rebuild Mind")
        rebuild_btn.setToolTip("Wipe and re-ingest mind.db from scratch using the latest project resolver.")
        rebuild_btn.clicked.connect(self._on_rebuild)
        hdr.addWidget(rebuild_btn)
        
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh)
        hdr.addWidget(refresh_btn)
        root.addLayout(hdr)

        # QTabWidget Layout
        self._tabs = QTabWidget()
        self._tabs.setStyleSheet(_TAB_QSS)
        root.addWidget(self._tabs, 1)

        # Tab 1: Activity & Stats
        self._stats_tab = _ActivityStatsTab()
        self._tabs.addTab(self._stats_tab, "Activity & Stats")

        # Tab 2: Categorical Mind (CatColab)
        self._categorical_tab = _CategoricalMindTab()
        self._tabs.addTab(self._categorical_tab, "Categorical Mind (ACT)")
        
        # Connect tab changes to load data dynamically
        self._tabs.currentChanged.connect(self._on_tab_changed)

    def _on_tab_changed(self, index: int) -> None:
        if index == 1:
            self._categorical_tab.load_data()

    def refresh(self) -> None:
        # Check active tab first
        active_tab_idx = self._tabs.currentIndex()
        
        stats = _api_get("/stats")
        if stats is None or stats.get("status") != "ok":
            self._status.setText("● mind offline — open Egon's Panop")
            self._status.setStyleSheet(f"color: {_ERR};")
            return
        
        self._status.setText(f"● live — schema v{stats.get('schema_version')}")
        self._status.setStyleSheet(f"color: {_OK};")

        # Route refresh to current active tab
        if active_tab_idx == 0:
            self._stats_tab.refresh(stats)
        elif active_tab_idx == 1:
            self._categorical_tab.load_data()

    def _on_rebuild(self) -> None:
        if _api_get("/stats", timeout=0.5) is not None:
            QMessageBox.warning(
                self, "Rebuild Mind DB",
                "The mind API is live, so the DB may be in use.\n\n"
                "Close Egon first, then run scripts/rebuild_mind.py from the "
                "Egon checkout for a safe full rebuild."
            )
            return
        reply = QMessageBox.question(
            self, "Rebuild Mind DB",
            "This will back up the current mind.db, wipe it, and re-ingest\n"
            "all agent transcripts from scratch using the latest resolver.\n\n"
            "Use this only when the mind API is offline and no other Egon/Panop\n"
            "process is holding the DB. For a normal rebuild, close Egon and\n"
            "run scripts/rebuild_mind.py from the checkout.\n\n"
            "Proceed?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._status.setText("⏳ rebuilding mind.db…")
        self._status.setStyleSheet(f"color: {_WARN};")

        class _Worker(QThread):
            done = Signal(bool, str)
            def run(self_):
                try:
                    import sys as _sys
                    from pathlib import Path as _P
                    egon_root = _P(__file__).resolve().parent.parent.parent
                    if str(egon_root) not in _sys.path:
                        _sys.path.insert(0, str(egon_root))
                    from scripts.rebuild_mind import main as rebuild_main
                    rc = rebuild_main()
                    if rc == 0:
                        self_.done.emit(True, "Mind DB rebuilt successfully.")
                    else:
                        self_.done.emit(False, f"Rebuild exited with code {rc}.")
                except Exception as e:
                    self_.done.emit(False, f"{type(e).__name__}: {str(e)[:300]}")

        def _on_done(ok: bool, msg: str):
            if ok:
                QMessageBox.information(self, "Rebuild Complete", msg)
                self.refresh()
            else:
                QMessageBox.warning(self, "Rebuild Failed", msg)
                self._status.setText("● rebuild failed")
                self._status.setStyleSheet(f"color: {_ERR};")

        self._rebuild_worker = _Worker()
        self._rebuild_worker.done.connect(_on_done)
        self._rebuild_worker.start()
