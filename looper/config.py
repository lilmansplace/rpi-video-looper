"""Configuration loading.

Three layers, later wins:
  1. DEFAULTS below
  2. /boot/firmware/looper.toml  (editable from any PC by pulling the SD card)
  3. /var/lib/looper/settings.json (runtime changes made from the web UI / Home Assistant)

Only keys listed in TUNABLES can be changed at runtime.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
import tomllib

log = logging.getLogger(__name__)

BOOT_CONFIG = os.environ.get("LOOPER_CONFIG", "/boot/firmware/looper.toml")
STATE_DIR = os.environ.get("LOOPER_STATE_DIR", "/var/lib/looper")
RUN_DIR = os.environ.get("LOOPER_RUN_DIR", "/run/looper")

DEFAULTS: dict = {
    "media": {
        "dir": "/media/usb",
        # Only scan when this block device exists, so an empty USB port never
        # makes the automount wait (and stall controls) for its timeout.
        "device": "/dev/sda1",
        "extensions": ["mp4", "m4v", "mkv", "mov", "avi", "h264", "ts"],
        "rescan_interval": 30,  # seconds; 0 = only on start / manual
        "recursive": True,
    },
    "playback": {
        "shuffle": True,
        "repeat": "all",  # all | one | off
        "volume": 100,
        "resume_on_boot": False,  # start with the last played file
    },
    "player": {
        # Pi Zero W: hardware H.264 decode via the bcm2835-codec V4L2 driver.
        # tools/bench.py tries the combinations and tells you which one is smooth.
        "vo": "gpu",
        "gpu_context": "drm",
        "hwdec": "auto-safe",
        "audio_output": "alsa",
        "extra_args": [],
    },
    "snapshot": {
        "interval": 30,  # seconds; 0 = off
        "width": 480,
        "quality": 70,
        "ffmpeg_fallback": False,  # costs CPU; only if mpv screenshots fail
    },
    "status": {
        "position_interval": 10,  # how often position is published (seconds)
    },
    "mqtt": {
        "enabled": False,
        "host": "homeassistant.local",
        "port": 1883,
        "username": "",
        "password": "",
        "discovery_prefix": "homeassistant",
        "node_id": "rpi_looper",
        "name": "Video Looper",
    },
    "web": {
        "enabled": True,
        "bind": "0.0.0.0",
        "port": 8080,
    },
}

# key -> (section, option, type, extra)  extra: choices list or (min, max)
TUNABLES: dict = {
    "shuffle": ("playback", "shuffle", bool, None),
    "repeat": ("playback", "repeat", str, ["all", "one", "off"]),
    "volume": ("playback", "volume", int, (0, 100)),
    "snapshot_interval": ("snapshot", "interval", int, (0, 3600)),
    "snapshot_width": ("snapshot", "width", int, (160, 1920)),
    "position_interval": ("status", "position_interval", int, (2, 300)),
    "rescan_interval": ("media", "rescan_interval", int, (0, 3600)),
}


def _merge(base: dict, over: dict) -> dict:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def coerce(key: str, value):
    """Validate a runtime setting. Raises ValueError on bad input."""
    if key not in TUNABLES:
        raise ValueError(f"unknown setting {key!r}")
    _, _, typ, extra = TUNABLES[key]
    if typ is bool:
        if isinstance(value, str):
            value = value.strip().lower() in ("1", "true", "on", "yes")
        value = bool(value)
    elif typ is int:
        value = int(float(value))
        lo, hi = extra
        value = max(lo, min(hi, value))
    else:
        value = str(value).strip().lower()
        if extra and value not in extra:
            raise ValueError(f"{key} must be one of {extra}")
    return value


class Config:
    def __init__(self, boot_path: str = BOOT_CONFIG, state_dir: str = STATE_DIR):
        self.boot_path = boot_path
        self.settings_path = os.path.join(state_dir, "settings.json")
        self.state_dir = state_dir
        self.data = copy.deepcopy(DEFAULTS)
        self.overrides: dict = {}
        self.load()

    def load(self) -> None:
        if os.path.exists(self.boot_path):
            try:
                with open(self.boot_path, "rb") as f:
                    _merge(self.data, tomllib.load(f))
            except Exception as e:  # bad TOML must never stop playback
                log.error("ignoring %s: %s", self.boot_path, e)
        if os.path.exists(self.settings_path):
            try:
                with open(self.settings_path) as f:
                    for k, v in json.load(f).items():
                        try:
                            self._apply(k, coerce(k, v))
                            self.overrides[k] = self.get(k)
                        except ValueError:
                            pass
            except Exception as e:
                log.error("ignoring %s: %s", self.settings_path, e)

    def section(self, name: str) -> dict:
        return self.data[name]

    def get(self, key: str):
        sec, opt, _, _ = TUNABLES[key]
        return self.data[sec][opt]

    def _apply(self, key: str, value) -> None:
        sec, opt, _, _ = TUNABLES[key]
        self.data[sec][opt] = value

    def set(self, key: str, value):
        value = coerce(key, value)
        self._apply(key, value)
        self.overrides[key] = value
        self._save()
        return value

    def tunables(self) -> dict:
        return {k: self.get(k) for k in TUNABLES}

    def _save(self) -> None:
        # atomic write so a power cut can't corrupt the file
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.state_dir, prefix=".settings")
            with os.fdopen(fd, "w") as f:
                json.dump(self.overrides, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.settings_path)
        except OSError as e:
            log.error("could not save settings: %s", e)
