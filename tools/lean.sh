#!/usr/bin/env bash
# Trim a working looper down to a fast-booting appliance. Reversible.
#   sudo tools/lean.sh                 apply (keeps SSH on while you're tuning)
#   sudo tools/lean.sh --no-ssh        apply and turn SSH off too
#   sudo tools/lean.sh --undo          put everything back
#
# Measured on a Pi Zero W (Trixie Lite, Imager setup): video started 278 s after
# power-on. cloud-init (~40 s, blocking the rest of boot), NetworkManager stuck
# behind it (93 s), a swap-file resize on every boot (29 s) and ~25 s of services
# a looper never uses were the bulk of it.
#
# SSH maintenance switch: with SSH off, add this to looper.toml on the SD card's
# boot partition (from any PC) and power-cycle:
#     [ssh]
#     enabled = true
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }

BOOT="${LOOPER_BOOT:-/boot/firmware}"; [ -d "$BOOT" ] || BOOT=/boot
BACK="${LOOPER_LEAN_BACK:-/var/lib/looper/lean}"
MODE=apply; NO_SSH=0
for a in "$@"; do
  case "$a" in
    --undo) MODE=undo ;;
    --no-ssh) NO_SSH=1 ;;
    *) echo "unknown option $a"; exit 1 ;;
  esac
done

UNITS=(
  rpi-resize-swap-file.service dphys-swapfile.service    # swap file resized/created every boot
  keyboard-setup.service console-setup.service           # no keyboard on a looper
  bluetooth.service hciuart.service                      # Bluetooth unused
  ModemManager.service triggerhappy.service triggerhappy.socket
  NetworkManager-wait-online.service                     # nothing waits for the network
  e2scrub_reap.service e2scrub_all.timer                 # filesystem maintenance jobs
  apt-daily.timer apt-daily-upgrade.timer man-db.timer cron.service rsyslog.service
  getty@tty1.service serial-getty@ttyS0.service serial-getty@ttyAMA0.service  # login consoles
  rpi-eeprom-update.service sshswitch.service
)

# FAT boot partition: write via temp file + rename + sync, so a power cut can't
# leave a half-written cmdline.txt (that bit us twice during testing)
write_boot_file() {  # write_boot_file <path> <content>
  local tmp="$1.lean-tmp"
  printf '%s' "$2" > "$tmp"; sync "$tmp" 2>/dev/null || sync
  mv -f "$tmp" "$1"; sync
}

if [ "$MODE" = undo ]; then
  [ -d "$BACK" ] || { echo "nothing to undo"; exit 0; }
  echo "==> Restoring services"
  while read -r unit state; do
    case "$state" in
      enabled|enabled-runtime) systemctl enable "$unit" >/dev/null 2>&1 || true ;;
      masked) ;;
      *) ;;
    esac
    systemctl unmask "$unit" >/dev/null 2>&1 || true
  done < "$BACK/units.txt"
  [ -f "$BACK/ssh.state" ] && [ "$(cat "$BACK/ssh.state")" = enabled ] && systemctl enable ssh >/dev/null 2>&1 || true
  echo "==> Restoring config.txt / cmdline.txt"
  [ -f "$BACK/config.txt" ] && write_boot_file "$BOOT/config.txt" "$(cat "$BACK/config.txt")"$'\n'
  [ -f "$BACK/cmdline.txt" ] && write_boot_file "$BOOT/cmdline.txt" "$(cat "$BACK/cmdline.txt")"$'\n'
  [ -f "$BACK/cloud-init-was-enabled" ] && rm -f /etc/cloud/cloud-init.disabled
  mv "$BACK" "$BACK.undone.$(date +%s)"
  echo "Done. Reboot to apply:  sudo reboot"
  exit 0
fi

# ---------------------------------------------------------------- apply
if [ -d "$BACK" ]; then
  echo "lean mode is already applied (run --undo first to re-apply from scratch)"
  [ "$NO_SSH" = 1 ] && { systemctl disable --now ssh.service ssh.socket >/dev/null 2>&1 || true; echo "SSH turned off."; }
  exit 0
fi
mkdir -p "$BACK"
cp "$BOOT/config.txt" "$BACK/config.txt"
cp "$BOOT/cmdline.txt" "$BACK/cmdline.txt"

