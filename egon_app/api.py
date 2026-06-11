"""Loopback HTTP helpers for the Egon UI — urllib, zero SSL setup.

Why this exists (2026-06-11 perf post-mortem): UI pages and their workers
called `httpx.Client()` / `httpx.post()` per request. On Windows, every new
httpx client builds an SSL context from the system cert store — measured at
~2.8s — even for plain http:// loopback calls that never use TLS. Three of
those during HomePage.__init__ made the window take 17s to appear; one-shot
`httpx.post` calls inside inbox actions made every button feel stuck.

These helpers use urllib (no SSL machinery for http://) and are safe from
any thread. ONLY for 127.0.0.1 services (mind :8000, panop, devtools :9222);
external HTTP should keep using httpx via lib.lazy_httpx.
"""
from __future__ import annotations

import json
import urllib.request
from typing import Any


def get_json(url: str, timeout: float = 5.0) -> Any | None:
    """GET → parsed JSON, or None on any failure."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            if 200 <= r.status < 300:
                return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None
    return None


def post_json(url: str, payload: dict | None = None,
              timeout: float = 5.0) -> Any | None:
    """POST json → parsed JSON (or {} for empty 2xx), None on failure."""
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload or {}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if 200 <= r.status < 300:
                body = r.read().decode("utf-8", "replace").strip()
                return json.loads(body) if body else {}
    except Exception:
        return None
    return None


class _Resp:
    """Minimal httpx.Response stand-in (status_code + .json()) so call sites
    that did `r = httpx.post(...); r.status_code` keep working unchanged."""

    def __init__(self, status_code: int, body: str):
        self.status_code = status_code
        self._body = body

    def json(self) -> Any:
        return json.loads(self._body) if self._body.strip() else {}


def post_compat(url: str, json_payload: dict | None = None,
                timeout: float = 5.0) -> _Resp:
    """Drop-in for one-shot `httpx.post(url, json=..., timeout=...)`."""
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(json_payload or {}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return _Resp(r.status, r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return _Resp(e.code, "")
    except Exception:
        return _Resp(599, "")


def get_compat(url: str, timeout: float = 5.0) -> _Resp:
    """Drop-in for one-shot `httpx.get(url, timeout=...)`."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return _Resp(r.status, r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return _Resp(e.code, "")
    except Exception:
        return _Resp(599, "")
