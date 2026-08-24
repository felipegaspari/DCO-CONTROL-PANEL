#!/usr/bin/env python3
"""Bench controller for the DCO board built with PySide6 (Qt6)."""

from __future__ import annotations

import argparse
import os
import sys
import threading

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QKeySequence, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

import calstages
import fileformats
import mcu_link
import models
import param_meta
import params
import presets
import protocol
import theme_qt as theme

try:
    import serial
except ImportError:
    sys.exit("pyserial is required: pip install pyserial")

SEND_INTERVAL_MS = 20
PARAM_BY_PID = {p.pid: p for p in params.PARAMS}

# Layout constants
# Layout constants in app.py
OSC_PITCH_PIDS = (13, 14, 34, 15, 35)
OSC_SYNC_PIDS = (32, 37, 38, 17)
OSC_VOICE_PIDS = (26, 27, 28, 18, 33, 29, 30, 31, 43, 21)
OSC_LEVEL_PIDS = (22, 23, 39, 24)
OSC_WAVE_MATRIX = [
    ("OSC1", (1, 2, 3)),
    ("OSC2", (87, 88, 89)),
    ("OSC3", (90, 91, 92)),
]
OSC_WAVE_COLS = ("Saw", "Pulse", "Tri")

ENV_ADSR_BLOCKS = ("adsr_vca", "adsr_vcf", "adsr_dco")
ENV_CURVE_RESTART_PIDS = (8, 9, 214, 48, 49, 50, 51, 52, 53, 54, 55, 56)
ENV_CURVE_COLUMNS = (
    ("EnvVCA curves", 48, 49, 50, 8),
    ("EnvVCF curves", 51, 52, 53, 9),
    ("EnvDCO curves", 54, 55, 56, 214),
)

PID_RUN_AUTOTUNE = 150
PID_MANUAL_CAL_MODE = 151
PID_MANUAL_CAL_STAGE = 152
PID_MANUAL_CAL_OFFSET = 153
PID_MANUAL_CAL_STORE = 156
PID_AMP_COMP_440 = 159
PID_AMP_COMP_DUTY_OFFSET = 161
PID_CAL_PW_CENTER = 162

CAL_KIND_PIDS = {
    PID_MANUAL_CAL_OFFSET: (calstages.KIND_SAW, calstages.KIND_TRI, calstages.KIND_PULSE),
    PID_CAL_PW_CENTER: (calstages.KIND_PULSE_PW,),
    PID_AMP_COMP_440: (calstages.KIND_440,),
}

ZOOM_LEVELS = [
    ("80%", 0.80),
    ("90%", 0.90),
    ("100%", 1.00),
    ("110%", 1.10),
    ("125%", 1.25),
    ("140%", 1.40),
    ("160%", 1.60),
    ("180%", 1.80),
    ("200%", 2.00),
]


def apply_active_model() -> None:
    params.apply_model(models.active())
    PARAM_BY_PID.clear()
    PARAM_BY_PID.update({p.pid: p for p in params.PARAMS})
    names = models.active().osc_row_names
    OSC_WAVE_MATRIX[:] = [
        (names[i], pids)
        for i, (_label, pids) in enumerate(OSC_WAVE_MATRIX)
        if not all(PARAM_BY_PID[pid].hidden for pid in pids)
    ]


class LinkEmitter(QObject):
    data_received = Signal(str)
    write_failed = Signal(str)


class Link:
    def __init__(self, emitter: LinkEmitter) -> None:
        self.port: serial.Serial | None = None
        self.emitter = emitter
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
            self.emitter.write_failed.emit(str(exc))
            self.close()

    def _read_loop(self) -> None:
        while not self._stop.is_set():
            try:
                data = self.port.read(256) if self.port else b""
            except (OSError, AttributeError, TypeError):
                break
            if data:
                self.emitter.data_received.emit(data.decode("utf-8", errors="replace"))


