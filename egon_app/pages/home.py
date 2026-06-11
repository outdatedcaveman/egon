"""Home page — landing dashboard. Native QWidget composition.

2026-05-22 redesign (Bruno: "ugly, misaligned, overly austere"):
  - Hero header: time-of-day greeting + last-pass summary
  - Stat strip: 4 headline metrics with big accent numbers
  - Source-health GRID (not a cramped vertical list): each source is a
    mini-card with a colour-coded status dot, item count, and detail line,
    laid out in a responsive lattice
  - Quick-actions row
All spacing/alignment is on an 8px rhythm; cards share one geometry.
"""
from __future__ import annotations

import html
import re
from urllib.parse import urlencode
from datetime import datetime

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QFrame, QScrollArea, QPushButton,
)

from egon_app import data

# palette (shared with Media cards)
_BG_CARD = "#0E2630"
_BORDER  = "#1F4858"
_ACCENT  = "#7BC5C7"
_TEXT    = "#F0E9D5"
_MUTED   = "#9CA3AF"
_GOLD    = "#D4A24C"
_OK      = "#7FB069"
_WARN    = "#D4A24C"
_ERR     = "#D67A6A"


def _status_color(status: str) -> str:
    return {"ok": _OK, "alive": _OK, "ready": _OK,
            "warming": _WARN, "stale": _WARN, "unconfigured": _MUTED,
            "timeout": _ERR, "error": _ERR}.get(str(status).lower(), _MUTED)


def _stat_card(label: str, value: str, accent: str = _ACCENT, hint: str = "") -> QFrame:
    card = QFrame()
    card.setObjectName("statCard")
    card.setMinimumHeight(96)
    v = QVBoxLayout(card)
    v.setContentsMargins(18, 14, 18, 14)
    v.setSpacing(2)
    l = QLabel(label.upper())
    l.setObjectName("statCardLabel")
    v.addWidget(l)
    val = QLabel(value)
    val.setObjectName("statCardVal")
    val.setStyleSheet(f"color: {accent};")
    v.addWidget(val)
    if hint:
        h = QLabel(hint)
        h.setObjectName("statCardHint")
        h.setWordWrap(True)
        v.addWidget(h)
    v.addStretch(1)
    return card


