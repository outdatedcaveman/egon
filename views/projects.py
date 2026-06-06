"""Projects view — every project I see in your session history, plus filesystem checks.

Sources:
- Token ledger (lib/ledger.compute_ledger) → tokens + cost per project
- Filesystem: maps known project labels → expected paths, reports exists / last_modified / size
"""
from __future__ import annotations

from datetime import datetime
from html import escape
from pathlib import Path

from nicegui import ui

from lib.ledger import compute_ledger, load_config
from views._async import lazy_panel

# Resolve home directory dynamically for privacy and portability
_HOME = Path.home()
PROJECT_PATHS: dict[str, Path | None] = {
    "Egon":                  _HOME / "Claude Code" / "egon",
    "claude-meta":           _HOME / "Claude Code" / "claude-meta",
    "Noiacast":              _HOME / "Claude Code" / "noiacast",
    "CareerOps":             _HOME / "careerops",
    "Panop":                 _HOME / "Desktop" / "Panop",
    "Carrera":               _HOME / "carrera",
    "Mouseion":              _HOME / "Desktop" / "mnt" / "outputs" / "zoterpile-main",
    "Routster":              _HOME / "Documents" / "Workspace" / "kms_auto_router",
    "Claude Code (general)": _HOME / "Claude Code",
    "home":                  _HOME,
    "claude-mem":            _HOME / ".claude-mem",
}


def _fmt_tokens(n: float | int) -> str:
    if n is None: return "—"
    if n >= 1_000_000_000: return f"{n/1_000_000_000:.2f}B"
    if n >= 1_000_000:     return f"{n/1_000_000:.2f}M"
    if n >= 1_000:         return f"{n/1_000:.0f}K"
    return f"{int(n):,}"


# Paths we never deep-scan: too big, too slow, would block the UI.
_NO_SCAN = {
    _HOME,
    _HOME / "Claude Code",
    _HOME / ".claude-mem",
}


def _dir_stats(p: Path | None) -> dict:
    if p is None or not p.exists():
        return {"exists": False, "size_mb": None, "last_modified": None, "files": None}
    if p in _NO_SCAN:
        # only top-level mtime — no traversal
        try:
            mtime = p.stat().st_mtime
            return {"exists": True, "size_mb": None, "files": None,
                    "last_modified": datetime.fromtimestamp(mtime), "skipped": True}
        except OSError:
            return {"exists": True, "size_mb": None, "last_modified": None, "files": None, "skipped": True}
    try:
        files = 0
        total = 0
        latest = 0.0
        # top-level + 1 level deep only — keeps scans <100ms even on Drive
        for f in p.glob("**/*"):
            if not f.is_file():
                continue
            files += 1
            try:
                st = f.stat()
                total += st.st_size
                latest = max(latest, st.st_mtime)
            except OSError:
                continue
            if files >= 2000:
                break
        return {
            "exists": True,
            "size_mb": round(total / 1_000_000, 1),
            "files": files,
            "last_modified": datetime.fromtimestamp(latest) if latest else None,
        }
    except Exception:
        return {"exists": True, "size_mb": None, "last_modified": None, "files": None}


def render(data: dict, **_) -> None:
    cfg = load_config()
    L = compute_ledger(plan_mode=cfg.get("plan_mode", "pro"), range_key="all")
    is_pro = cfg.get("plan_mode", "pro") in ("pro", "max")

    ui.html('<h1 class="page">Projects</h1>')
    ui.html('<p class="page-sub">Per-project view across <b>all time</b>. Token usage is real (parsed from sessions), '
            'on-disk stats are scanned live.</p>')

    by_project = {p["project"]: p for p in L.get("by_project", [])}
    all_labels = sorted(set(by_project.keys()) | set(PROJECT_PATHS.keys()),
                        key=lambda k: by_project.get(k, {}).get("tokens", 0), reverse=True)

    # Move the slow _dir_stats() walks (up to 2000 files per project, ~10 projects)
    # into a background thread; render a skeleton + populate when stats arrive.
    def _load_stats() -> dict:
        return {label: (_dir_stats(PROJECT_PATHS[label]) if PROJECT_PATHS.get(label) else {"exists": None})
                for label in all_labels}

    def _render_table(stats_by_label: dict) -> None:
        rows = ""
        for label in all_labels:
            proj = by_project.get(label, {})
            path = PROJECT_PATHS.get(label)
            stats = stats_by_label.get(label, {"exists": None})

            tokens = proj.get("tokens", 0)
            cost = proj.get("cost_usd", 0)
            share = proj.get("share", 0) * 100

            path_html = (
                f'<span style="color:var(--muted); font-family: monospace; font-size: 11px;">{escape(str(path))}</span>'
                if path else
                '<span style="color:var(--muted-soft); font-size: 11px;">— no canonical path</span>'
            )
            if path and not stats["exists"]:
                status_chip = '<span class="chip warn">missing</span>'
            elif path and stats["exists"]:
                status_chip = '<span class="chip sug">on disk</span>'
            else:
                status_chip = '<span class="chip">virtual</span>'

            fs_detail = ""
            if stats.get("exists"):
                if stats.get("skipped"):
                    fs_detail = (f'(skipped deep scan · too large) '
                                 f'top-level edited {stats["last_modified"].strftime("%Y-%m-%d %H:%M") if stats["last_modified"] else "—"}')
                else:
                    fs_detail = (f'{stats["files"] or 0:,} files · '
                                 f'{stats["size_mb"] or 0:.1f} MB · '
                                 f'edited {stats["last_modified"].strftime("%Y-%m-%d %H:%M") if stats["last_modified"] else "—"}')

            primary_metric = _fmt_tokens(tokens) if is_pro else f"${cost:,.2f}"
            secondary = (f"≈ ${cost:,.0f} API · {share:.1f}% of total"
                         if is_pro else f"{_fmt_tokens(tokens)} tokens · {share:.1f}%")

            rows += f"""
            <tr>
              <td>
                <div style="font-weight: 600; color: var(--text);">{escape(label)}</div>
                <div style="margin-top: 2px;">{path_html}</div>
                <div style="margin-top: 4px; font-size: 11px; color: var(--muted);">{fs_detail}</div>
              </td>
              <td>{status_chip}</td>
              <td class="r num">{primary_metric}</td>
              <td class="r" style="color: var(--muted);">{secondary}</td>
            </tr>
            """

        ui.html(f"""
        <div class="panel">
          <div class="phead"><span class="ttl">All projects ({len(all_labels)})</span></div>
          <div class="pbody flush">
            <table class="stbl">
              <thead><tr>
                <th>Project</th><th>Status</th>
                <th class="r">{'Tokens' if is_pro else 'Cost'}</th>
                <th class="r">Detail</th>
              </tr></thead>
              <tbody>{rows}</tbody>
            </table>
          </div>
        </div>
        """)

    lazy_panel(_load_stats, _render_table)