class App(QMainWindow):
    def __init__(
        self,
        preferred_port: str | None,
        mode: str = theme.DEFAULT_THEME,
        base_font_size: int = 12,
        cobs: bool = False,
    ) -> None:
        super().__init__()
        protocol.use_cobs = cobs
        self.mode = mode if mode in theme.PALETTES else theme.DEFAULT_THEME
        self.base_font_size = base_font_size
        self.zoom_scale = 1.0
        self.blocks_by_key = {b.key: b for b in params.BLOCKS}

        # Serial Link
        self.link_emitter = LinkEmitter()
        self.link_emitter.data_received.connect(self._on_data_received)
        self.link_emitter.write_failed.connect(self._on_write_failed)
        self.link = Link(self.link_emitter)

        self.mcu = mcu_link.McuLink(
            lambda frame: self.link.send(protocol.stuff(frame)), self.log
        )
        self._mcu_linebuf = ""
        self.mcu_dir: dict[int, str] = {}
        self._browser: PresetBrowser | None = None
        self.pending: dict[str, bytes] = {}

        # Widget Maps
        self.param_widgets: dict[int, QWidget] = {}
        self.block_widgets: dict[str, dict[str, QWidget]] = {}
        self.character_jitter_sliders: dict[int, QSlider] = {}
        self.pio_pulse_slider: QSlider | None = None
        self._readouts: dict[tuple, QLabel] = {}

        # Manual cal states
        self._manual_cal_live: list[int] | None = None
        self._manual_cal_baseline: list[int] | None = None
        self._manual_cal_dirty: set[int] = set()
        self._manual_cal_syncing = False
        self._manual_cal_indicator: QLabel | None = None
        self._cal_osc_combo: QComboBox | None = None
        self._cal_sub_combo: QComboBox | None = None
        self._cal_sub_kinds: tuple[int, ...] = ()
        self._cal_stage_readout: QLabel | None = None
        self._cal_kind_rows: dict[int, tuple[QLabel, QWidget]] = {}

        self._amp440_live: list[int] | None = None
        self._amp440_baseline: list[int] | None = None
        self._amp440_dirty: set[int] = set()

        self._pwcenter_live: list[int] | None = None
        self._pwcenter_baseline: list[int] | None = None
        self._pwcenter_dirty: set[int] = set()

        self._dutytrim_live: list[int] | None = None
        self._dutytrim_baseline: list[int] | None = None
        self._dutytrim_dirty: set[int] = set()

        self._cal_precision_offset = 0
        self._pulse_buttons: dict[int, QPushButton] = {}

        self.bank = presets.empty_bank()
        self._clean_fp = ""
        self._preset_loading = False

        # GUI Setup
        self.setWindowTitle(f"DCO Bench Controller — {models.active().display_name}")
        self.resize(1180, 940)
        self.setMinimumSize(920, 600)

        self._build_ui(preferred_port)
        self._setup_shortcuts()
        self._apply_theme()
        self._init_presets()

        # Timers
        self.flush_timer = QTimer(self)
        self.flush_timer.timeout.connect(self._flush)
        self.flush_timer.start(SEND_INTERVAL_MS)

        self.mcu_timer = QTimer(self)
        self.mcu_timer.timeout.connect(self.mcu.tick)
        self.mcu_timer.start(50)

    # --- Theming & Scaling ---

    def _apply_theme(self) -> None:
        effective_font_size = max(8, int(self.base_font_size * self.zoom_scale))
        self.setStyleSheet(theme.build_stylesheet(self.mode, effective_font_size))
        
        # Apply global application font
        app = QApplication.instance()
        if app:
            font = app.font()
            font.setPointSize(effective_font_size)
            app.setFont(font)

        if hasattr(self, "preset_name_entry"):
            self.preset_name_entry.setFixedWidth(int(170 * self.zoom_scale))


        if hasattr(self, "status_dot") and hasattr(self, "link"):
            dot_col = (
                theme.PALETTES[self.mode]["ok"]
                if self.link.is_open
                else theme.PALETTES[self.mode]["off"]
            )
            self.status_dot.setStyleSheet(
                f"color: {dot_col}; font-size: {effective_font_size + 3}px;"
            )

    def _on_theme_selected(self, index: int) -> None:
        key = self.theme_combo.itemData(index)
        if key in theme.PALETTES:
            self.mode = key
            self._apply_theme()

    def _on_zoom_selected(self, index: int) -> None:
        scale = self.zoom_combo.itemData(index)
        if scale:
            self.zoom_scale = scale
            self._apply_theme()

    def _zoom_in(self) -> None:
        idx = self.zoom_combo.currentIndex()
        if idx < self.zoom_combo.count() - 1:
            self.zoom_combo.setCurrentIndex(idx + 1)

    def _zoom_out(self) -> None:
        idx = self.zoom_combo.currentIndex()
        if idx > 0:
            self.zoom_combo.setCurrentIndex(idx - 1)

    def _zoom_reset(self) -> None:
        idx = self.zoom_combo.findData(1.00)
        if idx != -1:
            self.zoom_combo.setCurrentIndex(idx)

    def _setup_shortcuts(self) -> None:
        # Ctrl + / Ctrl - / Ctrl 0 zoom shortcuts
        act_in = QAction(self)
        act_in.setShortcuts([QKeySequence("Ctrl+="), QKeySequence("Ctrl++")])
        act_in.triggered.connect(self._zoom_in)
        self.addAction(act_in)

        act_out = QAction(self)
        act_out.setShortcut(QKeySequence("Ctrl+-"))
        act_out.triggered.connect(self._zoom_out)
        self.addAction(act_out)

        act_reset = QAction(self)
        act_reset.setShortcut(QKeySequence("Ctrl+0"))
        act_reset.triggered.connect(self._zoom_reset)
        self.addAction(act_reset)

    # --- UI Layout Builders ---

    def _build_ui(self, preferred_port: str | None) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.setSpacing(6)

        # Toolbar & Preset Bar
        main_layout.addWidget(self._build_toolbar(preferred_port))
        main_layout.addWidget(self._build_preset_bar())

        # Vertical Splitter (Tabs + Log)
        self.splitter = QSplitter(Qt.Orientation.Vertical)
        main_layout.addWidget(self.splitter, 1)

        self.tabs = QTabWidget()
        self.splitter.addWidget(self.tabs)
        self._build_tabs()

        self.log_pane = self._build_log_pane()
        self.splitter.addWidget(self.log_pane)
        self.splitter.setStretchFactor(0, 5)
        self.splitter.setStretchFactor(1, 1)

    def _build_toolbar(self, preferred_port: str | None) -> QWidget:
        bar = QFrame()
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        lay.addWidget(QLabel("Port"))
        self.port_combo = QComboBox()
        self.port_combo.setMinimumWidth(200)
        lay.addWidget(self.port_combo)
        self._refresh_ports(preferred_port)

        rescan_btn = QPushButton("Rescan")
        rescan_btn.clicked.connect(lambda: self._refresh_ports(None))
        lay.addWidget(rescan_btn)

        self.connect_btn = QPushButton("Connect")
        self.connect_btn.clicked.connect(self._toggle_connect)
        lay.addWidget(self.connect_btn)

        sep1 = QFrame()
        sep1.setFrameShape(QFrame.Shape.VLine)
        lay.addWidget(sep1)

        send_all_btn = QPushButton("Send all")
        send_all_btn.setObjectName("AccentButton")
        send_all_btn.clicked.connect(self.send_all)
        lay.addWidget(send_all_btn)

        reset_btn = QPushButton("Reset to defaults")
        reset_btn.clicked.connect(self.reset_defaults)
        lay.addWidget(reset_btn)

        lay.addStretch(1)

        # Status
        self.status_dot = QLabel("●")
        self.status_dot.setStyleSheet(
            f"color: {theme.PALETTES[self.mode]['off']}; font-size: 14px;"
        )
        lay.addWidget(self.status_dot)

        self.status_label = QLabel("not connected")
        self.status_label.setObjectName("MutedLabel")
        lay.addWidget(self.status_label)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.VLine)
        lay.addWidget(sep2)

        # Zoom Controls
        lay.addWidget(QLabel("Zoom"))
        btn_zoom_out = QPushButton("−")
        btn_zoom_out.setFixedWidth(24)
        btn_zoom_out.setToolTip("Zoom Out (Ctrl -)")
        btn_zoom_out.clicked.connect(self._zoom_out)
        lay.addWidget(btn_zoom_out)

        self.zoom_combo = QComboBox()
        for text, scale in ZOOM_LEVELS:
            self.zoom_combo.addItem(text, scale)
        self.zoom_combo.setCurrentIndex(self.zoom_combo.findData(1.00))
        self.zoom_combo.currentIndexChanged.connect(self._on_zoom_selected)
        lay.addWidget(self.zoom_combo)

        btn_zoom_in = QPushButton("+")
        btn_zoom_in.setFixedWidth(24)
        btn_zoom_in.setToolTip("Zoom In (Ctrl +)")
        btn_zoom_in.clicked.connect(self._zoom_in)
        lay.addWidget(btn_zoom_in)

        # Theme Selector Dropdown
        lay.addWidget(QLabel("Theme"))
        self.theme_combo = QComboBox()
        for key, p in theme.PALETTES.items():
            self.theme_combo.addItem(p["name"], key)

        idx = self.theme_combo.findData(self.mode)
        if idx != -1:
            self.theme_combo.setCurrentIndex(idx)
        self.theme_combo.currentIndexChanged.connect(self._on_theme_selected)
        lay.addWidget(self.theme_combo)

        return bar

    def _build_preset_bar(self) -> QWidget:
        bar = QFrame()
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        prev_btn = QPushButton("<")
        prev_btn.setFixedWidth(32)
        prev_btn.clicked.connect(self._preset_prev)
        lay.addWidget(prev_btn)

        self.preset_combo = QComboBox()
        self.preset_combo.setMinimumWidth(260)
        self.preset_combo.setMaxVisibleItems(25)
        self.preset_combo.currentIndexChanged.connect(self._preset_number_committed)
        lay.addWidget(self.preset_combo, 1)

        next_btn = QPushButton(">")
        next_btn.setFixedWidth(32)
        next_btn.clicked.connect(self._preset_next)
        lay.addWidget(next_btn)

        self.preset_dirty_label = QLabel("")
        self.preset_dirty_label.setFixedWidth(12)
        lay.addWidget(self.preset_dirty_label)

        self.preset_name_entry = QLineEdit("Init")
        self.preset_name_entry.setMaxLength(16)
        self.preset_name_entry.setFixedWidth(170)
        self.preset_name_entry.textChanged.connect(self._refresh_dirty)

        lay.addWidget(self.preset_name_entry)

        for text, slot_fn in (
            ("Load", self._preset_load),
            ("Save", lambda: self._preset_save(None)),
            ("Save as…", self._preset_save_as),
            ("Init", self._preset_init),
            ("Browse…", self._open_browser),
        ):
            btn = QPushButton(text)
            btn.clicked.connect(slot_fn)
            lay.addWidget(btn)

        file_btn = QPushButton("File ▼")
        menu = QMenu(file_btn)
        menu.addAction("Export patch…", self._export_patch_file)
        menu.addAction("Import patch…", self._import_patch_file)
        menu.addSeparator()
        menu.addAction("Export bank…", self._export_bank_file)
        menu.addAction("Import bank…", self._import_bank_file)
        file_btn.setMenu(menu)
        lay.addWidget(file_btn)

        return bar

    def _build_log_pane(self) -> QWidget:
        box = QGroupBox("Board output")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(6, 6, 6, 6)

        self.log_text = QPlainTextEdit()
        self.log_text.setObjectName("LogView")
        self.log_text.setReadOnly(True)
        lay.addWidget(self.log_text)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self.log_text.clear)
        btn_row.addWidget(clear_btn)
        lay.addLayout(btn_row)

        return box

    def _scrollable(self) -> tuple[QScrollArea, QWidget, QVBoxLayout]:
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        lay = QVBoxLayout(content)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(10)
        area.setWidget(content)
        return area, content, lay

    def _build_tabs(self) -> None:
        for group in params.GROUP_ORDER:
            area, _, lay = self._scrollable()
            self.tabs.addTab(area, group)

            if group == params.GROUP_OSC:
                self._build_osc_tab(lay)
            elif group == params.GROUP_ENV:
                self._build_env_tab(lay)
            elif group == params.GROUP_CHARACTER:
                for param in [p for p in params.PARAMS if p.group == group]:
                    self._add_param_widget(lay, param)
                self._add_character_jitter_sliders(lay)
                lay.addStretch(1)
            elif group == params.GROUP_CAL:
                for block in [b for b in params.BLOCKS if b.group == group]:
                    self._add_block_widget(lay, block)
                for param in [p for p in params.PARAMS if p.group == group]:
                    self._add_param_widget(lay, param)
                self._add_manual_cal_indicator(lay)
                self._wire_manual_cal_recall()
                self._add_pio_pulse_slider(lay)
                self._add_cal_diag_panel(
                    lay,
                    "Dev tables",
                    params.CAL_DEBUG_COMMANDS,
                    "Seed force-writes fake amp-comp + PW tables. Verify sweep measures duty errors.",
                )
                self._add_cal_diag_panel(
                    lay,
                    "Amp-comp calibration method",
                    models.filter_debug_commands(params.AMP_CAL_METHOD_COMMANDS),
                    "Search used to build amp-comp tables. Runtime-only.",
                )
                self._add_cal_backup_panel(lay)
                lay.addStretch(1)
            else:
                for block in [b for b in params.BLOCKS if b.group == group]:
                    self._add_block_widget(lay, block)
                for param in [p for p in params.PARAMS if p.group == group]:
                    self._add_param_widget(lay, param)
                lay.addStretch(1)

        # Diagnostics Tab
        area, _, lay = self._scrollable()
        self.tabs.addTab(area, params.GROUP_DIAG)
        self._build_diag_tab(lay)

    # --- Oscillators Tab ---

    def _build_osc_tab(self, parent_layout: QVBoxLayout) -> None:
        top_split = QHBoxLayout()

        left_box = QGroupBox("Pitch & Sync")
        left_lay = QVBoxLayout(left_box)
        for pid in OSC_PITCH_PIDS + OSC_SYNC_PIDS:
            if pid in PARAM_BY_PID:
                self._add_param_widget(left_lay, PARAM_BY_PID[pid])
        left_lay.addStretch(1)
        top_split.addWidget(left_box, 1)

        right_box = QGroupBox("Voice & Drift")
        right_lay = QVBoxLayout(right_box)
        for pid in OSC_VOICE_PIDS:
            if pid in PARAM_BY_PID:
                self._add_param_widget(right_lay, PARAM_BY_PID[pid])
        right_lay.addStretch(1)
        top_split.addWidget(right_box, 1)

        parent_layout.addLayout(top_split)

        # Levels
        lvl_box = QGroupBox("Levels")
        lvl_lay = QGridLayout(lvl_box)
        col = 0
        for pid in OSC_LEVEL_PIDS:
            p = PARAM_BY_PID[pid]
            if p.hidden:
                continue
            lbl = QLabel(p.label)
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(p.lo, p.hi)
            slider.setValue(p.default)
            rd = QLabel(str(p.default))
            rd.setObjectName("ReadoutLabel")

            slider.valueChanged.connect(
                lambda val, pid=p.pid, rd=rd: self._on_slider_changed(pid, val, rd)
            )
            lvl_lay.addWidget(lbl, 0, col)
            lvl_lay.addWidget(slider, 1, col)
            lvl_lay.addWidget(rd, 2, col, Qt.AlignmentFlag.AlignCenter)
            self.param_widgets[p.pid] = slider
            self._readouts[("p", p.pid)] = rd
            col += 1
        parent_layout.addWidget(lvl_box)

        # Waveforms Matrix
        wave_box = QGroupBox("Waveforms")
        wave_lay = QGridLayout(wave_box)
        for c, col_name in enumerate(OSC_WAVE_COLS, start=1):
            wave_lay.addWidget(QLabel(col_name), 0, c, Qt.AlignmentFlag.AlignCenter)
        for r, (row_name, pids) in enumerate(OSC_WAVE_MATRIX, start=1):
            wave_lay.addWidget(QLabel(row_name), r, 0, Qt.AlignmentFlag.AlignRight)
            for c, pid in enumerate(pids, start=1):
                p = PARAM_BY_PID[pid]
                if not p.hidden:
                    chk = QCheckBox()
                    chk.setChecked(bool(p.default))
                    chk.toggled.connect(
                        lambda checked, pid=p.pid: self._on_check_toggled(pid, checked)
                    )
                    wave_lay.addWidget(chk, r, c, Qt.AlignmentFlag.AlignCenter)
                    self.param_widgets[p.pid] = chk
        parent_layout.addWidget(wave_box)
        parent_layout.addStretch(1)

    # --- Envelopes Tab ---

    def _build_env_tab(self, parent_layout: QVBoxLayout) -> None:
        times_box = QGroupBox("ADSR Envelopes")
        times_lay = QHBoxLayout(times_box)

        for bkey in ENV_ADSR_BLOCKS:
            block = self.blocks_by_key[bkey]
            bbox = QGroupBox(block.label)
            blay = QHBoxLayout(bbox)
            self.block_widgets[bkey] = {}
            for f in block.fields:
                col = QVBoxLayout()
                col.addWidget(QLabel(f.label), 0, Qt.AlignmentFlag.AlignCenter)
                slider = QSlider(Qt.Orientation.Vertical)
                slider.setRange(f.lo, f.hi)
                slider.setValue(f.default)
                slider.setMinimumHeight(140)
                rd = QLabel(str(f.default))
                rd.setObjectName("ReadoutLabel")

                slider.valueChanged.connect(
                    lambda val, bkey=bkey, fkey=f.key, rd=rd: self._on_block_slider_changed(
                        bkey, fkey, val, rd
                    )
                )
                col.addWidget(slider, 1, Qt.AlignmentFlag.AlignCenter)
                col.addWidget(rd, 0, Qt.AlignmentFlag.AlignCenter)
                blay.addLayout(col)
                self.block_widgets[bkey][f.key] = slider
                self._readouts[("b", bkey, f.key)] = rd
            times_lay.addWidget(bbox)
        parent_layout.addWidget(times_box)

        # Curves
        curves_box = QGroupBox("Curves & Routing")
        curves_lay = QHBoxLayout(curves_box)
        for col_name, a_pid, d_pid, rel_pid, r_pid in ENV_CURVE_COLUMNS:
            col_box = QGroupBox(col_name)
            clay = QVBoxLayout(col_box)
            for pid, title in ((a_pid, "Attack"), (d_pid, "Decay"), (rel_pid, "Release")):
                if pid is not None and pid in PARAM_BY_PID:
                    clay.addWidget(QLabel(title))
                    p = PARAM_BY_PID[pid]
                    cb = QComboBox()
                    for label, val in p.choices:
                        cb.addItem(label, val)
                    cb.setCurrentIndex(
                        next(i for i, c in enumerate(p.choices) if c[1] == p.default)
                    )
                    cb.currentIndexChanged.connect(
                        lambda idx, pid=p.pid, cb=cb: self._on_combo_changed(
                            pid, cb.itemData(idx)
                        )
                    )
                    clay.addWidget(cb)
                    self.param_widgets[p.pid] = cb
            if r_pid is not None:
                rp = PARAM_BY_PID[r_pid]
                chk = QCheckBox(rp.label)
                chk.setChecked(bool(rp.default))
                chk.toggled.connect(
                    lambda checked, pid=rp.pid: self._on_check_toggled(pid, checked)
                )
                clay.addWidget(chk)
                self.param_widgets[rp.pid] = chk
            clay.addStretch(1)
            curves_lay.addWidget(col_box)
        parent_layout.addWidget(curves_box)

        for p in params.PARAMS:
            if p.group == params.GROUP_ENV and p.pid not in ENV_CURVE_RESTART_PIDS:
                self._add_param_widget(parent_layout, p)
        parent_layout.addStretch(1)

    # --- Diagnostics Tab ---

