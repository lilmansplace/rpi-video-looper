"""Tiny stdlib-only web UI + JSON API (no Flask: saves ~15 MB RAM and seconds of boot).

GET  /                 control page
GET  /api/state        current state (JSON)
GET  /api/library      list of video titles
POST /api/cmd          {"cmd": "next" | "previous" | "restart" | "toggle_pause" | "pause" | "play"
                               | "stop" | "rescan" | "jump" | "seek", "arg": ...}
POST /api/settings     {"volume": 80, "shuffle": false, ...}
GET  /snapshot.jpg     latest snapshot
"""

from __future__ import annotations

import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import TUNABLES

log = logging.getLogger(__name__)

STATIC = os.path.join(os.path.dirname(__file__), "static")
ALLOWED_CMDS = {"next", "previous", "restart", "toggle_pause", "pause", "play", "stop", "rescan", "jump", "seek"}


def make_handler(ctl):
    class Handler(BaseHTTPRequestHandler):
        server_version = "looper"

        def log_message(self, fmt, *args):  # keep the journal quiet
            log.debug("%s %s", self.address_string(), fmt % args)

        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200):
            self._send(code, json.dumps(obj).encode(), "application/json")

        def _body(self) -> dict:
            n = min(int(self.headers.get("Content-Length") or 0), 65536)
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(n))
            except ValueError:
                return {}

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                with open(os.path.join(STATIC, "index.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            elif path == "/api/state":
                self._json(ctl.snapshot_state())
            elif path == "/api/library":
                self._json(ctl.library())
            elif path == "/snapshot.jpg":
                try:
                    with open(ctl.snapshot_path, "rb") as f:
                        self._send(200, f.read(), "image/jpeg")
                except FileNotFoundError:
                    self._send(404, b"no snapshot yet", "text/plain")
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            data = self._body()
            if path == "/api/cmd":
                cmd = data.get("cmd")
                if cmd not in ALLOWED_CMDS:
                    return self._json({"error": f"unknown cmd {cmd!r}"}, 400)
                ctl.submit(cmd, data.get("arg"))
                return self._json({"ok": True})
            if path == "/api/settings":
                bad = [k for k in data if k not in TUNABLES]
                if bad:
                    return self._json({"error": f"unknown settings {bad}"}, 400)
                for k, v in data.items():
                    ctl.submit("set", (k, v))
                return self._json({"ok": True})
            self._send(404, b"not found", "text/plain")

    return Handler


def start(cfg: dict, ctl) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((cfg["bind"], int(cfg["port"])), make_handler(ctl))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, name="web", daemon=True).start()
    log.info("web UI on http://%s:%s/", cfg["bind"], cfg["port"])
    return srv
