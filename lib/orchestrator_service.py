"""Always-on Egon Orchestrator service.

Runs beside the mind service so routing/cooldown recovery is live even when the
desktop Orchestrator page is closed.
"""
from __future__ import annotations

import threading
import time
import json
from datetime import datetime
from pathlib import Path

from lib.orchestration_engine import ROOT, get_agent_routing_status, get_scheduler_status

LOG_FILE = ROOT / "logs" / "orchestrator-service.log"
AUTONOMY_STATE_FILE = ROOT / "state" / "orchestrator_autonomy.json"
ORCHESTRATOR_INTERVAL_S = 15
_SERVICE: OrchestratorService | None = None
_SERVICE_LOCK = threading.Lock()
_DEFAULT_AUTONOMY = {
    "enabled": True,
    "mode": "supervise_only",
    "stuck_after_seconds": 1800,
    "auto_requeue_stuck": True,
    "wake_hermes": True,
    "wake_agents": True,
    "provider_hooks": True,
}


def _log(level: str, event: str, **data) -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().isoformat(timespec="seconds")
        tail = " ".join(f"{k}={v}" for k, v in data.items())
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"{stamp} [{level}] event={event} {tail}\n".rstrip() + "\n")
    except Exception:
        pass


def _read_autonomy_state() -> dict:
    try:
        if AUTONOMY_STATE_FILE.exists():
            body = json.loads(AUTONOMY_STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(body, dict):
                return {**_DEFAULT_AUTONOMY, **body}
    except Exception:
        pass
    return dict(_DEFAULT_AUTONOMY)


def update_autonomy_state(**updates) -> dict:
    state = _read_autonomy_state()
    for key in _DEFAULT_AUTONOMY:
        if key in updates:
            state[key] = updates[key]
    try:
        state["enabled"] = bool(state.get("enabled"))
        state["auto_requeue_stuck"] = bool(state.get("auto_requeue_stuck"))
        state["wake_hermes"] = bool(state.get("wake_hermes"))
        state["wake_agents"] = bool(state.get("wake_agents"))
        state["provider_hooks"] = bool(state.get("provider_hooks"))
        state["stuck_after_seconds"] = max(60, int(state.get("stuck_after_seconds") or 1800))
        if state.get("mode") not in {"supervise_only", "off"}:
            state["mode"] = "supervise_only"
        AUTONOMY_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        AUTONOMY_STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    except Exception as e:
        state["write_error"] = f"{type(e).__name__}: {str(e)[:160]}"
    return autonomy_status(state)


def autonomy_status(state: dict | None = None) -> dict:
    state = state or _read_autonomy_state()
    return {
        "status": "ok",
        "autonomy": state,
        "state_file": str(AUTONOMY_STATE_FILE),
        "notes": [
            "supervise_only never invents new project work",
            "stale assigned tasks are requeued or rerouted after stuck_after_seconds",
            "quota/cooldown recovery is driven by provider hooks and agent failure reports",
            "wake_agents starts local native runners where a provider exposes one",
        ],
    }


class OrchestratorService:
    """Small daemon loop for cooldown pruning, reroute refresh, and Hermes wakeups."""

    def __init__(self, interval_s: int = ORCHESTRATOR_INTERVAL_S):
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._interval = max(5, int(interval_s))
        self.last_result: dict | None = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="egon-orchestrator-service",
        )
        self._thread.start()
        _log("info", "started", interval_s=self._interval)

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._thread is not None:
            try:
                self._thread.join(timeout=timeout)
            except Exception:
                pass
        _log("info", "stopped")

    def _run_once(self) -> dict:
        autonomy = _read_autonomy_state()
        routing = get_agent_routing_status()
        scheduler = get_scheduler_status()
        maintenance = {"status": "skipped", "reason": "autonomy disabled"}
        if autonomy.get("enabled") and autonomy.get("auto_requeue_stuck"):
            try:
                from lib.orchestration_engine import rebalance_stuck_tasks

                maintenance = rebalance_stuck_tasks(
                    stuck_after_seconds=int(autonomy.get("stuck_after_seconds") or 1800)
                )
            except Exception as e:
                maintenance = {"status": "error", "error": f"{type(e).__name__}: {str(e)[:120]}"}
        try:
            if autonomy.get("enabled") and autonomy.get("wake_hermes"):
                from lib.hermes_runner import trigger_hermes_runner

                trigger_hermes_runner()
                hermes = "triggered"
                # Gemini is an in-process agent too (API-backed; Antigravity's
                # standalone LS is Google-deprecated). Drain its pending tasks
                # on the same tick. Bruno 2026-07-06.
                try:
                    from lib.gemini_runner import trigger_gemini_runner
                    trigger_gemini_runner()
                except Exception:
                    pass
            else:
                hermes = "skipped"
        except Exception as e:
            hermes = f"error:{type(e).__name__}:{str(e)[:120]}"
        try:
            if autonomy.get("enabled") and autonomy.get("provider_hooks"):
                from lib.provider_hooks import scan_provider_hooks

                provider_hooks = scan_provider_hooks()
            else:
                provider_hooks = {"status": "skipped"}
        except Exception as e:
            provider_hooks = {"status": "error", "error": f"{type(e).__name__}: {str(e)[:120]}"}
        try:
            if autonomy.get("enabled") and autonomy.get("wake_agents"):
                from lib.agent_wake_bridge import wake_pending_agents

                wake_agents = wake_pending_agents()
            else:
                wake_agents = {"status": "skipped"}
        except Exception as e:
            wake_agents = {"status": "error", "error": f"{type(e).__name__}: {str(e)[:120]}"}
        available = sum(1 for state in routing.values() if state.get("available"))
        cooldowns = sum(1 for state in routing.values() if state.get("cooldown"))
        return {
            "status": "ok",
            "available_agents": available,
            "cooldown_agents": cooldowns,
            "active_work": scheduler.get("active_work", 0),
            "idle_agents": scheduler.get("idle_agents", []),
            "stuck_tasks": len(scheduler.get("stuck_tasks", [])),
            "autonomy": autonomy,
            "maintenance": maintenance,
            "hermes": hermes,
            "provider_hooks": provider_hooks,
            "wake_agents": wake_agents,
            "ts": int(time.time()),
        }

    def _run_loop(self) -> None:
        self._stop.wait(2.0)
        while not self._stop.is_set():
            try:
                self.last_result = self._run_once()
            except Exception as e:
                self.last_result = {
                    "status": "error",
                    "error": f"{type(e).__name__}: {str(e)[:200]}",
                    "ts": int(time.time()),
                }
                _log("warn", "tick_failed", error=self.last_result["error"])
            self._stop.wait(self._interval)


def ensure_orchestrator_service(interval_s: int = ORCHESTRATOR_INTERVAL_S) -> OrchestratorService:
    global _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is None or not _SERVICE.is_running():
            _SERVICE = OrchestratorService(interval_s=interval_s)
            _SERVICE.start()
        return _SERVICE


def stop_orchestrator_service(timeout: float = 3.0) -> None:
    global _SERVICE
    with _SERVICE_LOCK:
        svc = _SERVICE
        _SERVICE = None
    if svc is not None:
        svc.stop(timeout=timeout)


def orchestrator_service_status() -> dict:
    svc = _SERVICE
    return {
        "status": "ok",
        "running": bool(svc and svc.is_running()),
        "last_result": svc.last_result if svc else None,
        "interval_s": svc._interval if svc else ORCHESTRATOR_INTERVAL_S,
    }
