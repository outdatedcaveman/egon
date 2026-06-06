"""Egon native desktop app (PySide6).

This package is the standalone desktop UI for Egon. It replaces the previous
NiceGUI/web-rendered front-end with native Qt widgets — real Windows
controls, no embedded browser, no HTML rendering layer.

Architecture:
    main.py     — QApplication bootstrap, theme, single-instance guard
    window.py   — MainWindow: sidebar + QStackedWidget content area
    theme.py    — Qt stylesheet (dark teal palette, matches former UI)
    pages/      — one QWidget per nav item (home, inbox, ledger, etc.)
    data.py     — adapter to the existing pure-logic `lib/` package

The lib/ layer (state, classifier, ledger, panop_client) is reused as-is.
Only the rendering layer is rewritten. No NiceGUI imports anywhere here.

Compiled to a single Egon.exe via PyInstaller — see build_exe.py.
"""
__version__ = "0.3.0-native"
