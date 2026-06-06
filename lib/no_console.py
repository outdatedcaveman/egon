"""Global no-console guard — import this FIRST in any entry point that may
spawn subprocesses (adb, git, node, …).

Bruno's hard rule: no console/cmd windows ever pop up on the desktop.
`egon_app/main.py` has long carried an inline subprocess.Popen monkeypatch
that forces CREATE_NO_WINDOW. But standalone scripts (e.g.
scripts/rebuild_mind.py) that start Panop in-process DON'T go through
main.py, so Panop's ADB calls flashed `cmd.exe` windows. This module
centralises the patch so every entry point gets identical protection with
a single `import lib.no_console`.

Idempotent: patches subprocess.Popen.__init__ at most once per process.
No-op on non-Windows.
"""
from __future__ import annotations

import sys

_PATCHED = False


def install() -> None:
    global _PATCHED
    if _PATCHED or sys.platform != "win32":
        _PATCHED = True
        return
    import subprocess as _sp
    _CREATE_NO_WINDOW = 0x08000000
    _orig = _sp.Popen.__init__

    def _silent_init(self, *args, **kwargs):
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | _CREATE_NO_WINDOW
        si = kwargs.get("startupinfo") or _sp.STARTUPINFO()
        si.dwFlags |= _sp.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE
        kwargs["startupinfo"] = si
        return _orig(self, *args, **kwargs)

    _sp.Popen.__init__ = _silent_init
    _PATCHED = True


# Apply on import so `import lib.no_console` is enough.
install()
