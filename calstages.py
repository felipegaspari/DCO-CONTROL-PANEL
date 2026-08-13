"""The manual-calibration stage walk, as the firmware defines it.

PARAM_MANUAL_CALIBRATION_STAGE (152) counts substages, not oscillators: DCO3
walks saw / pulse / 440 Hz on each of its three oscillators (0..8), DCO4 packs
seven per voice pair (0..27) — A gets saw / triangle / pulse-with-PW / 440, B
gets saw / pulse / 440. Which oscillator a stage belongs to, and which encoder
is live on it, only come out of that walk.

This is a port of the cal_stage_*_n() helpers in DCO-PROTOCOL/params_def.h and
of cal_pw_channel() in DCO-SHARED-LIBRARIES/autotune.h, reading the oscillator
and PW-channel counts from the active model profile. Keep it in step with those
headers: they are what the board actually runs.
"""

from __future__ import annotations

import models

# CalStageKind (params_def.h).
KIND_SAW = 0
KIND_TRI = 1
KIND_PULSE = 2
KIND_PULSE_PW = 3
KIND_440 = 4

_KIND_LABELS = {
    KIND_SAW: "Saw",
    KIND_TRI: "Triangle",
    KIND_PULSE: "Pulse",
    KIND_PULSE_PW: "Pulse (PW)",
    KIND_440: "440 Hz",
}

# Substage order per oscillator on the packed (DCO4) walk.
_PACKED_A_KINDS = (KIND_SAW, KIND_TRI, KIND_PULSE_PW, KIND_440)
_PACKED_B_KINDS = (KIND_SAW, KIND_PULSE, KIND_440)
# Uniform (DCO3) walk.
_UNIFORM_KINDS = (KIND_SAW, KIND_PULSE, KIND_440)


def _nosc() -> int:
    return models.active().num_oscillators


def is_packed() -> bool:
    """DCO4's A4+B3 walk, as opposed to DCO3's uniform three per oscillator."""
    return _nosc() > 3


def stage_count() -> int:
    n = _nosc()
    return n * 3 if n <= 3 else (n // 2) * 7


def stage_max() -> int:
    n = stage_count()
    return 0 if n == 0 else n - 1


def _packed_split(stage: int) -> tuple[int, int]:
    """Packed stage → (voice pair, offset within its seven substages)."""
    n_voice = _nosc() // 2
    voice = 0
    remain = stage
    while remain >= 7 and voice + 1 < n_voice:
        remain -= 7
        voice += 1
    return voice, remain


def stage_to_osc(stage: int) -> int:
    n = _nosc()
    if n == 0:
        return 0
    if n <= 3:
        return min(stage // 3, n - 1)
    voice, remain = _packed_split(stage)
    if remain < 4:
        return voice * 2
    return min(voice * 2 + 1, n - 1)


def stage_kind(stage: int) -> int:
    if _nosc() <= 3:
        return {1: KIND_PULSE, 2: KIND_440}.get(stage % 3, KIND_SAW)
    _voice, remain = _packed_split(stage)
    if remain < 4:
        return {1: KIND_TRI, 2: KIND_PULSE_PW, 3: KIND_440}.get(remain, KIND_SAW)
    return {1: KIND_PULSE, 2: KIND_440}.get(remain - 4, KIND_SAW)


def kinds_for_osc(osc: int) -> tuple[int, ...]:
    if not is_packed():
        return _UNIFORM_KINDS
    return _PACKED_A_KINDS if osc % 2 == 0 else _PACKED_B_KINDS


def stage_for(osc: int, kind: int) -> int:
    """(oscillator, substage) → stage value, by searching the forward walk.

    Inverting the packing by hand would be a second place to get it wrong; the
    walk is 28 entries at most.
    """
    for stage in range(stage_count()):
        if stage_to_osc(stage) == osc and stage_kind(stage) == kind:
            return stage
    return 0


def pw_channel(osc: int) -> int:
    """PW channel driven by an oscillator (cal_pw_channel in autotune.h)."""
    m = models.active()
    if m.num_pw_channels == m.num_oscillators:
        return osc
    return osc // (m.num_oscillators // m.num_pw_channels)


def osc_label(osc: int) -> str:
    """Screen wording: DCO4 voice+chip ("0A", "1B"), DCO3 1-based ("OSC1")."""
    if is_packed():
        return f"{osc // 2}{'B' if osc % 2 else 'A'}"
    return f"OSC{osc + 1}"


def kind_label(kind: int) -> str:
    return _KIND_LABELS.get(kind, "Saw")


def stage_text(stage: int) -> str:
    """One-line description for the stage readout."""
    return (f"stage {stage} — {osc_label(stage_to_osc(stage))} "
            f"{kind_label(stage_kind(stage))}")
