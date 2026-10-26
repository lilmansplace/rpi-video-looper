#!/usr/bin/env bash
# Try GStreamer's zero-copy path on this Pi: hardware H.264 decode (v4l2h264dec) straight
# onto a display plane (kmssink), the modern equivalent of omxplayer. Tries several
# pipeline variants, measures each, and asks whether the projector looked smooth.
#   sudo tools/gst-test.sh /media/usb/video.mp4 [seconds]
set -uo pipefail
VIDEO="${1:?usage: $0 video.mp4 [seconds]}"
SECS="${2:-25}"
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }

if ! command -v gst-launch-1.0 >/dev/null; then
  echo "==> Installing GStreamer"
  apt-get install -y --no-install-recommends gstreamer1.0-tools gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good gstreamer1.0-plugins-bad >/dev/null
fi

echo "==> Stopping the looper so GStreamer can own the display"
systemctl stop rpi-video-looper.timer rpi-video-looper 2>/dev/null
sleep 2

SRC="filesrc location=\"$VIDEO\" ! qtdemux name=d d.video_0 ! queue max-size-buffers=8 ! h264parse"
SINK="fpsdisplaysink text-overlay=false sync=true video-sink=\"kmssink driver-name=vc4 force-modesetting=false\""
declare -a NAMES PIPES
NAMES+=("plain");              PIPES+=("$SRC ! v4l2h264dec ! $SINK")
NAMES+=("dmabuf-import");      PIPES+=("$SRC ! v4l2h264dec capture-io-mode=dmabuf ! $SINK")
NAMES+=("NV12 caps");          PIPES+=("$SRC ! v4l2h264dec ! video/x-raw,format=NV12 ! $SINK")
NAMES+=("I420 caps");          PIPES+=("$SRC ! v4l2h264dec ! video/x-raw,format=I420 ! $SINK")
NAMES+=("ISP convert");        PIPES+=("$SRC ! v4l2h264dec ! v4l2convert ! video/x-raw,format=RGB16 ! $SINK")
NAMES+=("playbin");            PIPES+=("playbin uri=\"file://$VIDEO\" video-sink=\"kmssink driver-name=vc4\" audio-sink=fakesink")

cpu() { awk '/^cpu /{print $2+$3+$4+$5+$6+$7+$8, $5+$6}' /proc/stat; }
for i in "${!NAMES[@]}"; do
  LOG="$(mktemp)"
  echo; echo "===== ${NAMES[$i]}"
  read -r T0 I0 < <(cpu)
  eval "GST_DEBUG=2 timeout $SECS gst-launch-1.0 -v ${PIPES[$i]}" >"$LOG" 2>&1
  read -r T1 I1 < <(cpu)
  echo "CPU:   $(( 100 - 100 * (I1 - I0) / (T1 - T0) ))%"
  STATS="$(grep -o "rendered: [0-9]*, dropped: [0-9]*, current: [0-9.]*, average: [0-9.]*" "$LOG" | tail -1)"
  echo "fps:   ${STATS:-no frames reported}"
  grep -E "ERROR|WARN|erroneous|not-negotiated|Could not" "$LOG" | sed 's/^/       /' | tail -4
  read -r -p "Picture on the projector, and smooth? (no / yes-choppy / yes-smooth) " ans
  echo "seen:  $ans"
  rm -f "$LOG"
done

echo
echo "Done. The looper stays stopped; start it again with:  sudo systemctl start rpi-video-looper"