# --- Diagnostics Tab ---

    def _build_diag_tab(self, parent_layout: QVBoxLayout) -> None:
        grid = QGridLayout()
        panel_specs = [
            (
                "Diagnostics",
                models.filter_debug_commands(params.DEBUG_COMMANDS),
                "RAM dumps, period probes, note retrig.",
                0,
                0,
            ),
            (
                "Hot-path profiler",
                params.BENCH_COMMANDS,
                "Needs RUNNING_AVERAGE in firmware.",
                0,
                1,
            ),
            (
                "Amp-comp method / bench",
                params.AMP_COMP_COMMANDS,
                "Speed/accuracy benchmarks.",
                1,
                0,
            ),
            (
                "Pitch-interp bench",
                params.PITCH_INTERP_COMMANDS,
                "Compares FLOAT / RATIO_Q16 / Q12.",
                1,
                1,
            ),
            (
                "Clkdiv methods",
                params.CLKDIV_HP_COMMANDS,
                "Speed & accuracy vs GOLD_REF.",
                2,
                0,
            ),
        ]
        if models.active().has_mainboard:
            panel_specs.append(
                (
                    "Mainboard profiler",
                    params.BENCH_MB_COMMANDS,
                    "Forwards 45 and 42 to STM32 Mainboard over Serial2.",
                    2,
                    1,
                )
            )

        # MCP4728 DACs panel (Probe: debug 43, Reattach: debug 44)
        mcp_row, mcp_col = (3, 0) if models.active().has_mainboard else (2, 1)
        panel_specs.append(
            (
                "MCP4728 DACs",
                params.MCP_DAC_COMMANDS,
                "Output appears in the Board pane. DCO4 forwards 43/44 to the STM32 "
                "Mainboard over Serial2. DCO3 runs them when ENABLE_MCP4728 is on.",
                mcp_row,
                mcp_col,
            )
        )

        for title, cmds, note, r, c in panel_specs:
            box = QGroupBox(title)
            lay = QVBoxLayout(box)
            btn_grid = QGridLayout()
            for i, (label, val) in enumerate(cmds):
                btn = QPushButton(label)
                btn.clicked.connect(lambda _, v=val, l=label: self._send_debug_cmd(v, l))
                btn_grid.addWidget(btn, i // 2, i % 2)
            lay.addLayout(btn_grid)
            note_lbl = QLabel(note)
            note_lbl.setObjectName("MutedLabel")
            note_lbl.setWordWrap(True)
            lay.addWidget(note_lbl)
            grid.addWidget(box, r, c)

        parent_layout.addLayout(grid)
        parent_layout.addStretch(1)

    # --- Generic Widget Builders ---

    def _add_param_widget(self, parent_layout: QVBoxLayout, p: params.Param) -> None:
        if p.hidden:
            return
        row = QHBoxLayout()
        lbl = QLabel(f"{p.label} [{p.pid}]")
        lbl.setMinimumWidth(220)
        row.addWidget(lbl)

        if p.pid == PID_MANUAL_CAL_STAGE:
            self._add_manual_cal_stage_row(row)
        elif p.kind == "slider":
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(p.lo, p.hi)
            slider.setValue(p.default)
            rd = QLabel(str(p.default))
            rd.setObjectName("ReadoutLabel")
            rd.setFixedWidth(50)
            rd.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            slider.valueChanged.connect(
                lambda val, pid=p.pid, rd=rd: self._on_slider_changed(pid, val, rd)
            )
            row.addWidget(slider, 1)
            row.addWidget(rd)
            self.param_widgets[p.pid] = slider
            self._readouts[("p", p.pid)] = rd
            if p.pid in CAL_KIND_PIDS:
                self._cal_kind_rows[p.pid] = (lbl, slider)
        elif p.kind == "combo":
            cb = QComboBox()
            for label, val in p.choices:
                cb.addItem(label, val)
            cb.setCurrentIndex(
                next((i for i, c in enumerate(p.choices) if c[1] == p.default), 0)
            )
            cb.currentIndexChanged.connect(
                lambda idx, pid=p.pid, cb=cb: self._on_combo_changed(pid, cb.itemData(idx))
            )
            row.addWidget(cb, 1)
            self.param_widgets[p.pid] = cb
        elif p.kind == "check":
            chk = QCheckBox()
            chk.setChecked(bool(p.default))
            chk.toggled.connect(
                lambda checked, pid=p.pid: self._on_check_toggled(pid, checked)
            )
            row.addWidget(chk, 1)
            self.param_widgets[p.pid] = chk
        elif p.kind == "pulse":
            if p.pid == PID_RUN_AUTOTUNE:
                self._add_cal_run_buttons(row, p)
            else:
                btn = QPushButton("Send")
                btn.clicked.connect(
                    lambda _, pid=p.pid, pv=p.pulse_value, l=p.label: self._on_pulse_clicked(
                        pid, pv, l
                    )
                )
                row.addWidget(btn)
                self._pulse_buttons[p.pid] = btn

        parent_layout.addLayout(row)

    def _add_block_widget(self, parent_layout: QVBoxLayout, block: params.Block) -> None:
        box = QGroupBox(block.label)
        lay = QVBoxLayout(box)
        self.block_widgets[block.key] = {}
        for f in block.fields:
            row = QHBoxLayout()
            row.addWidget(QLabel(f.label), 0)
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(f.lo, f.hi)
            slider.setValue(f.default)
            rd = QLabel(str(f.default))
            rd.setObjectName("ReadoutLabel")
            rd.setFixedWidth(50)
            rd.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            slider.valueChanged.connect(
                lambda val, bkey=block.key, fkey=f.key, rd=rd: self._on_block_slider_changed(
                    bkey, fkey, val, rd
                )
            )
            row.addWidget(slider, 1)
            row.addWidget(rd)
            lay.addLayout(row)
            self.block_widgets[block.key][f.key] = slider
            self._readouts[("b", block.key, f.key)] = rd
        parent_layout.addWidget(box)

    def _add_manual_cal_stage_row(self, row: QHBoxLayout) -> None:
        osc_labels = [
            calstages.osc_label(o) for o in range(models.active().num_oscillators)
        ]
        self._cal_osc_combo = QComboBox()
        self._cal_osc_combo.addItems(osc_labels)
        self._cal_osc_combo.currentIndexChanged.connect(self._manual_cal_on_osc_picked)
        row.addWidget(self._cal_osc_combo)

        self._cal_sub_combo = QComboBox()
        self._cal_sub_combo.currentIndexChanged.connect(
            self._manual_cal_on_substage_picked
        )
        row.addWidget(self._cal_sub_combo)

        self._cal_stage_readout = QLabel("")
        self._cal_stage_readout.setObjectName("MutedLabel")
        row.addWidget(self._cal_stage_readout, 1)

        self._manual_cal_fill_substages(0, calstages.KIND_SAW)
        self._manual_cal_update_stage_readout()

    def _add_cal_run_buttons(self, row: QHBoxLayout, p: params.Param) -> None:
        for text, val in params.CAL_SCOPE_BUTTONS:
            btn = QPushButton(text)
            btn.clicked.connect(lambda _, v=val, t=text: self._on_run_autotune(v, t))
            row.addWidget(btn)
            if val == params.CAL_SCOPE_FULL:
                self._pulse_buttons[p.pid] = btn

        stop_btn = QPushButton("Stop")
        stop_btn.clicked.connect(lambda: self._send_autotune_pulse(0, "Stop"))
        row.addWidget(stop_btn)

        # Radios
        for name, offset in params.CAL_PRECISION_CHOICES:
            rb = QRadioButton(name)
            if offset == 0:
                rb.setChecked(True)
            rb.toggled.connect(
                lambda checked, off=offset: self._on_precision_toggled(checked, off)
            )
            row.addWidget(rb)

    def _add_character_jitter_sliders(self, parent_layout: QVBoxLayout) -> None:
        box = QGroupBox("Diagnostic Noise Jitters (PARAM_DEBUG_COMMAND 160)")
        lay = QVBoxLayout(box)
        for label_text, type_hi in params.CHARACTER_JITTERS:
            row = QHBoxLayout()
            row.addWidget(QLabel(label_text))
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(params.CHARACTER_JITTER_LO, params.CHARACTER_JITTER_HI)
            slider.setValue(params.CHARACTER_JITTER_DEFAULT)
            rd = QLabel(str(params.CHARACTER_JITTER_DEFAULT))
            rd.setObjectName("ReadoutLabel")
            rd.setFixedWidth(50)
            slider.valueChanged.connect(
                lambda val, hi=type_hi, rd=rd: self._on_char_jitter_changed(hi, val, rd)
            )
            row.addWidget(slider, 1)
            row.addWidget(rd)
            lay.addLayout(row)
            self.character_jitter_sliders[type_hi] = slider
            self._readouts[("char_jitter", type_hi)] = rd
        parent_layout.addWidget(box)

    def _add_pio_pulse_slider(self, parent_layout: QVBoxLayout) -> None:
        row = QHBoxLayout()
        row.addWidget(QLabel("PIO pulse length (Y)"))
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(params.PIO_PULSE_LO, params.PIO_PULSE_HI)
        slider.setValue(params.PIO_PULSE_DEFAULT)
        rd = QLabel(str(params.PIO_PULSE_DEFAULT))
        rd.setObjectName("ReadoutLabel")
        rd.setFixedWidth(60)
        slider.valueChanged.connect(lambda val, rd=rd: self._on_pio_pulse_changed(val, rd))
        row.addWidget(slider, 1)
        row.addWidget(rd)
        self.pio_pulse_slider = slider
        self._readouts[("pio_pulse",)] = rd
        parent_layout.addLayout(row)

    def _add_cal_diag_panel(
        self, parent_layout: QVBoxLayout, title: str, cmds: tuple, note: str
    ) -> None:
        if not cmds:
            return
        box = QGroupBox(title)
        lay = QVBoxLayout(box)
        btn_row = QHBoxLayout()
        for label, val in cmds:
            btn = QPushButton(label)
            btn.clicked.connect(lambda _, v=val, l=label: self._send_debug_cmd(v, l))
            btn_row.addWidget(btn)
        lay.addLayout(btn_row)
        note_lbl = QLabel(note)
        note_lbl.setObjectName("MutedLabel")
        note_lbl.setWordWrap(True)
        lay.addWidget(note_lbl)
        parent_layout.addWidget(box)

    def _add_cal_backup_panel(self, parent_layout: QVBoxLayout) -> None:
        box = QGroupBox("Calibration Backup")
        lay = QHBoxLayout(box)
        dump_btn = QPushButton("Dump board → file…")
        dump_btn.clicked.connect(self._cal_dump_to_file)
        load_btn = QPushButton("Load file → board…")
        load_btn.clicked.connect(self._cal_load_from_file)
        lay.addWidget(dump_btn)
        lay.addWidget(load_btn)
        note = QLabel("Dump/restore 7 LittleFS cal tables as JSON.")
        note.setObjectName("MutedLabel")
        lay.addWidget(note, 1)
        parent_layout.addWidget(box)

    def _add_manual_cal_indicator(self, parent_layout: QVBoxLayout) -> None:
        self._manual_cal_indicator = QLabel("")
        self._manual_cal_indicator.setObjectName("MutedLabel")
        self._manual_cal_indicator.setWordWrap(True)
        parent_layout.addWidget(self._manual_cal_indicator)
        self._update_manual_cal_indicator()

    # --- Interaction Handlers ---

    def _on_slider_changed(self, pid: int, value: int, rd: QLabel) -> None:
        rd.setText(param_meta.format_display_value(pid, value))
        if self._preset_loading:
            return
        self.queue_param(pid, value)
        self._manual_cal_on_param_changed(pid, value)

    def _on_block_slider_changed(
        self, bkey: str, fkey: str, value: int, rd: QLabel
    ) -> None:
        rd.setText(str(value))
        if self._preset_loading:
            return
        self.queue_block(bkey)

    def _on_combo_changed(self, pid: int, value: int) -> None:
        if self._preset_loading:
            return
        self.queue_param(pid, value)
        if pid == PID_MANUAL_CAL_MODE and value != 0:
            self._manual_cal_on_mode_enabled()

    def _on_check_toggled(self, pid: int, checked: bool) -> None:
        if self._preset_loading:
            return
        val = 1 if checked else 0
        self.queue_param(pid, val)
        if pid == PID_MANUAL_CAL_MODE and val != 0:
            self._manual_cal_on_mode_enabled()

    def _on_pulse_clicked(self, pid: int, val: int, label: str) -> None:
        if not self._confirm_pulse(pid):
            return
        self.send_now(protocol.param16(pid, val))
        self.log(f"[send] {label} (param {pid} = {val})\n")
        if pid == PID_MANUAL_CAL_STORE:
            self._manual_cal_on_stored()

    def _on_precision_toggled(self, checked: bool, offset: int) -> None:
        if checked:
            self._cal_precision_offset = offset

    def _on_run_autotune(self, value: int, text: str) -> None:
        if not self._confirm_pulse(PID_RUN_AUTOTUNE):
            return
        wire = value + self._cal_precision_offset
        self.send_now(protocol.param16(PID_RUN_AUTOTUNE, wire))
        self.log(f"[send] Run {text} calibration (param {PID_RUN_AUTOTUNE} = {wire})\n")

    def _send_autotune_pulse(self, wire: int, name: str) -> None:
        self.send_now(protocol.param16(PID_RUN_AUTOTUNE, wire))
        self.log(f"[send] {name} calibration (param {PID_RUN_AUTOTUNE} = {wire})\n")

    def _send_debug_cmd(self, value: int, label: str) -> None:
        self.send_now(protocol.param16(params.DEBUG_PARAM_ID, value))
        self.log(f"[send] {label}\n")

    def _on_char_jitter_changed(self, type_hi: int, value: int, rd: QLabel) -> None:
        rd.setText(str(value))
        if self._preset_loading:
            return
        self.queue_debug_u16((type_hi << 8) | value)

    def _on_pio_pulse_changed(self, value: int, rd: QLabel) -> None:
        rd.setText(str(value))
        if self._preset_loading:
            return
        self.queue_debug_u16(value)

    # --- Manual Calibration Logic ---

    def _manual_cal_fill_substages(self, osc: int, keep_kind: int | None) -> None:
        if self._cal_sub_combo is None:
            return
        kinds = calstages.kinds_for_osc(osc)
        self._cal_sub_kinds = kinds
        self._cal_sub_combo.blockSignals(True)
        self._cal_sub_combo.clear()
        for k in kinds:
            self._cal_sub_combo.addItem(calstages.kind_label(k), k)
        kind = keep_kind if keep_kind in kinds else kinds[0]
        self._cal_sub_combo.setCurrentIndex(kinds.index(kind))
        self._cal_sub_combo.blockSignals(False)

    def _manual_cal_on_osc_picked(self) -> None:
        if self._cal_osc_combo is None:
            return
        osc = self._cal_osc_combo.currentIndex()
        kind = (
            self._cal_sub_combo.currentData()
            if self._cal_sub_combo
            else calstages.KIND_SAW
        )
        self._manual_cal_fill_substages(osc, kind)
        self._manual_cal_send_stage()

    def _manual_cal_on_substage_picked(self) -> None:
        self._manual_cal_send_stage()

    def _manual_cal_send_stage(self) -> None:
        if self._cal_osc_combo is None or self._cal_sub_combo is None:
            return
        osc = self._cal_osc_combo.currentIndex()
        kind = self._cal_sub_combo.currentData()
        stage = calstages.stage_for(osc, kind)
        self._manual_cal_update_stage_readout()
        if self._preset_loading or self._manual_cal_syncing:
            return
        self.queue_param(PID_MANUAL_CAL_STAGE, stage)
        self._manual_cal_sync_controls()

    def _manual_cal_update_stage_readout(self) -> None:
        if (
            self._cal_stage_readout is None
            or self._cal_osc_combo is None
            or self._cal_sub_combo is None
        ):
            return
        osc = self._cal_osc_combo.currentIndex()
        kind = self._cal_sub_combo.currentData()
        stage = calstages.stage_for(osc, kind)
        self._cal_stage_readout.setText(calstages.stage_text(stage))

    def _manual_cal_apply_stage_enables(self) -> None:
        if self._cal_osc_combo is None or self._cal_sub_combo is None:
            return
        osc = self._cal_osc_combo.currentIndex()
        kind = self._cal_sub_combo.currentData()
        for pid, (lbl, widget) in self._cal_kind_rows.items():
            live = kind in CAL_KIND_PIDS.get(pid, ())
            widget.setEnabled(live)
            lbl.setStyleSheet(
                "" if live else f"color: {theme.PALETTES[self.mode]['muted']};"
            )

    def _wire_manual_cal_recall(self) -> None:
        self._manual_cal_apply_stage_enables()

    def _manual_cal_on_mode_enabled(self) -> None:
        if self._manual_cal_syncing or self._preset_loading:
            return
        if self._manual_cal_live is not None and self._manual_cal_all_dirty():
            self._manual_cal_sync_controls()
            return
        self._manual_cal_refresh_from_board()


# In app.py inside class App:

    def _manual_cal_refresh_from_board(self) -> None:
        if not self._mcu_ready():
            return

        def pwcal_done(ok, payload):
            if ok:
                try:
                    channels = fileformats.decode_cal_table("PWCal3Pt", payload)
                    # Extract Point 1 (Mid / 440 Hz anchor) center for live UI slider
                    self._pwcenter_live = [int(ch[1]["center"]) for ch in channels]
                    self._pwcenter_baseline = list(self._pwcenter_live)
                    self._pwcenter_dirty.clear()
                except (ValueError, KeyError, IndexError) as e:
                    self.log(f"[mcu] PW 3-point recall err: {e}\n")
            self._manual_cal_sync_controls()
            self._update_manual_cal_indicator()

        def dutytrim_done(ok, payload):
            if ok:
                try:
                    self._dutytrim_live = list(
                        fileformats.decode_cal_table("AmpCompDutyOffset", payload)
                    )
                    self._dutytrim_baseline = list(self._dutytrim_live)
                    self._dutytrim_dirty.clear()
                except ValueError as e:
                    self.log(f"[mcu] duty trim recall err: {e}\n")
            self.mcu.dump_cal_table("PWCal3Pt", pwcal_done)

        def amp440_done(ok, payload):
            if ok:
                try:
                    self._amp440_live = list(
                        fileformats.decode_cal_table("AmpComp440", payload)
                    )
                    self._amp440_baseline = list(self._amp440_live)
                    self._amp440_dirty.clear()
                except ValueError as e:
                    self.log(f"[mcu] amp comp 440 err: {e}\n")
            self.mcu.dump_cal_table("AmpCompDutyOffset", dutytrim_done)

        def manual_done(ok, payload):
            if ok:
                try:
                    self._manual_cal_live = list(
                        fileformats.decode_cal_table("ManualOffset", payload)
                    )
                    self._manual_cal_baseline = list(self._manual_cal_live)
                    self._manual_cal_dirty.clear()
                except ValueError as e:
                    self.log(f"[mcu] manual offset err: {e}\n")
            self.mcu.dump_cal_table("AmpComp440", amp440_done)

        self.mcu.dump_cal_table("ManualOffset", manual_done)
        if not self._mcu_ready():
            return

        def pwcal_done(ok, payload):
            if ok:
                try:
                    channels = fileformats.decode_cal_table("PWCal3Pt", payload)
                    # Extract Point 1 (Mid / 440 Hz anchor) center for live UI slider
                    self._pwcenter_live = [int(ch[1]["center"]) for ch in channels]
                    self._pwcenter_baseline = list(self._pwcenter_live)
                    self._pwcenter_dirty.clear()
                except (ValueError, KeyError, IndexError) as e:
                    self.log(f"[mcu] PW 3-point recall err: {e}\n")
            self._manual_cal_sync_controls()
            self._update_manual_cal_indicator()

        def dutytrim_done(ok, payload):
            if ok:
                try:
                    self._dutytrim_live = list(
                        fileformats.decode_cal_table("AmpCompDutyOffset", payload)
                    )
                    self._dutytrim_baseline = list(self._dutytrim_live)
                    self._dutytrim_dirty.clear()
                except ValueError as e:
                    self.log(f"[mcu] duty trim recall err: {e}\n")
            self.mcu.dump_cal_table("PWCal3Pt", pwcal_done)

        def amp440_done(ok, payload):
            if ok:
                try:
                    self._amp440_live = list(
                        fileformats.decode_cal_table("AmpComp440", payload)
                    )
                    self._amp440_baseline = list(self._amp440_live)
                    self._amp440_dirty.clear()
                except ValueError as e:
                    self.log(f"[mcu] amp comp 440 err: {e}\n")
            self.mcu.dump_cal_table("AmpCompDutyOffset", dutytrim_done)

        def manual_done(ok, payload):
            if ok:
                try:
                    self._manual_cal_live = list(
                        fileformats.decode_cal_table("ManualOffset", payload)
                    )
                    self._manual_cal_baseline = list(self._manual_cal_live)
                    self._manual_cal_dirty.clear()
                except ValueError as e:
                    self.log(f"[mcu] manual offset err: {e}\n")
            self.mcu.dump_cal_table("AmpComp440", amp440_done)

        self.mcu.dump_cal_table("ManualOffset", manual_done)
        if not self._mcu_ready():
            return

    def _manual_cal_sync_controls(self) -> None:
        if self._cal_osc_combo is None:
            return
        osc = self._cal_osc_combo.currentIndex()
        self._manual_cal_syncing = True
        try:
            if self._manual_cal_live and osc < len(self._manual_cal_live):
                self._set_widget_value(PID_MANUAL_CAL_OFFSET, self._manual_cal_live[osc])
            if self._amp440_live and osc < len(self._amp440_live):
                stored = self._amp440_live[osc]
                p = PARAM_BY_PID[PID_AMP_COMP_440]
                if p.lo <= stored <= p.hi:
                    self._set_widget_value(PID_AMP_COMP_440, stored)
                else:
                    rd = self._readouts.get(("p", PID_AMP_COMP_440))
                    if rd:
                        rd.setText(str(stored))
            if self._dutytrim_live and osc < len(self._dutytrim_live):
                self._set_widget_value(
                    PID_AMP_COMP_DUTY_OFFSET, self._dutytrim_live[osc]
                )
            ch = calstages.pw_channel(osc)
            if (
                self._pwcenter_live
                and ch < len(self._pwcenter_live)
                and PID_CAL_PW_CENTER in self.param_widgets
            ):
                self._set_widget_value(PID_CAL_PW_CENTER, self._pwcenter_live[ch])
        finally:
            self._manual_cal_syncing = False
        self._manual_cal_apply_stage_enables()

    def _manual_cal_on_param_changed(self, pid: int, value: int) -> None:
        if self._manual_cal_syncing or self._cal_osc_combo is None:
            return
        osc = self._cal_osc_combo.currentIndex()
        if pid == PID_MANUAL_CAL_OFFSET and self._manual_cal_live:
            self._track_edit(
                self._manual_cal_live,
                self._manual_cal_baseline,
                self._manual_cal_dirty,
                osc,
                value,
            )
        elif pid == PID_AMP_COMP_440 and self._amp440_live:
            self._track_edit(
                self._amp440_live,
                self._amp440_baseline,
                self._amp440_dirty,
                osc,
                value,
            )
        elif pid == PID_AMP_COMP_DUTY_OFFSET and self._dutytrim_live:
            self._track_edit(
                self._dutytrim_live,
                self._dutytrim_baseline,
                self._dutytrim_dirty,
                osc,
                value,
            )
        elif pid == PID_CAL_PW_CENTER and self._pwcenter_live:
            self._track_edit(
                self._pwcenter_live,
                self._pwcenter_baseline,
                self._pwcenter_dirty,
                calstages.pw_channel(osc),
                value,
            )

    def _track_edit(
        self,
        live: list[int],
        baseline: list[int] | None,
        dirty: set[int],
        idx: int,
        val: int,
    ) -> None:
        if idx >= len(live):
            return
        live[idx] = val
        stored = baseline[idx] if baseline and idx < len(baseline) else 0
        if val != stored:
            dirty.add(idx)
        else:
            dirty.discard(idx)
        self._update_manual_cal_indicator()

    def _manual_cal_all_dirty(self) -> bool:
        return bool(
            self._manual_cal_dirty
            or self._amp440_dirty
            or self._dutytrim_dirty
            or self._pwcenter_dirty
        )

    def _update_manual_cal_indicator(self) -> None:
        if self._manual_cal_indicator is None:
            return
        if self._manual_cal_live is None:
            self._manual_cal_indicator.setText(
                "Manual cal values: not read from board yet — enable Manual cal mode to recall."
            )
        elif self._manual_cal_all_dirty():
            names = [
                f"OSC {calstages.osc_label(o)}"
                for o in sorted(
                    self._manual_cal_dirty | self._amp440_dirty | self._dutytrim_dirty
                )
            ]
            names += [f"PW ch {c}" for c in sorted(self._pwcenter_dirty)]
            self._manual_cal_indicator.setText(
                f"Manual cal values: unsaved changes for {', '.join(names)} — press Store before autotuning."
            )
        else:
            self._manual_cal_indicator.setText(
                "Manual cal values: matches what is stored on the board."
            )

    def _manual_cal_on_stored(self) -> None:
        if self._manual_cal_live:
            self._manual_cal_baseline = list(self._manual_cal_live)
        if self._amp440_live:
            self._amp440_baseline = list(self._amp440_live)
        if self._dutytrim_live:
            self._dutytrim_baseline = list(self._dutytrim_live)
        if self._pwcenter_live:
            self._pwcenter_baseline = list(self._pwcenter_live)
        self._manual_cal_dirty.clear()
        self._amp440_dirty.clear()
        self._dutytrim_dirty.clear()
        self._pwcenter_dirty.clear()
        self._update_manual_cal_indicator()

    def _confirm_pulse(self, pid: int) -> bool:
        if pid != PID_RUN_AUTOTUNE or not self._manual_cal_all_dirty():
            return True
        res = QMessageBox.warning(
            self,
            "Unsaved Manual Calibration Values",
            "You have unsaved manual calibration values. Auto calibration will reload LittleFS and discard them.",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
        )
        if res == QMessageBox.StandardButton.Cancel:
            return False
        if res == QMessageBox.StandardButton.Save:
            store = PARAM_BY_PID[PID_MANUAL_CAL_STORE]
            self.send_now(protocol.param16(PID_MANUAL_CAL_STORE, store.pulse_value))
            self._manual_cal_on_stored()
        return True

    # --- Preset Management ---

    def _init_presets(self) -> None:
        self.bank = presets.load_bank()
        self._refresh_preset_combo()
        self._preset_recall(int(self.bank["current"]), send=False, persist_current=False)
    def _refresh_preset_combo(self) -> None:
        """Re-populates the dropdown without triggering a load."""
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        for i in range(presets.NUM_SLOTS):
            slot = self.bank["slots"][i]
            name = "Init" if presets.slot_is_empty(slot) else slot["name"]
            self.preset_combo.addItem(f"{i:03d}: {name}")
        
        self.preset_combo.setCurrentIndex(int(self.bank.get("current", 0)))
        self.preset_combo.blockSignals(False)

    def _current_ui_slot(self, name: str | None = None) -> dict:
        def get_val(p: params.Param) -> int:
            w = self.param_widgets.get(p.pid)
            if isinstance(w, QSlider):
                return w.value()
            if isinstance(w, QComboBox):
                return w.currentData()
            if isinstance(w, QCheckBox):
                return 1 if w.isChecked() else 0
            return p.default

        slot = presets.defaults_slot(
            name or self.preset_name_entry.text().strip() or "Untitled"
        )
        for p in presets.patch_params():
            slot["params"][str(p.pid)] = get_val(p)
        for b in presets.patch_blocks():
            for f in b.fields:
                w = self.block_widgets.get(b.key, {}).get(f.key)
                if isinstance(w, QSlider):
                    slot["blocks"][b.key][f.key] = w.value()
        return slot

    def _refresh_dirty(self) -> None:
        if self._preset_loading:
            return
        fp = presets.slot_fingerprint(self._current_ui_slot())
        self.preset_dirty_label.setText("*" if fp != self._clean_fp else "")

    def _preset_recall(
        self, index: int, *, send: bool, persist_current: bool
    ) -> None:
        index = max(0, min(presets.NUM_SLOTS - 1, index))
        slot = self.bank["slots"][index]
        empty = presets.slot_is_empty(slot)
        self._preset_loading = True
        try:
            self._apply_slot_dict(None if empty else slot)
            name = "Init" if empty else slot["name"]
            self.preset_name_entry.setText(name)
            self._clean_fp = presets.slot_fingerprint(
                presets.defaults_slot() if empty else slot
            )
            self.preset_combo.blockSignals(True)
            self.preset_combo.setCurrentIndex(index)
            self.preset_combo.blockSignals(False)
            self.bank["current"] = index
        finally:
            self._preset_loading = False

        self._refresh_dirty()
        if persist_current:
            presets.save_bank(self.bank)
        if send and self.link.is_open:
            self.send_all()

    def _apply_slot_dict(self, slot: dict | None) -> None:
        data = (
            presets.defaults_slot()
            if presets.slot_is_empty(slot)
            else slot  # type: ignore[arg-type]
        )
        for p in presets.patch_params():
            val = int(data["params"].get(str(p.pid), p.default))
            self._set_widget_value(p.pid, val)
        for b in presets.patch_blocks():
            for f in b.fields:
                val = int(data["blocks"].get(b.key, {}).get(f.key, f.default))
                w = self.block_widgets.get(b.key, {}).get(f.key)
                if isinstance(w, QSlider):
                    w.setValue(val)
                    rd = self._readouts.get(("b", b.key, f.key))
                    if rd:
                        rd.setText(str(val))

    def _set_widget_value(self, pid: int, val: int) -> None:
        w = self.param_widgets.get(pid)
        if isinstance(w, QSlider):
            w.setValue(val)
            rd = self._readouts.get(("p", pid))
            if rd:
                rd.setText(param_meta.format_display_value(pid, val))
        elif isinstance(w, QComboBox):
            idx = w.findData(val)
            if idx != -1:
                w.setCurrentIndex(idx)
        elif isinstance(w, QCheckBox):
            w.setChecked(bool(val))

    def _preset_number_committed(self) -> None:
        idx = self.preset_combo.currentIndex()
        if idx == int(self.bank.get("current", -1)):
            return
        self._preset_recall(idx, send=True, persist_current=True)

    def _preset_prev(self) -> None:
        idx = (self.preset_combo.currentIndex() - 1) % presets.NUM_SLOTS
        self._preset_recall(idx, send=True, persist_current=True)

    def _preset_next(self) -> None:
        idx = (self.preset_combo.currentIndex() + 1) % presets.NUM_SLOTS
        self._preset_recall(idx, send=True, persist_current=True)

    def _preset_load(self) -> None:
        self._preset_recall(self.preset_combo.currentIndex(), send=True, persist_current=True)

    def _preset_save(self, index: int | None = None) -> None:
        if index is None:
            index = self.preset_combo.currentIndex()
        name = self.preset_name_entry.text().strip() or "Untitled"
        slot = self._current_ui_slot(name)
        self.bank["slots"][index] = slot
        self.bank["current"] = index
        presets.save_bank(self.bank)
        self._clean_fp = presets.slot_fingerprint(slot)
        self._refresh_preset_combo()
        self._refresh_dirty()
        self.log(f"[preset] saved {index:03d} {name}\n")
        if self._browser and self._browser.isVisible():
            self._browser.refresh()

    def _preset_save_as(self) -> None:
        dlg = SaveAsDialog(self, self.preset_combo.currentIndex(), self.preset_name_entry.text())
        if dlg.exec() == QDialog.DialogCode.Accepted:
            idx, name = dlg.get_data()
            
            # Apply name to UI
            self.preset_name_entry.setText(name)
            
            # Point combo to new index (silently)
            self.preset_combo.blockSignals(True)
            self.preset_combo.setCurrentIndex(idx)
            self.preset_combo.blockSignals(False)
            
            # Perform the save logic
            self._preset_save(idx)

    def _preset_init(self) -> None:
        self._preset_loading = True
        try:
            self._apply_slot_dict(None)
            self.preset_name_entry.setText("Init")
        finally:
            self._preset_loading = False
        self._refresh_dirty()
        self.log("[preset] Init defaults in UI -- Save to store in slot\n")
        if self.link.is_open:
            self.send_all()

    # --- File & Browser Dialogs ---

    def _open_browser(self) -> None:
        if self._browser is None:
            self._browser = PresetBrowser(self)
        self._browser.show()
        self._browser.raise_()
        self._browser.activateWindow()

    def _export_patch_file(self) -> None:
        slot = self._current_ui_slot()
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Patch", f"{slot['name']}.json", "JSON (*.json)"
        )
        if path:
            fileformats.save_patch_file(path, slot)
            self.log(f"[ui] exported patch to {path}\n")

    def _import_patch_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import Patch", "", "JSON (*.json)")
        if path:
            slot = fileformats.load_patch_file(path)
            self._preset_loading = True
            try:
                self._apply_slot_dict(slot)
                self.preset_name_entry.setText(slot["name"])
            finally:
                self._preset_loading = False
            self._refresh_dirty()
            if self.link.is_open:
                self.send_all()
            self.log(f"[ui] imported patch from {path}\n")

    def _export_bank_file(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Bank", "dco_bank.json", "JSON (*.json)"
        )
        if path:
            fileformats.save_bank_file(path, self.bank)
            self.log(f"[ui] exported bank to {path}\n")

    def _import_bank_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import Bank", "", "JSON (*.json)")
        if path:
            self.bank = fileformats.load_bank_file(path)
            presets.save_bank(self.bank)
            self._preset_recall(
                int(self.bank["current"]), send=True, persist_current=False
            )
            if self._browser:
                self._browser.refresh()
            self.log(f"[ui] imported bank from {path}\n")

    def _cal_dump_to_file(self) -> None:
        if not self._mcu_ready():
            return
        names = list(mcu_link.cal_tables())
        results: dict[str, bytes] = {}

        def step(k: int):
            if k >= len(names):
                path, _ = QFileDialog.getSaveFileName(
                    self,
                    "Save Calibration Dump",
                    "dco_calibration.json",
                    "JSON (*.json)",
                )
                if path:
                    fileformats.save_cal_file(path, results)
                    self.log(f"[mcu] saved cal dump to {path}\n")
                return
            name = names[k]

            def done(ok, payload):
                if ok:
                    results[name] = payload
                step(k + 1)

            self.mcu.dump_cal_table(name, done)

        self.log("[mcu] dumping calibration tables...\n")
        step(0)

    def _cal_load_from_file(self) -> None:
        if not self._mcu_ready():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Calibration File", "", "JSON (*.json)"
        )
        if not path:
            return
        tables = fileformats.load_cal_file(path)
        names = list(tables)

        def step(k: int):
            if k >= len(names):
                self.log(f"[mcu] calibration load finished\n")
                return
            name = names[k]
            self.mcu.push_cal_table(name, tables[name], lambda ok, _: step(k + 1))

        step(0)

    # --- Connection & IO ---

    def _mcu_ready(self) -> bool:
        if not self.link.is_open:
            self.log("[mcu] not connected\n")
            return False
        if self.mcu.busy:
            self.log("[mcu] transfer in progress\n")
            return False
        return True

    def _refresh_ports(self, preferred: str | None) -> None:
        self.port_combo.clear()
        ports = protocol.find_dco_ports()
        for p in ports:
            self.port_combo.addItem(f"{p.device} ({p.description})", p.device)
        if preferred:
            idx = self.port_combo.findData(preferred)
            if idx != -1:
                self.port_combo.setCurrentIndex(idx)

    def _toggle_connect(self) -> None:
        if self.link.is_open:
            self.mcu.cancel_all()
            self.link.close()
            self.connect_btn.setText("Connect")
            self.status_label.setText("not connected")
            self.status_dot.setStyleSheet(
                f"color: {theme.PALETTES[self.mode]['off']}; font-size: 14px;"
            )
            self.log("[link] disconnected\n")
            return

        device = self.port_combo.currentData()
        if not device:
            self.log("[link] no serial port selected\n")
            return
        try:
            self.link.open(device)
        except (OSError, ValueError) as e:
            self.log(f"[link] open failed: {e}\n")
            return
        self.connect_btn.setText("Disconnect")
        self.status_label.setText(f"connected: {device}")
        self.status_dot.setStyleSheet(
            f"color: {theme.PALETTES[self.mode]['ok']}; font-size: 14px;"
        )
        self.log(f"[link] connected to {device}\n")
        self.send_all()

    def _on_data_received(self, text: str) -> None:
        self.log(text)
        self._mcu_linebuf += text
        while "\n" in self._mcu_linebuf:
            line, self._mcu_linebuf = self._mcu_linebuf.split("\n", 1)
            self.mcu.feed_line(line.rstrip("\r"))
        if len(self._mcu_linebuf) > 4096:
            self._mcu_linebuf = ""

    def _on_write_failed(self, err: str) -> None:
        self.log(f"[link] write failed: {err}\n")
        self._toggle_connect()

    def log(self, text: str) -> None:
        text = text.replace("\r", "")
        cursor = self.log_text.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)

        fmt = QTextCharFormat()
        palette = theme.PALETTES[self.mode]
        if text.startswith("[link]"):
            fmt.setForeground(QColor(palette["accent"]))
        elif text.startswith("[send]") or text.startswith("[ui]"):
            fmt.setForeground(QColor(palette["muted"]))
        elif text.startswith("[mcu]"):
            fmt.setForeground(QColor(palette["ok"]))
        else:
            fmt.setForeground(QColor(palette["fg"]))

        cursor.insertText(text, fmt)
        self.log_text.setTextCursor(cursor)
        self.log_text.ensureCursorVisible()

    # --- Senders ---

    def queue_param(self, pid: int, val: int) -> None:
        self.pending[f"p{pid}"] = protocol.stuff(protocol.param16(pid, int(val)))

    def queue_debug_u16(self, val: int) -> None:
        self.pending["p_debug_u16"] = protocol.stuff(
            protocol.param16u(params.DEBUG_PARAM_ID, int(val))
        )

    def queue_block(self, key: str) -> None:
        block = self.blocks_by_key[key]
        vals = {f.key: self.block_widgets[key][f.key].value() for f in block.fields}  # type: ignore[union-attr]
        self.pending[f"b{key}"] = protocol.stuff(block.builder(vals))

    def send_now(self, frame: bytes) -> None:
        if self.link.is_open:
            self.link.send(protocol.stuff(frame))

    def _flush(self) -> None:
        if not self.pending or not self.link.is_open:
            return
        frames = list(self.pending.values())
        self.link.send(b"".join(frames))
        self.pending.clear()

    def send_all(self) -> int:
        if not self.link.is_open:
            return 0
        self.pending.clear()
        n = 0
        screen = models.active().has_screen_signals
        if screen:
            self.send_now(protocol.screen_signal(protocol.SCREEN_SIGNAL_SILENT))
            n += 1
            self.send_now(protocol.preset_name(self.preset_name_entry.text()))
            self.send_now(
                protocol.param16(
                    protocol.PARAM_UI_PRESET_SCROLL, self.preset_combo.currentIndex()
                )
            )
            n += 2            
        for p in presets.patch_params():
            w = self.param_widgets.get(p.pid)
            val = p.default
            if isinstance(w, QSlider):
                val = w.value()
            elif isinstance(w, QComboBox):
                val = w.currentData()
            elif isinstance(w, QCheckBox):
                val = 1 if w.isChecked() else 0
            self.queue_param(p.pid, val)
            n += 1
        for block in presets.patch_blocks():
            self.queue_block(block.key)
            n += 1
        self._flush()
        self.log(f"[send] patch {n} frames\n")
        return n

    def reset_defaults(self) -> None:
        self._preset_init()

    def closeEvent(self, event) -> None:
        self.link.close()
        event.accept()

