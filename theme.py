"""Appearance for the bench controller.

Restyles ttk on top of the built-in `clam` theme, so there is no required dependency. If
[sv-ttk](https://pypi.org/project/sv-ttk/) happens to be installed it is used instead, which
looks more modern and brings real toggle switches; nothing needs configuring for that to
happen.

The important part is `retint()`. ttk styling only reaches ttk widgets, so the app's
`tk.Canvas` scroll containers, its `tk.Text` log pane and the combobox dropdown listbox stay
at their default light grey unless they are recoloured by hand. `retint()` walks the widget
tree and fixes them, reading the colours back out of the live ttk style so it works under
either backend.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

UI_FONT = "Noto Sans"
MONO_FONT = "Noto Sans Mono"

DARK = {
    "bg": "#22272e",       # window and tab body
    "surface": "#2b313b",  # raised: buttons, selected tab
    "field": "#1a1f26",    # sunken: entries, log pane
    "fg": "#d6dae0",
    "muted": "#8b949e",
    "accent": "#4c8eda",
    "accent_text": "#ffffff",
    "trough": "#3a424e",
    "border": "#3d444d",
    "select_bg": "#2f5d8a",
    "ok": "#57ab5a",
    "off": "#6e7681",
}

LIGHT = {
    "bg": "#f2f4f7",
    "surface": "#ffffff",
    "field": "#ffffff",
    "fg": "#1f2328",
    "muted": "#6a737d",
    "accent": "#2563eb",
    "accent_text": "#ffffff",
    "trough": "#dbe0e6",
    "border": "#cfd6dd",
    "select_bg": "#cfe3ff",
    "ok": "#2da44e",
    "off": "#9aa4ae",
}

PALETTES = {"dark": DARK, "light": LIGHT}
MODES = tuple(PALETTES)

_active = DARK
_backend = "clam"


def palette() -> dict:
    """Colours of the currently applied mode.

    For widgets created after `apply()` and therefore missed by `retint()` — the tooltip
    windows, which only exist while the pointer is over a label.
    """
    return _active


def backend() -> str:
    """Either "sv_ttk" or "clam"."""
    return _backend


def check_style() -> str:
    """Style name for boolean checkbuttons.

    sv-ttk draws `Switch.TCheckbutton` as a real toggle switch, which suits an on/off
    parameter far better than a tick box. clam has no equivalent.
    """
    return "Switch.TCheckbutton" if _backend == "sv_ttk" else "TCheckbutton"


def apply(root: tk.Misc, mode: str = "dark", base_size: int = 12) -> dict:
    """Apply `mode` to `root` and everything already inside it. Returns the palette.

    Safe to call again at runtime to switch modes: ttk resolves styles dynamically, so
    existing widgets pick up the change without being rebuilt.
    """
    global _active, _backend
    if mode not in PALETTES:
        mode = "dark"
    _active = PALETTES[mode]
    p = _active

    _apply_fonts(base_size)
    style = ttk.Style(root)

    try:
        import sv_ttk
    except ImportError:
        _backend = "clam"
        style.theme_use("clam")
        _style_clam(style, root, p)
    else:
        _backend = "sv_ttk"
        sv_ttk.set_theme(mode)
        _active = p = _sv_palette(root, mode, p)

    _style_shared(style, p, base_size)
    root.configure(background=_lookup(style, "TFrame", "background", p["bg"]))
    _style_dropdown(root, style, p)
    retint(root)
    return p


def _sv_palette(root: tk.Misc, mode: str, base: dict) -> dict:
    """Adopt sun-valley's own colours for the plain-Tk widgets.

    sun-valley paints its widgets from a spritesheet and sets no `-background` on any style;
    it only pushes base colours onto `.` from a deferred `<<ThemeChanged>>` handler, so
    `style.lookup()` is still empty at this point. Its Tcl namespace holds the real values.
    """
    ns = "sv_dark" if mode == "dark" else "sv_light"

    def col(key: str, fallback: str) -> str:
        try:
            return root.tk.eval(f"set ttk::theme::{ns}::colors({key})") or fallback
        except tk.TclError:
            return fallback

    p = dict(base)
    bg = col("-bg", base["bg"])
    p.update(
        bg=bg,
        surface=bg,
        field=bg,  # sun-valley configures fieldbackground to the window background
        fg=col("-fg", base["fg"]),
        accent=col("-accent", base["accent"]),
        accent_text=col("-selfg", base["accent_text"]),
        select_bg=col("-selbg", base["select_bg"]),
        trough=bg,
        border=col("-disfg", base["border"]),
    )
    # `muted` deliberately keeps this module's own value: sun-valley's -disfg is too dim
    # against its background for the slider readouts and the notes to stay readable.
    return p


def _apply_fonts(base_size: int) -> None:
    families = set(tkfont.families())
    ui = UI_FONT if UI_FONT in families else "TkDefaultFont"
    mono = MONO_FONT if MONO_FONT in families else "DejaVu Sans Mono"
    for name, family, size in (
        ("TkDefaultFont", ui, base_size),
        ("TkTextFont", ui, base_size),
        ("TkHeadingFont", ui, base_size),
        ("TkMenuFont", ui, base_size),
        ("TkFixedFont", mono, base_size - 1),
    ):
        try:
            tkfont.nametofont(name).configure(family=family, size=size)
        except tk.TclError:
            pass


def _style_clam(style: ttk.Style, root: tk.Misc, p: dict) -> None:
    """Recolour clam. sv-ttk needs none of this; it ships its own complete theme."""
    style.configure(
        ".",
        background=p["bg"], foreground=p["fg"], fieldbackground=p["field"],
        bordercolor=p["border"], lightcolor=p["surface"], darkcolor=p["surface"],
        troughcolor=p["trough"], focuscolor=p["accent"], borderwidth=1,
        insertcolor=p["fg"],
    )
    style.map(".", foreground=[("disabled", p["muted"])])

    style.configure("TFrame", background=p["bg"])
    style.configure("TLabel", background=p["bg"], foreground=p["fg"])

    style.configure("TButton", background=p["surface"], foreground=p["fg"],
                    padding=(12, 6), relief="flat", borderwidth=1, anchor="center")
    style.map(
        "TButton",
        background=[("pressed", p["select_bg"]), ("active", p["trough"])],
        bordercolor=[("active", p["accent"]), ("focus", p["accent"])],
    )

    style.configure("TNotebook", background=p["bg"], borderwidth=0, tabmargins=(4, 6, 4, 0))
    style.configure("TNotebook.Tab", background=p["bg"], foreground=p["muted"],
                    padding=(16, 8), borderwidth=0)
    style.map(
        "TNotebook.Tab",
        background=[("selected", p["surface"]), ("active", p["trough"])],
        foreground=[("selected", p["fg"]), ("active", p["fg"])],
    )

    style.configure("TLabelframe", background=p["bg"], bordercolor=p["border"],
                    borderwidth=1, relief="solid")
    style.configure("TLabelframe.Label", background=p["bg"], foreground=p["accent"])

    # clam names these indicatorbackground / indicatorforeground, not indicatorcolor.
    # indicatorsize matches Horizontal.TScale arrowsize so ticks and faders share a height.
    style.configure("TCheckbutton", background=p["bg"], foreground=p["fg"],
                    indicatorbackground=p["field"], indicatorforeground=p["accent_text"],
                    indicatorsize=30, indicatormargin=(1, 1, 6, 1), padding=4,
                    focusthickness=0)
    style.map(
        "TCheckbutton",
        background=[("active", p["bg"])],
        indicatorbackground=[("selected", p["accent"]), ("active", p["trough"]),
                             ("disabled", p["bg"])],
    )

    for cls in ("TCombobox", "TSpinbox"):
        style.configure(cls, fieldbackground=p["field"], background=p["surface"],
                        foreground=p["fg"], arrowcolor=p["muted"], borderwidth=1,
                        padding=(8, 5), insertcolor=p["fg"])
        style.map(
            cls,
            fieldbackground=[("readonly", p["field"]), ("disabled", p["bg"])],
            bordercolor=[("focus", p["accent"]), ("active", p["accent"])],
            arrowcolor=[("active", p["accent"])],
            foreground=[("readonly", p["fg"])],
        )
    # Stop the readonly combobox showing a selection highlight over its own text.
    style.map("TCombobox",
              selectbackground=[("readonly", p["field"])],
              selectforeground=[("readonly", p["fg"])])

    # In clam the handle is drawn from -background; -gripcount 0 drops its grip lines.
    # Scale trough thickness is arrowsize here (sliderthickness is ignored on this Tk);
    # default is ~15, so 30 doubles the bar. sliderlength is the thumb along the track.
    style.configure("TScale", arrowsize=30, sliderlength=48)
    style.configure("Horizontal.TScale", background=p["accent"], troughcolor=p["trough"],
                    bordercolor=p["border"], lightcolor=p["accent"], darkcolor=p["accent"],
                    gripcount=0, arrowsize=30, sliderlength=48, troughrelief="flat")
    style.map(
        "Horizontal.TScale",
        background=[("active", p["select_bg"]), ("disabled", p["trough"])],
        lightcolor=[("active", p["select_bg"])],
        darkcolor=[("active", p["select_bg"])],
    )
    style.configure("Vertical.TScale", background=p["accent"], troughcolor=p["trough"],
                    bordercolor=p["border"], lightcolor=p["accent"], darkcolor=p["accent"],
                    gripcount=0, arrowsize=30, sliderlength=48, troughrelief="flat")
    style.map(
        "Vertical.TScale",
        background=[("active", p["select_bg"]), ("disabled", p["trough"])],
        lightcolor=[("active", p["select_bg"])],
        darkcolor=[("active", p["select_bg"])],
    )

    # Default clam scrollbar is width=12 / arrowsize=12; double both so the trough
    # (not only the arrows) is easier to grab.
    style.configure("TScrollbar", width=24, arrowsize=24)
    style.configure("Vertical.TScrollbar", background=p["surface"], troughcolor=p["bg"],
                    bordercolor=p["bg"], arrowcolor=p["muted"], borderwidth=0,
                    width=24, arrowsize=24)
    style.map("Vertical.TScrollbar", background=[("active", p["accent"])])
    style.configure("Horizontal.TScrollbar", background=p["surface"], troughcolor=p["bg"],
                    bordercolor=p["bg"], arrowcolor=p["muted"], borderwidth=0,
                    width=24, arrowsize=24)
    style.map("Horizontal.TScrollbar", background=[("active", p["accent"])])
    style.configure("TSeparator", background=p["border"])


def _style_shared(style: ttk.Style, p: dict, base_size: int) -> None:
    """Named styles the app asks for, defined for whichever backend is live."""
    bg = _lookup(style, "TFrame", "background", p["bg"])

    style.configure("Muted.TLabel", background=bg, foreground=p["muted"])
    style.configure("Heading.TLabel", background=bg, foreground=p["fg"],
                    font=(UI_FONT, base_size, "bold"))
    # Monospaced so digits keep a constant width and the value stops jittering mid-drag.
    style.configure("Readout.TLabel", background=bg, foreground=p["muted"],
                    font=(MONO_FONT, base_size - 1))
    style.configure("Status.TLabel", background=bg, foreground=p["muted"])
    style.configure("Dot.TLabel", background=bg, foreground=p["off"],
                    font=(UI_FONT, base_size + 2))
    style.configure("DotOn.TLabel", background=bg, foreground=p["ok"],
                    font=(UI_FONT, base_size + 2))

    # sv-ttk already provides Accent.TButton; only define it on clam.
    if _backend != "sv_ttk":
        style.configure("Accent.TButton", background=p["accent"], foreground=p["accent_text"])
        style.map("Accent.TButton",
                  background=[("active", p["select_bg"]), ("pressed", p["select_bg"])],
                  foreground=[("active", p["accent_text"])])


def _style_dropdown(root: tk.Misc, style: ttk.Style, p: dict) -> None:
    """A combobox's popup is a Tk listbox, reachable only through the option database."""
    field = _lookup(style, "TEntry", "fieldbackground", p["field"])
    fg = _lookup(style, "TLabel", "foreground", p["fg"])
    for option, value in (
        ("*TCombobox*Listbox.background", field),
        ("*TCombobox*Listbox.foreground", fg),
        ("*TCombobox*Listbox.selectBackground", p["accent"]),
        ("*TCombobox*Listbox.selectForeground", p["accent_text"]),
        ("*TCombobox*Listbox.borderWidth", "0"),
        ("*TCombobox*Listbox.highlightThickness", "0"),
    ):
        root.option_add(option, value)


