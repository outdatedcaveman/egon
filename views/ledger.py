"""💰 Token Ledger — Pro/Max/API plan-aware rendering.

In `pro`/`max` modes the headline is **tokens**; $$ is shown as "API equivalent" / cache savings.
In `api` mode $$ is the headline.
"""
from __future__ import annotations

from html import escape

from nicegui import ui

from lib.ledger import compute_ledger, load_config


# -- helpers ------------------------------------------------------------------

def _fmt_tokens(n: float | int) -> str:
    if n is None: return "—"
    if n >= 1_000_000_000: return f"{n/1_000_000_000:.2f}B"
    if n >= 1_000_000:     return f"{n/1_000_000:.2f}M"
    if n >= 1_000:         return f"{n/1_000:.0f}K"
    return f"{int(n):,}"


def _fmt_money(n: float | int) -> str:
    if n is None: return "—"
    if n >= 1000: return f"${n:,.0f}"
    return f"${n:,.2f}"


def _spark(points: list[float], color: str) -> str:
    if not points or all(p == 0 for p in points):
        return ""
    lo, hi = min(points), max(points)
    rng = (hi - lo) or 1.0
    n = len(points)
    coords = " ".join(f"{i*100/(n-1):.1f},{30-(p-lo)/rng*22-4:.1f}" for i, p in enumerate(points))
    return (
        f'<svg viewBox="0 0 100 30" preserveAspectRatio="none" style="width:100%; height:28px;">'
        f'<polyline fill="none" stroke="{color}" stroke-width="1.6" points="{coords}"/></svg>'
    )


def _stacked_area(stack: dict) -> str:
    layers = [
        ("cache_reads",  "var(--ledger-stack-1, #fde68a)"),
        ("cache_writes", "var(--ledger-stack-2, #f59e0b)"),
        ("input",        "var(--ledger-stack-3, #475569)"),
        ("output",       "var(--ledger-stack-4, #262730)"),
    ]
    series = {k: stack.get(k, []) for k, _ in layers}
    n = len(series.get("cache_reads", []))
    if n == 0:
        return '<div style="color:var(--muted, #9ca3af); padding: 30px;">no data in range</div>'

    totals = [sum(series[k][i] for k, _ in layers) for i in range(n)]
    max_total = max(totals) or 1.0
    W, H, BTM = 600, 220, 200

    def y_of(v: float) -> float:
        return BTM - (v / max_total) * (BTM - 10)

    paths = []
    cum_below = [0.0] * n
    for key, fill in layers:
        cum_top = [cum_below[i] + series[key][i] for i in range(n)]
        top_pts = [(i * W / max(n - 1, 1), y_of(cum_top[i])) for i in range(n)]
        bot_pts = [(i * W / max(n - 1, 1), y_of(cum_below[i])) for i in range(n - 1, -1, -1)]
        d = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in (top_pts + bot_pts)) + " Z"
        paths.append(f'<path d="{d}" fill="{fill}" />')
        cum_below = cum_top

    grid = "".join(
        f'<line x1="0" y1="{BTM*pct/100:.0f}" x2="{W}" y2="{BTM*pct/100:.0f}" '
        f'stroke="var(--border-soft, #f0f2f6)" stroke-width="1"/>'
        for pct in (25, 50, 75)
    )
    labels = stack.get("labels", [])
    label_html = ""
    if labels:
        for i, lab in enumerate(labels):
            x = i * W / (len(labels) - 1) if len(labels) > 1 else 0
            anchor = "start" if i == 0 else ("end" if i == len(labels) - 1 else "middle")
            label_html += (
                f'<text x="{x:.0f}" y="{H-5}" font-size="9" fill="var(--muted, #9ca3af)" '
                f'text-anchor="{anchor}">{escape(lab)}</text>'
            )

    return (
        f'<div style="height:240px;"><svg viewBox="0 0 {W} {H}" preserveAspectRatio="none" '
        f'style="width:100%; height:100%;">{grid}{"".join(paths)}{label_html}</svg></div>'
    )


