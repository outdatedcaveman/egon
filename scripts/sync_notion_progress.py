"""Sync current Egon system state into the Notion 🛰️ Egon page.

This is the "universal truth source" — every meaningful change (new adapter,
schema bump, cron retime, rename) should land here. Re-runnable: it rewrites
the same child pages each time rather than appending forever.

Currently writes / updates:
- "System Status" child page: live KPIs (which adapters work, snapshot counts, cron time)
- "Architecture" child page: pinned summary of what Egon is + how it's wired
- "Changes Log" child page: structured list of recent changes (this turn included)
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

EGON_PAGE_ID = "35393daa-9215-8134-9cf3-fc66d9a0e1a6"  # 🛰️ Egon root


def _token() -> str:
    env = Path(r"C:/Users/bruno/Claude Code/claude-meta/.env")
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.startswith("NOTION_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("NOTION_TOKEN missing")


H = lambda: {"Authorization": f"Bearer {_token()}", "Notion-Version": "2022-06-28",
             "Content-Type": "application/json"}


def _find_child(title: str) -> str | None:
    """Find an existing child page of the Egon root by title."""
    r = httpx.get(f"https://api.notion.com/v1/blocks/{EGON_PAGE_ID}/children",
                  headers=H(), timeout=15)
    if r.status_code != 200:
        return None
    for b in r.json().get("results", []):
        if b.get("type") == "child_page" and b.get("child_page", {}).get("title") == title:
            return b["id"]
    return None


def _archive_children(page_id: str) -> None:
    """Remove all blocks from a page so we can replace its body cleanly."""
    while True:
        r = httpx.get(f"https://api.notion.com/v1/blocks/{page_id}/children",
                      headers=H(), timeout=15)
        if r.status_code != 200:
            return
        blocks = r.json().get("results", [])
        if not blocks:
            return
        for b in blocks:
            httpx.delete(f"https://api.notion.com/v1/blocks/{b['id']}", headers=H(), timeout=10)


def _ensure_page(title: str, icon: str) -> str:
    """Return page-id for the child page named `title`; create if missing."""
    existing = _find_child(title)
    if existing:
        return existing
    r = httpx.post(
        "https://api.notion.com/v1/pages", headers=H(), timeout=15,
        json={
            "parent": {"page_id": EGON_PAGE_ID},
            "icon":   {"type": "emoji", "emoji": icon},
            "properties": {"title": {"title": [{"type": "text", "text": {"content": title}}]}},
        },
    )
    r.raise_for_status()
    return r.json()["id"]


def _append(page_id: str, blocks: list[dict]) -> None:
    # Notion caps at 100 blocks per request
    for i in range(0, len(blocks), 90):
        chunk = blocks[i:i+90]
        r = httpx.patch(
            f"https://api.notion.com/v1/blocks/{page_id}/children",
            headers=H(), timeout=20,
            json={"children": chunk},
        )
        if r.status_code != 200:
            print(f"append failed: {r.status_code} {r.text[:200]}", file=sys.stderr)


# -- block builders ----------------------------------------------------------

def _p(text: str, bold: bool = False) -> dict:
    return {"object": "block", "type": "paragraph", "paragraph": {
        "rich_text": [{"type": "text", "text": {"content": text},
                       "annotations": {"bold": bold}}]}}


def _h(text: str, level: int = 2) -> dict:
    t = f"heading_{level}"
    return {"object": "block", "type": t, t: {
        "rich_text": [{"type": "text", "text": {"content": text}}]}}


def _bullet(text: str) -> dict:
    return {"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {
        "rich_text": [{"type": "text", "text": {"content": text}}]}}


def _code(text: str, lang: str = "plain text") -> dict:
    return {"object": "block", "type": "code", "code": {
        "rich_text": [{"type": "text", "text": {"content": text[:1900]}}],
        "language": lang}}


def _divider() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}


# -- content -----------------------------------------------------------------

def architecture_blocks() -> list[dict]:
    return [
        _h("What Egon is", 2),
        _p("Local-only visual control plane for Bruno's KMS. NiceGUI dashboard at "
           "http://127.0.0.1:8088, contained pywebview window, system-tray icon. "
           "Reads every source (Routster, Mouseion, Notion, vault, Letterboxd, "
           "Chrome bookmarks, Zotero, Instapaper, …) and renders them in tabbed "
           "windows. No data ever leaves this machine."),
        _h("Wiring", 2),
        _bullet("Backend: NiceGUI 3.11 (FastAPI + websockets) on 127.0.0.1:8088"),
        _bullet("Launcher: pywebview + pystray; PID-file at egon/.egon.pid"),
        _bullet("Project root: C:/Users/bruno/Claude Code/egon/"),
        _bullet("Config (gitignored): egon/egon-config.json — all credentials + cache"),
        _bullet("State (double-backed-up): local egon/state/ + vault 050-Resources/egon/"),
        _bullet("Snapshots: date-partitioned JSON per source, never deleted (full audit)"),
        _bullet("Daily pass cron: KMS-Egon-DailyPass at 03:00 — snapshots + mirror"),
        _bullet("Mirror: snapshots → vault MD pages (default ON) + Notion DBs (default OFF)"),
        _bullet("Token ledger: parses ~/.claude/projects/*/*.jsonl for real Pro-plan usage"),
        _h("Security guarantees", 2),
        _bullet("egon.py asserts host == 127.0.0.1 before binding — refuses external"),
        _bullet("All secrets in egon-config.json (gitignored). Env vars override."),
        _bullet("Snapshots additive only — items never deleted; full audit trail"),
        _bullet("External-agent gate hard-coded OFF until UI flow built (lib/agent_gate.py)"),
        _bullet("Destructive actions declared per-adapter; type-to-confirm gating planned"),
    ]


def status_blocks() -> list[dict]:
    # gather live status from each adapter
    from lib.adapters import (chrome_bookmarks, chrome_tabs, instapaper as ins_basic,
                              instapaper_full, letterboxd, zotero_local, android_tabs)
    rows = []
    for name, mod in [("Letterboxd", letterboxd), ("Chrome Bookmarks", chrome_bookmarks),
                      ("Chrome Tabs (desktop)", chrome_tabs), ("Chrome Tabs (Android)", android_tabs),
                      ("Zotero", zotero_local), ("Instapaper (basic)", ins_basic),
                      ("Instapaper (full OAuth)", instapaper_full)]:
        try:
            s = mod.live_status()
            rows.append(f"{name}: {s.get('status','?')}")
        except Exception as e:
            rows.append(f"{name}: error · {e}")
    blocks = [
        _h("Live source status", 2),
        _p(f"Captured at {datetime.now().isoformat(timespec='minutes')}"),
    ]
    for row in rows:
        blocks.append(_bullet(row))
    return blocks


def changes_blocks(items: list[dict]) -> list[dict]:
    """items: [{date, headline, bullets:[…]}]"""
    blocks = []
    for c in items:
        blocks.append(_h(f"{c['date']} · {c['headline']}", 3))
        for b in c.get("bullets", []):
            blocks.append(_bullet(b))
    return blocks


CHANGES = [
    {"date": "2026-05-13", "headline": "Core principle: Connection ease-of-use ranking",
     "bullets": [
        "RULE: every adapter connection is ranked by (1) safety + privacy, (2) accessibility / ease for non-technical users, (3) versatility (uptime, realtime, bidirectional, breadth).",
        "Translates to TWO acceptable connection patterns per source:",
        "  · pattern A — direct API connection (no user steps beyond pasting credentials Egon then uses everywhere)",
        "  · pattern B — login-only (user gives their account creds, app does everything; no DevTools, cookies, manifests)",
        "REJECTED: anything that asks the user to copy cookie headers, manifest URLs, or dive into DevTools.",
        "Applied: YouTube Music switched from 'paste Cookie header' → 'drop Google Takeout ZIP'.",
        "Applied: Instapaper errors now report actual cause (wrong password / rate limit / timeout) instead of generic failure.",
     ]},
    {"date": "2026-05-13", "headline": "Core principle: UI correspondence for every behavior",
     "bullets": [
        "GUIDING PRINCIPLE (going forward): no functionality lives only as code/config — "
        "every behavior MUST have a UI element a non-technical user can adjust through the dashboard.",
        "Egon is a development environment, not a 'picture' of what's happening underneath.",
        "Applied: Navigation view rebuilt — status strip, action buttons, editable config form ALL native NiceGUI, "
        "above the embedded Panop frontend.",
        "Going forward: every new adapter/setting/cron gets a Settings entry; every action gets a button; "
        "every state gets a chip.",
     ]},
    {"date": "2026-05-13", "headline": "Panop fully vendored + mounted inside Egon",
     "bullets": [
        "Panop source vendored to egon/external/panop_server/ (17 MB inc. configs & history)",
        "Panop frontend vendored to egon/external/panop_gui/ + all 45 API URLs patched to /panop/api/v1/*",
        "FastAPI mounted at /panop · static UI served at /panop-ui/ · same Egon process, port 8088",
        "No more separate Panop sidecar process — everything runs in-process",
        "Branding wiped: 'Panop Control Center' → 'Navigation · Egon'; sidebar 'PANOP' → 'NAVIGATION'; "
        "all user-visible 'Panop' refs replaced with Egon equivalents",
     ]},
    {"date": "2026-05-13", "headline": "Notion truth-source sync · Letterboxd auto-login · Android tabs via Panop",
     "bullets": [
        "Renamed Notion 🧠 KMS → 🛰️ Egon (icon + title)",
        "Letterboxd: password-based auto-login (POST /user/login.do), session cookie cached & auto-refreshed",
        "Android tabs adapter wired to Panop's /api/v1/tabs/inspect (port autodetected)",
        "Notion sync script: rewrites Architecture + Status + Changes pages each run",
        "Chrome :9222 confirmed blocked by Chrome 127+ security — needs custom extension (deferred)",
     ]},
    {"date": "2026-05-13", "headline": "Apps autodiscovery + mirror cron + cookie scraper",
     "bullets": [
        "Cron retimed: KMS-Egon-DailyPass 23:00 → 03:00",
        "Apps autodiscovery: ports read from source code (Routster 4000 ✓, Mouseion 7274 default, Panop 8000 ✓), cached 7 days",
        "Letterboxd session cookie support + export ZIP fallback",
        "Chrome shortcut auto-modifier (taskbar updated, system shortcut needs admin)",
        "Mirror two-tier gate: vault writes default ON, Notion default OFF",
        "Mirror runs in daily pass after snapshots",
     ]},
    {"date": "2026-05-13", "headline": "Four new windows + unified Settings",
     "bullets": [
        "Artifacts · Media · References · Databases windows shipped",
        "Apps orchestrator window — single interface for Panop/Mouseion/Routster",
        "Reusable lib/snapshot_store.py with double-backup writer (local + vault)",
        "Adapter base protocol + tabbed view template",
        "Working adapters: chrome_bookmarks (73 MB file ✓), zotero_local (1.7 GB SQLite ✓), letterboxd",
        "Stub adapters with clear 'configure' hints: paperpile, youtube_music, kindle, tvtime, gdrive, etc.",
        "Unified Settings → Connections card (all credential fields in one place)",
        "Security: egon-config.json gitignored, 127.0.0.1-only bind asserted, agent gate hard-off",
     ]},
    {"date": "2026-05-13", "headline": "Token ledger fixes · plan-aware rendering · dark mode",
     "bullets": [
        "Burn rate fixed (real rolling 24h, not 2-day buckets)",
        "Cache-savings KPI scoped to MTD (was mixing range and MTD windows)",
        "Pro-plan-aware rendering: tokens are headline, $ shown as API counterfactual",
        "Time-range chips (24h · 7d · 30d · 90d · YTD · all) actually filter",
        "tool_use parsing → top skills (Bash 1612 calls · Write 299 · Edit 896 · …)",
        "Verification subtitle: 15 session files · 8,021 turns · last turn timestamp",
        "Dark mode toggle in header + Settings → CSS-variable theme swap",
     ]},
    {"date": "2026-05-07", "headline": "Project genesis · NiceGUI app · launcher",
     "bullets": [
        "Old Egon FastAPI/React scaffold archived to .backups/egon-scaffold-*.zip + renamed .legacy",
        "New NiceGUI project at C:/Users/bruno/Claude Code/egon",
        "Streamlit-aesthetic NiceGUI theme (light + dark via CSS variables)",
        "Contained pywebview launcher · system tray · desktop + Start Menu shortcuts",
        "Daily pass agent + Windows scheduled task",
        "All adapters double-back-up to vault 050-Resources/egon/snapshots/",
     ]},
]


def main() -> int:
    print("=== syncing to Notion 🛰️ Egon ===")

    # 1. Architecture page
    arch_id = _ensure_page("Architecture", "🏗️")
    _archive_children(arch_id)
    _append(arch_id, architecture_blocks())
    print(f"  ✓ Architecture page: {arch_id}")

    # 2. Status page
    status_id = _ensure_page("Live status", "📡")
    _archive_children(status_id)
    _append(status_id, status_blocks())
    print(f"  ✓ Live status page: {status_id}")

    # 3. Changes Log page
    log_id = _ensure_page("Changes log", "📜")
    _archive_children(log_id)
    _append(log_id, changes_blocks(CHANGES))
    print(f"  ✓ Changes log page: {log_id}")

    print("done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
