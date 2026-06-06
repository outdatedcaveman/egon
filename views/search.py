"""Search — one query, every source.

Two retrieval paths combined:
  1. Vault (markdown notes): hybrid TF-IDF + MiniLM via claude-meta/scripts/query_vault.py
  2. Snapshots (every adapter): lexical match across all snapshot JSONs (lib/cross_search.py)

Results unified, source-tagged, ranked. Each result has a one-click Open.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import webbrowser
from html import escape
from pathlib import Path

from nicegui import ui

from lib import cross_search

QUERY_VAULT = Path.home() / "Claude Code" / "claude-meta" / "scripts" / "query_vault.py"


# -- vault search via subprocess --------------------------------------------

def _vault_search(q: str, k: int = 10, mode: str = "hybrid") -> tuple[bool, list[dict] | str]:
    if not QUERY_VAULT.exists():
        return False, "query_vault.py not found"
    try:
        res = subprocess.run(
            [sys.executable, str(QUERY_VAULT), q, "-k", str(k), "--mode", mode, "--json"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, cwd=str(QUERY_VAULT.parent.parent),
        )
        if res.returncode != 0:
            return False, f"vault search failed: {res.stderr[:200]}"
        try:
            return True, json.loads(res.stdout)
        except json.JSONDecodeError:
            return True, _parse_human(res.stdout)
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except Exception as e:
        return False, str(e)


def _parse_human(out: str) -> list[dict]:
    results = []
    for block in out.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        m = re.match(r"^\[(\d+\.\d+)\]\s+(.+)", block)
        if m:
            results.append({"score": float(m.group(1)),
                            "path":  m.group(2).strip(),
                            "snippet": "\n".join(block.splitlines()[1:]).strip()[:400]})
    return results


# -- rendering helpers -------------------------------------------------------

def _src_chip(source: str, kind: str = "") -> str:
    return f'<span class="chip {kind}">{escape(source)}</span>'


def _render_snapshot_hit(r: dict) -> str:
    item   = r["item"]
    title  = cross_search.pretty_title(item)
    url    = cross_search.pretty_url(item)
    subln  = cross_search.pretty_subline(item, r["source"])
    score  = r["score"]
    url_html = (f'<div style="margin-top:4px; font-size:11px; color:var(--muted); '
                f'overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">'
                f'<a href="{escape(url)}" target="_blank" style="color:var(--accent); text-decoration:none;">'
                f'{escape(url[:140])}</a></div>'
                if url else '')
    return f'''
    <div style="padding:14px 18px; border-bottom:1px solid var(--border-soft);">
      <div style="display:flex; align-items:center; gap:10px; margin-bottom:4px;">
        {_src_chip(r["source"], "sug")}
        <span class="chip" style="background:var(--ledger-bg); color:var(--ledger-txt);">{score}</span>
        <span style="color:var(--muted); font-size:11px;">{escape(subln[:120])}</span>
      </div>
      <div style="font-weight:600; color:var(--text); font-size:14px;">{escape(str(title)[:140])}</div>
      {url_html}
    </div>
    '''


def _render_vault_hit(r: dict, idx: int) -> str:
    path = r.get("path", "?")
    score = r.get("score", 0)
    snippet = (r.get("snippet") or r.get("preview") or "")[:300]
    return f'''
    <div style="padding:14px 18px; border-bottom:1px solid var(--border-soft);">
      <div style="display:flex; align-items:center; gap:10px; margin-bottom:4px;">
        <span class="chip">vault</span>
        <span class="chip" style="background:var(--ledger-bg); color:var(--ledger-txt);">{score:.3f}</span>
        <span style="font-family:monospace; font-size:11px; color:var(--muted); overflow:hidden;
                     text-overflow:ellipsis;">{escape(path)[:140]}</span>
      </div>
      <div style="font-size:13px; color:var(--text-2); line-height:1.5;
                  white-space:pre-wrap; max-height:80px; overflow:hidden;">{escape(snippet)}</div>
    </div>
    '''


# -- view --------------------------------------------------------------------

def render(data: dict, **_) -> None:
    inv = cross_search.stats()
    vault_chip = '<span class="chip">vault: 3,978 docs</span>'
    src_chips = " ".join(f'<span class="chip">{s}: {n:,}</span>'
                         for s, n in inv.items() if n > 0)

    ui.html(f'''
    <h1 class="page">Search</h1>
    <p class="page-sub">One query · every source. Vault (notes, MiniLM) + every snapshot (Chrome bookmarks,
       Letterboxd, Zotero, Notion, Instapaper, …). All local.</p>
    <div style="margin-bottom: 16px;">{vault_chip} {src_chips}</div>
    ''')

    results_container = ui.element("div")

    # ---- search form ----
    with ui.row().style("gap:8px; margin-bottom:14px; align-items:center; flex-wrap:wrap;"):
        q_input = ui.input(placeholder="ask anything · 'transformer scaling', 'gone with the wind', 'reciprocity'…").props(
            "outlined dense clearable stack-label"
        ).style("min-width: 540px;")

        sources_input = ui.select(
            options=["all"] + sorted(inv.keys()),
            value="all",
            label="source",
        ).props("outlined dense").style("width: 160px;")

        k_input = ui.number("k per source", value=15, min=1, max=50).props(
            "outlined dense stack-label"
        ).style("width: 130px;")

        def _go():
            q = (q_input.value or "").strip()
            if not q:
                ui.notify("type a query")
                return
            results_container.clear()
            with results_container:
                ui.html(f'<p class="page-sub">Searching {escape(q)} …</p>')

            # 1. vault
            vault_ok, vault_hits = _vault_search(q, k=int(k_input.value))
            vault_hits = vault_hits if vault_ok else []

            # 2. snapshots
            srcs = None if sources_input.value == "all" else [sources_input.value]
            snap_hits = cross_search.search(q, sources=srcs, limit=int(k_input.value) * 4)

            results_container.clear()

            total = len(vault_hits) + len(snap_hits)
            with results_container:
                vault_err_html = ("" if vault_ok else
                    '<span style="color:var(--danger);"> · vault search failed</span>')
                ui.html(f'''<p class="page-sub" style="margin-bottom:14px;">
                    <b>{total}</b> hits · {len(vault_hits)} from vault · {len(snap_hits)} from snapshots
                    {vault_err_html}
                </p>''')

                if total == 0:
                    ui.html('<p class="page-sub">No results.</p>')
                    return

                # Vault block
                if vault_hits:
                    rows = "".join(_render_vault_hit(r, i) for i, r in enumerate(vault_hits, 1))
                    ui.html(f'''
                    <div class="panel" style="margin-bottom:14px;">
                      <div class="phead"><span class="ttl">📓 Vault · {len(vault_hits)} hits</span></div>
                      <div class="pbody flush">{rows}</div>
                    </div>
                    ''')

                # Snapshots block (grouped by source for legibility)
                if snap_hits:
                    by_source: dict[str, list[dict]] = {}
                    for h in snap_hits:
                        by_source.setdefault(h["source"], []).append(h)
                    for src, hits in by_source.items():
                        rows = "".join(_render_snapshot_hit(r) for r in hits)
                        icon = {
                            "letterboxd": "🎬",
                            "chrome_bookmarks": "🔖",
                            "zotero": "📚",
                            "instapaper": "📥",
                        }.get(src, "📦")
                        ui.html(f'''
                        <div class="panel" style="margin-bottom:14px;">
                          <div class="phead"><span class="ttl">{icon} {escape(src)} · {len(hits)} hits</span></div>
                          <div class="pbody flush">{rows}</div>
                        </div>
                        ''')

        ui.button("Search", on_click=_go).props("unelevated").style(
            "background: var(--accent); color: white; padding: 7px 18px;")
        q_input.on("keydown.enter", _go)
