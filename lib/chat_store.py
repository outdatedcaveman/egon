"""Chat sessions store — separate conversations, shared across PC + phone.

Bruno 2026-07-04: "It still doesn't separate sessions though." One endless
thread is not a chat surface. This is the single implementation both the
desktop widget and the phone endpoints use (one store, no drift):

    state/chat_sessions/<id>.json     — one conversation each: {id, title,
                                        updated_at, messages:[{role,content}]}
    state/chat_current.txt            — id of the conversation in focus,
                                        shared across devices

Titles derive from the first user message. The legacy single-thread file
(chat_history.json) and earlier Clear-archives (chat_*.json flat lists) are
migrated in place on first use — nothing is lost. Attachment payloads are
elided on save (blobs don't belong in the store).
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from lib import egon_paths

SESS_DIR = egon_paths.STATE_DIR / "chat_sessions"
CURRENT = egon_paths.STATE_DIR / "chat_current.txt"
LEGACY = egon_paths.STATE_DIR / "chat_history.json"


def _atomic_write(path: Path, text: str) -> None:
    """tmp + os.replace: desktop, phone server and egon_core all write these
    files from separate PROCESSES — a torn write would corrupt a conversation
    (2026-07-05 audit). os.replace is atomic on NTFS."""
    import os
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _elide(messages: list) -> list:
    out = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        c = m.get("content")
        if isinstance(c, list):
            c = [dict(p, data="", elided=True) if isinstance(p, dict) and p.get("data")
                 else p for p in c]
        out.append({"role": m.get("role", "user"), "content": c})
    return out


def _title_for(messages: list) -> str:
    for m in messages or []:
        if isinstance(m, dict) and m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, list):
                c = " ".join(p.get("text", "") for p in c
                             if isinstance(p, dict) and p.get("type") == "text")
            t = " ".join(str(c or "").split())
            if t:
                return t[:60]
    return "(empty conversation)"


def _new_id() -> str:
    # ns suffix: two sessions created in the same second must not collide
    return time.strftime("%Y%m%d_%H%M%S") + f"_{time.time_ns() % 100000}"


def _path(sid: str) -> Path:
    safe = "".join(ch for ch in sid if ch.isalnum() or ch in "_-")[:40]
    return SESS_DIR / f"{safe}.json"


def _migrate() -> None:
    """Adopt legacy stores: the single-thread file and old flat-list archives
    become proper sessions. Idempotent, never deletes (legacy file is renamed
    with a .migrated suffix so it can't double-import)."""
    SESS_DIR.mkdir(parents=True, exist_ok=True)
    # old Clear-archives: chat_YYYYmmdd_HHMMSS.json holding a bare list
    for f in SESS_DIR.glob("chat_*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(data, list):        # flat list -> wrap as session
                sid = f.stem.replace("chat_", "") or _new_id()
                _atomic_write(_path(sid), json.dumps({
                    "id": sid, "title": _title_for(data),
                    "updated_at": int(f.stat().st_mtime),
                    "messages": _elide(data)}, ensure_ascii=False))
                f.rename(f.with_suffix(".json.migrated"))
        except Exception:
            continue
    if LEGACY.exists():
        try:
            data = json.loads(LEGACY.read_text(encoding="utf-8"))
            if isinstance(data, list) and data:
                sid = _new_id()
                save(sid, data)
                set_current(sid)
            LEGACY.rename(LEGACY.with_suffix(".json.migrated"))
        except Exception:
            pass


def list_sessions() -> list[dict]:
    """Newest first: [{id, title, updated_at, count}]."""
    _migrate()
    out = []
    for f in SESS_DIR.glob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(d, dict) and "messages" in d:
                out.append({"id": d.get("id") or f.stem,
                            "title": d.get("title") or _title_for(d["messages"]),
                            "updated_at": d.get("updated_at") or int(f.stat().st_mtime),
                            "count": len(d["messages"])})
        except Exception:
            continue
    out.sort(key=lambda s: -(s["updated_at"] or 0))
    return out


def load(sid: str) -> list:
    p = _path(sid)
    try:
        if p.exists():
            d = json.loads(p.read_text(encoding="utf-8"))
            return d.get("messages") or []
    except Exception:
        pass
    return []


def save(sid: str, messages: list) -> None:
    SESS_DIR.mkdir(parents=True, exist_ok=True)
    msgs = _elide(messages)
    _atomic_write(_path(sid), json.dumps({
        "id": sid, "title": _title_for(msgs),
        "updated_at": int(time.time()), "messages": msgs},
        ensure_ascii=False))


def new_session() -> str:
    sid = _new_id()
    save(sid, [])
    set_current(sid)
    return sid


def current_id() -> str:
    _migrate()
    try:
        sid = CURRENT.read_text(encoding="utf-8").strip()
        if sid and _path(sid).exists():
            return sid
    except Exception:
        pass
    sessions = list_sessions()
    if sessions:
        set_current(sessions[0]["id"])
        return sessions[0]["id"]
    return new_session()


def set_current(sid: str) -> None:
    try:
        CURRENT.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(CURRENT, sid)
    except Exception:
        pass


def mtime_signature() -> float:
    """Change signal for watchers: max mtime across pointer + session files."""
    sig = 0.0
    try:
        if CURRENT.exists():
            sig = max(sig, CURRENT.stat().st_mtime)
        for f in SESS_DIR.glob("*.json"):
            sig = max(sig, f.stat().st_mtime)
    except Exception:
        pass
    return sig
