"""Request/response manager for the MCU preset & calibration protocol.

The board answers over the same USB CDC text stream the log pane shows, using
structured lines ([dump], [pdir], [preset], [bulk] — see DCO/preset_store.h).
The app feeds every complete line into feed_line(); operations are queued and
run one at a time, each with a timeout, so board debug chatter interleaving the
answer is simply skipped over.

All callbacks fire in the Tk thread (feed_line/tick are called from app timers)
as on_done(ok: bool, payload) where payload is the operation's result on
success or a short reason string on failure.
"""

from __future__ import annotations

import re
import time
import zlib
from typing import Callable

import models
import protocol

OP_TIMEOUT_S = 6.0


def cal_tables() -> dict[str, tuple[int, int, int]]:
    """Calibration table name → (PARAM_CAL_DUMP selector, bulk target, size).

    Sizes mirror the active model's DCO/FS.h (models.ModelProfile), so this is
    looked up per call rather than baked in at import time.
    """
    m = models.active()
    return {
        "voiceTables": (1, protocol.BULK_TARGET_VOICE_TABLES, m.voice_tables_size),
        "PWCenter": (2, protocol.BULK_TARGET_PW_CENTER, m.pw_bank_size),
        "PWHighLimit": (3, protocol.BULK_TARGET_PW_HIGH_LIMIT, m.pw_bank_size),
        "PWLowLimit": (4, protocol.BULK_TARGET_PW_LOW_LIMIT, m.pw_bank_size),
        "ManualOffset": (5, protocol.BULK_TARGET_MANUAL_OFFSET, m.manual_offset_size),
    }

_RE_DUMP_BEGIN = re.compile(r"^\[dump\] begin target=(\S+)(?: slot=(\d+))? size=(\d+)")
_RE_DUMP_DATA = re.compile(r"^\[dump\] d ([0-9A-Fa-f]{4}) ([0-9A-Fa-f]+)")
_RE_DUMP_END = re.compile(r"^\[dump\] end target=(\S+) crc=([0-9A-Fa-f]{1,8})")
_RE_DUMP_ERR = re.compile(r"^\[dump\] err target=(\S+) reason=(\S+)")
_RE_PDIR_BEGIN = re.compile(r"^\[pdir\] begin")
_RE_PDIR_SLOT = re.compile(r"^\[pdir\] slot=(\d+) name=\"(.*)\"")
_RE_PDIR_END = re.compile(r"^\[pdir\] end count=(\d+)")
_RE_BULK_OK = re.compile(r"^\[bulk\] ok target=(\S+)")
_RE_BULK_ERR = re.compile(r"^\[bulk\] err target=(\S+) reason=(\S+)")
_RE_PRESET_OK = re.compile(r"^\[preset\] (saved|loaded) slot=(\d+)")
_RE_PRESET_ERR = re.compile(r"^\[preset\] err slot=(\d+) reason=(\S+)")


class _Op:
    def __init__(self, kind: str, frames: list[bytes], on_done: Callable,
                 *, target: str | None = None, timeout: float = OP_TIMEOUT_S) -> None:
        self.kind = kind          # "dump" | "pdir" | "bulk" | "preset"
        self.frames = frames
        self.on_done = on_done
        self.target = target      # expected target= token for dump/bulk
        self.timeout = timeout
        self.deadline = 0.0
        # dump collection state
        self.buf: bytearray | None = None
        self.size = 0
        # pdir collection state
        self.entries: list[tuple[int, str]] = []
        self.in_pdir = False


