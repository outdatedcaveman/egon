"""Stub adapter generator — for sources we'll wire up later.

Each stub gets a uniform shape with a clear "needs" message telling you what
auth/API access is required to enable it.
"""
from __future__ import annotations

from types import ModuleType


def make_stub(adapter_id: str, label: str, icon: str, kind: str, needs: str) -> ModuleType:
    """Build a Python module-shaped object that exports the Adapter contract."""
    mod = ModuleType(f"egon.adapters.{adapter_id}")
    mod.META = {
        "id": adapter_id,
        "label": label,
        "icon": icon,
        "kind": kind,
        "needs_auth": True,
        "destructive_actions": [],
        "read_only_default": True,
    }
    err = {"status": "unconfigured", "error": needs}
    mod.live_status = lambda: err
    mod.snapshot    = lambda: err
    mod.items       = lambda limit=100: []
    mod.stats       = lambda: {"status": "unconfigured", "count": 0,
                                "last_synced": None, "error": needs}
    return mod


# Artifacts (queue / collection sources)
instapaper_full = make_stub("instapaper_full",   "Instapaper Reading List", "📥",
    "artifact", "Instapaper OAuth (Full API) needed. Register consumer at instapaper.com/main/request_oauth_consumer_token; "
                "then add instapaper.consumer_key/secret + user oauth tokens to egon-config.json")
chrome_tabs     = make_stub("chrome_tabs",        "Chrome Open Tabs (desktop)", "🌐",
    "artifact", "Chrome remote-debugging on :9222, OR the Claude-in-Chrome MCP extension. Toggle in Settings.")
android_tabs    = make_stub("android_tabs",       "Chrome Open Tabs (Android)", "📱",
    "artifact", "ADB over USB/Wi-Fi + Panop on :9222. Reuses existing Panop pipeline.")

# Media
youtube_music   = make_stub("youtube_music",      "YouTube Music",         "🎵",
    "media", "Google OAuth + ytmusicapi. Or paste headers from a browser session into egon-config.json:youtube_music.cookie")
kindle          = make_stub("kindle",             "Kindle Reads",          "📖",
    "media", "Amazon notebook export (https://read.amazon.com/notebook). No public API — manual ZIP import for now.")
tvtime          = make_stub("tvtime",             "TV Time",               "📺",
    "media", "TV Time has no public API. Use their CSV export, drop into egon-config.json:tvtime.export_path.")

# References — both replaced by real adapters in lib/adapters/{mouseion,paperpile_stub}.
# Stubs intentionally REMOVED to avoid duplicates in Settings + windows.
# References view now uses the real lib.adapters.mouseion + lib.adapters.zotero_local + lib.adapters.zotero_web.

# Databases
_gdrive_REMOVED_stub = None  # Replaced by real lib.adapters.gdrive — Drive shows up only in Databases via the real adapter.
notion_full     = make_stub("notion_full",        "Notion (full DB)",      "📓",
    "database", "Uses NOTION_TOKEN from claude-meta/.env already. Full snapshot reader landing next.")
obsidian_full   = make_stub("obsidian_full",      "Obsidian Vault",        "🟣",
    "database", "Filesystem at G:/MetaVault/. Real adapter is in lib/adapters/vault.py — wrapper coming.")
desktop_fs      = make_stub("desktop_fs",         "Desktop PC files",      "🖥️",
    "database", "Configure root paths in egon-config.json:desktop_fs.roots (list). Default: ~/Documents, ~/Desktop.")
