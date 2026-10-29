#!/usr/bin/env bash
# rpi-video-looper installer. Run on Raspberry Pi OS Lite (32-bit, Trixie):
#   curl -fsSL https://raw.githubusercontent.com/lilmansplace/rpi-video-looper/main/install.sh | sudo bash
# or, from a clone:  sudo bash install.sh
# Safe to run again to update:  cd rpi-video-looper && git pull && sudo bash install.sh
#
# LOOPER_IMAGE_BUILD=1 is set by tools/build-image.sh when baking a ready-to-flash image
# inside a chroot (no running systemd, nothing to start or stop).
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/lilmansplace/rpi-video-looper.git}"
BRANCH="${BRANCH:-main}"
APP=/opt/rpi-video-looper
BOOT=/boot/firmware
[ -d "$BOOT" ] || BOOT=/boot
IMAGE="${LOOPER_IMAGE_BUILD:-0}"

[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }

sysd() { if [ "$IMAGE" = 1 ]; then systemctl "$@" 2>/dev/null || true; else systemctl "$@"; fi; }

echo "==> Installing packages (no recommends, keeps the image small)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# FAT32, exFAT and NTFS thumb drives are handled by the kernel; no extra packages needed.
#   python3-qrcode + fonts: setup screens on the projector
#   dnsmasq-base: DHCP/DNS for the setup hotspot; avahi-utils: find Home Assistant
apt-get install -y --no-install-recommends \
  python3-gi gir1.2-gstreamer-1.0 gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad gstreamer1.0-libav gstreamer1.0-alsa \
  mpv ffmpeg python3 python3-paho-mqtt python3-pil python3-qrcode fonts-dejavu-core \
  network-manager dnsmasq-base avahi-daemon avahi-utils iw rfkill git ca-certificates

echo "==> Fetching code into $APP"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [ -n "$SRC" ] && [ "$SRC" = "$APP" ] && [ -f "$APP/looper/__main__.py" ]; then
  :  # running from the installed copy (image build): nothing to fetch
elif [ -n "$SRC" ] && [ -f "$SRC/looper/__main__.py" ]; then
  rm -rf "$APP"; mkdir -p "$APP"; cp -a "$SRC/." "$APP/"
elif [ -d "$APP/.git" ]; then
  git -C "$APP" fetch -q origin "$BRANCH" && git -C "$APP" reset -q --hard "origin/$BRANCH"
else
  rm -rf "$APP"; git clone -q --depth 1 -b "$BRANCH" "$REPO_URL" "$APP"
fi
find "$APP" -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
# Precompile to .pyc now so the first boot (and every boot) skips byte-compiling the whole
# package. On a single-core Pi Zero W this shaves seconds off time-to-first-frame.
python3 -m compileall -q "$APP/looper" >/dev/null 2>&1 || true

echo "==> Service user"
if id looper >/dev/null 2>&1; then
  :
elif [ "$IMAGE" = 1 ]; then
  # Ready-made image: a normal login user, password locked until set through setup (SSH off).
  if id pi >/dev/null 2>&1; then
    usermod -l looper -d /home/looper -m pi && groupmod -n looper pi
  else
    useradd -m -s /bin/bash looper
  fi
  usermod -aG sudo looper
  passwd -l looper >/dev/null
else
  useradd --system --home /var/lib/looper --shell /usr/sbin/nologin looper
fi
usermod -aG video,render,audio,input looper
mkdir -p /var/lib/looper && chown looper:looper /var/lib/looper

echo "==> Prebuilding the GStreamer plugin registry (so cold boot skips the plugin scan)"
# Built as 'looper' and world-readable so the service (User=looper, GST_REGISTRY_UPDATE=no)
# finds it. If this can't run now (e.g. inside an image-build chroot), the service's
# ExecStartPre rebuilds it on first start instead.
runuser -u looper -- env GST_REGISTRY=/var/lib/looper/gst-registry.bin GST_REGISTRY_UPDATE=yes \
  python3 -c 'import gi; gi.require_version("Gst", "1.0"); from gi.repository import Gst; Gst.init(None)' \
  >/dev/null 2>&1 || echo "    note: couldn't prebuild the registry now; it builds on first start"
[ -s /var/lib/looper/gst-registry.bin ] && chmod 0644 /var/lib/looper/gst-registry.bin || true

echo "==> Config file at $BOOT/looper.toml"
if [ -f "$BOOT/looper.toml" ] && ! grep -q '^\[device\]' "$BOOT/looper.toml"; then
  # v0.1/v0.2 template (no Wi-Fi/device sections): keep a copy, install the new one
  mv "$BOOT/looper.toml" "$BOOT/looper.toml.old"
  echo "    previous file kept as $BOOT/looper.toml.old"
fi
[ -f "$BOOT/looper.toml" ] || cp "$APP/config/looper.toml" "$BOOT/looper.toml"

echo "==> USB drive auto-mount at /media/usb (read-only, survives unplug/replug)"
mkdir -p /media/usb
sed -i '\#/media/usb#d' /etc/fstab
echo "/dev/sda1 /media/usb auto ro,nofail,noatime,x-systemd.automount,x-systemd.device-timeout=3s,x-systemd.mount-timeout=10s 0 0" >> /etc/fstab

