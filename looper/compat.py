"""What plays smoothly on a Raspberry Pi Zero / Zero W.

Shared by the looper (which checks each file as it plays, using properties mpv
already knows) and tools/convert_videos.py (which checks with ffprobe on a PC).
No dependencies, so the PC tool can import it from a plain clone.
"""

from __future__ import annotations

# The Zero's VideoCore IV decodes H.264 in hardware, up to 1080p30.
HW_CODECS = {"h264"}
MAX_WIDTH = 1920
MAX_HEIGHT = 1088  # 1080p is often coded as 1088 lines
MAX_PIXELS = 1920 * 1088
MAX_FPS_1080 = 31.0  # 30 fps (+ rounding) at full HD
MAX_FPS_720 = 61.0  # 720p60 is usually fine

CODEC_NAMES = {
    "hevc": "H.265/HEVC",
    "h265": "H.265/HEVC",
    "vp9": "VP9",
    "vp8": "VP8",
    "av1": "AV1",
    "mpeg4": "MPEG-4 Part 2",
    "mpeg2video": "MPEG-2",
    "prores": "ProRes",
    "wmv3": "WMV",
}


def normalize_codec(name: str | None) -> str:
    """'h264 (H.264 / AVC / ...)' or 'H264' -> 'h264'."""
    if not name:
        return ""
    c = str(name).strip().split()[0].lower()
    return {"avc": "h264", "avc1": "h264", "x264": "h264", "h265": "hevc", "hvc1": "hevc"}.get(c, c)


def check(codec=None, width=None, height=None, fps=None, hwdec=None, hwdec_wanted=True) -> list[str]:
    """Return human-readable reasons a video won't play smoothly. Empty = fine.

    Any value may be None (unknown); unknowns are not counted against the file.
    """
    reasons: list[str] = []
    c = normalize_codec(codec)
    if c and c not in HW_CODECS:
        reasons.append(f"{CODEC_NAMES.get(c, c)} isn't hardware-decoded on a Pi Zero (use H.264)")
    try:
        w, h = int(width or 0), int(height or 0)
    except (TypeError, ValueError):
        w = h = 0
    if w and h and (w > MAX_WIDTH or h > MAX_HEIGHT or w * h > MAX_PIXELS):
        label = "4K" if w >= 3000 or h >= 2000 else f"{w}x{h}"
        reasons.append(f"{label} is above the Pi Zero's 1080p limit")
    try:
        f = float(fps or 0)
    except (TypeError, ValueError):
        f = 0.0
    if f and w and h:
        if h > 720 and f > MAX_FPS_1080:
            reasons.append(f"{f:.0f} fps at {h}p is too fast (max 30 fps at 1080p)")
        elif f > MAX_FPS_720:
            reasons.append(f"{f:.0f} fps is too fast")
    # Only blame the decoder if the codec itself is fine: that points at settings.
    if not reasons and hwdec_wanted and hwdec in ("no", ""):
        reasons.append("hardware decoding isn't active (check [player] hwdec, or run tools/bench.py)")
    return reasons
