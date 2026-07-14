"""Databases — the Notion/Obsidian observatory.

Bruno 2026-06-12: "Databases can be the place with all the minutiae
statistics, high-level visualization and general actions regarding my data
entries on Notion and Obsidian."

v1 surfaces, all loaded async (never on the UI thread):
  • Stat cards — the four bodies of data: unified mind, file index,
    Obsidian vault (Documents/Obsidian Vault), Notion workspace.
  • Mirror drift table — for every source the Notion mirror knows how to
    carry (lib/notion_mirror.SCHEMAS): local items vs rows actually present
    in the mirror DB, with the gap called out. Read-only: this page never
    creates Notion pages/DBs; the mirror itself does that on its own runs.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QFrame,
    QPushButton, QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView, QScrollArea, QProgressBar,
)

ROOT = Path(__file__).resolve().parent.parent.parent
OBSIDIAN_VAULT = Path.home() / "Documents" / "Obsidian Vault"

_TEXT = "#f5f5f7"; _MUTED = "#76767f"; _GOLD = "#ff9f0a"
_OK = "#30d158"; _ERR = "#ff453a"; _BORDER = "#22252a"; _CARD = "#16181c"


def _card(title: str, value: str, hint: str, accent: str = _GOLD) -> QFrame:
    f = QFrame()
    f.setStyleSheet(f"QFrame {{ background: {_CARD}; border: 1px solid "
                    f"{_BORDER}; border-radius: 8px; }}")
    f.setMinimumHeight(92)
    v = QVBoxLayout(f); v.setContentsMargins(16, 12, 16, 12); v.setSpacing(2)
    t = QLabel(title.upper()); t.setStyleSheet(
        f"color: {_MUTED}; font-size: 10px; font-weight: 700; border: none;")
    v.addWidget(t)
    val = QLabel(value); val.setStyleSheet(
        f"color: {accent}; font-size: 22px; font-weight: 700; border: none;")
    v.addWidget(val)
    h = QLabel(hint); h.setStyleSheet(
        f"color: {_MUTED}; font-size: 11px; border: none;")
    h.setWordWrap(True)
    v.addWidget(h)
    v.addStretch(1)
    return f


# ── data gathering (worker thread) ───────────────────────────────────────────
_OBS_CACHE = ROOT / "state" / "obsidian_stats_cache.json"
_OBS_CACHE_TTL_S = 12 * 3600


def _obsidian_stats() -> dict:
    """Vault stats. The vault holds ~880k mirror files, so a full os.walk takes
    MINUTES — and it was re-run on every Databases open, leaving the card blank
    for 3 min each time (Bruno 2026-07-13: 'works only nominally'). Now cached
    with a 12h TTL: the walk runs at most twice a day (still off the UI thread
    via _kick's worker), every other open reads the cache instantly."""
    if not OBSIDIAN_VAULT.is_dir():
        return {"ok": False, "error": "vault not found"}
    # Fresh cache → instant.
    try:
        if _OBS_CACHE.exists() and (time.time() - _OBS_CACHE.stat().st_mtime) < _OBS_CACHE_TTL_S:
            d = json.loads(_OBS_CACHE.read_text(encoding="utf-8"))
            d["cached"] = True
            return d
    except Exception:
        pass
    notes = attachments = 0
    size = 0
    newest = 0.0
    for dirpath, dirnames, filenames in os.walk(OBSIDIAN_VAULT):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            try:
                st = os.stat(p)
            except OSError:
                continue
            size += st.st_size
            newest = max(newest, st.st_mtime)
            if fn.lower().endswith(".md"):
                notes += 1
            else:
                attachments += 1
    out = {"ok": True, "notes": notes, "attachments": attachments,
           "mb": round(size / 1e6), "newest": newest}
    try:
        _OBS_CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _OBS_CACHE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(out), encoding="utf-8")
        os.replace(tmp, _OBS_CACHE)
    except Exception:
        pass
    return out


