#!/usr/bin/env python3
"""Bench controller for the DCO board.

Drives the DCO's whole parameter surface over its USB serial port, so the board can be
tested with no Input board and no Screen attached. Notes are not handled here: the DCO
already enumerates as a USB MIDI device, so play it from a MIDI keyboard or VMPK.

Requires the firmware to be built with ENABLE_USB_CONTROL (see DCO/DCO.ino).
Match SERIAL_FRAMING_COBS with --cobs or DCO_SERIAL_COBS=1.

    python3 app.py [--port /dev/ttyACM0] [--theme dark|light] [--cobs]
"""

from __future__ import annotations

import argparse
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

import fileformats
import mcu_link
import models
import params
import presets
import protocol
import theme

try:
    import serial
except ImportError:
    sys.exit("pyserial is required:  pacman -S python-pyserial   (or pip install pyserial)")

# Coalesce slider drags into one write per interval (last-write-wins), so dragging
# cannot flood the link — important once PIO pulse reloads running SMs.
SEND_INTERVAL_MS = 20

PARAM_BY_PID = {p.pid: p for p in params.PARAMS}

# Oscillators tab layout (see _build_osc_tab). Pids a model hides (Param.hidden)
# are skipped at build time, so these tuples stay the superset.
OSC_PITCH_PIDS = (13, 14, 33, 15, 34)
OSC_SYNC_PIDS = (31, 36, 37, 17)
OSC_VOICE_PIDS = (26, 27, 18, 32, 28, 29, 30, 43, 21)
OSC_LEVEL_PIDS = (22, 23, 38, 24)
OSC_WAVE_MATRIX = [
    ("OSC1", (1, 2, 3)),
    ("OSC2", (84, 85, 86)),
    ("OSC3", (87, 88, 89)),
]
OSC_WAVE_COLS = ("Saw", "Pulse", "Tri")


def apply_active_model() -> None:
    """Bake the active model profile into params and this module's tables.

    Must run before App() is constructed (main() does it after --model /
    auto-detection picks the profile).
    """
    params.apply_model(models.active())
    PARAM_BY_PID.clear()
    PARAM_BY_PID.update({p.pid: p for p in params.PARAMS})
    names = models.active().osc_row_names
    OSC_WAVE_MATRIX[:] = [
        (names[i], pids)
        for i, (_label, pids) in enumerate(OSC_WAVE_MATRIX)
        if not all(PARAM_BY_PID[pid].hidden for pid in pids)
    ]

# Reflow Oscillators tab: stack Pitch|Sync over Voice below this width (px).
OSC_SPLIT_STACK_WIDTH = 720

# Diagnostics tab compact layout (see _build_diag_tab).
DIAG_SPLIT_STACK_WIDTH = 720
DIAG_FRAME_PAD = 4
DIAG_SECTION_PADY = 4

# Envelopes tab layout (see _build_env_tab).
ENV_ADSR_BLOCKS = ("adsr_vca", "adsr_vcf", "adsr_dco")
ENV_CURVE_RESTART_PIDS = (8, 9, 48, 49, 50, 51)
ENV_CURVE_COLUMNS = (
    ("EnvVCA curves", 48, 49, 8),
    ("EnvVCF curves", 50, 51, 9),
    ("EnvDCO curves", None, None, None),
)
ENV_ADSR_VFADER_MIN = 80
ENV_ADSR_VFADER_MAX = 280
ENV_ADSR_VIEWPORT_FRAC = 0.35

JSON_FILETYPES = (("JSON files", "*.json"), ("All files", "*"))

# Manual calibration param ids (DCO/params_def.h), used to wire live offset
# recall and dirty-edit tracking on the Calibration tab (see _wire_manual_cal_recall).
PID_RUN_AUTOTUNE = 150
PID_MANUAL_CAL_MODE = 151
PID_MANUAL_CAL_STAGE = 152
PID_MANUAL_CAL_OFFSET = 153
PID_MANUAL_CAL_STORE = 156


def _bind_scale_jump(scale: ttk.Scale, on_change) -> None:
    """Button-1 on trough jumps to click position (clam default only steps)."""

    def on_press(event):
        part = scale.identify(event.x, event.y)
        if part and ("trough" in str(part) or "track" in str(part)):
            scale.set(scale.get(event.x, event.y))
            on_change(str(scale.get()))
            return "break"

    scale.bind("<Button-1>", on_press, add="+")


def _make_scale(parent, *, lo, hi, var, command, orient="horizontal", length=None) -> ttk.Scale:
    kwargs: dict = dict(from_=lo, to=hi, variable=var, orient=orient, command=command)
    if length is not None:
        kwargs["length"] = length
    scale = ttk.Scale(parent, **kwargs)
    _bind_scale_jump(scale, command)
    return scale


def ivar(var: tk.Variable) -> int:
    """Read a Tk variable as an int.

    ttk.Scale always writes floats, so slider variables are DoubleVar and have to be
    rounded on the way out rather than read as IntVar (which would raise on "3.5").
    """
    try:
        return int(round(float(var.get())))
    except (tk.TclError, ValueError):
        return 0


class Link:
    """Serial connection plus a background reader for the board's debug output."""

    def __init__(self) -> None:
        self.port = None
        self.rx: queue.Queue[str] = queue.Queue()
        self._reader: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def is_open(self) -> bool:
        return self.port is not None and self.port.is_open

    def open(self, device: str) -> None:
        self.close()
        self.port = serial.Serial(device, protocol.BAUD, timeout=0.1)
        self._stop.clear()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def close(self) -> None:
        self._stop.set()
        if self._reader is not None:
            self._reader.join(timeout=0.5)
            self._reader = None
        if self.port is not None:
            try:
                self.port.close()
            except OSError:
                pass
            self.port = None

    def send(self, frame: bytes) -> None:
        if not self.is_open:
            return
        try:
            self.port.write(frame)
        except OSError as exc:
            self.rx.put(f"[link] write failed: {exc}\n")
            self.close()

    def _read_loop(self) -> None:
        # The board only ever sends plain debug text back, never frames, so decode
        # loosely and forward whatever arrives to the log pane.
        while not self._stop.is_set():
            try:
                data = self.port.read(256)
            except (OSError, AttributeError, TypeError):
                break
            if data:
                self.rx.put(data.decode("utf-8", errors="replace"))


