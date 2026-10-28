"""Animated boot splash painted straight onto the firmware framebuffer (/dev/fb0).

On a Pi the firmware sets up a full-resolution framebuffer within a couple of seconds of
power-on -- long before the vc4 DRM driver (and therefore the video) is ready. We draw the
looper's splash there, with a progress bar that slides so it reads as "working", and keep
redrawing it so nothing clears it. When vc4 takes the display over (the framebuffer stops
being the 32-bpp firmware one) we stop, and the player shows video.

Resolution-independent, and a safe no-op if there is no 32-bpp framebuffer to draw on.
"""

from __future__ import annotations

import logging
import os
import time

log = logging.getLogger("looper.fbsplash")

try:
    from PIL import Image, ImageDraw
    from . import screens as S
except Exception:  # pragma: no cover - Pillow missing: just skip the splash
    Image = None

FB = os.environ.get("LOOPER_FB", "/dev/fb0")
SYS = "/sys/class/graphics/fb0"


def _geometry():
    with open(f"{SYS}/virtual_size") as f:
        w, h = (int(x) for x in f.read().strip().split(","))
    with open(f"{SYS}/bits_per_pixel") as f:
        bpp = int(f.read().strip())
    try:
        with open(f"{SYS}/stride") as f:
            stride = int(f.read().strip())
    except OSError:
        stride = w * (bpp // 8)
    return w, h, bpp, stride


def _wait_for_fb(deadline: float):
    """Wait until a 32-bpp firmware framebuffer exists. Returns its geometry or None."""
    while time.monotonic() < deadline:
        try:
            w, h, bpp, stride = _geometry()
            if bpp == 32:
                return w, h, bpp, stride
        except OSError:
            pass
        time.sleep(0.3)
    return None


def _background(w: int, h: int):
    """Full splash with an empty track; returns (image, track rect) for the bar to slide in."""
    img = Image.new("RGBA", (w, h), S.BG + (255,))
    d = ImageDraw.Draw(img)
    cx, cy = w // 2, h // 2
    tf = S._font(max(48, h // 9), True)
    d.text((cx - d.textlength("Video Looper", font=tf) / 2, cy - h // 7),
           "Video Looper", fill=S.FG + (255,), font=tf)
    sf = S._font(max(22, h // 22))
    d.text((cx - d.textlength("starting up", font=sf) / 2, cy - h // 40),
           "starting up", fill=S.MUTE + (255,), font=sf)
    bw, bh = w // 3, max(6, h // 110)
    bx, by = cx - bw // 2, cy + h // 12
    d.rounded_rectangle([bx, by, bx + bw, by + bh], radius=bh // 2, fill=(32, 36, 42, 255))
    return img, (bx, by, bw, bh)


def main() -> int:
    if Image is None:
        return 0
    geo = _wait_for_fb(time.monotonic() + 10)
    if not geo:
        log.info("no 32-bpp framebuffer; not drawing a splash")
        return 0
    w, h, _, stride = geo
    bg, (bx, by, bw, bh) = _background(w, h)
    try:
        fb = open(FB, "r+b", buffering=0)
    except OSError as e:
        log.warning("could not open %s: %s", FB, e)
        return 0
    line = w * 4
    # the strip of rows the moving bar lives in; everything else is drawn once
    seg = bw // 3
    with fb:
        try:
            full = bg.tobytes("raw", "BGRA")
            if stride == line:
                fb.seek(0); fb.write(full)
            else:
                for y in range(h):
                    fb.seek(y * stride); fb.write(full[y * line:(y + 1) * line])
        except OSError:
            return 0
        strip = bg.crop((0, by, w, by + bh))          # the track row(s), to composite on
        progress, direction = 0.0, 1
        while True:
            if os.path.exists("/dev/dri/card0"):       # vc4 is up: release the display at once
                return 0
            try:
                if _geometry()[2] != 32:               # framebuffer handed over: let the video show
                    return 0
            except OSError:
                return 0
            frame = strip.copy()
            x = int(progress * (bw - seg))
            ImageDraw.Draw(frame).rounded_rectangle(
                [bx + x, 0, bx + x + seg, bh - 1], radius=bh // 2, fill=S.ACCENT + (255,))
            raw = frame.tobytes("raw", "BGRA")
            try:
                for i in range(bh):
                    fb.seek((by + i) * stride); fb.write(raw[i * line:(i + 1) * line])
            except OSError:
                return 0
            progress += direction * 0.04
            if progress >= 1: progress, direction = 1.0, -1
            elif progress <= 0: progress, direction = 0.0, 1
            time.sleep(0.05)


if __name__ == "__main__":
    raise SystemExit(main())
