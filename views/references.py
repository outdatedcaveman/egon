"""References window — Zotero (full library) · Paperpile · Mouseion.

Rich tabular display per source: sortable columns, live filter, type/year facets.
Visual cohesion analysis on top (DOI overlap across managers).
"""
from __future__ import annotations

import json
from html import escape

from nicegui import ui

from lib.snapshot_store import latest_snapshot


# Only one Zotero entry — the full-library one via Web API.
# (The local SQLite one only sees what Zotero has cached locally and is redundant.)
SOURCES = [
    ("zotero_web", "📚", "Zotero"),
    ("paperpile",  "📑", "Paperpile"),
    ("mouseion",   "🐭", "Mouseion"),
]

# Column spec per source — (key, header, width, align, formatter_name)
# All sources share the same base set; renderers fall back to "" for missing fields.
COLUMNS = [
    ("title",    "Title",   "40%", "left",  "text"),
    ("creators", "Authors", "20%", "left",  "text"),
    ("year",     "Year",    "60px", "right", "text"),
    ("type",     "Type",    "110px", "left",  "type"),
    ("doi",      "DOI",     "180px", "left",  "doi"),
    ("added",    "Added",   "110px", "left",  "date"),
]


def _cohesion_panel() -> None:
    """DOI-based overlap across managers."""
    def _dois(snap_id):
        s = latest_snapshot(snap_id) or {}
        return {(i.get("doi") or "").strip().lower()
                for i in s.get("items", []) if i.get("doi")}

    Z, P, M = _dois("zotero_web"), _dois("paperpile"), _dois("mouseion")
    union = len(Z | P | M)
    if union == 0:
        return
    three = len(Z & P & M)
    pct = round(100 * three / union) if union else 0
    chips = (
        f'<span class="status-pill"><span style="color:var(--muted);">Zotero</span> '
        f'<b style="color:var(--text);">{len(Z):,}</b></span>'
        f'<span class="status-pill"><span style="color:var(--muted);">Paperpile</span> '
        f'<b style="color:var(--text);">{len(P):,}</b></span>'
        f'<span class="status-pill"><span style="color:var(--muted);">Mouseion</span> '
        f'<b style="color:var(--text);">{len(M):,}</b></span>'
        f'<span class="status-pill"><span style="color:var(--muted);">union</span> '
        f'<b style="color:var(--text);">{union:,}</b></span>'
        f'<span class="status-pill"><span style="color:var(--muted);">three-way overlap</span> '
        f'<b style="color:var(--ledger-txt);">{pct}%</b></span>'
    )
    ui.html(f'<div style="margin-bottom: 14px;">{chips}</div>')


def render(data: dict, **_) -> None:
    ui.html('<h1 class="page">📚 References</h1>')
    ui.html('<p class="page-sub">Reference managers in one place — tabular, sortable, filterable. '
            'DOI-based cohesion across all three on top.</p>')
    _cohesion_panel()

    with ui.tabs().props("inline-label dense").style(
        "background: transparent; border-bottom: 1px solid var(--border); margin-bottom: 14px;"
    ) as tabs:
        for sid, icon, label in SOURCES:
            ui.tab(sid, label=f"{icon} {label}")

    with ui.tab_panels(tabs, value="zotero_web").style("background: transparent;"):
        for sid, icon, label in SOURCES:
            with ui.tab_panel(sid):
                _render_source(sid, icon, label)


def _fmt(value, kind: str) -> str:
    if value is None:
        return ""
    s = str(value)
    if not s:
        return ""
    if kind == "type":
        return f'<span class="chip">{escape(s)}</span>'
    if kind == "doi":
        return (f'<a href="https://doi.org/{escape(s)}" target="_blank" '
                f'style="color:var(--accent); text-decoration:none; font-family:monospace; '
                f'font-size:11px;">{escape(s)}</a>')
    if kind == "date":
        return f'<span style="color:var(--muted); font-size:11px;">{escape(s[:10])}</span>'
    return escape(s)


