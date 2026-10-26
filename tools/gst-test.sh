#!/usr/bin/env bash
# Try GStreamer's zero-copy path on this Pi: hardware H.264 decode straight onto a
# display plane (v4l2h264dec -> kmssink). Compare its CPU use with tools/bench.py.
# Stop the looper first:  sudo systemctl stop rpi-video-looper
# Usage: sudo tools/gst-test.sh /media/usb/video.mp4 [seconds]
set -euo pipefail
VIDEO="${1:?usage: $0 video.mp4 [seconds]}"
SECS="${2:-20}"

if ! command -v gst-launch-1.0 >/dev/null; then
  echo "==> Installing GStreamer (about 15 MB)"
  apt-get install -y --no-install-recommends gstreamer1.0-tools gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good gstreamer1.0-plugins-bad >/dev/null
fi

cpu() { awk '/^cpu /{print $2+$3+$4+$5+$6+$7+$8, $5+$6}' /proc/stat; }
LOG="$(mktemp)"
read -r T0 I0 < <(cpu)
timeout "$((SECS + 5))" gst-launch-1.0 -v filesrc location="$VIDEO" ! qtdemux ! queue ! h264parse \
  ! v4l2h264dec ! fpsdisplaysink video-sink=kmssink text-overlay=false sync=true >"$LOG" 2>&1 || true
read -r T1 I1 < <(cpu)

echo "CPU: $(( 100 - 100 * (I1 - I0) / (T1 - T0) ))%  (over $((SECS + 5)) s, including startup)"
grep -o "rendered: [0-9]*, dropped: [0-9]*, current: [0-9.]*, average: [0-9.]*" "$LOG" | tail -1 \
  || { echo "No playback stats. Last lines of the log:"; tail -15 "$LOG"; }
rm -f "$LOG"
