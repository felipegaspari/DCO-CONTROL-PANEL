"""On-disk JSON formats and MCU binary codecs for patches, banks and calibration.

Three file kinds, all JSON with a "format" tag so they can't be mixed up
(<model> is the active profile key from models.py, "dco3" or "dco4"):

  <model>-patch  one preset slot (same shape presets.py stores in the bank)
  <model>-bank   a whole 256-slot bank
  <model>-cal    the board's five calibration tables, decoded to numbers

Patches and banks share one param numbering across both synths, so either
model's file loads anywhere (params the other synth lacks fall back to
defaults via presets.normalize_bank). Calibration files are strictly
per-model: table sizes follow the oscillator / PW channel counts.

Plus the codec between a host slot dict and the MCU's fixed 598-byte preset
record (DCO/preset_store.h): magic/version, 16-char name, a 256-bit "param was
set" bitmap, 256 int16 params, 16 uint16 block fields, CRC32 (zlib-compatible).

The MCU stores ADSR attack/decay/release in the exp wire domain (0..25000);
host slots keep the linear fader domain (0..4095), so the codec converts.
"""

from __future__ import annotations

import json
import math
import struct
import warnings
import zlib
from pathlib import Path
from typing import Any

import models
import presets
import protocol

FILE_VERSION = 1


def patch_format() -> str:
    return f"{models.active().key}-patch"


def bank_format() -> str:
    return f"{models.active().key}-bank"


def cal_format() -> str:
    return f"{models.active().key}-cal"


_ALL_PATCH_FORMATS = {f"{k}-patch" for k in models.PROFILES}
_ALL_BANK_FORMATS = {f"{k}-bank" for k in models.PROFILES}

# --- MCU preset record layout (DCO/preset_store.h) ---------------------------

RECORD_SIZE = 598
RECORD_MAGIC = 0xA5
RECORD_VERSION = 1
NAME_LEN = 16
OFF_MAGIC = 0
OFF_VERSION = 1
OFF_NAME = 2
OFF_BITMAP = 18
OFF_PARAMS = 50
OFF_BLOCKS = 562
OFF_CRC = 594
PARAM_COUNT = 256

# Record block-field order; exp entries travel the wire exp-mapped (0..25000).
_BLOCK_FIELD_ORDER: tuple[tuple[str, str], ...] = (
    ("adsr_vca", "attack"), ("adsr_vca", "decay"), ("adsr_vca", "sustain"), ("adsr_vca", "release"),
    ("adsr_vcf", "attack"), ("adsr_vcf", "decay"), ("adsr_vcf", "sustain"), ("adsr_vcf", "release"),
    ("adsr_dco", "attack"), ("adsr_dco", "decay"), ("adsr_dco", "sustain"), ("adsr_dco", "release"),
    ("filter", "cutoff"), ("filter", "resonance"), ("filter", "adsr2_to_vcf"), ("filter", "lfo2_to_vcf"),
)
_EXP_FIELDS = {
    (b, f) for b in ("adsr_vca", "adsr_vcf", "adsr_dco") for f in ("attack", "decay", "release")
}


def exp_to_lin(value: int) -> int:
    """Inverse of protocol.lin_to_exp (exp wire domain 0..25000 → fader 0..4095)."""
    value = max(0, min(protocol.ADSR_EXP_MAX, int(value)))
    exp_value = value * (protocol.ADSR_EXP_BASE - 1.0) / protocol.ADSR_EXP_MAX
    normalized = math.log(exp_value + 1.0, protocol.ADSR_EXP_BASE)
    return int(round(normalized * protocol.ADSR_LIN_MAX))


def slot_to_record(slot: dict[str, Any]) -> bytes:
    """Encode a host slot dict into an MCU preset record ready for bulk push."""
    slot = presets.normalize_bank({"slots": [slot]})["slots"][0]
    buf = bytearray(RECORD_SIZE)
    buf[OFF_MAGIC] = RECORD_MAGIC
    buf[OFF_VERSION] = RECORD_VERSION

    name = slot["name"].encode("ascii", errors="replace")[:NAME_LEN]
    buf[OFF_NAME:OFF_NAME + len(name)] = name

    for p in presets.patch_params():
        pid = p.pid
        if not 0 <= pid < PARAM_COUNT:
            continue
        value = int(slot["params"][str(pid)])
        buf[OFF_BITMAP + (pid >> 3)] |= 1 << (pid & 7)
        struct.pack_into("<h", buf, OFF_PARAMS + pid * 2, max(-32768, min(32767, value)))

    for i, (bkey, fkey) in enumerate(_BLOCK_FIELD_ORDER):
        value = int(slot["blocks"][bkey][fkey])
        if (bkey, fkey) in _EXP_FIELDS:
            value = protocol.lin_to_exp(value)
        struct.pack_into("<H", buf, OFF_BLOCKS + i * 2, max(0, min(0xFFFF, value)))

    struct.pack_into("<I", buf, OFF_CRC, zlib.crc32(bytes(buf[:OFF_CRC])))
    return bytes(buf)


