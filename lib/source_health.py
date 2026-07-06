"""Honest per-source health for the data adapters.

Bruno 2026-07-06: the Media page (and every status surface) showed a wall of
green "ok" even when a source's last refresh had timed out or errored — an
adapter's live_status() returns status="ok" while carrying an error string, so
stale data looked current ("all the databases are broken/outdated and I can't
tell which"). This module derives an HONEST health from two objective signals:

  1. Did the LAST PROBE actually succeed?  (last_pass sources[id]: a non-empty
     `error`, or status in {error,timeout}, means the refresh did NOT succeed —
     regardless of a stated status="ok".)
  2. How OLD is the newest stored snapshot?  (read cheaply from the dated
     filename `state/snapshots/<id>/<YYYY-MM-DD>.json` — no big-file parse.)

Health values: fresh 🟢 · stale 🟡 · failed 🔴 · unconfigured ⚙ · off ⚪.
Pure logic + tiny filesystem globs; safe to call from a worker thread.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

FRESH_HOURS = 48.0  # data newer than this AND a clean probe → fresh

# Snapshot files are json.dumps(indent=2) with metadata keys FIRST, then a huge
# items[] array. We only need synced_at/status — read the small header, never
# parse multi-MB item lists (that took 17s across 7 sources).
_HEAD_BYTES = 8192
_RE_SYNCED = re.compile(r'"(?:synced_at|captured_at|generated_at)"\s*:\s*"([^"]+)"')
_RE_STATUS = re.compile(r'"status"\s*:\s*"([^"]+)"')
_RE_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def _peek_meta(path) -> dict:
    """{synced_at, status} from the file header only — a bounded 8 KB read."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(_HEAD_BYTES).decode("utf-8", "ignore")
    except Exception:
        return {}
    ms, mt = _RE_SYNCED.search(head), _RE_STATUS.search(head)
    return {"synced_at": ms.group(1) if ms else None,
            "status": (mt.group(1) if mt else "").lower()}


def _data_synced_at(source: str) -> tuple[str | None, bool]:
    """(synced_at_iso, has_data) for the newest snapshot that actually holds
    data — skips error/timeout/unconfigured files so a dated-but-failed probe
    file can't masquerade as fresh. Header-only reads; fast enough for many
    sources, but still call off the UI thread."""
    try:
        from lib.snapshot_store import list_snapshots
        files = list_snapshots(source)          # newest first
    except Exception:
        return None, False
    for f in files:
        meta = _peek_meta(f)
        if meta.get("status") in ("error", "timeout", "unconfigured"):
            continue
        ts = meta.get("synced_at")
        if not ts:
            m = _RE_DATE.search(f.name)          # fall back to the dated filename
            ts = m.group(1) if m else None
        return ts, True
    return None, False


def _age_hours(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        s = str(ts).replace("Z", "+00:00")
        d = datetime.fromisoformat(s) if ("T" in s or "-" in s[4:]) \
            else datetime.strptime(s, "%Y-%m-%d")
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - d).total_seconds() / 3600.0)
    except Exception:
        return None


def _fmt_age(hours: float | None) -> str:
    if hours is None:
        return "never"
    if hours < 24:
        return f"{int(round(hours))}h ago"
    return f"{int(round(hours / 24))}d ago"


def _h(source, health, emoji, synced_at, age, reason, error) -> dict:
    return {"source": source, "health": health, "emoji": emoji,
            "synced_at": synced_at, "age_hours": age, "age_label": _fmt_age(age),
            "reason": reason, "error": (error or "")[:200]}


def classify(source: str, lp_source: dict | None = None,
             fresh_hours: float = FRESH_HOURS) -> dict:
    """Honest health for one source id. `lp_source` is the last_pass
    sources[id] entry; pass {} or None to have it read (cached)."""
    if lp_source is None:
        try:
            from lib.state import load_last_pass
            lp_source = (load_last_pass().get("sources") or {}).get(source) or {}
        except Exception:
            lp_source = {}
    status = str(lp_source.get("status") or "").lower()
    error = str(lp_source.get("error") or "").strip()
    ts, has_data = _data_synced_at(source)
    age = _age_hours(ts)

    # deliberate / setup states pass through untouched
    if status == "off":
        return _h(source, "off", "⚪", ts, age, "disabled", error)
    if status == "unconfigured":
        return _h(source, "unconfigured", "⚙", ts, age, "needs setup", error)

    # THE honesty rule: an error string (even alongside status="ok") means the
    # last refresh did not cleanly succeed.
    probe_failed = status in ("error", "timeout") or bool(error)

    if not has_data:
        why = (error or status or "no snapshot yet") if probe_failed else "no snapshot yet"
        return _h(source, "failed", "🔴", ts, age, why, error)

    old = age is not None and age > fresh_hours
    fail = (error or status)[:70]
    # Separate the two honest signals: how OLD the data is vs whether the last
    # REFRESH worked. Old data → stale; recent data but a failing refresh →
    # degraded (working now, silently breaking); both → stale (worst-case).
    if old:
        why = f"data {_fmt_age(age)}" + (f"; last refresh failed: {fail}" if probe_failed
                                         else " (no recent refresh)")
        return _h(source, "stale", "🟡", ts, age, why, error)
    if probe_failed:
        return _h(source, "degraded", "🟠",
                  ts, age, f"data {_fmt_age(age)} but last refresh failed: {fail}", error)
    return _h(source, "fresh", "🟢", ts, age, f"synced {_fmt_age(age)}", error)


def for_sources(pairs: list[tuple[str, str]],
                fresh_hours: float = FRESH_HOURS) -> list[dict]:
    """pairs: [(label, source_id), ...]. Reads last_pass ONCE. Returns each
    health dict with 'label' added, in the given order."""
    try:
        from lib.state import load_last_pass
        lp = load_last_pass().get("sources") or {}
    except Exception:
        lp = {}
    out = []
    for label, sid in pairs:
        h = classify(sid, lp.get(sid) or {}, fresh_hours)
        h["label"] = label
        out.append(h)
    return out


def has_recent_data(source: str, max_hours: float = 72.0) -> bool:
    """True if `source` has a stored snapshot with real data no older than
    max_hours. Cheap (header-only reads). Adapters use this to report OK from
    a fresh harvest when a live re-check is slow/flaky, instead of degrading a
    source whose data is actually fine (Bruno 2026-07-06)."""
    ts, has_data = _data_synced_at(source)
    if not has_data:
        return False
    age = _age_hours(ts)
    return age is None or age <= max_hours


def is_healthy(lp_source: dict) -> bool:
    """Honest replacement for `status in ('ok','alive')` used in pass summaries:
    a source with a non-empty error did NOT succeed this pass."""
    status = str((lp_source or {}).get("status") or "").lower()
    return status in ("ok", "alive") and not str((lp_source or {}).get("error") or "").strip()


def counts(healths: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for h in healths:
        out[h["health"]] = out.get(h["health"], 0) + 1
    return out
