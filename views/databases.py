"""Databases window — Google Drive, Notion, Obsidian, desktop PC."""
from __future__ import annotations

from lib.adapters import _stubs
from views._window import render_window


def render(data: dict, **_) -> None:
    render_window(
        title="Databases",
        subtitle="Your four big knowledge stores. Notion + Obsidian already power Egon's daily pass; "
                 "this window adds full-corpus snapshots so you can search across them in P5.",
        adapters=[
            _stubs.notion_full,
            _stubs.obsidian_full,
            _stubs.desktop_fs,
        ],
        actions_help="<b>Cross-platform semantic search</b> (P5) will index all four sources into "
                     "one local FAISS index, queryable from the Search tab. Embeddings stay on-device.",
    )
