# Egon

**A local-first visual control plane for your personal knowledge system.**

Egon is a native desktop dashboard that sits on top of the tools you already use to
capture and store knowledge — Notion, an Obsidian/Drive vault, a reference manager,
read-it-later apps, and more — and gives you a single, fast, visible surface to see
their state and trigger actions. It reads mostly cached/snapshotted data and keeps
every write action explicit, so it stays responsive and never does anything behind
your back.

Everything runs on your machine. Nothing is sent anywhere except the API calls you
configure (e.g. your own Notion token), and credentials live outside the repo.

> Egon is deliberately modular: each data source is a small adapter, and any source
> you haven't configured simply doesn't appear. You can run it with a single
> connector or a dozen.

## Download (Windows)

Grab the latest **Egon-windows.zip** from [Releases](https://github.com/outdatedcaveman/egon/releases),
extract it anywhere, and run **Egon.exe**. No Python required. Then point Egon at your data
sources via the environment variables in [Configuration](#configuration).

## Features

- **One dashboard, many sources.** Pluggable adapters under `lib/adapters/` — Notion,
  vault/Obsidian mirror, reference manager, Google services, and more. Configure only
  what you use.
- **Cache-first and offline-tolerant.** Egon reads the newest available snapshot, so
  the UI stays usable even when a cloud source is slow, offline, or unavailable.
- **Explicit writes.** Nothing mutates your data without a deliberate click.
- **Native desktop app.** PySide6 UI, packaged to a standalone Windows executable.
- **Portable by design.** All machine-specific paths are resolved from environment
  variables with sensible defaults — see [Configuration](#configuration).

## Configuration

Egon resolves every machine-specific path in one place: [`lib/egon_paths.py`](lib/egon_paths.py).
Override any of them with environment variables; all are optional and degrade
gracefully when unset.

| Variable | Purpose | Default |
|---|---|---|
| `EGON_VAULT_ROOT` | Cloud/Drive vault mirror root | `~/EgonVault` |
| `EGON_ENV_FILE` | `.env` file holding tokens (e.g. `NOTION_TOKEN`) | `<repo>/.env` |
| `EGON_BRAIN_DIRS` | Agent log/brain dirs to ingest (path-separated) | Claude / Codex / Antigravity dirs |
| `NOTION_TOKEN` | Notion integration token | _unset_ |
| `NOTION_KMS_ROOT_ID` / `NOTION_HOME_PAGE_ID` | Your Notion page IDs | _unset_ |
| `ROUTSTER_PATH` / `MOUSEION_PATH` / `PANOP_PATH` | Local sibling-app checkouts | `~/<App>` |
| `ANDROID_ADB` | Path to `adb.exe` (phone helpers) | standard SDK location |

Non-secret settings live in `egon-config.json` (created on first run); secrets flow
through `lib/secrets.py` (env vars → local config → never committed).

## Run from source

```powershell
cd egon
python -m venv .venv
.\.venv\Scripts\pip.exe install -e .
.\.venv\Scripts\python.exe -m egon_app.main
```

Refresh the dashboard data without opening the UI:

```powershell
.\.venv\Scripts\python.exe -m lib.snapshot
```

## Build a standalone executable

```powershell
.\.venv\Scripts\python.exe build_exe.py
```

The packaged app lands in `dist/Egon/Egon.exe`. Use `build_exe.py --full-ml` when the
packaged app itself needs local ML/search libraries bundled.

## Project layout

```
egon/
├── egon_app/        # PySide6 native desktop UI (main entry point)
├── lib/
│   ├── adapters/    # one adapter per data source
│   ├── egon_paths.py# central, env-overridable path config
│   ├── secrets.py   # env → local config secret loader
│   ├── snapshot.py  # probes adapters, writes last_pass.json
│   └── state.py     # cache-first read path for the UI
├── views/           # legacy NiceGUI web UI (kept as fallback)
├── scripts/         # maintenance + integration helpers
└── docs/            # architecture and design notes
```

## Repo hygiene

Runtime state, browser profiles, build output, tokens, and `egon-config.json` are
git-ignored and must never be committed. The repository contains application code and
safe defaults only; credentials and machine-specific values belong in your local
environment.

## License

MIT — see [LICENSE](LICENSE).
