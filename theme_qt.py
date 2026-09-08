"""Theme palettes and responsive QSS stylesheet generator for PySide6."""

from __future__ import annotations

# Default theme: "monokai", "dark", "cyberpunk", "solarized_light", "github_light", "light"
DEFAULT_THEME = "monokai"

# --- Palettes ---

DARK_PALETTE = {
    "name": "Dark (Studio)",
    "bg": "#16181d",
    "surface": "#20242c",
    "surface_alt": "#282e38",
    "border": "#353b47",
    "border_hover": "#4a5364",
    "fg": "#e6ebf2",
    "label": "#b8c1cf",         # Crisp medium-contrast parameter label
    "muted": "#758092",
    "accent": "#3b82f6",
    "accent_hover": "#60a5fa",
    "accent_active": "#2563eb",
    "accent_text": "#ffffff",
    "field": "#111317",
    "slider_trough": "#262b34",
    "slider_fill": "#3b82f6",
    "ok": "#10b981",
    "off": "#475161",
    "log_bg": "#0d0f12",
}

LIGHT_PALETTE = {
    "name": "Light (Clean)",
    "bg": "#f1f5f9",
    "surface": "#ffffff",
    "surface_alt": "#e2e8f0",
    "border": "#cbd5e1",
    "border_hover": "#94a3b8",
    "fg": "#0f172a",
    "label": "#334155",
    "muted": "#64748b",
    "accent": "#2563eb",
    "accent_hover": "#3b82f6",
    "accent_active": "#1d4ed8",
    "accent_text": "#ffffff",
    "field": "#f8fafc",
    "slider_trough": "#cbd5e1",
    "slider_fill": "#2563eb",
    "ok": "#16a34a",
    "off": "#94a3b8",
    "log_bg": "#ffffff",
}

MONOKAI_PALETTE = {
    "name": "Monokai Pro",
    "bg": "#22231e",
    "surface": "#32332c",
    "surface_alt": "#42433a",
    "border": "#545548",
    "border_hover": "#75715e",
    "fg": "#f8f8f2",
    "label": "#dcdcd4",
    "muted": "#75715e",
    "accent": "#a6e22e",        # Neon Green
    "accent_hover": "#b8f33f",
    "accent_active": "#f92672", # Magenta
    "accent_text": "#22231e",
    "field": "#181915",
    "slider_trough": "#42433a",
    "slider_fill": "#a6e22e",
    "ok": "#a6e22e",
    "off": "#605d4e",
    "log_bg": "#181915",
}

SOLARIZED_LIGHT_PALETTE = {
    "name": "Solarized Light",
    "bg": "#fdf6e3",
    "surface": "#eee8d5",
    "surface_alt": "#e0d7be",
    "border": "#cbbf9e",
    "border_hover": "#93a1a1",
    "fg": "#586e75",
    "label": "#40545b",
    "muted": "#93a1a1",
    "accent": "#268bd2",        # Solarized Blue
    "accent_hover": "#2aa198",  # Solarized Cyan
    "accent_active": "#cb4b16", # Solarized Orange
    "accent_text": "#ffffff",
    "field": "#fbf3de",
    "slider_trough": "#dcd4bb",
    "slider_fill": "#268bd2",
    "ok": "#859900",            # Solarized Green
    "off": "#93a1a1",
    "log_bg": "#eee8d5",
}

GITHUB_LIGHT_PALETTE = {
    "name": "GitHub Light",
    "bg": "#ffffff",
    "surface": "#f6f8fa",
    "surface_alt": "#eaeef2",
    "border": "#d0d7de",
    "border_hover": "#8c959f",
    "fg": "#24292f",
    "label": "#363c44",
    "muted": "#57606a",
    "accent": "#0969da",
    "accent_hover": "#218bff",
    "accent_active": "#0550ae",
    "accent_text": "#ffffff",
    "field": "#f6f8fa",
    "slider_trough": "#d0d7de",
    "slider_fill": "#0969da",
    "ok": "#1a7f37",
    "off": "#8c959f",
    "log_bg": "#f6f8fa",
}

