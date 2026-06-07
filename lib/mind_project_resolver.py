"""Canonical project slug resolver.

The whole point of Egon's unified mind is that **a Claude session about
Egon and a Codex session about Egon land under the same project**. That
only works if all three agents (and Antigravity, and any future agent)
agree on what "the Egon project" is *called* in the mind. They don't
naturally — Claude encodes path-as-name (`C--Users-you-Claude-Code--egon`),
Codex has no project concept at all (just rollout UUIDs), Antigravity
uses session UUIDs + filename prefixes. This module is the
normalization layer that turns any of those into one canonical slug.

Two ways to drive it:

  • **Heuristic** — strip path separators, take the last meaningful
    segment, lowercase, drop noise prefixes (`Claude-Code-`,
    `egon-`-with-archive-suffix, …). Good enough for most cases.

  • **Manual alias map** — Bruno's `egon-config.json` has a
    `mind.project_aliases` block. Anything in there overrides the
    heuristic. E.g. {"kms_auto_router": "routster"} so the actual
    directory name maps to the project Bruno mentally calls it by.

Use:
    from lib.mind_project_resolver import canonical_slug
    canonical_slug("/path/to/egon")       # -> "egon"
    canonical_slug("C--Users-you-Claude-Code--egon")      # -> "egon"
    canonical_slug("kms_auto_router")                       # -> "routster"  (via alias)
    canonical_slug("routster_v3_plan.md")                   # -> "routster"
    canonical_slug(None)                                    # -> None
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _ROOT / "egon-config.json"

# Built-in aliases — extend via egon-config.json.mind.project_aliases.
# Keys are heuristic outputs; values are the canonical slug.
_DEFAULT_ALIASES: dict[str, str] = {
    "kms_auto_router": "routster",
    "kmsautorouter":   "routster",
    "zoterpile":       "mouseion",
    "zoterpile-main":  "mouseion",
    "egon-local":      "egon",
    "egon.legacy":     "egon",
    # Common Antigravity filename hints that should collapse to a project
    "routster_release_plan": "routster",
    "routster_v1_2_release": "routster",
    "routster_v2_plan":      "routster",
    "routster_v3_plan":      "routster",
    "mouseion_compile":      "mouseion",
    "mouseion_network_bench":"mouseion",
    "mouseion_setbased_dedup":"mouseion",
    # Antigravity scratch app folders → project (Bruno 2026-05-29)
    "double-app":      "double",
    "double_app":      "double",
    "new project":     "flood",
    "new-project":     "flood",
    # Asympt — AI audio editor for podcasters, built in the `noiacast` repo.
    # Bruno also refers to it as "Asynth". Unify all three onto "asympt".
    "noiacast":        "asympt",
    "asynth":          "asympt",
}

# Noise prefixes we strip during heuristic extraction.
_NOISE_PREFIXES = (
    "claude-code-", "claude_code_",
    "the-",
)

# Known canonical slugs Bruno actively works on. The resolver's last-resort
# pass matches any candidate that starts with one of these (longest first),
# so we don't have to enumerate every Antigravity screenshot suffix.
_KNOWN_CANONICAL_SLUGS = {
    "egon", "panop", "routster", "mouseion", "synesism",
    "infohub", "careerops", "asympt", "claude-meta",
    # Bruno 2026-05-29: projects surfaced by the inventory audit.
    "double",       # ADHD learning app, Antigravity scratch/double-app
    "flood",        # Codex project under Documents/New project
    "citizenship",  # Portuguese citizenship (Lei 37/81) dossier
    "ancestry",     # family-tree / genealogy research
}


@lru_cache(maxsize=1)
def _user_aliases() -> dict[str, str]:
    try:
        with _CONFIG_PATH.open(encoding="utf-8") as f:
            cfg = json.load(f)
        m = (cfg.get("mind") or {}).get("project_aliases") or {}
        if isinstance(m, dict):
            return {str(k).lower(): str(v).lower() for k, v in m.items()}
    except Exception:
        pass
    return {}


def _aliases() -> dict[str, str]:
    """User aliases override defaults so Bruno can correct anything."""
    return {**_DEFAULT_ALIASES, **_user_aliases()}


def _last_segment(s: str) -> str:
    """Last meaningful path-like segment of `s`. Treats both `/`, `\\`
    and Claude's `--` encoding as separators."""
    # Normalize Claude's path-as-name encoding (`a--b--c` → `a/b/c`)
    if "--" in s and "/" not in s and "\\" not in s:
        s = s.replace("--", "/")
    # File extension off
    if "." in s.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]:
        s = re.sub(r"\.[A-Za-z0-9]{1,8}$", "", s)
    for sep in ("/", "\\"):
        if sep in s:
            parts = [p for p in s.split(sep) if p.strip()]
            if parts:
                s = parts[-1]
    return s


def _strip_noise(s: str) -> str:
    low = s.lower()
    for p in _NOISE_PREFIXES:
        if low.startswith(p):
            return low[len(p):]
    return low


