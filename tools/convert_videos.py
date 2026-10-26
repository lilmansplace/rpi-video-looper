#!/usr/bin/env python3
"""Make a folder of videos Pi-Zero-friendly. Run this on your PC, not on the Pi.

Checks every video with ffprobe and re-encodes only the ones the Pi Zero can't
play smoothly (HEVC/H.265, VP9, 4K, 60 fps at 1080p, ...) to H.264 1080p30 MP4.
Files that are already fine are copied as-is, so the output folder can go
straight onto the thumb drive.

Needs ffmpeg + ffprobe on your PATH:
  Windows:  winget install ffmpeg        macOS: brew install ffmpeg
  Linux:    sudo apt install ffmpeg

Examples:
  python3 tools/convert_videos.py ~/Downloads/AtmosFX --check    # report only
  python3 tools/convert_videos.py ~/Downloads/AtmosFX            # -> ~/Downloads/AtmosFX/pi-ready
  python3 tools/convert_videos.py D:\\Halloween --out E:\\         # write straight to the USB drive
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from looper import compat  # noqa: E402

VIDEO_EXTS = {".mp4", ".m4v", ".mkv", ".mov", ".avi", ".ts", ".webm", ".wmv", ".mpg", ".mpeg", ".h264", ".hevc"}
COPY_AS_IS_EXTS = {".mp4", ".m4v", ".mkv", ".mov"}  # containers mpv on the Pi handles well


def probe(path: str) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", path],
        capture_output=True, text=True, check=True,
    ).stdout
    data = json.loads(out)
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"
              and not s.get("disposition", {}).get("attached_pic")), None)
    a = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    fps = None
    if v:
        for key in ("avg_frame_rate", "r_frame_rate"):
            num, _, den = (v.get(key) or "0/0").partition("/")
            try:
                if float(den or 1):
                    fps = float(num) / float(den or 1)
                    break
            except ValueError:
                pass
    return {
        "codec": v.get("codec_name") if v else None,
        "width": v.get("width") if v else None,
        "height": v.get("height") if v else None,
        "fps": fps,
        "pix_fmt": v.get("pix_fmt") if v else None,
        "audio": a.get("codec_name") if a else None,
    }


def reasons_for(info: dict, ext: str) -> list[str]:
    r = compat.check(info["codec"], info["width"], info["height"], info["fps"], hwdec=None)
    pf = info.get("pix_fmt") or ""
    if info["codec"] == "h264" and pf and pf not in ("yuv420p", "yuvj420p"):
        r.append(f"{pf} pixel format isn't hardware-decoded (needs 8-bit 4:2:0)")
    if not r and ext not in COPY_AS_IS_EXTS:
        r.append(f"{ext} container (remuxing to .mp4)")
    return r


def build_cmd(src: str, dst: str, info: dict, crf: int, preset: str) -> list[str]:
    w, h = info["width"] or 1920, info["height"] or 1080
    scale = min(1.0, 1920 / w, 1080 / h)
    out_h = int(h * scale)
    vf = ["scale='min(1920,iw)':'min(1080,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2"]
    fps = info["fps"] or 0
    if fps > 31 and out_h > 720:
        vf.append("fps=30")
    elif fps > 61:
        vf.append("fps=60")
    vf.append("format=yuv420p")

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-stats", "-y", "-i", src,
           "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn", "-map_metadata", "-1",
           "-vf", ",".join(vf),
           "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
           "-profile:v", "high", "-level:v", "4.1",
           # regular keyframes keep seeking / skipping snappy on the Pi
           "-g", "60", "-keyint_min", "30"]
    if info["audio"] == "aac":
        cmd += ["-c:a", "copy"]
    else:
        cmd += ["-c:a", "aac", "-b:a", "160k", "-ac", "2"]
    cmd += ["-movflags", "+faststart", dst]
    return cmd


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def main() -> int:
    try:  # Windows consoles may not do UTF-8; never crash over a check mark
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="folder with your videos")
    ap.add_argument("--out", help="output folder (default: <source>/pi-ready)")
    ap.add_argument("--check", action="store_true", help="only report which files need converting")
    ap.add_argument("--no-copy", action="store_true", help="don't copy files that are already fine")
    ap.add_argument("--crf", type=int, default=20, help="quality, lower = better/bigger (default 20)")
    ap.add_argument("--preset", default="medium", help="x264 preset: faster | medium | slow (default medium)")
    args = ap.parse_args()

    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            print(f"{tool} not found. Install ffmpeg first (see the top of this file).")
            return 1

    src_root = os.path.abspath(args.source)
    out_root = os.path.abspath(args.out or os.path.join(src_root, "pi-ready"))
    files = []
    for d, dirs, names in os.walk(src_root):
        if os.path.abspath(d).startswith(out_root):
            dirs[:] = []
            continue
        dirs[:] = [x for x in dirs if not x.startswith(".")]
        files += [os.path.join(d, n) for n in sorted(names)
                  if not n.startswith(".") and os.path.splitext(n)[1].lower() in VIDEO_EXTS]
    if not files:
        print(f"No videos found in {src_root}")
        return 1

    todo, ok, failed = [], [], []
    print(f"Checking {len(files)} videos in {src_root}\n")
    for f in files:
        rel = os.path.relpath(f, src_root)
        try:
            info = probe(f)
        except Exception as e:
            print(f"  ?  {rel}: can't read ({e})")
            failed.append(rel)
            continue
        if not info["codec"]:
            print(f"  ?  {rel}: no video stream")
            continue
        why = reasons_for(info, os.path.splitext(f)[1].lower())
        desc = f"{info['codec']} {info['width']}x{info['height']} {info['fps'] or 0:.0f}fps"
        if why:
            print(f"  ✗  {rel}  [{desc}]")
            for r in why:
                print(f"       - {r}")
            todo.append((f, rel, info))
        else:
            print(f"  ✓  {rel}  [{desc}]")
            ok.append((f, rel))

    print(f"\n{len(ok)} ready, {len(todo)} need converting.")
    if args.check or (not todo and args.no_copy):
        return 0

    os.makedirs(out_root, exist_ok=True)
    if not args.no_copy:
        for f, rel in ok:
            dst = os.path.join(out_root, rel)
            if os.path.exists(dst) and os.path.getsize(dst) == os.path.getsize(f):
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            print(f"copy     {rel}")
            shutil.copy2(f, dst)

    for i, (f, rel, info) in enumerate(todo, 1):
        dst = os.path.join(out_root, os.path.splitext(rel)[0] + ".mp4")
        if os.path.exists(dst) and os.path.getmtime(dst) >= os.path.getmtime(f):
            print(f"skip     {rel} (already converted)")
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        tmp = dst + ".part.mp4"
        print(f"convert  [{i}/{len(todo)}] {rel}")
        t0 = time.time()
        try:
            subprocess.run(build_cmd(f, tmp, info, args.crf, args.preset), check=True)
            os.replace(tmp, dst)
            print(f"         done in {time.time() - t0:.0f}s, {human(os.path.getsize(f))} -> {human(os.path.getsize(dst))}")
        except (subprocess.CalledProcessError, KeyboardInterrupt) as e:
            if os.path.exists(tmp):
                os.unlink(tmp)
            if isinstance(e, KeyboardInterrupt):
                print("\nstopped.")
                return 130
            print(f"         FAILED: {rel}")
            failed.append(rel)

    print(f"\nPi-ready videos are in {out_root}")
    if failed:
        print(f"{len(failed)} failed: " + ", ".join(failed))
    if any(info.get("pix_fmt", "").endswith(("10le", "10be")) for _, _, info in todo):
        print("Note: HDR/10-bit sources may look a little washed out after conversion.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
