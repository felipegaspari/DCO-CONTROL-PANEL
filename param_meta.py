"""Bridge and parser for DCO-PROTOCOL/param_meta.h.

Provides dynamic lookup for display names, curve choices, waveform choices,
voice modes, and bipolar/octave display value scaling.
"""

from __future__ import annotations

import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROTOCOL_DIR = HERE.parent / "DCO-PROTOCOL"
PARAM_META_HEADER = PROTOCOL_DIR / "param_meta.h"

# Fallback definitions matching param_meta.h directly
CURVE_PROFILES: tuple[tuple[str, int], ...] = (
    ("0 - EXP", 0),
    ("1 - SOFT", 1),
    ("2 - STEEP", 2),
    ("3 - CONCAVE", 3),
    ("4 - FAST S", 4),
    ("5 - SLOW THEN LIN", 5),
    ("6 - ALMOST LIN", 6),
    ("7 - LINEAR", 7),
)

VOICE_MODES: tuple[tuple[str, int], ...] = (
    ("0 - MONO", 0),
    ("1 - POLY", 1),
    ("2 - UNISON", 2),
)

VOICE_ALLOC_MODES: tuple[tuple[str, int], ...] = (
    ("0 - ROUND ROBIN", 0),
    ("1 - OLDEST", 1),
    ("2 - QUIETEST", 2),
    ("3 - QUIETEST LOW", 3),
    ("4 - QUIETEST HIGH", 4),
    ("5 - NO STEAL", 5),
)

LFO_WAVEFORMS: tuple[tuple[str, int], ...] = (
    ("0 - Off", 0),
    ("1 - Saw", 1),
    ("2 - Tri Slewed", 2),
    ("3 - Sine", 3),
    ("4 - Square", 4),
    ("5 - Sharktooth", 5),
    ("6 - Trapezoid", 6),
    ("7 - Linear Tri", 7),
    ("8 - Staircase", 8),
    ("9 - Folded Sine", 9),
)


def scale_display_value(pid: int, val: int) -> int:
    """Mirror of param_scale_display_value() in param_meta.h."""
    if pid == 13:  # PARAM_OSC1_INTERVAL
        return (val - 36) // 12
    if pid in (14, 34):  # PARAM_OSC2_INTERVAL, PARAM_OSC3_INTERVAL
        return val - 36
    if pid in (15, 35):  # PARAM_OSC2_DETUNE_VAL, PARAM_OSC3_DETUNE_VAL
        return val - 256
    if pid == 46:  # PARAM_ADSR3_TO_PWM
        return val - 512
    return val


def format_display_value(pid: int, val: int) -> str:
    """Format raw value into user-facing display string with sign/unit."""
    scaled = scale_display_value(pid, val)
    if pid == 13:
        return f"{scaled:+d} oct"
    if pid in (14, 34):
        return f"{scaled:+d} st"
    if pid in (15, 35):
        return f"{scaled:+d}"
    if pid == 46:
        return f"{scaled:+d}"
    return str(scaled)