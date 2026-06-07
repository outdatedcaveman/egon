"""Build the native Egon app into dist/Egon/Egon.exe."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ICON = ROOT / "shell" / "egon.ico"
ENTRY = ROOT / "egon_app" / "main.py"

HIDDEN_IMPORTS = [
    "lib.state",
    "lib.ledger",
    "lib.actions",
    "lib.status_cache",
    "lib.panop_proc",
    "lib.snapshot",
    "lib.snapshot_store",
    "lib.secrets",
    "lib.google_oauth",
    "lib.scraper",
    "lib.adapters.android_tabs",
    "lib.adapters.chrome_bookmarks",
    "lib.adapters.chrome_tabs",
    "lib.adapters.gcalendar",
    "lib.adapters.gdrive",
    "lib.adapters.gfit",
    "lib.adapters.gmail",
    "lib.adapters.instapaper",
    "lib.adapters.instapaper_full",
    "lib.adapters.kindle",
    "lib.adapters.letterboxd",
    "lib.adapters.mouseion",
    "lib.adapters.notion",
    "lib.adapters.notion_workspace",
    "lib.adapters.paperpile",
    "lib.adapters.routster",
    "lib.adapters.tvtime",
    "lib.adapters.vault",
    "lib.adapters.youtube",
    "lib.adapters.zotero_local",
    "lib.adapters.zotero_web",
]

ML_MODULES = [
    # zeroconf bundles mDNS .pyd extensions that AV may lock during build;
    # the import is guarded (optional phone auto-discovery), so excluding is safe.
    "zeroconf",
    "torch",
    "transformers",
    "sentence_transformers",
    "sklearn",
    "scipy",
    "tensorflow",
    "numpy.distutils",
]


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    if not ENTRY.exists():
        print(f"missing entry point: {ENTRY}")
        return 1
    if not ICON.exists():
        print(f"missing icon: {ICON}")
        return 2

    for dirname in ("build", "dist"):
        path = ROOT / dirname
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)

    include_ml = "--full-ml" in sys.argv[1:]

    args = [
        sys.executable, "-m", "PyInstaller",
        "--name", "Egon",
        "--onedir",
        "--windowed",
        "--icon", str(ICON),
        "--clean",
        "--noconfirm",
        "--collect-submodules", "egon_app",
        "--collect-data", "PySide6",
        "--add-data", f"{ICON}{';' if sys.platform == 'win32' else ':'}shell",
    ]
    for module in HIDDEN_IMPORTS:
        args.extend(["--hidden-import", module])
    if not include_ml:
        for module in ML_MODULES:
            args.extend(["--exclude-module", module])
    args.append(str(ENTRY))

    print("=" * 70)
    mode = "full ML" if include_ml else "lean always-on"
    print(f"Building Egon.exe ({mode}) - first run can take 3-5 minutes")
    print("=" * 70)
    rc = subprocess.call(args, cwd=str(ROOT))
    if rc != 0:
        print(f"\nBuild FAILED (exit {rc})")
        return rc

    exe = ROOT / "dist" / "Egon" / "Egon.exe"
    if not exe.exists():
        exe = ROOT / "dist" / "Egon.exe"
    if exe.exists():
        size_mb = exe.stat().st_size / 1024 / 1024
        print("\n" + "=" * 70)
        print(f"  BUILT  ->  {exe}")
        print(f"  size   ->  {size_mb:.1f} MB")
        print("=" * 70)
        print(r"Double-click dist\Egon\Egon.exe to launch, or pin it to your taskbar.")
        return 0
    print("Build succeeded but Egon.exe not found in dist/")
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
