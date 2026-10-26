#!/usr/bin/env python3
"""A stand-in for mpv's JSON IPC so the controller can be tested off-device.

Usage mirrors mpv: fake_mpv.py --input-ipc-server=PATH [...ignored args]
Each loaded file "plays" for FAKE_MPV_LEN seconds (default 1), then emits end-file eof.
"""

import json
import os
import socket
import sys
import threading
import time

LEN = float(os.environ.get("FAKE_MPV_LEN", "1"))
path = next(a.split("=", 1)[1] for a in sys.argv if a.startswith("--input-ipc-server="))
props = {"pause": False, "volume": 100, "time-pos": 0.0, "duration": LEN, "path": None}
srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
srv.bind(path)
srv.listen(1)
conn, _ = srv.accept()
lock = threading.Lock()
gen = 0


def send(obj):
    with lock:
        conn.sendall(json.dumps(obj).encode() + b"\n")


def play(g, f):
    send({"event": "file-loaded"})
    start = time.time()
    while time.time() - start < LEN:
        time.sleep(0.05)
        if g != gen:
            return
        if not props["pause"]:
            props["time-pos"] = time.time() - start
    if g == gen:
        send({"event": "end-file", "reason": "eof"})


buf = b""
while True:
    chunk = conn.recv(4096)
    if not chunk:
        break
    buf += chunk
    while b"\n" in buf:
        line, buf = buf.split(b"\n", 1)
        msg = json.loads(line)
        cmd, rid = msg["command"], msg.get("request_id")
        reply = {"error": "success", "request_id": rid}
        name = cmd[0]
        if name == "get_property":
            if cmd[1] in props:
                reply["data"] = props[cmd[1]]
            else:
                reply["error"] = "property unavailable"
        elif name == "set_property":
            props[cmd[1]] = cmd[2]
        elif name == "loadfile":
            gen += 1
            props["path"] = cmd[1]
            props["time-pos"] = 0.0
            threading.Thread(target=play, args=(gen, cmd[1]), daemon=True).start()
        elif name == "stop":
            gen += 1
        elif name == "seek":
            props["time-pos"] = float(cmd[1])
        elif name == "screenshot-to-file":
            try:
                from PIL import Image
                Image.new("RGB", (1920, 1080), (200, 80, 20)).save(cmd[1], "JPEG")
            except ImportError:
                with open(cmd[1], "wb") as f:
                    f.write(b"\xff\xd8fakejpeg\xff\xd9")
        send(reply)
