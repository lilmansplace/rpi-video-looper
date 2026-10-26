"""Find an MQTT broker (usually Home Assistant's Mosquitto) and keep the HA link running.

mqtt.enabled:
  "auto" (default)  look for Home Assistant / a broker on the network and use the first one
                    that accepts us (with the configured login, if any)
  true              use mqtt.host only
  false             off

Discovery is cheap: one mDNS browse plus a few TCP connects, retried with backoff,
on a background thread so it never delays playback.
"""

from __future__ import annotations

import copy
import logging
import os
import shutil
import subprocess
import threading
import time
import uuid

from .config import DEFAULTS, Config

log = logging.getLogger(__name__)

try:
    import paho.mqtt.client as mqtt
except ImportError:  # pragma: no cover
    mqtt = None

FALLBACK_HOSTS = ["homeassistant.local", "homeassistant"]


def enabled_mode(v) -> str:
    if isinstance(v, bool):
        return "on" if v else "off"
    v = str(v).strip().lower()
    if v in ("auto", ""):
        return "auto"
    return "on" if v in ("1", "true", "yes", "on") else "off"


def new_client(client_id: str):
    try:
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    except AttributeError:  # paho < 2
        return mqtt.Client(client_id=client_id)


def probe(host: str, port: int, username: str = "", password: str = "", timeout: float = 5.0) -> str:
    """'ok' | 'auth' (broker found, login refused/needed) | 'unreachable' | 'error'."""
    if mqtt is None:
        return "error"
    result: dict = {}
    c = new_client(f"looper-probe-{uuid.uuid4().hex[:6]}")
    if username:
        c.username_pw_set(username, password or None)

    def on_connect(cl, ud, flags, rc, props=None):
        result["rc"] = rc

    c.on_connect = on_connect
    try:
        c.connect(host, int(port), keepalive=10)
    except Exception:
        return "unreachable"
    end = time.monotonic() + timeout
    try:
        while "rc" not in result and time.monotonic() < end:
            c.loop(0.2)
    finally:
        try:
            c.disconnect()
        except Exception:
            pass
    rc = result.get("rc")
    if rc is None:
        return "unreachable"
    failed = rc.is_failure if hasattr(rc, "is_failure") else rc != 0
    if not failed:
        return "ok"
    val = getattr(rc, "value", rc)
    return "auth" if val in (4, 5, 134, 135) else "error"


def avahi_hosts() -> list[tuple[str, int]]:
    """Brokers / Home Assistant instances advertised over mDNS: [(ip, mqtt_port)]."""
    if not shutil.which("avahi-browse"):
        return []
    found: list[tuple[str, int]] = []
    for svc, use_port in (("_mqtt._tcp", True), ("_home-assistant._tcp", False)):
        try:
            out = subprocess.run(["avahi-browse", "-rtpk", svc], capture_output=True, text=True,
                                 timeout=8).stdout
        except (subprocess.TimeoutExpired, OSError):
            continue
        for line in out.splitlines():
            f = line.split(";")
            if len(f) > 8 and f[0] == "=" and f[2] == "IPv4":
                port = int(f[8]) if use_port and f[8].isdigit() else 1883
                if (f[7], port) not in found:
                    found.append((f[7], port))
    return found


class MqttManager:
    def __init__(self, cfg: Config, ctl, discover=avahi_hosts, prober=probe, client_factory=None):
        self.cfg = cfg
        self.ctl = ctl
        self.discover = discover
        self.probe = prober
        self.client_factory = client_factory
        self.client = None
        self._wake = threading.Event()
        self._stop = False
        self._thread: threading.Thread | None = None

    def _status(self, state: str, text: str, host: str | None = None) -> None:
        self.ctl._set(mqtt=text, mqtt_state=state, mqtt_host=host)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="mqtt-auto", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop = True
        self._wake.set()
        self._stop_client()

    def reconfigure(self) -> None:
        """Settings changed (web form / setup portal): reload and search again."""
        fresh = Config(self.cfg.boot_path, self.cfg.state_dir)
        self.cfg.data["mqtt"] = fresh.data["mqtt"]
        self._stop_client()
        self._wake.set()

    def _stop_client(self) -> None:
        if self.client:
            try:
                self.client.stop()
            except Exception:
                pass
            if self.client.__class__.__name__ == "HomeAssistantMQTT":
                try:
                    self.ctl.listeners.remove(self.client._on_state)
                except ValueError:
                    pass
            self.client = None

    def candidates(self, m: dict) -> list[tuple[str, int]]:
        port = int(m.get("port") or 1883)
        host = (m.get("host") or "").strip()
        mode = enabled_mode(m.get("enabled"))
        explicit = host and host != DEFAULTS["mqtt"]["host"]
        if mode == "on" or explicit:
            out = [(host or DEFAULTS["mqtt"]["host"], port)]
            if mode == "on":
                return out
        else:
            out = []
        for h in self.discover() + [(h, port) for h in FALLBACK_HOSTS]:
            if h not in out:
                out.append(h)
        return out

    def search_once(self) -> bool:
        m = self.cfg.section("mqtt")
        if enabled_mode(m.get("enabled")) == "off":
            self._status("off", "off")
            return True
        if mqtt is None:
            self._status("error", "python3-paho-mqtt is not installed")
            return True
        self._status("searching", "looking for Home Assistant…")
        login_needed = None
        for host, port in self.candidates(m):
            r = self.probe(host, port, m.get("username", ""), m.get("password", ""))
            log.info("MQTT %s:%s -> %s", host, port, r)
            if r == "ok":
                self._connect(host, port)
                return True
            if r == "auth" and not login_needed:
                login_needed = host
        if login_needed:
            msg = "login rejected" if m.get("username") else "needs a username and password"
            self._status("needs_login", f"{msg} ({login_needed})", login_needed)
        else:
            self._status("not_found", "no MQTT broker found")
        return False

    def _connect(self, host: str, port: int) -> None:
        conf = copy.deepcopy(self.cfg.section("mqtt"))
        conf.update(host=host, port=port)
        if self.client_factory:
            self.client = self.client_factory(conf, self.ctl)
        else:
            from .mqtt_ha import HomeAssistantMQTT
            self.client = HomeAssistantMQTT(conf, self.ctl)
        self.client.start()
        self._status("connected", f"connected ({host})", host)

    def _loop(self) -> None:
        delay = 30
        while not self._stop:
            self._wake.clear()
            try:
                done = self.search_once()
            except Exception:
                log.exception("MQTT search failed")
                done = False
            if done:
                self._wake.wait()  # sleep until reconfigured
                delay = 30
                continue
            self._wake.wait(delay)
            delay = min(delay * 2, 600)