class McuLink:
    """Serialized MCU operations over one send callable and a text line tap."""

    def __init__(self, send_frame: Callable[[bytes], None], log: Callable[[str], None]) -> None:
        self._send = send_frame  # takes an inner frame; caller handles stuffing
        self._log = log
        self._queue: list[_Op] = []
        self._active: _Op | None = None

    @property
    def busy(self) -> bool:
        return self._active is not None or bool(self._queue)

    # --- public operations -------------------------------------------------

    def read_directory(self, on_done: Callable) -> None:
        """[pdir] listing → on_done(True, list[(slot, name)])."""
        self._enqueue(_Op(
            "pdir",
            [protocol.param16(protocol.PARAM_PRESET_DUMP, -1)],
            on_done,
        ))

    def dump_preset_slot(self, slot: int, on_done: Callable) -> None:
        """Slot record hex dump → on_done(True, bytes[598])."""
        self._enqueue(_Op(
            "dump",
            [protocol.param16(protocol.PARAM_PRESET_DUMP, slot)],
            on_done,
            target="preset",
        ))

    def dump_cal_table(self, name: str, on_done: Callable) -> None:
        """One calibration table → on_done(True, bytes)."""
        sel = cal_tables()[name][0]
        self._enqueue(_Op(
            "dump",
            [protocol.param16(protocol.PARAM_CAL_DUMP, sel)],
            on_done,
            target=name,
        ))

    def push_preset_record(self, slot: int, record: bytes, on_done: Callable) -> None:
        """Write one 598-byte record into an MCU slot → on_done(True, None)."""
        self._enqueue(_Op(
            "bulk",
            protocol.bulk_frames(protocol.BULK_TARGET_PRESET, slot, record),
            on_done,
            target="preset",
        ))

    def push_cal_table(self, name: str, data: bytes, on_done: Callable) -> None:
        """Write one calibration table (board reloads it) → on_done(True, None)."""
        _sel, target_id, size = cal_tables()[name]
        if len(data) != size:
            on_done(False, f"{name}: expected {size} bytes, got {len(data)}")
            return
        self._enqueue(_Op(
            "bulk",
            protocol.bulk_frames(target_id, 0, data),
            on_done,
            target=name,
        ))

    def save_live_to_slot(self, slot: int, name: str, on_done: Callable) -> None:
        """Board snapshots its own live state into a slot ('q' name + save)."""
        self._enqueue(_Op(
            "preset",
            [protocol.preset_name(name), protocol.param16(protocol.PARAM_PRESET_SAVE, slot)],
            on_done,
        ))

    def recall_slot(self, slot: int, on_done: Callable) -> None:
        """Board recalls one of its own slots."""
        self._enqueue(_Op(
            "preset",
            [protocol.param16(protocol.PARAM_PRESET_LOAD, slot)],
            on_done,
        ))

    # --- plumbing ------------------------------------------------------------

    def cancel_all(self) -> None:
        """Drop everything (e.g. on disconnect); pending callbacks get a failure."""
        ops = ([self._active] if self._active else []) + self._queue
        self._active = None
        self._queue = []
        for op in ops:
            op.on_done(False, "cancelled")

    def tick(self) -> None:
        """Timeout check; call periodically from a UI timer."""
        if self._active is not None and time.monotonic() > self._active.deadline:
            self._finish(False, "timeout")

    def feed_line(self, line: str) -> None:
        op = self._active
        if op is None:
            return
        line = line.rstrip("\r\n")
        if op.kind == "dump":
            self._feed_dump(op, line)
        elif op.kind == "pdir":
            self._feed_pdir(op, line)
        elif op.kind == "bulk":
            self._feed_bulk(op, line)
        elif op.kind == "preset":
            self._feed_preset(op, line)

    # --- per-kind line handling --------------------------------------------

    def _feed_dump(self, op: _Op, line: str) -> None:
        m = _RE_DUMP_BEGIN.match(line)
        if m and m.group(1) == op.target:
            op.size = int(m.group(3))
            op.buf = bytearray(op.size)
            return
        m = _RE_DUMP_DATA.match(line)
        if m and op.buf is not None:
            off = int(m.group(1), 16)
            data = bytes.fromhex(m.group(2))
            if off + len(data) <= op.size:
                op.buf[off:off + len(data)] = data
            return
        m = _RE_DUMP_END.match(line)
        if m and m.group(1) == op.target and op.buf is not None:
            want = int(m.group(2), 16)
            got = zlib.crc32(bytes(op.buf))
            if got == want:
                self._finish(True, bytes(op.buf))
            else:
                self._finish(False, f"crc mismatch ({got:08X} != {want:08X})")
            return
        m = _RE_DUMP_ERR.match(line)
        if m and m.group(1) == op.target:
            self._finish(False, m.group(2))

    def _feed_pdir(self, op: _Op, line: str) -> None:
        if _RE_PDIR_BEGIN.match(line):
            op.in_pdir = True
            op.entries = []
            return
        if not op.in_pdir:
            return
        m = _RE_PDIR_SLOT.match(line)
        if m:
            op.entries.append((int(m.group(1)), m.group(2).strip()))
            return
        if _RE_PDIR_END.match(line):
            self._finish(True, op.entries)

    def _feed_bulk(self, op: _Op, line: str) -> None:
        m = _RE_BULK_OK.match(line)
        if m and m.group(1) == op.target:
            self._finish(True, None)
            return
        m = _RE_BULK_ERR.match(line)
        if m and m.group(1) == op.target:
            self._finish(False, m.group(2))

    def _feed_preset(self, op: _Op, line: str) -> None:
        m = _RE_PRESET_OK.match(line)
        if m:
            self._finish(True, int(m.group(2)))
            return
        m = _RE_PRESET_ERR.match(line)
        if m:
            self._finish(False, m.group(2))

    # --- queue ---------------------------------------------------------------

    def _enqueue(self, op: _Op) -> None:
        self._queue.append(op)
        if self._active is None:
            self._start_next()

    def _start_next(self) -> None:
        if not self._queue:
            self._active = None
            return
        op = self._queue.pop(0)
        self._active = op
        op.deadline = time.monotonic() + op.timeout
        for frame in op.frames:
            self._send(frame)

    def _finish(self, ok: bool, payload) -> None:
        op = self._active
        self._active = None
        if op is not None:
            try:
                op.on_done(ok, payload)
            finally:
                if self._active is None:
                    self._start_next()
