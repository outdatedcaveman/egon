"""Unified Inbox — Routster + Notion + Vault items in one queue."""
from __future__ import annotations

from html import escape

from nicegui import ui

from lib.actions import trigger_pass


def render(data: dict) -> None:
    ui.html('<h1 class="page">Inbox</h1>')
    ui.html('<p class="page-sub">Unified queue across Routster · Panop · Mouseion · Notion. '
            'Agent suggestions in green; review the warns.</p>')

    items = data.get("inbox_preview", [])
    src = data.get("sources", {})

    # ---- counts strip ----
    rt = src.get("routster", {})
    nt = src.get("notion", {})
    vm = src.get("vault", {})
    pills = [
        ("Routster",      rt.get("queue_count", "—"), rt.get("status") == "ok"),
        ("Notion Inbox",  nt.get("queue_count", "—"), nt.get("status") == "ok"),
        ("Vault 001-Inbox", vm.get("inbox_count", "—"), vm.get("status") == "ok"),
        ("Mouseion dupes", src.get("mouseion", {}).get("duplicates_flagged", "—"),
         src.get("mouseion", {}).get("status") == "ok"),
    ]
    pill_html = "".join(
        f'<span class="status-pill"><span class="dot {("" if ok else "warn")}"></span>'
        f'<b style="color:var(--text);">{val}</b>&nbsp;{escape(name)}</span>'
        for name, val, ok in pills
    )
    ui.html(f'<div style="margin-bottom:18px;">{pill_html}</div>')

    if not items:
        ui.html('<div class="panel"><div class="pbody"><p style="color:var(--muted);">'
                'Empty queue. The agent will repopulate at 23:00.</p></div></div>')
        return

    rows = ""
    for it in items:
        c = it["confidence"]
        chip_cls = "sug" if c >= 0.85 else ("warn" if c < 0.80 else "")
        rows += (
            "<tr>"
            f'<td><span class="chip">{escape(it["source"])}</span></td>'
            f'<td>{escape(it["title"])}</td>'
            f'<td style="color:var(--muted);">{escape(it["age"])}</td>'
            f'<td>{escape(it["suggested_target"])}</td>'
            f'<td><span class="chip {chip_cls}">{c:.2f}</span></td>'
            '<td style="white-space:nowrap;">'
              '<button class="q-btn q-btn--flat" title="Apply suggestion" '
                      'style="font-size:14px; padding:0 8px;">✓</button>'
              '<button class="q-btn q-btn--flat" title="Open external" '
                      'style="font-size:14px; padding:0 8px;">↗</button>'
              '<button class="q-btn q-btn--flat" title="Re-classify" '
                      'style="font-size:14px; padding:0 8px;">↻</button>'
            '</td></tr>'
        )

    ui.html(f"""
    <div class="panel">
      <div class="phead">
        <span class="ttl">Top {len(items)} items · agent suggestions</span>
        <span class="lnk">load full queue →</span>
      </div>
      <div class="pbody flush">
        <table class="stbl">
          <thead><tr>
            <th>Source</th><th>Title</th><th>Age</th>
            <th>Suggested target</th><th>Conf.</th><th></th>
          </tr></thead>
          <tbody>{rows}</tbody>
        </table>
      </div>
    </div>
    """)

    # actions
    ui.html('<div style="margin-top:18px;"></div>')
    with ui.row().style("gap: 8px;"):
        ui.button("Re-classify all",
                  on_click=lambda: (trigger_pass("inbox"), ui.notify("inbox pass queued"))
                  ).props("unelevated").style("background:var(--accent); color:white;")
        ui.button("Apply all confident (≥0.90)",
                  on_click=lambda: ui.notify("bulk-apply (stub — agent will action this)")
                  ).props("unelevated outline").style("color:var(--text-2); border:1px solid var(--border);")
