"""Network onboarding for a freshly flashed looper. Runs as root (looper-net.service).

How a looper gets onto a network, in order:
  1. Settings already on the Pi (Raspberry Pi Imager customisation, or earlier setup).
  2. A [wifi] section in looper.toml on the SD card's boot partition or on the thumb drive.
     Passwords in the boot-partition file are scrubbed once they are saved.
  3. Setup hotspot: if there is no network after a short wait, the Pi starts a WPA2
     hotspot ("Looper-Setup-XXXX"). The projector shows a QR code that joins it; the phone
     then pops up a setup page (captive portal) to pick a network and optional MQTT login.

The looper (controller) reads STATE_FILE and draws the matching screen on the projector.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import queue
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import BOOT_CONFIG, STATE_DIR, read_toml, save_setup, slug, usb_config_path, DEFAULTS

log = logging.getLogger("looper.net")

RUN_DIR = os.environ.get("LOOPER_NET_RUN_DIR", "/run/looper-net")
STATE_FILE = os.path.join(RUN_DIR, "state.json")
AP_CON = "looper-setup"
AP_ADDR = "10.42.0.1"
PSK_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no 0/o/1/l/i: readable off a projector

FIRST_WAIT = 180  # seconds to wait for a saved network at boot before the hotspot
# (a Pi Zero W can take well over a minute to join Wi-Fi while cloud-init runs)
NO_PROFILE_WAIT = 10  # ... when there is no saved Wi-Fi at all
DROPOUT_WAIT = 300  # a working network vanished: wait this long before the hotspot
RETRY_EVERY = 300  # while the hotspot is up and nobody is on it, retry saved Wi-Fi
ONLINE_POLL = 30  # once connected, only re-check for a dropout this often. Each check forks
# nmcli twice (~1.2s of CPU on a Pi Zero W); polling every 2s stole most of a core from video.
LINK_POLL = 3  # while still trying to get online, check more eagerly


# ---------------------------------------------------------------- helpers
def nm_split(line: str) -> list[str]:
    """Split one line of `nmcli -t` output (':' separated, '\\:' escaped)."""
    out, cur, esc = [], [], False
    for ch in line:
        if esc:
            cur.append(ch)
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return out


def scrub_secrets(text: str, sections=("wifi", "ssh", "mqtt")) -> tuple[str, bool]:
    """Blank out password = "..." lines in the given TOML sections. Returns (text, changed)."""
    cur, changed, out = None, False, []
    for line in text.splitlines(keepends=True):
        m = re.match(r"\s*\[([A-Za-z0-9_.-]+)\]", line)
        if m:
            cur = m.group(1).lower()
        elif cur in sections:
            pm = re.match(r'(\s*password\s*=\s*)(["\'])(.+)\2(.*)$', line.rstrip("\r\n"))
            if pm:
                nl = line[len(line.rstrip("\r\n")):]
                line = f'{pm.group(1)}""  # saved on the Pi{nl}'
                changed = True
        out.append(line)
    return "".join(out), changed


def run(cmd: list[str], timeout: float = 30) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if p.returncode:
            log.debug("%s -> %s %s", " ".join(cmd[:4]), p.returncode, p.stderr.strip())
        return p.returncode, p.stdout
    except FileNotFoundError:
        return 127, ""
    except subprocess.TimeoutExpired:
        return 124, ""


# ---------------------------------------------------------------- NetworkManager
class NetworkManager:
    """Thin nmcli wrapper. Swapped for a fake in tests."""

    def __init__(self):
        self.dev = self._wifi_device() or "wlan0"

    def nm(self, *args, timeout=30):
        return run(["nmcli", *args], timeout)

    def _wifi_device(self):
        rc, out = self.nm("-t", "-f", "DEVICE,TYPE", "device")
        for line in out.splitlines():
            d = nm_split(line)
            if len(d) >= 2 and d[1] == "wifi":
                return d[0]
        return None

    def has_wifi(self) -> bool:
        """Is there any Wi-Fi adapter at all? A non-W Pi Zero has none. Reads sysfs directly so
        it's reliable even before NetworkManager has settled."""
        try:
            for iface in os.listdir("/sys/class/net"):
                if os.path.exists(f"/sys/class/net/{iface}/wireless") \
                        or os.path.exists(f"/sys/class/net/{iface}/phy80211"):
                    return True
        except OSError:
            pass
        return False

    def online(self) -> str | None:
        """IP address if connected to a real network (not our hotspot)."""
        rc, out = self.nm("-t", "-f", "NAME,TYPE,DEVICE", "connection", "show", "--active")
        for line in out.splitlines():
            name, typ, dev = (nm_split(line) + ["", "", ""])[:3]
            if name == AP_CON or not dev or typ not in ("802-11-wireless", "802-3-ethernet"):
                continue
            rc, ip = self.nm("-g", "IP4.ADDRESS", "device", "show", dev)
            ip = ip.strip().split("|")[0].split("/")[0].strip()
            if ip:
                return ip
        return None

    def current_ssid(self) -> str | None:
        rc, out = self.nm("-t", "-f", "ACTIVE,SSID", "device", "wifi", "list", "--rescan", "no")
        for line in out.splitlines():
            a = nm_split(line)
            if a[0] == "yes":
                return a[1]
        return None

    def saved_wifi(self) -> list[str]:
        rc, out = self.nm("-t", "-f", "NAME,TYPE", "connection", "show")
        return [nm_split(l)[0] for l in out.splitlines()
                if nm_split(l)[1:2] == ["802-11-wireless"] and nm_split(l)[0] != AP_CON]

    def scan(self) -> list[dict]:
        rc, out = self.nm("-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list", "--rescan", "yes",
                          timeout=40)
        best: dict[str, dict] = {}
        for line in out.splitlines():
            f = nm_split(line)
            if len(f) < 3 or not f[0]:
                continue
            sig = int(f[1] or 0) if f[1].isdigit() else 0
            if f[0] not in best or sig > best[f[0]]["signal"]:
                best[f[0]] = {"ssid": f[0], "signal": sig, "secure": f[2] not in ("", "--")}
        return sorted(best.values(), key=lambda n: -n["signal"])

    def add_wifi(self, ssid: str, psk: str, hidden: bool = False) -> str:
        name = "looper-wifi-" + slug(ssid)[:24]
        self.nm("connection", "delete", "id", name)
        args = ["connection", "add", "type", "wifi", "ifname", self.dev, "con-name", name, "ssid", ssid,
                "connection.autoconnect", "yes", "connection.autoconnect-priority", "10"]
        if psk:
            args += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", psk]
        if hidden:
            args += ["802-11-wireless.hidden", "yes"]
        rc, _ = self.nm(*args)
        if rc:
            raise RuntimeError(f"could not save Wi-Fi '{ssid}'")
        return name

    def up(self, name: str, timeout: int = 45) -> bool:
        rc, _ = self.nm("--wait", str(timeout), "connection", "up", "id", name, timeout=timeout + 10)
        return rc == 0

    def try_saved(self, timeout: int = 40) -> bool:
        """Let NetworkManager autoconnect to any saved network."""
        self.nm("device", "set", self.dev, "autoconnect", "yes")
        self.nm("device", "connect", self.dev, timeout=timeout)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.online():
                return True
            time.sleep(2)
        return False

    def start_ap(self, ssid: str, psk: str) -> bool:
        self.nm("connection", "delete", "id", AP_CON)
        rc, _ = self.nm("connection", "add", "type", "wifi", "ifname", self.dev, "con-name", AP_CON,
                        "ssid", ssid, "connection.autoconnect", "no",
                        "802-11-wireless.mode", "ap", "802-11-wireless.band", "bg",
                        "ipv4.method", "shared", "ipv4.addresses", f"{AP_ADDR}/24", "ipv6.method", "disabled",
                        "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.proto", "rsn",
                        "wifi-sec.pairwise", "ccmp", "wifi-sec.group", "ccmp", "wifi-sec.psk", psk)
        return rc == 0 and self.up(AP_CON, 30)

    def stop_ap(self) -> None:
        self.nm("connection", "down", "id", AP_CON)

    def stations(self) -> int:
        rc, out = run(["iw", "dev", self.dev, "station", "dump"], 5)
        return out.count("Station ")

    def mac(self) -> str:
        try:
            with open(f"/sys/class/net/{self.dev}/address") as f:
                return f.read().strip()
        except OSError:
            return "00:00:00:00:00:00"


