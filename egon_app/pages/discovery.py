"""Discovery page for Bruno-approved research candidates.

This page is intentionally siloed: candidates come from state/discovery_queue.json
and decisions go to state/discovery_decisions.json. No Zotero import is
performed here.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QThread, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from lib import egon_paths
from lib.discovery_watchers import QUEUE_PATH, run_watchers

DECISIONS_PATH = egon_paths.STATE_DIR / "discovery_decisions.json"

_BG = "#0b0c10"
_CARD = "#16181c"
_BORDER = "#27272a"
_TEXT = "#f5f5f7"
_MUTED = "#9ca3af"
_ACCENT = "#ff453a"
_OK = "#30d158"
_WARN = "#ff9f0a"


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=True, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
    finally:
        try:
            Path(tmp).unlink(missing_ok=True)
        except Exception:
            pass


def _queue_payload() -> dict[str, Any]:
    data = _read_json(QUEUE_PATH, {})
    if isinstance(data, list):
        data = {"version": 1, "candidates": data}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("version", 1)
    data.setdefault("candidates", [])
    return data


def _decisions_payload() -> dict[str, Any]:
    data = _read_json(DECISIONS_PATH, {})
    if isinstance(data, list):
        data = {"version": 1, "decisions": data}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("version", 1)
    data.setdefault("decisions", [])
    return data


def _decision_map(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in payload.get("decisions") or []:
        if isinstance(row, dict) and row.get("key"):
            out[str(row["key"])] = row
    return out


def _load_state() -> dict[str, Any]:
    queue = _queue_payload()
    decisions = _decisions_payload()
    decided = _decision_map(decisions)
    candidates = [
        c for c in (queue.get("candidates") or [])
        if isinstance(c, dict) and c.get("key") and c.get("key") not in decided
    ]
    candidates.sort(
        key=lambda c: (int(c.get("relevance_score") or 0), int(c.get("found_at") or 0)),
        reverse=True,
    )
    approved = [d for d in decided.values() if d.get("decision") == "approved"]
    rejected = [d for d in decided.values() if d.get("decision") == "rejected"]
    return {
        "queue": queue,
        "decisions": decisions,
        "candidates": candidates,
        "approved": approved,
        "rejected": rejected,
        "queue_path": str(QUEUE_PATH),
        "decisions_path": str(DECISIONS_PATH),
    }


def _record_decision(candidate: dict[str, Any], decision: str) -> dict[str, Any]:
    decision = "approved" if decision == "approved" else "rejected"
    key = str(candidate.get("key") or "")
    if not key:
        raise ValueError("candidate has no key")
    payload = _decisions_payload()
    rows = [
        d for d in (payload.get("decisions") or [])
        if not isinstance(d, dict) or d.get("key") != key
    ]
    rows.append({
        "key": key,
        "decision": decision,
        "decided_at": int(time.time()),
        "candidate": candidate,
    })
    payload["version"] = 1
    payload["updated_at"] = int(time.time())
    payload["decisions"] = rows
    _atomic_write_json(DECISIONS_PATH, payload)
    return _load_state()


class _DiscoveryWorker(QObject):
    finished = Signal(dict)

    def __init__(self, action: str, candidate: dict[str, Any] | None = None,
                 decision: str = "", force: bool = False):
        super().__init__()
        self._action = action
        self._candidate = candidate or {}
        self._decision = decision
        self._force = force

    def run(self) -> None:
        try:
            if self._action == "load":
                self.finished.emit({"ok": True, "data": _load_state()})
            elif self._action == "decide":
                data = _record_decision(self._candidate, self._decision)
                self.finished.emit({"ok": True, "data": data, "decision": self._decision})
            elif self._action == "watch":
                summary = run_watchers(force=self._force)
                self.finished.emit({"ok": True, "data": _load_state(), "watchers": summary})
            else:
                self.finished.emit({"ok": False, "error": "unknown action"})
        except Exception as exc:
            self.finished.emit({"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}"})


class DiscoveryPage(QWidget):
    def __init__(self):
        super().__init__()
        self._threads: list[QThread] = []
        self._workers: list[_DiscoveryWorker] = []
        self._candidate_by_key: dict[str, dict[str, Any]] = {}
        self.setStyleSheet(f"background: {_BG}; color: {_TEXT};")

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(14)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("Discovery")
        title.setStyleSheet(f"color: {_TEXT}; font-size: 26px; font-weight: 700;")
        subtitle = QLabel("Siloed candidate queue. Approval records only local decisions.")
        subtitle.setStyleSheet(f"color: {_MUTED}; font-size: 12px;")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch(1)

        self._run_btn = QPushButton("Run daily watchers")
        self._run_btn.setToolTip("Query OpenAlex and arXiv off the UI thread")
        self._run_btn.clicked.connect(lambda: self._start_worker("watch", force=False))
        header.addWidget(self._run_btn)

        self._refresh_btn = QPushButton("Refresh")
        self._refresh_btn.clicked.connect(self.refresh)
        header.addWidget(self._refresh_btn)
        root.addLayout(header)

        self._status = QLabel("Loading discovery queue...")
        self._status.setStyleSheet(f"color: {_MUTED};")
        root.addWidget(self._status)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setStyleSheet(
            f"QScrollArea {{ border: 1px solid {_BORDER}; border-radius: 8px; background: {_BG}; }}"
            "QScrollBar:vertical { background: #09090b; width: 8px; }"
            "QScrollBar::handle:vertical { background: #3f3f46; border-radius: 4px; }"
        )
        self._body = QWidget()
        self._body_lay = QVBoxLayout(self._body)
        self._body_lay.setContentsMargins(12, 12, 12, 12)
        self._body_lay.setSpacing(10)
        self._scroll.setWidget(self._body)
        root.addWidget(self._scroll, 1)

        self._timer = QTimer(self)
        self._timer.setInterval(30_000)
        self._timer.timeout.connect(self.refresh)
        self.refresh()

    def refresh(self) -> None:
        self._start_worker("load")

    def _start_worker(self, action: str, candidate: dict[str, Any] | None = None,
                      decision: str = "", force: bool = False) -> None:
        self._set_busy(True)
        worker = _DiscoveryWorker(action, candidate=candidate, decision=decision, force=force)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(lambda res, w=worker, t=thread: self._finish_worker(res, w, t))
        worker.finished.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(lambda w=worker, t=thread: self._drop_worker(w, t))
        self._workers.append(worker)
        self._threads.append(thread)
        thread.start()

    def _drop_worker(self, worker: _DiscoveryWorker, thread: QThread) -> None:
        if worker in self._workers:
            self._workers.remove(worker)
        if thread in self._threads:
            self._threads.remove(thread)
        thread.deleteLater()

    def _finish_worker(self, result: dict[str, Any], worker: _DiscoveryWorker, thread: QThread) -> None:
        self._set_busy(False)
        if not result.get("ok"):
            self._status.setText(f"Discovery error: {result.get('error', 'unknown')}")
            return
        data = result.get("data") or {}
        watch = result.get("watchers")
        self._render(data, watch)

    def _set_busy(self, busy: bool) -> None:
        self._run_btn.setEnabled(not busy)
        self._refresh_btn.setEnabled(not busy)

    def _clear_cards(self) -> None:
        while self._body_lay.count():
            item = self._body_lay.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _render(self, data: dict[str, Any], watch: dict[str, Any] | None = None) -> None:
        self._clear_cards()
        candidates = data.get("candidates") or []
        approved = data.get("approved") or []
        rejected = data.get("rejected") or []
        queue = data.get("queue") or {}
        self._candidate_by_key = {str(c.get("key")): c for c in candidates if c.get("key")}

        parts = [
            f"{len(candidates)} pending",
            f"{len(approved)} approved",
            f"{len(rejected)} rejected",
        ]
        if queue.get("last_run_date"):
            parts.append(f"last run {queue.get('last_run_date')}")
        if watch:
            parts.append(f"watchers {watch.get('status')} (+{watch.get('added', 0)})")
        self._status.setText(" | ".join(parts))

        if not candidates:
            empty = QLabel(
                "No pending discovery candidates. Run watchers after adding interests in Persona, "
                "or review approved items in state/discovery_decisions.json."
            )
            empty.setWordWrap(True)
            empty.setStyleSheet(f"color: {_MUTED}; font-size: 13px; padding: 24px;")
            self._body_lay.addWidget(empty)
            self._body_lay.addStretch(1)
            return

        for candidate in candidates[:200]:
            self._body_lay.addWidget(self._card(candidate))
        self._body_lay.addStretch(1)

    def _card(self, candidate: dict[str, Any]) -> QFrame:
        card = QFrame()
        card.setObjectName("discoveryCard")
        card.setStyleSheet(
            f"QFrame#discoveryCard {{ background: {_CARD}; border: 1px solid {_BORDER}; "
            "border-radius: 8px; }}"
        )
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        top = QHBoxLayout()
        source = str(candidate.get("source") or "?").upper()
        score = candidate.get("relevance_score") or 0
        meta = QLabel(f"{source} | score {score} | {candidate.get('year') or 'year ?'}")
        meta.setStyleSheet(f"color: {_WARN}; font-size: 11px; font-weight: 700;")
        top.addWidget(meta)
        top.addStretch(1)
        key = str(candidate.get("key") or "")
        top.addWidget(self._action_button("Approve", key, "approved", _OK))
        top.addWidget(self._action_button("Reject", key, "rejected", _ACCENT))
        layout.addLayout(top)

        title = QLabel(str(candidate.get("title") or "(untitled)"))
        title.setWordWrap(True)
        title.setTextInteractionFlags(Qt.TextSelectableByMouse)
        title.setStyleSheet(f"color: {_TEXT}; font-size: 15px; font-weight: 700;")
        layout.addWidget(title)

        authors = ", ".join(str(a) for a in (candidate.get("authors") or [])[:8] if a)
        if authors:
            author_lbl = QLabel(authors)
            author_lbl.setWordWrap(True)
            author_lbl.setStyleSheet(f"color: {_MUTED}; font-size: 12px;")
            layout.addWidget(author_lbl)

        abstract = QLabel(str(candidate.get("abstract_head") or "No abstract preview."))
        abstract.setWordWrap(True)
        abstract.setTextInteractionFlags(Qt.TextSelectableByMouse)
        abstract.setStyleSheet(f"color: {_MUTED}; font-size: 12px; line-height: 1.35;")
        layout.addWidget(abstract)

        bottom = QLabel(str(candidate.get("source_url") or ""))
        bottom.setTextInteractionFlags(Qt.TextSelectableByMouse)
        bottom.setStyleSheet(f"color: {_MUTED}; font-size: 11px;")
        layout.addWidget(bottom)
        return card

    def _action_button(self, label: str, key: str, decision: str, color: str) -> QPushButton:
        btn = QPushButton(label)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setStyleSheet(
            f"QPushButton {{ background: #09090b; color: {color}; border: 1px solid {_BORDER}; "
            "border-radius: 6px; padding: 5px 10px; font-weight: 700; }}"
            "QPushButton:hover { background: #1f2026; }"
        )
        btn.clicked.connect(lambda _=False, k=key, d=decision: self._decide(k, d))
        return btn

    def _decide(self, key: str, decision: str) -> None:
        candidate = self._candidate_by_key.get(key)
        if not candidate:
            self._status.setText("Candidate is no longer pending.")
            return
        self._start_worker("decide", candidate=candidate, decision=decision)
