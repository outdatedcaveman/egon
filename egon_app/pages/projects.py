"""Projects page — every project across every agent body.

Replaces the OLD generic source-card view (which only showed the
hardcoded "Active pipelines" — Mouseion, Routster, Panop) with a real
unified-mind project tree. Pulls from /api/v1/mind/projects (every
project the mind has ever seen across Claude Code, Codex, Antigravity,
ChatGPT, …) and per-project /api/v1/mind/activity to surface the most
recent thing each agent did on each project.

Layout:
  • Top strip: the "official" Egon-managed pipelines (Mouseion,
    Routster, Panop) since those have dedicated adapters with live
    status. Same data as the old page; we don't lose that view.
  • Main grid: every project from the mind, sorted by last-touched.
    Each card shows: agents that worked on it, recent activity count
    (7d), most-recent activity preview, link to drill in via the Mind
    tab filtered to that project.
  • Empty-state copy explains exactly what's missing if the mind has
    no data (Egon was just opened, ingestion hasn't run yet, etc.)

Bruno 2026-05-29: this is the answer to "where's flood from Codex,
Double from Antigravity, all my Claude projects" — they ALL surface
here once ingestion lands.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QFrame, QScrollArea, QPushButton, QSizePolicy,
)

# Shared palette (matches Home, Media, Mind)
_BG_CARD = "#0E2630"
_BORDER  = "#1F4858"
_ACCENT  = "#7BC5C7"
_TEXT    = "#F0E9D5"
_MUTED   = "#9CA3AF"
_GOLD    = "#D4A24C"
_OK      = "#7FB069"
_WARN    = "#D4A24C"
_ERR     = "#D67A6A"

_AGENT_COLOR = {
    "claude-code": "#D77A56",
    "codex":       "#7BC5C7",
    "antigravity": "#9D7BC5",
    "chatgpt":     "#7FB069",
    "gemini":      "#D4A24C",
}

_MIND = "http://127.0.0.1:8000/api/v1/mind"

# Per-project icon. Falls back to a folder glyph for anything unknown.
_PROJECT_ICON = {
    "egon":        "🧠",
    "panop":       "📥",
    "routster":    "🔀",
    "mouseion":    "📚",
    "synesism":    "🌀",
    "double":      "🎓",
    "flood":       "🌊",
    "asympt":      "🎙️",
    "citizenship": "🛂",
    "ancestry":    "🌳",
    "infohub":     "📊",
    "careerops":   "💼",
    "claude-meta": "🤖",
    "noiacast":    "🎙️",
}


def _icon_for(slug: str) -> str:
    return _PROJECT_ICON.get((slug or "").lower(), "📁")


def _api_get(path: str, params: dict | None = None,
             timeout: float = 1.5) -> dict | None:
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.get(f"{_MIND}{path}", params=params or {})
        if r.status_code == 200:
            return r.json()
    except Exception:
        return None
    return None


def _fmt_age(ts: int | None) -> str:
    if not ts:
        return "—"
    delta = int(datetime.now().timestamp()) - int(ts)
    if delta < 0:    return "future?"
    if delta < 60:   return f"{delta}s ago"
    if delta < 3600: return f"{delta // 60}m ago"
    if delta < 86400: return f"{delta // 3600}h ago"
    if delta < 7 * 86400: return f"{delta // 86400}d ago"
    return f"{delta // 86400}d ago"


def _project_card(slug: str, summary: dict) -> QFrame:
    """Render one project card. `summary` keys:
        agents (list of agent_name strings),
        activity_count_7d (int),
        last_ts (int unix),
        last_kind (str), last_payload_preview (str), last_agent (str)."""
    card = QFrame()
    card.setObjectName("projCard")
    # IMPORTANT: scope the border to the frame's object name. An unscoped
    # `border: 1px` cascades onto every child QLabel in Qt, which is what
    # drew the boxed-in lines Bruno flagged. Scoping keeps the border on the
    # card only. Bruno 2026-05-29.
    card.setStyleSheet(
        f"QFrame#projCard {{ background-color: {_BG_CARD}; "
        f"border: 1px solid {_BORDER}; border-radius: 10px; }}")
    # Fixed shape — Bruno 2026-05-29 ("each individual project a card"),
    # no stretching. Cards behave as a true tile grid.
    card.setFixedSize(310, 150)
    card.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
    v = QVBoxLayout(card); v.setContentsMargins(16, 14, 16, 14); v.setSpacing(6)

    # Header: icon + slug + recent agents pill row
    hdr = QHBoxLayout(); hdr.setSpacing(8)
    icon = QLabel(_icon_for(slug))
    icon.setStyleSheet("font-size: 20px;")
    hdr.addWidget(icon)
    title = QLabel(slug)
    title.setStyleSheet(f"color: {_TEXT}; font-size: 16px; font-weight: 600;")
    hdr.addWidget(title)
    hdr.addStretch(1)
    age = QLabel(_fmt_age(summary.get("last_ts")))
    age.setStyleSheet(f"color: {_MUTED};")
    hdr.addWidget(age)
    v.addLayout(hdr)

    # Agents row
    agents = summary.get("agents") or []
    if agents:
        ar = QHBoxLayout(); ar.setSpacing(6)
        for a in agents[:5]:
            pill = QLabel(a)
            pill.setStyleSheet(
                f"background-color: {_AGENT_COLOR.get(a, _MUTED)}; "
                f"color: #0E2630; padding: 2px 8px; border-radius: 8px; "
                f"font-weight: 600; font-size: 11px;")
            ar.addWidget(pill)
        ar.addStretch(1)
        v.addLayout(ar)

    # Activity metric
    n_7d = summary.get("activity_count_7d") or 0
    metric = QLabel(f"{n_7d} activity events in last 7 days")
    metric.setStyleSheet(f"color: {_ACCENT};")
    v.addWidget(metric)

    # Most-recent activity preview
    last_kind = summary.get("last_kind")
    last_preview = summary.get("last_payload_preview") or ""
    last_agent = summary.get("last_agent")
    if last_kind:
        preview = QLabel(f"latest: [{last_agent}] {last_kind} — {last_preview[:110]}")
        preview.setStyleSheet(f"color: {_MUTED};")
        preview.setWordWrap(True)
        v.addWidget(preview)
    v.addStretch(1)

    return card


def _pipeline_card(data_key: str, info: dict, label: str) -> QFrame:
    """Re-render of the old 'active pipeline' card. Bruno 2026-05-29:
    inside Egon these are named by their EGON role, not their upstream
    project — `panop`→"Inbox", `routster`→"Navigation" — so Bruno doesn't
    confuse the external projects with Egon's embedded pipelines. `label`
    is the display name; `data_key` selects the snapshot source + icon."""
    card = QFrame()
    card.setObjectName("pipeCard")
    # Scoped selector so the border stays on the card and doesn't cascade
    # onto the child labels (the boxed-line artefact). Bruno 2026-05-29.
    card.setStyleSheet(
        f"QFrame#pipeCard {{ background-color: {_BG_CARD}; "
        f"border: 1px solid {_BORDER}; border-radius: 10px; }}")
    card.setFixedSize(310, 150)
    card.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
    v = QVBoxLayout(card); v.setContentsMargins(16, 14, 16, 14); v.setSpacing(6)

    # Header: icon + name + status dot on the right
    hdr = QHBoxLayout(); hdr.setSpacing(8)
    name = label
    icon = QLabel(_icon_for(data_key))
    icon.setStyleSheet("font-size: 20px;")
    hdr.addWidget(icon)
    title = QLabel(name)
    title.setStyleSheet(f"color: {_TEXT}; font-size: 16px; font-weight: 600;")
    hdr.addWidget(title)
    hdr.addStretch(1)
    status = (info or {}).get("status", "—")
    color = _OK if status == "ok" else (_WARN if status == "warming" else
                                        _MUTED if status == "unconfigured" else _ERR)
    dot = QLabel(f"● {status}")
    dot.setStyleSheet(f"color: {color}; font-weight: 600;")
    hdr.addWidget(dot)
    v.addLayout(hdr)

    # Show a couple of representative numeric fields as clean "key   value"
    # rows. Skip noisy/list-y fields like `tables` that don't read well on a
    # tile. Bruno 2026-05-29.
    shown = 0
    for k in ("total_links", "links", "delta_24h", "queue_count", "items"):
        info_d = info or {}
        if k in info_d and shown < 3:
            row = QHBoxLayout(); row.setSpacing(8)
            kl = QLabel(k.replace("_", " "))
            kl.setStyleSheet(f"color: {_MUTED}; font-size: 12px;")
            vl = QLabel(str(info_d[k]))
            vl.setStyleSheet(f"color: {_TEXT}; font-size: 12px; font-weight: 600;")
            row.addWidget(kl); row.addStretch(1); row.addWidget(vl)
            v.addLayout(row)
            shown += 1
    v.addStretch(1)
    return card


class ProjectsPage(QWidget):
    REFRESH_MS = 8000

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
        root.setContentsMargins(24, 18, 24, 18); root.setSpacing(14)

        hdr = QHBoxLayout(); hdr.setSpacing(10)
        title = QLabel("Projects")
        title.setStyleSheet(f"color: {_TEXT}; font-size: 22px; font-weight: 600;")
        hdr.addWidget(title)
        self._status = QLabel("—")
        self._status.setStyleSheet(f"color: {_MUTED};")
        hdr.addWidget(self._status)
        hdr.addStretch(1)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh)
        hdr.addWidget(refresh_btn)
        root.addLayout(hdr)

        # Pipelines strip — a proper grid (no QHBoxLayout stretching)
        pipe_label = QLabel("Egon-managed pipelines (snapshot-driven)")
        pipe_label.setStyleSheet(f"color: {_MUTED};")
        root.addWidget(pipe_label)
        self._pipe_grid_host = QFrame()
        self._pipe_grid = QGridLayout(self._pipe_grid_host)
        self._pipe_grid.setContentsMargins(0, 0, 0, 0)
        self._pipe_grid.setSpacing(12)
        self._pipe_grid.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        root.addWidget(self._pipe_grid_host)

        # All-projects grid heading
        all_label = QLabel("All projects from the unified mind (every agent, every body)")
        all_label.setStyleSheet(f"color: {_TEXT}; font-weight: 600; padding-top: 6px;")
        root.addWidget(all_label)

        # Scrollable grid
        scroll = QScrollArea()
        scroll.setObjectName("projScroll")
        scroll.setWidgetResizable(True)
        scroll.setMinimumHeight(420)
        # Scope to the object name so the border doesn't cascade onto the
        # project cards' inner labels. Bruno 2026-05-29.
        scroll.setStyleSheet(
            f"QScrollArea#projScroll {{ background-color: transparent; "
            f"border: 1px solid {_BORDER}; border-radius: 10px; }}")
        self._grid_host = QWidget()
        self._grid = QGridLayout(self._grid_host)
        self._grid.setContentsMargins(10, 10, 10, 10)
        self._grid.setSpacing(12)
        scroll.setWidget(self._grid_host)
        root.addWidget(scroll, stretch=1)

    def _clear_layout(self, layout) -> None:
        while layout.count():
            it = layout.takeAt(0)
            if it and it.widget():
                it.widget().deleteLater()

    def refresh(self) -> None:
        # ----- pipelines (grid, fixed-size cards) -----
        self._clear_layout(self._pipe_grid)
        from egon_app import data
        pipelines = data.last_pass() or {}
        sources = (pipelines.get("sources") or {})
        # (snapshot key, Egon display name). Inside Egon, Panop is "Inbox"
        # and Routster is "Navigation" — Bruno 2026-05-29, to avoid mixing
        # the embedded pipelines up with the external projects below.
        pipelines = (("mouseion", "Mouseion"),
                     ("routster", "Navigation"),
                     ("panop", "Inbox"))
        for i, (key, label) in enumerate(pipelines):
            info = sources.get(key) or {}
            # 4 columns max, wraps as the window narrows
            self._pipe_grid.addWidget(_pipeline_card(key, info, label),
                                      i // 4, i % 4)

        # ----- mind projects (single batch query) -----
        self._clear_layout(self._grid)
        # Antigravity 2026-05-31: use the batch /projects/summary endpoint
        # instead of one /activity query per project. Cuts N HTTP calls to 1.
        summary_resp = _api_get("/projects/summary", timeout=3.0)
        if summary_resp is None or summary_resp.get("status") != "ok":
            # Fallback: try the plain /projects endpoint (in case Panop is
            # running an older version without the batch endpoint).
            projects_resp = _api_get("/projects")
            if projects_resp is None:
                self._status.setText("● mind offline — restart Egon so Panop binds :8000")
                self._status.setStyleSheet(f"color: {_ERR};")
                self._render_empty(
                    "No connection to Egon's mind on :8000.\n\n"
                    "If Egon is open, give it ~30 s to bind. If it's not, "
                    "launch it once and come back to this tab."
                )
                return
            projects = (projects_resp or {}).get("projects") or []
            self._status.setText(f"● {len(projects)} projects tracked")
            self._status.setStyleSheet(f"color: {_OK};")
            if not projects:
                self._render_empty("The mind is up but no projects are registered yet.")
                return
            # Minimal cards without per-project activity detail
            COLS = 4
            for i, proj in enumerate(projects[:50]):
                slug = proj.get("slug") or "?"
                summary = {
                    "agents": [], "activity_count_7d": 0,
                    "last_ts": proj.get("updated_at") or 0,
                    "last_kind": None, "last_payload_preview": "",
                    "last_agent": "—",
                }
                self._grid.addWidget(_project_card(slug, summary),
                                      i // COLS, i % COLS,
                                      Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            self._grid.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            self._grid.setRowStretch((len(projects[:50]) + COLS - 1) // COLS, 1)
            return

        projects = summary_resp.get("projects") or []
        self._status.setText(f"● {len(projects)} projects tracked")
        self._status.setStyleSheet(f"color: {_OK};")

        if not projects:
            stats = _api_get("/stats") or {}
            sessions = stats.get("sessions", 0)
            activity = stats.get("activity", 0)
            self._render_empty(
                f"The mind is up (schema v{stats.get('schema_version')}) "
                f"but no projects are registered yet.\n\n"
                f"Stats: agents={stats.get('agents', 0)}, sessions={sessions}, "
                f"activity={activity}, memory={stats.get('memory', 0)}.\n\n"
                "The pull-ingestion service scans your Claude/Codex/Antigravity "
                "memory dirs every 60 s. First-time ingestion can take a few "
                "minutes — projects show up here as soon as they're attributed."
            )
            return

        # Render in a 4-column grid of fixed-size cards.
        COLS = 4
        for i, proj in enumerate(projects):
            slug = proj.get("slug") or "?"
            # The batch endpoint returns the summary fields directly
            summary = {
                "agents": proj.get("agents") or [],
                "activity_count_7d": proj.get("activity_count_7d") or 0,
                "last_ts": proj.get("last_ts") or proj.get("updated_at") or 0,
                "last_kind": proj.get("last_kind"),
                "last_payload_preview": proj.get("last_payload_preview") or "",
                "last_agent": proj.get("last_agent") or "—",
            }
            self._grid.addWidget(_project_card(slug, summary),
                                  i // COLS, i % COLS,
                                  Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        # Push everything to the top-left, no stretching.
        self._grid.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self._grid.setRowStretch((len(projects) + COLS - 1) // COLS, 1)

    def _render_empty(self, msg: str) -> None:
        empty = QLabel(msg)
        empty.setStyleSheet(f"color: {_MUTED};")
        empty.setWordWrap(True)
        empty.setAlignment(Qt.AlignmentFlag.AlignTop)
        empty.setMinimumHeight(120)
        self._grid.addWidget(empty, 0, 0)


def _build_summary(slug: str, proj: dict, activity_rows: list[dict]) -> dict:
    agents: set[str] = set()
    last_ts = proj.get("updated_at") or 0
    last_kind = None
    last_payload_preview = ""
    last_agent = None
    last_seen_ts = 0
    n_7d = 0
    seven_days_ago = int(datetime.now().timestamp()) - 7 * 86400
    for r in activity_rows:
        ts = r.get("ts") or 0
        if ts >= seven_days_ago:
            n_7d += 1
        a = r.get("agent_name")
        if a:
            agents.add(a)
        if ts > last_seen_ts:
            last_seen_ts = ts
            last_kind = r.get("kind")
            payload = r.get("payload") or {}
            try:
                import json as _json
                last_payload_preview = _json.dumps(payload, ensure_ascii=False)
            except Exception:
                last_payload_preview = str(payload)
            last_agent = a
    return {
        "agents": sorted(agents),
        "activity_count_7d": n_7d,
        "last_ts": last_seen_ts or last_ts,
        "last_kind": last_kind,
        "last_payload_preview": last_payload_preview,
        "last_agent": last_agent or "—",
    }
