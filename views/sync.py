"""Sync — scheduled tasks, last runs, log tails, re-trigger buttons."""
from __future__ import annotations

import subprocess
from html import escape
from pathlib import Path

from nicegui import ui

from lib.actions import trigger_pass
from views._async import lazy_panel

EGON_LOG = Path(__file__).resolve().parent.parent / "logs"


def _scheduled_tasks() -> list[dict]:
    """Query Windows Task Scheduler for KMS-* tasks."""
    try:
        res = subprocess.run(
            ["schtasks", "/Query", "/FO", "CSV", "/V"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
        )
        out: list[dict] = []
        if res.returncode != 0:
            return out
        lines = [l for l in res.stdout.splitlines() if "KMS-" in l]
        # CSV w/ many fields: [Folder, TaskName, NextRunTime, Status, ...]
        for line in lines:
            cells = [c.strip('"') for c in line.split('","')]
            if len(cells) < 4:
                continue
            name = cells[1].lstrip("\\")
            if not name.startswith("KMS-"):
                continue
            out.append({
                "name": name,
                "next_run": cells[2],
                "status": cells[3],
            })
        # de-dup by name
        seen, uniq = set(), []
        for t in out:
            if t["name"] in seen:
                continue
            seen.add(t["name"])
            uniq.append(t)
        return uniq
    except Exception:
        return []


def _log_tail(path: Path, n: int = 30) -> str:
    if not path.exists():
        return "(no log yet)"
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return "".join(lines[-n:])
    except Exception as e:
        return f"(error reading log: {e})"


def render(data: dict) -> None:
    ui.html('<h1 class="page">Sync</h1>')
    ui.html('<p class="page-sub">Scheduled jobs · last runs · log tails · re-trigger.</p>')

    # ---- scheduled tasks (lazy: schtasks subprocess can take ~5-10s) ----
    def _render_tasks(tasks: list[dict]) -> None:
        rows = ""
        for t in tasks:
            rows += (
                "<tr>"
                f'<td><b style="color:var(--text);">{escape(t["name"])}</b></td>'
                f'<td>{escape(t["next_run"])}</td>'
                f'<td><span class="chip {"sug" if t["status"]=="Ready" else "warn"}">'
                f'{escape(t["status"])}</span></td>'
                "</tr>"
            )
        if not rows:
            rows = '<tr><td colspan="3" style="color:var(--muted); padding:14px;">No KMS-* scheduled tasks found.</td></tr>'
        ui.html(f"""
        <div class="panel" style="margin-bottom:14px;">
          <div class="phead"><span class="ttl">Windows Scheduled Tasks · KMS-*</span></div>
          <div class="pbody flush">
            <table class="stbl">
              <thead><tr><th>Task</th><th>Next run</th><th>Status</th></tr></thead>
              <tbody>{rows}</tbody>
            </table>
          </div>
        </div>
        """)
    lazy_panel(_scheduled_tasks, _render_tasks)

    # ---- recent pass log (lazy: file read should be fast, but keep parallel) ----
    from datetime import datetime
    ym = datetime.now().strftime("%Y-%m")
    log_path = EGON_LOG / f"pass-{ym}.log"

    def _render_log(tail: str) -> None:
        ui.html(f"""
        <div class="panel" style="margin-bottom:14px;">
          <div class="phead"><span class="ttl">Last 30 lines · {escape(log_path.name)}</span></div>
          <div class="pbody">
            <pre style="font-family: 'JetBrains Mono', 'Cascadia Code', monospace; font-size: 12px;
                        background: var(--panel-2); padding: 12px; border-radius: 4px;
                        color: var(--text-2); max-height: 320px; overflow: auto; margin: 0;
                        white-space: pre-wrap; word-break: break-word;">{escape(tail)}</pre>
          </div>
        </div>
        """)
    lazy_panel(lambda: _log_tail(log_path, 30), _render_log)

    # ---- triggers ----
    ui.html('<div class="panel"><div class="phead"><span class="ttl">Manual triggers</span></div>'
            '<div class="pbody"></div></div>')
    with ui.row().style("gap: 8px; margin-top: 14px;"):
        ui.button("Run daily pass now",
                  on_click=lambda: (trigger_pass("daily"), ui.notify("daily pass queued"))
                  ).props("unelevated").style("background:var(--accent); color:white;")
        ui.button("Run notion→vault mirror",
                  on_click=lambda: (trigger_pass("mirror"), ui.notify("mirror queued"))
                  ).props("unelevated outline").style("color:var(--text-2); border:1px solid var(--border);")
        ui.button("Inbox-only pass",
                  on_click=lambda: (trigger_pass("inbox"), ui.notify("inbox pass queued"))
                  ).props("unelevated outline").style("color:var(--text-2); border:1px solid var(--border);")