def record_to_slot(data: bytes) -> dict[str, Any]:
    """Decode an MCU preset record into a host slot dict. Raises ValueError."""
    if len(data) != RECORD_SIZE:
        raise ValueError(f"record is {len(data)} bytes, expected {RECORD_SIZE}")
    if data[OFF_MAGIC] != RECORD_MAGIC:
        raise ValueError("bad record magic")
    if data[OFF_VERSION] != RECORD_VERSION:
        raise ValueError(f"unsupported record version {data[OFF_VERSION]}")
    (crc,) = struct.unpack_from("<I", data, OFF_CRC)
    if crc != zlib.crc32(data[:OFF_CRC]):
        raise ValueError("record CRC mismatch")

    name = data[OFF_NAME:OFF_NAME + NAME_LEN].split(b"\x00")[0]
    name_str = name.decode("ascii", errors="replace").strip() or "Untitled"

    slot = presets.defaults_slot(name_str)
    for p in presets.patch_params():
        pid = p.pid
        if not 0 <= pid < PARAM_COUNT:
            continue
        if not data[OFF_BITMAP + (pid >> 3)] & (1 << (pid & 7)):
            continue  # never set on the board; keep the host default
        (value,) = struct.unpack_from("<h", data, OFF_PARAMS + pid * 2)
        slot["params"][str(pid)] = int(value)

    blocks_by_key = {b.key: b for b in presets.patch_blocks()}
    for i, (bkey, fkey) in enumerate(_BLOCK_FIELD_ORDER):
        (value,) = struct.unpack_from("<H", data, OFF_BLOCKS + i * 2)
        if (bkey, fkey) in _EXP_FIELDS:
            value = exp_to_lin(value)
        f = next(f for f in blocks_by_key[bkey].fields if f.key == fkey)
        slot["blocks"][bkey][fkey] = max(f.lo, min(f.hi, int(value)))
    return slot


# --- patch / bank files -------------------------------------------------------

def save_patch_file(path: str | Path, slot: dict[str, Any]) -> None:
    slot = presets.normalize_bank({"slots": [slot]})["slots"][0]
    doc = {"format": patch_format(), "version": FILE_VERSION, **slot}
    Path(path).write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


