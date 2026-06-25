"""Configuration and runtime enforcement checks for Egon's unified mind."""
from __future__ import annotations

import json
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
HOME = Path.home()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def enforcement_status(project: str | None = "egon",
                       since_hours: int = 168) -> dict[str, Any]:
    scorecard = None
    try:
        from lib.mind_scorecard import build_mind_scorecard

        scorecard = build_mind_scorecard(project=project, since_hours=since_hours)
    except Exception:
        scorecard = None
    checks = [
        _service_check(),
        _shared_workspace_check(),
        _claude_hooks_check(),
        _codex_mcp_check(),
        _codex_directive_check(),
        _gemini_mcp_check(),
        _gemini_directive_check(),
        _project_directive_check(),
        _egon_claude_check(),
        _claude_session_state_check(),
        _agent_state_guard_check(),
        _mcp_server_v2_check(),
        _mcp_live_smoke_check(project=project),
        _token_waste_sentinel_check(project=project, since_hours=since_hours, card=scorecard),
        _runtime_scorecard_check(project=project, since_hours=since_hours, card=scorecard),
    ]
    gaps = []
    for check in checks:
        if check["status"] != "pass":
            gaps.extend(check.get("gaps") or [])
    summary = _summarize(checks)
    config_score = _score_checks(checks[:-1])
    runtime_score = checks[-1].get("score")
    overall = round((config_score * 0.45) + ((runtime_score or 0) * 0.55)
                    + (summary["pass"] / max(1, summary["total"])) * 5)
    return {
        "status": "ok",
        "version": "mind-enforcement-v1",
        "project": project,
        "score": max(0, min(100, overall)),
        "config_score": config_score,
        "runtime_score": runtime_score,
        "summary": summary,
        "checks": checks,
        "gaps": gaps[:20],
        "next_actions": _next_actions(checks, gaps),
    }


def _service_check() -> dict[str, Any]:
    ok = False
    try:
        with socket.create_connection(("127.0.0.1", 8000), timeout=0.5):
            ok = True
    except Exception:
        ok = False
    return _check(
        "mind_service",
        ok,
        "Mind service is listening on 127.0.0.1:8000.",
        "Mind service is not listening on 127.0.0.1:8000.",
        "Run Start Egon Mind Service.bat or scripts/start_mind_service.ps1.",
    )


def _shared_workspace_check() -> dict[str, Any]:
    try:
        from lib.shared_workspace import shared_status

        status = shared_status()
    except Exception as e:
        return {
            "name": "shared_workspace",
            "status": "fail",
            "message": f"Shared workspace check failed: {type(e).__name__}: {str(e)[:160]}",
            "fix": "Inspect lib/shared_workspace.py and run scripts/bootstrap_shared_workspace.py.",
            "gaps": ["Shared workspace could not be checked."],
        }

    dirs = status.get("directories") or {}
    required = [
        "root",
        "projects",
        "memories",
        "skills",
        "sessions",
        "artifacts",
        "state",
        "pointers",
    ]
    missing = [
        name for name in required
        if not (dirs.get(name) or {}).get("exists")
    ]
    double = next(
        (p for p in status.get("pointers", []) if p.get("name") == "double"),
        {},
    )
    double_ready = (
        double.get("target_exists")
        and (double.get("source_is_pointer") or not double.get("source_exists"))
    )
    if not double_ready:
        missing.append("double project pointer")

    ok = not missing
    return _check(
        "shared_workspace",
        ok,
        (
            "Shared AI workspace is initialized at "
            f"{status.get('root')} with Double resolved through the canonical root."
        ),
        f"Shared AI workspace is incomplete: {', '.join(missing)}.",
        (
            "Run scripts/bootstrap_shared_workspace.py --apply "
            "--adopt-projects --project double, then adopt agent state deliberately."
        ),
        None,
        missing,
    )