# ---------------------------------------------------------------- system changes
class System:
    """Host changes that need root. Swapped for a fake in tests."""

    def set_country(self, cc: str) -> None:
        cc = (cc or "US").upper()[:2]
        if shutil.which("raspi-config"):
            run(["raspi-config", "nonint", "do_wifi_country", cc], 30)
        else:
            run(["iw", "reg", "set", cc], 5)
        run(["rfkill", "unblock", "wifi"], 5)

    def set_hostname(self, name: str) -> str:
        host = slug(name).replace("_", "-")[:30] or "looper"
        rc, cur = run(["hostname"], 5)
        if cur.strip() != host:
            run(["hostnamectl", "set-hostname", host], 10)
            try:
                with open("/etc/hosts") as f:
                    lines = [l for l in f if not l.startswith("127.0.1.1")]
                lines.append(f"127.0.1.1\t{host}\n")
                with open("/etc/hosts", "w") as f:
                    f.writelines(lines)
            except OSError:
                pass
            run(["systemctl", "try-restart", "avahi-daemon"], 15)
        return host

    def set_ssh(self, enabled: bool, user: str, password: str) -> None:
        if password:
            subprocess.run(["chpasswd"], input=f"{user}:{password}\n", text=True, capture_output=True)
            run(["passwd", "-u", user], 5)
        run(["systemctl", "enable" if enabled else "disable", "--now", "ssh"], 30)

    def restart_looper(self) -> None:
        run(["systemctl", "try-restart", "rpi-video-looper"], 30)

    def first_boot_done(self) -> None:
        # Hook for one-time cleanup after the first successful connection. cloud-init is
        # deliberately left running: Raspberry Pi OS uses it for Imager's settings.
        pass


