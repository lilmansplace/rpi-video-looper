"""Find the smoothest mpv output/decoder combination for YOUR videos on this Pi.

Stop the looper first, then run:
    sudo systemctl stop rpi-video-looper
    python3 tools/bench.py /media/usb/some-video.mp4 [seconds]

For each combination it plays the clip, then reports dropped frames, CPU use and
whether hardware decoding was actually used. Put the winner in
/boot/firmware/looper.toml under [player].
"""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from looper.player import MpvPlayer  # noqa: E402

COMBOS = [
    {"vo": "gpu", "gpu_context": "drm", "hwdec": "drm"},
    {"vo": "gpu", "gpu_context": "drm", "hwdec": "v4l2m2m-copy"},
    {"vo": "gpu", "gpu_context": "drm", "hwdec": "auto-safe"},
    {"vo": "drm", "gpu_context": "", "hwdec": "v4l2m2m-copy"},
    {"vo": "drm", "gpu_context": "", "hwdec": "no"},
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
    secs = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    sock = "/tmp/looper-bench.sock"
    results = []
    for c in COMBOS:
        cfg = dict(c, audio_output="alsa", extra_args=[])
        label = f"vo={c['vo']} hwdec={c['hwdec']}"
        p = MpvPlayer(cfg, sock)
        try:
            p.start()
            p.play(video)
            time.sleep(3)  # let it settle
            t0, i0 = cpu_times()
            time.sleep(secs)
            t1, i1 = cpu_times()
            cpu = 100 * (1 - (i1 - i0) / max(1, t1 - t0))
            r = {
                "combo": label,
                "hwdec": p.get("hwdec-current", "?"),
                "dropped": (p.get("frame-drop-count", 0) or 0) + (p.get("decoder-frame-drop-count", 0) or 0),
                "cpu": round(cpu),
            }
        except Exception as e:
            r = {"combo": label, "hwdec": "-", "dropped": "FAILED", "cpu": "-", "err": str(e)}
        finally:
            p.stop()
        results.append(r)
        print(f"{r['combo']:<36} hw={r['hwdec']:<14} dropped={r['dropped']:<7} cpu={r['cpu']}%", flush=True)
        time.sleep(2)

    ok = [r for r in results if isinstance(r["dropped"], int)]
    if ok:
        best = min(ok, key=lambda r: (r["dropped"], r["cpu"]))
        print(f"\nBest: {best['combo']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
