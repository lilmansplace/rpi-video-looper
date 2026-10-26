"""GStreamer player: hardware H.264 decode straight onto a display plane.

Measured on a Pi Zero W (tools/gst-test.sh), 1080p30 H.264:
  v4l2h264dec capture-io-mode=dmabuf -> kmssink   29.75 fps shown, 0 dropped, 52% CPU, smooth
  mpv (best setup found)                          ~29 fps shown, ~2 drops/s, 80%+ CPU, stuttery
The decoded frames never touch the CPU: the VideoCore decoder hands a dmabuf to the
display controller, which scales it onto the screen. It's the modern equivalent of
what omxplayer did with the old firmware APIs.

Same interface as MpvPlayer so the controller can use either.
"""

from __future__ import annotations

import logging
import os
import threading

from .player import PlayerError

log = logging.getLogger(__name__)

try:
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
except (ImportError, ValueError):  # pragma: no cover
    Gst = None

IMAGE_EXTS = {".png", ".jpg", ".jpeg"}
DEFAULT_SINK = "kmssink driver-name=vc4 force-modesetting=false"
DEFAULT_HW = "v4l2h264dec capture-io-mode=dmabuf"


class GstPlayer:
    def __init__(self, cfg: dict, socket_path: str | None = None, on_event=None):
        self.cfg = cfg
        self.on_event = on_event or (lambda ev: None)
        self.pipeline = None
        self.path: str | None = None
        self._alive = False
        self._paused = False
        self._volume = 1.0
        self._info: dict = {}
        self._lock = threading.RLock()
        self._bus_thread: threading.Thread | None = None
        self._gen = 0  # bumps on every new pipeline so stale bus threads exit

    # ---------- lifecycle ----------
    def start(self, timeout: float = 15.0) -> None:
        if Gst is None:
            raise PlayerError("GStreamer Python bindings missing (python3-gi, gir1.2-gstreamer-1.0)")
        Gst.init(None)
        if not Gst.ElementFactory.find(self.cfg.get("video_sink", DEFAULT_SINK).split()[0]):
            raise PlayerError(f"GStreamer element {self.cfg.get('video_sink', DEFAULT_SINK).split()[0]} missing")
        self._alive = True
        log.info("GStreamer %s ready (sink: %s)", Gst.version_string(), self.cfg.get("video_sink", DEFAULT_SINK))

    def alive(self) -> bool:
        return self._alive

    def stop(self) -> None:
        self._teardown()
        self._alive = False

    def _teardown(self) -> None:
        with self._lock:
            self._gen += 1
            p, self.pipeline = self.pipeline, None
        if p is not None:
            p.set_state(Gst.State.NULL)
            p.get_state(3 * Gst.SECOND)

    # ---------- building pipelines ----------
    def _sink_desc(self) -> str:
        return self.cfg.get("video_sink") or DEFAULT_SINK

    def _make_branch(self, desc: str):
        """Build a bin from `desc` whose only external pad is a sink on its first element.

        (parse_bin_from_description(..., True) also ghosts decodebin's not-yet-existing
        outputs, which then breaks linking: audio got NOFORMAT, images a data-flow error.)
        """
        first, rest = desc.split("!", 1) if "!" in desc else (desc, "")
        desc = f"{first.strip()} name=branch_in" + (f" ! {rest.strip()}" if rest else "")
        b = Gst.parse_bin_from_description(desc, False)
        b.add_pad(Gst.GhostPad.new("sink", b.get_by_name("branch_in").get_static_pad("sink")))
        return b

    def _on_pad_added(self, parsebin, pad, pipeline, gen):
        if gen != self._gen:
            return
        caps = pad.get_current_caps() or pad.query_caps(None)
        if not caps or caps.get_size() == 0:
            return
        st = caps.get_structure(0)
        name = st.get_name()
        info = self._info
        if name.startswith("video/") and "video" not in info:
            codec = name.split("/", 1)[1].replace("x-", "")
            ok_w, w = st.get_int("width")
            ok_h, h = st.get_int("height")
            ok_f, num, den = st.get_fraction("framerate")
            info.update(video=codec, width=w if ok_w else None, height=h if ok_h else None,
                        fps=(num / den) if ok_f and den else None)
            if codec == "h264" and self.cfg.get("hwdec", "v4l2m2m") not in ("no", ""):
                desc = f"queue max-size-buffers=8 ! {self.cfg.get('hw_decoder') or DEFAULT_HW} ! {self._sink_desc()}"
                info["hw"] = "v4l2m2m"
            else:
                desc = f"queue ! decodebin ! videoconvert ! videoscale ! {self._sink_desc()}"
                info["hw"] = "no"
        elif name.startswith("audio/") and "audio" not in info and self.cfg.get("audio_output", "alsa") not in ("null", "none", ""):
            info["audio"] = name
            sink = "alsasink" if self.cfg.get("audio_output", "alsa") == "alsa" else "autoaudiosink"
            desc = (f"queue ! decodebin ! audioconvert ! audioresample ! "
                    f"volume name=vol volume={self._volume} ! {sink}")
        else:
            desc = "queue ! fakesink sync=false"
        try:
            branch = self._make_branch(desc)
            pipeline.add(branch)
            branch.sync_state_with_parent()
            res = pad.link(branch.get_static_pad("sink"))
            if res != Gst.PadLinkReturn.OK:
                log.warning("could not link %s: %s", name, res)
        except Exception as e:
            log.error("could not build %s branch: %s", name, e)

    def _build(self, path: str):
        ext = os.path.splitext(path)[1].lower()
        p = Gst.Pipeline.new("looper")
        src = Gst.ElementFactory.make("filesrc", None)
        src.set_property("location", path)
        p.add(src)
        if ext in IMAGE_EXTS:
            # info screens (setup QR codes etc.): decode once, hold the frame
            branch = self._make_branch(f"decodebin ! videoconvert ! videoscale ! imagefreeze ! {self._sink_desc()}")
            p.add(branch)
            src.get_static_pad("src").link(branch.get_static_pad("sink"))
            self._info = {"image": True}
        else:
            parse = Gst.ElementFactory.make("parsebin", None)
            p.add(parse)
            src.link(parse)
            self._info = {}
            parse.connect("pad-added", self._on_pad_added, p, self._gen)
        return p

    def _bus_loop(self, pipeline, gen) -> None:
        bus = pipeline.get_bus()
        loaded = False
        mask = Gst.MessageType.EOS | Gst.MessageType.ERROR | Gst.MessageType.ASYNC_DONE
        while gen == self._gen:
            msg = bus.timed_pop_filtered(250 * Gst.MSECOND, mask)
            if msg is None or gen != self._gen:
                continue
            t = msg.type
            if t == Gst.MessageType.ASYNC_DONE and not loaded:
                loaded = True
                self.on_event({"event": "file-loaded"})
            elif t == Gst.MessageType.EOS:
                if not self._info.get("image"):
                    self.on_event({"event": "end-file", "reason": "eof"})
                return
            elif t == Gst.MessageType.ERROR:
                err, dbg = msg.parse_error()
                log.error("GStreamer: %s (%s)", err.message, (dbg or "").split("\n")[0])
                self.on_event({"event": "end-file", "reason": "error", "file_error": err.message})
                return

    # ---------- interface used by the controller ----------
    def play(self, path: str) -> None:
        if not self._alive:
            raise PlayerError("player not started")
        with self._lock:
            # same file again (repeat-one, or a one-video loop): seek to the start instead
            # of rebuilding, so the loop is seamless and the display isn't re-initialised
            if (path == self.path and self.pipeline is not None
                    and os.path.splitext(path)[1].lower() not in IMAGE_EXTS):
                ok = self.pipeline.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT, 0)
                if ok:
                    self.pipeline.set_state(Gst.State.PLAYING)
                    self._paused = False
                    self._gen += 1
                    gen = self._gen
                    self._bus_thread = threading.Thread(target=self._bus_loop, args=(self.pipeline, gen),
                                                        name="gst-bus", daemon=True)
                    self._bus_thread.start()
                    self.on_event({"event": "file-loaded"})
                    return
        self._teardown()
        with self._lock:
            pipeline = self._build(path)
            self.pipeline, self.path, self._paused = pipeline, path, False
            gen = self._gen
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self._teardown()
            raise PlayerError(f"could not play {os.path.basename(path)}")
        self._bus_thread = threading.Thread(target=self._bus_loop, args=(pipeline, gen), name="gst-bus", daemon=True)
        self._bus_thread.start()

    def stop_playback(self) -> None:
        self._teardown()
        self.path = None

    def toggle_pause(self) -> bool:
        self.set("pause", not self._paused)
        return self._paused

    def seek(self, seconds: float, mode: str = "absolute") -> None:
        p = self.pipeline
        if p is None:
            raise PlayerError("nothing playing")
        if not p.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                             int(max(0.0, float(seconds)) * Gst.SECOND)):
            raise PlayerError("seek failed")

    def get(self, prop: str, default=None):
        p, info = self.pipeline, self._info
        try:
            if prop == "time-pos" and p is not None:
                ok, pos = p.query_position(Gst.Format.TIME)
                return pos / Gst.SECOND if ok else default
            if prop == "duration" and p is not None:
                ok, dur = p.query_duration(Gst.Format.TIME)
                return dur / Gst.SECOND if ok else default
            if prop == "pause":
                return self._paused
            if prop == "volume":
                return round(self._volume * 100)
            mapping = {"video-format": "video", "video-codec": "video", "width": "width", "height": "height",
                       "container-fps": "fps", "hwdec-current": "hw"}
            if prop in mapping:
                return info.get(mapping[prop], default)
        except Exception:
            pass
        return default

    def set(self, prop: str, value) -> None:
        p = self.pipeline
        if prop == "pause":
            self._paused = bool(value)
            if p is not None:
                p.set_state(Gst.State.PAUSED if self._paused else Gst.State.PLAYING)
        elif prop == "volume":
            self._volume = max(0.0, min(1.0, float(value) / 100))
            vol = p.get_by_name("vol") if p is not None else None
            if vol is not None:
                vol.set_property("volume", self._volume)

    def command(self, *args, timeout: float = 5.0):
        raise PlayerError(f"{args[0] if args else 'command'} not supported by the GStreamer player")

    def screenshot(self, path: str) -> bool:
        return False  # snapshots come from the video file (snapshot.method = "file")