def _mirror_drift() -> list[dict]:
    """Read-only: local snapshot counts vs rows in each Notion mirror DB."""
    out: list[dict] = []
    try:
        from lib.notion_mirror import (SCHEMAS, _h, EGON_PAGE_ID,
                                       MIRRORS_PAGE_TITLE, _existing_keys)
        from lib.lazy_httpx import httpx
        from lib import cross_search
    except Exception as e:
        return [{"source": "mirror", "local": "", "mirrored": "",
                 "drift": "", "note": f"mirror lib unavailable: {e}"}]

    # find (never create) the Mirrors container and its child DBs
    dbs: dict[str, str] = {}
    try:
        r = httpx.get(
            f"https://api.notion.com/v1/blocks/{EGON_PAGE_ID}/children",
            headers=_h(), timeout=15)
        mirrors_page = None
        for b in (r.json().get("results", []) if r.status_code == 200 else []):
            if (b.get("type") == "child_page" and
                    b.get("child_page", {}).get("title") == MIRRORS_PAGE_TITLE):
                mirrors_page = b["id"]
                break
        if mirrors_page:
            r = httpx.get(
                f"https://api.notion.com/v1/blocks/{mirrors_page}/children",
                headers=_h(), timeout=15)
            for b in (r.json().get("results", []) if r.status_code == 200 else []):
                if b.get("type") == "child_database":
                    dbs[b.get("child_database", {}).get("title", "")] = b["id"]
    except Exception as e:
        return [{"source": "notion", "local": "", "mirrored": "",
                 "drift": "", "note": f"Notion unreachable: {str(e)[:60]}"}]

    for source in sorted(SCHEMAS):
        snap = None
        try:
            snap = cross_search._latest_snapshot_for(source)
        except Exception:
            pass
        local = len((snap or {}).get("items") or [])
        if source not in dbs:
            out.append({"source": source, "local": local, "mirrored": 0,
                        "drift": local,
                        "note": "no mirror DB yet — runs on next mirror pass"})
            continue
        try:
            mirrored = len(_existing_keys(dbs[source]))
        except Exception as e:
            out.append({"source": source, "local": local, "mirrored": "?",
                        "drift": "?", "note": str(e)[:60]})
            continue
        out.append({"source": source, "local": local, "mirrored": mirrored,
                    "drift": max(0, local - mirrored),
                    "note": "in sync ✓" if local <= mirrored else
                            f"{local - mirrored} items not yet mirrored"})
    return out


def _gather() -> dict:
    from egon_app.api import get_json
    mind = get_json("http://127.0.0.1:8000/api/v1/mind/stats", timeout=6.0) or {}
    files_n = 0
    fp = ROOT / "state" / "files_index.jsonl"
    if fp.exists():
        try:
            files_n = sum(1 for _ in fp.open(encoding="utf-8"))
        except Exception:
            pass
    notion = {}
    try:
        from lib.adapters import notion_workspace
        notion = notion_workspace.live_status() or {}
    except Exception:
        pass
    # Progress bar (phase 0) and drift (phase 2) load separately so this cards
    # payload never blocks them.
    return {"mind": mind, "files_n": files_n, "obsidian": _obsidian_stats(),
            "notion": notion}


