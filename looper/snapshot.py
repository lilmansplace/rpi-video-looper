"""Low-cost snapshots of what is playing.

A live video stream would need a second H.264 encode on a single-core CPU that
is already busy feeding the decoder, so we take periodic stills instead.

Path: mpv writes the current frame to a JPEG in /run (tmpfs, no SD wear), then
Pillow's JPEG "draft" mode decodes it at 1/2, 1/4 or 1/8 scale inside the DCT,
which is many times cheaper than a full decode + resize.
"""

from __future__ import annotations

import logging
import os
import subprocess

log = logging.getLogger(__name__)

try:
    from PIL import Image
except ImportError:  # snapshots still work, just full size
    Image = None


def shrink(src: str, dst: str, width: int, quality: int) -> bool:
    if Image is None:
        os.replace(src, dst)
        return True
    try:
        with Image.open(src) as im:
            w, h = im.size
            target = (width, max(1, int(h * width / w)))
            im.draft("RGB", target)  # cheap DCT-domain downscale
            im = im.convert("RGB")
            if im.size[0] > width:
                im.thumbnail(target, Image.Resampling.BILINEAR)
            tmp = dst + ".tmp"
            im.save(tmp, "JPEG", quality=quality, optimize=False)
            os.replace(tmp, dst)
        os.unlink(src)
        return True
    except Exception as e:
        log.warning("snapshot resize failed: %s", e)
        return False


def ffmpeg_grab(video: str, position: float, dst: str, width: int) -> bool:
    """Fallback: decode one frame with ffmpeg at the lowest priority."""
    tmp = dst + ".ff.jpg"
    cmd = [
        "nice", "-n", "19", "ionice", "-c", "3",
        "ffmpeg", "-nostdin", "-loglevel", "error", "-y",
        "-ss", f"{max(0.0, position):.1f}", "-i", video,
        "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "6", tmp,
    ]
    try:
        subprocess.run(cmd, timeout=20, check=True)
        os.replace(tmp, dst)
        return True
    except Exception as e:
        log.warning("ffmpeg snapshot failed: %s", e)
        return False
