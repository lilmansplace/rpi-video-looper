"""Off-device tests for onboarding: seed files, setup portal, hotspot flow, MQTT discovery, screens."""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import TMP  # noqa: E402,F401

from looper import config as config_mod  # noqa: E402
from looper import mqtt_auto, netsetup, screens  # noqa: E402


class FakeNM:
    def __init__(self, join_ok=True):
        self.dev = "wlan0"
        self.saved: dict[str, str] = {}
        self.join_ok = join_ok
        self.ap = False
        self.ip = None
        self.calls = []

    def mac(self):
        return "b8:27:eb:93:44:e0"

    def online(self):
        return self.ip

    def current_ssid(self):
        return "Home" if self.ip else None

    def saved_wifi(self):
        return list(self.saved)

    def scan(self):
        return [{"ssid": "Home", "signal": 80, "secure": True}, {"ssid": "Guest", "signal": 40, "secure": False}]

    def add_wifi(self, ssid, psk, hidden=False):
        self.calls.append(("add", ssid, psk, hidden))
        name = "looper-wifi-" + ssid.lower()
        self.saved[name] = psk
        return name

    def up(self, name, timeout=45):
        self.calls.append(("up", name))
        if self.join_ok:
            self.ip = "192.168.20.148"
        return self.join_ok

    def try_saved(self, timeout=40):
        return False

    def start_ap(self, ssid, psk):
        self.ap = True
        self.calls.append(("ap", ssid))
        return True

    def stop_ap(self):
        self.ap = False

    def stations(self):
        return 0

    def mac(self):
        return "b8:27:eb:00:44:e0"

    def has_wifi(self):
        return True


class FakeSystem:
    def __init__(self):
        self.calls = []

    def set_country(self, cc):
        self.calls.append(("country", cc))

    def set_hostname(self, name):
        self.calls.append(("hostname", name))
        return name

    def set_ssh(self, enabled, user, password):
        self.calls.append(("ssh", enabled, user, password))

    def restart_looper(self):
        self.calls.append(("restart",))

    def first_boot_done(self):
        self.calls.append(("firstboot",))


def make_svc(boot_text="", join_ok=True):
    d = tempfile.mkdtemp()
    boot = os.path.join(d, "looper.toml")
    with open(boot, "w") as f:
        f.write(boot_text)
    svc = netsetup.NetSetup(nm=FakeNM(join_ok), system=FakeSystem(), state_dir=os.path.join(d, "state"),
                            boot_path=boot)
    return svc, boot


class HelperTests(unittest.TestCase):
    def test_nm_split(self):
        self.assertEqual(netsetup.nm_split(r"My\:Net:80:WPA2"), ["My:Net", "80", "WPA2"])
        self.assertEqual(netsetup.nm_split("a::c"), ["a", "", "c"])

    def test_scrub_only_secret_sections(self):
        text = ('[wifi]\nssid = "Home"\npassword = "hunter22"\n\n[mqtt]\npassword = \'x y\'\n'
                '[media]\npassword = "keep"\n')
        out, changed = netsetup.scrub_secrets(text)
        self.assertTrue(changed)
        self.assertNotIn("hunter22", out)
        self.assertNotIn("x y", out)
        self.assertIn('password = "keep"', out)
        self.assertIn('ssid = "Home"', out)
        again, changed2 = netsetup.scrub_secrets(out)
        self.assertFalse(changed2)
        self.assertEqual(again, out)

    def test_validate_form(self):
        ok, err = netsetup.validate_form({"ssid": "Home", "password": "longenough", "country": "us",
                                          "mqtt_host": "192.168.1.5:1884"})
        self.assertIsNone(err)
        self.assertEqual((ok["country"], ok["mqtt_host"], ok["mqtt_port"]), ("US", "192.168.1.5", "1884"))
        self.assertIsNotNone(netsetup.validate_form({"ssid": ""})[1])
        self.assertIsNotNone(netsetup.validate_form({"ssid": "Home", "password": "short"})[1])
        self.assertIsNotNone(netsetup.validate_form({"ssid": "Home", "country": "USA"})[1])
        self.assertIsNotNone(netsetup.validate_form({"ssid": "Home", "mqtt_host": "bad host;rm"})[1])
        other, _ = netsetup.validate_form({"ssid": "Home", "ssid_other": "Hidden Net"})
        self.assertEqual(other["ssid"], "Hidden Net")
        self.assertTrue(other["hidden"])

    def test_wifi_qr_escaping(self):
        self.assertEqual(screens.wifi_qr_payload('a;b', 'p"w'), 'WIFI:T:WPA;S:a\\;b;P:p\\"w;;')

    def test_render_screens(self):
        d = tempfile.mkdtemp()
        for st in ({"mode": "setup", "ssid": "Looper-Setup-44E0", "psk": "abcdefgh23"},
                   {"mode": "connecting", "network": "Home"},
                   {"mode": "online", "ip": "10.0.0.2", "network": "Home"}):
            p = os.path.join(d, st["mode"] + ".png")
            self.assertTrue(screens.render(st, p), st)
            self.assertGreater(os.path.getsize(p), 1000)
        self.assertFalse(screens.render({"mode": "offline"}, os.path.join(d, "x.png")))


