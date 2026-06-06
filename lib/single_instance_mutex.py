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

import sys

_MUTEX_HANDLE = None  # keep alive for process lifetime


def claim_or_exit(name: str) -> bool:
    """Return True if WE claimed the mutex. False if someone else owns it.

    On non-Windows platforms always returns True (caller falls through
    to whatever secondary guard exists)."""
    global _MUTEX_HANDLE
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
        handle = CreateMutexW(None, False, full)
        last_error = ctypes.get_last_error()

        if handle == 0:
            # Failed to create — best-effort fall through.
            return True

        if last_error == ERROR_ALREADY_EXISTS:
            # Another process already owns this mutex.
            # Close our handle (we never owned it) and signal failure.
            try:
                kernel32.CloseHandle(handle)
            except Exception:
                pass
            return False

        # We own the mutex. Hold the handle for the process lifetime so
        # the kernel keeps the name reserved until we exit.
        _MUTEX_HANDLE = handle
        return True
    except Exception:
        # ctypes failure on a weird system — fall through.
        return True
