"""GStreamer player tests. Need GStreamer + PyGObject and ffmpeg; skipped otherwise.

Off-device the display sink is swapped for fakesink and the hardware decoder for
avdec_h264, so the same pipeline logic runs everywhere.
"""

import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import TMP  # noqa: E402,F401

from looper import gstplayer  # noqa: E402

HAVE = gstplayer.Gst is not None and shutil.which("ffmpeg")
CFG = {"backend": "gstreamer", "hwdec": "v4l2m2m", "audio_output": "null",
       "video_sink": "fakesink sync=true", "hw_decoder": "avdec_h264"}


def make_clip(path, secs=2, size="320x180", audio=True):
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=30"]
    if audio:
        cmd += ["-f", "lavfi", "-i", "sine=f=440"]
    cmd += ["-t", str(secs), "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", "30"]
    if audio:
        cmd += ["-c:a", "aac", "-shortest"]
    subprocess.run(cmd + [path], check=True)


@unittest.skipUnless(HAVE, "GStreamer bindings or ffmpeg not available")
class GstPlayerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = tempfile.mkdtemp()
        cls.clip = os.path.join(cls.d, "clip.mp4")
        make_clip(cls.clip)
        cls.png = os.path.join(cls.d, "screen.png")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=320x180",
                        "-frames:v", "1", cls.png], check=True)

    def setUp(self):
        self.events = queue.Queue()
        self.p = gstplayer.GstPlayer(dict(CFG), None, self.events.put)
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def wait_event(self, name, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            try:
                ev = self.events.get(timeout=0.2)
            except queue.Empty:
                continue
            if ev.get("event") == name:
                return ev
        self.fail(f"no {name} event")

    def test_play_to_end_reports_eof(self):
        self.p.play(self.clip)
        self.wait_event("file-loaded")
        self.assertEqual(self.p.get("video-format"), "h264")
        self.assertEqual(self.p.get("hwdec-current"), "v4l2m2m")
        self.assertEqual((self.p.get("width"), self.p.get("height")), (320, 180))
        self.assertAlmostEqual(self.p.get("duration"), 2.0, delta=0.2)
        ev = self.wait_event("end-file")
        self.assertEqual(ev["reason"], "eof")

    def test_repeat_same_file_seeks_instead_of_rebuilding(self):
        self.p.play(self.clip)
        self.wait_event("end-file")
        first = self.p.pipeline
        self.p.play(self.clip)
        self.assertIs(self.p.pipeline, first)
        self.wait_event("end-file")

    def test_pause_seek_position(self):
        long = os.path.join(self.d, "long.mp4")
        make_clip(long, secs=8, audio=False)
        self.p.play(long)
        self.wait_event("file-loaded")
        time.sleep(1.0)
        self.assertGreater(self.p.get("time-pos", 0), 0.3)
        self.assertTrue(self.p.toggle_pause())
        time.sleep(0.3)
        a = self.p.get("time-pos")
        time.sleep(0.6)
        self.assertAlmostEqual(self.p.get("time-pos"), a, delta=0.1)
        self.p.set("pause", False)
        self.p.seek(5)
        time.sleep(0.5)
        self.assertGreaterEqual(self.p.get("time-pos"), 4.5)

    def test_image_holds_without_eof(self):
        self.p.play(self.png)
        time.sleep(1.5)
        while not self.events.empty():
            self.assertNotEqual(self.events.get().get("event"), "end-file")

    def test_audio_and_volume(self):
        p = gstplayer.GstPlayer(dict(CFG, audio_output="fake"), None, self.events.put)
        p.cfg["audio_output"] = "auto"
        # autoaudiosink may not exist in CI; use fakesink by monkeypatching the description
        p._sink_desc = lambda: "fakesink sync=true"
        orig = p._make_branch
        p._make_branch = lambda desc: orig(desc.replace("autoaudiosink", "fakesink sync=true"))
        p.start()
        try:
            p.set("volume", 40)
            p.play(self.clip)
            self.wait_event("file-loaded")
            self.assertTrue(p.get("video-format"))
            self.assertIsNotNone(p.pipeline.get_by_name("vol"))
            self.assertAlmostEqual(p.pipeline.get_by_name("vol").get_property("volume"), 0.4, places=2)
            p.set("volume", 80)
            self.assertAlmostEqual(p.pipeline.get_by_name("vol").get_property("volume"), 0.8, places=2)
        finally:
            p.stop()

    def test_broken_file_reports_error(self):
        bad = os.path.join(self.d, "bad.mp4")
        with open(bad, "wb") as f:
            f.write(b"not a video" * 100)
        self.p.play(bad)
        ev = self.wait_event("end-file")
        self.assertEqual(ev["reason"], "error")


@unittest.skipUnless(HAVE, "GStreamer bindings or ffmpeg not available")
class ControllerWithGstTests(unittest.TestCase):
    def test_auto_advance_and_snapshot_method(self):
        from looper import config as config_mod
        from looper import controller

        media = tempfile.mkdtemp()
        for n in ("a", "b"):
            make_clip(os.path.join(media, f"{n}.mp4"), secs=1, audio=False)
        cfg = config_mod.Config(os.path.join(media, "none.toml"), tempfile.mkdtemp())
        cfg.data["media"].update(dir=media, device="")
        cfg.data["playback"]["shuffle"] = False
        cfg.data["player"].update(CFG)
        cfg.data["snapshot"]["interval"] = 0
        ctl = controller.Controller(cfg)
        self.assertEqual(ctl.player.__class__.__name__, "GstPlayer")
        # GstPlayer writes a live JPEG straight off the decoded frames
        self.assertEqual(ctl._snapshot_method(), "live")
        t = threading.Thread(target=ctl.run, daemon=True)
        t.start()
        try:
            seen = set()
            end = time.time() + 15
            while time.time() < end and len(seen) < 2:
                if ctl.state["status"] == "playing" and ctl.state["title"]:
                    seen.add(ctl.state["title"])
                time.sleep(0.1)
            self.assertEqual(seen, {"a", "b"})
        finally:
            ctl.shutdown()
            t.join(5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
