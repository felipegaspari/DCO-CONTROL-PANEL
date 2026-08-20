#!/usr/bin/env python3
"""Emit the MIDI CC map, its chart, and an Open Stage Control panel from params.py.

params.py is the single source of truth for the DCO control surface.
This script produces:
  - DCO/_shared/midi_cc_map.h
  - DCO/docs/MIDI_CC_MAP.md
  - DCO/tools/panels/<model>_panel.json

Usage:
  python3 gen_midi_map.py           # write the outputs
  python3 gen_midi_map.py --check   # validate and check drift (exit 1 on drift)
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from html import escape
import json
from pathlib import Path
import re
import sys

import models
import params
import protocol

# Controllers left untouched for standard MIDI / DAW operations:
RESERVED_CC = {
    0, 1, 6, 7, 10, 11, 32, 38, 42, 64, 98, 99, 100, 101,
    120, 121, 122, 123, 124, 125, 126, 127
}

MIDI_CHANNEL = 1
MIDI_TARGET = "midi:dco"
SESSION_VERSION = "1.30.0"

CELL_WIDTH = 132
ROW_UNIT = 30
CELL_ROWS = 5
VALUE_HEIGHT = 20

GROUP_ACCENT = {
    params.GROUP_OSC: "#dda44a",
    params.GROUP_SUB: "#c2825b",
    params.GROUP_ENV: "#6fbf8b",
    params.GROUP_FILTER: "#d1685f",
    params.GROUP_PWM: "#b98bd1",
    params.GROUP_LFO: "#4fb3c4",
    params.GROUP_MOD: "#a67c52",
    params.GROUP_CHARACTER: "#d99b79",
    params.GROUP_CAL: "#8d97a3",
}

CURVE_LINEAR = "MIDI_CC_LINEAR"
CURVE_EXP_TIME = "MIDI_CC_EXP_TIME"
GENERATED_BY = "DCO-CONTROL-PANEL/gen_midi_map.py from DCO-CONTROL-PANEL/params.py"

# --- Directory & File Topology ---------------------------------------------------

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent

# Sibling directories at the project root
DCO_DIR = PROJECT_ROOT / "DCO"
PROTOCOL_DIR = PROJECT_ROOT / "DCO-PROTOCOL"

# Target header (prefers DCO/_shared/ if present, otherwise DCO/)
MAP_HEADER = (
    DCO_DIR / "_shared" / "midi_cc_map.h"
    if (DCO_DIR / "_shared").is_dir()
    else DCO_DIR / "midi_cc_map.h"
)

# Target Markdown chart (prefers DCO/docs/ if present, otherwise DCO/)
CHART = (
    DCO_DIR / "docs" / "MIDI_CC_MAP.md"
    if (DCO_DIR / "docs").is_dir()
    else DCO_DIR / "MIDI_CC_MAP.md"
)


def panel_path() -> Path:
    """Target Open Stage Control panel JSON path."""
    target_dir = DCO_DIR / "tools" / "panels"
    if not target_dir.is_dir():
        target_dir = HERE / "panels"
    return target_dir / models.active().panel_filename


def detect_firmware_model() -> str | None:
    """Read USB product descriptor from DCO/Serial.ino (or DCO/DCO.ino)."""
    source_path = DCO_DIR / "Serial.ino"
    if not source_path.exists():
        source_path = DCO_DIR / "DCO.ino"
    
    try:
        source = source_path.read_text(encoding="utf-8")
    except OSError:
        source = ""
    
    m = re.search(r'setProductDescriptor\("([^"]+)"\)', source)
    if m:
        product = m.group(1).strip().lower()
        for profile in models.PROFILES.values():
            if product.startswith(profile.usb_product_prefix.lower()):
                return profile.key
    return models.detect_from_project()


# --- Resilient Firmware Readers --------------------------------------------------

def read_param_ids() -> tuple[dict[int, str], set[str]]:
    """Read ParamId enum and PERSISTABLE_PARAMS array from DCO-PROTOCOL/params_def.h."""
    proto_header = PROTOCOL_DIR / "params_def.h"
    if not proto_header.exists():
        proto_header = DCO_DIR / "params_def.h"
    
    header = proto_header.read_text(encoding="utf-8")
    
    # 1. Parse Enum ID -> Name
    enum_by_id: dict[int, str] = {}
    for name, value in re.findall(r"^\s*(PARAM_\w+)\s*=\s*(\d+)", header, re.M):
        enum_by_id[int(value)] = name

    # 2. Parse PERSISTABLE_PARAMS[] list
    persistable: set[str] = set()
    m = re.search(r"PERSISTABLE_PARAMS\s*\[\s*\]\s*=\s*\{([^}]+)\}", header, re.S)
    if m:
        persistable = set(re.findall(r"\b(PARAM_\w+)\b", m.group(1)))
    else:
        persistable = set(enum_by_id.values())

    return enum_by_id, persistable
    """Read all valid enum constants from DCO-PROTOCOL/params_def.h."""
    proto_header = PROTOCOL_DIR / "params_def.h"
    if not proto_header.exists():
        proto_header = DCO_DIR / "params_def.h"
    
    header = proto_header.read_text(encoding="utf-8")
    enum_by_id: dict[int, str] = {}
    for name, value in re.findall(r"^\s*(PARAM_\w+)\s*=\s*(\d+)", header, re.M):
        enum_by_id[int(value)] = name

    # Returns the 2-tuple expected by main()
    return enum_by_id, set(enum_by_id.values())


def validate(entries: list[Entry], enum_by_id: dict[int, str], persistable: set[str] | None = None) -> list[str]:
    problems: list[str] = []
    seen: dict[int, str] = {}

    for e in entries:
        if not 0 <= e.cc <= 127:
            problems.append(f"CC {e.cc} out of range ({e.label})")
        if e.cc in RESERVED_CC:
            problems.append(f"CC {e.cc} is reserved ({e.label})")
        if e.cc in seen:
            problems.append(f"CC {e.cc} used twice: {seen[e.cc]} and {e.label}")
        seen[e.cc] = e.label
        if e.hi <= e.lo:
            problems.append(f"CC {e.cc} has an empty range {e.lo}..{e.hi} ({e.label})")

    for p in params.PARAMS:
        if p.kind == "pulse" and p.cc is not None:
            problems.append(f"parameter {p.pid} ({p.label}) is a command and must not have a CC")
        if p.cc is None:
            continue
        name = enum_by_id.get(p.pid)
        if name is None:
            problems.append(f"parameter {p.pid} ({p.label}) is not in the params_def.h enum")

    declared, handled = read_local_targets()
    for e in entries:
        if not e.is_local:
            continue
        if e.target not in declared:
            problems.append(f"{e.target} is not declared in midi_cc.h ({e.label})")
        if e.target not in handled:
            problems.append(f"{e.target} has no case in midi_cc_apply() ({e.label})")
    for name in sorted(declared - {e.target for e in entries if e.is_local}):
        problems.append(f"{name} is declared in midi_cc.h but no CC maps to it")

    problems.extend(validate_protocol(enum_by_id))
    return problems
    problems: list[str] = []
    seen: dict[int, str] = {}

    for e in entries:
        if not 0 <= e.cc <= 127:
            problems.append(f"CC {e.cc} out of range ({e.label})")
        if e.cc in RESERVED_CC:
            problems.append(f"CC {e.cc} is reserved ({e.label})")
        if e.cc in seen:
            problems.append(f"CC {e.cc} used twice: {seen[e.cc]} and {e.label}")
        seen[e.cc] = e.label
        if e.hi <= e.lo:
            problems.append(f"CC {e.cc} has an empty range {e.lo}..{e.hi} ({e.label})")

    for p in params.PARAMS:
        if p.kind == "pulse" and p.cc is not None:
            problems.append(f"parameter {p.pid} ({p.label}) is a command and must not have a CC")
        if p.cc is None:
            continue
        name = enum_by_id.get(p.pid)
        if name is None:
            problems.append(f"parameter {p.pid} ({p.label}) is not in the params_def.h enum")
        elif name not in persistable:
            problems.append(f"parameter {p.pid} ({name}) is not in PERSISTABLE_PARAMS in params_def.h")

    declared, handled = read_local_targets()
    for e in entries:
        if not e.is_local:
            continue
        if e.target not in declared:
            problems.append(f"{e.target} is not declared in midi_cc.h ({e.label})")
        if e.target not in handled:
            problems.append(f"{e.target} has no case in midi_cc_apply() ({e.label})")
    for name in sorted(declared - {e.target for e in entries if e.is_local}):
        problems.append(f"{name} is declared in midi_cc.h but no CC maps to it")

    problems.extend(validate_protocol(enum_by_id))
    return problems
    problems: list[str] = []
    seen: dict[int, str] = {}

    for e in entries:
        if not 0 <= e.cc <= 127:
            problems.append(f"CC {e.cc} out of range ({e.label})")
        if e.cc in RESERVED_CC:
            problems.append(f"CC {e.cc} is reserved ({e.label})")
        if e.cc in seen:
            problems.append(f"CC {e.cc} used twice: {seen[e.cc]} and {e.label}")
        seen[e.cc] = e.label
        if e.hi <= e.lo:
            problems.append(f"CC {e.cc} has an empty range {e.lo}..{e.hi} ({e.label})")

    for p in params.PARAMS:
        if p.kind == "pulse" and p.cc is not None:
            problems.append(f"parameter {p.pid} ({p.label}) is a command and must not have a CC")
        if p.cc is None:
            continue
        name = enum_by_id.get(p.pid)
        if name is None:
            problems.append(f"parameter {p.pid} ({p.label}) is not in the params_def.h enum")

    declared, handled = read_local_targets()
    for e in entries:
        if not e.is_local:
            continue
        if e.target not in declared:
            problems.append(f"{e.target} is not declared in midi_cc.h ({e.label})")
        if e.target not in handled:
            problems.append(f"{e.target} has no case in midi_cc_apply() ({e.label})")
    for name in sorted(declared - {e.target for e in entries if e.is_local}):
        problems.append(f"{name} is declared in midi_cc.h but no CC maps to it")

    problems.extend(validate_protocol(enum_by_id))
    return problems
    problems: list[str] = []
    seen: dict[int, str] = {}

    for e in entries:
        if not 0 <= e.cc <= 127:
            problems.append(f"CC {e.cc} out of range ({e.label})")
        if e.cc in RESERVED_CC:
            problems.append(f"CC {e.cc} is reserved ({e.label})")
        if e.cc in seen:
            problems.append(f"CC {e.cc} used twice: {seen[e.cc]} and {e.label}")
        seen[e.cc] = e.label
        if e.hi <= e.lo:
            problems.append(f"CC {e.cc} has an empty range {e.lo}..{e.hi} ({e.label})")

    for p in params.PARAMS:
        if p.kind == "pulse" and p.cc is not None:
            problems.append(f"parameter {p.pid} ({p.label}) is a command and must not have a CC")
        if p.cc is None:
            continue
        name = enum_by_id.get(p.pid)
        if name is None:
            problems.append(f"parameter {p.pid} ({p.label}) is not in the params_def.h enum")

    declared, handled = read_local_targets()
    for e in entries:
        if not e.is_local:
            continue
        if e.target not in declared:
            problems.append(f"{e.target} is not declared in midi_cc.h ({e.label})")
        if e.target not in handled:
            problems.append(f"{e.target} has no case in midi_cc_apply() ({e.label})")
    for name in sorted(declared - {e.target for e in entries if e.is_local}):
        problems.append(f"{name} is declared in midi_cc.h but no CC maps to it")

    problems.extend(validate_protocol(enum_by_id))
    return problems


def read_local_targets() -> tuple[set[str], set[str]]:
    """CC_LOCAL_* names declared in midi_cc.h, and those handled in midi.ino."""
    midi_cc_header = DCO_DIR / "_shared" / "midi_cc.h"
    if not midi_cc_header.exists():
        midi_cc_header = DCO_DIR / "midi_cc.h"

    declared = set(re.findall(r"(CC_LOCAL_\w+)", midi_cc_header.read_text(encoding="utf-8")))
    declared.discard("CC_LOCAL_FIRST")

    midi_ino = DCO_DIR / "midi.ino"
    if not midi_ino.exists():
        midi_ino = DCO_DIR / "DCO.ino"
        
    handled = set(re.findall(r"case\s+(CC_LOCAL_\w+)\s*:", midi_ino.read_text(encoding="utf-8")))
    return declared, handled


def read_protocol_header() -> tuple[dict[str, int], dict[str, int]]:
    """Parse exact CMD_* opcodes and SERIAL_LEN_* constants from serial_input_protocol.h."""
    proto_header = PROTOCOL_DIR / "serial_input_protocol.h"
    if not proto_header.exists():
        proto_header = DCO_DIR / "serial_input_protocol.h"

    text = proto_header.read_text(encoding="utf-8")

    # 1. Parse base dimensions (e.g. PRESET_NAME_LEN = 16)
    constants: dict[str, int] = {}
    for name, val in re.findall(r"(?:constexpr\s+\w+\s+|\b)(\w+_LEN|\w+_COUNT)\s*=\s*(\d+)", text):
        constants[name] = int(val)
    constants.setdefault("PRESET_NAME_LEN", 16)

    # 2. Parse command opcodes (e.g. CMD_PARAM_16 = 'p')
    commands: dict[str, int] = {}
    for name, char in re.findall(r"(?:constexpr\s+\w+\s+|\b)(INPUT_CMD_\w+|CMD_\w+)\s*=\s*'(.)'", text):
        commands[name] = ord(char)
        if name.startswith("INPUT_"):
            commands[name[6:]] = ord(char)
        else:
            commands["INPUT_" + name] = ord(char)

    # 3. Parse exact SERIAL_LEN_* payload sizes
    lengths: dict[str, int] = {}
    for line in text.splitlines():
        m = re.search(r"(?:constexpr\s+\w+\s+|\b)(SERIAL_LEN_\w+|INPUT_SERIAL_LEN_\w+)\s*=\s*([^;]+);", line)
        if not m:
            continue
        name, expr = m.group(1), m.group(2).strip()
        if expr.isdigit():
            lengths[name] = int(expr)
        elif expr in constants:
            lengths[name] = constants[expr]
        else:
            # Match inline comments like `// 16`
            comment_m = re.search(r"//\s*(\d+)", line)
            if comment_m:
                lengths[name] = int(comment_m.group(1))

    return commands, lengths

    
@dataclass
class Entry:
    cc: int
    target: str
    lo: int
    hi: int
    curve: str
    label: str
    group: str
    kind: str
    note: str = ""
    choices: tuple = ()
    unreachable: tuple = ()
    is_local: bool = False
    section: str = ""
    short_label: str = ""
    default: int = 0


def cc_to_native(cc: int, lo: int, hi: int, curve: str) -> int:
    value = lo + ((hi - lo) * cc + 63) // 127
    if curve == CURVE_EXP_TIME:
        value = protocol.lin_to_exp(value)
    return value


def local_target(block: params.Block, field_: params.BlockField) -> str:
    return f"CC_LOCAL_{block.key}_{field_.key}".upper()


def param_target(pid: int, enum_by_id: dict[int, str]) -> str:
    return enum_by_id.get(pid, str(pid))


def param_range(p: params.Param) -> tuple[int, int, str]:
    if p.kind == "combo":
        return 0, 127, "menu"
    if p.kind == "check":
        return 0, 1, "switch"
    return p.lo, p.hi, "knob"


def build_entries(enum_by_id: dict[int, str]) -> list[Entry]:
    entries: list[Entry] = []
    for group in params.GROUP_ORDER:
        for p in params.PARAMS:
            if p.group != group or p.cc is None:
                continue
            lo, hi, kind = param_range(p)
            entry = Entry(
                cc=p.cc,
                target=param_target(p.pid, enum_by_id),
                lo=lo,
                hi=hi,
                curve=CURVE_LINEAR,
                label=p.label,
                group=group,
                kind=kind,
                note=p.note,
                short_label=p.label,
                default=p.default,
            )
            if p.kind == "combo":
                reachable = []
                unreachable = []
                for choice_label, value in p.choices:
                    if 0 <= value <= 127 and cc_to_native(value, lo, hi, entry.curve) == value:
                        reachable.append((choice_label, value))
                    else:
                        unreachable.append((choice_label, value))
                entry.choices = tuple(reachable)
                entry.unreachable = tuple(unreachable)
            entries.append(entry)

        for block in params.BLOCKS:
            if block.group != group:
                continue
            for field_ in block.fields:
                if field_.cc is None:
                    continue
                entries.append(
                    Entry(
                        cc=field_.cc,
                        target=local_target(block, field_),
                        lo=field_.lo,
                        hi=field_.hi,
                        curve=CURVE_EXP_TIME if field_.exp else CURVE_LINEAR,
                        label=f"{block.label}: {field_.label}",
                        group=group,
                        kind="knob",
                        note=block.note if field_ is block.fields[0] else "",
                        is_local=True,
                        section=block.label,
                        short_label=field_.label,
                        default=field_.default,
                    )
                )
    return entries

def sample_frames() -> dict[str, bytes]:
    """Sample frames keyed by their exact canonical SERIAL_LEN_* constants."""
    return {
        "SERIAL_LEN_ADSR_BLOCK": protocol.adsr_block(protocol.CMD_ADSR1_BLOCK, 0, 0, 0, 0),
        "SERIAL_LEN_FILTER_BLOCK": protocol.filter_block(0, 0, 0, 0),
        "SERIAL_LEN_PARAM_16": protocol.param16(0, 0),
        "SERIAL_LEN_PRESET_NAME": protocol.preset_name(""),
        "SERIAL_LEN_BULK_CHUNK": protocol.bulk_chunk(0, 0, 0, b""),
        "SERIAL_LEN_BULK_COMMIT": protocol.bulk_commit(0, 0, 0, 0),
    }

def validate_protocol(enum_by_id: dict[int, str]) -> list[str]:
    """Check protocol.py against serial_input_protocol.h and params_def.h."""
    problems: list[str] = []
    commands, lengths = read_protocol_header()

    for name in sorted(n for n in dir(protocol) if n.startswith("CMD_")):
        value = getattr(protocol, name)
        if not isinstance(value, bytes):
            continue
        if name not in commands:
            problems.append(f"protocol.{name} is missing from serial_input_protocol.h")
        elif commands[name] != value[0]:
            problems.append(
                f"protocol.{name} is {value!r} but serial_input_protocol.h has '{chr(commands[name])}'"
            )

    for length_name, frame in sample_frames().items():
        expected = lengths.get(length_name)
        if expected is None:
            problems.append(f"{length_name} is missing from serial_input_protocol.h")
        elif len(frame) - 1 != expected:
            problems.append(
                f"protocol.py builds a {len(frame) - 1}-byte payload for {chr(frame[0])!r} "
                f"but {length_name} is {expected}"
            )

    for name in (
        "PARAM_PRESET_SAVE",
        "PARAM_PRESET_LOAD",
        "PARAM_PRESET_DUMP",
        "PARAM_CAL_DUMP",
        "PARAM_UI_PRESET_SCROLL",
    ):
        pid = getattr(protocol, name)
        if enum_by_id.get(pid) != name:
            problems.append(
                f"protocol.{name} is {pid}, which is {enum_by_id.get(pid) or 'unused'} in params_def.h"
            )

    return problems

def emit_map_header(entries: list[Entry]) -> str:
    width = max(len(e.target) for e in entries) + 1
    lines = [
        "#ifndef __MIDI_CC_MAP_H__",
        "#define __MIDI_CC_MAP_H__",
        "",
        "// GENERATED FILE - do not edit.",
        f"// Emitted by {GENERATED_BY}.",
        "// Re-run that script after changing params.py; see docs/MIDI_CC_MAP.md for the chart.",
        "//",
        "// cc, target, lo, hi, curve. CC 0 lands on lo and CC 127 on hi; targets at or above",
        "// CC_LOCAL_FIRST are block values that midi_cc_apply() writes directly.",
        "",
        '#include <stddef.h>',
        '#include "../params_def.h"',
        '#include "midi_cc.h"',
        "",
        "static const MidiCcEntry midiCcMap[] = {",
    ]
    current_group = None
    for e in entries:
        if e.group != current_group:
            lines.append(f"  // --- {e.group} ---")
            current_group = e.group
        lines.append(
            f"  {{ {e.cc:3d}, {e.target + ',':<{width}} {e.lo:5d}, {e.hi:5d}, {e.curve} }},"
        )
    lines += [
        "};",
        "",
        "static const size_t midiCcMapSize = sizeof(midiCcMap) / sizeof(midiCcMap[0]);",
        "",
        "#endif",
        "",
    ]
    return "\n".join(lines)


def emit_chart(entries: list[Entry]) -> str:
    out: list[str] = [
        "# MIDI CC implementation chart",
        "",
        "Generated from `DCO-CONTROL-PANEL/params.py` by `DCO-CONTROL-PANEL/gen_midi_map.py`. "
        "Do not edit by hand.",
        "",
        "Every control the bench app exposes is reachable from a 7-bit CC on any channel "
        "(the DCO listens omni), over USB MIDI or the DIN input. The board does not send "
        "anything back, so a panel should push its state after connecting.",
        "",
        "A controller value scales into the parameter's native range as",
        "",
        "```",
        "value = lo + ((hi - lo) * cc + 63) / 127",
        "```",
        "",
        "so CC 0 lands on `CC 0` below and CC 127 on `CC 127`. Envelope attack, decay and "
        "release then go through `linearToExponential(value, 50, 25000)`, the same curve the "
        "Input board applies to its faders, because the `'a'`-`'c'` block frames carry those "
        "values already exp-mapped.",
        "",
        "## Map",
        "",
        "| CC | Control | Group | Target | CC 0 | CC 127 | Curve |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for e in entries:
        curve = "exp" if e.curve == CURVE_EXP_TIME else "linear"
        out.append(
            f"| {e.cc} | {e.label} | {e.group} | `{e.target}` | "
            f"{cc_to_native(0, e.lo, e.hi, e.curve)} | "
            f"{cc_to_native(127, e.lo, e.hi, e.curve)} | {curve} |"
        )

    menus = [e for e in entries if e.kind == "menu"]
    if menus:
        out += ["", "## Menu values", "",
                "These parameters take discrete values; the CC number to send is the value "
                "itself.", ""]
        for e in menus:
            values = ", ".join(f"{label} = {value}" for label, value in e.choices)
            out.append(f"- **CC {e.cc}, {e.label}**: {values}")
            if e.note:
                out.append(f"  - {e.note}")
            if e.unreachable:
                missing = ", ".join(f"{label} ({value})" for label, value in e.unreachable)
                out.append(f"  - out of 7-bit reach, use the serial bench app instead: {missing}")

    skipped = [p for p in params.PARAMS if p.cc is None]
    out += ["", "## Deliberately not mapped", ""]
    if models.active().has_sub_engine:
        out += [
            "Every non-reserved 7-bit controller is already assigned (0 free). Sub-oscillator "
            "ParamIds 93–101 and LFO2→OSC3 coarse therefore stay panel/serial only; continuous "
            "sub shape still reaches the board through mod-matrix destinations 10/11 "
            "(`MOD_DEST_SUB_PHASE` / `MOD_DEST_SUB_PW`, which land on sub 2).",
            "",
        ]
    else:
        out += [
            "These parameters stay panel/serial only:",
            "",
        ]
    for p in skipped:
        out.append(f"- **{p.label}** (parameter {p.pid})")
    out += [
        "",
        "Autotune takes the board over for about a minute and the store writes the "
        "filesystem, so neither should be one stray controller away. Both are still "
        "available from the serial bench app in `DCO-CONTROL-PANEL`.",
        "",
        "Reserved controllers left untouched: "
        + ", ".join(str(c) for c in sorted(RESERVED_CC))
        + ". CC 0 / CC 32 are Bank Select: nonzero latches bank 1 so the next Program "
        "Change recalls slots 128..255 (`midi.ino`). CC 42 keeps its historical meaning "
        "here, pitch-bend range in semitones. 98-101 stay free so a later NRPN upgrade "
        "needs no reshuffling. CC 120 (All Sound Off) is reserved and is why LFO2→OSC3 "
        "coarse has no assignment.",
        "",
    ]
    return "\n".join(out)


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def js_number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else repr(value)


def widget_id(e: Entry) -> str:
    return f"cc{e.cc}_{slug(e.short_label)}"


def cc_for_native(e: Entry, native: int) -> int:
    cc = round((native - e.lo) * 127 / (e.hi - e.lo))
    return max(0, min(127, cc))


def readout_js(e: Entry) -> str:
    linear = f"Math.floor(({e.hi - e.lo} * Math.round(@{{{widget_id(e)}}}) + 63) / 127)"
    if e.lo:
        linear = f"{e.lo} + {linear}"
    if e.curve == CURVE_EXP_TIME:
        base = js_number(protocol.ADSR_EXP_BASE)
        return (f"#{{ Math.floor((Math.pow({base}, ({linear}) / {protocol.ADSR_LIN_MAX}) - 1)"
                f" * ({protocol.ADSR_EXP_MAX} / {js_number(protocol.ADSR_EXP_BASE - 1)})) }}")
    return f"#{{ {linear} }}"


def section_header(e: Entry) -> dict:
    return {
        "type": "text",
        "id": "head_" + slug(e.section or e.group),
        "value": (e.section or e.group).upper(),
        "align": "left bottom",
        "css": "grid-column: 1 / -1; font-weight: bold; font-size: 90%; opacity: 0.55;",
    }


def panel_cell(e: Entry) -> dict:
    rows = []
    control = {
        "id": widget_id(e),
        "address": "/control",
        "preArgs": [MIDI_CHANNEL, e.cc],
        "target": MIDI_TARGET,
        "default": cc_for_native(e, e.default),
        "expand": True,
        "html": escape(e.short_label),
        "css": "> .html { white-space: normal; line-height: 1.15em; font-size: 85%; }",
    }
    if e.kind == "menu":
        rows.append({
            "type": "menu",
            **control,
            "values": {f"{label} ": value for label, value in e.choices},
        })
    elif e.kind == "switch":
        rows.append({"type": "switch", **control, "values": {"Off ": 0, "On ": 127}})
    else:
        knob = {
            "type": "knob",
            **control,
            "range": {"min": 0, "max": 127},
            "decimals": 0,
            "doubleTap": True,
            "pips": False,
        }
        if e.lo < 0:
            knob["origin"] = cc_for_native(e, 0)
        rows.append(knob)
        rows.append({
            "type": "text",
            "id": "val_" + widget_id(e),
            "value": readout_js(e),
            "height": VALUE_HEIGHT,
            "css": "font-size: 95%; opacity: 0.65;",
        })

    return {
        "type": "panel",
        "id": "cell_" + widget_id(e),
        "layout": "vertical",
        "scroll": False,
        "innerPadding": False,
        "padding": 4,
        "css": f"grid-row: span {CELL_ROWS};",
        "widgets": rows,
    }


def emit_panel(entries: list[Entry]) -> str:
    tabs = []
    for group in params.GROUP_ORDER:
        in_group = [e for e in entries if e.group == group]
        if not in_group:
            continue
        widgets = []
        section = ""
        for e in in_group:
            if e.section != section:
                section = e.section
                widgets.append(section_header(e))
            widgets.append(panel_cell(e))
        tabs.append({
            "type": "tab",
            "id": "tab_" + slug(group),
            "label": group,
            "layout": "grid",
            "gridTemplate": f"none / repeat(auto-fill, minmax({CELL_WIDTH}px, 1fr))",
            "css": f"> inner {{ grid-auto-rows: {ROW_UNIT}rem; }}",
            "scroll": True,
            "padding": 10,
            "colorWidget": GROUP_ACCENT.get(group, "#8d97a3"),
            "widgets": widgets,
        })

    session = {
        "version": SESSION_VERSION,
        "type": "session",
        "content": {
            "type": "root",
            "id": "root",
            "width": 1280,
            "height": 860,
            "colorBg": "#16181d",
            "colorText": "#dbe0e6",
            "tabs": tabs,
        },
    }
    return json.dumps(session, indent=2) + "\n"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="validate and report drift without writing")
    parser.add_argument("--model", choices=sorted(models.PROFILES), help="synth model (default: auto-detect)")
    args = parser.parse_args(argv)

    global MIDI_TARGET
    model = args.model or detect_firmware_model()
    if model is None:
        print("error: cannot determine synth model; pass --model dco3|dco4", file=sys.stderr)
        return 1
    profile = models.set_active(model)
    params.apply_model(profile)
    MIDI_TARGET = profile.midi_target

    enum_by_id, routed = read_param_ids()
    entries = build_entries(enum_by_id)
    problems = validate(entries, enum_by_id, routed)
    if problems:
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        return 1

    outputs = {
        MAP_HEADER: emit_map_header(entries),
        CHART: emit_chart(entries),
        panel_path(): emit_panel(entries),
    }

    stale = []
    for path, text in outputs.items():
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current == text:
            continue
        stale.append(path)
        if not args.check:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    for e in entries:
        for label, value in e.unreachable:
            print(f"note: CC cannot reach '{label}' ({value}) on CC {e.cc}, {e.label}")

    if args.check:
        for path in stale:
            try:
                rel = path.relative_to(DCO_DIR)
            except ValueError:
                rel = path
            print(f"error: {rel} is out of date", file=sys.stderr)
        if stale:
            return 1
        print(f"up to date: {len(entries)} controllers mapped across {models.active().display_name}")
        return 0

    for path in stale:
        try:
            rel = path.relative_to(DCO_DIR)
        except ValueError:
            rel = path
        print(f"wrote {rel}")
    print(
        f"{len(entries)} controllers mapped, {len(RESERVED_CC)} reserved, "
        f"{128 - len(RESERVED_CC) - len(entries)} free"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))