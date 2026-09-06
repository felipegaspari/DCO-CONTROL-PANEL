#!/usr/bin/env python3
"""Bench controller for the DCO board built with PySide6 (Qt6)."""

from __future__ import annotations

import argparse
import math
import os
import sys
import threading

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QFont,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPen,
    QTextCharFormat,
    QTextCursor,
)
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
OSC_PITCH_PIDS = (13, 14, 34, 15, 35)
OSC_SYNC_PIDS = (32, 37, 38, 17, 130,)
OSC_VOICE_PIDS = (26, 27, 28, 18, 33, 29, 30, 31, 43, 21)
OSC_LEVEL_PIDS = (22, 23, 39, 24)
OSC_WAVE_MATRIX = [
    ("OSC1", (1, 2, 3)),
    ("OSC2", (87, 88, 89)),
    ("OSC3", (90, 91, 92)),
]
OSC_WAVE_COLS = ("Saw", "Pulse", "Tri")

ENV_ADSR_BLOCKS = ("adsr_vca", "adsr_vcf", "adsr_dco")
ENV_CURVE_RESTART_PIDS = (8, 9, 214, 48, 49, 50, 51, 52, 53, 54, 55, 56, 223, 224, 225)
ENV_CURVE_COLUMNS = (
    ("EnvVCA (ADSR1)", 224, 48, 49, 50, 8),
    ("EnvVCF (ADSR2)", 225, 51, 52, 53, 9),
    ("EnvDCO (ADSR3)", 223, 54, 55, 56, 214),
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


class BipolarSlider(QSlider):
    """Horizontal slider that resets to center (or designated reset value) on double-click."""

    def __init__(self, orientation=Qt.Orientation.Horizontal, reset_val: int = 0, parent=None) -> None:
        super().__init__(orientation, parent)
        self.reset_val = reset_val

    def mouseDoubleClickEvent(self, event) -> None:
        self.setValue(self.reset_val)
        event.accept()

class FilterResponsePreview(QWidget):
    """Vector frequency response curve (Bode plot) showing Cutoff, Resonance, and Mode."""

    def __init__(self, color: QColor, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(75)
        self._color = color
        self._cutoff = 4095
        self._reso = 0
        self._mode = 0

    def set_values(self, cutoff: int, reso: int, mode: int = 0) -> None:
        self._cutoff = max(0, min(4095, cutoff))
        self._reso = max(0, min(4095, reso))
        self._mode = mode
        self.update()

    def set_color(self, color: QColor) -> None:
        self._color = color
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = float(self.width())
        h = float(self.height())
        pad_x = 10.0
        pad_y = 8.0
        usable_w = w - 2 * pad_x
        usable_h = h - 2 * pad_y

        painter.fillRect(0, 0, int(w), int(h), QColor(0, 0, 0, 35))

        # Normalized cutoff (0.0 .. 1.0) and resonance factor
        fc_norm = self._cutoff / 4095.0
        res_factor = self._reso / 4095.0
        fc_x = pad_x + usable_w * fc_norm
        base_y = pad_y + usable_h * 0.78
        peak_y = base_y - (res_factor * (usable_h * 0.65))

        path = QPainterPath()
        mode_names = {0: "LP24", 1: "BP12", 2: "HP6/LP18", 3: "Alt"}

        if self._mode == 1:  # Bandpass (BP12)
            path.moveTo(pad_x, pad_y + usable_h)
            path.quadTo((pad_x + fc_x) / 2.0, base_y, fc_x, peak_y)
            path.quadTo((fc_x + (w - pad_x)) / 2.0, base_y, w - pad_x, pad_y + usable_h)
        else:  # Lowpass (LP24 / Default)
            path.moveTo(pad_x, base_y)
            path.lineTo(max(pad_x, fc_x - 22.0), base_y)
            path.cubicTo(fc_x - 8.0, base_y, fc_x - 3.0, peak_y, fc_x, peak_y)
            roll_end_x = min(w - pad_x, fc_x + 38.0)
            path.quadTo(fc_x + 10.0, base_y + 12.0, roll_end_x, pad_y + usable_h)
            path.lineTo(w - pad_x, pad_y + usable_h)

        # Translucent filled area
        fill_path = QPainterPath(path)
        fill_path.lineTo(w - pad_x, pad_y + usable_h)
        fill_path.lineTo(pad_x, pad_y + usable_h)
        fill_path.closeSubpath()

        fill_col = QColor(self._color)
        fill_col.setAlpha(35)
        painter.fillPath(fill_path, fill_col)

        # Response line & peak dot
        painter.setPen(QPen(self._color, 2.0))
        painter.drawPath(path)

        painter.setPen(QPen(self._color, 5.0))
        painter.drawPoint(int(fc_x), int(peak_y))

        # Mode text tag
        painter.setPen(QColor(180, 180, 180, 160))
        font = painter.font()
        font.setPointSize(8)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(int(pad_x + 4), int(pad_y + 14), mode_names.get(self._mode, "LP24"))


class PWMCyclePreview(QWidget):
    """Vector oscilloscope displaying live square wave duty cycle."""

    def __init__(self, color: QColor, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(75)
        self._color = color
        self._duty = 2048

    def set_duty(self, duty: int) -> None:
        self._duty = max(0, min(4095, duty))
        self.update()

    def set_color(self, color: QColor) -> None:
        self._color = color
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = float(self.width())
        h = float(self.height())
        pad_x = 10.0
        pad_y = 10.0
        usable_w = w - 2 * pad_x
        usable_h = h - 2 * pad_y

        painter.fillRect(0, 0, int(w), int(h), QColor(0, 0, 0, 35))

        # Calculate duty cycle percentage (clamped between 2% and 98% for visual visibility)
        pct = max(0.02, min(0.98, self._duty / 4095.0))
        y_high = pad_y
        y_low = pad_y + usable_h

        # Draw two full wave cycles
        period = usable_w / 2.0
        thigh = period * pct

        path = QPainterPath()
        path.moveTo(pad_x, y_high)

        for c in range(2):
            x_start = pad_x + c * period
            x_fall = x_start + thigh
            x_end = x_start + period
            path.lineTo(x_fall, y_high)
            path.lineTo(x_fall, y_low)
            path.lineTo(x_end, y_low)
            if c < 1:
                path.lineTo(x_end, y_high)

        # Translucent fill for active pulses
        fill_path = QPainterPath(path)
        fill_path.lineTo(pad_x + 2 * period, y_low)
        fill_path.lineTo(pad_x, y_low)
        fill_path.closeSubpath()

        fill_col = QColor(self._color)
        fill_col.setAlpha(35)
        painter.fillPath(fill_path, fill_col)

        # Center 50% dotted reference line
        painter.setPen(QPen(QColor(140, 140, 140, 90), 1.0, Qt.PenStyle.DashLine))
        for c in range(2):
            mid_x = pad_x + c * period + period * 0.5
            painter.drawLine(int(mid_x), int(pad_y), int(mid_x), int(pad_y + usable_h))

        # Pulse wave line
        painter.setPen(QPen(self._color, 2.0))
        painter.drawPath(path)

        # Percentage readout badge
        painter.setPen(self._color)
        font = painter.font()
        font.setPointSize(9)
        font.setBold(True)
        painter.setFont(font)
        badge_text = f"{(self._duty / 4095.0) * 100:.1f}%"
        painter.drawText(int(w - pad_x - 48), int(pad_y + 14), badge_text)

class EnvelopePreviewWidget(QWidget):
    """Vector graphic visualizer that plots live ADSR curves."""

    def __init__(self, color: QColor, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(70)
        self._color = color
        self._a = 0
        self._d = 1200
        self._s = 3000
        self._r = 600
        self._mode = 0  # 0: Normal, 1: Centered, 2: Inverted

    def set_values(self, a: int, d: int, s: int, r: int, mode: int = 0) -> None:
        self._a = max(0, min(4095, a))
        self._d = max(0, min(4095, d))
        self._s = max(0, min(4095, s))
        self._r = max(0, min(4095, r))
        self._mode = mode
        self.update()

    def set_color(self, color: QColor) -> None:
        self._color = color
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = float(self.width())
        h = float(self.height())
        pad_x = 8.0
        pad_y = 6.0
        usable_w = w - 2 * pad_x
        usable_h = h - 2 * pad_y

        painter.fillRect(0, 0, int(w), int(h), QColor(0, 0, 0, 35))

        # Proportional stage widths (guarantees minimum width for 0 ms values)
        min_seg = 10.0
        total_time = float(self._a + self._d + self._r + 1000)
        avail_w = usable_w - (4 * min_seg)

        w_a = min_seg + avail_w * (self._a / total_time)
        w_d = min_seg + avail_w * (self._d / total_time)
        w_s = min_seg + avail_w * (1000.0 / total_time)
        w_r = usable_w - (w_a + w_d + w_s)

        x0 = pad_x
        x1 = x0 + w_a
        x2 = x1 + w_d
        x3 = x2 + w_s
        x4 = x0 + usable_w

        if self._mode == 2:  # Inverted mode
            y_base = pad_y
            y_peak = pad_y + usable_h
            y_sus = pad_y + (self._s / 4095.0) * usable_h
        else:  # Normal mode
            y_base = pad_y + usable_h
            y_peak = pad_y
            y_sus = y_base - (self._s / 4095.0) * usable_h

        path = QPainterPath()
        path.moveTo(x0, y_base)
        path.lineTo(x1, y_peak)
        path.lineTo(x2, y_sus)
        path.lineTo(x3, y_sus)
        path.lineTo(x4, y_base)

        # Translucent fill
        fill_path = QPainterPath(path)
        fill_path.lineTo(x4, y_base)
        fill_path.lineTo(x0, y_base)
        fill_path.closeSubpath()

        fill_col = QColor(self._color)
        fill_col.setAlpha(35)
        painter.fillPath(fill_path, fill_col)

        # Border outline
        painter.setPen(QPen(self._color, 2.0))
        painter.drawPath(path)

        # Stage anchor points
        painter.setPen(QPen(self._color, 4.0))
        painter.drawPoint(int(x1), int(y_peak))
        painter.drawPoint(int(x2), int(y_sus))
        painter.drawPoint(int(x3), int(y_sus))

class LFOWaveformPreview(QWidget):
    """Vector oscilloscope displaying standard, analog-modeled, and complex LFO waveforms."""

    def __init__(self, color: QColor, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(82, 36)
        self._wave_name = "sine"
        self._color = color

    def set_waveform(self, name: str) -> None:
        self._wave_name = name.lower()
        self.update()

    def set_color(self, color: QColor) -> None:
        self._color = color
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = float(self.width())
        h = float(self.height())
        pad_x = 8.0
        pad_y = 6.0
        usable_w = w - 2 * pad_x
        usable_h = h - 2 * pad_y
        mid_y = pad_y + usable_h / 2.0
        amp = usable_h / 2.0

        # Oscilloscope screen background
        painter.fillRect(0, 0, int(w), int(h), QColor(0, 0, 0, 35))

        # Center zero-voltage reference line
        painter.setPen(QPen(QColor(120, 120, 120, 50), 1.0, Qt.PenStyle.DashLine))
        painter.drawLine(int(pad_x), int(mid_y), int(w - pad_x), int(mid_y))

        path = QPainterPath()
        pen = QPen(self._color, 2.0)
        painter.setPen(pen)

        t = self._wave_name

        # --- Flat / Off ---
        if "off" in t:
            path.moveTo(pad_x, mid_y)
            path.lineTo(w - pad_x, mid_y)

        # --- 1. Analog Broken (Heavily deformed, glitchy zero-crossing notch) ---
        elif "broken" in t:
            steps = 54
            for i in range(steps + 1):
                frac = i / steps
                th = frac * 2.0 * math.pi
                raw = math.sin(th) + 0.48 * math.sin(2 * th + 0.8) - 0.32 * math.sin(3 * th)
                if abs(raw) < 0.22:
                    raw *= 0.35  # Severe crossover kink
                val = max(-1.0, min(1.0, raw / 1.32))
                x = pad_x + usable_w * frac
                y = mid_y - val * amp
                if i == 0:
                    path.moveTo(x, y)
                else:
                    path.lineTo(x, y)

        # --- 2. Analog Tube (Asymmetric soft-clipped top/bottom plateaus) ---
        elif "tube" in t:
            steps = 48
            for i in range(steps + 1):
                frac = i / steps
                th = frac * 2.0 * math.pi
                s = math.sin(th) + 0.18 * math.sin(2 * th - 0.3)
                if s >= 0:
                    val = math.tanh(2.3 * s) * 0.94
                else:
                    val = math.tanh(1.8 * s) * 0.88
                x = pad_x + usable_w * frac
                y = mid_y - val * amp
                if i == 0:
                    path.moveTo(x, y)
                else:
                    path.lineTo(x, y)

        # --- 3. Analog Tape (Hysteresis forward tilt and saturation) ---
        elif "tape" in t:
            steps = 48
            for i in range(steps + 1):
                frac = i / steps
                th = frac * 2.0 * math.pi
                th_skew = th + 0.36 * math.cos(th)
                s = math.sin(th_skew) + 0.22 * math.sin(2 * th_skew - 0.25)
                val = math.tanh(1.5 * s) / math.tanh(1.5)
                x = pad_x + usable_w * frac
                y = mid_y - val * amp
                if i == 0:
                    path.moveTo(x, y)
                else:
                    path.lineTo(x, y)

        # --- 4. Analog Sine (Organic, subtly deformed sine wave) ---
        elif "analog" in t and "sine" in t:
            steps = 44
            for i in range(steps + 1):
                frac = i / steps
                th = frac * 2.0 * math.pi
                val = (math.sin(th) + 0.14 * math.sin(2 * th - 0.2) - 0.05 * math.cos(3 * th)) / 1.08
                x = pad_x + usable_w * frac
                y = mid_y - val * amp
                if i == 0:
                    path.moveTo(x, y)
                else:
                    path.lineTo(x, y)

        # --- 5. Wavefolded Sine (Buchla/Serge style inward crest fold) ---
        elif "fold" in t:
            steps = 56
            for i in range(steps + 1):
                frac = i / steps
                th = frac * 2.0 * math.pi
                # Classic diode-wavefolder fold equation
                val = math.sin(2.4 * math.sin(th))
                x = pad_x + usable_w * frac
                y = mid_y - val * amp
                if i == 0:
                    path.moveTo(x, y)
                else:
                    path.lineTo(x, y)

        # --- 6. Sharktooth Saw (Convex rising scoop with sharp drop) ---
        elif "shark" in t:
            steps = 48
            for i in range(steps + 1):
                frac = i / steps
                if frac <= 0.90:
                    u = frac / 0.90
                    # Convex scooped rise
                    val = -1.0 + 2.0 * math.sin(u * math.pi * 0.5)
                else:
                    # Quick vertical return to base
                    u = (frac - 0.90) / 0.10
                    val = 1.0 - 2.0 * u
                x = pad_x + usable_w * frac
                y = mid_y - val * amp
                if i == 0:
                    path.moveTo(x, y)
                else:
                    path.lineTo(x, y)

        # --- 7. Trapezoid (Squared trapezoid with steep sloped walls & rounded corners) ---
        elif "trap" in t:
            steps = 56
            for i in range(steps + 1):
                frac = i / steps
                th = frac * 2.0 * math.pi
                # Normalized triangle generator
                tri = 2.0 * math.asin(math.sin(th)) / math.pi
                # Slew saturation: 5.2x creates wide squared plateaus with clean trapezoidal slopes
                val = math.tanh(5.2 * tri) / math.tanh(5.2)
                x = pad_x + usable_w * frac
                y = mid_y - val * amp
                if i == 0:
                    path.moveTo(x, y)
                else:
                    path.lineTo(x, y)

        # --- 8. Staircase (Stepped triangle wave with 6 levels up and down) ---
        elif "stair" in t or "step" in t:
            # 6 discrete voltage levels from -1.0 to +1.0
            levels_up = [-1.0, -0.6, -0.2, 0.2, 0.6, 1.0]
            levels_dn = [0.6, 0.2, -0.2, -0.6, -1.0]
            terraces = levels_up + levels_dn
            step_w = usable_w / len(terraces)

            for i, lvl in enumerate(terraces):
                x_start = pad_x + i * step_w
                x_end = x_start + step_w
                y = mid_y - lvl * amp
                if i == 0:
                    path.moveTo(x_start, y)
                else:
                    path.lineTo(x_start, y)  # Vertical riser
                path.lineTo(x_end, y)        # Horizontal tread

        # --- Standard Shapes ---
        elif "tri" in t:
            path.moveTo(pad_x, mid_y)
            path.lineTo(pad_x + usable_w * 0.25, mid_y - amp)
            path.lineTo(pad_x + usable_w * 0.75, mid_y + amp)
            path.lineTo(w - pad_x, mid_y)

        elif "saw" in t or "ramp" in t:
            path.moveTo(pad_x, mid_y + amp)
            path.lineTo(w - pad_x, mid_y - amp)
            path.lineTo(w - pad_x, mid_y + amp)

        elif "sq" in t or "pulse" in t:
            path.moveTo(pad_x, mid_y - amp)
            path.lineTo(pad_x + usable_w * 0.5, mid_y - amp)
            path.lineTo(pad_x + usable_w * 0.5, mid_y + amp)
            path.lineTo(w - pad_x, mid_y + amp)

        elif "rand" in t or "s&h" in t or "noise" in t:
            levels = [0.25, -0.75, 0.8, -0.3, 0.5]
            step_w = usable_w / len(levels)
            for i, lvl in enumerate(levels):
                x1 = pad_x + i * step_w
                x2 = x1 + step_w
                y = mid_y - lvl * amp
                if i == 0:
                    path.moveTo(x1, y)
                else:
                    path.lineTo(x1, y)
                path.lineTo(x2, y)

        else:  # Clean Digital Sine (Default)
            steps = 36
            for i in range(steps + 1):
                frac = i / steps
                th = frac * 2.0 * math.pi
                x = pad_x + usable_w * frac
                y = mid_y - math.sin(th) * amp
                if i == 0:
                    path.moveTo(x, y)
                else:
                    path.lineTo(x, y)

        painter.drawPath(path)

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

        # Mod Matrix State
        self._mod_slot_dots: list[QLabel] = []
        self._mod_slot_rows: list[QWidget] = []

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

        if hasattr(self, "_mod_slot_dots") and self._mod_slot_dots:
            self._update_all_mod_indicators()

        if hasattr(self, "_lfo1_preview") and hasattr(self, "_lfo2_preview"):
            accent_col = QColor(theme.PALETTES[self.mode]["accent"])
            self._lfo1_preview.set_color(accent_col)
            self._lfo2_preview.set_color(accent_col)

        if hasattr(self, "_env_previews"):
         accent_col = QColor(theme.PALETTES[self.mode]["accent"])
         for prev in self._env_previews.values():
             prev.set_color(accent_col)

        if hasattr(self, "_wave_buttons"):
         for btn in self._wave_buttons.values():
             self._style_wave_button(btn, btn.isChecked())

        if hasattr(self, "_filter_preview") and hasattr(self, "_pwm_preview"):
         accent_col = QColor(theme.PALETTES[self.mode]["accent"])
         self._filter_preview.set_color(accent_col)
         self._pwm_preview.set_color(accent_col)

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

        # Theme Selector
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

    # --- Calibration Tab ---

    def _create_cal_slider_row(
        self,
        parent_layout: QBoxLayout,
        pid: int,
        label_text: str | None = None,
        reset_val: int | None = None,
    ) -> tuple[QLabel, QSlider]:
        p = PARAM_BY_PID[pid]
        row = QHBoxLayout()
        row.setContentsMargins(0, 1, 0, 1)
        row.setSpacing(6)

        lbl = QLabel(label_text or p.label)
        lbl.setFixedWidth(175)
        row.addWidget(lbl)

        target_reset = p.default if reset_val is None else reset_val
        slider = BipolarSlider(Qt.Orientation.Horizontal, reset_val=target_reset)
        slider.setRange(p.lo, p.hi)
        slider.setValue(p.default)
        slider.setToolTip(f"Double-click to reset ({target_reset})")

        rd = QLabel(param_meta.format_display_value(pid, p.default))
        rd.setObjectName("ReadoutLabel")
        rd.setFixedWidth(52)
        rd.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        slider.valueChanged.connect(
            lambda val, p_id=p.pid, r=rd: self._on_slider_changed(p_id, val, r)
        )

        row.addWidget(slider, 1)
        row.addWidget(rd)

        zero_btn = QPushButton("↺" if target_reset != 0 else "0")
        zero_btn.setToolTip(f"Reset ({target_reset})")
        zero_btn.setFixedWidth(26)
        zero_btn.clicked.connect(lambda _, s=slider, r_val=target_reset: s.setValue(r_val))
        row.addWidget(zero_btn)

        parent_layout.addLayout(row)
        self.param_widgets[p.pid] = slider
        self._readouts[("p", p.pid)] = rd
        return lbl, slider

    def _build_cal_tab(self, parent_layout: QVBoxLayout) -> None:
        palette = theme.PALETTES.get(self.mode, theme.PALETTES[theme.DEFAULT_THEME])

        master_split = QHBoxLayout()
        master_split.setSpacing(12)

        # =====================================================================
        # LEFT COLUMN: CALIBRATION ENGINES (AUTOTUNE & MANUAL WORKBENCH)
        # =====================================================================
        left_col = QVBoxLayout()
        left_col.setSpacing(10)

        # 1. Automatic Calibration Engine
        auto_box = QGroupBox("Automatic Calibration (Autotune)")
        auto_lay = QVBoxLayout(auto_box)
        auto_lay.setSpacing(8)

        auto_top_row = QHBoxLayout()
        auto_top_row.setSpacing(6)

        # Scope Action Buttons
        p_auto = PARAM_BY_PID[PID_RUN_AUTOTUNE]
        btn_full = QPushButton("⚡ Full Autotune")
        btn_full.setObjectName("AccentButton")
        btn_full.setToolTip("Run complete amplitude and pulse-width auto-calibration")
        btn_full.clicked.connect(lambda: self._on_run_autotune(params.CAL_SCOPE_FULL, "Full"))
        self._pulse_buttons[PID_RUN_AUTOTUNE] = btn_full
        auto_top_row.addWidget(btn_full, 2)

        btn_amp = QPushButton("Amp Comp")
        btn_amp.setToolTip("Run amplitude compensation calibration only")
        btn_amp.clicked.connect(lambda: self._on_run_autotune(params.CAL_SCOPE_AMP, "Amp comp"))
        auto_top_row.addWidget(btn_amp, 1)

        btn_pw = QPushButton("Pulse Width")
        btn_pw.setToolTip("Run pulse-width calibration only")
        btn_pw.clicked.connect(lambda: self._on_run_autotune(params.CAL_SCOPE_PW, "PW"))
        auto_top_row.addWidget(btn_pw, 1)

        btn_stop = QPushButton("Stop")
        btn_stop.setToolTip("Stop active calibration sweep")
        btn_stop.setStyleSheet(f"color: {palette['accent_active']}; font-weight: bold;")
        btn_stop.clicked.connect(lambda: self._send_autotune_pulse(0, "Stop"))
        auto_top_row.addWidget(btn_stop, 1)

        auto_lay.addLayout(auto_top_row)

        # Precision Selector Row
        prec_row = QHBoxLayout()
        prec_lbl = QLabel("Precision Mode:")
        prec_lbl.setObjectName("MutedLabel")
        prec_row.addWidget(prec_lbl)

        for name, offset in params.CAL_PRECISION_CHOICES:
            rb = QRadioButton(name)
            if offset == 0:
                rb.setChecked(True)
            rb.toggled.connect(
                lambda checked, off=offset: self._on_precision_toggled(checked, off)
            )
            prec_row.addWidget(rb)
        prec_row.addStretch(1)
        auto_lay.addLayout(prec_row)

        left_col.addWidget(auto_box)

        # 2. Manual Calibration & Fine Trimming Console
        manual_box = QGroupBox("Manual Calibration & Stage Trimming")
        manual_lay = QVBoxLayout(manual_box)
        manual_lay.setSpacing(8)

        # Stage Selection Bar
        stage_bar = QHBoxLayout()
        stage_bar.setSpacing(6)

        p_mode = PARAM_BY_PID[PID_MANUAL_CAL_MODE]
        chk_mode = QCheckBox("Enable Manual Mode")
        chk_mode.setChecked(bool(p_mode.default))
        chk_mode.toggled.connect(
            lambda checked, pid=PID_MANUAL_CAL_MODE: self._on_check_toggled(pid, checked)
        )
        stage_bar.addWidget(chk_mode)
        self.param_widgets[PID_MANUAL_CAL_MODE] = chk_mode

        # Oscillator Selector
        osc_labels = [calstages.osc_label(o) for o in range(models.active().num_oscillators)]
        self._cal_osc_combo = QComboBox()
        self._cal_osc_combo.addItems(osc_labels)
        self._cal_osc_combo.currentIndexChanged.connect(self._manual_cal_on_osc_picked)
        stage_bar.addWidget(self._cal_osc_combo)

        # Stage / Kind Selector
        self._cal_sub_combo = QComboBox()
        self._cal_sub_combo.currentIndexChanged.connect(self._manual_cal_on_substage_picked)
        stage_bar.addWidget(self._cal_sub_combo, 1)

        # Stage Badge
        self._cal_stage_readout = QLabel("")
        self._cal_stage_readout.setObjectName("ReadoutLabel")
        self._cal_stage_readout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        stage_bar.addWidget(self._cal_stage_readout)

        manual_lay.addLayout(stage_bar)

        # Live Hardware Memory Indicator Banner
        self._manual_cal_indicator = QLabel("")
        self._manual_cal_indicator.setObjectName("MutedLabel")
        self._manual_cal_indicator.setWordWrap(True)
        self._manual_cal_indicator.setStyleSheet(
            f"background-color: {palette['field']}; border: 1px solid {palette['border']}; "
            f"border-radius: 4px; padding: 4px 8px;"
        )
        manual_lay.addWidget(self._manual_cal_indicator)

        # Trimming Sliders (Dimmed/Enabled according to stage kind)
        self._cal_kind_rows.clear()
        if PID_MANUAL_CAL_OFFSET in PARAM_BY_PID:
            lbl, sl = self._create_cal_slider_row(manual_lay, PID_MANUAL_CAL_OFFSET, "Manual Offset", reset_val=0)
            self._cal_kind_rows[PID_MANUAL_CAL_OFFSET] = (lbl, sl)

        if PID_AMP_COMP_440 in PARAM_BY_PID:
            p_440 = PARAM_BY_PID[PID_AMP_COMP_440]
            lbl, sl = self._create_cal_slider_row(manual_lay, PID_AMP_COMP_440, "Amp Comp @ 440 Hz", reset_val=p_440.default)
            self._cal_kind_rows[PID_AMP_COMP_440] = (lbl, sl)

        if PID_CAL_PW_CENTER in PARAM_BY_PID and not PARAM_BY_PID[PID_CAL_PW_CENTER].hidden:
            p_pw = PARAM_BY_PID[PID_CAL_PW_CENTER]
            lbl, sl = self._create_cal_slider_row(
                manual_lay, PID_CAL_PW_CENTER, "PW Center (Cal)", reset_val=p_pw.default
            )
            self._cal_kind_rows[PID_CAL_PW_CENTER] = (lbl, sl)

        if PID_AMP_COMP_DUTY_OFFSET in PARAM_BY_PID:
            lbl, sl = self._create_cal_slider_row(manual_lay, PID_AMP_COMP_DUTY_OFFSET, "Duty Trim (0.01%)", reset_val=0)
            self._cal_kind_rows[PID_AMP_COMP_DUTY_OFFSET] = (lbl, sl)

        # Store & Recall Row
        store_row = QHBoxLayout()
        store_row.setSpacing(6)

        p_store = PARAM_BY_PID[PID_MANUAL_CAL_STORE]
        store_btn = QPushButton("💾 Store Offsets to Flash")
        store_btn.setObjectName("AccentButton")
        store_btn.setToolTip("Persist manual calibration trims into board LittleFS memory")
        store_btn.clicked.connect(
            lambda: self._on_pulse_clicked(PID_MANUAL_CAL_STORE, p_store.pulse_value, p_store.label)
        )
        self._pulse_buttons[PID_MANUAL_CAL_STORE] = store_btn
        store_row.addWidget(store_btn, 2)

        recall_btn = QPushButton("↻ Recall From Board")
        recall_btn.setToolTip("Discard local edits and reload stored calibration from board")
        recall_btn.clicked.connect(self._manual_cal_refresh_from_board)
        store_row.addWidget(recall_btn, 1)

        manual_lay.addLayout(store_row)
        left_col.addWidget(manual_box)
        left_col.addStretch(1)

        master_split.addLayout(left_col, 6)

        # =====================================================================
        # RIGHT COLUMN: FLASH BACKUP, HARDWARE TIMING & DEV TOOLS
        # =====================================================================
        right_col = QVBoxLayout()
        right_col.setSpacing(10)

        # 1. LittleFS Flash Memory Backup / Restore
        backup_box = QGroupBox("Flash Calibration Backup (LittleFS)")
        backup_lay = QVBoxLayout(backup_box)
        backup_lay.setSpacing(6)

        note_bak = QLabel("Export or restore all 7 LittleFS calibration tables as JSON files:")
        note_bak.setObjectName("MutedLabel")
        backup_lay.addWidget(note_bak)

        bak_btn_row = QHBoxLayout()
        bak_btn_row.setSpacing(6)
        dump_btn = QPushButton("📥 Dump Board → File…")
        dump_btn.clicked.connect(self._cal_dump_to_file)
        load_btn = QPushButton("📤 Load File → Board…")
        load_btn.clicked.connect(self._cal_load_from_file)
        bak_btn_row.addWidget(dump_btn)
        bak_btn_row.addWidget(load_btn)
        backup_lay.addLayout(bak_btn_row)
        right_col.addWidget(backup_box)

        # 2. Low-Level Timing Configuration (PIO Reset Pulse)
        timing_box = QGroupBox("Low-Level Timing Configuration")
        timing_lay = QVBoxLayout(timing_box)
        timing_lay.setSpacing(6)

        pio_row = QHBoxLayout()
        pio_row.setSpacing(6)
        pio_lbl = QLabel("PIO Pulse Length (Y):")
        pio_lbl.setFixedWidth(160)
        pio_row.addWidget(pio_lbl)

        pio_slider = BipolarSlider(Qt.Orientation.Horizontal, reset_val=params.PIO_PULSE_DEFAULT)
        pio_slider.setRange(params.PIO_PULSE_LO, params.PIO_PULSE_HI)
        pio_slider.setValue(params.PIO_PULSE_DEFAULT)
        pio_slider.setToolTip(f"Double-click to reset ({params.PIO_PULSE_DEFAULT})")

        pio_rd = QLabel(str(params.PIO_PULSE_DEFAULT))
        pio_rd.setObjectName("ReadoutLabel")
        pio_rd.setFixedWidth(52)
        pio_rd.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        pio_slider.valueChanged.connect(lambda val, rd=pio_rd: self._on_pio_pulse_changed(val, rd))
        pio_row.addWidget(pio_slider, 1)
        pio_row.addWidget(pio_rd)

        pio_rst_btn = QPushButton("↺")
        pio_rst_btn.setToolTip(f"Reset to default ({params.PIO_PULSE_DEFAULT})")
        pio_rst_btn.setFixedWidth(26)
        pio_rst_btn.clicked.connect(lambda: pio_slider.setValue(params.PIO_PULSE_DEFAULT))
        pio_row.addWidget(pio_rst_btn)

        timing_lay.addLayout(pio_row)
        self.pio_pulse_slider = pio_slider
        self._readouts[("pio_pulse",)] = pio_rd
        right_col.addWidget(timing_box)

        # 3. Calibration Sweep & Diagnostic Dev Tools
        dev_box = QGroupBox("Calibration Diagnostics & Dev Tables")
        dev_lay = QVBoxLayout(dev_box)
        dev_lay.setSpacing(6)

        dev_grid = QGridLayout()
        dev_grid.setSpacing(4)
        for idx, (lbl_txt, cmd_val) in enumerate(params.CAL_DEBUG_COMMANDS):
            btn = QPushButton(lbl_txt)
            btn.clicked.connect(lambda _, v=cmd_val, l=lbl_txt: self._send_debug_cmd(v, l))
            dev_grid.addWidget(btn, idx // 2, idx % 2)
        dev_lay.addLayout(dev_grid)

        dev_note = QLabel("Seed force-writes test tables. Verify sweep measures duty errors.")
        dev_note.setObjectName("MutedLabel")
        dev_lay.addWidget(dev_note)
        right_col.addWidget(dev_box)

        # 4. Amp-Comp Calibration Search Method (Runtime)
        amp_meth_cmds = models.filter_debug_commands(params.AMP_CAL_METHOD_COMMANDS)
        if amp_meth_cmds:
            meth_box = QGroupBox("Amp-Comp Calibration Method (Runtime)")
            meth_lay = QVBoxLayout(meth_box)
            meth_lay.setSpacing(6)

            meth_row = QHBoxLayout()
            meth_row.setSpacing(4)
            for lbl_txt, cmd_val in amp_meth_cmds:
                btn = QPushButton(lbl_txt.replace("Amp cal: ", "").capitalize())
                btn.clicked.connect(lambda _, v=cmd_val, l=lbl_txt: self._send_debug_cmd(v, l))
                meth_row.addWidget(btn)
            meth_lay.addLayout(meth_row)

            meth_note = QLabel("Algorithm used to construct amplitude compensation tables.")
            meth_note.setObjectName("MutedLabel")
            meth_lay.addWidget(meth_note)
            right_col.addWidget(meth_box)

        right_col.addStretch(1)
        master_split.addLayout(right_col, 4)

        parent_layout.addLayout(master_split)
        parent_layout.addStretch(1)

        # Initialize calibration controls and indicators
        self._manual_cal_fill_substages(0, calstages.KIND_SAW)
        self._manual_cal_update_stage_readout()
        self._wire_manual_cal_recall()
        self._update_manual_cal_indicator()

    # --- VCF / PWM Tab ---

    def _build_vcf_pwm_tab(self, parent_layout: QVBoxLayout) -> None:
        palette = theme.PALETTES.get(self.mode, theme.PALETTES[theme.DEFAULT_THEME])
        accent_col = QColor(palette["accent"])

        master_split = QHBoxLayout()
        master_split.setSpacing(12)

        # =====================================================================
        # LEFT COLUMN: VOLTAGE CONTROLLED FILTER (VCF)
        # =====================================================================
        vcf_col = QVBoxLayout()
        vcf_col.setSpacing(8)

        # 1. Filter Core & Bode Response Curve
        core_box = QGroupBox("Filter Core (VCF)")
        core_lay = QVBoxLayout(core_box)
        core_lay.setSpacing(6)

        self._filter_preview = FilterResponsePreview(accent_col)
        core_lay.addWidget(self._filter_preview)

        # Filter Mode & Resonance Amp Compensation Row
        top_opts = QHBoxLayout()
        if 60 in PARAM_BY_PID:
            p60 = PARAM_BY_PID[60]
            top_opts.addWidget(QLabel("Mode:"))
            cb60 = QComboBox()
            for label, val in p60.choices:
                cb60.addItem(label, val)
            cb60.setCurrentIndex(
                next((i for i, c in enumerate(p60.choices) if c[1] == p60.default), 0)
            )
            cb60.currentIndexChanged.connect(
                lambda idx, p=60, cb=cb60: self._on_filter_mode_changed(p, cb.itemData(idx))
            )
            top_opts.addWidget(cb60, 1)
            self.param_widgets[60] = cb60

        if 7 in PARAM_BY_PID:
            p7 = PARAM_BY_PID[7]
            chk7 = QCheckBox("Reso Amp Comp")
            chk7.setChecked(bool(p7.default))
            chk7.toggled.connect(
                lambda checked, p=7: self._on_check_toggled(p, checked)
            )
            top_opts.addWidget(chk7)
            self.param_widgets[7] = chk7

        core_lay.addLayout(top_opts)

        # Core Sliders: Cutoff, Resonance, and EnvVCF Depth
        self.block_widgets["filter"] = {}
        block = self.blocks_by_key["filter"]
        for f in block.fields:
            if f.key == "cutoff":
                self._create_filter_block_slider(core_lay, f, "Cutoff Freq")
            elif f.key == "resonance":
                self._create_filter_block_slider(core_lay, f, "Resonance")
            elif f.key == "adsr2_to_vcf":
                self._create_filter_block_slider(core_lay, f, "EnvVCF (ADSR 2) Depth")

        vcf_col.addWidget(core_box)

        # 2. Cutoff Modulation Routings (LFO, Keytrack, Velocity)
        mod_box = QGroupBox("Cutoff Modulation")
        mod_lay = QVBoxLayout(mod_box)
        mod_lay.setSpacing(6)

        # LFO 2 Mod Depth
        for f in block.fields:
            if f.key == "lfo2_to_vcf":
                self._create_filter_block_slider(mod_lay, f, "LFO 2 Mod Depth")

        # Mirrored Keytrack and Velocity (Synced with Oscillators tab)
        self._vcf_keytrack_slider, self._vcf_keytrack_rd = self._create_mirrored_slider_row(
            mod_lay, "VCF Keytrack", -256, 255, 0
        )
        self._vcf_vel_slider, self._vcf_vel_rd = self._create_mirrored_slider_row(
            mod_lay, "Velocity → VCF", 0, 20, 0
        )

        vcf_col.addWidget(mod_box)

        # 3. Analog Distortion & Saturation
        dist_box = QGroupBox("Post-Filter Saturation & Drive")
        dist_lay = QVBoxLayout(dist_box)
        dist_lay.setSpacing(6)

        if 58 in PARAM_BY_PID:
            self._create_osc_slider_row(dist_lay, 58, "Drive Level", reset_val=0)
        if 59 in PARAM_BY_PID:
            self._create_osc_slider_row(dist_lay, 59, "Dry / Wet Mix", reset_val=0)

        vcf_col.addWidget(dist_box)
        vcf_col.addStretch(1)

        master_split.addLayout(vcf_col, 5)

        # =====================================================================
        # RIGHT COLUMN: PULSE WIDTH MODULATION (PWM)
        # =====================================================================
        pwm_col = QVBoxLayout()
        pwm_col.setSpacing(8)

        # 1. Pulse Width Engine & Duty Cycle Wave Visualizer
        pwm_engine_box = QGroupBox("Pulse Width Engine (PWM)")
        pwm_engine_lay = QVBoxLayout(pwm_engine_box)
        pwm_engine_lay.setSpacing(6)

        self._pwm_preview = PWMCyclePreview(accent_col)
        pwm_engine_lay.addWidget(self._pwm_preview)

        # Base Pulse Width Slider (PID 210, default 2048 = 50.0%)
        if 210 in PARAM_BY_PID:
            p210 = PARAM_BY_PID[210]
            row_pw = QHBoxLayout()
            row_pw.setSpacing(6)

            lbl = QLabel("Pulse Width:")
            lbl.setMinimumWidth(90)
            row_pw.addWidget(lbl)

            slider_pw = BipolarSlider(Qt.Orientation.Horizontal, reset_val=2048)
            slider_pw.setRange(p210.lo, p210.hi)
            slider_pw.setValue(p210.default)
            slider_pw.setToolTip("Double-click to snap to center 50% (2048)")

            rd_pw = QLabel(str(p210.default))
            rd_pw.setObjectName("ReadoutLabel")
            rd_pw.setFixedWidth(52)
            rd_pw.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

            slider_pw.valueChanged.connect(
                lambda val, pid=210, rd=rd_pw: self._on_pwm_width_changed(pid, val, rd)
            )

            row_pw.addWidget(slider_pw, 1)
            row_pw.addWidget(rd_pw)

            center_btn = QPushButton("↺")
            center_btn.setToolTip("Snap to center 50% (2048)")
            center_btn.setFixedWidth(26)
            center_btn.clicked.connect(lambda: slider_pw.setValue(2048))
            row_pw.addWidget(center_btn)

            pwm_engine_lay.addLayout(row_pw)
            self.param_widgets[210] = slider_pw
            self._readouts[("p", 210)] = rd_pw

        # Manual Pots Override Checkbox
        if 124 in PARAM_BY_PID:
            p124 = PARAM_BY_PID[124]
            chk124 = QCheckBox("PWM Manual Potentiometer Override")
            chk124.setChecked(bool(p124.default))
            chk124.toggled.connect(
                lambda checked, p=124: self._on_check_toggled(p, checked)
            )
            pwm_engine_lay.addWidget(chk124)
            self.param_widgets[124] = chk124

        pwm_col.addWidget(pwm_engine_box)

        # 2. PWM Dynamic Modulations
        pwm_mod_box = QGroupBox("PWM Modulations")
        pwm_mod_lay = QVBoxLayout(pwm_mod_box)
        pwm_mod_lay.setSpacing(6)

        if 45 in PARAM_BY_PID:
            self._create_osc_slider_row(pwm_mod_lay, 45, "LFO 2 → PW Depth", reset_val=0)

        if 46 in PARAM_BY_PID:
            self._create_osc_slider_row(pwm_mod_lay, 46, "EnvDCO (ADSR 3) Depth", reset_val=512)

        note_lbl = QLabel("ⓘ <i>LFO 2 and EnvDCO dynamically swing the pulse duty cycle away from center.</i>")
        note_lbl.setObjectName("MutedLabel")
        note_lbl.setWordWrap(True)
        pwm_mod_lay.addWidget(note_lbl)

        # Quick Center Reset Button
        btn_center_all = QPushButton("Center Pulse Width & Reset Modulations")
        btn_center_all.setToolTip("Sets Pulse Width to 50% and zeros modulation depths")
        btn_center_all.clicked.connect(self._reset_pwm_to_center)
        pwm_mod_lay.addWidget(btn_center_all)

        pwm_col.addWidget(pwm_mod_box)
        pwm_col.addStretch(1)

        master_split.addLayout(pwm_col, 4)

        parent_layout.addLayout(master_split)
        parent_layout.addStretch(1)

        self._update_vcf_preview()
        self._update_pwm_preview()

    def _create_filter_block_slider(
        self, parent_layout: QVBoxLayout, f: params.BlockField, label_text: str
    ) -> None:
        row = QHBoxLayout()
        row.setSpacing(6)

        lbl = QLabel(label_text)
        lbl.setMinimumWidth(140)
        row.addWidget(lbl)

        slider = BipolarSlider(Qt.Orientation.Horizontal, reset_val=f.default)
        slider.setRange(f.lo, f.hi)
        slider.setValue(f.default)
        slider.setToolTip(f"Double-click to reset ({f.default})")

        rd = QLabel(str(f.default))
        rd.setObjectName("ReadoutLabel")
        rd.setFixedWidth(52)
        rd.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        slider.valueChanged.connect(
            lambda val, bk="filter", fk=f.key, r=rd: self._on_filter_slider_changed(bk, fk, val, r)
        )

        row.addWidget(slider, 1)
        row.addWidget(rd)

        btn = QPushButton("↺" if f.default != 0 else "0")
        btn.setToolTip(f"Reset ({f.default})")
        btn.setFixedWidth(26)
        btn.clicked.connect(lambda _, s=slider, def_v=f.default: s.setValue(def_v))
        row.addWidget(btn)

        parent_layout.addLayout(row)
        self.block_widgets["filter"][f.key] = slider
        self._readouts[("b", "filter", f.key)] = rd

    def _on_filter_slider_changed(
        self, bkey: str, fkey: str, value: int, rd: QLabel
    ) -> None:
        self._on_block_slider_changed(bkey, fkey, value, rd)
        self._update_vcf_preview()

    def _on_filter_mode_changed(self, pid: int, value: int) -> None:
        self._on_combo_changed(pid, value)
        self._update_vcf_preview()

    def _on_pwm_width_changed(self, pid: int, value: int, rd: QLabel) -> None:
        self._on_slider_changed(pid, value, rd)
        self._update_pwm_preview()

    def _update_vcf_preview(self) -> None:
        if not hasattr(self, "_filter_preview"):
            return
        w = self.block_widgets.get("filter", {})
        cutoff = w["cutoff"].value() if "cutoff" in w else 4095
        reso = w["resonance"].value() if "resonance" in w else 0
        mode_cb = self.param_widgets.get(60)
        mode = mode_cb.currentData() if isinstance(mode_cb, QComboBox) else 0
        self._filter_preview.set_values(cutoff, reso, mode)

    def _update_pwm_preview(self) -> None:
        if not hasattr(self, "_pwm_preview"):
            return
        pw_w = self.param_widgets.get(210)
        val = pw_w.value() if isinstance(pw_w, QSlider) else 2048
        self._pwm_preview.set_duty(val)

    def _reset_pwm_to_center(self) -> None:
        pw_w = self.param_widgets.get(210)
        if isinstance(pw_w, QSlider):
            pw_w.setValue(2048)
        lfo_w = self.param_widgets.get(45)
        if isinstance(lfo_w, QSlider):
            lfo_w.setValue(0)
        env_w = self.param_widgets.get(46)
        if isinstance(env_w, QSlider):
            env_w.setValue(512)
        self.log("[ui] Reset PWM to center 50% and cleared modulations\n")

    def _build_tabs(self) -> None:
        for group in params.GROUP_ORDER:
            # Skip PWM as a standalone tab; it is now merged with Filter
            if group == params.GROUP_PWM:
                continue

            area, _, lay = self._scrollable()
            tab_title = "VCF / PWM" if group == params.GROUP_FILTER else group
            self.tabs.addTab(area, tab_title)

            if group == params.GROUP_OSC:
                self._build_osc_tab(lay)
            elif group == params.GROUP_ENV:
                self._build_env_tab(lay)
            elif group == params.GROUP_FILTER:
                self._build_vcf_pwm_tab(lay)
            elif group == params.GROUP_LFO:
                self._build_lfo_tab(lay)
            elif group == params.GROUP_MOD:
                self._build_mod_tab(lay)
            elif group == params.GROUP_CHARACTER:
                for param in [p for p in params.PARAMS if p.group == group]:
                    self._add_param_widget(lay, param)
                self._add_character_jitter_sliders(lay)
                lay.addStretch(1)
            elif group == params.GROUP_CAL:
                self._build_cal_tab(lay)
            else:
                for block in [b for b in params.BLOCKS if b.group == group]:
                    self._add_block_widget(lay, block)
                for param in [p for p in params.PARAMS if p.group == group]:
                    if param.pid in self.param_widgets:
                        continue
                    self._add_param_widget(lay, param)
                lay.addStretch(1)

        # Diagnostics Tab
        area, _, lay = self._scrollable()
        self.tabs.addTab(area, params.GROUP_DIAG)
        self._build_diag_tab(lay)

        # Wire all cross-tab synced sliders
        self._wire_cross_panel_sync()


    # --- Oscillators Tab ---

    def _create_osc_slider_row(
        self,
        parent_layout: QVBoxLayout,
        pid: int,
        label_text: str | None = None,
        reset_val: int | None = None,
    ) -> QSlider:
        p = PARAM_BY_PID[pid]
        row = QHBoxLayout()
        row.setContentsMargins(0, 1, 0, 1)
        row.setSpacing(6)

        lbl = QLabel(label_text or p.label)
        lbl.setMinimumWidth(110)
        row.addWidget(lbl)

        target_reset = p.default if reset_val is None else reset_val
        slider = BipolarSlider(Qt.Orientation.Horizontal, reset_val=target_reset)
        slider.setRange(p.lo, p.hi)
        slider.setValue(p.default)
        slider.setToolTip(f"Double-click to reset ({target_reset})")

        rd = QLabel(param_meta.format_display_value(pid, p.default))
        rd.setObjectName("ReadoutLabel")
        rd.setFixedWidth(52)
        rd.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        slider.valueChanged.connect(
            lambda val, p_id=p.pid, r=rd: self._on_slider_changed(p_id, val, r)
        )

        row.addWidget(slider, 1)
        row.addWidget(rd)

        zero_btn = QPushButton("↺" if target_reset != 0 else "0")
        zero_btn.setToolTip(f"Reset to default ({target_reset})")
        zero_btn.setFixedWidth(26)
        zero_btn.clicked.connect(lambda _, s=slider, r_val=target_reset: s.setValue(r_val))
        row.addWidget(zero_btn)

        parent_layout.addLayout(row)
        self.param_widgets[p.pid] = slider
        self._readouts[("p", p.pid)] = rd
        return slider

    def _style_wave_button(self, btn: QPushButton, checked: bool) -> None:
        palette = theme.PALETTES.get(self.mode, theme.PALETTES[theme.DEFAULT_THEME])
        if checked:
            btn.setStyleSheet(
                f"font-weight: bold; background-color: {palette['accent']}; color: {palette['bg']}; "
                f"border-radius: 3px; padding: 4px 6px;"
            )
        else:
            btn.setStyleSheet(
                f"font-weight: normal; color: {palette['muted']}; border-radius: 3px; padding: 4px 6px;"
            )

    def _on_wave_button_toggled(self, pid: int, checked: bool, btn: QPushButton) -> None:
        self._style_wave_button(btn, checked)
        self._on_check_toggled(pid, checked)

    def _add_osc_wave_buttons(self, parent_layout: QVBoxLayout, pids: tuple[int, ...]) -> None:
        wave_box = QGroupBox("Waveforms")
        h_lay = QHBoxLayout(wave_box)
        h_lay.setContentsMargins(4, 4, 4, 4)
        h_lay.setSpacing(4)

        # Explicit PID-to-glyph map prevents index-shifting bugs when waves are omitted
        wave_glyphs = {
            1: "◺ Saw", 2: "⎍ Pulse", 3: "⋀ Tri",
            87: "◺ Saw", 88: "⎍ Pulse", 89: "⋀ Tri",
            90: "◺ Saw", 91: "⎍ Pulse", 92: "⋀ Tri",
        }

        for pid in pids:
            # OSC B (OSC2) has no Triangle wave on DCO4 (only available on DCO3)
            if pid == 89 and models.active().key != "dco3":
                continue

            if pid not in PARAM_BY_PID or PARAM_BY_PID[pid].hidden:
                continue

            p = PARAM_BY_PID[pid]
            btn = QPushButton(wave_glyphs.get(pid, p.label))
            btn.setCheckable(True)
            btn.setChecked(bool(p.default))
            self._style_wave_button(btn, bool(p.default))

            btn.toggled.connect(
                lambda checked, p_id=pid, b=btn: self._on_wave_button_toggled(p_id, checked, b)
            )

            h_lay.addWidget(btn)
            self.param_widgets[pid] = btn
            self._wave_buttons[pid] = btn

        parent_layout.addWidget(wave_box)

# --- Oscillators Tab ---

    def _build_osc_tab(self, parent_layout: QVBoxLayout) -> None:
        self._wave_buttons: dict[int, QPushButton] = {}

        # Master horizontal split: Left (Generators) vs Right (Engine & Dynamics)
        main_split = QHBoxLayout()
        main_split.setSpacing(12)

        # =====================================================================
        # LEFT COLUMN: AUDIO GENERATORS (OSC 1, OSC 2, OSC 3, SUB)
        # =====================================================================
        left_col = QVBoxLayout()
        left_col.setSpacing(8)

        # --- OSC 1 ---
        osc1_box = QGroupBox(models.active().osc_row_names[0])
        osc1_lay = QVBoxLayout(osc1_box)
        osc1_lay.setSpacing(6)

        if 13 in PARAM_BY_PID:
            p13 = PARAM_BY_PID[13]
            row_oct = QHBoxLayout()
            row_oct.addWidget(QLabel("Octave:"))
            cb13 = QComboBox()
            for label, val in p13.choices:
                cb13.addItem(label, val)
            cb13.setCurrentIndex(
                next((i for i, c in enumerate(p13.choices) if c[1] == p13.default), 0)
            )
            cb13.currentIndexChanged.connect(
                lambda idx, pid=13, cb=cb13: self._on_combo_changed(pid, cb.itemData(idx))
            )
            row_oct.addWidget(cb13, 1)
            osc1_lay.addLayout(row_oct)
            self.param_widgets[13] = cb13

        self._add_osc_wave_buttons(osc1_lay, (1, 2, 3))

        if 22 in PARAM_BY_PID:
            self._create_osc_slider_row(osc1_lay, 22, "Level", reset_val=127)

        left_col.addWidget(osc1_box)

        # --- OSC 2 ---
        osc2_box = QGroupBox(models.active().osc_row_names[1])
        osc2_lay = QVBoxLayout(osc2_box)
        osc2_lay.setSpacing(6)

        if 14 in PARAM_BY_PID:
            self._create_osc_slider_row(osc2_lay, 14, "Interval", reset_val=36)
        if 15 in PARAM_BY_PID:
            self._create_osc_slider_row(osc2_lay, 15, "Detune", reset_val=256)

        self._add_osc_wave_buttons(osc2_lay, (87, 88, 89))

        if 23 in PARAM_BY_PID:
            self._create_osc_slider_row(osc2_lay, 23, "Level", reset_val=0)

        left_col.addWidget(osc2_box)

        # --- OSC 3 (if model supports >= 3 oscillators) ---
        has_osc3 = (
            len(models.active().osc_row_names) >= 3
            and 34 in PARAM_BY_PID
            and not PARAM_BY_PID[34].hidden
        )
        if has_osc3:
            osc3_box = QGroupBox(models.active().osc_row_names[2])
            osc3_lay = QVBoxLayout(osc3_box)
            osc3_lay.setSpacing(6)

            if 34 in PARAM_BY_PID:
                self._create_osc_slider_row(osc3_lay, 34, "Interval", reset_val=36)
            if 35 in PARAM_BY_PID:
                self._create_osc_slider_row(osc3_lay, 35, "Detune", reset_val=256)

            self._add_osc_wave_buttons(osc3_lay, (90, 91, 92))

            if 39 in PARAM_BY_PID:
                self._create_osc_slider_row(osc3_lay, 39, "Level", reset_val=0)

            left_col.addWidget(osc3_box)

        # --- Sub-Oscillator (Compact box at the bottom of the left column) ---
        sub_box = QGroupBox("Sub-Oscillator")
        sub_lay = QVBoxLayout(sub_box)
        sub_lay.setSpacing(6)

        # Only display Sub Divide on DCO3
        if models.active().key == "dco3" and 38 in PARAM_BY_PID and not PARAM_BY_PID[38].hidden:
            p38 = PARAM_BY_PID[38]
            row_sub = QHBoxLayout()
            row_sub.addWidget(QLabel("Divide:"))
            cb38 = QComboBox()
            for label, val in p38.choices:
                cb38.addItem(label, val)
            cb38.setCurrentIndex(
                next((i for i, c in enumerate(p38.choices) if c[1] == p38.default), 0)
            )
            cb38.currentIndexChanged.connect(
                lambda idx, pid=38, cb=cb38: self._on_combo_changed(pid, cb.itemData(idx))
            )
            row_sub.addWidget(cb38, 1)
            sub_lay.addLayout(row_sub)
            self.param_widgets[38] = cb38

        if 24 in PARAM_BY_PID:
            self._create_osc_slider_row(sub_lay, 24, "Sub Level", reset_val=0)

        left_col.addWidget(sub_box)
        left_col.addStretch(1)

        # Add left column to master layout with stretch factor 4
        main_split.addLayout(left_col, 4)

        # =====================================================================
        # RIGHT COLUMN: ENGINE, MODULATION & DYNAMICS
        # =====================================================================
        right_col = QVBoxLayout()
        right_col.setSpacing(10)

        # --- Top Right Row: Voice Mode & Portamento ---
        row_voice_porta = QHBoxLayout()
        row_voice_porta.setSpacing(10)

        # Voice Mode
        voice_box = QGroupBox("Voice Mode")
        voice_lay = QVBoxLayout(voice_box)
        voice_lay.setSpacing(6)

        if 26 in PARAM_BY_PID:
            p26 = PARAM_BY_PID[26]
            r26 = QHBoxLayout()
            r26.addWidget(QLabel("Voice Mode:"))
            cb26 = QComboBox()
            for label, val in p26.choices:
                cb26.addItem(label, val)
            cb26.setCurrentIndex(
                next((i for i, c in enumerate(p26.choices) if c[1] == p26.default), 0)
            )
            cb26.currentIndexChanged.connect(
                lambda idx, p=26, c=cb26: self._on_combo_changed(p, c.itemData(idx))
            )
            r26.addWidget(cb26, 1)
            voice_lay.addLayout(r26)
            self.param_widgets[26] = cb26

        # Unison Detune right below Voice Mode
        if 28 in PARAM_BY_PID:
            self._create_osc_slider_row(voice_lay, 28, "Unison Detune", reset_val=0)

        if 27 in PARAM_BY_PID:
            p27 = PARAM_BY_PID[27]
            r27 = QHBoxLayout()
            r27.addWidget(QLabel("Allocation:"))
            cb27 = QComboBox()
            for label, val in p27.choices:
                cb27.addItem(label, val)
            cb27.setCurrentIndex(
                next((i for i, c in enumerate(p27.choices) if c[1] == p27.default), 0)
            )
            cb27.currentIndexChanged.connect(
                lambda idx, p=27, c=cb27: self._on_combo_changed(p, c.itemData(idx))
            )
            r27.addWidget(cb27, 1)
            voice_lay.addLayout(r27)
            self.param_widgets[27] = cb27

        voice_lay.addStretch(1)
        row_voice_porta.addWidget(voice_box, 1)

        # Portamento
        porta_box = QGroupBox("Portamento")
        porta_lay = QVBoxLayout(porta_box)
        porta_lay.setSpacing(6)

        if 33 in PARAM_BY_PID:
            p33 = PARAM_BY_PID[33]
            r33 = QHBoxLayout()
            r33.addWidget(QLabel("Glide Mode:"))
            cb33 = QComboBox()
            for label, val in p33.choices:
                cb33.addItem(label, val)
            cb33.setCurrentIndex(
                next((i for i, c in enumerate(p33.choices) if c[1] == p33.default), 0)
            )
            cb33.currentIndexChanged.connect(
                lambda idx, p=33, c=cb33: self._on_combo_changed(p, c.itemData(idx))
            )
            r33.addWidget(cb33, 1)
            porta_lay.addLayout(r33)
            self.param_widgets[33] = cb33

        # Glide Time right below Glide Mode
        if 18 in PARAM_BY_PID:
            self._create_osc_slider_row(porta_lay, 18, "Glide Time", reset_val=0)

        porta_lay.addStretch(1)
        row_voice_porta.addWidget(porta_box, 1)

        right_col.addLayout(row_voice_porta)

        # --- Middle Right Row: Sync/Crossmod & Analog Drift ---
        row_sync_drift = QHBoxLayout()
        row_sync_drift.setSpacing(10)

        # Sync & Crossmod
        sync_box = QGroupBox("Sync & Cross-Modulation")
        sync_lay = QVBoxLayout(sync_box)
        sync_lay.setSpacing(6)

        for pid, title in (
            (32, "Hard Sync:"),
            (37, "Soft Sync:"),
            (17, "Phase Align:"),
        ):
            if pid in PARAM_BY_PID:
                p = PARAM_BY_PID[pid]
                r = QHBoxLayout()
                r.addWidget(QLabel(title))
                cb = QComboBox()
                for label, val in p.choices:
                    cb.addItem(label, val)
                cb.setCurrentIndex(
                    next((i for i, c in enumerate(p.choices) if c[1] == p.default), 0)
                )
                cb.currentIndexChanged.connect(
                    lambda idx, p_id=pid, cbox=cb: self._on_combo_changed(p_id, cbox.itemData(idx))
                )
                r.addWidget(cb, 1)
                sync_lay.addLayout(r)
                self.param_widgets[pid] = cb

        if 130 in PARAM_BY_PID:
            self._create_osc_slider_row(sync_lay, 130, "Crossmod Depth", reset_val=0)

        sync_lay.addStretch(1)
        row_sync_drift.addWidget(sync_box, 1)

        # Analog Drift
        drift_box = QGroupBox("Analog Drift")
        drift_lay = QVBoxLayout(drift_box)
        drift_lay.setSpacing(6)

        if 29 in PARAM_BY_PID:
            self._create_osc_slider_row(drift_lay, 29, "Drift Amount", reset_val=0)
        if 30 in PARAM_BY_PID:
            self._create_osc_slider_row(drift_lay, 30, "Drift Speed", reset_val=1)
        if 31 in PARAM_BY_PID:
            self._create_osc_slider_row(drift_lay, 31, "Stereo Spread", reset_val=1)

        drift_lay.addStretch(1)
        row_sync_drift.addWidget(drift_box, 1)

        right_col.addLayout(row_sync_drift)

        # --- Bottom Right: Dynamics & Keytracking (Full width of right pane) ---
        dyn_box = QGroupBox("Dynamics & Keytracking")
        dyn_lay = QHBoxLayout(dyn_box)
        dyn_lay.setSpacing(16)

        # Column A: VCA Output & Velocity
        vca_col = QVBoxLayout()
        vca_col.setSpacing(6)
        if 43 in PARAM_BY_PID:
            self._create_osc_slider_row(vca_col, 43, "VCA Output Level", reset_val=128)
        if 21 in PARAM_BY_PID:
            self._create_osc_slider_row(vca_col, 21, "Velocity → VCA", reset_val=0)

        # Auto-detect VCA keytrack if defined
        vca_kt_pid = next(
            (p.pid for p in params.PARAMS if "vca" in p.label.lower() and "keytrack" in p.label.lower()),
            None,
        )
        if vca_kt_pid and vca_kt_pid in PARAM_BY_PID:
            self._create_osc_slider_row(vca_col, vca_kt_pid, "VCA Keytrack", reset_val=0)

        vca_col.addStretch(1)
        dyn_lay.addLayout(vca_col, 1)

        # Column B: VCF Dynamics & Keytracking
        vcf_col = QVBoxLayout()
        vcf_col.setSpacing(6)
        if 20 in PARAM_BY_PID:
            self._create_osc_slider_row(vcf_col, 20, "Velocity → VCF", reset_val=0)
        if 19 in PARAM_BY_PID:
            self._create_osc_slider_row(vcf_col, 19, "VCF Keytrack", reset_val=0)

        vcf_col.addStretch(1)
        dyn_lay.addLayout(vcf_col, 1)

        right_col.addWidget(dyn_box)
        right_col.addStretch(1)

        # Add right column to master layout with stretch factor 6
        main_split.addLayout(right_col, 6)

        parent_layout.addLayout(main_split)
        parent_layout.addStretch(1)

    def _link_sliders(
        self, slider_a: QSlider, rd_a: QLabel, slider_b: QSlider, rd_b: QLabel
    ) -> None:
        """Bidirectionally binds two QSliders without infinite recursion or dropped transmissions."""
        syncing = False

        def on_a_changed(val: int) -> None:
            nonlocal syncing
            if syncing:
                return
            syncing = True
            try:
                slider_b.setValue(val)
                rd_a.setText(str(val))
                rd_b.setText(str(val))
            finally:
                syncing = False

        def on_b_changed(val: int) -> None:
            nonlocal syncing
            if syncing:
                return
            syncing = True
            try:
                slider_a.setValue(val)
                rd_a.setText(str(val))
                rd_b.setText(str(val))
            finally:
                syncing = False

        slider_a.valueChanged.connect(on_a_changed)
        slider_b.valueChanged.connect(on_b_changed)
        on_b_changed(slider_b.value())

    def _wire_cross_panel_sync(self) -> None:
        """Connects all cross-tab mirrored sliders after all tabs are built."""
        # 1. Envelopes -> Oscillators (VCA Output Level -> PID 43)
        primary_vca = self.param_widgets.get(43)
        rd_vca = self._readouts.get(("p", 43))
        if hasattr(self, "_env_vca_slider") and isinstance(primary_vca, QSlider) and rd_vca:
            self._link_sliders(self._env_vca_slider, self._env_vca_rd, primary_vca, rd_vca)

        # 2. Envelopes -> Filter (VCF Cutoff Depth -> adsr2_to_vcf)
        filter_widgets = self.block_widgets.get("filter", {})
        primary_vcf = filter_widgets.get("adsr2_to_vcf")
        rd_vcf = self._readouts.get(("b", "filter", "adsr2_to_vcf"))
        if hasattr(self, "_env_vcf_slider") and isinstance(primary_vcf, QSlider) and rd_vcf:
            self._link_sliders(self._env_vcf_slider, self._env_vcf_rd, primary_vcf, rd_vcf)

        # 3. Envelopes -> PWM (PWM Depth -> PID 46)
        primary_pwm = self.param_widgets.get(46)
        rd_pwm = self._readouts.get(("p", 46))
        if hasattr(self, "_env_pwm_slider") and isinstance(primary_pwm, QSlider) and rd_pwm:
            self._link_sliders(self._env_pwm_slider, self._env_pwm_rd, primary_pwm, rd_pwm)

        # 4. LFO 2 -> Filter (Cutoff Mod -> lfo2_to_vcf)
        primary_lfo_vcf = filter_widgets.get("lfo2_to_vcf")
        rd_lfo_vcf = self._readouts.get(("b", "filter", "lfo2_to_vcf"))
        if hasattr(self, "_lfo2_vcf_slider") and isinstance(primary_lfo_vcf, QSlider) and rd_lfo_vcf:
            self._link_sliders(self._lfo2_vcf_slider, self._lfo2_vcf_rd, primary_lfo_vcf, rd_lfo_vcf)

        # 5. LFO 2 -> PWM (PW Mod -> PID 45)
        primary_lfo_pw = self.param_widgets.get(45)
        rd_lfo_pw = self._readouts.get(("p", 45))
        if hasattr(self, "_lfo2_pw_slider") and isinstance(primary_lfo_pw, QSlider) and rd_lfo_pw:
            self._link_sliders(self._lfo2_pw_slider, self._lfo2_pw_rd, primary_lfo_pw, rd_lfo_pw)

        # 6. VCF / PWM -> Oscillators (VCF Keytrack & Velocity sync)
        primary_kt = self.param_widgets.get(19)
        rd_kt = self._readouts.get(("p", 19))
        if hasattr(self, "_vcf_keytrack_slider") and isinstance(primary_kt, QSlider) and rd_kt:
            self._link_sliders(self._vcf_keytrack_slider, self._vcf_keytrack_rd, primary_kt, rd_kt)

        primary_vel_vcf = self.param_widgets.get(20)
        rd_vel_vcf = self._readouts.get(("p", 20))
        if hasattr(self, "_vcf_vel_slider") and isinstance(primary_vel_vcf, QSlider) and rd_vel_vcf:
            self._link_sliders(self._vcf_vel_slider, self._vcf_vel_rd, primary_vel_vcf, rd_vel_vcf)

# --- Envelopes Tab ---

    def _build_env_tab(self, parent_layout: QVBoxLayout) -> None:
        palette = theme.PALETTES.get(self.mode, theme.PALETTES[theme.DEFAULT_THEME])
        accent_col = QColor(palette["accent"])

        columns_layout = QHBoxLayout()
        columns_layout.setSpacing(10)

        self._env_previews: dict[str, EnvelopePreviewWidget] = {}

        env_specs = [
            ("adsr_vca", "EnvVCA (ADSR 1) — Amplitude", 224, 48, 49, 50, 8),
            ("adsr_vcf", "EnvVCF (ADSR 2) — Filter Timbre", 225, 51, 52, 53, 9),
            ("adsr_dco", "EnvDCO (ADSR 3) — Pitch & PWM", 223, 54, 55, 56, 214),
        ]

        for bkey, title, mode_pid, a_pid, d_pid, rel_pid, r_pid in env_specs:
            block = self.blocks_by_key[bkey]
            box = QGroupBox(title)
            lay = QVBoxLayout(box)
            lay.setSpacing(8)

            # 1. Live Vector Preview
            prev = EnvelopePreviewWidget(accent_col)
            lay.addWidget(prev)
            self._env_previews[bkey] = prev

            # 2. ADSR Vertical Faders
            faders_box = QGroupBox("Stages (ADSR)")
            faders_lay = QHBoxLayout(faders_box)
            faders_lay.setContentsMargins(4, 8, 4, 8)
            faders_lay.setSpacing(6)
            self.block_widgets[bkey] = {}

            for f in block.fields:
                col = QVBoxLayout()
                col.setSpacing(4)
                lbl = QLabel(f.label[0].upper())  # 'A', 'D', 'S', 'R'
                lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                lbl.setStyleSheet("font-weight: bold;")
                col.addWidget(lbl)

                slider = QSlider(Qt.Orientation.Vertical)
                slider.setRange(f.lo, f.hi)
                slider.setValue(f.default)
                slider.setMinimumHeight(120)

                rd = QLabel(str(f.default))
                rd.setObjectName("ReadoutLabel")
                rd.setAlignment(Qt.AlignmentFlag.AlignCenter)

                slider.valueChanged.connect(
                    lambda val, bk=bkey, fk=f.key, r=rd: self._on_env_slider_changed(
                        bk, fk, val, r
                    )
                )

                col.addWidget(slider, 1, Qt.AlignmentFlag.AlignCenter)
                col.addWidget(rd, 0, Qt.AlignmentFlag.AlignCenter)
                faders_lay.addLayout(col)

                self.block_widgets[bkey][f.key] = slider
                self._readouts[("b", bkey, f.key)] = rd

            lay.addWidget(faders_box)

            # 3. Shape & Response (Mode, Curves, Restart)
            shape_box = QGroupBox("Shape & Curves")
            shape_lay = QVBoxLayout(shape_box)
            shape_lay.setSpacing(4)

            # Mode
            if mode_pid in PARAM_BY_PID:
                mp = PARAM_BY_PID[mode_pid]
                m_row = QHBoxLayout()
                m_row.addWidget(QLabel("Mode:"))
                mcb = QComboBox()
                for label, val in mp.choices:
                    mcb.addItem(label, val)
                mcb.setCurrentIndex(
                    next((i for i, c in enumerate(mp.choices) if c[1] == mp.default), 0)
                )
                mcb.currentIndexChanged.connect(
                    lambda idx, pid=mp.pid, cb=mcb, bk=bkey: self._on_env_mode_changed(
                        pid, cb.itemData(idx), bk
                    )
                )
                m_row.addWidget(mcb, 1)
                shape_lay.addLayout(m_row)
                self.param_widgets[mp.pid] = mcb

            # Curves Grid (Attack, Decay, Release)
            grid_curves = QGridLayout()
            grid_curves.setContentsMargins(0, 2, 0, 2)
            grid_curves.setSpacing(4)

            for idx_c, (pid, clbl) in enumerate(
                ((a_pid, "Atk"), (d_pid, "Dec"), (rel_pid, "Rel"))
            ):
                if pid in PARAM_BY_PID:
                    cp = PARAM_BY_PID[pid]
                    grid_curves.addWidget(QLabel(clbl), 0, idx_c)
                    cb = QComboBox()
                    for label, val in cp.choices:
                        cb.addItem(label, val)
                    cb.setCurrentIndex(
                        next((i for i, c in enumerate(cp.choices) if c[1] == cp.default), 0)
                    )
                    cb.currentIndexChanged.connect(
                        lambda c_idx, p=pid, cbox=cb: self._on_combo_changed(
                            p, cbox.itemData(c_idx)
                        )
                    )
                    grid_curves.addWidget(cb, 1, idx_c)
                    self.param_widgets[pid] = cb

            shape_lay.addLayout(grid_curves)

            # Key Restart
            if r_pid in PARAM_BY_PID:
                rp = PARAM_BY_PID[r_pid]
                chk = QCheckBox(rp.label)
                chk.setChecked(bool(rp.default))
                chk.toggled.connect(
                    lambda checked, p=rp.pid: self._on_check_toggled(p, checked)
                )
                shape_lay.addWidget(chk)
                self.param_widgets[rp.pid] = chk

            lay.addWidget(shape_box)

            # 4. Target Modulations (Cross-Panel Synced)
            mod_box = QGroupBox("Target Modulations")
            mod_lay = QVBoxLayout(mod_box)
            mod_lay.setSpacing(4)

            if bkey == "adsr_vca":
                # Cross-linked to Oscillators Tab -> VCA level [PID 43]
                self._env_vca_slider, self._env_vca_rd = self._create_mirrored_slider_row(
                    mod_lay, "VCA Output Level", 0, 128, 128
                )
                # Envelope to VCA depth
                if 222 in PARAM_BY_PID:
                    self._create_lfo_slider_row(mod_lay, 222, "Env to VCA Depth")

            elif bkey == "adsr_vcf":
                # Cross-linked to Filter Tab -> adsr2_to_vcf
                self._env_vcf_slider, self._env_vcf_rd = self._create_mirrored_slider_row(
                    mod_lay, "VCF Cutoff Depth", 0, 512, 0
                )

            elif bkey == "adsr_dco":
                if 47 in PARAM_BY_PID:
                    self._create_lfo_slider_row(mod_lay, 47, "Pitch Detune Depth")
                if 10 in PARAM_BY_PID:
                    t_row = QHBoxLayout()
                    t_row.addWidget(QLabel("Target:"))
                    top_p = PARAM_BY_PID[10]
                    t_cb = QComboBox()
                    for label, val in top_p.choices:
                        t_cb.addItem(label, val)
                    t_cb.setCurrentIndex(
                        next((i for i, c in enumerate(top_p.choices) if c[1] == top_p.default), 0)
                    )
                    t_cb.currentIndexChanged.connect(
                        lambda idx, pid=10, cb=t_cb: self._on_combo_changed(
                            pid, cb.itemData(idx)
                        )
                    )
                    t_row.addWidget(t_cb, 1)
                    mod_lay.addLayout(t_row)
                    self.param_widgets[10] = t_cb

                # Cross-linked to PWM Tab -> ADSR3 to PWM [PID 46]
                self._env_pwm_slider, self._env_pwm_rd = self._create_mirrored_slider_row(
                    mod_lay, "PWM Depth", 0, 1023, 512
                )

            lay.addWidget(mod_box)
            lay.addStretch(1)
            columns_layout.addWidget(box, 1)

        parent_layout.addLayout(columns_layout)
        parent_layout.addStretch(1)
        self._update_all_env_previews()

    def _on_env_slider_changed(
        self, bkey: str, fkey: str, value: int, rd: QLabel
    ) -> None:
        self._on_block_slider_changed(bkey, fkey, value, rd)
        self._update_env_preview(bkey)

    def _on_env_mode_changed(self, pid: int, value: int, bkey: str) -> None:
        self._on_combo_changed(pid, value)
        self._update_env_preview(bkey)

    def _update_env_preview(self, bkey: str) -> None:
        if bkey not in self._env_previews or bkey not in self.block_widgets:
            return
        w = self.block_widgets[bkey]
        a = w["attack"].value() if "attack" in w else 0
        d = w["decay"].value() if "decay" in w else 1200
        s = w["sustain"].value() if "sustain" in w else 3000
        r = w["release"].value() if "release" in w else 600

        mode_pids = {"adsr_vca": 224, "adsr_vcf": 225, "adsr_dco": 223}
        mode_cb = self.param_widgets.get(mode_pids.get(bkey, -1))
        mode = mode_cb.currentData() if isinstance(mode_cb, QComboBox) else 0

        self._env_previews[bkey].set_values(a, d, s, r, mode)

    def _update_all_env_previews(self) -> None:
        for bkey in ("adsr_vca", "adsr_vcf", "adsr_dco"):
            self._update_env_preview(bkey)
        for bkey in ("adsr_vca", "adsr_vcf", "adsr_dco"):
            self._update_env_preview(bkey)
        palette = theme.PALETTES.get(self.mode, theme.PALETTES[theme.DEFAULT_THEME])
        accent_col = QColor(palette["accent"])

        columns_layout = QHBoxLayout()
        columns_layout.setSpacing(10)

        self._env_previews: dict[str, EnvelopePreviewWidget] = {}

        # Envelope strip definitions:
        # (bkey, title, mode_pid, a_curve, d_curve, r_curve, restart_pid)
        env_specs = [
            ("adsr_vca", "EnvVCA (ADSR 1) — Amplitude", 224, 48, 49, 50, 8),
            ("adsr_vcf", "EnvVCF (ADSR 2) — Filter Timbre", 225, 51, 52, 53, 9),
            ("adsr_dco", "EnvDCO (ADSR 3) — Pitch & PWM", 223, 54, 55, 56, 214),
        ]

        for bkey, title, mode_pid, a_pid, d_pid, rel_pid, r_pid in env_specs:
            block = self.blocks_by_key[bkey]
            box = QGroupBox(title)
            lay = QVBoxLayout(box)
            lay.setSpacing(8)

            # Special Header Toggle for EnvDCO enable
            if bkey == "adsr_dco" and 126 in PARAM_BY_PID:
                en_p = PARAM_BY_PID[126]
                en_chk = QCheckBox("Enable EnvDCO")
                en_chk.setChecked(bool(en_p.default))
                en_chk.toggled.connect(
                    lambda checked, pid=126: self._on_check_toggled(pid, checked)
                )
                lay.addWidget(en_chk)
                self.param_widgets[126] = en_chk

            # 1. Live Vector Preview
            prev = EnvelopePreviewWidget(accent_col)
            lay.addWidget(prev)
            self._env_previews[bkey] = prev

            # 2. ADSR Vertical Faders
            faders_box = QGroupBox("Stages (ADSR)")
            faders_lay = QHBoxLayout(faders_box)
            faders_lay.setContentsMargins(4, 8, 4, 8)
            faders_lay.setSpacing(6)
            self.block_widgets[bkey] = {}

            for f in block.fields:
                col = QVBoxLayout()
                col.setSpacing(4)
                lbl = QLabel(f.label[0].upper())  # 'A', 'D', 'S', 'R'
                lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                lbl.setStyleSheet("font-weight: bold;")
                col.addWidget(lbl)

                slider = QSlider(Qt.Orientation.Vertical)
                slider.setRange(f.lo, f.hi)
                slider.setValue(f.default)
                slider.setMinimumHeight(120)

                rd = QLabel(str(f.default))
                rd.setObjectName("ReadoutLabel")
                rd.setAlignment(Qt.AlignmentFlag.AlignCenter)

                slider.valueChanged.connect(
                    lambda val, bk=bkey, fk=f.key, r=rd: self._on_env_slider_changed(
                        bk, fk, val, r
                    )
                )

                col.addWidget(slider, 1, Qt.AlignmentFlag.AlignCenter)
                col.addWidget(rd, 0, Qt.AlignmentFlag.AlignCenter)
                faders_lay.addLayout(col)

                self.block_widgets[bkey][f.key] = slider
                self._readouts[("b", bkey, f.key)] = rd

            lay.addWidget(faders_box)

            # 3. Shape & Response (Mode, Curves, Restart)
            shape_box = QGroupBox("Shape & Curves")
            shape_lay = QVBoxLayout(shape_box)
            shape_lay.setSpacing(4)

            # Mode
            if mode_pid in PARAM_BY_PID:
                mp = PARAM_BY_PID[mode_pid]
                m_row = QHBoxLayout()
                m_row.addWidget(QLabel("Mode:"))
                mcb = QComboBox()
                for label, val in mp.choices:
                    mcb.addItem(label, val)
                mcb.setCurrentIndex(
                    next((i for i, c in enumerate(mp.choices) if c[1] == mp.default), 0)
                )
                mcb.currentIndexChanged.connect(
                    lambda idx, pid=mp.pid, cb=mcb, bk=bkey: self._on_env_mode_changed(
                        pid, cb.itemData(idx), bk
                    )
                )
                m_row.addWidget(mcb, 1)
                shape_lay.addLayout(m_row)
                self.param_widgets[mp.pid] = mcb

            # Curves Grid (Attack, Decay, Release)
            grid_curves = QGridLayout()
            grid_curves.setContentsMargins(0, 2, 0, 2)
            grid_curves.setSpacing(4)

            for idx_c, (pid, clbl) in enumerate(
                ((a_pid, "Atk"), (d_pid, "Dec"), (rel_pid, "Rel"))
            ):
                if pid in PARAM_BY_PID:
                    cp = PARAM_BY_PID[pid]
                    grid_curves.addWidget(QLabel(clbl), 0, idx_c)
                    cb = QComboBox()
                    for label, val in cp.choices:
                        cb.addItem(label, val)
                    cb.setCurrentIndex(
                        next((i for i, c in enumerate(cp.choices) if c[1] == cp.default), 0)
                    )
                    cb.currentIndexChanged.connect(
                        lambda c_idx, p=pid, cbox=cb: self._on_combo_changed(
                            p, cbox.itemData(c_idx)
                        )
                    )
                    grid_curves.addWidget(cb, 1, idx_c)
                    self.param_widgets[pid] = cb

            shape_lay.addLayout(grid_curves)

            # Restart
            if r_pid in PARAM_BY_PID:
                rp = PARAM_BY_PID[r_pid]
                chk = QCheckBox(rp.label)
                chk.setChecked(bool(rp.default))
                chk.toggled.connect(
                    lambda checked, p=rp.pid: self._on_check_toggled(p, checked)
                )
                shape_lay.addWidget(chk)
                self.param_widgets[rp.pid] = chk

            lay.addWidget(shape_box)

            # 4. Target Modulations / Cross-Panel Links
            mod_box = QGroupBox("Target Modulations")
            mod_lay = QVBoxLayout(mod_box)
            mod_lay.setSpacing(4)

            if bkey == "adsr_vca" and 222 in PARAM_BY_PID:
                self._create_lfo_slider_row(mod_lay, 222, "VCA Output Level")
            elif bkey == "adsr_vcf":
                self._create_synced_slider_row(
                    mod_lay,
                    "VCF Cutoff Depth",
                    0,
                    512,
                    self.block_widgets.get("filter", {}).get("adsr2_to_vcf"),
                )
            elif bkey == "adsr_dco":
                if 47 in PARAM_BY_PID:
                    self._create_lfo_slider_row(mod_lay, 47, "Pitch Detune Depth")
                if 10 in PARAM_BY_PID:
                    t_row = QHBoxLayout()
                    t_row.addWidget(QLabel("Target:"))
                    top_p = PARAM_BY_PID[10]
                    t_cb = QComboBox()
                    for label, val in top_p.choices:
                        t_cb.addItem(label, val)
                    t_cb.setCurrentIndex(
                        next((i for i, c in enumerate(top_p.choices) if c[1] == top_p.default), 0)
                    )
                    t_cb.currentIndexChanged.connect(
                        lambda idx, pid=10, cb=t_cb: self._on_combo_changed(
                            pid, cb.itemData(idx)
                        )
                    )
                    t_row.addWidget(t_cb, 1)
                    mod_lay.addLayout(t_row)
                    self.param_widgets[10] = t_cb

                # Synced PWM envelope slider
                primary_pwm = self.param_widgets.get(46)
                if isinstance(primary_pwm, QSlider):
                    self._create_synced_slider_row(
                        mod_lay, "PWM Depth", 0, 1023, primary_pwm
                    )

            lay.addWidget(mod_box)
            lay.addStretch(1)
            columns_layout.addWidget(box, 1)

        parent_layout.addLayout(columns_layout)
        parent_layout.addStretch(1)
        self._update_all_env_previews()

    def _on_env_slider_changed(
        self, bkey: str, fkey: str, value: int, rd: QLabel
    ) -> None:
        self._on_block_slider_changed(bkey, fkey, value, rd)
        self._update_env_preview(bkey)

    def _on_env_mode_changed(self, pid: int, value: int, bkey: str) -> None:
        self._on_combo_changed(pid, value)
        self._update_env_preview(bkey)

    def _update_env_preview(self, bkey: str) -> None:
        if bkey not in self._env_previews or bkey not in self.block_widgets:
            return
        w = self.block_widgets[bkey]
        a = w["attack"].value() if "attack" in w else 0
        d = w["decay"].value() if "decay" in w else 1200
        s = w["sustain"].value() if "sustain" in w else 3000
        r = w["release"].value() if "release" in w else 600

        mode_pids = {"adsr_vca": 224, "adsr_vcf": 225, "adsr_dco": 223}
        mode_cb = self.param_widgets.get(mode_pids.get(bkey, -1))
        mode = mode_cb.currentData() if isinstance(mode_cb, QComboBox) else 0

        self._env_previews[bkey].set_values(a, d, s, r, mode)

    def _update_all_env_previews(self) -> None:
        for bkey in ("adsr_vca", "adsr_vcf", "adsr_dco"):
            self._update_env_preview(bkey)


    # --- LFOs Tab ---

    def _build_lfo_tab(self, parent_layout: QVBoxLayout) -> None:
        palette = theme.PALETTES.get(self.mode, theme.PALETTES[theme.DEFAULT_THEME])
        accent_col = QColor(palette["accent"])

        columns_layout = QHBoxLayout()
        columns_layout.setSpacing(12)

        # =====================================================================
        # LFO 1
        # =====================================================================
        lfo1_box = QGroupBox("LFO 1")
        lfo1_lay = QVBoxLayout(lfo1_box)
        lfo1_lay.setSpacing(8)

        # Generator (Waveform & Speed)
        gen1_box = QGroupBox("Generator")
        gen1_lay = QVBoxLayout(gen1_box)
        gen1_lay.setSpacing(6)

        wave1_row = QHBoxLayout()
        wave1_row.setContentsMargins(0, 1, 0, 1)
        wave1_row.setSpacing(6)

        lbl_w1 = QLabel("LFO1 waveform")
        lbl_w1.setFixedWidth(175)
        wave1_row.addWidget(lbl_w1)

        p_wave1 = PARAM_BY_PID[11]
        self._lfo1_combo = QComboBox()
        for label, val in p_wave1.choices:
            self._lfo1_combo.addItem(label, val)
        self._lfo1_combo.setCurrentIndex(
            next((i for i, c in enumerate(p_wave1.choices) if c[1] == p_wave1.default), 0)
        )
        self.param_widgets[11] = self._lfo1_combo
        wave1_row.addWidget(self._lfo1_combo, 1)

        self._lfo1_preview = LFOWaveformPreview(accent_col)
        self._lfo1_preview.set_waveform(self._lfo1_combo.currentText())
        wave1_row.addWidget(self._lfo1_preview)

        self._lfo1_combo.currentIndexChanged.connect(
            lambda idx, pid=11, cb=self._lfo1_combo, prev=self._lfo1_preview: self._on_lfo_wave_changed(
                pid, cb.itemData(idx), cb, prev
            )
        )
        gen1_lay.addLayout(wave1_row)

        self._create_lfo_slider_row(gen1_lay, 41, "LFO1 speed")
        lfo1_lay.addWidget(gen1_box)

        # LFO1 Destinations
        mod1_box = QGroupBox("LFO1 Destinations")
        mod1_lay = QVBoxLayout(mod1_box)
        mod1_lay.setSpacing(6)

        if 40 in PARAM_BY_PID:
            self._create_lfo_slider_row(mod1_lay, 40, "LFO1 to DCO")
        if 216 in PARAM_BY_PID:
            self._create_lfo_slider_row(mod1_lay, 216, "LFO1 to OSC1 extra")
        if 217 in PARAM_BY_PID:
            self._create_lfo_slider_row(mod1_lay, 217, "LFO1 to OSC2 extra")
        if 218 in PARAM_BY_PID and not PARAM_BY_PID[218].hidden:
            self._create_lfo_slider_row(mod1_lay, 218, "LFO1 to OSC3 extra")
        if 44 in PARAM_BY_PID:
            self._create_lfo_slider_row(mod1_lay, 44, "LFO1 to VCA")

        lfo1_lay.addWidget(mod1_box)

        zero_lfo1_btn = QPushButton("Zero LFO1 modulations")
        zero_lfo1_btn.clicked.connect(self._zero_lfo1_mods)
        lfo1_lay.addWidget(zero_lfo1_btn)

        lfo1_lay.addStretch(1)
        columns_layout.addWidget(lfo1_box, 1)

        # =====================================================================
        # LFO 2
        # =====================================================================
        lfo2_box = QGroupBox("LFO 2")
        lfo2_lay = QVBoxLayout(lfo2_box)
        lfo2_lay.setSpacing(8)

        # Generator (Waveform & Speed)
        gen2_box = QGroupBox("Generator")
        gen2_lay = QVBoxLayout(gen2_box)
        gen2_lay.setSpacing(6)

        wave2_row = QHBoxLayout()
        wave2_row.setContentsMargins(0, 1, 0, 1)
        wave2_row.setSpacing(6)

        lbl_w2 = QLabel("LFO2 waveform")
        lbl_w2.setFixedWidth(175)
        wave2_row.addWidget(lbl_w2)

        p_wave2 = PARAM_BY_PID[12]
        self._lfo2_combo = QComboBox()
        for label, val in p_wave2.choices:
            self._lfo2_combo.addItem(label, val)
        self._lfo2_combo.setCurrentIndex(
            next((i for i, c in enumerate(p_wave2.choices) if c[1] == p_wave2.default), 0)
        )
        self.param_widgets[12] = self._lfo2_combo
        wave2_row.addWidget(self._lfo2_combo, 1)

        self._lfo2_preview = LFOWaveformPreview(accent_col)
        self._lfo2_preview.set_waveform(self._lfo2_combo.currentText())
        wave2_row.addWidget(self._lfo2_preview)

        self._lfo2_combo.currentIndexChanged.connect(
            lambda idx, pid=12, cb=self._lfo2_combo, prev=self._lfo2_preview: self._on_lfo_wave_changed(
                pid, cb.itemData(idx), cb, prev
            )
        )
        gen2_lay.addLayout(wave2_row)

        self._create_lfo_slider_row(gen2_lay, 42, "LFO2 speed")
        lfo2_lay.addWidget(gen2_box)

        # LFO2 Destinations
        mod2_box = QGroupBox("LFO2 Destinations")
        mod2_lay = QVBoxLayout(mod2_box)
        mod2_lay.setSpacing(6)

        if 16 in PARAM_BY_PID:
            self._create_lfo_slider_row(mod2_lay, 16, "LFO2 to OSC2 detune")
        if 219 in PARAM_BY_PID:
            self._create_lfo_slider_row(mod2_lay, 219, "LFO2 to OSC2 coarse")
        if 36 in PARAM_BY_PID and not PARAM_BY_PID[36].hidden:
            self._create_lfo_slider_row(mod2_lay, 36, "LFO2 to OSC3 detune")
        if 220 in PARAM_BY_PID and not PARAM_BY_PID[220].hidden:
            self._create_lfo_slider_row(mod2_lay, 220, "LFO2 to OSC3 coarse")

        # Mirrored cross-panel sliders (now perfectly aligned)
        self._lfo2_vcf_slider, self._lfo2_vcf_rd = self._create_mirrored_slider_row(
            mod2_lay, "LFO2 to VCF", 0, 512, 0
        )
        self._lfo2_pw_slider, self._lfo2_pw_rd = self._create_mirrored_slider_row(
            mod2_lay, "LFO2 to PW", 0, 511, 0
        )

        lfo2_lay.addWidget(mod2_box)

        zero_lfo2_btn = QPushButton("Zero LFO2 modulations")
        zero_lfo2_btn.clicked.connect(self._zero_lfo2_mods)
        lfo2_lay.addWidget(zero_lfo2_btn)

        lfo2_lay.addStretch(1)
        columns_layout.addWidget(lfo2_box, 1)

        parent_layout.addLayout(columns_layout)
        parent_layout.addStretch(1)


    def _create_lfo_slider_row(
        self, parent_layout: QVBoxLayout, pid: int, label_text: str | None = None
    ) -> QSlider:
        p = PARAM_BY_PID[pid]
        row = QHBoxLayout()
        row.setContentsMargins(0, 1, 0, 1)
        row.setSpacing(6)

        lbl = QLabel(label_text or p.label)
        lbl.setFixedWidth(175)  # Exact fixed width for pixel-perfect alignment
        row.addWidget(lbl)

        slider = BipolarSlider(Qt.Orientation.Horizontal)
        slider.setRange(p.lo, p.hi)
        slider.setValue(p.default)
        slider.setToolTip("Double-click to reset to 0")

        rd = QLabel(param_meta.format_display_value(pid, p.default))
        rd.setObjectName("ReadoutLabel")
        rd.setFixedWidth(50)
        rd.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        slider.valueChanged.connect(
            lambda val, p_id=p.pid, r=rd: self._on_slider_changed(p_id, val, r)
        )

        row.addWidget(slider, 1)
        row.addWidget(rd)

        zero_btn = QPushButton("0")
        zero_btn.setToolTip("Reset to 0")
        zero_btn.setFixedWidth(26)
        zero_btn.clicked.connect(lambda _, s=slider: s.setValue(0))
        row.addWidget(zero_btn)

        parent_layout.addLayout(row)
        self.param_widgets[p.pid] = slider
        self._readouts[("p", p.pid)] = rd
        return slider

    def _create_mirrored_slider_row(
        self, parent_layout: QVBoxLayout, label_text: str, lo: int, hi: int, default: int = 0
    ) -> tuple[QSlider, QLabel]:
        row = QHBoxLayout()
        row.setContentsMargins(0, 1, 0, 1)
        row.setSpacing(6)

        lbl = QLabel(label_text)
        lbl.setFixedWidth(175)  # Matches _create_lfo_slider_row exactly
        row.addWidget(lbl)

        slider = BipolarSlider(Qt.Orientation.Horizontal)
        slider.setRange(lo, hi)
        slider.setValue(default)
        slider.setToolTip("Double-click to reset to 0 (synced across tabs)")

        rd = QLabel(str(default))
        rd.setObjectName("ReadoutLabel")
        rd.setFixedWidth(50)
        rd.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        slider.valueChanged.connect(lambda val, r=rd: r.setText(str(val)))

        row.addWidget(slider, 1)
        row.addWidget(rd)

        zero_btn = QPushButton("0")
        zero_btn.setToolTip("Reset to 0")
        zero_btn.setFixedWidth(26)
        zero_btn.clicked.connect(lambda _, s=slider: s.setValue(0))
        row.addWidget(zero_btn)

        parent_layout.addLayout(row)
        return slider, rd

    def _create_synced_slider_row(
        self,
        parent_layout: QVBoxLayout,
        label_text: str,
        lo: int,
        hi: int,
        primary_slider: QSlider | None,
    ) -> tuple[QSlider, QLabel]:
        """Creates a slider row that stays bidirectionally in sync with a primary slider on another tab."""
        row = QHBoxLayout()
        row.setContentsMargins(0, 1, 0, 1)

        lbl = QLabel(label_text)
        lbl.setMinimumWidth(160)
        row.addWidget(lbl)

        slider = BipolarSlider(Qt.Orientation.Horizontal)
        slider.setRange(lo, hi)
        init_val = primary_slider.value() if primary_slider else 0
        slider.setValue(init_val)
        slider.setToolTip("Double-click to reset to 0 (synced across tabs)")

        rd = QLabel(str(init_val))
        rd.setObjectName("ReadoutLabel")
        rd.setFixedWidth(50)
        rd.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        row.addWidget(slider, 1)
        row.addWidget(rd)

        zero_btn = QPushButton("0")
        zero_btn.setToolTip("Reset to 0")
        zero_btn.setFixedWidth(26)
        zero_btn.clicked.connect(lambda _, s=slider: s.setValue(0))
        row.addWidget(zero_btn)

        if primary_slider:
            def sync_from_primary(val: int) -> None:
                slider.blockSignals(True)
                slider.setValue(val)
                slider.blockSignals(False)
                rd.setText(str(val))

            primary_slider.valueChanged.connect(sync_from_primary)
            slider.valueChanged.connect(primary_slider.setValue)

        parent_layout.addLayout(row)
        return slider, rd

    def _zero_lfo1_mods(self) -> None:
        for pid in (40, 44, 216, 217, 218):
            w = self.param_widgets.get(pid)
            if isinstance(w, QSlider):
                w.setValue(0)
        self.log("[ui] Zeroed LFO 1 modulations\n")

    def _zero_lfo2_mods(self) -> None:
        for pid in (16, 219, 36, 220, 45):
            w = self.param_widgets.get(pid)
            if isinstance(w, QSlider):
                w.setValue(0)
        vcf_w = self.block_widgets.get("filter", {}).get("lfo2_to_vcf")
        if isinstance(vcf_w, QSlider):
            vcf_w.setValue(0)
        self.log("[ui] Zeroed LFO 2 modulations\n")

    def _on_lfo_wave_changed(
        self, pid: int, value: int, combo: QComboBox, preview: LFOWaveformPreview
    ) -> None:
        self._on_combo_changed(pid, value)
        preview.set_waveform(combo.currentText())


    # --- Mod Matrix Tab ---

    def _build_mod_tab(self, parent_layout: QVBoxLayout) -> None:
        top_bar = QHBoxLayout()
        self.mod_summary_label = QLabel("0 of 8 routings active")
        self.mod_summary_label.setObjectName("MutedLabel")
        top_bar.addWidget(self.mod_summary_label)
        top_bar.addStretch(1)

        zero_all_btn = QPushButton("Zero All Depths")
        zero_all_btn.setToolTip("Set all 8 modulation depths to 0")
        zero_all_btn.clicked.connect(self._mod_zero_all)
        top_bar.addWidget(zero_all_btn)

        clear_all_btn = QPushButton("Clear All")
        clear_all_btn.setToolTip("Reset all sources to Off and depths to 0")
        clear_all_btn.clicked.connect(self._mod_clear_all)
        top_bar.addWidget(clear_all_btn)

        parent_layout.addLayout(top_bar)

        matrix_box = QGroupBox("Modulation Matrix (8 Slots)")
        grid = QGridLayout(matrix_box)
        grid.setContentsMargins(12, 12, 12, 12)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)

        headers = [
            ("SLOT", 0, Qt.AlignmentFlag.AlignCenter),
            ("SOURCE", 1, Qt.AlignmentFlag.AlignLeft),
            ("", 2, Qt.AlignmentFlag.AlignCenter),
            ("DESTINATION", 3, Qt.AlignmentFlag.AlignLeft),
            ("DEPTH / AMOUNT", 4, Qt.AlignmentFlag.AlignLeft),
            ("VALUE", 5, Qt.AlignmentFlag.AlignRight),
            ("", 6, Qt.AlignmentFlag.AlignCenter),
            ("", 7, Qt.AlignmentFlag.AlignCenter),
        ]
        for title, col, align in headers:
            if not title:
                continue
            lbl = QLabel(title)
            lbl.setObjectName("MutedLabel")
            font = lbl.font()
            font.setBold(True)
            font.setPointSize(max(8, int(self.base_font_size * self.zoom_scale * 0.85)))
            lbl.setFont(font)
            grid.addWidget(lbl, 0, col, align)

        self._mod_slot_dots.clear()

        for i in range(8):
            r = i + 1
            src_pid = 63 + i * 3
            dest_pid = 64 + i * 3
            depth_pid = 65 + i * 3

            slot_id_widget = QWidget()
            slot_id_lay = QHBoxLayout(slot_id_widget)
            slot_id_lay.setContentsMargins(0, 0, 0, 0)
            slot_id_lay.setSpacing(4)

            dot = QLabel("●")
            dot.setFixedWidth(14)
            slot_id_lay.addWidget(dot)
            self._mod_slot_dots.append(dot)

            slot_num = QLabel(f"{i + 1:02d}")
            font = slot_num.font()
            font.setBold(True)
            slot_num.setFont(font)
            slot_num.setFixedWidth(22)
            slot_id_lay.addWidget(slot_num)
            grid.addWidget(slot_id_widget, r, 0, Qt.AlignmentFlag.AlignCenter)

            sp = PARAM_BY_PID[src_pid]
            src_cb = QComboBox()
            for label, val in sp.choices:
                src_cb.addItem(label, val)
            src_cb.setCurrentIndex(
                next((idx for idx, c in enumerate(sp.choices) if c[1] == sp.default), 0)
            )
            src_cb.currentIndexChanged.connect(
                lambda idx, pid=src_pid, cb=src_cb, s=i: self._on_mod_combo_changed(
                    pid, cb.itemData(idx), s
                )
            )
            src_cb.setMinimumWidth(150)
            grid.addWidget(src_cb, r, 1)
            self.param_widgets[src_pid] = src_cb

            arrow = QLabel("➔")
            arrow.setObjectName("MutedLabel")
            arrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
            arrow.setFixedWidth(18)
            grid.addWidget(arrow, r, 2)

            dp = PARAM_BY_PID[dest_pid]
            dest_cb = QComboBox()
            for label, val in dp.choices:
                dest_cb.addItem(label, val)
            dest_cb.setCurrentIndex(
                next((idx for idx, c in enumerate(dp.choices) if c[1] == dp.default), 0)
            )
            dest_cb.currentIndexChanged.connect(
                lambda idx, pid=dest_pid, cb=dest_cb, s=i: self._on_mod_combo_changed(
                    pid, cb.itemData(idx), s
                )
            )
            dest_cb.setMinimumWidth(150)
            grid.addWidget(dest_cb, r, 3)
            self.param_widgets[dest_pid] = dest_cb

            depth_p = PARAM_BY_PID[depth_pid]
            slider = BipolarSlider(Qt.Orientation.Horizontal)
            slider.setRange(depth_p.lo, depth_p.hi)
            slider.setValue(depth_p.default)
            slider.setToolTip("Double-click to reset to 0")

            rd = QLabel(param_meta.format_display_value(depth_pid, depth_p.default))
            rd.setObjectName("ReadoutLabel")
            rd.setFixedWidth(54)
            rd.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

            slider.valueChanged.connect(
                lambda val, pid=depth_pid, rd=rd, s=i: self._on_mod_slider_changed(
                    pid, val, rd, s
                )
            )
            grid.addWidget(slider, r, 4)
            grid.addWidget(rd, r, 5)
            self.param_widgets[depth_pid] = slider
            self._readouts[("p", depth_pid)] = rd

            zero_btn = QPushButton("0")
            zero_btn.setToolTip(f"Reset Slot {i+1} depth to 0")
            zero_btn.setFixedWidth(28)
            zero_btn.clicked.connect(lambda _, s=slider: s.setValue(0))
            grid.addWidget(zero_btn, r, 6)

            inv_btn = QPushButton("±")
            inv_btn.setToolTip(f"Invert Slot {i+1} depth polarity")
            inv_btn.setFixedWidth(28)
            inv_btn.clicked.connect(lambda _, s=slider: s.setValue(-s.value()))
            grid.addWidget(inv_btn, r, 7)

        grid.setColumnStretch(1, 3)
        grid.setColumnStretch(3, 3)
        grid.setColumnStretch(4, 5)

        parent_layout.addWidget(matrix_box)

        tip = QLabel("💡 Tip: Double-click any depth slider or press [0] to center. Press [±] to invert modulation polarity.")
        tip.setObjectName("MutedLabel")
        parent_layout.addWidget(tip)
        parent_layout.addStretch(1)

        self._update_all_mod_indicators()

    def _on_mod_combo_changed(self, pid: int, value: int, slot: int) -> None:
        self._on_combo_changed(pid, value)
        self._update_mod_slot_indicator(slot)

    def _on_mod_slider_changed(self, pid: int, value: int, rd: QLabel, slot: int) -> None:
        self._on_slider_changed(pid, value, rd)
        self._update_mod_slot_indicator(slot)

    def _is_mod_slot_active(self, slot: int) -> bool:
        src_w = self.param_widgets.get(63 + slot * 3)
        depth_w = self.param_widgets.get(65 + slot * 3)
        src_val = src_w.currentData() if isinstance(src_w, QComboBox) else 0
        depth_val = depth_w.value() if isinstance(depth_w, QSlider) else 0
        return src_val != 0 and depth_val != 0

    def _update_mod_slot_indicator(self, slot: int) -> None:
        if slot >= len(self._mod_slot_dots):
            return
        dot = self._mod_slot_dots[slot]
        active = self._is_mod_slot_active(slot)
        palette = theme.PALETTES.get(self.mode, theme.PALETTES[theme.DEFAULT_THEME])
        dot_col = palette["ok"] if active else palette["off"]
        dot.setStyleSheet(f"color: {dot_col}; font-size: 13px;")
        self._update_mod_summary()

    def _update_all_mod_indicators(self) -> None:
        for i in range(len(self._mod_slot_dots)):
            self._update_mod_slot_indicator(i)

    def _update_mod_summary(self) -> None:
        if not hasattr(self, "mod_summary_label"):
            return
        count = sum(1 for i in range(8) if self._is_mod_slot_active(i))
        self.mod_summary_label.setText(f"{count} of 8 routings active")

    def _mod_zero_all(self) -> None:
        for i in range(8):
            w = self.param_widgets.get(65 + i * 3)
            if isinstance(w, QSlider):
                w.setValue(0)
        self.log("[ui] Zeroed all mod matrix depths\n")

    def _mod_clear_all(self) -> None:
        res = QMessageBox.question(
            self,
            "Clear Mod Matrix",
            "Reset all 8 modulation matrix slots to default (Off, 0 depth)?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if res != QMessageBox.StandardButton.Yes:
            return

        for i in range(8):
            src_cb = self.param_widgets.get(63 + i * 3)
            dest_cb = self.param_widgets.get(64 + i * 3)
            depth_sl = self.param_widgets.get(65 + i * 3)
            if isinstance(src_cb, QComboBox):
                src_cb.setCurrentIndex(0)
            if isinstance(dest_cb, QComboBox):
                dest_cb.setCurrentIndex(0)
            if isinstance(depth_sl, QSlider):
                depth_sl.setValue(0)
        self.log("[ui] Cleared all mod matrix slots\n")


    # --- Diagnostics Tab ---

    def _build_diag_tab(self, parent_layout: QVBoxLayout) -> None:
        palette = theme.PALETTES.get(self.mode, theme.PALETTES[theme.DEFAULT_THEME])

        # FIX: Extract the command IDs into a set of integers for proper lookup
        valid_debug_ids = {val for _, val in models.filter_debug_commands(params.DEBUG_COMMANDS)}

        def make_btn(label: str, val: int, danger: bool = False) -> QPushButton:
            btn = QPushButton(label)
            btn.setMinimumHeight(26)
            if danger:
                btn.setStyleSheet(
                    f"color: {palette['accent_active']}; font-weight: bold; border-color: {palette['accent_active']};"
                )
            btn.clicked.connect(lambda _, v=val, l=label: self._send_debug_cmd(v, l))
            return btn

        master_split = QHBoxLayout()
        master_split.setSpacing(12)

        # =====================================================================
        # LEFT COLUMN: SYSTEM HEALTH, HARDWARE PROBES & MCU LIFECYCLE
        # =====================================================================
        left_col = QVBoxLayout()
        left_col.setSpacing(8)

        # 1. Hardware & DMA Topology
        hw_box = QGroupBox("Hardware & DMA Topology")
        hw_lay = QVBoxLayout(hw_box)
        hw_lay.setSpacing(6)

        hw_grid = QGridLayout()
        hw_grid.setSpacing(4)
        r = 0
        if 1 in valid_debug_ids:
            hw_grid.addWidget(make_btn("PIO Topology", 1), r, 0)
        if 4 in valid_debug_ids:
            hw_grid.addWidget(make_btn("Sub-Osc Engine", 4), r, 1)
            r += 1
        elif 1 in valid_debug_ids:
            r += 1

        if 5 in valid_debug_ids:
            hw_grid.addWidget(make_btn("PWM DMA Report", 5), r, 0)
        if 6 in valid_debug_ids:
            hw_grid.addWidget(make_btn("DMA Channel Map", 6), r, 1)
        hw_lay.addLayout(hw_grid)
        left_col.addWidget(hw_box)

        # 2. Timing Probes & Note Retrigger
        timing_box = QGroupBox("Timing Probes & Note Retrigger")
        timing_lay = QVBoxLayout(timing_box)
        timing_lay.setSpacing(6)

        timing_grid = QGridLayout()
        timing_grid.setSpacing(4)
        if 2 in valid_debug_ids:
            timing_grid.addWidget(make_btn("Period Probe (div 2k)", 2), 0, 0)
        if 3 in valid_debug_ids:
            timing_grid.addWidget(make_btn("Period Probe (div 20k)", 3), 0, 1)
        if 26 in valid_debug_ids:
            timing_grid.addWidget(make_btn("Retrig: EXACT_Y", 26), 1, 0)
        if 27 in valid_debug_ids:
            timing_grid.addWidget(make_btn("Retrig: SYNC_JMP", 27), 1, 1)
        timing_lay.addLayout(timing_grid)
        left_col.addWidget(timing_box)

        # 3. Memory & Peripherals (MCP4728)
        util_box = QGroupBox("Memory & Peripherals")
        util_lay = QVBoxLayout(util_box)
        util_lay.setSpacing(6)

        util_grid = QGridLayout()
        util_grid.setSpacing(4)
        if 13 in valid_debug_ids:
            util_grid.addWidget(make_btn("Dump RAM (Heap/Stack)", 13), 0, 0, 1, 2)
        if 15 in valid_debug_ids:
            util_grid.addWidget(make_btn("Mem Polls ON", 15), 1, 0)
        if 14 in valid_debug_ids:
            util_grid.addWidget(make_btn("Mem Polls OFF", 14), 1, 1)

        # MCP4728 DACs
        util_grid.addWidget(make_btn("MCP4728 Probe", 43), 2, 0)
        util_grid.addWidget(make_btn("MCP4728 Reattach", 44), 2, 1)

        util_lay.addLayout(util_grid)
        left_col.addWidget(util_box)

        # 4. MCU Lifecycle (Power / Bootloader)
        mcu_box = QGroupBox("MCU Power & Bootloader")
        mcu_lay = QHBoxLayout(mcu_box)
        mcu_lay.setSpacing(6)

        if 90 in valid_debug_ids:
            mcu_lay.addWidget(make_btn("Reboot MCU", 90, danger=True))
        if 91 in valid_debug_ids:
            mcu_lay.addWidget(make_btn("Reboot to BOOTSEL", 91, danger=True))

        left_col.addWidget(mcu_box)
        left_col.addStretch(1)

        master_split.addLayout(left_col, 5)

        # =====================================================================
        # RIGHT COLUMN: EXECUTION PROFILERS & DSP BENCHMARKS
        # =====================================================================
        right_col = QVBoxLayout()
        right_col.setSpacing(8)

        # 1. Hot-Path Execution Profilers
        prof_box = QGroupBox("Execution Profilers")
        prof_lay = QVBoxLayout(prof_box)
        prof_lay.setSpacing(6)

        lbl_dco_prof = QLabel("DCO Core Profiler (requires RUNNING_AVERAGE in firmware):")
        lbl_dco_prof.setObjectName("MutedLabel")
        prof_lay.addWidget(lbl_dco_prof)

        dco_prof_row = QHBoxLayout()
        dco_prof_row.setSpacing(4)
        for label, val in params.BENCH_COMMANDS:
            dco_prof_row.addWidget(make_btn(label.replace("profiler ", "").capitalize(), val))
        prof_lay.addLayout(dco_prof_row)

        # Mainboard Profiler (STM32 over Serial2, if supported)
        if models.active().has_mainboard:
            lbl_mb_prof = QLabel("Mainboard Profiler (STM32 over Serial2):")
            lbl_mb_prof.setObjectName("MutedLabel")
            prof_lay.addWidget(lbl_mb_prof)

            mb_prof_row = QHBoxLayout()
            mb_prof_row.setSpacing(4)
            for label, val in params.BENCH_MB_COMMANDS:
                mb_prof_row.addWidget(make_btn(label.replace("Mainboard ", "").capitalize(), val))
            prof_lay.addLayout(mb_prof_row)

        right_col.addWidget(prof_box)

        # 2. DSP & Math Algorithm Benchmarks
        bench_box = QGroupBox("DSP & Math Benchmarks")
        bench_lay = QVBoxLayout(bench_box)
        bench_lay.setSpacing(8)

        # A. Amp-Compensation Methods & Tests
        lbl_amp = QLabel("Amplitude Compensation (Method Selectors & Benchmarks):")
        lbl_amp.setObjectName("MutedLabel")
        bench_lay.addWidget(lbl_amp)

        amp_grid = QGridLayout()
        amp_grid.setSpacing(4)
        amp_grid.addWidget(make_btn("FLOAT_QUAD", 20), 0, 0)
        amp_grid.addWidget(make_btn("LUT Table", 21), 0, 1)
        amp_grid.addWidget(make_btn("FIXED Math", 22), 0, 2)
        amp_grid.addWidget(make_btn("Speed Bench", 24), 1, 0, 1, 1)
        amp_grid.addWidget(make_btn("Accuracy Test", 25), 1, 1, 1, 2)
        bench_lay.addLayout(amp_grid)

        # B. Pitch Interpolation
        lbl_pitch = QLabel("Pitch Interpolation (FLOAT vs RATIO_Q16 vs Q12):")
        lbl_pitch.setObjectName("MutedLabel")
        bench_lay.addWidget(lbl_pitch)

        pitch_row = QHBoxLayout()
        pitch_row.setSpacing(4)
        for label, val in params.PITCH_INTERP_COMMANDS:
            pitch_row.addWidget(make_btn(label.replace("Pitch: ", "").capitalize(), val))
        bench_lay.addLayout(pitch_row)

        # C. Clock Divider Methods
        lbl_clk = QLabel("Clock Divider (HP Methods vs GOLD_REF):")
        lbl_clk.setObjectName("MutedLabel")
        bench_lay.addWidget(lbl_clk)

        clk_row = QHBoxLayout()
        clk_row.setSpacing(4)
        for label, val in params.CLKDIV_HP_COMMANDS:
            clk_row.addWidget(make_btn(label.replace("Clkdiv: ", "").capitalize(), val))
        bench_lay.addLayout(clk_row)

        right_col.addWidget(bench_box)
        right_col.addStretch(1)

        master_split.addLayout(right_col, 5)

        parent_layout.addLayout(master_split)

        # Footer Log Reminder
        tip_lbl = QLabel("💡 <i>All diagnostic reports, benchmark statistics, and RAM dumps stream directly to the Board Output pane below.</i>")
        tip_lbl.setObjectName("MutedLabel")
        parent_layout.addWidget(tip_lbl)
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

    def _manual_cal_refresh_from_board(self) -> None:
        if not self._mcu_ready():
            return

        def pwcal_done(ok, payload):
            if ok:
                try:
                    channels = fileformats.decode_cal_table("PWCal3Pt", payload)
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
                cd = w.currentData()
                return cd if cd is not None else w.currentIndex()
            if isinstance(w, QCheckBox):
                return 1 if w.isChecked() else 0
            if isinstance(w, QPushButton) and w.isCheckable():
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

        # Refresh all dynamic previews with new patch values
        if hasattr(self, "_mod_slot_dots") and self._mod_slot_dots:
            self._update_all_mod_indicators()
        if hasattr(self, "_lfo1_preview") and hasattr(self, "_lfo1_combo"):
            self._lfo1_preview.set_waveform(self._lfo1_combo.currentText())
        if hasattr(self, "_lfo2_preview") and hasattr(self, "_lfo2_combo"):
            self._lfo2_preview.set_waveform(self._lfo2_combo.currentText())
        if hasattr(self, "_env_previews"):
            self._update_all_env_previews()
        if hasattr(self, "_filter_preview"):
            self._update_vcf_preview()
        if hasattr(self, "_pwm_preview"):
            self._update_pwm_preview()
        if hasattr(self, "_wave_buttons"):
            for btn in self._wave_buttons.values():
                self._style_wave_button(btn, btn.isChecked())

        self._refresh_dirty()
        if persist_current:
            presets.save_bank(self.bank)
        if send and self.link.is_open:
            self.send_all()

    def _apply_slot_dict(self, slot: dict | None) -> None:
        data = (
            presets.defaults_slot()
            if presets.slot_is_empty(slot)
            else slot
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
            elif 0 <= val < w.count():
                w.setCurrentIndex(val)
        elif isinstance(w, QCheckBox):
            w.setChecked(bool(val))
        elif isinstance(w, QPushButton) and w.isCheckable():
            w.setChecked(bool(val))
            if hasattr(self, "_style_wave_button"):
                self._style_wave_button(w, bool(val))

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
            self.preset_name_entry.setText(name)
            self.preset_combo.blockSignals(True)
            self.preset_combo.setCurrentIndex(idx)
            self.preset_combo.blockSignals(False)
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
        vals = {f.key: self.block_widgets[key][f.key].value() for f in block.fields}
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
        slot_idx = self.preset_combo.currentIndex()

        screen = models.active().has_screen_signals
        if screen:
            self.send_now(protocol.screen_signal(protocol.SCREEN_SIGNAL_SILENT))
            n += 1

        self.send_now(protocol.param16(protocol.PARAM_UI_PRESET_SCROLL, slot_idx))
        n += 1

        self.send_now(protocol.preset_name(self.preset_name_entry.text()))
        n += 1

        for p in presets.patch_params():
            w = self.param_widgets.get(p.pid)
            val = p.default
            if isinstance(w, QSlider):
                val = w.value()
            elif isinstance(w, QComboBox):
                cd = w.currentData()
                val = cd if cd is not None else (w.currentIndex() if w.currentIndex() >= 0 else p.default)
            elif isinstance(w, QCheckBox):
                val = 1 if w.isChecked() else 0
            elif isinstance(w, QPushButton) and w.isCheckable():
                val = 1 if w.isChecked() else 0

            self.queue_param(p.pid, val)
            n += 1

        for block in presets.patch_blocks():
            self.queue_block(block.key)
            n += 1

        self._flush()

        if screen:
            self.send_now(protocol.screen_signal(protocol.SCREEN_SIGNAL_PRESET_SCROLL))
            n += 1

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

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setAutoDefault(False)
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        save_btn = QPushButton("Save")
        save_btn.setObjectName("AccentButton")
        save_btn.setDefault(True)
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

            slot_str = f"▶ {i:03d}" if is_active else f"  {i:03d}"
            item_slot = QTableWidgetItem(slot_str)
            if is_active:
                font = item_slot.font()
                font.setBold(True)
                item_slot.setFont(font)
                item_slot.setForeground(accent_col)
            self.table.setItem(i, 0, item_slot)

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