CYBERPUNK_PALETTE = {
    "name": "Cyberpunk Neon",
    "bg": "#12101b",
    "surface": "#1b172b",
    "surface_alt": "#27213d",
    "border": "#463666",
    "border_hover": "#6c539e",
    "fg": "#e5ddf5",
    "label": "#cbbee5",
    "muted": "#7f6a9f",
    "accent": "#ff007f",        # Hot Pink
    "accent_hover": "#ff409f",
    "accent_active": "#00f0ff", # Neon Cyan
    "accent_text": "#ffffff",
    "field": "#0b0912",
    "slider_trough": "#27213d",
    "slider_fill": "#00f0ff",
    "ok": "#00f0ff",
    "off": "#50416f",
    "log_bg": "#0b0912",
}

PALETTES = {
    "monokai": MONOKAI_PALETTE,
    "dark": DARK_PALETTE,
    "cyberpunk": CYBERPUNK_PALETTE,
    "solarized_light": SOLARIZED_LIGHT_PALETTE,
    "github_light": GITHUB_LIGHT_PALETTE,
    "light": LIGHT_PALETTE,
}


def build_stylesheet(mode: str = DEFAULT_THEME, base_size: int = 12) -> str:
    p = PALETTES.get(mode, PALETTES[DEFAULT_THEME])

    # Dynamic sizes relative to base font size
    f_main = base_size
    f_small = max(9, base_size - 1)
    f_heading = base_size + 1

    tab_pad_v = max(4, int(base_size * 0.55))
    tab_pad_h = max(8, int(base_size * 1.0))
    btn_pad_v = max(3, int(base_size * 0.4))
    btn_pad_h = max(8, int(base_size * 0.85))
    input_pad_v = max(3, int(base_size * 0.35))
    input_pad_h = max(8, int(base_size * 0.75))

    groove_h = max(4, int(base_size * 0.45))
    handle_w = max(12, int(base_size * 1.1))
    handle_m = -int((handle_w - groove_h) / 2)
    handle_rad = int(handle_w / 2)

    # URL-encoded hex colors for SVG chevron vector
    p_muted_enc = p["muted"].replace("#", "%23")
    p_accent_enc = p["accent"].replace("#", "%23")
    p_label = p.get("label", p["fg"])
    p_border_hover = p.get("border_hover", p["accent"])

    return f"""
    /* Global Application Typography */
    QWidget {{
        background-color: {p["bg"]};
        color: {p["fg"]};
        font-family: "Inter", "Segoe UI Variable Text", "SF Pro Text", -apple-system, "Segoe UI", "Noto Sans", sans-serif;
        font-size: {f_main}px;
        selection-background-color: {p["accent"]};
        selection-color: {p["accent_text"]};
    }}

    QMainWindow, QDialog {{
        background-color: {p["bg"]};
    }}

    /* Toolbars */
    QToolBar {{
        background-color: {p["surface"]};
        border-bottom: 1px solid {p["border"]};
        padding: 4px;
        spacing: 6px;
    }}

    /* Tab Widget */
    QTabWidget::pane {{
        border: 1px solid {p["border"]};
        background: {p["surface"]};
        border-radius: 5px;
    }}
    QTabBar::tab {{
        background: {p["bg"]};
        color: {p["muted"]};
        padding: {tab_pad_v}px {tab_pad_h}px;
        margin-right: 2px;
        border-top-left-radius: 4px;
        border-top-right-radius: 4px;
        font-weight: 500;
        font-size: {f_main}px;
    }}
    QTabBar::tab:selected {{
        background: {p["surface"]};
        color: {p["accent"]};
        font-weight: 600;
        border-top: 2px solid {p["accent"]};
    }}
    QTabBar::tab:hover:!selected {{
        background: {p["surface_alt"]};
        color: {p["fg"]};
    }}

    /* Hardware Faceplate Group Boxes */
    QGroupBox {{
        background-color: {p["surface"]};
        border: 1px solid {p["border"]};
        border-radius: 6px;
        margin-top: {int(f_main * 1.35)}px;
        padding-top: {int(f_main * 0.9)}px;
        font-weight: 600;
        font-size: {f_heading}px;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        subcontrol-position: top left;
        padding: 0 6px;
        color: {p["accent"]};
        background-color: transparent;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.8px;
    }}

    /* Push Buttons */
    QPushButton {{
        background-color: {p["surface_alt"]};
        border: 1px solid {p["border"]};
        border-radius: 4px;
        color: {p["fg"]};
        padding: {btn_pad_v}px {btn_pad_h}px;
        font-weight: 500;
        font-size: {f_main}px;
    }}
    QPushButton:hover {{
        background-color: {p["border"]};
        border-color: {p["accent"]};
    }}
    QPushButton:pressed {{
        background-color: {p["accent_active"]};
        color: {p["accent_text"]};
    }}
    QPushButton:disabled {{
        background-color: {p["surface"]};
        color: {p["muted"]};
        border-color: {p["border"]};
    }}
    QPushButton#AccentButton {{
        background-color: {p["accent"]};
        color: {p["accent_text"]};
        border: none;
        font-weight: 600;
    }}
    QPushButton#AccentButton:hover {{
        background-color: {p["accent_hover"]};
    }}

    /* Modern Audio-Plugin Style Dropdowns (QComboBox) */
    QComboBox {{
        background-color: {p["field"]};
        border: 1px solid {p["border"]};
        border-radius: 5px;
        padding: {input_pad_v}px 26px {input_pad_v}px {input_pad_h}px;
        color: {p["fg"]};
        font-size: {f_main}px;
        font-weight: 500;
        min-height: {int(f_main * 1.55)}px;
    }}
    QComboBox:hover {{
        background-color: {p["surface_alt"]};
        border-color: {p_border_hover};
    }}
    QComboBox:focus, QComboBox:on {{
        border: 1px solid {p["accent"]};
        background-color: {p["field"]};
    }}
    QComboBox:disabled {{
        background-color: {p["bg"]};
        border-color: {p["border"]};
        color: {p["muted"]};
    }}

    /* Dropdown Chevron Arrow (Vector SVG) */
    QComboBox::drop-down {{
        subcontrol-origin: padding;
        subcontrol-position: center right;
        width: 22px;
        border: none;
        background: transparent;
    }}
    QComboBox::down-arrow {{
        image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='10' height='6' viewBox='0 0 10 6'><path d='M1 1L5 5L9 1' stroke='{p_muted_enc}' stroke-width='1.6' stroke-linecap='round' stroke-linejoin='round' fill='none'/></svg>");
        width: 10px;
        height: 6px;
        margin-right: 6px;
    }}
    QComboBox::down-arrow:hover, QComboBox::down-arrow:on {{
        image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='10' height='6' viewBox='0 0 10 6'><path d='M1 1L5 5L9 1' stroke='{p_accent_enc}' stroke-width='1.6' stroke-linecap='round' stroke-linejoin='round' fill='none'/></svg>");
    }}

    /* Floating Menu Popup (QAbstractItemView) */
    QComboBox QAbstractItemView {{
        background-color: {p["surface"]};
        border: 1px solid {p["border"]};
        border-radius: 6px;
        padding: 4px;
        outline: 0;
        selection-background-color: {p["accent"]};
        selection-color: {p["accent_text"]};
    }}
    QComboBox QAbstractItemView::item {{
        min-height: {int(f_main * 1.75)}px;
        padding: 4px 8px;
        border-radius: 4px;
        color: {p["fg"]};
        font-size: {f_main}px;
    }}
    QComboBox QAbstractItemView::item:hover {{
        background-color: {p["surface_alt"]};
        color: {p["fg"]};
    }}
    QComboBox QAbstractItemView::item:selected {{
        background-color: {p["accent"]};
        color: {p["accent_text"]};
        font-weight: 600;
    }}

    /* LineEdit and SpinBox */
    QLineEdit, QSpinBox {{
        background-color: {p["field"]};
        border: 1px solid {p["border"]};
        border-radius: 4px;
        padding: {input_pad_v}px {input_pad_h}px;
        color: {p["fg"]};
        font-size: {f_main}px;
        font-weight: 500;
    }}
    QLineEdit:focus, QSpinBox:focus {{
        border: 1px solid {p["accent"]};
    }}

/* ==================================================================== */
    /* HORIZONTAL SLIDERS (Synth Parameters)                               */
    /* ==================================================================== */

    /* 1. Track / Groove (Thicker by 50%: 6px instead of 4px) */
    QSlider::groove:horizontal {
        border: 1px solid {p['border']};
        height: 6px;
        background: {p['field']};
        border-radius: 3px;
    }

    QSlider::sub-page:horizontal {
        background: {p['panel']};
        border: 1px solid {p['border']};
        border-radius: 3px;
    }

    /* 2. Fader Handle / Cap (50% wider: 22px instead of 14px, squared with center line) */
    QSlider::handle:horizontal {
        /* Center line drawn via a sharp linear gradient */
        background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
            stop: 0.00 {p['panel']},
            stop: 0.43 {p['panel']},
            stop: 0.45 {p['accent']},
            stop: 0.55 {p['accent']},
            stop: 0.57 {p['panel']},
            stop: 1.00 {p['panel']}
        );
        border: 1px solid {p['border']};
        border-radius: 2px;              /* Squared edges like a mixer fader */
        width: 22px;                      /* 50% wider than standard 14px */
        margin-top: -8px;                 /* Centers cap over 6px groove (height becomes 22px) */
        margin-bottom: -8px;
    }

    QSlider::handle:horizontal:hover {
        border: 1px solid {p['accent']};
        background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
            stop: 0.00 {p['field']},
            stop: 0.42 {p['field']},
            stop: 0.44 {p['accent']},
            stop: 0.56 {p['accent']},
            stop: 0.58 {p['field']},
            stop: 1.00 {p['field']}
        );
    }

    QSlider::handle:horizontal:pressed {
        border: 1px solid {p['accent_active']};
        background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
            stop: 0.00 {p['bg']},
            stop: 0.42 {p['bg']},
            stop: 0.44 {p['accent_active']},
            stop: 0.56 {p['accent_active']},
            stop: 0.58 {p['bg']},
            stop: 1.00 {p['bg']}
        );
    }

    /* ==================================================================== */
    /* VERTICAL SLIDERS (ADSR Envelopes)                                   */
    /* ==================================================================== */

    QSlider::groove:vertical {
        border: 1px solid {p['border']};
        width: 6px;
        background: {p['field']};
        border-radius: 3px;
    }

    QSlider::handle:vertical {
        /* Horizontal center line on vertical faders */
        background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
            stop: 0.00 {p['panel']},
            stop: 0.43 {p['panel']},
            stop: 0.45 {p['accent']},
            stop: 0.55 {p['accent']},
            stop: 0.57 {p['panel']},
            stop: 1.00 {p['panel']}
        );
        border: 1px solid {p['border']};
        border-radius: 2px;
        height: 18px;
        width: 32px;                      /* Wide mixer fader cap */
        margin-left: -13px;               /* Centers the 32px cap over 6px groove */
        margin-right: -13px;
    }

    QSlider::handle:vertical:hover {
        border: 1px solid {p['accent']};
    }

    QSlider::handle:vertical:pressed {
        border: 1px solid {p['accent_active']};
    }

    /* Scrollbars */
    QScrollBar:vertical {{
        background: {p["bg"]};
        width: {max(8, int(base_size * 0.75))}px;
        margin: 0;
    }}
    QScrollBar::handle:vertical {{
        background: {p["border"]};
        min-height: 20px;
        border-radius: 4px;
    }}
    QScrollBar::handle:vertical:hover {{
        background: {p["muted"]};
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0;
    }}

    /* Monospaced Log Output */
    QPlainTextEdit#LogView {{
        background-color: {p["log_bg"]};
        color: {p["fg"]};
        border: 1px solid {p["border"]};
        border-radius: 4px;
        font-family: "JetBrains Mono", "SF Mono", "Fira Code", "Noto Sans Mono", monospace;
        font-size: {f_small}px;
    }}

    /* Parameter Labels Hierarchy */
    QLabel {{
        color: {p_label};
        font-weight: 500;
        letter-spacing: 0.25px;
    }}

    /* Digital OLED/LCD Style Readout Pill Badges */
    QLabel#ReadoutLabel {{
        color: {p["fg"]};
        background-color: {p["field"]};
        border: 1px solid {p["border"]};
        border-radius: 3px;
        padding: 1px 4px;
        font-family: "JetBrains Mono", "SF Mono", "Fira Code", "Cascadia Code", "Consolas", monospace;
        font-size: {f_small}px;
        font-weight: 600;
    }}

    /* Subtle Notes / Secondary Headers */
    QLabel#MutedLabel {{
        color: {p["muted"]};
        font-size: {f_small}px;
        font-weight: 400;
    }}
    """
