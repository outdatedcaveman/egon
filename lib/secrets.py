"""Secrets loader — env vars > egon-config.json > None. Never leaves the machine.

All sensitive credentials (API keys, OAuth tokens, passwords) MUST flow through here.

Resolution order on `secrets.get("foo.bar")`:
  1. os.environ["FOO_BAR"]
  2. egon-local/config/connectors.env (auto-loaded into os.environ at import)
  3. egon-config.json (non-secret values; secret slots should be null)
  4. default
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

_EGON_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = _EGON_DIR / "egon-config.json"
LOCAL_ENV_PATH = _EGON_DIR.parent / "egon-local" / "config" / "connectors.env"

HAS_DPAPI = False
if sys.platform == "win32":
    try:
        import ctypes
        from ctypes import wintypes
        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]
        HAS_DPAPI = True
    except Exception:
        pass

def encrypt_dpapi(data: str, entropy: str = "egon") -> bytes:
    if not HAS_DPAPI:
        raise NotImplementedError("DPAPI is only available on Windows")
    data_bytes = data.encode('utf-8')
    entropy_bytes = entropy.encode('utf-8')
    
    blob_in = DATA_BLOB(len(data_bytes), ctypes.cast(ctypes.create_string_buffer(data_bytes), ctypes.POINTER(ctypes.c_byte)))
    blob_entropy = DATA_BLOB(len(entropy_bytes), ctypes.cast(ctypes.create_string_buffer(entropy_bytes), ctypes.POINTER(ctypes.c_byte)))
    blob_out = DATA_BLOB()
    
    ret = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(blob_in),
        u"egon_secret",
        ctypes.byref(blob_entropy),
        None,
        None,
        0x01,
        ctypes.byref(blob_out)
    )
    if not ret:
        raise ctypes.WinError()
    
    result = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    return result

def decrypt_dpapi(data_bytes: bytes, entropy: str = "egon") -> str:
    if not HAS_DPAPI:
        raise NotImplementedError("DPAPI is only available on Windows")
    entropy_bytes = entropy.encode('utf-8')
    
    blob_in = DATA_BLOB(len(data_bytes), ctypes.cast(ctypes.create_string_buffer(data_bytes), ctypes.POINTER(ctypes.c_byte)))
    blob_entropy = DATA_BLOB(len(entropy_bytes), ctypes.cast(ctypes.create_string_buffer(entropy_bytes), ctypes.POINTER(ctypes.c_byte)))
    blob_out = DATA_BLOB()
    
    ret = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in),
        None,
        ctypes.byref(blob_entropy),
        None,
        None,
        0x01,
        ctypes.byref(blob_out)
    )
    if not ret:
        raise ctypes.WinError()
    
    result = ctypes.string_at(blob_out.pbData, blob_out.cbData).decode('utf-8')
    ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    return result

SENSITIVE_KEYS = {"password", "api_key", "token", "client_secret", "secret"}

def encrypt_val(val: str) -> str:
    if not val:
        return val
    if val.startswith("__dpapi__:"):
        return val
    try:
        enc = encrypt_dpapi(val)
        return f"__dpapi__:{enc.hex()}"
    except Exception:
        return val

def decrypt_val(val: str) -> str:
    if not val or not val.startswith("__dpapi__:"):
        return val
    try:
        hex_data = val.partition(":")[2]
        dec = decrypt_dpapi(bytes.fromhex(hex_data))
        return dec
    except Exception:
        return val

def encrypt_dict(d: Any, parent_key: str = "") -> Any:
    if isinstance(d, dict):
        res = {}
        for k, v in d.items():
            if isinstance(v, str) and k.lower() in SENSITIVE_KEYS:
                res[k] = encrypt_val(v)
            else:
                res[k] = encrypt_dict(v, k)
        return res
    elif isinstance(d, list):
        return [encrypt_dict(x, parent_key) for x in d]
    return d

def decrypt_dict(d: Any) -> Any:
    if isinstance(d, dict):
        return {k: decrypt_dict(v) for k, v in d.items()}
    elif isinstance(d, list):
        return [decrypt_dict(x) for x in d]
    elif isinstance(d, str):
        return decrypt_val(d)
    return d

def _load_local_env() -> None:
    if not LOCAL_ENV_PATH.exists():
        return
    for raw in LOCAL_ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


_load_local_env()


def _load_file() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        raw_cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return decrypt_dict(raw_cfg)
    except Exception:
        return {}


def get(path: str, default: Any = None) -> Any:
    """Get a config value. `path` is dot-separated: 'instapaper.username'.
    Env vars override: 'instapaper.username' → INSTAPAPER_USERNAME.
    """
    env_key = path.upper().replace(".", "_")
    if env_key in os.environ and os.environ[env_key] != "":
        return os.environ[env_key]
    cur = _load_file()
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    if cur is None:
        return default
    return cur


def has(path: str) -> bool:
    return get(path) is not None

