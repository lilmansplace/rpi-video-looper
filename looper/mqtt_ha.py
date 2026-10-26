"""Home Assistant integration over MQTT with auto-discovery.

No custom component needed: point the looper at your broker (Mosquitto add-on)
and a "Video Looper" device appears in HA with sensors, buttons, selects,
switches, numbers and a snapshot camera.

Topics (base = looper/<node_id>):
  base/availability      online | offline   (LWT)
  base/state             JSON state, retained
  base/snapshot          JPEG bytes, retained
  base/cmd/<command>     payload = argument
"""

from __future__ import annotations

import json
import logging
import os

from . import __version__

log = logging.getLogger(__name__)

try:
    import paho.mqtt.client as mqtt
except ImportError:  # pragma: no cover
    mqtt = None

BUTTONS = {
    "next": ("Next", "mdi:skip-next"),
    "previous": ("Previous", "mdi:skip-previous"),
    "restart": ("Restart video", "mdi:restart"),
    "toggle_pause": ("Play/Pause", "mdi:play-pause"),
    "rescan": ("Rescan USB", "mdi:usb-flash-drive"),
}

NUMBERS = {
    # key: (name, min, max, step, unit, icon)
    "volume": ("Volume", 0, 100, 5, "%", "mdi:volume-high"),
    "snapshot_interval": ("Snapshot interval", 0, 600, 5, "s", "mdi:camera-timer"),
    "position_interval": ("Position update interval", 2, 300, 1, "s", "mdi:timer-sync"),
    "rescan_interval": ("USB rescan interval", 0, 3600, 10, "s", "mdi:folder-refresh"),
}

SENSORS = {
    # key: (name, value_template, unit, icon, device_class, entity_category)
    "now_playing": ("Now playing", "{{ value_json.title or 'None' }}", None, "mdi:movie-open-play", None, None),
    "status": ("Status", "{{ value_json.status }}", None, "mdi:information-outline", None, None),
    "position": ("Position", "{{ value_json.position }}", "s", "mdi:timer-outline", "duration", None),
    "duration": ("Duration", "{{ value_json.duration }}", "s", "mdi:timer-sand", "duration", None),
    "file_count": ("Video count", "{{ value_json.count }}", None, "mdi:filmstrip-box-multiple", None, None),
    "cpu_temp": ("CPU temperature", "{{ value_json.system.cpu_temp }}", "°C", None, "temperature", "diagnostic"),
    "load": ("CPU load", "{{ value_json.system.load1 }}", None, "mdi:chip", None, "diagnostic"),
    "memory": ("Free memory", "{{ value_json.system.mem_avail_mb }}", "MB", "mdi:memory", None, "diagnostic"),
}