class SeedFileTests(unittest.TestCase):
    def test_boot_wifi_is_saved_once_and_scrubbed(self):
        svc, boot = make_svc('[wifi]\nssid = "Home"\npassword = "hunter2222"\ncountry = "GB"\n'
                             '[device]\nname = "Porch Looper"\n')
        self.assertTrue(svc.apply_seed_files())
        svc.ensure_hostname()
        self.assertIn(("add", "Home", "hunter2222", False), svc.nm.calls)
        self.assertIn(("country", "GB"), svc.sys.calls)
        self.assertIn(("hostname", "Porch Looper"), svc.sys.calls)
        text = open(boot).read()
        self.assertNotIn("hunter2222", text)
        self.assertIn('ssid = "Home"', text)
        svc.nm.calls.clear()
        self.assertFalse(svc.apply_seed_files())  # scrubbed file: not re-added with an empty password
        self.assertEqual([c for c in svc.nm.calls if c[0] == "add"], [])

    def test_default_hostname_has_mac_suffix(self):
        svc, _ = make_svc("")  # no [device] name chosen
        svc.ensure_hostname()
        self.assertIn(("hostname", "looper-44e0"), svc.sys.calls)  # from FakeNM.mac() ...00:44:e0

    def test_chosen_name_wins_over_mac_suffix(self):
        svc, _ = make_svc('[device]\nname = "Garage"\n')
        svc.ensure_hostname()
        self.assertIn(("hostname", "Garage"), svc.sys.calls)

    def test_no_wifi_adapter_stands_down(self):
        # a non-W Pi Zero: no Wi-Fi device -> run offline, never start a hotspot
        svc, _ = make_svc("")
        svc.nm.has_wifi = lambda: False
        svc._run_offline = lambda: setattr(svc, "_offline", True)  # don't idle forever in the test
        svc.run()
        self.assertTrue(getattr(svc, "_offline", False))
        self.assertEqual([c for c in svc.nm.calls if c[0] == "ap"], [])  # no hotspot attempt

    def test_new_password_is_applied(self):
        svc, boot = make_svc('[wifi]\nssid = "Home"\npassword = "hunter2222"\n')
        svc.apply_seed_files()
        with open(boot, "w") as f:
            f.write('[wifi]\nssid = "Home"\npassword = "newpass123"\n')
        self.assertTrue(svc.apply_seed_files())

    def test_ssh_from_file(self):
        svc, _ = make_svc('[ssh]\nenabled = true\npassword = "s3cretpass"\n')
        svc.apply_seed_files()
        self.assertIn(("ssh", True, "looper", "s3cretpass"), svc.sys.calls)

    def test_no_wifi_section(self):
        svc, _ = make_svc('[media]\nrescan_interval = 5\n')
        self.assertFalse(svc.apply_seed_files())