def _claude_hooks_check() -> dict[str, Any]:
    path = HOME / ".claude" / "settings.local.json"
    text = _read(path)
    lower = text.lower()
    required = {
        "UserPromptSubmit": " prompt",
        "PreToolUse": " pretool",
        "PostToolUse": " tool",
        "Stop": " stop",
    }
    missing = [k for k, needle in required.items()
               if k not in text or "mind_hook.py" not in lower or needle not in lower]
    return _check(
        "claude_hooks",
        path.exists() and not missing,
        "Claude hooks cover prompt context, pre-tool leases, post-tool activity, and stop summaries.",
        f"Claude hook gaps: {', '.join(missing)}",
        f"Patch {path} so all hook events call scripts/mind_hook.py.",
        path,
        missing,
    )


def _codex_mcp_check() -> dict[str, Any]:
    path = HOME / ".codex" / "config.toml"
    text = _read(path)
    command = _extract_toml_value(text, "command")
    args = _extract_toml_value(text, "args")
    ok = (
        "[mcp_servers.egon_mind]" in text
        and "egon_mind_mcp/server.py" in text
        and command is not None
        and "python.exe" in command.replace("\\", "/").lower()
        and "pythonw.exe" not in command.replace("\\", "/").lower()
    )
    missing = []
    if "[mcp_servers.egon_mind]" not in text:
        missing.append("egon_mind block")
    if "egon_mind_mcp/server.py" not in text:
        missing.append("server.py arg")
    if command and "pythonw.exe" in command.replace("\\", "/").lower():
        missing.append("stdio command uses pythonw.exe")
    if not command:
        missing.append("command")
    if args and "egon_mind_mcp/server.py" not in args:
        missing.append("args")
    return _check(
        "codex_mcp",
        ok,
        "Codex has egon_mind MCP registered with python.exe stdio.",
        "Codex egon_mind MCP registration is missing, incomplete, or using pythonw.exe for stdio.",
        f"Patch {path} with [mcp_servers.egon_mind] using .venv/Scripts/python.exe.",
        path,
        missing,
    )


def _codex_directive_check() -> dict[str, Any]:
    path = HOME / ".codex" / "AGENTS.md"
    return _directive_check("codex_directive", path)


def _gemini_mcp_check() -> dict[str, Any]:
    paths = [
        HOME / ".gemini" / "antigravity-ide" / "mcp_config.json",
        HOME / ".gemini" / "config" / "mcp_config.json",
    ]
    missing = []
    for path in paths:
        text = _read(path)
        lower = text.replace("\\", "/").lower()
        if not (path.exists()
                and "egon-mind" in text
                and "egon_mind_mcp" in text
                and "python.exe" in lower
                and "pythonw.exe" not in lower):
            missing.append(str(path))
    ok = not missing
    return _check(
        "antigravity_mcp",
        ok,
        "Antigravity/Gemini MCP configs include egon-mind with python.exe stdio.",
        "One or more Antigravity/Gemini MCP configs do not expose egon-mind with python.exe stdio.",
        "Patch the listed mcp_config.json files with egon-mind server.py and python.exe.",
        None,
        missing,
    )


def _gemini_directive_check() -> dict[str, Any]:
    path = HOME / ".gemini" / "GEMINI.md"
    return _directive_check("gemini_directive", path)


def _project_directive_check() -> dict[str, Any]:
    path = HOME / "Documents" / "New project" / "AGENTS.md"
    return _directive_check("workspace_directive", path)


def _egon_claude_check() -> dict[str, Any]:
    path = ROOT / "CLAUDE.md"
    return _directive_check("egon_claude_directive", path)


def _mcp_server_v2_check() -> dict[str, Any]:
    path = ROOT / "external" / "egon_mind_mcp" / "server.py"
    text = _read(path)
    ok = "mind_context_v2" in text and "\"/context/v2\"" in text
    return _check(
        "mcp_server_v2",
        ok,
        "MCP server exposes mind_context_v2 and routes mind_context through Context Broker v2.",
        "MCP server does not expose the Context Broker v2 path.",
        f"Patch {path} so mind_context prefers /context/v2 and mind_context_v2 is listed.",
        path,
    )


