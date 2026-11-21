import sys
import json
import time
import math
import csv
import wave
import tempfile
import subprocess
import shutil
import threading
import queue
from pathlib import Path

import serial
from serial.tools import list_ports
import numpy as np
import soundfile as sf
import sounddevice as sd

from PySide6.QtCore import (
    Qt, QTimer, QThread, Signal, QSize, QPoint, QRectF, QPointF, QMimeData
)
from PySide6.QtGui import (
    QKeySequence, QAction, QIcon, QPixmap, QFont, QPainter,
    QColor, QPen, QBrush, QPainterPath, QDrag, QTextDocument
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QFormLayout, QGridLayout, QTabWidget, QLabel, QPushButton,
    QComboBox, QSpinBox, QSlider, QTableWidget, QTableWidgetItem,
    QHeaderView, QDialog, QLineEdit, QMessageBox, QDoubleSpinBox,
    QStackedWidget, QProgressBar, QGroupBox, QScrollArea,
    QPlainTextEdit, QCheckBox, QSizePolicy, QFrame, QFileDialog,
    QListWidget, QListWidgetItem, QMenu, QListView, QInputDialog,
    QStyledItemDelegate, QStyle
)


class NoWheelSpinBox(QSpinBox):
    def wheelEvent(self, event):
        event.ignore()


class NoWheelDoubleSpinBox(QDoubleSpinBox):
    def wheelEvent(self, event):
        event.ignore()


class NoWheelComboBox(QComboBox):
    def wheelEvent(self, event):
        event.ignore()


QSpinBox = NoWheelSpinBox
QDoubleSpinBox = NoWheelDoubleSpinBox
QComboBox = NoWheelComboBox


class DetentSlider(QSlider):
    """Vertical/horizontal slider with a wide, sticky detent at 0 (used for pitch)."""
    DETENT = 90  # ~1.44% of the 2001-range fader snaps to 0

    def __init__(self, orientation, parent=None):
        super().__init__(orientation, parent)
        self._suppress_detent = False

    def setValue(self, value):
        # Programmatic set bypasses the detent so sync buttons can land precisely.
        self._suppress_detent = True
        try:
            super().setValue(value)
        finally:
            self._suppress_detent = False

    def _snap_to_zero_if_near(self):
        if self._suppress_detent:
            return
        v = self.value()
        if 0 < abs(v) <= self.DETENT:
            self._suppress_detent = True
            try:
                super().setValue(0)
            finally:
                self._suppress_detent = False

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
        self._snap_to_zero_if_near()

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        self._snap_to_zero_if_near()

    def keyPressEvent(self, event):
        super().keyPressEvent(event)
        self._snap_to_zero_if_near()

    def wheelEvent(self, event):
        event.ignore()


CONFIG_FILE = Path(__file__).with_name("dmx_settings.json")
PLAYLIST_FILE = Path(__file__).with_name("dmx_playlist.json")
PLAYLIST_AUDIO_DIR = Path(__file__).with_name("dmx_playlist_audio")
PLAYLIST_SHOW_DIR = Path(__file__).with_name("dmx_playlist_shows")
CHANNELS = 512

VOSK_MODEL_DIR = Path(__file__).with_name("vosk-model-small-en-us-0.15")

FADE_IN_SECONDS = 5.0
FADE_OUT_SECONDS = 0.6

PRELOAD_COUNT = 4
QUEUE_PREVIEW_COUNT = 4

DECK_RATE = 44100

PITCH_RANGE = 0.16
ENVELOPE_BIN_SECONDS = 0.005

ACTIONS = [
    "Set value",
    "Momentary (reset on release)",
    "Toggle value",
    "Increase while held",
    "Decrease while held",
    "Joystick (Continuous)",
    "Joystick (Hold Value)",
    "Trigger (Continuous)",
    "Trigger (Positive Hold Value)",
    "Trigger (Negative Hold Value)",
    "Increase by amount when pressed",
    "Decrease by amount when pressed",
    "Single Increase (+1 per press)",
    "Single Decrease (-1 per press)",
    "Pause / Play generated channel",
    "Play sound effect (full one-shot)",
    "Play sound effect while held",
    "Talk into output (while held)",
    "Fade music out while held (fade back in on release)",
    "Voice command (while held)",
]

CONTROLLER_CONTROLS = [
    "Left Stick -X", "Left Stick +X",
    "Left Stick -Y", "Left Stick +Y",
    "Right Stick -X", "Right Stick +X",
    "Right Stick -Y", "Right Stick +Y",
    "Left Trigger", "Right Trigger",
    "A", "B", "X", "Y", "LB", "RB",
    "View", "Menu", "Left Stick Click",
    "Right Stick Click", "Guide",
    "D-pad Up", "D-pad Down", "D-pad Left", "D-pad Right"
]

BUTTON_MAP = {
    0: "A", 1: "B", 2: "X", 3: "Y",
    4: "LB", 5: "RB", 6: "View", 7: "Menu",
    8: "Left Stick Click", 9: "Right Stick Click", 10: "Guide"
}

AXIS_MAP = {
    "Left Stick X": 0,
    "Left Stick Y": 1,
    "Right Stick X": 2,
    "Right Stick Y": 3,
    "Left Trigger": 4,
    "Right Trigger": 5
}

STICK_DIRECTION_MAP = {
    "Left Stick -X": ("Left Stick X", -1),
    "Left Stick +X": ("Left Stick X", 1),
    "Left Stick -Y": ("Left Stick Y", -1),
    "Left Stick +Y": ("Left Stick Y", 1),
    "Right Stick -X": ("Right Stick X", -1),
    "Right Stick +X": ("Right Stick X", 1),
    "Right Stick -Y": ("Right Stick Y", -1),
    "Right Stick +Y": ("Right Stick Y", 1),
}

STICK_CONTROLS = set(STICK_DIRECTION_MAP)
TRIGGER_CONTROLS = {"Left Trigger", "Right Trigger"}

SENSITIVITY_DEFAULTS = {
    "left_x_sensitivity": 100,
    "left_y_sensitivity": 100,
    "right_x_sensitivity": 100,
    "right_y_sensitivity": 100,
    "left_trigger_sensitivity": 100,
    "right_trigger_sensitivity": 100,
    "left_stick_sensitivity": 100,
    "right_stick_sensitivity": 100
}

MIC_SAMPLE_RATE = 16000

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

CAMELOT_MAJOR = {
    0: "8B", 1: "3B", 2: "10B", 3: "5B", 4: "12B", 5: "7B",
    6: "2B", 7: "9B", 8: "4B", 9: "11B", 10: "6B", 11: "1B",
}
CAMELOT_MINOR = {
    0: "5A", 1: "12A", 2: "7A", 3: "2A", 4: "9A", 5: "4A",
    6: "11A", 7: "6A", 8: "1A", 9: "8A", 10: "3A", 11: "10A",
}

PIANO_SAMPLE_RATE = 44100


def midi_note_name(note):
    if note < 0:
        return "—"
    octave = (note // 12) - 1
    return f"{NOTE_NAMES[note % 12]}{octave}"


def midi_note_frequency(note):
    return 440.0 * (2.0 ** ((int(note) - 69) / 12.0))


def key_to_string(pitch_class, is_minor):
    note = NOTE_NAMES[pitch_class % 12]
    quality = "m" if is_minor else ""
    return f"{note}{quality}"


def key_to_camelot(pitch_class, is_minor):
    table = CAMELOT_MINOR if is_minor else CAMELOT_MAJOR
    return table[pitch_class % 12]


def camelot_compatibility(a, b):
    if not a or not b:
        return 0.3
    try:
        na, la = int(a[:-1]), a[-1].upper()
        nb, lb = int(b[:-1]), b[-1].upper()
    except (ValueError, IndexError):
        return 0.3
    if a == b:
        return 1.0
    diff = min(abs(na - nb), 12 - abs(na - nb))
    same_letter = la == lb
    if same_letter and diff == 1:
        return 0.85
    if not same_letter and diff == 0:
        return 0.80
    if not same_letter and diff == 1:
        return 0.60
    if same_letter and diff == 2:
        return 0.55
    return 0.20


def _compat_color(score):
    try:
        score = max(0.0, min(1.0, float(score)))
    except (TypeError, ValueError):
        score = 0.0
    color = QColor()
    color.setHsvF((score * 120.0) / 360.0, 0.80, 0.72)
    return color.name()


def ft_in_to_ft(feet, inches):
    return float(feet) + float(inches) / 12.0


def ft_to_ft_in(value):
    value = float(value)
    sign = -1.0 if value < 0 else 1.0
    value = abs(value)
    feet = int(value)
    inches = int(round((value - feet) * 12.0))
    if inches >= 12:
        feet += 1
        inches = 0
    return sign * feet, inches


def ft_in_str(value_ft):
    feet, inches = ft_to_ft_in(value_ft)
    sign = "-" if feet < 0 or (feet == 0 and inches < 0) else ""
    return f"{sign}{abs(feet)} ft {abs(inches)} in"


def np_repeat_stereo(values):
    array = np.asarray(values)
    if array.ndim == 1:
        array = array.reshape(-1, 1)
    return np.repeat(array[:, :1], 2, axis=1)


def np_clip(value, low, high):
    return max(low, min(high, value))


def fader_to_gain(position):
    position = max(0.0, min(1.0, float(position)))
    db = (position - 1.0) * 60.0
    return 10.0 ** (db / 20.0)


def resample_to_rate(data, src_rate, dst_rate):
    data = np.asarray(data, dtype=np.float32)
    if src_rate == dst_rate or src_rate <= 0:
        return data
    from scipy.signal import resample_poly
    div = math.gcd(int(src_rate), int(dst_rate))
    return resample_poly(
        data,
        int(dst_rate) // div,
        int(src_rate) // div,
        axis=0,
    ).astype(np.float32, copy=False)


def load_config():
    defaults = {
        "port": "COM3",
        "baud": 250000,
        "break_us": 100,
        "bindings": [],
        "values": [0] * CHANNELS,
        "channel_names": [""] * CHANNELS,
        "steps_per_second": 10.0,
        "step_size": 1,
        "deadzone": 12,
        "script_notes": "",
        "volume_percent": 100,
        "mic_input_device": "",
        "music_view_mode": "list",
        "music_font_size": 13,
        "piano_surface": "wall",
        "piano_fixture_height_ft": 1.0,
        "piano_wall_distance_ft": 10.0,
        "piano_ceiling_height_ft": 9.0,
        "piano_forward_distance_ft": 6.0,
        "piano_strip_length_ft": 20.0,
        "piano_left_offset_ft": 0.0,
        "piano_forward_back_offset_ft": 0.0,
        "piano_aim_mode": "degrees",
        "piano_tilt_deg": 0.0,
        "piano_target_height_ft": 5.0,
        "piano_pan_channel": 1,
        "piano_tilt_channel": 2,
        "piano_dimmer_channel": 7,
        "piano_pan_min_deg": -270.0,
        "piano_pan_max_deg": 270.0,
        "piano_tilt_min_deg": -135.0,
        "piano_tilt_max_deg": 135.0,
        "piano_flash_mode": "flash",
        "piano_attack_ms": 20,
        "piano_hold_decay_ms": 2000,
        "piano_release_decay_ms": 200,
        "piano_num_keys": 88,
        "piano_start_note_midi": 21,
        "piano_midi_port": "",
        "piano_soundfont": "",
        "crossfader": 500,
        "fixtures": [],
        **SENSITIVITY_DEFAULTS
    }

    try:
        saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            defaults.update(saved)
    except (OSError, ValueError, TypeError):
        pass

    defaults["values"] = (list(defaults.get("values", [])) + [0] * CHANNELS)[:CHANNELS]
    defaults["channel_names"] = (list(defaults.get("channel_names", [])) + [""] * CHANNELS)[:CHANNELS]

    if not isinstance(defaults.get("bindings"), list):
        defaults["bindings"] = []

    for binding in defaults["bindings"]:
        binding.setdefault("device", "Keyboard")
        binding.setdefault("control", binding.get("key", ""))
        binding.setdefault("key", binding.get("control", ""))
        try:
            binding["mode"] = max(0, min(len(ACTIONS) - 1, int(binding.get("mode", 0))))
        except (TypeError, ValueError):
            binding["mode"] = 0
        if binding["mode"] == 18 or binding["mode"] == 19:
            binding["mode"] = 18
        if not isinstance(binding.get("sound_path", ""), str):
            binding["sound_path"] = ""
        try:
            binding["channel"] = max(1, min(CHANNELS, int(binding.get("channel", 1))))
        except (TypeError, ValueError):
            binding["channel"] = 1
        try:
            binding["value"] = max(0, min(255, int(binding.get("value", 255))))
        except (TypeError, ValueError):
            binding["value"] = 255
        try:
            binding["amount"] = max(1, min(255, int(binding.get("amount", 10))))
        except (TypeError, ValueError):
            binding["amount"] = 10

        words = binding.get("voice_words", [])
        if not isinstance(words, list):
            words = []
        cleaned = []
        for w in words:
            if isinstance(w, str):
                w = w.strip().lower()
                if w:
                    cleaned.append(w)
        binding["voice_words"] = cleaned

    for key in SENSITIVITY_DEFAULTS:
        try:
            defaults[key] = max(1, min(1000000000, int(defaults[key])))
        except (TypeError, ValueError, OverflowError):
            defaults[key] = 100

    try:
        defaults["volume_percent"] = max(0, min(100, int(defaults.get("volume_percent", 100))))
    except (TypeError, ValueError):
        defaults["volume_percent"] = 100

    try:
        defaults["crossfader"] = max(0, min(1000, int(defaults.get("crossfader", 500))))
    except (TypeError, ValueError):
        defaults["crossfader"] = 500

    if not isinstance(defaults.get("fixtures"), list):
        defaults["fixtures"] = []
    cleaned_fixtures = []
    for i, fx in enumerate(defaults["fixtures"]):
        if not isinstance(fx, dict):
            continue
        fx.setdefault("name", f"Fixture {i + 1}")
        fx.setdefault("enabled", True)
        try:
            fx["channel_offset"] = max(0, min(CHANNELS - 1, int(fx.get("channel_offset", 0))))
        except (TypeError, ValueError):
            fx["channel_offset"] = 0
        for ch_key, ch_default in (
            ("pan_channel", 1), ("tilt_channel", 2), ("dimmer_channel", 7),
            ("color_channel", 8), ("strobe_channel", 6),
        ):
            try:
                fx[ch_key] = max(0, min(CHANNELS, int(fx.get(ch_key, ch_default))))
            except (TypeError, ValueError):
                fx[ch_key] = ch_default
        try:
            fx["phase_offset"] = float(fx.get("phase_offset", 0.0))
        except (TypeError, ValueError):
            fx["phase_offset"] = 0.0
        cleaned_fixtures.append(fx)
    defaults["fixtures"] = cleaned_fixtures

    if not isinstance(defaults.get("script_notes"), str):
        defaults["script_notes"] = ""
    if not isinstance(defaults.get("mic_input_device", ""), str):
        defaults["mic_input_device"] = ""
    if defaults.get("music_view_mode") not in ("list", "grid_large", "grid_medium", "grid_small"):
        defaults["music_view_mode"] = "list"
    try:
        defaults["music_font_size"] = max(8, min(48, int(defaults.get("music_font_size", 13))))
    except (TypeError, ValueError):
        defaults["music_font_size"] = 13

    if defaults.get("piano_surface") not in ("wall", "ceiling"):
        defaults["piano_surface"] = "wall"
    if defaults.get("piano_aim_mode") not in ("degrees", "height"):
        defaults["piano_aim_mode"] = "degrees"
    if defaults.get("piano_flash_mode") not in ("flash", "fade"):
        defaults["piano_flash_mode"] = "flash"

    for key in (
        "piano_fixture_height_ft", "piano_wall_distance_ft", "piano_ceiling_height_ft",
        "piano_forward_distance_ft", "piano_strip_length_ft",
        "piano_left_offset_ft", "piano_forward_back_offset_ft", "piano_target_height_ft",
        "piano_pan_min_deg", "piano_pan_max_deg", "piano_tilt_min_deg", "piano_tilt_max_deg",
        "piano_tilt_deg",
    ):
        try:
            defaults[key] = float(defaults[key])
        except (TypeError, ValueError):
            pass

    for key in (
        "piano_pan_channel", "piano_tilt_channel", "piano_dimmer_channel",
        "piano_attack_ms", "piano_hold_decay_ms", "piano_release_decay_ms",
        "piano_num_keys", "piano_start_note_midi",
    ):
        try:
            defaults[key] = int(defaults[key])
        except (TypeError, ValueError):
            pass

    defaults["piano_num_keys"] = max(1, min(200, int(defaults.get("piano_num_keys", 88))))
    defaults["piano_start_note_midi"] = max(0, min(127, int(defaults.get("piano_start_note_midi", 21))))
    for key in ("piano_pan_channel", "piano_tilt_channel", "piano_dimmer_channel"):
        defaults[key] = max(0, min(CHANNELS, int(defaults.get(key, 1))))
    for key in ("piano_attack_ms", "piano_hold_decay_ms", "piano_release_decay_ms"):
        defaults[key] = max(1, min(60000, int(defaults.get(key, 20))))

    if not isinstance(defaults.get("piano_midi_port", ""), str):
        defaults["piano_midi_port"] = ""
    if not isinstance(defaults.get("piano_soundfont", ""), str):
        defaults["piano_soundfont"] = ""

    return defaults


def save_config(config):
    CONFIG_FILE.write_text(json.dumps(config, indent=2), encoding="utf-8")


def load_playlist():
    if not PLAYLIST_FILE.is_file():
        return []
    try:
        saved = json.loads(PLAYLIST_FILE.read_text(encoding="utf-8"))
        if isinstance(saved, list):
            return saved
    except (OSError, ValueError, TypeError):
        pass
    return []


def save_playlist(entries):
    PLAYLIST_FILE.write_text(json.dumps(entries, indent=2), encoding="utf-8")


class DeckSource:
    def __init__(self):
        self.lock = threading.Lock()
        self.audio = None
        self.keylock_audio = None
        self.stems = {}
        self.keylock_stems = {}
        self.stem_mutes = {}
        self.sample_rate = DECK_RATE
        self.frames_total = 0
        self.frames_played = 0.0
        self.playing = False
        self.gain = 1.0
        self.crossfade_gain = 1.0
        self.keylock = False
        self.stem_mix_ratio = 0.0
        self.stem_xfade_remaining = 0
        self.stem_xfade_from = 0.0
        self.pitch_factor = 1.0
        self.entry = None
        self.load_time = 0.0
        self._prev_tail = None

    def load(self, audio, stems, sample_rate, entry, keylock=False):
        audio = np_repeat_stereo(audio)
        prepared_stems = {}
        for k, v in (stems or {}).items():
            prepared_stems[k] = np_repeat_stereo(v)
        with self.lock:
            self.audio = audio.astype(np.float32, copy=False)
            self.keylock_audio = None
            self.stems = {k: v.astype(np.float32, copy=False) for k, v in prepared_stems.items()}
            self.keylock_stems = {}
            self.stem_mutes = {k: False for k in self.stems}
            self.sample_rate = int(sample_rate) or DECK_RATE
            self.frames_total = self.audio.shape[0]
            self.frames_played = 0.0
            self.playing = False
            self.stem_mix_ratio = 0.0
            self.stem_xfade_remaining = 0
            self.stem_xfade_from = 0.0
            self.entry = dict(entry) if entry else None
            self.keylock = bool(keylock)
            self.load_time = time.monotonic()

    def clear(self):
        with self.lock:
            self.audio = None
            self.keylock_audio = None
            self.stems = {}
            self.keylock_stems = {}
            self.stem_mutes = {}
            self.frames_total = 0
            self.frames_played = 0.0
            self.playing = False
            self.entry = None
            self.stem_mix_ratio = 0.0
            self.stem_xfade_remaining = 0
            self.stem_xfade_from = 0.0
            self.load_time = 0.0

    def set_stem_mute(self, key, muted):
        with self.lock:
            self.stem_mutes[key] = bool(muted)
            target = 1.0 if any(self.stem_mutes.values()) else 0.0
            if abs(target - self.stem_mix_ratio) > 1e-4:
                self.stem_xfade_from = self.stem_mix_ratio
                self.stem_xfade_remaining = max(
                    self.stem_xfade_remaining,
                    int(self.sample_rate * 0.02),
                )

    def duration(self):
        with self.lock:
            if self.sample_rate <= 0:
                return 0.0
            return self.frames_total / float(self.sample_rate)

    def position(self):
        with self.lock:
            if self.sample_rate <= 0:
                return 0.0
            return self.frames_played / float(self.sample_rate)

    def seek_seconds(self, seconds):
        with self.lock:
            if self.frames_total <= 0:
                return
            self.frames_played = float(
                max(0, min(self.frames_total, seconds * self.sample_rate))
            )

    def set_playing(self, value):
        with self.lock:
            self.playing = bool(value)

    def toggle_playing(self):
        with self.lock:
            self.playing = not self.playing

    def is_playing(self):
        with self.lock:
            return self.playing

    def set_gain(self, value):
        self.gain = float(max(0.0, value))

    def set_crossfade_gain(self, value):
        self.crossfade_gain = float(max(0.0, min(1.0, value)))

    def set_pitch_factor(self, factor):
        self.pitch_factor = max(0.5, min(2.0, float(factor)))

    def set_keylock(self, enabled):
        enabled = bool(enabled)
        with self.lock:
            if enabled == self.keylock:
                return
            if self.audio is None:
                self.keylock = enabled
                return
            if enabled:
                try:
                    import librosa
                    factor = self.pitch_factor if self.pitch_factor > 0 else 1.0
                    if abs(factor - 1.0) > 0.001:
                        stretched = librosa.effects.time_stretch(
                            self.audio.T, rate=factor
                        ).T.astype(np.float32, copy=False)
                    else:
                        stretched = self.audio
                    self.keylock_audio = stretched
                    stretched_stems = {}
                    for k, v in self.stems.items():
                        if abs(factor - 1.0) > 0.001:
                            stretched_stems[k] = librosa.effects.time_stretch(
                                v.T, rate=factor
                            ).T.astype(np.float32, copy=False)
                        else:
                            stretched_stems[k] = v
                    self.keylock_stems = stretched_stems
                    self.keylock = True
                except Exception:
                    self.keylock = False
            else:
                self.keylock = False
                self.keylock_audio = None
                self.keylock_stems = {}

    def _read_fractional(self, data, start, end):
        n_out = int(round(end - start))
        if n_out <= 0 or data is None:
            return None
        i0 = int(math.floor(start))
        i1 = int(math.floor(end))
        if i0 < 0:
            i0 = 0
        if i1 >= data.shape[0]:
            i1 = data.shape[0] - 1
        if i1 <= i0:
            return np.zeros((n_out, 2), dtype=np.float32)
        seg = data[i0:i1 + 1]
        t = np.linspace(start - i0, end - i0, n_out, dtype=np.float32)
        i_lo = np.floor(t).astype(np.int64)
        frac = (t - i_lo).astype(np.float32)[:, None]
        i_lo = np.clip(i_lo, 0, seg.shape[0] - 1)
        i_hi = np.clip(i_lo + 1, 0, seg.shape[0] - 1)
        return (seg[i_lo] * (1.0 - frac) + seg[i_hi] * frac).astype(np.float32, copy=False)

    def mix_into(self, out, frames):
        with self.lock:
            if self.audio is None:
                return
            if not self.playing:
                return
            src = self.keylock_audio if (self.keylock and self.keylock_audio is not None) else self.audio
            if src is None:
                return

            stream_rate = AUDIO_ENGINE.sample_rate or self.sample_rate
            if stream_rate <= 0:
                return

            deck_per_stream = (self.sample_rate / float(stream_rate))
            pitch = 1.0 if (self.keylock and self.keylock_audio is not None) else max(0.1, self.pitch_factor)
            advance = deck_per_stream * pitch

            pos = self.frames_played
            total_src = float(self.frames_total if self.keylock_audio is None else src.shape[0])
            advance_float = frames * advance
            end = min(total_src, pos + advance_float)

            if end <= pos + 0.5:
                if pos >= total_src - 0.5:
                    self.playing = False
                return

            if abs(advance - 1.0) < 1e-9:
                chunk = self._read_fractional(src, pos, pos + frames)
                if chunk is None:
                    return
                n = min(frames, chunk.shape[0])
                if n <= 0:
                    return
                resampled = chunk[:n]
            else:
                out_positions = pos + np.arange(frames, dtype=np.float64) * advance
                out_positions = np.clip(out_positions, 0.0, total_src - 1.0)
                i_lo = np.floor(out_positions).astype(np.int64)
                frac = (out_positions - i_lo).astype(np.float32)[:, None]
                i_hi = np.clip(i_lo + 1, 0, src.shape[0] - 1)
                i_lo = np.clip(i_lo, 0, src.shape[0] - 1)
                resampled = (src[i_lo] * (1.0 - frac) + src[i_hi] * frac).astype(np.float32)
                n = resampled.shape[0]

            stem_chunk = None
            if self.stem_xfade_remaining > 0 or self.stem_mix_ratio > 0.001:
                stem_src = self.keylock_stems if (self.keylock and self.keylock_stems) else self.stems
                out_positions = pos + np.arange(n, dtype=np.float64) * advance
                out_positions = np.clip(out_positions, 0.0, total_src - 1.0)
                i_lo_s = np.floor(out_positions).astype(np.int64)
                frac_s = (out_positions - i_lo_s).astype(np.float32)[:, None]
                stem_mix = np.zeros((n, 2), dtype=np.float32)
                for key, arr in stem_src.items():
                    if self.stem_mutes.get(key, False):
                        continue
                    if arr.shape[0] <= 0:
                        continue
                    lo = np.clip(i_lo_s, 0, arr.shape[0] - 1)
                    hi = np.clip(lo + 1, 0, arr.shape[0] - 1)
                    stem_mix += arr[lo] * (1.0 - frac_s) + arr[hi] * frac_s
                np.clip(stem_mix, -1.0, 1.0, out=stem_mix)
                stem_chunk = stem_mix

            if stem_chunk is not None:
                if self.stem_xfade_remaining > 0:
                    target = 1.0 if any(self.stem_mutes.values()) else 0.0
                    xf = min(self.stem_xfade_remaining, n)
                    t = np.linspace(0.0, 1.0, xf, dtype=np.float32)[:, None]
                    begin = self.stem_xfade_from
                    blended = begin * (1.0 - t) + target * t
                    head = resampled[:xf] * (1.0 - blended) + stem_chunk[:xf] * blended
                    out[:xf] += head * self.gain * self.crossfade_gain
                    if xf < n:
                        ratio = target
                        out[xf:n] += (
                            resampled[xf:n] * (1.0 - ratio)
                            + stem_chunk[xf:n] * ratio
                        ) * self.gain * self.crossfade_gain
                    self.stem_mix_ratio = target
                    self.stem_xfade_remaining = max(0, self.stem_xfade_remaining - xf)
                else:
                    ratio = self.stem_mix_ratio
                    out[:n] += (
                        resampled[:n] * (1.0 - ratio) + stem_chunk[:n] * ratio
                    ) * self.gain * self.crossfade_gain
            else:
                out[:n] += resampled[:n] * self.gain * self.crossfade_gain

            self.frames_played = min(pos + frames * advance, total_src)
            if self.frames_played >= total_src - 0.5:
                self.frames_played = float(total_src)
                self.playing = False


class AudioEngine:
    CROSSFADE_MS = 20

    def __init__(self):
        self.stream = None
        self.lock = threading.Lock()

        self.sample_rate = 0
        self.frames_total = 0
        self.frames_played = 0
        self.playing = False
        self.paused = False

        self.original = None
        self.stems = {}
        self.stem_mutes = {}
        self.use_stems = False
        self.crossfade_remaining = 0
        self.crossfade_from_stems = False

        self.master_gain = 1.0

        self.fade_current = 1.0
        self.fade_target = 1.0
        self.fade_rate_per_second = 1.0 / FADE_IN_SECONDS

        self.mic_enabled = False
        self.mic_lock = threading.Lock()
        self.mic_buffer = np.zeros((0, 2), dtype=np.float32)
        self.mic_gain = 1.0
        self.mic_listeners = []

        self.effects = []
        self.effects_lock = threading.Lock()

        self.deck_a = DeckSource()
        self.deck_b = DeckSource()

        self._ensure_stream()

    def _ensure_stream(self):
        if self.stream is not None:
            return
        self.stream = sd.OutputStream(
            samplerate=PIANO_SAMPLE_RATE,
            channels=2,
            dtype="float32",
            blocksize=0,
            callback=self._callback,
        )
        self.sample_rate = PIANO_SAMPLE_RATE
        self.stream.start()

    def open(self, sample_rate, channels=2):
        sample_rate = int(sample_rate)
        if sample_rate <= 0:
            raise RuntimeError("Invalid sample rate.")
        if self.stream is not None and self.sample_rate == sample_rate:
            return
        self.close()
        self.stream = sd.OutputStream(
            samplerate=sample_rate,
            channels=channels,
            dtype="float32",
            blocksize=0,
            callback=self._callback,
        )
        self.sample_rate = sample_rate
        self.stream.start()

    def close(self):
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
        self.stream = None
        self.sample_rate = 0

    def load(self, original, stems, sample_rate, mutes):
        self.open(sample_rate, channels=2)
        sample_rate = int(sample_rate)
        arrays = []
        if original is not None:
            original = np_repeat_stereo(original)
            arrays.append(original.shape[0])
        prepared_stems = {}
        for key, arr in (stems or {}).items():
            arr = np_repeat_stereo(arr)
            prepared_stems[key] = arr
            arrays.append(arr.shape[0])
        if not arrays:
            return
        target_len = min(arrays)

        def align(arr):
            if arr.shape[0] >= target_len:
                return arr[:target_len].copy()
            pad = np.zeros((target_len - arr.shape[0], arr.shape[1]), dtype=np.float32)
            return np.vstack([arr, pad])

        if original is not None:
            original = align(original)
        prepared_stems = {k: align(v) for k, v in prepared_stems.items()}

        with self.lock:
            self.original = original
            self.stems = prepared_stems
            self.stem_mutes = dict(mutes or {})
            self.frames_total = target_len
            self.frames_played = 0
            self.playing = True
            self.paused = False
            self.crossfade_remaining = 0
            self.fade_current = 1.0
            self.fade_target = 1.0
            self.use_stems = any(self.stem_mutes.get(k, False) for k in self.stems)

    def set_stem_mute(self, key, muted):
        with self.lock:
            self.stem_mutes[key] = bool(muted)
            should_use_stems = any(self.stem_mutes.get(k, False) for k in self.stems)
            if should_use_stems != self.use_stems:
                self.crossfade_from_stems = self.use_stems
                self.use_stems = should_use_stems
                self.crossfade_remaining = max(
                    self.crossfade_remaining,
                    int(self.sample_rate * (self.CROSSFADE_MS / 1000.0)),
                )

    def set_master_gain(self, gain):
        self.master_gain = float(max(0.0, min(1.0, gain)))

    def begin_fade_out(self):
        with self.lock:
            self.fade_target = 0.0
            self.fade_rate_per_second = 1.0 / max(0.05, FADE_OUT_SECONDS)

    def begin_fade_in(self):
        with self.lock:
            self.fade_target = 1.0
            self.fade_rate_per_second = 1.0 / max(0.05, FADE_IN_SECONDS)

    def is_faded_out(self):
        with self.lock:
            return self.fade_current <= 0.0001 and self.fade_target <= 0.0001

    def seek(self, frame_index):
        with self.lock:
            self.frames_played = max(0, min(self.frames_total, int(frame_index)))
            self.paused = False
            self.crossfade_remaining = 0

    def current_frame(self):
        with self.lock:
            return int(self.frames_played)

    def current_time(self):
        rate = self.sample_rate or 1
        return self.current_frame() / float(rate)

    def is_playing(self):
        with self.lock:
            return self.playing and not self.paused

    def stop(self):
        with self.lock:
            self.playing = False
            self.paused = False
            self.original = None
            self.stems = {}
            self.use_stems = False
            self.frames_played = 0
            self.frames_total = 0
            self.crossfade_remaining = 0
            self.fade_current = 1.0
            self.fade_target = 1.0

    def set_mic_enabled(self, enabled):
        self.mic_enabled = bool(enabled)
        if not enabled:
            with self.mic_lock:
                self.mic_buffer = np.zeros((0, 2), dtype=np.float32)

    def push_mic_frames(self, frames):
        if frames is None or len(frames) == 0:
            return
        frames = np.asarray(frames, dtype=np.float32).reshape(-1)
        for listener in list(self.mic_listeners):
            try:
                listener(frames.copy())
            except Exception:
                pass
        if not self.mic_enabled:
            return
        target_rate = self.sample_rate or MIC_SAMPLE_RATE
        if target_rate != MIC_SAMPLE_RATE:
            from scipy.signal import resample_poly
            divisor = math.gcd(MIC_SAMPLE_RATE, target_rate)
            frames = resample_poly(
                frames, target_rate // divisor, MIC_SAMPLE_RATE // divisor
            ).astype(np.float32, copy=False)
        stereo = np.repeat(frames.reshape(-1, 1), 2, axis=1)
        with self.mic_lock:
            self.mic_buffer = np.vstack([self.mic_buffer, stereo])

    def add_mic_listener(self, fn):
        if fn not in self.mic_listeners:
            self.mic_listeners.append(fn)

    def remove_mic_listener(self, fn):
        if fn in self.mic_listeners:
            self.mic_listeners.remove(fn)

    def play_effect(self, array, sample_rate, loop=False, volume=1.0):
        array = np_repeat_stereo(array)
        if self.stream is not None and sample_rate != self.sample_rate:
            from scipy.signal import resample_poly
            divisor = math.gcd(int(sample_rate), int(self.sample_rate))
            array = resample_poly(
                array,
                self.sample_rate // divisor,
                sample_rate // divisor,
                axis=0,
            ).astype(np.float32, copy=False)
        with self.effects_lock:
            self.effects.append({
                "array": array,
                "position": 0,
                "loop": bool(loop),
                "volume": float(volume),
            })

    def stop_all_effects(self):
        with self.effects_lock:
            self.effects = []

    def _callback(self, outdata, frames, time_info, status):
        with self.lock:
            playing = self.playing and not self.paused
            start = self.frames_played
            total = self.frames_total
            original = self.original
            stems = dict(self.stems)
            mutes = dict(self.stem_mutes)
            use_stems = self.use_stems
            crossfade_remaining = self.crossfade_remaining
            crossfade_from_stems = self.crossfade_from_stems
            gain = self.master_gain
            fade_current = self.fade_current
            fade_target = self.fade_target
            fade_rate = self.fade_rate_per_second
            rate = self.sample_rate or 1

        out = np.zeros((frames, 2), dtype=np.float32)

        if fade_current != fade_target:
            step = fade_rate * (frames / float(rate))
            if fade_target < fade_current:
                fade_current = max(fade_target, fade_current - step)
            else:
                fade_current = min(fade_target, fade_current + step)

        if playing:
            mix = np.zeros((frames, 2), dtype=np.float32)
            stem_mix = np.zeros((frames, 2), dtype=np.float32)

            if original is not None:
                end = min(total, start + frames)
                count = max(0, end - start)
                if count:
                    mix[:count] = original[start:end]

            for key, arr in stems.items():
                if mutes.get(key, False):
                    continue
                end = min(arr.shape[0], start + frames)
                count = max(0, end - start)
                if count:
                    stem_mix[:count] += arr[start:end]
            np.clip(stem_mix, -1.0, 1.0, out=stem_mix)

            if crossfade_remaining > 0:
                n = min(crossfade_remaining, frames)
                t = np.linspace(0.0, 1.0, n, dtype=np.float32)[:, None]
                if crossfade_from_stems:
                    out[:n] = stem_mix[:n] * (1.0 - t) + mix[:n] * t
                else:
                    out[:n] = mix[:n] * (1.0 - t) + stem_mix[:n] * t
                if n < frames:
                    out[n:] = stem_mix[n:] if use_stems else mix[n:]
                with self.lock:
                    self.crossfade_remaining = max(0, crossfade_remaining - n)
            else:
                out[:] = stem_mix if use_stems else mix

            if fade_current < 1.0:
                out *= fade_current

            with self.lock:
                self.frames_played = min(total, start + frames)
                self.fade_current = fade_current
                if self.frames_played >= total:
                    self.playing = False
        else:
            with self.lock:
                self.fade_current = fade_current

        try:
            self.deck_a.mix_into(out, frames)
        except Exception:
            pass
        try:
            self.deck_b.mix_into(out, frames)
        except Exception:
            pass
        out *= self.master_gain
        np.clip(out, -1.0, 1.0, out=out)

        if self.mic_enabled:
            with self.mic_lock:
                if self.mic_buffer.shape[0] > 0:
                    take = min(frames, self.mic_buffer.shape[0])
                    out[:take] += self.mic_buffer[:take] * self.mic_gain
                    self.mic_buffer = self.mic_buffer[take:]
            np.clip(out, -1.0, 1.0, out=out)

        with self.effects_lock:
            if self.effects:
                for effect in list(self.effects):
                    arr = effect["array"]
                    pos = effect["position"]
                    volume = effect["volume"]
                    remaining = frames
                    out_pos = 0
                    while remaining > 0 and arr.shape[0] > 0:
                        if pos >= arr.shape[0]:
                            if effect["loop"]:
                                pos = 0
                            else:
                                break
                        take = min(remaining, arr.shape[0] - pos)
                        out[out_pos:out_pos + take] += arr[pos:pos + take] * volume
                        pos += take
                        out_pos += take
                        remaining -= take
                    effect["position"] = pos
                    if not effect["loop"] and pos >= arr.shape[0]:
                        try:
                            self.effects.remove(effect)
                        except ValueError:
                            pass
                np.clip(out, -1.0, 1.0, out=out)

        outdata[:] = out


AUDIO_ENGINE = AudioEngine()


class MicrophoneInput:
    def __init__(self):
        self.stream = None
        self.device = None

    def start(self, device_name=""):
        if self.stream is not None:
            return
        try:
            device = None
            if device_name:
                for idx, info in enumerate(sd.query_devices()):
                    if info.get("max_input_channels", 0) > 0 and device_name.lower() in info["name"].lower():
                        device = idx
                        break
            self.device = device
            self.stream = sd.InputStream(
                samplerate=MIC_SAMPLE_RATE,
                channels=1,
                dtype="float32",
                blocksize=0,
                device=self.device,
                callback=self._callback,
            )
            self.stream.start()
        except Exception as error:
            self.stream = None
            raise RuntimeError(f"Microphone could not be opened: {error}")

    def stop(self):
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
        self.stream = None

    def _callback(self, indata, frames, time_info, status):
        mono = np.mean(indata, axis=1).astype(np.float32)
        AUDIO_ENGINE.push_mic_frames(mono)


MIC_INPUT = MicrophoneInput()


class VoiceCommandWorker(QThread):
    word_detected = Signal(str)
    status = Signal(str)

    def __init__(self, model_path, parent=None):
        super().__init__(parent)
        self.model_path = str(model_path)
        self._queue = queue.Queue()
        self._stop = False
        self._paused = True

    def feed(self, frames):
        try:
            self._queue.put_nowait(frames)
        except queue.Full:
            pass

    def set_paused(self, paused):
        self._paused = bool(paused)

    def run(self):
        try:
            if not self.model_path or not Path(self.model_path).is_dir():
                raise RuntimeError(
                    f"Vosk model not found at {self.model_path}. "
                    "Download a model from https://alphacephei.com/vosk/models "
                    "and unzip it next to this script."
                )
            try:
                import vosk
            except ImportError:
                raise RuntimeError(
                    "Voice command requires the vosk package. "
                    "Install it with: py -m pip install vosk"
                )
            vosk.SetLogLevel(-1)
            model = vosk.Model(self.model_path)
            rec = vosk.KaldiRecognizer(model, MIC_SAMPLE_RATE)
            rec.SetWords(True)
            self.status.emit("Voice command ready.")
            AUDIO_ENGINE.add_mic_listener(self.feed)

            while not self._stop:
                try:
                    frames = self._queue.get(timeout=0.2)
                except queue.Empty:
                    continue
                if self._paused:
                    continue
                try:
                    pcm = (frames * 32767.0).astype("<i2", copy=False)
                    if rec.AcceptWaveform(pcm.tobytes()):
                        result = json.loads(rec.Result())
                        text = (result.get("text") or "").strip().lower()
                        if text:
                            self.word_detected.emit(text)
                    else:
                        partial = json.loads(rec.PartialResult()).get("partial", "").strip().lower()
                        if partial:
                            self.word_detected.emit(partial)
                except Exception as error:
                    self.status.emit(f"Voice recognition error: {error}")

            AUDIO_ENGINE.remove_mic_listener(self.feed)
            self.status.emit("Voice command stopped.")
        except Exception as error:
            AUDIO_ENGINE.remove_mic_listener(self.feed)
            self.status.emit(f"Voice command error: {error}")

    def stop(self):
        self._stop = True


class PianoSynth:
    def __init__(self):
        self.active = {}
        self.lock = threading.Lock()
        self.master_gain = 0.35
        self.soundfont_path = ""

    def set_soundfont(self, path):
        self.soundfont_path = str(path) if path else ""

    def _render_note(self, midi_note, velocity, sample_rate):
        freq = midi_note_frequency(midi_note)
        duration = 2.6
        n = int(sample_rate * duration)
        if n <= 0:
            return np.zeros((0, 2), dtype=np.float32)
        t = np.arange(n, dtype=np.float32) / sample_rate
        harmonics = (
            (1, 1.00, 2.2),
            (2, 0.45, 2.8),
            (3, 0.28, 3.2),
            (4, 0.15, 3.8),
            (5, 0.09, 4.4),
            (6, 0.06, 5.0),
            (8, 0.04, 5.5),
        )
        wave = np.zeros(n, dtype=np.float32)
        for multiple, amp, decay in harmonics:
            f = freq * multiple
            if f >= sample_rate / 2.0:
                continue
            wave += amp * np.sin(2.0 * math.pi * f * t) * np.exp(-decay * t)
        attack = np.minimum(1.0, t / 0.01)
        wave *= attack * self.master_gain * max(0.15, min(1.0, velocity / 127.0))
        tail_env = np.exp(-2.4 * t)
        wave *= 0.5 + 0.5 * tail_env
        right = np.roll(wave, 1) * 0.985
        stereo = np.stack([wave, right], axis=1).astype(np.float32)
        peak = float(np.max(np.abs(stereo)))
        if peak > 1.0:
            stereo /= peak
        return stereo

    def note_on(self, midi_note, velocity=100, sample_rate=None):
        if sample_rate is None:
            sample_rate = PIANO_SAMPLE_RATE
        try:
            buf = self._render_note(midi_note, velocity, sample_rate)
        except Exception:
            return
        if buf.size == 0:
            return
        AUDIO_ENGINE.play_effect(buf, sample_rate, loop=False, volume=1.0)
        with self.lock:
            self.active[int(midi_note)] = buf

    def note_off(self, midi_note):
        with self.lock:
            self.active.pop(int(midi_note), None)

    def all_off(self):
        with self.lock:
            self.active.clear()


PIANO_SYNTH = PianoSynth()


def detect_key_from_audio(y, sr):
    try:
        import librosa
        chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
        chroma_mean = np.mean(chroma, axis=1)
        if np.max(chroma_mean) <= 0:
            return None
        major_profile = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09,
                                  2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
        minor_profile = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53,
                                  2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
        best_score = -2.0
        best_pc = 0
        best_minor = False
        for pc in range(12):
            rotated = np.roll(chroma_mean, -pc)
            for profile, is_minor in ((major_profile, False), (minor_profile, True)):
                a = rotated - np.mean(rotated)
                b = profile - np.mean(profile)
                denom = np.linalg.norm(a) * np.linalg.norm(b)
                if denom <= 1e-9:
                    continue
                corr = float(np.dot(a, b) / denom)
                if corr > best_score:
                    best_score = corr
                    best_pc = pc
                    best_minor = is_minor
        return best_pc, best_minor, key_to_string(best_pc, best_minor), key_to_camelot(best_pc, best_minor)
    except Exception:
        return None


class SongAnalysisWorker(QThread):
    completed = Signal(object)
    failed = Signal(str)
    status = Signal(str)

    def __init__(self, filename, parent=None):
        super().__init__(parent)
        self.filename = filename

    def run(self):
        try:
            import librosa
            from scipy.signal import find_peaks
            self.status.emit("Decoding the complete song...")
            y, sr = librosa.load(self.filename, sr=22050, mono=True)
            if y.size < sr * 3:
                raise RuntimeError("The selected song is too short.")
            y = np.nan_to_num(y.astype(np.float32))
            duration = len(y) / sr
            drum_y = y
            stem_signals = {}
            stem_files = {}
            stem_availability = {"drums": False, "bass": False, "vocals": False, "other": False}
            stem_separation = "Full mix fallback"
            stem_error = ""
            self.status.emit("Separating drums, bass, vocals and other instruments with Demucs...")
            try:
                with tempfile.TemporaryDirectory(prefix="dmx_demucs_") as stem_dir:
                    command = [sys.executable, "-m", "demucs", "-n", "htdemucs_ft", "--out", stem_dir, self.filename]
                    completed = subprocess.run(command, capture_output=True, text=True, check=True)
                    found = {}
                    for stem_path in Path(stem_dir).rglob("*.wav"):
                        key = stem_path.stem.lower()
                        if key in ("drums", "bass", "vocals", "other"):
                            found[key] = stem_path
                    playback_dir = Path(tempfile.mkdtemp(prefix="dmx_stem_playback_"))
                    for key, stem_path in found.items():
                        playback_path = playback_dir / f"{key}.wav"
                        shutil.copy2(stem_path, playback_path)
                        stem_files[key] = str(playback_path)
                        stem_audio, _ = librosa.load(str(stem_path), sr=sr, mono=True)
                        if stem_audio.size >= sr:
                            stem_signals[key] = np.nan_to_num(stem_audio.astype(np.float32))
                            stem_availability[key] = True
                    if all(stem_availability.values()):
                        stem_separation = "Demucs htdemucs_ft (4 isolated stems)"
                    elif stem_signals:
                        stem_separation = "Demucs htdemucs_ft (partial stems)"
                        missing = [k for k, available in stem_availability.items() if not available]
                        stem_error = "Demucs did not produce: " + ", ".join(missing)
                    else:
                        raise RuntimeError("Demucs ran but no stems were found.")
                    drum_y = stem_signals.get("drums", y)
            except Exception as separation_exception:
                stem_separation = "Full mix fallback"
                if isinstance(separation_exception, subprocess.CalledProcessError):
                    details = (separation_exception.stderr or separation_exception.stdout or "").strip()
                    stem_error = details[-1400:] or str(separation_exception)
                else:
                    stem_error = str(separation_exception).strip() or separation_exception.__class__.__name__
                self.status.emit("Demucs separation failed; continuing with full-mix analysis. " + stem_error[-900:])
            hop = 1102
            frame_size = 4096
            frame_times = np.arange(0, duration, hop / sr, dtype=np.float32)
            self.status.emit("Tracking the beat grid and musical phrases...")
            onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=512, aggregate=np.median)
            tempo, beat_times = librosa.beat.beat_track(
                onset_envelope=onset_env, sr=sr, hop_length=512, units="time", trim=False)
            tempo_value = float(np.asarray(tempo).reshape(-1)[0]) if np.size(tempo) else 0.0
            beat_times = np.asarray(beat_times, dtype=np.float64)
            beat_times = beat_times[(beat_times >= 0) & (beat_times < duration)]
            if len(beat_times) >= 3:
                intervals = np.diff(beat_times)
                valid_intervals = intervals[(intervals >= 0.25) & (intervals <= 1.5)]
                if len(valid_intervals):
                    measured = 60.0 / float(np.median(valid_intervals))
                    if 55 <= measured <= 210:
                        tempo_value = measured
            if tempo_value <= 0:
                tempo_value = 120.0
            beat_period = 60.0 / tempo_value
            if len(beat_times) >= 2:
                valid = np.diff(beat_times)
                valid = valid[(valid >= 0.25) & (valid <= 1.5)]
                local_period = float(np.median(valid)) if len(valid) else beat_period
                repaired = [float(beat_times[0])]
                for next_time in beat_times[1:]:
                    gap = float(next_time - repaired[-1])
                    if gap > local_period * 1.55:
                        missing = max(1, int(round(gap / local_period)) - 1)
                        previous_time = repaired[-1]
                        for step in range(1, missing + 1):
                            inserted = previous_time + gap * step / (missing + 1)
                            repaired.append(inserted)
                    if next_time - repaired[-1] > 0.12:
                        repaired.append(float(next_time))
                beat_times = np.asarray(repaired, dtype=np.float64)
            elif len(beat_times) == 1:
                beat_times = np.arange(float(beat_times[0]), duration, beat_period, dtype=np.float64)
            else:
                beat_times = np.arange(0.0, duration, beat_period, dtype=np.float64)
            self.status.emit("Detecting musical key...")
            key_info = detect_key_from_audio(y, sr)
            if key_info:
                estimated_pc, estimated_minor, key_string, camelot = key_info
            else:
                estimated_pc, estimated_minor, key_string, camelot = 0, False, "C", "8B"
            self.status.emit("Measuring bass, mids, highs and musical changes...")
            window = np.hanning(frame_size).astype(np.float32)
            freqs = np.fft.rfftfreq(frame_size, 1.0 / sr)
            band_masks = [((freqs >= lo) & (freqs < hi)) for lo, hi in
                          ((25, 60), (60, 150), (150, 1200), (1200, 8000))]
            raw_features = []
            for start_sample in range(0, len(y), hop):
                chunk = y[start_sample:start_sample + frame_size]
                if len(chunk) < frame_size:
                    chunk = np.pad(chunk, (0, frame_size - len(chunk)))
                spectrum = np.abs(np.fft.rfft(chunk * window))
                bands = [float(np.sqrt(np.mean(spectrum[m] ** 2))) if np.any(m) else 0.0 for m in band_masks]
                rms = float(np.sqrt(np.mean(chunk ** 2)))
                raw_features.append([rms, *bands])
            raw = np.asarray(raw_features, dtype=np.float32)
            normalized = np.zeros_like(raw)
            for col in range(raw.shape[1]):
                values = raw[:, col]
                p90, p98 = np.percentile(values, [90, 98])
                scale = max(float(p90), 1e-8)
                normalized[:, col] = np.clip(values / scale, 0.0, 1.0)
                if p98 > p90 * 1.5:
                    normalized[:, col] = np.sqrt(normalized[:, col])
            stem_level_tracks = {}
            for stem_name in ("drums", "bass", "vocals", "other"):
                stem_audio = stem_signals.get(stem_name)
                if stem_audio is None:
                    stem_level_tracks[stem_name] = np.zeros(len(frame_times), dtype=np.float32)
                    continue
                rms_values = librosa.feature.rms(y=stem_audio, frame_length=frame_size, hop_length=hop, center=True)[0]
                rms_times = librosa.frames_to_time(np.arange(len(rms_values)), sr=sr, hop_length=hop)
                aligned_rms = np.interp(frame_times, rms_times, rms_values, left=0.0, right=0.0)
                scale = max(float(np.percentile(aligned_rms, 96)), 1e-8)
                stem_level_tracks[stem_name] = np.clip(np.sqrt(np.maximum(0.0, aligned_rms / scale)), 0.0, 1.0).astype(np.float32)
            onset_times = librosa.frames_to_time(np.arange(len(onset_env)), sr=sr, hop_length=512)
            onset_at_frames = np.interp(frame_times, onset_times, onset_env, left=0.0, right=0.0)
            onset_scale = max(float(np.percentile(onset_at_frames, 95)), 1e-8)
            onset_norm = np.clip(onset_at_frames / onset_scale, 0.0, 1.0)
            self.status.emit("Separating kick, snare and hi-hat transients...")
            def make_drum_track(fmin, fmax, min_gap_seconds, floor):
                env = librosa.onset.onset_strength(y=drum_y, sr=sr, hop_length=256, fmin=fmin, fmax=fmax, n_mels=32, aggregate=np.mean)
                env = np.asarray(env, dtype=np.float32)
                env_times = librosa.frames_to_time(np.arange(len(env)), sr=sr, hop_length=256)
                env_on_frames = np.interp(frame_times, env_times, env, left=0.0, right=0.0)
                scale = max(float(np.percentile(env_on_frames, 95)), 1e-8)
                norm = np.clip(env_on_frames / scale, 0.0, 1.0)
                env_scale = max(float(np.percentile(env, 95)), 1e-8)
                env_norm = np.clip(env / env_scale, 0.0, 1.5)
                median = float(np.median(env_norm))
                spread = float(np.std(env_norm))
                threshold = max(float(floor), median + 0.55 * spread, float(np.percentile(env_norm, 62)))
                distance = max(1, int(round(min_gap_seconds * sr / 256)))
                peaks, _ = find_peaks(env_norm, height=threshold, distance=distance, prominence=max(0.045, spread * 0.22))
                hits = np.zeros(len(frame_times), dtype=bool)
                strengths = np.zeros(len(frame_times), dtype=np.float32)
                for peak in peaks:
                    peak_time = float(env_times[int(peak)])
                    frame_index = int(np.clip(round(peak_time / (hop / sr)), 0, len(frame_times)-1))
                    hits[frame_index] = True
                    strengths[frame_index] = max(strengths[frame_index], norm[frame_index])
                return norm, hits, strengths
            kick_norm, kick_hits, kick_strength = make_drum_track(35, 180, 0.085, 0.20)
            snare_norm, snare_hits, snare_strength = make_drum_track(180, 5000, 0.075, 0.20)
            hat_norm, hat_hits, hat_strength = make_drum_track(5000, 10500, 0.060, 0.18)
            beat_frames = np.clip(np.rint(beat_times / (hop / sr)).astype(int), 0, len(normalized)-1)
            beat_frames = np.unique(beat_frames)
            beat_times = beat_frames.astype(np.float64) * (hop / sr)
            phase_scores = [[], [], [], []]
            for j, bi in enumerate(beat_frames):
                phase_scores[j % 4].append(float(normalized[bi, 2] + 0.45 * normalized[bi, 1]))
            phase_means = [float(np.mean(v)) if v else 0.0 for v in phase_scores]
            downbeat_phase = int(np.argmax(phase_means)) if any(phase_means) else 0
            beat_lookup = {int(idx): j for j, idx in enumerate(beat_frames)}
            beat_mask = np.zeros(len(normalized), dtype=bool)
            beat_mask[beat_frames] = True
            self.status.emit("Finding sections, buildups, drops and breakdowns...")
            chroma = librosa.feature.chroma_stft(y=y, sr=sr, hop_length=hop)
            section_hop_seconds = 2.0
            section_step = max(1, int(section_hop_seconds / (hop / sr)))
            vectors = []
            section_energy = []
            section_onset = []
            for a in range(0, min(chroma.shape[1], len(normalized)), section_step):
                b = min(chroma.shape[1], len(normalized), a + section_step)
                if b <= a:
                    continue
                harmonic = np.mean(chroma[:, a:b], axis=1)
                harmonic = harmonic / (np.linalg.norm(harmonic) + 1e-8)
                energy = float(np.mean(normalized[a:b, 0]))
                attack = float(np.mean(onset_norm[a:b]))
                vectors.append(np.concatenate([harmonic * 1.6, [energy, attack]]))
                section_energy.append(energy)
                section_onset.append(attack)
            vectors = np.asarray(vectors, dtype=np.float32)
            novelty = np.zeros(len(vectors), dtype=np.float32)
            for i in range(1, len(vectors)):
                a, b = vectors[i-1], vectors[i]
                cosine = 1.0 - float(np.dot(a, b) / ((np.linalg.norm(a) * np.linalg.norm(b)) + 1e-8))
                energy_jump = abs(float(b[-2] - a[-2]))
                novelty[i] = cosine + 0.8 * energy_jump
            if len(novelty) >= 5:
                smooth = np.convolve(novelty, np.ones(3, dtype=np.float32)/3, mode="same")
                threshold = max(float(np.percentile(smooth[1:], 55)), float(np.mean(smooth) + 0.20*np.std(smooth)))
                candidates, _ = find_peaks(smooth, height=threshold, distance=max(3, int(6/section_hop_seconds)), prominence=max(0.008, float(np.std(smooth)*0.12)))
                boundary_seconds = [0.0]
                energy_changes = np.abs(np.diff(np.asarray(section_energy, dtype=np.float32)))
                onset_changes = np.abs(np.diff(np.asarray(section_onset, dtype=np.float32)))
                if len(energy_changes):
                    change_score = energy_changes + 0.25 * onset_changes
                    change_threshold = max(0.10, float(np.percentile(change_score, 70)))
                    for change_index, score in enumerate(change_score, start=1):
                        sec = float(change_index * section_hop_seconds)
                        if (score >= change_threshold and sec >= 4.0 and duration-sec >= 4.0
                                and all(abs(sec-existing) >= 6.0 for existing in boundary_seconds)):
                            boundary_seconds.append(sec)
                for candidate in candidates:
                    sec = float(candidate * section_hop_seconds)
                    if sec >= 4.0 and duration-sec >= 4.0 and all(abs(sec-existing) >= 4.0 for existing in boundary_seconds):
                        boundary_seconds.append(sec)
                boundary_seconds.append(duration)
                boundary_seconds = sorted(set(boundary_seconds))
            else:
                boundary_seconds = [0.0, duration]
            if len(beat_times) >= 4 and len(boundary_seconds) > 2:
                bar_starts = np.asarray([beat_times[j] for j in range(downbeat_phase, len(beat_times), 4)], dtype=np.float64)
                aligned = [0.0]
                for boundary in boundary_seconds[1:-1]:
                    if len(bar_starts):
                        nearest = float(bar_starts[int(np.argmin(np.abs(bar_starts - boundary)))])
                        if abs(nearest - boundary) <= 2.0:
                            boundary = nearest
                    if boundary - aligned[-1] >= 4.0:
                        aligned.append(float(boundary))
                if duration - aligned[-1] >= 2.0:
                    aligned.append(duration)
                else:
                    aligned[-1] = duration
                boundary_seconds = sorted(set(aligned))
            global_energy = float(np.median(section_energy)) if section_energy else 0.0
            high_cut = float(np.percentile(section_energy, 72)) if section_energy else 0.0
            low_cut = float(np.percentile(section_energy, 28)) if section_energy else 0.0
            low_baseline = float(np.percentile(section_energy, 30)) if section_energy else 0.0
            section_records = []
            for si, (left, right) in enumerate(zip(boundary_seconds[:-1], boundary_seconds[1:])):
                a = max(0, int(left / (hop / sr)))
                b = min(len(normalized), max(a+1, int(right / (hop / sr))))
                seg = normalized[a:b, 0]
                seg_onset = onset_norm[a:b]
                mean_e = float(np.mean(seg)) if len(seg) else 0.0
                mean_o = float(np.mean(seg_onset)) if len(seg_onset) else 0.0
                third = max(1, len(seg)//3)
                slope = float(np.mean(seg[-third:]) - np.mean(seg[:third])) if len(seg) >= 3 else 0.0
                if len(boundary_seconds) == 2:
                    label = "Verse / Groove"
                elif si == 0 and mean_e < global_energy * 0.9:
                    label = "Intro"
                elif si == len(boundary_seconds)-2 and mean_e < global_energy * 0.9:
                    label = "Outro"
                elif slope > 0.12 and mean_e >= global_energy * 0.65 and mean_e < high_cut * 1.10:
                    label = "Buildup"
                elif mean_e >= high_cut * 0.88 and mean_e >= max(0.05, low_baseline * 1.40):
                    label = "Drop"
                elif mean_e <= low_cut:
                    label = "Breakdown"
                else:
                    label = "Verse / Groove"
                section_records.append({"start": left, "end": right, "label": label, "energy": mean_e, "onset": mean_o})
            timeline = []
            section_index = 0
            beat_counter = -1
            for i, row in enumerate(normalized):
                t = i * hop / sr
                while section_index + 1 < len(section_records) and t >= section_records[section_index]["end"]:
                    section_index += 1
                section = section_records[section_index]
                is_beat = bool(beat_mask[i])
                if is_beat:
                    beat_counter = beat_lookup.get(i, beat_counter + 1)
                beat_in_bar = ((beat_counter - downbeat_phase) % 4) if beat_counter >= 0 else 0
                bar_number = max(0, (beat_counter - downbeat_phase) // 4) if beat_counter >= 0 else 0
                phrase_number = bar_number // 4
                if beat_counter >= 0 and beat_counter + 1 < len(beat_times):
                    beat_phase = float(np.clip((t - beat_times[beat_counter]) / max(0.001, beat_times[beat_counter + 1] - beat_times[beat_counter]), 0.0, 1.0))
                else:
                    beat_phase = 0.0
                section_progress = float(np.clip((t - section["start"]) / max(0.001, section["end"] - section["start"]), 0.0, 1.0))
                values = {
                    "time": round(t, 3), "energy": round(float(row[0]), 4),
                    "sub": round(float(row[1]), 4), "bass": round(float(row[2]), 4),
                    "mids": round(float(row[3]), 4), "highs": round(float(row[4]), 4),
                    "beat": is_beat, "downbeat": bool(is_beat and beat_in_bar == 0),
                    "beat_in_bar": int(beat_in_bar), "bar": int(bar_number),
                    "phrase": int(phrase_number), "beat_phase": round(beat_phase, 4),
                    "section_progress": round(section_progress, 4),
                    "onset": round(float(onset_norm[i]), 4),
                    "kick": round(float(kick_norm[i]), 4),
                    "snare": round(float(snare_norm[i]), 4),
                    "hat": round(float(hat_norm[i]), 4),
                    "kick_hit": bool(kick_hits[i]),
                    "snare_hit": bool(snare_hits[i]),
                    "hat_hit": bool(hat_hits[i]),
                    "kick_strength": round(float(kick_strength[i]), 4),
                    "snare_strength": round(float(snare_strength[i]), 4),
                    "hat_strength": round(float(hat_strength[i]), 4),
                    "section": section["label"],
                    "stem_levels": {name: round(float(track[i]), 4) for name, track in stem_level_tracks.items()},
                    "transition": bool(i > 0 and abs(t - section["start"]) < (hop/sr)*0.6)
                }
                timeline.append(values)
            self.completed.emit({
                "filename": self.filename, "duration": duration, "bpm": tempo_value,
                "beat_count": int(len(beat_frames)), "timeline": timeline,
                "sections": section_records, "downbeat_phase": downbeat_phase,
                "fps": float(sr / hop), "kick_count": int(np.count_nonzero(kick_hits)),
                "snare_count": int(np.count_nonzero(snare_hits)),
                "hat_count": int(np.count_nonzero(hat_hits)),
                "stem_separation": stem_separation,
                "stem_error": stem_error,
                "stem_availability": stem_availability,
                "stem_files": stem_files,
                "stem_levels": {name: track.tolist() for name, track in stem_level_tracks.items()},
                "estimated_key": key_string,
                "camelot_key": camelot,
                "estimated_pitch_class": estimated_pc,
                "estimated_minor": estimated_minor,
            })
        except Exception as exc:
            self.failed.emit(str(exc))


class KeyBackfillWorker(QThread):
    status = Signal(str)

    def __init__(self, playlist, parent=None):
        super().__init__(parent)
        self.playlist = playlist

    def run(self):
        try:
            import librosa
        except Exception:
            return
        for entry in self.playlist:
            show_path = entry.get("show_path", "")
            audio_path = entry.get("audio_path", "")
            if not show_path or not audio_path:
                continue
            if not Path(show_path).is_file() or not Path(audio_path).is_file():
                continue
            try:
                payload = json.loads(Path(show_path).read_text(encoding="utf-8"))
            except Exception:
                continue
            if payload.get("camelot_key") and payload.get("estimated_key"):
                continue
            try:
                y, sr = librosa.load(audio_path, sr=22050, mono=True, duration=90)
                if y.size < sr:
                    continue
                key_info = detect_key_from_audio(np.nan_to_num(y.astype(np.float32)), sr)
                if not key_info:
                    continue
                pc, is_minor, key_string, camelot = key_info
                payload["estimated_key"] = key_string
                payload["camelot_key"] = camelot
                payload["estimated_pitch_class"] = pc
                payload["estimated_minor"] = is_minor
                Path(show_path).write_text(json.dumps(payload, indent=2), encoding="utf-8")
                entry["camelot_key"] = camelot
                entry["estimated_key"] = key_string
                self.status.emit(f"Key backfilled: {entry.get('name', '?')} → {key_string} ({camelot})")
            except Exception as exc:
                self.status.emit(f"Key backfill skipped for {entry.get('name', '?')}: {exc}")


class KeyCapture(QLineEdit):
    def __init__(self):
        super().__init__()
        self.key_name = ""
        self.setReadOnly(True)
        self.setPlaceholderText("Click here, then press a key")

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Escape, Qt.Key.Key_unknown):
            return
        self.key_name = QKeySequence(event.keyCombination()).toString()
        self.setText(self.key_name)


class BindingDialog(QDialog):
    def __init__(self, parent, existing=None):
        super().__init__(parent)
        self.setWindowTitle("Input Binding")
        self.setMinimumSize(0, 0)
        self.resize(460, 600)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSizeConstraint(QVBoxLayout.SizeConstraint.SetNoConstraint)
        form = QFormLayout()

        self.device = QComboBox()
        self.device.addItems(["Keyboard", "Xbox Controller"])
        form.addRow("Input device:", self.device)

        self.key_input = KeyCapture()
        self.controller_control = QComboBox()
        self.controller_control.addItems(CONTROLLER_CONTROLS)

        self.control_stack = QStackedWidget()
        self.control_stack.addWidget(self.key_input)
        self.control_stack.addWidget(self.controller_control)
        form.addRow("Key / Controller input:", self.control_stack)

        self.channel = QSpinBox()
        self.channel.setRange(1, CHANNELS)
        form.addRow("DMX channel:", self.channel)

        self.value = QSpinBox()
        self.value.setRange(0, 255)
        self.value_row_label = QLabel("Target value:")
        form.addRow(self.value_row_label, self.value)

        self.amount = QSpinBox()
        self.amount.setRange(1, 255)
        self.amount.setValue(10)
        self.amount_row_label = QLabel("Amount per press:")
        form.addRow(self.amount_row_label, self.amount)

        self.action = QComboBox()
        self.action.addItems(ACTIONS)
        form.addRow("Action:", self.action)

        self.sound_path = QLineEdit()
        self.sound_path.setPlaceholderText("Choose a sound effect file")
        self.sound_browse = QPushButton("Browse")
        self.sound_browse.clicked.connect(self.choose_sound_file)
        sound_file_row = QWidget()
        sound_file_layout = QHBoxLayout(sound_file_row)
        sound_file_layout.setContentsMargins(0, 0, 0, 0)
        sound_file_layout.addWidget(self.sound_path, 1)
        sound_file_layout.addWidget(self.sound_browse)
        self.sound_file_label = QLabel("Sound file:")
        form.addRow(self.sound_file_label, sound_file_row)

        self.voice_words = QLineEdit()
        self.voice_words.setPlaceholderText("Comma-separated words, e.g. red, open, strobe")
        self.voice_words_label = QLabel("Voice words:")
        form.addRow(self.voice_words_label, self.voice_words)

        layout.addLayout(form)

        self.device.currentIndexChanged.connect(self.update_device_fields)
        self.action.currentIndexChanged.connect(self.update_action_note)

        self.note = QLabel()
        self.note.setWordWrap(True)
        layout.addWidget(self.note)

        buttons = QGridLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setHorizontalSpacing(6)
        save_button = QPushButton("Apply")
        save_button.setMinimumWidth(0)
        save_button.clicked.connect(self.accept)
        cancel_button = QPushButton("Cancel")
        cancel_button.setMinimumWidth(0)
        cancel_button.clicked.connect(self.reject)
        buttons.addWidget(save_button, 0, 0)
        buttons.addWidget(cancel_button, 0, 1)
        buttons.setColumnStretch(0, 1)
        buttons.setColumnStretch(1, 1)
        layout.addLayout(buttons)

        if existing:
            self.device.setCurrentText(existing.get("device", "Keyboard"))
            control = existing.get("control", existing.get("key", ""))
            if existing.get("device", "Keyboard") == "Keyboard":
                self.key_input.key_name = control
                self.key_input.setText(control)
            else:
                index = self.controller_control.findText(control)
                if index >= 0:
                    self.controller_control.setCurrentIndex(index)
            self.channel.setValue(max(1, min(CHANNELS, int(existing.get("channel", 1)))))
            self.value.setValue(max(0, min(255, int(existing.get("value", 255)))))
            self.action.setCurrentIndex(max(0, min(len(ACTIONS) - 1, int(existing.get("mode", 0)))))
            self.sound_path.setText(existing.get("sound_path", ""))
            self.amount.setValue(max(1, min(255, int(existing.get("amount", 10)))))
            self.voice_words.setText(", ".join(existing.get("voice_words", [])))
        else:
            self.channel.setValue(1)
            self.value.setValue(255)

        self.update_device_fields()
        self.update_action_note()

    def update_device_fields(self, *_):
        self.control_stack.setCurrentIndex(self.device.currentIndex())

    def update_action_note(self, *_):
        mode = self.action.currentIndex()
        notes = {
            0: "Press the input to set the channel to the target value.",
            1: "Sets the target value while held, then resets the channel to 0 on release.",
            2: "Each press switches the channel between the target value and 0.",
            3: "Increases the channel continuously while the input is held. Uses Hold speed and Value per step.",
            4: "Decreases the channel continuously while the input is held. Uses Hold speed and Value per step.",
            5: "Stick Continuous follows the selected stick direction.",
            6: "Stick Hold Value increases for +X/+Y or decreases for -X/-Y while pushed. It holds the last value at center.",
            7: "Trigger Continuous maps released to 0 and fully pressed to 255.",
            8: "Trigger pressure increases the channel value. Releasing the trigger stops movement and holds the value.",
            9: "Trigger pressure decreases the channel value. Releasing the trigger stops movement and holds the value.",
            10: "Each press increases the channel by the amount specified below.",
            11: "Each press decreases the channel by the amount specified below.",
            12: "Each press increases the channel by exactly 1. Holding the button does not repeat.",
            13: "Each press decreases the channel by exactly 1. Holding the button does not repeat.",
            14: "While a generated song show is playing, pause/resume timeline control of this channel. Manual controls remain usable while paused.",
            15: "Press once to play the selected sound effect all the way through. It will not cut off when you release the button.",
            16: "The selected sound effect loops while the button is held and stops immediately when you release it.",
            17: "While held, the microphone is mixed into the audio output. The song keeps playing underneath.",
            18: "While held, the music fades out to silence. When you release it, the music fades back in over five seconds. This action does not use the channel or target value.",
            19: "While held, the microphone listens for the words listed below and sets this channel to the target value when one is heard. Speech recognition is offline.",
        }
        one_shot_adjustment = mode in (10, 11)
        sound_action = mode in (15, 16)
        voice_action = mode == 19
        fade_action = mode == 18
        show_value = (
            voice_action
            or (not one_shot_adjustment and mode not in (14, 15, 16, 17, 18))
        )

        self.amount.setVisible(one_shot_adjustment)
        self.amount_row_label.setVisible(one_shot_adjustment)
        self.value.setVisible(show_value)
        self.value_row_label.setVisible(show_value)
        self.sound_path.setVisible(sound_action)
        self.sound_browse.setVisible(sound_action)
        self.sound_file_label.setVisible(sound_action)
        self.voice_words.setVisible(voice_action)
        self.voice_words_label.setVisible(voice_action)
        self.channel.setEnabled(not fade_action and not (mode == 17) and not voice_action)
        self.note.setText(notes.get(mode, ""))

    def choose_sound_file(self):
        filename, _ = QFileDialog.getOpenFileName(
            self, "Choose sound effect", "",
            "Audio files (*.wav *.ogg *.mp3 *.flac *.m4a);;All files (*.*)"
        )
        if filename:
            self.sound_path.setText(filename)

    def get_binding(self):
        device = self.device.currentText()
        control = (self.key_input.key_name if device == "Keyboard"
                   else self.controller_control.currentText())
        words = []
        for part in self.voice_words.text().split(","):
            part = part.strip().lower()
            if part:
                words.append(part)
        return {
            "device": device,
            "control": control,
            "key": control,
            "channel": self.channel.value(),
            "value": self.value.value(),
            "amount": self.amount.value(),
            "mode": self.action.currentIndex(),
            "sound_path": self.sound_path.text().strip(),
            "voice_words": words,
        }


class DMXOutput:
    def __init__(self):
        self.devices = {}
        self.break_us = 100
        self.baud = 250000

    def connect(self, port, baud=250000, break_us=100):
        self.disconnect(port)
        self.baud = baud
        self.break_us = break_us
        self.devices[port] = serial.Serial(
            port=port,
            baudrate=baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_TWO,
            timeout=0,
            write_timeout=0.1
        )

    def disconnect(self, port=None):
        if port is None:
            ports = list(self.devices.keys())
        else:
            ports = [port] if port in self.devices else []
        for p in ports:
            dev = self.devices.pop(p, None)
            if dev is not None:
                try:
                    dev.break_condition = False
                    dev.close()
                except (serial.SerialException, OSError):
                    pass

    def send(self, values):
        if not self.devices:
            return
        packet = bytes([0] + [max(0, min(255, int(v))) for v in values[:CHANNELS]])
        if len(packet) < CHANNELS + 1:
            packet += bytes(CHANNELS + 1 - len(packet))
        for port, dev in list(self.devices.items()):
            if not dev or not dev.is_open:
                continue
            try:
                dev.break_condition = True
                time.sleep(self.break_us / 1_000_000)
                dev.break_condition = False
                time.sleep(0.000012)
                dev.write(packet)
            except (serial.SerialException, OSError) as error:
                self.disconnect(port)
                raise RuntimeError(f"{port}: {error}") from error

    @property
    def device(self):
        return next(iter(self.devices.values()), None)


class StemVisualizer(QWidget):
    seek_requested = Signal(float)
    def __init__(self, color, parent=None):
        super().__init__(parent)
        self.color = QColor(color)
        self.levels = []
        self.playhead = 0.0
        self.duration = 0.0
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Click anywhere on this waveform to seek this deck.")
        self.setMinimumHeight(46)
        self.setMaximumHeight(70)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_data(self, levels):
        self.levels = [max(0.0, min(1.0, float(v))) for v in (levels or [])]
        self.update()

    def set_playhead(self, seconds):
        self.playhead = max(0.0, float(seconds))
        self.update()

    def set_duration(self, seconds):
        self.duration = max(0.0, float(seconds))
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.duration > 0:
            x = max(0.0, min(float(self.width()), float(event.position().x())))
            self.seek_requested.emit((x / max(1.0, float(self.width()))) * self.duration)
            event.accept()
            return
        super().mousePressEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = self.rect().adjusted(2, 2, -2, -2)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(28, 31, 38))
        painter.drawRoundedRect(rect, 6, 6)
        if not self.levels:
            painter.setPen(QColor(145, 150, 160))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "No song")
            return
        mid = rect.center().y()
        count = len(self.levels)
        step = max(1, int(rect.width() / max(1, count)))
        bar_w = max(1, min(4, step))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self.color)
        for i, value in enumerate(self.levels):
            x = rect.left() + int(i * rect.width() / count)
            h = max(1.5, (rect.height() - 8) * value)
            painter.drawRoundedRect(int(x), int(mid - h / 2), bar_w, int(h), 1.5, 1.5)
        if self.playhead > 0:
            duration = max(0.001, self.duration or (len(self.levels) / 20.0))
            x = rect.left() + int(rect.width() * min(1.0, self.playhead / duration))
            painter.setPen(QPen(QColor(255, 255, 255), 2))
            painter.drawLine(x, rect.top() + 2, x, rect.bottom() - 2)


class SwipeWaveform(QWidget):
    """Interactive scrolling waveform. Drag horizontally to scrub."""
    scrubbed = Signal(float)
    grab_started = Signal()
    grab_ended = Signal()

    def __init__(self, accent, parent=None):
        super().__init__(parent)
        self.accent = QColor(accent)
        self.envelope = []
        self.bin_seconds = ENVELOPE_BIN_SECONDS
        self.duration = 0.0
        self.position = 0.0
        self.window_seconds = 16.0
        self.pitch_factor = 1.0
        self.beat_times = []
        self.downbeat_times = []
        self.eighth_bar_times = []
        self.section_ranges = []
        self._dragging = False
        self._drag_start_x = 0.0
        self._drag_start_position = 0.0
        self.setMinimumHeight(90)
        self.setMaximumHeight(130)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setToolTip(
            "Drag horizontally to scrub. Bars are colored by song section.")

    def _effective_window(self):
        return self.window_seconds * self.pitch_factor

    def set_pitch_factor(self, factor):
        factor = max(0.5, min(2.0, float(factor)))
        if abs(factor - self.pitch_factor) < 1e-6:
            return
        self.pitch_factor = factor
        self.update()

    def set_envelope(self, envelope, bin_seconds, duration):
        self.envelope = list(envelope) if envelope else []
        self.bin_seconds = max(1e-4, float(bin_seconds) or ENVELOPE_BIN_SECONDS)
        self.duration = max(0.0, float(duration))
        self.update()

    def set_beat_data(self, beat_times, downbeat_times):
        self.beat_times = sorted(float(t) for t in (beat_times or []))
        db = set(round(float(t), 4) for t in (downbeat_times or []))
        self.downbeat_times = [t for t in self.beat_times if round(t, 4) in db]
        self.eighth_bar_times = self.downbeat_times[::8] if self.downbeat_times else []
        self.update()

    def set_section_ranges(self, sections):
        ranges = []
        for s in sections or []:
            try:
                ranges.append((float(s.get("start", 0.0)),
                               float(s.get("end", 0.0)),
                               str(s.get("label", ""))))
            except (TypeError, ValueError):
                continue
        self.section_ranges = ranges
        self.update()

    @staticmethod
    def _section_color(label):
        label = (label or "").lower()
        if "intro" in label:
            return QColor(120, 180, 255)
        if "buildup" in label or "build" in label:
            return QColor(255, 170, 60)
        if "drop" in label:
            return QColor(255, 70, 90)
        if "breakdown" in label or "break" in label:
            return QColor(150, 110, 255)
        if "outro" in label:
            return QColor(120, 200, 150)
        return QColor(200, 210, 220)

    def _color_for_time(self, t):
        for start, end, label in self.section_ranges:
            if start <= t < end:
                return self._section_color(label)
        return self.accent

    def set_position(self, seconds):
        if not self._dragging:
            self.position = max(0.0, float(seconds))
            self.update()

    def clear(self):
        self.envelope = []
        self.duration = 0.0
        self.position = 0.0
        self.beat_times = []
        self.downbeat_times = []
        self.eighth_bar_times = []
        self._dragging = False
        self.update()

    def _x_to_seconds(self, x):
        rect = self.rect().adjusted(2, 2, -2, -2)
        width = max(1, rect.width())
        half = self._effective_window() / 2.0
        start_sec = self.position - half
        end_sec = self.position + half
        ratio = (x - rect.left()) / float(width)
        ratio = max(0.0, min(1.0, ratio))
        return start_sec + ratio * (end_sec - start_sec)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.duration > 0:
            self._dragging = True
            self._drag_start_x = event.position().x()
            self._drag_start_position = self.position
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            self.grab_started.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if not self._dragging:
            super().mouseMoveEvent(event)
            return
        rect = self.rect().adjusted(2, 2, -2, -2)
        width = max(1, rect.width())
        dx = event.position().x() - self._drag_start_x
        seconds_per_pixel = self._effective_window() / float(width)
        new_position = self._drag_start_position - dx * seconds_per_pixel
        new_position = max(0.0, min(self.duration, new_position))
        self.position = new_position
        self.scrubbed.emit(new_position)
        self.update()
        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._dragging:
            self._dragging = False
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            self.grab_ended.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = self.rect().adjusted(2, 2, -2, -2)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(24, 27, 33))
        painter.drawRoundedRect(rect, 6, 6)

        mid = rect.center().y()
        painter.setPen(QPen(QColor(60, 66, 78), 1))
        painter.drawLine(rect.left() + 4, mid, rect.right() - 4, mid)

        if not self.envelope or self.duration <= 0.0:
            painter.setPen(QColor(140, 146, 156))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "No song loaded")
            return

        half = self._effective_window() / 2.0
        start_sec = self.position - half
        end_sec = self.position + half
        if end_sec - start_sec <= 1e-6:
            return

        n_bins = len(self.envelope)
        total_sec = n_bins * self.bin_seconds
        width = max(1, rect.width())
        mid_x = rect.left() + width / 2.0
        seconds_per_pixel = (end_sec - start_sec) / float(width)

        painter.setPen(Qt.PenStyle.NoPen)
        for px in range(width):
            t = start_sec + px * seconds_per_pixel
            if t < 0.0 or t >= total_sec:
                continue
            bin_lo = int(t / self.bin_seconds)
            bin_hi = int((t + seconds_per_pixel) / self.bin_seconds)
            bin_lo = max(0, min(n_bins - 1, bin_lo))
            bin_hi = max(bin_lo, min(n_bins, bin_hi))
            if bin_hi <= bin_lo:
                value = self.envelope[bin_lo]
            else:
                value = max(self.envelope[bin_lo:bin_hi])
            h = max(1.0, (rect.height() - 10) * value)
            x = rect.left() + px
            distance = abs(x - mid_x) / max(1.0, width / 2.0)
            alpha = int(255 * (1.0 - 0.45 * min(1.0, distance)))
            base = self._color_for_time(t)
            painter.setBrush(QColor(base.red(), base.green(), base.blue(), alpha))
            painter.drawRect(x, int(mid - h / 2), 1, int(h))

        def x_for(t):
            if end_sec - start_sec <= 1e-6:
                return None
            ratio = (t - start_sec) / (end_sec - start_sec)
            if ratio < 0.0 or ratio > 1.0:
                return None
            return rect.left() + ratio * (width - 1)

        painter.setPen(QPen(QColor(120, 126, 138, 90), 1))
        for t in self.beat_times:
            x = x_for(t)
            if x is not None:
                painter.drawLine(int(x), rect.top() + 2, int(x), rect.bottom() - 2)

        painter.setPen(QPen(QColor(90, 170, 255, 220), 1))
        for t in self.downbeat_times:
            x = x_for(t)
            if x is not None:
                painter.drawLine(int(x), rect.top() + 2, int(x), rect.bottom() - 2)

        painter.setPen(QPen(QColor(255, 70, 80, 240), 2))
        for t in self.eighth_bar_times:
            x = x_for(t)
            if x is not None:
                painter.drawLine(int(x), rect.top() + 2, int(x), rect.bottom() - 2)

        painter.setPen(QPen(QColor(255, 255, 255), 2))
        painter.drawLine(int(mid_x), rect.top() + 3, int(mid_x), rect.bottom() - 3)


class PlaylistListWidget(QListWidget):
    empty_context_requested = Signal(QPoint)
    item_context_requested = Signal(int, QPoint)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)
        self.setDragEnabled(True)
        self.setDragDropMode(QListWidget.DragDropMode.DragOnly)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)

    def startDrag(self, supported_actions):
        item = self.currentItem()
        if item is None:
            return
        row = item.data(Qt.ItemDataRole.UserRole)
        if row is None:
            return
        data = json.dumps({"playlist_index": int(row)})
        drag = QDrag(self)
        mime = QMimeData()
        mime.setData("application/x-dmx-playlist", data.encode("utf-8"))
        mime.setText(item.text())
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.CopyAction)

    def _on_context_menu(self, pos):
        item = self.itemAt(pos)
        if item is None:
            self.empty_context_requested.emit(self.mapToGlobal(pos))
            return
        row = self.row(item)
        self.item_context_requested.emit(row, self.mapToGlobal(pos))


class RichTextDelegate(QStyledItemDelegate):
    """Renders simple HTML in list items so recommended-track scores can be colored."""

    def paint(self, painter, option, index):
        text = index.data(Qt.ItemDataRole.DisplayRole)
        if not isinstance(text, str) or "<" not in text:
            super().paint(painter, option, index)
            return
        painter.save()
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect, option.palette.highlight())
        else:
            painter.fillRect(option.rect, option.palette.base())
        doc = QTextDocument()
        doc.setDefaultFont(option.font)
        doc.setHtml(text)
        doc.setTextWidth(max(32, option.rect.width() - 10))
        painter.translate(option.rect.left() + 5, option.rect.top() + 3)
        doc.drawContents(painter)
        painter.restore()

    def sizeHint(self, option, index):
        text = index.data(Qt.ItemDataRole.DisplayRole)
        if not isinstance(text, str) or "<" not in text:
            return super().sizeHint(option, index)
        width = option.rect.width()
        if width <= 50:
            width = 480
        doc = QTextDocument()
        doc.setDefaultFont(option.font)
        doc.setHtml(text)
        doc.setTextWidth(max(64, width - 10))
        return QSize(width, max(24, int(doc.size().height()) + 6))


class QueueListWidget(QListWidget):
    queue_reordered = Signal(list)
    queue_request_play = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context)
        self.model().rowsMoved.connect(self._after_move)

    def _after_move(self, *_):
        order = []
        for i in range(self.count()):
            it = self.item(i)
            if it is not None:
                entry = it.data(Qt.ItemDataRole.UserRole)
                if entry is not None:
                    order.append(entry)
        self.queue_reordered.emit(order)

    def _on_context(self, pos):
        item = self.itemAt(pos)
        if item is None:
            return
        menu = QMenu(self)
        play_action = QAction("Play now", self)
        menu.addAction(play_action)
        chosen = menu.exec(self.mapToGlobal(pos))
        if chosen is play_action:
            self.queue_request_play.emit(self.row(item))


class PianoKeyboardWidget(QWidget):
    note_pressed = Signal(int)
    note_released = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.start_note = 21
        self.num_keys = 88
        self.active = set()
        self.pressed_by_mouse = set()
        self.setMinimumHeight(120)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    def set_range(self, start_note, num_keys):
        self.start_note = max(0, min(127, int(start_note)))
        self.num_keys = max(1, min(200, int(num_keys)))
        self.update()

    def set_active_notes(self, notes):
        self.active = set(int(n) for n in notes)
        self.update()

    def _key_layout(self):
        keys = list(range(self.start_note, self.start_note + self.num_keys))
        white = [k for k in keys if NOTE_NAMES[k % 12] not in ("C#", "D#", "F#", "G#", "A#")]
        black = [k for k in keys if NOTE_NAMES[k % 12] in ("C#", "D#", "F#", "G#", "A#")]
        return keys, white, black

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect()
        painter.fillRect(rect, QColor(20, 22, 26))
        keys, white, black = self._key_layout()
        if not white:
            return
        n_white = len(white)
        white_w = rect.width() / n_white
        white_index = {k: i for i, k in enumerate(white)}
        for i, note in enumerate(white):
            x = i * white_w
            r = QRectF(x, 0, white_w, rect.height())
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(240, 240, 240) if note not in self.active else QColor(120, 200, 255))
            painter.drawRect(r)
            painter.setPen(QPen(QColor(0, 0, 0), 1))
            painter.drawLine(int(x), 0, int(x), rect.height())
            painter.setPen(QColor(40, 40, 40))
            painter.setFont(QFont("Segoe UI", 7))
            painter.drawText(r.adjusted(2, rect.height() - 16, -2, -2),
                             Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom,
                             midi_note_name(note))
        for note in black:
            white_before = None
            for k in reversed(range(self.start_note, note)):
                if k in white_index:
                    white_before = white_index[k]
                    break
            if white_before is None:
                continue
            x = (white_before + 1) * white_w - white_w * 0.35
            w = white_w * 0.7
            r = QRectF(x, 0, w, rect.height() * 0.6)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(20, 20, 20) if note not in self.active else QColor(60, 120, 200))
            painter.drawRect(r)

    def mousePressEvent(self, event):
        note = self._note_at(event.position().x(), event.position().y())
        if note is not None:
            self.pressed_by_mouse.add(note)
            self.note_pressed.emit(note)

    def mouseReleaseEvent(self, event):
        for note in list(self.pressed_by_mouse):
            self.pressed_by_mouse.discard(note)
            self.note_released.emit(note)

    def _note_at(self, x, y):
        keys, white, black = self._key_layout()
        if not white:
            return None
        n_white = len(white)
        white_w = self.width() / n_white
        for note in black:
            white_before = None
            for k in reversed(range(self.start_note, note)):
                if k in white:
                    white_before = white.index(k)
                    break
            if white_before is None:
                continue
            bx = (white_before + 1) * white_w - white_w * 0.35
            bw = white_w * 0.7
            if bx <= x <= bx + bw and y <= self.height() * 0.6:
                return note
        idx = int(x / white_w)
        if 0 <= idx < n_white:
            return white[idx]
        return None


class DJDeck(QWidget):
    play_pause_clicked = Signal()
    cue_clicked = Signal()
    dropped = Signal(object)
    scrub_moved = Signal(float)
    seek_requested = Signal(float)
    pitch_changed = Signal(float)
    stem_mute_changed = Signal(str, bool)
    stem_seek_requested = Signal(str, float)
    sync_clicked = Signal()
    empty_clicked = Signal()
    lineup_clicked = Signal()

    def __init__(self, accent, label, sync_label, parent=None, show_waveform=True):
        super().__init__(parent)
        self.accent = QColor(accent)
        self.label = label
        self.sync_label = sync_label
        self.show_waveform = bool(show_waveform)
        self.original_bpm = 0.0
        self.original_key = ""
        self.original_camelot = ""
        self.setMinimumWidth(280)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)
        self._build()

    def _build(self):
        self.setAcceptDrops(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)
        layout.setSizeConstraint(QVBoxLayout.SizeConstraint.SetMinimumSize)

        header_row = QHBoxLayout()
        self.title_label = QLabel(self.label)
        self.title_label.setStyleSheet(
            f"font-size: 14px; font-weight: bold; color: {self.accent.name()};"
        )
        header_row.addWidget(self.title_label)
        header_row.addStretch()
        layout.addLayout(header_row)

        # Song title above the timestamp row.
        title_row = QHBoxLayout()
        self.song_title_label = QLabel("No song loaded")
        self.song_title_label.setStyleSheet(
            "font-size: 13px; font-weight: bold; color: #101010;")
        title_row.addWidget(self.song_title_label, 1)
        layout.addLayout(title_row)

        progress_row = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setMinimumHeight(14)
        progress_row.addWidget(self.progress, 1)

        self.time_label = QLabel("0:00 / 0:00")
        self.time_label.setMinimumWidth(100)
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        progress_row.addWidget(self.time_label)

        self.empty_button = QPushButton("✕")
        self.empty_button.setFixedSize(22, 22)
        self.empty_button.setToolTip("Empty this deck.")
        self.empty_button.setStyleSheet(
            "QPushButton { padding: 0px; font-weight: bold; border-radius: 11px; }")
        self.empty_button.clicked.connect(self.empty_clicked.emit)
        progress_row.addWidget(self.empty_button)

        layout.addLayout(progress_row)

        self.waveform = SwipeWaveform(self.accent)
        self.waveform.scrubbed.connect(self.seek_requested.emit)
        self.waveform.grab_started.connect(lambda: self.scrub_moved.emit(0.0))
        self.waveform.setMinimumHeight(90)
        self.waveform.setMaximumHeight(130)
        if self.show_waveform:
            layout.addWidget(self.waveform, 2)
        else:
            self.waveform.setParent(None)
            self.waveform.hide()

        transport_row = QHBoxLayout()
        transport_row.setSpacing(6)
        transport_row.addStretch()
        self.play_pause_button = QPushButton("▶")
        self.play_pause_button.setFixedSize(52, 40)
        self.play_pause_button.setToolTip("Play / Pause this deck.")
        self.play_pause_button.clicked.connect(self.play_pause_clicked.emit)
        transport_row.addWidget(self.play_pause_button)
        self.cue_button = QPushButton("CUE")
        self.cue_button.setFixedSize(52, 40)
        self.cue_button.setToolTip("Restart this deck's song from the beginning.")
        self.cue_button.clicked.connect(self.cue_clicked.emit)
        transport_row.addWidget(self.cue_button)

        self.lineup_button = QPushButton("LINEUP")
        self.lineup_button.setFixedSize(64, 40)
        self.lineup_button.setToolTip(
            "Jump both decks to their nearest bar boundary and start them together.")
        self.lineup_button.clicked.connect(self.lineup_clicked.emit)
        transport_row.addWidget(self.lineup_button)
        transport_row.addStretch()
        layout.addLayout(transport_row)

        stems_group = QGroupBox("Stems")
        stems_layout = QGridLayout(stems_group)
        stems_layout.setContentsMargins(4, 4, 4, 4)
        stems_layout.setHorizontalSpacing(6)
        stems_layout.setVerticalSpacing(2)
        self.stem_visualizers = {}
        self.stem_mutes = {}
        for key, label, color in (
            ("vocals", "Vocals", "#60a5fa"),
            ("drums", "Drums", "#f59e0b"),
            ("bass", "Bass", "#22c55e"),
            ("other", "Other", "#c084fc"),
        ):
            lbl = QLabel(label)
            lbl.setStyleSheet("font-size: 9px;")
            vis = StemVisualizer(color)
            vis.seek_requested.connect(
                lambda secs, k=key: self.stem_seek_requested.emit(k, secs))
            mute = QCheckBox("M")
            mute.setToolTip(f"Mute {label}.")
            mute.toggled.connect(
                lambda checked, k=key: self.stem_mute_changed.emit(k, checked))
            row_idx = len(self.stem_visualizers)
            stems_layout.addWidget(lbl, row_idx, 0)
            stems_layout.addWidget(vis, row_idx, 1)
            stems_layout.addWidget(mute, row_idx, 2)
            self.stem_visualizers[key] = vis
            self.stem_mutes[key] = mute
        layout.addWidget(stems_group, 1)

        pitch_row = QHBoxLayout()
        pitch_row.setSpacing(10)
        pitch_row.addStretch()

        pitch_buttons_col = QVBoxLayout()
        pitch_buttons_col.setSpacing(4)
        pitch_buttons_col.addStretch()

        self.sync_button = QPushButton(self.sync_label)
        self.sync_button.setFixedWidth(64)
        self.sync_button.setToolTip(
            "Match this deck's tempo to the other deck (half/double detection uses the ratio "
            "closest to unity).")
        self.sync_button.clicked.connect(self.sync_clicked.emit)
        pitch_buttons_col.addWidget(self.sync_button)
        pitch_buttons_col.addStretch()

        pitch_row.addLayout(pitch_buttons_col)

        pitch_col = QVBoxLayout()
        pitch_col.setSpacing(2)
        pitch_label = QLabel("Pitch")
        pitch_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        pitch_label.setStyleSheet("font-size: 9px; color: #9aa4b2;")
        pitch_col.addWidget(pitch_label)
        self.pitch_fader = DetentSlider(Qt.Orientation.Vertical)
        self.pitch_fader.setRange(-1000, 1000)
        self.pitch_fader.setValue(0)
        self.pitch_fader.setFixedWidth(52)
        self.pitch_fader.setMinimumHeight(150)
        self.pitch_fader.setToolTip(
            "Pitch / tempo fader ±16%. Snaps to 0% near the centre. Changes playback speed and BPM.")
        self.pitch_fader.valueChanged.connect(self._on_pitch_changed)
        pitch_col.addWidget(self.pitch_fader)
        self.pitch_label = QLabel("+0.00%")
        self.pitch_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.pitch_label.setMinimumWidth(64)
        self.pitch_label.setStyleSheet(
            "font-family: Consolas, 'Courier New', monospace; font-size: 10px;")
        pitch_col.addWidget(self.pitch_label)
        pitch_row.addLayout(pitch_col)

        bpm_col = QVBoxLayout()
        bpm_col.setSpacing(2)
        bpm_col.addStretch()
        self.orig_bpm_label = QLabel("Orig   —")
        self.orig_bpm_label.setStyleSheet(
            "font-family: Consolas, 'Courier New', monospace; "
            "font-size: 11px; color: #9aa4b2;")
        bpm_col.addWidget(self.orig_bpm_label)
        self.now_bpm_label = QLabel("Now    —")
        self.now_bpm_label.setStyleSheet(
            f"font-family: Consolas, 'Courier New', monospace; "
            f"font-size: 14px; font-weight: bold; color: {self.accent.name()};")
        bpm_col.addWidget(self.now_bpm_label)
        self.key_label = QLabel("Key    —")
        self.key_label.setStyleSheet(
            "font-family: Consolas, 'Courier New', monospace; "
            "font-size: 12px; color: #9aa4b2;")
        bpm_col.addWidget(self.key_label)
        bpm_col.addStretch()
        pitch_row.addLayout(bpm_col)

        pitch_row.addStretch()
        layout.addLayout(pitch_row)

    def set_song(self, entry, audio, stem_levels, duration):
        if entry is None or audio is None:
            self.time_label.setText("0:00 / 0:00")
            self.progress.setValue(0)
            self.song_title_label.setText("No song loaded")
            for vis in self.stem_visualizers.values():
                vis.set_data([])
                vis.set_duration(0.0)
                vis.set_playhead(0.0)
            self.waveform.clear()
            return
        self.time_label.setText(f"0:00 / {self._fmt(duration)}")
        self.progress.setValue(0)
        self.song_title_label.setText(entry.get("name", "Untitled"))
        for key, vis in self.stem_visualizers.items():
            vis.set_data(stem_levels.get(key, []))
            vis.set_duration(duration)
            vis.set_playhead(0.0)

    def set_waveform(self, envelope, bin_seconds, duration):
        self.waveform.set_envelope(envelope, bin_seconds, duration)

    def update_position(self, position, duration, playing):
        self.waveform.set_position(position)
        self.progress.setValue(int(max(0.0, min(1.0, position / max(0.001, duration))) * 1000))
        self.time_label.setText(f"{self._fmt(position)} / {self._fmt(duration)}")
        self.play_pause_button.setText("⏸" if playing else "▶")
        for vis in self.stem_visualizers.values():
            vis.set_playhead(position)

    def clear_visuals(self):
        self.waveform.clear()
        self.time_label.setText("0:00 / 0:00")
        self.progress.setValue(0)
        self.play_pause_button.setText("▶")
        self.song_title_label.setText("No song loaded")
        self.orig_bpm_label.setText("Orig   —")
        self.now_bpm_label.setText("Now    —")
        self.key_label.setText("Key    —")
        self.original_bpm = 0.0
        self.original_key = ""
        self.original_camelot = ""
        for vis in self.stem_visualizers.values():
            vis.set_data([])
            vis.set_duration(0.0)
            vis.set_playhead(0.0)

    def _on_pitch_changed(self, value):
        factor = 1.0 + (value / 1000.0) * PITCH_RANGE
        self.pitch_label.setText(f"{(factor - 1.0) * 100.0:+.2f}%")
        self.waveform.set_pitch_factor(factor)
        self._update_bpm_labels()
        self.pitch_changed.emit(factor)

    def _update_bpm_labels(self):
        factor = 1.0 + (self.pitch_fader.value() / 1000.0) * PITCH_RANGE
        if self.original_bpm > 0:
            self.orig_bpm_label.setText(f"Orig   {self.original_bpm:5.1f}")
            self.now_bpm_label.setText(f"Now    {self.original_bpm * factor:5.1f}")
        else:
            self.orig_bpm_label.setText("Orig   —")
            self.now_bpm_label.setText("Now    —")

    def set_original_bpm(self, bpm):
        self.original_bpm = float(bpm or 0.0)
        self._update_bpm_labels()

    def set_song_key(self, key_string, camelot):
        self.original_key = key_string or ""
        self.original_camelot = camelot or ""
        if key_string or camelot:
            parts = []
            if camelot:
                parts.append(camelot)
            if key_string:
                parts.append(key_string)
            self.key_label.setText("Key    " + " / ".join(parts))
        else:
            self.key_label.setText("Key    —")
        self.key_label.setStyleSheet(
            "font-family: Consolas, 'Courier New', monospace; "
            "font-size: 12px; color: #101010;")

    def set_beat_data(self, beat_times, downbeat_times):
        self.waveform.set_beat_data(beat_times, downbeat_times)

    def set_pitch_factor(self, factor):
        factor = max(1.0 - PITCH_RANGE, min(1.0 + PITCH_RANGE, float(factor)))
        value = int(round((factor - 1.0) / PITCH_RANGE * 1000.0))
        self.pitch_fader.blockSignals(True)
        self.pitch_fader.setValue(value)
        self.pitch_fader.blockSignals(False)
        self.pitch_label.setText(f"{(factor - 1.0) * 100.0:+.2f}%")
        self.waveform.set_pitch_factor(factor)
        self._update_bpm_labels()

    def dragEnterEvent(self, event):
        mime = event.mimeData()
        if mime.hasFormat("application/x-dmx-playlist") or mime.hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        mime = event.mimeData()
        if mime.hasFormat("application/x-dmx-playlist") or mime.hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        mime = event.mimeData()
        if mime.hasFormat("application/x-dmx-playlist"):
            raw = bytes(mime.data("application/x-dmx-playlist")).decode("utf-8")
            try:
                info = json.loads(raw)
            except Exception:
                event.ignore()
                return
            idx = info.get("playlist_index")
            if idx is not None:
                self.dropped.emit({"playlist_index": int(idx)})
                event.acceptProposedAction()
                return
        if mime.hasUrls():
            for url in mime.urls():
                if url.isLocalFile():
                    self.dropped.emit({"path": url.toLocalFile()})
                    event.acceptProposedAction()
                    return
        event.ignore()

    @staticmethod
    def _fmt(seconds):
        seconds = max(0.0, float(seconds))
        return f"{int(seconds // 60)}:{int(seconds % 60):02d}"


class PreloadWorker(QThread):
    ready = Signal(str, object)
    failed = Signal(str, str)

    def __init__(self, entry_key, audio_path, show_path, parent=None):
        super().__init__(parent)
        self.entry_key = entry_key
        self.audio_path = audio_path
        self.show_path = show_path

    def run(self):
        try:
            payload = json.loads(Path(self.show_path).read_text(encoding="utf-8"))
            rate = 0
            try:
                info = sf.info(str(self.audio_path))
                rate = int(info.samplerate)
            except Exception:
                pass
            if rate <= 0:
                rate = DECK_RATE
            import librosa
            audio, actual_rate = librosa.load(self.audio_path, sr=rate, mono=False)
            audio = np.asarray(audio, dtype=np.float32)
            if audio.ndim == 1:
                audio = np.repeat(audio.reshape(-1, 1), 2, axis=1)
            else:
                audio = audio.T
                if audio.shape[1] == 1:
                    audio = np.repeat(audio, 2, axis=1)
                elif audio.shape[1] > 2:
                    audio = audio[:, :2]
            audio = np.nan_to_num(np.clip(audio, -1.0, 1.0))
            self.ready.emit(self.entry_key, {
                "audio": audio,
                "rate": int(actual_rate or rate),
                "payload": payload,
            })
        except Exception as exc:
            self.failed.emit(self.entry_key, str(exc))


class Preview3DWindow(QMainWindow):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.setWindowTitle("3D Preview")
        self.resize(1000, 760)
        self.plotter = None
        self.actors = {}
        self.text_actors = []
        self.last_note_midi = None
        self._camera_initialized = False
        self._build_ui()
        self._init_plotter()
        self._rebuild_scene()
        self._set_default_camera()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(50)

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.canvas_frame = QFrame(central)
        self.canvas_frame.setStyleSheet("background-color: #12141a;")
        layout.addWidget(self.canvas_frame, 1)
        self.canvas_layout = QVBoxLayout(self.canvas_frame)
        self.canvas_layout.setContentsMargins(0, 0, 0, 0)

    def _init_plotter(self):
        try:
            import pyvista as pv
            from pyvistaqt import QtInteractor
        except ImportError as exc:
            lbl = QLabel(
                "3D Preview requires pyvista and pyvistaqt.\n\n"
                "Install them with:\n    py -m pip install pyvista pyvistaqt\n\n"
                f"Details: {exc}"
            )
            lbl.setStyleSheet("color: #ffb070; padding: 20px;")
            lbl.setWordWrap(True)
            self.canvas_layout.addWidget(lbl)
            self.plotter = None
            return
        pv.global_theme.background = "#12141a"
        pv.global_theme.font.color = "white"
        self.plotter = QtInteractor(self.canvas_frame)
        self.plotter.set_background("#12141a")
        self.canvas_layout.addWidget(self.plotter.interactor)
        self.plotter.enable_anti_aliasing()
        self.plotter.add_axes()

    def _set_default_camera(self):
        if self.plotter is None:
            return
        c = self.controller
        try:
            strip_len = float(c.piano_strip_length_ft_spin.value())
            wall_dist = float(c.piano_wall_distance_ft_spin.value())
            ceil_h = float(c.piano_ceiling_height_ft_spin.value())
            fwd = float(c.piano_forward_distance_ft_spin.value())
            span = max(10.0, strip_len, wall_dist, ceil_h, fwd)
            self.plotter.camera.position = (
                span * 1.2, -span * 1.2, span * 1.1
            )
            self.plotter.camera.focal_point = (
                span * 0.2, span * 0.3, span * 0.35
            )
            self.plotter.camera.up = (0.0, 0.0, 1.0)
            self.plotter.reset_camera_clipping_range()
            self._camera_initialized = True
        except Exception:
            try:
                self.plotter.reset_camera()
            except Exception:
                pass

    def _clear_scene(self):
        if self.plotter is None:
            return
        try:
            self.plotter.clear()
        except Exception:
            pass
        self.actors = {}
        self.text_actors = []

    def _add_text(self, text, point, color="white", size=14):
        if self.plotter is None:
            return
        try:
            actor = self.plotter.add_point_labels(
                [point], [text], font_size=size, text_color=color,
                shape_opacity=0.0, always_visible=True, name=None,
            )
            self.text_actors.append(actor)
        except Exception:
            pass

    def _rebuild_scene(self):
        if self.plotter is None:
            return
        self._clear_scene()
        try:
            import pyvista as pv
        except Exception:
            return
        c = self.controller
        surface = c.piano_surface_combo.currentIndex()
        is_wall = surface == 0
        fixture_h = float(c.piano_fixture_height_ft_spin.value())
        strip_len = float(c.piano_strip_length_ft_spin.value())
        left_off = float(c.piano_left_offset_ft_spin.value())
        if strip_len <= 0:
            strip_len = 1.0
        strip_depth = strip_len / 4.0

        fixture_pos = np.array([0.0, 0.0, fixture_h], dtype=float)
        self.actors["fixture"] = self.plotter.add_mesh(
            pv.Sphere(radius=max(0.08, min(0.6, strip_len * 0.02)),
                       center=tuple(fixture_pos)),
            color="#ffc94a",
        )

        grid_size = max(10.0, strip_len * 1.5)
        floor = pv.Plane(
            center=(0.0, 0.0, 0.0),
            direction=(0.0, 0.0, 1.0),
            i_size=grid_size, j_size=grid_size,
        )
        self.plotter.add_mesh(
            floor, color="#6a7480", style="wireframe", opacity=0.55,
            line_width=1,
        )

        self.plotter.add_arrows(np.array([fixture_pos]),
                                np.array([[1.0, 0.0, 0.0]]),
                                mag=1.0, color="#ff5d5d")
        self.plotter.add_arrows(np.array([fixture_pos]),
                                np.array([[0.0, 1.0, 0.0]]),
                                mag=1.0, color="#5dff7a")
        self.plotter.add_arrows(np.array([fixture_pos]),
                                np.array([[0.0, 0.0, 1.0]]),
                                mag=1.0, color="#5d7aff")

        if is_wall:
            wall_dist = float(c.piano_wall_distance_ft_spin.value())
            if wall_dist <= 0:
                wall_dist = 1.0
            if c.piano_aim_combo.currentIndex() == 0:
                tilt_deg = float(c.piano_tilt_deg_spin.value())
                strip_center_z = fixture_h + math.tan(math.radians(tilt_deg)) * wall_dist
            else:
                strip_center_z = float(c.piano_target_height_ft_spin.value())
            strip_center_x = left_off

            wall_points = np.array([
                [strip_center_x - strip_len / 2.0, wall_dist, strip_center_z - strip_depth / 2.0],
                [strip_center_x + strip_len / 2.0, wall_dist, strip_center_z - strip_depth / 2.0],
                [strip_center_x + strip_len / 2.0, wall_dist, strip_center_z + strip_depth / 2.0],
                [strip_center_x - strip_len / 2.0, wall_dist, strip_center_z + strip_depth / 2.0],
            ], dtype=float)
            wall = pv.PolyData(wall_points)
            wall.faces = np.array([4, 0, 1, 2, 3], dtype=np.int64)
            self.actors["surface"] = self.plotter.add_mesh(
                wall, color="#3a6078", opacity=0.45,
            )
            self._draw_key_ticks_on_wall(
                strip_center_x, wall_dist, strip_center_z, strip_len,
            )
            self._add_text(
                f"Wall\n{ft_in_str(wall_dist)} away\nStrip {ft_in_str(strip_len)} long",
                (strip_center_x, wall_dist, strip_center_z + strip_depth * 0.9),
                color="#bfe4ff", size=12,
            )
            self._add_text(
                f"Fixture height\n{ft_in_str(fixture_h)}",
                (0.0, 0.0, fixture_h * 0.5),
                color="#ffe9a0", size=12,
            )
            self.plotter.add_mesh(
                pv.Line(tuple(fixture_pos), (0.0, wall_dist, fixture_h)),
                color="#ffb070", line_width=2,
            )
            self.plotter.add_mesh(
                pv.Line(
                    (strip_center_x - strip_len / 2.0, wall_dist, strip_center_z),
                    (strip_center_x + strip_len / 2.0, wall_dist, strip_center_z),
                ),
                color="#a0e0ff", line_width=2,
            )
            if abs(strip_center_x) > 0.001:
                self.plotter.add_arrows(
                    np.array([[0.0, wall_dist, strip_center_z]]),
                    np.array([[1.0 if strip_center_x >= 0 else -1.0, 0.0, 0.0]]),
                    mag=abs(strip_center_x), color="#ff9ad0",
                )
                self._add_text(
                    f"Left/right offset\n{ft_in_str(strip_center_x)}",
                    (strip_center_x * 0.5, wall_dist, strip_center_z + strip_depth * 1.2),
                    color="#ffc0e0", size=12,
                )
            target_point = (strip_center_x, wall_dist, strip_center_z)
            surface_normal = (0.0, -1.0, 0.0)
        else:
            ceil_h = float(c.piano_ceiling_height_ft_spin.value())
            fwd = float(c.piano_forward_distance_ft_spin.value())
            fb_off = float(c.piano_forward_back_offset_ft_spin.value())
            vertical = ceil_h - fixture_h
            if vertical <= 0.01:
                vertical = 0.01
            horizontal_center = fwd + fb_off
            strip_center_x = left_off
            strip_center_y = horizontal_center

            ceil_points = np.array([
                [strip_center_x - strip_len / 2.0, strip_center_y - strip_depth / 2.0, ceil_h],
                [strip_center_x + strip_len / 2.0, strip_center_y - strip_depth / 2.0, ceil_h],
                [strip_center_x + strip_len / 2.0, strip_center_y + strip_depth / 2.0, ceil_h],
                [strip_center_x - strip_len / 2.0, strip_center_y + strip_depth / 2.0, ceil_h],
            ], dtype=float)
            ceil = pv.PolyData(ceil_points)
            ceil.faces = np.array([4, 0, 1, 2, 3], dtype=np.int64)
            self.actors["surface"] = self.plotter.add_mesh(
                ceil, color="#3a6078", opacity=0.45,
            )
            self._draw_key_ticks_on_ceiling(
                strip_center_x, strip_center_y, ceil_h, strip_len,
            )
            self._add_text(
                f"Ceiling\n{ft_in_str(ceil_h)} above floor\nForward {ft_in_str(fwd)}",
                (strip_center_x, strip_center_y, ceil_h + 0.4),
                color="#bfe4ff", size=12,
            )
            self._add_text(
                f"Fixture height\n{ft_in_str(fixture_h)}",
                (0.0, 0.0, fixture_h * 0.5),
                color="#ffe9a0", size=12,
            )
            self.plotter.add_mesh(
                pv.Line(tuple(fixture_pos), (0.0, 0.0, ceil_h)),
                color="#a0e0ff", line_width=2,
            )
            if abs(horizontal_center) > 0.001:
                self.plotter.add_arrows(
                    np.array([[0.0, 0.0, ceil_h]]),
                    np.array([[0.0, 1.0 if horizontal_center >= 0 else -1.0, 0.0]]),
                    mag=abs(horizontal_center), color="#ff9ad0",
                )
                self._add_text(
                    f"Forward/back\n{ft_in_str(horizontal_center)}",
                    (0.0, horizontal_center * 0.5, ceil_h + 0.4),
                    color="#ffc0e0", size=12,
                )
            if abs(left_off) > 0.001:
                self.plotter.add_arrows(
                    np.array([[0.0, horizontal_center, ceil_h]]),
                    np.array([[1.0 if left_off >= 0 else -1.0, 0.0, 0.0]]),
                    mag=abs(left_off), color="#ff9ad0",
                )
                self._add_text(
                    f"Left/right\n{ft_in_str(left_off)}",
                    (left_off * 0.5, horizontal_center, ceil_h + 0.4),
                    color="#ffc0e0", size=12,
                )
            target_point = (strip_center_x, strip_center_y, ceil_h)
            surface_normal = (0.0, 0.0, -1.0)

        self._draw_beam_and_target(fixture_pos, target_point, surface_normal,
                                   is_wall, strip_len)

        if not self._camera_initialized:
            self._set_default_camera()

    def _draw_key_ticks_on_wall(self, center_x, wall_y, center_z, strip_len):
        if self.plotter is None:
            return
        try:
            import pyvista as pv
        except Exception:
            return
        c = self.controller
        n = max(1, int(c.piano_keys_spin.value()))
        start = int(c.piano_start_combo.currentIndex())
        depth = strip_len / 4.0
        x_left = center_x - strip_len / 2.0
        for i in range(n):
            frac = (i + 0.5) / n
            x = x_left + frac * strip_len
            a = (x, wall_y, center_z - depth / 2.0)
            b = (x, wall_y, center_z + depth / 2.0)
            self.plotter.add_mesh(pv.Line(a, b), color="#7fa0c0", line_width=1)
            midi = start + i
            if midi % 12 == 0:
                self._add_text(midi_note_name(midi),
                               (x, wall_y, center_z + depth / 2.0 + 0.4),
                               color="#cfd6e0", size=10)

    def _draw_key_ticks_on_ceiling(self, center_x, center_y, ceil_z, strip_len):
        if self.plotter is None:
            return
        try:
            import pyvista as pv
        except Exception:
            return
        c = self.controller
        n = max(1, int(c.piano_keys_spin.value()))
        start = int(c.piano_start_combo.currentIndex())
        depth = strip_len / 4.0
        x_left = center_x - strip_len / 2.0
        for i in range(n):
            frac = (i + 0.5) / n
            x = x_left + frac * strip_len
            a = (x, center_y - depth / 2.0, ceil_z)
            b = (x, center_y + depth / 2.0, ceil_z)
            self.plotter.add_mesh(pv.Line(a, b), color="#7fa0c0", line_width=1)
            midi = start + i
            if midi % 12 == 0:
                self._add_text(midi_note_name(midi),
                               (x, center_y + depth / 2.0 + 0.4, ceil_z + 0.2),
                               color="#cfd6e0", size=10)

    def _draw_beam_and_target(self, fixture_pos, default_target, surface_normal,
                              is_wall, strip_len):
        if self.plotter is None:
            return
        try:
            import pyvista as pv
        except Exception:
            return
        c = self.controller
        note = c.piano_last_note_midi if c.piano_last_note_midi is not None else None
        if note is not None:
            aim = c._compute_piano_aim(note)
        else:
            aim = None
        marker_radius = max(0.08, min(0.4, strip_len * 0.02))
        if aim is None:
            target = default_target
            color = "#3fa8ff"
            marker_color = "#3fa8ff"
            text_color = "#9ac8ff"
            text = "No active note"
        else:
            pan_deg, tilt_deg = aim
            fixture_h = float(c.piano_fixture_height_ft_spin.value())
            if is_wall:
                wall_dist = float(c.piano_wall_distance_ft_spin.value())
                target = (
                    math.tan(math.radians(pan_deg)) * wall_dist,
                    wall_dist,
                    fixture_h + math.tan(math.radians(tilt_deg)) * wall_dist,
                )
            else:
                ceil_h = float(c.piano_ceiling_height_ft_spin.value())
                fwd = float(c.piano_forward_distance_ft_spin.value())
                fb = float(c.piano_forward_back_offset_ft_spin.value())
                forward = fwd + fb
                x = math.tan(math.radians(pan_deg)) * forward
                target = (x, forward, ceil_h)
            color = "#ffd166"
            marker_color = "#00e0ff"
            text_color = "#c8f0ff"
            text = f"{midi_note_name(note)}   Pan {pan_deg:+.1f}° / Tilt {tilt_deg:+.1f}°"

        direction = np.array([
            target[0] - fixture_pos[0],
            target[1] - fixture_pos[1],
            target[2] - fixture_pos[2],
        ], dtype=float)
        length = float(np.linalg.norm(direction))
        if length > 0.001:
            unit = direction / length
            try:
                self.plotter.add_arrows(
                    np.array([fixture_pos]), np.array([unit]),
                    mag=length, color=color,
                )
            except Exception:
                pass
        try:
            self.plotter.add_mesh(
                pv.Sphere(radius=marker_radius, center=target),
                color=marker_color,
            )
        except Exception:
            pass
        self._add_text(text, (target[0], target[1], target[2] + 0.4),
                       color=text_color, size=12)

    def _tick(self):
        if not self.isVisible():
            return
        note = self.controller.piano_last_note_midi
        if note != self.last_note_midi:
            self.last_note_midi = note
            self._rebuild_scene()
            return
        sig = self._scene_signature()
        if sig != getattr(self, "_last_signature", None):
            self._last_signature = sig
            self._rebuild_scene()

    def _scene_signature(self):
        c = self.controller
        return (
            c.piano_surface_combo.currentIndex(),
            c.piano_fixture_height_ft_spin.value(),
            c.piano_wall_distance_ft_spin.value(),
            c.piano_ceiling_height_ft_spin.value(),
            c.piano_forward_distance_ft_spin.value(),
            c.piano_strip_length_ft_spin.value(),
            c.piano_left_offset_ft_spin.value(),
            c.piano_forward_back_offset_ft_spin.value(),
            c.piano_aim_combo.currentIndex(),
            c.piano_tilt_deg_spin.value(),
            c.piano_target_height_ft_spin.value(),
            c.piano_keys_spin.value(),
            c.piano_start_combo.currentIndex(),
            c.piano_last_note_midi,
        )

    def reset_camera(self):
        self._set_default_camera()

    def closeEvent(self, event):
        if self.timer is not None:
            self.timer.stop()
        try:
            if self.plotter is not None:
                self.plotter.close()
        except Exception:
            pass
        super().closeEvent(event)


class DMXController(QMainWindow):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("DMX Keybind Controller")
        self.resize(1280, 860)
        self.setMinimumSize(640, 560)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._normal_geometry = None

        self.config = load_config()
        self.values = self.config["values"]
        self.channel_names = self.config["channel_names"]
        self.bindings = self.config["bindings"]
        self.output = DMXOutput()

        self.audio_worker = None
        self.audio_reactive_active = False

        self.held_keys = set()
        self.held_bindings = {}
        self.key_accumulators = {}
        self.joystick_accumulators = {}
        self.toggled = set()
        self.sound_effect_cache = {}

        self.mic_active = False
        self.mic_users = set()
        self.voice_worker = None
        self.voice_worker_active = False
        self.voice_users = set()

        self.fade_users = set()

        self.controller = None
        self.controller_id = None
        self.controller_held_controls = set()
        self.controller_status_text = "Controller not connected"

        self.last_tick = time.monotonic()
        self.last_controller_check = 0.0
        self.table_updating = False

        self.controller_page = None
        self.controller_axis_labels = {}
        self.controller_axis_bars = {}
        self.controller_button_labels = {}
        self.controller_dpad_labels = {}
        self.sensitivity_sliders = {}
        self.sensitivity_labels = {}

        self.playlist = load_playlist()
        self.playlist_current_index = -1
        self.playlist_active = False
        self.queue_entries = []
        self._last_rewind_click_time = 0.0

        self.preloaded_entries = {}
        self.preload_workers = {}
        self.preload_failures = {}
        self.dj_mode_active = False

        self.deck_a_entry = None
        self.deck_b_entry = None
        self.deck_a_stem_levels = {}
        self.deck_b_stem_levels = {}
        self.deck_a_bpm = 0.0
        self.deck_b_bpm = 0.0
        self.deck_a_key = ""
        self.deck_a_camelot = ""
        self.deck_b_key = ""
        self.deck_b_camelot = ""
        self.deck_a_load_time = 0.0
        self.deck_b_load_time = 0.0
        self._deck_a_cue_point = None
        self._deck_b_cue_point = None

        self.dmx_source = "A"
        self.deck_a_frames = []
        self.deck_b_frames = []
        self.deck_a_fps = 20.0
        self.deck_b_fps = 20.0
        self.dj_dmx_active_deck = None
        self.dj_dmx_original_values = None
        self.dj_dmx_last_idx = -1

        self.piano_midi_port = None
        self.piano_port_name = ""
        self.piano_held_notes = {}
        self.piano_last_note_midi = None
        self.piano_last_pan_deg = 0.0
        self.piano_last_tilt_deg = 0.0
        self.piano_dimmer_current = 0.0
        self.piano_dimmer_target = 0.0
        self.piano_dimmer_target_time = 0.0
        self.piano_dimmer_rate = 0.0

        self.piano_mid_events = []
        self.piano_mid_time = 0.0
        self.piano_mid_duration = 0.0
        self.piano_mid_playing = False
        self.piano_mid_paused = False
        self.piano_mid_started_at = 0.0
        self.piano_mid_last_played = 0
        self.piano_mid_note_active = {}
        self.piano_mid_filename = ""
        self._piano_mid_last_tick = time.monotonic()

        self.preview_3d_window = None

        self.song_original_values = None
        self.song_paused = False
        self.song_paused_channels = set()
        self.song_paused_channel_values = {}

        self.key_backfill_worker = None

        try:
            import pygame
            pygame.init()
            pygame.joystick.init()
        except Exception:
            pass

        self.build_interface()
        self.refresh_bindings()
        self.refresh_ports()
        self.refresh_controller()

        self.control_timer = QTimer(self)
        self.control_timer.timeout.connect(self.tick)
        self.control_timer.start(20)

        self.output_timer = QTimer(self)
        self.output_timer.timeout.connect(self.send_frame)
        self.output_timer.start(40)

        self.progress_timer = QTimer(self)
        self.progress_timer.timeout.connect(self.update_progress_ui)
        self.progress_timer.start(33)

        self.piano_timer = QTimer(self)
        self.piano_timer.timeout.connect(self.tick_piano)
        self.piano_timer.start(15)

        saved_sf = self.config.get("piano_soundfont", "")
        if saved_sf:
            PIANO_SYNTH.set_soundfont(saved_sf)

        AUDIO_ENGINE.set_master_gain(int(self.config.get("volume_percent", 100)) / 100.0)
        self._apply_crossfader(self.config.get("crossfader", 500))

        QTimer.singleShot(800, self._start_key_backfill)

    def _start_key_backfill(self):
        if self.key_backfill_worker is not None:
            return
        needs_backfill = False
        for entry in self.playlist:
            show_path = entry.get("show_path", "")
            if show_path and Path(show_path).is_file():
                try:
                    payload = json.loads(Path(show_path).read_text(encoding="utf-8"))
                    if not payload.get("camelot_key"):
                        needs_backfill = True
                        break
                except Exception:
                    continue
        if not needs_backfill:
            return
        self.key_backfill_worker = KeyBackfillWorker(self.playlist, self)
        self.key_backfill_worker.status.connect(
            lambda msg: self.statusBar().showMessage(msg, 4000))
        self.key_backfill_worker.start()

    def build_interface(self):
        root = QWidget()
        root_layout = QVBoxLayout(root)
        top = QHBoxLayout()
        title = QLabel("DMX KEYBIND CONTROLLER")
        title.setStyleSheet("font-size: 18px; font-weight: bold;")
        top.addWidget(title)
        top.addStretch()
        save_button = QPushButton("Save All Settings")
        save_button.clicked.connect(self.save_settings)
        top.addWidget(save_button)
        root_layout.addLayout(top)
        self.tabs = QTabWidget()
        root_layout.addWidget(self.tabs)
        self.setCentralWidget(root)
        self.build_channels_tab()
        self.build_bindings_tab()
        self.build_output_tab()
        self.build_music_tab()
        self.build_scripts_tab()
        self.build_midi_piano_tab()
        self.build_mic_tab()

    def _scrollable_tab(self, page):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        page.setMinimumSize(0, 0)
        scroll.setWidget(page)
        return scroll

    def build_channels_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        controls = QHBoxLayout()
        controls.addWidget(QLabel("Channel:"))
        self.selected_channel = QSpinBox()
        self.selected_channel.setRange(1, CHANNELS)
        controls.addWidget(self.selected_channel)
        controls.addWidget(QLabel("Value:"))
        self.selected_value = QSpinBox()
        self.selected_value.setRange(0, 255)
        controls.addWidget(self.selected_value)
        self.channel_slider = QSlider(Qt.Orientation.Horizontal)
        self.channel_slider.setRange(0, 255)
        self.channel_slider.valueChanged.connect(self.selected_value.setValue)
        self.selected_value.valueChanged.connect(self.channel_slider.setValue)
        self.selected_value.lineEdit().returnPressed.connect(
            lambda: self.set_channel(self.selected_channel.value(), self.selected_value.value()))
        self.selected_value.editingFinished.connect(
            lambda: self.set_channel(self.selected_channel.value(), self.selected_value.value()))
        set_button = QPushButton("Set Value")
        set_button.clicked.connect(
            lambda: self.set_channel(self.selected_channel.value(), self.selected_value.value()))
        controls.addWidget(set_button)
        zero_button = QPushButton("Zero All")
        zero_button.clicked.connect(self.zero_all)
        controls.addWidget(zero_button)
        layout.addLayout(controls)
        layout.addWidget(self.channel_slider)
        self.channel_table = QTableWidget(CHANNELS, 3)
        self.channel_table.setHorizontalHeaderLabels(["DMX Channel", "Custom Name", "Value"])
        self.channel_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.channel_table.verticalHeader().setVisible(False)
        self.channel_table.setMinimumWidth(0)
        self.channel_table.setSizeAdjustPolicy(QTableWidget.SizeAdjustPolicy.AdjustToContents)
        for row in range(CHANNELS):
            number = QTableWidgetItem(str(row + 1))
            number.setFlags(number.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.channel_table.setItem(row, 0, number)
            self.channel_table.setItem(row, 1, QTableWidgetItem(self.channel_names[row]))
            self.channel_table.setItem(row, 2, QTableWidgetItem(str(self.values[row])))
        self.channel_table.itemChanged.connect(self.channel_table_changed)
        self.channel_table.cellClicked.connect(self.select_channel_row)
        layout.addWidget(self.channel_table)
        layout.addWidget(QLabel("Edit Custom Name to rename a channel. Use Save All Settings to preserve changes."))
        self.tabs.addTab(self._scrollable_tab(page), "Channels")

    def select_channel_row(self, row, column):
        self.selected_channel.setValue(row + 1)
        self.selected_value.setValue(self.values[row])

    def channel_table_changed(self, item):
        if self.table_updating:
            return
        row = item.row()
        if item.column() == 1:
            self.channel_names[row] = item.text().strip()
            self.update_binding_channel_labels()
        elif item.column() == 2:
            try:
                value = max(0, min(255, int(item.text())))
            except ValueError:
                value = self.values[row]
            self.set_channel(row + 1, value)

    def set_channel(self, channel, value):
        channel = max(1, min(CHANNELS, int(channel)))
        value = max(0, min(255, int(value)))
        if self.values[channel - 1] == value:
            return
        self.values[channel - 1] = value
        self.table_updating = True
        item = self.channel_table.item(channel - 1, 2)
        if item:
            item.setText(str(value))
        self.table_updating = False
        if self.selected_channel.value() == channel:
            self.selected_value.blockSignals(True)
            self.selected_value.setValue(value)
            self.selected_value.blockSignals(False)
            self.channel_slider.blockSignals(True)
            self.channel_slider.setValue(value)
            self.channel_slider.blockSignals(False)

    def zero_all(self):
        for channel in range(1, CHANNELS + 1):
            self.set_channel(channel, 0)

    def channel_label(self, channel):
        name = self.channel_names[channel - 1].strip()
        return f"{channel}: {name}" if name else f"Channel {channel}"

    def build_bindings_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        speed_form = QFormLayout()
        speed_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self.speed = QDoubleSpinBox()
        self.speed.setRange(0.1, 100.0)
        self.speed.setDecimals(1)
        self.speed.setSingleStep(1.0)
        self.speed.setSuffix(" steps/sec")
        self.speed.setValue(float(self.config.get("steps_per_second", 10.0)))
        speed_form.addRow("Button/key hold speed:", self.speed)
        self.deadzone = QSpinBox()
        self.deadzone.setRange(0, 40)
        self.deadzone.setSuffix("%")
        self.deadzone.setValue(int(self.config.get("deadzone", 12)))
        speed_form.addRow("Joystick deadzone:", self.deadzone)
        layout.addLayout(speed_form)
        self.controller_status = QLabel("Controller not connected")
        layout.addWidget(self.controller_status)
        controller_refresh = QPushButton("Refresh Controller")
        controller_refresh.setMinimumWidth(0)
        controller_refresh.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        controller_refresh.clicked.connect(self.refresh_controller)
        layout.addWidget(controller_refresh)
        layout.addWidget(QLabel(
            "Choose separate -X, +X, -Y, or +Y stick directions. Stick Continuous maps the selected "
            "direction from 0 to 255. Stick Hold Value changes in increments while pushed and holds its "
            "value at center. Trigger Continuous maps released to 0 and fully pressed to 255. "
            "Joystick and trigger Hold Value use their own sensitivity-based rate. Talk-through mixes "
            "the microphone into the output while held. Fade music out fades the song to silence while held "
            "and fades it back in over five seconds when released. Voice command listens for the listed "
            "words while held."))
        self.binding_table = QTableWidget(0, 5)
        self.binding_table.setHorizontalHeaderLabels(["Input Device", "Control", "DMX Channel", "Target Value", "Action"])
        header = self.binding_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        header.setMinimumSectionSize(35)
        header.setStretchLastSection(True)
        self.binding_table.setMinimumWidth(0)
        self.binding_table.setSizeAdjustPolicy(QTableWidget.SizeAdjustPolicy.AdjustToContents)
        self.binding_table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.binding_table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        layout.addWidget(self.binding_table, 1)
        buttons = QGridLayout()
        buttons.setHorizontalSpacing(6)
        buttons.setVerticalSpacing(6)
        add_button = QPushButton("Add Binding")
        add_button.clicked.connect(self.add_binding)
        buttons.addWidget(add_button, 0, 0)
        edit_button = QPushButton("Edit Selected")
        edit_button.clicked.connect(self.edit_binding)
        buttons.addWidget(edit_button, 0, 1)
        remove_button = QPushButton("Remove Selected")
        remove_button.clicked.connect(self.remove_binding)
        buttons.addWidget(remove_button, 1, 0, 1, 2)
        buttons.setColumnStretch(0, 1)
        buttons.setColumnStretch(1, 1)
        layout.addLayout(buttons)
        help_label = QLabel("Select Keyboard or Xbox Controller when creating a binding. "
                            "Use the Mic / Voice tab to choose the microphone and test talk-through.")
        help_label.setWordWrap(True)
        layout.addWidget(help_label)
        self.tabs.addTab(self._scrollable_tab(page), "Button Remapping")

    def refresh_bindings(self):
        self.binding_table.setRowCount(len(self.bindings))
        for row, binding in enumerate(self.bindings):
            device = binding.get("device", "Keyboard")
            control = binding.get("control", binding.get("key", ""))
            for col, text in enumerate([device, control]):
                item = QTableWidgetItem(str(text))
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.binding_table.setItem(row, col, item)
            channel_combo = QComboBox()
            channel_combo.setMinimumWidth(0)
            channel_combo.addItems([self.channel_label(ch) for ch in range(1, CHANNELS + 1)])
            channel_combo.setCurrentIndex(max(0, min(CHANNELS - 1, binding["channel"] - 1)))
            channel_combo.setToolTip("Select the DMX channel for this binding.")
            channel_combo.currentIndexChanged.connect(
                lambda index, r=row: self.change_binding_channel(r, index + 1))
            self.binding_table.setCellWidget(row, 2, channel_combo)
            value_spin = QSpinBox()
            value_spin.setMinimumWidth(0)
            value_spin.setRange(0, 255)
            value_spin.setValue(binding["value"])
            value_spin.valueChanged.connect(
                lambda value, r=row: self.change_binding_value(r, value))
            self.binding_table.setCellWidget(row, 3, value_spin)
            action_combo = QComboBox()
            action_combo.setMinimumWidth(0)
            action_combo.addItems(ACTIONS)
            action_combo.setCurrentIndex(max(0, min(len(ACTIONS) - 1, int(binding.get("mode", 0)))))
            action_combo.setToolTip("Changing this menu immediately changes the binding action.")
            action_combo.currentIndexChanged.connect(
                lambda index, r=row: self.change_binding_action(r, index))
            self.binding_table.setCellWidget(row, 4, action_combo)

    def update_binding_channel_labels(self):
        for row in range(min(len(self.bindings), self.binding_table.rowCount())):
            combo = self.binding_table.cellWidget(row, 2)
            if not isinstance(combo, QComboBox):
                continue
            channel = self.bindings[row]["channel"]
            combo.blockSignals(True)
            combo.clear()
            combo.addItems([self.channel_label(ch) for ch in range(1, CHANNELS + 1)])
            combo.setCurrentIndex(channel - 1)
            combo.blockSignals(False)

    def change_binding_channel(self, row, channel):
        if row < len(self.bindings):
            self.bindings[row]["channel"] = channel

    def change_binding_value(self, row, value):
        if row < len(self.bindings):
            self.bindings[row]["value"] = value

    def change_binding_action(self, row, mode):
        if row < len(self.bindings):
            self.bindings[row]["mode"] = mode

    def add_binding(self):
        dialog = BindingDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        binding = dialog.get_binding()
        if not binding["control"]:
            QMessageBox.warning(self, "Missing input", "Choose or press an input first.")
            return
        self.bindings.append(binding)
        self.refresh_bindings()

    def edit_binding(self):
        row = self.binding_table.currentRow()
        if row < 0:
            return
        dialog = BindingDialog(self, self.bindings[row])
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        binding = dialog.get_binding()
        if not binding["control"]:
            QMessageBox.warning(self, "Missing input", "Choose or press an input first.")
            return
        self.bindings[row] = binding
        self.refresh_bindings()

    def remove_binding(self):
        row = self.binding_table.currentRow()
        if row >= 0:
            self.bindings.pop(row)
            self.refresh_bindings()

    def keyPressEvent(self, event):
        if event.isAutoRepeat():
            return
        key = QKeySequence(event.keyCombination()).toString()
        if key:
            self.handle_key_press(key)
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if event.isAutoRepeat():
            return
        key = QKeySequence(event.keyCombination()).toString()
        if key:
            self.handle_key_release(key)
        super().keyReleaseEvent(event)

    def handle_key_press(self, key):
        if key in self.held_keys:
            return
        self.held_keys.add(key)
        for index, binding in enumerate(self.bindings):
            if (binding.get("device", "Keyboard") == "Keyboard"
                    and binding.get("control", binding.get("key", "")) == key):
                self.binding_pressed(index)

    def handle_key_release(self, key):
        self.held_keys.discard(key)
        for index, binding in enumerate(self.bindings):
            if (binding.get("device", "Keyboard") == "Keyboard"
                    and binding.get("control", binding.get("key", "")) == key):
                self.binding_released(index)

    def binding_pressed(self, index):
        if index >= len(self.bindings):
            return
        binding = self.bindings[index]
        channel = binding["channel"]
        value = binding["value"]
        mode = binding.get("mode", 0)

        if mode == 0:
            self.set_channel(channel, value)
        elif mode == 1:
            self.set_channel(channel, value)
        elif mode == 2:
            if index in self.toggled:
                self.toggled.remove(index)
                self.set_channel(channel, 0)
            else:
                self.toggled.add(index)
                self.set_channel(channel, value)
        elif mode in (3, 4):
            self.held_bindings[index] = True
            self.key_accumulators[index] = 0.0
        elif mode == 10:
            amount = max(1, min(255, int(binding.get("amount", 10))))
            self.set_channel(channel, min(255, self.values[channel - 1] + amount))
        elif mode == 11:
            amount = max(1, min(255, int(binding.get("amount", 10))))
            self.set_channel(channel, max(0, self.values[channel - 1] - amount))
        elif mode == 12:
            self.set_channel(channel, min(255, self.values[channel - 1] + 1))
        elif mode == 13:
            self.set_channel(channel, max(0, self.values[channel - 1] - 1))
        elif mode == 14:
            handled = False
            if self.song_original_values is not None:
                if channel in self.song_paused_channels:
                    self.song_paused_channels.discard(channel)
                    self.song_paused_channel_values.pop(channel, None)
                    self.statusBar().showMessage(
                        f"Script control resumed on DMX channel {channel}.", 2500)
                else:
                    self.song_paused_channels.add(channel)
                    self.song_paused_channel_values.pop(channel, None)
                    self.statusBar().showMessage(
                        f"Script control paused on DMX channel {channel}. You can still control it manually.", 3500)
                handled = True
            if self.dj_mode_active:
                src = AUDIO_ENGINE.deck_a if self.dmx_source == "A" else AUDIO_ENGINE.deck_b
                if src.audio is not None:
                    src.toggle_playing()
                    state = "resumed" if src.is_playing() else "paused"
                    self.statusBar().showMessage(
                        f"Deck {self.dmx_source} {state}.", 2500)
                    handled = True
            if not handled:
                self.statusBar().showMessage(
                    "Start the generated show or DJ mode before pausing a channel.", 2500)
        elif mode in (15, 16):
            self.play_binding_sound(index, loop=(mode == 16))
        elif mode == 17:
            self.start_talk_through(index)
        elif mode == 18:
            self.start_fade_out(index)
        elif mode == 19:
            self.start_voice_command(index)

    def play_binding_sound(self, index, loop=False):
        if index >= len(self.bindings):
            return
        path = self.bindings[index].get("sound_path", "")
        if not path or not Path(path).is_file():
            self.statusBar().showMessage("Sound effect file is missing. Edit this binding and choose a valid file.", 5000)
            return
        try:
            data, rate = sf.read(path, dtype="float32", always_2d=True)
            if data.size == 0:
                raise RuntimeError("Empty sound effect file.")
            AUDIO_ENGINE.play_effect(data, rate, loop=loop, volume=1.0)
            self.sound_effect_cache[index] = path
        except Exception as exc:
            self.statusBar().showMessage(f"Sound effect failed: {exc}", 6000)

    def start_talk_through(self, index):
        self.mic_users.add(index)
        self._refresh_mic_state()
        if self.mic_active:
            self.statusBar().showMessage("Talk-through ON. Speak into the microphone.", 3000)

    def start_fade_out(self, index):
        self.fade_users.add(index)
        AUDIO_ENGINE.begin_fade_out()
        self.statusBar().showMessage("Music fading out while held…", 2000)

    def start_voice_command(self, index):
        self.voice_users.add(index)
        self._refresh_voice_state()
        if self.voice_worker_active:
            words = ", ".join(self.bindings[index].get("voice_words", []))
            self.statusBar().showMessage(f"Listening for voice command(s): {words or '(none configured)'}", 4000)

    def binding_released(self, index):
        if index >= len(self.bindings):
            return
        binding = self.bindings[index]
        mode = binding.get("mode", 0)
        if mode == 1:
            self.set_channel(binding["channel"], 0)
        if mode in (3, 4):
            self.held_bindings.pop(index, None)
            self.key_accumulators.pop(index, None)
        if mode == 16:
            AUDIO_ENGINE.stop_all_effects()
        if mode == 17:
            self.mic_users.discard(index)
            self._refresh_mic_state()
        if mode == 18:
            self.fade_users.discard(index)
            if not self.fade_users:
                AUDIO_ENGINE.begin_fade_in()
                self.statusBar().showMessage("Music fading back in over 5 seconds.", 3000)
        if mode == 19:
            self.voice_users.discard(index)
            self._refresh_voice_state()

    def _refresh_mic_state(self):
        should_be_active = bool(self.mic_users)
        if should_be_active == self.mic_active:
            return
        self.mic_active = should_be_active
        try:
            if self.mic_active:
                AUDIO_ENGINE.set_mic_enabled(True)
                MIC_INPUT.start(self.config.get("mic_input_device", ""))
            else:
                AUDIO_ENGINE.set_mic_enabled(False)
                if not self.voice_worker_active:
                    MIC_INPUT.stop()
        except Exception as exc:
            self.mic_active = False
            AUDIO_ENGINE.set_mic_enabled(False)
            self.statusBar().showMessage(f"Microphone could not be started: {exc}", 6000)

    def _refresh_voice_state(self):
        should_be_active = bool(self.voice_users)
        if should_be_active == self.voice_worker_active:
            if self.voice_worker is not None:
                self.voice_worker.set_paused(not should_be_active)
            return
        self.voice_worker_active = should_be_active
        if not self.voice_worker_active:
            if self.voice_worker is not None:
                self.voice_worker.stop()
                self.voice_worker = None
            return
        try:
            MIC_INPUT.start(self.config.get("mic_input_device", ""))
        except Exception as exc:
            self.voice_worker_active = False
            self.statusBar().showMessage(f"Microphone could not be started: {exc}", 6000)
            return
        self.voice_worker = VoiceCommandWorker(VOSK_MODEL_DIR, self)
        self.voice_worker.word_detected.connect(self.handle_voice_word)
        self.voice_worker.status.connect(self._voice_status)
        self.voice_worker.set_paused(False)
        self.voice_worker.start()

    def _voice_status(self, message):
        self.statusBar().showMessage(message, 5000)

    def handle_voice_word(self, text):
        if not text:
            return
        text_lower = text.lower()
        best_match = None
        best_word_len = 0
        for index in list(self.voice_users):
            if index >= len(self.bindings):
                continue
            for word in self.bindings[index].get("voice_words", []):
                if word and word in text_lower and len(word) > best_word_len:
                    best_match = (index, word)
                    best_word_len = len(word)
        if best_match is None:
            return
        index, word = best_match
        binding = self.bindings[index]
        self.set_channel(binding["channel"], binding["value"])
        self.statusBar().showMessage(
            f"Voice command '{word}' → channel {binding['channel']} = {binding['value']}", 3000)

    # ---------------- MUSIC TAB ----------------

    def build_music_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        heading = QLabel("MUSIC PLAYLIST")
        heading.setStyleSheet("font-size: 17px; font-weight: bold;")
        layout.addWidget(heading)

        search_row = QHBoxLayout()
        self.dj_mode_button = QPushButton("Enter DJ Mode")
        self.dj_mode_button.setToolTip(
            "Switch to DJ mode: two interactive waveform decks, crossfader, master volume, and sync buttons.")
        self.dj_mode_button.clicked.connect(self._toggle_dj_mode)
        search_row.addWidget(self.dj_mode_button)

        self.music_search_edit = QLineEdit()
        self.music_search_edit.setPlaceholderText("Search songs…")
        self.music_search_edit.setClearButtonEnabled(True)
        self.music_search_edit.textChanged.connect(self._on_music_search)
        search_row.addWidget(self.music_search_edit, 1)

        self.music_view_button = QPushButton("View")
        self.music_view_button.setToolTip("Change playlist view mode and text size.")
        self.music_view_button.clicked.connect(self._show_view_menu)
        search_row.addWidget(self.music_view_button)
        layout.addLayout(search_row)

        self.music_middle_stack = QStackedWidget()

        playlist_page = QWidget()
        playlist_layout = QVBoxLayout(playlist_page)
        playlist_layout.setContentsMargins(0, 0, 0, 0)
        self.playlist_widget = PlaylistListWidget()
        self.playlist_widget.itemDoubleClicked.connect(lambda _item: self.play_playlist_entry())
        self.playlist_widget.empty_context_requested.connect(self._show_empty_music_menu)
        self.playlist_widget.item_context_requested.connect(self._show_item_music_menu)
        self._apply_playlist_view_mode(self.config.get("music_view_mode", "list"))
        self._apply_playlist_font_size(self.config.get("music_font_size", 13))
        playlist_layout.addWidget(self.playlist_widget)
        self.music_middle_stack.addWidget(playlist_page)

        dj_page = QWidget()
        dj_layout = QGridLayout(dj_page)
        dj_layout.setContentsMargins(0, 0, 0, 0)
        dj_layout.setSpacing(8)
        dj_layout.setColumnStretch(0, 3)
        dj_layout.setColumnStretch(1, 4)
        dj_layout.setColumnStretch(2, 3)

        waveform_box = QGroupBox("Live Waveforms")
        waveform_layout = QVBoxLayout(waveform_box)
        waveform_layout.setContentsMargins(4, 4, 4, 4)
        waveform_layout.setSpacing(4)

        self.deck_a = DJDeck("#60a5fa", "Deck A", "SYNC B", show_waveform=False)
        self.deck_a.play_pause_clicked.connect(self._deck_a_play_pause)
        self.deck_a.cue_clicked.connect(self._deck_a_cue)
        self.deck_a.lineup_clicked.connect(lambda: self._lineup_deck("A"))
        self.deck_a.dropped.connect(self._deck_a_dropped)
        self.deck_a.scrub_moved.connect(self._deck_a_scrub)
        self.deck_a.seek_requested.connect(self._deck_a_seek)
        self.deck_a.stem_mute_changed.connect(self._deck_a_stem_mute)
        self.deck_a.stem_seek_requested.connect(
            lambda _key, secs: self._deck_a_seek(secs))
        self.deck_a.pitch_changed.connect(self._deck_a_pitch_changed)
        self.deck_a.sync_clicked.connect(self._sync_a_to_b)
        self.deck_a.empty_clicked.connect(lambda: self._empty_deck("A"))

        deck_a_wave_row = QHBoxLayout()
        deck_a_wave_row.setSpacing(4)
        deck_a_wave_row.addWidget(self.deck_a.waveform, 1)
        waveform_layout.addLayout(deck_a_wave_row)
        self.deck_a.waveform.setParent(waveform_box)
        self.deck_a.waveform.show()

        self.deck_b = DJDeck("#22c55e", "Deck B", "SYNC A", show_waveform=False)
        self.deck_b.play_pause_clicked.connect(self._deck_b_play_pause)
        self.deck_b.cue_clicked.connect(self._deck_b_cue)
        self.deck_b.lineup_clicked.connect(lambda: self._lineup_deck("B"))
        self.deck_b.dropped.connect(self._deck_b_dropped)
        self.deck_b.scrub_moved.connect(self._deck_b_scrub)
        self.deck_b.seek_requested.connect(self._deck_b_seek)
        self.deck_b.stem_mute_changed.connect(self._deck_b_stem_mute)
        self.deck_b.stem_seek_requested.connect(
            lambda _key, secs: self._deck_b_seek(secs))
        self.deck_b.pitch_changed.connect(self._deck_b_pitch_changed)
        self.deck_b.sync_clicked.connect(self._sync_b_to_a)
        self.deck_b.empty_clicked.connect(lambda: self._empty_deck("B"))

        deck_b_wave_row = QHBoxLayout()
        deck_b_wave_row.setSpacing(4)
        deck_b_wave_row.addWidget(self.deck_b.waveform, 1)
        waveform_layout.addLayout(deck_b_wave_row)
        self.deck_b.waveform.setParent(waveform_box)
        self.deck_b.waveform.show()

        dj_layout.addWidget(waveform_box, 0, 0, 1, 3)

        dj_center = QWidget()
        dj_center_layout = QVBoxLayout(dj_center)
        dj_center_layout.setContentsMargins(0, 0, 0, 0)
        dj_center_layout.setSpacing(6)

        self.dmx_source_label = QLabel("DMX source: A")
        self.dmx_source_label.setStyleSheet(
            "font-size: 11px; font-weight: bold; color: #9aa4b2;")
        self.dmx_source_label.setToolTip(
            "The deck with the majority crossfade gain drives the DMX show automatically.")
        dj_center_layout.addWidget(self.dmx_source_label)
        self.dmx_source_a = None
        self.dmx_source_b = None

        dj_search_row = QHBoxLayout()
        self.dj_search_edit = QLineEdit()
        self.dj_search_edit.setPlaceholderText("Search DJ playlist…")
        self.dj_search_edit.setClearButtonEnabled(True)
        self.dj_search_edit.textChanged.connect(self._on_dj_search)
        dj_search_row.addWidget(self.dj_search_edit, 1)
        self.dj_recommend_button = QPushButton("Recommend")
        self.dj_recommend_button.setCheckable(True)
        self.dj_recommend_button.setToolTip(
            "Toggle recommendation ranking. When on, the list is sorted by BPM and key "
            "compatibility against the deck that has had a song loaded the longest. "
            "When off, the list shows the normal playlist.")
        self.dj_recommend_button.toggled.connect(self._on_recommend_toggled)
        dj_search_row.addWidget(self.dj_recommend_button)
        dj_center_layout.addLayout(dj_search_row)

        self.dj_playlist_widget = PlaylistListWidget()
        self.dj_playlist_widget.itemDoubleClicked.connect(
            lambda _item: self._play_dj_selected())
        self.dj_playlist_widget.empty_context_requested.connect(self._show_empty_music_menu)
        self.dj_playlist_widget.item_context_requested.connect(self._show_item_music_menu)
        self.dj_playlist_widget.setWordWrap(True)
        self._dj_rich_delegate = RichTextDelegate(self.dj_playlist_widget)
        self.dj_playlist_widget.setItemDelegate(self._dj_rich_delegate)
        dj_center_layout.addWidget(self.dj_playlist_widget, 1)

        cross_row = QHBoxLayout()
        cross_label_a = QLabel("A")
        cross_label_a.setStyleSheet("font-weight: bold; color: #60a5fa;")
        cross_row.addWidget(cross_label_a)
        self.crossfader = QSlider(Qt.Orientation.Horizontal)
        self.crossfader.setRange(0, 1000)
        self.crossfader.setValue(int(self.config.get("crossfader", 500)))
        self.crossfader.setToolTip(
            "Equal-power crossfader. Auto-switches DMX source to the dominant deck past the midpoint.")
        self.crossfader.valueChanged.connect(self._on_crossfader_changed)
        cross_row.addWidget(self.crossfader, 1)
        cross_label_b = QLabel("B")
        cross_label_b.setStyleSheet("font-weight: bold; color: #22c55e;")
        cross_row.addWidget(cross_label_b)
        dj_center_layout.addLayout(cross_row)

        master_row = QHBoxLayout()
        master_label = QLabel("Master")
        master_label.setStyleSheet("font-weight: bold;")
        master_row.addWidget(master_label)
        self.dj_master_slider = QSlider(Qt.Orientation.Horizontal)
        self.dj_master_slider.setRange(0, 100)
        self.dj_master_slider.setValue(int(self.config.get("volume_percent", 100)))
        self.dj_master_slider.setToolTip("Master song volume. Shared with the Mic / Voice tab.")
        self.dj_master_slider.valueChanged.connect(self._on_master_slider_changed)
        master_row.addWidget(self.dj_master_slider, 1)
        self.dj_master_label = QLabel(f"{int(self.config.get('volume_percent', 100))}%")
        self.dj_master_label.setMinimumWidth(38)
        self.dj_master_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        master_row.addWidget(self.dj_master_label)
        dj_center_layout.addLayout(master_row)

        dj_center.setMinimumWidth(260)
        dj_layout.addWidget(dj_center, 1, 1)

        dj_layout.addWidget(self.deck_a, 1, 0)
        dj_layout.addWidget(self.deck_b, 1, 2)

        dj_layout.setRowStretch(0, 0)
        dj_layout.setRowStretch(1, 1)

        self.music_middle_stack.addWidget(dj_page)
        self.music_middle_stack.setCurrentIndex(0)
        layout.addWidget(self.music_middle_stack, 2)

        self.music_bottom_container = QWidget()
        self.music_bottom_container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.music_bottom_container.setMinimumHeight(220)
        self.music_bottom_container.setMaximumHeight(320)
        bottom_layout = QHBoxLayout(self.music_bottom_container)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.setSpacing(8)

        queue_box = QGroupBox("Up Next")
        queue_layout = QVBoxLayout(queue_box)
        self.queue_widget = QueueListWidget()
        self.queue_widget.queue_reordered.connect(self._on_queue_reordered)
        self.queue_widget.queue_request_play.connect(self._on_queue_play)
        self.queue_widget.setToolTip(
            "Next songs. The top four are preloaded and show a green check when ready. "
            "Drag to reorder; the top is next. Right-click to play now.")
        queue_layout.addWidget(self.queue_widget)
        bottom_layout.addWidget(queue_box, 1)

        center_box = QGroupBox("Now Playing")
        center_layout = QVBoxLayout(center_box)
        self.music_now_selected_label = QLabel("—")
        self.music_now_selected_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.music_now_selected_label.setStyleSheet("font-weight: bold;")
        self.music_now_selected_label.setWordWrap(True)
        center_layout.addWidget(self.music_now_selected_label)

        transport = QHBoxLayout()
        transport.addStretch()
        self.music_rewind_button = QPushButton("⏪")
        self.music_rewind_button.setFixedSize(44, 44)
        self.music_rewind_button.setToolTip(
            "Replay current song. Press twice quickly to go to the previous song.")
        self.music_rewind_button.clicked.connect(self._music_rewind)
        transport.addWidget(self.music_rewind_button)

        self.music_play_pause_button = QPushButton("▶")
        self.music_play_pause_button.setFixedSize(64, 64)
        self.music_play_pause_button.clicked.connect(self._music_toggle_play)
        transport.addWidget(self.music_play_pause_button)

        self.music_forward_button = QPushButton("⏩")
        self.music_forward_button.setFixedSize(44, 44)
        self.music_forward_button.setToolTip("Skip to the next song.")
        self.music_forward_button.clicked.connect(self._music_forward)
        transport.addWidget(self.music_forward_button)
        transport.addStretch()
        center_layout.addLayout(transport)

        self.music_position_label = QLabel("00:00 / 00:00")
        self.music_position_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        center_layout.addWidget(self.music_position_label)

        self.music_progress = QProgressBar()
        self.music_progress.setRange(0, 1000)
        self.music_progress.setTextVisible(False)
        center_layout.addWidget(self.music_progress)
        bottom_layout.addWidget(center_box, 1)

        stems_box = QGroupBox("Stems")
        stems_layout = QVBoxLayout(stems_box)
        self.music_stem_visualizers = {}
        self.music_stem_mute_boxes = {}
        for key, label, color in (
            ("vocals", "Vocals", "#60a5fa"),
            ("drums", "Drums", "#f59e0b"),
            ("bass", "Bass", "#22c55e"),
            ("other", "Other", "#c084fc"),
        ):
            col = QVBoxLayout()
            vis = StemVisualizer(color)
            vis.seek_requested.connect(self.seek_generated_show)
            col.addWidget(vis, 1)
            label_widget = QLabel(label)
            label_widget.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            label_widget.setStyleSheet("font-weight: bold;")
            col.addWidget(label_widget)
            mute = QCheckBox("Mute")
            mute.setEnabled(False)
            mute.toggled.connect(lambda muted, k=key: self._music_stem_mute_changed(k, muted))
            col.addWidget(mute)
            stems_layout.addLayout(col)
            self.music_stem_visualizers[key] = vis
            self.music_stem_mute_boxes[key] = mute
        bottom_layout.addWidget(stems_box, 1)

        layout.addWidget(self.music_bottom_container, 1)

        self.playlist_status = QLabel("No songs in the playlist yet.")
        self.playlist_status.setWordWrap(True)
        layout.addWidget(self.playlist_status)

        self._last_rewind_click_time = 0.0

        self.tabs.addTab(self._scrollable_tab(page), "Music")
        self.refresh_playlist_ui()
        self._refresh_queue_ui()
        self._schedule_preloads()

    def _on_crossfader_changed(self, value):
        self._apply_crossfader(value)
        self.config["crossfader"] = int(value)

    def _apply_crossfader(self, value):
        t = max(0.0, min(1.0, float(value) / 1000.0))
        gain_a = math.cos(t * math.pi / 2.0)
        gain_b = math.sin(t * math.pi / 2.0)
        AUDIO_ENGINE.deck_a.set_crossfade_gain(gain_a)
        AUDIO_ENGINE.deck_b.set_crossfade_gain(gain_b)

        if AUDIO_ENGINE.deck_a.audio is not None and not AUDIO_ENGINE.deck_a.is_playing():
            if gain_a >= 0.10 and AUDIO_ENGINE.deck_b.is_playing():
                AUDIO_ENGINE.deck_a.set_playing(True)
        if AUDIO_ENGINE.deck_b.audio is not None and not AUDIO_ENGINE.deck_b.is_playing():
            if gain_b >= 0.10 and AUDIO_ENGINE.deck_a.is_playing():
                AUDIO_ENGINE.deck_b.set_playing(True)

        dominant = "B" if gain_b > gain_a else "A"
        if self.dj_mode_active and dominant != self.dmx_source:
            other_frames = self.deck_b_frames if dominant == "B" else self.deck_a_frames
            other_src = AUDIO_ENGINE.deck_b if dominant == "B" else AUDIO_ENGINE.deck_a
            if other_frames and other_src.audio is not None:
                self.dmx_source = dominant
                self._update_dmx_source_label()
                self._switch_dj_dmx_to(dominant)

    def _on_master_slider_changed(self, value):
        self._set_volume_everywhere(int(value))

    def _set_volume_everywhere(self, value):
        value = max(0, min(100, int(value)))
        self.config["volume_percent"] = value
        AUDIO_ENGINE.set_master_gain(value / 100.0)
        if hasattr(self, "dj_master_slider"):
            self.dj_master_slider.blockSignals(True)
            self.dj_master_slider.setValue(value)
            self.dj_master_slider.blockSignals(False)
            self.dj_master_label.setText(f"{value}%")
        if hasattr(self, "volume_slider"):
            self.volume_slider.blockSignals(True)
            self.volume_slider.setValue(value)
            self.volume_slider.blockSignals(False)
        if hasattr(self, "volume_label"):
            self.volume_label.setText(f"Song volume: {value}%")

    def _toggle_dj_mode(self):
        self.dj_mode_active = not self.dj_mode_active
        if self.dj_mode_active:
            self._was_maximized = self.isMaximized()
            self._was_fullscreen = self.isFullScreen()
            self._normal_geometry = self.geometry()
            if not (self._was_maximized or self._was_fullscreen):
                self.resize(1180, 760)
            try:
                AUDIO_ENGINE.open(DECK_RATE, channels=2)
            except Exception as exc:
                self.statusBar().showMessage(
                    f"Could not open 44.1 kHz output ({exc}); using device rate.", 4000)
            AUDIO_ENGINE.set_master_gain(
                int(self.config.get("volume_percent", 100)) / 100.0)
            self._apply_crossfader(self.crossfader.value())
            if self.song_original_values is not None:
                self.stop_generated_show()
            self.dj_mode_button.setText("Exit DJ Mode")
            self.music_middle_stack.setCurrentIndex(1)
            self.music_bottom_container.setVisible(False)
            self.music_search_edit.setVisible(False)
            self.music_view_button.setVisible(False)
            self._rebuild_dj_playlist()
            self._update_dmx_source_label()
            self._switch_dj_dmx_to(self.dmx_source)
        else:
            self.dj_mode_button.setText("Enter DJ Mode")
            self.music_middle_stack.setCurrentIndex(0)
            self.music_bottom_container.setVisible(True)
            self.music_search_edit.setVisible(True)
            self.music_view_button.setVisible(True)
            self._end_dj_dmx(try_auto_advance=False)
            AUDIO_ENGINE.deck_a.set_playing(False)
            AUDIO_ENGINE.deck_b.set_playing(False)
            if getattr(self, "_was_fullscreen", False):
                self.showFullScreen()
            elif getattr(self, "_was_maximized", False):
                self.showMaximized()
            elif self._normal_geometry is not None:
                self.setGeometry(self._normal_geometry)
            self._normal_geometry = None
            self._was_maximized = False
            self._was_fullscreen = False
            try:
                AUDIO_ENGINE.open(DECK_RATE, channels=2)
            except Exception:
                pass

    def _rebuild_dj_playlist(self, query=""):
        self.dj_playlist_widget.clear()
        query = (query or "").strip().lower()
        for idx, entry in enumerate(self.playlist):
            name = entry.get("name", "Untitled")
            if query and query not in name.lower():
                continue
            audio_path = entry.get("audio_path", "")
            show_path = entry.get("show_path", "")
            if not audio_path or not Path(audio_path).is_file():
                continue
            if not show_path or not Path(show_path).is_file():
                continue
            dur = float(entry.get("duration", 0.0))
            it = QListWidgetItem(f"{name}   ({int(dur // 60):02d}:{int(dur % 60):02d})")
            it.setData(Qt.ItemDataRole.UserRole, idx)
            self.dj_playlist_widget.addItem(it)

    def _on_dj_search(self, text):
        if getattr(self, "dj_recommend_button", None) is not None and self.dj_recommend_button.isChecked():
            self._recommend_dj_tracks()
        else:
            self._rebuild_dj_playlist(query=text)

    def _on_recommend_toggled(self, checked):
        if checked:
            self.dj_recommend_button.setText("Normal")
            self._recommend_dj_tracks()
        else:
            self.dj_recommend_button.setText("Recommend")
            self._rebuild_dj_playlist(query=self.dj_search_edit.text())

    def _update_dmx_source_label(self):
        if hasattr(self, "dmx_source_label"):
            self.dmx_source_label.setText(f"DMX source: {self.dmx_source}")

    def _reference_deck(self):
        a_ready = self.deck_a_load_time > 0 and self.deck_a_bpm > 0
        b_ready = self.deck_b_load_time > 0 and self.deck_b_bpm > 0
        if not a_ready and not b_ready:
            return None
        if not a_ready:
            return "B"
        if not b_ready:
            return "A"
        return "A" if self.deck_a_load_time <= self.deck_b_load_time else "B"

    def _recommend_dj_tracks(self):
        ref = self._reference_deck()
        if ref is None:
            self.statusBar().showMessage(
                "Load a song onto Deck A or Deck B first to recommend matches.", 4000)
            return
        if ref == "A":
            ref_bpm = self.deck_a_bpm
            ref_camelot = self.deck_a_camelot
            ref_name = self.deck_a_entry.get("name", "Deck A") if self.deck_a_entry else "Deck A"
        else:
            ref_bpm = self.deck_b_bpm
            ref_camelot = self.deck_b_camelot
            ref_name = self.deck_b_entry.get("name", "Deck B") if self.deck_b_entry else "Deck B"
        if ref_bpm <= 0:
            self.statusBar().showMessage(
                f"Deck {ref} has no analyzed BPM. Run Analyze & Generate Show first.", 4000)
            return

        scored = []
        for idx, entry in enumerate(self.playlist):
            if ref == "A" and self.deck_a_entry and entry is self.deck_a_entry:
                continue
            if ref == "B" and self.deck_b_entry and entry is self.deck_b_entry:
                continue

            cand_bpm = 0.0
            cand_camelot = entry.get("camelot_key", "")
            audio_path = entry.get("audio_path", "")
            show_path = entry.get("show_path", "")
            if not audio_path or not Path(audio_path).is_file():
                continue
            if not show_path or not Path(show_path).is_file():
                continue
            if show_path and Path(show_path).is_file():
                try:
                    payload = json.loads(Path(show_path).read_text(encoding="utf-8"))
                    cand_bpm = float(payload.get("estimated_bpm", 0.0) or 0.0)
                    cand_camelot = payload.get("camelot_key", cand_camelot) or cand_camelot
                    entry["camelot_key"] = cand_camelot
                    entry["estimated_key"] = payload.get("estimated_key", "")
                except Exception:
                    pass

            if cand_bpm <= 0:
                continue

            ratios = [
                abs((cand_bpm / ref_bpm) - 1.0),
                abs((cand_bpm * 2.0 / ref_bpm) - 1.0),
                abs((cand_bpm / 2.0 / ref_bpm) - 1.0),
            ]
            best_bpm_err = min(ratios)
            bpm_score = max(0.0, 1.0 - best_bpm_err)
            key_score = camelot_compatibility(ref_camelot, cand_camelot)
            compat = 0.6 * bpm_score + 0.4 * key_score
            scored.append((compat, idx, entry, cand_bpm, cand_camelot,
                           bpm_score, key_score))

        if not scored:
            self.statusBar().showMessage(
                "No other analyzed tracks found. Analyze songs first.", 4000)
            return

        scored.sort(key=lambda x: x[0], reverse=True)
        self.dj_playlist_widget.clear()
        for compat, idx, entry, cand_bpm, cand_camelot, bpm_score, key_score in scored:
            name = entry.get("name", "Untitled")
            dur = float(entry.get("duration", 0.0))
            key_part = f"  {cand_camelot}" if cand_camelot else ""
            dur_str = f"{int(dur // 60):02d}:{int(dur % 60):02d}"
            bpm_pct = int(round(max(0.0, min(1.0, bpm_score)) * 100))
            key_pct = int(round(max(0.0, min(1.0, key_score)) * 100))
            bpm_color = _compat_color(bpm_score)
            key_color = _compat_color(key_score)
            html = (
                f'<span style="color:#000000; font-weight:bold;">{name}</span>'
                f'<span style="color:#000000;">  [{cand_bpm:.0f} BPM{key_part}]   '
                f'({dur_str})   </span>'
                f'<span style="color:{bpm_color}; font-weight:bold;">{bpm_pct}% BPM</span>'
                f'<span style="color:#000000;"> · </span>'
                f'<span style="color:{key_color}; font-weight:bold;">{key_pct}% Key</span>'
            )
            it = QListWidgetItem(html)
            it.setData(Qt.ItemDataRole.UserRole, idx)
            self.dj_playlist_widget.addItem(it)
        self.statusBar().showMessage(
            f"Ranked {len(scored)} tracks against '{ref_name}' (Deck {ref}).", 5000)
        if getattr(self, "dj_recommend_button", None) is not None:
            self.dj_recommend_button.blockSignals(True)
            self.dj_recommend_button.setChecked(True)
            self.dj_recommend_button.setText("Normal")
            self.dj_recommend_button.blockSignals(False)

    def _play_dj_selected(self):
        item = self.dj_playlist_widget.currentItem()
        if item is None:
            return
        idx = item.data(Qt.ItemDataRole.UserRole)
        if idx is None or idx < 0 or idx >= len(self.playlist):
            return
        if AUDIO_ENGINE.deck_a.audio is None:
            deck = "A"
        elif AUDIO_ENGINE.deck_b.audio is None:
            deck = "B"
        else:
            deck = self.dmx_source
        self._load_playlist_entry_onto_deck(self.playlist[idx], deck)

    def _frames_for_payload(self, payload):
        frames_data = payload.get("frames", [])
        duration = float(payload.get("duration", 0.0))
        fps = float(payload.get("fps", 20.0))
        frames = []
        for frame in frames_data:
            frames.append({
                "time": float(frame.get("time", 0.0)),
                "values": {int(k): int(v) for k, v in frame.get("channels", {}).items()},
            })
        return frames, fps, duration

    def _set_dmx_source(self, source):
        self.dmx_source = source
        self._update_dmx_source_label()
        self._switch_dj_dmx_to(source)

    def _switch_dj_dmx_to(self, source):
        self._end_dj_dmx(try_auto_advance=False)
        if source == "A":
            frames = self.deck_a_frames
            src = AUDIO_ENGINE.deck_a
        else:
            frames = self.deck_b_frames
            src = AUDIO_ENGINE.deck_b
        if frames and src.audio is not None:
            self.dj_dmx_original_values = list(self.values)
            self.dj_dmx_active_deck = source

    def _end_dj_dmx(self, try_auto_advance=True):
        if self.dj_dmx_original_values is not None:
            for ch, v in enumerate(self.dj_dmx_original_values, start=1):
                self.set_channel(ch, v)
            self.dj_dmx_original_values = None
        self.dj_dmx_last_idx = -1
        self.dj_dmx_active_deck = None
        if try_auto_advance:
            other = "B" if self.dmx_source == "A" else "A"
            other_frames = self.deck_b_frames if other == "B" else self.deck_a_frames
            other_src = AUDIO_ENGINE.deck_b if other == "B" else AUDIO_ENGINE.deck_a
            if other_frames and other_src.audio is not None and other_src.is_playing():
                self.dmx_source = other
                self._update_dmx_source_label()
                self.dj_dmx_original_values = list(self.values)
                self.dj_dmx_active_deck = other

    def advance_dj_show(self):
        if not self.dj_mode_active:
            return
        if self.dj_dmx_active_deck is None:
            return
        if self.dj_dmx_active_deck == "A":
            frames = self.deck_a_frames
            fps = self.deck_a_fps
            src = AUDIO_ENGINE.deck_a
        else:
            frames = self.deck_b_frames
            fps = self.deck_b_fps
            src = AUDIO_ENGINE.deck_b
        if not frames or src.audio is None:
            self._end_dj_dmx()
            return
        pos = src.position()
        dur = src.duration()
        if dur <= 0 or pos >= dur - 0.05:
            self._end_dj_dmx()
            return
        idx = min(len(frames) - 1, max(0, int(pos * fps)))
        if idx >= self.dj_dmx_last_idx:
            frame = frames[idx]
            for ch, v in frame["values"].items():
                self.set_channel(ch, v)
            self.dj_dmx_last_idx = idx + 1

    def _load_playlist_entry_onto_deck(self, entry, deck):
        try:
            self._load_playlist_entry_onto_deck_impl(entry, deck)
        except Exception as exc:
            import traceback
            traceback.print_exc()
            self.statusBar().showMessage(
                f"Deck {deck} load failed: {exc}", 8000)

    def _load_playlist_entry_onto_deck_impl(self, entry, deck):
        audio_path = entry.get("audio_path", "")
        show_path = entry.get("show_path", "")
        if not audio_path or not Path(audio_path).is_file():
            QMessageBox.warning(self, "Missing file", "The audio file for this song is missing.")
            return
        if not show_path or not Path(show_path).is_file():
            QMessageBox.warning(self, "Missing file", "The show file for this song is missing.")
            return
        try:
            payload = json.loads(Path(show_path).read_text(encoding="utf-8"))
        except Exception as exc:
            QMessageBox.warning(self, "Load failed", str(exc))
            return
        stem_files = payload.get("stem_files", {})
        try:
            import librosa
            audio, actual_rate = librosa.load(audio_path, sr=DECK_RATE, mono=False)
            audio = np.asarray(audio, dtype=np.float32)
            if audio.ndim == 1:
                audio = np.repeat(audio.reshape(-1, 1), 2, axis=1)
            else:
                audio = audio.T
                if audio.shape[1] == 1:
                    audio = np.repeat(audio, 2, axis=1)
                elif audio.shape[1] > 2:
                    audio = audio[:, :2]
            audio = np.nan_to_num(np.clip(audio, -1.0, 1.0))
        except Exception as exc:
            QMessageBox.warning(self, "Audio load failed", str(exc))
            return

        stem_arrays = {}
        for key, stem_path in stem_files.items():
            if not stem_path or not Path(stem_path).is_file():
                continue
            try:
                data, srate = sf.read(stem_path, dtype="float32", always_2d=True)
                if data.ndim == 1:
                    data = np.repeat(data.reshape(-1, 1), 2, axis=1)
                if data.shape[1] == 1:
                    data = np.repeat(data, 2, axis=1)
                if data.shape[1] > 2:
                    data = data[:, :2]
                data = resample_to_rate(data, int(srate), DECK_RATE)
                stem_arrays[key] = np.nan_to_num(np.clip(data, -1.0, 1.0)).astype(np.float32, copy=False)
            except Exception:
                continue

        stem_levels = payload.get("stem_levels", {})
        duration = audio.shape[0] / float(DECK_RATE)
        frames, fps, _ = self._frames_for_payload(payload)

        envelope_list = []
        bin_seconds = ENVELOPE_BIN_SECONDS
        try:
            samples_per_bin = max(1, int(round(DECK_RATE * ENVELOPE_BIN_SECONDS)))
            mono = audio.mean(axis=1)
            n_bins = max(1, len(mono) // samples_per_bin)
            trimmed = mono[: n_bins * samples_per_bin]
            envelope = np.abs(trimmed).reshape(n_bins, samples_per_bin).max(axis=1)
            peak = float(envelope.max()) if envelope.size else 0.0
            if peak > 0:
                envelope = (envelope / peak).astype(np.float32)
            envelope_list = envelope.tolist()
            bin_seconds = samples_per_bin / float(DECK_RATE)
        except Exception:
            envelope_list = []
            bin_seconds = ENVELOPE_BIN_SECONDS

        beat_times = []
        downbeat_times = []
        try:
            for frame in payload.get("frames", []):
                t = float(frame.get("time", 0.0))
                if bool(frame.get("beat", False)):
                    beat_times.append(t)
                if bool(frame.get("downbeat", False)):
                    downbeat_times.append(t)
        except Exception:
            beat_times = []
            downbeat_times = []

        try:
            bpm_value = float(payload.get("estimated_bpm", 0.0) or 0.0)
        except Exception:
            bpm_value = 0.0
        key_string = payload.get("estimated_key", "")
        camelot = payload.get("camelot_key", "")
        section_list = payload.get("sections", [])

        if deck == "A":
            target = AUDIO_ENGINE.deck_a
            self.deck_a_entry = dict(entry)
            self.deck_a_stem_levels = stem_levels
            self.deck_a_frames = frames
            self.deck_a_fps = fps
            self.deck_a_bpm = bpm_value
            self.deck_a_key = key_string
            self.deck_a_camelot = camelot
            self.deck_a_load_time = time.monotonic()
            self._deck_a_cue_point = None
            self.deck_a.set_song(entry, audio, stem_levels, duration)
            self.deck_a.set_waveform(envelope_list, bin_seconds, duration)
            self.deck_a.set_original_bpm(bpm_value)
            self.deck_a.set_song_key(key_string, camelot)
            self.deck_a.set_beat_data(beat_times, downbeat_times)
            self.deck_a.waveform.set_section_ranges(section_list)
        else:
            target = AUDIO_ENGINE.deck_b
            self.deck_b_entry = dict(entry)
            self.deck_b_stem_levels = stem_levels
            self.deck_b_frames = frames
            self.deck_b_fps = fps
            self.deck_b_bpm = bpm_value
            self.deck_b_key = key_string
            self.deck_b_camelot = camelot
            self.deck_b_load_time = time.monotonic()
            self._deck_b_cue_point = None
            self.deck_b.set_song(entry, audio, stem_levels, duration)
            self.deck_b.set_waveform(envelope_list, bin_seconds, duration)
            self.deck_b.set_original_bpm(bpm_value)
            self.deck_b.set_song_key(key_string, camelot)
            self.deck_b.set_beat_data(beat_times, downbeat_times)
            self.deck_b.waveform.set_section_ranges(section_list)

        deck_widget = self.deck_a if deck == "A" else self.deck_b
        target.load(audio, stem_arrays, DECK_RATE, entry, keylock=False)
        pitch_factor = 1.0 + (deck_widget.pitch_fader.value() / 1000.0) * PITCH_RANGE
        target.set_pitch_factor(pitch_factor)
        target.set_playing(False)

        other = AUDIO_ENGINE.deck_b if deck == "A" else AUDIO_ENGINE.deck_a
        if other.audio is None:
            center = 500
            if self.crossfader.value() != center:
                self.crossfader.setValue(center)
            else:
                self._apply_crossfader(center)
            if deck == "A":
                AUDIO_ENGINE.deck_a.set_crossfade_gain(math.cos(center / 1000.0 * math.pi / 2.0))
                AUDIO_ENGINE.deck_b.set_crossfade_gain(math.sin(center / 1000.0 * math.pi / 2.0))
            else:
                AUDIO_ENGINE.deck_b.set_crossfade_gain(math.sin(center / 1000.0 * math.pi / 2.0))
                AUDIO_ENGINE.deck_a.set_crossfade_gain(math.cos(center / 1000.0 * math.pi / 2.0))

        if self.dmx_source == deck:
            self._switch_dj_dmx_to(deck)

    def _empty_deck(self, deck):
        if deck == "A":
            src = AUDIO_ENGINE.deck_a
            widget = self.deck_a
            was_source = (self.dmx_source == "A")
            self.deck_a_entry = None
            self.deck_a_stem_levels = {}
            self.deck_a_frames = []
            self.deck_a_bpm = 0.0
            self.deck_a_key = ""
            self.deck_a_camelot = ""
            self.deck_a_load_time = 0.0
            self._deck_a_cue_point = None
        else:
            src = AUDIO_ENGINE.deck_b
            widget = self.deck_b
            was_source = (self.dmx_source == "B")
            self.deck_b_entry = None
            self.deck_b_stem_levels = {}
            self.deck_b_frames = []
            self.deck_b_bpm = 0.0
            self.deck_b_key = ""
            self.deck_b_camelot = ""
            self.deck_b_load_time = 0.0
            self._deck_b_cue_point = None
        try:
            src.clear()
        except Exception:
            pass
        widget.clear_visuals()
        widget.waveform.set_section_ranges([])
        for mute in widget.stem_mutes.values():
            mute.blockSignals(True)
            mute.setChecked(False)
            mute.blockSignals(False)
        if was_source:
            other = "B" if deck == "A" else "A"
            other_src = AUDIO_ENGINE.deck_b if other == "B" else AUDIO_ENGINE.deck_a
            if other_src.audio is not None:
                self.dmx_source = other
                self._update_dmx_source_label()
                self._switch_dj_dmx_to(other)
            else:
                self._end_dj_dmx(try_auto_advance=False)
        self.statusBar().showMessage(f"Deck {deck} emptied.", 2500)

    def _deck_a_dropped(self, info):
        self._handle_deck_drop(info, "A")

    def _deck_b_dropped(self, info):
        self._handle_deck_drop(info, "B")

    def _handle_deck_drop(self, info, deck):
        if "playlist_index" in info:
            idx = info["playlist_index"]
            if 0 <= idx < len(self.playlist):
                self._load_playlist_entry_onto_deck(self.playlist[idx], deck)
        elif "path" in info:
            QMessageBox.information(
                self, "External drop",
                "External file drops require a generated show. Analyze this song on the Scripts tab "
                "and use 'Save to Playlist', then drag it from the DJ playlist.")

    def _deck_a_play_pause(self):
        AUDIO_ENGINE.deck_a.toggle_playing()

    def _deck_b_play_pause(self):
        AUDIO_ENGINE.deck_b.toggle_playing()

    def _deck_a_cue(self):
        src = AUDIO_ENGINE.deck_a
        if src.audio is None:
            return
        src.seek_seconds(0.0)
        src.set_playing(True)
        self._deck_a_cue_point = 0.0
        self.statusBar().showMessage("Deck A restarted from the beginning.", 2000)

    def _deck_b_cue(self):
        src = AUDIO_ENGINE.deck_b
        if src.audio is None:
            return
        src.seek_seconds(0.0)
        src.set_playing(True)
        self._deck_b_cue_point = 0.0
        self.statusBar().showMessage("Deck B restarted from the beginning.", 2000)

    def _nearest_bar_seconds(self, deck_widget, position):
        bars = getattr(deck_widget.waveform, "downbeat_times", []) or []
        bars = sorted(float(b) for b in bars)
        if bars:
            return min(bars, key=lambda b: abs(b - position))
        bpm = self.deck_a_bpm if deck_widget is self.deck_a else self.deck_b_bpm
        if bpm > 0:
            bar_len = 4.0 * 60.0 / bpm
            return round(position / bar_len) * bar_len
        return None

    def _lineup_deck(self, deck):
        if deck == "A":
            src_this = AUDIO_ENGINE.deck_a
            src_other = AUDIO_ENGINE.deck_b
            dw_this = self.deck_a
            dw_other = self.deck_b
        else:
            src_this = AUDIO_ENGINE.deck_b
            src_other = AUDIO_ENGINE.deck_a
            dw_this = self.deck_b
            dw_other = self.deck_a
        if src_this.audio is None or src_other.audio is None:
            self.statusBar().showMessage(
                "Load songs onto both decks to line them up.", 3000)
            return
        bar_this = self._nearest_bar_seconds(dw_this, src_this.position())
        if bar_this is None:
            self.statusBar().showMessage(
                "Deck has no bar grid. Analyze the song first.", 3000)
            return
        bar_other = self._nearest_bar_seconds(dw_other, src_other.position())
        src_this.seek_seconds(max(0.0, bar_this))
        if bar_other is not None:
            src_other.seek_seconds(max(0.0, bar_other))
        src_this.set_playing(True)
        src_other.set_playing(True)
        other_name = "B" if deck == "A" else "A"
        self.statusBar().showMessage(
            f"Deck {deck} and Deck {other_name} lined up on bar and playing.", 3000)

    def _deck_a_scrub(self, delta_seconds):
        pass

    def _deck_b_scrub(self, delta_seconds):
        pass

    def _deck_a_seek(self, seconds):
        try:
            AUDIO_ENGINE.deck_a.seek_seconds(seconds)
            self.deck_a.update_position(
                AUDIO_ENGINE.deck_a.position(),
                AUDIO_ENGINE.deck_a.duration(),
                AUDIO_ENGINE.deck_a.is_playing())
        except Exception as exc:
            self.statusBar().showMessage(f"Deck A seek error: {exc}", 3000)

    def _deck_b_seek(self, seconds):
        try:
            AUDIO_ENGINE.deck_b.seek_seconds(seconds)
            self.deck_b.update_position(
                AUDIO_ENGINE.deck_b.position(),
                AUDIO_ENGINE.deck_b.duration(),
                AUDIO_ENGINE.deck_b.is_playing())
        except Exception as exc:
            self.statusBar().showMessage(f"Deck B seek error: {exc}", 3000)

    def _deck_a_pitch_changed(self, factor):
        AUDIO_ENGINE.deck_a.set_pitch_factor(factor)
        if AUDIO_ENGINE.deck_a.keylock:
            AUDIO_ENGINE.deck_a.set_keylock(False)
            AUDIO_ENGINE.deck_a.set_keylock(True)

    def _deck_b_pitch_changed(self, factor):
        AUDIO_ENGINE.deck_b.set_pitch_factor(factor)
        if AUDIO_ENGINE.deck_b.keylock:
            AUDIO_ENGINE.deck_b.set_keylock(False)
            AUDIO_ENGINE.deck_b.set_keylock(True)

    def _deck_a_keylock(self, enabled):
        AUDIO_ENGINE.deck_a.set_keylock(enabled)

    def _deck_b_keylock(self, enabled):
        AUDIO_ENGINE.deck_b.set_keylock(enabled)

    def _deck_a_stem_mute(self, key, muted):
        AUDIO_ENGINE.deck_a.set_stem_mute(key, muted)

    def _deck_b_stem_mute(self, key, muted):
        AUDIO_ENGINE.deck_b.set_stem_mute(key, muted)

    def _best_pitch_ratio(self, src_bpm, target_bpm):
        if src_bpm <= 0 or target_bpm <= 0:
            return None
        candidates = [
            target_bpm / src_bpm,
            (target_bpm * 2.0) / src_bpm,
            (target_bpm / 2.0) / src_bpm,
        ]
        candidates.sort(key=lambda f: abs(f - 1.0))
        best = candidates[0]
        if not (1.0 - PITCH_RANGE <= best <= 1.0 + PITCH_RANGE):
            return None
        return best

    def _sync_a_to_b(self):
        if self.deck_b_bpm <= 0 or self.deck_a_bpm <= 0:
            self.statusBar().showMessage(
                "Both decks need an analyzed BPM before syncing.", 4000)
            return
        factor = self._best_pitch_ratio(self.deck_a_bpm, self.deck_b_bpm)
        if factor is None:
            self.statusBar().showMessage(
                "Sync not possible: required pitch is outside ±16%.", 4000)
            return
        self.deck_a.set_pitch_factor(factor)
        AUDIO_ENGINE.deck_a.set_pitch_factor(factor)
        if AUDIO_ENGINE.deck_a.keylock:
            AUDIO_ENGINE.deck_a.set_keylock(False)
            AUDIO_ENGINE.deck_a.set_keylock(True)
        self.statusBar().showMessage(
            f"Deck A synced to Deck B: {(factor-1)*100:+.2f}% "
            f"({self.deck_a_bpm*factor:.1f} → {self.deck_b_bpm:.1f} BPM equivalent).", 4000)

    def _sync_b_to_a(self):
        if self.deck_a_bpm <= 0 or self.deck_b_bpm <= 0:
            self.statusBar().showMessage(
                "Both decks need an analyzed BPM before syncing.", 4000)
            return
        factor = self._best_pitch_ratio(self.deck_b_bpm, self.deck_a_bpm)
        if factor is None:
            self.statusBar().showMessage(
                "Sync not possible: required pitch is outside ±16%.", 4000)
            return
        self.deck_b.set_pitch_factor(factor)
        AUDIO_ENGINE.deck_b.set_pitch_factor(factor)
        if AUDIO_ENGINE.deck_b.keylock:
            AUDIO_ENGINE.deck_b.set_keylock(False)
            AUDIO_ENGINE.deck_b.set_keylock(True)
        self.statusBar().showMessage(
            f"Deck B synced to Deck A: {(factor-1)*100:+.2f}% "
            f"({self.deck_b_bpm*factor:.1f} → {self.deck_a_bpm:.1f} BPM equivalent).", 4000)

    def _update_dj_decks(self):
        if not self.dj_mode_active:
            return
        for deck_widget, src, deck_id in (
            (self.deck_a, AUDIO_ENGINE.deck_a, "A"),
            (self.deck_b, AUDIO_ENGINE.deck_b, "B"),
        ):
            dur = src.duration()
            pos = src.position()
            playing = src.is_playing()

            if (src.audio is not None and dur > 1.0
                    and not playing and pos >= dur - 0.05):
                src.clear()
                deck_widget.clear_visuals()
                deck_widget.waveform.set_section_ranges([])
                for mute in deck_widget.stem_mutes.values():
                    mute.blockSignals(True)
                    mute.setChecked(False)
                    mute.blockSignals(False)
                if deck_id == "A":
                    self.deck_a_frames = []
                    self.deck_a_bpm = 0.0
                    self.deck_a_key = ""
                    self.deck_a_camelot = ""
                    self.deck_a_load_time = 0.0
                else:
                    self.deck_b_frames = []
                    self.deck_b_bpm = 0.0
                    self.deck_b_key = ""
                    self.deck_b_camelot = ""
                    self.deck_b_load_time = 0.0
                continue

            deck_widget.update_position(pos, dur, playing)

    # ----------------- Preload management (non-DJ) -----------------

    def _entry_key(self, entry):
        return entry.get("show_path") or entry.get("audio_path") or entry.get("name", "")

    def _queue_preload_keys(self):
        return [self._entry_key(e) for e in self.queue_entries[:PRELOAD_COUNT]]

    def _schedule_preloads(self):
        wanted = self._queue_preload_keys()
        for key in list(self.preload_workers.keys()):
            if key not in wanted:
                worker = self.preload_workers.pop(key)
                try:
                    worker.quit()
                    worker.wait(50)
                except Exception:
                    pass
        for key in list(self.preloaded_entries.keys()):
            if key not in wanted:
                self.preloaded_entries.pop(key, None)
        for key in wanted:
            if key in self.preloaded_entries or key in self.preload_workers:
                continue
            entry = self._queue_entry_by_key(key)
            if entry is None:
                continue
            audio_path = entry.get("audio_path", "")
            show_path = entry.get("show_path", "")
            if not audio_path or not show_path:
                continue
            if not Path(audio_path).is_file() or not Path(show_path).is_file():
                self.preload_failures[key] = "Missing files"
                continue
            worker = PreloadWorker(key, audio_path, show_path, self)
            worker.ready.connect(self._on_preload_ready)
            worker.failed.connect(self._on_preload_failed)
            self.preload_workers[key] = worker
            worker.start()
        self._refresh_queue_ui()

    def _queue_entry_by_key(self, key):
        for entry in self.queue_entries[:PRELOAD_COUNT]:
            if self._entry_key(entry) == key:
                return entry
        return None

    def _on_preload_ready(self, key, data):
        self.preloaded_entries[key] = data
        self.preload_failures.pop(key, None)
        worker = self.preload_workers.pop(key, None)
        if worker is not None:
            try:
                worker.wait(50)
            except Exception:
                pass
        self._refresh_queue_ui()

    def _on_preload_failed(self, key, message):
        self.preload_failures[key] = message
        self.preload_workers.pop(key, None)
        self._refresh_queue_ui()

    def _entry_ready(self, entry):
        return self._entry_key(entry) in self.preloaded_entries

    def _refresh_queue_ui(self):
        if not hasattr(self, "queue_widget"):
            return
        self.queue_widget.clear()
        for entry in self.queue_entries[:max(PRELOAD_COUNT, QUEUE_PREVIEW_COUNT)]:
            label = entry.get("name", "Untitled")
            dur = float(entry.get("duration", 0.0))
            ready = self._entry_ready(entry)
            mark = "✓ " if ready else "   "
            it = QListWidgetItem(f"{mark}{label}   ({int(dur // 60):02d}:{int(dur % 60):02d})")
            it.setData(Qt.ItemDataRole.UserRole, dict(entry))
            it.setForeground(QColor(120, 230, 140) if ready else QColor(220, 226, 236))
            self.queue_widget.addItem(it)

    def _apply_playlist_view_mode(self, mode):
        if mode == "grid_large":
            self.playlist_widget.setViewMode(QListView.ViewMode.IconMode)
            self.playlist_widget.setIconSize(QSize(160, 160))
            self.playlist_widget.setGridSize(QSize(180, 200))
        elif mode == "grid_medium":
            self.playlist_widget.setViewMode(QListView.ViewMode.IconMode)
            self.playlist_widget.setIconSize(QSize(110, 110))
            self.playlist_widget.setGridSize(QSize(130, 150))
        elif mode == "grid_small":
            self.playlist_widget.setViewMode(QListView.ViewMode.IconMode)
            self.playlist_widget.setIconSize(QSize(72, 72))
            self.playlist_widget.setGridSize(QSize(90, 110))
        else:
            self.playlist_widget.setViewMode(QListView.ViewMode.ListMode)
            self.playlist_widget.setIconSize(QSize())
            self.playlist_widget.setGridSize(QSize())
        self.config["music_view_mode"] = mode

    def _apply_view_mode_everywhere(self, mode):
        self._apply_playlist_view_mode(mode)
        if hasattr(self, "dj_playlist_widget"):
            if mode == "list":
                self.dj_playlist_widget.setViewMode(QListView.ViewMode.ListMode)
                self.dj_playlist_widget.setIconSize(QSize())
                self.dj_playlist_widget.setGridSize(QSize())
            elif mode == "grid_small":
                self.dj_playlist_widget.setViewMode(QListView.ViewMode.IconMode)
                self.dj_playlist_widget.setIconSize(QSize(72, 72))
                self.dj_playlist_widget.setGridSize(QSize(90, 110))
            elif mode == "grid_medium":
                self.dj_playlist_widget.setViewMode(QListView.ViewMode.IconMode)
                self.dj_playlist_widget.setIconSize(QSize(110, 110))
                self.dj_playlist_widget.setGridSize(QSize(130, 150))
            else:
                self.dj_playlist_widget.setViewMode(QListView.ViewMode.IconMode)
                self.dj_playlist_widget.setIconSize(QSize(160, 160))
                self.dj_playlist_widget.setGridSize(QSize(180, 200))

    def _apply_playlist_font_size(self, size):
        size = max(8, min(48, int(size)))
        font = QFont()
        font.setPointSize(size)
        self.playlist_widget.setFont(font)
        self.config["music_font_size"] = size

    def _show_view_menu(self):
        menu = QMenu(self)
        for label, mode in (
            ("Grid — Large", "grid_large"),
            ("Grid — Medium", "grid_medium"),
            ("Grid — Small", "grid_small"),
            ("List", "list"),
        ):
            act = QAction(label, self)
            act.setCheckable(True)
            act.setChecked(self.config.get("music_view_mode", "list") == mode)
            act.triggered.connect(lambda _checked=False, m=mode: self._apply_view_mode_everywhere(m))
            menu.addAction(act)
        text_action = QAction("Text size…", self)
        text_action.triggered.connect(self._ask_music_text_size)
        menu.addAction(text_action)
        menu.exec(self.music_view_button.mapToGlobal(self.music_view_button.rect().bottomLeft()))

    def _show_empty_music_menu(self, global_pos):
        menu = QMenu(self)
        view_menu = menu.addMenu("View")
        for label, mode in (
            ("Grid — Large", "grid_large"),
            ("Grid — Medium", "grid_medium"),
            ("Grid — Small", "grid_small"),
            ("List", "list"),
        ):
            act = QAction(label, self)
            act.setCheckable(True)
            act.setChecked(self.config.get("music_view_mode", "list") == mode)
            act.triggered.connect(lambda _checked=False, m=mode: self._apply_view_mode_everywhere(m))
            view_menu.addAction(act)
        text_action = QAction("Text size…", self)
        text_action.triggered.connect(self._ask_music_text_size)
        menu.addAction(text_action)
        refresh_action = QAction("Refresh", self)
        refresh_action.triggered.connect(self._refresh_music_tab)
        menu.addAction(refresh_action)
        menu.exec(global_pos)

    def _show_item_music_menu(self, row, global_pos):
        if row < 0 or row >= len(self.playlist):
            return
        menu = QMenu(self)
        queue_next = QAction("Queue Next", self)
        queue_next.triggered.connect(lambda: self._queue_entry(row, next_position=True))
        menu.addAction(queue_next)
        queue_last = QAction("Queue Last", self)
        queue_last.triggered.connect(lambda: self._queue_entry(row, next_position=False))
        menu.addAction(queue_last)
        menu.addSeparator()
        play_action = QAction("Play Now", self)
        play_action.triggered.connect(lambda: self._play_playlist_index(row))
        menu.addAction(play_action)
        remove_action = QAction("Remove from playlist", self)
        remove_action.triggered.connect(self.remove_playlist_entry)
        menu.addAction(remove_action)
        delete_pc_action = QAction("Delete from PC…", self)
        delete_pc_action.triggered.connect(lambda: self._delete_entry_from_pc(row))
        menu.addAction(delete_pc_action)
        menu.exec(global_pos)

    def _delete_entry_from_pc(self, row):
        if row < 0 or row >= len(self.playlist):
            return
        entry = self.playlist[row]
        name = entry.get("name", "Untitled")
        audio_path = entry.get("audio_path", "")
        show_path = entry.get("show_path", "")
        stem_files = []
        if show_path and Path(show_path).is_file():
            try:
                payload = json.loads(Path(show_path).read_text(encoding="utf-8"))
                stem_files = [p for p in payload.get("stem_files", {}).values()
                              if p and Path(p).is_file()]
            except Exception:
                stem_files = []
        paths = [p for p in [audio_path, show_path, *stem_files] if p and Path(p).is_file()]
        if not paths:
            reply = QMessageBox.question(
                self, "Delete from PC",
                f"The files for '{name}' are already missing. Remove the entry from the "
                "playlist?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply == QMessageBox.StandardButton.Yes:
                self.playlist.pop(row)
                save_playlist(self.playlist)
                self.refresh_playlist_ui()
                self._rebuild_dj_playlist()
            return
        listing = "\n".join(f"  • {Path(p).name}" for p in paths)
        reply = QMessageBox.warning(
            self, "Delete from PC",
            f"Permanently delete the following files for '{name}'?\n\n{listing}\n\n"
            "This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        failed = []
        for p in paths:
            try:
                Path(p).unlink()
            except OSError as exc:
                failed.append(f"{Path(p).name}: {exc}")
        self.playlist.pop(row)
        save_playlist(self.playlist)
        self.refresh_playlist_ui()
        self._rebuild_dj_playlist()
        self._refresh_queue_ui()
        self._schedule_preloads()
        if failed:
            QMessageBox.warning(
                self, "Delete from PC",
                "Some files could not be deleted:\n" + "\n".join(failed))
        else:
            self.statusBar().showMessage(
                f"Deleted '{name}' and its files from the PC.", 4000)

    def _ask_music_text_size(self):
        current = int(self.config.get("music_font_size", 13))
        value, ok = QInputDialog.getInt(self, "Text size", "Font size (pt):", current, 8, 48, 1)
        if ok:
            self._apply_playlist_font_size(value)

    def _refresh_music_tab(self):
        self.refresh_playlist_ui()
        self._refresh_queue_ui()

    def _on_music_search(self, text):
        query = (text or "").strip().lower()
        self.refresh_playlist_ui(query=query)

    def refresh_playlist_ui(self, query=""):
        if not hasattr(self, "playlist_widget"):
            return
        self.playlist_widget.clear()
        query = (query or "").strip().lower()
        entries = list(enumerate(self.playlist))
        if query:
            matches = [(idx, e) for idx, e in entries
                       if query in (e.get("name", "") or "").lower()]
            non_matches = [(idx, e) for idx, e in entries
                           if query not in (e.get("name", "") or "").lower()]
            ordered = matches + non_matches
        else:
            ordered = entries
        for idx, entry in ordered:
            audio_path = entry.get("audio_path", "")
            show_path = entry.get("show_path", "")
            if not audio_path or not Path(audio_path).is_file():
                continue
            if not show_path or not Path(show_path).is_file():
                continue
            name = entry.get("name", "Untitled")
            duration = float(entry.get("duration", 0.0))
            minutes = int(duration // 60)
            seconds = int(duration % 60)
            item = QListWidgetItem(f"{name}   ({minutes:02d}:{seconds:02d})")
            item.setData(Qt.ItemDataRole.UserRole, idx)
            if query and query in name.lower():
                item.setBackground(QColor(60, 120, 90))
                item.setForeground(QColor(240, 255, 240))
            if self.config.get("music_view_mode", "list") != "list":
                pix = QPixmap(96, 96)
                pix.fill(QColor(45, 50, 60))
                item.setIcon(QIcon(pix))
            self.playlist_widget.addItem(item)
        if self.playlist:
            self.playlist_status.setText(
                f"{len(self.playlist)} song(s) in playlist."
                + (f"  Filter: '{query}'" if query else ""))
        else:
            self.playlist_status.setText(
                "No songs in the playlist yet. Use 'Save to Playlist' on the Scripts tab.")
        if hasattr(self, "dj_playlist_widget"):
            self._rebuild_dj_playlist()

    def _queue_entry(self, playlist_index, next_position):
        if playlist_index < 0 or playlist_index >= len(self.playlist):
            return
        entry = dict(self.playlist[playlist_index])
        if next_position:
            self.queue_entries.insert(0, entry)
        else:
            self.queue_entries.append(entry)
        self._refresh_queue_ui()
        self._schedule_preloads()
        self.statusBar().showMessage(
            f"Queued '{entry.get('name', 'Untitled')}' ({'next' if next_position else 'last'}).", 3000)

    def _on_queue_reordered(self, order):
        self.queue_entries = list(order)
        self._refresh_queue_ui()
        self._schedule_preloads()

    def _on_queue_play(self, row):
        if row < 0 or row >= len(self.queue_entries):
            return
        entry = self.queue_entries.pop(row)
        self._refresh_queue_ui()
        self._schedule_preloads()
        self._play_playlist_entry_dict(entry)

    def _music_stem_mute_changed(self, stem_key, muted):
        AUDIO_ENGINE.set_stem_mute(stem_key, muted)

    def _current_music_entry(self):
        if 0 <= self.playlist_current_index < len(self.playlist):
            return self.playlist[self.playlist_current_index]
        return None

    def _music_toggle_play(self):
        if self.song_original_values is None:
            entry = self._current_music_entry()
            if entry is None:
                item = self.playlist_widget.currentItem()
                if item is not None:
                    self._play_playlist_index(item.data(Qt.ItemDataRole.UserRole))
                else:
                    self.statusBar().showMessage("Select a song first.", 2500)
                return
            self._play_playlist_entry_dict(entry)
        else:
            self.toggle_song_pause()

    def _play_next_from_queue(self):
        if not self.queue_entries:
            return False
        entry = self.queue_entries.pop(0)
        self._refresh_queue_ui()
        self._schedule_preloads()
        self._play_playlist_entry_dict(entry)
        return True

    def _play_previous_from_history(self):
        if self.playlist_current_index is None or self.playlist_current_index < 0:
            return False
        prev = self.playlist_current_index - 1
        if prev < 0:
            prev = len(self.playlist) - 1
        if prev < 0 or prev >= len(self.playlist):
            return False
        self._play_playlist_index(prev)
        return True

    def _music_rewind(self):
        now = time.monotonic()
        if now - getattr(self, "_last_rewind_click_time", 0.0) < 0.8:
            self._last_rewind_click_time = 0.0
            self._play_previous_from_history()
            return
        self._last_rewind_click_time = now
        if self.song_original_values is not None:
            self.seek_generated_show(0.0)
        elif self._current_music_entry() is not None:
            self._play_playlist_entry_dict(self._current_music_entry())

    def _music_forward(self):
        if self._play_next_from_queue():
            return
        if self.playlist_current_index is not None and self.playlist_current_index >= 0:
            nxt = self.playlist_current_index + 1
            if nxt < len(self.playlist):
                self._play_playlist_index(nxt)
                return
        self.statusBar().showMessage("No next song in queue or playlist.", 2500)

    def play_playlist_entry(self):
        item = self.playlist_widget.currentItem()
        if item is None:
            self.statusBar().showMessage("Select a song from the playlist first.", 3000)
            return
        self._play_playlist_index(item.data(Qt.ItemDataRole.UserRole))

    def _play_playlist_index(self, idx):
        if idx is None or idx < 0 or idx >= len(self.playlist):
            return
        self._play_playlist_entry_dict(self.playlist[idx], idx)

    def _play_playlist_entry_dict(self, entry, idx=None):
        audio_path = entry.get("audio_path", "")
        show_path = entry.get("show_path", "")
        if not audio_path or not Path(audio_path).is_file():
            QMessageBox.warning(self, "Playlist entry missing",
                                "The saved audio file for this entry is missing.")
            return
        if not show_path or not Path(show_path).is_file():
            QMessageBox.warning(self, "Playlist entry missing",
                                "The saved show file for this entry is missing.")
            return
        self.stop_generated_show()
        key = self._entry_key(entry)
        preloaded = self.preloaded_entries.get(key)
        try:
            if preloaded is not None:
                payload = preloaded.get("payload") or json.loads(
                    Path(show_path).read_text(encoding="utf-8"))
                audio_array = preloaded.get("audio")
                audio_rate = int(preloaded.get("rate", 0))
            else:
                payload = json.loads(Path(show_path).read_text(encoding="utf-8"))
                audio_array = None
                audio_rate = 0
        except Exception as exc:
            QMessageBox.warning(self, "Show load failed", str(exc))
            return
        self._load_show_from_payload(payload, audio_path)
        self.song_array = audio_array
        self.song_sample_rate = audio_rate
        self.song_stem_arrays = {}
        self.playlist_current_index = idx if idx is not None else -1
        self.playlist_active = True
        self.music_now_selected_label.setText(f"Now playing: {entry.get('name', 'Untitled')}")
        self.playlist_status.setText(f"Playing: {entry.get('name', 'Untitled')}")
        has_stems = bool(payload.get("stem_files"))
        for stem_key, mute in self.music_stem_mute_boxes.items():
            mute.blockSignals(True)
            mute.setEnabled(has_stems)
            mute.setChecked(False)
            mute.blockSignals(False)
            if has_stems:
                AUDIO_ENGINE.set_stem_mute(stem_key, False)
        self.play_generated_show()
        self.preloaded_entries.pop(key, None)
        self._schedule_preloads()

    def _load_show_from_payload(self, payload, audio_path):
        frames = payload.get("frames", [])
        duration = float(payload.get("duration", 0.0))
        bpm = float(payload.get("estimated_bpm", 120.0))
        sections = payload.get("sections", [])
        downbeat_phase = int(payload.get("downbeat_phase", 0))
        timeline = []
        for frame in frames:
            feature = {
                "time": float(frame.get("time", 0.0)),
                "section": frame.get("section", "Verse / Groove"),
                "beat": bool(frame.get("beat", False)),
                "downbeat": bool(frame.get("downbeat", False)),
                "bar": int(frame.get("bar", 0)),
                "phrase": int(frame.get("phrase", 0)),
                "energy": float(frame.get("energy", 0.0)),
                "onset": float(frame.get("onset", 0.0)),
                "kick": float(frame.get("kick", 0.0)),
                "snare": float(frame.get("snare", 0.0)),
                "hat": float(frame.get("hat", 0.0)),
                "kick_hit": bool(frame.get("kick_hit", False)),
                "snare_hit": bool(frame.get("snare_hit", False)),
                "hat_hit": bool(frame.get("hat_hit", False)),
                "kick_strength": float(frame.get("kick_strength", 0.0)),
                "snare_strength": float(frame.get("snare_strength", 0.0)),
                "hat_strength": float(frame.get("hat_strength", 0.0)),
                "beat_in_bar": int(frame.get("beat_in_bar", 0)),
                "beat_phase": float(frame.get("beat_phase", 0.0)),
                "section_progress": float(frame.get("section_progress", 0.0)),
                "stem_levels": frame.get("stem_levels", {}),
                "transition": bool(frame.get("transition", False)),
                "sub": float(frame.get("sub", 0.0)),
                "bass": float(frame.get("bass", 0.0)),
                "mids": float(frame.get("mids", 0.0)),
                "highs": float(frame.get("highs", 0.0)),
            }
            timeline.append(feature)
        self.song_result = {
            "filename": audio_path,
            "duration": duration,
            "bpm": bpm,
            "beat_count": int(payload.get("beat_count", 0)),
            "sections": sections,
            "downbeat_phase": downbeat_phase,
            "fps": float(payload.get("fps", 20.0)),
            "kick_count": int(payload.get("kick_count", 0)),
            "snare_count": int(payload.get("snare_count", 0)),
            "hat_count": int(payload.get("hat_count", 0)),
            "stem_separation": payload.get("stem_separation", "Saved show"),
            "stem_error": "",
            "stem_availability": payload.get("stem_availability", {}),
            "stem_files": payload.get("stem_files", {}),
            "stem_levels": payload.get("stem_levels", {}),
            "estimated_key": payload.get("estimated_key", ""),
            "camelot_key": payload.get("camelot_key", ""),
            "timeline": timeline,
        }
        self.song_frames = []
        for frame in frames:
            self.song_frames.append({
                "time": frame.get("time", 0.0),
                "values": {int(k): int(v) for k, v in frame.get("channels", {}).items()},
                "feature": next((f for f in timeline if abs(f["time"] - frame.get("time", 0.0)) < 1e-6), timeline[0] if timeline else {}),
            })
        song_duration = duration
        for stem_key, visualizer in getattr(self, "song_stem_visualizers", {}).items():
            levels = payload.get("stem_levels", {}).get(stem_key, [])
            visualizer.set_data(levels)
            visualizer.set_duration(song_duration)
            visualizer.set_playhead(0.0)
        for stem_key, visualizer in getattr(self, "music_stem_visualizers", {}).items():
            levels = payload.get("stem_levels", {}).get(stem_key, [])
            visualizer.set_data(levels)
            visualizer.set_duration(song_duration)
            visualizer.set_playhead(0.0)
        if hasattr(self, "song_play_button"):
            self.song_play_button.setEnabled(bool(self.song_frames))
        if hasattr(self, "song_export_csv_button"):
            self.song_export_csv_button.setEnabled(bool(self.song_frames))
        if hasattr(self, "song_export_json_button"):
            self.song_export_json_button.setEnabled(bool(self.song_frames))
        if hasattr(self, "song_status"):
            self.song_status.setText(f"Loaded saved show: {Path(audio_path).name}")

    def remove_playlist_entry(self):
        item = self.playlist_widget.currentItem()
        if item is None:
            return
        idx = item.data(Qt.ItemDataRole.UserRole)
        if idx is None or idx < 0 or idx >= len(self.playlist):
            return
        entry = self.playlist[idx]
        reply = QMessageBox.question(
            self, "Remove from playlist",
            f"Remove '{entry.get('name', 'Untitled')}' from the playlist?\n"
            "This deletes the copied audio and show files too.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        self.playlist.pop(idx)
        save_playlist(self.playlist)
        for key in ("audio_path", "show_path"):
            path = entry.get(key, "")
            if path and Path(path).is_file():
                try:
                    Path(path).unlink()
                except OSError:
                    pass
        self.refresh_playlist_ui()
        self._schedule_preloads()

    def save_to_playlist(self):
        if not getattr(self, "song_result", None) or not getattr(self, "song_frames", None):
            QMessageBox.information(self, "Nothing to save",
                                    "Analyze a song and generate a show first.")
            return
        source = self.song_result.get("filename") or self.song_path_edit.text().strip()
        if not source or not Path(source).is_file():
            QMessageBox.warning(self, "Missing audio", "The original audio file is no longer available.")
            return
        name = Path(source).stem
        PLAYLIST_AUDIO_DIR.mkdir(exist_ok=True)
        PLAYLIST_SHOW_DIR.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        unique = f"{name}_{stamp}"
        audio_target = PLAYLIST_AUDIO_DIR / f"{unique}{Path(source).suffix.lower()}"
        show_target = PLAYLIST_SHOW_DIR / f"{unique}.json"
        try:
            shutil.copy2(source, audio_target)
        except OSError as exc:
            QMessageBox.warning(self, "Save failed", f"Could not copy audio: {exc}")
            return
        stem_sources = self.song_result.get("stem_files", {})
        saved_stems = {}
        stem_availability = dict(self.song_result.get("stem_availability", {}))
        for key, stem_path in stem_sources.items():
            if stem_path and Path(stem_path).is_file():
                stem_target = PLAYLIST_AUDIO_DIR / f"{unique}_{key}.wav"
                try:
                    shutil.copy2(stem_path, stem_target)
                    saved_stems[key] = str(stem_target)
                except OSError:
                    pass
        payload = {
            "format": "DMX Song Timeline",
            "version": 2,
            "name": name,
            "source": str(audio_target),
            "audio_path": str(audio_target),
            "duration": self.song_result.get("duration", 0.0),
            "estimated_bpm": self.song_result.get("bpm", 120.0),
            "downbeat_phase": self.song_result.get("downbeat_phase", 0),
            "sections": self.song_result.get("sections", []),
            "fps": self.song_result.get("fps", 20.0),
            "beat_count": self.song_result.get("beat_count", 0),
            "kick_count": self.song_result.get("kick_count", 0),
            "snare_count": self.song_result.get("snare_count", 0),
            "hat_count": self.song_result.get("hat_count", 0),
            "stem_separation": self.song_result.get("stem_separation", ""),
            "stem_availability": stem_availability,
            "stem_files": saved_stems,
            "stem_levels": self.song_result.get("stem_levels", {}),
            "estimated_key": self.song_result.get("estimated_key", ""),
            "camelot_key": self.song_result.get("camelot_key", ""),
            "frames": [{
                "time": f["time"],
                "channels": {str(k): v for k, v in f["values"].items()},
                "section": f["feature"].get("section", "Verse / Groove"),
                "beat": f["feature"].get("beat", False),
                "downbeat": f["feature"].get("downbeat", False),
                "bar": f["feature"].get("bar", 0),
                "phrase": f["feature"].get("phrase", 0),
                "beat_in_bar": f["feature"].get("beat_in_bar", 0),
                "beat_phase": f["feature"].get("beat_phase", 0.0),
                "section_progress": f["feature"].get("section_progress", 0.0),
                "energy": f["feature"].get("energy", 0.0),
                "sub": f["feature"].get("sub", 0.0),
                "bass": f["feature"].get("bass", 0.0),
                "mids": f["feature"].get("mids", 0.0),
                "highs": f["feature"].get("highs", 0.0),
                "onset": f["feature"].get("onset", 0.0),
                "kick": f["feature"].get("kick", 0.0),
                "snare": f["feature"].get("snare", 0.0),
                "hat": f["feature"].get("hat", 0.0),
                "kick_hit": f["feature"].get("kick_hit", False),
                "snare_hit": f["feature"].get("snare_hit", False),
                "hat_hit": f["feature"].get("hat_hit", False),
                "kick_strength": f["feature"].get("kick_strength", 0.0),
                "snare_strength": f["feature"].get("snare_strength", 0.0),
                "hat_strength": f["feature"].get("hat_strength", 0.0),
                "stem_levels": f["feature"].get("stem_levels", {}),
                "transition": f["feature"].get("transition", False),
            } for f in self.song_frames]
        }
        try:
            show_target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(self, "Save failed", f"Could not write show: {exc}")
            return
        entry = {
            "name": name,
            "audio_path": str(audio_target),
            "show_path": str(show_target),
            "duration": float(self.song_result.get("duration", 0.0)),
            "camelot_key": self.song_result.get("camelot_key", ""),
            "estimated_key": self.song_result.get("estimated_key", ""),
        }
        self.playlist.append(entry)
        save_playlist(self.playlist)
        self.refresh_playlist_ui()
        self.statusBar().showMessage(f"Saved '{name}' to the playlist.", 4000)

    # ---------------- SCRIPTS TAB ----------------

    def build_scripts_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        heading = QLabel("FULL SONG → DMX SHOW GENERATOR")
        heading.setStyleSheet("font-size: 17px; font-weight: bold;")
        layout.addWidget(heading)
        layout.addWidget(QLabel(
            "Analyze the full song for beats, phrases, energy shifts and section changes, then generate a "
            "beat-synchronized DMX timeline. The original file plays by default; muting any stem crossfades "
            "into the isolated stems, and unmuting all of them crossfades back to the original."))
        file_row = QHBoxLayout()
        self.song_path_edit = QLineEdit()
        self.song_path_edit.setReadOnly(True)
        self.song_path_edit.setPlaceholderText("Choose an audio file...")
        file_row.addWidget(self.song_path_edit, 1)
        browse = QPushButton("Choose Song")
        browse.clicked.connect(self.choose_song_file)
        file_row.addWidget(browse)
        layout.addLayout(file_row)
        settings = QGroupBox("Fixture channel mapping (set a channel to 0 to disable it)")
        settings_layout = QVBoxLayout(settings)
        grid = QGridLayout()
        self.song_channel_boxes = {}
        mapping = [
            ("Pan / horizontal", 1), ("Tilt / vertical", 2),
            ("Pan small", 0), ("Tilt small", 0), ("Frost", 0),
            ("Strobe / shutter", 6), ("Dimmer", 7), ("Color wheel", 8),
            ("Color blending", 0), ("Fractal", 10), ("Fractal spin", 11),
            ("Focus", 0), ("Eight prism", 0), ("Blending filter", 14)
        ]
        for row, (label, default) in enumerate(mapping):
            grid.addWidget(QLabel(f"{label} (0 = unused):"), row, 0)
            spin = QSpinBox()
            spin.setRange(0, 512)
            spin.setValue(default)
            spin.setToolTip("DMX output channel for this fixture function. Set to 0 to disable.")
            grid.addWidget(spin, row, 1)
            self.song_channel_boxes[label] = spin
        movement_box = QGroupBox("Movement limits")
        movement_grid = QGridLayout(movement_box)
        self.song_pan_min = QSpinBox(); self.song_pan_min.setRange(0, 255); self.song_pan_min.setValue(0)
        self.song_pan_max = QSpinBox(); self.song_pan_max.setRange(0, 255); self.song_pan_max.setValue(255)
        self.song_tilt_min = QSpinBox(); self.song_tilt_min.setRange(0, 255); self.song_tilt_min.setValue(0)
        self.song_tilt_max = QSpinBox(); self.song_tilt_max.setRange(0, 255); self.song_tilt_max.setValue(255)
        movement_grid.addWidget(QLabel("Pan minimum:"), 0, 0); movement_grid.addWidget(self.song_pan_min, 0, 1)
        movement_grid.addWidget(QLabel("Pan maximum:"), 0, 2); movement_grid.addWidget(self.song_pan_max, 0, 3)
        movement_grid.addWidget(QLabel("Tilt minimum:"), 1, 0); movement_grid.addWidget(self.song_tilt_min, 1, 1)
        movement_grid.addWidget(QLabel("Tilt maximum:"), 1, 2); movement_grid.addWidget(self.song_tilt_max, 1, 3)
        controls = QGridLayout()
        controls.addWidget(QLabel("Movement amount:"), 0, 0)
        self.song_movement_spin = QSpinBox(); self.song_movement_spin.setRange(0, 255); self.song_movement_spin.setValue(180)
        controls.addWidget(self.song_movement_spin, 0, 1)
        controls.addWidget(QLabel("Base dimmer:"), 1, 0)
        self.song_dimmer_spin = QSpinBox(); self.song_dimmer_spin.setRange(0, 255); self.song_dimmer_spin.setValue(35)
        controls.addWidget(self.song_dimmer_spin, 1, 1)
        controls.addWidget(QLabel("Show style:"), 2, 0)
        self.song_style_combo = QComboBox(); self.song_style_combo.addItems(["Smooth", "Minimal", "High energy", "Aggressive"])
        controls.addWidget(self.song_style_combo, 2, 1)
        controls.addWidget(QLabel("Hi-hat sensitivity:"), 3, 0)
        self.song_hat_sensitivity = QSpinBox(); self.song_hat_sensitivity.setRange(1, 100); self.song_hat_sensitivity.setValue(55)
        self.song_hat_sensitivity.setToolTip("Higher values detect quieter hi-hats and cymbal ticks; lower values flash only on stronger high-frequency hits.")
        controls.addWidget(self.song_hat_sensitivity, 3, 1)
        settings_layout.addLayout(grid)
        settings_layout.addWidget(movement_box)
        settings_layout.addLayout(controls)
        layout.addWidget(settings)
        action_row = QHBoxLayout()
        self.song_generate_button = QPushButton("Analyze & Generate Show")
        self.song_generate_button.clicked.connect(self.generate_song_show)
        action_row.addWidget(self.song_generate_button)
        self.song_play_button = QPushButton("Play Song + Show")
        self.song_play_button.setEnabled(False)
        self.song_play_button.clicked.connect(self.play_generated_show)
        action_row.addWidget(self.song_play_button)
        self.song_pause_button = QPushButton("Pause")
        self.song_pause_button.setEnabled(False)
        self.song_pause_button.clicked.connect(self.toggle_song_pause)
        action_row.addWidget(self.song_pause_button)
        self.song_stop_button = QPushButton("Stop Song + Show")
        self.song_stop_button.setEnabled(False)
        self.song_stop_button.clicked.connect(self.stop_generated_show)
        action_row.addWidget(self.song_stop_button)
        layout.addLayout(action_row)
        export_row = QHBoxLayout()
        self.song_save_playlist_button = QPushButton("Save to Playlist")
        self.song_save_playlist_button.setEnabled(False)
        self.song_save_playlist_button.clicked.connect(self.save_to_playlist)
        export_row.addWidget(self.song_save_playlist_button)
        self.song_export_csv_button = QPushButton("Export CSV Timeline")
        self.song_export_csv_button.setEnabled(False)
        self.song_export_csv_button.clicked.connect(self.export_song_csv)
        export_row.addWidget(self.song_export_csv_button)
        self.song_export_json_button = QPushButton("Export JSON Show")
        self.song_export_json_button.setEnabled(False)
        self.song_export_json_button.clicked.connect(self.export_song_json)
        export_row.addWidget(self.song_export_json_button)
        layout.addLayout(export_row)
        self.song_progress = QProgressBar(); self.song_progress.setRange(0, 1000); self.song_progress.setValue(0)
        self.song_progress.setTextVisible(False)
        layout.addWidget(self.song_progress)
        self.song_position_label = QLabel("Playback: 00:00 / 00:00")
        layout.addWidget(self.song_position_label)
        self.song_status = QLabel("No song loaded. Choose a track to begin.")
        self.song_status.setWordWrap(True)
        layout.addWidget(self.song_status)
        self.song_summary = QLabel("Analysis: --")
        layout.addWidget(self.song_summary)
        stems_group = QGroupBox("Stem visualizers and DMX routing")
        stems_layout = QGridLayout(stems_group)
        self.song_stem_visualizers = {}
        self.song_stem_route_boxes = {}
        self.song_stem_mute_boxes = {}
        stem_specs = [
            ("drums", "Drums", "#f59e0b"), ("bass", "Bass", "#22c55e"),
            ("vocals", "Vocals", "#60a5fa"), ("other", "Other / Instruments", "#c084fc")
        ]
        for stem_index, (stem_key, stem_label, stem_color) in enumerate(stem_specs):
            card = QGroupBox(stem_label)
            card_layout = QVBoxLayout(card)
            status_label = QLabel("Not analyzed")
            status_label.setObjectName("stem_status_" + stem_key)
            card_layout.addWidget(status_label)
            visualizer = StemVisualizer(stem_color)
            visualizer.seek_requested.connect(self.seek_generated_show)
            card_layout.addWidget(visualizer)
            route_row = QHBoxLayout()
            route_row.addWidget(QLabel("Route:"))
            route = QComboBox()
            route.addItem("Normal (script decides)", "script")
            route.addItem("Unused", "unused")
            for channel_number in range(1, CHANNELS + 1):
                route.addItem(f"DMX Channel {channel_number}", f"channel:{channel_number}")
            route.setToolTip("Normal leaves this stem under the existing show-style logic. Unused ignores it. "
                             "Choosing a DMX channel makes this stem's level directly drive that channel (0-255).")
            route_row.addWidget(route, 1)
            mute_box = QCheckBox("Mute")
            mute_box.setEnabled(False)
            mute_box.setToolTip("Mute this isolated stem. Muting any stem crossfades playback into the "
                                "stem bus; unmuting all of them crossfades back to the original file.")
            mute_box.toggled.connect(lambda muted, key=stem_key: self.set_song_stem_muted(key, muted))
            route_row.addWidget(mute_box)
            card_layout.addLayout(route_row)
            stems_layout.addWidget(card, stem_index // 2, stem_index % 2)
            self.song_stem_visualizers[stem_key] = visualizer
            self.song_stem_route_boxes[stem_key] = route
            self.song_stem_mute_boxes[stem_key] = mute_box
        layout.addWidget(stems_group)
        self.song_table = QTableWidget(0, 10)
        self.song_table.setHorizontalHeaderLabels(["Time (s)", "Section", "Energy", "Bass", "Mids", "Highs", "Kick", "Snare", "Hi-hat", "DMX changes"])
        self.song_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.song_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.song_table.setAlternatingRowColors(True)
        layout.addWidget(self.song_table, 1)
        self.script_editor = QPlainTextEdit()
        self.script_editor.setPlainText(self.config.get("script_notes", ""))
        self.song_worker = None
        self.song_result = None
        self.song_frames = []
        self.song_play_index = 0
        self.song_original_values = None
        self.song_paused = False
        self.song_paused_channels = set()
        self.song_paused_channel_values = {}
        self.song_started_at = 0.0
        self.song_position_offset = 0.0
        self._restoring_song_values = False
        self.song_sample_rate = 0
        self.song_array = None
        self.song_stem_arrays = {}
        self.song_using_stems = False
        self.song_play_timer = QTimer(self)
        self.song_play_timer.setInterval(20)
        self.song_play_timer.timeout.connect(self.advance_generated_show)

        self.tabs.addTab(self._scrollable_tab(page), "Scripts")

    def choose_song_file(self):
        filename, _ = QFileDialog.getOpenFileName(
            self, "Choose a song", "", "Audio files (*.mp3 *.wav *.flac *.m4a *.ogg);;All files (*.*)")
        if filename:
            self.song_path_edit.setText(filename)
            self.song_status.setText("Ready to analyze. Existing generated show will be replaced after generation.")

    def generate_song_show(self):
        filename = self.song_path_edit.text().strip()
        if not filename or not Path(filename).is_file():
            QMessageBox.information(self, "Choose a song", "Choose an audio file first.")
            return
        if self.song_worker and self.song_worker.isRunning():
            return
        self.stop_generated_show()
        self.song_generate_button.setEnabled(False)
        self.song_progress.setRange(0, 0)
        self.song_status.setText("Starting full-track analysis...")
        self.song_worker = SongAnalysisWorker(filename, self)
        self.song_worker.status.connect(self.song_status.setText)
        self.song_worker.completed.connect(self.finish_song_generation)
        self.song_worker.failed.connect(self.song_generation_failed)
        self.song_worker.start()

    def song_generation_failed(self, message):
        self.song_progress.setRange(0, 1000)
        self.song_progress.setValue(0)
        self.song_generate_button.setEnabled(True)
        self.song_status.setText("Analysis failed: " + message)
        self.song_worker = None
        QMessageBox.warning(self, "Song analysis failed", message)

    def finish_song_generation(self, result):
        self.song_result = result
        self.song_array = None
        self.song_stem_arrays = {}
        self.song_frames = self.build_dmx_timeline(result["timeline"])
        self.song_generate_button.setEnabled(True)
        self.song_progress.setRange(0, 1000)
        self.song_progress.setValue(1000)
        self.song_play_button.setEnabled(bool(self.song_frames))
        self.song_save_playlist_button.setEnabled(bool(self.song_frames))
        self.song_export_csv_button.setEnabled(bool(self.song_frames))
        self.song_export_json_button.setEnabled(bool(self.song_frames))
        self.song_worker = None
        section_text = ", ".join(
            f"{section['label']} {section['start']:.0f}-{section['end']:.0f}s"
            for section in result.get("sections", []))
        self.song_summary.setText(
            f"Duration: {result['duration']:.1f}s  |  Beat-tracked tempo: {result['bpm']:.1f} BPM  |  "
            f"Key: {result.get('estimated_key', '?')} ({result.get('camelot_key', '?')})  |  "
            f"Beats: {result['beat_count']}  |  Kick: {result.get('kick_count', 0)}  |  "
            f"Snare: {result.get('snare_count', 0)}  |  Hi-hats: {result.get('hat_count', 0)}  |  "
            f"Sections: {len(result.get('sections', []))}  |  Timeline: {len(self.song_frames)} frames at {result.get('fps', 20):.1f} FPS  |  "
            f"Drum source: {result.get('stem_separation', 'Full mix fallback')}")
        separation_status = result.get("stem_separation", "Full mix fallback")
        separation_error = result.get("stem_error", "")
        self.song_status.setText(
            "Detected structure: " + (section_text if section_text else "No clear section changes") +
            ". Stem processing: " + separation_status +
            (". Stem details: " + separation_error[-700:] if separation_error else "") +
            ". Press Play Song + Show to start the audio and DMX together.")
        availability = result.get("stem_availability", {})
        stem_files = result.get("stem_files", {})
        stems_ready = all(availability.get(key, False) and stem_files.get(key)
                          and Path(stem_files[key]).is_file()
                          for key in ("drums", "bass", "vocals", "other"))
        for mute_box in self.song_stem_mute_boxes.values():
            mute_box.setEnabled(stems_ready)
            if not stems_ready:
                mute_box.setChecked(False)
        for mute_box in getattr(self, "music_stem_mute_boxes", {}).values():
            mute_box.setEnabled(stems_ready)
            if not stems_ready:
                mute_box.setChecked(False)
        status_widgets = {key: self.findChild(QLabel, "stem_status_" + key) for key in self.song_stem_visualizers}
        song_duration = float(result.get("duration", 0.0))
        for stem_key, visualizer in self.song_stem_visualizers.items():
            visualizer.set_data(result.get("stem_levels", {}).get(stem_key, []))
            visualizer.set_duration(song_duration)
            visualizer.set_playhead(0.0)
            label = status_widgets.get(stem_key)
            if label:
                if availability.get(stem_key, False):
                    label.setText("Demucs isolated stem")
                elif result.get("stem_separation") == "Full mix fallback":
                    label.setText("Unavailable — full-mix fallback")
                else:
                    label.setText("Stem unavailable — partial separation")
        for stem_key, visualizer in getattr(self, "music_stem_visualizers", {}).items():
            visualizer.set_data(result.get("stem_levels", {}).get(stem_key, []))
            visualizer.set_duration(song_duration)
            visualizer.set_playhead(0.0)
        self.populate_song_table()

    def set_song_stem_muted(self, stem_name, muted):
        AUDIO_ENGINE.set_stem_mute(stem_name, muted)

    def build_dmx_timeline(self, features):
        mapping = {name: box.value() for name, box in self.song_channel_boxes.items()}
        movement = self.song_movement_spin.value() / 255.0
        base_dim = self.song_dimmer_spin.value()
        style = self.song_style_combo.currentIndex()
        pan_min, pan_max = self.song_pan_min.value(), self.song_pan_max.value()
        tilt_min, tilt_max = self.song_tilt_min.value(), self.song_tilt_max.value()
        if pan_min > pan_max:
            pan_min, pan_max = pan_max, pan_min
        if tilt_min > tilt_max:
            tilt_min, tilt_max = tilt_max, tilt_min
        pattern_pan = (-1.0, 1.0, -0.62, 0.62)
        pattern_tilt = (0.25, 1.0, -0.25, -1.0)
        color_steps = (3, 10, 17, 24, 31, 38, 45, 52, 59, 66,
                       73, 80, 98, 129, 153, 171, 178, 185)

        fixtures = [{
            "name": "Default",
            "channel_offset": 0,
            "pan_channel": mapping.get("Pan / horizontal", 1),
            "tilt_channel": mapping.get("Tilt / vertical", 2),
            "dimmer_channel": mapping.get("Dimmer", 7),
            "color_channel": mapping.get("Color wheel", 8),
            "strobe_channel": mapping.get("Strobe / shutter", 6),
            "phase_offset": 0.0,
        }]

        frames = []
        for f in features:
            route_choices = {name: box.currentData()
                             for name, box in getattr(
                                 self, "song_stem_route_boxes", {}).items()}
            for stem_name, route in route_choices.items():
                if route == "script":
                    continue
                if stem_name == "drums":
                    f["kick"] = f["snare"] = f["hat"] = 0.0
                    f["kick_hit"] = f["snare_hit"] = f["hat_hit"] = False
                    f["kick_strength"] = f["snare_strength"] = f["hat_strength"] = 0.0
                    f["beat"] = f["downbeat"] = False
                    f["beat_in_bar"] = 0
                    f["beat_phase"] = 0.0
                    f["bar"] = 0
                    f["phrase"] = 0
                elif stem_name == "bass":
                    f["sub"] = f["bass"] = 0.0
                elif stem_name == "vocals":
                    f["mids"] = 0.0
                elif stem_name == "other":
                    f["highs"] = 0.0

            stem_levels_for_script = f.get("stem_levels", {})
            active_script_levels = [
                float(stem_levels_for_script.get(name, 0.0))
                for name, route in route_choices.items() if route == "script"]
            if len(active_script_levels) < 4:
                f["energy"] = (sum(active_script_levels) / len(active_script_levels)
                               if active_script_levels else 0.0)

            t = f["time"]
            energy, sub, bass = f["energy"], f["sub"], f["bass"]
            mids, highs, onset = f["mids"], f["highs"], f.get("onset", 0.0)
            beat_in_bar = f.get("beat_in_bar", 0)
            beat_phase = f.get("beat_phase", 0.0)
            bar = f.get("bar", 0)
            phrase = f.get("phrase", 0)
            section = f.get("section", "Verse / Groove")
            section_progress = f.get("section_progress", 0.0)
            downbeat = f.get("downbeat", False)
            transition = f.get("transition", False)

            if section == "Intro":
                section_gain = 0.22 + 0.38 * section_progress
            elif section == "Buildup":
                section_gain = 0.35 + 0.65 * section_progress
            elif section == "Drop":
                section_gain = 1.0
            elif section == "Breakdown":
                section_gain = 0.16 + 0.22 * energy
            elif section == "Outro":
                section_gain = max(0.08, 0.65 * (1.0 - section_progress))
            else:
                section_gain = 0.52 + 0.28 * energy

            dimmer = base_dim + int(energy * (85 + 135 * section_gain))
            if section in ("Breakdown", "Intro"):
                dimmer = base_dim + int(energy * 75)
            if downbeat and section in ("Drop", "Buildup"):
                dimmer += 35
            if transition:
                dimmer += 35 if section in ("Drop", "Buildup") else 10
            if style == 2 and section in ("Drop", "Buildup"):
                pulse = (bool(f.get("beat", False))
                         or bool(f.get("snare_hit", False))
                         or (onset > 0.62 and highs > 0.12))
                if pulse:
                    dimmer = 255
            elif style == 3:
                sens = (self.song_hat_sensitivity.value() / 100.0
                        if hasattr(self, "song_hat_sensitivity") else 0.55)
                hat_threshold = 0.78 - sens * 0.58
                hat_hit = (bool(f.get("hat_hit", False))
                           and float(f.get("hat_strength", 0.0)) >= hat_threshold)
                snare_hit = bool(f.get("snare_hit", False))
                kick_hit = bool(f.get("kick_hit", False))
                if hat_hit or snare_hit:
                    dimmer = 255
                elif kick_hit or (downbeat and section in ("Drop", "Buildup")):
                    dimmer = 205
                elif bool(f.get("beat", False)):
                    dimmer = max(base_dim, 90)
                else:
                    dimmer = 0
            dimmer = int(np_clip(dimmer, 0, 255))

            color_index = (phrase + (2 if section == "Drop" else 0)) % len(color_steps)
            if section == "Breakdown":
                color_index = (phrase // 2) % 4
            color_val = color_steps[color_index]

            color_blend = int(np_clip(25 + highs * 130 + mids * 45, 0, 255))
            strobe_val = 12
            frost_val = 0 if section in ("Drop", "Buildup") else 80
            fractal_active = (section == "Drop"
                              or (section == "Buildup" and section_progress > 0.65))
            fractal_val = 200 if fractal_active else 0
            if section == "Drop":
                fractal_spin = int(np_clip(45 + highs * 70, 3, 128))
            elif section == "Buildup":
                fractal_spin = int(np_clip(129 + section_progress * 100, 129, 229))
            else:
                fractal_spin = 0
            focus_val = int(np_clip(150 - highs * 35, 90, 180))
            blending_filter = (64 if fractal_active
                               and (mapping["Fractal"] or mapping["Fractal spin"])
                               else 200)

            frame_values = {}

            for fx in fixtures:
                base = int(fx.get("channel_offset", 0))
                phase = float(fx.get("phase_offset", 0.0))

                p0 = pattern_pan[beat_in_bar % 4]
                p1 = pattern_pan[(beat_in_bar + 1) % 4]
                t0 = pattern_tilt[beat_in_bar % 4]
                t1 = pattern_tilt[(beat_in_bar + 1) % 4]
                pan_target = p0 + (p1 - p0) * beat_phase
                tilt_target = t0 + (t1 - t0) * beat_phase

                phase_bp = beat_in_bar + beat_phase + phase * 4.0
                if style == 0:
                    pan_wave = math.sin((bar * 4 + phase_bp) * math.pi / 16)
                    tilt_wave = math.sin((bar * 4 + phase_bp) * math.pi / 20 + 1.1)
                    movement_gain = 0.42 + 0.36 * section_gain
                elif style == 1:
                    pan_wave = pan_target if downbeat else pan_target * 0.12
                    tilt_wave = tilt_target * (0.38 if downbeat else 0.08)
                    movement_gain = 0.30 + 0.30 * section_gain
                elif style == 2:
                    pan_wave = pan_target * (-1 if bar % 2 else 1)
                    tilt_wave = (math.sin(phase_bp * math.pi / 2)
                                 * (-1 if phrase % 2 else 1))
                    movement_gain = 0.72 + 0.28 * section_gain
                else:
                    pan_wave = pan_target
                    tilt_wave = tilt_target * (0.55 + 0.45 * bass)
                    movement_gain = section_gain
                    if downbeat or transition:
                        pan_wave = -pan_wave if (phrase % 2) else pan_wave

                pan_center = (pan_min + pan_max) / 2.0
                pan_half = (pan_max - pan_min) / 2.0
                tilt_center = (tilt_min + tilt_max) / 2.0
                tilt_half = (tilt_max - tilt_min) / 2.0

                def put(global_ch, value):
                    if not global_ch:
                        return
                    target = base + global_ch
                    if 1 <= target <= CHANNELS:
                        frame_values[target] = int(np_clip(value, 0, 255))

                put(mapping["Pan / horizontal"],
                    pan_center + pan_wave * pan_half * movement * movement_gain)
                put(mapping["Tilt / vertical"],
                    tilt_center + tilt_wave * tilt_half * movement * movement_gain)
                put(mapping["Dimmer"], dimmer)
                put(mapping["Strobe / shutter"], strobe_val)
                put(mapping["Color wheel"], color_val)
                put(mapping["Color blending"], color_blend)
                put(mapping["Frost"], frost_val)
                put(mapping["Fractal"], fractal_val)
                put(mapping["Fractal spin"], fractal_spin)
                put(mapping["Focus"], focus_val)
                put(mapping["Eight prism"], 0)
                put(mapping["Blending filter"], blending_filter)

            stem_levels = f.get("stem_levels", {})
            for stem_name, route in route_choices.items():
                if isinstance(route, str) and route.startswith("channel:"):
                    target_channel = int(route.split(":", 1)[1])
                    level = float(stem_levels.get(stem_name, 0.0))
                    frame_values[target_channel] = int(
                        np_clip(round(level * 255), 0, 255))

            frames.append({"time": t, "values": frame_values, "feature": f})
        return frames

    def populate_song_table(self):
        frames = self.song_frames
        selected = []
        last_second = -1
        for frame in frames:
            sec = int(frame["time"])
            if (frame["feature"]["beat"] or frame["feature"].get("hat_hit")
                    or frame["feature"].get("snare_hit") or frame["feature"].get("kick_hit")
                    or sec != last_second):
                selected.append(frame)
                last_second = sec
        selected = selected[:3000]
        self.song_table.setRowCount(len(selected))
        for row, frame in enumerate(selected):
            f = frame["feature"]
            changes = ", ".join(f"CH{ch}={val}" for ch, val in frame["values"].items())
            vals = [f"{frame['time']:.2f}", f["section"], f"{f['energy']:.2f}",
                    f"{f['bass']:.2f}", f"{f['mids']:.2f}", f"{f['highs']:.2f}",
                    "HIT" if f.get("kick_hit") else "", "HIT" if f.get("snare_hit") else "",
                    "HIT" if f.get("hat_hit") else "", changes]
            for col, value in enumerate(vals):
                self.song_table.setItem(row, col, QTableWidgetItem(value))

    def _get_song_source_sample_rate(self, filename):
        try:
            info = sf.info(str(filename))
            if info.samplerate > 0:
                return int(info.samplerate)
        except Exception:
            pass
        try:
            import librosa
            rate = int(librosa.get_samplerate(str(filename)))
            if rate > 0:
                return rate
        except Exception:
            pass
        try:
            with wave.open(str(filename), "rb") as audio_file:
                rate = int(audio_file.getframerate())
                if rate > 0:
                    return rate
        except Exception:
            pass
        raise RuntimeError("Could not read the original song's sample rate.")

    def _load_song_audio(self, filename, target_rate):
        import librosa
        audio, rate = librosa.load(filename, sr=target_rate, mono=False)
        if int(rate) != int(target_rate):
            raise RuntimeError(
                f"Decoder returned {rate} Hz, expected the original {target_rate} Hz.")
        audio = np.asarray(audio, dtype=np.float32)
        if audio.ndim == 1:
            audio = np_repeat_stereo(audio.reshape(-1, 1))
        else:
            audio = audio.T
            audio = np_repeat_stereo(audio)
        return np.nan_to_num(np.clip(audio, -1.0, 1.0))

    def _load_stem_arrays(self, target_rate):
        stem_files = self.song_result.get("stem_files", {})
        stem_arrays = {}
        for key in ("drums", "bass", "vocals", "other"):
            path = stem_files.get(key)
            if not path or not Path(path).is_file():
                continue
            data, rate = sf.read(str(path), dtype="float32", always_2d=True)
            if data.size == 0:
                continue
            data = np_repeat_stereo(data)
            if int(rate) != int(target_rate):
                from scipy.signal import resample_poly
                divisor = math.gcd(int(rate), int(target_rate))
                data = resample_poly(
                    data,
                    int(target_rate) // divisor,
                    int(rate) // divisor,
                    axis=0,
                )
            stem_arrays[key] = np.nan_to_num(np.clip(data, -1.0, 1.0)).astype(np.float32, copy=False)
        return stem_arrays

    def play_generated_show(self):
        if self.dj_mode_active:
            self.statusBar().showMessage(
                "Exit DJ mode to play the Scripts show, or drag the song onto a DJ deck.", 4000)
            return
        if not self.song_frames or not self.song_result:
            return
        filename = self.song_result.get("filename") or self.song_path_edit.text().strip()
        try:
            stem_files = self.song_result.get("stem_files", {})
            stems_ready = all(
                self.song_result.get("stem_availability", {}).get(key, False)
                and stem_files.get(key) and Path(stem_files[key]).is_file()
                for key in ("drums", "bass", "vocals", "other"))
            if self.song_array is None:
                rate = int(getattr(self, "song_sample_rate", 0) or 0)
                if rate <= 0:
                    rate = self._get_song_source_sample_rate(filename)
                    self.song_sample_rate = rate
                self.song_array = self._load_song_audio(filename, rate)
            if int(getattr(self, "song_sample_rate", 0) or 0) <= 0:
                self.song_sample_rate = self._get_song_source_sample_rate(filename)
            if stems_ready and not self.song_stem_arrays:
                self.song_stem_arrays = self._load_stem_arrays(self.song_sample_rate)
            mutes = {key: self.song_stem_mute_boxes[key].isChecked()
                     for key in ("drums", "bass", "vocals", "other")}
            AUDIO_ENGINE.load(
                self.song_array,
                self.song_stem_arrays if self.song_stem_arrays else None,
                self.song_sample_rate,
                mutes,
            )
            self.song_using_stems = bool(self.song_stem_arrays)
            AUDIO_ENGINE.set_master_gain(int(self.config.get("volume_percent", 100)) / 100.0)
        except Exception as exc:
            QMessageBox.warning(
                self, "Audio playback failed",
                "The song could not be played.\n\n" + str(exc))
            return
        self.song_original_values = list(self.values)
        self.song_play_index = 0
        self.song_paused = False
        self.song_position_offset = 0.0
        self.song_started_at = time.monotonic()
        self.song_play_button.setEnabled(False)
        self.song_pause_button.setEnabled(True)
        self.song_pause_button.setText("Pause")
        self.song_stop_button.setEnabled(True)
        self.song_status.setText("Playing audio and synchronized DMX show...")
        if hasattr(self, "music_play_pause_button"):
            self.music_play_pause_button.setText("⏸")
        self.song_play_timer.start()
        self.advance_generated_show()

    def seek_generated_show(self, seconds):
        if not self.song_frames or not self.song_result:
            return
        if self.song_original_values is None:
            self.play_generated_show()
            if self.song_original_values is None:
                return
        duration = max(0.0, float(self.song_result.get("duration", 0.0)))
        position = np_clip(float(seconds), 0.0, max(0.0, duration - 0.02))
        try:
            rate = int(getattr(self, "song_sample_rate", 0) or 0)
            if rate <= 0:
                filename = self.song_result.get("filename") or self.song_path_edit.text().strip()
                rate = self._get_song_source_sample_rate(filename)
                self.song_sample_rate = rate
            frame_index = int(round(position * rate))
            AUDIO_ENGINE.seek(frame_index)
            if self.song_paused:
                AUDIO_ENGINE.paused = False
                self.song_paused = False
                self.song_pause_button.setText("Pause")
                if hasattr(self, "music_play_pause_button"):
                    self.music_play_pause_button.setText("⏸")
                self.song_play_timer.start()
            self.song_position_offset = position
            self.song_started_at = time.monotonic()
            timeline_fps = float(self.song_result.get("fps", 10.0))
            self.song_play_index = min(
                len(self.song_frames), max(0, int(position * timeline_fps)))
            self.song_status.setText("Playing from selected position...")
            self.song_progress.setValue(
                int(np_clip(position / max(0.1, duration) * 1000.0, 0, 1000)))
            for visualizer in self.song_stem_visualizers.values():
                visualizer.set_playhead(position)
            for visualizer in getattr(self, "music_stem_visualizers", {}).values():
                visualizer.set_playhead(position)
            self.song_position_label.setText(
                f"Playback: {int(position // 60):02d}:{int(position % 60):02d} / "
                f"{int(duration // 60):02d}:{int(duration % 60):02d}")
            self.advance_generated_show()
        except Exception as exc:
            QMessageBox.warning(self, "Seek failed",
                                "Could not seek the song and stems together.\n\n" + str(exc))

    def toggle_song_pause(self):
        if not self.song_original_values:
            return
        try:
            if self.song_paused:
                AUDIO_ENGINE.paused = False
                self.song_started_at = time.monotonic()
                self.song_paused = False
                self.song_pause_button.setText("Pause")
                if hasattr(self, "music_play_pause_button"):
                    self.music_play_pause_button.setText("⏸")
                self.song_status.setText("Audio and DMX show resumed.")
                self.song_play_timer.start()
            else:
                self.song_position_offset = self.current_song_position()
                AUDIO_ENGINE.paused = True
                self.song_paused = True
                self.song_pause_button.setText("Resume")
                if hasattr(self, "music_play_pause_button"):
                    self.music_play_pause_button.setText("▶")
                self.song_status.setText("Audio and DMX show paused.")
                self.song_play_timer.stop()
        except Exception as exc:
            QMessageBox.warning(self, "Playback control failed", str(exc))

    def current_song_position(self):
        if self.song_paused:
            return max(0.0, float(self.song_position_offset))
        if not self.song_sample_rate:
            return max(0.0, float(self.song_position_offset))
        return AUDIO_ENGINE.current_frame() / float(self.song_sample_rate)

    def advance_generated_show(self):
        if not self.song_frames or self.song_paused:
            return
        position = self.current_song_position()
        if not AUDIO_ENGINE.is_playing() and position > 0.35:
            self.stop_generated_show(completed=True)
            return
        timeline_fps = float(self.song_result.get("fps", 10.0)) if self.song_result else 10.0
        index = min(len(self.song_frames) - 1, max(0, int(position * timeline_fps)))
        if index >= self.song_play_index:
            frame = self.song_frames[index]
            for channel, value in frame["values"].items():
                if channel not in self.song_paused_channels:
                    self.set_channel(channel, value)
            self.song_play_index = index + 1
        duration = max(0.1, float(self.song_result.get("duration", 0.1)))
        if position >= duration - 0.05:
            self.stop_generated_show(completed=True)

    def update_progress_ui(self):
        if self.dj_mode_active:
            self._update_dj_decks()
            self.advance_dj_show()
            return
        if not self.song_result or self.song_original_values is None:
            return
        position = self.current_song_position()
        duration = max(0.1, float(self.song_result.get("duration", 0.1)))
        value = int(np_clip(position / duration * 1000.0, 0, 1000))
        if value != self.song_progress.value():
            self.song_progress.setValue(value)
        if hasattr(self, "music_progress"):
            self.music_progress.setValue(value)
            self.music_position_label.setText(
                f"{int(position // 60):02d}:{int(position % 60):02d} / "
                f"{int(duration // 60):02d}:{int(duration % 60):02d}")
        for visualizer in getattr(self, "song_stem_visualizers", {}).values():
            visualizer.set_playhead(position)
        for visualizer in getattr(self, "music_stem_visualizers", {}).values():
            visualizer.set_playhead(position)
        self.song_position_label.setText(
            f"Playback: {int(position // 60):02d}:{int(position % 60):02d} / "
            f"{int(duration // 60):02d}:{int(duration % 60):02d}")

    def stop_generated_show(self, completed=False):
        if hasattr(self, "song_play_timer"):
            self.song_play_timer.stop()
        AUDIO_ENGINE.stop()
        self.song_paused = False
        self.song_paused_channels = set()
        self.song_paused_channel_values = {}
        if self.song_original_values is not None:
            self._restoring_song_values = True
            try:
                for channel, value in enumerate(self.song_original_values, start=1):
                    if self.values[channel - 1] != value:
                        self.set_channel(channel, value)
            finally:
                self._restoring_song_values = False
            self.song_original_values = None
        self.song_position_offset = 0.0
        self.playlist_active = False
        if hasattr(self, "music_play_pause_button"):
            self.music_play_pause_button.setText("▶")
        if hasattr(self, "song_play_button"):
            self.song_play_button.setEnabled(bool(self.song_frames))
            self.song_pause_button.setEnabled(False)
            self.song_pause_button.setText("Pause")
            self.song_stop_button.setEnabled(False)
        if hasattr(self, "song_status") and not completed:
            self.song_status.setText("Song and show stopped. DMX channels restored to their starting values.")
        elif hasattr(self, "song_status") and completed:
            self.song_progress.setValue(1000)
            self.song_status.setText("Song and show complete. DMX channels restored to their starting values.")
        if hasattr(self, "playlist_status") and not self.playlist_active:
            self.playlist_status.setText(f"{len(self.playlist)} song(s) in playlist.")

        self._schedule_preloads()

        if completed:
            self.song_array = None
            self.song_stem_arrays = {}
            self.song_sample_rate = 0
            if self.queue_entries:
                entry = self.queue_entries.pop(0)
                self._refresh_queue_ui()
                self._schedule_preloads()
                QTimer.singleShot(250, lambda e=entry: self._play_playlist_entry_dict(e))
            elif self.playlist_current_index is not None and self.playlist_current_index >= 0:
                nxt = self.playlist_current_index + 1
                if nxt < len(self.playlist):
                    QTimer.singleShot(250, lambda i=nxt: self._play_playlist_index(i))

    def export_song_csv(self):
        if not self.song_frames:
            return
        filename, _ = QFileDialog.getSaveFileName(self, "Export DMX timeline", "dmx_song_timeline.csv", "CSV files (*.csv)")
        if not filename:
            return
        channels = sorted({ch for frame in self.song_frames for ch in frame["values"]})
        try:
            with open(filename, "w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow([
                    "time_seconds", "section", "beat", "downbeat", "bar", "phrase",
                    "energy", "sub", "bass", "mids", "highs", "onset",
                    "kick", "snare", "hat", "kick_hit", "snare_hit", "hat_hit",
                    *[f"channel_{ch}" for ch in channels]])
                for frame in self.song_frames:
                    feature = frame["feature"]
                    writer.writerow([
                        frame["time"], feature["section"], int(feature["beat"]),
                        int(feature.get("downbeat", False)), feature.get("bar", 0),
                        feature.get("phrase", 0), feature["energy"], feature["sub"],
                        feature["bass"], feature["mids"], feature["highs"],
                        feature.get("onset", 0.0), feature.get("kick", 0.0),
                        feature.get("snare", 0.0), feature.get("hat", 0.0),
                        int(feature.get("kick_hit", False)), int(feature.get("snare_hit", False)),
                        int(feature.get("hat_hit", False)),
                        *[frame["values"].get(ch, "") for ch in channels]])
            self.song_status.setText("CSV timeline exported: " + filename)
        except OSError as exc:
            QMessageBox.warning(self, "Export failed", str(exc))

    def export_song_json(self):
        if not self.song_result or not self.song_frames:
            return
        filename, _ = QFileDialog.getSaveFileName(self, "Export DMX show", "dmx_song_show.json", "JSON files (*.json)")
        if not filename:
            return
        payload = {
            "format": "DMX Song Timeline", "version": 2,
            "source": self.song_result["filename"],
            "duration": self.song_result["duration"],
            "estimated_bpm": self.song_result["bpm"],
            "estimated_key": self.song_result.get("estimated_key", ""),
            "camelot_key": self.song_result.get("camelot_key", ""),
            "downbeat_phase": self.song_result.get("downbeat_phase", 0),
            "sections": self.song_result.get("sections", []),
            "frames": [{
                "time": f["time"],
                "channels": {str(k): v for k, v in f["values"].items()},
                "section": f["feature"]["section"],
                "beat": f["feature"]["beat"],
                "downbeat": f["feature"].get("downbeat", False),
                "bar": f["feature"].get("bar", 0),
                "phrase": f["feature"].get("phrase", 0),
                "energy": f["feature"]["energy"],
                "onset": f["feature"].get("onset", 0.0),
                "kick": f["feature"].get("kick", 0.0),
                "snare": f["feature"].get("snare", 0.0),
                "hat": f["feature"].get("hat", 0.0),
                "kick_hit": f["feature"].get("kick_hit", False),
                "snare_hit": f["feature"].get("snare_hit", False),
                "hat_hit": f["feature"].get("hat_hit", False)
            } for f in self.song_frames]
        }
        try:
            Path(filename).write_text(json.dumps(payload, indent=2), encoding="utf-8")
            self.song_status.setText("JSON show exported: " + filename)
        except OSError as exc:
            QMessageBox.warning(self, "Export failed", str(exc))

    # ---------------- MIDI PIANO TAB ----------------

    def build_midi_piano_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)

        heading = QLabel("MIDI PIANO — LIVE FIXTURE AIM")
        heading.setStyleSheet("font-size: 17px; font-weight: bold;")
        layout.addWidget(heading)

        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("MIDI input:"))
        self.piano_port_combo = QComboBox()
        self.piano_port_combo.setMinimumWidth(220)
        self.piano_port_combo.currentIndexChanged.connect(self._on_piano_port_changed)
        top_row.addWidget(self.piano_port_combo)

        self.piano_port_refresh_button = QPushButton("Refresh MIDI")
        self.piano_port_refresh_button.setToolTip(
            "Scan for MIDI keyboards again. Use this if a device was plugged in after launch.")
        self.piano_port_refresh_button.clicked.connect(self._refresh_midi_ports)
        top_row.addWidget(self.piano_port_refresh_button)

        self.piano_mid_load_button = QPushButton("Load .mid…")
        self.piano_mid_load_button.clicked.connect(self._load_midi_file)
        top_row.addWidget(self.piano_mid_load_button)

        self.piano_sf_button = QPushButton("Choose SoundFont…")
        self.piano_sf_button.setToolTip(
            "Pick a .sf2 file. The path is saved for future SoundFont-based synthesis. "
            "The built-in synth always plays regardless of the SoundFont setting.")
        self.piano_sf_button.clicked.connect(self._choose_soundfont)
        top_row.addWidget(self.piano_sf_button)

        self.piano_preview_3d_button = QPushButton("3D Preview")
        self.piano_preview_3d_button.setToolTip(
            "Open a live 3D view of the fixture, the active surface, and the beam. "
            "Right-drag to orbit, middle-drag to pan, scroll to zoom, Home to reset.")
        self.piano_preview_3d_button.clicked.connect(self._open_3d_preview)
        top_row.addWidget(self.piano_preview_3d_button)

        top_row.addStretch()
        layout.addLayout(top_row)

        self.piano_live_label = QLabel("Live: enabled")
        self.piano_live_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(self.piano_live_label)

        self.piano_midi_status_label = QLabel("")
        self.piano_midi_status_label.setWordWrap(True)
        layout.addWidget(self.piano_midi_status_label)

        settings_group = QGroupBox("Projection settings")
        settings_layout = QVBoxLayout(settings_group)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Surface:"))
        self.piano_surface_combo = QComboBox()
        self.piano_surface_combo.addItems(["Wall", "Ceiling"])
        self.piano_surface_combo.setCurrentIndex(0 if self.config.get("piano_surface", "wall") == "wall" else 1)
        self.piano_surface_combo.currentIndexChanged.connect(self._on_piano_settings_changed)
        row1.addWidget(self.piano_surface_combo)
        row1.addWidget(QLabel("Number of keys:"))
        self.piano_keys_spin = QSpinBox()
        self.piano_keys_spin.setRange(1, 200)
        self.piano_keys_spin.setValue(int(self.config.get("piano_num_keys", 88)))
        self.piano_keys_spin.valueChanged.connect(self._on_piano_keys_changed)
        row1.addWidget(self.piano_keys_spin)
        row1.addWidget(QLabel("Start note:"))
        self.piano_start_combo = QComboBox()
        for n in range(0, 128):
            self.piano_start_combo.addItem(f"{midi_note_name(n)} ({n})", n)
        start_idx = max(0, min(127, int(self.config.get("piano_start_note_midi", 21))))
        self.piano_start_combo.setCurrentIndex(start_idx)
        self.piano_start_combo.currentIndexChanged.connect(self._on_piano_keys_changed)
        row1.addWidget(self.piano_start_combo)
        row1.addStretch()
        settings_layout.addLayout(row1)

        lengths_group = QGroupBox("Geometry")
        lengths_layout = QGridLayout(lengths_group)
        self.piano_fixture_height_ft_spin = self._ft_in_row(
            lengths_layout, 0, "Fixture height above floor:",
            self.config.get("piano_fixture_height_ft", 1.0))
        self.piano_wall_distance_ft_spin = self._ft_in_row(
            lengths_layout, 1, "Distance to wall:",
            self.config.get("piano_wall_distance_ft", 10.0))
        self.piano_ceiling_height_ft_spin = self._ft_in_row(
            lengths_layout, 2, "Ceiling height above floor:",
            self.config.get("piano_ceiling_height_ft", 9.0))
        self.piano_forward_distance_ft_spin = self._ft_in_row(
            lengths_layout, 3, "Forward distance (ceiling):",
            self.config.get("piano_forward_distance_ft", 6.0))
        self.piano_strip_length_ft_spin = self._ft_in_row(
            lengths_layout, 4, "Strip length (side to side):",
            self.config.get("piano_strip_length_ft", 20.0))
        self.piano_left_offset_ft_spin = self._ft_in_row(
            lengths_layout, 5, "Left/right offset (negative = left):",
            self.config.get("piano_left_offset_ft", 0.0), allow_negative=True)
        self.piano_forward_back_offset_ft_spin = self._ft_in_row(
            lengths_layout, 6, "Forward/back offset (ceiling only):",
            self.config.get("piano_forward_back_offset_ft", 0.0), allow_negative=True)
        settings_layout.addWidget(lengths_group)

        aim_group = QGroupBox("Vertical aim (wall only)")
        aim_layout = QGridLayout(aim_group)
        self.piano_aim_combo = QComboBox()
        self.piano_aim_combo.addItems(["Degrees", "Target height"])
        self.piano_aim_combo.setCurrentIndex(0 if self.config.get("piano_aim_mode", "degrees") == "degrees" else 1)
        self.piano_aim_combo.currentIndexChanged.connect(self._on_piano_settings_changed)
        aim_layout.addWidget(QLabel("Mode:"), 0, 0)
        aim_layout.addWidget(self.piano_aim_combo, 0, 1)

        self.piano_tilt_deg_spin = QDoubleSpinBox()
        self.piano_tilt_deg_spin.setRange(-180.0, 180.0)
        self.piano_tilt_deg_spin.setDecimals(1)
        self.piano_tilt_deg_spin.setSuffix("°")
        self.piano_tilt_deg_spin.setValue(float(self.config.get("piano_tilt_deg", 0.0)))
        self.piano_tilt_deg_spin.valueChanged.connect(self._on_piano_settings_changed)
        aim_layout.addWidget(QLabel("Tilt (degrees):"), 1, 0)
        aim_layout.addWidget(self.piano_tilt_deg_spin, 1, 1)

        self.piano_target_height_ft_spin = self._ft_in_row(
            aim_layout, 2, "Target height above floor:",
            self.config.get("piano_target_height_ft", 5.0))
        settings_layout.addWidget(aim_group)

        range_group = QGroupBox("Mechanical range")
        range_layout = QGridLayout(range_group)
        range_layout.addWidget(QLabel("Pan min (°):"), 0, 0)
        self.piano_pan_min_spin = QDoubleSpinBox()
        self.piano_pan_min_spin.setRange(-720.0, 720.0)
        self.piano_pan_min_spin.setValue(float(self.config.get("piano_pan_min_deg", -270.0)))
        self.piano_pan_min_spin.valueChanged.connect(self._on_piano_settings_changed)
        range_layout.addWidget(self.piano_pan_min_spin, 0, 1)
        range_layout.addWidget(QLabel("Pan max (°):"), 0, 2)
        self.piano_pan_max_spin = QDoubleSpinBox()
        self.piano_pan_max_spin.setRange(-720.0, 720.0)
        self.piano_pan_max_spin.setValue(float(self.config.get("piano_pan_max_deg", 270.0)))
        self.piano_pan_max_spin.valueChanged.connect(self._on_piano_settings_changed)
        range_layout.addWidget(self.piano_pan_max_spin, 0, 3)
        range_layout.addWidget(QLabel("Tilt min (°):"), 1, 0)
        self.piano_tilt_min_spin = QDoubleSpinBox()
        self.piano_tilt_min_spin.setRange(-720.0, 720.0)
        self.piano_tilt_min_spin.setValue(float(self.config.get("piano_tilt_min_deg", -135.0)))
        self.piano_tilt_min_spin.valueChanged.connect(self._on_piano_settings_changed)
        range_layout.addWidget(self.piano_tilt_min_spin, 1, 1)
        range_layout.addWidget(QLabel("Tilt max (°):"), 1, 2)
        self.piano_tilt_max_spin = QDoubleSpinBox()
        self.piano_tilt_max_spin.setRange(-720.0, 720.0)
        self.piano_tilt_max_spin.setValue(float(self.config.get("piano_tilt_max_deg", 135.0)))
        self.piano_tilt_max_spin.valueChanged.connect(self._on_piano_settings_changed)
        range_layout.addWidget(self.piano_tilt_max_spin, 1, 3)
        settings_layout.addWidget(range_group)

        ch_group = QGroupBox("Channels and dimmer")
        ch_layout = QGridLayout(ch_group)
        ch_layout.addWidget(QLabel("Pan channel:"), 0, 0)
        self.piano_pan_channel_spin = QSpinBox()
        self.piano_pan_channel_spin.setRange(0, CHANNELS)
        self.piano_pan_channel_spin.setValue(int(self.config.get("piano_pan_channel", 1)))
        self.piano_pan_channel_spin.valueChanged.connect(self._on_piano_settings_changed)
        ch_layout.addWidget(self.piano_pan_channel_spin, 0, 1)
        ch_layout.addWidget(QLabel("Tilt channel:"), 0, 2)
        self.piano_tilt_channel_spin = QSpinBox()
        self.piano_tilt_channel_spin.setRange(0, CHANNELS)
        self.piano_tilt_channel_spin.setValue(int(self.config.get("piano_tilt_channel", 2)))
        self.piano_tilt_channel_spin.valueChanged.connect(self._on_piano_settings_changed)
        ch_layout.addWidget(self.piano_tilt_channel_spin, 0, 3)
        ch_layout.addWidget(QLabel("Dimmer channel:"), 0, 4)
        self.piano_dimmer_channel_spin = QSpinBox()
        self.piano_dimmer_channel_spin.setRange(0, CHANNELS)
        self.piano_dimmer_channel_spin.setValue(int(self.config.get("piano_dimmer_channel", 7)))
        self.piano_dimmer_channel_spin.valueChanged.connect(self._on_piano_settings_changed)
        ch_layout.addWidget(self.piano_dimmer_channel_spin, 0, 5)

        ch_layout.addWidget(QLabel("Dimmer mode:"), 1, 0)
        self.piano_flash_mode_combo = QComboBox()
        self.piano_flash_mode_combo.addItems(["Flash", "Fade"])
        self.piano_flash_mode_combo.setCurrentIndex(0 if self.config.get("piano_flash_mode", "flash") == "flash" else 1)
        self.piano_flash_mode_combo.currentIndexChanged.connect(self._on_piano_settings_changed)
        ch_layout.addWidget(self.piano_flash_mode_combo, 1, 1)
        ch_layout.addWidget(QLabel("Attack (ms):"), 1, 2)
        self.piano_attack_spin = QSpinBox()
        self.piano_attack_spin.setRange(1, 5000)
        self.piano_attack_spin.setValue(int(self.config.get("piano_attack_ms", 20)))
        self.piano_attack_spin.valueChanged.connect(self._on_piano_settings_changed)
        ch_layout.addWidget(self.piano_attack_spin, 1, 3)
        ch_layout.addWidget(QLabel("Hold decay (ms):"), 1, 4)
        self.piano_hold_decay_spin = QSpinBox()
        self.piano_hold_decay_spin.setRange(1, 60000)
        self.piano_hold_decay_spin.setValue(int(self.config.get("piano_hold_decay_ms", 2000)))
        self.piano_hold_decay_spin.valueChanged.connect(self._on_piano_settings_changed)
        ch_layout.addWidget(self.piano_hold_decay_spin, 1, 5)
        ch_layout.addWidget(QLabel("Release decay (ms):"), 2, 0)
        self.piano_release_decay_spin = QSpinBox()
        self.piano_release_decay_spin.setRange(1, 60000)
        self.piano_release_decay_spin.setValue(int(self.config.get("piano_release_decay_ms", 200)))
        self.piano_release_decay_spin.valueChanged.connect(self._on_piano_settings_changed)
        ch_layout.addWidget(self.piano_release_decay_spin, 2, 1)
        settings_layout.addWidget(ch_group)

        layout.addWidget(settings_group)

        self.piano_note_label = QLabel("—")
        self.piano_note_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.piano_note_label.setStyleSheet("font-size: 24px; font-weight: bold;")
        layout.addWidget(self.piano_note_label)

        self.piano_widget = PianoKeyboardWidget(self)
        self.piano_widget.set_range(
            int(self.config.get("piano_start_note_midi", 21)),
            int(self.config.get("piano_num_keys", 88)))
        self.piano_widget.note_pressed.connect(self._on_piano_key_pressed)
        self.piano_widget.note_released.connect(self._on_piano_key_released)
        layout.addWidget(self.piano_widget, 2)

        mid_row = QHBoxLayout()
        self.piano_mid_play_button = QPushButton("▶")
        self.piano_mid_play_button.clicked.connect(self._piano_mid_play)
        mid_row.addWidget(self.piano_mid_play_button)
        self.piano_mid_pause_button = QPushButton("⏸")
        self.piano_mid_pause_button.clicked.connect(self._piano_mid_pause)
        mid_row.addWidget(self.piano_mid_pause_button)
        self.piano_mid_stop_button = QPushButton("⏹")
        self.piano_mid_stop_button.clicked.connect(self._piano_mid_stop)
        mid_row.addWidget(self.piano_mid_stop_button)
        self.piano_mid_seek = QSlider(Qt.Orientation.Horizontal)
        self.piano_mid_seek.setRange(0, 1000)
        self.piano_mid_seek.sliderReleased.connect(self._piano_mid_seek)
        mid_row.addWidget(self.piano_mid_seek, 1)
        self.piano_mid_time_label = QLabel("0:00 / 0:00")
        mid_row.addWidget(self.piano_mid_time_label)
        layout.addLayout(mid_row)

        self.piano_settings_status = QLabel("")
        self.piano_settings_status.setWordWrap(True)
        layout.addWidget(self.piano_settings_status)

        self._update_piano_visibility()
        self._refresh_midi_ports()

        self.tabs.addTab(self._scrollable_tab(page), "MIDI Piano")

    def _ft_in_row(self, layout, row, label_text, value_ft, allow_negative=False):
        feet, inches = ft_to_ft_in(value_ft)
        layout.addWidget(QLabel(label_text), row, 0)
        feet_spin = QSpinBox()
        feet_spin.setRange(-1000 if allow_negative else 0, 1000)
        feet_spin.setSuffix(" ft")
        feet_spin.setValue(feet)
        layout.addWidget(feet_spin, row, 1)
        inches_spin = QSpinBox()
        inches_spin.setRange(0, 11)
        inches_spin.setSuffix(" in")
        inches_spin.setValue(inches)
        layout.addWidget(inches_spin, row, 2)

        holder = {"feet": feet_spin, "inches": inches_spin}

        def changed(*_):
            self._on_piano_settings_changed()

        feet_spin.valueChanged.connect(changed)
        inches_spin.valueChanged.connect(changed)

        class _Accessor:
            def __init__(self, d):
                self.d = d
            def value(self):
                return ft_in_to_ft(self.d["feet"].value(), self.d["inches"].value())
            def set_value(self, v):
                f, i = ft_to_ft_in(v)
                self.d["feet"].blockSignals(True)
                self.d["inches"].blockSignals(True)
                self.d["feet"].setValue(f)
                self.d["inches"].setValue(i)
                self.d["feet"].blockSignals(False)
                self.d["inches"].blockSignals(False)
        return _Accessor(holder)

    def _on_piano_keys_changed(self, *_):
        self.piano_widget.set_range(
            int(self.piano_start_combo.currentIndex()),
            int(self.piano_keys_spin.value()))
        self._on_piano_settings_changed()

    def _on_piano_settings_changed(self, *_):
        self._update_piano_visibility()
        self.config["piano_surface"] = "wall" if self.piano_surface_combo.currentIndex() == 0 else "ceiling"
        self.config["piano_fixture_height_ft"] = self.piano_fixture_height_ft_spin.value()
        self.config["piano_wall_distance_ft"] = self.piano_wall_distance_ft_spin.value()
        self.config["piano_ceiling_height_ft"] = self.piano_ceiling_height_ft_spin.value()
        self.config["piano_forward_distance_ft"] = self.piano_forward_distance_ft_spin.value()
        self.config["piano_strip_length_ft"] = self.piano_strip_length_ft_spin.value()
        self.config["piano_left_offset_ft"] = self.piano_left_offset_ft_spin.value()
        self.config["piano_forward_back_offset_ft"] = self.piano_forward_back_offset_ft_spin.value()
        self.config["piano_aim_mode"] = "degrees" if self.piano_aim_combo.currentIndex() == 0 else "height"
        self.config["piano_tilt_deg"] = float(self.piano_tilt_deg_spin.value())
        self.config["piano_target_height_ft"] = self.piano_target_height_ft_spin.value()
        self.config["piano_pan_min_deg"] = float(self.piano_pan_min_spin.value())
        self.config["piano_pan_max_deg"] = float(self.piano_pan_max_spin.value())
        self.config["piano_tilt_min_deg"] = float(self.piano_tilt_min_spin.value())
        self.config["piano_tilt_max_deg"] = float(self.piano_tilt_max_spin.value())
        self.config["piano_pan_channel"] = int(self.piano_pan_channel_spin.value())
        self.config["piano_tilt_channel"] = int(self.piano_tilt_channel_spin.value())
        self.config["piano_dimmer_channel"] = int(self.piano_dimmer_channel_spin.value())
        self.config["piano_flash_mode"] = "flash" if self.piano_flash_mode_combo.currentIndex() == 0 else "fade"
        self.config["piano_attack_ms"] = int(self.piano_attack_spin.value())
        self.config["piano_hold_decay_ms"] = int(self.piano_hold_decay_spin.value())
        self.config["piano_release_decay_ms"] = int(self.piano_release_decay_spin.value())
        self.config["piano_num_keys"] = int(self.piano_keys_spin.value())
        self.config["piano_start_note_midi"] = int(self.piano_start_combo.currentIndex())

    def _update_piano_visibility(self):
        is_wall = self.piano_surface_combo.currentIndex() == 0
        self.piano_wall_distance_ft_spin.d["feet"].setVisible(is_wall)
        self.piano_wall_distance_ft_spin.d["inches"].setVisible(is_wall)
        self.piano_ceiling_height_ft_spin.d["feet"].setEnabled(not is_wall)
        self.piano_ceiling_height_ft_spin.d["inches"].setEnabled(not is_wall)
        self.piano_forward_distance_ft_spin.d["feet"].setEnabled(not is_wall)
        self.piano_forward_distance_ft_spin.d["inches"].setEnabled(not is_wall)
        self.piano_forward_back_offset_ft_spin.d["feet"].setEnabled(not is_wall)
        self.piano_forward_back_offset_ft_spin.d["inches"].setEnabled(not is_wall)
        aim = self.piano_aim_combo.currentIndex() == 0
        self.piano_tilt_deg_spin.setEnabled(is_wall and aim)
        self.piano_target_height_ft_spin.d["feet"].setEnabled(is_wall and not aim)
        self.piano_target_height_ft_spin.d["inches"].setEnabled(is_wall and not aim)
        self.piano_aim_combo.setEnabled(is_wall)

    def _choose_soundfont(self):
        filename, _ = QFileDialog.getOpenFileName(
            self, "Choose a SoundFont (.sf2)", "",
            "SoundFonts (*.sf2);;All files (*.*)")
        if not filename:
            return
        if not Path(filename).is_file():
            return
        self.config["piano_soundfont"] = filename
        PIANO_SYNTH.set_soundfont(filename)
        self.piano_settings_status.setText(
            f"SoundFont path saved: {Path(filename).name}. "
            "The built-in synth keeps playing the same tone regardless of the SoundFont.")

    def _open_3d_preview(self):
        if self.preview_3d_window is not None and self.preview_3d_window.isVisible():
            self.preview_3d_window.raise_()
            self.preview_3d_window.activateWindow()
            return
        try:
            window = Preview3DWindow(self, self)
        except Exception as exc:
            QMessageBox.warning(
                self, "3D Preview failed",
                "Could not open the 3D preview. Make sure pyvista and pyvistaqt are installed:\n"
                "    py -m pip install pyvista pyvistaqt\n\nDetails: " + str(exc))
            return
        window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        window.destroyed.connect(self._on_3d_preview_destroyed)
        self.preview_3d_window = window
        window.show()
        window.raise_()

    def _on_3d_preview_destroyed(self, *_):
        self.preview_3d_window = None

    def _refresh_midi_ports(self):
        try:
            import mido
        except ImportError:
            self.piano_port_combo.blockSignals(True)
            self.piano_port_combo.clear()
            self.piano_port_combo.addItem("mido not installed", "")
            self.piano_port_combo.blockSignals(False)
            self.piano_midi_status_label.setText(
                "MIDI input requires the mido and python-rtmidi packages. "
                "Install them with: py -m pip install mido python-rtmidi")
            return
        current = self.piano_port_name or self.config.get("piano_midi_port", "")
        self.piano_port_combo.blockSignals(True)
        self.piano_port_combo.clear()
        try:
            ports = mido.get_input_names()
        except Exception as exc:
            ports = []
            self.piano_midi_status_label.setText(f"Could not list MIDI ports: {exc}")
        if not ports:
            self.piano_port_combo.addItem("No MIDI devices connected", "")
            self.piano_midi_status_label.setText(
                "No MIDI keyboards detected. Plug one in, then click Refresh MIDI. "
                "The on-screen piano still works for mouse and .mid playback.")
        else:
            for name in ports:
                self.piano_port_combo.addItem(name, name)
            self.piano_midi_status_label.setText(
                f"Detected {len(ports)} MIDI input(s).")
        idx = self.piano_port_combo.findData(current)
        if idx >= 0:
            self.piano_port_combo.setCurrentIndex(idx)
        elif ports:
            self.piano_port_combo.setCurrentIndex(0)
        self.piano_port_combo.blockSignals(False)
        self._on_piano_port_changed()

    def _on_piano_port_changed(self, *_):
        try:
            import mido
        except ImportError:
            return
        name = self.piano_port_combo.currentData() or ""
        if not name:
            self._close_piano_port()
            return
        if name == self.piano_port_name and self.piano_midi_port is not None:
            self.config["piano_midi_port"] = name
            return
        self._close_piano_port()
        try:
            port = mido.open_input(name, callback=self._on_midi_message)
            self.piano_midi_port = port
            self.piano_port_name = name
            self.config["piano_midi_port"] = name
            self.piano_midi_status_label.setText(f"Connected to: {name}")
        except Exception as exc:
            self.piano_midi_status_label.setText(f"Could not open MIDI port: {exc}")

    def _close_piano_port(self):
        if self.piano_midi_port is not None:
            try:
                self.piano_midi_port.close()
            except Exception:
                pass
        self.piano_midi_port = None
        self.piano_port_name = ""

    def _on_midi_message(self, msg):
        if msg.type == "note_on" and msg.velocity > 0:
            self._piano_note_on(int(msg.note), int(msg.velocity))
        elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
            self._piano_note_off(int(msg.note))

    def _is_piano_live_enabled(self):
        if self.song_original_values is not None and not self.song_paused:
            return False
        return True

    def _piano_note_on(self, midi_note, velocity=100):
        if not self._is_piano_live_enabled():
            return
        if not (0 <= midi_note <= 127):
            return
        self.piano_held_notes[midi_note] = {"press_time": time.monotonic(), "released": False}
        self.piano_last_note_midi = midi_note
        try:
            PIANO_SYNTH.note_on(midi_note, velocity)
        except Exception:
            pass
        self.piano_widget.set_active_notes(set(self.piano_held_notes.keys()))
        self._update_piano_note_label()
        self._apply_piano_dmx()

    def _piano_note_off(self, midi_note):
        if midi_note in self.piano_held_notes:
            del self.piano_held_notes[midi_note]
        try:
            PIANO_SYNTH.note_off(midi_note)
        except Exception:
            pass
        self.piano_widget.set_active_notes(set(self.piano_held_notes.keys()))
        self._update_piano_note_label()
        if not self.piano_held_notes:
            self.piano_dimmer_target = 0.0
            self.piano_dimmer_target_time = time.monotonic()
            mode = self.piano_flash_mode_combo.currentIndex()
            if mode == 0:
                self.piano_dimmer_current = 0.0

    def _on_piano_key_pressed(self, midi_note):
        if not self._is_piano_live_enabled():
            return
        self._piano_note_on(midi_note, 100)

    def _on_piano_key_released(self, midi_note):
        self._piano_note_off(midi_note)

    def _update_piano_note_label(self):
        if not self.piano_held_notes and not self.piano_mid_note_active:
            self.piano_note_label.setText("—")
            return
        names = [midi_note_name(n) for n in sorted(set(self.piano_held_notes.keys()) | set(self.piano_mid_note_active.keys()))]
        self.piano_note_label.setText("   ".join(names))

    def _apply_piano_dmx(self):
        if not self._is_piano_live_enabled():
            return
        if self.piano_last_note_midi is None:
            return
        aim = self._compute_piano_aim(self.piano_last_note_midi)
        if aim is None:
            return
        pan_deg, tilt_deg = aim
        self.piano_last_pan_deg = pan_deg
        self.piano_last_tilt_deg = tilt_deg

        pan_ch = int(self.piano_pan_channel_spin.value())
        tilt_ch = int(self.piano_tilt_channel_spin.value())
        dim_ch = int(self.piano_dimmer_channel_spin.value())

        mech_pan_min = float(self.piano_pan_min_spin.value())
        mech_pan_max = float(self.piano_pan_max_spin.value())
        mech_tilt_min = float(self.piano_tilt_min_spin.value())
        mech_tilt_max = float(self.piano_tilt_max_spin.value())

        pan_travel_min, pan_travel_max, tilt_travel_min, tilt_travel_max = self._piano_travel_bounds()

        pan_val = self._angle_to_dmx(
            pan_deg, mech_pan_min, mech_pan_max, pan_travel_min, pan_travel_max,
        )
        tilt_val = self._angle_to_dmx(
            tilt_deg, mech_tilt_min, mech_tilt_max, tilt_travel_min, tilt_travel_max,
        )

        if pan_ch > 0:
            self.set_channel(pan_ch, pan_val)
        if tilt_ch > 0:
            self.set_channel(tilt_ch, tilt_val)
        if dim_ch > 0:
            self.piano_dimmer_target = 255.0
            self.piano_dimmer_target_time = time.monotonic()
            mode = self.piano_flash_mode_combo.currentIndex()
            if mode == 0:
                self.piano_dimmer_current = 255.0
                self.set_channel(dim_ch, 255)
            else:
                attack = max(1, self.piano_attack_spin.value())
                self.piano_dimmer_rate = 255.0 / attack

    def _piano_travel_bounds(self):
        try:
            L = float(self.piano_strip_length_ft_spin.value())
            left_offset = float(self.piano_left_offset_ft_spin.value())
            surface = self.piano_surface_combo.currentIndex()
            fixture_h = float(self.piano_fixture_height_ft_spin.value())
            if L <= 0:
                return (None, None, None, None)
            x_left = -L / 2.0 + left_offset
            x_right = L / 2.0 + left_offset
            x_lo = min(x_left, x_right)
            x_hi = max(x_left, x_right)
            if surface == 0:
                dist = float(self.piano_wall_distance_ft_spin.value())
                if dist <= 0:
                    return (None, None, None, None)
                pan_min = math.degrees(math.atan2(x_lo, dist))
                pan_max = math.degrees(math.atan2(x_hi, dist))
                if self.piano_aim_combo.currentIndex() == 0:
                    t = float(self.piano_tilt_deg_spin.value())
                    tilt_min = tilt_max = t
                else:
                    target_h = float(self.piano_target_height_ft_spin.value())
                    t = math.degrees(math.atan2(target_h - fixture_h, dist))
                    tilt_min = tilt_max = t
                return (pan_min, pan_max, tilt_min, tilt_max)
            ceil_h = float(self.piano_ceiling_height_ft_spin.value())
            fwd = float(self.piano_forward_distance_ft_spin.value())
            fb_off = float(self.piano_forward_back_offset_ft_spin.value())
            forward = fwd + fb_off
            if abs(forward) < 1e-6:
                forward = 0.001
            pan_min = math.degrees(math.atan2(x_lo, forward))
            pan_max = math.degrees(math.atan2(x_hi, forward))
            vertical = max(0.001, ceil_h - fixture_h)
            near = min(abs(x_lo), abs(x_hi))
            far = max(abs(x_lo), abs(x_hi))
            tilt_near = math.degrees(math.atan2(vertical, math.hypot(forward, near)))
            tilt_far = math.degrees(math.atan2(vertical, math.hypot(forward, far)))
            return (pan_min, pan_max, min(tilt_near, tilt_far), max(tilt_near, tilt_far))
        except Exception:
            return (None, None, None, None)

    def _angle_to_dmx(self, angle_deg, mech_min, mech_max,
                     travel_min=None, travel_max=None):
        def _finite(x):
            return x is not None and not (isinstance(x, float) and (math.isnan(x) or math.isinf(x)))

        mech_ok = _finite(mech_min) and _finite(mech_max) and (mech_max > mech_min)
        travel_ok = (
            _finite(travel_min) and _finite(travel_max)
            and (travel_max - travel_min) > 1e-6
        )

        if travel_ok:
            lo, hi = travel_min, travel_max
            if mech_ok:
                lo = max(mech_min, lo)
                hi = min(mech_max, hi)
            if hi - lo <= 1e-6:
                travel_ok = False

        if not travel_ok:
            if not mech_ok:
                return 0
            lo, hi = mech_min, mech_max

        v = (float(angle_deg) - lo) / (hi - lo)
        v = max(0.0, min(1.0, v))
        return int(round(v * 255.0))

    def _compute_piano_aim(self, midi_note):
        n = int(self.piano_keys_spin.value())
        if n < 1:
            return None
        start = int(self.piano_start_combo.currentIndex())
        idx = max(0, min(n - 1, midi_note - start))
        L = self.piano_strip_length_ft_spin.value()
        if L <= 0:
            return None
        left_offset = self.piano_left_offset_ft_spin.value()

        surface = self.piano_surface_combo.currentIndex()
        fixture_h = self.piano_fixture_height_ft_spin.value()
        if surface == 0:
            dist = self.piano_wall_distance_ft_spin.value()
            if dist <= 0:
                return None
            x = (-L / 2.0) + (idx + 0.5) * (L / n)
            pan = math.degrees(math.atan2(x + left_offset, dist))
            if self.piano_aim_combo.currentIndex() == 0:
                tilt = float(self.piano_tilt_deg_spin.value())
            else:
                target_h = self.piano_target_height_ft_spin.value()
                tilt = math.degrees(math.atan2(target_h - fixture_h, dist))
            return pan, tilt
        else:
            ceil_h = self.piano_ceiling_height_ft_spin.value()
            fwd = self.piano_forward_distance_ft_spin.value()
            fb_off = self.piano_forward_back_offset_ft_spin.value()
            vertical = ceil_h - fixture_h
            if vertical <= 0:
                return None
            x = (-L / 2.0) + (idx + 0.5) * (L / n) + left_offset
            forward = fwd + fb_off
            pan = math.degrees(math.atan2(x, forward)) if forward != 0 else 0.0
            horizontal = math.hypot(forward, x)
            tilt = math.degrees(math.atan2(vertical, horizontal))
            return pan, tilt

    def _update_piano_live_label(self):
        if not hasattr(self, "piano_live_label"):
            return
        if self._is_piano_live_enabled():
            self.piano_live_label.setText("Live: enabled")
        else:
            self.piano_live_label.setText("Live: disabled — pause or stop the Scripts show to play")

    def tick_piano(self):
        dim_ch = int(self.piano_dimmer_channel_spin.value())
        if dim_ch > 0:
            mode = self.piano_flash_mode_combo.currentIndex()
            if self.piano_held_notes:
                if mode == 0:
                    self.piano_dimmer_current = 255.0
                else:
                    target_hold = 150.0
                    attack = max(1, self.piano_attack_spin.value())
                    if self.piano_dimmer_current < 255.0:
                        step = 255.0 * 0.015 / (attack / 1000.0)
                        self.piano_dimmer_current = min(255.0, self.piano_dimmer_current + step)
                    else:
                        hold_ms = max(1, self.piano_hold_decay_spin.value())
                        step = 255.0 * 0.015 / (hold_ms / 1000.0)
                        self.piano_dimmer_current = max(target_hold, self.piano_dimmer_current - step)
                self.set_channel(dim_ch, int(round(self.piano_dimmer_current)))
            else:
                if self.piano_dimmer_current > 0:
                    rel_ms = max(1, self.piano_release_decay_spin.value())
                    step = 255.0 * 0.015 / (rel_ms / 1000.0)
                    self.piano_dimmer_current = max(0.0, self.piano_dimmer_current - step)
                    self.set_channel(dim_ch, int(round(self.piano_dimmer_current)))

        if self.piano_mid_playing:
            now = time.monotonic()
            self._advance_midi_playback(now)

        self._update_piano_live_label()

    def _advance_midi_playback(self, now):
        dt = now - self._piano_mid_last_tick
        self._piano_mid_last_tick = now
        if not self.piano_mid_playing:
            return
        self.piano_mid_time += dt
        if self.piano_mid_time >= self.piano_mid_duration:
            self._piano_mid_stop()
            return
        while self.piano_mid_last_played < len(self.piano_mid_events):
            ev = self.piano_mid_events[self.piano_mid_last_played]
            if ev["time"] > self.piano_mid_time:
                break
            self.piano_mid_last_played += 1
            if ev["type"] == "on":
                self.piano_mid_note_active[ev["note"]] = True
                try:
                    PIANO_SYNTH.note_on(ev["note"], ev.get("velocity", 100))
                except Exception:
                    pass
                self.piano_last_note_midi = ev["note"]
                self._apply_piano_dmx()
                self._update_piano_note_label()
            else:
                try:
                    PIANO_SYNTH.note_off(ev["note"])
                except Exception:
                    pass
                self.piano_mid_note_active.pop(ev["note"], None)
                self._update_piano_note_label()
            self.piano_widget.set_active_notes(
                set(self.piano_held_notes.keys()) | set(self.piano_mid_note_active.keys()))
        if self.piano_mid_duration > 0:
            self.piano_mid_seek.blockSignals(True)
            self.piano_mid_seek.setValue(int(1000 * self.piano_mid_time / self.piano_mid_duration))
            self.piano_mid_seek.blockSignals(False)
        self.piano_mid_time_label.setText(
            f"{int(self.piano_mid_time // 60)}:{int(self.piano_mid_time % 60):02d} / "
            f"{int(self.piano_mid_duration // 60)}:{int(self.piano_mid_duration % 60):02d}")

    def _load_midi_file(self):
        filename, _ = QFileDialog.getOpenFileName(
            self, "Load MIDI file", "", "MIDI files (*.mid *.midi);;All files (*.*)")
        if not filename:
            return
        try:
            import mido
            mid = mido.MidiFile(filename)
        except Exception as exc:
            QMessageBox.warning(self, "Load failed", str(exc))
            return
        events = []
        current_time = 0.0
        for msg in mid:
            current_time += msg.time
            if msg.type == "note_on" and msg.velocity > 0:
                events.append({"time": current_time, "type": "on", "note": msg.note, "velocity": msg.velocity})
            elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
                events.append({"time": current_time, "type": "off", "note": msg.note})
        self.piano_mid_events = events
        self.piano_mid_duration = max((ev["time"] for ev in events), default=0.0)
        self.piano_mid_filename = filename
        self.piano_mid_time = 0.0
        self.piano_mid_last_played = 0
        self.piano_mid_playing = False
        self.piano_mid_paused = False
        self.piano_mid_note_active = {}
        self.piano_widget.set_active_notes(set(self.piano_held_notes.keys()))
        self.piano_mid_seek.setValue(0)
        self.piano_mid_time_label.setText(
            f"0:00 / {int(self.piano_mid_duration // 60)}:{int(self.piano_mid_duration % 60):02d}")
        self._update_piano_note_label()

    def _piano_mid_play(self):
        if not self._is_piano_live_enabled():
            self.statusBar().showMessage("Cannot play MIDI while a Scripts show is running.", 4000)
            return
        if not self.piano_mid_events:
            self.statusBar().showMessage("Load a .mid file first.", 3000)
            return
        self.piano_mid_playing = True
        self.piano_mid_paused = False
        self._piano_mid_last_tick = time.monotonic()

    def _piano_mid_pause(self):
        self.piano_mid_playing = False

    def _piano_mid_stop(self):
        for note in list(self.piano_mid_note_active.keys()):
            try:
                PIANO_SYNTH.note_off(note)
            except Exception:
                pass
        self.piano_mid_note_active = {}
        self.piano_mid_playing = False
        self.piano_mid_paused = False
        self.piano_mid_time = 0.0
        self.piano_mid_last_played = 0
        self.piano_widget.set_active_notes(set(self.piano_held_notes.keys()))
        self.piano_mid_seek.setValue(0)
        self.piano_mid_time_label.setText("0:00 / 0:00")
        pan_ch = int(self.piano_pan_channel_spin.value())
        tilt_ch = int(self.piano_tilt_channel_spin.value())
        dim_ch = int(self.piano_dimmer_channel_spin.value())
        if pan_ch > 0:
            self.set_channel(pan_ch, 0)
        if tilt_ch > 0:
            self.set_channel(tilt_ch, 0)
        if dim_ch > 0:
            self.piano_dimmer_current = 0.0
            self.set_channel(dim_ch, 0)
        self._update_piano_note_label()

    def _piano_mid_seek(self):
        if not self.piano_mid_events or self.piano_mid_duration <= 0:
            return
        new_time = self.piano_mid_duration * self.piano_mid_seek.value() / 1000.0
        self.piano_mid_time = new_time
        for note in list(self.piano_mid_note_active.keys()):
            try:
                PIANO_SYNTH.note_off(note)
            except Exception:
                pass
        self.piano_mid_note_active = {}
        idx = 0
        while idx < len(self.piano_mid_events) and self.piano_mid_events[idx]["time"] <= new_time:
            idx += 1
        self.piano_mid_last_played = idx
        for ev in self.piano_mid_events[:idx]:
            if ev["type"] == "on":
                self.piano_mid_note_active[ev["note"]] = True
            elif ev["note"] in self.piano_mid_note_active:
                self.piano_mid_note_active.pop(ev["note"], None)
        for note in self.piano_mid_note_active.keys():
            try:
                PIANO_SYNTH.note_on(note, 100)
            except Exception:
                pass
        self.piano_widget.set_active_notes(
            set(self.piano_held_notes.keys()) | set(self.piano_mid_note_active.keys()))
        self._update_piano_note_label()

    # ---------------- MIC TAB ----------------

    def build_mic_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        heading = QLabel("MICROPHONE AND VOICE COMMAND")
        heading.setStyleSheet("font-size: 17px; font-weight: bold;")
        layout.addWidget(heading)
        layout.addWidget(QLabel(
            "Talk-through mixes the microphone into the output while a 'Talk into output (while held)' binding is held. "
            "Voice command listens for the configured words while a 'Voice command (while held)' binding is held. "
            "Recognition is fully offline via Vosk."))
        form = QFormLayout()
        self.mic_device_combo = QComboBox()
        self.mic_device_combo.setEditable(False)
        form.addRow("Microphone input:", self.mic_device_combo)
        self.volume_label = QLabel(f"Song volume: {int(self.config.get('volume_percent', 100))}%")
        form.addRow("Song volume:", self.volume_label)
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(int(self.config.get("volume_percent", 100)))
        self.volume_slider.valueChanged.connect(self._on_volume_slider_changed)
        form.addRow("Volume:", self.volume_slider)
        self.vosk_status_label = QLabel(self._vosk_status_text())
        self.vosk_status_label.setWordWrap(True)
        form.addRow("Voice model:", self.vosk_status_label)
        layout.addLayout(form)
        refresh_button = QPushButton("Refresh Microphones")
        refresh_button.clicked.connect(self.populate_mic_devices)
        layout.addWidget(refresh_button)
        test_row = QHBoxLayout()
        self.talk_test_button = QPushButton("Test talk-through (hold)")
        self.talk_test_button.pressed.connect(self._begin_mic_test)
        self.talk_test_button.released.connect(self._end_mic_test)
        test_row.addWidget(self.talk_test_button)
        layout.addLayout(test_row)
        layout.addWidget(QLabel(
            "If talk-through does not work: verify the microphone appears in the list, and that "
            "Windows allows the app to use the microphone. The output device is the system default."))
        layout.addStretch()
        self.tabs.addTab(self._scrollable_tab(page), "Mic / Voice")
        self.populate_mic_devices()

    def _vosk_status_text(self):
        if VOSK_MODEL_DIR.is_dir():
            return f"Found: {VOSK_MODEL_DIR.name}"
        try:
            import vosk  # noqa: F401
            return f"Vosk installed, but no model at {VOSK_MODEL_DIR.name}."
        except ImportError:
            return "Vosk not installed. Install with: py -m pip install vosk"

    def populate_mic_devices(self):
        if not hasattr(self, "mic_device_combo"):
            return
        current = self.config.get("mic_input_device", "")
        self.mic_device_combo.clear()
        self.mic_device_combo.addItem("System default", "")
        try:
            for idx, info in enumerate(sd.query_devices()):
                if info.get("max_input_channels", 0) > 0:
                    self.mic_device_combo.addItem(f"{idx}: {info['name']}", info["name"])
        except Exception:
            pass
        index = self.mic_device_combo.findData(current)
        if index >= 0:
            self.mic_device_combo.setCurrentIndex(index)
        self.mic_device_combo.currentIndexChanged.connect(self._on_mic_device_changed)

    def _on_mic_device_changed(self, *_):
        self.config["mic_input_device"] = self.mic_device_combo.currentData() or ""
        if self.mic_active or self.voice_worker_active:
            MIC_INPUT.stop()
            try:
                MIC_INPUT.start(self.config.get("mic_input_device", ""))
            except Exception as exc:
                self.statusBar().showMessage(f"Microphone restart failed: {exc}", 5000)

    def _on_volume_slider_changed(self, value):
        self._set_volume_everywhere(int(value))

    def _begin_mic_test(self):
        try:
            AUDIO_ENGINE.set_mic_enabled(True)
            MIC_INPUT.start(self.config.get("mic_input_device", ""))
            self.statusBar().showMessage("Talk-through test ON.", 2000)
        except Exception as exc:
            self.statusBar().showMessage(f"Microphone failed: {exc}", 5000)

    def _end_mic_test(self):
        if not self.mic_active:
            AUDIO_ENGINE.set_mic_enabled(False)
            if not self.voice_worker_active:
                MIC_INPUT.stop()
        self.statusBar().showMessage("Talk-through test OFF.", 2000)

    # ---------------- CONTROLLER ----------------

    def refresh_controller(self):
        try:
            import pygame
            pygame.event.pump()
            pygame.joystick.init()
            count = pygame.joystick.get_count()
            if count == 0:
                if self.controller:
                    try:
                        self.controller.quit()
                    except Exception:
                        pass
                self.controller = None
                self.controller_id = None
                self.controller_held_controls.clear()
                self.controller_status_text = "No controller detected. Connect an Xbox controller."
                self.remove_controller_tab()
            else:
                joystick = pygame.joystick.Joystick(0)
                joystick.init()
                new_id = joystick.get_instance_id()
                if self.controller is None or self.controller_id != new_id:
                    if self.controller:
                        try:
                            self.controller.quit()
                        except Exception:
                            pass
                    self.controller = joystick
                    self.controller_id = new_id
                    self.controller_held_controls.clear()
                    self.create_controller_tab()
                else:
                    if joystick is not self.controller:
                        try:
                            joystick.quit()
                        except Exception:
                            pass
                self.controller_status_text = (
                    f"Connected: {self.controller.get_name()} "
                    f"({self.controller.get_numbuttons()} buttons, "
                    f"{self.controller.get_numaxes()} axes, "
                    f"{self.controller.get_numhats()} hats)")
        except Exception as error:
            self.controller = None
            self.controller_id = None
            self.controller_held_controls.clear()
            self.controller_status_text = f"Controller error: {error}"
            self.remove_controller_tab()
        if hasattr(self, "controller_status"):
            self.controller_status.setText(self.controller_status_text)

    def create_controller_tab(self):
        self.remove_controller_tab()
        page = QWidget()
        main_layout = QVBoxLayout(page)
        self.controller_axis_labels = {}
        self.controller_axis_bars = {}
        self.controller_button_labels = {}
        self.controller_dpad_labels = {}
        self.sensitivity_sliders = {}
        self.sensitivity_labels = {}
        header = QLabel("LIVE XBOX CONTROLLER INPUT")
        header.setStyleSheet("font-size: 16px; font-weight: bold;")
        main_layout.addWidget(header)
        self.controller_monitor_status = QLabel("Waiting for input...")
        main_layout.addWidget(self.controller_monitor_status)
        axis_group = QGroupBox("Joystick Axes and Triggers")
        axis_layout = QGridLayout(axis_group)
        axis_layout.addWidget(QLabel("Control"), 0, 0)
        axis_layout.addWidget(QLabel("Raw Value"), 0, 1)
        axis_layout.addWidget(QLabel("Position"), 0, 2)
        for row, name in enumerate(AXIS_MAP, start=1):
            name_label = QLabel(name)
            value_label = QLabel("0.000")
            value_label.setMinimumWidth(0)
            bar = QProgressBar()
            bar.setRange(-1000, 1000)
            bar.setValue(0)
            bar.setTextVisible(False)
            bar.setMinimumWidth(0)
            axis_layout.addWidget(name_label, row, 0)
            axis_layout.addWidget(value_label, row, 1)
            axis_layout.addWidget(bar, row, 2)
            self.controller_axis_labels[name] = value_label
            self.controller_axis_bars[name] = bar
        main_layout.addWidget(axis_group)
        sensitivity_group = QGroupBox("Independent Sensitivity")
        sensitivity_layout = QGridLayout(sensitivity_group)
        sensitivity_layout.setColumnStretch(0, 0)
        sensitivity_layout.setColumnStretch(1, 1)
        sensitivity_layout.setColumnStretch(2, 0)
        sensitivity_settings = [
            ("Left Stick X", "left_x_sensitivity"), ("Left Stick Y", "left_y_sensitivity"),
            ("Right Stick X", "right_x_sensitivity"), ("Right Stick Y", "right_y_sensitivity"),
            ("Left Trigger", "left_trigger_sensitivity"), ("Right Trigger", "right_trigger_sensitivity")
        ]
        for row, (label_text, config_key) in enumerate(sensitivity_settings):
            label = QLabel(f"{label_text}:")
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(10, 200)
            slider.setSingleStep(5)
            slider.setPageStep(10)
            slider.setValue(int(self.config.get(config_key, 100)))
            percentage = QSpinBox()
            percentage.setRange(1, 2147483647)
            percentage.setSuffix("%")
            percentage.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
            percentage.setMinimumWidth(0)
            percentage.setValue(int(self.config.get(config_key, 100)))
            slider.blockSignals(True)
            slider.setValue(min(200, percentage.value()))
            slider.blockSignals(False)
            slider.valueChanged.connect(
                lambda value, key=config_key, spin=percentage:
                    self.update_sensitivity_from_slider(key, value, spin))
            percentage.valueChanged.connect(
                lambda value, key=config_key, s=slider:
                    self.update_sensitivity_from_spin(key, value, s))
            sensitivity_layout.addWidget(label, row, 0)
            sensitivity_layout.addWidget(slider, row, 1)
            sensitivity_layout.addWidget(percentage, row, 2)
            self.sensitivity_sliders[config_key] = slider
            self.sensitivity_labels[config_key] = percentage
        main_layout.addWidget(sensitivity_group)
        buttons_group = QGroupBox("Buttons")
        buttons_layout = QGridLayout(buttons_group)
        for position, name in enumerate(BUTTON_MAP.values()):
            label = QLabel(f"{name}: RELEASED")
            label.setMinimumWidth(0)
            label.setWordWrap(True)
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            self.controller_button_labels[name] = label
            buttons_layout.addWidget(label, position // 3, position % 3)
        main_layout.addWidget(buttons_group)
        dpad_group = QGroupBox("D-pad")
        dpad_layout = QGridLayout(dpad_group)
        dpad_positions = {"D-pad Up": (0, 1), "D-pad Left": (1, 0), "D-pad Right": (1, 2), "D-pad Down": (2, 1)}
        for name, position in dpad_positions.items():
            label = QLabel(f"{name}: RELEASED")
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setMinimumWidth(0)
            label.setWordWrap(True)
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            self.controller_dpad_labels[name] = label
            dpad_layout.addWidget(label, *position)
        main_layout.addWidget(dpad_group)
        main_layout.addWidget(QLabel("Raw axes usually range from -1.000 to +1.000. Sensitivity settings are saved with Save All Settings."))
        main_layout.addStretch()
        self.controller_page = self._scrollable_tab(page)
        self.tabs.addTab(self.controller_page, "Controller")

    def remove_controller_tab(self):
        if self.controller_page is None:
            return
        index = self.tabs.indexOf(self.controller_page)
        if index >= 0:
            self.tabs.removeTab(index)
        self.controller_page.deleteLater()
        self.controller_page = None
        self.controller_axis_labels = {}
        self.controller_axis_bars = {}
        self.controller_button_labels = {}
        self.controller_dpad_labels = {}
        self.sensitivity_sliders = {}
        self.sensitivity_labels = {}

    def update_sensitivity_from_slider(self, config_key, value, percentage):
        percentage.blockSignals(True)
        percentage.setValue(value)
        percentage.blockSignals(False)
        self.config[config_key] = value

    def update_sensitivity_from_spin(self, config_key, value, slider):
        slider.blockSignals(True)
        slider.setValue(min(200, value))
        slider.blockSignals(False)
        self.config[config_key] = value

    def update_controller_monitor(self, current_controls):
        if not self.controller or self.controller_page is None:
            return
        try:
            for name, axis_index in AXIS_MAP.items():
                raw = (float(self.controller.get_axis(axis_index))
                       if axis_index < self.controller.get_numaxes() else 0.0)
                label = self.controller_axis_labels.get(name)
                bar = self.controller_axis_bars.get(name)
                if label:
                    label.setText(f"{raw:+.3f}")
                if bar:
                    bar.setValue(max(-1000, min(1000, round(raw * 1000))))
            for name, label in self.controller_button_labels.items():
                pressed = name in current_controls
                label.setText(f"{name}: {'PRESSED' if pressed else 'RELEASED'}")
                label.setStyleSheet("font-weight: bold; color: #16803c;" if pressed else "")
            for name, label in self.controller_dpad_labels.items():
                pressed = name in current_controls
                label.setText(f"{name}: {'PRESSED' if pressed else 'RELEASED'}")
                label.setStyleSheet("font-weight: bold; color: #16803c;" if pressed else "")
            self.controller_monitor_status.setText(f"Live input active | {self.controller.get_name()}")
        except Exception:
            self.refresh_controller()

    def get_controller_controls(self):
        if not self.controller:
            return set()
        controls = set()
        try:
            import pygame
            pygame.event.pump()
            for button_index, name in BUTTON_MAP.items():
                if (button_index < self.controller.get_numbuttons()
                        and self.controller.get_button(button_index)):
                    controls.add(name)
            if self.controller.get_numhats() > 0:
                x, y = self.controller.get_hat(0)
                if y == 1:
                    controls.add("D-pad Up")
                if y == -1:
                    controls.add("D-pad Down")
                if x == -1:
                    controls.add("D-pad Left")
                if x == 1:
                    controls.add("D-pad Right")
        except Exception:
            self.refresh_controller()
        return controls

    def read_axis(self, name):
        if not self.controller:
            return None
        axis_name = (STICK_DIRECTION_MAP[name][0] if name in STICK_DIRECTION_MAP else name)
        if axis_name not in AXIS_MAP:
            return None
        axis_index = AXIS_MAP[axis_name]
        try:
            if axis_index >= self.controller.get_numaxes():
                return None
            return float(self.controller.get_axis(axis_index))
        except Exception:
            self.refresh_controller()
            return None

    def _sensitivity(self, config_key):
        percentage = self.sensitivity_labels.get(config_key)
        if percentage is not None:
            return max(1, percentage.value()) / 100.0
        return max(1.0, float(self.config.get(config_key, 100))) / 100.0

    def axis_to_dmx(self, control, raw):
        raw = max(-1.0, min(1.0, float(raw)))
        if control in TRIGGER_CONTROLS:
            config_key = ("left_trigger_sensitivity" if control == "Left Trigger"
                          else "right_trigger_sensitivity")
            sensitivity = self._sensitivity(config_key)
            normalized = max(0.0, min(1.0, (raw + 1.0) / 2.0))
            normalized = max(0.0, min(1.0, normalized * sensitivity))
            return max(0, min(255, round(normalized * 255)))
        if control in STICK_CONTROLS:
            axis_name = STICK_DIRECTION_MAP[control][0]
            config_key = {
                "Left Stick X": "left_x_sensitivity",
                "Left Stick Y": "left_y_sensitivity",
                "Right Stick X": "right_x_sensitivity",
                "Right Stick Y": "right_y_sensitivity"
            }[axis_name]
            sensitivity = self._sensitivity(config_key)
            deadzone = self.deadzone.value() / 100.0
            _, direction_sign = STICK_DIRECTION_MAP[control]
            directional_raw = raw * direction_sign
            if directional_raw <= deadzone:
                return 0
            magnitude = (directional_raw - deadzone) / max(0.001, 1.0 - deadzone)
            magnitude = max(0.0, min(1.0, magnitude * sensitivity))
            return max(0, min(255, round(magnitude * 255)))
        return 128

    def axis_is_active(self, control, raw):
        if control in STICK_CONTROLS:
            _, direction_sign = STICK_DIRECTION_MAP[control]
            return raw * direction_sign > self.deadzone.value() / 100.0
        if control in TRIGGER_CONTROLS:
            trigger_amount = max(0.0, min(1.0, (raw + 1.0) / 2.0))
            return trigger_amount > 0.05
        return False

    def poll_controller(self, elapsed):
        if not self.controller:
            return
        current_controls = self.get_controller_controls()
        pressed = current_controls - self.controller_held_controls
        released = self.controller_held_controls - current_controls
        elapsed = max(0.0, min(float(elapsed), 0.1))
        axis_hold_rate = 10.0
        axis_hold_step = 1
        for index, binding in enumerate(self.bindings):
            if binding.get("device", "Keyboard") != "Xbox Controller":
                continue
            control = binding.get("control", binding.get("key", ""))
            mode = binding.get("mode", 0)
            is_stick = control in STICK_CONTROLS
            is_trigger = control in TRIGGER_CONTROLS

            if is_stick and mode in (5, 6):
                raw = self.read_axis(control)
                if raw is None:
                    continue
                channel = binding["channel"]
                active = self.axis_is_active(control, raw)
                if mode == 5:
                    value = self.axis_to_dmx(control, raw)
                    if self.values[channel - 1] != value:
                        self.set_channel(channel, value)
                elif mode == 6:
                    if not active:
                        self.joystick_accumulators[index] = 0.0
                        continue
                    axis_name = STICK_DIRECTION_MAP[control][0]
                    config_key = {
                        "Left Stick X": "left_x_sensitivity",
                        "Left Stick Y": "left_y_sensitivity",
                        "Right Stick X": "right_x_sensitivity",
                        "Right Stick Y": "right_y_sensitivity"
                    }[axis_name]
                    sensitivity = self._sensitivity(config_key)
                    deadzone = self.deadzone.value() / 100.0
                    _, direction_sign = STICK_DIRECTION_MAP[control]
                    directional_raw = max(0.0, raw * direction_sign)
                    magnitude = (directional_raw - deadzone) / max(0.001, 1.0 - deadzone)
                    magnitude = max(0.0, min(1.0, magnitude))
                    accumulator = self.joystick_accumulators.get(index, 0.0)
                    accumulator += elapsed * axis_hold_rate * magnitude * sensitivity
                    increments = int(accumulator)
                    self.joystick_accumulators[index] = accumulator - increments
                    if increments > 0:
                        current = self.values[channel - 1]
                        change = increments * axis_hold_step * direction_sign
                        self.set_channel(channel, max(0, min(255, current + change)))
                continue

            if is_trigger and mode in (7, 8, 9):
                raw = self.read_axis(control)
                if raw is None:
                    continue
                channel = binding["channel"]
                config_key = ("left_trigger_sensitivity" if control == "Left Trigger"
                              else "right_trigger_sensitivity")
                sensitivity = self._sensitivity(config_key)
                trigger_amount = max(0.0, min(1.0, (raw + 1.0) / 2.0))
                active = trigger_amount > 0.05
                if mode == 7:
                    value = self.axis_to_dmx(control, raw)
                    if self.values[channel - 1] != value:
                        self.set_channel(channel, value)
                elif mode in (8, 9):
                    if not active:
                        self.joystick_accumulators[index] = 0.0
                        continue
                    accumulator = self.joystick_accumulators.get(index, 0.0)
                    accumulator += elapsed * axis_hold_rate * trigger_amount * sensitivity
                    increments = int(accumulator)
                    self.joystick_accumulators[index] = accumulator - increments
                    if increments > 0:
                        current = self.values[channel - 1]
                        direction = 1 if mode == 8 else -1
                        change = increments * axis_hold_step * direction
                        self.set_channel(channel, max(0, min(255, current + change)))
                continue

            if is_stick or is_trigger:
                continue

            if control in pressed:
                self.binding_pressed(index)
            if control in released:
                self.binding_released(index)
        self.controller_held_controls = current_controls
        self.update_controller_monitor(current_controls)

    def tick(self):
        now = time.monotonic()
        elapsed = min(now - self.last_tick, 0.1)
        self.last_tick = now
        if now - self.last_controller_check >= 1.0:
            self.last_controller_check = now
            if not self.controller:
                self.refresh_controller()
            else:
                try:
                    if not self.controller.get_init():
                        self.refresh_controller()
                except Exception:
                    self.refresh_controller()
        self.poll_controller(elapsed)
        rate = self.speed.value()
        step = 1
        for index in list(self.held_bindings):
            if index >= len(self.bindings):
                self.held_bindings.pop(index, None)
                self.key_accumulators.pop(index, None)
                continue
            binding = self.bindings[index]
            channel = binding["channel"]
            mode = binding.get("mode", 0)
            self.key_accumulators[index] = self.key_accumulators.get(index, 0.0) + elapsed * rate
            increments = int(self.key_accumulators[index])
            if increments <= 0:
                continue
            self.key_accumulators[index] -= increments
            current = self.values[channel - 1]
            change = increments * step
            if mode == 3:
                self.set_channel(channel, min(255, current + change))
            elif mode == 4:
                self.set_channel(channel, max(0, current - change))

    def send_frame(self):
        if not self.output.device:
            return
        try:
            self.output.send(self._gated_values())
        except Exception as error:
            self.output.disconnect()
            self.output_status.setText("Output error")
            QMessageBox.warning(self, "DMX output stopped", str(error))

    def _gated_values(self):
        return list(self.values)

    def build_output_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        form = QFormLayout()
        self.baud_combo = QComboBox()
        self.baud_combo.addItems(["250000", "115200", "57600", "38400", "9600"])
        self.baud_combo.setCurrentText(str(self.config.get("baud", 250000)))
        form.addRow("Baud rate:", self.baud_combo)
        self.break_spin = QSpinBox()
        self.break_spin.setRange(88, 1000)
        self.break_spin.setSuffix(" microseconds")
        self.break_spin.setValue(int(self.config.get("break_us", 100)))
        form.addRow("Break duration:", self.break_spin)
        layout.addLayout(form)

        serial_group = QGroupBox("USB-DMX serial port")
        serial_layout = QVBoxLayout(serial_group)
        serial_layout.addWidget(QLabel(
            "Right-click a COM port to connect / disconnect the DMX output. Only one port "
            "at a time can drive DMX hardware."))
        self.serial_port_list = QListWidget()
        self.serial_port_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.serial_port_list.customContextMenuRequested.connect(self._on_serial_port_context)
        serial_layout.addWidget(self.serial_port_list)
        layout.addWidget(serial_group)

        self.output_status = QLabel("Output disconnected")
        layout.addWidget(self.output_status)
        layout.addWidget(QLabel(
            "Standard DMX uses 250000 baud, 8 data bits, no parity, and 2 stop bits."))
        layout.addStretch()
        self.tabs.addTab(self._scrollable_tab(page), "Output Settings")

        self._enabled_ports = set()
        self._active_serial_port = ""
        self.refresh_ports()

    def refresh_ports(self):
        if hasattr(self, "serial_port_list"):
            self.serial_port_list.clear()
            try:
                ports = list(list_ports.comports())
            except Exception:
                ports = []
            if not ports:
                empty = QListWidgetItem(
                    "(No COM ports detected — plug in your USB-DMX adapter)")
                empty.setFlags(Qt.ItemFlag.NoItemFlags)
                self.serial_port_list.addItem(empty)
            else:
                for p in ports:
                    port_name = p.device
                    connected = port_name in self._enabled_ports
                    item = QListWidgetItem(
                        f"{port_name}  —  {p.description}"
                        + ("  [CONNECTED]" if connected else ""))
                    item.setData(Qt.ItemDataRole.UserRole, port_name)
                    item.setForeground(
                        QColor(120, 230, 140) if connected else QColor(220, 226, 236))
                    self.serial_port_list.addItem(item)

    def _on_serial_port_context(self, pos):
        item = self.serial_port_list.itemAt(pos)
        if item is None:
            return
        port_name = item.data(Qt.ItemDataRole.UserRole)
        if not port_name:
            return
        menu = QMenu(self)
        if port_name in self._enabled_ports:
            action = QAction(f"Disconnect {port_name}", self)
            action.triggered.connect(lambda: self._disconnect_single_port(port_name))
        else:
            action = QAction(f"Connect {port_name}", self)
            action.triggered.connect(lambda: self._connect_serial_port(port_name))
        menu.addAction(action)
        menu.exec(self.serial_port_list.mapToGlobal(pos))

    def _disconnect_single_port(self, port_name):
        try:
            self.output.disconnect(port_name)
        except Exception:
            pass
        self._enabled_ports.discard(port_name)
        if self._enabled_ports:
            self.output_status.setText(
                f"Output connected: {', '.join(sorted(self._enabled_ports))}")
        else:
            self.output_status.setText("Output disconnected")
        self.refresh_ports()

    def _connect_serial_port(self, port_name):
        try:
            self.output.connect(
                port_name,
                int(self.baud_combo.currentText()),
                self.break_spin.value())
            self._enabled_ports.add(port_name)
            self.output_status.setText(
                f"Output connected: {', '.join(sorted(self._enabled_ports))}")
        except Exception as exc:
            self.output_status.setText(f"Connection failed: {exc}")
            QMessageBox.warning(self, "Connection failed", str(exc))
        self.refresh_ports()

    def _disconnect_serial_port(self):
        for port_name in list(self._enabled_ports):
            try:
                self.output.disconnect(port_name)
            except Exception:
                pass
        self._enabled_ports.clear()
        self.output_status.setText("Output disconnected")
        self.refresh_ports()

    def save_settings(self):
        self.config["port"] = (
            ",".join(sorted(self._enabled_ports))
            if self._enabled_ports
            else self.config.get("port", "")
        )
        self.config["baud"] = int(self.baud_combo.currentText())
        self.config["break_us"] = self.break_spin.value()
        self.config["bindings"] = self.bindings
        self.config["values"] = self.values
        self.config["channel_names"] = self.channel_names
        self.config["steps_per_second"] = self.speed.value()
        self.config.pop("step_size", None)
        self.config["deadzone"] = self.deadzone.value()
        self.config["script_notes"] = self.script_editor.toPlainText() if hasattr(self, "script_editor") else self.config.get("script_notes", "")
        for key, default in SENSITIVITY_DEFAULTS.items():
            percentage = self.sensitivity_labels.get(key)
            if percentage is not None:
                self.config[key] = percentage.value()
            else:
                self.config.setdefault(key, default)
        try:
            save_config(self.config)
            self.statusBar().showMessage("All settings saved.", 3000)
        except OSError as error:
            QMessageBox.warning(self, "Save Error", str(error))

    def closeEvent(self, event):
        self._close_piano_port()
        self.stop_generated_show()
        AUDIO_ENGINE.stop_all_effects()
        AUDIO_ENGINE.close()
        MIC_INPUT.stop()
        if self.voice_worker is not None:
            self.voice_worker.stop()
            self.voice_worker = None
        if self.song_worker is not None:
            try:
                self.song_worker.quit()
                self.song_worker.wait(50)
            except Exception:
                pass
            self.song_worker = None
        if self.key_backfill_worker is not None:
            try:
                self.key_backfill_worker.quit()
                self.key_backfill_worker.wait(50)
            except Exception:
                pass
            self.key_backfill_worker = None
        for worker in list(self.preload_workers.values()):
            try:
                worker.quit()
                worker.wait(50)
            except Exception:
                pass
        self.preload_workers.clear()
        self.control_timer.stop()
        self.progress_timer.stop()
        self.output_timer.stop()
        if hasattr(self, "piano_timer"):
            self.piano_timer.stop()
        if self.preview_3d_window is not None:
            try:
                self.preview_3d_window.close()
            except Exception:
                pass
            self.preview_3d_window = None
        self.output.disconnect()
        try:
            self.save_settings()
        finally:
            try:
                import pygame
                if self.controller:
                    try:
                        self.controller.quit()
                    except Exception:
                        pass
                pygame.quit()
            except Exception:
                pass
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = DMXController()
    window.show()
    sys.exit(app.exec())