class App:
    def __init__(
        self,
        root: tk.Tk,
        preferred_port: str | None,
        mode: str = "dark",
        cobs: bool = False,
    ) -> None:
        protocol.use_cobs = cobs
        self.root = root
        self.link = Link()
        # MCU preset/cal transfers ride the same CDC stream; the board's structured
        # text answers ([dump]/[pdir]/...) are parsed out of the log feed.
        self.mcu = mcu_link.McuLink(
            lambda frame: self.link.send(protocol.stuff(frame)), self.log)
        self._mcu_linebuf = ""
        self.mcu_dir: dict[int, str] = {}  # slot -> name from the last [pdir] listing
        self._browser: PresetBrowser | None = None
        self.pending: dict[str, bytes] = {}  # dedup key -> most recent frame
        self.param_vars: dict[int, tk.Variable] = {}
        self.block_vars: dict[str, dict[str, tk.Variable]] = {}
        # Scale readout Labels: ("p", pid), ("b", block_key, field_key),
        # ("pio_pulse",), or ("char_jitter", hi).
        # ttk.Scale often skips command on programmatic var.set(); we sync these after apply.
        self._readouts: dict[tuple, ttk.Label] = {}
        self.blocks_by_key = {b.key: b for b in params.BLOCKS}
        self.pio_pulse_var: tk.DoubleVar | None = None
        # Character-tab diagnostic jitters (PARAM_DEBUG_COMMAND); not in presets / Send all.
        self.character_jitter_vars: dict[int, tk.DoubleVar] = {}
        self.mode = mode if mode in theme.MODES else "dark"
        self.dot_on = False
        self.bank = presets.empty_bank()
        self._clean_fp = ""
        self._preset_loading = False
        # Manual calibration offset recall: seeded once from the board's stored
        # ManualOffset table, then cached locally so switching oscillator stage
        # never overwrites an unsaved edit with a stale re-dump (see
        # _wire_manual_cal_recall / docs/PRESET_STORE.md).
        self._manual_cal_live: list[int] | None = None
        self._manual_cal_baseline: list[int] | None = None
        self._manual_cal_dirty: set[int] = set()
        self._manual_cal_syncing = False
        self._manual_cal_indicator: ttk.Label | None = None
        self._pulse_buttons: dict[int, ttk.Button] = {}
        self._tab_canvases: list[tk.Canvas] = []
        self._wheel_canvas: tk.Canvas | None = None
        self.notebook: ttk.Notebook | None = None

        root.title(f"DCO bench controller — {models.active().display_name}")
        # Wide enough that the toolbar's right-hand side is never squeezed out by pack().
        root.geometry("1140x960")
        root.minsize(900, 600)
        # Before building, so every widget is created already styled.
        theme.apply(root, self.mode)

        self._build_toolbar(preferred_port)
        self._build_preset_bar()
        self.paned = ttk.Panedwindow(root, orient=tk.VERTICAL)
        self.paned.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self._build_tabs()
        self._build_log()
        self._init_presets()
        # The canvases and log pane are plain Tk and were created after apply()'s own pass.
        theme.retint(root)
        self._style_log_tags()
        # Board output starts compact; drag the sash up when dumping profiler tables.
        self.root.after_idle(self._shrink_log_pane)

        self.root.after(SEND_INTERVAL_MS, self._flush)
        self.root.after(50, self._drain_log)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # --- toolbar ---------------------------------------------------------

    def _build_toolbar(self, preferred_port: str | None) -> None:
        bar = ttk.Frame(self.root, padding=(8, 6))
        bar.pack(fill="x")

        ttk.Label(bar, text="Port").pack(side="left")
        self.port_var = tk.StringVar()
        self.port_combo = ttk.Combobox(bar, textvariable=self.port_var, width=28, state="readonly")
        self.port_combo.pack(side="left", padx=6)
        self._refresh_ports(preferred_port)

        ttk.Button(bar, text="Rescan", command=lambda: self._refresh_ports(None)).pack(side="left")
        self.connect_btn = ttk.Button(bar, text="Connect", command=self._toggle_connect)
        self.connect_btn.pack(side="left", padx=6)

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=10)
        ttk.Button(bar, text="Send all", command=self.send_all,
                   style="Accent.TButton").pack(side="left")
        ttk.Button(bar, text="Reset to defaults", command=self.reset_defaults).pack(side="left", padx=6)

        self.status_var = tk.StringVar(value="not connected")
        ttk.Label(bar, textvariable=self.status_var, style="Status.TLabel").pack(side="right")
        self.status_dot = ttk.Label(bar, text="\u25cf", style="Dot.TLabel")
        self.status_dot.pack(side="right", padx=(10, 5))

        self.theme_btn = ttk.Button(bar, command=self._toggle_theme, width=7)
        self.theme_btn.pack(side="right", padx=(0, 12))
        self._sync_theme_button()

    # --- preset bar ------------------------------------------------------

    def _build_preset_bar(self) -> None:
        bar = ttk.Frame(self.root, padding=(8, 0, 8, 6))
        bar.pack(fill="x")

        ttk.Button(bar, text="<", width=3, command=self._preset_prev).pack(side="left")
        self.preset_num_var = tk.StringVar(value="000")
        self.preset_spin = ttk.Spinbox(
            bar,
            from_=0,
            to=presets.NUM_SLOTS - 1,
            textvariable=self.preset_num_var,
            width=4,
            command=self._preset_number_committed,
        )
        self.preset_spin.pack(side="left", padx=(6, 0))
        self.preset_spin.bind("<Return>", lambda _e: self._preset_number_committed())
        self.preset_spin.bind("<FocusOut>", lambda _e: self._preset_number_committed())

        self.preset_dirty_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.preset_dirty_var, width=1).pack(side="left", padx=(8, 0))

        self.preset_name_var = tk.StringVar(value="Init")
        self.preset_name_entry = ttk.Entry(bar, textvariable=self.preset_name_var, width=28)
        self.preset_name_entry.pack(side="left", padx=(2, 6), fill="x", expand=True)

        ttk.Button(bar, text=">", width=3, command=self._preset_next).pack(side="left")
        ttk.Button(bar, text="Load", command=self._preset_load).pack(side="left", padx=(10, 0))
        ttk.Button(bar, text="Save", command=self._preset_save).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="Save as…", command=self._preset_save_as).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="Init", command=self._preset_init).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="Browse…", command=self._open_browser).pack(side="left", padx=(10, 0))

        file_btn = ttk.Menubutton(bar, text="File")
        file_menu = tk.Menu(file_btn, tearoff=0)
        file_menu.add_command(label="Export patch…", command=self._export_patch_file)
        file_menu.add_command(label="Import patch…", command=self._import_patch_file)
        file_menu.add_separator()
        file_menu.add_command(label="Export bank…", command=self._export_bank_file)
        file_menu.add_command(label="Import bank…", command=self._import_bank_file)
        file_btn.configure(menu=file_menu)
        file_btn.pack(side="left", padx=(6, 0))

    def _init_presets(self) -> None:
        self.bank = presets.load_bank()
        for var in self.param_vars.values():
            var.trace_add("write", lambda *_a: self._refresh_dirty())
        for fields in self.block_vars.values():
            for var in fields.values():
                var.trace_add("write", lambda *_a: self._refresh_dirty())
        self._preset_recall(int(self.bank["current"]), send=False, persist_current=False)

    def _preset_slot_index(self) -> int:
        try:
            n = int(str(self.preset_num_var.get()).strip())
        except ValueError:
            n = int(self.bank.get("current", 0))
        return max(0, min(presets.NUM_SLOTS - 1, n))

    def _set_preset_number(self, index: int) -> None:
        self.preset_num_var.set(f"{index:03d}")

    def _current_ui_slot(self, name: str | None = None) -> dict:
        slot = presets.capture(self.param_vars, self.block_vars, self._param_value)
        if name is None:
            name = self.preset_name_var.get().strip() or "Untitled"
        slot["name"] = name[:48]
        return slot

    def _refresh_dirty(self) -> None:
        if self._preset_loading:
            return
        fp = presets.slot_fingerprint(self._current_ui_slot())
        self.preset_dirty_var.set("*" if fp != self._clean_fp else "")

    def _push_if_connected(self) -> None:
        if self.link.is_open:
            self.send_all()

    def _sync_readouts(self) -> None:
        """Refresh Scale numeric labels from live vars (command often skipped on var.set)."""
        for key, rd in self._readouts.items():
            if key[0] == "p":
                var = self.param_vars.get(key[1])
            elif key[0] == "pio_pulse":
                var = self.pio_pulse_var
            elif key[0] == "char_jitter":
                var = self.character_jitter_vars.get(key[1])
            else:
                var = self.block_vars.get(key[1], {}).get(key[2])
            if var is None:
                continue
            rd.config(text=str(ivar(var)))

    def _preset_recall(self, index: int, *, send: bool, persist_current: bool) -> None:
        index = max(0, min(presets.NUM_SLOTS - 1, index))
        slot = self.bank["slots"][index]
        empty = presets.slot_is_empty(slot)
        self._preset_loading = True
        try:
            presets.apply(self.param_vars, self.block_vars, None if empty else slot)
            if empty:
                self.preset_name_var.set("Init")
                self._clean_fp = presets.slot_fingerprint(presets.defaults_slot())
            else:
                self.preset_name_var.set(slot["name"])
                self._clean_fp = presets.slot_fingerprint(slot)
            self._set_preset_number(index)
            self.bank["current"] = index
        finally:
            self._preset_loading = False
        self._sync_readouts()
        self.preset_dirty_var.set("")
        if persist_current:
            presets.save_bank(self.bank)
        if empty:
            self.log(f"[preset] {index:03d} empty -- Init defaults loaded\n")
        else:
            self.log(f"[preset] loaded {index:03d} {slot['name']}\n")
        if send:
            self._push_if_connected()

    def _preset_number_committed(self) -> None:
        index = self._preset_slot_index()
        if index == int(self.bank.get("current", -1)):
            self._set_preset_number(index)
            return
        self._preset_recall(index, send=True, persist_current=True)

    def _preset_prev(self) -> None:
        index = (self._preset_slot_index() - 1) % presets.NUM_SLOTS
        self._preset_recall(index, send=True, persist_current=True)

    def _preset_next(self) -> None:
        index = (self._preset_slot_index() + 1) % presets.NUM_SLOTS
        self._preset_recall(index, send=True, persist_current=True)

    def _preset_load(self) -> None:
        self._preset_recall(self._preset_slot_index(), send=True, persist_current=True)

    def _preset_save(self, index: int | None = None) -> None:
        if index is None:
            index = self._preset_slot_index()
        typed = self.preset_name_var.get().strip()
        existing = self.bank["slots"][index]
        if typed:
            name = typed[:48]
        elif not presets.slot_is_empty(existing):
            name = existing["name"]
        else:
            name = "Untitled"
        slot = self._current_ui_slot(name)
        self.bank["slots"][index] = slot
        self.bank["current"] = index
        presets.save_bank(self.bank)
        self.preset_name_var.set(name)
        self._set_preset_number(index)
        self._clean_fp = presets.slot_fingerprint(slot)
        self.preset_dirty_var.set("")
        self.log(f"[preset] saved {index:03d} {name}\n")
        self._browser_refresh()

    def _preset_save_as(self) -> None:
        current_index = self._preset_slot_index()
        dest = simpledialog.askinteger(
            "Save as…", "Slot (0–255):",
            initialvalue=current_index, minvalue=0, maxvalue=presets.NUM_SLOTS - 1,
            parent=self.root,
        )
        if dest is None:
            return
        existing = self.bank["slots"][dest]
        if dest != current_index and not presets.slot_is_empty(existing):
            if not messagebox.askyesno(
                "Save as…",
                f"Slot {dest:03d} already has “{existing['name']}”. Overwrite?",
                parent=self.root,
            ):
                return
        current_name = self.preset_name_var.get().strip() or "Untitled"
        name = simpledialog.askstring(
            "Save as…", "Preset name:", initialvalue=current_name, parent=self.root
        )
        if name is None:
            return
        name = name.strip()[:48] or "Untitled"
        self.preset_name_var.set(name)
        self._preset_save(dest)

    def _preset_init(self) -> None:
        self._preset_loading = True
        try:
            presets.apply(self.param_vars, self.block_vars, None)
            self.preset_name_var.set("Init")
        finally:
            self._preset_loading = False
        self._sync_readouts()
        self._refresh_dirty()
        self.log("[preset] Init defaults in UI -- Save to store in this slot\n")
        self._push_if_connected()

    # --- preset browser / files -------------------------------------------

    def _open_browser(self) -> None:
        if self._browser is not None and self._browser.winfo_exists():
            self._browser.lift()
            self._browser.focus_set()
            return
        self._browser = PresetBrowser(self)

    def _browser_refresh(self) -> None:
        if self._browser is not None and self._browser.winfo_exists():
            self._browser.refresh()

    def apply_slot_to_ui(self, slot: dict, *, send: bool) -> None:
        """Load a slot dict (from a file or the MCU) into the live controls."""
        self._preset_loading = True
        try:
            presets.apply(self.param_vars, self.block_vars, slot)
            self.preset_name_var.set(slot["name"])
        finally:
            self._preset_loading = False
        self._sync_readouts()
        self._refresh_dirty()
        if send:
            self._push_if_connected()

    def _export_patch_file(self) -> None:
        slot = self._current_ui_slot()
        path = filedialog.asksaveasfilename(
            parent=self.root, title="Export patch", defaultextension=".json",
            initialfile=f"{slot['name'] or 'patch'}.json", filetypes=JSON_FILETYPES)
        if not path:
            return
        try:
            fileformats.save_patch_file(path, slot)
        except OSError as exc:
            self.log(f"[ui] patch export failed: {exc}\n")
            return
        self.log(f"[ui] exported patch \"{slot['name']}\" to {path}\n")

    def _import_patch_file(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.root, title="Import patch", filetypes=JSON_FILETYPES)
        if not path:
            return
        try:
            slot = fileformats.load_patch_file(path)
        except (OSError, ValueError) as exc:
            self.log(f"[ui] patch import failed: {exc}\n")
            return
        self.apply_slot_to_ui(slot, send=True)
        self.log(f"[ui] imported patch \"{slot['name']}\" -- Save to keep it in a slot\n")

    def _export_bank_file(self) -> None:
        path = filedialog.asksaveasfilename(
            parent=self.root, title="Export bank", defaultextension=".json",
            initialfile="dco_bank.json", filetypes=JSON_FILETYPES)
        if not path:
            return
        try:
            fileformats.save_bank_file(path, self.bank)
        except OSError as exc:
            self.log(f"[ui] bank export failed: {exc}\n")
            return
        self.log(f"[ui] exported bank to {path}\n")

    def _import_bank_file(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.root, title="Import bank", filetypes=JSON_FILETYPES)
        if not path:
            return
        try:
            bank = fileformats.load_bank_file(path)
        except (OSError, ValueError) as exc:
            self.log(f"[ui] bank import failed: {exc}\n")
            return
        if not messagebox.askyesno(
            "Import bank",
            "Replace the whole local bank with this file? The current bank.json "
            "is overwritten.",
            parent=self.root,
        ):
            return
        self.bank = bank
        presets.save_bank(self.bank)
        self._preset_recall(int(self.bank["current"]), send=True, persist_current=False)
        self._browser_refresh()
        self.log(f"[ui] imported bank from {path}\n")

    # --- MCU sync / calibration backup -------------------------------------

    def _mcu_ready(self) -> bool:
        if not self.link.is_open:
            self.log("[mcu] not connected\n")
            return False
        if self.mcu.busy:
            self.log("[mcu] transfer in progress -- wait for it to finish\n")
            return False
        return True

    def _cal_dump_to_file(self) -> None:
        """Pull all five calibration tables off the board, then ask where to save."""
        if not self._mcu_ready():
            return
        names = list(mcu_link.cal_tables())
        results: dict[str, bytes] = {}

        def step(k: int) -> None:
            if k >= len(names):
                self._cal_dump_finish(results, names)
                return
            name = names[k]

            def done(ok, payload, name=name, k=k):
                if ok:
                    results[name] = payload
                else:
                    self.log(f"[mcu] cal dump {name}: {payload}\n")
                step(k + 1)

            self.mcu.dump_cal_table(name, done)

        self.log("[mcu] dumping calibration tables...\n")
        step(0)

    def _cal_dump_finish(self, results: dict[str, bytes], names: list[str]) -> None:
        if not results:
            self.log("[mcu] calibration dump failed -- nothing to save\n")
            return
        missing = [n for n in names if n not in results]
        if missing:
            self.log(f"[mcu] missing tables (saved anyway): {', '.join(missing)}\n")
        path = filedialog.asksaveasfilename(
            parent=self.root, title="Save calibration dump", defaultextension=".json",
            initialfile="dco_calibration.json", filetypes=JSON_FILETYPES)
        if not path:
            return
        try:
            fileformats.save_cal_file(path, results)
        except OSError as exc:
            self.log(f"[mcu] calibration save failed: {exc}\n")
            return
        self.log(f"[mcu] calibration ({len(results)} tables) saved to {path}\n")

    def _cal_load_from_file(self) -> None:
        """Push calibration tables from a <model>-cal file into the board's LittleFS."""
        if not self._mcu_ready():
            return
        path = filedialog.askopenfilename(
            parent=self.root, title="Load calibration file", filetypes=JSON_FILETYPES)
        if not path:
            return
        try:
            tables = fileformats.load_cal_file(path)
        except (OSError, ValueError) as exc:
            self.log(f"[mcu] calibration file rejected: {exc}\n")
            return
        if not tables:
            self.log("[mcu] calibration file has no tables\n")
            return
        if not messagebox.askyesno(
            "Load calibration",
            f"Overwrite the board's calibration with {len(tables)} table(s) from this "
            "file? This rewrites LittleFS and reloads the tables live.",
            parent=self.root,
        ):
            return
        names = list(tables)
        state = {"fail": 0}

        def step(k: int) -> None:
            if k >= len(names):
                ok_n = len(names) - state["fail"]
                self.log(f"[mcu] calibration load done: {ok_n} ok, {state['fail']} failed\n")
                return
            name = names[k]

            def done(ok, payload, name=name, k=k):
                if not ok:
                    state["fail"] += 1
                    self.log(f"[mcu] cal load {name}: {payload}\n")
                step(k + 1)

            self.mcu.push_cal_table(name, tables[name], done)

        step(0)

    def _toggle_theme(self) -> None:
        self.mode = "light" if self.mode == "dark" else "dark"
        # ttk resolves styles at draw time, so this restyles the live widget tree in place.
        theme.apply(self.root, self.mode)
        self._style_log_tags()
        self._sync_theme_button()
        self._set_status_dot(self.link.is_open)

    def _sync_theme_button(self) -> None:
        self.theme_btn.config(text="Light" if self.mode == "dark" else "Dark")

    def _set_status_dot(self, connected: bool) -> None:
        self.status_dot.config(style="DotOn.TLabel" if connected else "Dot.TLabel")
        self.dot_on = connected

    def _refresh_ports(self, preferred: str | None) -> None:
        values = [f"{p.device}  ({p.description})" for p in protocol.find_dco_ports()]
        if preferred:
            values.insert(0, preferred)
        self.port_combo["values"] = values
        if values:
            self.port_combo.current(0)
        else:
            self.port_var.set("")

    def _selected_device(self) -> str:
        raw = self.port_var.get().strip()
        return raw.split()[0] if raw else ""

    def _toggle_connect(self) -> None:
        if self.link.is_open:
            self.mcu.cancel_all()
            self._mcu_linebuf = ""
            self.link.close()
            self.connect_btn.config(text="Connect")
            self.status_var.set("not connected")
            self._set_status_dot(False)
            self.log("[link] disconnected\n")
            return
        device = self._selected_device()
        if not device:
            self.log("[link] no serial port selected\n")
            return
        try:
            self.link.open(device)
        except (OSError, ValueError) as exc:
            self.log(f"[link] could not open {device}: {exc}\n")
            return
        self.connect_btn.config(text="Disconnect")
        self.status_var.set(f"connected to {device}")
        self._set_status_dot(True)
        self.log(f"[link] connected to {device} -- pushing UI state\n")
        self.log(f"[link] framing={'COBS' if protocol.use_cobs else 'RAW'} "
                 f"model={models.active().key}\n")
        n = self.send_all()
        self.log(f"[link] pushed UI state ({n} frames)\n")

    # --- tabs ------------------------------------------------------------

    def _shrink_log_pane(self) -> None:
        """Park the Board output sash near the bottom so tabs get most of the window."""
        height = self.paned.winfo_height()
        if height <= 1:
            self.root.after(50, self._shrink_log_pane)
            return
        # Leave a short log strip; the sash can be dragged up for long dumps.
        self.paned.sashpos(0, max(200, height - 140))

    def _build_tabs(self) -> None:
        notebook = ttk.Notebook(self.paned)
        self.notebook = notebook
        self.paned.add(notebook, weight=5)
        self._tab_canvases = []

        for group in params.GROUP_ORDER:
            page = ttk.Frame(notebook, padding=8)
            notebook.add(page, text=group)
            inner = self._scrollable(page)
            if group == params.GROUP_OSC:
                self._build_osc_tab(inner)
            elif group == params.GROUP_ENV:
                self._build_env_tab(inner)
            elif group == params.GROUP_CHARACTER:
                row = 0
                for param in [p for p in params.PARAMS if p.group == group]:
                    row = self._add_param(inner, param, row)
                row = self._add_character_jitter_sliders(inner, row)
                inner.columnconfigure(1, weight=1)
            elif group == params.GROUP_CAL:
                row = 0
                for block in [b for b in params.BLOCKS if b.group == group]:
                    row = self._add_block(inner, block, row)
                for param in [p for p in params.PARAMS if p.group == group]:
                    row = self._add_param(inner, param, row)
                row = self._add_manual_cal_indicator(inner, row)
                self._wire_manual_cal_recall()
                row = self._add_pio_pulse_slider(inner, row)
                panel = self._add_diag_panel(
                    inner, row=row, column=0, title="Dev tables",
                    commands=params.CAL_DEBUG_COMMANDS,
                    note="PARAM_DEBUG_COMMAND 30: force-write fake amp-comp + PW "
                         "to LittleFS (development placeholder).",
                )
                panel.grid_configure(columnspan=3, sticky="ew")
                # Single-button panel: place button + note without diag reflow.
                for i, btn in enumerate(panel._diag_buttons):  # type: ignore[attr-defined]
                    btn.grid(row=i, column=0, sticky="w", pady=1)
                panel._diag_note_label.grid(  # type: ignore[attr-defined]
                    row=len(panel._diag_buttons), column=0, sticky="w", pady=(4, 0))  # type: ignore[attr-defined]
                self._add_cal_backup_panel(inner, row + 1)
                inner.columnconfigure(1, weight=1)
            else:
                row = 0
                for block in [b for b in params.BLOCKS if b.group == group]:
                    row = self._add_block(inner, block, row)
                for param in [p for p in params.PARAMS if p.group == group]:
                    row = self._add_param(inner, param, row)
                inner.columnconfigure(1, weight=1)

        diag = ttk.Frame(notebook, padding=8)
        notebook.add(diag, text=params.GROUP_DIAG)
        diag_inner = self._scrollable(diag)
        self._build_diag_tab(diag_inner)

        notebook.bind("<<NotebookTabChanged>>", self._on_notebook_tab_changed)
        for seq in ("<Button-4>", "<Button-5>", "<MouseWheel>"):
            notebook.bind(seq, self._on_tab_wheel)
        self.root.bind_all("<Button-4>", self._on_tab_wheel, add="+")
        self.root.bind_all("<Button-5>", self._on_tab_wheel, add="+")
        self.root.bind_all("<MouseWheel>", self._on_tab_wheel, add="+")
        self._on_notebook_tab_changed()

    def _scrollable(self, parent: ttk.Frame) -> ttk.Frame:
        canvas = tk.Canvas(parent, highlightthickness=0)
        bar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        canvas.configure(yscrollcommand=bar.set)
        canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        window = canvas.create_window((0, 0), window=inner, anchor="nw")

        def on_configure(_event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfigure(window, width=canvas.winfo_width())

        inner.bind("<Configure>", on_configure)
        canvas.bind("<Configure>", on_configure)

        self._tab_canvases.append(canvas)
        for seq in ("<Button-4>", "<Button-5>", "<MouseWheel>"):
            canvas.bind(seq, self._on_tab_wheel)
            inner.bind(seq, self._on_tab_wheel)
        return inner

    def _on_notebook_tab_changed(self, _event=None) -> None:
        if self.notebook is None or not self._tab_canvases:
            self._wheel_canvas = None
            return
        try:
            idx = self.notebook.index(self.notebook.select())
            self._wheel_canvas = self._tab_canvases[idx]
        except (tk.TclError, IndexError):
            self._wheel_canvas = self._tab_canvases[0]

    def _as_widget(self, widget) -> tk.Misc | None:
        # bind_all often delivers event.widget as a Tcl path string, not a Misc.
        if widget is None:
            return None
        if isinstance(widget, str):
            try:
                return self.root.nametowidget(widget)
            except (KeyError, tk.TclError):
                return None
        return widget

    def _widget_is_descendant(self, widget, ancestor) -> bool:
        w = self._as_widget(widget)
        anc = self._as_widget(ancestor)
        if w is None or anc is None:
            return False
        while w is not None:
            if w == anc:
                return True
            w = w.master
        return False

    def _wheel_over_log(self, widget) -> bool:
        log = getattr(self, "log_text", None)
        return log is not None and self._widget_is_descendant(widget, log)

    def _on_tab_wheel(self, event) -> str | None:
        if self._wheel_over_log(event.widget):
            return None
        if self.notebook is not None:
            x, y = self.root.winfo_pointerxy()
            under = self.root.winfo_containing(x, y)
            if under is None or not self._widget_is_descendant(under, self.notebook):
                return None
        canvas = self._wheel_canvas
        if canvas is None:
            return None
        if event.num == 4:
            canvas.yview_scroll(-2, "units")
        elif event.num == 5:
            canvas.yview_scroll(2, "units")
        elif event.delta:
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        return "break"

    def _build_osc_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)

        self._osc_split = ttk.Frame(parent)
        self._osc_split.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        self._osc_col_left = ttk.Frame(self._osc_split)
        self._osc_col_right = ttk.Frame(self._osc_split)

        row = 0
        row = self._add_osc_section(self._osc_col_left, row, "Pitch", OSC_PITCH_PIDS, columnspan=1)
        row = self._add_osc_section(self._osc_col_left, row, "Sync", OSC_SYNC_PIDS, columnspan=1)
        self._add_osc_section(self._osc_col_right, 0, "Voice & drift", OSC_VOICE_PIDS, columnspan=1)

        row = self._add_osc_levels_row(parent, 1, OSC_LEVEL_PIDS)
        self._add_osc_wave_matrix(parent, row)

        self._osc_reflow_width = -1
        parent.bind("<Configure>", self._osc_layout_reflow, add="+")
        parent.after_idle(self._osc_layout_reflow)

    def _osc_layout_reflow(self, event=None) -> None:
        split = getattr(self, "_osc_split", None)
        if split is None:
            return
        width = split.winfo_width()
        if width < 8:
            return
        if width == self._osc_reflow_width:
            return
        self._osc_reflow_width = width

        stacked = width < OSC_SPLIT_STACK_WIDTH
        left = self._osc_col_left
        right = self._osc_col_right

        if stacked:
            left.grid(row=0, column=0, sticky="ew")
            right.grid(row=1, column=0, sticky="ew", pady=(8, 0))
            split.columnconfigure(0, weight=1)
            split.columnconfigure(1, weight=0)
        else:
            left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
            right.grid(row=0, column=1, sticky="nsew")
            split.columnconfigure(0, weight=1)
            split.columnconfigure(1, weight=1)

        left.columnconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)

        cells = getattr(self, "_osc_level_cells", ())
        levels_frame = getattr(self, "_osc_levels_frame", None)
        if levels_frame is not None and cells:
            compact = stacked or width < OSC_SPLIT_STACK_WIDTH + 120
            for c in range(4):
                levels_frame.columnconfigure(c, weight=0)
            for r in range(2):
                levels_frame.rowconfigure(r, weight=0)
            if compact:
                levels_frame.columnconfigure(0, weight=1)
                levels_frame.columnconfigure(1, weight=1)
                for i, cell in enumerate(cells):
                    cell.grid(row=i // 2, column=i % 2, sticky="ew", padx=4, pady=4)
            else:
                for i, cell in enumerate(cells):
                    levels_frame.columnconfigure(i, weight=1)
                    cell.grid(row=0, column=i, sticky="ew", padx=4, pady=0)

    def _build_env_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(1, weight=1)
        self._env_scales = []
        self._env_fader_length = -1
        self._env_inner = parent

        times = ttk.Frame(parent)
        times.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        self._env_times = times
        for col, key in enumerate(ENV_ADSR_BLOCKS):
            self._add_adsr_block_vertical(times, self.blocks_by_key[key], col)
        for c in range(len(ENV_ADSR_BLOCKS)):
            self._env_times.columnconfigure(c, weight=1, uniform="env")
        self._build_env_curves_row(times)

        row = 1
        for p in params.PARAMS:
            if p.group == params.GROUP_ENV and p.pid not in ENV_CURVE_RESTART_PIDS:
                row = self._add_param(parent, p, row)

        parent.bind("<Configure>", self._env_fader_reflow, add="+")
        parent.after_idle(self._env_fader_reflow)

    def _env_fader_reflow(self, _event=None) -> None:
        scales = getattr(self, "_env_scales", None)
        inner = getattr(self, "_env_inner", None)
        if not scales or inner is None:
            return
        canvas = inner.master
        if not isinstance(canvas, tk.Canvas):
            return
        viewport_h = canvas.winfo_height()
        if viewport_h < 2:
            return
        length = int(max(
            ENV_ADSR_VFADER_MIN,
            min(ENV_ADSR_VFADER_MAX, viewport_h * ENV_ADSR_VIEWPORT_FRAC),
        ))
        if length == self._env_fader_length:
            return
        self._env_fader_length = length
        for scale in scales:
            scale.configure(length=length)

    def _add_adsr_block_vertical(self, parent: ttk.Frame, block: params.Block, column: int) -> None:
        frame = ttk.LabelFrame(parent, text=block.label, padding=8)
        frame.grid(row=0, column=column, sticky="ew", padx=(0, 8 if column < 2 else 0))
        parent.columnconfigure(column, weight=1, uniform="env")

        self.block_vars[block.key] = {}
        for i, f in enumerate(block.fields):
            cell = ttk.Frame(frame)
            cell.grid(row=0, column=i, padx=4, sticky="new")
            cell.columnconfigure(0, weight=1)
            frame.columnconfigure(i, weight=1)
            ttk.Label(cell, text=f.label).grid(row=0, column=0)
            var = tk.DoubleVar(value=f.default)
            self.block_vars[block.key][f.key] = var
            readout = ttk.Label(cell, text=str(f.default), style="Readout.TLabel")

            def on_slide(_v, key=block.key, var=var, rd=readout):
                rd.config(text=str(ivar(var)))
                if self._preset_loading:
                    return
                self.queue_block(key)

            scale = _make_scale(
                cell, lo=f.lo, hi=f.hi, var=var, command=on_slide,
                orient="vertical", length=ENV_ADSR_VFADER_MIN,
            )
            scale.grid(row=1, column=0, pady=4)
            readout.grid(row=2, column=0)
            self._env_scales.append(scale)
            self._readouts[("b", block.key, f.key)] = readout

    def _add_env_curve_spin(self, cell: ttk.Frame, p: params.Param, short_label: str) -> None:
        cell.columnconfigure(0, weight=1)
        ttk.Label(cell, text=short_label).grid(row=0, column=0)
        labels = [c[0] for c in p.choices]
        lookup = dict(p.choices)
        var = tk.StringVar(value=next((c[0] for c in p.choices if c[1] == p.default), labels[0]))
        self.param_vars[p.pid] = var

        def on_pick(_e=None, pid=p.pid, var=var, lookup=lookup):
            if self._preset_loading:
                return
            self.queue_param(pid, lookup[var.get()])

        combo = ttk.Combobox(cell, textvariable=var, values=labels, state="readonly",
                             width=18)
        combo.grid(row=1, column=0, pady=4, sticky="ew")
        combo.bind("<<ComboboxSelected>>", on_pick)
        tip = f"{p.label}  [{p.pid}]"
        if p.note:
            tip += f"\n{p.note}"
        self._tooltip(combo, tip)

    def _add_env_curve_column(self, parent: ttk.Frame, column: int, title: str,
                              attack_pid: int | None, decay_pid: int | None,
                              restart_pid: int | None, *, row: int = 1) -> None:
        frame = ttk.LabelFrame(parent, text=title, padding=8)
        frame.grid(row=row, column=column, sticky="ew", padx=(0, 8 if column < 2 else 0),
                   pady=(8, 0) if row else 0)
        parent.columnconfigure(column, weight=1, uniform="env")

        if attack_pid is not None and decay_pid is not None:
            for i, (pid, label) in enumerate(((attack_pid, "Attack"), (decay_pid, "Decay"))):
                cell = ttk.Frame(frame)
                cell.grid(row=0, column=i, padx=4, sticky="new")
                frame.columnconfigure(i, weight=1)
                self._add_env_curve_spin(cell, PARAM_BY_PID[pid], label)

        if restart_pid is not None:
            rp = PARAM_BY_PID[restart_pid]
            restart_cell = ttk.Frame(frame)
            restart_cell.grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))
            ttk.Label(restart_cell, text="Restart").grid(row=0, column=0, padx=(0, 4))
            var = tk.IntVar(value=rp.default)
            self.param_vars[rp.pid] = var

            def on_check(pid=rp.pid, var=var):
                if self._preset_loading:
                    return
                self.queue_param(pid, ivar(var))

            btn = ttk.Checkbutton(restart_cell, variable=var, command=on_check,
                                  style=theme.check_style())
            btn.grid(row=0, column=1)
            tip = f"{rp.label}  [{rp.pid}]"
            if rp.note:
                tip += f"\n{rp.note}"
            self._tooltip(btn, tip)

    def _build_env_curves_row(self, times: ttk.Frame, row: int = 1) -> None:
        for col, (title, attack_pid, decay_pid, restart_pid) in enumerate(ENV_CURVE_COLUMNS):
            self._add_env_curve_column(times, col, title, attack_pid, decay_pid, restart_pid, row=row)

    def _add_osc_section(self, parent: ttk.Frame, row: int, title: str,
                         pids: tuple[int, ...], *, columnspan: int = 3) -> int:
        frame = ttk.LabelFrame(parent, text=title, padding=8)
        frame.grid(row=row, column=0, columnspan=columnspan, sticky="ew", pady=(4, 10))
        frame.columnconfigure(1, weight=1)
        subrow = 0
        for pid in pids:
            subrow = self._add_param(frame, PARAM_BY_PID[pid], subrow)
        return row + 1

    def _add_osc_levels_row(self, parent: ttk.Frame, row: int, pids: tuple[int, ...]) -> int:
        frame = ttk.LabelFrame(parent, text="Levels", padding=8)
        frame.grid(row=row, column=0, sticky="ew", pady=(4, 10))
        self._osc_levels_frame = frame
        self._osc_level_cells = []
        for pid in pids:
            p = PARAM_BY_PID[pid]
            if p.hidden:
                continue
            cell = ttk.Frame(frame)
            self._osc_level_cells.append(cell)
            self._add_osc_level_cell(cell, p)
        return row + 1

    def _add_osc_level_cell(self, cell: ttk.Frame, p: params.Param) -> None:
        ttk.Label(cell, text=p.label).grid(row=0, column=0, columnspan=2, sticky="w")
        var = tk.DoubleVar(value=p.default)
        self.param_vars[p.pid] = var
        readout = ttk.Label(cell, width=4, text=str(p.default), anchor="e",
                            style="Readout.TLabel")

        def on_slide(_v, pid=p.pid, var=var, rd=readout):
            rd.config(text=str(ivar(var)))
            if self._preset_loading:
                return
            self.queue_param(pid, ivar(var))

        _make_scale(cell, lo=p.lo, hi=p.hi, var=var, command=on_slide).grid(
            row=1, column=0, sticky="ew", pady=4)
        readout.grid(row=1, column=1, sticky="e", padx=(4, 0))
        cell.columnconfigure(0, weight=1)
        self._readouts[("p", p.pid)] = readout

    def _add_osc_wave_matrix(self, parent: ttk.Frame, row: int) -> None:
        frame = ttk.LabelFrame(parent, text="Waveforms", padding=8)
        frame.grid(row=row, column=0, sticky="ew", pady=(4, 10))
        for col, title in enumerate(OSC_WAVE_COLS, start=1):
            ttk.Label(frame, text=title).grid(row=0, column=col, padx=12, pady=(0, 4))
        for r, (osc_label, pids) in enumerate(OSC_WAVE_MATRIX, start=1):
            ttk.Label(frame, text=osc_label).grid(row=r, column=0, sticky="e", padx=(0, 12))
            for c, pid in enumerate(pids, start=1):
                if not PARAM_BY_PID[pid].hidden:
                    self._wire_check(frame, PARAM_BY_PID[pid], row=r, column=c)

    def _wire_check(self, parent: ttk.Frame, p: params.Param, *, row: int, column: int) -> None:
        var = tk.IntVar(value=p.default)
        self.param_vars[p.pid] = var

        def on_check(pid=p.pid, var=var):
            if self._preset_loading:
                return
            self.queue_param(pid, ivar(var))

        btn = ttk.Checkbutton(parent, variable=var, command=on_check,
                              style=theme.check_style())
        btn.grid(row=row, column=column, padx=12, pady=2)
        tip = f"{p.label}  [{p.pid}]"
        if p.note:
            tip += f"\n{p.note}"
        self._tooltip(btn, tip)

    def _add_pio_pulse_slider(self, parent: ttk.Frame, row: int) -> int:
        """Calibration-only: PARAM_DEBUG_COMMAND 160 values 200..50000 set pioPulseLength."""
        row_pad = 8
        lo, hi, default = params.PIO_PULSE_LO, params.PIO_PULSE_HI, params.PIO_PULSE_DEFAULT
        label = ttk.Label(parent, text=f"PIO pulse length (Y)  [{params.DEBUG_PARAM_ID}]")
        label.grid(row=row, column=0, sticky="w", pady=row_pad, padx=(0, 10))
        self._tooltip(
            label,
            "Reset pulse width in system clock cycles. Sent as unsigned 16-bit on "
            "PARAM_DEBUG_COMMAND 160 (values 200–50000). Reloads running SMs via defer "
            "reset (audible while a note is held); large changes may need amp-comp redo.",
        )
        var = tk.DoubleVar(value=default)
        self.pio_pulse_var = var
        readout = ttk.Label(parent, width=6, text=str(default), anchor="e",
                            style="Readout.TLabel")
        self._readouts[("pio_pulse",)] = readout

        def on_slide(_v, var=var, rd=readout):
            rd.config(text=str(ivar(var)))
            if self._preset_loading:
                return
            self.queue_debug_u16(ivar(var))

        _make_scale(parent, lo=lo, hi=hi, var=var, command=on_slide).grid(
            row=row, column=1, sticky="ew", pady=row_pad)
        readout.grid(row=row, column=2, sticky="e", padx=(8, 0))
        return row + 1

    def _add_character_jitter_sliders(self, parent: ttk.Frame, row: int) -> int:
        """Character tab: diagnostic jitter amounts via packed PARAM_DEBUG_COMMAND 160."""
        row_pad = 8
        lo = params.CHARACTER_JITTER_LO
        hi = params.CHARACTER_JITTER_HI
        default = params.CHARACTER_JITTER_DEFAULT
        for label_text, type_hi in params.CHARACTER_JITTERS:
            label = ttk.Label(
                parent, text=f"{label_text}  [{params.DEBUG_PARAM_ID}:0x{type_hi:02X}xx]")
            label.grid(row=row, column=0, sticky="w", pady=row_pad, padx=(0, 10))
            self._tooltip(
                label,
                f"Diagnostic only (not a ParamId). Sent as unsigned 16-bit on "
                f"PARAM_DEBUG_COMMAND 160: (0x{type_hi:02X} << 8) | amount, amount 0..128. "
                f"Stored in firmware; not applied to DSP yet.",
            )
            var = tk.DoubleVar(value=default)
            self.character_jitter_vars[type_hi] = var
            readout = ttk.Label(parent, width=6, text=str(default), anchor="e",
                                style="Readout.TLabel")
            self._readouts[("char_jitter", type_hi)] = readout

            def on_slide(_v, var=var, rd=readout, type_hi=type_hi):
                amount = ivar(var)
                rd.config(text=str(amount))
                if self._preset_loading:
                    return
                self.queue_debug_u16((type_hi << 8) | amount)

            _make_scale(parent, lo=lo, hi=hi, var=var, command=on_slide).grid(
                row=row, column=1, sticky="ew", pady=row_pad)
            readout.grid(row=row, column=2, sticky="e", padx=(8, 0))
            row += 1
        return row

    def _add_param(self, parent: ttk.Frame, p: params.Param, row: int) -> int:
        if p.hidden:
            return row  # model keeps the param (presets/MIDI) but not the GUI
        row_pad = 8
        label = ttk.Label(parent, text=f"{p.label}  [{p.pid}]")
        label.grid(row=row, column=0, sticky="w", pady=row_pad, padx=(0, 10))
        if p.note:
            self._tooltip(label, p.note)

        if p.kind == "slider":
            var = tk.DoubleVar(value=p.default)
            self.param_vars[p.pid] = var
            readout = ttk.Label(parent, width=6, text=str(p.default), anchor="e",
                                style="Readout.TLabel")

            def on_slide(_v, pid=p.pid, var=var, rd=readout):
                rd.config(text=str(ivar(var)))
                if self._preset_loading:
                    return
                self.queue_param(pid, ivar(var))

            _make_scale(parent, lo=p.lo, hi=p.hi, var=var, command=on_slide).grid(
                row=row, column=1, sticky="ew", pady=row_pad)
            readout.grid(row=row, column=2, sticky="e", padx=(8, 0))
            self._readouts[("p", p.pid)] = readout

        elif p.kind == "combo":
            labels = [c[0] for c in p.choices]
            lookup = dict(p.choices)
            var = tk.StringVar(value=next((c[0] for c in p.choices if c[1] == p.default), labels[0]))
            self.param_vars[p.pid] = var
            combo = ttk.Combobox(parent, textvariable=var, values=labels, state="readonly")
            combo.grid(row=row, column=1, sticky="ew", pady=row_pad)

            def on_pick(_e=None, pid=p.pid, var=var, lookup=lookup):
                if self._preset_loading:
                    return
                self.queue_param(pid, lookup[var.get()])

            combo.bind("<<ComboboxSelected>>", on_pick)

        elif p.kind == "check":
            self._wire_check(parent, p, row=row, column=1)

        elif p.kind == "pulse":
            def on_pulse(pid=p.pid, value=p.pulse_value, label=p.label):
                if not self._confirm_pulse(pid):
                    return
                self.send_now(protocol.param16(pid, value))
                self.log(f"[send] {label} (param {pid} = {value})\n")
                self._on_pulse_sent(pid)

            btn = ttk.Button(parent, text="Send", command=on_pulse)
            btn.grid(row=row, column=1, sticky="w", pady=row_pad)
            self._pulse_buttons[p.pid] = btn

        return row + 1

    def _add_block(self, parent: ttk.Frame, block: params.Block, row: int,
                   *, columnspan: int = 3) -> int:
        frame = ttk.LabelFrame(parent, text=block.label, padding=8)
        frame.grid(row=row, column=0, columnspan=columnspan, sticky="ew", pady=(4, 10))
        frame.columnconfigure(1, weight=1)

        self.block_vars[block.key] = {}
        for i, f in enumerate(block.fields):
            ttk.Label(frame, text=f.label).grid(row=i, column=0, sticky="w", padx=(0, 10), pady=6)
            var = tk.DoubleVar(value=f.default)
            self.block_vars[block.key][f.key] = var
            readout = ttk.Label(frame, width=6, text=str(f.default), anchor="e",
                                style="Readout.TLabel")

            def on_slide(_v, key=block.key, var=var, rd=readout):
                rd.config(text=str(ivar(var)))
                if self._preset_loading:
                    return
                self.queue_block(key)

            _make_scale(frame, lo=f.lo, hi=f.hi, var=var, command=on_slide).grid(
                row=i, column=1, sticky="ew", pady=6)
            readout.grid(row=i, column=2, sticky="e", padx=(8, 0))
            self._readouts[("b", block.key, f.key)] = readout

        if block.note:
            ttk.Label(frame, text=block.note, wraplength=700, style="Muted.TLabel").grid(
                row=len(block.fields), column=0, columnspan=3, sticky="w", pady=(6, 0))
        return row + 1

    # --- manual calibration offset recall ---------------------------------

    def _add_manual_cal_indicator(self, parent: ttk.Frame, row: int) -> int:
        """Calibration tab: shows recall/dirty status for the manual offset sliders."""
        label = ttk.Label(parent, text="", style="Muted.TLabel", wraplength=700)
        label.grid(row=row, column=0, columnspan=3, sticky="w", pady=(0, 8))
        self._manual_cal_indicator = label
        self._update_manual_cal_indicator()
        return row + 1

    def _update_manual_cal_indicator(self) -> None:
        if self._manual_cal_indicator is None:
            return
        if self._manual_cal_live is None:
            text = ("Manual cal offsets: not read from the board yet -- enable "
                    "Manual calibration mode to recall the stored values.")
        elif self._manual_cal_dirty:
            oscs = ", ".join(str(i) for i in sorted(self._manual_cal_dirty))
            text = (f"Manual cal offsets: unsaved changes for oscillator(s) {oscs} -- "
                    "press Store manual cal offsets before running autotune, or they "
                    "will be discarded.")
        else:
            text = "Manual cal offsets: matches what's stored on the board."
        self._manual_cal_indicator.config(text=text)

    def _wire_manual_cal_recall(self) -> None:
        """Hook the manual-cal mode/stage/offset vars so the offset slider always
        reflects the real per-oscillator value instead of whatever it last showed."""
        self.param_vars[PID_MANUAL_CAL_MODE].trace_add("write", self._manual_cal_on_mode_changed)
        self.param_vars[PID_MANUAL_CAL_STAGE].trace_add("write", self._manual_cal_on_stage_changed)
        self.param_vars[PID_MANUAL_CAL_OFFSET].trace_add("write", self._manual_cal_on_offset_changed)

    def _manual_cal_on_mode_changed(self, *_args) -> None:
        if self._manual_cal_syncing or self._preset_loading:
            return
        if ivar(self.param_vars[PID_MANUAL_CAL_MODE]) == 0:
            return
        if self._manual_cal_live is not None and self._manual_cal_dirty:
            # Unsaved edits pending -- don't clobber them with a stale flash re-dump.
            self._manual_cal_sync_offset_slider()
            return
        self._manual_cal_refresh_from_board()

    def _manual_cal_refresh_from_board(self) -> None:
        """Recall the board's stored ManualOffset table via the existing cal-dump path."""
        if not self._mcu_ready():
            self.log("[mcu] can't recall manual cal offsets -- not connected\n")
            return

        def done(ok, payload):
            if not ok:
                self.log(f"[mcu] manual cal recall failed: {payload}\n")
                return
            try:
                values = fileformats.decode_cal_table("ManualOffset", payload)
            except ValueError as exc:
                self.log(f"[mcu] manual cal recall: {exc}\n")
                return
            self._manual_cal_live = list(values)
            self._manual_cal_baseline = list(values)
            self._manual_cal_dirty.clear()
            self._manual_cal_syncing = True
            try:
                self.param_vars[PID_MANUAL_CAL_STAGE].set(0)
            finally:
                self._manual_cal_syncing = False
            self._manual_cal_sync_offset_slider()
            self._update_manual_cal_indicator()
            self.log(f"[mcu] manual cal offsets recalled: {values}\n")

        self.mcu.dump_cal_table("ManualOffset", done)

    def _manual_cal_sync_offset_slider(self) -> None:
        """Show the cached offset for whichever oscillator stage is selected."""
        if self._manual_cal_live is None:
            return
        stage = max(0, min(len(self._manual_cal_live) - 1,
                            ivar(self.param_vars[PID_MANUAL_CAL_STAGE])))
        self._manual_cal_syncing = True
        try:
            self.param_vars[PID_MANUAL_CAL_OFFSET].set(self._manual_cal_live[stage])
        finally:
            self._manual_cal_syncing = False
        self._sync_readouts()

    def _manual_cal_on_stage_changed(self, *_args) -> None:
        if self._manual_cal_syncing or self._manual_cal_live is None:
            return
        self._manual_cal_sync_offset_slider()

    def _manual_cal_on_offset_changed(self, *_args) -> None:
        if self._manual_cal_syncing or self._manual_cal_live is None:
            return
        stage = max(0, min(len(self._manual_cal_live) - 1,
                            ivar(self.param_vars[PID_MANUAL_CAL_STAGE])))
        value = ivar(self.param_vars[PID_MANUAL_CAL_OFFSET])
        self._manual_cal_live[stage] = value
        baseline = self._manual_cal_baseline[stage] if self._manual_cal_baseline else 0
        if value != baseline:
            self._manual_cal_dirty.add(stage)
        else:
            self._manual_cal_dirty.discard(stage)
        self._update_manual_cal_indicator()

    def _manual_cal_on_stored(self) -> None:
        if self._manual_cal_live is not None:
            self._manual_cal_baseline = list(self._manual_cal_live)
        self._manual_cal_dirty.clear()
        self._update_manual_cal_indicator()

    def _confirm_pulse(self, pid: int) -> bool:
        """Gate a pulse Send: warn before Run autotune discards unsaved manual offsets."""
        if pid != PID_RUN_AUTOTUNE or not self._manual_cal_dirty:
            return True
        oscs = ", ".join(str(i) for i in sorted(self._manual_cal_dirty))
        choice = messagebox.askyesnocancel(
            "Unsaved manual calibration offsets",
            f"Oscillator(s) {oscs} have unsaved manual calibration offsets.\n\n"
            "Auto calibration reloads the board's filesystem when it finishes, "
            "which discards any manual offset edit that wasn't stored.\n\n"
            "Yes: store them now, then run autotune.\n"
            "No: run autotune anyway and discard the unsaved edits.\n"
            "Cancel: don't run autotune.",
            parent=self.root,
        )
        if choice is None:
            return False
        if choice:
            store = PARAM_BY_PID[PID_MANUAL_CAL_STORE]
            self.send_now(protocol.param16(PID_MANUAL_CAL_STORE, store.pulse_value))
            self.log(f"[send] {store.label} (param {PID_MANUAL_CAL_STORE} = {store.pulse_value})\n")
            self._manual_cal_on_stored()
        return True

    def _on_pulse_sent(self, pid: int) -> None:
        """Hook for after-send bookkeeping. Most pulses don't need this."""
        if pid == PID_MANUAL_CAL_STORE:
            self._manual_cal_on_stored()

    def _add_cal_backup_panel(self, parent: ttk.Frame, row: int) -> None:
        """Calibration tab: dump/restore the board's five LittleFS cal tables."""
        frame = ttk.LabelFrame(parent, text="Calibration backup", padding=8)
        frame.grid(row=row, column=0, columnspan=3, sticky="ew", pady=4)
        ttk.Button(frame, text="Dump board → file…",
                   command=self._cal_dump_to_file).grid(row=0, column=0, sticky="w")
        ttk.Button(frame, text="Load file → board…",
                   command=self._cal_load_from_file).grid(row=0, column=1, sticky="w", padx=(6, 0))
        ttk.Label(
            frame,
            text=f"Amp-comp, PW and manual-offset tables as a {fileformats.cal_format()} JSON file. "
                 "Loading overwrites the board's LittleFS calibration and reloads "
                 "it live (needs a connected board).",
            wraplength=700, style="Muted.TLabel",
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))

    def _add_diag_panel(self, parent: ttk.Frame, *, row: int, column: int, title: str,
                        commands: tuple[tuple[str, int], ...], note: str) -> ttk.LabelFrame:
        frame = ttk.LabelFrame(parent, text=title, padding=DIAG_FRAME_PAD)
        frame.grid(row=row, column=column, sticky="ew", pady=DIAG_SECTION_PADY)
        buttons: list[ttk.Button] = []
        for label, value in commands:
            def on_click(value=value, label=label):
                self.send_now(protocol.param16(params.DEBUG_PARAM_ID, value))
                self.log(f"[send] {label}\n")

            buttons.append(ttk.Button(frame, text=label, command=on_click))
        frame._diag_buttons = buttons  # type: ignore[attr-defined]
        note_label = ttk.Label(frame, text=note, wraplength=340, style="Muted.TLabel")
        frame._diag_note_label = note_label  # type: ignore[attr-defined]
        return frame

    def _diag_panel_ncol(self, panel_w: int, n_buttons: int, stacked: bool) -> int:
        if n_buttons <= 1:
            return 1
        ncol = 2
        if stacked and panel_w >= 640:
            ncol = 3
        elif not stacked and panel_w >= 400:
            ncol = 3
        return min(ncol, n_buttons)

    def _diag_reflow_panel(self, frame: ttk.LabelFrame, panel_w: int, stacked: bool,
                           wrap: int) -> None:
        buttons: list[ttk.Button] = frame._diag_buttons  # type: ignore[attr-defined]
        ncol = self._diag_panel_ncol(panel_w, len(buttons), stacked)
        for c in range(max(ncol, 1)):
            frame.columnconfigure(c, weight=1)
        for i, btn in enumerate(buttons):
            btn.grid(row=i // ncol, column=i % ncol, padx=2, pady=1, sticky="ew")
        btn_rows = (len(buttons) - 1) // ncol + 1 if buttons else 0
        note_label: ttk.Label = frame._diag_note_label  # type: ignore[attr-defined]
        note_label.config(wraplength=wrap)
        note_label.grid(row=btn_rows, column=0, columnspan=ncol, sticky="ew", pady=(4, 0))

    def _build_diag_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.columnconfigure(1, weight=1)
        self._diag_grid = parent
        self._diag_reflow_width = -1

        panel_specs = [
            ("Diagnostics", models.filter_debug_commands(params.DEBUG_COMMANDS),
             "Output appears in the log below. Period probes only hold while no note "
             "is playing. Dump RAM (13) prints heap + per-core stack (needs ENABLE_MEM_DIAG). "
             "14/15 disable/enable mem_diag loop polls (A/B vs profiler). "
             "Note retrig 26/27 A/B EXACT_Y vs SYNC_JMP (needs oscSync ≥ 1); "
             "Board ack note_retrig=… when RUNNING_AVERAGE.", 0, 0),
            ("Hot-path profiler", params.BENCH_COMMANDS,
             "Needs RUNNING_AVERAGE in the firmware (otherwise these are no-ops). "
             "Dump is asynchronous: core 0 prints after both cores snapshot. "
             "Toggle prints 'bench periodic on/off' immediately; the tables land "
             "in the Board output pane (drag the sash to enlarge it).", 0, 1),
            ("Amp-comp method / bench", params.AMP_COMP_COMMANDS,
             "Method buttons (20–22) switch the live lookup for profiler / speed A/B "
             "(PWM difference is tiny — expect Board ack amp_comp method=…). "
             "FLOAT_QUAD=cached walk. "
             "Speed/accuracy (24–25) need AMP_COMP_BENCHMARK + "
             "RUNNING_AVERAGE; confirm live_method= on the speed report.",
             1, 0),
            ("Pitch-interp bench", params.PITCH_INTERP_COMMANDS,
             "Speed/accuracy (28–29) need RUNNING_AVERAGE (paced Board output). "
             "Compares FLOAT / RATIO_Q16 / Q12 (private tables; Q20 slope is inside RATIO). "
             "Profiler dump (10) prints build: … pitch=… flags.", 1, 1),
            ("Clkdiv GOLD_REF / LIVE / FLOAT / Q16 / Q8 / Q4", params.CLKDIV_HP_COMMANDS,
             "Speed/accuracy (32–33) need RUNNING_AVERAGE (paced Board output). "
             "All seven methods on both voice engines. GOLD_REF = true-Hz llround (speed 100%). "
             "Fixed: Q24 helpers. Float: native Hz for GOLD/FLOAT; Hz→Q24 then helper for integer. "
             "Accuracy vs GOLD_REF. Live float honors CLKDIV_MODE via clkdiv_live_hz.",
             2, 0),
        ]
        if models.active().has_mainboard:
            panel_specs.append(
                ("Mainboard profiler", params.BENCH_MB_COMMANDS,
                 "DCO forwards 40–42 to the STM32 Mainboard over Serial2; its ASCII "
                 "dump comes back as text chunks into the Board output pane. Needs "
                 "RUNNING_AVERAGE in the Mainboard firmware.", 2, 1))
        self._diag_panels = []
        for title, commands, note, grid_row, grid_col in panel_specs:
            frame = self._add_diag_panel(
                parent, row=grid_row, column=grid_col, title=title,
                commands=commands, note=note,
            )
            self._diag_panels.append((frame, grid_row, grid_col))

        parent.bind("<Configure>", self._diag_layout_reflow, add="+")
        parent.after_idle(self._diag_layout_reflow)

    def _diag_layout_reflow(self, _event=None) -> None:
        grid = getattr(self, "_diag_grid", None)
        panels = getattr(self, "_diag_panels", None)
        if grid is None or not panels:
            return
        width = grid.winfo_width()
        if width < 2 or width == self._diag_reflow_width:
            return
        self._diag_reflow_width = width
        stacked = width < DIAG_SPLIT_STACK_WIDTH
        panel_w = width if stacked else max(200, width // 2 - 16)
        wrap = 700 if stacked else max(280, width // 2 - 24)
        for i, (frame, wide_row, wide_col) in enumerate(panels):
            if stacked:
                frame.grid(row=i, column=0, columnspan=2, sticky="ew", pady=DIAG_SECTION_PADY)
            else:
                frame.grid(row=wide_row, column=wide_col, columnspan=1, sticky="ew",
                           padx=(0, 4 if wide_col == 0 else 0), pady=DIAG_SECTION_PADY)
            self._diag_reflow_panel(frame, panel_w, stacked, wrap)

    def _tooltip(self, widget: tk.Widget, text: str) -> None:
        state: dict[str, tk.Toplevel | None] = {"win": None}

        def show(_e):
            if state["win"] is not None:
                return
            # Created on hover, so the startup retint() cannot reach it: colour it here.
            p = theme.palette()
            win = tk.Toplevel(widget)
            win.wm_overrideredirect(True)
            win.wm_geometry(f"+{widget.winfo_rootx() + 20}+{widget.winfo_rooty() + widget.winfo_height() + 4}")
            win.configure(background=p["border"])
            tk.Label(win, text=text, justify="left", wraplength=420, padx=8, pady=5,
                     background=p["surface"], foreground=p["fg"],
                     relief="flat", borderwidth=0).pack(padx=1, pady=1)
            state["win"] = win

        def hide(_e):
            if state["win"] is not None:
                state["win"].destroy()
                state["win"] = None

        widget.bind("<Enter>", show)
        widget.bind("<Leave>", hide)

    # --- log -------------------------------------------------------------

    def _build_log(self) -> None:
        frame = ttk.LabelFrame(self.paned, text="Board output", padding=6)
        self.paned.add(frame, weight=1)
        self.log_text = tk.Text(frame, height=6, wrap="none")
        vbar = ttk.Scrollbar(frame, orient="vertical", command=self.log_text.yview)
        hbar = ttk.Scrollbar(frame, orient="horizontal", command=self.log_text.xview)
        self.log_text.configure(yscrollcommand=vbar.set, xscrollcommand=hbar.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        vbar.grid(row=0, column=1, sticky="ns")
        hbar.grid(row=1, column=0, sticky="ew")
        ttk.Button(frame, text="Clear", command=lambda: self.log_text.delete("1.0", "end")).grid(
            row=2, column=0, columnspan=2, sticky="e", pady=(4, 0))
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        def on_log_wheel(event):
            if event.num == 4:
                self.log_text.yview_scroll(-2, "units")
            elif event.num == 5:
                self.log_text.yview_scroll(2, "units")
            elif event.delta:
                self.log_text.yview_scroll(int(-1 * (event.delta / 120)), "units")
            return "break"

        for seq in ("<Button-4>", "<Button-5>", "<MouseWheel>"):
            self.log_text.bind(seq, on_log_wheel)

    def _style_log_tags(self) -> None:
        p = theme.palette()
        self.log_text.tag_configure("link", foreground=p["accent"])
        self.log_text.tag_configure("send", foreground=p["muted"])
        self.log_text.tag_configure("ui", foreground=p["muted"])
        self.log_text.tag_configure("mcu", foreground=p["accent"])

    def log(self, text: str) -> None:
        # Tag our own lines so they read as commentary, leaving board output plain.
        tag = ""
        for name in ("link", "send", "ui", "mcu"):
            if text.startswith(f"[{name}]"):
                tag = name
                break
        self.log_text.insert("end", text, tag or ())
        # Keep the buffer bounded; DCO_DEBUG_REPORT is chatty.
        if int(self.log_text.index("end-1c").split(".")[0]) > 2000:
            self.log_text.delete("1.0", "500.0")
        self.log_text.see("end")

    def _drain_log(self) -> None:
        try:
            while True:
                chunk = self.link.rx.get_nowait()
                self.log(chunk)
                self._mcu_feed(chunk)
        except queue.Empty:
            pass
        self.mcu.tick()
        if self.dot_on and not self.link.is_open:
            # A failed write closes the link from the reader thread's side.
            self._set_status_dot(False)
            self.status_var.set("not connected")
            self.connect_btn.config(text="Connect")
            self.mcu.cancel_all()
        self.root.after(50, self._drain_log)

    def _mcu_feed(self, chunk: str) -> None:
        """Reassemble the RX stream into lines for the MCU protocol parser."""
        self._mcu_linebuf += chunk
        while "\n" in self._mcu_linebuf:
            line, self._mcu_linebuf = self._mcu_linebuf.split("\n", 1)
            self.mcu.feed_line(line)
        if len(self._mcu_linebuf) > 4096:  # runaway line; nothing we parse is this long
            self._mcu_linebuf = ""

    # --- sending ---------------------------------------------------------

    def _enqueue(self, key: str, frame: bytes) -> None:
        """Stage a frame; last write for the same key wins until the next flush."""
        self.pending[key] = protocol.stuff(frame)

    def queue_param(self, pid: int, value: int) -> None:
        """Stage a 16-bit parameter frame (coalesced by the 20 ms flush)."""
        self._enqueue(f"p{pid}", protocol.param16(pid, int(value)))

    def queue_debug_u16(self, value: int) -> None:
        """Stage unsigned 16-bit PARAM_DEBUG_COMMAND (e.g. pioPulseLength)."""
        self._enqueue(
            "p_debug_u16", protocol.param16u(params.DEBUG_PARAM_ID, int(value)))

    def queue_block(self, key: str) -> None:
        block = self.blocks_by_key[key]
        values = {k: ivar(v) for k, v in self.block_vars[key].items()}
        self._enqueue(f"b{key}", block.builder(values))

    def send_now(self, frame: bytes) -> None:
        if not self.link.is_open:
            self.log("[link] not connected\n")
            return
        self.link.send(protocol.stuff(frame))

    def _flush_pending(self) -> int:
        """Send staged frames if connected. Clears only after a successful send."""
        if not self.pending or not self.link.is_open:
            return 0
        frames = list(self.pending.values())
        self.link.send(b"".join(frames))
        n = len(frames)
        self.pending.clear()
        return n

    def _flush(self) -> None:
        """Periodic coalesce tick: deliver pending when linked; keep staging offline."""
        self._flush_pending()
        self.root.after(SEND_INTERVAL_MS, self._flush)

    def send_all(self) -> int:
        """Push the sound patch once. Returns the number of frames sent.

        Calibration, Diagnostics, and bench/debug controls are omitted — those send
        only when the user operates them. Needed after connecting: the board boots
        with its own defaults and has no idea what this window is showing.
        """
        if not self.link.is_open:
            self.log("[link] not connected\n")
            return 0
        # UI is authoritative; drop any stale offline queue before the full push.
        self.pending.clear()
        n = 0
        for p in presets.patch_params():
            self.queue_param(p.pid, self._param_value(p))
            n += 1
        for block in presets.patch_blocks():
            self.queue_block(block.key)
            n += 1
        self._flush_pending()
        self.log(f"[send] patch {n} frames\n")
        return n

    def _param_value(self, p: params.Param) -> int:
        var = self.param_vars.get(p.pid)
        if var is None:
            return p.default
        if p.kind == "combo":
            return dict(p.choices)[var.get()]
        return ivar(var)

    def reset_defaults(self) -> None:
        self._preset_loading = True
        try:
            for p in params.PARAMS:
                var = self.param_vars.get(p.pid)
                if var is None:
                    continue
                if p.kind == "combo":
                    var.set(next(c[0] for c in p.choices if c[1] == p.default))
                else:
                    var.set(p.default)
            for block in params.BLOCKS:
                for f in block.fields:
                    self.block_vars[block.key][f.key].set(f.default)
            if self.pio_pulse_var is not None:
                self.pio_pulse_var.set(params.PIO_PULSE_DEFAULT)
            for var in self.character_jitter_vars.values():
                var.set(params.CHARACTER_JITTER_DEFAULT)
        finally:
            self._preset_loading = False
        self._sync_readouts()
        self._refresh_dirty()
        self.log("[ui] controls reset to defaults -- press 'Send all' to push them\n")

    def _on_close(self) -> None:
        self.link.close()
        self.root.destroy()


class PresetBrowser(tk.Toplevel):
    """256-slot bank browser: local slot management, patch files, MCU sync.

    The MCU column shows the board's own preset names from the last directory
    fetch ([pdir]); it is empty until "Refresh board list" is pressed. All board
    operations run through app.mcu one at a time and report to the log pane.
    """

    def __init__(self, app: App) -> None:
        super().__init__(app.root)
        self.app = app
        self.title("Preset browser")
        self.geometry("780x680")
        self.minsize(600, 440)

        body = ttk.Frame(self, padding=8)
        body.pack(fill="both", expand=True)

        columns = ("slot", "name", "mcu")
        self.tree = ttk.Treeview(body, columns=columns, show="headings",
                                 selectmode="browse")
        self.tree.heading("slot", text="Slot")
        self.tree.heading("name", text="Local name")
        self.tree.heading("mcu", text="Board name")
        self.tree.column("slot", width=60, anchor="e", stretch=False)
        self.tree.column("name", width=300)
        self.tree.column("mcu", width=240)
        bar = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        bar.grid(row=0, column=1, sticky="ns")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        self.tree.bind("<Double-1>", lambda _e: self._local_load())

        local = ttk.LabelFrame(body, text="Local bank", padding=6)
        local.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        for text, cmd in (
            ("Load", self._local_load),
            ("Save into", self._local_save_into),
            ("Rename…", self._local_rename),
            ("Copy to…", self._local_copy_to),
            ("Move to…", self._local_move_to),
            ("Delete", self._local_delete),
            ("Export slot…", self._file_export),
            ("Import into slot…", self._file_import),
        ):
            ttk.Button(local, text=text, command=cmd).pack(side="left", padx=(0, 6))

        mcu = ttk.LabelFrame(body, text="Board (MCU)", padding=6)
        mcu.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        mcu_specs = (
            ("Refresh board list", self._mcu_refresh_dir),
            ("Send slot → board", self._mcu_send_slot),
            ("Fetch slot ← board", self._mcu_fetch_slot),
            ("Recall slot on board", self._mcu_recall),
            ("Push all → board", self._mcu_push_all),
            ("Pull all ← board", self._mcu_pull_all),
            ("Save board live state → slot", self._mcu_save_live),
        )
        for i, (text, cmd) in enumerate(mcu_specs):
            ttk.Button(mcu, text=text, command=cmd).grid(
                row=i // 4, column=i % 4, padx=(0, 6), pady=2, sticky="ew")

        self.refresh()

    # --- helpers ----------------------------------------------------------

    def log(self, text: str) -> None:
        self.app.log(text)

    def _sel(self) -> int | None:
        sel = self.tree.selection()
        if not sel:
            self.log("[ui] select a slot in the browser first\n")
            return None
        return int(sel[0])

    def refresh(self) -> None:
        selected = self.tree.selection()
        top = self.tree.yview()[0]
        self.tree.delete(*self.tree.get_children())
        for i in range(presets.NUM_SLOTS):
            slot = self.app.bank["slots"][i]
            name = "" if presets.slot_is_empty(slot) else slot["name"]
            self.tree.insert("", "end", iid=str(i),
                             values=(f"{i:03d}", name, self.app.mcu_dir.get(i, "")))
        if selected:
            self.tree.selection_set(selected)
        self.tree.yview_moveto(top)

    def _safe_refresh(self) -> None:
        """Refresh from an async MCU callback; the window may have been closed."""
        try:
            if self.winfo_exists():
                self.refresh()
        except tk.TclError:
            pass

    def _local_slot(self, index: int) -> dict | None:
        slot = self.app.bank["slots"][index]
        return None if presets.slot_is_empty(slot) else slot

    def _save_bank(self) -> None:
        presets.save_bank(self.app.bank)
        # save_bank normalizes; keep the in-memory copy identical to disk.
        self.app.bank = presets.load_bank()
        self.refresh()

    # --- local bank ---------------------------------------------------------

    def _local_load(self) -> None:
        i = self._sel()
        if i is None:
            return
        self.app._preset_recall(i, send=True, persist_current=True)

    def _local_save_into(self) -> None:
        i = self._sel()
        if i is None:
            return
        existing = self._local_slot(i)
        if existing is not None and not messagebox.askyesno(
            "Save into slot",
            f"Slot {i:03d} already has “{existing['name']}”. Overwrite?",
            parent=self,
        ):
            return
        self.app._preset_save(i)

    def _local_rename(self) -> None:
        i = self._sel()
        if i is None:
            return
        slot = self._local_slot(i)
        if slot is None:
            self.log(f"[ui] slot {i:03d} is empty\n")
            return
        name = simpledialog.askstring(
            "Rename preset", "Name:", initialvalue=slot["name"], parent=self)
        if name is None:
            return
        slot["name"] = name.strip()[:48] or "Untitled"
        if i == int(self.app.bank.get("current", -1)):
            self.app.preset_name_var.set(slot["name"])
        self._save_bank()
        self.log(f"[preset] renamed {i:03d} to {slot['name']}\n")

    def _pick_dest(self, title: str, source: int) -> int | None:
        dest = simpledialog.askinteger(
            title, "Destination slot (0–255):", initialvalue=source,
            minvalue=0, maxvalue=presets.NUM_SLOTS - 1, parent=self)
        if dest is None or dest == source:
            return None
        existing = self._local_slot(dest)
        if existing is not None and not messagebox.askyesno(
            title, f"Slot {dest:03d} already has “{existing['name']}”. Overwrite?",
            parent=self,
        ):
            return None
        return dest

    def _local_copy_to(self) -> None:
        i = self._sel()
        if i is None:
            return
        slot = self._local_slot(i)
        if slot is None:
            self.log(f"[ui] slot {i:03d} is empty\n")
            return
        dest = self._pick_dest("Copy to…", i)
        if dest is None:
            return
        self.app.bank["slots"][dest] = presets.normalize_bank({"slots": [slot]})["slots"][0]
        self._save_bank()
        self.log(f"[preset] copied {i:03d} to {dest:03d}\n")

    def _local_move_to(self) -> None:
        i = self._sel()
        if i is None:
            return
        slot = self._local_slot(i)
        if slot is None:
            self.log(f"[ui] slot {i:03d} is empty\n")
            return
        dest = self._pick_dest("Move to…", i)
        if dest is None:
            return
        self.app.bank["slots"][dest] = slot
        self.app.bank["slots"][i] = None
        self._save_bank()
        self.log(f"[preset] moved {i:03d} to {dest:03d}\n")

    def _local_delete(self) -> None:
        i = self._sel()
        if i is None:
            return
        slot = self._local_slot(i)
        if slot is None:
            return
        if not messagebox.askyesno(
            "Delete preset", f"Delete {i:03d} “{slot['name']}” from the local bank?",
            parent=self,
        ):
            return
        self.app.bank["slots"][i] = None
        self._save_bank()  # slot 0 re-inits to defaults; others go empty
        self.log(f"[preset] deleted {i:03d}\n")

    # --- patch files ----------------------------------------------------------

    def _file_export(self) -> None:
        i = self._sel()
        if i is None:
            return
        slot = self._local_slot(i)
        if slot is None:
            self.log(f"[ui] slot {i:03d} is empty\n")
            return
        path = filedialog.asksaveasfilename(
            parent=self, title="Export slot", defaultextension=".json",
            initialfile=f"{slot['name']}.json", filetypes=JSON_FILETYPES)
        if not path:
            return
        try:
            fileformats.save_patch_file(path, slot)
        except OSError as exc:
            self.log(f"[ui] patch export failed: {exc}\n")
            return
        self.log(f"[ui] exported {i:03d} \"{slot['name']}\" to {path}\n")

    def _file_import(self) -> None:
        i = self._sel()
        if i is None:
            return
        path = filedialog.askopenfilename(
            parent=self, title="Import patch into slot", filetypes=JSON_FILETYPES)
        if not path:
            return
        try:
            slot = fileformats.load_patch_file(path)
        except (OSError, ValueError) as exc:
            self.log(f"[ui] patch import failed: {exc}\n")
            return
        existing = self._local_slot(i)
        if existing is not None and not messagebox.askyesno(
            "Import into slot",
            f"Slot {i:03d} already has “{existing['name']}”. Overwrite with "
            f"“{slot['name']}”?",
            parent=self,
        ):
            return
        self.app.bank["slots"][i] = slot
        self._save_bank()
        self.log(f"[ui] imported \"{slot['name']}\" into {i:03d}\n")

    # --- board (MCU) sync -------------------------------------------------------

    def _mcu_refresh_dir(self) -> None:
        app = self.app
        if not app._mcu_ready():
            return

        def done(ok, payload):
            if not ok:
                self.log(f"[mcu] directory listing failed: {payload}\n")
                return
            app.mcu_dir = dict(payload)
            self.log(f"[mcu] board has {len(payload)} preset(s)\n")
            self._safe_refresh()

        app.mcu.read_directory(done)

    def _mcu_send_slot(self) -> None:
        i = self._sel()
        if i is None:
            return
        slot = self._local_slot(i)
        if slot is None:
            self.log(f"[ui] slot {i:03d} is empty\n")
            return
        app = self.app
        if not app._mcu_ready():
            return
        record = fileformats.slot_to_record(slot)

        def done(ok, payload, i=i, name=slot["name"]):
            if ok:
                app.mcu_dir[i] = name[:16]
                self.log(f"[mcu] slot {i:03d} \"{name}\" written to board\n")
                self._safe_refresh()
            else:
                self.log(f"[mcu] send slot {i:03d} failed: {payload}\n")

        app.mcu.push_preset_record(i, record, done)

    def _mcu_fetch_slot(self) -> None:
        i = self._sel()
        if i is None:
            return
        app = self.app
        if not app._mcu_ready():
            return
        existing = self._local_slot(i)
        if existing is not None and not messagebox.askyesno(
            "Fetch slot",
            f"Overwrite local slot {i:03d} “{existing['name']}” with the board's "
            "copy?",
            parent=self,
        ):
            return

        def done(ok, payload, i=i):
            if not ok:
                self.log(f"[mcu] fetch slot {i:03d} failed: {payload}\n")
                return
            try:
                slot = fileformats.record_to_slot(payload)
            except ValueError as exc:
                self.log(f"[mcu] fetch slot {i:03d}: bad record ({exc})\n")
                return
            app.bank["slots"][i] = slot
            app.mcu_dir[i] = slot["name"][:16]
            self._save_bank()
            self.log(f"[mcu] fetched slot {i:03d} \"{slot['name']}\" from board\n")

        app.mcu.dump_preset_slot(i, done)

    def _mcu_recall(self) -> None:
        i = self._sel()
        if i is None:
            return
        app = self.app
        if not app._mcu_ready():
            return

        def done(ok, payload, i=i):
            if ok:
                self.log(f"[mcu] board recalled slot {i:03d} (panel UI unchanged -- "
                         "use Fetch to sync it here)\n")
            else:
                self.log(f"[mcu] recall slot {i:03d} failed: {payload}\n")

        app.mcu.recall_slot(i, done)

    def _mcu_save_live(self) -> None:
        i = self._sel()
        if i is None:
            return
        app = self.app
        if not app._mcu_ready():
            return
        name = simpledialog.askstring(
            "Save board live state", "Preset name (16 chars reach the board):",
            initialvalue=app.preset_name_var.get().strip()[:16], parent=self)
        if name is None:
            return
        name = name.strip() or "Untitled"

        def done(ok, payload, i=i, name=name):
            if ok:
                app.mcu_dir[i] = name[:16]
                self.log(f"[mcu] board saved its live state into slot {i:03d}\n")
                self._safe_refresh()
            else:
                self.log(f"[mcu] board save failed: {payload}\n")

        app.mcu.save_live_to_slot(i, name, done)

    def _mcu_push_all(self) -> None:
        app = self.app
        if not app._mcu_ready():
            return
        entries = [
            (i, s) for i, s in enumerate(app.bank["slots"])
            if not presets.slot_is_empty(s)
        ]
        if not entries:
            self.log("[ui] local bank is empty\n")
            return
        if not messagebox.askyesno(
            "Push all",
            f"Write {len(entries)} local preset(s) into the board's slots? "
            "Matching board slots are overwritten.",
            parent=self,
        ):
            return
        state = {"fail": 0}

        def step(k: int) -> None:
            if k >= len(entries):
                ok_n = len(entries) - state["fail"]
                self.log(f"[mcu] push all done: {ok_n} ok, {state['fail']} failed\n")
                self._safe_refresh()
                return
            i, slot = entries[k]

            def done(ok, payload, i=i, name=slot["name"], k=k):
                if ok:
                    app.mcu_dir[i] = name[:16]
                else:
                    state["fail"] += 1
                    self.log(f"[mcu] push slot {i:03d} failed: {payload}\n")
                step(k + 1)

            app.mcu.push_preset_record(i, fileformats.slot_to_record(slot), done)

        self.log(f"[mcu] pushing {len(entries)} preset(s) to the board...\n")
        step(0)

    def _mcu_pull_all(self) -> None:
        app = self.app
        if not app._mcu_ready():
            return

        def on_dir(ok, payload):
            if not ok:
                self.log(f"[mcu] directory listing failed: {payload}\n")
                return
            app.mcu_dir = dict(payload)
            self._safe_refresh()
            if not payload:
                self.log("[mcu] board has no presets\n")
                return
            if not messagebox.askyesno(
                "Pull all",
                f"Copy {len(payload)} preset(s) from the board into the local bank? "
                "Matching local slots are overwritten.",
                parent=self,
            ):
                return
            slots = [slot for slot, _name in payload]
            state = {"fail": 0}

            def step(k: int) -> None:
                if k >= len(slots):
                    ok_n = len(slots) - state["fail"]
                    self._save_bank()
                    self.log(f"[mcu] pull all done: {ok_n} ok, {state['fail']} failed\n")
                    return
                i = slots[k]

                def done(ok, payload, i=i, k=k):
                    if ok:
                        try:
                            app.bank["slots"][i] = fileformats.record_to_slot(payload)
                        except ValueError as exc:
                            state["fail"] += 1
                            self.log(f"[mcu] pull slot {i:03d}: bad record ({exc})\n")
                    else:
                        state["fail"] += 1
                        self.log(f"[mcu] pull slot {i:03d} failed: {payload}\n")
                    step(k + 1)

                app.mcu.dump_preset_slot(i, done)

            self.log(f"[mcu] pulling {len(slots)} preset(s) from the board...\n")
            step(0)

        app.mcu.read_directory(on_dir)


def main() -> None:
    ap = argparse.ArgumentParser(description="DCO bench controller")
    ap.add_argument("--port", help="serial device, e.g. /dev/ttyACM0 (default: auto-detect)")
    ap.add_argument("--model", choices=sorted(models.PROFILES),
                    help="synth model (default: auto-detect from USB, else "
                         f"DCO_CONTROL_MODEL env, else {models.active().key})")
    ap.add_argument("--theme", choices=theme.MODES, default="dark",
                    help="colour scheme; also switchable from the toolbar (default: dark)")
    ap.add_argument("--cobs", action="store_true",
                    help="COBS on-wire framing (match firmware SERIAL_FRAMING_COBS)")
    args = ap.parse_args()

    env_cobs = os.environ.get("DCO_SERIAL_COBS", "").strip().lower() in ("1", "true", "yes")
    cobs = args.cobs or env_cobs

    env_model = os.environ.get("DCO_CONTROL_MODEL", "").strip().lower()
    model = args.model or models.detect() or (env_model if env_model in models.PROFILES else None)
    if model:
        models.set_active(model)
    apply_active_model()

    root = tk.Tk()
    App(root, args.port, args.theme, cobs=cobs)
    root.mainloop()


if __name__ == "__main__":
    main()