def _claude_session_state_check() -> dict[str, Any]:
    try:
        from lib.agent_state_guard import (
            claude_session_state_health,
            repair_claude_archived_only_transcripts,
        )

        repair = repair_claude_archived_only_transcripts()
        health = claude_session_state_health()
    except Exception as e:
        return {
            "name": "claude_session_state",
            "status": "fail",
            "message": f"Claude session-state check failed: {type(e).__name__}: {str(e)[:160]}",
            "fix": "Inspect lib/agent_state_guard.py and Claude session metadata.",
            "gaps": ["Claude session state could not be checked."],
        }
    ok = health.get("status") == "ok"
    check = _check(
        "claude_session_state",
        ok,
        (
            "Claude live transcripts and desktop metadata are coherent "
            f"({health.get('live_jsonl_count')} live JSONL files)."
        ),
        (
            "Claude session state is unsafe: "
            f"{health.get('archived_only_count')} archived-only transcripts, "
            f"{health.get('transcript_unavailable_count')} unavailable metadata files."
        ),
        "Restore missing live *.jsonl files and clear only verified transcriptUnavailable flags.",
        None,
        (health.get("archived_only_examples") or []) + (health.get("transcript_unavailable_examples") or []),
    )
    if repair.get("restored"):
        check["repair"] = repair
        check["message"] += f" Auto-restored {repair.get('restored')} archived-only live transcript(s)."
    return check


def _agent_state_guard_check() -> dict[str, Any]:
    compactor = ROOT / "scripts" / "compact_transcripts.py"
    restore_lib = ROOT / "lib" / "restore_points.py"
    compactor_text = _read(compactor)
    restore_text = _read(restore_lib)
    forbidden = [
        token for token in ("os.rename", ".rename(", ".unlink(", "os.remove", "shutil.move")
        if token in compactor_text
    ]
    ok = (
        "create_agent_restore_point" in compactor_text
        and "live transcript disappeared" in compactor_text
        and "_snapshot_dest" in restore_text
        and not forbidden
    )
    missing = []
    if "create_agent_restore_point" not in compactor_text:
        missing.append("compactor restore-point preflight")
    if "live transcript disappeared" not in compactor_text:
        missing.append("post-compaction live transcript invariant")
    if "_snapshot_dest" not in restore_text:
        missing.append("collision-proof restore point filenames")
    missing.extend(f"forbidden mutation token: {token}" for token in forbidden)
    return _check(
        "agent_state_guard",
        ok,
        "Agent-state mutation paths require restore points and preserve live Claude transcripts.",
        "Agent-state mutation guard is missing or contains unsafe transcript operations.",
        "Patch compact_transcripts.py and restore_points.py before running transcript maintenance.",
        compactor,
        missing,
    )


