"""Synth model profiles: one dco_control codebase driving two boards.

The tool is shared between DCO3-MONOSYNTH and DCO4-REBORN. Both firmwares speak
the same slim serial protocol, preset-store record format and '[dump]' text
protocol; what differs is the control surface (DCO3 has the dual sub-osc
engine, DCO4 hides the OSC3 controls because its voices are 2-osc), the
calibration table sizes (oscillator / PW channel counts), whether the DCO relays
Screen signals for the host, and the USB product string used for auto-detection.

Everything model-specific lives in a ModelProfile here. The active profile is
chosen at startup — --model dco3|dco4, or auto-detected from the USB product
descriptors of the connected serial ports, or from the PROJECT_INSTRUMENT in
the superproject's project_config.h when no board is plugged in — and the rest
of the tool reads it via models.active(). params.apply_model() bakes the
profile's param overrides into params.PARAMS before the GUI is built.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

AMP_COMP_PAIRS = 22  # [freq_x100, pwm] pairs per oscillator (both firmwares)


@dataclass(frozen=True)
class ModelProfile:
    key: str                 # "dco3" | "dco4" — also the JSON file-format prefix
    display_name: str
    usb_product_prefix: str  # USBDevice.setProductDescriptor() prefix in DCO.ino
    num_oscillators: int     # voiceTables / ManualOffset entry count (FS.h)
    num_pw_channels: int     # PWCenter / PWHighLimit / PWLowLimit entry count
    has_sub_engine: bool     # Sub-osc tab + params 90-99 (ENABLE_SUBOSC_ENGINE2)
    has_mainboard: bool      # STM32 Mainboard behind Serial2 (profiler opcode 42)
    # USB 's' ScreenMode relay + PARAM_UI_PRESET_SCROLL handlers in DCO/Serial.ino.
    # Without them the Screen keeps showing whatever the Input board last wrote,
    # so the panel skips those frames instead of sending ones the board drops.
    has_screen_signals: bool
    osc_row_names: tuple[str, str, str]  # wave-matrix / UI row labels
    # Params kept in PARAMS (firmware routes them; presets/MIDI map keep them)
    # but hidden from the GUI on this model.
    hidden_pids: frozenset[int] = frozenset()
    # Cosmetic per-model wording (OSC1/OSC2 vs OSC A/OSC B).
    label_overrides: Mapping[int, str] = field(default_factory=dict)
    choice_overrides: Mapping[int, tuple] = field(default_factory=dict)
    note_overrides: Mapping[int, str] = field(default_factory=dict)
    # Debug-button opcodes this model's firmware lacks (Diagnostics + Calibration tabs).
    hidden_debug_values: frozenset[int] = frozenset()
    midi_target: str = "midi:dco"       # gen_midi_map Open Stage Control target
    panel_filename: str = "dco_panel.json"

    @property
    def bank_filename(self) -> str:
        return f"bank_{self.key}.json"

    # Calibration bank sizes in bytes, mirroring each firmware's FS.h.
    @property
    def voice_tables_size(self) -> int:
        return self.num_oscillators * AMP_COMP_PAIRS * 8

    # 3 points x 6 bytes [center: u16, lowLimit: u16, highLimit: u16] = 18 bytes per channel
    @property
    def pw_bank_size(self) -> int:
        return self.num_pw_channels * 18

    # 1 byte per oscillator
    @property
    def amp_comp_top_pair_size(self) -> int:
        return self.num_oscillators

    @property
    def manual_offset_size(self) -> int:
        return self.num_oscillators

    @property
    def amp_comp_440_size(self) -> int:
        return self.num_oscillators * 2

    @property
    def amp_comp_duty_size(self) -> int:
        return self.num_oscillators * 2

DCO3 = ModelProfile(
    key="dco3",
    display_name="DCO3-MONOSYNTH",
    usb_product_prefix="DCO3-MONO",
    num_oscillators=3,
    num_pw_channels=3,
    has_sub_engine=True,
    has_mainboard=False,
    has_screen_signals=False,
    osc_row_names=("OSC1", "OSC2", "OSC3"),
    midi_target="midi:dco3",
    panel_filename="dco3_panel.json",
)

# DCO4-REBORN: 4 voices x 2 oscillators. The OSC3 params exist in the firmware
# param table (numeric parity with DCO3) but drive nothing a player can hear,
# so they stay out of the GUI. Sub-osc engine 2 (params 90-99) is DCO3-only.
DCO4 = ModelProfile(
    key="dco4",
    display_name="DCO4-REBORN",
    usb_product_prefix="DCO4-REBORN",
    num_oscillators=8,
    num_pw_channels=4,
    has_sub_engine=False,
    has_mainboard=True,
    has_screen_signals=True,
    osc_row_names=("OSC A", "OSC B", "OSC3"),
    hidden_pids=frozenset({33, 34, 35, 38, 87, 88, 89, 218, 220}),
    label_overrides=MappingProxyType({
        1: "OSC A Saw enable",
        2: "OSC A Pulse enable",
        3: "OSC A Tri enable",
        14: "OSC B interval (semitones)",
        15: "OSC B detune",
        16: "LFO2 to OSC B detune",
        17: "Osc sync / phase align OSC B",
        22: "OSC A level",
        23: "OSC B level",
        47: "ADSR3 to OSC A detune",
        84: "OSC B Saw enable",
        85: "OSC B Pulse enable",
        86: "OSC B Tri enable",
        216: "LFO1 to OSC A extra",
        217: "LFO1 to OSC B extra",
        219: "LFO2 to OSC B coarse",
    }),
    choice_overrides=MappingProxyType({
        31: (("0 - all free running", 0),
             ("1 - OSC B masters OSC A", 1),
             ("2 - OSC A masters OSC B", 2)),
        10: (("0 - OSC A", 0), ("1 - OSC B", 1), ("2 - OSC A+B", 2),
             ("3 - OSC3", 3), ("4 - all", 4)),
    }),
    note_overrides=MappingProxyType({
        31: "which oscillator's sideset drives which reset pin; not the note-on "
            "phase reset, which is 'Osc sync / phase align OSC B' below",
        37: "output on GP8, needs a mixer input on the carrier to be audible",
    }),
    # Sub-osc engine report (DCO4 has no ENABLE_SUBOSC_ENGINE2).
    hidden_debug_values=frozenset({4}),
    midi_target="midi:dco4",
    panel_filename="dco4_panel.json",
)

PROFILES: dict[str, ModelProfile] = {p.key: p for p in (DCO3, DCO4)}

_active: ModelProfile = DCO3


def active() -> ModelProfile:
    return _active


def set_active(key: str) -> ModelProfile:
    global _active
    _active = PROFILES[key]
    return _active


def detect() -> str | None:
    """Guess the model from the connected board, or from the project this copy sits in.

    Returns a profile key, or None when neither is available (both boards
    present also returns the first match — use --model to be explicit).
    """
    return detect_from_usb() or detect_from_project()


def detect_from_usb() -> str | None:
    """Match the USB product strings of the visible serial ports against the profiles."""
    try:
        from serial.tools import list_ports
    except ImportError:
        return None
    for p in list_ports.comports():
        product = ((getattr(p, "product", None) or "") + " " + (p.description or "")).lower()
        for profile in PROFILES.values():
            if profile.usb_product_prefix.lower() in product:
                return profile.key
    return None


def detect_from_project() -> str | None:
    """Read PROJECT_INSTRUMENT from the superproject's project_config.h.

    This repo is checked out into both DCO3-MONOSYNTH and DCO4-REBORN, and that
    header is how every board in a project knows which instrument it belongs to.
    Reading it here means the tool opens as the right synth with nothing plugged
    in, instead of falling back to the dco3 profile inside a dco4 tree.
    """
    import re
    from pathlib import Path

    header = Path(__file__).resolve().parent.parent / "project_config.h"
    try:
        text = header.read_text()
    except OSError:
        return None
    m = re.search(r"^\s*#\s*define\s+PROJECT_INSTRUMENT\s+(\d+)", text, re.M)
    if not m:
        return None
    return {"3": "dco3", "4": "dco4"}.get(m.group(1))


def filter_debug_commands(commands: tuple[tuple[str, int], ...]) -> tuple[tuple[str, int], ...]:
    """Drop Diagnostics buttons whose opcode the active firmware doesn't have."""
    return tuple(c for c in commands if c[1] not in _active.hidden_debug_values)
