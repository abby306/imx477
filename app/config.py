"""
Configuration: schema, defaults, validation, atomic persistence.

Everything here is editable from the web UI — the client never opens this file.
config.json sits next to app.py and is gitignored, so `git pull` never clobbers
a tuned belt setup.
"""

import copy
import json
import os
import tempfile
import threading

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

# --------------------------------------------------------------------------- #
# Defaults. Anything missing from config.json is filled from here, so a partial
# or hand-edited file can never crash the app.
# --------------------------------------------------------------------------- #
DEFAULTS = {
    "sensor": {
        "enabled": True,
        "pin": 17,
        "pull_up": True,
        "invert": False,            # flip if a replacement sensor idles the other way
        "bounce_time_ms": 20,       # debounce — without this one object = several events
        "trigger_edge": "break",    # break | clear | both
        "trigger_delay_ms": 0,      # the knob: centres the item in frame
        "cooldown_ms": 1000,        # min gap between events
        "min_break_ms": 30,         # shorter than this = dust/noise, ignore
        "max_break_ms": 5000,       # longer than this = jam
    },
    "capture": {
        "mode": "fast_grab",        # fast_grab (~40ms) | full_res (~1-2s, misses movers)
        "burst_count": 1,
        "burst_interval_ms": 60,
        "jpeg_quality": 92,
        "cameras": [0, 1],          # which cameras a beam break fires
        "per_camera_delay_ms": {"0": 0, "1": 0},
        "sweep_delays_ms": [0, 50, 100, 150, 200],
    },
    "storage": {
        "session_tag": "run",
        "min_free_mb": 500,         # auto-disarm below this
        "item_length_mm": 0,        # set this and break duration gives you belt speed
    },
    "stream": {
        "preview_size": [1024, 768],
        "record_size": [2028, 1520],
        "still_size": [4056, 3040],
        "framerate": 30,
        "preview_fps": 20,
        "preview_quality": 80,
    },
    "cameras": {
        "0": {
            "auto_exposure": True,
            "exposure_us": 10000,
            "gain": 1.0,
            "auto_wb": True,
            "brightness": 0.0,
            "contrast": 1.0,
            "saturation": 1.0,
            "sharpness": 1.0,
        },
        "1": {
            "auto_exposure": True,
            "exposure_us": 10000,
            "gain": 1.0,
            "auto_wb": True,
            "brightness": 0.0,
            "contrast": 1.0,
            "saturation": 1.0,
            "sharpness": 1.0,
        },
    },
    "overlays": {
        "grid": False,
        "crosshair": True,
        "focusbox": True,
        "peaking": False,
        "peak_threshold": 40,
    },
    "integration": {
        "webhook_url": "",
    },
    "ui": {
        "advanced": False,          # simple by default; the switch reveals dev controls
    },
    "presets": {
        "motion freeze": {
            "cameras": {
                "0": {"auto_exposure": False, "exposure_us": 2000, "gain": 8.0},
                "1": {"auto_exposure": False, "exposure_us": 2000, "gain": 8.0},
            }
        },
        "production lock": {
            "cameras": {
                "0": {"auto_exposure": False, "auto_wb": False, "exposure_us": 3000, "gain": 6.0},
                "1": {"auto_exposure": False, "auto_wb": False, "exposure_us": 3000, "gain": 6.0},
            }
        },
    },
}

# (min, max) clamps — keeps a bad UI value or hand edit from wedging the app.
CLAMPS = {
    "sensor.pin": (0, 27),
    "sensor.bounce_time_ms": (0, 500),
    "sensor.trigger_delay_ms": (0, 10000),
    "sensor.cooldown_ms": (0, 60000),
    "sensor.min_break_ms": (0, 2000),
    "sensor.max_break_ms": (100, 120000),
    "capture.burst_count": (1, 5),          # >5 risks starving the camera buffer pool
    "capture.burst_interval_ms": (10, 2000),
    "capture.jpeg_quality": (40, 100),
    "storage.min_free_mb": (0, 100000),
    "storage.item_length_mm": (0, 10000),
    "stream.framerate": (5, 60),
    "stream.preview_fps": (1, 30),
    "stream.preview_quality": (30, 100),
}

