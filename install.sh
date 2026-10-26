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
  mpv python3 python3-paho-mqtt python3-pil python3-qrcode fonts-dejavu-core \
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

echo "==> Installing services"
install -m 644 "$APP/system/rpi-video-looper.service" /etc/systemd/system/
install -m 644 "$APP/system/rpi-video-looper.timer" /etc/systemd/system/
install -m 644 "$APP/system/looper-net.service" /etc/systemd/system/
sysd daemon-reload
# v0.1-v0.3 enabled the service directly (started mid-boot); now a timer starts it after boot
rm -f /etc/systemd/system/multi-user.target.wants/rpi-video-looper.service
mkdir -p /var/log/journal
for unit in rpi-video-looper.timer looper-net.service NetworkManager.service avahi-daemon.service; do
  sysd enable "$unit" >/dev/null 2>&1 || echo "    note: couldn't enable $unit (masked?)"
done

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

# Boot tweaks are opt-in until they've been proven on hardware:  sudo BOOT_TWEAKS=1 bash install.sh
if [ "${BOOT_TWEAKS:-0}" = "1" ]; then
  LOOPER_IMAGE_BUILD="$IMAGE" bash "$APP/tools/optimize-boot.sh"
fi

if [ "$IMAGE" = 1 ]; then
  echo "==> Image build: done"
  exit 0
fi

systemctl restart looper-net.service rpi-video-looper.service 2>/dev/null || true
echo
echo "Done. Reboot to start playing:  sudo reboot"
echo "Web UI:  http://$(hostname).local:8080/"
