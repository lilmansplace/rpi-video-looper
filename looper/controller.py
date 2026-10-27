"""The controller owns all state and runs everything on ONE thread.

Web, MQTT and mpv events only drop commands into a queue; the main loop executes
them in order and also runs the timers (rescan, position, snapshot, watchdog).
One thread means no races and a tiny, predictable CPU footprint on a Pi Zero.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import socket
import subprocess
import threading
import time

from . import __version__, compat, screens, sdnotify, snapshot
from .config import RUN_DIR, Config
from .player import MpvPlayer, PlayerError
from .playlist import Playlist, scan, title_of

log = logging.getLogger(__name__)


def _read(path: str, default: str = "") -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


NET_STATE = os.environ.get("LOOPER_NET_STATE", "/run/looper-net/state.json")
NAV_CMDS = {"next", "previous", "restart", "jump", "toggle_pause", "play", "pause", "seek", "stop"}
ANNOUNCE_SECS = 30


_cpu_prev: list = []


def cpu_busy() -> float:
    """CPU use (0-100) since the previous call. Unlike load average, this isn't inflated
    by GStreamer's many mostly-waiting threads (load ~3.5 while the CPU was ~50% busy)."""
    try:
        with open("/proc/stat") as f:
            v = list(map(int, f.readline().split()[1:]))
    except OSError:
        return 0.0
    total, idle = sum(v), v[3] + v[4]
    prev = _cpu_prev[:] if _cpu_prev else [total, idle]
    _cpu_prev[:] = [total, idle]
    dt = total - prev[0]
    return 0.0 if dt <= 0 else 100.0 * (1 - (idle - prev[1]) / dt)


