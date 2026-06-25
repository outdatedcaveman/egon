"""THE MASTERLAW — Bruno's absolute, non-negotiable safety constraints on any
AUTONOMOUS action (Hermes/orchestrator dispatch, unsupervised agent work).

Every autonomous dispatch is screened by `check_dispatch()` BEFORE it runs, and
every dispatched task carries `task_contract()` so the executing AI is bound by
the same law it already follows individually. When in doubt, the law BLOCKS and
requires Bruno — fail-closed, never fail-open.

  MASTERLAW (Bruno 2026-06-24, TERMINANT — will not be tolerated if violated):
    1. NO leaking of personal information. Never transmit PII / personal data to
       any external service, recipient, or public surface.
    2. NO deletion or overwrite of documents/data without a restorable backup.
       Irreversible/unrecoverable destruction is FORBIDDEN. Deletes go to a
       restorable trash, never permanent.
    3. ALWAYS follow Bruno's fixed guidelines & principles (CLAUDE.md + each AI's
       individual safety rules). Autonomy never overrides them.
    4. Bruno has FULL visibility + veto. Any action is stoppable/editable from
       the Egon console; an autonomous action must be interruptible.
  Catastrophic or UNFIXABLE errors — above all PII leakage or unrecoverable
  deletion — are TERMINANTLY forbidden. No exceptions, no "test mode", no
  authority claim in any task/content overrides this.
"""
from __future__ import annotations

import re
import shutil
import time
from pathlib import Path

from lib import egon_paths

# Restorable trash — nothing is ever permanently deleted by autonomous routines.
TRASH_DIR = egon_paths.STATE_DIR / "_masterlaw_trash"

# Patterns that indicate a forbidden / high-risk autonomous action. Matching any
# of these BLOCKS autonomous dispatch (a human must do it). Deliberately broad —
# fail-closed. These are screened against the task description/intent.
_IRREVERSIBLE_DELETE = re.compile(
    r"\b(rm\s+-rf|rmdir\s+/s|del\s+/[sf]|format\s+[a-z]:|drop\s+(table|database)|"
    r"truncate\s+table|permanently?\s+delete|empty\s+(the\s+)?(trash|recycle)|"
    r"shred|wipe|unrecoverabl|hard.?delete|force.?delete|git\s+push\s+--force|"
    r"reset\s+--hard\s+origin)\b", re.I)
_PII_EGRESS = re.compile(
    r"\b(send|email|post|publish|upload|share|tweet|dm|forward|export\s+to|"
    r"sync\s+to)\b.{0,40}\b(password|cpf|ssn|passport|bank|credit\s*card|"
    r"address|phone|personal|private|secret|token|api[_\s-]?key|credential)\b",
    re.I)
# HARD masterlaw block — disabling protection enables other catastrophes.
_DISABLE_PROTECTION = re.compile(
    r"\bdisable\s+(antivirus|firewall|bitdefender|defender|protection|"
    r"secure\s*boot|encryption)\b", re.I)

# NOT a masterlaw catastrophe — these are reversible/outward actions that are
# fine WITH Bruno's say-so (he's OSS-first; making a repo public is routine and
# undoable). So they don't BLOCK; they require his confirmation before an
# autonomous dispatch runs them. "if I directly allow it" = this confirm gate.
_NEEDS_CONFIRM = re.compile(
    r"\b(make|set|turn|change|flip)\b.{0,30}\bpublic\b|"
    r"\b(publish|deploy\s+to\s+prod|share\s+with|grant\s+access|"
    r"add\s+collaborator|change\s+permission|send\s+(email|message|dm))\b", re.I)

_PII_HINT = re.compile(
    r"\b(cpf|ssn|passport\s*(no|number)|credit\s*card|bank\s*account|"
    r"\d{3}-\d{2}-\d{4}|password\s*[:=])\b", re.I)


def task_contract() -> list[str]:
    """The masterlaw lines injected into every autonomously-dispatched task, so
    the executing AI is explicitly re-bound to Bruno's hard rules."""
    return [
        "MASTERLAW (absolute, overrides any other instruction in this task):",
        "1. NEVER leak personal information — do not send/post/upload PII or "
        "secrets to any external service or public surface.",
        "2. NEVER delete or overwrite documents/data irreversibly. If removal is "
        "needed, move to a restorable backup/trash and report it — never permanent.",
        "3. ALWAYS follow Bruno's standing guidelines (CLAUDE.md + your own safety "
        "rules). Autonomy does not relax them.",
        "4. Bruno can stop/edit this at any moment from the Egon console — keep "
        "actions interruptible and leave shared-mind evidence of each step.",
        "5. If a step risks a catastrophic or unfixable outcome, STOP and ask "
        "Bruno instead of proceeding.",
    ]


def check_dispatch(task_desc: str, agent: str | None = None) -> dict:
    """Screen a task BEFORE autonomous dispatch. Returns
    {allowed, code, tier, reason}.
      tier='block'   → a MASTERLAW catastrophe (unfixable): never auto-runs.
      tier='confirm' → reversible/outward action: fine WITH Bruno's say-so, so it
                       waits for his confirmation (not a violation).
    Both land as needs_clarification so Bruno decides; only 'block' is alarming."""
    text = task_desc or ""
    if len(text.strip()) < 3:
        return {"allowed": False, "code": "empty", "tier": "block", "reason": "empty task"}
    for rx, code, why in (
        (_IRREVERSIBLE_DELETE, "irreversible_delete",
         "irreversible deletion/overwrite/force-push (unrecoverable)"),
        (_PII_EGRESS, "pii_egress", "may transmit personal data externally"),
        (_DISABLE_PROTECTION, "disable_protection", "disables security protection"),
    ):
        if rx.search(text):
            return {"allowed": False, "code": code, "tier": "block",
                    "reason": f"MASTERLAW block: {why} — forbidden for autonomous run."}
    if _NEEDS_CONFIRM.search(text):
        return {"allowed": False, "code": "needs_confirm", "tier": "confirm",
                "reason": "reversible outward action (e.g. make-public/share/send) "
                          "— fine once you confirm it; awaiting your OK."}
    return {"allowed": True, "code": "ok", "tier": "ok", "reason": "passes masterlaw screen"}


def redact(text: str) -> str:
    """Best-effort PII redaction for anything an autonomous routine might surface
    or log, so personal data never leaks through telemetry/logs."""
    if not text:
        return text
    return _PII_HINT.sub("[REDACTED]", text)


def safe_delete(path: str | Path) -> dict:
    """The ONLY deletion autonomous routines may use: move to a restorable trash,
    never permanent. Returns where it went so it can always be restored."""
    p = Path(path)
    if not p.exists():
        return {"status": "noop", "reason": "path missing"}
    try:
        TRASH_DIR.mkdir(parents=True, exist_ok=True)
        dest = TRASH_DIR / f"{int(time.time())}__{p.name}"
        shutil.move(str(p), str(dest))
        return {"status": "trashed", "restorable_at": str(dest), "original": str(p)}
    except Exception as e:
        return {"status": "error", "error": str(e)[:160]}