def _render_source(source: str, icon: str, label: str) -> None:
    snap = latest_snapshot(source)
    if not snap or snap.get("status") != "ok":
        err = (snap or {}).get("error", "not yet synced")
        ui.html(f'''
        <div class="panel"><div class="pbody">
          <p style="color: var(--muted); margin: 0; font-size: 13px;">
            <b>{escape(label)}</b> — {escape(err)}. Configure in
            <a href="/settings" style="color: var(--accent);">Settings</a>.
          </p>
        </div></div>
        ''')
        return

    items = list(snap.get("items", []))
    total_in_lib = snap.get("total_in_library")

    # ---- stats strip ----
    with_doi = sum(1 for i in items if i.get("doi"))
    types = {(i.get("type") or "") for i in items if i.get("type")}
    years = sorted({(i.get("year") or "")[:4] for i in items if i.get("year")}, reverse=True)

    chips = (
        f'<span class="status-pill"><span style="color:var(--muted);">in view</span> '
        f'<b style="color:var(--text);">{len(items):,}</b></span>'
    )
    if total_in_lib and total_in_lib != len(items):
        chips += (f'<span class="status-pill"><span style="color:var(--muted);">in library</span> '
                  f'<b style="color:var(--text);">{total_in_lib:,}</b></span>')
    chips += (f'<span class="status-pill"><span style="color:var(--muted);">with DOI</span> '
              f'<b style="color:var(--text);">{with_doi:,}</b></span>')
    if types:
        chips += (f'<span class="status-pill"><span style="color:var(--muted);">types</span> '
                  f'<b style="color:var(--text);">{len(types)}</b></span>')
    sync_at = (snap.get("synced_at") or "")[:16]
    if sync_at:
        chips += (f'<span class="status-pill"><span style="color:var(--muted);">synced</span> '
                  f'<b style="color:var(--text);">{escape(sync_at)}</b></span>')
    ui.html(f'<div style="margin-bottom:14px;">{chips}</div>')

    # ---- filter controls ----
    # Generate a unique table id so multiple source tabs don't collide in the DOM.
    table_id = f"reftbl-{source}"
    with ui.row().style("gap: 8px; align-items: center; margin-bottom: 10px; flex-wrap: wrap;"):
        search_inp = ui.input(placeholder="filter title / authors / DOI…").props(
            "outlined dense stack-label clearable"
        ).style("min-width: 280px;")
        type_opts = {"": "All types"} | {t: t for t in sorted(types)}
        type_sel = ui.select(type_opts, value="").props("dense outlined").style("min-width:140px;")
        year_opts = {"": "All years"} | {y: y for y in years[:30] if y}
        year_sel = ui.select(year_opts, value="").props("dense outlined").style("min-width:120px;")
        doi_chk = ui.checkbox("DOI only", value=False)

        js_filter = f"""
        (() => {{
          const tbl = document.getElementById({json.dumps(table_id)});
          if (!tbl) return;
          const q = ({{q_in}}).toLowerCase().trim();
          const t = {{t_in}}; const y = {{y_in}}; const doionly = {{d_in}};
          let shown = 0;
          tbl.querySelectorAll('tbody tr').forEach(tr => {{
            const blob = tr.dataset.search || '';
            const ty = tr.dataset.type || '';
            const yr = tr.dataset.year || '';
            const hasdoi = tr.dataset.hasdoi === '1';
            const hit = (!q || blob.includes(q)) && (!t || ty === t) && (!y || yr === y) && (!doionly || hasdoi);
            tr.style.display = hit ? '' : 'none';
            if (hit) shown++;
          }});
          const cnt = document.getElementById({json.dumps(table_id + '-count')});
          if (cnt) cnt.textContent = shown.toLocaleString();
        }})()
        """

        def _apply_filters():
            ui.run_javascript(js_filter
                .replace("{q_in}", json.dumps(search_inp.value or ""))
                .replace("{t_in}", json.dumps(type_sel.value or ""))
                .replace("{y_in}", json.dumps(year_sel.value or ""))
                .replace("{d_in}", "true" if doi_chk.value else "false"))

        search_inp.on_value_change(lambda _e: _apply_filters())
        type_sel.on_value_change(lambda _e: _apply_filters())
        year_sel.on_value_change(lambda _e: _apply_filters())
        doi_chk.on_value_change(lambda _e: _apply_filters())

    # ---- table ----
    # Cap at 2000 rows in DOM for perf; user can refine filters to see more.
    cap = 2000
    rows = items[:cap]
    overflow = max(0, len(items) - cap)

    thead = "".join(
        f'<th style="width:{w}; text-align:{a}; cursor:pointer;" '
        f'onclick="window.__sortRefTbl({json.dumps(table_id)}, {idx})">'
        f'{escape(h)} <span style="color:var(--muted); font-size:10px;">↕</span></th>'
        for idx, (_k, h, w, a, _f) in enumerate(COLUMNS)
    )

    body = []
    for it in rows:
        # Include both 'creators' and 'authors' in the search blob
        blob = " ".join(str(it.get(k, "")) for k in ("title", "creators", "authors", "doi", "type", "year")).lower()
        cells = []
        for (k, _h, _w, a, fmt) in COLUMNS:
            v = it.get(k, "")
            if k == "creators" and not v:
                v = it.get("authors", "")
            cells.append(f'<td style="text-align:{a}; vertical-align:top;">{_fmt(v, fmt)}</td>')
        body.append(
            f'<tr data-search="{escape(blob, quote=True)}" '
            f'data-type="{escape(it.get("type",""), quote=True)}" '
            f'data-year="{escape((it.get("year","") or "")[:4], quote=True)}" '
            f'data-hasdoi="{1 if it.get("doi") else 0}">'
            f'{"".join(cells)}</tr>'
        )
    if not body:
        body.append(f'<tr><td colspan="{len(COLUMNS)}" '
                    f'style="color:var(--muted); padding:18px; text-align:center;">'
                    f'No items.</td></tr>')

    # Inject a one-time JS column sorter (idempotent — guards on window.__sortRefTbl).
    ui.add_body_html("""
    <script>
    if (!window.__sortRefTbl) {
      window.__sortRefTbl = function(tableId, colIdx) {
        const tbl = document.getElementById(tableId);
        if (!tbl) return;
        const tbody = tbl.querySelector('tbody');
        const rows = Array.from(tbody.querySelectorAll('tr'));
        const prev = tbl.dataset.sortCol;
        const prevDir = tbl.dataset.sortDir || 'asc';
        const dir = (prev == colIdx && prevDir === 'asc') ? 'desc' : 'asc';
        tbl.dataset.sortCol = colIdx;
        tbl.dataset.sortDir = dir;
        const mult = dir === 'asc' ? 1 : -1;
        rows.sort((a, b) => {
          const av = (a.children[colIdx]?.innerText || '').trim().toLowerCase();
          const bv = (b.children[colIdx]?.innerText || '').trim().toLowerCase();
          const an = parseFloat(av), bn = parseFloat(bv);
          if (!isNaN(an) && !isNaN(bn)) return (an - bn) * mult;
          return av.localeCompare(bv) * mult;
        });
        rows.forEach(r => tbody.appendChild(r));
      };
    }
    </script>
    """)

    ui.html(f"""
    <div class="panel">
      <div class="phead">
        <span class="ttl">{escape(label)} · <span id="{table_id}-count">{len(rows)}</span> of {len(items):,} shown</span>
        {f'<span class="lnk" style="color:var(--muted);">+{overflow:,} more — narrow your filter</span>' if overflow else ''}
      </div>
      <div class="pbody flush" style="max-height: 70vh; overflow: auto;">
        <table class="stbl" id="{table_id}" style="font-size: 12px;">
          <thead><tr>{thead}</tr></thead>
          <tbody>{"".join(body)}</tbody>
        </table>
      </div>
    </div>
    """)