# -- render -------------------------------------------------------------------

def render(data: dict, range_key: str = "30d") -> None:
    cfg = load_config()
    plan = cfg.get("plan_mode", "pro")

    # Cached at module level — only re-parses JSONLs when their mtime changes.
    L = compute_ledger(plan_mode=plan, range_key=range_key)

    is_pro = plan in ("pro", "max")
    v = L.get("verification", {})

    # ---- header ----
    plan_chip = {
        "pro":  '<span class="chip" style="background:#dcfce7;color:#166534;">PRO PLAN</span>',
        "max":  '<span class="chip" style="background:#ede9fe;color:#6d28d9;">MAX PLAN</span>',
        "api":  '<span class="chip" style="background:#fef3c7;color:#b45309;">API METERED</span>',
    }.get(plan, "")
    last_turn = (v.get("last_turn_iso") or "—")[:16].replace("T", " ")
    ui.html(
        '<h1 class="page">💰 Token Ledger '
        f'{plan_chip} <span class="badge">live</span></h1>'
        f'<p class="page-sub">'
        f'Parsed <b>{v.get("files_parsed","—")}</b> session files · '
        f'<b>{v.get("total_turns_ever",0):,}</b> assistant turns · '
        f'<b>{v.get("sessions_ever","—")}</b> sessions · '
        f'last turn <b>{escape(last_turn)}</b> UTC · '
        f'scope: top KPIs = today / MTD (fixed) ·&nbsp; chart + tables = '
        f'<b>{escape(range_key)}</b> filter below.'
        '</p>'
    )

    # ---- anomaly flag ----
    a = L.get("anomaly")
    if a:
        ui.html(
            '<div class="flag"><span style="font-size:18px;">⚠</span>'
            f'<div><b>{escape(a["headline"])}</b> '
            f'{escape(a.get("driver",""))} '
            f'<i>Suggested:</i> {escape(a.get("suggestion",""))}'
            ' <span style="color:var(--ledger-txt-soft, #a16207);">— agent</span></div></div>'
        )

    today_cost = L.get("today_cost_usd", 0)
    today_tok  = L.get("today_tokens", 0)
    mtd_cost   = L.get("mtd_cost_usd", 0)
    mtd_tok    = L.get("mtd_tokens", 0)
    burn       = L.get("burn_rate_per_hour", 0)
    burn_turns = L.get("burn_rate_24h_turns", 0)
    hit        = L.get("cache_hit_ratio", 0)
    proj       = L.get("projection", {})
    plan_b     = L.get("plan_budget", {})
    saved_usd  = L.get("mtd_saved_usd", 0)
    saved_pct  = L.get("mtd_cache_savings_pct", 0)

    # ---- KPI strip — all five cards explicitly scoped ----
    vs = plan_b.get("vs_last_month_pct")
    vs_html = (
        f'<div class="delta {"up" if (vs or 0) > 0 else "dn"}">'
        f'{"▲" if (vs or 0) > 0 else "▼"} {abs(vs or 0)}% vs last month '
        f'(same day)</div>'
    ) if vs is not None else ''

    if is_pro:
        hero_lbl   = "Tokens · today"
        hero_val   = _fmt_tokens(today_tok)
        hero_sub   = f"≈ {_fmt_money(today_cost)} if on the API · you pay $0 (Pro)"
        c2_lbl     = "Tokens · this month (MTD)"
        c2_val     = _fmt_tokens(mtd_tok)
        c2_extra   = vs_html
        c3_lbl     = "Cache saved · MTD"
        c3_val     = _fmt_money(saved_usd)
        c3_val_color = "var(--success)"
        c3_sub     = f"{saved_pct}% off no-cache · matches MTD scope"
    else:
        hero_lbl   = "Spend · today"
        hero_val   = _fmt_money(today_cost)
        hero_sub   = f"{_fmt_tokens(today_tok)} tokens"
        c2_lbl     = "Spend · this month (MTD)"
        c2_val     = _fmt_money(mtd_cost)
        c2_extra   = f'<div class="sub">{_fmt_tokens(mtd_tok)} tokens</div>{vs_html}'
        c3_lbl     = "Cache saved · MTD"
        c3_val     = _fmt_money(saved_usd)
        c3_val_color = "var(--success)"
        c3_sub     = f"{saved_pct}% off no-cache"

    ui.html(f"""
    <div style="display:grid; grid-template-columns: 1.3fr 1fr 1fr 1fr 1fr; gap:12px; margin-bottom:18px;">
      <div class="kpi hero">
        <div class="lbl">{hero_lbl}</div>
        <div class="val">{hero_val}</div>
        <div class="sub">{hero_sub}</div>
      </div>
      <div class="kpi">
        <div class="lbl">{c2_lbl}</div>
        <div class="val">{c2_val}</div>
        {c2_extra}
      </div>
      <div class="kpi">
        <div class="lbl">{c3_lbl}</div>
        <div class="val" style="color:{c3_val_color};">{c3_val}</div>
        <div class="sub">{c3_sub}</div>
      </div>
      <div class="kpi">
        <div class="lbl">Burn rate · rolling 24h</div>
        <div class="val">{_fmt_tokens(burn)}/hr</div>
        <div class="sub">{burn_turns} turns in last 24h</div>
      </div>
      <div class="kpi">
        <div class="lbl">Cache hit · today</div>
        <div class="val">{hit*100:.0f}%</div>
      </div>
    </div>
    """)

    # ---- range selector (real links) ----
    range_buttons_html = '<span class="rng" style="display:flex; gap:4px; font-size:11px;">'
    for r, lbl in (("24h","24h"),("7d","7d"),("30d","30d"),("90d","90d"),("ytd","YTD"),("all","all")):
        sel = "background:var(--ledger-bg,#f59e0b22); color:var(--ledger-txt,#b45309); font-weight:600;" if r == range_key else "color:var(--muted,#6b7280);"
        range_buttons_html += (
            f'<a href="/ledger?range={r}" style="padding:3px 9px; border-radius:4px; '
            f'cursor:pointer; text-decoration:none; {sel}">{lbl}</a>'
        )
    range_buttons_html += '</span>'

    # ---- chart + side panel ----
    stack = L.get("stacked_30d", {})
    legend_html = (
        '<div style="display:flex; gap:14px; font-size:11px; color:var(--muted,#6b7280); '
        'margin-top:10px; flex-wrap:wrap;">'
        '<span><i style="width:10px;height:10px;border-radius:2px;background:#fde68a;display:inline-block;"></i>'
        ' &nbsp;Cache reads (cheapest, ~10× cheaper)</span>'
        '<span><i style="width:10px;height:10px;border-radius:2px;background:#f59e0b;display:inline-block;"></i>'
        ' &nbsp;Cache writes</span>'
        '<span><i style="width:10px;height:10px;border-radius:2px;background:#475569;display:inline-block;"></i>'
        ' &nbsp;Input</span>'
        '<span><i style="width:10px;height:10px;border-radius:2px;background:#262730;display:inline-block;"></i>'
        ' &nbsp;Output (5× input)</span>'
        '</div>'
    )
    proj_html = ""
    if proj:
        if is_pro:
            proj_html = f"""
            <div style="display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-top:12px;
                        padding-top:12px; border-top:1px solid var(--border,#e1e4e8);">
              <div><div style="color:var(--muted,#6b7280); font-size:12px;">Projected month-end tokens</div>
                   <div style="color:var(--text,#262730); font-weight:600; font-size:14px; margin-top:2px;">
                     {_fmt_tokens(proj.get('month_end_tokens',0))} tokens
                   </div></div>
              <div><div style="color:var(--muted,#6b7280); font-size:12px;">If you were on the API instead</div>
                   <div style="color:var(--text,#262730); font-weight:600; font-size:14px; margin-top:2px;">
                     {_fmt_money(proj.get('month_end_cost_usd',0))} this month · cache saves {proj.get('cache_savings_pct',0)}%
                   </div></div>
            </div>
            """
        else:
            proj_html = f"""
            <div style="display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-top:12px;
                        padding-top:12px; border-top:1px solid var(--border,#e1e4e8);">
              <div><div style="color:var(--muted,#6b7280); font-size:12px;">Projected month-end at current burn</div>
                   <div style="color:var(--text,#262730); font-weight:600; font-size:14px; margin-top:2px;">
                     {_fmt_money(proj.get('month_end_cost_usd',0))} · {_fmt_tokens(proj.get('month_end_tokens',0))} tokens
                   </div></div>
              <div><div style="color:var(--muted,#6b7280); font-size:12px;">Without cache (counterfactual)</div>
                   <div style="color:var(--text,#262730); font-weight:600; font-size:14px; margin-top:2px;">
                     {_fmt_money(proj.get('without_cache_usd',0))} · cache saves {proj.get('cache_savings_pct',0)}%
                   </div></div>
            </div>
            """

    chart_block = f"""
    <div class="panel">
      <div class="phead">
        <span class="ttl">Token usage · stacked · {escape(range_key)}</span>
        {range_buttons_html}
      </div>
      <div class="pbody">{_stacked_area(stack)}{legend_html}{proj_html}</div>
    </div>
    """

    # by-model + by-project
    bm = L.get("by_model", [])
    bp = L.get("by_project", [])
    model_color = {"opus": "#6d28d9", "sonnet": "#1d4ed8", "haiku": "#166534"}
    bm_rows = ""
    show_field = "tokens" if is_pro else "cost_usd"
    for m in bm:
        c = model_color.get(m["code"], "#6b7280")
        val = _fmt_tokens(m["tokens"]) if is_pro else _fmt_money(m["cost_usd"])
        bm_rows += (
            f'<div class="bar-row">'
            f'<span class="lbl"><span class="chip {m["code"]}">{escape(m["model"])}</span></span>'
            f'<span class="bar"><i style="width:{m["share"]*100:.0f}%; background:{c};"></i></span>'
            f'<span class="v"><b>{val}</b></span></div>'
        )
    bp_rows = ""
    for p in bp:
        op = max(0.3, min(p["share"] * 1.6, 1.0))
        val = _fmt_tokens(p["tokens"]) if is_pro else _fmt_money(p["cost_usd"])
        bp_rows += (
            f'<div class="bar-row">'
            f'<span class="lbl">{escape(p["project"])}</span>'
            f'<span class="bar"><i style="width:{p["share"]*100:.0f}%; background:var(--accent,#ff4b4b); opacity:{op:.2f};"></i></span>'
            f'<span class="v">{val}</span></div>'
        )

    side_block = f"""
    <div class="panel">
      <div class="phead"><span class="ttl">By model · {escape(range_key)}</span></div>
      <div class="pbody">
        {bm_rows}
        <div style="margin-top:18px; padding-top:14px; border-top:1px solid var(--border,#e1e4e8);">
          <div style="font-size:12px; color:var(--muted,#6b7280); margin-bottom:8px; font-weight:600;">
            By project · {escape(range_key)}
          </div>
          {bp_rows}
        </div>
      </div>
    </div>
    """

    ui.html(
        '<div style="display:grid; grid-template-columns:2fr 1fr; gap:14px; margin-bottom:14px;">'
        f'{chart_block}{side_block}</div>'
    )

    # recent sessions
    sessions = L.get("recent_sessions", [])
    s_rows = ""
    for s in sessions:
        hit_pct = s["hit_pct"]
        hit_color = "var(--success,#16a34a)" if hit_pct >= 90 else ("var(--danger,#dc2626)" if hit_pct < 60 else "var(--text-2,#374151)")
        cost_or_tok = _fmt_money(s["cost_usd"]) if not is_pro else _fmt_tokens(s["input"]+s["output"]+s["cache_read"]+s["cache_write"])
        s_rows += (
            "<tr>"
            f'<td>{escape(s["time"])}</td>'
            f'<td>{escape(s["project"])}</td>'
            f'<td><span class="chip {s["model"]}">{escape(s["model"].title())}</span></td>'
            f'<td class="r num">{_fmt_tokens(s["input"])}</td>'
            f'<td class="r num">{_fmt_tokens(s["output"])}</td>'
            f'<td class="r num">{_fmt_tokens(s["cache_read"])} / {_fmt_tokens(s["cache_write"])}</td>'
            f'<td class="r" style="color:{hit_color};">{hit_pct}%</td>'
            f'<td class="r cost">{cost_or_tok}</td>'
            "</tr>"
        )
    if not s_rows:
        s_rows = '<tr><td colspan="8" style="color:var(--muted,#9ca3af); padding:18px; text-align:center;">No sessions in range.</td></tr>'

    sess_block = f"""
    <div class="panel">
      <div class="phead"><span class="ttl">Recent sessions · {escape(range_key)}</span>
                         <span class="lnk">all sessions →</span></div>
      <div class="pbody flush">
        <table class="stbl">
          <thead><tr>
            <th>Time</th><th>Project</th><th>Model</th>
            <th class="r">In</th><th class="r">Out</th><th class="r">Cache R / W</th>
            <th class="r">Hit %</th><th class="r">{'Tokens' if is_pro else 'Cost'}</th>
          </tr></thead>
          <tbody>{s_rows}</tbody>
        </table>
      </div>
    </div>
    """

    skills = L.get("top_skills", [])
    if skills:
        sk_rows = ""
        for s in skills:
            sk_rows += (
                '<div class="skill-row">'
                f'<span class="name"><b>{escape(s["name"])}</b><br/>'
                f'<small>{escape(s["kind"])} · {escape(s["subtitle"])}</small></span>'
                f'<span class="ct">{s["calls"]} calls</span>'
                f'<span class="cost">{_fmt_money(s["cost_usd"])}</span>'
                '</div>'
            )
    else:
        sk_rows = ('<div style="padding:18px; color:var(--muted,#9ca3af); font-size:13px;">'
                   'Skill-level breakdown lands in P4 (parsing tool_use events).</div>')
    sk_block = f"""
    <div class="panel">
      <div class="phead"><span class="ttl">Top skills · {escape(range_key)}</span></div>
      <div class="pbody flush">{sk_rows}</div>
    </div>
    """

    ui.html(
        '<div style="display:grid; grid-template-columns: 1.55fr 1fr; gap:14px;">'
        f'{sess_block}{sk_block}</div>'
    )

    # ---- action row ----
    ui.html('<div style="margin-top: 18px;"></div>')
    with ui.row().style("gap: 8px;"):
        ui.button("Refresh now",
                  on_click=lambda: ui.navigate.reload()
                  ).props("unelevated").style("background:var(--accent,#ff4b4b); color:white;")
        ui.button("Export ledger CSV",
                  on_click=lambda: ui.notify("CSV export (P4)")
                  ).props("unelevated outline").style("color:var(--text-2,#374151); border:1px solid var(--border,#d1d5db);")
        ui.button("Pricing config",
                  on_click=lambda: ui.navigate.to("/settings")
                  ).props("unelevated outline").style("color:var(--text-2,#374151); border:1px solid var(--border,#d1d5db);")
        ui.button("Plan mode",
                  on_click=lambda: ui.navigate.to("/settings")
                  ).props("unelevated outline").style("color:var(--text-2,#374151); border:1px solid var(--border,#d1d5db);")
