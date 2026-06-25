"""Qt stylesheet — Premium macOS-aesthetic design system (Option A + Things 3 / Craft)."""
from __future__ import annotations

PALETTES = {
    "light": {
        "bg":            "#f5f5f7",   # macOS system gray 6
        "bg_panel":      "#ffffff",   # Crisp white card surface
        "bg_panel_2":    "#e5e5ea",   # Hover / light gray
        "bg_select":     "#e1e4e8",   # Selected list item
        "border":        "#d1d1d6",   # Hairline separator
        "text":          "#1d1d1f",   # SF primary text
        "text_dim":      "#48484a",   # Secondary body
        "text_muted":    "#86868b",   # Muted caption
        "accent":        "#ff3b30",   # Vibrant system red
        "accent_strong": "#c8102e",
        "ledger":        "#ff9500",   # Apple gold/amber
        "danger":        "#ff3b30",
        "ok":            "#34c759",   # Apple green
    },
    "dark": {
        "bg":            "#0c0d0f",   # Deep space-black/dark slate
        "bg_panel":      "#16181c",   # Elevated space-gray card surface
        "bg_panel_2":    "#212328",   # Hover / active panel
        "bg_select":     "#2a2d34",   # Selected list item
        "border":        "#22252a",   # Sub-pixel border
        "text":          "#f5f5f7",   # SF primary text
        "text_dim":      "#a1a1a6",   # Secondary body
        "text_muted":    "#76767f",   # Muted caption
        "accent":        "#ff453a",   # Vibrant system red
        "accent_strong": "#ff6961",
        "ledger":        "#ff9f0a",   # Apple gold/amber
        "danger":        "#ff453a",
        "ok":            "#30d158",   # Apple green
    }
}


