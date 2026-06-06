"""Egon in-process services.

Per the 2026-05-27 rule, nothing runs outside Egon. The modules here are
QThread-/daemon-thread-backed services that start when Egon's MainWindow
opens and stop on close. Each one mirrors what used to be a standalone
script in `scripts/`, but lives inside the Egon process so it dies with
the UI.

The original standalone scripts are intentionally left in place
(additive principle, Bruno 2026-05-27 — don't delete other agents' work).
They are no longer auto-started; the Startup-folder shortcuts that used
to spawn them at logon have been moved to
`.backups/startup-disabled-2026-05-27/`.
"""