def local_ip() -> str | None:
    """Our LAN address (no packets are sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            ip = s.getsockname()[0]
            return None if ip.startswith("127.") else ip
    except OSError:
        return None


def system_stats() -> dict:
    temp = _read("/sys/class/thermal/thermal_zone0/temp")
    load = _read("/proc/loadavg").split()
    mem_avail = 0
    for line in _read("/proc/meminfo").splitlines():
        if line.startswith("MemAvailable:"):
            mem_avail = int(line.split()[1]) // 1024
            break
    throttled = _read("/sys/devices/platform/soc/soc:firmware/get_throttled", "0")
    return {
        "cpu_temp": round(int(temp) / 1000, 1) if temp.isdigit() else None,
        "load1": float(load[0]) if load else None,
        "mem_avail_mb": mem_avail,
        "throttled": throttled not in ("0", "0x0", ""),
    }


class Controller:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        os.makedirs(RUN_DIR, exist_ok=True)
        self.snapshot_path = os.path.join(RUN_DIR, "snapshot.jpg")
        self.q: queue.Queue = queue.Queue()
        self.listeners: list = []  # fn(state: dict, kind: str) kind in state|library|snapshot
        pb = cfg.section("playback")
        self.playlist = Playlist(shuffle=pb["shuffle"], repeat=pb["repeat"])
        self.player = self._make_player()
        self.state: dict = {
            "status": "starting",
            "title": None,
            "file": None,
            "index": -1,
            "count": 0,
            "position": 0,
            "duration": 0,
            "paused": False,
            "snapshot_time": None,
            "video": None,  # e.g. "h264 1920x1080 30fps (hw)"
            "warnings": [],  # [{"title", "reasons"}] for files in the library
            "warning_count": 0,
            "network": None,  # {"mode", "ip", "network"} from the setup service
            "mqtt": "starting",
            "version": __version__,
        }
        self.state.update(cfg.tunables())
        self._lock = threading.Lock()
        self._running = True
        self._errors = 0
        self._player_restarts = 0
        self.compat_path = os.path.join(cfg.state_dir, "compat.json")
        self.compat_db: dict = self._load_compat()
        self._check_due: float | None = None
        self.overlay: str | None = None  # key of the info screen being shown instead of video
        self.net: dict = {}
        self._net_mtime = None
        self._snapshot_for: str | None = None  # file we already have a still for (videos loop)

    # ---------- public API (thread-safe) ----------
    def submit(self, cmd: str, arg=None) -> None:
        self.q.put((cmd, arg))

    def snapshot_state(self) -> dict:
        with self._lock:
            s = dict(self.state)
        s["system"] = system_stats()
        return s

    def library(self) -> list[str]:
        return self.playlist.titles()

    def shutdown(self) -> None:
        self._running = False
        self.q.put(("_quit", None))

    # ---------- internals ----------
    def _set(self, kind: str = "state", **changes) -> None:
        with self._lock:
            changed = {k: v for k, v in changes.items() if self.state.get(k) != v}
            self.state.update(changed)
        if changed or kind != "state":
            self._emit(kind)

    def _emit(self, kind: str) -> None:
        st = self.snapshot_state()
        for fn in self.listeners:
            try:
                fn(st, kind)
            except Exception:
                log.exception("listener failed")

    def _on_mpv_event(self, ev: dict) -> None:  # runs on the mpv reader thread
        name = ev.get("event")
        if name == "end-file":
            reason = ev.get("reason")
            if reason == "eof":
                self.submit("_ended")
            elif reason == "error":
                self.submit("_file_error", ev.get("file_error"))
        elif name == "file-loaded":
            self.submit("_loaded")
        elif name == "player-died":
            self.submit("_player_died")

    # ---------- format compatibility ----------
    def _load_compat(self) -> dict:
        try:
            with open(self.compat_path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_compat(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.compat_path), exist_ok=True)
            tmp = self.compat_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.compat_db, f)
            os.replace(tmp, self.compat_path)
        except OSError as e:
            log.debug("could not save compat db: %s", e)

    @staticmethod
    def _fingerprint(path: str):
        try:
            st = os.stat(path)
            return [st.st_size, int(st.st_mtime)]
        except OSError:
            return None

    def known_bad(self, path: str) -> list[str]:
        """Reasons recorded for this exact file (re-checked if the file changes)."""
        e = self.compat_db.get(path)
        if e and e.get("fp") == self._fingerprint(path):
            return e.get("reasons", [])
        return []

    def _refresh_warnings(self) -> None:
        warnings = [
            {"title": title_of(p), "reasons": self.known_bad(p)}
            for p in self.playlist.files
            if self.known_bad(p)
        ]
        self._set(warnings=warnings, warning_count=len(warnings))

    def _check_current(self) -> None:
        """Ask mpv what it is actually playing and judge it against the Pi's limits."""
        path = self.playlist.current
        if not path or self.state["status"] not in ("playing", "paused"):
            return
        p = self.player.get
        codec = p("video-format") or p("video-codec")
        width, height = p("width"), p("height")
        fps = p("container-fps") or p("estimated-vf-fps")
        hwdec = p("hwdec-current")
        if codec is None and width is None:  # audio-only or not ready
            return
        wanted = str(self.cfg.section("player").get("hwdec", "")).lower() not in ("no", "")
        reasons = compat.check(codec, width, height, fps, hwdec, hwdec_wanted=wanted)
        desc = f"{compat.normalize_codec(codec) or '?'} {width or '?'}x{height or '?'}"
        if fps:
            desc += f" {float(fps):.0f}fps"
        desc += " (hw)" if hwdec not in (None, "", "no") else " (software)"
        self._set(video=desc)
        old = self.compat_db.get(path, {}).get("reasons")
        self.compat_db[path] = {"fp": self._fingerprint(path), "reasons": reasons, "video": desc}
        if old != reasons:
            self._save_compat()
            if reasons:
                log.warning("%s won't play smoothly: %s", title_of(path), "; ".join(reasons))
        self._refresh_warnings()
        if reasons and self.cfg.get("skip_incompatible") and self._has_good_file():
            log.info("skipping incompatible %s", title_of(path))
            self._play(self.playlist.next())

    def _has_good_file(self) -> bool:
        return any(not self.known_bad(f) for f in self.playlist.files)

    def _play(self, path: str | None) -> None:
        if path and self.cfg.get("skip_incompatible") and self.known_bad(path) and self._has_good_file():
            for _ in range(len(self.playlist.files)):
                path = self.playlist.next()
                if not path or not self.known_bad(path):
                    break
        self._check_due = None
        self.overlay = None
        if not path:
            try:
                self.player.stop_playback()
            except PlayerError:
                pass
            self._set(status="idle" if self.playlist.files else "no_media",
                      title=None, file=None, index=-1, position=0, duration=0, paused=False, video=None)
            return
        log.info("playing %s", path)
        try:
            self.player.play(path)
        except PlayerError as e:
            log.error("play failed: %s", e)
            self.submit("_player_died")
            return
        self._set(status="playing", title=title_of(path), file=os.path.basename(path),
                  index=self.playlist.index, position=0, duration=0, paused=False, video=None)
        try:
            with open(os.path.join(self.cfg.state_dir, "last"), "w") as f:
                f.write(path)
        except OSError:
            pass

    def _rescan(self, initial: bool = False) -> None:
        m = self.cfg.section("media")
        dev = m.get("device")
        if dev and not os.path.exists(dev):
            files = []
        else:
            files = scan(m["dir"], m["extensions"], m.get("recursive", True))
        if self.playlist.set_files(files) or initial:
            log.info("library: %d files", len(files))
            self._set("library", count=len(files))
            self._refresh_warnings()
            cur = self.playlist.current
            if not files:
                self._play(None)
            elif cur is None or cur not in files:
                # nothing playing, or the playing file vanished (USB pulled)
                if initial and self.cfg.section("playback").get("resume_on_boot"):
                    last = _read(os.path.join(self.cfg.state_dir, "last"))
                    if last in files:
                        self._play(self.playlist.jump(last))
                        return
                self._play(self.playlist.next())

    def _start_player(self) -> bool:
        try:
            self.player.start()
            self.player.set("volume", self.cfg.get("volume"))
            self._player_restarts = 0
            return True
        except Exception as e:
            self._player_restarts += 1
            log.error("mpv failed to start (%d): %s", self._player_restarts, e)
            self._set(status="error")
            return False

    def _make_player(self):
        pc = self.cfg.section("player")
        if str(pc.get("backend", "gstreamer")).lower() == "gstreamer":
            from . import gstplayer
            if gstplayer.Gst is not None:
                sc = self.cfg.section("snapshot")
                snap = {"path": self.snapshot_path, "interval": self.cfg.get("snapshot_interval"),
                        "width": sc.get("width"), "quality": sc.get("quality")}
                return gstplayer.GstPlayer(pc, None, self._on_mpv_event, snapshot=snap)
            log.warning("GStreamer bindings not installed; falling back to mpv")
        return MpvPlayer(pc, os.path.join(RUN_DIR, "mpv.sock"), self._on_mpv_event)

    def _snapshot_method(self) -> str:
        sc = self.cfg.section("snapshot")
        m = str(sc.get("method", "auto")).lower()
        if m in ("file", "player", "live"):
            return m
        if self.player.__class__.__name__ == "GstPlayer":
            return "live"  # the GStreamer player writes a live JPEG straight off the decoded frames
        hw = str(self.cfg.section("player").get("hwdec", "")).lower()
        zero_copy = hw not in ("", "no") and not hw.endswith("-copy") and hw != "auto-safe"
        return "file" if zero_copy else "player"

    def _take_snapshot(self) -> None:
        if self.state["status"] != "playing" or not self.state["file"] or self.overlay:
            return
        sc = self.cfg.section("snapshot")
        method = self._snapshot_method()
        if method == "live":
            # the GStreamer pipeline writes snapshot.jpg itself from the decoded frames;
            # nothing to decode here -- just reflect how fresh the file is.
            try:
                mt = int(os.stat(self.snapshot_path).st_mtime)
            except OSError:
                return
            if mt != self.state.get("snapshot_time"):
                self._set("snapshot", snapshot_time=mt)
            return
        if method == "player":
            raw = os.path.join(RUN_DIR, "raw.jpg")
            if self.player.screenshot(raw) and snapshot.shrink(raw, self.snapshot_path, sc["width"], sc["quality"]):
                self._set("snapshot", snapshot_time=int(time.time()))
            return
        # file method: run off the main thread so controls never wait on ffmpeg
        if getattr(self, "_snap_thread", None) and self._snap_thread.is_alive():
            return
        video = self.playlist.current
        if not shutil.which("ffmpeg") or not video:
            return
        if self._snapshot_for == video:
            return  # videos loop, so one still per file is enough: decoding a 1080p keyframe in
            # software every interval pegged the single core and stuttered playback
        if cpu_busy() > 85:  # Pi is busy: a snapshot can wait, playback can't
            return
        pos = float(self.player.get("time-pos", self.state["position"]) or 0)

        def work():
            if snapshot.ffmpeg_grab(video, pos, self.snapshot_path, sc["width"]):
                self._snapshot_for = video
                self._set("snapshot", snapshot_time=int(time.time()))

        self._snap_thread = threading.Thread(target=work, name="snapshot", daemon=True)
        self._snap_thread.start()

    def _update_position(self) -> None:
        if self.state["status"] not in ("playing",):
            return
        pos = self.player.get("time-pos")
        if pos is not None:
            self._set(position=int(pos))

    # ---------- projector info screens ----------
    def _read_net(self) -> None:
        try:
            mt = os.stat(NET_STATE).st_mtime
        except OSError:
            mt = None
        if mt != self._net_mtime:
            self._net_mtime = mt
            try:
                with open(NET_STATE) as f:
                    self.net = json.load(f)
            except (OSError, ValueError):
                self.net = {}
            self._set(network={k: self.net.get(k) for k in ("mode", "ip", "network")})

    def _wanted_overlay(self) -> tuple[str, dict] | tuple[None, None]:
        n = self.net
        mode = n.get("mode")
        if mode == "setup":
            return f"setup:{n.get('t')}", n
        if mode == "connecting":
            return f"connecting:{n.get('network')}", n
        port = self.cfg.section("web")["port"]
        ip = n.get("ip") if mode == "online" else None
        if mode == "online" and n.get("announce") and time.time() - n.get("t", 0) < ANNOUNCE_SECS:
            return f"online:{n.get('t')}", {**n, "web_port": port, "mqtt": self._mqtt_line()}
        if self.state["status"] == "no_media":
            ip = ip or local_ip()
            if ip:
                return f"nomedia:{ip}", {"mode": "online", "ip": ip, "network": n.get("network"),
                                          "web_port": port, "title": "No videos found",
                                          "note": "Copy H.264 MP4 videos onto a USB drive and plug it in.",
                                          "mqtt": self._mqtt_line()}
        return None, None

    def _mqtt_line(self) -> str | None:
        m = self.state.get("mqtt") or ""
        return f"Home Assistant: {m}" if m and m not in ("off", "starting") else None

    def _net_tick(self) -> None:
        self._read_net()
        key, st = self._wanted_overlay()
        if key == self.overlay:
            return
        if key is None:
            # info screen no longer needed: go back to the videos
            self.overlay = None
            if self.playlist.files:
                self._play(self.playlist.current or self.playlist.next())
            return
        path = os.path.join(RUN_DIR, "screen.png")
        if screens.render(st, path):
            try:
                self.player.play(path)
                self.overlay = key
                kind = key.split(":")[0]
                status = "setup" if kind in ("setup", "connecting") else "no_media" if kind == "nomedia" else "info"
                self._set(status=status, title=None, file=None, position=0, duration=0, video=None)
            except PlayerError as e:
                log.error("could not show info screen: %s", e)

    def _handle(self, cmd: str, arg) -> None:
        pl = self.playlist
        if self.overlay and cmd in NAV_CMDS:
            if self.overlay.split(":")[0] in ("setup", "connecting"):
                log.info("ignoring %s during network setup", cmd)
                return
            if cmd in ("toggle_pause", "play", "restart") and pl.files:
                self._play(pl.current or pl.next())
                return
        if cmd == "next":
            self._play(pl.next())
        elif cmd == "previous":
            # like a CD player: >5s in restarts the track, otherwise go back
            if self.state["position"] > 5 and pl.current:
                self._handle("restart", None)
            else:
                self._play(pl.previous())
        elif cmd == "restart":
            try:
                self.player.seek(0)
                self._set(position=0)
            except PlayerError:
                self._play(pl.current)
        elif cmd == "jump":
            path = pl.jump(arg)
            if path:
                self._play(path)
            else:
                log.warning("jump: no such file %r", arg)
        elif cmd in ("pause", "play", "toggle_pause"):
            try:
                if cmd == "toggle_pause":
                    paused = self.player.toggle_pause()
                else:
                    paused = cmd == "pause"
                    self.player.set("pause", paused)
                if self.state["status"] in ("idle",) and not paused:
                    self._play(pl.next())
                else:
                    self._set(paused=paused, status="paused" if paused else "playing")
            except PlayerError as e:
                log.error("%s failed: %s", cmd, e)
        elif cmd == "stop":
            self._play(None)
        elif cmd == "seek":
            try:
                self.player.seek(float(arg))
            except (PlayerError, TypeError, ValueError) as e:
                log.error("seek failed: %s", e)
        elif cmd == "rescan":
            self._rescan()
        elif cmd == "set":
            key, value = arg
            try:
                value = self.cfg.set(key, value)
            except ValueError as e:
                log.error("set %s: %s", key, e)
                return
            if key == "shuffle":
                pl.set_shuffle(value)
            elif key == "repeat":
                pl.repeat = value
            elif key == "skip_incompatible" and value and pl.current and self.known_bad(pl.current) \
                    and self._has_good_file():
                self._play(pl.next())
            elif key == "volume":
                try:
                    self.player.set("volume", value)
                except PlayerError:
                    pass
            self._set(**{key: value})
        # ---- internal events ----
        elif cmd == "_loaded":
            self._errors = 0
            dur = self.player.get("duration")
            self._set(duration=int(dur or 0))
            # give the decoder a moment so hwdec-current is meaningful
            if not self.overlay:
                self._check_due = time.monotonic() + 2.5
        elif cmd == "_ended":
            if self.overlay:
                return
            self._play(pl.next(auto=True))
        elif cmd == "_file_error":
            self._errors += 1
            log.warning("cannot play %s (%s)", self.state["file"], arg)
            if self._errors >= max(3, len(pl.files)):
                log.error("every file failed; waiting for a rescan")
                self._set(status="error")
                self._errors = 0
                return
            time.sleep(0.5)  # back off so a bad drive can't spin the CPU
            self._play(pl.next())
        elif cmd == "_player_died":
            if self._running and not self.player.alive():
                time.sleep(min(30, 2 ** min(self._player_restarts, 5)))
                if self._start_player():
                    self._play(pl.current or pl.next())

    # ---------- main loop ----------
    def _wait_for_boot(self) -> None:
        """Hold the video until boot work (cloud-init, network setup, ...) has finished.

        On a single-core Pi Zero W, starting playback while boot is still busy starved
        everything (screen, SSH, Wi-Fi) for minutes. Wait until systemd says boot is done
        and the load has calmed down, up to playback.boot_settle_max seconds.
        """
        max_wait = int(os.environ.get("LOOPER_BOOT_SETTLE",
                                      self.cfg.section("playback").get("boot_settle_max", 300)) or 0)
        if max_wait <= 0:
            return
        sdnotify.notify("READY=1")  # web UI is up; don't hold up the boot itself
        self._set(status="starting")
        deadline, calm = time.monotonic() + max_wait, 0
        cpu_busy()  # start a fresh measurement window
        while self._running and time.monotonic() < deadline:
            try:
                st = subprocess.run(["systemctl", "is-system-running"], capture_output=True,
                                    text=True, timeout=10).stdout.strip()
            except (OSError, subprocess.TimeoutExpired):
                st = "unknown"
            busy = cpu_busy()  # real CPU use since the last check (load average lies here:
            # a task stuck waiting on hardware kept it at 3.4 on an idle Pi)
            calm = calm + 1 if st not in ("initializing", "starting") and busy < 60 else 0
            if calm >= 3:
                log.info("boot settled (system %s, cpu %.0f%%)", st, busy)
                return
            sdnotify.notify("WATCHDOG=1")
            time.sleep(3)
        log.info("starting playback anyway after %ss", max_wait)

    def run(self) -> None:
        self._wait_for_boot()
        self._start_player()
        self._rescan(initial=True)
        sdnotify.notify("READY=1")
        wd = sdnotify.watchdog_interval()
        now = time.monotonic()
        timers = {"rescan": now, "position": now, "snapshot": now + 5, "watchdog": now, "stats": now, "net": now}
        while self._running:
            try:
                cmd, arg = self.q.get(timeout=1.0)
                if cmd == "_quit":
                    break
                self._handle(cmd, arg)
            except queue.Empty:
                pass
            except Exception:
                log.exception("command failed")

            now = time.monotonic()
            if self._check_due and now >= self._check_due:
                self._check_due = None
                try:
                    self._check_current()
                except Exception:
                    log.exception("compatibility check failed")
            if now >= timers["net"]:
                try:
                    self._net_tick()
                except Exception:
                    log.exception("info screen update failed")
                timers["net"] = now + 2
            if wd and now >= timers["watchdog"]:
                # only pet the watchdog if mpv is healthy (or deliberately idle)
                if self.player.alive() or self.state["status"] in ("no_media",):
                    sdnotify.notify("WATCHDOG=1")
                timers["watchdog"] = now + wd
            ri = self.cfg.get("rescan_interval")
            if ri and now >= timers["rescan"]:
                self._rescan()
                timers["rescan"] = now + ri
            if now >= timers["position"]:
                self._update_position()
                timers["position"] = now + self.cfg.get("position_interval")
            si = self.cfg.get("snapshot_interval")
            if si and now >= timers["snapshot"]:
                self._take_snapshot()
                timers["snapshot"] = now + si
            if now >= timers["stats"]:
                self._emit("stats")
                timers["stats"] = now + 60
        self.player.stop()