echo "==> 1/4 Saving Wi-Fi as a permanent network profile (so cloud-init isn't needed)"
CON="$(nmcli -t -f NAME,TYPE,DEVICE connection show --active | awk -F: '$2=="802-11-wireless" && $3!="" {print $1; exit}')"
if [ -z "$CON" ]; then
  echo "    Not connected to Wi-Fi right now; leaving cloud-init on. Re-run once online."
  KEEP_CLOUD=1
else
  SSID="$(nmcli -e no -g 802-11-wireless.ssid connection show "$CON")"
  PSK="$(nmcli -s -e no -g 802-11-wireless-security.psk connection show "$CON" 2>/dev/null || true)"
  if ! nmcli -t -f NAME connection show | grep -qx "looper-wifi"; then
    args=(connection add type wifi ifname wlan0 con-name looper-wifi ssid "$SSID"
          connection.autoconnect yes connection.autoconnect-priority 20)
    [ -n "$PSK" ] && args+=(wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$PSK")
    nmcli "${args[@]}" >/dev/null
  fi
  if nmcli -t -f NAME connection show | grep -qx "looper-wifi"; then
    echo "    saved \"$SSID\" as looper-wifi"
    KEEP_CLOUD=0
  else
    echo "    couldn't save the Wi-Fi profile; leaving cloud-init on"
    KEEP_CLOUD=1
  fi
fi

echo "==> 2/4 Turning off cloud-init (Raspberry Pi Imager's first-boot setup)"
if [ "$KEEP_CLOUD" = 0 ]; then
  if [ -d /etc/cloud ] && [ ! -f /etc/cloud/cloud-init.disabled ]; then
    touch "$BACK/cloud-init-was-enabled"
    touch /etc/cloud/cloud-init.disabled
  fi
else
  echo "    skipped"
fi

echo "==> 3/4 Turning off services a looper doesn't use"
: > "$BACK/units.txt"
for u in "${UNITS[@]}"; do
  state="$(systemctl is-enabled "$u" 2>/dev/null || true)"
  [ -z "$state" ] || [ "$state" = "not-found" ] && continue
  echo "$u $state" >> "$BACK/units.txt"
  systemctl disable "$u" >/dev/null 2>&1 || true
  systemctl mask "$u" >/dev/null 2>&1 || true
  echo "    off: $u"
done
systemctl is-enabled ssh 2>/dev/null > "$BACK/ssh.state" || echo disabled > "$BACK/ssh.state"
if [ "$NO_SSH" = 1 ]; then
  systemctl disable ssh.service ssh.socket >/dev/null 2>&1 || true
  echo "    off: ssh (turn back on with [ssh] enabled = true in looper.toml)"
fi
swapoff -a 2>/dev/null || true

echo "==> 4/4 Firmware and kernel boot options"
cfg="$(cat "$BACK/config.txt")"
cfg="$(printf '%s\n' "$cfg" | sed '/# >>> rpi-video-looper/,/# <<< rpi-video-looper/d' \
       | sed 's/^camera_auto_detect=1/camera_auto_detect=0/; s/^display_auto_detect=1/display_auto_detect=0/')"
cfg="$cfg
# >>> rpi-video-looper (tools/lean.sh)
[all]
disable_splash=1
boot_delay=0
dtoverlay=disable-bt
# <<< rpi-video-looper
"
write_boot_file "$BOOT/config.txt" "$cfg"

line="$(head -n1 "$BACK/cmdline.txt" | tr -d '\r')"
line="$(printf '%s' "$line" | sed -E 's/ cloud-init=enabled//g; s/ systemd\.mask=[^ ]*//g')"
# Move the console off the display: otherwise ext4's "recovering journal" and a parked
# cursor land on the projector during boot (looks broken). Now it stays clean until video.
line="$(printf '%s' "$line" | sed -E 's/\bconsole=tty1\b/console=tty3/')"
for opt in quiet loglevel=3 logo.nologo vt.global_cursor_default=0 consoleblank=0; do
  key="${opt%%=*}"
  line="$(printf ' %s ' "$line" | sed -E "s/ ${key//./\\.}(=[^ ]*)? / /g" | sed -E 's/^ +| +$//g')"
  line="$line $opt"
done
write_boot_file "$BOOT/cmdline.txt" "$line"$'\n'
echo "    cmdline: $line"

echo
echo "Done. Reboot to apply:  sudo reboot"
echo "Undo everything later:  sudo $0 --undo"
