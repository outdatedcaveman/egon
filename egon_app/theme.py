"""Qt stylesheet — Streamlit-aesthetic design tokens (light + dark)."""
from __future__ import annotations

PALETTES = {
    "light": {
        "bg":          "#ffffff",   # main canvas
        "bg_panel":    "#f8f9fb",   # sidebar / cards / header
        "bg_panel_2":  "#f0f2f6",   # hover
        "bg_select":   "#e1e4e8",   # selected row
        "border":      "#e1e4e8",
        "text":        "#262730",   # primary text
        "text_dim":    "#374151",   # secondary
        "text_muted":  "#6b7280",
        "accent":      "#ff4b4b",   # streamlit red
        "accent_strong":"#b3261e",
        "ledger":      "#f59e0b",   # amber
        "danger":      "#dc2626",
        "ok":          "#16a34a",
    },
    "dark": {
        "bg":          "#0e1117",   # main canvas
        "bg_panel":    "#161a23",   # sidebar / cards / header
        "bg_panel_2":  "#1c2230",   # hover
        "bg_select":   "#1a1f29",   # selected row
        "border":      "#1f242e",
        "text":        "#e6e7ea",   # primary text
        "text_dim":    "#cfd2da",   # secondary
        "text_muted":  "#9aa0ac",
        "accent":      "#ef4444",   # streamlit red
        "accent_strong":"#fca5a5",
        "ledger":      "#f59e0b",   # amber
        "danger":      "#f87171",
        "ok":          "#4ade80",
    }
}


def get_stylesheet(dark: bool = True) -> str:
    p = PALETTES["dark" if dark else "light"]
    return f"""
QMainWindow, QWidget#central {{
    background: {p['bg']};
    color: {p['text']};
    font-family: "Source Sans Pro", "Segoe UI", "Inter", sans-serif;
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
    font-size: 15px;
    font-weight: 600;
    padding-left: 16px;
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
    border-radius: 4px;
    font-weight: 600;
}}
QPushButton#runPassBtn:hover {{ background: {p['accent_strong']}; }}

/* ---------- sidebar ---------- */
QFrame#sidebar {{
    background: {p['bg_panel']};
    border-right: 1px solid {p['border']};
}}
QPushButton#navItem {{
    background: transparent;
    color: {p['text_dim']};
    border: none;
    text-align: left;
    padding: 8px 18px;
    font-size: 13px;
    border-left: 3px solid transparent;
}}
QPushButton#navItem:hover {{
    background: {p['bg_panel_2']};
}}
QPushButton#navItem:checked {{
    background: {p['bg_panel_2']};
    border-left: 3px solid {p['accent']};
    font-weight: 600;
    color: {p['text']};
}}
/* Ledger nav item */
QPushButton#navItemLedger {{
    background: transparent;
    color: {p['text_dim']};
    border: none;
    text-align: left;
    padding: 8px 18px;
    font-size: 13px;
    border-left: 3px solid transparent;
}}
QPushButton#navItemLedger:hover {{
    background: {p['bg_panel_2']};
}}
QPushButton#navItemLedger:checked {{
    background: {p['bg_panel_2']};
    border-left: 3px solid {p['ledger']};
    font-weight: 600;
    color: {p['ledger']};
}}
QLabel#sidebarFooter {{
    color: {p['text_muted']};
    font-size: 10px;
    padding: 10px 18px;
    border-top: 1px solid {p['border']};
}}

/* ---------- content cards ---------- */
QFrame#card, QFrame#statCard, QFrame#srcCard, QFrame#mcard {{
    background: {p['bg_panel']};
    border: 1px solid {p['border']};
    border-radius: 8px;
}}
QFrame#statCard:hover, QFrame#srcCard:hover, QFrame#mcard:hover {{
    border: 1px solid {p['accent']};
    background: {p['bg_panel_2']};
}}
QLabel#cardTitle {{
    color: {p['text']};
    font-size: 14px;
    font-weight: 600;
    padding: 12px 16px 6px;
}}
QLabel#cardBody {{
    color: {p['text_dim']};
    padding: 0 16px 12px;
}}
QLabel#metricBig {{
    color: {p['accent']};
    font-size: 28px;
    font-weight: 700;
}}
QLabel#metricLabel {{
    color: {p['text_muted']};
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
}}

/* ---------- home page bespoke cards ---------- */
QLabel#statCardLabel {{
    color: {p['text_muted']};
    font-size: 11px;
    letter-spacing: 0.06em;
}}
QLabel#statCardVal {{
    font-size: 30px;
    font-weight: 700;
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
    width: 10px;
}}
QScrollBar::handle:vertical {{
    background: {p['border']};
    min-height: 30px;
    border-radius: 5px;
}}
QScrollBar::handle:vertical:hover {{ background: {p['accent']}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}

QPushButton {{
    background: {p['bg_panel_2']};
    color: {p['text_dim']};
    border: 1px solid {p['border']};
    padding: 6px 12px;
    border-radius: 4px;
}}
QPushButton:hover {{ background: {p['bg_select']}; color: {p['text']}; }}
QPushButton:pressed {{ background: {p['accent']}; color: white; }}

QStatusBar {{
    background: {p['bg_panel']};
    color: {p['text_muted']};
    border-top: 1px solid {p['border']};
}}

QTableWidget {{
    background: {p['bg_panel']};
    color: {p['text']};
    gridline-color: {p['border']};
    border: 1px solid {p['border']};
    border-radius: 6px;
}}
QHeaderView::section {{
    background: {p['bg_panel_2']};
    color: {p['text_muted']};
    padding: 6px;
    border: none;
    border-bottom: 1px solid {p['border']};
    font-weight: 600;
}}
QTableWidget::item:selected {{
    background: {p['bg_select']};
    color: {p['text']};
}}

QLineEdit, QComboBox {{
    background: {p['bg_panel']};
    color: {p['text']};
    border: 1px solid {p['border']};
    border-radius: 4px;
    padding: 4px 8px;
}}
QLineEdit:focus, QComboBox:focus {{
    border-color: {p['accent']};
}}
"""


QSS = get_stylesheet(True)
