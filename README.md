# rpi-video-looper

A lightweight, fast-booting video looper for the **Raspberry Pi Zero W**, inspired by
[Adafruit's Pi Video Looper](https://learn.adafruit.com/raspberry-pi-video-looper-2/overview) /
[videolooper.de](https://videolooper.de/), rebuilt around Wi-Fi control:

- **Now-playing sensor** plus playback position, duration and Pi health (temp, load, memory)
- **Tunable settings** (shuffle, repeat, volume, snapshot rate, …) from the web UI or Home Assistant
- **Controls:** next, previous, restart, play/pause, jump to any video
- **Snapshots** of what's on screen, shown in the web UI and as a Home Assistant camera
- **Home Assistant over MQTT** with auto-discovery: no custom integration to install
- **Fast boot** and **self-healing** (service watchdog + hardware watchdog)

## How it stays light on a Pi Zero W

| Concern | Approach |
|---|---|
| Decoding | One long-lived `mpv` process using the Pi's hardware H.264 decoder; the CPU mostly just shuffles buffers |
| Control | One small Python service (stdlib + paho-mqtt), single main thread, ~1 s tick, no web framework |
| "Video stream" | Periodic stills instead of a live stream (a second encode would starve playback). JPEGs are shrunk with Pillow's DCT draft mode, which is very cheap |
| SD card | Logs and snapshots live in RAM; the USB drive is mounted read-only |
| Lock-ups | systemd watchdog restarts the service if it hangs; the BCM2835 hardware watchdog reboots the Pi if the OS hangs; memory cap on the service |
| Bad files | Backoff on errors, so a broken drive can't spin the CPU |

```
 USB drive ──► mpv (hw decode) ──► HDMI ──► projector
                  ▲  JSON IPC
                  │
            looper (Python) ──► web UI :8080
                  │
                  └──► MQTT ──► Home Assistant
```

## Video format

Use **H.264 (AVC) in .mp4, up to 1080p at 30 fps**. That's what the Zero W decodes
in hardware. H.265/HEVC, VP9, 4K and 60 fps 1080p will stutter.

**AtmosFX users:** download the **HD (1080p)** version of each effect, not 4K.

### The looper checks for you

Each video is checked the first time it plays (using what the player already knows, so it
costs nothing). Anything too heavy for the Pi shows up as a **warning** in the web UI and the
*Format warnings* sensor in Home Assistant, with the reason ("4K is above the Pi Zero's
1080p limit", "H.265/HEVC isn't hardware-decoded…"). Turn on **Skip incompatible videos**
to have the looper pass over those files instead of stuttering through them.

### Convert on your computer

`tools/convert_videos.py` runs on Windows, macOS or Linux. It checks a folder and
re-encodes only the files that need it; everything else is copied as-is, so the output
folder can go straight onto the thumb drive. Needs Python 3 and ffmpeg
(`winget install ffmpeg`, `brew install ffmpeg` or `sudo apt install ffmpeg`).

```bash
python3 tools/convert_videos.py ~/Videos/Halloween --check   # just report
python3 tools/convert_videos.py ~/Videos/Halloween           # writes ~/Videos/Halloween/pi-ready
python3 tools/convert_videos.py D:\Halloween --out E:\       # straight to the USB drive
```

Don't convert on the Pi itself: a Zero would take hours per video.

## Install

1. **Flash** with Raspberry Pi Imager: *Raspberry Pi Zero* → *Raspberry Pi OS (other)* →
   **Raspberry Pi OS Lite (32-bit)**. In the customisation screen set the hostname
   (e.g. `looper`), your Wi-Fi (2.4 GHz), a user, and enable SSH.
2. Boot the Pi, then SSH in: `ssh <user>@looper.local`
3. Install (takes a few minutes on a Zero):
   ```bash
   sudo apt-get install -y git
   git clone https://github.com/lilmansplace/rpi-video-looper.git
   cd rpi-video-looper && sudo bash install.sh
   ```
4. Optional: edit `/boot/firmware/looper.toml` to turn on MQTT (see below).
5. `sudo reboot`. Videos on the USB drive start playing; the UI is at `http://looper.local:8080/`.

## Home Assistant

In `/boot/firmware/looper.toml`:

```toml
[mqtt]
enabled = true
host = "192.168.1.10"   # your HA / Mosquitto address
username = "looper"
password = "…"
```

Restart (`sudo systemctl restart rpi-video-looper`). A **Video Looper** device appears
under *Settings → Devices → MQTT* with:

| Entity | Type |
|---|---|
| Now playing (attributes: file, index, position, duration, …) | sensor |
| Status, Position, Duration, Video count | sensor |
| Format warnings (count; attribute `videos` lists each file and why) | sensor |
| Video format of the current file, e.g. `h264 1920x1080 30fps (hw)` | diagnostic sensor |
| CPU temperature, CPU load, Free memory | diagnostic sensor |
| Next, Previous, Restart video, Play/Pause, Rescan USB | button |
| Play video (pick any file) | select |
| Repeat (all / one / off) | select |
| Shuffle, Paused, Skip incompatible videos | switch |
| Volume, Snapshot interval, Position update interval, USB rescan interval | number |
| Snapshot | camera |

Example automation, *start the Halloween reel when the porch motion sensor trips*:

```yaml
action:
  - action: select.select_option
    target: { entity_id: select.rpi_looper_play_video }
    data: { option: "jump scare 1" }
```

Raw MQTT topics are documented in [`looper/mqtt_ha.py`](looper/mqtt_ha.py).

## Web API

```
GET  /api/state              GET /api/library           GET /snapshot.jpg
POST /api/cmd       {"cmd": "next"}   {"cmd": "jump", "arg": 3}   {"cmd": "seek", "arg": 30}
POST /api/settings  {"volume": 80, "shuffle": false, "repeat": "one"}
```

## Tuning & troubleshooting

```bash
journalctl -u rpi-video-looper -f            # live log
systemd-analyze blame                        # what's slowing boot
sudo systemctl stop rpi-video-looper
python3 /opt/rpi-video-looper/tools/bench.py /media/usb/clip.mp4   # find the smoothest player settings
```

- `tools/optimize-boot.sh` (run by the installer) disables the splash, Bluetooth, unused services and
  boot delays. It backs up `config.txt` / `cmdline.txt` first.
- Set `SKIP_BOOT_TWEAKS=1` before `install.sh` to leave boot settings alone.
- The USB drive is expected as `/dev/sda1`. For a drive without a partition table set
  `device = "/dev/sda"` and edit the `/media/usb` line in `/etc/fstab`.

## Development

Runs off-device with a fake mpv:

```bash
python3 -m unittest tests.test_looper -v
```

## Roadmap

- [x] Format check, warnings and auto-skip; PC conversion tool
- [ ] Verify the default `[player]` settings on real hardware (`tools/bench.py`)
- [ ] Optional read-only root (overlayfs) for pull-the-plug safety
- [ ] Measure and trim boot time further
- [ ] Optional web UI password

## License

[MIT](LICENSE): use it, change it, sell it, fork it. Just keep the license notice.
