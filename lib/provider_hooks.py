"""Provider-specific transcript hooks for Egon's orchestrator.

This module watches local agent-owned state for Claude Code, Codex, and
Antigravity. It does not mutate those files. It tails new transcript/log bytes,
extracts safe previews, reports agent heartbeats, forwards active-task output to
the orchestrator event stream, and reports quota-shaped failures.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from lib.orchestration_engine import (
    ROOT,
    append_task_event,
    clear_agent_cooldown,
    get_agents_cooldowns,
    is_quota_failure,
    record_agent_heartbeat,
    report_agent_failure,
)

STATE_PATH = ROOT / "state" / "provider_hooks_state.json"
WAKE_STATE_PATH = ROOT / "state" / "agent_wake" / "wake_state.json"
MAX_FILES_PER_PROVIDER = 12
MAX_BYTES_PER_TEXT_SCAN = 96_000
MAX_BYTES_PER_BINARY_SCAN = 24_000

HOME = Path.home()
APPDATA = Path(os.environ.get("APPDATA", HOME / "AppData" / "Roaming"))

PROVIDERS: dict[str, dict[str, Any]] = {
    "claude-code": {
        "text_globs": [
            str(HOME / ".claude" / "projects" / "*" / "*.jsonl"),
            str(HOME / ".claude" / "history.jsonl"),
            str(APPDATA / "Claude" / "logs" / "*.log"),
        ],
        "binary_globs": [],
    },
    "codex": {
        "text_globs": [
            str(HOME / ".codex" / "sessions" / "*" / "*" / "*" / "*.jsonl"),
            str(HOME / ".codex" / "session_index.jsonl"),
        ],
        "binary_globs": [],
    },
    "antigravity": {
        "text_globs": [
            str(HOME / ".gemini" / "antigravity-ide" / "code_tracker" / "active" / "*" / "*.toml"),
            str(HOME / ".gemini" / "antigravity-ide" / "code_tracker" / "active" / "*" / "*.py"),
        ],
        "binary_globs": [
            str(HOME / ".gemini" / "antigravity-ide" / "conversations" / "*.pb"),
            str(HOME / ".gemini" / "antigravity-ide" / "conversations" / "*.tmp"),
        ],
    },
}

QUOTA_RESET_KEYS = ("resets_at", "reset_at", "reset_time")
UUID_RE = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.I)


def _now() -> int:
    return int(time.time())


def _load_state() -> dict:
    try:
        if STATE_PATH.exists():
            with STATE_PATH.open("r", encoding="utf-8") as f:
                body = json.load(f)
                if isinstance(body, dict):
                    return body
    except Exception:
        pass
    return {"files": {}, "providers": {}, "last_scan_at": 0}


def _save_state(state: dict) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=True, indent=2)
        tmp.replace(STATE_PATH)
    except Exception:
        pass


def _clip(text: Any, limit: int = 1600) -> str:
    s = " ".join(str(text or "").replace("\x00", " ").split())
    if len(s) <= limit:
        return s
    return s[: max(0, limit - 1)].rstrip() + "..."


def _provider_files(provider: str, binary: bool = False) -> list[Path]:
    spec = PROVIDERS.get(provider, {})
    key = "binary_globs" if binary else "text_globs"
    paths: list[Path] = []
    for pattern in spec.get(key, []):
        for match in glob.glob(pattern, recursive=True):
            try:
                p = Path(match)
                if p.is_file():
                    paths.append(p)
            except Exception:
                continue
    paths.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    return paths[:MAX_FILES_PER_PROVIDER]


def _active_task_for_agent(agent_name: str) -> int | None:
    from lib.orchestration_engine import DB_PATH

    try:
        conn = sqlite3.connect(DB_PATH, timeout=4)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                """SELECT s.current_task_id
                   FROM orchestrator_agent_state s
                   JOIN orchestrator_tasks t ON t.id = s.current_task_id
                   WHERE s.agent_name = ?
                     AND s.current_task_id IS NOT NULL
                     AND t.status IN ('pending','assigned','paused','needs_clarification')""",
                (agent_name,),
            ).fetchone()
            if row and row["current_task_id"]:
                return int(row["current_task_id"])
            row = conn.execute(
                """SELECT id FROM orchestrator_tasks
                   WHERE agent_name = ? AND status = 'assigned'
                   ORDER BY updated_at DESC LIMIT 1""",
                (agent_name,),
            ).fetchone()
            return int(row["id"]) if row else None
        finally:
            conn.close()
    except Exception:
        return None


def _provider_session_id(provider: str, path: Path) -> int | None:
    if provider != "codex":
        return None
    match = UUID_RE.search(path.name)
    if not match:
        return None
    external_id = match.group(1)
    from lib.orchestration_engine import DB_PATH

    try:
        conn = sqlite3.connect(DB_PATH, timeout=4)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                """SELECT s.id
                   FROM sessions s
                   JOIN agents ag ON ag.id = s.agent_id
                   WHERE ag.name = 'codex' AND s.external_id = ?
                   ORDER BY s.started_at DESC LIMIT 1""",
                (external_id,),
            ).fetchone()
            return int(row["id"]) if row else None
        finally:
            conn.close()
    except Exception:
        return None


def _codex_thread_id_from_path(path: Path) -> str | None:
    match = UUID_RE.search(path.name)
    return match.group(1).lower() if match else None


def _codex_wake_thread_for_task(entry: dict) -> str | None:
    stdout_path = entry.get("stdout_path")
    if not stdout_path:
        return None
    try:
        p = Path(stdout_path)
        if not p.exists():
            return None
        with p.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if "thread.started" not in line:
                    continue
                try:
                    item = json.loads(line)
                except Exception:
                    continue
                thread_id = item.get("thread_id")
                if thread_id:
                    return str(thread_id).lower()
    except Exception:
        return None
    return None


def _task_for_provider_event(provider: str, path: Path, state: dict) -> tuple[int | None, str]:
    if provider != "codex":
        return _active_task_for_agent(provider), "active_agent"

    path_thread_id = _codex_thread_id_from_path(path)
    wake_tasks = {}
    try:
        if WAKE_STATE_PATH.exists():
            wake_state = json.loads(WAKE_STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(wake_state, dict):
                wake_tasks = wake_state.get("tasks") or {}
    except Exception:
        wake_tasks = {}

    has_running_codex_wake = False
    for raw_entry in wake_tasks.values():
        entry = raw_entry if isinstance(raw_entry, dict) else {}
        if entry.get("agent") != "codex" or entry.get("status") != "running":
            continue
        has_running_codex_wake = True
        task_id = entry.get("task_id")
        wake_thread_id = _codex_wake_thread_for_task(entry)
        if path_thread_id and wake_thread_id and path_thread_id == wake_thread_id:
            return int(task_id), "wake_thread"

    if has_running_codex_wake and path_thread_id:
        suppressed = state.setdefault("providers", {}).setdefault(provider, {}).setdefault("suppressed_paths", {})
        suppressed[str(path)] = _now()
        return None, "suppressed_other_codex_thread"
    if has_running_codex_wake:
        suppressed = state.setdefault("providers", {}).setdefault(provider, {}).setdefault("suppressed_paths", {})
        suppressed[str(path)] = _now()
        return None, "suppressed_unattributed_codex_file"

    return _active_task_for_agent(provider), "active_agent"


def _event_ts(raw_ts: Any) -> int:
    if isinstance(raw_ts, (int, float)):
        return int(raw_ts)
    text = str(raw_ts or "").strip()
    if not text:
        return _now()
    try:
        from datetime import datetime

        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
    except Exception:
        return _now()


def _ingest_token_ledger(provider: str, path: Path, meta: dict) -> None:
    if provider != "codex":
        return
    usage = meta.get("token_usage")
    if not isinstance(usage, dict):
        return
    sid = _provider_session_id(provider, path)
    if sid is None:
        return
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    cache_read = int(usage.get("cached_input_tokens") or 0)
    if input_tokens <= 0 and output_tokens <= 0:
        return
    ts = _event_ts(meta.get("timestamp"))
    model = str(meta.get("model") or "codex")
    from lib.orchestration_engine import DB_PATH

    try:
        conn = sqlite3.connect(DB_PATH, timeout=4)
        try:
            conn.execute(
                """INSERT OR IGNORE INTO turns_ledger
                   (session_id, ts, model, input_tokens, output_tokens,
                    cache_write_tokens, cache_read_tokens, tools)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (sid, ts, model, input_tokens, output_tokens, 0, cache_read, "provider_hooks"),
            )
            conn.execute(
                """INSERT INTO activity (session_id, ts, kind, payload_json)
                   VALUES (?, ?, 'token_ledger_ingest', ?)""",
                (
                    sid,
                    ts,
                    json.dumps({
                        "source": "provider_hooks",
                        "path": str(path),
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "cache_read_tokens": cache_read,
                    }, ensure_ascii=True),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _ingest_context_marker(provider: str, path: Path, text: str, meta: dict) -> None:
    if provider != "codex" or "/context/v2" not in str(text):
        return
    sid = _provider_session_id(provider, path)
    if sid is None:
        return
    ts = _event_ts(meta.get("timestamp"))
    from lib.orchestration_engine import DB_PATH

    payload = {
        "project": "egon",
        "broker_version": "context-broker-v2",
        "source": "provider_hooks_context_marker",
        "path": str(path),
    }
    try:
        conn = sqlite3.connect(DB_PATH, timeout=4)
        try:
            exists = conn.execute(
                "SELECT 1 FROM activity WHERE session_id = ? AND kind = 'mind_context' LIMIT 1",
                (sid,),
            ).fetchone()
            if not exists:
                conn.execute(
                    """INSERT INTO activity (session_id, ts, kind, payload_json)
                       VALUES (?, ?, 'mind_context', ?)""",
                    (sid, ts, json.dumps(payload, ensure_ascii=False)),
                )
                conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _json_text_parts(value: Any, depth: int = 0) -> list[str]:
    if depth > 4:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        out: list[str] = []
        for item in value[:20]:
            out.extend(_json_text_parts(item, depth + 1))
        return out
    if isinstance(value, dict):
        preferred = []
        for key in ("message", "text", "output", "content", "lastPrompt", "customTitle", "aiTitle"):
            if key in value:
                preferred.extend(_json_text_parts(value.get(key), depth + 1))
        if preferred:
            return preferred
        out: list[str] = []
        for key, item in list(value.items())[:30]:
            if key in {"encrypted_content", "id", "uuid", "call_id", "sessionId", "requestId"}:
                continue
            out.extend(_json_text_parts(item, depth + 1))
        return out
    return []


def _extract_json_line(provider: str, line: str) -> tuple[str, dict]:
    try:
        event = json.loads(line)
    except Exception:
        return _clip(line), {}

    payload = event.get("payload") if isinstance(event, dict) else None
    meta = {"raw_type": event.get("type") if isinstance(event, dict) else None}
    if isinstance(event, dict):
        meta["timestamp"] = event.get("timestamp")

    if provider == "codex" and isinstance(payload, dict):
        ptype = payload.get("type")
        meta["payload_type"] = ptype
        if ptype == "token_count":
            info = payload.get("info") or {}
            rate_limits = payload.get("rate_limits") or {}
            meta["token_usage"] = info.get("last_token_usage") or info.get("total_token_usage")
            meta["rate_limits"] = rate_limits
            return _clip(f"Codex token/rate status: {rate_limits}"), meta
        if ptype == "agent_message":
            return _clip(payload.get("message")), meta
        if ptype == "function_call_output":
            return _clip(payload.get("output")), meta
        if ptype == "message":
            return _clip(" ".join(_json_text_parts(payload.get("content")))), meta

    if provider == "claude-code" and isinstance(event, dict):
        if isinstance(event.get("message"), dict):
            msg = event["message"]
            meta["role"] = msg.get("role")
            meta["usage"] = msg.get("usage")
            return _clip(" ".join(_json_text_parts(msg.get("content")))), meta
        if event.get("type") in {"last-prompt", "custom-title", "ai-title", "mode"}:
            return _clip(" ".join(_json_text_parts(event))), meta

    return _clip(" ".join(_json_text_parts(event))), meta


def _quota_cooldown_seconds(provider: str, meta: dict, default: int = 1800) -> int:
    rate_limits = meta.get("rate_limits") if isinstance(meta, dict) else None
    if provider == "codex" and isinstance(rate_limits, dict):
        reached = rate_limits.get("rate_limit_reached_type")
        buckets = [rate_limits.get("primary"), rate_limits.get("secondary")]
        reset_times = []
        for bucket in buckets:
            if isinstance(bucket, dict):
                for key in QUOTA_RESET_KEYS:
                    value = bucket.get(key)
                    if isinstance(value, (int, float)) and value > time.time():
                        reset_times.append(int(value))
        if reached and reset_times:
            return max(60, min(24 * 3600, min(reset_times) - _now()))
    return default


def _rate_limit_reached(provider: str, meta: dict) -> bool:
    if provider != "codex":
        return False
    rate_limits = meta.get("rate_limits") if isinstance(meta, dict) else None
    if not isinstance(rate_limits, dict):
        return False
    if rate_limits.get("rate_limit_reached_type"):
        return True
    primary = rate_limits.get("primary")
    return isinstance(primary, dict) and float(primary.get("used_percent") or 0) >= 100.0


def _provider_quota_signal(provider: str, text: str, meta: dict) -> bool:
    if provider == "codex":
        if _rate_limit_reached(provider, meta):
            return True
        payload_type = str(meta.get("payload_type") or "").lower()
        raw_type = str(meta.get("raw_type") or "").lower()
        error_shaped = payload_type in {"error", "api_error"} or raw_type in {"error", "api_error"}
        return error_shaped and is_quota_failure(text)
    if provider == "claude-code":
        raw_type = str(meta.get("raw_type") or "").lower()
        role = str(meta.get("role") or "").lower()
        error_shaped = raw_type in {"error", "api_error"} or "error" in raw_type or role == "error"
        low = str(text or "").lower()
        log_error = "[error]" in low or " error " in low or "api_error" in low
        return (error_shaped or log_error) and is_quota_failure(text)
    return is_quota_failure(text)


def _emit_provider_event(provider: str, text: str, path: Path, meta: dict, state: dict) -> dict:
    if not text:
        return {"emitted": 0, "quota": 0}

    _ingest_token_ledger(provider, path, meta)
    _ingest_context_marker(provider, path, text, meta)
    providers = state.setdefault("providers", {})
    pstate = providers.setdefault(provider, {})
    task_id, attribution = _task_for_provider_event(provider, path, state)
    event_type = "provider_output"
    quota = _provider_quota_signal(provider, text, meta)
    if quota:
        event_type = "provider_quota"
        seconds = _quota_cooldown_seconds(provider, meta)
        report_agent_failure(provider, text, cooldown_seconds=seconds)
        pstate["last_quota_at"] = _now()
        pstate["last_quota_preview"] = _clip(text, 500)
    else:
        pstate["last_success_at"] = _now()

    payload = {
        "provider": provider,
        "path": str(path),
        "attribution": attribution,
        "meta": meta,
    }
    if task_id is not None:
        append_task_event(task_id, provider, event_type, text, payload)
        record_agent_heartbeat(provider, task_id, event_type, text)
    else:
        pstate["last_suppressed_at"] = _now()
        pstate["last_suppressed_path"] = str(path)
        pstate["last_suppressed_reason"] = attribution
    pstate["last_seen_at"] = _now()
    pstate["last_path"] = str(path)
    pstate["last_preview"] = _clip(text, 500)
    return {"emitted": 1 if task_id is not None else 0, "quota": 1 if quota else 0}


def _scan_text_file(provider: str, path: Path, state: dict) -> dict:
    files = state.setdefault("files", {})
    key = str(path)
    try:
        size = path.stat().st_size
        mtime = int(path.stat().st_mtime)
    except Exception:
        return {"emitted": 0, "quota": 0}

    rec = files.setdefault(key, {"provider": provider, "offset": None, "size": 0, "mtime": 0})
    offset = rec.get("offset")
    if offset is None:
        rec.update({"provider": provider, "offset": size, "size": size, "mtime": mtime, "binary": False})
        return {"emitted": 0, "quota": 0}
    if size < int(offset or 0):
        offset = max(0, size - 8192)
    if size == int(offset or 0) and mtime == int(rec.get("mtime") or 0):
        return {"emitted": 0, "quota": 0}

    read_from = max(0, int(offset or 0))
    if size - read_from > MAX_BYTES_PER_TEXT_SCAN:
        read_from = max(0, size - MAX_BYTES_PER_TEXT_SCAN)
    try:
        with path.open("rb") as f:
            f.seek(read_from)
            chunk = f.read(max(0, size - read_from)).decode("utf-8", errors="replace")
    except Exception:
        return {"emitted": 0, "quota": 0}

    emitted = 0
    quota = 0
    for line in chunk.splitlines():
        if not line.strip():
            continue
        text, meta = _extract_json_line(provider, line)
        if len(text) < 8:
            continue
        result = _emit_provider_event(provider, text, path, meta, state)
        emitted += result["emitted"]
        quota += result["quota"]

    rec.update({"provider": provider, "offset": size, "size": size, "mtime": mtime, "binary": False})
    return {"emitted": emitted, "quota": quota}


PRINTABLE_RE = re.compile(rb"[ -~]{12,}")


def _scan_binary_file(provider: str, path: Path, state: dict) -> dict:
    files = state.setdefault("files", {})
    key = str(path)
    try:
        stat = path.stat()
        size = stat.st_size
        mtime = int(stat.st_mtime)
    except Exception:
        return {"emitted": 0, "quota": 0}
    first_seen = key not in files
    rec = files.setdefault(key, {"provider": provider, "size": 0, "mtime": 0, "signature": ""})
    if size == int(rec.get("size") or 0) and mtime == int(rec.get("mtime") or 0):
        return {"emitted": 0, "quota": 0}
    try:
        with path.open("rb") as f:
            f.seek(max(0, size - MAX_BYTES_PER_BINARY_SCAN))
            data = f.read(MAX_BYTES_PER_BINARY_SCAN)
    except Exception:
        return {"emitted": 0, "quota": 0}
    strings = [m.group(0).decode("utf-8", errors="replace") for m in PRINTABLE_RE.finditer(data)]
    preview = _clip(" | ".join(strings[-12:]) or f"{path.name} changed ({size} bytes)", 1600)
    signature = hashlib.sha256(preview.encode("utf-8", errors="replace")).hexdigest()
    if first_seen:
        rec.update({"provider": provider, "size": size, "mtime": mtime, "signature": signature, "binary": True})
        return {"emitted": 0, "quota": 0}
    if signature == rec.get("signature"):
        rec.update({"size": size, "mtime": mtime, "binary": True})
        return {"emitted": 0, "quota": 0}
    result = _emit_provider_event(provider, preview, path, {"binary": True}, state)
    rec.update({"provider": provider, "size": size, "mtime": mtime, "signature": signature, "binary": True})
    return result


def scan_provider_hooks() -> dict:
    """Scan provider state once and forward new activity into the orchestrator."""
    state = _load_state()
    total = {"emitted": 0, "quota": 0, "providers": {}, "ts": _now()}
    for provider in PROVIDERS:
        p_total = {"emitted": 0, "quota": 0, "text_files": 0, "binary_files": 0}
        for path in _provider_files(provider, binary=False):
            result = _scan_text_file(provider, path, state)
            p_total["emitted"] += result["emitted"]
            p_total["quota"] += result["quota"]
            p_total["text_files"] += 1
        for path in _provider_files(provider, binary=True):
            result = _scan_binary_file(provider, path, state)
            p_total["emitted"] += result["emitted"]
            p_total["quota"] += result["quota"]
            p_total["binary_files"] += 1
        total["emitted"] += p_total["emitted"]
        total["quota"] += p_total["quota"]
        total["providers"][provider] = p_total

    _clear_recovered_cooldowns(state)
    state["last_scan_at"] = _now()
    _save_state(state)
    return {"status": "ok", **total}


def _clear_recovered_cooldowns(state: dict) -> None:
    try:
        cooldowns = get_agents_cooldowns()
    except Exception:
        cooldowns = {}
    providers = state.get("providers") or {}
    for provider, cooldown in cooldowns.items():
        pstate = providers.get(provider) or {}
        last_success = int(pstate.get("last_success_at") or 0)
        last_quota = int(pstate.get("last_quota_at") or 0)
        if last_success and last_success > last_quota:
            try:
                clear_agent_cooldown(provider)
                pstate["last_recovered_at"] = _now()
            except Exception:
                pass


def provider_hooks_status() -> dict:
    state = _load_state()
    files = state.get("files") or {}
    providers = state.get("providers") or {}
    watched = {}
    for provider in PROVIDERS:
        watched[provider] = {
            "text_candidates": len(_provider_files(provider, binary=False)),
            "binary_candidates": len(_provider_files(provider, binary=True)),
            "state": providers.get(provider, {}),
        }
    return {
        "status": "ok",
        "last_scan_at": state.get("last_scan_at"),
        "tracked_files": len(files),
        "providers": watched,
    }
