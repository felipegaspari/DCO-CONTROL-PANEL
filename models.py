"""Synth model profiles: one dco_control codebase driving two boards.

The tool is shared between DCO3-MONOSYNTH and DCO4-REBORN. Both firmwares speak
the same slim serial protocol, preset-store record format and '[dump]' text
protocol; what differs is the control surface (DCO3 has the dual sub-osc
engine, DCO4 hides the OSC3 controls because its voices are 2-osc), the
calibration table sizes (oscillator / PW channel counts), whether the DCO relays
Screen signals for the host, and the USB product string used for auto-detection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
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
    has_sub_engine: bool     # Sub-osc tab + params 93-101 (ENABLE_SUBOSC_ENGINE2)
    has_mainboard: bool      # STM32 Mainboard behind Serial2 (profiler opcode 42)
    has_screen_signals: bool
    osc_row_names: tuple[str, str, str]  # wave-matrix / UI row labels
    hidden_pids: frozenset[int] = frozenset()
    label_overrides: Mapping[int, str] = field(default_factory=dict)
    choice_overrides: Mapping[int, tuple] = field(default_factory=dict)
    note_overrides: Mapping[int, str] = field(default_factory=dict)
    hidden_debug_values: frozenset[int] = frozenset()
    midi_target: str = "midi:dco"
    panel_filename: str = "dco_panel.json"

    @property
    def bank_filename(self) -> str:
        return f"bank_{self.key}.json"

    @property
    def voice_tables_size(self) -> int:
        return self.num_oscillators * AMP_COMP_PAIRS * 8

    # 3 points x 6 bytes [center: u16, lowLimit: u16, highLimit: u16] = 18 bytes per channel
    @property
    def pw_bank_size(self) -> int:
        return self.num_pw_channels * 18

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

# DCO4-REBORN: 4 voices x 2 oscillators.
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
    # Hide OSC3 controls (34, 35, 36, 39, 90, 91, 92, 218, 220)
    hidden_pids=frozenset({34, 35, 36, 39, 90, 91, 92, 218, 220}),
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
        87: "OSC B Saw enable",
        88: "OSC B Pulse enable",
        89: "OSC B Tri enable",
        216: "LFO1 to OSC A extra",
        217: "LFO1 to OSC B extra",
        219: "LFO2 to OSC B coarse",
    }),
    choice_overrides=MappingProxyType({
        32: (("0 - all free running", 0),
             ("1 - OSC B masters OSC A", 1),
             ("2 - OSC A masters OSC B", 2)),
        10: (("0 - OSC A", 0), ("1 - OSC B", 1), ("2 - OSC A+B", 2),
             ("3 - OSC3", 3), ("4 - all", 4)),
    }),
    note_overrides=MappingProxyType({
        32: "which oscillator's sideset drives which reset pin; not the note-on "
            "phase reset, which is 'Osc sync / phase align OSC B' below",
        38: "output on GP8, needs a mixer input on the carrier to be audible",
    }),
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
    return detect_from_usb() or detect_from_project()


def detect_from_usb() -> str | None:
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
    return tuple(c for c in commands if c[1] not in _active.hidden_debug_values)