class PortalTests(unittest.TestCase):
    def setUp(self):
        self.svc, _ = make_svc()
        self.assertTrue(self.svc.start_hotspot())
        self.svc.stop_portal()
        self.svc.start_portal(port=0, bind="127.0.0.1")
        self.port = self.svc.portal.server_address[1]

    def tearDown(self):
        self.svc.stop_portal()

    def req(self, path, data=None, host=None):
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                   data=urllib.parse.urlencode(data).encode() if data else None)
        if host:
            r.add_header("Host", host)

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        opener = urllib.request.build_opener(NoRedirect)
        try:
            with opener.open(r, timeout=5) as resp:
                return resp.status, resp.headers, resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read().decode()

    def test_state_published_for_projector(self):
        st = json.load(open(netsetup.STATE_FILE))
        self.assertEqual(st["mode"], "setup")
        self.assertEqual(st["ssid"], "Looper-Setup-44E0")
        self.assertGreaterEqual(len(st["psk"]), 8)

    def test_captive_redirect(self):
        code, headers, _ = self.req("/generate_204", host="connectivitycheck.gstatic.com")
        self.assertEqual(code, 302)
        self.assertEqual(headers["Location"], "http://10.42.0.1/")

    def test_form_lists_networks_and_hides_psk(self):
        code, _, body = self.req("/", host="10.42.0.1")
        self.assertEqual(code, 200)
        self.assertIn("Home", body)
        self.assertIn("Guest", body)
        code, _, body = self.req("/status", host="10.42.0.1")
        self.assertNotIn("psk", json.loads(body))

    def test_bad_form_shows_error(self):
        code, _, body = self.req("/save", {"ssid": "Home", "password": "short"}, host="10.42.0.1")
        self.assertIn("8 to 63", body)
        self.assertTrue(self.svc.actions.empty())

    def test_good_form_queues_and_connects(self):
        self.req("/save", {"ssid": "Home", "password": "hunter2222", "country": "US", "name": "Garage",
                           "mqtt_user": "ha", "mqtt_password": "pw"}, host="10.42.0.1")
        form = self.svc.actions.get(timeout=2)
        self.svc.handle_submit(form)
        self.assertEqual(self.svc.state["mode"], "online")
        self.assertTrue(self.svc.state["announce"])
        self.assertIn(("restart",), self.svc.sys.calls)
        setup = json.load(open(os.path.join(self.svc.state_dir, "setup.json")))
        self.assertEqual(setup["device"]["name"], "Garage")
        self.assertEqual(setup["mqtt"]["username"], "ha")
        self.assertNotIn("enabled", setup["mqtt"])  # no server given: stays automatic

    def test_failed_join_returns_to_hotspot_with_error(self):
        self.svc.nm.join_ok = False
        form, _ = netsetup.validate_form({"ssid": "Home", "password": "wrongpass1"})
        self.svc.handle_submit(form)
        self.assertEqual(self.svc.state["mode"], "setup")
        self.assertIn("Couldn't join", self.svc.state["error"])
        self.assertTrue(self.svc.nm.ap)


class MqttAutoTests(unittest.TestCase):
    class Ctl:
        def __init__(self):
            self.state = {}
            self.listeners = []

        def _set(self, kind="state", **kw):
            self.state.update(kw)

    def mgr(self, mqtt_conf, results, discovered=()):
        cfg = config_mod.Config(os.path.join(tempfile.mkdtemp(), "none.toml"), tempfile.mkdtemp())
        cfg.data["mqtt"].update(mqtt_conf)
        tried = []

        def prober(host, port, user="", pw=""):
            tried.append((host, port, user))
            return results.get(host, "unreachable")

        class FakeClient:
            def __init__(self, conf, ctl):
                self.conf = conf

            def start(self):
                pass

            def stop(self):
                pass

        m = mqtt_auto.MqttManager(cfg, self.Ctl(), discover=lambda: list(discovered), prober=prober,
                                  client_factory=FakeClient)
        return m, tried

    def test_auto_finds_home_assistant_via_mdns(self):
        m, tried = self.mgr({"enabled": "auto"}, {"192.168.20.10": "ok"}, [("192.168.20.10", 1883)])
        self.assertTrue(m.search_once())
        self.assertEqual(m.ctl.state["mqtt_state"], "connected")
        self.assertEqual(m.client.conf["host"], "192.168.20.10")

    def test_auto_reports_login_needed(self):
        m, tried = self.mgr({"enabled": "auto"}, {"homeassistant.local": "auth"})
        self.assertFalse(m.search_once())
        self.assertEqual(m.ctl.state["mqtt_state"], "needs_login")
        self.assertIn("homeassistant.local", m.ctl.state["mqtt"])

    def test_explicit_host_only_when_enabled_true(self):
        m, tried = self.mgr({"enabled": True, "host": "10.0.0.9"}, {}, [("192.168.20.10", 1883)])
        m.search_once()
        self.assertEqual([t[0] for t in tried], ["10.0.0.9"])
        self.assertEqual(m.ctl.state["mqtt_state"], "not_found")

    def test_off(self):
        m, tried = self.mgr({"enabled": False}, {})
        self.assertTrue(m.search_once())
        self.assertEqual(tried, [])
        self.assertEqual(m.ctl.state["mqtt_state"], "off")

    def test_enabled_parsing(self):
        self.assertEqual(mqtt_auto.enabled_mode("auto"), "auto")
        self.assertEqual(mqtt_auto.enabled_mode(True), "on")
        self.assertEqual(mqtt_auto.enabled_mode("false"), "off")


