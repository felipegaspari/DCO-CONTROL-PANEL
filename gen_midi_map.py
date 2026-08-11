#!/usr/bin/env python3
"""Emit the MIDI CC map, its chart and an Open Stage Control panel from params.py.

params.py is the one description of the DCO's control surface, so the firmware table,
the implementation chart and the panel session are all generated from it and cannot
drift apart. Three outputs:

  ../../midi_cc_map.h              the MidiCcEntry table, included by DCO/midi_cc.h
  ../../docs/MIDI_CC_MAP.md        the implementation chart
  ../panels/<model>_panel.json     the Open Stage Control session

The synth model (models.py) is read from the firmware this tool copy sits in —
the USBDevice.setProductDescriptor() string in ../../Serial.ino — so each
project's copy targets its own board; --model overrides.

Usage:
  python3 gen_midi_map.py           write the three files
  python3 gen_midi_map.py --check   validate and diff only, exit 1 on drift

The checks are the interesting part: they catch a CC collision, a reserved controller,
a parameter that params.py claims but the firmware does not route, a block value whose
CC_LOCAL_* target is not handled in midi.ino, and a combo entry that 7-bit CC cannot
express exactly.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from html import escape
from pathlib import Path

import models
import params
import protocol

# Controllers left alone: 0/32 bank select (implemented in midi.ino: nonzero = bank 1
# for Program Change → slots 128..255), 1 mod wheel, 6/38 data entry, 7 volume,
# 10 pan, 11 expression, 42 pitch-bend range (the DCO's own historical use), 64 sustain,
# 98-101 NRPN/RPN, and 120-127 channel mode. Keeping 98-101 free also leaves room for a
# later NRPN upgrade without moving any assignment made here.
RESERVED_CC = {0, 1, 6, 7, 10, 11, 32, 38, 42, 64, 98, 99, 100, 101,
               120, 121, 122, 123, 124, 125, 126, 127}

MIDI_CHANNEL = 1
# Open Stage Control target; per model, set by main() (midi:dco3 / midi:dco4).
MIDI_TARGET = "midi:dco3"

# A session without a version is treated as pre-0.49.12 and run through every legacy
# converter, which quietly rewrites properties on the way in: decimals is replaced by the
# long-gone precision, colorWidget by color, and every container with widgets has its
# padding forced to 0. The newest converter is 1.24.2, so anything above that is left
# alone. Open Stage Control stamps its own version here when it saves.
SESSION_VERSION = "1.30.0"

# Panel geometry. The grid reflows to the window, so the width is a minimum per column.
# Heights have to come from the grid itself: the app's stylesheet forces height to auto
# on every direct child of a grid container, so a cell cannot size itself. Rows are one
# ROW_UNIT tall and a cell spans CELL_ROWS of them; a section header takes a single row.
CELL_WIDTH = 132
ROW_UNIT = 30
CELL_ROWS = 5
VALUE_HEIGHT = 20

# One muted accent per tab, so a knob's colour says which section it belongs to.
GROUP_ACCENT = {
    params.GROUP_OSC: "#dda44a",
    params.GROUP_ENV: "#6fbf8b",
    params.GROUP_FILTER: "#d1685f",
    params.GROUP_PWM: "#b98bd1",
    params.GROUP_LFO: "#4fb3c4",
    params.GROUP_MOD: "#a67c52",
    params.GROUP_CAL: "#8d97a3",
}

CURVE_LINEAR = "MIDI_CC_LINEAR"
CURVE_EXP_TIME = "MIDI_CC_EXP_TIME"

HERE = Path(__file__).resolve().parent
DCO_DIR = HERE.parents[1]

MAP_HEADER = DCO_DIR / "midi_cc_map.h"
CHART = DCO_DIR / "docs" / "MIDI_CC_MAP.md"


def panel_path() -> Path:
    return DCO_DIR / "tools" / "panels" / models.active().panel_filename


def detect_firmware_model() -> str | None:
    """Read the USB product descriptor from ../../Serial.ino and match a profile."""
    try:
        source = (DCO_DIR / "Serial.ino").read_text()
    except OSError:
        return None
    m = re.search(r'setProductDescriptor\("([^"]+)"\)', source)
    if not m:
        return None
    product = m.group(1).strip().lower()
    for profile in models.PROFILES.values():
        if product.startswith(profile.usb_product_prefix.lower()):
            return profile.key
    return None

GENERATED_BY = "tools/dco_control/gen_midi_map.py from tools/dco_control/params.py"


@dataclass
class Entry:
    """One row of the CC map, in both firmware and panel terms."""

    cc: int
    target: str  # C expression: a PARAM_* name or a CC_LOCAL_* code
    lo: int
    hi: int
    curve: str
    label: str  # full name, with the block prefix, for the chart
    group: str
    kind: str  # knob, menu or switch
    note: str = ""
    choices: tuple = ()  # (label, native value) pairs, menus only
    unreachable: tuple = ()  # choices 7-bit CC cannot express
    is_local: bool = False
    section: str = ""  # block label, or "" for a plain parameter
    short_label: str = ""  # name without the block prefix, for the panel cell
    default: int = 0  # native default, pre-exp for the envelope times


def cc_to_native(cc: int, lo: int, hi: int, curve: str) -> int:
    """Mirror of the scaling in midi_cc_handle(), so the chart cannot lie."""
    value = lo + ((hi - lo) * cc + 63) // 127
    if curve == CURVE_EXP_TIME:
        value = protocol.lin_to_exp(value)
    return value


def local_target(block: params.Block, field_: params.BlockField) -> str:
    return f"CC_LOCAL_{block.key}_{field_.key}".upper()


def param_target(pid: int, enum_by_id: dict[int, str]) -> str:
    return enum_by_id.get(pid, str(pid))


def param_range(p: params.Param) -> tuple[int, int, str]:
    """lo, hi and widget kind for a parameter.

    Combos map a CC straight onto the native value (lo 0, hi 127 makes the scaling an
    identity), which is what lets a menu entry pick an exact value such as sub-osc
    divide 2. Checks scale to 0..1 so a CC below 64 is off and 64 or above is on.
    """
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
            entry = Entry(cc=p.cc, target=param_target(p.pid, enum_by_id), lo=lo, hi=hi,
                          curve=CURVE_LINEAR, label=p.label, group=group, kind=kind,
                          note=p.note, short_label=p.label, default=p.default)
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
                entries.append(Entry(
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
                ))
    return entries


# --- firmware cross-checks --------------------------------------------------------


def read_param_ids() -> tuple[dict[int, str], set[str]]:
    """(id -> PARAM_* name) from params_def.h, and the names routed by paramTable[]."""
    header = (DCO_DIR / "params_def.h").read_text()
    enum_by_id: dict[int, str] = {}
    for name, value in re.findall(r"^\s*(PARAM_\w+)\s*=\s*(\d+)", header, re.M):
        enum_by_id[int(value)] = name

    table = (DCO_DIR / "params.ino").read_text()
    body = table.split("static const ParamDescriptorT<int16_t> paramTable[]", 1)
    routed = set(re.findall(r"\{\s*(PARAM_\w+),", body[-1]))
    return enum_by_id, routed


def read_local_targets() -> tuple[set[str], set[str]]:
    """CC_LOCAL_* names declared in midi_cc.h, and those handled in midi.ino."""
    declared = set(re.findall(r"(CC_LOCAL_\w+)", (DCO_DIR / "midi_cc.h").read_text()))
    declared.discard("CC_LOCAL_FIRST")
    handled = set(re.findall(r"case\s+(CC_LOCAL_\w+)\s*:", (DCO_DIR / "midi.ino").read_text()))
    return declared, handled


def validate(entries: list[Entry], enum_by_id: dict[int, str], routed: set[str]) -> list[str]:
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
        elif name not in routed:
            problems.append(f"parameter {p.pid} ({name}) is not routed by paramTable[]")

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

    return problems


# --- emitters ---------------------------------------------------------------------


def emit_map_header(entries: list[Entry]) -> str:
    width = max(len(e.target) for e in entries) + 1  # room for the comma
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
        '#include "params_def.h"',
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
        "Generated from `tools/dco_control/params.py` by `tools/dco_control/gen_midi_map.py`. "
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
        "Menu-style parameters use a 0..127 range so the scaling is an identity and a menu "
        "entry can pick an exact native value. Switches scale to 0..1, so under 64 is off "
        "and 64 or over is on.",
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
            "ParamIds 90–99 and LFO2→OSC3 coarse therefore stay panel/serial only; continuous "
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
        "available from the serial bench app in `tools/dco_control`.",
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
            # A number would become "none / repeat(n, 1fr)"; a string goes straight into
            # the css grid-template shorthand, which is rows first, hence the "none /".
            "gridTemplate": f"none / repeat(auto-fill, minmax({CELL_WIDTH}px, 1fr))",
            # Row height lives here because the cells are not allowed to set their own.
            # The child selector matters: the same rule on every nested container would
            # reach the cells, whose inner is a flex box that reads these properties too.
            "css": f"> inner {{ grid-auto-rows: {ROW_UNIT}rem; }}",
            "scroll": True,
            "padding": 10,
            "colorWidget": GROUP_ACCENT[group],
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


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def js_number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else repr(value)


def widget_id(e: Entry) -> str:
    """Short and stable: the CC number alone already makes it unique."""
    return f"cc{e.cc}_{slug(e.short_label)}"


def cc_for_native(e: Entry, native: int) -> int:
    """Inverse of the CC scaling, for the widget's default and gauge origin.

    Envelope times invert in the linear domain, because lo..hi is what the exp curve
    is fed, not what it returns.
    """
    cc = round((native - e.lo) * 127 / (e.hi - e.lo))
    return max(0, min(127, cc))


def readout_js(e: Entry) -> str:
    """A `#{}` expression giving the native value the DCO will hold for this CC.

    Mirrors cc_to_native(), so the number under the knob is the number in the chart.
    `#{}` prepends a return, so this has to stay one expression. Math.round comes
    first because `decimals: 0` is what the knob actually sends.
    """
    linear = f"Math.floor(({e.hi - e.lo} * Math.round(@{{{widget_id(e)}}}) + 63) / 127)"
    if e.lo:
        linear = f"{e.lo} + {linear}"
    if e.curve == CURVE_EXP_TIME:
        # linearToExponential(v, 50, 25000). Grouping the constant division the same way
        # protocol.lin_to_exp() does keeps both sides on the same double.
        base = js_number(protocol.ADSR_EXP_BASE)
        return (f"#{{ Math.floor((Math.pow({base}, ({linear}) / {protocol.ADSR_LIN_MAX}) - 1)"
                f" * ({protocol.ADSR_EXP_MAX} / {js_number(protocol.ADSR_EXP_BASE - 1)})) }}")
    return f"#{{ {linear} }}"


def section_header(e: Entry) -> dict:
    """A row that spans the whole grid, naming the block the next cells belong to."""
    return {
        "type": "text",
        "id": "head_" + slug(e.section or e.group),
        "value": (e.section or e.group).upper(),
        "align": "left bottom",
        "css": "grid-column: 1 / -1; font-weight: bold; font-size: 90%; opacity: 0.55;",
    }


def panel_cell(e: Entry) -> dict:
    """One grid cell: the name, the control, and the native value it is sending.

    The value needs a widget of its own: a slider cannot show its own value, because
    `label` is not one of the dynamic properties and a widget may not feed its own value
    into a property that is not.
    """
    rows = []
    control = {
        "id": widget_id(e),
        "address": "/control",
        "preArgs": [MIDI_CHANNEL, e.cc],
        "target": MIDI_TARGET,
        "default": cc_for_native(e, e.default),
        "expand": True,
        # The name goes in `html`, not `label`: only button, dropdown, menu, modal, tab
        # and xy still have a label, and on a menu it is the value readout. The default
        # line-height for that element is a full row, so names have to be told to wrap.
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
        # The knob carries controller numbers because that is what /control sends; the
        # text below it translates them back into what the parameter is worth.
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


# --- driver -----------------------------------------------------------------------


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="validate and report drift without writing anything")
    parser.add_argument("--model", choices=sorted(models.PROFILES),
                        help="synth model (default: read from ../../Serial.ino)")
    args = parser.parse_args(argv)

    global MIDI_TARGET
    model = args.model or detect_firmware_model()
    if model is None:
        print("error: cannot tell which synth this firmware is; pass --model",
              file=sys.stderr)
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
        current = path.read_text() if path.exists() else None
        if current == text:
            continue
        stale.append(path)
        if not args.check:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    for e in entries:
        for label, value in e.unreachable:
            print(f"note: CC cannot reach '{label}' ({value}) on CC {e.cc}, {e.label}")

    if args.check:
        for path in stale:
            print(f"error: {path.relative_to(DCO_DIR)} is out of date", file=sys.stderr)
        if stale:
            return 1
        print(f"up to date: {len(entries)} controllers")
        return 0

    for path in stale:
        print(f"wrote {path.relative_to(DCO_DIR)}")
    print(f"{len(entries)} controllers mapped, "
          f"{len(RESERVED_CC)} reserved, "
          f"{128 - len(RESERVED_CC) - len(entries)} free")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
