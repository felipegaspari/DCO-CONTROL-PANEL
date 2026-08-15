"""Theme palettes and responsive QSS stylesheet generator for PySide6."""

from __future__ import annotations

# Set your preferred default theme here:
# Options: "monokai", "dark", "cyberpunk", "solarized_light", "github_light", "light"
DEFAULT_THEME = "monokai"

# --- Palettes ---

DARK_PALETTE = {
    "name": "Dark (Studio)",
    "bg": "#181a1f",
    "surface": "#22262e",
    "surface_alt": "#2c313c",
    "border": "#363c48",
    "fg": "#dbe0e8",
    "muted": "#7c8594",
    "accent": "#3b82f6",
    "accent_hover": "#60a5fa",
    "accent_active": "#2563eb",
    "accent_text": "#ffffff",
    "field": "#121418",
    "slider_trough": "#282c34",
    "slider_fill": "#3b82f6",
    "ok": "#10b981",
    "off": "#4b5563",
    "log_bg": "#0f1115",
}

LIGHT_PALETTE = {
    "name": "Light (Clean)",
    "bg": "#f1f5f9",
    "surface": "#ffffff",
    "surface_alt": "#e2e8f0",
    "border": "#cbd5e1",
    "fg": "#0f172a",
    "muted": "#64748b",
    "accent": "#2563eb",
    "accent_hover": "#3b82f6",
    "accent_active": "#1d4ed8",
    "accent_text": "#ffffff",
    "field": "#ffffff",
    "slider_trough": "#cbd5e1",
    "slider_fill": "#2563eb",
    "ok": "#16a34a",
    "off": "#94a3b8",
    "log_bg": "#f8fafc",
}

MONOKAI_PALETTE = {
    "name": "Monokai Pro",
    "bg": "#272822",
    "surface": "#383830",
    "surface_alt": "#49483e",
    "border": "#595744",
    "fg": "#f8f8f2",
    "muted": "#75715e",
    "accent": "#a6e22e",        # Neon Green
    "accent_hover": "#b8f33f",
    "accent_active": "#f92672", # Magenta
    "accent_text": "#272822",
    "field": "#1e1f1c",
    "slider_trough": "#49483e",
    "slider_fill": "#a6e22e",
    "ok": "#a6e22e",
    "off": "#75715e",
    "log_bg": "#1e1f1c",
}

SOLARIZED_LIGHT_PALETTE = {
    "name": "Solarized Light",
    "bg": "#fdf6e3",
    "surface": "#eee8d5",
    "surface_alt": "#e0d7be",
    "border": "#cbbf9e",
    "fg": "#586e75",
    "muted": "#93a1a1",
    "accent": "#268bd2",        # Solarized Blue
    "accent_hover": "#2aa198",  # Solarized Cyan
    "accent_active": "#cb4b16", # Solarized Orange
    "accent_text": "#ffffff",
    "field": "#fffdf8",
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
    "fg": "#24292f",
    "muted": "#57606a",
    "accent": "#0969da",
    "accent_hover": "#218bff",
    "accent_active": "#0550ae",
    "accent_text": "#ffffff",
    "field": "#ffffff",
    "slider_trough": "#d0d7de",
    "slider_fill": "#0969da",
    "ok": "#1a7f37",
    "off": "#8c959f",
    "log_bg": "#f6f8fa",
}

