"""Home view — what tonight's agent pass produced."""
from __future__ import annotations

from html import escape

from nicegui import ui

from lib.actions import trigger_pass


def _act(kind: str):
    def _go():
        ok, msg = trigger_pass(kind=kind)
        ui.notify(msg, type="positive" if ok else "negative")
    return _go


def _spark(points: list[float], color: str = "var(--accent)") -> str:
    if not points:
        return ""
    lo, hi = min(points), max(points)
    rng = (hi - lo) or 1.0
    n = len(points)
    coords = " ".join(f"{i*100/(n-1):.1f},{30-(p-lo)/rng*22-4:.1f}" for i, p in enumerate(points))
    return (
        f'<svg viewBox="0 0 100 30" preserveAspectRatio="none" style="width:100%; height:30px;">'
        f'<polyline fill="none" stroke="{color}" stroke-width="1.6" points="{coords}"/></svg>'
    )


def _delta(n: float | int | None, suffix: str = " since yesterday") -> str:
    if n is None:
        n = 0
    if n > 0:
        return f'<div class="delta up">▲ {n}{suffix}</div>'
    if n < 0:
        return f'<div class="delta dn">▼ {abs(n)}{suffix}</div>'
    return f'<div class="delta flat">flat{suffix}</div>'


def _conf_chip(c: float) -> str:
    cls = "sug" if c >= 0.85 else ("warn" if c < 0.80 else "")
    return f'<span class="chip {cls}">{c:.2f}</span>'


def render(data: dict) -> None:
    ui.html('<h1 class="page">Home</h1>')
    ui.html('<p class="page-sub">All sources, all queues, all status — at a glance.</p>')

    # ---- service status pills ----
    statuses = data.get("service_status", [])
    if statuses:
        html = ""
        for s in statuses:
            dot = "" if s.get("ok") else "warn"
            note = f" · {escape(s['note'])}" if s.get("note") else ""
            html += (
                f'<span class="status-pill"><span class="dot {dot}"></span>'
                f'{escape(s["name"])}{note}</span>'
            )
        ui.html(f'<div style="margin-bottom:18px;">{html}</div>')

    # ---- KPI cards ----
    src = data.get("sources", {})
    rt = src.get("routster", {})
    ni = src.get("notion", {})
    mu = src.get("mouseion", {})
    vm = src.get("vault", {})

    cards = [
        ("Routster queue",   rt.get("queue_count", "—"),       _delta(rt.get("delta_24h", 0)),                rt.get("spark_7d", [])),
        ("Notion 001-Inbox", ni.get("queue_count", "—"),       _delta(ni.get("delta_24h", 0)),                ni.get("spark_7d", [])),
        ("Mouseion refs",    f'{mu.get("queue_count", 0):,}',  _delta(mu.get("delta_24h", 0), " today"),      mu.get("spark_7d", [])),
        ("Vault mirror",     vm.get("pages_mirrored", "—"),    f'<div class="delta flat">clean · {escape((vm.get("last_run_iso") or "")[11:16])} ✓</div>', None),
    ]
    grid = '<div style="display:grid; grid-template-columns: repeat(4, 1fr); gap:14px; margin-bottom:24px;">'
    for lbl, val, delta, spark in cards:
        spark_html = _spark(spark) if spark else ""
        grid += (
            f'<div class="kpi"><div class="lbl">{escape(lbl)}</div>'
            f'<div class="val">{val}</div>{delta}{spark_html}</div>'
        )
    grid += "</div>"
    ui.html(grid)

    # ---- two-col: inbox preview + digest ----
    with ui.element("div").style("display:grid; grid-template-columns: 2fr 1fr; gap:14px;"):
        # inbox preview
        ui.html(_inbox_panel(data.get("inbox_preview", [])))
        # digest
        ui.html(_digest_panel(data.get("digest_bullets", [])))

    # ---- action row ----
    ui.html('<div style="margin-top: 18px;"></div>')
    with ui.row().style("gap: 8px;"):
        ui.button("Process inbox",   on_click=_act("inbox")
                  ).props("unelevated").style("background:var(--accent); color:white;")
        ui.button("Run mirror now",  on_click=_act("mirror")
                  ).props("unelevated outline").style("color:var(--text-2); border:1px solid var(--border);")
        ui.button("Open Notion 🏠",  on_click=lambda: ui.run_javascript(
                  'window.open("https://www.notion.so/35893daa921581dfa7e0ce655e2613d0","_blank")')
                  ).props("unelevated outline").style("color:var(--text-2); border:1px solid var(--border);")
        ui.button("Search vault",    on_click=lambda: ui.navigate.to("/search")
                  ).props("unelevated outline").style("color:var(--text-2); border:1px solid var(--border);")


def _inbox_panel(items: list[dict]) -> str:
    rows = ""
    for it in items:
        rows += (
            "<tr>"
            f'<td><span class="chip">{escape(it["source"])}</span></td>'
            f'<td>{escape(it["title"])}</td>'
            f'<td>{escape(it["suggested_target"])}</td>'
            f'<td>{_conf_chip(it["confidence"])}</td>'
            f'<td><button class="q-btn q-btn--flat" style="font-size:14px; padding:0 8px;">✓</button>'
            f'<button class="q-btn q-btn--flat" style="font-size:14px; padding:0 8px;">↗</button></td>'
            "</tr>"
        )
    return f"""
    <div class="panel">
      <div class="phead"><span class="ttl">Inbox preview · agent suggestions</span>
                          <span class="lnk">View all →</span></div>
      <div class="pbody flush">
        <table class="stbl">
          <thead><tr><th>Source</th><th>Title</th><th>Suggested target</th><th>Conf.</th><th></th></tr></thead>
          <tbody>{rows}</tbody>
        </table>
      </div>
    </div>
    """


def _digest_panel(bullets: list[str]) -> str:
    inner = "".join(f'<div class="digest-bullet"><span class="dot"></span><div>{escape(b)}</div></div>'
                    for b in bullets)
    return f"""
    <div class="panel">
      <div class="phead"><span class="ttl">Today's digest</span>
                          <span class="lnk">history →</span></div>
      <div class="pbody flush">{inner}</div>
    </div>
    """