def get_stylesheet(dark: bool = True) -> str:
    p = PALETTES["dark" if dark else "light"]
    return f"""
QMainWindow, QWidget#central {{
    background: {p['bg']};
    color: {p['text']};
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text", "Segoe UI", "Inter", sans-serif;
    font-size: 13px;
}}

QScrollArea, QScrollArea > QWidget {{
    background: transparent;
    border: none;
}}

/* ---------- header strip ---------- */
QFrame#headerBar {{
    background: {p['bg_panel']};
    border-bottom: 1px solid {p['border']};
}}
QLabel#headerTitle {{
    color: {p['text']};
    font-size: 16px;
    font-weight: 700;
    padding-left: 18px;
    letter-spacing: -0.01em;
}}
QLabel#headerStats {{
    color: {p['text_muted']};
    font-size: 11px;
}}
QPushButton#runPassBtn {{
    background: {p['accent']};
    color: white;
    border: none;
    padding: 6px 14px;
    border-radius: 6px;
    font-weight: 600;
    font-size: 12px;
}}
QPushButton#runPassBtn:hover {{ background: {p['accent_strong']}; }}

/* ---------- sidebar ---------- */
QFrame#sidebar {{
    background: {p['bg']};
    border-right: 1px solid {p['border']};
}}
QPushButton#navItem {{
    background: transparent;
    color: {p['text_dim']};
    border: none;
    text-align: left;
    padding: 8px 16px;
    font-size: 13px;
    border-radius: 6px;
    margin: 2px 8px;
    outline: none;
}}
QPushButton#navItem:hover {{
    background: {p['bg_panel']};
    color: {p['text']};
}}
QPushButton#navItem:checked, QPushButton#navItem:checked:focus {{
    background: {p['bg_panel_2']};
    color: #ffffff;
}}
/* Ledger nav item */
QPushButton#navItemLedger {{
    background: transparent;
    color: {p['text_dim']};
    border: none;
    text-align: left;
    padding: 8px 16px;
    font-size: 13px;
    border-radius: 6px;
    margin: 2px 8px;
    outline: none;
}}
QPushButton#navItemLedger:hover {{
    background: {p['bg_panel']};
    color: {p['text']};
}}
QPushButton#navItemLedger:checked, QPushButton#navItemLedger:checked:focus {{
    background: {p['bg_panel_2']};
    color: {p['ledger']};
}}
QLabel#sidebarFooter {{
    color: {p['text_muted']};
    font-size: 10px;
    padding: 12px 18px;
    border-top: 1px solid {p['border']};
}}

/* ---------- content cards ---------- */
QFrame#card, QFrame#statCard, QFrame#srcCard, QFrame#mcard {{
    background: {p['bg_panel']};
    border: 1px solid {p['border']};
    border-radius: 12px;
}}
QFrame#statCard:hover, QFrame#srcCard:hover, QFrame#mcard:hover {{
    border: 1px solid {p['accent']};
    background: {p['bg_panel']};
}}
QLabel#cardTitle {{
    color: {p['text']};
    font-size: 14px;
    font-weight: 700;
    padding: 14px 18px 6px;
    letter-spacing: -0.01em;
}}
QLabel#cardBody {{
    color: {p['text_dim']};
    padding: 0 18px 14px;
    line-height: 1.4;
}}
QLabel#metricBig {{
    color: {p['accent']};
    font-size: 32px;
    font-weight: 700;
    letter-spacing: -0.02em;
}}
QLabel#metricLabel {{
    color: {p['text_muted']};
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
}}

/* ---------- home page bespoke cards ---------- */
QLabel#statCardLabel {{
    color: {p['text_muted']};
    font-size: 11px;
    letter-spacing: 0.06em;
    text-transform: uppercase;
}}
QLabel#statCardVal {{
    font-size: 32px;
    font-weight: 700;
    letter-spacing: -0.02em;
}}
QLabel#statCardHint {{
    color: {p['text_muted']};
    font-size: 11px;
}}
QLabel#srcCardName {{
    color: {p['text']};
    font-weight: 600;
    font-size: 13px;
}}
QLabel#srcCardDetail {{
    color: {p['text_muted']};
    font-size: 11px;
}}

/* ---------- generic widgets ---------- */
QScrollBar:vertical {{
    background: transparent;
    width: 8px;
    margin: 4px;
}}
QScrollBar::handle:vertical {{
    background: {p['bg_panel_2']};
    min-height: 20px;
    border-radius: 4px;
}}
QScrollBar::handle:vertical:hover {{ background: {p['text_muted']}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}

QPushButton {{
    background: {p['bg_panel_2']};
    color: {p['text']};
    border: 1px solid {p['border']};
    padding: 6px 14px;
    border-radius: 8px;
    font-size: 12px;
    font-weight: 500;
}}
QPushButton:hover {{
    background: {p['bg_select']};
    border-color: {p['text_muted']};
}}
QPushButton:pressed {{
    background: {p['accent']};
    color: white;
    border-color: {p['accent']};
}}

QStatusBar {{
    background: {p['bg_panel']};
    color: {p['text_muted']};
    border-top: 1px solid {p['border']};
}}

QTableWidget {{
    background: {p['bg_panel']};
    color: {p['text']};
    gridline-color: transparent;
    border: none;
    border-radius: 12px;
    padding: 4px;
}}
QHeaderView::section {{
    background: {p['bg_panel']};
    color: {p['text_muted']};
    padding: 8px;
    border: none;
    border-bottom: 1px solid {p['border']};
    font-weight: 600;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
}}
QTableWidget::item {{
    padding: 6px;
}}
QTableWidget::item:selected {{
    background: {p['bg_select']};
    color: {p['text']};
}}

QLineEdit, QComboBox, QTextEdit {{
    background: {p['bg_panel']};
    color: {p['text']};
    border: 1px solid {p['border']};
    border-radius: 8px;
    padding: 6px 12px;
    font-size: 13px;
}}
QLineEdit:focus, QComboBox:focus, QTextEdit:focus {{
    border-color: {p['accent']};
}}

QLineEdit#intField {{
    background: transparent;
    border: none;
    padding: 4px 6px;
}}
QLineEdit#intField:focus {{
    background: {p['bg_panel_2']};
    border: 1px solid {p['border']};
    border-radius: 4px;
}}
"""

QSS = get_stylesheet(True)