class ConfigLayerTests(unittest.TestCase):
    def test_setup_json_and_usb_file(self):
        d = tempfile.mkdtemp()
        media = os.path.join(d, "usb")
        os.makedirs(media)
        boot = os.path.join(d, "looper.toml")
        with open(boot, "w") as f:
            f.write(f'[media]\ndir = "{media}"\ndevice = ""\n[device]\nname = "Boot Name"\n')
        config_mod.save_setup(d, "mqtt", {"username": "ha", "password": "pw"})
        config_mod.save_setup(d, "device", {"name": "Portal Name"})
        c = config_mod.Config(boot, d)
        self.assertEqual(c.section("mqtt")["username"], "ha")
        self.assertEqual(c.section("mqtt")["name"], "Portal Name")
        self.assertEqual(c.section("mqtt")["node_id"], "portal_name")
        with open(os.path.join(media, "looper.toml"), "w") as f:
            f.write('[device]\nname = "USB Name"\n[playback]\nvolume = 30\n')
        c = config_mod.Config(boot, d)
        self.assertEqual(c.section("mqtt")["name"], "USB Name")
        self.assertEqual(c.get("volume"), 30)


class ControllerOverlayTests(unittest.TestCase):
    """The controller shows the setup screen while the setup service says so."""

    def test_setup_screen_then_back_to_video(self):
        from looper import controller, web

        media = os.path.join(TMP, "media3")
        os.makedirs(media, exist_ok=True)
        open(os.path.join(media, "clip.mp4"), "w").close()
        cfg = config_mod.Config()
        cfg.data["player"]["backend"] = "mpv"  # fake mpv in tests
        cfg.data["media"].update(dir=media, device="")
        cfg.data["snapshot"]["interval"] = 0
        fake = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_mpv.py")
        if not hasattr(controller.MpvPlayer, "_orig_args"):
            controller.MpvPlayer._orig_args = controller.MpvPlayer.args
        orig = controller.MpvPlayer._orig_args
        controller.MpvPlayer.args = lambda self: [sys.executable, fake] + orig(self)[1:]
        os.environ["FAKE_MPV_LEN"] = "30"
        ctl = controller.Controller(cfg)
        t = threading.Thread(target=ctl.run, daemon=True)
        t.start()
        try:
            self._wait(lambda: ctl.state["status"] == "playing")
            os.makedirs(os.path.dirname(controller.NET_STATE), exist_ok=True)
            with open(controller.NET_STATE, "w") as f:
                json.dump({"mode": "setup", "ssid": "Looper-Setup-44E0", "psk": "abcdefgh23", "t": 1}, f)
            self._wait(lambda: ctl.state["status"] == "setup")
            self.assertTrue(os.path.exists(os.path.join(os.environ["LOOPER_RUN_DIR"], "screen.png")))
            ctl.submit("next")  # controls are ignored during setup
            time.sleep(0.5)
            self.assertEqual(ctl.state["status"], "setup")
            with open(controller.NET_STATE, "w") as f:
                json.dump({"mode": "online", "ip": "10.0.0.2", "t": 2}, f)
            os.utime(controller.NET_STATE, (time.time() + 5, time.time() + 5))
            self._wait(lambda: ctl.state["status"] == "playing")
        finally:
            ctl.shutdown()
            t.join(5)
            os.unlink(controller.NET_STATE)

    def _wait(self, pred, timeout=8):
        end = time.time() + timeout
        while time.time() < end:
            if pred():
                return
            time.sleep(0.1)
        self.fail("timed out")


if __name__ == "__main__":
    unittest.main(verbosity=2)
