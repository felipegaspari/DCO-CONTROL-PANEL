"""The DCO's control surface, as data. The GUI is generated entirely from this file.

Ranges are what the DCO's apply_param_*() functions in DCO/params.ino expect. Only
parameters the DCO actually handles are listed: the table mirrors paramTable[] in
DCO/params.ino.

Everything that is not a 1 ms ADSR/filter block goes out as a 4-byte 'p' frame
(id + int16 LE). ADSR times and the filter block stay packed ('a'–'d').

This table is the superset for both synths (see models.py). Call
apply_model(models.active()) once at startup: it drops params the model's
firmware doesn't route (Param.models), marks GUI-hidden ones (Param.hidden)
and applies the model's cosmetic label/choice/note overrides.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field

import models
import protocol

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
# App-only tab (PIO reports + profiler). Not in GROUP_ORDER — no MIDI CC / OSC panel.
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

_MOD_SOURCES = (
    ("Off / empty", 255),
    ("0 ADSR3 (EnvDCO)", 0),
    ("1 ADSR4 (stub)", 1),
    ("2 LFO3 (stub)", 2),
    ("3 LFO4 (stub)", 3),
    ("4 Velocity", 4),
    ("5 Keytrack", 5),
    ("6 Random", 6),
    ("7 Aftertouch", 7),
    ("8 LFO1", 8),
    ("9 LFO2", 9),
    ("10 Pitch bend", 10),
    ("11 Mod wheel", 11),
    ("12 Noise 0", 12),
    ("13 Noise 1", 13),
    ("14 Noise 2 (reserved)", 14),
    ("15 Noise 3 (reserved)", 15),
)

_MOD_DESTS = (
    ("Off / empty", 255),
    ("0 OSC1 level", 0),
    ("1 OSC2 level", 1),
    ("2 OSC3 level", 2),
    ("3 Sub level", 3),
    ("4 VCF1 reso", 4),
    ("5 VCF2 reso", 5),
    ("6 Dist Drive", 6),
    ("7 VCF cutoff", 7),
    ("8 Dist Mix", 8),
    ("9 Pitch (±1 oct @ ±1023)", 9),
    ("10 Sub phase (sub 2)", 10),
    ("11 Sub pulse width (sub 2)", 11),
)

# --- Sub-oscillators (ENABLE_SUBOSC_ENGINE2, RP2350 only) -------------------
# Two subs on pio2, plus the boolean combiner on SM3 that is the section's actual output. None
# of these get a MIDI CC: every non-reserved controller is already taken (gen_midi_map.py
# reports 0 free), and for the two that want continuous control the mod matrix has SUB_PHASE
# and SUB_PW destinations, which reach them at control-frame rate instead of MIDI rate.
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
    """One 'p'-frame parameter and how to present it.

    kind is one of:
      slider - continuous, lo..hi
      combo  - pick from choices, a tuple of (label, value) pairs
      check  - 0 or 1
      pulse  - a button that sends pulse_value once, for command-style params

    cc is the MIDI controller number gen_midi_map.py assigns to this parameter, or None
    for the ones deliberately left unreachable by CC. The numbers are written out rather
    than allocated on the fly so that reordering this table never moves an existing
    assignment out from under a saved panel or a DAW automation lane.
    """

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
    # None = every model routes this param; otherwise a tuple of model keys.
    # apply_model() removes params entirely for models not listed here.
    models: tuple[str, ...] | None = None
    # Set by apply_model() from the profile: keep in PARAMS (presets / MIDI
    # map still carry it) but skip the GUI on this model.
    hidden: bool = False


@dataclass(frozen=True)
class BlockField:
    key: str
    label: str
    lo: int
    hi: int
    default: int
    exp: bool = False  # run through lin_to_exp() before sending
    cc: int | None = None


@dataclass(frozen=True)
class Block:
    """A packed 1 ms frame ('a'–'d'). Any field change re-sends the whole frame,
    which is exactly what the Input board does every millisecond."""

    key: str
    label: str
    group: str
    fields: tuple
    builder: object = field(repr=False, default=None)
    note: str = ""


# --- Osc sync and phase align (PARAM_OSC_SYNC_MODE = 17) --------------------
# One param, three regimes, per apply_param_osc_sync_mode() in DCO/params.ino and the
# `oscSync >= 1` gate in DCO/voices.ino:
#
#   0      the note-on block is skipped entirely, so the oscillators run straight through
#          note-on and their phase relationship is whatever it happens to be
#   1      OSC1 and OSC2 are stopped, reloaded and restarted together at note-on, with no
#          phase offset
#   2..8   the same restart, plus OSC2 first-flyback offset (45..315 deg); EXACT_Y
#   >8     the same restart, with the offset in degrees being value * 2
#
# Presented as one combo so the encoding never has to be worked out by hand.
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


# Bézier curve indices 0–7; names match SCREEN displayParams.ino (attack vs decay
# differ because attack uses the reversed tables). Decay's screen case 8 LINEAR is
# unreachable (firmware/encoder clamp to 7) and is omitted.
_ENV_ATTACK_CURVES = (
    ("0 - EXP", 0),
    ("1 - SOFT", 1),
    ("2 - STEEP", 2),
    ("3 - CONCAVE", 3),
    ("4 - FAST S", 4),
    ("5 - SLOW THEN LIN", 5),
    ("6 - ALMOST LIN", 6),
    ("7 - LINEAR", 7),
)
_ENV_DECAY_CURVES = (
    ("0 - EXP", 0),
    ("1 - SOFT", 1),
    ("2 - STEEP", 2),
    ("3 - CONVEX", 3),
    ("4 - FAST START S", 4),
    ("5 - SLOW THEN LIN", 5),
    ("6 - FAST THEN LIN", 6),
    ("7 - ALMOST LIN", 7),
)


PARAMS: list[Param] = [
    # --- Oscillators (pitch, sync, voice, levels, wave enables) ---
    # Wire value is biased: table_index = midi - 36 + value (36 ⇒ unison).
    Param(13, "Octave shift", GROUP_OSC, "combo",
          choices=tuple((f"{(s - 36) // 12:+d}", s) for s in range(0, 73, 12)),
          default=24, cc=2),
    Param(14, "OSC2 interval (semitones)", GROUP_OSC, "slider", 0, 60, 36, cc=3),
    Param(33, "OSC3 interval (semitones)", GROUP_OSC, "slider", 0, 60, 36, cc=4),
    Param(15, "OSC2 detune", GROUP_OSC, "slider", 0, 512, 0, cc=5),
    Param(34, "OSC3 detune", GROUP_OSC, "slider", 0, 512, 0, cc=8),
    Param(31, "Hard sync topology", GROUP_OSC, "combo", default=0,
          choices=(("0 - all free running", 0), ("1 - OSC2 masters OSC1", 1), ("2 - OSC1 masters OSC2", 2)),
          note="which oscillator's sideset drives which reset pin; not the note-on phase "
               "reset, which is 'Osc sync / phase align OSC2' below",
          cc=20),
    Param(36, "Soft sync", GROUP_OSC, "combo", default=0,
          choices=(("0 - hard sync (cap only)", 0),
                   ("1 - soft ~40% window", 1),
                   ("2 - soft ~67% window", 2),
                   ("3 - soft ~86% window", 3)),
          note="0 = hard sync (sideset); 1..3 = soft sync trailing polled chunks",
          cc=21),
    Param(37, "Sub-oscillator divide", GROUP_OSC, "combo", default=0,
          choices=(("Off", 0), ("Divide by 2", 2), ("Divide by 4", 4)),
          note="the legacy single sub on GP8. On an ENABLE_SUBOSC_ENGINE2 build this sets both "
               "subs at once, and the Sub-osc tab is the finer-grained version of it", cc=22),
    Param(17, "Osc sync / phase align OSC2", GROUP_OSC, "combo", default=0,
          choices=_phase_choices(),
          note="Off leaves the oscillators running through note-on; every other setting "
               "restarts OSC1 and OSC2 together there, the degree entries delaying OSC2's "
               "first flyback (EXACT_Y). Changing this retriggers all notes.",
          cc=23),
    Param(26, "Voice mode", GROUP_OSC, "combo", default=0,
          choices=(("0 - mono", 0), ("1 - poly", 1), ("2 - stack", 2)),
          note="Mono (`0`) uses a last-note-priority held-note stack (overlapping keys; "
               "release falls back and retriggers porta). See [`REFERENCE_AI.md`]"
               "(REFERENCE_AI.md) (`note_on` / `note_off`).",
          cc=69),
    Param(27, "Unison detune", GROUP_OSC, "slider", 0, 127, 0, cc=70),
    Param(18, "Portamento time", GROUP_OSC, "slider", 0, 255, 0, cc=71),
    Param(32, "Portamento mode", GROUP_OSC, "combo", default=0,
          choices=(("0 - fixed time (same duration any interval)", 0),
                   ("1 - slew rate (time scales with interval; knob = time per octave)", 1)),
          cc=72),
    Param(28, "Analog drift amount", GROUP_OSC, "slider", 0, 127, 0, cc=73),
    Param(29, "Analog drift speed", GROUP_OSC, "slider", 1, 255, 1, cc=74),
    Param(30, "Analog drift spread", GROUP_OSC, "slider", 1, 127, 1, cc=75),
    Param(43, "VCA level", GROUP_OSC, "slider", 0, 128, 128, cc=76),
    Param(21, "Velocity to VCA", GROUP_OSC, "slider", 0, 20, 0, cc=77),
    Param(22, "OSC1 level", GROUP_OSC, "slider", 0, 127, 127, cc=9),
    Param(23, "OSC2 level", GROUP_OSC, "slider", 0, 127, 0, cc=12),
    Param(38, "OSC3 level", GROUP_OSC, "slider", 0, 127, 0, cc=83),
    Param(24, "Sub level", GROUP_OSC, "slider", 0, 127, 0, cc=13),
    Param(1, "OSC1 Saw enable", GROUP_OSC, "check", default=0,
          note="DG411 via dual 595; needs ENABLE_WAVE_MUX", cc=16),
    Param(2, "OSC1 Pulse enable", GROUP_OSC, "check", default=0, note="analog Pulse", cc=17),
    Param(3, "OSC1 Tri enable", GROUP_OSC, "check", default=0, cc=18),
    Param(84, "OSC2 Saw enable", GROUP_OSC, "check", default=0, cc=112),
    Param(85, "OSC2 Pulse enable", GROUP_OSC, "check", default=0, cc=113),
    Param(86, "OSC2 Tri enable", GROUP_OSC, "check", default=0, cc=114),
    Param(87, "OSC3 Saw enable", GROUP_OSC, "check", default=0, cc=115),
    Param(88, "OSC3 Pulse enable", GROUP_OSC, "check", default=0, cc=116),
    Param(89, "OSC3 Tri enable", GROUP_OSC, "check", default=0, cc=117),

    # --- Sub-oscillators: two subs and their combination (ENABLE_SUBOSC_ENGINE2) ---
    # A sub counts the flybacks of whichever oscillator it follows, so its frequency is locked to
    # that oscillator no matter what phase and width do. The combiner on GP10 is what gets mixed:
    # it can put out either sub on its own as well as any logic combination of the two, so the
    # carrier needs one mixer input for the whole section.
    # A build without the engine (RP2040) keeps the single fixed-50% sub: 'Sub 1 divide' then
    # behaves exactly like 'Sub-oscillator divide' on the Oscillators tab, and everything else on
    # this tab does nothing.
    Param(90, "Sub 1 divide", GROUP_SUB, "combo", default=0, choices=_SUB_DIVIDES,
          note="square on GP8; reaches the mixer through the combiner on GP10",
          models=("dco3",)),
    Param(92, "Sub 1 master", GROUP_SUB, "combo", default=0, choices=_SUB_MASTERS,
          note="which oscillator's reset this sub locks to. Both subs on one master gives "
               "harmonic pulse patterns from the combiner; different masters gives ring-mod "
               "beating that tracks the detune between them",
          models=("dco3",)),
    Param(93, "Sub 1 phase", GROUP_SUB, "slider", 0, 359, 0,
          note="rising edge delayed this many degrees of the MASTER period, not the sub "
               "period - shifting a sub by whole master periods is inaudible",
          models=("dco3",)),
    Param(96, "Sub 1 width", GROUP_SUB, "slider", 1, 255, 128,
          note="duty in 1/256ths of the sub period; 128 is the classic 50% square",
          models=("dco3",)),
    Param(91, "Sub 2 divide", GROUP_SUB, "combo", default=0, choices=_SUB_DIVIDES,
          note="square on GP9", models=("dco3",)),
    Param(95, "Sub 2 master", GROUP_SUB, "combo", default=1, choices=_SUB_MASTERS,
          models=("dco3",)),
    Param(94, "Sub 2 phase", GROUP_SUB, "slider", 0, 359, 0,
          note="the mod matrix's Sub phase destination lands here, on sub 2 alone: moving both "
               "subs together leaves the combined output unchanged",
          models=("dco3",)),
    Param(97, "Sub 2 width", GROUP_SUB, "slider", 1, 255, 128,
          note="the Sub pulse width destination lands here for the same reason",
          models=("dco3",)),
    Param(99, "Logic combiner", GROUP_SUB, "combo", default=0, choices=_SUB_LOGIC_OPS,
          note="the section's output, on GP10: a bitwise combination of the two subs (XOR is "
               "digital ring modulation), or one sub passed straight through. The logic "
               "operators need both subs at a divide above Off to have edges to work with; "
               "'Sub-osc engine report' on the Diagnostics tab shows what it is doing",
          models=("dco3",)),

    # --- Envelopes (curves and routing; times live in the a/b/c blocks) ---
    Param(222, "ADSR1 to VCA", GROUP_ENV, "slider", 0, 512, 512, cc=48),
    Param(126, "EnvDCO (ADSR3) enabled", GROUP_ENV, "check", default=1, cc=24),
    Param(10, "ADSR3 to osc select", GROUP_ENV, "combo", default=0,
          choices=(("0 - OSC1", 0), ("1 - OSC2", 1), ("2 - OSC1+2", 2), ("3 - OSC3", 3), ("4 - all", 4)),
          cc=25),
    Param(47, "ADSR3 to OSC1 detune", GROUP_ENV, "slider", -511, 511, 0, cc=26),
    Param(223, "EnvDCO pitch centered", GROUP_ENV, "check", default=0,
          note="off = unipolar env×depth; on = (env−16384)×2 so mid sustain ≈ note, ±2 oct @ full CW. PW stays unipolar."),
    Param(48, "ADSR1 attack curve", GROUP_ENV, "combo", default=0,
          choices=_ENV_ATTACK_CURVES, cc=27),
    Param(49, "ADSR1 decay curve", GROUP_ENV, "combo", default=0,
          choices=_ENV_DECAY_CURVES, cc=28),
    Param(50, "ADSR2 attack curve", GROUP_ENV, "combo", default=0,
          choices=_ENV_ATTACK_CURVES, cc=29),
    Param(51, "ADSR2 decay curve", GROUP_ENV, "combo", default=0,
          choices=_ENV_DECAY_CURVES, cc=30),
    Param(8, "VCA ADSR restart", GROUP_ENV, "check", default=0, cc=31),
    Param(9, "VCF ADSR restart", GROUP_ENV, "check", default=0, cc=33),

    # --- Filter ---
    Param(19, "VCF keytrack", GROUP_FILTER, "slider", -256, 255, 0, cc=49),
    Param(20, "Velocity to VCF", GROUP_FILTER, "slider", 0, 20, 0, cc=50),
    Param(7, "Resonance amp compensation", GROUP_FILTER, "check", default=0, cc=51),
    Param(54, "Filter mode", GROUP_FILTER, "combo", default=0,
          choices=(("0 - LP24", 0), ("1 - BP12", 1), ("2 - HP6/LP18", 2), ("3 - alt", 3)),
          note="AS3320 multimode (PARAM_FILTER_MODE); GPIO via voice-aux or solo ENABLE_CV_OUTS",
          cc=118),
    Param(52, "Distortion drive", GROUP_FILTER, "slider", 0, 4095, 0,
          note="post-LP Drive VCA CV; needs ENABLE_CV_OUTS + analog stage", cc=81),
    Param(53, "Distortion mix", GROUP_FILTER, "slider", 0, 4095, 0,
          note="0 = dry, 4095 = full wet; post-LP / pre-HP", cc=82),

    # --- PWM ---
    Param(210, "Pulse width", GROUP_PWM, "slider", 0, 4095, 2048,
          note="the DCO stores this as value / 4", cc=59),
    Param(45, "LFO2 to PW", GROUP_PWM, "slider", 0, 511, 0, cc=56),
    Param(46, "ADSR3 to PWM", GROUP_PWM, "slider", 0, 1023, 512,
          note="512 is centre; the DCO subtracts 512 internally", cc=57),
    Param(124, "PWM pots manual", GROUP_PWM, "check", default=1, cc=58),

    # --- LFOs ---
    Param(11, "LFO1 waveform", GROUP_LFO, "combo", default=1,
          choices=(("0 - off", 0), ("1 - saw", 1), ("2 - triangle", 2), ("3 - sine", 3), ("4 - square", 4)),
          cc=60),
    Param(12, "LFO2 waveform", GROUP_LFO, "combo", default=1,
          choices=(("0 - off", 0), ("1 - saw", 1), ("2 - triangle", 2), ("3 - sine", 3), ("4 - square", 4)),
          cc=61),
    Param(41, "LFO1 speed", GROUP_LFO, "slider", 0, 4095, 0, cc=62),
    Param(42, "LFO2 speed", GROUP_LFO, "slider", 0, 4095, 0, cc=63),
    Param(40, "LFO1 to DCO", GROUP_LFO, "slider", 0, 511, 0, cc=65),
    Param(216, "LFO1 to OSC1 extra", GROUP_LFO, "slider", 0, 255, 0, cc=14),
    Param(217, "LFO1 to OSC2 extra", GROUP_LFO, "slider", 0, 255, 0, cc=15),
    Param(218, "LFO1 to OSC3 extra", GROUP_LFO, "slider", 0, 255, 0, cc=19),
    Param(44, "LFO1 to VCA", GROUP_LFO, "slider", 0, 1023, 0, cc=66),
    Param(16, "LFO2 to OSC2 detune", GROUP_LFO, "slider", 0, 255, 0, cc=67),
    Param(35, "LFO2 to OSC3 detune", GROUP_LFO, "slider", 0, 255, 0, cc=68),
    Param(219, "LFO2 to OSC2 coarse", GROUP_LFO, "slider", 0, 511, 0, cc=119),
    # No CC: 120 is the All Sound Off channel-mode message, so a DAW panic button would have
    # slammed this to a value. Nothing else is free (gen_midi_map.py reports 0 remaining), and
    # the mod matrix reaches osc pitch anyway, so it stays panel-only rather than displacing
    # another assignment.
    Param(220, "LFO2 to OSC3 coarse", GROUP_LFO, "slider", 0, 511, 0),

    # --- Mod matrix (ParamIds 60–83; see DCO/docs/MOD_MATRIX.md) ---
    # CCs skip reserved 98–101.
    Param(60, "Mod slot 0 source", GROUP_MOD, "combo", default=255, choices=_MOD_SOURCES, cc=84),
    Param(61, "Mod slot 0 dest", GROUP_MOD, "combo", default=255, choices=_MOD_DESTS, cc=85),
    Param(62, "Mod slot 0 depth", GROUP_MOD, "slider", -4095, 4095, 0, cc=86),
    Param(63, "Mod slot 1 source", GROUP_MOD, "combo", default=255, choices=_MOD_SOURCES, cc=87),
    Param(64, "Mod slot 1 dest", GROUP_MOD, "combo", default=255, choices=_MOD_DESTS, cc=88),
    Param(65, "Mod slot 1 depth", GROUP_MOD, "slider", -4095, 4095, 0, cc=89),
    Param(66, "Mod slot 2 source", GROUP_MOD, "combo", default=255, choices=_MOD_SOURCES, cc=90),
    Param(67, "Mod slot 2 dest", GROUP_MOD, "combo", default=255, choices=_MOD_DESTS, cc=91),
    Param(68, "Mod slot 2 depth", GROUP_MOD, "slider", -4095, 4095, 0, cc=92),
    Param(69, "Mod slot 3 source", GROUP_MOD, "combo", default=255, choices=_MOD_SOURCES, cc=93),
    Param(70, "Mod slot 3 dest", GROUP_MOD, "combo", default=255, choices=_MOD_DESTS, cc=94),
    Param(71, "Mod slot 3 depth", GROUP_MOD, "slider", -4095, 4095, 0, cc=95),
    Param(72, "Mod slot 4 source", GROUP_MOD, "combo", default=255, choices=_MOD_SOURCES, cc=96),
    Param(73, "Mod slot 4 dest", GROUP_MOD, "combo", default=255, choices=_MOD_DESTS, cc=97),
    Param(74, "Mod slot 4 depth", GROUP_MOD, "slider", -4095, 4095, 0, cc=102),
    Param(75, "Mod slot 5 source", GROUP_MOD, "combo", default=255, choices=_MOD_SOURCES, cc=103),
    Param(76, "Mod slot 5 dest", GROUP_MOD, "combo", default=255, choices=_MOD_DESTS, cc=104),
    Param(77, "Mod slot 5 depth", GROUP_MOD, "slider", -4095, 4095, 0, cc=105),
    Param(78, "Mod slot 6 source", GROUP_MOD, "combo", default=255, choices=_MOD_SOURCES, cc=106),
    Param(79, "Mod slot 6 dest", GROUP_MOD, "combo", default=255, choices=_MOD_DESTS, cc=107),
    Param(80, "Mod slot 6 depth", GROUP_MOD, "slider", -4095, 4095, 0, cc=108),
    Param(81, "Mod slot 7 source", GROUP_MOD, "combo", default=255, choices=_MOD_SOURCES, cc=109),
    Param(82, "Mod slot 7 dest", GROUP_MOD, "combo", default=255, choices=_MOD_DESTS, cc=110),
    Param(83, "Mod slot 7 depth", GROUP_MOD, "slider", -4095, 4095, 0, cc=111),

    # --- Character ---
    # Real param; jitter siblings on this tab are diagnostic (PARAM_DEBUG_COMMAND), not PARAMS.
    Param(221, "Character", GROUP_CHARACTER, "slider", 0, 128, 0),

    # --- Calibration ---
    # The two pulses keep cc=None on purpose: autotune takes over the board for a minute
    # and the store writes the filesystem, neither of which should be one stray CC away.
    Param(150, "Run autotune", GROUP_CAL, "pulse", pulse_value=1),
    Param(151, "Manual calibration mode", GROUP_CAL, "check", default=0, cc=78),
    Param(152, "Manual cal stage (osc)", GROUP_CAL, "slider", 0, 2, 0, cc=79),
    Param(153, "Manual cal offset", GROUP_CAL, "slider", -20, 20, 0, cc=80),
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
          _adsr_builder(protocol.CMD_ADSR1_BLOCK),
          note="attack, decay and release are exp-mapped on the wire; sustain is linear"),
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


# Diagnostic buttons, all PARAM_DEBUG_COMMAND (160). See DCO/docs/PIO_OSCILLATORS.md
# section 12. The period probes only hold while no note is playing, because voice_task_main()
# pushes a fresh divider every frame for a held note.
DEBUG_COMMANDS = (
    ("PIO topology report", 1),
    ("Sub-osc engine report", 4),
    ("Period probe, clk_div 2000", 2),
    ("Period probe, clk_div 20000", 3),
    ("Dump RAM (heap/stack)", 13),
    ("Mem diag polls off", 14),
    ("Mem diag polls on", 15),
    ("Note retrig: EXACT_Y", 26),
    ("Note retrig: SYNC_JMP", 27),
)

# PARAM_DEBUG_COMMAND (160). Needs RUNNING_AVERAGE in the firmware; otherwise no-ops.
# See DCO/docs/BENCHMARKING.md.
BENCH_COMMANDS = (
    ("Dump profiler once", 10),
    ("Reset profiler", 11),
    ("Toggle ~1 Hz dump", 12),
)

# Mainboard profiler (PARAM_DEBUG_COMMAND 160), DCO4-REBORN only (has_mainboard).
# DCO forwards 40-42 over Serial2; the STM32 dumps ASCII back as slim 't' chunks
# into the Board output pane. Needs RUNNING_AVERAGE on the Mainboard.
BENCH_MB_COMMANDS = (
    ("Dump Mainboard once", 40),
    ("Reset Mainboard profiler", 41),
    ("Toggle Mainboard ~1 Hz dump", 42),
)

# Amp-comp method + benches (PARAM_DEBUG_COMMAND 160). Method select always acks;
# speed/accuracy need AMP_COMP_BENCHMARK + RUNNING_AVERAGE. See BENCHMARKING.md §8.
AMP_COMP_COMMANDS = (
    ("Amp: FLOAT_QUAD", 20),
    ("Amp: LUT", 21),
    ("Amp: FIXED", 22),
    ("Amp: speed bench", 24),
    ("Amp: accuracy", 25),
)

# Pitch-interp speed/accuracy (PARAM_DEBUG_COMMAND 160). Needs RUNNING_AVERAGE
# for paced Board output. Self-contained private tables — see BENCHMARKING.md.
PITCH_INTERP_COMMANDS = (
    ("Pitch: speed bench", 28),
    ("Pitch: accuracy", 29),
)

# Clkdiv GOLD_REF / GOLD_LIVE / FLOAT_LIVE / Q16 / Q8 / FAST_Q4
# (PARAM_DEBUG_COMMAND 160). Needs RUNNING_AVERAGE. See BENCHMARKING.md §10.
# All six on both voice engines; glue matches live domain. Speed pctVsGOLD_REF.
CLKDIV_HP_COMMANDS = (
    ("Clkdiv: speed bench", 32),
    ("Clkdiv: accuracy", 33),
)

# Calibration-tab debug actions (PARAM_DEBUG_COMMAND 160). Not synth params.
CAL_DEBUG_COMMANDS = (
    ("Seed fake calibration tables", 30),
)

# PIO reset pulse Y (cycles). Sent as unsigned 16-bit on PARAM_DEBUG_COMMAND 160;
# firmware treats values in [PIO_PULSE_LO, PIO_PULSE_HI] as set-pioPulseLength.
PIO_PULSE_LO = 200
PIO_PULSE_HI = 50000
PIO_PULSE_DEFAULT = 1600

# Character-tab diagnostic jitter sliders (not synth params). Sent as unsigned 16-bit
# on PARAM_DEBUG_COMMAND 160: (hi << 8) | amount, amount in 0..128.
# Firmware hi bytes: 0xC8 amp-comp, 0xCA pitch, 0xCB pulsewidth.
CHARACTER_JITTERS = (
    ("Amplitude compensation jitter", 0xC8),
    ("Pitch jitter", 0xCA),
    ("Pulsewidth jitter", 0xCB),
)
CHARACTER_JITTER_LO = 0
CHARACTER_JITTER_HI = 128
CHARACTER_JITTER_DEFAULT = 0

DEBUG_PARAM_ID = 160

# Mod-matrix destination values that only exist with the sub-osc engine
# (models.ModelProfile.has_sub_engine); stripped from dest combos otherwise.
_SUB_ONLY_MOD_DEST_VALUES = {10, 11}
_MOD_DEST_PIDS = {61, 64, 67, 70, 73, 76, 79, 82}


def apply_model(profile: models.ModelProfile) -> None:
    """Bake a model profile into PARAMS / GROUP_ORDER. Call once, before the GUI.

    Drops params the model's firmware doesn't route, marks GUI-hidden ones and
    rewrites labels/choices/notes with the model's wording. Tabs left with no
    params (Sub-osc on DCO4) fall out of GROUP_ORDER.
    """
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
        rebuilt.append(dataclasses.replace(p, **changes) if changes else p)
    PARAMS[:] = rebuilt

    populated = {p.group for p in PARAMS} | {b.group for b in BLOCKS}
    GROUP_ORDER[:] = [g for g in GROUP_ORDER if g in populated]


def visible_params(group: str | None = None) -> list[Param]:
    """PARAMS minus the model-hidden ones, optionally filtered to one tab."""
    return [p for p in PARAMS
            if not p.hidden and (group is None or p.group == group)]