CYBERPUNK_PALETTE = {
    "name": "Cyberpunk Neon",
    "bg": "#14121e",
    "surface": "#1e1930",
    "surface_alt": "#2c2448",
    "border": "#4c3a70",
    "fg": "#e2d9f3",
    "muted": "#8471a5",
    "accent": "#ff007f",        # Hot Pink
    "accent_hover": "#ff409f",
    "accent_active": "#00f0ff", # Neon Cyan
    "accent_text": "#ffffff",
    "field": "#0c0a14",
    "slider_trough": "#2c2448",
    "slider_fill": "#00f0ff",
    "ok": "#00f0ff",
    "off": "#574878",
    "log_bg": "#0c0a14",
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

    # Dynamic sizes calculated relative to base font size
    f_main = base_size
    f_small = max(9, base_size - 1)
    f_heading = base_size + 1
    
    tab_pad_v = max(4, int(base_size * 0.6))
    tab_pad_h = max(8, int(base_size * 1.1))
    btn_pad_v = max(3, int(base_size * 0.4))
    btn_pad_h = max(8, int(base_size * 0.9))
    input_pad_v = max(2, int(base_size * 0.35))
    input_pad_h = max(6, int(base_size * 0.65))

    groove_h = max(4, int(base_size * 0.5))
    handle_w = max(12, int(base_size * 1.15))
    handle_m = -int((handle_w - groove_h) / 2)
    handle_rad = int(handle_w / 2)

    return f"""
    QWidget {{
        background-color: {p["bg"]};
        color: {p["fg"]};
        font-family: "Inter", "Segoe UI", "Noto Sans", sans-serif;
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
        border-radius: 4px;
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
        font-weight: bold;
        border-top: 2px solid {p["accent"]};
    }}
    QTabBar::tab:hover:!selected {{
        background: {p["surface_alt"]};
        color: {p["fg"]};
    }}

    /* Group Boxes */
    QGroupBox {{
        background-color: {p["surface"]};
        border: 1px solid {p["border"]};
        border-radius: 6px;
        margin-top: {int(f_main * 1.4)}px;
        padding-top: {int(f_main * 1.1)}px;
        font-weight: bold;
        font-size: {f_heading}px;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        subcontrol-position: top left;
        padding: 0 6px;
        color: {p["accent"]};
        background-color: transparent;
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
        font-weight: bold;
    }}
    QPushButton#AccentButton:hover {{
        background-color: {p["accent_hover"]};
    }}

    /* Inputs */
    QComboBox, QLineEdit, QSpinBox {{
        background-color: {p["field"]};
        border: 1px solid {p["border"]};
        border-radius: 4px;
        padding: {input_pad_v}px {input_pad_h}px;
        color: {p["fg"]};
        font-size: {f_main}px;
    }}
    QComboBox:focus, QLineEdit:focus, QSpinBox:focus {{
        border: 1px solid {p["accent"]};
    }}
    QComboBox::drop-down {{
        border: none;
        width: 20px;
    }}
    QComboBox QAbstractItemView {{
        background-color: {p["surface"]};
        border: 1px solid {p["border"]};
        selection-background-color: {p["accent"]};
        selection-color: {p["accent_text"]};
    }}

    /* Sliders */
    QSlider::groove:horizontal {{
        height: {groove_h}px;
        background: {p["slider_trough"]};
        border-radius: {int(groove_h / 2)}px;
    }}
    QSlider::sub-page:horizontal {{
        background: {p["slider_fill"]};
        border-radius: {int(groove_h / 2)}px;
    }}
    QSlider::handle:horizontal {{
        background: {p["fg"]};
        border: 2px solid {p["accent"]};
        width: {handle_w}px;
        margin: {handle_m}px 0;
        border-radius: {handle_rad}px;
    }}
    QSlider::handle:horizontal:hover {{
        background: {p["accent_hover"]};
    }}

    QSlider::groove:vertical {{
        width: {groove_h}px;
        background: {p["slider_trough"]};
        border-radius: {int(groove_h / 2)}px;
    }}
    QSlider::sub-page:vertical {{
        background: {p["slider_trough"]};
        border-radius: {int(groove_h / 2)}px;
    }}
    QSlider::add-page:vertical {{
        background: {p["slider_fill"]};
        border-radius: {int(groove_h / 2)}px;
    }}
    QSlider::handle:vertical {{
        background: {p["fg"]};
        border: 2px solid {p["accent"]};
        height: {handle_w}px;
        margin: 0 {handle_m}px;
        border-radius: {handle_rad}px;
    }}

    /* Scrollbars */
    QScrollBar:vertical {{
        background: {p["bg"]};
        width: {max(8, int(base_size * 0.8))}px;
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

    /* Log Output / PlainTextEdit */
    QPlainTextEdit#LogView {{
        background-color: {p["log_bg"]};
        color: {p["fg"]};
        border: 1px solid {p["border"]};
        border-radius: 4px;
        font-family: "JetBrains Mono", "Fira Code", "Noto Sans Mono", monospace;
        font-size: {f_small}px;
    }}

    /* Labels */
    QLabel#ReadoutLabel {{
        color: {p["muted"]};
        font-family: "JetBrains Mono", "Fira Code", "Noto Sans Mono", monospace;
        font-size: {f_small}px;
        font-weight: bold;
    }}
    QLabel#MutedLabel {{
        color: {p["muted"]};
        font-size: {f_small}px;
    }}
    """