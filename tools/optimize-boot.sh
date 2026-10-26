#!/usr/bin/env bash
# Trim Raspberry Pi OS Lite for the fastest power-on-to-video time on a Pi Zero W.
# Every change is reversible; a backup of config.txt / cmdline.txt is kept.
# Run:  sudo tools/optimize-boot.sh        Measure afterwards:  systemd-analyze blame
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }

BOOT=/boot/firmware; [ -d "$BOOT" ] || BOOT=/boot
CFG="$BOOT/config.txt"; CMD="$BOOT/cmdline.txt"
[ -f "$CFG.looper-bak" ] || cp "$CFG" "$CFG.looper-bak"
[ -f "$CMD.looper-bak" ] || cp "$CMD" "$CMD.looper-bak"

echo "==> config.txt: skip the rainbow splash, boot delay and hardware probing"
sed -i '/# >>> rpi-video-looper/,/# <<< rpi-video-looper/d' "$CFG"
sed -i 's/^camera_auto_detect=1/camera_auto_detect=0/; s/^display_auto_detect=1/display_auto_detect=0/' "$CFG"
cat >> "$CFG" <<'EOF'
# >>> rpi-video-looper
[all]
disable_splash=1
boot_delay=0
# Bluetooth is unused; frees the UART and saves a service at boot
dtoverlay=disable-bt
# Short CPU boost while booting
initial_turbo=30
# Keep HDMI on even if the projector is powered after the Pi
hdmi_force_hotplug=1
# <<< rpi-video-looper
EOF

echo "==> cmdline.txt: quiet, no logo, no blinking cursor, no console blanking"
line="$(head -n1 "$CMD")"
for opt in quiet loglevel=3 logo.nologo vt.global_cursor_default=0 consoleblank=0; do
  key="${opt%%=*}"
  line="$(echo "$line" | sed -E "s/(^| )${key}(=[^ ]*)?( |$)/ /g")"
  line="$line $opt"
done
echo "$line" | tr -s ' ' | sed 's/^ //' > "$CMD"

echo "==> Disabling services the looper doesn't need"
for unit in bluetooth.service hciuart.service ModemManager.service triggerhappy.service userconfig.service \
            triggerhappy.socket NetworkManager-wait-online.service systemd-networkd-wait-online.service \
            apt-daily.timer apt-daily-upgrade.timer man-db.timer e2scrub_all.timer \
            keyboard-setup.service rpi-eeprom-update.service cups.service; do
  systemctl disable --now "$unit" >/dev/null 2>&1 || systemctl disable "$unit" >/dev/null 2>&1 || true
done
# cloud-init only matters on first boot (Raspberry Pi Imager settings). A ready-made image
# keeps it for that first boot; looper-net turns it off once the Pi is online.
if [ "${LOOPER_IMAGE_BUILD:-0}" != 1 ] && [ -d /etc/cloud ] && [ ! -f /etc/cloud/cloud-init.disabled ]; then
  touch /etc/cloud/cloud-init.disabled
fi

echo "==> Boot tweaks applied. Reboot to see the difference."
echo "    Restore with: cp $CFG.looper-bak $CFG && cp $CMD.looper-bak $CMD"
