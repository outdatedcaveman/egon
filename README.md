# Egon

Native visual control plane for Bruno's KMS.

Egon is a local-only desktop dashboard over the existing capture and storage
system: Notion KMS, the Drive/Obsidian vault mirror, Routster, Panop, Mouseion,
Google services, Zotero, Instapaper, Letterboxd, Kindle, TV Time, and related
adapters. It reads mostly cached/snapshotted state and keeps write actions
explicit.

## Current Shape

- `egon_app/` - PySide6 native desktop UI.
- `egon.py` and `views/` - legacy NiceGUI web UI kept for reference/fallback.
- `lib/adapters/` - one adapter per source.
- `lib/snapshot.py` - probes adapters and writes `last_pass.json`.
- `lib/state.py` - cache-first read path for the UI.
- `state/` - machine-local runtime state, ignored by git.
- `egon-local/` - private credentials and local pointers live outside this repo.

## State Files

`lib.snapshot` writes `last_pass.json` to two places:

- Local first: `state/last_pass.json`
- Vault mirror second: `G:/My Drive/MetaVault/VaultDrive/050 - Resources/egon/state/last_pass.json`

The UI reads whichever file is newest, so Egon stays usable if Google Drive is
slow, offline, or unavailable from a sandboxed tool.

## Run From Source

```powershell
cd egon
.\.venv\Scripts\python.exe -m egon_app.main
```

To refresh the dashboard data without opening the UI:

```powershell
.\.venv\Scripts\python.exe -m lib.snapshot
```

## Build

```powershell
.\.venv\Scripts\python.exe build_exe.py
```

The packaged app is written to `dist/Egon/Egon.exe`. By default this builds the
lean always-on desktop app. Use `.\.venv\Scripts\python.exe build_exe.py --full-ml`
when the packaged app itself needs local ML/search libraries bundled.

## Repo Hygiene

Do not commit runtime state, browser profiles, build output, tokens, or
`egon-config.json`. The public repo should contain the application code and
safe defaults only; credentials and machine-specific paths belong in the
private/local layer.