class SaveAsDialog(QDialog):
    def __init__(self, app: App, current_idx: int, current_name: str) -> None:
        super().__init__(app)
        self.setWindowTitle("Save As...")
        self.resize(350, 400)
        
        layout = QVBoxLayout(self)
        
        layout.addWidget(QLabel("Select destination slot:"))
        
        # Scrollable list of slots
        self.list_widget = QListWidget()
        for i in range(presets.NUM_SLOTS):
            slot = app.bank["slots"][i]
            name = "Init" if presets.slot_is_empty(slot) else slot["name"]
            self.list_widget.addItem(f"{i:03d}: {name}")
        
        self.list_widget.setCurrentRow(current_idx)
        layout.addWidget(self.list_widget)
        
        layout.addWidget(QLabel("Preset Name:"))
        self.name_input = QLineEdit(current_name)
        layout.addWidget(self.name_input)
        
        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)
        
        save_btn = QPushButton("Save")
        save_btn.setObjectName("AccentButton")
        save_btn.clicked.connect(self.accept)
        btn_layout.addWidget(save_btn)
        
        layout.addLayout(btn_layout)

    def get_data(self) -> tuple[int, str]:
        idx = self.list_widget.currentRow()
        name = self.name_input.text().strip() or "Untitled"
        return idx, name

