"""256-slot program bank for the DCO bench controller.

Stores patch parameters only (no calibration, no pulse/command params). The bank
is one JSON file per synth model (presets/bank_dco3.json / bank_dco4.json) next
to this module so it travels with the tool; a pre-model-aware presets/bank.json
is adopted by the first model that runs without its own file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import models
import params

NUM_SLOTS = 256
BANK_VERSION = 1
PRESETS_DIR = Path(__file__).resolve().parent / "presets"
LEGACY_BANK_PATH = PRESETS_DIR / "bank.json"


def bank_path() -> Path:
    return PRESETS_DIR / models.active().bank_filename


def patch_params() -> list[params.Param]:
    return [p for p in params.PARAMS if p.kind != "pulse" and p.group != params.GROUP_CAL]


def patch_blocks() -> list[params.Block]:
    return [b for b in params.BLOCKS if b.group != params.GROUP_CAL]


def defaults_slot(name: str = "Init") -> dict[str, Any]:
    param_map = {str(p.pid): int(p.default) for p in patch_params()}
    block_map = {
        b.key: {f.key: int(f.default) for f in b.fields}
        for b in patch_blocks()
    }
    return {"name": name, "params": param_map, "blocks": block_map}


def slot_is_empty(slot: Any) -> bool:
    return slot is None or not isinstance(slot, dict)


def empty_bank(current: int = 0) -> dict[str, Any]:
    slots: list[Any] = [None] * NUM_SLOTS
    slots[0] = defaults_slot("Init")
    return {"version": BANK_VERSION, "current": current, "slots": slots}


def normalize_bank(data: Any) -> dict[str, Any]:
    """Coerce on-disk JSON into a well-formed bank; fill gaps with nulls."""
    if not isinstance(data, dict):
        return empty_bank()

    slots_in = data.get("slots")
    if not isinstance(slots_in, list):
        return empty_bank()

    slots: list[Any] = []
    for i in range(NUM_SLOTS):
        if i < len(slots_in) and isinstance(slots_in[i], dict):
            slots.append(_normalize_slot(slots_in[i]))
        else:
            slots.append(None)

    if slots[0] is None:
        slots[0] = defaults_slot("Init")

    try:
        current = int(data.get("current", 0))
    except (TypeError, ValueError):
        current = 0
    current = max(0, min(NUM_SLOTS - 1, current))

    return {"version": BANK_VERSION, "current": current, "slots": slots}


def _normalize_slot(slot: dict) -> dict[str, Any]:
    name = slot.get("name")
    if not isinstance(name, str) or not name.strip():
        name = "Untitled"
    else:
        name = name.strip()[:48]

    raw_params = slot.get("params") if isinstance(slot.get("params"), dict) else {}
    raw_blocks = slot.get("blocks") if isinstance(slot.get("blocks"), dict) else {}

    # Start from defaults, overlay known keys so missing fields stay valid.
    out = defaults_slot(name)
    for p in patch_params():
        key = str(p.pid)
        if key in raw_params:
            try:
                out["params"][key] = int(raw_params[key])
            except (TypeError, ValueError):
                pass

    # Pre-slim bank.json stored PW / EnvVCA→VCA as 'e'/'f' blocks.
    _legacy_block_to_param = {
        ("adsr1_to_vca", "amount"): "222",
        ("pw", "pw"): "210",
    }
    for (bkey, fkey), pid in _legacy_block_to_param.items():
        src = raw_blocks.get(bkey)
        if pid in out["params"] and pid not in raw_params and isinstance(src, dict) and fkey in src:
            try:
                out["params"][pid] = int(src[fkey])
            except (TypeError, ValueError):
                pass

    for b in patch_blocks():
        src = raw_blocks.get(b.key)
        if not isinstance(src, dict):
            continue
        for f in b.fields:
            if f.key in src:
                try:
                    out["blocks"][b.key][f.key] = int(src[f.key])
                except (TypeError, ValueError):
                    pass
    return out


def load_bank(path: Path | None = None) -> dict[str, Any]:
    path = path or bank_path()
    if not path.is_file() and LEGACY_BANK_PATH.is_file():
        # One-time adoption of the pre-model-aware bank.json: it was written by
        # this tool copy for whichever synth this project is, i.e. this model.
        try:
            data = json.loads(LEGACY_BANK_PATH.read_text(encoding="utf-8"))
            bank = normalize_bank(data)
            save_bank(bank, path)
            return bank
        except (OSError, json.JSONDecodeError):
            pass
    if not path.is_file():
        bank = empty_bank()
        save_bank(bank, path)
        return bank
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        bank = empty_bank()
        save_bank(bank, path)
        return bank
    return normalize_bank(data)


def save_bank(bank: dict[str, Any], path: Path | None = None) -> None:
    path = path or bank_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = normalize_bank(bank)
    path.write_text(
        json.dumps(normalized, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def capture(param_vars: dict, block_vars: dict, get_param_value) -> dict[str, Any]:
    """Snapshot UI state into a slot dict (name filled by the caller)."""
    param_map = {}
    for p in patch_params():
        param_map[str(p.pid)] = int(get_param_value(p))
    block_map = {}
    for b in patch_blocks():
        fields = {}
        for f in b.fields:
            var = block_vars[b.key][f.key]
            try:
                fields[f.key] = int(round(float(var.get())))
            except (TypeError, ValueError):
                fields[f.key] = int(f.default)
        block_map[b.key] = fields
    return {"name": "Untitled", "params": param_map, "blocks": block_map}


def apply(param_vars: dict, block_vars: dict, slot: dict | None) -> None:
    """Write a slot (or Init defaults if empty) into the UI variables."""
    data = defaults_slot() if slot_is_empty(slot) else _normalize_slot(slot)  # type: ignore[arg-type]

    for p in patch_params():
        var = param_vars.get(p.pid)
        if var is None:
            continue
        value = int(data["params"].get(str(p.pid), p.default))
        if p.kind == "combo":
            label = next((c[0] for c in p.choices if c[1] == value), None)
            if label is None:
                label = next(c[0] for c in p.choices if c[1] == p.default)
            var.set(label)
        elif p.kind == "check":
            var.set(1 if value else 0)
        else:
            var.set(value)

    for b in patch_blocks():
        for f in b.fields:
            value = int(data["blocks"].get(b.key, {}).get(f.key, f.default))
            block_vars[b.key][f.key].set(value)


def slot_fingerprint(slot: dict | None) -> str:
    """Stable string for dirty detection (ignores name)."""
    data = defaults_slot() if slot_is_empty(slot) else _normalize_slot(slot)  # type: ignore[arg-type]
    payload = {"params": data["params"], "blocks": data["blocks"]}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))