def _distill_recall_text(content: str) -> tuple[str, str]:
    """Turn raw shared-mind rows into a readable dashboard insight."""
    text = re.sub(r"\\\\\?\\[A-Z]:\\[^\s]+", "", str(content or ""))
    text = re.sub(r"\bthread_id:\s*[0-9a-f-]+", "", text, flags=re.I)
    text = re.sub(r"\brollout_path:\s*\S+", "", text, flags=re.I)
    text = re.sub(r"\bcwd:\s*\S+", "", text, flags=re.I)
    text = re.sub(r"\bupdated_at:\s*\S+", "", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip(" :;-")
    if "#" in text:
        text = text.split("#", 1)[1].strip()
    text = re.sub(r"^Rollout Summary:\s*", "", text, flags=re.I).strip(" :;-")

    lower = text.lower()
    if "adb" in lower or "phone" in lower or "inbox" in lower:
        title = "Inbox Needs Phone Authorization"
        insight = (
            "Inbox is not stuck on classification. It is waiting for Android Debug Bridge "
            "to see an authorized phone, then it can fetch Chrome tabs."
        )
    elif "token" in lower or "guardrail" in lower:
        title = "Token Guardrails Matter"
        insight = (
            "Recent Egon work is about preventing agents from wasting scarce context: "
            "use targeted mind capsules, restore points, and durable handoffs."
        )
    elif "mind" in lower or "mcp" in lower:
        title = "Shared Mind Is The Coordination Layer"
        insight = (
            "The important invariant is that Codex, Claude, and Antigravity should read "
            "the same compact context before acting and write back durable outcomes."
        )
    else:
        title = "Relevant Context"
        insight = text[:260] + ("..." if len(text) > 260 else "")
    return title, insight


def _source_card(name: str, info: dict) -> QFrame:
    status = (info.get("status", "—") if isinstance(info, dict) else "—")
    raw_issue = ""
    if isinstance(info, dict):
        raw_issue = str(info.get("error") or info.get("note") or "")
    if str(status).lower() in {"ok", "alive", "ready"} and re.search(r"not reachable|unreachable|failed|error", raw_issue, re.I):
        status = "error"
    colour = _status_color(status)
    card = QFrame()
    card.setObjectName("srcCard")
    card.setMinimumHeight(72)
    v = QVBoxLayout(card)
    v.setContentsMargins(14, 10, 14, 10)
    v.setSpacing(3)

    top = QHBoxLayout(); top.setSpacing(8)
    dot = QLabel("●"); dot.setStyleSheet(f"color: {colour}; font-size: 12px;")
    top.addWidget(dot)
    n = QLabel(name)
    n.setObjectName("srcCardName")
    top.addWidget(n)
    top.addStretch(1)
    # "unconfigured" reads like a fault; it actually means "waiting on you"
    label = "needs setup" if str(status).lower() in ("unconfigured", "skip") else str(status)
    st = QLabel(label)
    st.setStyleSheet(f"color: {colour}; font-size: 11px;")
    top.addWidget(st)
    tw = QWidget(); tw.setLayout(top)
    v.addWidget(tw)

    # detail line — best available metric
    detail = ""
    if isinstance(info, dict):
        for k, fmt in (("total_items", "{:,} items"), ("count", "{:,} items"),
                       ("total_links", "{:,} links"), ("pages_mirrored", "{:,} pages"),
                       ("queue_count", "queue {}"), ("size_mb", "{} MB")):
            if info.get(k) not in (None, ""):
                try:
                    detail = fmt.format(info[k])
                except Exception:
                    detail = f"{info[k]}"
                break
        if not detail and info.get("error"):
            detail = str(info["error"])[:80]
        elif not detail and info.get("note"):
            detail = str(info["note"])[:80]
    d = QLabel(detail)
    d.setObjectName("srcCardDetail")
    d.setWordWrap(True)
    v.addWidget(d)
    return card


class HomePage(QWidget):
    card_reviewed = Signal()
    # (proposals, stats, capsule) fetched off-thread; auto-queued to UI thread.
    _bg_ready = Signal(object, object, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget()
        scroll.setWidget(body)
        outer.addWidget(scroll)

        self._v = QVBoxLayout(body)
        self._v.setContentsMargins(32, 28, 32, 28)
        self._v.setSpacing(20)

        # hero
        self._greeting = QLabel()
        self._greeting.setStyleSheet(f"color: {_TEXT}; font-size: 26px; font-weight: 700;")
        self._v.addWidget(self._greeting)
        self._subhead = QLabel()
        self._subhead.setStyleSheet(f"color: {_MUTED}; font-size: 13px;")
        self._v.addWidget(self._subhead)

        # stat strip
        self._stats_grid = QGridLayout()
        self._stats_grid.setSpacing(14)
        self._v.addLayout(self._stats_grid)

        # Proactive Insights & Strategies
        self._insights_header = QLabel("Proactive Insights & Strategies")
        self._insights_header.setStyleSheet(f"color: {_TEXT}; font-size: 15px; font-weight: 600; margin-top: 4px;")
        self._v.addWidget(self._insights_header)

        self._insights_card = QFrame()
        self._insights_card.setStyleSheet(f"background-color: {_BG_CARD}; border: 1px solid {_BORDER}; border-radius: 6px;")
        self._insights_layout = QVBoxLayout(self._insights_card)
        self._insights_layout.setContentsMargins(16, 14, 16, 14)
        self._insights_layout.setSpacing(10)
        
        self._insights_list = QVBoxLayout()
        self._insights_list.setSpacing(8)
        self._insights_layout.addLayout(self._insights_list)
        self._v.addWidget(self._insights_card)

        # Contextual recall: surface relevant saved knowledge from the shared mind.
        self._recall_header = QLabel("Contextual Recall")
        self._recall_header.setStyleSheet(f"color: {_TEXT}; font-size: 15px; font-weight: 600; margin-top: 4px;")
        self._v.addWidget(self._recall_header)

        self._recall_card = QFrame()
        self._recall_card.setStyleSheet(f"background-color: {_BG_CARD}; border: 1px solid {_BORDER}; border-radius: 6px;")
        recall_layout = QVBoxLayout(self._recall_card)
        recall_layout.setContentsMargins(16, 14, 16, 14)
        recall_layout.setSpacing(12)

        # Question label
        self._q_label = QLabel()
        self._q_label.setWordWrap(True)
        self._q_label.setTextFormat(Qt.RichText)
        self._q_label.setStyleSheet(f"color: {_TEXT}; font-size: 14px; font-weight: 500;")
        recall_layout.addWidget(self._q_label)

        # Answer label (initially hidden)
        self._a_label = QLabel()
        self._a_label.setWordWrap(True)
        self._a_label.setTextFormat(Qt.RichText)
        self._a_label.setStyleSheet(f"color: {_GOLD}; font-size: 14px; border-top: 1px dashed {_BORDER}; padding-top: 8px;")
        self._a_label.hide()
        recall_layout.addWidget(self._a_label)

        # Metadata/Tags label
        self._meta_label = QLabel()
        self._meta_label.setStyleSheet("font-size: 11px;")
        recall_layout.addWidget(self._meta_label)

        # Button row containing Reveal and Rating buttons
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        # Reveal relevance Button
        self._reveal_btn = QPushButton("Show why this matters")
        self._reveal_btn.setStyleSheet(
            f"background-color: {_BORDER}; color: {_TEXT}; font-weight: 600; "
            f"padding: 6px 16px; border-radius: 4px;"
        )
        self._reveal_btn.clicked.connect(self.reveal_answer)
        btn_row.addWidget(self._reveal_btn)

        # Rating buttons widget (initially hidden)
        self._rating_layout_widget = QWidget()
        rating_layout = QHBoxLayout(self._rating_layout_widget)
        rating_layout.setContentsMargins(0, 0, 0, 0)
        rating_layout.setSpacing(8)

        ratings = [
            ("Forgot", 0, _ERR),
            ("Hard", 2, _WARN),
            ("Good", 4, _OK),
            ("Easy", 5, _ACCENT),
        ]
        for label, val, color in ratings:
            btn = QPushButton(label)
            btn.setStyleSheet(
                f"background-color: {color}; color: #0E2A35; font-weight: 600; "
                f"padding: 6px 14px; border-radius: 4px;"
            )
            btn.clicked.connect(lambda checked=False, r_val=val: self.submit_review(r_val))
            rating_layout.addWidget(btn)
        rating_layout.addStretch(1)
        self._rating_layout_widget.hide()
        btn_row.addWidget(self._rating_layout_widget)

        btn_row.addStretch(1)
        recall_layout.addLayout(btn_row)

        self._v.addWidget(self._recall_card)

        # data loads async after construction — see _kick_async below

        # source health header
        sh = QLabel("Source health")
        sh.setStyleSheet(f"color: {_TEXT}; font-size: 15px; font-weight: 600; margin-top: 4px;")
        self._v.addWidget(sh)
        self._sources_grid = QGridLayout()
        self._sources_grid.setSpacing(12)
        self._v.addLayout(self._sources_grid)

        self._v.addStretch(1)

        self._last_signature = None   # skip rebuilds when nothing changed

        # Never fetch on the UI thread. __init__ used to call refresh() +
        # load_next_card() synchronously — three blocking mind-API calls that
        # made the window take seconds to appear (2026-06-11 perf pass). Now
        # the page paints instantly with a placeholder and a worker thread
        # delivers the data via _bg_ready (Qt queues it to the UI thread).
        self._bg_ready.connect(self._on_bg_ready)
        ph = QLabel("● loading mind data…")
        ph.setStyleSheet(f"color: {_MUTED}; font-size: 12px; font-style: italic;")
        self._insights_list.addWidget(ph)
        QTimer.singleShot(50, self._kick_async)
        self._timer = QTimer(self)
        self._timer.setInterval(30_000)   # was 15s; data layer caches at 60s anyway
        self._timer.timeout.connect(self._kick_async)
        self._timer.start()

    def _kick_async(self) -> None:
        import threading

        def _bg():
            proposals = _api_get("/introspection/proposals", timeout=8.0)
            stats = _api_get("/stats", timeout=4.0)
            query = (
                "surface one important relevant data point from recent work: "
                "saved pages, Notion, Mouseion/articles, Claude sessions, files, "
                "or shared agent memory; explain why it matters now"
            )
            capsule = _api_get(
                "/context/v2?" + urlencode(
                    {"project": "egon", "query": query, "budget_chars": 3500}),
                timeout=10.0)
            self._bg_ready.emit(proposals, stats, capsule)

        threading.Thread(target=_bg, daemon=True, name="home-refresh").start()

    def _on_bg_ready(self, proposals, stats, capsule) -> None:
        self.refresh(pre=(proposals, stats))
        self.load_next_card(pre=capsule)

    def reveal_answer(self) -> None:
        self._reveal_btn.hide()
        self._a_label.show()

    def load_next_card(self, pre=None) -> None:
        if pre is not None:
            res = pre
        else:
            query = (
                "surface one important relevant data point from recent work: "
                "saved pages, Notion, Mouseion/articles, Claude sessions, files, "
                "or shared agent memory; explain why it matters now"
            )
            res = _api_get(f"/context/v2?{urlencode({'project': 'egon', 'query': query, 'budget_chars': 3500})}", timeout=4.0)
        self._current_card = None
        if not res or res.get("status") != "ok":
            self._recall_card.hide()
            self._recall_header.hide()
            return

        self._recall_header.show()
        self._recall_card.show()

        sections = res.get("sections") if isinstance(res.get("sections"), dict) else {}
        memories = sections.get("durable_memory") if isinstance(sections.get("durable_memory"), list) else []
        activities = sections.get("recent_activity") if isinstance(sections.get("recent_activity"), list) else []
        candidates = memories + activities
        item = {}
        for candidate in candidates:
            raw = str(candidate.get("content") or candidate.get("summary") or "")
            if raw and "rollout_path" not in raw.lower() and "thread_id" not in raw.lower():
                item = candidate
                break
        if not item and candidates:
            item = candidates[0]
        content = str(item.get("content") or item.get("summary") or res.get("briefing") or "").strip()
        title, insight = _distill_recall_text(content)
        tags = item.get("tags") if isinstance(item.get("tags"), list) else []
        source = f"memory {item.get('id')}" if item.get("id") else "mind context"
        self._q_label.setText(
            f"<b>{html.escape(title)}:</b> {html.escape(insight)}"
        )
        self._a_label.setText(
            "<b>Why now:</b> "
            "This is the compressed point from shared memory; raw paths, thread ids, and rollout metadata stay hidden unless you open the source."
        )
        tags_text = ", ".join(str(t) for t in tags[:8]) if tags else "context broker v2"
        self._meta_label.setText(
            f"<span style='color: {_MUTED};'>Source: {html.escape(source)} | Tags: {html.escape(tags_text)}</span>"
        )

        self._a_label.hide()
        self._rating_layout_widget.hide()
        self._reveal_btn.show()

    def submit_review(self, rating_val: int) -> None:
        if not self._current_card:
            return
        card_id = self._current_card["id"]
        self._rating_layout_widget.setEnabled(False)

        def _bg():
            _api_post(f"/memory/{card_id}/review", {"rating": rating_val})
            self.card_reviewed.emit()

        import threading
        threading.Thread(target=_bg, daemon=True).start()

    def on_card_reviewed(self) -> None:
        self._rating_layout_widget.setEnabled(True)
        self.load_next_card()

    def refresh(self, pre=None) -> None:
        # Proactive insights — prefetched by _kick_async when called from the
        # background path; only fetches inline if invoked directly (legacy).
        if pre is not None:
            res, _stats = pre
        else:
            res, _stats = _api_get("/introspection/proposals"), None
        proposals = (res or {}).get("proposals") or []

        while self._insights_list.count():
            item = self._insights_list.takeAt(0)
            w = item.widget()
            if w: w.deleteLater()

        if not res:
            stats = _stats if pre is not None else _api_get("/stats")
            if stats:
                text = "mind online; proactive-insights feed is not configured yet"
                color = _WARN
            else:
                text = "mind service unreachable"
                color = _ERR
            empty = QLabel(f"● {text}")
            empty.setStyleSheet(f"color: {color}; font-size: 12px; font-style: italic;")
            self._insights_list.addWidget(empty)
        elif not proposals:
            empty = QLabel("All systems running efficiently. No anomalies or lock conflicts detected.")
            empty.setStyleSheet(f"color: {_OK}; font-size: 12px; font-style: italic;")
            self._insights_list.addWidget(empty)
        else:
            for p in proposals[:5]:
                p_widget = QFrame()
                p_color = _WARN if p.get("severity") == "warning" else _OK if p.get("severity") == "info" else _ERR
                p_widget.setStyleSheet(
                    f"background-color: #16404F; border: 1px solid {_BORDER}; "
                    f"border-radius: 8px; padding: 10px;"
                )
                ph = QHBoxLayout(p_widget)
                ph.setContentsMargins(10, 8, 10, 8)
                ph.setSpacing(12)
                
                dot = QLabel("●")
                dot.setStyleSheet(f"color: {p_color}; font-size: 16px;")
                ph.addWidget(dot)
                
                pv = QVBoxLayout()
                pv.setSpacing(2)
                
                title = QLabel(f"<b>{p.get('title')}</b>")
                title.setTextFormat(Qt.RichText)
                title.setStyleSheet(f"color: {_TEXT}; font-size: 12px;")
                pv.addWidget(title)
                
                desc = QLabel(p.get("description"))
                desc.setStyleSheet(f"color: {_MUTED}; font-size: 11px;")
                desc.setWordWrap(True)
                pv.addWidget(desc)
                
                ph.addLayout(pv, stretch=1)
                
                proj = p.get("project")
                if proj and proj != "general":
                    badge = QLabel(proj.upper())
                    badge.setStyleSheet(
                        f"background-color: {_BORDER}; color: {_GOLD}; "
                        f"font-size: 9px; padding: 2px 6px; border-radius: 4px; font-weight: 600;"
                    )
                    ph.addWidget(badge)
                    
                self._insights_list.addWidget(p_widget)

        d = data.last_pass()
        sources = d.get("sources", {}) or {}

        # Skip the (expensive) widget rebuild when the underlying data is
        # unchanged — rebuilding 20+ cards every tick was needless churn.
        sig = (d.get("generated_at"), len(sources),
               tuple(sorted((k, str(v.get("status")) if isinstance(v, dict) else "")
                            for k, v in sources.items())))
        if sig == self._last_signature:
            return
        self._last_signature = sig
        generated = d.get("generated_at", "—")
        if isinstance(generated, str) and "T" in generated:
            generated = generated.replace("T", " ")[:16]

        # greeting
        hr = datetime.now().hour
        part = ("Good morning" if hr < 12 else
                "Good afternoon" if hr < 18 else "Good evening")
        self._greeting.setText(f"{part}, Bruno")
        # Three honest buckets, not a binary: "unconfigured" means the source
        # is waiting on Bruno (a token, an Authorize click, an optional
        # self-hosted tool) — calling it offline made the app look broken when
        # it wasn't (2026-06-11 master check).
        def _bucket(v) -> str:
            s = str(v.get("status", "")).lower() if isinstance(v, dict) else ""
            if s in ("ok", "alive", "ready"):
                return "ok"
            if s in ("unconfigured", "skip"):
                return "setup"
            return "broken"

        n_ok = sum(1 for v in sources.values() if _bucket(v) == "ok")
        n_setup = sum(1 for v in sources.values() if _bucket(v) == "setup")
        n_bad = len(sources) - n_ok - n_setup
        parts = [f"{n_ok} healthy"]
        if n_setup:
            parts.append(f"{n_setup} awaiting setup (optional/tokens)")
        if n_bad:
            parts.append(f"{n_bad} broken")
        self._subhead.setText(
            f"Last pass {generated}  ·  {len(sources)} sources: " + "  ·  ".join(parts))

        # total items across all sources
        total_items = 0
        for v in sources.values():
            if isinstance(v, dict):
                total_items += int(v.get("total_items") or v.get("count") or 0)

        # stat strip
        while self._stats_grid.count():
            it = self._stats_grid.takeAt(0)
            w = it.widget()
            if w: w.deleteLater()
        ledger = d.get("ledger") or {}
        stats = [
            ("Sources healthy", f"{n_ok}/{len(sources)}",
             _OK if n_bad == 0 else _WARN,
             f"{n_setup} optional/awaiting tokens · {n_bad} broken" if (n_setup or n_bad)
             else "adapters reporting ok"),
            ("Items indexed",   f"{total_items:,}" if total_items else "—", _ACCENT, "across all sources"),
            ("Last pass",       str(generated), _TEXT, f"{d.get('duration_seconds','—')}s"),
            ("MTD tokens",      _fmt_tok(ledger.get("mtd_tokens")), _GOLD,
             f"${ledger.get('mtd_cost_usd','—')}" if ledger.get("mtd_cost_usd") else "this month"),
        ]
        for i, (lbl, val, accent, hint) in enumerate(stats):
            self._stats_grid.addWidget(_stat_card(lbl, val, accent, hint), 0, i)
            self._stats_grid.setColumnStretch(i, 1)

        # source health grid (responsive: 3 cols)
        while self._sources_grid.count():
            it = self._sources_grid.takeAt(0)
            w = it.widget()
            if w: w.deleteLater()
        if not sources:
            empty = QLabel("No source data yet — hit ⚡ Run pass now.")
            empty.setStyleSheet(f"color: {_MUTED}; padding: 12px;")
            self._sources_grid.addWidget(empty, 0, 0)
            return
        cols = 3
        for idx, (name, info) in enumerate(sorted(sources.items())):
            r, c = divmod(idx, cols)
            self._sources_grid.addWidget(_source_card(name, info), r, c)
        for c in range(cols):
            self._sources_grid.setColumnStretch(c, 1)


def _fmt_tok(n) -> str:
    try:
        n = float(n)
    except Exception:
        return "—"
    if n >= 1e9: return f"{n/1e9:.1f}B"
    if n >= 1e6: return f"{n/1e6:.1f}M"
    if n >= 1e3: return f"{n/1e3:.0f}K"
    return f"{int(n)}"


# urllib, not httpx: these hit loopback HTTP only. Each httpx.Client used to
# build a fresh Windows SSL context (~2.8s!) per call — three of them on the
# UI thread during HomePage.__init__ accounted for most of a 17s MainWindow
# construction. urllib on http:// has no SSL setup at all. 2026-06-11 perf.
def _api_get(path: str, timeout: float = 1.5) -> dict | None:
    import json as _json
    import urllib.request
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:8000/api/v1/mind{path}", timeout=timeout) as r:
            if r.status == 200:
                return _json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None
    return None


def _api_post(path: str, payload: dict, timeout: float = 1.5) -> dict | None:
    import json as _json
    import urllib.request
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:8000/api/v1/mind{path}",
            data=_json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status == 200:
                return _json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None
    return None
