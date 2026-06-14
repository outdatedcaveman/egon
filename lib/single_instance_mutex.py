"""Windows kernel named-mutex single-instance guard.

The QLocalServer guard in egon_app/main.py works for the common case,
but it has a small race window where two simultaneous launches can
both believe they're first. A kernel-level named mutex closes that
completely — the Windows kernel serializes access, so exactly one
process owns the mutex at a time. Cross-platform: no-op on non-Windows
(falls back to QLocalServer alone).

Usage:
    from lib.single_instance_mutex import claim_or_exit
    if not claim_or_exit("Egon-2026-05"):
        return 0    # another Egon already owns the mutex
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_MUTEX_HANDLES = []  # keep alive for process lifetime
_LOCK_FILES = []  # keep file handles alive for process lifetime


def _lock_path(name: str) -> Path:
    root = Path(__file__).resolve().parent.parent
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_") or "egon"
    return root / "state" / "locks" / f"{safe[:120]}.lock"


def _claim_file_lock(name: str) -> bool:
    """Best-effort Windows file lock fallback for singleton processes."""
    if sys.platform != "win32":
        return True
    fh = None
    try:
        import msvcrt

        path = _lock_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = path.open("a+b")
        if fh.tell() == 0:
            fh.write(b"0")
            fh.flush()
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        _LOCK_FILES.append(fh)
        return True
    except OSError:
        if fh is not None:
            try:
                fh.close()
            except Exception:
                pass
        return False
    except Exception:
        if fh is not None:
            try:
                fh.close()
            except Exception:
                pass
        return True


def claim_or_exit(name: str) -> bool:
    """Return True if WE claimed the mutex. False if someone else owns it.

    On non-Windows platforms always returns True (caller falls through
    to whatever secondary guard exists)."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        # CreateMutexW(SECURITY_ATTRIBUTES*, BOOL bInitialOwner, LPCWSTR lpName)
        CreateMutexW = kernel32.CreateMutexW
        CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        CreateMutexW.restype = wintypes.HANDLE

        ERROR_ALREADY_EXISTS = 0xB7

        # Use a Local\ prefix so the mutex is scoped to the current user
        # session (avoids clashes across Fast User Switching).
        full = f"Local\\{name}"
        ctypes.set_last_error(0)
        handle = CreateMutexW(None, False, full)
        last_error = ctypes.get_last_error()

        if handle == 0:
            # Failed to create; still try the file lock before falling through.
            return _claim_file_lock(name)

        if last_error == ERROR_ALREADY_EXISTS:
            # Another process already owns this mutex.
            # Close our handle (we never owned it) and signal failure.
            try:
                kernel32.CloseHandle(handle)
            except Exception:
                pass
            return False

        # We own the mutex. Also claim a 1-byte file lock; this gives Egon's
        # always-on services a second OS-released singleton guard if the named
        # mutex path ever behaves inconsistently.
        if not _claim_file_lock(name):
            try:
                kernel32.CloseHandle(handle)
            except Exception:
                pass
            return False

        # Hold handles for the process lifetime so the kernel keeps the guards
        # reserved until we exit.
        _MUTEX_HANDLES.append(handle)
        return True
    except Exception:
        # ctypes failure on a weird system; still try the file lock before
        # falling through unprotected.
        return _claim_file_lock(name)
