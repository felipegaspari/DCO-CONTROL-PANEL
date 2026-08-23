"""Bridge and parser for DCO-PROTOCOL/param_meta.h and params_def.h.
Provides dynamic lookup for display names, curve choices, waveform choices,
voice modes, and bipolar/octave display value scaling.
"""
from __future__ import annotations

from pathlib import Path
import re

HERE = Path(__file__).resolve().parent
PROTOCOL_DIR = HERE.parent / "DCO-PROTOCOL"
PARAMS_DEF_HEADER = PROTOCOL_DIR / "params_def.h"


def _read_params_def_text() -> str:
    if PARAMS_DEF_HEADER.exists():
        return PARAMS_DEF_HEADER.read_text(encoding="utf-8")
    dco_header = HERE.parent / "DCO" / "params_def.h"
    if dco_header.exists():
        return dco_header.read_text(encoding="utf-8")
    return ""


def load_mod_sources() -> tuple[tuple[str, int], ...]:
    """Parse enum ModSource from params_def.h and strip 'SRC_' prefix."""
    text = _read_params_def_text()
    m = re.search(r"enum\s+ModSource\s*:\s*\w+\s*\{([^}]+)\}", text, re.S)
    if not m:
        # Fallback if header is unavailable
        return (
            ("0 - OFF", 0), ("1 - LFO1", 1), ("2 - LFO2", 2),
            ("3 - ENV_VCA", 3), ("4 - ENV_VCF", 4), ("5 - ENV_DCO", 5),
            ("6 - MODWHEEL", 6), ("7 - AFTERTC", 7), ("8 - VELOCITY", 8),
            ("9 - BEND", 9), ("10 - DRIFT", 10), ("11 - KEYTRACK", 11),
            ("12 - DRIFT_VOICE", 12), ("13 - RANDOM_SH", 13), ("14 - VOICE_ID", 14),
        )

    results: list[tuple[str, int]] = []
    for line in m.group(1).splitlines():
        item_match = re.search(r"^\s*([A-Z0-9_]+)\s*=\s*(\d+)", line)
        if item_match:
            raw_name = item_match.group(1)
            val = int(item_match.group(2))
            clean_name = raw_name[4:] if raw_name.startswith("SRC_") else raw_name
            label = f"{val} - {clean_name}"
            results.append((label, val))

    return tuple(results)


def load_mod_destinations() -> tuple[tuple[str, int], ...]:
    """Parse enum ModDest from params_def.h and strip 'DEST_' prefix."""
    text = _read_params_def_text()
    m = re.search(r"enum\s+ModDest\s*:\s*\w+\s*\{([^}]+)\}", text, re.S)
    if not m:
        # Fallback if header is unavailable
        return (
            ("0 - PITCH", 0), ("1 - VCF_CUTOFF", 1), ("2 - OSC1_LEVEL", 2),
            ("3 - OSC2_LEVEL", 3), ("4 - SUB_LEVEL", 4), ("5 - DIST_DRIVE", 5),
            ("6 - DIST_MIX", 6), ("7 - VCA_LEVEL", 7), ("8 - VCF_RESO", 8),
            ("9 - ENV_TO_VCF", 9), ("10 - ENV_TO_VCA", 10), ("11 - LFO1_SPEED", 11),
            ("12 - LFO2_SPEED", 12), ("13 - LFO1_DEPTH", 13), ("14 - LFO2_DEPTH", 14),
            ("15 - OSC2_DETUNE", 15), ("16 - PW", 16), ("17 - ENV_VCF_ATTACK", 17),
            ("18 - ENV_VCF_DECAY", 18), ("19 - ENV_VCA_ATTACK", 19),
            ("20 - ENV_VCA_DECAY", 20), ("21 - ENV_ALL_TIME", 21),
        )

    results: list[tuple[str, int]] = []
    for line in m.group(1).splitlines():
        item_match = re.search(r"^\s*([A-Z0-9_]+)\s*=\s*(\d+)", line)
        if item_match:
            raw_name = item_match.group(1)
            val = int(item_match.group(2))
            clean_name = raw_name[5:] if raw_name.startswith("DEST_") else raw_name
            label = f"{val} - {clean_name}"
            results.append((label, val))

    return tuple(results)

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