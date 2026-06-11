"""Notion adapter — uses NOTION_TOKEN from claude-meta/.env to query the KMS root."""
from __future__ import annotations

import os
from pathlib import Path

from lib.lazy_httpx import httpx  # deferred ~2s import (2026-06-11 perf pass)

from lib.egon_paths import ENV_FILE as ENV_PATH
KMS_ROOT_ID = os.environ.get("NOTION_KMS_ROOT_ID", "")   # your Notion KMS root page id
HOME_PAGE_ID = os.environ.get("NOTION_HOME_PAGE_ID", "")  # your Notion home page id


def _token() -> str | None:
    tok = os.environ.get("NOTION_TOKEN")
    if tok:
        return tok
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("NOTION_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def _headers(tok: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {tok}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }


def live_status(timeout: float = 4.0) -> dict:
    tok = _token()
    if not tok:
        return {"status": "error", "error": "NOTION_TOKEN missing"}
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.post(
                "https://api.notion.com/v1/search",
                headers=_headers(tok),
                json={"filter": {"property": "object", "value": "page"}, "page_size": 1},
            )
            r.raise_for_status()
            j = r.json()
            count = j.get("total", len(j.get("results", [])))

        return {
            "queue_count": None,           # filled by agent (classified inbox)
            "delta_24h": None,
            "status": "ok",
            "indexed_pages_min": count,
        }
    except httpx.HTTPError as e:
        return {"status": "error", "error": f"http: {e}"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


# ── push: write Egon's daily status back to a designated Notion page ─────────
#
# Bruno 2026-05-29: the adapter so far only PULLS from Notion (workspace dumps
# into state/snapshots/notion_workspace/). This adds the WRITE direction so
# Egon's daily summary lands back in Notion as a toggle block under a page
# Bruno designates via egon-config.json.notion.status_page_id.
#
# Design notes:
# - Append-only: we PATCH /v1/blocks/{page_id}/children with a new toggle
#   block per day, so Bruno gets a stack of "Egon — YYYY-MM-DD" entries he
#   can expand. No risk of deleting or overwriting existing page content.
# - Idempotency-by-day: caller passes today's date; if the same date already
#   has a toggle block as a top-level child, we skip. (Cheap one-call check.)
# - All failures non-fatal — daily pass should not crash if Notion is down.


def _status_page_id() -> str | None:
    """Read the designated status page id from egon-config.json.notion."""
    import json
    from lib.egon_paths import EGON_ROOT; cfg_path = EGON_ROOT / "egon-config.json"
    if not cfg_path.exists():
        return None
    try:
        with cfg_path.open(encoding="utf-8") as f:
            cfg = json.load(f)
        return (cfg.get("notion") or {}).get("status_page_id")
    except Exception:
        return None


def _already_pushed_today(page_id: str, date_str: str, tok: str,
                          timeout: float = 6.0) -> bool:
    """Return True iff a toggle block titled 'Egon — <date_str>' already exists
    as a direct child of `page_id`. Best-effort: returns False on error so we
    err on the side of pushing (the duplicate is visible, missing is silent)."""
    try:
        marker = f"Egon — {date_str}"
        with httpx.Client(timeout=timeout) as client:
            # One page of children is enough for "did we push today" — the
            # most recent push will be among the first results when sorted
            # by Notion's default (creation order). We fetch up to 50.
            r = client.get(
                f"https://api.notion.com/v1/blocks/{page_id}/children?page_size=50",
                headers=_headers(tok),
            )
            r.raise_for_status()
            for block in r.json().get("results", []):
                if block.get("type") != "toggle":
                    continue
                rt = (block.get("toggle") or {}).get("rich_text") or []
                text = "".join((t.get("plain_text") or "") for t in rt)
                if marker in text:
                    return True
    except Exception:
        return False
    return False


def push_daily_status(date_str: str, summary_lines: list[str],
                      timeout: float = 8.0) -> dict:
    """Append a daily status toggle to the designated Notion status page.

    Args:
        date_str: YYYY-MM-DD — used as the toggle title suffix and idempotency key.
        summary_lines: bullet items to nest inside the toggle. Each becomes
            a bulleted-list child block (Notion truncates each line at 2000 chars).

    Returns: {"status": "ok"|"skipped"|"error", ...}.
    """
    tok = _token()
    if not tok:
        return {"status": "error", "error": "NOTION_TOKEN missing"}

    page_id = _status_page_id()
    if not page_id:
        return {"status": "error",
                "error": "egon-config.json.notion.status_page_id not set"}

    if _already_pushed_today(page_id, date_str, tok, timeout=timeout):
        return {"status": "skipped", "reason": "already_pushed",
                "date": date_str}

    # Notion limits: 2000 chars per rich_text element, ~100 children per call.
    def _bullet(text: str) -> dict:
        return {
            "object": "block",
            "type": "bulleted_list_item",
            "bulleted_list_item": {
                "rich_text": [{
                    "type": "text",
                    "text": {"content": (text or "")[:2000]},
                }],
            },
        }

    children = [_bullet(line) for line in (summary_lines or [])[:90]]

    toggle = {
        "object": "block",
        "type": "toggle",
        "toggle": {
            "rich_text": [{
                "type": "text",
                "text": {"content": f"Egon — {date_str}"},
            }],
            "children": children,
        },
    }

    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.patch(
                f"https://api.notion.com/v1/blocks/{page_id}/children",
                headers=_headers(tok),
                json={"children": [toggle]},
            )
            r.raise_for_status()
        return {"status": "ok", "date": date_str,
                "bullets": len(children), "page_id": page_id}
    except httpx.HTTPError as e:
        return {"status": "error", "error": f"http: {e}"}
    except Exception as e:
        return {"status": "error", "error": str(e)}
