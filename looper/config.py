"""Configuration loading.

Layers, later wins:
  1. DEFAULTS below
  2. /boot/firmware/looper.toml   (editable from any PC by pulling the SD card)
  3. /var/lib/looper/setup.json   (written by the setup hotspot and the web UI's MQTT form)
  4. <thumb drive>/looper.toml    (drop one next to your videos to configure a looper)
  5. /var/lib/looper/settings.json (playback tweaks made from the web UI / Home Assistant)

[wifi] and [ssh] are handled by the network setup service (looper/netsetup.py).

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
        # Skip files the looper has found won't play smoothly on this Pi
        # (wrong codec, 4K, 60 fps...). Off = play them anyway, but still warn.
        "skip_incompatible": False,
        # Wait (up to this many seconds) for boot work to finish before starting video.
        "boot_settle_max": 300,
    },
    "player": {
        # "gstreamer": hardware H.264 decode straight onto a display plane. Measured on a
        # Pi Zero W: 1080p30 smooth, 0 dropped frames, ~52% CPU (tools/gst-test.sh).
        # "mpv": kept as a fallback; on a Pi Zero W it can't display 1080p smoothly.
        "backend": "gstreamer",
        "hwdec": "v4l2m2m",  # "no" = software decoding
        "audio_output": "alsa",  # "null" = no sound
        # GStreamer only
        "video_sink": "kmssink driver-name=vc4 force-modesetting=false",
        "hw_decoder": "v4l2h264dec capture-io-mode=dmabuf",
        # mpv only
        "vo": "gpu",
        "gpu_context": "drm",
        "profile": "fast",
        "display_mode": "",
        "extra_args": ["--gpu-hwdec-interop=drmprime"],
    },
    "snapshot": {
        "interval": 5,  # seconds between refreshes of the "now playing" preview; 0 = off
        "width": 480,
        "quality": 70,
        # "live": the GStreamer player writes a JPEG straight off the frames it already
        #         decoded for the screen (near-free; the default for that backend).
        #         "file": ffmpeg decodes one keyframe from the file in the background.
        #         "player": ask mpv (froze the HDMI output on a Pi Zero W with zero-copy
        #         decoding). "auto": "live" on GStreamer, else "file"/"player" by hwdec.
        "method": "auto",
    },
    "status": {
        "position_interval": 10,  # how often position is published (seconds)
    },
    "device": {
        "name": "Video Looper",  # shown in Home Assistant; also sets the hostname
    },
    "mqtt": {
        # "auto" = look for Home Assistant / an MQTT broker on the network and
        # connect if it allows it; true = use host below; false = off.
        "enabled": "auto",
        "host": "homeassistant.local",
        "port": 1883,
        "username": "",
        "password": "",
        "discovery_prefix": "homeassistant",
        "node_id": "",  # default: derived from the device name
        "name": "",  # default: the device name
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
    "skip_incompatible": ("playback", "skip_incompatible", bool, None),
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


def read_toml(path: str) -> dict:
    """Load a TOML file; a broken or missing file is logged and treated as empty."""
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:  # bad TOML must never stop playback
        log.error("ignoring %s: %s", path, e)
        return {}


def usb_config_path(media: dict) -> str | None:
    """looper.toml on the thumb drive, only if the drive is actually present."""
    dev = media.get("device")
    if dev and not os.path.exists(dev):
        return None
    path = os.path.join(media.get("dir", "/media/usb"), "looper.toml")
    try:
        return path if os.path.isfile(path) else None
    except OSError:
        return None


def slug(name: str) -> str:
    out = "".join(c if c.isalnum() else "_" for c in name.lower()).strip("_")
    while "__" in out:
        out = out.replace("__", "_")
    return out or "looper"


def mac_suffix(n: int = 4) -> str:
    """Last n hex chars of a hardware MAC (wlan0 preferred), lowercase. Used to make each
    looper's default name/hostname/MQTT id unique so several don't collide. Matches the
    setup hotspot's suffix (Looper-Setup-XXXX), so one looper looks the same everywhere."""
    for iface in ("wlan0", "eth0"):
        try:
            with open(f"/sys/class/net/{iface}/address") as f:
                mac = f.read().strip().replace(":", "")
            if len(mac) >= n and mac.strip("0"):
                return mac[-n:].lower()
        except OSError:
            continue
    try:  # fall back to the machine id if there is no network device yet
        with open("/etc/machine-id") as f:
            mid = f.read().strip()
        if len(mid) >= n:
            return mid[-n:].lower()
    except OSError:
        pass
    return ""


def save_setup(state_dir: str, section: str, values: dict) -> None:
    """Merge values into setup.json (used by the setup portal and web UI)."""
    path = os.path.join(state_dir, "setup.json")
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    data.setdefault(section, {}).update(values)
    os.makedirs(state_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=state_dir, prefix=".setup")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o640)
    try:  # written by root (setup portal) but read by the looper user
        st = os.stat(state_dir)
        os.chown(tmp, st.st_uid, st.st_gid)
    except OSError:
        pass
    os.replace(tmp, path)


class Config:
    def __init__(self, boot_path: str = BOOT_CONFIG, state_dir: str = STATE_DIR):
        self.boot_path = boot_path
        self.settings_path = os.path.join(state_dir, "settings.json")
        self.setup_path = os.path.join(state_dir, "setup.json")
        self.state_dir = state_dir
        self.data = copy.deepcopy(DEFAULTS)
        self.overrides: dict = {}
        self.load()

    def load(self) -> None:
        _merge(self.data, read_toml(self.boot_path))
        try:
            with open(self.setup_path) as f:
                _merge(self.data, json.load(f))
        except FileNotFoundError:
            pass
        except Exception as e:
            log.error("ignoring %s: %s", self.setup_path, e)
        usb = usb_config_path(self.data["media"])
        if usb:
            log.info("using settings from %s", usb)
            _merge(self.data, read_toml(usb))
        m, dev = self.data["mqtt"], self.data["device"]
        custom = (dev.get("name") or "").strip()
        if not custom or custom == DEFAULTS["device"]["name"]:
            # No chosen name: append this board's MAC suffix so multiple loopers don't all
            # show up as "Video Looper"/"looper" and collide in Home Assistant and on mDNS.
            sfx = mac_suffix()
            name = DEFAULTS["device"]["name"] + (f" {sfx.upper()}" if sfx else "")
        else:
            name = custom
        dev["name"] = name
        m["name"] = m.get("name") or name
        m["node_id"] = m.get("node_id") or slug(name)
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
