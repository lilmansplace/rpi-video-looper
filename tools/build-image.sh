#!/usr/bin/env bash
# Build a ready-to-flash rpi-video-looper image from the official Raspberry Pi OS Lite (32-bit).
# Runs on any x86/arm Linux with sudo (used by .github/workflows/build-image.yml):
#   sudo apt-get install -y qemu-user-static binfmt-support xz-utils parted e2fsprogs
#   sudo tools/build-image.sh            -> out/rpi-video-looper-<version>.img.xz
# The OS is customised inside a chroot (qemu emulates the Pi's ARM CPU), using install.sh.
set -euo pipefail

BASE_URL="${BASE_URL:-https://downloads.raspberrypi.com/raspios_lite_armhf_latest}"
GROW_MB="${GROW_MB:-1024}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="${VERSION:-$(python3 -c "import sys; sys.path.insert(0,'$REPO'); import looper; print(looper.__version__)")}"
WORK="${WORK:-$REPO/build}"
OUT="${OUT:-$REPO/out}"
NAME="rpi-video-looper-${VERSION}"
MNT="$WORK/root"

[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
mkdir -p "$WORK" "$OUT"

LOOP=""
cleanup() {
  set +e
  for m in boot/firmware dev/pts dev proc sys run; do
    mountpoint -q "$MNT/$m" && umount -l "$MNT/$m"
  done
  mountpoint -q "$MNT" && umount -l "$MNT"
  [ -n "$LOOP" ] && losetup -d "$LOOP"
}
trap cleanup EXIT

echo "==> Downloading Raspberry Pi OS Lite (32-bit)"
if [ ! -f "$WORK/base.img" ]; then
  curl -fL --retry 3 -o "$WORK/base.img.xz" "$BASE_URL"
  xz -d -T0 -c "$WORK/base.img.xz" > "$WORK/base.img"
fi
cp --sparse=always "$WORK/base.img" "$WORK/$NAME.img"
IMG="$WORK/$NAME.img"

echo "==> Making room for the looper (+${GROW_MB} MB)"
truncate -s "+${GROW_MB}M" "$IMG"
parted -s "$IMG" resizepart 2 100%
LOOP="$(losetup -fP --show "$IMG")"
partprobe "$LOOP" 2>/dev/null || true
sleep 1
e2fsck -pf "${LOOP}p2" || true
resize2fs "${LOOP}p2"

echo "==> Mounting"
mkdir -p "$MNT"
mount "${LOOP}p2" "$MNT"
mkdir -p "$MNT/boot/firmware"
mount "${LOOP}p1" "$MNT/boot/firmware"
for m in dev dev/pts proc sys run; do mount --bind "/$m" "$MNT/$m"; done
cp --remove-destination /etc/resolv.conf "$MNT/etc/resolv.conf" 2>/dev/null || true

echo "==> Copying the looper"
rm -rf "$MNT/opt/rpi-video-looper"
mkdir -p "$MNT/opt/rpi-video-looper"
git -C "$REPO" archive HEAD | tar -x -C "$MNT/opt/rpi-video-looper"

echo "==> Installing inside the image (ARM emulation: this takes a while)"
# no service starts inside the chroot
printf '#!/bin/sh\nexit 101\n' > "$MNT/usr/sbin/policy-rc.d"; chmod +x "$MNT/usr/sbin/policy-rc.d"
chroot "$MNT" /usr/bin/env LOOPER_IMAGE_BUILD=1 DEBIAN_FRONTEND=noninteractive \
  bash /opt/rpi-video-looper/install.sh
rm -f "$MNT/usr/sbin/policy-rc.d"

echo "==> Image defaults"
# SSH stays off until a password is set (looper.toml [ssh] or the setup page).
chroot "$MNT" systemctl disable ssh.service 2>/dev/null || true
chroot "$MNT" systemctl mask userconfig.service 2>/dev/null || true   # no first-boot user prompt
echo looper > "$MNT/etc/hostname"
sed -i 's/^127\.0\.1\.1.*/127.0.1.1\tlooper/' "$MNT/etc/hosts"
grep -q '^127.0.1.1' "$MNT/etc/hosts" || printf '127.0.1.1\tlooper\n' >> "$MNT/etc/hosts"
echo "${VERSION}" > "$MNT/etc/looper-image-version"

echo "==> Slimming"
chroot "$MNT" apt-get clean
rm -rf "$MNT/var/lib/apt/lists/"* "$MNT/var/cache/apt/"*.bin "$MNT/tmp/"* "$MNT/var/log/"*.log
rm -f "$MNT/etc/ssh/ssh_host_"*   # regenerated on first boot, unique per looper
fstrim -v "$MNT" 2>/dev/null || true

cleanup
LOOP=""
trap - EXIT

echo "==> Compressing"
xz -T0 -6 -f -k "$IMG"
mv "$IMG.xz" "$OUT/$NAME.img.xz"

echo "==> Imager listing"
python3 - "$IMG" "$OUT/$NAME.img.xz" "$VERSION" "$OUT/os-list.json" <<'PY'
import hashlib, json, os, sys, datetime
img, xz, version, out = sys.argv[1:5]
def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()
url = f"https://github.com/lilmansplace/rpi-video-looper/releases/download/v{version}/{os.path.basename(xz)}"
entry = {
    "name": f"rpi-video-looper {version}",
    "description": "Video looper for Raspberry Pi Zero W: plays a USB drive on loop, web + Home Assistant control",
    "icon": "https://raw.githubusercontent.com/lilmansplace/rpi-video-looper/main/docs/icon.png",
    "url": url,
    "extract_size": os.path.getsize(img),
    "extract_sha256": sha(img),
    "image_download_size": os.path.getsize(xz),
    "image_download_sha256": sha(xz),
    "release_date": datetime.date.today().isoformat(),
    "init_format": "cloudinit-rpi",
    "devices": ["pi1-32bit", "pi2-32bit", "pi3-32bit"],
}
with open(out, "w") as f:
    json.dump({"os_list": [entry]}, f, indent=2)
print(json.dumps(entry, indent=2))
PY
rm -f "$IMG"
ls -lh "$OUT"
