"""Measure how smoothly the RUNNING looper is playing (frames shown vs dropped/late).

    sudo -u looper python3 tools/playcheck.py [seconds]

Unlike bench.py (which only proves decoding keeps up), this asks the live player how
many frames actually reached the screen on time.
"""

import json
import os
import socket
import sys
import time

SOCK = os.environ.get("LOOPER_MPV_SOCK", "/run/looper/mpv.sock")
PROPS = ["frame-drop-count", "decoder-frame-drop-count", "vo-delayed-frame-count",
         "mistimed-frame-count", "time-pos", "container-fps", "hwdec-current", "width", "height",
         "display-width", "display-height", "estimated-display-fps"]


def query(props):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(5)
    s.connect(SOCK)
    out, buf = {}, b""
    for i, p in enumerate(props):
        s.sendall(json.dumps({"command": ["get_property", p], "request_id": i}).encode() + b"\n")
    while len(out) < len(props):
        buf += s.recv(65536)
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            m = json.loads(line)
            if "request_id" in m:
                out[props[m["request_id"]]] = m.get("data")
    s.close()
    return out


def main():
    secs = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    a, t0 = query(PROPS), time.monotonic()
    l0 = open("/proc/loadavg").read().split()[0]
    time.sleep(secs)
    b, t1 = query(PROPS), time.monotonic()
    wall = t1 - t0
    fps = b.get("container-fps") or 30
    adv = (b.get("time-pos") or 0) - (a.get("time-pos") or 0)

    def d(k):
        return (b.get(k) or 0) - (a.get(k) or 0)

    expected = adv * fps if adv > 0 else wall * fps
    bad = d("frame-drop-count") + d("decoder-frame-drop-count") + d("vo-delayed-frame-count")
    shown = max(0.0, expected - d("frame-drop-count") - d("decoder-frame-drop-count"))
    print(f"video    {b.get('width')}x{b.get('height')} @ {fps:.2f} fps, decoder {b.get('hwdec-current')}")
    print(f"display  {b.get('display-width')}x{b.get('display-height')} @ {b.get('estimated-display-fps') or 0:.1f} Hz")
    print(f"speed    {adv / wall:.2f}x real time")
    print(f"frames   ~{shown / wall:.1f} fps shown | dropped {d('frame-drop-count')} (vo) "
          f"+ {d('decoder-frame-drop-count')} (decoder) | late {d('vo-delayed-frame-count')} "
          f"| mistimed {d('mistimed-frame-count')}  over {wall:.0f}s")
    print(f"load     {l0} -> {open('/proc/loadavg').read().split()[0]}")
    verdict = "SMOOTH" if bad <= wall * 0.5 and adv / wall > 0.97 else "STUTTERING"
    print(f"verdict  {verdict}")


if __name__ == "__main__":
    main()