ENUMS = {
    "sensor.trigger_edge": ("break", "clear", "both"),
    "capture.mode": ("fast_grab", "full_res"),
}

# Changing these needs a camera/GPIO rebuild rather than a plain set_controls.
LIVE_REBUILD = ("stream.preview_size", "stream.record_size", "stream.framerate")


def _deep_merge(base, patch):
    out = copy.deepcopy(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _get_path(d, dotted):
    cur = d
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _set_path(d, dotted, value):
    parts = dotted.split(".")
    cur = d
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


class Config:
    """Thread-safe config store. Read with .get(), change with .update()."""

    def __init__(self, path=CONFIG_PATH):
        self.path = path
        self.lock = threading.RLock()
        self.data = copy.deepcopy(DEFAULTS)
        self.load()

    # ---- persistence -------------------------------------------------------
    def load(self):
        with self.lock:
            try:
                with open(self.path, "r") as f:
                    disk = json.load(f)
                self.data = _deep_merge(DEFAULTS, disk)
            except FileNotFoundError:
                self.data = copy.deepcopy(DEFAULTS)
                self.save()
            except Exception as e:
                # Corrupt file: run on defaults rather than refuse to start.
                print(f"[config] {self.path} unreadable ({e}); using defaults")
                self.data = copy.deepcopy(DEFAULTS)
            self._validate()

    def save(self):
        with self.lock:
            d = os.path.dirname(self.path)
            os.makedirs(d, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(self.data, f, indent=2)
                os.replace(tmp, self.path)  # atomic: never a half-written config
            except Exception:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise

    # ---- validation --------------------------------------------------------
    def _validate(self):
        for dotted, (lo, hi) in CLAMPS.items():
            v = _get_path(self.data, dotted)
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            _set_path(self.data, dotted, max(lo, min(hi, v)))
        for dotted, allowed in ENUMS.items():
            v = _get_path(self.data, dotted)
            if v not in allowed:
                _set_path(self.data, dotted, _get_path(DEFAULTS, dotted))
        cams = self.data.get("capture", {}).get("cameras", [])
        self.data["capture"]["cameras"] = [int(c) for c in cams if str(c).isdigit() or isinstance(c, int)]
        # Per-camera delays are a free-form dict (one key per camera), so they
        # can't live in CLAMPS. Clamp each here so a stray value can't wedge the
        # capture thread with a multi-second sleep.
        pcd = self.data.get("capture", {}).get("per_camera_delay_ms", {})
        if isinstance(pcd, dict):
            for k, v in list(pcd.items()):
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    pcd[k] = 0
                else:
                    pcd[k] = int(max(0, min(10000, v)))

    # ---- access ------------------------------------------------------------
    def get(self, dotted=None):
        with self.lock:
            if dotted is None:
                return copy.deepcopy(self.data)
            return copy.deepcopy(_get_path(self.data, dotted))

    def update(self, patch):
        """Deep-merge a patch, clamp, persist. Returns the new full config."""
        with self.lock:
            self.data = _deep_merge(self.data, patch or {})
            self._validate()
            self.save()
            return copy.deepcopy(self.data)

    def camera(self, index):
        return self.get(f"cameras.{int(index)}") or copy.deepcopy(DEFAULTS["cameras"]["0"])

    # ---- presets -----------------------------------------------------------
    def save_preset(self, name, patch):
        with self.lock:
            self.data.setdefault("presets", {})[name] = copy.deepcopy(patch)
            self.save()
            return list(self.data["presets"].keys())

    def apply_preset(self, name):
        with self.lock:
            patch = self.data.get("presets", {}).get(name)
            if patch is None:
                return None
            return self.update(patch)

    def delete_preset(self, name):
        with self.lock:
            self.data.get("presets", {}).pop(name, None)
            self.save()
            return list(self.data.get("presets", {}).keys())