class PresetBrowser(QDialog):
    def __init__(self, app: App) -> None:
        super().__init__(app)
        self.app = app
        self.setWindowTitle("Preset Browser")
        self.resize(780, 660)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)

        # 1. Search & Filter Header
        filter_box = QHBoxLayout()
        filter_box.addWidget(QLabel("Search:"))
        self.filter_entry = QLineEdit()
        self.filter_entry.setPlaceholderText("Filter by slot number or preset name...")
        self.filter_entry.textChanged.connect(self._apply_filter)
        filter_box.addWidget(self.filter_entry, 1)

        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self.filter_entry.clear)
        filter_box.addWidget(clear_btn)
        lay.addLayout(filter_box)

        # 2. Presets Table
        self.table = QTableWidget(presets.NUM_SLOTS, 3)
        self.table.setHorizontalHeaderLabels(["Slot", "Local Patch Name", "Board (LittleFS) Name"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.cellDoubleClicked.connect(lambda: self._local_load())
        lay.addWidget(self.table)

        # 3. Local Bank Actions
        local_box = QGroupBox("Local Bank")
        l_lay = QHBoxLayout(local_box)
        for name, fn in (
            ("Load into Synth", self._local_load),
            ("Save Current UI into Slot", self._local_save_into),
            ("Rename…", self._local_rename),
            ("Delete Slot", self._local_delete),
        ):
            btn = QPushButton(name)
            btn.clicked.connect(fn)
            l_lay.addWidget(btn)
        lay.addWidget(local_box)

        # 4. Board (MCU) Actions
        mcu_box = QGroupBox("Board (MCU LittleFS)")
        m_lay = QHBoxLayout(mcu_box)
        for name, fn in (
            ("Refresh Board List", self._mcu_refresh_dir),
            ("Send Slot → Board", self._mcu_send_slot),
            ("Fetch Slot ← Board", self._mcu_fetch_slot),
            ("Recall on Board", self._mcu_recall_on_board),
            ("Push All → Board", self._mcu_push_all),
            ("Pull All ← Board", self._mcu_pull_all),
        ):
            btn = QPushButton(name)
            btn.clicked.connect(fn)
            m_lay.addWidget(btn)
        lay.addWidget(mcu_box)

        self.refresh()

    def _selected_row(self) -> int:
        rows = self.table.selectionModel().selectedRows()
        return rows[0].row() if rows else 0

    def refresh(self) -> None:
        active_idx = int(self.app.bank.get("current", -1))
        palette = theme.PALETTES[self.app.mode]
        accent_col = QColor(palette["accent"])
        muted_col = QColor(palette["muted"])

        for i in range(presets.NUM_SLOTS):
            slot = self.app.bank["slots"][i]
            is_active = (i == active_idx)
            
            # Column 0: Slot indicator
            slot_str = f"▶ {i:03d}" if is_active else f"  {i:03d}"
            item_slot = QTableWidgetItem(slot_str)
            if is_active:
                font = item_slot.font()
                font.setBold(True)
                item_slot.setFont(font)
                item_slot.setForeground(accent_col)
            self.table.setItem(i, 0, item_slot)

            # Column 1: Local Name
            if presets.slot_is_empty(slot):
                item_name = QTableWidgetItem("[Empty]")
                item_name.setForeground(muted_col)
            else:
                item_name = QTableWidgetItem(slot["name"])
                if is_active:
                    font = item_name.font()
                    font.setBold(True)
                    item_name.setFont(font)
                    item_name.setForeground(accent_col)
            self.table.setItem(i, 1, item_name)

            # Column 2: Board Name
            mcu_name = self.app.mcu_dir.get(i, "")
            item_mcu = QTableWidgetItem(mcu_name)
            self.table.setItem(i, 2, item_mcu)

        self._apply_filter()

    def _apply_filter(self) -> None:
        query = self.filter_entry.text().strip().lower()
        for i in range(presets.NUM_SLOTS):
            if not query:
                self.table.setRowHidden(i, False)
                continue

            slot = self.app.bank["slots"][i]
            local_name = ("" if presets.slot_is_empty(slot) else slot["name"]).lower()
            mcu_name = self.app.mcu_dir.get(i, "").lower()
            slot_num_str = f"{i:03d}"

            match = (query in slot_num_str or str(i) == query or query in local_name or query in mcu_name)
            self.table.setRowHidden(i, not match)

    def _local_load(self) -> None:
        idx = self._selected_row()
        self.app._preset_recall(idx, send=True, persist_current=True)
        self.refresh()

    def _local_save_into(self) -> None:
        idx = self._selected_row()
        slot = self.app.bank["slots"][idx]
        if not presets.slot_is_empty(slot):
            res = QMessageBox.question(
                self,
                "Overwrite Preset",
                f"Slot {idx:03d} already contains '{slot['name']}'. Overwrite it with the current UI patch?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if res != QMessageBox.StandardButton.Yes:
                return

        self.app._preset_save(idx)
        self.refresh()

    def _local_rename(self) -> None:
        idx = self._selected_row()
        slot = self.app.bank["slots"][idx]
        if presets.slot_is_empty(slot):
            QMessageBox.information(self, "Rename Preset", f"Slot {idx:03d} is currently empty.")
            return

        name, ok = QInputDialog.getText(self, "Rename Preset", f"New name for slot {idx:03d}:", text=slot["name"])
        if ok:
            clean_name = name.strip() or "Untitled"
            slot["name"] = clean_name
            presets.save_bank(self.app.bank)
            if idx == int(self.app.bank.get("current", -1)):
                self.app.preset_name_entry.setText(clean_name)
            self.app._refresh_preset_combo()
            self.refresh()

    def _local_delete(self) -> None:
        idx = self._selected_row()
        slot = self.app.bank["slots"][idx]
        if presets.slot_is_empty(slot):
            return

        res = QMessageBox.warning(
            self,
            "Delete Preset",
            f"Are you sure you want to clear slot {idx:03d} ('{slot['name']}')?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if res != QMessageBox.StandardButton.Yes:
            return

        self.app.bank["slots"][idx] = None
        presets.save_bank(self.app.bank)
        self.app._refresh_preset_combo()
        self.refresh()

    def _mcu_recall_on_board(self) -> None:
        idx = self._selected_row()
        if not self.app._mcu_ready():
            return
        self.app.mcu.recall_slot(
            idx,
            lambda ok, _: self.app.log(f"[mcu] recalled slot {idx:03d} on board\n" if ok else f"[mcu] recall failed\n")
        )

    def _mcu_refresh_dir(self) -> None:
        if not self.app._mcu_ready():
            return
        self.app.mcu.read_directory(lambda ok, payload: self._on_dir_done(ok, payload))

    def _on_dir_done(self, ok: bool, payload: list) -> None:
        if ok:
            self.app.mcu_dir = dict(payload)
            self.refresh()

    def _mcu_send_slot(self) -> None:
        idx = self._selected_row()
        slot = self.app.bank["slots"][idx]
        if presets.slot_is_empty(slot):
            QMessageBox.information(self, "Send Slot", f"Local slot {idx:03d} is empty.")
            return

        if self.app._mcu_ready():
            rec = fileformats.slot_to_record(slot)
            self.app.mcu.push_preset_record(
                idx, rec, lambda ok, _: self._mcu_refresh_dir()
            )

    def _mcu_fetch_slot(self) -> None:
        idx = self._selected_row()
        if self.app._mcu_ready():
            self.app.mcu.dump_preset_slot(
                idx, lambda ok, payload: self._on_fetched_slot(idx, ok, payload)
            )

    def _on_fetched_slot(self, idx: int, ok: bool, payload: bytes) -> None:
        if ok:
            slot = fileformats.record_to_slot(payload)
            self.app.bank["slots"][idx] = slot
            presets.save_bank(self.app.bank)
            self.app._refresh_preset_combo()
            self.refresh()
            self.app.log(f"[mcu] fetched slot {idx:03d} '{slot['name']}'\n")

    def _mcu_push_all(self) -> None:
        entries = [
            (i, s)
            for i, s in enumerate(self.app.bank["slots"])
            if not presets.slot_is_empty(s)
        ]
        if not entries or not self.app._mcu_ready():
            return

        res = QMessageBox.warning(
            self,
            "Push All Presets to MCU",
            f"This will write {len(entries)} local presets into the board's LittleFS flash storage. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if res != QMessageBox.StandardButton.Yes:
            return

        def step(k: int):
            if k >= len(entries):
                self._mcu_refresh_dir()
                self.app.log("[mcu] push all completed\n")
                return
            i, slot = entries[k]
            self.app.mcu.push_preset_record(
                i, fileformats.slot_to_record(slot), lambda ok, _: step(k + 1)
            )

        step(0)

    def _mcu_pull_all(self) -> None:
        if not self.app._mcu_ready():
            return

        res = QMessageBox.warning(
            self,
            "Pull All Presets from MCU",
            "This will overwrite occupied local slots with the presets currently stored on the board. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if res != QMessageBox.StandardButton.Yes:
            return

        def on_dir(ok, payload):
            if not ok or not payload:
                return
            slots = [s for s, _ in payload]

            def step(k: int):
                if k >= len(slots):
                    presets.save_bank(self.app.bank)
                    self.app._refresh_preset_combo()
                    self.refresh()
                    self.app.log("[mcu] pull all completed\n")
                    return
                i = slots[k]
                self.app.mcu.dump_preset_slot(
                    i,
                    lambda ok, p: self._on_pulled_step(i, ok, p, lambda: step(k + 1)),
                )

            step(0)

        self.app.mcu.read_directory(on_dir)

    def _on_pulled_step(self, i: int, ok: bool, payload: bytes, next_fn) -> None:
        if ok:
            try:
                self.app.bank["slots"][i] = fileformats.record_to_slot(payload)
            except ValueError:
                pass
        next_fn()

def main() -> None:
    ap = argparse.ArgumentParser(description="DCO Bench Controller (PySide6)")
    ap.add_argument("--port", help="serial device (e.g. /dev/ttyACM0)")
    ap.add_argument("--model", choices=sorted(models.PROFILES))
    ap.add_argument(
        "--theme",
        choices=sorted(theme.PALETTES.keys()),
        default=theme.DEFAULT_THEME,
        help=f"Color theme (default: {theme.DEFAULT_THEME}, choices: {', '.join(theme.PALETTES.keys())})",
    )
    ap.add_argument(
        "--font-size",
        type=int,
        default=12,
        help="Base font size in pt/px (default: 12)",
    )
    ap.add_argument("--cobs", action="store_true")
    args = ap.parse_args()

    env_cobs = (
        os.environ.get("DCO_SERIAL_COBS", "").strip().lower() in ("1", "true", "yes")
    )
    cobs = args.cobs or env_cobs

    env_model = os.environ.get("DCO_CONTROL_MODEL", "").strip().lower()
    model = (
        args.model
        or models.detect()
        or (env_model if env_model in models.PROFILES else None)
    )
    if model:
        models.set_active(model)
    apply_active_model()

    q_app = QApplication(sys.argv)
    window = App(args.port, mode=args.theme, base_font_size=args.font_size, cobs=cobs)
    window.show()
    sys.exit(q_app.exec())


if __name__ == "__main__":
    main()
