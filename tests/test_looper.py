"""Off-device tests: playlist logic, settings, and the controller driven by a fake mpv."""

import json
import os
import random
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp()
os.environ["LOOPER_RUN_DIR"] = os.path.join(TMP, "run")
os.environ["LOOPER_STATE_DIR"] = os.path.join(TMP, "state")
os.environ["LOOPER_CONFIG"] = os.path.join(TMP, "looper.toml")

from looper import config as config_mod  # noqa: E402
from looper.playlist import Playlist  # noqa: E402

FILES = [f"/m/{c}.mp4" for c in "abcde"]


class PlaylistTests(unittest.TestCase):
    def test_shuffle_plays_every_file_once_per_round(self):
        pl = Playlist(shuffle=True, rng=random.Random(1))
        pl.set_files(FILES)
        seen = [pl.next(auto=True) for _ in range(5)]
        self.assertEqual(sorted(seen), FILES)

    def test_no_back_to_back_repeat_across_rounds(self):
        for seed in range(50):
            pl = Playlist(shuffle=True, rng=random.Random(seed))
            pl.set_files(FILES)
            seq = [pl.next(auto=True) for _ in range(40)]
            for a, b in zip(seq, seq[1:]):
                self.assertNotEqual(a, b, f"seed {seed}")

    def test_repeat_one_and_off(self):
        pl = Playlist(shuffle=False, repeat="one")
        pl.set_files(FILES)
        first = pl.next()
        self.assertEqual(pl.next(auto=True), first)
        self.assertNotEqual(pl.next(auto=False), first)  # manual skip still skips
        pl = Playlist(shuffle=False, repeat="off")
        pl.set_files(FILES)
        seq = [pl.next(auto=True) for _ in range(6)]
        self.assertEqual(seq, FILES + [None])

    def test_previous_and_jump(self):
        pl = Playlist(shuffle=False)
        pl.set_files(FILES)
        pl.next(); pl.next(); pl.next()
        self.assertEqual(pl.previous(), FILES[1])
        self.assertEqual(pl.jump(4), FILES[4])
        self.assertEqual(pl.jump("a"), FILES[0])
        self.assertIsNone(pl.jump("nope"))

    def test_file_removed_keeps_current(self):
        pl = Playlist(shuffle=False)
        pl.set_files(FILES)
        pl.jump(2)
        pl.set_files(FILES[:2] + FILES[2:4])
        self.assertEqual(pl.current, FILES[2])


class ConfigTests(unittest.TestCase):
    def test_coerce(self):
        self.assertEqual(config_mod.coerce("volume", "150"), 100)
        self.assertIs(config_mod.coerce("shuffle", "off"), False)
        with self.assertRaises(ValueError):
            config_mod.coerce("repeat", "sometimes")
        with self.assertRaises(ValueError):
            config_mod.coerce("hack", 1)

    def test_persisted_settings_override_boot_file(self):
        d = tempfile.mkdtemp()
        boot = os.path.join(d, "looper.toml")
        with open(boot, "w") as f:
            f.write('[playback]\nvolume = 40\nrepeat = "one"\n')
        c = config_mod.Config(boot, d)
        self.assertEqual(c.get("volume"), 40)
        c.set("volume", 70)
        c2 = config_mod.Config(boot, d)
        self.assertEqual(c2.get("volume"), 70)
        self.assertEqual(c2.get("repeat"), "one")

    def test_broken_toml_is_ignored(self):
        d = tempfile.mkdtemp()
        boot = os.path.join(d, "looper.toml")
        with open(boot, "w") as f:
            f.write("this is [ not toml")
        c = config_mod.Config(boot, d)
        self.assertEqual(c.get("volume"), 100)


class ControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from looper import controller, web

        cls.media = os.path.join(TMP, "media")
        os.makedirs(cls.media)
        for n in ("one", "two", "three"):
            open(os.path.join(cls.media, f"{n}.mp4"), "w").close()
        open(os.path.join(cls.media, "notes.txt"), "w").close()
        open(os.path.join(cls.media, "._one.mp4"), "w").close()  # mac junk

        cfg = config_mod.Config()
        cfg.data["media"]["dir"] = cls.media
        cfg.data["media"]["device"] = ""
        cfg.data["playback"]["shuffle"] = False
        cfg.data["snapshot"]["interval"] = 1
        cfg.data["status"]["position_interval"] = 2
        fake = os.path.join(ROOT, "tests", "fake_mpv.py")
        orig_args = controller.MpvPlayer.args
        controller.MpvPlayer.args = lambda self: [sys.executable, fake] + orig_args(self)[1:]
        os.environ["FAKE_MPV_LEN"] = "1.5"
        cls.ctl = controller.Controller(cfg)
        cls.events = []
        cls.ctl.listeners.append(lambda st, kind: cls.events.append((kind, st["title"])))
        cls.srv = web.start({"bind": "127.0.0.1", "port": 0}, cls.ctl)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.ctl.run, daemon=True)
        cls.thread.start()
        time.sleep(1.0)

    @classmethod
    def tearDownClass(cls):
        cls.ctl.shutdown()
        cls.thread.join(5)
        cls.srv.shutdown()

    def api(self, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            raw = r.read()
            return json.loads(raw) if r.headers.get_content_type() == "application/json" else raw

    def wait_for(self, pred, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            s = self.api("/api/state")
            if pred(s):
                return s
            time.sleep(0.1)
        self.fail(f"condition not met; state={s}")

    def test_1_library_and_autoplay(self):
        self.assertEqual(self.api("/api/library"), ["one", "three", "two"])
        s = self.wait_for(lambda s: s["status"] == "playing")
        self.assertEqual(s["count"], 3)

    def test_2_auto_advance(self):
        first = self.api("/api/state")["title"]
        self.wait_for(lambda s: s["title"] != first, timeout=4)

    def test_3_controls(self):
        self.api("/api/cmd", {"cmd": "jump", "arg": 2})
        self.wait_for(lambda s: s["title"] == "two")
        self.api("/api/settings", {"repeat": "one"})
        self.wait_for(lambda s: s["repeat"] == "one")
        time.sleep(2)  # file ends; repeat-one keeps it
        self.assertEqual(self.api("/api/state")["title"], "two")
        self.api("/api/cmd", {"cmd": "next"})
        self.wait_for(lambda s: s["title"] == "one")  # wraps around
        self.api("/api/cmd", {"cmd": "toggle_pause"})
        self.wait_for(lambda s: s["status"] == "paused")
        self.api("/api/cmd", {"cmd": "toggle_pause"})
        self.wait_for(lambda s: s["status"] == "playing")
        self.api("/api/settings", {"repeat": "all", "volume": 55})
        self.wait_for(lambda s: s["volume"] == 55)

    def test_4_snapshot(self):
        self.wait_for(lambda s: s["snapshot_time"] is not None)
        self.assertTrue(self.api("/snapshot.jpg").startswith(b"\xff\xd8"))

    def test_5_usb_pulled_and_replugged(self):
        moved = os.path.join(TMP, "moved")
        os.rename(self.media, moved)
        self.api("/api/cmd", {"cmd": "rescan"})
        self.wait_for(lambda s: s["status"] == "no_media" and s["count"] == 0)
        os.rename(moved, self.media)
        self.api("/api/cmd", {"cmd": "rescan"})
        self.wait_for(lambda s: s["status"] == "playing" and s["count"] == 3)

    def test_6_bad_requests(self):
        with self.assertRaises(urllib.error.HTTPError):
            self.api("/api/cmd", {"cmd": "rm -rf"})
        with self.assertRaises(urllib.error.HTTPError):
            self.api("/api/settings", {"hwdec": "no"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