# ---------------------------------------------------------------- the service
class NetSetup:
    def __init__(self, nm=None, system=None, state_dir: str = STATE_DIR, boot_path: str = BOOT_CONFIG):
        self.nm = nm or NetworkManager()
        self.sys = system or System()
        self.state_dir = state_dir
        self.boot_path = boot_path
        self.actions: queue.Queue = queue.Queue()
        self.networks: list[dict] = []
        self.state: dict = {"mode": "starting"}
        self.portal: ThreadingHTTPServer | None = None
        self.ap_ssid = "Looper-Setup-" + self.nm.mac().replace(":", "")[-4:].upper()
        self.ap_psk = self._hotspot_psk()

    # ----- state for the projector / portal
    def publish(self, **st) -> None:
        self.state = {"t": int(time.time()), **st}
        os.makedirs(RUN_DIR, exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f)
        os.chmod(tmp, 0o644)
        os.replace(tmp, STATE_FILE)
        log.info("state: %s", {k: v for k, v in self.state.items() if k != "psk"})

    def _hotspot_psk(self) -> str:
        path = os.path.join(self.state_dir, "hotspot-psk")
        try:
            with open(path) as f:
                psk = f.read().strip()
                if len(psk) >= 8:
                    return psk
        except OSError:
            pass
        psk = "".join(secrets.choice(PSK_ALPHABET) for _ in range(10))
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            with open(path, "w") as f:
                f.write(psk)
            os.chmod(path, 0o600)
        except OSError:
            pass
        return psk

    def _setup_state(self, error: str | None = None) -> dict:
        return {"mode": "setup", "ssid": self.ap_ssid, "psk": self.ap_psk,
                "portal": f"http://{AP_ADDR}/", "error": error}

    # ----- seed files
    def _applied(self) -> dict:
        try:
            with open(os.path.join(self.state_dir, "net-applied.json")) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _mark_applied(self, data: dict) -> None:
        path = os.path.join(self.state_dir, "net-applied.json")
        os.makedirs(self.state_dir, exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f)
        os.chmod(path, 0o600)

    def apply_seed_files(self) -> bool:
        """Apply [wifi]/[device]/[ssh] from looper.toml files. True if Wi-Fi was (re)configured."""
        boot = read_toml(self.boot_path)
        media = {**DEFAULTS["media"], **boot.get("media", {})}
        usb_path = usb_config_path(media)
        usb = read_toml(usb_path) if usb_path else {}
        applied = self._applied()
        new_wifi = False
        for src, cfg in (("boot", boot), ("usb", usb)):
            wifi = cfg.get("wifi") or {}
            ssid = str(wifi.get("ssid") or "").strip()
            if ssid:
                psk = str(wifi.get("password") or "")
                key = hashlib.sha256(f"{ssid}\0{psk}\0{wifi.get('country', '')}".encode()).hexdigest()
                if applied.get(ssid) == key or (not psk and ssid in applied and src == "boot"):
                    pass  # already saved (boot file passwords are blanked after saving)
                else:
                    log.info("saving Wi-Fi '%s' from %s looper.toml", ssid, src)
                    self.sys.set_country(wifi.get("country", "US"))
                    try:
                        self.nm.add_wifi(ssid, psk, bool(wifi.get("hidden")))
                        applied[ssid] = key
                        new_wifi = True
                    except RuntimeError as e:
                        log.error("%s", e)
            ssh = cfg.get("ssh") or {}
            if "enabled" in ssh or ssh.get("password"):
                skey = hashlib.sha256(json.dumps(ssh, sort_keys=True).encode()).hexdigest()
                if applied.get("__ssh_" + src) != skey and (ssh.get("password") or "enabled" in ssh):
                    self.sys.set_ssh(bool(ssh.get("enabled", True)), ssh.get("user", "looper"),
                                     str(ssh.get("password") or ""))
                    applied["__ssh_" + src] = skey
        self._mark_applied(applied)
        # don't leave passwords readable on the boot partition
        try:
            with open(self.boot_path) as f:
                text = f.read()
            text, changed = scrub_secrets(text)
            if changed:
                with open(self.boot_path, "w") as f:
                    f.write(text)
                log.info("removed saved passwords from %s", self.boot_path)
        except OSError:
            pass
        return new_wifi

    def ensure_hostname(self) -> None:
        """Give the looper a stable, unique-ish hostname: the chosen device name if set,
        otherwise looper-<mac> so several loopers don't all answer to 'looper'. The suffix
        matches the setup hotspot (Looper-Setup-XXXX)."""
        boot = read_toml(self.boot_path)
        media = {**DEFAULTS["media"], **boot.get("media", {})}
        usb_path = usb_config_path(media)
        usb = read_toml(usb_path) if usb_path else {}
        name = ((usb.get("device") or {}).get("name")
                or (boot.get("device") or {}).get("name") or "").strip()
        if name and name != DEFAULTS["device"]["name"]:
            self.sys.set_hostname(name)
        else:
            sfx = self.nm.mac().replace(":", "")[-4:].lower()
            self.sys.set_hostname(f"looper-{sfx}" if sfx and sfx.strip("0") else "looper")

    # ----- portal submissions
    def handle_submit(self, form: dict) -> None:
        """Called on the main loop with a validated portal form."""
        ssid, psk = form["ssid"], form.get("password", "")
        self.publish(mode="connecting", network=ssid)
        if form.get("name"):
            save_setup(self.state_dir, "device", {"name": form["name"]})
            self.sys.set_hostname(form["name"])
        mq = {}
        if form.get("mqtt_host"):
            mq.update(enabled=True, host=form["mqtt_host"], port=int(form.get("mqtt_port") or 1883))
        if form.get("mqtt_user"):
            mq.update(username=form["mqtt_user"], password=form.get("mqtt_password", ""))
        if mq:
            save_setup(self.state_dir, "mqtt", mq)
        if form.get("ssh_password"):
            self.sys.set_ssh(True, "looper", form["ssh_password"])
        self.sys.set_country(form.get("country") or "US")
        self.stop_portal()
        self.nm.stop_ap()
        try:
            name = self.nm.add_wifi(ssid, psk, bool(form.get("hidden")))
            ok = self.nm.up(name, 45)
        except RuntimeError:
            ok = False
        ip = ok and self.nm.online()
        if ip:
            self.publish(mode="online", network=ssid, ip=ip, announce=True)
            self.sys.restart_looper()
            self.sys.first_boot_done()
        else:
            log.warning("could not join %s", ssid)
            self.start_hotspot(error=f"Couldn't join “{ssid}”. Check the password and try again.")

    # ----- hotspot
    def start_hotspot(self, error: str | None = None) -> bool:
        try:
            self.networks = self.nm.scan()  # scan first: scanning is unreliable once in AP mode
        except Exception:
            log.exception("scan failed")
        self.sys.set_country(self._country())
        if not self.nm.start_ap(self.ap_ssid, self.ap_psk):
            log.error("could not start the setup hotspot")
            self.publish(mode="offline", error="Couldn't start the setup hotspot")
            return False
        self.publish(**self._setup_state(error))
        self.start_portal()
        return True

    def _country(self) -> str:
        return ((read_toml(self.boot_path).get("wifi") or {}).get("country") or "US")

    def start_portal(self, port: int = 80, bind: str = AP_ADDR) -> None:
        if self.portal:
            return
        try:
            self.portal = ThreadingHTTPServer((bind, port), make_portal_handler(self))
        except OSError as e:
            log.error("portal failed to start: %s", e)
            return
        self.portal.daemon_threads = True
        threading.Thread(target=self.portal.serve_forever, name="portal", daemon=True).start()

    def stop_portal(self) -> None:
        if self.portal:
            self.portal.shutdown()
            self.portal.server_close()
            self.portal = None

    def _run_offline(self) -> None:
        """No Wi-Fi: publish a benign 'offline' state (which draws no setup screen, so the
        projector just shows video) and idle. The looper plays from the USB looper.toml."""
        log.info("no Wi-Fi adapter; running offline (video plays, configure via USB looper.toml)")
        self.publish(mode="offline")
        while True:
            time.sleep(3600)

    # ----- main loop
    def run(self) -> None:
        run(["rfkill", "unblock", "wifi"], 5)
        self.publish(mode="starting")
        try:
            self.ensure_hostname()
        except Exception:
            log.exception("could not set hostname")
        # No Wi-Fi hardware (e.g. a non-W Pi Zero): there is nothing to connect to or set up.
        # Stand down quietly instead of flailing at a hotspot that can't exist, so the video
        # just plays -- configure it with a looper.toml on the USB drive.
        if not self.nm.has_wifi():
            self._run_offline()
            return
        new = False
        try:
            new = self.apply_seed_files()
        except Exception:
            log.exception("could not apply looper.toml network settings")
        if new:
            self.nm.try_saved(40)
        has_saved = bool(self.nm.saved_wifi())
        deadline = time.monotonic() + (FIRST_WAIT if has_saved else NO_PROFILE_WAIT)
        mode, offline_since, next_retry, next_poll = "starting", None, 0.0, 0.0
        while True:
            try:
                form = self.actions.get(timeout=2)
                self.handle_submit(form)
                mode = self.state["mode"]
                next_poll = 0.0  # re-probe connectivity right away after a change
                if mode == "setup":
                    next_retry = time.monotonic() + RETRY_EVERY
                continue
            except queue.Empty:
                pass
            now = time.monotonic()
            if mode == "setup":
                if now >= next_retry and self.nm.stations() == 0 and self.nm.saved_wifi():
                    log.info("retrying saved Wi-Fi")
                    self.stop_portal()
                    self.nm.stop_ap()
                    if self.nm.try_saved(40):
                        mode = "starting"
                        continue
                    self.start_hotspot(self.state.get("error"))
                    next_retry = time.monotonic() + RETRY_EVERY
                continue
            # Rate-limit the nmcli connectivity probe: forking nmcli every 2s burned most of
            # a core. Once online we only need to notice a dropout within DROPOUT_WAIT.
            if now < next_poll:
                continue
            next_poll = now + (ONLINE_POLL if mode == "online" else LINK_POLL)
            ip = self.nm.online()
            if ip:
                if mode != "online" or self.state.get("ip") != ip:
                    self.publish(mode="online", ip=ip, network=self.nm.current_ssid())
                    self.sys.first_boot_done()
                mode, offline_since = "online", None
                continue
            if mode == "online":
                offline_since = now
                mode = "offline"
                self.publish(mode="offline")
            limit = deadline if offline_since is None else offline_since + DROPOUT_WAIT
            if now >= limit:
                if self.start_hotspot():
                    mode = "setup"
                    next_retry = time.monotonic() + RETRY_EVERY
                else:
                    deadline = now + 60