def canonical_slug(value: str | None) -> str | None:
    """Best-effort canonical project slug. Returns None when nothing
    plausible can be extracted — callers should pass through.

    The function is forgiving by design: garbage in returns None, not
    an exception. The mind treats `None` project as "unattributed" and
    surfaces those rows under their agent name in the dashboard.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None

    # Strip Antigravity filename suffixes like `_v3_plan.md` BEFORE the
    # path split, because those suffixes ARE the project hint.
    candidate = _last_segment(s)
    candidate = _strip_noise(candidate)

    # Strip trailing all-digit timestamps that Antigravity screenshots stamp on
    # (e.g. `routster_loaded_1775605841876`). 6+ digits to avoid eating version
    # numbers like `v3`.
    candidate = re.sub(r"_\d{6,}$", "", candidate)

    # Strip leading verbs Antigravity uses for its visual artifacts
    # (`verify_routster_launch`, `check_routster_running`, `inspect_routster_db`).
    candidate = re.sub(r"^(?:verify|check|inspect|view|see)_", "", candidate)

    # Iteratively strip well-known Antigravity / Codex filename tails so
    # `routster_v3_plan` collapses cleanly through both `_plan` and `_v3`.
    tail_re = re.compile(
        r"_(?:"
        r"v\d+(?:_\d+)*|"
        r"release(?:_?plan)?|plan|loaded|running|setup|check|verify|install|"
        r"compile|bench|enrichment[a-z_]*|probe[a-z_]*|recovery[a-z_]*|"
        r"debug[a-z_]*|hard_tail[a-z_]*|"
        # UI-state suffix words Antigravity uses in its screenshot names:
        r"launch|launching|window|screen|panel|view|dialog|popup|"
        r"db|status|state|config|settings|onboarding"
        r")$",
        re.IGNORECASE,
    )
    while True:
        stripped = tail_re.sub("", candidate)
        if stripped == candidate:
            break
        candidate = stripped

    candidate = candidate.strip("-_ ").lower()
    # Generic structural / document words that are NEVER a project name. These
    # are the filename fragments (implementation_plan.md → "implementation",
    # task.md → "task", walkthrough.md → "walkthrough") that used to pollute
    # the project list with dozens of fake Antigravity "projects". Bruno
    # 2026-05-29. Anything matching here is unattributed, not a project.
    _GENERIC = {
        "main", "src", "lib", "scripts", "script",
        "implementation", "task", "tasks", "walkthrough", "plan", "plans",
        "note", "notes", "overview", "summary", "readme", "index", "app",
        "content", "message", "messages", "step", "steps", "feature",
        "features", "reference", "references", "test", "tests", "temp",
        "scratch", "draft", "untitled", "new", "project", "projects",
        "changelog", "release", "setup", "config", "settings", "metadata",
    }
    if not candidate or candidate in _GENERIC:
        return None
    # User-path fragments from Claude's path-encoded dir names that aren't a
    # real project (e.g. `C--Users-you-Claude-Code` → "users-you-claude-code",
    # the Claude Code home itself, not a project). Treat as unattributed.
    # Bruno 2026-05-29.
    if (candidate.startswith("users-") or candidate.startswith("c-users")
            or candidate in {"claude-mem-observer-sessions",
                             "claude-mem", "claude-code"}):
        return None

    aliases = _aliases()
    if candidate in aliases:
        return aliases[candidate]

    # Final pass: if the candidate STARTS WITH a known canonical project
    # slug (one of the alias *values*, plus the known KMS family), match
    # the longest-prefix one. This catches Antigravity screenshot names
    # we couldn't fully strip via tail rules — e.g. `routster_theme_preview`
    # or `mouseion_pdf_manager` — without us enumerating every UI suffix.
    known = set(aliases.values()) | _KNOWN_CANONICAL_SLUGS
    for k in sorted(known, key=len, reverse=True):
        if k and (candidate == k
                  or candidate.startswith(k + "_")
                  or candidate.startswith(k + "-")):
            return k

    # Sentence/title fragments masquerading as slugs: a real project name is
    # short (1-3 tokens). 4+ hyphen-separated word tokens is almost always a
    # captured prompt/title (e.g. "from-recent-prs-and-reviews-suggest"), not
    # a project. Reject unless it was a known slug (handled above).
    # Bruno 2026-05-29.
    if candidate.count("-") >= 3:
        return None

    return candidate


def known_project_slugs() -> set[str]:
    """The set of slugs we treat as definitely-real projects: the built-in
    canonical family plus every alias *target*. Used by callers that want to
    reject heuristic echoes (e.g. matching a free-text title word) and only
    accept a slug they're confident about."""
    return set(_aliases().values()) | set(_KNOWN_CANONICAL_SLUGS)


def _bust_cache() -> None:
    """Reset the user-alias cache (called after egon-config.json edits)."""
    _user_aliases.cache_clear()
