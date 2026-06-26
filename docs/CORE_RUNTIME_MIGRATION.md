# Egon Core Runtime Migration

Goal: move Egon's always-on substrate away from loose Python entrypoints while
preserving the current mind/orchestrator behavior.

## Current Stabilization

The immediate hardening step is to stop launching core roles through
`.venv\Scripts\pythonw.exe`. On Windows that executable is a redirector stub: it
can leave a wrapper parent plus a real base-Python child, which makes process
counts, ownership, restart policy, and failure diagnosis unreliable.

All sanctioned launch paths should resolve the base interpreter from
`.venv\pyvenv.cfg`, inject `.venv\Lib\site-packages` through `PYTHONPATH`, and
then start the target script directly.

Core roles covered by this rule:

- `scripts/egon_core.py`
- `scripts/mind_service.py`
- `egon_app.main`
- `scripts/watchdog.py`
- `external/egon_mind_mcp/server.py` autostart

`scripts/egon_runtime_doctor.py` should report any venv redirector wrapper for
these roles as degraded.

## Target Architecture

Phase 1: compiled supervisor, Python workers.

- Build a small Windows service or tray-hosted executable for core ownership.
- Own exactly one process tree for mind, orchestrator/Hermes, health checks, and
  quota/cooldown routing.
- Keep Python only for isolated workers that still need Python libraries:
  semantic indexing, file hydration, ML embedding, OCR, adapters.
- Workers must be short-lived, idempotent, and launched with explicit env.

Phase 2: packaged workers.

- Package remaining Python workers with a fixed embedded runtime or PyInstaller
  bundle so the host no longer depends on ambient Python installs.
- Keep data outside the bundle under `state/` and configured Drive paths.
- Preserve rollback and restore-point behavior before replacing any live worker.

Phase 3: native replacements where they pay off.

- Move stable control-plane code to the compiled supervisor: process inventory,
  health probes, orchestrator scheduling, quota state, task controls, and wake
  routing.
- Leave ML-heavy or fast-changing integrations in workers until a native
  replacement is clearly lower risk.

## Non-Negotiables

- Never promote or mark a stateful artifact complete without post-write
  validation.
- Never run multiple logical owners for the same role.
- Never rely on a global Python installation for always-on core roles.
- Every restart or rollback must be visible in mind activity or durable memory.