# ---------------------------------------------------------------- captive portal
PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Looper setup</title>
<style>
body{{margin:0;font:16px/1.4 system-ui,sans-serif;background:#111418;color:#e8ecf1}}
main{{max-width:480px;margin:0 auto;padding:18px}} h1{{font-size:22px;margin:6px 0 4px}}
p.sub{{color:#8b95a3;margin:0 0 16px}} fieldset{{border:1px solid #2a313b;border-radius:12px;margin:0 0 14px;padding:12px 14px}}
legend{{color:#ff8a3d;font-weight:600;padding:0 6px}} label{{display:block;margin:10px 0 4px;color:#b8c0cc;font-size:14px}}
input,select{{width:100%;box-sizing:border-box;font:inherit;color:#e8ecf1;background:#1b2027;border:1px solid #2a313b;border-radius:8px;padding:11px}}
button{{width:100%;font:inherit;font-weight:600;border:0;border-radius:10px;padding:14px;background:#ff8a3d;color:#111}}
.err{{background:#3a1d1d;border:1px solid #6b2a2a;border-radius:10px;padding:10px 12px;margin-bottom:14px}}
.hint{{color:#8b95a3;font-size:13px;margin-top:4px}} details summary{{cursor:pointer;color:#ff8a3d}}
</style></head><body><main>
<h1>Set up your Video Looper</h1><p class="sub">Connect it to your Wi-Fi. Everything else is optional.</p>
{error}
<form method="post" action="/save">
<fieldset><legend>Wi-Fi</legend>
<label for="ssid">Network</label>
<select id="ssid" name="ssid">{options}<option value="">Other (type it below)…</option></select>
<label for="ssid_other">Other network name</label><input id="ssid_other" name="ssid_other" autocomplete="off">
<label for="password">Password</label><input id="password" name="password" type="password" autocomplete="off">
<div class="hint">The Pi Zero W only sees 2.4&nbsp;GHz networks.</div>
<label for="country">Country</label><input id="country" name="country" value="US" maxlength="2">
</fieldset>
<fieldset><legend>Name</legend>
<label for="name">Looper name</label><input id="name" name="name" placeholder="Video Looper" maxlength="40">
<div class="hint">Shown in Home Assistant. Use different names for more than one looper.</div>
</fieldset>
<details><summary>Home Assistant / MQTT (optional)</summary><fieldset>
<div class="hint">Leave the server empty to find Home Assistant automatically. Mosquitto usually needs a login:
a Home Assistant user or a Mosquitto add-on login works.</div>
<label for="mqtt_host">Server</label><input id="mqtt_host" name="mqtt_host" placeholder="automatic" autocomplete="off">
<label for="mqtt_user">Username</label><input id="mqtt_user" name="mqtt_user" autocomplete="off">
<label for="mqtt_password">Password</label><input id="mqtt_password" name="mqtt_password" type="password" autocomplete="off">
</fieldset>
<fieldset><legend>Remote login (optional)</legend>
<label for="ssh_password">SSH password for user <b>looper</b></label>
<input id="ssh_password" name="ssh_password" type="password" autocomplete="new-password">
<div class="hint">Leave empty to keep SSH off.</div>
</fieldset></details>
<button type="submit">Connect</button>
</form></main></body></html>"""

DONE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Connecting…</title><style>body{{font:17px/1.5 system-ui,sans-serif;background:#111418;color:#e8ecf1;padding:24px}}</style>
</head><body><h1>Connecting to {ssid}…</h1>
<p>This hotspot will close now. <b>Watch the projector</b>: it shows the looper's new address when it's connected,
or comes back with this hotspot if something went wrong.</p></body></html>"""


def make_portal_handler(svc: NetSetup):
    class Portal(BaseHTTPRequestHandler):
        server_version = "looper-setup"

        def log_message(self, fmt, *args):
            log.debug("portal %s", fmt % args)

        def _send(self, code, body: str, ctype="text/html; charset=utf-8", headers=None):
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _ours(self) -> bool:
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in ("", AP_ADDR, "looper.setup")

        def do_GET(self):
            # Any other host (connectivity checks from phones) -> redirect: triggers the captive popup.
            if not self._ours():
                return self._send(302, "", headers={"Location": f"http://{AP_ADDR}/"})
            path = self.path.split("?")[0]
            if path == "/status":
                st = {k: v for k, v in svc.state.items() if k != "psk"}
                return self._send(200, json.dumps(st), "application/json")
            self._send(200, render_page(svc.networks, svc.state.get("error")))

        def do_POST(self):
            if self.path.split("?")[0] != "/save":
                return self._send(404, "not found", "text/plain")
            n = min(int(self.headers.get("Content-Length") or 0), 8192)
            raw = self.rfile.read(n).decode(errors="replace")
            form = {k: v[0] for k, v in urllib.parse.parse_qs(raw, keep_blank_values=True).items()}
            clean, err = validate_form(form)
            if err:
                return self._send(200, render_page(svc.networks, err))
            self._send(200, DONE.format(ssid=html.escape(clean["ssid"])))
            svc.actions.put(clean)

        do_HEAD = do_GET

    return Portal


def render_page(networks: list[dict], error: str | None) -> str:
    opts = "".join(
        f'<option value="{html.escape(n["ssid"], True)}">{html.escape(n["ssid"])}'
        f'{" 🔒" if n.get("secure") else ""} ({n.get("signal", 0)}%)</option>'
        for n in networks[:30]
    )
    err = f'<div class="err">{html.escape(error)}</div>' if error else ""
    return PAGE.format(options=opts, error=err)


def validate_form(form: dict) -> tuple[dict, str | None]:
    ssid = (form.get("ssid_other") or "").strip() or (form.get("ssid") or "").strip()
    if not ssid:
        return {}, "Pick a Wi-Fi network or type its name."
    if len(ssid.encode()) > 32:
        return {}, "That network name is too long."
    pw = form.get("password") or ""
    if pw and not (8 <= len(pw) <= 63):
        return {}, "Wi-Fi passwords are 8 to 63 characters."
    cc = (form.get("country") or "US").strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", cc):
        return {}, "Country should be a two-letter code like US or GB."
    sshpw = form.get("ssh_password") or ""
    if sshpw and len(sshpw) < 8:
        return {}, "Use at least 8 characters for the SSH password."
    host = (form.get("mqtt_host") or "").strip()
    if host and not re.fullmatch(r"[A-Za-z0-9.\-:\[\]]{1,253}", host):
        return {}, "The MQTT server should be a hostname or IP address."
    port = None
    if ":" in host and host.count(":") == 1:
        host, port = host.split(":")
    return {
        "ssid": ssid, "password": pw, "country": cc,
        "hidden": bool(form.get("ssid_other")),
        "name": (form.get("name") or "").strip()[:40],
        "mqtt_host": host, "mqtt_port": port or "1883",
        "mqtt_user": (form.get("mqtt_user") or "").strip(),
        "mqtt_password": form.get("mqtt_password") or "",
        "ssh_password": sshpw,
    }, None


def main() -> int:
    logging.basicConfig(level=os.environ.get("LOOPER_LOG", "INFO").upper(),
                        format="%(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    if os.geteuid() != 0:
        print("looper.netsetup must run as root")
        return 1
    NetSetup().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
