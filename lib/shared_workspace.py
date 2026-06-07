"""Canonical shared workspace for Bruno's AI tools.

Egon owns the filesystem truth that all AI bodies should agree on:

    <EGON_SHARED_ROOT>/
      projects/   canonical project checkouts
      memories/   durable and imported memories
      skills/     reusable skills shared across agents
      sessions/   agent transcript/session stores when safely adopted
      artifacts/  generated outputs and handoff files
      state/      shared runtime state
      pointers/   pointer manifests for app-specific folders

Tool-specific folders should become junctions or symlinks to this substrate
instead of drifting into separate copies of the same project/memory/skill.
"""
from __future__ import annotations

import json
import os
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

HOME = Path.home()
ROOT = Path(__file__).resolve().parent.parent


def shared_root() -> Path:
    return Path(os.environ.get("EGON_SHARED_ROOT", HOME / "AI")).expanduser()


def shared_dirs(root: Path | None = None) -> dict[str, Path]:
    base = root or shared_root()
    return {
        "root": base,
        "projects": base / "projects",
        "memories": base / "memories",
        "skills": base / "skills",
        "sessions": base / "sessions",
        "artifacts": base / "artifacts",
        "state": base / "state",
        "pointers": base / "pointers",
        "backups": base / "_backups",
    }


@dataclass(frozen=True)
class PointerSpec:
    name: str
    source: Path
    target: Path
    kind: str
    description: str

    def to_json(self) -> dict[str, str]:
        data = asdict(self)
        data["source"] = str(self.source)
        data["target"] = str(self.target)
        return data


def project_specs(root: Path | None = None) -> list[PointerSpec]:
    dirs = shared_dirs(root)
    return [
        PointerSpec(
            name="double",
            source=HOME / "Desktop" / "double-app",
            target=dirs["projects"] / "double",
            kind="project",
            description="Double ADHD learning app; legacy Antigravity path.",
        ),
        PointerSpec(
            name="flood",
            source=HOME / "Documents" / "New project" / "flood-review",
            target=dirs["projects"] / "flood",
            kind="project",
            description="Flood app checkout from the old generic New project folder.",
        ),
        PointerSpec(
            name="panop",
            source=HOME / "Desktop" / "Panop",
            target=dirs["projects"] / "panop",
            kind="project",
            description="Panop desktop/server project.",
        ),
        PointerSpec(
            name="mouseion",
            source=HOME / "Desktop" / "zoterpile-main",
            target=dirs["projects"] / "mouseion",
            kind="project",
            description="Mouseion / Zoterpile checkout.",
        ),
    ]


def agent_state_specs(root: Path | None = None) -> list[PointerSpec]:
    dirs = shared_dirs(root)
    return [
        PointerSpec(
            name="codex-memories",
            source=HOME / ".codex" / "memories",
            target=dirs["memories"] / "codex",
            kind="memory",
            description="Codex durable memories and rollout summaries.",
        ),
        PointerSpec(
            name="codex-skills",
            source=HOME / ".codex" / "skills",
            target=dirs["skills"] / "codex",
            kind="skill",
            description="Codex-local skills.",
        ),
        PointerSpec(
            name="shared-agent-skills",
            source=HOME / ".agents" / "skills",
            target=dirs["skills"] / "shared",
            kind="skill",
            description="User-authored shared skills already consumed by multiple agents.",
        ),
        PointerSpec(
            name="claude-projects",
            source=HOME / ".claude" / "projects",
            target=dirs["sessions"] / "claude-projects",
            kind="session",
            description="Claude Code project transcript store.",
        ),
        PointerSpec(
            name="claude-sessions",
            source=HOME / ".claude" / "sessions",
            target=dirs["sessions"] / "claude-sessions",
            kind="session",
            description="Claude session metadata store.",
        ),
        PointerSpec(
            name="antigravity-brain",
            source=HOME / ".gemini" / "antigravity" / "brain",
            target=dirs["memories"] / "antigravity",
            kind="memory",
            description="Antigravity/Gemini brain store when present.",
        ),
    ]


def manifest_path(root: Path | None = None) -> Path:
    return shared_dirs(root)["root"] / "workspace.json"


def build_manifest(root: Path | None = None) -> dict[str, Any]:
    base = root or shared_root()
    dirs = shared_dirs(base)
    return {
        "version": 1,
        "root": str(base),
        "directories": {name: str(path) for name, path in dirs.items()},
        "projects": [spec.to_json() for spec in project_specs(base)],
        "agent_state": [spec.to_json() for spec in agent_state_specs(base)],
        "policy": {
            "canonical_first": True,
            "legacy_paths_are_pointers": True,
            "agent_state_adoption_requires_explicit_flag": True,
        },
    }


def write_manifest(root: Path | None = None) -> Path:
    base = root or shared_root()
    for path in shared_dirs(base).values():
        path.mkdir(parents=True, exist_ok=True)
    path = manifest_path(base)
    path.write_text(json.dumps(build_manifest(base), indent=2) + "\n", encoding="utf-8")
    return path


def is_pointer(path: Path) -> bool:
    try:
        is_junction = getattr(path, "is_junction", None)
        if callable(is_junction) and is_junction():
            return True
        if path.is_symlink():
            return True
        attrs = path.stat().st_file_attributes  # type: ignore[attr-defined]
        return bool(attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except Exception:
        return False


def resolve_project_path(slug: str, root: Path | None = None) -> Path | None:
    """Return the canonical project path, falling back to known legacy paths."""
    key = slug.strip().lower()
    for spec in project_specs(root):
        if spec.name == key:
            if spec.target.exists():
                return spec.target
            if spec.source.exists():
                return spec.source
            return spec.target
    candidate = shared_dirs(root)["projects"] / key
    return candidate if candidate.exists() else None


def shared_status(root: Path | None = None) -> dict[str, Any]:
    base = root or shared_root()
    dirs = shared_dirs(base)
    specs = project_specs(base) + agent_state_specs(base)
    return {
        "root": str(base),
        "root_exists": base.exists(),
        "directories": {
            name: {"path": str(path), "exists": path.exists()}
            for name, path in dirs.items()
        },
        "pointers": [
            {
                **spec.to_json(),
                "source_exists": spec.source.exists(),
                "source_is_pointer": is_pointer(spec.source),
                "target_exists": spec.target.exists(),
            }
            for spec in specs
        ],
    }
