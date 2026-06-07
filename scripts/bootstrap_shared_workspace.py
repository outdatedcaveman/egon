"""Create Bruno's canonical AI shared workspace and legacy path pointers.

Default mode is a dry run. Use --apply to write directories/manifests. Use
--adopt-projects to move project folders into the shared root and replace their
old paths with junctions. Use --adopt-agent-state separately because session
stores are live tool state and must be handled deliberately.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.shared_workspace import (  # noqa: E402
    PointerSpec,
    agent_state_specs,
    build_manifest,
    is_pointer,
    project_specs,
    shared_dirs,
    shared_root,
    write_manifest,
)


def _run_junction(source: Path, target: Path, apply: bool) -> str:
    if not apply:
        return f"would create junction {source} -> {target}"
    source.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        proc = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(source), str(target)],
            text=True,
            capture_output=True,
            timeout=30,
        )
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or "").strip())
        return (proc.stdout or "").strip()
    source.symlink_to(target, target_is_directory=True)
    return f"created symlink {source} -> {target}"


def _backup_path(root: Path, spec: PointerSpec) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return shared_dirs(root)["backups"] / stamp / spec.name


def _copy_then_replace_with_pointer(spec: PointerSpec, root: Path, apply: bool) -> dict[str, Any]:
    source = spec.source
    target = spec.target
    if not source.exists():
        if target.exists():
            msg = _run_junction(source, target, apply)
            return {"name": spec.name, "status": "linked_missing_source", "message": msg}
        return {"name": spec.name, "status": "missing", "message": f"{source} does not exist"}

    if is_pointer(source):
        return {"name": spec.name, "status": "already_pointer", "source": str(source)}

    if target.exists():
        return {
            "name": spec.name,
            "status": "conflict",
            "message": f"source and target both exist; inspect {source} and {target}",
        }

    if not source.is_dir():
        return {"name": spec.name, "status": "skipped", "message": f"{source} is not a directory"}

    backup = _backup_path(root, spec)
    if not apply:
        return {
            "name": spec.name,
            "status": "would_adopt",
            "source": str(source),
            "target": str(target),
            "backup": str(backup),
        }

    target.parent.mkdir(parents=True, exist_ok=True)
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)
    shutil.move(str(source), str(backup))
    msg = _run_junction(source, target, apply=True)
    return {
        "name": spec.name,
        "status": "adopted",
        "source": str(source),
        "target": str(target),
        "backup": str(backup),
        "message": msg,
    }


def bootstrap(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.root).expanduser() if args.root else shared_root()
    dirs = shared_dirs(root)
    planned_specs: list[PointerSpec] = []
    if args.adopt_projects:
        selected = set(args.project or [])
        specs = project_specs(root)
        if selected:
            specs = [spec for spec in specs if spec.name in selected]
        planned_specs.extend(specs)
    if args.adopt_agent_state:
        kinds = set(args.agent_kind or [])
        specs = agent_state_specs(root)
        if kinds:
            specs = [spec for spec in specs if spec.kind in kinds]
        planned_specs.extend(specs)

    result: dict[str, Any] = {
        "root": str(root),
        "apply": args.apply,
        "directories": {name: str(path) for name, path in dirs.items()},
        "manifest": str(root / "workspace.json"),
        "actions": [],
    }

    if args.apply:
        write_manifest(root)
    else:
        result["manifest_preview"] = build_manifest(root)

    for spec in planned_specs:
        result["actions"].append(_copy_then_replace_with_pointer(spec, root, args.apply))

    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="write changes")
    parser.add_argument("--root", help="shared root; defaults to EGON_SHARED_ROOT or ~/AI")
    parser.add_argument("--adopt-projects", action="store_true")
    parser.add_argument("--adopt-agent-state", action="store_true")
    parser.add_argument(
        "--agent-kind",
        action="append",
        choices=["memory", "skill", "session"],
        help="with --adopt-agent-state, only adopt this kind; repeatable",
    )
    parser.add_argument("--project", action="append", help="only adopt this project slug")
    args = parser.parse_args()
    print(json.dumps(bootstrap(args), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
