"""Phone-port discovery via direct mDNS (zeroconf).

Replaces the unreliable `adb mdns services` call which fails silently on
multi-NIC Windows hosts (the PC has Ethernet + multiple wireless adapters;
adb's mDNS implementation doesn't always pick the right one).

Uses the `zeroconf` library to listen for Android wireless-debug
broadcasts (`_adb-tls-connect._tcp.local.`) directly. Returns the
first match in `timeout_s` seconds.

Usage:
    from lib.adapters.phone_discovery import find_phone
    addr = find_phone(serial_prefix="ZF524TB4GG", timeout_s=8)
    if addr: print(addr)  # e.g. "192.168.0.3:41385"
"""
from __future__ import annotations

import json
import socket
import time
from pathlib import Path

# Persistent lock written by scripts/lock_phone_to_5555.py — if present and
# reachable, we skip mDNS entirely. This is the durable solution: phone is
# locked to a fixed port until next reboot.
_LOCKED_FILE = Path(__file__).resolve().parent.parent.parent / "state/panop/locked_target.json"


def _locked_target_if_reachable(timeout_s: float = 1.5) -> str | None:
    """Read the locked target file written by lock_phone_to_5555.py and
    confirm the host:port is currently reachable via a TCP probe.
    Returns the target string if alive, None otherwise.
    """
    try:
        if not _LOCKED_FILE.exists():
            return None
        data = json.loads(_LOCKED_FILE.read_text(encoding="utf-8"))
        target = data.get("target", "")
        if ":" not in target:
            return None
        host, port_s = target.rsplit(":", 1)
        port = int(port_s)
        with socket.create_connection((host, port), timeout=timeout_s):
            return target
    except Exception:
        return None


def find_phone(serial_prefix: str = "ZF524TB4GG", timeout_s: float = 8.0) -> str | None:
    """Return 'IP:PORT' of the phone if found, else None."""
    try:
        from zeroconf import Zeroconf, ServiceBrowser, ServiceListener
    except ImportError:
        return None

    found: list[str] = []
    target_service = "_adb-tls-connect._tcp.local."

    class _Listener(ServiceListener):
        def add_service(self, zc, type_, name):
            if serial_prefix and serial_prefix not in name:
                return
            info = zc.get_service_info(type_, name, timeout=2000)
            if not info: return
            try:
                ip = socket.inet_ntoa(info.addresses[0])
                found.append(f"{ip}:{info.port}")
            except Exception:
                pass
        def update_service(self, *a, **k): pass
        def remove_service(self, *a, **k): pass

    zc = Zeroconf()
    try:
        ServiceBrowser(zc, target_service, _Listener())
        deadline = time.time() + timeout_s
        while time.time() < deadline and not found:
            time.sleep(0.25)
    finally:
        zc.close()

    return found[0] if found else None


def find_phone_any_service(serial_prefix: str = "ZF524TB4GG",
                           timeout_s: float = 8.0) -> str | None:
    """Like find_phone but also checks the legacy `_adb._tcp` advertisement
    (which is what shows up when `adb tcpip 5555` is set).

    Priority order:
      1. Locked target (scripts/lock_phone_to_5555.py) — instant, no rotation
      2. mDNS via zeroconf — works for both Android 11+ wireless-debug AND
         the legacy `adb tcpip` advertisement
    """
    # Try the locked target first — it's instant and never rotates.
    locked = _locked_target_if_reachable()
    if locked:
        return locked

    try:
        from zeroconf import Zeroconf, ServiceBrowser, ServiceListener
    except ImportError:
        return None

    found: list[str] = []

    class _L(ServiceListener):
        def add_service(self, zc, type_, name):
            if serial_prefix and serial_prefix not in name:
                return
            info = zc.get_service_info(type_, name, timeout=2000)
            if not info: return
            try:
                ip = socket.inet_ntoa(info.addresses[0])
                # Prefer _adb-tls-connect (modern, persistent) over _adb (legacy)
                addr = f"{ip}:{info.port}"
                if "tls-connect" in type_:
                    found.insert(0, addr)
                else:
                    found.append(addr)
            except Exception:
                pass
        def update_service(self, *a, **k): pass
        def remove_service(self, *a, **k): pass

    zc = Zeroconf()
    try:
        for svc in ("_adb-tls-connect._tcp.local.", "_adb._tcp.local."):
            ServiceBrowser(zc, svc, _L())
        deadline = time.time() + timeout_s
        while time.time() < deadline and not found:
            time.sleep(0.25)
    finally:
        zc.close()
    return found[0] if found else None
