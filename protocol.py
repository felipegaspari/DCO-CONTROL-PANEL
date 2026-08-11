"""Frame builders for the DCO board's slim inner serial protocol.

The DCO accepts these frames on its USB CDC port when built with ENABLE_USB_CONTROL
(see DCO/Serial.ino). They match serial_input_protocol.h / serial_frame.h: one command
byte plus a fixed little-endian payload. On-wire is RAW by default; wrap with
stuff(..., cobs=True) / use_cobs when firmware has SERIAL_FRAMING_COBS.

ADSR/filter blocks ('a'–'d') stay packed for the 1 ms Input path. Everything else is
a 4-byte 'p' frame: cmd + id + int16 LE. No finish byte, no 'w'/'e'/'f'.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

# Command bytes, from DCO/serial_input_protocol.h.
CMD_ADSR1_BLOCK = b"a"  # EnvVCA times
CMD_ADSR2_BLOCK = b"b"  # EnvVCF times
CMD_ADSR3_BLOCK = b"c"  # EnvDCO times (pitch / PW)
CMD_FILTER_BLOCK = b"d"
CMD_PARAM_16 = b"p"
CMD_PRESET_NAME = b"q"
CMD_BULK_CHUNK = b"B"   # staged restore chunk (DCO/preset_store.h)
CMD_BULK_COMMIT = b"C"  # verify + persist staged bytes

# Preset store / dump command params (DCO/params_def.h).
PARAM_PRESET_SAVE = 170  # value = slot: save the board's live state
PARAM_PRESET_LOAD = 171  # value = slot: recall a slot on the board
PARAM_PRESET_DUMP = 172  # -1 = directory listing, 0..255 = slot record hex
PARAM_CAL_DUMP = 173     # 0/-1 = all cal tables, 1..5 = one

# Bulk restore targets ('B'/'C' first payload byte, DCO/preset_store.h).
BULK_TARGET_PRESET = 0
BULK_TARGET_VOICE_TABLES = 1
BULK_TARGET_PW_CENTER = 2
BULK_TARGET_PW_HIGH_LIMIT = 3
BULK_TARGET_PW_LOW_LIMIT = 4
BULK_TARGET_MANUAL_OFFSET = 5

BULK_CHUNK_DATA = 32

# The USB product string set by USBDevice.setProductDescriptor() in DCO/DCO.ino
# lives in the model profile (models.ModelProfile.usb_product_prefix); the
# descriptor is space-padded, so it is matched as a prefix.

# USB CDC ignores the line rate, but pyserial requires one. Mirror the firmware's
# Serial.begin(2000000) for symmetry.
BAUD = 2000000

# On-wire delimiter when SERIAL_FRAMING_COBS is on. Never a command byte.
DELIMITER = 0x00

# Module flag: wrap inner frames as COBS+0x00. Set from app.py --cobs / DCO_SERIAL_COBS.
use_cobs = False

# Envelope lin->exp mapping, from INPUT-CONTROLLER/auxiliary.h.
ADSR_LIN_MAX = 4095
ADSR_EXP_MAX = 25000
ADSR_EXP_BASE = 50.0


def lin_to_exp(linear: int) -> int:
    """Replicate linearToExponential(linear, 50, 25000) from the Input board.

    Envelope attack/decay/release travel the wire already exp-mapped, so applying
    the same curve here makes a host slider behave like the physical fader.
    Sustain is sent linear and must not go through this.
    """
    linear = max(0, min(ADSR_LIN_MAX, int(linear)))
    normalized = linear / float(ADSR_LIN_MAX)
    exp_value = ADSR_EXP_BASE**normalized - 1.0
    max_exp_value = ADSR_EXP_BASE - 1.0
    return int(exp_value * (ADSR_EXP_MAX / max_exp_value))


def param16(param_id: int, value: int) -> bytes:
    """'p' frame: 16-bit parameter, little-endian. Negative values are two's complement."""
    return CMD_PARAM_16 + bytes([param_id & 0xFF]) + struct.pack("<h", _clamp16(value))


def param16u(param_id: int, value: int) -> bytes:
    """'p' frame with unsigned 16-bit value (0..65535). Firmware reinterprets as uint16_t."""
    return CMD_PARAM_16 + bytes([param_id & 0xFF]) + struct.pack("<H", _clampu16(value))


