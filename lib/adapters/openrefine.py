"""OpenRefine adapter — bulk data cleaning + reconciliation.

OpenRefine is the FOSS power-tool for messy data: dedup by clustering,
reconcile names against Wikidata/VIAF/etc, bulk transform with GREL.
Egon will use it for:
  • Routster category cleanup (cluster near-duplicate categories).
  • Mouseion author normalization ("Kim, J." vs "Jaegwon Kim").
  • Vault-wide reference dedup before Zotero/Paperpile sync.

It runs as a local server (default :3333) and exposes a small REST API.
Setup: download from openrefine.org, run `openrefine.exe` once. No
auth. Adapter probes the port; gracefully reports unconfigured when not
running so Egon's UI shows a "Start OpenRefine" hint.

Docs: https://openrefine.org/docs/technical-reference/api
"""
from __future__ import annotations

import socket

import httpx

HOST = "127.0.0.1"
PORT = 3333
BASE = f"http://{HOST}:{PORT}"

META = {
    "id": "openrefine",
    "label": "OpenRefine",
    "icon": "🧹",
    "kind": "data_cleaning",
    "needs_auth": False,
    "destructive_actions": ["delete_project"],
    "read_only_default": True,
}


def _port_open(timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((HOST, PORT), timeout=timeout):
            return True
    except Exception:
        return False


def live_status(timeout: float = 4.0) -> dict:
    if not _port_open():
        return {"status": "unconfigured",
                "error": f"OpenRefine not running on :{PORT}. "
                         "Install from openrefine.org and launch openrefine.exe."}
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.get(f"{BASE}/command/core/get-version")
        if r.status_code == 200:
            try:
                j = r.json()
                return {"status": "ok",
                        "version": j.get("full_version") or j.get("version")}
            except Exception:
                return {"status": "ok"}
        return {"status": "error", "error": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


def list_projects() -> list[dict]:
    if not _port_open():
        return []
    try:
        with httpx.Client(timeout=8) as c:
            r = c.get(f"{BASE}/command/core/get-all-project-metadata")
        if r.status_code != 200:
            return []
        meta = (r.json() or {}).get("projects") or {}
        out = []
        for pid, m in meta.items():
            out.append({
                "id": pid,
                "name": m.get("name", ""),
                "created": m.get("created"),
                "modified": m.get("modified"),
                "row_count": m.get("rowCount"),
            })
        return out
    except Exception:
        return []