class DatabasesPage(QWidget):
    _ready = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 18, 24, 12)
        outer.setSpacing(10)

        head = QHBoxLayout()
        title = QLabel("🗄 Databases — Notion · Obsidian · raw stores")
        title.setStyleSheet(f"color: {_TEXT}; font-size: 20px; font-weight: 700;")
        head.addWidget(title)
        self._status = QLabel("loading…")
        self._status.setStyleSheet(f"color: {_MUTED}; font-size: 11px;")
        head.addWidget(self._status)
        head.addStretch(1)
        btn = QPushButton("Refresh")
        btn.clicked.connect(self._kick)
        head.addWidget(btn)
        outer.addLayout(head)

        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget(); scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        v = QVBoxLayout(body); v.setSpacing(14)

        self._cards = QGridLayout(); self._cards.setSpacing(12)
        v.addLayout(self._cards)

        # Notion sync progress — push vs the full corpus (Obsidian counts as the
        # per-source totals). The backfill runs in the background via catchup.
        prog_panel = QFrame()
        prog_panel.setStyleSheet(
            f"QFrame {{ background: #16181c; border: 1px solid {_BORDER}; "
            f"border-radius: 8px; }}")
        pv = QVBoxLayout(prog_panel); pv.setContentsMargins(14, 10, 14, 12); pv.setSpacing(6)
        ph = QHBoxLayout()
        plbl = QLabel("🟦 Notion sync — backfilling in the background")
        plbl.setStyleSheet(f"color: {_TEXT}; font-weight: 600; font-size: 13px;")
        ph.addWidget(plbl); ph.addStretch(1)
        self._prog_pct = QLabel("—")
        self._prog_pct.setStyleSheet(f"color: {_GOLD}; font-weight: 700; font-size: 13px;")
        ph.addWidget(self._prog_pct)
        pv.addLayout(ph)
        self._prog_bar = QProgressBar()
        self._prog_bar.setRange(0, 1000)
        self._prog_bar.setTextVisible(False)
        self._prog_bar.setFixedHeight(10)
        self._prog_bar.setStyleSheet(
            f"QProgressBar {{ background: #0c0d0f; border: 1px solid {_BORDER}; "
            f"border-radius: 5px; }}"
            f"QProgressBar::chunk {{ background: {_GOLD}; border-radius: 5px; }}")
        pv.addWidget(self._prog_bar)
        self._prog_detail = QLabel("loading…")
        self._prog_detail.setStyleSheet(f"color: {_MUTED}; font-size: 11px;")
        self._prog_detail.setWordWrap(True)
        pv.addWidget(self._prog_detail)
        v.addWidget(prog_panel)

        drift_label = QLabel("Mirror drift — what the Notion mirror is missing")
        drift_label.setStyleSheet(f"color: {_TEXT}; font-weight: 600;")
        v.addWidget(drift_label)
        drift_hint = QLabel(
            "Local snapshot items vs rows actually present in each mirror DB "
            "under 050 · Mirrors. Read-only: this page never writes to Notion.")
        drift_hint.setStyleSheet(f"color: {_MUTED}; font-size: 11px;")
        drift_hint.setWordWrap(True)
        v.addWidget(drift_hint)

        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(
            ["Source", "Local items", "Mirrored", "Drift", "Note"])
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setStyleSheet(
            f"QTableWidget {{ background: #0c0d0f; color: {_TEXT}; "
            f"gridline-color: {_BORDER}; border: 1px solid {_BORDER}; "
            f"border-radius: 6px; }}"
            f"QHeaderView::section {{ background: #212328; color: {_MUTED}; "
            f"padding: 6px; border: none; font-weight: 600; }}")
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setMinimumHeight(260)
        v.addWidget(self._table)
        v.addStretch(1)

        self._ready.connect(self._render)
        self._kick()

    def _kick(self) -> None:
        self._status.setText("loading… (Notion drift can take ~20s)")
        import threading

        def _bg():
            # Phase 0: progress bar — LOCAL only (mirror state + cached obsidian
            # counts), no network, so it paints instantly regardless of Notion.
            try:
                from lib import mirror_runner
                self._ready.emit({"progress": mirror_runner.status()})
            except Exception:
                pass
            # Phase 1: cards (includes a Notion live_status network call).
            try:
                self._ready.emit(_gather())
            except Exception as e:
                self._ready.emit({"error": str(e)[:200]})
            # Phase 2: mirror drift (slow Notion network scan).
            try:
                self._ready.emit({"drift": _mirror_drift()})
            except Exception as e:
                self._ready.emit({"drift": [{"source": "notion", "local": "",
                    "mirrored": "", "drift": "", "note": f"drift failed: {str(e)[:60]}"}]})

        threading.Thread(target=_bg, daemon=True, name="db-observatory").start()

    def _render(self, d: dict) -> None:
        # Key-driven: each phase updates only its own section, so a slow/failed
        # phase never blanks the others.
        if d.get("error"):
            self._status.setText(f"load failed: {d['error']}")
            return

        if "progress" in d:                       # phase 0 — local, instant
            prog = d.get("progress") or {}
            if prog.get("notion_total_items"):
                pct = prog.get("notion_pct", 0.0)
                self._prog_bar.setValue(int(pct * 10))
                self._prog_pct.setText(f"{pct}%")
                top = sorted(prog.get("notion_per_source", {}).items(),
                             key=lambda kv: kv[1]["total"] - kv[1]["pushed"], reverse=True)
                biggest = ", ".join(
                    f"{s} {v['pushed']:,}/{v['total']:,}" for s, v in top[:3])
                self._prog_detail.setText(
                    f"{prog['notion_total_pushed']:,} / {prog['notion_total_items']:,} pages · "
                    f"{prog['notion_remaining']:,} to go · biggest remaining: {biggest}")
            else:
                self._prog_pct.setText("—")
                self._prog_detail.setText("no mirror progress data yet")

        if "mind" in d:                            # phase 1 — cards
            self._status.setText("loading Notion drift (~20s)…")
            while self._cards.count():
                it = self._cards.takeAt(0)
                if it.widget():
                    it.widget().deleteLater()
            mind = d.get("mind") or {}
            ob = d.get("obsidian") or {}
            notion = d.get("notion") or {}
            cards = [
                ("🌐 Unified mind",
                 f"{mind.get('activity', 0):,}",
                 f"activity rows · {mind.get('memory', 0)} memories · "
                 f"{mind.get('sessions', 0)} sessions · {mind.get('projects', 0)} projects",
                 _GOLD),
                ("📁 File index",
                 f"{d.get('files_n', 0):,}",
                 "files across PC + Drive (+ phone on demand)", "#ff453a"),
                ("🟣 Obsidian vault",
                 f"{ob.get('notes', 0):,}" if ob.get("ok") else "—",
                 (f"notes · {ob.get('attachments', 0)} attachments · "
                  f"{ob.get('mb', 0)} MB") if ob.get("ok")
                 else f"vault: {ob.get('error', '?')}", "#9D7BD8"),
                ("🟦 Notion",
                 "online" if notion.get("status") == "ok" else
                 str(notion.get("status", "—")),
                 notion.get("error") or "workspace API reachable; mirror drift below",
                 _OK if notion.get("status") == "ok" else _ERR),
            ]
            for i, (t, val, hint, accent) in enumerate(cards):
                self._cards.addWidget(_card(t, val, hint, accent), 0, i)
                self._cards.setColumnStretch(i, 1)
            if self._table.rowCount() == 0:
                self._table.setRowCount(1)
                self._table.setItem(0, 0, QTableWidgetItem(
                    "loading mirror drift from Notion (~20s)…"))
                for c in range(1, 5):
                    self._table.setItem(0, c, QTableWidgetItem(""))

        if "drift" in d:                           # phase 2 — slow network
            self._fill_drift(d.get("drift") or [])
            self._status.setText(time.strftime("updated %H:%M", time.localtime()))

    def _fill_drift(self, drift: list) -> None:
        self._table.setRowCount(0)
        for i, row in enumerate(drift):
            self._table.insertRow(i)
            vals = [str(row.get("source", "")), str(row.get("local", "")),
                    str(row.get("mirrored", "")), str(row.get("drift", "")),
                    str(row.get("note", ""))]
            for c, val in enumerate(vals):
                item = QTableWidgetItem(val)
                if c == 3 and str(row.get("drift")) not in ("0", "", "?"):
                    item.setForeground(Qt.GlobalColor.yellow)
                self._table.setItem(i, c, item)