class HomeAssistantMQTT:
    def __init__(self, cfg: dict, controller):
        if mqtt is None:
            raise RuntimeError("python3-paho-mqtt is not installed")
        self.cfg = cfg
        self.ctl = controller
        self.node = cfg["node_id"]
        self.base = f"looper/{self.node}"
        self.prefix = cfg["discovery_prefix"]
        self._last_titles: list[str] | None = None
        try:
            self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.node)
        except AttributeError:  # paho < 2
            self.client = mqtt.Client(client_id=self.node)
        if cfg.get("username"):
            self.client.username_pw_set(cfg["username"], cfg.get("password") or None)
        self.client.will_set(f"{self.base}/availability", "offline", retain=True)
        self.client.reconnect_delay_set(2, 60)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        controller.listeners.append(self._on_state)

    def start(self) -> None:
        # connect_async + loop_start: never blocks boot if the broker is down
        self.client.connect_async(self.cfg["host"], int(self.cfg["port"]), keepalive=60)
        self.client.loop_start()

    def stop(self) -> None:
        try:
            self.client.publish(f"{self.base}/availability", "offline", retain=True)
            self.client.disconnect()
            self.client.loop_stop()
        except Exception:
            pass

    # ---------- discovery ----------
    def _device(self) -> dict:
        return {
            "identifiers": [self.node],
            "name": self.cfg.get("name", "Video Looper"),
            "manufacturer": "rpi-video-looper",
            "model": "Raspberry Pi Zero W",
            "sw_version": __version__,
        }

    def _common(self, key: str, name: str) -> dict:
        return {
            "name": name,
            "unique_id": f"{self.node}_{key}",
            "object_id": f"{self.node}_{key}",
            "availability_topic": f"{self.base}/availability",
            "device": self._device(),
        }

    def _pub_config(self, component: str, key: str, payload: dict) -> None:
        topic = f"{self.prefix}/{component}/{self.node}/{key}/config"
        self.client.publish(topic, json.dumps(payload), retain=True)

    def publish_discovery(self) -> None:
        st = f"{self.base}/state"
        for key, (name, tpl, unit, icon, dclass, cat) in SENSORS.items():
            p = self._common(key, name) | {"state_topic": st, "value_template": tpl}
            if unit:
                p["unit_of_measurement"] = unit
            if icon:
                p["icon"] = icon
            if dclass:
                p["device_class"] = dclass
            if cat:
                p["entity_category"] = cat
            if key == "now_playing":
                p["json_attributes_topic"] = st
            self._pub_config("sensor", key, p)

        for key, (name, icon) in BUTTONS.items():
            p = self._common(key, name) | {"command_topic": f"{self.base}/cmd/{key}", "icon": icon}
            self._pub_config("button", key, p)

        for key, (name, lo, hi, step, unit, icon) in NUMBERS.items():
            p = self._common(key, name) | {
                "command_topic": f"{self.base}/cmd/set/{key}",
                "state_topic": st,
                "value_template": f"{{{{ value_json.{key} }}}}",
                "min": lo, "max": hi, "step": step, "unit_of_measurement": unit,
                "icon": icon, "mode": "slider" if key == "volume" else "box",
            }
            if key != "volume":
                p["entity_category"] = "config"
            self._pub_config("number", key, p)

        self._pub_config("switch", "shuffle", self._common("shuffle", "Shuffle") | {
            "command_topic": f"{self.base}/cmd/set/shuffle",
            "state_topic": st,
            "value_template": "{{ 'ON' if value_json.shuffle else 'OFF' }}",
            "payload_on": "ON", "payload_off": "OFF", "icon": "mdi:shuffle-variant",
        })
        self._pub_config("switch", "paused", self._common("paused", "Paused") | {
            "command_topic": f"{self.base}/cmd/pause_switch",
            "state_topic": st,
            "value_template": "{{ 'ON' if value_json.paused else 'OFF' }}",
            "payload_on": "ON", "payload_off": "OFF", "icon": "mdi:pause",
        })
        self._pub_config("select", "repeat", self._common("repeat", "Repeat") | {
            "command_topic": f"{self.base}/cmd/set/repeat",
            "state_topic": st,
            "value_template": "{{ value_json.repeat }}",
            "options": ["all", "one", "off"], "icon": "mdi:repeat",
        })
        self._pub_config("camera", "snapshot", self._common("snapshot", "Snapshot") | {
            "topic": f"{self.base}/snapshot",
        })
        self.publish_library()

    def publish_library(self) -> None:
        titles = self.ctl.library()
        # HA select needs at least one option
        options = titles or ["(no videos)"]
        self._last_titles = titles
        self._pub_config("select", "play_video", self._common("play_video", "Play video") | {
            "command_topic": f"{self.base}/cmd/jump",
            "state_topic": f"{self.base}/state",
            "value_template": "{{ value_json.title or '' }}",
            "options": options, "icon": "mdi:playlist-play",
        })

    # ---------- runtime ----------
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        failed = reason_code.is_failure if hasattr(reason_code, "is_failure") else reason_code != 0
        if failed:
            log.error("MQTT connect failed: %s", reason_code)
            return
        log.info("MQTT connected to %s", self.cfg["host"])
        client.subscribe(f"{self.base}/cmd/#")
        client.subscribe(f"{self.prefix}/status")  # HA restarted -> re-announce
        client.publish(f"{self.base}/availability", "online", retain=True)
        self.publish_discovery()
        self._on_state(self.ctl.snapshot_state(), "state")
        self._publish_snapshot()

    def _on_message(self, client, userdata, msg):
        payload = msg.payload.decode(errors="replace").strip()
        if msg.topic == f"{self.prefix}/status":
            if payload == "online":
                self.publish_discovery()
                self._on_state(self.ctl.snapshot_state(), "state")
            return
        cmd = msg.topic[len(self.base) + len("/cmd/"):]
        if cmd.startswith("set/"):
            key = cmd[4:]
            if key == "shuffle":
                payload = payload.upper() == "ON"
            self.ctl.submit("set", (key, payload))
        elif cmd == "pause_switch":
            self.ctl.submit("pause" if payload.upper() == "ON" else "play")
        elif cmd in ("jump", "seek"):
            self.ctl.submit(cmd, payload)
        elif cmd in BUTTONS or cmd in ("pause", "play", "stop"):
            self.ctl.submit(cmd)
        else:
            log.warning("unknown MQTT command %s", cmd)

    def _on_state(self, state: dict, kind: str) -> None:
        if not self.client.is_connected():
            return
        if kind == "library" and self.ctl.library() != self._last_titles:
            self.publish_library()
        if kind == "snapshot":
            self._publish_snapshot()
        self.client.publish(f"{self.base}/state", json.dumps(state), retain=True)

    def _publish_snapshot(self) -> None:
        path = self.ctl.snapshot_path
        if os.path.exists(path):
            with open(path, "rb") as f:
                self.client.publish(f"{self.base}/snapshot", f.read(), retain=True)