def load_patch_file(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("format") not in _ALL_PATCH_FORMATS:
        raise ValueError(f"not a {patch_format()} file")
    return presets.normalize_bank({"slots": [data]})["slots"][0]


def save_bank_file(path: str | Path, bank: dict[str, Any]) -> None:
    bank = presets.normalize_bank(bank)
    doc = {
        "format": bank_format(),
        "version": FILE_VERSION,
        "current": bank["current"],
        "slots": bank["slots"],
    }
    Path(path).write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


def load_bank_file(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("format") not in _ALL_BANK_FORMATS:
        raise ValueError(f"not a {bank_format()} file")
    return presets.normalize_bank(data)


# --- calibration tables ---------------------------------------------------------
#
# Binary layouts (each firmware's DCO/FS.h, little-endian), sized by the active
# model profile (dco3: 3 osc / 3 PW ch, dco4: 8 osc / 4 PW ch):
#   voiceTables   num_oscillators x 22 pairs of [freq_x100:u32][range_pwm:u32]
#   PWCenter / PWHighLimit / PWLowLimit   num_pw_channels x u16
#   ManualOffset  num_oscillators x i8

AMP_COMP_PAIRS = models.AMP_COMP_PAIRS

# JSON key → LittleFS/dump table name.
CAL_JSON_KEYS: dict[str, str] = {
    "amp_comp": "voiceTables",
    "pw_center": "PWCenter",
    "pw_high_limit": "PWHighLimit",
    "pw_low_limit": "PWLowLimit",
    "manual_offset": "ManualOffset",
}


def _clamp_cal_bytes(name: str, data: bytes, expected: int) -> bytes:
    """Fit a dumped calibration payload to its expected size.

    Fixed firmware (preset_store.ino) already clamps dumps to the compile-time
    bank size, but boards still running older firmware may report the raw
    on-disk LittleFS file size instead, which can be a stale, larger leftover
    from an earlier board revision (e.g. a different oscillator count). The
    leading `expected` bytes are always the real, live data (that's all
    init_FS() ever loads at boot), so truncate rather than fail. A payload
    that's shorter than expected is genuine corruption/short data, so still
    raise for that case.
    """
    if len(data) == expected:
        return data
    if len(data) < expected:
        raise ValueError(f"{name} is {len(data)} bytes, expected {expected}")
    warnings.warn(
        f"{name} dump is {len(data)} bytes, expected {expected}; truncating "
        "stale trailing bytes (reflash firmware to fix at the source)",
        stacklevel=2,
    )
    return data[:expected]


def decode_cal_table(name: str, data: bytes) -> Any:
    """Binary table → JSON-friendly numbers (sizes from the active model)."""
    m = models.active()
    if name == "voiceTables":
        data = _clamp_cal_bytes(name, data, m.voice_tables_size)
        out = []
        for osc in range(m.num_oscillators):
            base = osc * AMP_COMP_PAIRS * 8
            pairs = []
            for i in range(AMP_COMP_PAIRS):
                freq, pwm = struct.unpack_from("<II", data, base + i * 8)
                pairs.append([freq, pwm])
            out.append(pairs)
        return out
    if name in ("PWCenter", "PWHighLimit", "PWLowLimit"):
        data = _clamp_cal_bytes(name, data, m.pw_bank_size)
        return list(struct.unpack(f"<{m.num_pw_channels}H", data))
    if name == "ManualOffset":
        data = _clamp_cal_bytes(name, data, m.manual_offset_size)
        return list(struct.unpack(f"<{m.num_oscillators}b", data))
    raise ValueError(f"unknown calibration table {name}")


def encode_cal_table(name: str, value: Any) -> bytes:
    """JSON numbers → binary table, validating shape and ranges."""
    m = models.active()
    if name == "voiceTables":
        if len(value) != m.num_oscillators or any(len(v) != AMP_COMP_PAIRS for v in value):
            raise ValueError(
                f"amp_comp must be {m.num_oscillators} oscillators x "
                f"{AMP_COMP_PAIRS} [freq_x100, pwm] pairs")
        buf = bytearray()
        for osc in value:
            for freq, pwm in osc:
                buf += struct.pack("<II", int(freq) & 0xFFFFFFFF, int(pwm) & 0xFFFFFFFF)
        return bytes(buf)
    if name in ("PWCenter", "PWHighLimit", "PWLowLimit"):
        if len(value) != m.num_pw_channels:
            raise ValueError(f"{name} needs {m.num_pw_channels} values")
        return struct.pack(f"<{m.num_pw_channels}H",
                           *(max(0, min(0xFFFF, int(v))) for v in value))
    if name == "ManualOffset":
        if len(value) != m.num_oscillators:
            raise ValueError(f"ManualOffset needs {m.num_oscillators} values")
        return struct.pack(f"<{m.num_oscillators}b",
                           *(max(-128, min(127, int(v))) for v in value))
    raise ValueError(f"unknown calibration table {name}")


def save_cal_file(path: str | Path, tables: dict[str, bytes]) -> None:
    """Write dumped binary tables (by LittleFS name) as a <model>-cal JSON file."""
    m = models.active()
    doc: dict[str, Any] = {
        "format": cal_format(),
        "version": FILE_VERSION,
        "num_oscillators": m.num_oscillators,
        "num_pw_channels": m.num_pw_channels,
    }
    for json_key, table_name in CAL_JSON_KEYS.items():
        if table_name in tables:
            doc[json_key] = decode_cal_table(table_name, tables[table_name])
    Path(path).write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


def load_cal_file(path: str | Path) -> dict[str, bytes]:
    """Read a <model>-cal file → binary tables (by LittleFS name) for bulk push.

    Strictly per-model (table sizes differ between the synths). Only the tables
    present in the file are returned, so a partial dump can be restored without
    touching the others.
    """
    m = models.active()
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("format") != cal_format():
        raise ValueError(f"not a {cal_format()} file")
    if int(data.get("num_oscillators", m.num_oscillators)) != m.num_oscillators:
        raise ValueError("num_oscillators mismatch")
    if int(data.get("num_pw_channels", m.num_pw_channels)) != m.num_pw_channels:
        raise ValueError("num_pw_channels mismatch")
    out: dict[str, bytes] = {}
    for json_key, table_name in CAL_JSON_KEYS.items():
        if json_key in data:
            out[table_name] = encode_cal_table(table_name, data[json_key])
    return out
