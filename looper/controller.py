"""The controller owns all state and runs everything on ONE thread.

Web, MQTT and mpv events only drop commands into a queue; the main loop executes
them in order and also runs the timers (rescan, position, snapshot, watchdog).
One thread means no races and a tiny, predictable CPU footprint on a Pi Zero.
"""

from __future__ import annotations

import logging
import os
import queue
import shutil
import threading
import time

from . import __version__, sdnotify, snapshot
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
        self.player = MpvPlayer(cfg.section("player"), os.path.join(RUN_DIR, "mpv.sock"), self._on_mpv_event)
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
            "version": __version__,
        }
        self.state.update(cfg.tunables())
        self._lock = threading.Lock()
        self._running = True
        self._errors = 0
        self._player_restarts = 0

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

    def _play(self, path: str | None) -> None:
        if not path:
            try:
                self.player.stop_playback()
            except PlayerError:
                pass
            self._set(status="idle" if self.playlist.files else "no_media",
                      title=None, file=None, index=-1, position=0, duration=0, paused=False)
            return
        log.info("playing %s", path)
        try:
            self.player.play(path)
        except PlayerError as e:
            log.error("play failed: %s", e)
            self.submit("_player_died")
            return
        self._set(status="playing", title=title_of(path), file=os.path.basename(path),
                  index=self.playlist.index, position=0, duration=0, paused=False)
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

    def _take_snapshot(self) -> None:
        if self.state["status"] != "playing" or not self.state["file"]:
            return
        raw = os.path.join(RUN_DIR, "raw.jpg")
        sc = self.cfg.section("snapshot")
        ok = self.player.screenshot(raw) and snapshot.shrink(raw, self.snapshot_path, sc["width"], sc["quality"])
        if not ok and sc.get("ffmpeg_fallback") and shutil.which("ffmpeg") and self.playlist.current:
            ok = snapshot.ffmpeg_grab(self.playlist.current, self.state["position"], self.snapshot_path, sc["width"])
        if ok:
            self._set("snapshot", snapshot_time=int(time.time()))

    def _update_position(self) -> None:
        if self.state["status"] not in ("playing",):
            return
        pos = self.player.get("time-pos")
        if pos is not None:
            self._set(position=int(pos))

    def _handle(self, cmd: str, arg) -> None:
        pl = self.playlist
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
        elif cmd == "_ended":
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
    def run(self) -> None:
        self._start_player()
        self._rescan(initial=True)
        sdnotify.notify("READY=1")
        wd = sdnotify.watchdog_interval()
        now = time.monotonic()
        timers = {"rescan": now, "position": now, "snapshot": now + 5, "watchdog": now, "stats": now}
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
