"""End-to-end activation tests for Egon's unified-mind harness."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
API_BASE = "http://127.0.0.1:8000/api/v1/mind"
DB_PATH = ROOT / "state" / "mind.db"


def run_activation_test(project: str | None = "egon",
                        query: str | None = "activation test",
                        run_mcp: bool = True) -> dict[str, Any]:
    stamp = int(time.time())
    external_id = f"activation-{stamp}-{os.getpid()}"
    project_slug = (project or "egon").strip() or "egon"
    results: list[dict[str, Any]] = []
    session_id: int | None = None

    def step(name: str, ok: bool, detail: str = "",
             data: dict[str, Any] | None = None) -> None:
        results.append({
            "name": name,
            "status": "pass" if ok else "fail",
            "detail": detail,
            "data": data or {},
        })

    stats = _http("GET", "/stats")
    step("mind_service", stats.get("status") == "ok",
         "Mind stats endpoint responded." if stats.get("status") == "ok" else str(stats)[:300],
         _pick(stats, "agents", "projects", "sessions", "activity", "memory"))

    start = _http("POST", "/sessions/start", {
        "agent": "egon-activation-runner",
        "agent_kind": "diagnostic",
        "external_id": external_id,
        "project": project_slug,
        "started_at": stamp,
    })
    session_id = start.get("id") if start.get("status") == "ok" else None
    step("synthetic_session_start", session_id is not None,
         f"session_id={session_id}" if session_id else str(start)[:300],
         {"session_id": session_id, "external_id": external_id})

    context = _http("GET", "/context/v2", params={
        "project": project_slug,
        "query": query or "activation test",
        "budget_chars": 2600,
        "limit_activity": 4,
        "limit_memory": 6,
    })
    ctx_ok = context.get("status") == "ok" and context.get("version") == "context-broker-v2"
    step("context_broker_v2", ctx_ok,
         f"tokens={(context.get('budget') or {}).get('approx_tokens')}" if ctx_ok else str(context)[:300],
         {"version": context.get("version"),
          "approx_tokens": (context.get("budget") or {}).get("approx_tokens")})
    if session_id and ctx_ok:
        _http("POST", "/activity", {
            "session_id": session_id,
            "kind": "mind_context",
            "payload": {
                "broker_version": context.get("version"),
                "approx_tokens": (context.get("budget") or {}).get("approx_tokens"),
                "activation_test": True,
            },
            "ts": stamp,
        })

    lease_path = str((ROOT / "state" / "activation-test.lock").resolve()).replace("\\", "/")
    lease = _http("POST", "/files/lease", {
        "path": lease_path,
        "session_id": session_id,
        "duration_seconds": 60,
    }) if session_id else {"status": "error", "error": "no session"}
    lease_ok = lease.get("status") == "ok"
    step("file_lease_acquire", lease_ok,
         "Lease acquired and will be released." if lease_ok else str(lease)[:300],
         {"path": lease_path})
    if session_id and lease_ok:
        _http("POST", "/activity", {
            "session_id": session_id,
            "kind": "file_lease",
            "payload": {"path": lease_path, "activation_test": True},
            "ts": stamp,
        })
        _http("POST", "/activity", {
            "session_id": session_id,
            "kind": "tool_Edit",
            "payload": {
                "tool": "Edit",
                "input_preview": json.dumps({"path": lease_path}),
                "response_preview": "{}",
                "activation_test": True,
            },
            "ts": stamp,
        })

    release = _http("POST", "/files/release", {
        "path": lease_path,
        "session_id": session_id,
    }) if session_id else {"status": "error", "error": "no session"}
    release_ok = release.get("status") == "ok"
    step("file_lease_release", release_ok,
         "Lease released." if release_ok else str(release)[:300])
    if session_id and release_ok:
        _http("POST", "/activity", {
            "session_id": session_id,
            "kind": "file_release",
            "payload": {"path": lease_path, "activation_test": True},
            "ts": stamp,
        })

    memory = _http("POST", "/memory", {
        "kind": "activation_test",
        "content": (
            f"Activation test {external_id}: verified service, Context Broker v2, "
            "file lease/release, memory write, token ledger, and clean session close."
        ),
        "tags": [project_slug, "activation-test", "unified-mind", "ephemeral"],
        "attribution_session_id": session_id,
    }) if session_id else {"status": "error", "error": "no session"}
    step("memory_write", memory.get("status") == "ok",
         f"memory_id={memory.get('id')}" if memory.get("status") == "ok" else str(memory)[:300],
         {"memory_id": memory.get("id")})

    ledger = _http("POST", "/ledger/turns", {
        "session_id": session_id,
        "ts": stamp,
        "model": "activation-test",
        "usage": {
            "input_tokens": 120,
            "output_tokens": 30,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
        "tools": ["activation_test"],
    }) if session_id else {"status": "error", "error": "no session"}
    step("token_ledger_write", ledger.get("status") == "ok",
         "Synthetic token ledger row accepted." if ledger.get("status") == "ok" else str(ledger)[:300])

    if run_mcp:
        mcp = _mcp_context_probe(project_slug, query or "activation test")
        step("mcp_context_v2", mcp.get("status") == "ok",
             mcp.get("detail", ""),
             {"tool": mcp.get("tool"), "version": mcp.get("version")})

    close = _http("POST", "/sessions/end", {
        "session_id": session_id,
        "summary": "Activation test completed and closed cleanly.",
        "ended_at": int(time.time()),
    }) if session_id else {"status": "error", "error": "no session"}
    step("synthetic_session_close", close.get("status") == "ok",
         "Session closed." if close.get("status") == "ok" else str(close)[:300])

    scorecard = _http("GET", "/scorecard", params={
        "project": project_slug,
        "since_hours": 168 * 7,
        "capsule_budget_chars": 3500,
    })
    step("scorecard_refresh", scorecard.get("status") == "ok",
         f"score={scorecard.get('score')} grade={scorecard.get('grade')}"
         if scorecard.get("status") == "ok" else str(scorecard)[:300],
         {"score": scorecard.get("score"), "grade": scorecard.get("grade"),
          "metrics": scorecard.get("metrics") or {}})

    enforcement = _http("GET", "/enforcement/status", params={
        "project": project_slug,
        "since_hours": 168,
    })
    step("enforcement_refresh", enforcement.get("status") == "ok",
         f"score={enforcement.get('score')}"
         if enforcement.get("status") == "ok" else str(enforcement)[:300],
         {"score": enforcement.get("score"),
          "config_score": enforcement.get("config_score"),
          "runtime_score": enforcement.get("runtime_score")})

    passed = sum(1 for r in results if r["status"] == "pass")
    failed = len(results) - passed
    out = {
        "status": "ok" if failed == 0 else "warn",
        "version": "mind-activation-v1",
        "project": project_slug,
        "external_id": external_id,
        "session_id": session_id,
        "passed": passed,
        "failed": failed,
        "score": round((passed / max(1, len(results))) * 100),
        "results": results,
        "next_actions": _next_actions(results, scorecard),
    }
    if session_id:
        _http("POST", "/activity", {
            "session_id": session_id,
            "kind": "activation_result",
            "payload": {
                "external_id": external_id,
                "activation_score": out["score"],
                "passed": passed,
                "failed": failed,
                "scorecard_score": scorecard.get("score"),
                "scorecard_grade": scorecard.get("grade"),
                "scorecard_metrics": scorecard.get("metrics") or {},
                "enforcement_score": enforcement.get("score"),
                "enforcement_config_score": enforcement.get("config_score"),
                "enforcement_runtime_score": enforcement.get("runtime_score"),
                "failed_steps": [r["name"] for r in results if r["status"] != "pass"],
            },
            "ts": int(time.time()),
        })
    return out


def activation_history(project: str | None = "egon",
                       limit: int = 20) -> dict[str, Any]:
    project_slug = (project or "egon").strip() or "egon"
    limit = max(1, min(int(limit), 100))
    if not DB_PATH.exists():
        return {"status": "error", "error": "mind.db missing"}
    rows: list[dict[str, Any]] = []
    with _connect() as conn:
        sql = """SELECT a.id, a.ts, a.payload_json, s.external_id,
                        p.slug AS project_slug
                 FROM activity a
                 JOIN sessions s ON s.id = a.session_id
                 LEFT JOIN projects p ON p.id = s.project_id
                 WHERE a.kind = 'activation_result'"""
        params: list[Any] = []
        if project_slug:
            sql += " AND p.slug = ?"
            params.append(project_slug)
        sql += " ORDER BY a.ts DESC LIMIT ?"
        params.append(limit)
        for r in conn.execute(sql, params).fetchall():
            payload = _loads(r["payload_json"])
            rows.append({
                "activity_id": r["id"],
                "ts": r["ts"],
                "external_id": payload.get("external_id") or r["external_id"],
                "project": r["project_slug"],
                "activation_score": payload.get("activation_score"),
                "passed": payload.get("passed"),
                "failed": payload.get("failed"),
                "scorecard_score": payload.get("scorecard_score"),
                "scorecard_grade": payload.get("scorecard_grade"),
                "enforcement_score": payload.get("enforcement_score"),
                "metrics": payload.get("scorecard_metrics") or {},
                "failed_steps": payload.get("failed_steps") or [],
            })
    return {
        "status": "ok",
        "version": "mind-activation-history-v1",
        "project": project_slug,
        "count": len(rows),
        "latest": rows[0] if rows else None,
        "delta": _delta(rows),
        "runs": rows,
    }


def _http(method: str, path: str, body: dict | None = None,
          params: dict | None = None, timeout: float = 8.0) -> dict[str, Any]:
    url = API_BASE + path
    if params:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        if query:
            url += "?" + query
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        try:
            return json.loads(raw)
        except Exception:
            return {"status": "error", "error": raw[:500]}
    except urllib.error.URLError as e:
        return {"status": "error", "error": f"offline: {e}"}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}


def _mcp_context_probe(project: str, query: str) -> dict[str, Any]:
    server = ROOT / "external" / "egon_mind_mcp" / "server.py"
    py = ROOT / ".venv" / "Scripts" / "python.exe"
    if not py.exists():
        py = Path(sys.executable)
    payload = "\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}),
        json.dumps({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "mind_context_v2",
                "arguments": {
                    "project": project,
                    "query": query,
                    "budget_chars": 2200,
                    "limit_activity": 2,
                    "limit_memory": 4,
                },
            },
        }),
        json.dumps({"jsonrpc": "2.0", "id": 3, "method": "exit", "params": {}}),
        "",
    ])
    kwargs: dict[str, Any] = {
        "cwd": str(ROOT),
        "input": payload,
        "text": True,
        "capture_output": True,
        "timeout": 20,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run([str(py), str(server)], **kwargs)
    except Exception as e:
        return {"status": "error", "detail": f"{type(e).__name__}: {str(e)[:160]}"}
    for line in (proc.stdout or "").splitlines():
        try:
            msg = json.loads(line)
        except Exception:
            continue
        if msg.get("id") != 2:
            continue
        content = (((msg.get("result") or {}).get("content") or [{}])[0] or {}).get("text")
        if not content:
            return {"status": "error", "detail": "MCP response did not include text content."}
        try:
            body = json.loads(content)
        except Exception:
            return {"status": "error", "detail": content[:200]}
        ok = body.get("status") == "ok" and body.get("version") == "context-broker-v2"
        return {
            "status": "ok" if ok else "error",
            "tool": "mind_context_v2",
            "version": body.get("version"),
            "detail": "Direct MCP mind_context_v2 returned Context Broker v2."
            if ok else str(body)[:240],
        }
    return {"status": "error", "detail": (proc.stderr or proc.stdout or "no MCP response")[:240]}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH.as_posix(), timeout=8)
    conn.row_factory = sqlite3.Row
    return conn


def _loads(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        body = json.loads(raw)
        return body if isinstance(body, dict) else {"value": body}
    except Exception:
        return {}


def _delta(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if len(rows) < 2:
        return {}
    latest = rows[0]
    previous = rows[1]
    out: dict[str, Any] = {}
    for key in ("activation_score", "scorecard_score", "enforcement_score"):
        if latest.get(key) is not None and previous.get(key) is not None:
            out[key] = latest[key] - previous[key]
    latest_metrics = latest.get("metrics") or {}
    previous_metrics = previous.get("metrics") or {}
    metric_delta = {}
    for key in sorted(set(latest_metrics) & set(previous_metrics)):
        if isinstance(latest_metrics.get(key), (int, float)) and isinstance(previous_metrics.get(key), (int, float)):
            metric_delta[key] = round(latest_metrics[key] - previous_metrics[key], 2)
    if metric_delta:
        out["metrics"] = metric_delta
    return out


def _pick(d: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {k: d.get(k) for k in keys if k in d}


def _next_actions(results: list[dict[str, Any]],
                  scorecard: dict[str, Any]) -> list[dict[str, str]]:
    actions: list[dict[str, str]] = []
    failed = [r["name"] for r in results if r["status"] != "pass"]
    if "mcp_context_v2" in failed:
        actions.append({
            "priority": "high",
            "title": "Restart stale agent hosts",
            "action": "Restart Codex/Claude/Antigravity so their MCP tool list refreshes with mind_context_v2.",
        })
    if any(name.startswith("file_lease") for name in failed):
        actions.append({
            "priority": "high",
            "title": "Fix file lease endpoints",
            "action": "Check /files/lease and /files/release before allowing edit-heavy sessions.",
        })
    if "token_ledger_write" in failed:
        actions.append({
            "priority": "medium",
            "title": "Fix token ledger writes",
            "action": "Check /ledger/turns schema and hook transcript usage parsing.",
        })
    metrics = scorecard.get("metrics") or {}
    if metrics.get("v2_context_adoption", 0) < 90:
        actions.append({
            "priority": "medium",
            "title": "Let fresh sessions replace stale history",
            "action": "Open new Claude/Codex/Antigravity sessions after restart; old rows will age out of the weekly window.",
        })
    if not actions:
        actions.append({
            "priority": "low",
            "title": "Activation healthy",
            "action": "Use the scorecard trend next: compare activation results over time.",
        })
    return actions[:6]


if __name__ == "__main__":
    print(json.dumps(run_activation_test(), indent=2, ensure_ascii=False))
