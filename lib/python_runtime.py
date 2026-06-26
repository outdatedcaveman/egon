"""Resolve Egon's Python runtime without using venv redirector stubs.

Windows venv launchers in .venv/Scripts can spawn a wrapper parent plus a real
base-python child. For Egon's always-on substrate that looks like duplicate core
apps and makes process ownership brittle. Use the base interpreter from
pyvenv.cfg with the venv site-packages on PYTHONPATH instead.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def venv_dir(root: Path) -> Path:
    return root / ".venv"


def site_packages(root: Path) -> Path:
    if sys.platform == "win32":
        return venv_dir(root) / "Lib" / "site-packages"
    pyver = f"python{sys.version_info.major}.{sys.version_info.minor}"
    return venv_dir(root) / "lib" / pyver / "site-packages"


def base_python(root: Path, *, windowed: bool = True) -> Path:
    """Return the base interpreter for the repo venv, avoiding stub launchers."""
    venv = venv_dir(root)
    cfg = venv / "pyvenv.cfg"
    exe_name = "pythonw.exe" if windowed and sys.platform == "win32" else (
        "python.exe" if sys.platform == "win32" else "python"
    )
    try:
        for line in cfg.read_text(encoding="utf-8").splitlines():
            if line.lower().replace(" ", "").startswith("home="):
                candidate = Path(line.split("=", 1)[1].strip()) / exe_name
                if candidate.exists():
                    return candidate
    except Exception:
        pass

    # Fallbacks are only for degraded/local dev cases where pyvenv.cfg is absent.
    scripts_dir = venv / ("Scripts" if sys.platform == "win32" else "bin")
    fallback = scripts_dir / exe_name
    if fallback.exists():
        return fallback
    return Path(sys.executable)


def runtime_env(root: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    site = str(site_packages(root))
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = site if not existing else site + os.pathsep + existing
    env["PYTHONDONTWRITEBYTECODE"] = env.get("PYTHONDONTWRITEBYTECODE", "1")
    if extra:
        env.update(extra)
    return env