def adsr_block(cmd: bytes, attack: int, decay: int, sustain: int, release: int) -> bytes:
    """'a'/'b'/'c' frame. attack/decay/release are exp-domain (0..25000), sustain linear.

    Callers should pass values already run through lin_to_exp() for the three times.
    """
    return cmd + struct.pack(
        "<HHHH", _clampu16(attack), _clampu16(decay), _clampu16(sustain), _clampu16(release)
    )


def filter_block(cutoff: int, resonance: int, adsr2_to_vcf: int, lfo2_to_vcf: int) -> bytes:
    """'d' frame. cutoff/resonance are 0..4095; the two mod amounts are 0..512.

    The Input board inverts its cutoff and resonance pots when reading the ADC, but
    that is a pot-wiring detail: on the wire, larger simply means more.
    """
    return CMD_FILTER_BLOCK + struct.pack(
        "<HHHH",
        _clampu16(cutoff),
        _clampu16(resonance),
        _clampu16(adsr2_to_vcf),
        _clampu16(lfo2_to_vcf),
    )


def preset_name(name: str) -> bytes:
    """'q' frame: 16 ASCII chars, space-padded."""
    padded = name.encode("ascii", errors="replace")[:16].ljust(16, b" ")
    return CMD_PRESET_NAME + padded


def bulk_chunk(target: int, slot: int, offset: int, data: bytes) -> bytes:
    """'B' frame: stage 32 bytes at offset in the board's bulk buffer."""
    payload = bytes(data)[:BULK_CHUNK_DATA].ljust(BULK_CHUNK_DATA, b"\x00")
    return CMD_BULK_CHUNK + struct.pack("<BBH", target & 0xFF, slot & 0xFF, offset & 0xFFFF) + payload


def bulk_commit(target: int, slot: int, size: int, crc32: int) -> bytes:
    """'C' frame: verify CRC32 over the staged bytes and persist to LittleFS."""
    return CMD_BULK_COMMIT + struct.pack(
        "<BBHI", target & 0xFF, slot & 0xFF, size & 0xFFFF, crc32 & 0xFFFFFFFF
    )


def bulk_frames(target: int, slot: int, data: bytes) -> list[bytes]:
    """All 'B' chunks plus the final 'C' commit for one blob."""
    blob = bytes(data)
    frames = [
        bulk_chunk(target, slot, off, blob[off:off + BULK_CHUNK_DATA])
        for off in range(0, len(blob), BULK_CHUNK_DATA)
    ]
    frames.append(bulk_commit(target, slot, len(blob), zlib.crc32(blob)))
    return frames


def cobs_encode(data: bytes) -> bytes:
    """Consistent Overhead Byte Stuffing. Output never contains 0x00."""
    src = bytes(data)
    out = bytearray()
    code_idx = 0
    out.append(0)
    code = 1
    n = len(src)
    for i, b in enumerate(src):
        if b == 0:
            out[code_idx] = code
            code_idx = len(out)
            out.append(0)
            code = 1
        else:
            out.append(b)
            code += 1
            if code == 0xFF:
                out[code_idx] = code
                if i + 1 < n:
                    code_idx = len(out)
                    out.append(0)
                    code = 1
    out[code_idx] = code
    return bytes(out)


def stuff(inner: bytes, cobs: bool | None = None) -> bytes:
    """Wrap an inner frame for the wire. RAW copy, or COBS + 0x00."""
    if cobs is None:
        cobs = use_cobs
    blob = bytes(inner)
    if not cobs:
        return blob
    return cobs_encode(blob) + bytes([DELIMITER])


def _clamp16(v: int) -> int:
    return max(-32768, min(32767, int(v)))


def _clampu16(v: int) -> int:
    return max(0, min(0xFFFF, int(v)))


@dataclass
class PortInfo:
    device: str
    description: str


def find_dco_ports() -> list[PortInfo]:
    """Return candidate serial ports, active-model boards first.

    Matches the USB product descriptor the firmware advertises. Falls back to
    listing everything so a board with a stale descriptor is still reachable.
    """
    import models
    from serial.tools import list_ports

    prefix = models.active().usb_product_prefix.lower()
    matches: list[PortInfo] = []
    others: list[PortInfo] = []
    for p in list_ports.comports():
        info = PortInfo(p.device, p.description or "")
        product = (getattr(p, "product", None) or "") + " " + (p.description or "")
        if prefix in product.lower():
            matches.append(info)
        else:
            others.append(info)
    return matches + others
