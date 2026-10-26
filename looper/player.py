"""mpv wrapper over its JSON IPC socket.

mpv runs as one long-lived process in --idle mode; we feed it one file at a time
with `loadfile`. Keeping a single process avoids the cost of re-initialising the
display and decoder for every video, which is slow on a Pi Zero.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import queue
import socket
import subprocess
import threading
import time

log = logging.getLogger(__name__)


class PlayerError(Exception):
    pass


class MpvPlayer:
    def __init__(self, cfg: dict, socket_path: str, on_event=None):
        self.cfg = cfg
        self.socket_path = socket_path
        self.on_event = on_event or (lambda ev: None)
        self.proc: subprocess.Popen | None = None
        self.sock: socket.socket | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, queue.Queue] = {}
        self._lock = threading.Lock()
        self._reader: threading.Thread | None = None

    # ---------- process ----------
    def args(self) -> list[str]:
        c = self.cfg
        a = [
            "mpv",
            "--idle=yes",
            f"--input-ipc-server={self.socket_path}",
            "--fullscreen",
            "--no-terminal",
            "--really-quiet",
            "--osc=no",
            "--osd-level=0",
            "--no-input-default-bindings",
            "--input-vo-keyboard=no",
            "--keep-open=no",
            "--image-display-duration=inf",  # setup/info screens stay up until replaced
            "--force-window=yes",  # black screen between files instead of console
            "--background-color=#000000",
            "--cursor-autohide=always",
            "--load-scripts=no",
            "--ytdl=no",
            "--sub-auto=no",
            "--audio-file-auto=no",
            "--screenshot-format=jpg",
            "--screenshot-jpeg-quality=85",
            "--hr-seek=no",  # keyframe seeks are much cheaper
            "--demuxer-max-bytes=16MiB",  # cap RAM use (512 MB total on a Zero)
            "--demuxer-max-back-bytes=2MiB",
            f"--vo={c.get('vo', 'gpu')}",
            f"--hwdec={c.get('hwdec', 'v4l2m2m')}",
            f"--ao={c.get('audio_output', 'alsa')}",
        ]
        if c.get("gpu_context") and c.get("vo") == "gpu":
            a.append(f"--gpu-context={c['gpu_context']}")
        if c.get("profile"):
            a.append(f"--profile={c['profile']}")
        if c.get("display_mode") and (c.get("vo") == "drm" or c.get("gpu_context") == "drm"):
            a.append(f"--drm-mode={c['display_mode']}")
        a += list(c.get("extra_args", []))
        return a

    def start(self, timeout: float = 15.0) -> None:
        self.stop()
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        args = self.args()
        log.info("starting mpv: %s", " ".join(args))
        self.proc = subprocess.Popen(args, stdin=subprocess.DEVNULL)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise PlayerError(f"mpv exited with {self.proc.returncode}")
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(self.socket_path)
                self.sock = s
                break
            except (FileNotFoundError, ConnectionRefusedError):
                s.close()
                time.sleep(0.1)
        else:
            raise PlayerError("mpv IPC socket never appeared")
        self._reader = threading.Thread(target=self._read_loop, name="mpv-ipc", daemon=True)
        self._reader.start()

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None and self.sock is not None

    def stop(self) -> None:
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    # ---------- IPC ----------
    def _read_loop(self) -> None:
        buf = b""
        sock = self.sock
        while sock is not None:
            try:
                chunk = sock.recv(65536)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                rid = msg.get("request_id")
                if rid is not None and rid in self._pending:
                    self._pending.pop(rid).put(msg)
                elif "event" in msg:
                    try:
                        self.on_event(msg)
                    except Exception:
                        log.exception("event handler failed")
        log.warning("mpv IPC connection closed")
        self.sock = None
        self.on_event({"event": "player-died"})

    def command(self, *args, timeout: float = 5.0):
        if not self.sock:
            raise PlayerError("mpv not running")
        rid = next(self._ids)
        q: queue.Queue = queue.Queue(1)
        self._pending[rid] = q
        data = json.dumps({"command": list(args), "request_id": rid}).encode() + b"\n"
        with self._lock:
            try:
                self.sock.sendall(data)
            except OSError as e:
                self._pending.pop(rid, None)
                raise PlayerError(str(e)) from e
        try:
            msg = q.get(timeout=timeout)
        except queue.Empty:
            self._pending.pop(rid, None)
            raise PlayerError(f"timeout waiting for {args[0]}")
        if msg.get("error") != "success":
            raise PlayerError(f"{args[0]}: {msg.get('error')}")
        return msg.get("data")

    def get(self, prop: str, default=None):
        try:
            return self.command("get_property", prop)
        except PlayerError:
            return default

    def set(self, prop: str, value) -> None:
        self.command("set_property", prop, value)

    # ---------- high level ----------
    def play(self, path: str) -> None:
        self.command("loadfile", path, "replace")
        self.set("pause", False)

    def stop_playback(self) -> None:
        self.command("stop")

    def toggle_pause(self) -> bool:
        paused = not bool(self.get("pause", False))
        self.set("pause", paused)
        return paused

    def seek(self, seconds: float, mode: str = "absolute") -> None:
        self.command("seek", seconds, mode)

    def screenshot(self, path: str) -> bool:
        """Grab the current video frame (without OSD) to a file."""
        # "video" needs the decoded frame in RAM, which zero-copy hwdec doesn't have;
        # "window" reads back what's on screen instead (720p, a brief GPU readback).
        for mode in ("video", "window"):
            try:
                self.command("screenshot-to-file", path, mode, timeout=10)
                if os.path.exists(path):
                    return True
            except PlayerError as e:
                log.debug("mpv %s screenshot failed: %s", mode, e)
        return False
