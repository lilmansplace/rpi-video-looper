#!/usr/bin/env bash
# rpi-video-looper installer. Run on a fresh Raspberry Pi OS Lite (32-bit, Trixie):
#   curl -fsSL https://raw.githubusercontent.com/lilmansplace/rpi-video-looper/main/install.sh | sudo bash
# or, from a clone:  sudo ./install.sh
# Safe to run again to update.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/lilmansplace/rpi-video-looper.git}"
BRANCH="${BRANCH:-main}"
APP=/opt/rpi-video-looper
BOOT=/boot/firmware
[ -d "$BOOT" ] || BOOT=/boot

[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }

echo "==> Installing packages (no recommends, keeps the image small)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# FAT32, exFAT and NTFS thumb drives are handled by the kernel; no extra packages needed.
apt-get install -y --no-install-recommends \
  mpv python3 python3-paho-mqtt python3-pil git ca-certificates

echo "==> Fetching code into $APP"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [ -n "$SRC" ] && [ -f "$SRC/looper/__main__.py" ] && [ "$SRC" != "$APP" ]; then
  rm -rf "$APP"; mkdir -p "$APP"; cp -a "$SRC/." "$APP/"
elif [ -d "$APP/.git" ]; then
  git -C "$APP" fetch -q origin "$BRANCH" && git -C "$APP" reset -q --hard "origin/$BRANCH"
else
  rm -rf "$APP"; git clone -q --depth 1 -b "$BRANCH" "$REPO_URL" "$APP"
fi

echo "==> Creating service user"
id looper >/dev/null 2>&1 || useradd --system --home /var/lib/looper --shell /usr/sbin/nologin looper
usermod -aG video,render,audio,input looper

echo "==> Config file at $BOOT/looper.toml"
[ -f "$BOOT/looper.toml" ] || cp "$APP/config/looper.toml" "$BOOT/looper.toml"

echo "==> USB drive auto-mount at /media/usb (read-only, survives unplug/replug)"
mkdir -p /media/usb
sed -i '\#/media/usb#d' /etc/fstab
echo "/dev/sda1 /media/usb auto ro,nofail,noatime,x-systemd.automount,x-systemd.device-timeout=3s,x-systemd.mount-timeout=10s 0 0" >> /etc/fstab
systemctl daemon-reload

echo "==> Installing service"
install -m 644 "$APP/system/rpi-video-looper.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable rpi-video-looper.service >/dev/null

echo "==> Hardware watchdog: reboot automatically if the whole Pi locks up"
mkdir -p /etc/systemd/system.conf.d
install -m 644 "$APP/system/watchdog.conf" /etc/systemd/system.conf.d/looper-watchdog.conf

echo "==> Logs in RAM (no SD card wear, faster)"
mkdir -p /etc/systemd/journald.conf.d
install -m 644 "$APP/system/journald.conf" /etc/systemd/journald.conf.d/looper.conf

echo "==> Keep the console off the projector"
systemctl disable getty@tty1.service >/dev/null 2>&1 || true

if [ "${SKIP_BOOT_TWEAKS:-0}" != "1" ]; then
  bash "$APP/tools/optimize-boot.sh"
fi

echo
echo "Done. Edit $BOOT/looper.toml for MQTT, then reboot:  sudo reboot"
echo "Web UI after boot:  http://$(hostname).local:8080/"
