"""Demand-gated phone ADB access policy for Egon.

The phone link must be available when Bruno asks for it, but background code
must not wake ADB continuously. This module coordinates short-lived access
leases and optional downtime windows across Egon desktop, core, and Panop.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / "egon-config.json"
LEASE_FILE = ROOT / "state" / "panop" / "phone_access_lease.json"

DEFAULT_USER_TTL_S = 5 * 60
DEFAULT_LONG_TTL_S = 60 * 60
DEFAULT_DORMANT_SLEEP_S = 120


def _utc_ts(ts: float) -> str:
    return datetime.utcfromtimestamp(ts).isoformat(timespec="seconds") + "Z"


def _load_phone_cfg() -> dict:
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        cfg = {}
    phone = cfg.get("phone_link") or {}
    if not isinstance(phone, dict):
        phone = {}
    return phone


def _parse_hhmm(value: str) -> int | None:
    try:
        hh, mm = str(value).strip().split(":", 1)
        h = int(hh)
        m = int(mm)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h * 60 + m
    except Exception:
        pass
    return None


def _in_window(now: datetime, window: str) -> bool:
    if "-" not in str(window):
        return False
    start_s, end_s = str(window).split("-", 1)
    start = _parse_hhmm(start_s)
    end = _parse_hhmm(end_s)
    if start is None or end is None or start == end:
        return False
    minute = now.hour * 60 + now.minute
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end


def _active_lease(now_ts: float | None = None) -> dict | None:
    now_ts = time.time() if now_ts is None else now_ts
    try:
        lease = json.loads(LEASE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    try:
        expires_at = float(lease.get("expires_at", 0))
    except Exception:
        expires_at = 0
    if expires_at <= now_ts:
        return None
    lease["expires_in_s"] = max(0, int(expires_at - now_ts))
    return lease


def request_phone_access(owner: str, reason: str, ttl_s: int | None = None,
                         mode: str = "user") -> dict:
    ttl = int(ttl_s or DEFAULT_USER_TTL_S)
    now = time.time()
    lease = {
        "owner": str(owner or "unknown"),
        "reason": str(reason or "requested phone access"),
        "mode": str(mode or "user"),
        "created_at": _utc_ts(now),
        "expires_at": now + max(30, ttl),
        "expires_at_iso": _utc_ts(now + max(30, ttl)),
    }
    LEASE_FILE.parent.mkdir(parents=True, exist_ok=True)
    LEASE_FILE.write_text(json.dumps(lease, indent=2), encoding="utf-8")
    lease["expires_in_s"] = max(30, ttl)
    return lease


def clear_phone_access(owner: str | None = None) -> None:
    if not LEASE_FILE.exists():
        return
    if owner:
        lease = _active_lease()
        if lease and lease.get("owner") != owner:
            return
    try:
        LEASE_FILE.unlink()
    except FileNotFoundError:
        pass


def phone_access_state(background: bool = False, user_request: bool = False,
                       owner: str = "", reason: str = "",
                       ttl_s: int | None = None) -> dict:
    """Return whether an ADB caller may touch the phone now.

    `user_request=True` creates a short lease. Background callers are allowed
    only when an active lease exists or the current local time is inside a
    configured downtime window.
    """
    if user_request:
        lease = request_phone_access(owner or "user_request",
                                     reason or "explicit phone action",
                                     ttl_s=ttl_s, mode="user")
        return {
            "allowed": True,
            "source": "user_lease",
            "lease": lease,
            "sleep_s": 0,
            "message": f"Phone access granted for {lease['expires_in_s']}s.",
        }

    now_ts = time.time()
    lease = _active_lease(now_ts)
    if lease:
        return {
            "allowed": True,
            "source": "active_lease",
            "lease": lease,
            "sleep_s": 0,
            "message": (
                f"Phone access active for {lease.get('owner')} "
                f"({lease.get('expires_in_s')}s remaining)."
            ),
        }

    cfg = _load_phone_cfg()
    windows = cfg.get("downtime_windows") or cfg.get("background_windows") or []
    if isinstance(windows, str):
        windows = [windows]
    if background and cfg.get("background_enabled", False):
        now = datetime.now()
        for window in windows:
            if _in_window(now, str(window)):
                return {
                    "allowed": True,
                    "source": "downtime_window",
                    "window": str(window),
                    "sleep_s": 0,
                    "message": f"Phone access allowed during downtime window {window}.",
                }

    sleep_s = int(cfg.get("dormant_poll_interval_s") or DEFAULT_DORMANT_SLEEP_S)
    return {
        "allowed": False,
        "source": "dormant",
        "sleep_s": max(30, sleep_s),
        "message": (
            "Phone access dormant: no active user request lease and no "
            "configured downtime window is open."
        ),
    }