def retint(widget: tk.Misc) -> None:
    """Recolour the plain-Tk widgets that ttk styling cannot reach.

    Call after building the UI. `apply()` already does this for whatever exists at the time,
    but widgets created later (each tab's canvas, the log pane) need a second pass.
    """
    style = ttk.Style(widget)
    p = _active
    bg = _lookup(style, "TFrame", "background", p["bg"])
    field = _lookup(style, "TEntry", "fieldbackground", p["field"])
    fg = _lookup(style, "TLabel", "foreground", p["fg"])
    _retint(widget, bg, field, fg, p)


def _retint(widget: tk.Misc, bg: str, field: str, fg: str, p: dict) -> None:
    for child in widget.winfo_children():
        try:
            if isinstance(child, tk.Canvas):
                child.configure(background=bg, highlightthickness=0)
            elif isinstance(child, tk.Text):
                child.configure(
                    background=field, foreground=fg, insertbackground=p["accent"],
                    selectbackground=p["select_bg"], selectforeground=fg,
                    borderwidth=0, highlightthickness=0, padx=8, pady=6,
                    font=(MONO_FONT, 9),
                )
            elif isinstance(child, tk.Listbox):
                child.configure(background=field, foreground=fg, borderwidth=0,
                                highlightthickness=0, selectbackground=p["accent"],
                                selectforeground=p["accent_text"])
            elif isinstance(child, (tk.Toplevel, tk.Frame, tk.LabelFrame)):
                child.configure(background=bg)
        except tk.TclError:
            pass
        _retint(child, bg, field, fg, p)


def _lookup(style: ttk.Style, element: str, option: str, fallback: str) -> str:
    try:
        return str(style.lookup(element, option)) or fallback
    except tk.TclError:
        return fallback
