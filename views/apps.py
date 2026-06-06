"""Apps view — orchestrate Bruno's own apps from a single interface.

One row per app: live status · open UI · trigger common actions.
Every triggered action notifies in-place and logs to logs/actions-YYYY-MM.jsonl.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from html import escape
from pathlib import Path

from nicegui import ui

from lib.orchestrator import APPS
from views._async import lazy_panel

ACTIONS_LOG = Path(__file__).resolve().parent.parent / "logs" / f"actions-{datetime.now():%Y-%m}.jsonl"
log = logging.getLogger("egon.apps")


def _log_action(app_id: str, action: str, ok: bool, detail: str) -> None:
    ACTIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts": datetime.now().isoformat(), "app": app_id, "action": action,
             "ok": ok, "detail": detail}
    with ACTIONS_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def render(data: dict, **_) -> None:
    ui.html('<h1 class="page">Apps</h1>')
    ui.html('<p class="page-sub">Single orchestrator for your own apps — Panop · Mouseion · Routster. '
            'All run locally; Egon never sends data anywhere else.</p>')

    # Each app card lazy-loads its own status probe so the 3 apps probe in parallel
    # rather than serially (was: ~5s × 3 = 15s+ in render()).
    for app in APPS:
        def _load(app=app):
            return {"st": app["status"](), "actions": app["actions"]()}
        def _render(payload, app=app):
            _render_app_card(app, payload["st"], payload["actions"])
        lazy_panel(_load, _render)

    # action audit log preview
    if ACTIONS_LOG.exists():
        try:
            tail = ACTIONS_LOG.read_text(encoding="utf-8").splitlines()[-10:]
            tail_html = "\n".join(escape(t) for t in tail)
            ui.html(f"""
            <div class="panel" style="margin-top: 18px;">
              <div class="phead"><span class="ttl">Recent actions · last 10</span>
                                  <span class="lnk">{escape(ACTIONS_LOG.name)}</span></div>
              <div class="pbody">
                <pre style="font-family: monospace; font-size: 11px; max-height: 240px;
                            overflow: auto; margin: 0; color: var(--text-2);">{tail_html}</pre>
              </div>
            </div>
            """)
        except Exception:
            pass


def _render_app_card(app: dict, st: dict, actions: dict) -> None:
    running = st.get("running", False)
    icon = st["icon"]; label = st["label"]
    chip = '<span class="chip sug">running</span>' if running else '<span class="chip warn">offline</span>'

    with ui.card().style("padding: 14px 18px; margin-bottom: 12px;"):
        with ui.row().style("align-items: center; gap: 10px; margin-bottom: 6px; flex-wrap: wrap;"):
            ui.html(f'<span style="font-size: 22px;">{icon}</span>')
            ui.label(label).style("font-weight: 600; font-size: 15px; color: var(--text);")
            ui.html(chip)
            ui.html(f'<span style="color: var(--muted); font-size: 12px;">:{st["port"]} · {escape(st.get("detail","?"))}</span>')
            ui.space()

        install = st.get("install")
        if install:
            from_hint = st.get("detected_from")
            ui.html(
                f'<div style="font-size: 11px; color: var(--muted); margin-bottom: 8px;">'
                f'📁 <code style="font-size: 11px;">{escape(install)}</code>'
                + (f' · port auto-detected from <code style="font-size: 11px;">{escape(from_hint)[-50:]}</code>'
                   if from_hint else ' · port: default')
                + '</div>'
            )
        else:
            ui.html('<div style="font-size: 11px; color: var(--danger); margin-bottom: 8px;">install path not found</div>')

        with ui.row().style("gap: 8px; flex-wrap: wrap;"):
            for action_id, (label_text, fn) in actions.items():
                def _make(fn=fn, action_id=action_id, app_id=st["id"]):
                    def _go():
                        try:
                            result = fn()
                            if isinstance(result, tuple):
                                ok, detail = result
                            else:
                                ok, detail = True, str(result)
                            _log_action(app_id, action_id, ok, detail)
                            ui.notify(detail, type="positive" if ok else "negative")
                        except Exception as e:
                            _log_action(app_id, action_id, False, str(e))
                            ui.notify(f"✗ {e}", type="negative")
                    return _go
                ui.button(label_text, on_click=_make()).props("unelevated outline dense").style(
                    "color: var(--text-2); border: 1px solid var(--border);"
                )
