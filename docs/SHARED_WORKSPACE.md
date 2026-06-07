# Egon Shared Workspace

Egon now defines one canonical filesystem substrate for projects, memories,
skills, sessions, artifacts, pointers, and shared state. The default root is:

```text
C:\Users\bruno\AI
```

Override it with `EGON_SHARED_ROOT` if this needs to live on another drive.

## Layout

```text
AI/
  projects/   canonical project checkouts, for example projects/double
  memories/   durable memories and imported agent memory stores
  skills/     shared and agent-specific skills
  sessions/   adopted live session/transcript stores
  artifacts/  generated outputs and handoff files
  state/      shared runtime state
  pointers/   pointer manifests and helper files
```

Legacy app and tool folders should become Windows junctions or symlinks into
this root. For example, `C:\Users\bruno\Desktop\double-app` should point to
`C:\Users\bruno\AI\projects\double`.

## Bootstrap

Dry-run the current plan:

```powershell
python scripts\bootstrap_shared_workspace.py --adopt-projects
```

Create the root, write `workspace.json`, and adopt Double only:

```powershell
python scripts\bootstrap_shared_workspace.py --apply --adopt-projects --project double
```

Adopting memory and skill stores can be done separately from live sessions:

```powershell
python scripts\bootstrap_shared_workspace.py --apply --adopt-agent-state --agent-kind memory --agent-kind skill
```

Adopting live session state is separate on purpose:

```powershell
python scripts\bootstrap_shared_workspace.py --apply --adopt-agent-state
```

The bootstrap copies each adopted directory into the shared root, moves the
original folder into `AI\_backups\<timestamp>\...`, then creates a junction at
the original path. If the source and target both already exist, it reports a
conflict instead of guessing.

## Policy

- Canonical paths live under `AI`.
- Legacy paths are pointers, not second sources of truth.
- Egon's resolver still normalizes project names, but project lookup should
  prefer `lib.shared_workspace.resolve_project_path`.
- Agent session stores are live user state. Adopt them only when no tool is
  actively writing, and keep the backup folder until the tool has restarted and
  verified the pointer.
