"""Insight-card distillation for extracted full-text documents.

Builds one durable `insight_card` memory per extracted document. This is a
bulk worker: writes go directly to mind.db with marker-line idempotency, never
through per-item HTTP.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from lib import egon_paths

ROOT = egon_paths.EGON_ROOT
STATE_DIR = egon_paths.STATE_DIR
STATE_FILE = STATE_DIR / "insight_cards_state.json"
DB_PATH = STATE_DIR / "mind.db"
EXTRACT_ROOTS = (
    egon_paths.FILE_EXTRACTS_DIR,
    STATE_DIR / "mind_archive" / "_extracts",
)

MODEL_PROVIDER = "claude"
MODEL_NAME = "claude-haiku-4-5-20251001"
DEFAULT_DAILY_LIMIT = 300
MAX_DOC_CHARS = int(os.environ.get("EGON_CARD_MAX_DOC_CHARS", "18000"))
MIN_TEXT_CHARS = int(os.environ.get("EGON_CARD_MIN_TEXT_CHARS", "400"))

_TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".json", ".jsonl", ".csv", ".tsv",
    ".rst", ".log",
}
_SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(api[_-]?key|secret|token|password)\b\s*[:=]\s*['\"]?[A-Za-z0-9_\-./+=]{16,}"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bsk-proj-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
]
_REDACTIONS = [
    (re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"), "[EMAIL]"),
    (re.compile(r"\b(?:\+?\d[\d\s().-]{7,}\d)\b"), "[PHONE]"),
    (re.compile(r"\b(?:[A-Za-z]:\\|/Users/|/home/)[^\s\"'<>]{3,}"), "[LOCAL_PATH]"),
    (re.compile(r"\bsk-proj-[A-Za-z0-9_-]{10,}\b"), "[OPENAI_KEY]"),
    (re.compile(r"\bsk-[A-Za-z0-9]{10,}\b"), "[OPENAI_KEY]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{10,}\b"), "[GITHUB_TOKEN]"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "[SLACK_TOKEN]"),
    (
        re.compile(r"(?i)\b(api[_-]?key|secret|token|password)\b\s*[:=]\s*['\"]?[^ \n\r\t,'\"]{8,}"),
        r"\1=[REDACTED]",
    ),
]


@dataclass(frozen=True)
class Document:
    path: Path
    rel: str
    doc_id: str
    sha1: str
    size: int
    mtime: int


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _read_state() -> dict[str, Any]:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=STATE_FILE.name + ".", suffix=".tmp", dir=str(STATE_FILE.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=True, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, STATE_FILE)
    finally:
        try:
            Path(tmp).unlink(missing_ok=True)
        except Exception:
            pass


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _daily_limit() -> int:
    raw = os.environ.get("EGON_CARDS_PER_DAY", str(DEFAULT_DAILY_LIMIT))
    try:
        return max(0, int(raw))
    except Exception:
        return DEFAULT_DAILY_LIMIT


def _normalize_state(state: dict[str, Any]) -> dict[str, Any]:
    today = _today()
    state.setdefault("version", 1)
    state.setdefault("documents", {})
    state.setdefault("daily", {})
    daily = state["daily"] if isinstance(state.get("daily"), dict) else {}
    if daily.get("date") != today:
        daily = {"date": today, "cards": 0, "skipped": 0, "errors": 0}
    state["daily"] = daily
    return state


def _doc_id(rel: str) -> str:
    return hashlib.sha1(rel.encode("utf-8", "replace")).hexdigest()[:20]


def _sha1_file(path: Path, cap: int = 4 * 1024 * 1024) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        remaining = cap
        while remaining > 0:
            chunk = f.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            h.update(chunk)
            remaining -= len(chunk)
    return h.hexdigest()


def _iter_documents() -> list[Document]:
    docs: list[Document] = []
    seen: set[str] = set()
    for root in EXTRACT_ROOTS:
        if not root.exists():
            continue
        root_name = "file_extracts" if root == egon_paths.FILE_EXTRACTS_DIR else "mind_archive/_extracts"
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in _TEXT_EXTENSIONS:
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            rel = f"{root_name}/{path.relative_to(root).as_posix()}"
            if rel in seen:
                continue
            seen.add(rel)
            try:
                sha1 = _sha1_file(path)
            except OSError:
                continue
            docs.append(Document(path, rel, _doc_id(rel), sha1, int(st.st_size), int(st.st_mtime)))
    docs.sort(key=lambda d: d.rel)
    return docs


def _sample_text(text: str, limit: int = MAX_DOC_CHARS) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    part = max(1000, limit // 3)
    head = text[:part]
    mid_start = max(part, (len(text) // 2) - (part // 2))
    middle = text[mid_start:mid_start + part]
    tail = text[-part:]
    return f"{head}\n\n[...middle excerpt...]\n\n{middle}\n\n[...final excerpt...]\n\n{tail}"


def _sanitize_for_external_model(text: str) -> tuple[str, str | None]:
    matches = sum(1 for pat in _SECRET_PATTERNS if pat.search(text))
    if matches:
        for pat, repl in _REDACTIONS:
            text = pat.sub(repl, text)
        if any(pat.search(text) for pat in _SECRET_PATTERNS):
            return "", "contains-secret-like-material"
    for pat, repl in _REDACTIONS:
        text = pat.sub(repl, text)
    return text, None


def _extract_json(raw: str) -> dict[str, Any]:
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            raise
        obj = json.loads(raw[start:end + 1])
    if not isinstance(obj, dict):
        raise ValueError("card response was not a JSON object")
    return obj


def _string_list(value: Any, limit: int = 8) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value[:limit]:
        if isinstance(item, dict):
            s = "; ".join(f"{k}: {v}" for k, v in item.items() if isinstance(k, str))
        else:
            s = str(item)
        s = re.sub(r"\s+", " ", s).strip()
        if s:
            out.append(s[:700])
    return out


def _normalize_card(obj: dict[str, Any], doc: Document) -> dict[str, Any]:
    try:
        novelty = int(obj.get("novelty", 1))
    except Exception:
        novelty = 1
    novelty = min(5, max(1, novelty))
    domain = re.sub(r"\s+", " ", str(obj.get("domain") or "unknown")).strip()[:120] or "unknown"
    return {
        "source": doc.rel,
        "source_sha1": doc.sha1,
        "claims": _string_list(obj.get("claims")),
        "mechanisms": _string_list(obj.get("mechanisms")),
        "connections": _string_list(obj.get("connections")),
        "novelty": novelty,
        "domain": domain,
        "model": MODEL_NAME,
        "created_at": int(time.time()),
    }


def _build_prompt(doc: Document, text: str) -> str:
    return (
        "Distill exactly one structured insight card from the document excerpt below.\n"
        "Return valid JSON only with keys: claims, mechanisms, connections, novelty, domain.\n"
        "claims, mechanisms, and connections must be arrays of concise strings.\n"
        "novelty must be an integer from 1 to 5 where 1=common and 5=surprising/high-leverage.\n"
        "domain must be a short topical label. Do not include personal data or secrets.\n\n"
        f"SOURCE: {doc.rel}\n"
        f"SHA1: {doc.sha1}\n\n"
        "DOCUMENT EXCERPT:\n"
        f"{text}"
    )


def _call_model(doc: Document, text: str) -> dict[str, Any]:
    from lib.egon_chat import chat

    raw = chat(
        [{"role": "user", "content": _build_prompt(doc, text)}],
        provider=MODEL_PROVIDER,
        model=MODEL_NAME,
        inject_context=False,
        temperature=0.0,
        max_tokens=900,
    )
    return _normalize_card(_extract_json(raw), doc)


def _upsert_memory_direct(conn: sqlite3.Connection, kind: str, marker: str,
                          content: str, tags: list[str]) -> bool:
    """Direct idempotent memory write by first-line marker.

    Mirrors lib.mind_exhaustive's bulk pattern: no per-item HTTP, no external_id
    dependency, and existing FTS triggers keep search in sync.
    """
    try:
        now = int(time.time())
        row = conn.execute(
            "SELECT id FROM memory WHERE kind=? AND content LIKE ? LIMIT 1",
            (kind, marker + "%"),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE memory SET content=?, tags=?, updated_at=? WHERE id=?",
                (content, ",".join(tags), now, row["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO memory (kind, content, tags, created_at, updated_at) VALUES (?,?,?,?,?)",
                (kind, content, ",".join(tags), now, now),
            )
        return True
    except Exception:
        return False


def _store_card(conn: sqlite3.Connection, doc: Document, card: dict[str, Any]) -> bool:
    marker = f"Insight card {doc.doc_id}"
    content = marker + "\n" + json.dumps(card, ensure_ascii=True, sort_keys=True)
    tags = ["egon", "insight_card", "distillation", card.get("domain", "unknown")]
    return _upsert_memory_direct(conn, "insight_card", marker, content, tags)


def pending_count(state: dict[str, Any] | None = None) -> int:
    state = _normalize_state(state or _read_state())
    records = state.get("documents") if isinstance(state.get("documents"), dict) else {}
    pending = 0
    for doc in _iter_documents():
        rec = records.get(doc.doc_id) if isinstance(records, dict) else None
        if not rec or rec.get("sha1") != doc.sha1 or rec.get("status") != "processed":
            pending += 1
    return pending


def run(limit: int | None = None, stop_check=None) -> dict[str, Any]:
    state = _normalize_state(_read_state())
    daily = state["daily"]
    cap = _daily_limit() if limit is None else max(0, int(limit))
    remaining = max(0, min(cap, _daily_limit() - int(daily.get("cards", 0) or 0)))
    records = state.setdefault("documents", {})
    stats = {
        "status": "ok",
        "processed": 0,
        "skipped": 0,
        "errors": 0,
        "remaining_today": remaining,
        "state_file": str(STATE_FILE),
    }
    if remaining <= 0:
        stats["status"] = "budget_exhausted"
        return stats

    conn = _conn()
    try:
        for doc in _iter_documents():
            if stop_check and stop_check():
                stats["status"] = "aborted"
                break
            if stats["processed"] >= remaining:
                break
            rec = records.get(doc.doc_id)
            if rec and rec.get("sha1") == doc.sha1 and rec.get("status") == "processed":
                continue
            try:
                text = doc.path.read_text(encoding="utf-8", errors="replace")
                if len(text.strip()) < MIN_TEXT_CHARS:
                    records[doc.doc_id] = {
                        "source": doc.rel, "sha1": doc.sha1, "status": "skipped",
                        "reason": "too-short", "updated_at": int(time.time()),
                    }
                    daily["skipped"] = int(daily.get("skipped", 0) or 0) + 1
                    stats["skipped"] += 1
                    continue
                text, reason = _sanitize_for_external_model(_sample_text(text))
                if reason:
                    records[doc.doc_id] = {
                        "source": doc.rel, "sha1": doc.sha1, "status": "skipped",
                        "reason": reason, "updated_at": int(time.time()),
                    }
                    daily["skipped"] = int(daily.get("skipped", 0) or 0) + 1
                    stats["skipped"] += 1
                    continue
                card = _call_model(doc, text)
                if not _store_card(conn, doc, card):
                    raise RuntimeError("direct memory upsert failed")
                conn.commit()
                records[doc.doc_id] = {
                    "source": doc.rel,
                    "sha1": doc.sha1,
                    "status": "processed",
                    "memory_marker": f"Insight card {doc.doc_id}",
                    "domain": card.get("domain", "unknown"),
                    "updated_at": int(time.time()),
                }
                daily["cards"] = int(daily.get("cards", 0) or 0) + 1
                stats["processed"] += 1
                stats["remaining_today"] = max(0, _daily_limit() - int(daily.get("cards", 0) or 0))
            except Exception as exc:
                conn.rollback()
                records[doc.doc_id] = {
                    "source": doc.rel, "sha1": doc.sha1, "status": "error",
                    "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                    "updated_at": int(time.time()),
                }
                daily["errors"] = int(daily.get("errors", 0) or 0) + 1
                stats["errors"] += 1
                if stats["errors"] >= 5 and stats["processed"] == 0:
                    stats["status"] = "error"
                    break
            finally:
                _write_state(state)
    finally:
        conn.close()
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Distill extracted full-texts into durable insight cards.")
    ap.add_argument("--limit", type=int, default=None, help="Maximum cards to create this run.")
    ap.add_argument("--pending", action="store_true", help="Only print pending document count.")
    args = ap.parse_args(argv)
    if args.pending:
        print(json.dumps({"pending": pending_count()}, ensure_ascii=True))
        return 0
    result = run(limit=args.limit)
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0 if result.get("status") in {"ok", "budget_exhausted", "aborted"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
