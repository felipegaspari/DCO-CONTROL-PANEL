"""Bridge and parser for DCO-PROTOCOL/param_meta.h and params_def.h.
Provides dynamic lookup for display names, curve choices, waveform choices,
voice modes, and modulation matrix routing.
"""
from __future__ import annotations

from pathlib import Path
import re

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
PROTOCOL_DIR = PROJECT_ROOT / "DCO-PROTOCOL"
DCO_DIR = PROJECT_ROOT / "DCO"

def _find_header(filename: str) -> Path | None:
    for candidate in (
        PROTOCOL_DIR / filename,
        DCO_DIR / "_shared" / filename,
        DCO_DIR / filename,
        HERE / filename,
    ):
        if candidate.is_file():
            return candidate
    return None

def _read_header_text(filename: str) -> str:
    path = _find_header(filename)
    if path and path.is_file():
        return path.read_text(encoding="utf-8")
    return ""

def _parse_c_switch_labels(text: str, func_name: str) -> dict[str, str]:
    """Extract case ENUM_CONST: return " Label"; mappings from a C++ switch function."""
    labels: dict[str, str] = {}
    pattern = rf"{func_name}\s*\([^)]*\)\s*\{{(.*?)\n\}}"
    match = re.search(pattern, text, re.DOTALL)
    if match:
        body = match.group(1)
        for case_match in re.finditer(r'case\s+([A-Z0-9_]+)\s*:\s*return\s*"([^"]*)";', body):
            enum_key = case_match.group(1)
            raw_label = case_match.group(2).strip()
            labels[enum_key] = raw_label
    return labels

def _format_token_fallback(raw_token: str, prefix: str) -> str:
    """Format raw enum string (e.g. DEST_VCF_CUTOFF -> VCF Cutoff) if label is missing."""
    token = raw_token[len(prefix):] if raw_token.startswith(prefix) else raw_token
    words = [w.upper() if len(w) <= 3 else w.capitalize() for w in token.split('_')]
    return " ".join(words)

def load_mod_sources() -> tuple[tuple[str, int], ...]:
    """Parse enum ModSource from params_def.h and labels from param_meta.h."""
    def_text = _read_header_text("params_def.h")
    meta_text = _read_header_text("param_meta.h")
    meta_labels = _parse_c_switch_labels(meta_text, "param_mod_source_name")

    results: list[tuple[str, int]] = []
    enum_match = re.search(r"enum\s+ModSource\s*:\s*\w+\s*\{([^}]+)\}", def_text, re.DOTALL)
    if enum_match:
        for line in enum_match.group(1).splitlines():
            item_match = re.search(r"^\s*([A-Z0-9_]+)\s*=\s*(\d+)", line)
            if item_match:
                raw_name = item_match.group(1)
                val = int(item_match.group(2))
                label = meta_labels.get(raw_name) or _format_token_fallback(raw_name, "SRC_")
                results.append((f"{val} - {label}", val))
    
    if not results:
        # Emergency static fallback if headers are completely absent
        return (("0 - Off", 0), ("1 - LFO 1", 1), ("2 - LFO 2", 2))
    return tuple(results)

def load_mod_destinations() -> tuple[tuple[str, int], ...]:
    """Parse enum ModDest from params_def.h and labels from param_meta.h."""
    def_text = _read_header_text("params_def.h")
    meta_text = _read_header_text("param_meta.h")
    meta_labels = _parse_c_switch_labels(meta_text, "param_mod_dest_name")

    results: list[tuple[str, int]] = []
    enum_match = re.search(r"enum\s+ModDest\s*:\s*\w+\s*\{([^}]+)\}", def_text, re.DOTALL)
    if enum_match:
        for line in enum_match.group(1).splitlines():
            item_match = re.search(r"^\s*([A-Z0-9_]+)\s*=\s*(\d+)", line)
            if item_match:
                raw_name = item_match.group(1)
                val = int(item_match.group(2))
                label = meta_labels.get(raw_name) or _format_token_fallback(raw_name, "DEST_")
                results.append((f"{val} - {label}", val))

    if not results:
        return (("0 - Master Pitch", 0), ("1 - Cutoff", 1))
    return tuple(results)

# --- Dynamic C++ Switch Case Parser for Numeric Enums ---
def load_enum_choices(func_name: str, fallback: tuple[tuple[str, int], ...] = ()) -> tuple[tuple[str, int], ...]:
    """Parse case N: return " Label"; from any switch function in param_meta.h."""
    meta_text = _read_header_text("param_meta.h")
    pattern = rf"{func_name}\s*\([^)]*\)\s*\{{(.*?)\n\}}"
    match = re.search(pattern, meta_text, re.DOTALL)
    if not match:
        return fallback

    body = match.group(1)
    choices: list[tuple[str, int]] = []

    # Matches: case 0: return " OFF";
    for case_match in re.finditer(r'case\s+(-?\d+)\s*:\s*return\s*"([^"]*)";', body):
        val = int(case_match.group(1))
        label = case_match.group(2).strip()
        if "out of range" not in label.lower():
            choices.append((f"{val} - {label}", val))

    return tuple(choices) if choices else fallback

# --- Dynamically Populated Tuples (Single Source of Truth: param_meta.h) ---
CURVE_PROFILES     = load_enum_choices("param_curve_name")
VOICE_MODES        = load_enum_choices("param_voice_mode_name")
VOICE_ALLOC_MODES  = load_enum_choices("param_voice_alloc_name")
LFO_WAVEFORMS      = load_enum_choices("param_lfo_waveform_name")
ENV_MODES          = load_enum_choices("param_env_mode_name")
SYNC_MODES         = load_enum_choices("param_sync_mode_name")
SOFT_SYNC_MODES    = load_enum_choices("param_soft_sync_mode_name")
PORTA_MODES        = load_enum_choices("param_portamento_mode_name")

def scale_display_value(pid: int, val: int) -> int:
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
    scaled = scale_display_value(pid, val)
    if pid == 13:
        return f"{scaled:+d} oct"
    if pid in (14, 34):
        return f"{scaled:+d} st"
    if pid in (15, 35):
        return f"{scaled:+d}"
    if pid == 46:
        return f"{scaled:+d}"
    # Modulation Matrix depths (Slots 0..7: PIDs 65, 68, 71, 74, 77, 80, 83, 86)
    if 65 <= pid <= 86 and (pid - 65) % 3 == 0:
        return f"{scaled:+d}" if scaled != 0 else "0"
    return str(scaled)
