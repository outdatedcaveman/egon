"""Audit Egon's live runtime without confusing venv wrappers for duplicates.

Checks:
- Mind/Panop API on 127.0.0.1:8000.
- Native GUI health server on 127.0.0.1:8088.
- Codex MCP registration for egon_mind.
- Process topology, collapsed into logical wrapper/child groups.

Exit code is 0 for operational, 1 for degraded.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CODEX_CONFIG = Path.home() / ".codex" / "config.toml"


def _http_json(url: str, timeout: float = 2.0) -> tuple[bool, object]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        return True, json.loads(raw)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _port_owner(port: int) -> int | None:
    if sys.platform != "win32":
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return 0
        except Exception:
            return None
    try:
        out = subprocess.check_output(
            ["netstat", "-ano", "-p", "tcp"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        needle = f":{port}"
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 5:
                continue
            local_addr, state, pid = parts[1], parts[3].upper(), parts[-1]
            if state == "LISTENING" and local_addr.endswith(needle):
                return int(pid)
        return None
    except Exception:
        return None


def _processes() -> list[dict]:
    if sys.platform != "win32":
        return []
    script = r"""
$procs = Get-CimInstance Win32_Process |
  Where-Object {
    ($_.Name -eq 'python.exe' -or $_.Name -eq 'pythonw.exe') -and
    ($_.CommandLine -like '*egon*' -or $_.CommandLine -like '*egon_mind_mcp*')
  } |
  Select-Object ProcessId,ParentProcessId,Name,ExecutablePath,CommandLine
$procs | ConvertTo-Json -Depth 3
"""
    try:
        raw = subprocess.check_output(
            ["powershell.exe", "-NoProfile", "-Command", script],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=8,
        ).strip()
        if not raw:
            return []
        data = json.loads(raw)
        if isinstance(data, dict):
            return [data]
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _role(command: str) -> str | None:
    cmd = command.lower()
    if "scripts\\mind_service.py" in cmd or "scripts/mind_service.py" in cmd:
        return "mind_service"
    if "egon_app.main" in cmd:
        return "gui"
    if "egon_mind_mcp" in cmd:
        return "mcp"
    if "scripts\\watchdog.py" in cmd or "scripts/watchdog.py" in cmd:
        return "watchdog"
    return None


def _logical_groups(procs: list[dict]) -> dict[str, dict]:
    by_pid = {int(p.get("ProcessId")): p for p in procs if p.get("ProcessId") is not None}
    groups: dict[str, dict] = {}
    for p in procs:
        command = str(p.get("CommandLine") or "")
        role = _role(command)
        if role is None:
            continue
        pid = int(p["ProcessId"])
        parent = int(p.get("ParentProcessId") or 0)
        exe = str(p.get("ExecutablePath") or "")
        is_wrapper = "\\.venv\\scripts\\" in exe.lower()
        is_child = parent in by_pid and _role(str(by_pid[parent].get("CommandLine") or "")) == role
        g = groups.setdefault(role, {"wrappers": [], "children": [], "other": [], "logical_count": 0})
        if is_wrapper:
            g["wrappers"].append(pid)
        elif is_child:
            g["children"].append(pid)
        else:
            g["other"].append(pid)
    for g in groups.values():
        # A venv launcher plus its base-python child is one logical process.
        wrapper_pair_count = len(g["children"]) if g["children"] else (1 if g["wrappers"] else 0)
        g["logical_count"] = wrapper_pair_count + len(g["other"])
    return groups


def main() -> int:
    mind_ok, mind_body = _http_json("http://127.0.0.1:8000/api/v1/mind/stats")
    status_ok, status_body = _http_json("http://127.0.0.1:8000/api/v1/status")
    gui_ok, gui_body = _http_json("http://127.0.0.1:8088/health")

    config_text = CODEX_CONFIG.read_text(encoding="utf-8", errors="replace") if CODEX_CONFIG.exists() else ""
    mcp_config_ok = (
        "[mcp_servers.egon_mind]" in config_text
        and "external/egon_mind_mcp/server.py" in config_text.replace("\\", "/")
    )

    procs = _processes()
    groups = _logical_groups(procs)
    port_8000_owner = _port_owner(8000)
    port_8088_owner = _port_owner(8088)

    issues: list[str] = []
    if not (mind_ok and isinstance(mind_body, dict) and mind_body.get("status") == "ok"):
        issues.append("mind_stats_not_ok")
    if not (status_ok and isinstance(status_body, dict)):
        issues.append("panop_status_not_ok")
    if not (gui_ok and isinstance(gui_body, dict) and gui_body.get("ok")):
        issues.append("gui_health_not_ok")
    if not mcp_config_ok:
        issues.append("codex_egon_mind_mcp_not_registered")
    for role in ("mind_service", "gui", "mcp"):
        count = groups.get(role, {}).get("logical_count", 0)
        if count > 1:
            issues.append(f"duplicate_logical_{role}:{count}")

    result = {
        "status": "ok" if not issues else "degraded",
        "issues": issues,
        "ports": {
            "8000_owner_pid": port_8000_owner,
            "8088_owner_pid": port_8088_owner,
        },
        "endpoints": {
            "mind_stats": mind_body if mind_ok else {"error": mind_body},
            "panop_status_ok": status_ok,
            "gui_health": gui_body if gui_ok else {"error": gui_body},
        },
        "mcp": {
            "codex_config_ok": mcp_config_ok,
            "logical_processes": groups.get("mcp", {}),
        },
        "process_groups": groups,
        "note": "Venv launcher parent plus base Python child counts as one logical process.",
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if not issues else 1


if __name__ == "__main__":
    raise SystemExit(main())
