"""Central path configuration for Egon.

Every machine-specific or user-specific path Egon needs is resolved here, in one
place, from environment variables with portable defaults derived from the user's
home directory and the location of this repository. Nothing user-specific is
hard-coded, so Egon runs unchanged on any machine.

Override any path by exporting the matching environment variable before launch:

    EGON_VAULT_ROOT     Root of the cloud/Drive vault mirror (optional).
                        If unset, Egon keeps all state locally under EGON_ROOT/state.
    EGON_SHARED_ROOT    Canonical shared substrate for AI projects, memories,
                        skills, sessions, artifacts, pointers, and state.
                        Defaults to ~/AI.
    EGON_ENV_FILE       Path to a .env file holding tokens (NOTION_TOKEN, etc.).
                        Defaults to EGON_ROOT/.env.
    EGON_BRAIN_DIRS     Path-separator list of agent "brain"/log dirs to ingest.
                        Defaults to the standard Claude / Codex / Antigravity dirs.
    ROUTSTER_PATH       Location of a local Routster checkout (optional).
    MOUSEION_PATH       Location of a local Mouseion checkout / refs.db (optional).
    PANOP_PATH          Location of a local Panop checkout (optional).
    ANDROID_ADB         Path to adb.exe (optional; falls back to PATH lookup).

All values are plain `pathlib.Path` objects. A path that does not exist on this
machine is not an error — the features that depend on it degrade gracefully.
"""
from __future__ import annotations

import os
from pathlib import Path

HOME = Path.home()

# Repository root (this file lives in <root>/lib/egon_paths.py).
EGON_ROOT = Path(__file__).resolve().parent.parent


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser() if raw else default


def _env_paths(name: str, defaults: list[Path]) -> list[Path]:
    raw = os.environ.get(name)
    if not raw:
        return defaults
    return [Path(p).expanduser() for p in raw.split(os.pathsep) if p.strip()]


# --- Shared AI workspace -------------------------------------------------
SHARED_ROOT = _env_path("EGON_SHARED_ROOT", HOME / "AI")
SHARED_PROJECTS = SHARED_ROOT / "projects"
SHARED_MEMORIES = SHARED_ROOT / "memories"
SHARED_SKILLS = SHARED_ROOT / "skills"
SHARED_SESSIONS = SHARED_ROOT / "sessions"
SHARED_ARTIFACTS = SHARED_ROOT / "artifacts"
SHARED_POINTERS = SHARED_ROOT / "pointers"
SHARED_STATE = SHARED_ROOT / "state"

# --- Local + vault state -------------------------------------------------
STATE_DIR = _env_path("EGON_STATE_DIR", EGON_ROOT / "state")

# Optional cloud/Drive mirror. When the directory is absent, Egon's snapshot
# logic simply uses the newest available local file, so nothing breaks.
VAULT_ROOT = _env_path("EGON_VAULT_ROOT", HOME / "EgonVault")
VAULT_RESOURCES = VAULT_ROOT / "050 - Resources"
VAULT_EGON = VAULT_RESOURCES / "egon"
VAULT_STATE = VAULT_EGON / "state"
VAULT_SNAPSHOTS = VAULT_EGON / "snapshots"
VAULT_MIRROR_ROOT = VAULT_RESOURCES / "Mirrors"
LAST_PASS = VAULT_STATE / "last_pass.json"

# --- Credentials env file ------------------------------------------------
ENV_FILE = _env_path("EGON_ENV_FILE", EGON_ROOT / ".env")

# --- Agent "brain" / log dirs ingested by the unified mind ---------------
BRAIN_DIRS = _env_paths(
    "EGON_BRAIN_DIRS",
    [
        SHARED_MEMORIES,
        SHARED_SESSIONS,
        HOME / ".claude" / "projects",
        HOME / ".codex",
        HOME / ".gemini" / "antigravity" / "brain",
    ],
)
# Convenience single-dir accessor for the Antigravity brain.
ANTIGRAVITY_BRAIN = HOME / ".gemini" / "antigravity" / "brain"

# --- Sibling project locations (all optional) ----------------------------
ROUTSTER_PATH = _env_path("ROUTSTER_PATH", HOME / "Routster")
MOUSEION_PATH = _env_path("MOUSEION_PATH", HOME / "Mouseion")
MOUSEION_DB = _env_path("MOUSEION_DB", MOUSEION_PATH / "refs.db")
PANOP_PATH = _env_path("PANOP_PATH", HOME / "Panop")
DOUBLE_PATH = _env_path("DOUBLE_PATH", SHARED_PROJECTS / "double")

# --- Android Debug Bridge (used by phone-keepalive helpers) --------------
ADB_PATH = _env_path(
    "ANDROID_ADB",
    HOME / "AppData" / "Local" / "Android" / "Sdk" / "platform-tools" / "adb.exe",
)
