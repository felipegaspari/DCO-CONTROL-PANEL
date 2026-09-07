"""The DCO's control surface, as data. The GUI is generated entirely from this file.

Ranges are what the DCO's apply_param_*() functions in DCO/params.ino expect. Only
parameters the DCO actually handles are listed: the table mirrors paramTable[] in
DCO/params.ino.

Everything that is not a 1 ms ADSR/filter block goes out as a 4-byte 'p' frame
(id + int16 LE). ADSR times and the filter block stay packed ('a'–'d').
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
import re

import calstages
import models
import param_meta
import protocol

def _read_project_config_macro(name: str, fallback: int) -> int:
    """Read an integer macro definition from project_config.h in the parent directory."""
    header = Path(__file__).resolve().parent.parent / "project_config.h"
    try:
        text = header.read_text()
    except OSError:
        return fallback
    m = re.search(rf"^\s*#\s*define\s+{name}\s+(\d+)", text, re.M)
    return int(m.group(1)) if m else fallback

def _range_pwm_wrap() -> int:
    """RANGE_PWM_WRAP from the superproject; 14000 if the header is missing."""
    header = Path(__file__).resolve().parent.parent / "project_config.h"
    try:
        text = header.read_text()
    except OSError:
        return 14000
    m = re.search(r"^\s*#\s*define\s+RANGE_PWM_WRAP\s+(\d+)", text, re.M)
    return int(m.group(1)) if m else 14000


RANGE_PWM_WRAP = _range_pwm_wrap()
AMP_COMP_440_MIN = RANGE_PWM_WRAP // 18
AMP_COMP_440_MAX = RANGE_PWM_WRAP // 4

# --- Pulse Width PWM Wrap from project_config.h ---
PW_PWM_WRAP = _read_project_config_macro("PW_PWM_WRAP", 2047)
PW_CENTER_CAL_MIN = 0
PW_CENTER_CAL_MAX = PW_PWM_WRAP
PW_CENTER_CAL_DEFAULT = PW_PWM_WRAP // 2

# Tab names, in display order.
GROUP_OSC = "Oscillators"
GROUP_SUB = "Sub-osc"
GROUP_ENV = "Envelopes"
GROUP_FILTER = "Filter"
GROUP_PWM = "PWM"
GROUP_LFO = "LFOs"
GROUP_MOD = "Mod matrix"
GROUP_CHARACTER = "Character"
GROUP_CAL = "Calibration"
GROUP_DIAG = "Diagnostics"

GROUP_ORDER = [
    GROUP_OSC,
    GROUP_SUB,
    GROUP_ENV,
    GROUP_FILTER,
    GROUP_PWM,
    GROUP_LFO,
    GROUP_MOD,
    GROUP_CHARACTER,
    GROUP_CAL,
]

# --- Modulation Matrix Sources & Destinations (Parsed automatically from params_def.h) ---
_MOD_SOURCES = param_meta.load_mod_sources()
_MOD_DESTS = param_meta.load_mod_destinations()

# --- Sub-oscillators (ENABLE_SUBOSC_ENGINE2, RP2350 only; IDs 93–101) --------
_SUB_DIVIDES = (
    ("Off", 0),
    ("1 - master rate (phase / PWM only)", 1),
    ("2 - one octave down", 2),
    ("3 - octave + fifth down", 3),
    ("4 - two octaves down", 4),
    ("5", 5),
    ("6", 6),
    ("7", 7),
    ("8 - three octaves down", 8),
)
_SUB_LOGIC_OPS = (
    ("Off", 0),
    ("XOR - ring mod", 1),
    ("AND", 2),
    ("OR", 3),
    ("XNOR", 4),
    ("NAND", 5),
    ("NOR", 6),
    ("Sub 1 only", 7),
    ("Sub 2 only", 8),
)
_SUB_MASTERS = (
    ("OSC1", 0),
    ("OSC2", 1),
    ("OSC3", 2),
)


@dataclass(frozen=True)
class Param:
    pid: int
    label: str
    group: str
    kind: str = "slider"
    lo: int = 0
    hi: int = 127
    default: int = 0
    choices: tuple = ()
    pulse_value: int = 1
    note: str = ""
    cc: int | None = None
    models: tuple[str, ...] | None = None
    hidden: bool = False


@dataclass(frozen=True)
class BlockField:
    key: str
    label: str
    lo: int
    hi: int
    default: int
    exp: bool = False
    cc: int | None = None


@dataclass(frozen=True)
class Block:
    key: str
    label: str
    group: str
    fields: tuple
    builder: object = field(repr=False, default=None)
    note: str = ""


# --- Phase choices for PARAM_OSC_PHASE_SYNC (17) ----------------------------
_PHASE_PRESETS = {45: 2, 90: 3, 135: 4, 180: 5, 225: 6, 270: 7, 315: 8}
_PHASE_FINE = [30, 60, 120, 150, 210, 240, 300, 330]


def _phase_choices() -> tuple:
    entries = [
        ("Off - free running (no note-on sync)", 0),
        ("Sync at note-on (0 deg)", 1),
    ]
    by_deg = dict(_PHASE_PRESETS)
    for deg in _PHASE_FINE:
        by_deg[deg] = deg // 2
    for deg in sorted(by_deg):
        entries.append((f"Sync + {deg} deg", by_deg[deg]))
    return tuple(entries)


# Calibration stage scopes (PARAM_CALIBRATION_FLAG 150)
CAL_SCOPE_AMP = 1
CAL_SCOPE_PW = 2
CAL_SCOPE_FULL = 3
CAL_SCOPE_BUTTONS: tuple[tuple[str, int], ...] = (
    ("Amp comp", CAL_SCOPE_AMP),
    ("PW", CAL_SCOPE_PW),
    ("Full", CAL_SCOPE_FULL),
)

CAL_PRECISION_FINE_OFFSET = 4
CAL_PRECISION_FAST_OFFSET = 8
CAL_PRECISION_CHOICES: tuple[tuple[str, int], ...] = (
    ("Fast", CAL_PRECISION_FAST_OFFSET),
    ("Normal", 0),
    ("Fine", CAL_PRECISION_FINE_OFFSET),
)


PARAMS: list[Param] = [
    # --- Oscillators (Pitch, Sync, Voice, Levels, Enables) ---
    Param(13, "Octave shift", GROUP_OSC, "combo",
          choices=tuple((f"{(s - 36) // 12:+d}", s) for s in range(0, 73, 12)),
          default=24, cc=2),
    Param(14, "OSC2 interval (semitones)", GROUP_OSC, "slider", 0, 60, 36, cc=3),
    Param(34, "OSC3 interval (semitones)", GROUP_OSC, "slider", 0, 60, 36, cc=4),
    Param(15, "OSC2 detune", GROUP_OSC, "slider", 0, 512, 256, cc=5),
    Param(35, "OSC3 detune", GROUP_OSC, "slider", 0, 512, 256, cc=8),
    Param(32, "Hard sync topology", GROUP_OSC, "combo", default=0,
          choices=(("0 - all free running", 0), ("1 - OSC2 masters OSC1", 1), ("2 - OSC1 masters OSC2", 2)),
          note="which oscillator's sideset drives which reset pin",
          cc=20),
    Param(37, "Soft sync", GROUP_OSC, "combo", default=0,
          choices=(("0 - hard sync (cap only)", 0),
                   ("1 - soft ~40% window", 1),
                   ("2 - soft ~67% window", 2),
                   ("3 - soft ~86% window", 3)),
          note="0 = hard sync (sideset); 1..3 = soft sync trailing polled chunks",
          cc=21),
    Param(38, "Sub-oscillator divide", GROUP_OSC, "combo", default=0,
          choices=(("Off", 0), ("Divide by 2", 2), ("Divide by 4", 4)),
          cc=22),
    Param(17, "Osc sync / phase align OSC2", GROUP_OSC, "combo", default=0,
          choices=_phase_choices(),
          cc=23),
    Param(26, "Voice mode", GROUP_OSC, "combo", default=1,
          choices=param_meta.VOICE_MODES,
          cc=69),
    Param(27, "Voice alloc / note priority", GROUP_OSC, "combo", default=0,
          choices=param_meta.VOICE_ALLOC_MODES,
          cc=78),
    Param(28, "Unison detune", GROUP_OSC, "slider", 0, 127, 0, cc=70),
    Param(18, "Portamento time", GROUP_OSC, "slider", 0, 255, 0, cc=71),
    Param(33, "Portamento mode", GROUP_OSC, "combo", default=0,
          choices=(("0 - fixed time", 0), ("1 - slew rate", 1)),
          cc=72),
    Param(29, "Analog drift amount", GROUP_OSC, "slider", 0, 127, 0, cc=73),
    Param(30, "Analog drift speed", GROUP_OSC, "slider", 1, 255, 1, cc=74),
    Param(31, "Analog drift spread", GROUP_OSC, "slider", 1, 127, 1, cc=75),
    Param(43, "VCA level", GROUP_OSC, "slider", 0, 128, 128, cc=76),
    Param(21, "Velocity to VCA", GROUP_OSC, "slider", 0, 20, 0, cc=77),
    Param(22, "OSC1 level", GROUP_OSC, "slider", 0, 127, 127, cc=9),
    Param(23, "OSC2 level", GROUP_OSC, "slider", 0, 127, 0, cc=12),
    Param(39, "OSC3 level", GROUP_OSC, "slider", 0, 127, 0, cc=83),
    Param(24, "Sub level", GROUP_OSC, "slider", 0, 127, 0, cc=13),
    Param(1, "OSC1 Saw enable", GROUP_OSC, "check", default=0, cc=16),
    Param(2, "OSC1 Pulse enable", GROUP_OSC, "check", default=0, cc=17),
    Param(3, "OSC1 Tri enable", GROUP_OSC, "check", default=0, cc=18),
    Param(87, "OSC2 Saw enable", GROUP_OSC, "check", default=0, cc=112),
    Param(88, "OSC2 Pulse enable", GROUP_OSC, "check", default=0, cc=113),
    Param(89, "OSC2 Tri enable", GROUP_OSC, "check", default=0, cc=114, models=("dco3",)),
    Param(90, "OSC3 Saw enable", GROUP_OSC, "check", default=0, cc=115),
    Param(91, "OSC3 Pulse enable", GROUP_OSC, "check", default=0, cc=116),
    Param(92, "OSC3 Tri enable", GROUP_OSC, "check", default=0, cc=117),

    Param(130, "Crossmod depth", GROUP_OSC, "slider", 0, 32767, 0),
    Param(131, "Crossmod mode", GROUP_OSC, "combo", default=0, choices=param_meta.CROSSMOD_MODES),

    # --- Sub-oscillators (ENABLE_SUBOSC_ENGINE2; RP2350 only) ----------------
    Param(93, "Sub 1 divide", GROUP_SUB, "combo", default=0, choices=_SUB_DIVIDES, models=("dco3",)),
    Param(95, "Sub 1 master", GROUP_SUB, "combo", default=0, choices=_SUB_MASTERS, models=("dco3",)),
    Param(97, "Sub 1 phase", GROUP_SUB, "slider", 0, 359, 0, models=("dco3",)),
    Param(99, "Sub 1 width", GROUP_SUB, "slider", 1, 255, 128, models=("dco3",)),
    Param(94, "Sub 2 divide", GROUP_SUB, "combo", default=0, choices=_SUB_DIVIDES, models=("dco3",)),
    Param(96, "Sub 2 master", GROUP_SUB, "combo", default=1, choices=_SUB_MASTERS, models=("dco3",)),
    Param(98, "Sub 2 phase", GROUP_SUB, "slider", 0, 359, 0, models=("dco3",)),
    Param(100, "Sub 2 width", GROUP_SUB, "slider", 1, 255, 128, models=("dco3",)),
    Param(101, "Logic combiner", GROUP_SUB, "combo", default=0, choices=_SUB_LOGIC_OPS, models=("dco3",)),

    # --- Envelopes (Curves, Re-trigger & Routing) ----------------------------
    Param(222, "ADSR1 to VCA", GROUP_ENV, "slider", 0, 512, 512, cc=48),
    Param(126, "EnvDCO (ADSR3) enabled", GROUP_ENV, "check", default=1, cc=24),
    Param(10, "ADSR3 to osc select", GROUP_ENV, "combo", default=0,
          choices=(("0 - OSC1", 0), ("1 - OSC2", 1), ("2 - OSC1+2", 2), ("3 - OSC3", 3), ("4 - all", 4)),
          cc=25),
    Param(47, "ADSR3 to OSC1 detune", GROUP_ENV, "slider", -511, 511, 0, cc=26),
    
    # Envelope Modes (0=NORMAL, 1=CENTERED, 2=INVERTED)
    Param(224, "EnvVCA mode", GROUP_ENV, "combo", default=0, choices=param_meta.ENV_MODES),
    Param(225, "EnvVCF mode", GROUP_ENV, "combo", default=0, choices=param_meta.ENV_MODES),
    Param(223, "EnvDCO mode", GROUP_ENV, "combo", default=0, choices=param_meta.ENV_MODES),

    Param(48, "ADSR1 attack curve", GROUP_ENV, "combo", default=0, choices=param_meta.CURVE_PROFILES, cc=27),
    Param(49, "ADSR1 decay curve", GROUP_ENV, "combo", default=0, choices=param_meta.CURVE_PROFILES, cc=28),
    Param(50, "ADSR1 release curve", GROUP_ENV, "combo", default=0, choices=param_meta.CURVE_PROFILES),
    Param(51, "ADSR2 attack curve", GROUP_ENV, "combo", default=0, choices=param_meta.CURVE_PROFILES, cc=29),
    Param(52, "ADSR2 decay curve", GROUP_ENV, "combo", default=0, choices=param_meta.CURVE_PROFILES, cc=30),
    Param(53, "ADSR2 release curve", GROUP_ENV, "combo", default=0, choices=param_meta.CURVE_PROFILES),
    Param(54, "ADSR3 attack curve", GROUP_ENV, "combo", default=0, choices=param_meta.CURVE_PROFILES),
    Param(55, "ADSR3 decay curve", GROUP_ENV, "combo", default=0, choices=param_meta.CURVE_PROFILES),
    Param(56, "ADSR3 release curve", GROUP_ENV, "combo", default=0, choices=param_meta.CURVE_PROFILES),
    Param(8, "VCA ADSR restart", GROUP_ENV, "check", default=0, cc=31),
    Param(9, "VCF ADSR restart", GROUP_ENV, "check", default=0, cc=33),
    Param(214, "ADSR3 restart", GROUP_ENV, "check", default=0),

    # --- Filter & Post-Filter CVs --------------------------------------------
    Param(19, "VCF keytrack", GROUP_FILTER, "slider", -256, 255, 0, cc=49),
    Param(20, "Velocity to VCF", GROUP_FILTER, "slider", 0, 20, 0, cc=50),
    Param(7, "Resonance amp compensation", GROUP_FILTER, "check", default=0, cc=51),
    Param(60, "Filter mode", GROUP_FILTER, "combo", default=0,
          choices=(("0 - LP24", 0), ("1 - BP12", 1), ("2 - HP6/LP18", 2), ("3 - alt", 3)),
          cc=118),
    Param(58, "Distortion drive", GROUP_FILTER, "slider", 0, 4095, 0, cc=81),
    Param(59, "Distortion mix", GROUP_FILTER, "slider", 0, 4095, 0, cc=82),

    # --- PWM -----------------------------------------------------------------
    Param(210, "Pulse width", GROUP_PWM, "slider", 0, 4095, 2048, cc=59),
    Param(45, "LFO2 to PW", GROUP_PWM, "slider", 0, 511, 0, cc=56),
    Param(46, "ADSR3 to PWM", GROUP_PWM, "slider", 0, 1023, 512, cc=57),
    Param(124, "PWM pots manual", GROUP_PWM, "check", default=1, cc=None),

    # --- LFOs ----------------------------------------------------------------
    Param(11, "LFO1 waveform", GROUP_LFO, "combo", default=1, choices=param_meta.LFO_WAVEFORMS, cc=60),
    Param(12, "LFO2 waveform", GROUP_LFO, "combo", default=1, choices=param_meta.LFO_WAVEFORMS, cc=61),
    Param(41, "LFO1 speed", GROUP_LFO, "slider", 0, 4095, 0, cc=62),
    Param(42, "LFO2 speed", GROUP_LFO, "slider", 0, 4095, 0, cc=63),
    Param(40, "LFO1 to DCO", GROUP_LFO, "slider", 0, 511, 0, cc=65),
    Param(216, "LFO1 to OSC1 extra", GROUP_LFO, "slider", 0, 255, 0, cc=14),
    Param(217, "LFO1 to OSC2 extra", GROUP_LFO, "slider", 0, 255, 0, cc=15),
    Param(218, "LFO1 to OSC3 extra", GROUP_LFO, "slider", 0, 255, 0, cc=19),
    Param(44, "LFO1 to VCA", GROUP_LFO, "slider", 0, 1023, 0, cc=66),
    Param(16, "LFO2 to OSC2 detune", GROUP_LFO, "slider", 0, 255, 0, cc=67),
    Param(36, "LFO2 to OSC3 detune", GROUP_LFO, "slider", 0, 255, 0, cc=68),
    Param(219, "LFO2 to OSC2 coarse", GROUP_LFO, "slider", 0, 511, 0, cc=119),
    Param(220, "LFO2 to OSC3 coarse", GROUP_LFO, "slider", 0, 511, 0),

# --- Modulation Matrix (Slots 0..7 => ParamIds 63..86) -------------------
    Param(63, "Mod slot 0 source", GROUP_MOD, "combo", default=0, choices=_MOD_SOURCES, cc=84),
    Param(64, "Mod slot 0 dest", GROUP_MOD, "combo", default=0, choices=_MOD_DESTS, cc=85),
    Param(65, "Mod slot 0 depth", GROUP_MOD, "slider", -4095, 4095, 0, cc=86),
    Param(66, "Mod slot 1 source", GROUP_MOD, "combo", default=0, choices=_MOD_SOURCES, cc=87),
    Param(67, "Mod slot 1 dest", GROUP_MOD, "combo", default=0, choices=_MOD_DESTS, cc=88),
    Param(68, "Mod slot 1 depth", GROUP_MOD, "slider", -4095, 4095, 0, cc=89),
    Param(69, "Mod slot 2 source", GROUP_MOD, "combo", default=0, choices=_MOD_SOURCES, cc=90),
    Param(70, "Mod slot 2 dest", GROUP_MOD, "combo", default=0, choices=_MOD_DESTS, cc=91),
    Param(71, "Mod slot 2 depth", GROUP_MOD, "slider", -4095, 4095, 0, cc=92),
    Param(72, "Mod slot 3 source", GROUP_MOD, "combo", default=0, choices=_MOD_SOURCES, cc=93),
    Param(73, "Mod slot 3 dest", GROUP_MOD, "combo", default=0, choices=_MOD_DESTS, cc=94),
    Param(74, "Mod slot 3 depth", GROUP_MOD, "slider", -4095, 4095, 0, cc=95),
    Param(75, "Mod slot 4 source", GROUP_MOD, "combo", default=0, choices=_MOD_SOURCES, cc=96),
    Param(76, "Mod slot 4 dest", GROUP_MOD, "combo", default=0, choices=_MOD_DESTS, cc=97),
    Param(77, "Mod slot 4 depth", GROUP_MOD, "slider", -4095, 4095, 0, cc=102),
    Param(78, "Mod slot 5 source", GROUP_MOD, "combo", default=0, choices=_MOD_SOURCES, cc=103),
    Param(79, "Mod slot 5 dest", GROUP_MOD, "combo", default=0, choices=_MOD_DESTS, cc=104),
    Param(80, "Mod slot 5 depth", GROUP_MOD, "slider", -4095, 4095, 0, cc=105),
    Param(81, "Mod slot 6 source", GROUP_MOD, "combo", default=0, choices=_MOD_SOURCES, cc=106),
    Param(82, "Mod slot 6 dest", GROUP_MOD, "combo", default=0, choices=_MOD_DESTS, cc=107),
    Param(83, "Mod slot 6 depth", GROUP_MOD, "slider", -4095, 4095, 0, cc=108),
    Param(84, "Mod slot 7 source", GROUP_MOD, "combo", default=0, choices=_MOD_SOURCES, cc=109),
    Param(85, "Mod slot 7 dest", GROUP_MOD, "combo", default=0, choices=_MOD_DESTS, cc=110),
    Param(86, "Mod slot 7 depth", GROUP_MOD, "slider", -4095, 4095, 0, cc=111),

    # --- Character ---
    Param(221, "Character", GROUP_CHARACTER, "slider", 0, 128, 0),

    # --- Calibration ---
    Param(150, "Run calibration", GROUP_CAL, "pulse", pulse_value=CAL_SCOPE_FULL),
    Param(151, "Manual calibration mode", GROUP_CAL, "check", default=0),
    Param(152, "Manual cal stage", GROUP_CAL, "slider", 0, 8, 0),
    Param(153, "Manual cal offset", GROUP_CAL, "slider", -40, 50, 0),
    Param(159, "Amp comp @ 440 Hz", GROUP_CAL, "slider",
          AMP_COMP_440_MIN, AMP_COMP_440_MAX, AMP_COMP_440_MIN),
    Param(162, "PW center (cal)", GROUP_CAL, "slider",
          PW_CENTER_CAL_MIN, PW_CENTER_CAL_MAX, PW_CENTER_CAL_DEFAULT),
    Param(161, "Duty trim (0.01%)", GROUP_CAL, "slider", -500, 500, 0),
    Param(156, "Store manual cal offsets", GROUP_CAL, "pulse", pulse_value=1),
]


def _adsr_builder(cmd: bytes):
    def build(values: dict) -> bytes:
        return protocol.adsr_block(
            cmd,
            protocol.lin_to_exp(values["attack"]),
            protocol.lin_to_exp(values["decay"]),
            values["sustain"],
            protocol.lin_to_exp(values["release"]),
        )
    return build


def _adsr_fields(ccs: tuple, sustain_default: int = 4095) -> tuple:
    return (
        BlockField("attack", "Attack", 0, 4095, 0, exp=True, cc=ccs[0]),
        BlockField("decay", "Decay", 0, 4095, 1200, exp=True, cc=ccs[1]),
        BlockField("sustain", "Sustain", 0, 4095, sustain_default, cc=ccs[2]),
        BlockField("release", "Release", 0, 4095, 600, exp=True, cc=ccs[3]),
    )


BLOCKS: list[Block] = [
    Block("adsr_vca", "EnvVCA times", GROUP_ENV, _adsr_fields((34, 35, 36, 37)),
          _adsr_builder(protocol.CMD_ADSR1_BLOCK)),
    Block("adsr_vcf", "EnvVCF times", GROUP_ENV, _adsr_fields((39, 40, 41, 43)),
          _adsr_builder(protocol.CMD_ADSR2_BLOCK)),
    Block("adsr_dco", "EnvDCO times (pitch and PW)", GROUP_ENV, _adsr_fields((44, 45, 46, 47)),
          _adsr_builder(protocol.CMD_ADSR3_BLOCK)),
    Block("filter", "Filter block", GROUP_FILTER,
          (
              BlockField("cutoff", "Cutoff", 0, 4095, 4095, cc=52),
              BlockField("resonance", "Resonance", 0, 4095, 0, cc=53),
              BlockField("adsr2_to_vcf", "EnvVCF to cutoff", 0, 512, 0, cc=54),
              BlockField("lfo2_to_vcf", "LFO2 to cutoff", 0, 512, 0, cc=55),
          ),
          lambda v: protocol.filter_block(v["cutoff"], v["resonance"], v["adsr2_to_vcf"], v["lfo2_to_vcf"])),
]

DEBUG_COMMANDS = (
    ("PIO topology report", 1),
    ("Sub-osc engine report", 4),
    ("PWM DMA Report", 5),
    ("MCU DMA Channel Map", 6),
    ("Period probe, clk_div 2000", 2),
    ("Period probe, clk_div 20000", 3),
    ("Dump RAM (heap/stack)", 13),
    ("Mem diag polls off", 14),
    ("Mem diag polls on", 15),
    ("Note retrig: EXACT_Y", 26),
    ("Note retrig: SYNC_JMP", 27),
    ("Reboot MCU", 90),
    ("Reboot to BOOTSEL", 91),
)

BENCH_COMMANDS = (
    ("Dump profiler once", 10),
    ("Reset profiler", 11),
    ("Toggle ~1 Hz dump", 12),
)

BENCH_MB_COMMANDS = (
    ("Dump Mainboard profiler once", 45),
    ("Toggle Mainboard ~1 Hz dump", 42),
)

MCP_DAC_COMMANDS = (
    ("MCP4728 probe", 43),
    ("MCP4728 reattach", 44),
)

AMP_COMP_COMMANDS = (
    ("Amp: FLOAT_QUAD", 20),
    ("Amp: LUT", 21),
    ("Amp: FIXED", 22),
    ("Amp: speed bench", 24),
    ("Amp: accuracy", 25),
)

PITCH_INTERP_COMMANDS = (
    ("Pitch: speed bench", 28),
    ("Pitch: accuracy", 29),
)

CLKDIV_HP_COMMANDS = (
    ("Clkdiv: speed bench", 32),
    ("Clkdiv: accuracy", 33),
)

CAL_DEBUG_COMMANDS = (
    ("Seed fake calibration tables", 30),
    ("Verify sweep (measure stored tables)", 36),
    ("PW CV probe (needs manual cal)", 46),
)

AMP_CAL_METHOD_COMMANDS = (
    ("Amp cal: CLASSIC (per-note PWM)", 34),
    ("Amp cal: FREQ_TRACE (freq bisection)", 35),
)

FREQ_SEARCH_MODE_COMMANDS = (
    ("Search: BISECT (midpoint, sign only)", 37),
    ("Search: INTERP (Illinois secant)", 38),
    ("Search: GATED (secant above noise)", 39),
)

AMP0_MODE_COMMANDS = (
    ("Amp-0: MEASURE (live hunt)", 40),
    ("Amp-0: CALC (bottom-rung fit)", 41),
)

PIO_PULSE_LO = 200
PIO_PULSE_HI = 50000
PIO_PULSE_DEFAULT = 1600

CHARACTER_JITTERS = (
    ("Amplitude compensation jitter", 0xC8),
    ("Pitch jitter", 0xCA),
    ("Pulsewidth jitter", 0xCB),
)
CHARACTER_JITTER_LO = 0
CHARACTER_JITTER_HI = 128
CHARACTER_JITTER_DEFAULT = 0

DEBUG_PARAM_ID = 160

_SUB_ONLY_MOD_DEST_VALUES = {}
_MOD_DEST_PIDS = {64, 67, 70, 73, 76, 79, 82, 85}


def apply_model(profile: models.ModelProfile) -> None:
    rebuilt: list[Param] = []
    for p in PARAMS:
        if p.models is not None and profile.key not in p.models:
            continue
        changes: dict = {}
        if p.pid in profile.label_overrides:
            changes["label"] = profile.label_overrides[p.pid]
        if p.pid in profile.choice_overrides:
            changes["choices"] = profile.choice_overrides[p.pid]
        if p.pid in profile.note_overrides:
            changes["note"] = profile.note_overrides[p.pid]
        if not profile.has_sub_engine and p.pid in _MOD_DEST_PIDS:
            changes["choices"] = tuple(
                c for c in p.choices if c[1] not in _SUB_ONLY_MOD_DEST_VALUES)
        if p.pid in profile.hidden_pids:
            changes["hidden"] = True
        if p.pid == 152:
            changes["hi"] = calstages.stage_max()
        if p.pid == 162 and not calstages.is_packed():
            changes["hidden"] = True
        rebuilt.append(dataclasses.replace(p, **changes) if changes else p)
    PARAMS[:] = rebuilt

    populated = {p.group for p in PARAMS} | {b.group for b in BLOCKS}
    GROUP_ORDER[:] = [g for g in GROUP_ORDER if g in populated]


def visible_params(group: str | None = None) -> list[Param]:
    return [p for p in PARAMS
            if not p.hidden and (group is None or p.group == group)]
