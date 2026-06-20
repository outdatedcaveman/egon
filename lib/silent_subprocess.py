"""Silences all subprocess.Popen calls on Windows to prevent console windows.

Import this module at the TOP of any script that runs independently
(not under egon_app.main which has its own monkeypatch):

    import lib.silent_subprocess  # noqa: F401  — side-effect import

After this import, every subprocess.Popen (including subprocess.run,
subprocess.call, subprocess.check_output etc.) will run without creating
a visible console window.
"""
from __future__ import annotations

import subprocess as _sp
import sys

if sys.platform == "win32":
    _CREATE_NO_WINDOW = 0x08000000
    _orig_popen_init = _sp.Popen.__init__

    def _silent_popen_init(self, *args, **kwargs):
        flags = kwargs.get("creationflags", 0) | _CREATE_NO_WINDOW
        kwargs["creationflags"] = flags
        si = kwargs.get("startupinfo")
        if si is None:
            si = _sp.STARTUPINFO()
        si.dwFlags |= _sp.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE
        kwargs["startupinfo"] = si
        return _orig_popen_init(self, *args, **kwargs)

    _sp.Popen.__init__ = _silent_popen_init

    # Monkey-patch subprocess.run to prevent hangs on Windows during timeouts
    # when a process becomes a zombie in uninterruptible kernel sleep.
    _orig_run = _sp.run

    def _silent_run(*popenargs, **kwargs):
        timeout = kwargs.pop("timeout", None)
        if timeout is not None:
            check = kwargs.pop("check", False)
            capture_output = kwargs.pop("capture_output", False)
            if capture_output:
                if kwargs.get("stdout") is not None or kwargs.get("stderr") is not None:
                    raise ValueError("stdout and stderr arguments may not be used with capture_output.")
                kwargs["stdout"] = _sp.PIPE
                kwargs["stderr"] = _sp.PIPE
            with _sp.Popen(*popenargs, **kwargs) as process:
                try:
                    stdout, stderr = process.communicate(timeout=timeout)
                    ret = _sp.CompletedProcess(process.args, process.returncode, stdout, stderr)
                    if check and process.returncode != 0:
                        raise _sp.CalledProcessError(process.returncode, process.args, output=stdout, stderr=stderr)
                    return ret
                except _sp.TimeoutExpired as exc:
                    try:
                        process.kill()
                    except Exception:
                        pass
                    # Safely close pipe handles to prevent blocking in communicate()
                    for pipe in (process.stdout, process.stderr, process.stdin):
                        if pipe:
                            try:
                                pipe.close()
                            except Exception:
                                pass
                    raise _sp.TimeoutExpired(process.args, timeout, output=None, stderr=None)
        return _orig_run(*popenargs, **kwargs)

    _sp.run = _silent_run

