# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.hooks import collect_submodules

import os
_SPEC_DIR = os.path.abspath(os.getcwd())
datas = [(os.path.join(_SPEC_DIR, 'shell', 'egon.ico'), 'shell')]
hiddenimports = ['lib.state', 'lib.ledger', 'lib.actions', 'lib.status_cache', 'lib.panop_proc', 'lib.snapshot', 'lib.snapshot_store', 'lib.secrets', 'lib.google_oauth', 'lib.scraper', 'lib.adapters.android_tabs', 'lib.adapters.chrome_bookmarks', 'lib.adapters.chrome_tabs', 'lib.adapters.gcalendar', 'lib.adapters.gdrive', 'lib.adapters.gfit', 'lib.adapters.gmail', 'lib.adapters.instapaper', 'lib.adapters.instapaper_full', 'lib.adapters.kindle', 'lib.adapters.letterboxd', 'lib.adapters.mouseion', 'lib.adapters.notion', 'lib.adapters.notion_workspace', 'lib.adapters.paperpile', 'lib.adapters.routster', 'lib.adapters.tvtime', 'lib.adapters.vault', 'lib.adapters.youtube', 'lib.adapters.zotero_local', 'lib.adapters.zotero_web', 'zeroconf']
datas += collect_data_files('PySide6')
hiddenimports += collect_submodules('egon_app')


a = Analysis(
    [os.path.join(_SPEC_DIR, 'egon_app', 'main.py')],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['torch', 'transformers', 'sentence_transformers', 'sklearn', 'scipy', 'tensorflow', 'numpy.distutils'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Egon',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[os.path.join(_SPEC_DIR, 'shell', 'egon.ico')],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Egon',
)