echo "==> Clean boot: quiet kernel, HDMI forced on, no console text on the projector"
# The kernel command line a projector appliance wants, set once and idempotently:
#  - video=HDMI-A-1:e : force the HDMI connector on (mode still from the display's EDID) so a
#                       projector that is off or not detected at boot still gets video.
#  - console=serial0  : console to the serial port only -- no boot text on any display VT
#                       (the framebuffer console is also unbound, see 99-looper-nofbcon.rules).
#  - quiet/nologo/... : hush the kernel, hide the logo and cursor, no console blanking.
# The boot splash (looper-splash.service -> looper/fbsplash.py) then paints the framebuffer.
if [ -f "$BOOT/cmdline.txt" ]; then
  orig="$(head -n1 "$BOOT/cmdline.txt" | tr -d '\r')"
  cl="$(printf '%s' "$orig" | sed -E 's/ console=tty[0-9]+//g; s/ splash//g')"
  for opt in quiet loglevel=3 logo.nologo vt.global_cursor_default=0 consoleblank=0 video=HDMI-A-1:e; do
    key="${opt%%=*}"
    cl="$(printf ' %s ' "$cl" | sed -E "s/ ${key//./\\.}(=[^ ]*)? / /g" | sed -E 's/^ +| +$//g')"
    cl="$cl $opt"
  done
  if [ "$cl" != "$orig" ]; then
    printf '%s\n' "$cl" > "$BOOT/cmdline.txt.looper-tmp"
    sync && mv -f "$BOOT/cmdline.txt.looper-tmp" "$BOOT/cmdline.txt" && sync
    echo "    cmdline: $cl"
  fi
fi

echo "==> Installing services"
install -m 644 "$APP/system/rpi-video-looper.service" /etc/systemd/system/
install -m 644 "$APP/system/rpi-video-looper.timer" /etc/systemd/system/
install -m 644 "$APP/system/looper-net.service" /etc/systemd/system/
install -m 644 "$APP/system/looper-splash.service" /etc/systemd/system/
# Boot splash on the framebuffer, console text kept off the display, and the H.264 codec
# loaded early so the first video never races the decoder coming up.
install -m 644 "$APP/system/99-looper-nofbcon.rules" /etc/udev/rules.d/99-looper-nofbcon.rules
mkdir -p /etc/modules-load.d
install -m 644 "$APP/system/modules-load.d/looper-codec.conf" /etc/modules-load.d/looper-codec.conf
sysd daemon-reload
# v0.1-v0.3 enabled the service directly (started mid-boot); now a timer starts it after boot
rm -f /etc/systemd/system/multi-user.target.wants/rpi-video-looper.service
mkdir -p /var/log/journal
for unit in rpi-video-looper.timer looper-net.service looper-splash.service NetworkManager.service avahi-daemon.service; do
  sysd enable "$unit" >/dev/null 2>&1 || echo "    note: couldn't enable $unit (masked?)"
done

echo "==> Let the looper persist settings to the USB drive (settings travel with the drive)"
# A looper.toml on the USB is the state file: runtime changes are written back to it. The USB
# is mounted read-only, so the looper runs this small helper as root (it remounts read-write
# only for the atomic write, and only writes inside the USB mount).
chmod +x "$APP/tools/usb-write" 2>/dev/null || true
cat > /etc/sudoers.d/looper-usb <<SUDO
looper ALL=(root) NOPASSWD: $APP/tools/usb-write
SUDO
chmod 0440 /etc/sudoers.d/looper-usb
visudo -cf /etc/sudoers.d/looper-usb >/dev/null 2>&1 || { echo "    note: sudoers check failed; USB persist disabled"; rm -f /etc/sudoers.d/looper-usb; }

echo "==> Setup hotspot: send every web address to the setup page (captive portal)"
mkdir -p /etc/NetworkManager/dnsmasq-shared.d
install -m 644 "$APP/system/captive-dns.conf" /etc/NetworkManager/dnsmasq-shared.d/looper-captive.conf

echo "==> Hardware watchdog settings (off by default, see system/watchdog.conf)"
mkdir -p /etc/systemd/system.conf.d
install -m 644 "$APP/system/watchdog.conf" /etc/systemd/system.conf.d/looper-watchdog.conf

echo "==> Logs: kept across reboots, capped at 16 MB"
mkdir -p /etc/systemd/journald.conf.d
install -m 644 "$APP/system/journald.conf" /etc/systemd/journald.conf.d/looper.conf

echo "==> Keep the console off the projector"
sysd disable getty@tty1.service >/dev/null 2>&1 || true

# Fast-boot trimming is a separate, reversible step once the looper works:
#   sudo /opt/rpi-video-looper/tools/lean.sh      (--no-ssh to turn SSH off, --undo to revert)
if [ "${LEAN:-0}" = "1" ]; then
  bash "$APP/tools/lean.sh"
fi

if [ "$IMAGE" = 1 ]; then
  echo "==> Image build: done"
  exit 0
fi

systemctl restart looper-net.service rpi-video-looper.service 2>/dev/null || true
echo
echo "Done. Reboot to start playing:  sudo reboot"
echo "Web UI:  http://$(hostname).local:8080/"
