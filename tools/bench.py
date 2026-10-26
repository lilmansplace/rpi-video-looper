"""Find the smoothest mpv output/decoder combination for YOUR videos on this Pi.

Stop the looper first, then run:
    sudo systemctl stop rpi-video-looper
    sudo -u looper python3 tools/bench.py /media/usb/some-video.mp4 [seconds]

For each combination it plays the clip without audio (so a pipeline that can't keep up
visibly plays slower than real time) and reports:
  hw     decoder actually used ("no" = software)
  speed  playback speed vs real time: 1.00 = smooth, 0.80 = 20% too slow
  drop   frames dropped by the output
  cpu    total CPU use while playing
"""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from looper.player import MpvPlayer  # noqa: E402

FAST = ["--profile=fast"]  # bilinear scaling, no dithering/debanding: much less GPU work
HD720 = ["--drm-mode=1280x720"]  # drive HDMI at 720p: 2.25x fewer pixels to draw

COMBOS = [
    ("gpu, hw copy", {"vo": "gpu", "gpu_context": "drm", "hwdec": "v4l2m2m-copy"}, []),
    ("gpu, hw copy, fast", {"vo": "gpu", "gpu_context": "drm", "hwdec": "v4l2m2m-copy"}, FAST),
    ("gpu, hw copy, fast, 720p", {"vo": "gpu", "gpu_context": "drm", "hwdec": "v4l2m2m-copy"}, FAST + HD720),
    ("drm, hw copy, 720p", {"vo": "drm", "gpu_context": "", "hwdec": "v4l2m2m-copy"}, HD720),
    ("gpu, hw zero-copy, fast, 720p", {"vo": "gpu", "gpu_context": "drm", "hwdec": "v4l2m2m"}, FAST + HD720),
    ("gpu, software, fast, 720p", {"vo": "gpu", "gpu_context": "drm", "hwdec": "no"}, FAST + HD720),
]


def cpu_times():
    with open("/proc/stat") as f:
        v = list(map(int, f.readline().split()[1:]))
    return sum(v), v[3] + v[4]


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    video = sys.argv[1]
    secs = int(sys.argv[2]) if len(sys.argv) > 2 else 20
    sock = "/tmp/looper-bench.sock"
    results = []
    print(f"{'setup':<32} {'hw':<14} {'speed':>5} {'drop':>5} {'cpu':>5}")
    for label, c, extra in COMBOS:
        cfg = dict(c, audio_output="null", extra_args=["--aid=no", "--loop-file=inf"] + extra)
        p = MpvPlayer(cfg, sock)
        try:
            p.start()
            p.play(video)
            time.sleep(4)  # let it settle
            pos0, t0w = p.get("time-pos", 0) or 0, time.monotonic()
            d0 = p.get("frame-drop-count", 0) or 0
            c0, i0 = cpu_times()
            time.sleep(secs)
            c1, i1 = cpu_times()
            pos1, t1w = p.get("time-pos", 0) or 0, time.monotonic()
            dur = p.get("duration", 0) or 0
            advanced = pos1 - pos0 if pos1 >= pos0 else pos1 + dur - pos0  # handle loop wrap
            r = {
                "label": label,
                "hw": p.get("hwdec-current", "?") or "no",
                "speed": round(advanced / (t1w - t0w), 2),
                "drop": (p.get("frame-drop-count", 0) or 0) - d0,
                "cpu": round(100 * (1 - (i1 - i0) / max(1, c1 - c0))),
                "extra": extra, "cfg": c,
            }
        except Exception as e:
            r = {"label": label, "hw": "-", "speed": 0, "drop": "FAIL", "cpu": "-", "err": str(e)}
        finally:
            p.stop()
        results.append(r)
        print(f"{r['label']:<32} {r['hw']:<14} {r['speed']:>5} {r['drop']:>5} {r['cpu']:>4}%", flush=True)
        time.sleep(2)

    ok = [r for r in results if isinstance(r["drop"], int) and r["speed"] >= 0.97]
    if ok:
        best = min(ok, key=lambda r: (r["hw"] == "no", r["cpu"], r["drop"]))
        print(f"\nBest: {best['label']}")
        print(f"  [player] vo = \"{best['cfg']['vo']}\", hwdec = \"{best['cfg']['hwdec']}\","
              f" extra_args = {best['extra']}")
    else:
        print("\nNothing kept up with real time. Try tools/gst-test.sh, or 720p videos.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