def _mcp_live_smoke_check(project: str | None) -> dict[str, Any]:
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
                    "project": project or "egon",
                    "query": "enforcement live MCP smoke check",
                    "budget_chars": 1800,
                    "limit_activity": 2,
                    "limit_memory": 3,
                    "include_graph": False,
                    "include_audit": False,
                },
            },
        }),
        json.dumps({"jsonrpc": "2.0", "id": 3, "method": "exit", "params": {}}),
        "",
    ])
    kwargs: dict[str, Any] = {
        "cwd": str(ROOT),
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.Popen([str(py), str(server)], **kwargs)
        stdout, stderr = proc.communicate(input=payload, timeout=45)
    except Exception as e:
        return {
            "name": "mcp_live_smoke",
            "status": "fail",
            "message": f"MCP live smoke failed: {type(e).__name__}: {str(e)[:160]}",
            "fix": "Run the MCP server with python.exe and verify mind_context_v2 responds.",
            "gaps": ["MCP live smoke check failed."],
        }
    detail = (stderr or stdout or "")[:400]
    for line in (stdout or "").splitlines():
        try:
            msg = json.loads(line)
        except Exception:
            continue
        if msg.get("id") != 2:
            continue
        content = (((msg.get("result") or {}).get("content") or [{}])[0] or {}).get("text")
        try:
            body = json.loads(content or "")
        except Exception:
            body = {}
        ok = body.get("status") == "ok" and body.get("version") == "context-broker-v2"
        return _check(
            "mcp_live_smoke",
            ok,
            "MCP server answered live mind_context_v2 smoke probe.",
            "MCP server did not return Context Broker v2 in live smoke probe.",
            "Run the MCP server with python.exe and verify /context/v2 is reachable.",
            server,
            [] if ok else [str(body)[:240] or detail],
        )
    return {
        "name": "mcp_live_smoke",
        "status": "fail",
        "path": str(server),
        "message": "MCP server did not return a tools/call response.",
        "fix": "Run the MCP server with python.exe and inspect stderr/stdout.",
        "missing": [detail],
        "gaps": ["MCP live smoke check failed."],
    }


def _token_waste_sentinel_check(project: str | None, since_hours: int,
                                card: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        if card is None:
            from lib.mind_scorecard import build_mind_scorecard

            card = build_mind_scorecard(project=project, since_hours=since_hours)
    except Exception as e:
        return {
            "name": "token_waste_sentinel",
            "status": "fail",
            "message": f"Token waste sentinel unavailable: {type(e).__name__}: {str(e)[:160]}",
            "fix": "Fix scorecard computation so token waste can be audited.",
            "gaps": ["Token waste sentinel could not run."],
        }
    metrics = card.get("metrics") or {}
    tokens = card.get("tokens") or {}
    gaps = []
    turn_count = int(tokens.get("turn_count") or 0)
    if turn_count > 0 and metrics.get("context_coverage", 0) < 90:
        gaps.append("Tracked token turns exist while context coverage is below 90%.")
    if turn_count > 0 and metrics.get("v2_context_adoption", 0) < 90:
        gaps.append("Tracked token turns exist while Context Broker v2 adoption is below 90%.")
    if turn_count > 0 and metrics.get("estimated_token_roi", 0) < 20:
        gaps.append("Estimated token ROI is below 20% for tracked work.")
    ok = not gaps
    status = "pass" if ok else "fail"
    return {
        "name": "token_waste_sentinel",
        "status": status,
        "message": (
            "Token waste sentinel is clear: "
            f"{turn_count} tracked turns, context={metrics.get('context_coverage')}%, "
            f"v2={metrics.get('v2_context_adoption')}%, roi={metrics.get('estimated_token_roi')}%."
        ) if ok else (
            "Token waste sentinel tripped: "
            f"{turn_count} tracked turns, context={metrics.get('context_coverage')}%, "
            f"v2={metrics.get('v2_context_adoption')}%, roi={metrics.get('estimated_token_roi')}%."
        ),
        "fix": None if ok else "Stop long agent work, restore context/MCP coverage, and re-run activation before spending more tokens.",
        "gaps": gaps,
        "metrics": metrics,
        "tokens": tokens,
    }


def _runtime_scorecard_check(project: str | None, since_hours: int,
                             card: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        if card is None:
            from lib.mind_scorecard import build_mind_scorecard

            card = build_mind_scorecard(project=project, since_hours=since_hours)
    except Exception as e:
        return {
            "name": "runtime_scorecard",
            "status": "fail",
            "score": 0,
            "message": f"Runtime scorecard unavailable: {type(e).__name__}: {str(e)[:160]}",
            "gaps": ["Runtime scorecard could not be computed."],
        }
    metrics = card.get("metrics") or {}
    gaps = []
    if metrics.get("context_coverage", 0) < 90:
        gaps.append("Session-start mind_context coverage below 90%.")
    if metrics.get("v2_context_adoption", 0) < 90:
        gaps.append("Context Broker v2 adoption below 90%.")
    if metrics.get("file_lease_coverage", 100) < 95:
        gaps.append("File lease coverage below 95%.")
    if (card.get("tokens") or {}).get("turn_count", 0) == 0:
        gaps.append("Per-turn token ledger has no tracked turns.")
    status = "pass" if card.get("score", 0) >= 75 and not gaps else "warn"
    return {
        "name": "runtime_scorecard",
        "status": status,
        "score": card.get("score"),
        "grade": card.get("grade"),
        "message": (
            f"Runtime scorecard {card.get('score')}/100 ({card.get('grade')}); "
            f"context={metrics.get('context_coverage')}%, "
            f"v2={metrics.get('v2_context_adoption')}%, "
            f"token_roi={metrics.get('estimated_token_roi')}%."
        ),
        "metrics": metrics,
        "gaps": gaps,
    }


def _directive_check(name: str, path: Path) -> dict[str, Any]:
    text = _read(path)
    lower = text.lower()
    required = {
        "mind_context_v2": "mind_context_v2" in lower,
        "Context Broker v2": "context broker v2" in lower,
        "file lease": "file lease" in lower or "file leases" in lower or "leases before edits" in lower,
        "durable memory": "durable memory" in lower or "durable outcomes must become memory" in lower,
    }
    missing = [item for item, ok in required.items() if not ok]
    return _check(
        name,
        path.exists() and not missing,
        f"{path} explicitly requires Context Broker v2, leases, and durable memory.",
        f"{path} is missing explicit directive items: {', '.join(missing)}.",
        f"Patch {path} to prefer mind_context_v2 and document leases/memory.",
        path,
        missing,
    )


def _check(name: str, ok: bool, pass_msg: str, fail_msg: str,
           fix: str, path: Path | None = None,
           missing: list[str] | None = None) -> dict[str, Any]:
    status = "pass" if ok else "fail"
    return {
        "name": name,
        "status": status,
        "path": str(path) if path else None,
        "message": pass_msg if ok else fail_msg,
        "fix": None if ok else fix,
        "missing": missing or [],
        "gaps": [] if ok else [fail_msg],
    }


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _extract_toml_value(text: str, key: str) -> str | None:
    in_block = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            in_block = line == "[mcp_servers.egon_mind]"
            continue
        if not in_block or not line.startswith(f"{key} "):
            continue
        _, value = line.split("=", 1)
        return value.strip().strip("'\"")
    return None


def _score_checks(checks: list[dict[str, Any]]) -> int:
    if not checks:
        return 0
    weights = {"pass": 1.0, "warn": 0.5, "fail": 0.0}
    return round(sum(weights.get(c.get("status"), 0.0) for c in checks)
                 / len(checks) * 100)


def _summarize(checks: list[dict[str, Any]]) -> dict[str, int]:
    out = {"total": len(checks), "pass": 0, "warn": 0, "fail": 0}
    for check in checks:
        status = check.get("status")
        if status in out:
            out[status] += 1
    return out


def _next_actions(checks: list[dict[str, Any]],
                  gaps: list[str]) -> list[dict[str, str]]:
    actions = []
    for check in checks:
        if check.get("status") != "pass" and check.get("fix"):
            actions.append({
                "priority": "high" if check["name"] in {
                    "claude_hooks", "codex_mcp", "antigravity_mcp",
                    "mcp_server_v2", "mcp_live_smoke", "claude_session_state",
                    "agent_state_guard", "token_waste_sentinel", "runtime_scorecard",
                } else "medium",
                "title": check["name"],
                "action": check["fix"],
            })
    if any("token ledger" in g.lower() for g in gaps):
        actions.append({
            "priority": "medium",
            "title": "token_ledger",
            "action": "Wire usage capture for agents that expose per-turn token counts.",
        })
    return actions[:8]


if __name__ == "__main__":
    print(json.dumps(enforcement_status(), indent=2, ensure_ascii=False))
