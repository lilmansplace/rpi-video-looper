"""Full-screen info cards shown on the projector (setup QR codes, status).

Rendered once to a PNG in /run (tmpfs) and shown by mpv like any other file,
so nothing extra has to own the display.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover
    Image = None
try:
    import qrcode
except ImportError:  # pragma: no cover
    qrcode = None

W, H = 1280, 720
BG = (14, 16, 20)
FG = (236, 240, 245)
MUTE = (140, 150, 165)
ACCENT = (255, 138, 61)
ERR = (255, 120, 110)
FONT_DIRS = ["/usr/share/fonts/truetype/dejavu"]


def _font(size: int, bold: bool = False):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    for d in FONT_DIRS:
        p = os.path.join(d, name)
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def wifi_qr_payload(ssid: str, psk: str) -> str:
    def esc(s: str) -> str:
        for ch in '\\;,":':
            s = s.replace(ch, "\\" + ch)
        return s
    return f"WIFI:T:WPA;S:{esc(ssid)};P:{esc(psk)};;"


def _qr(data: str, size: int):
    q = qrcode.QRCode(border=2, box_size=10, error_correction=qrcode.constants.ERROR_CORRECT_M)
    q.add_data(data)
    q.make(fit=True)
    img = q.make_image(fill_color="black", back_color="white").get_image().convert("RGB")
    return img.resize((size, size), Image.Resampling.NEAREST)


def _wrap(draw, text: str, font, width: int) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if draw.textlength(trial, font=font) <= width:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def _card(title: str, lines: list[tuple[str, tuple, int, bool]], qr: tuple[str, str] | None = None,
          footer: str | None = None):
    """lines: (text, color, size, bold). qr: (payload, caption)."""
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 8], fill=ACCENT)
    x, y = 80, 80
    text_w = W - 2 * x
    if qr:
        qs = 400
        qx, qy = W - 80 - qs, (H - qs) // 2 - 20
        img.paste(_qr(qr[0], qs), (qx, qy))
        cap_f = _font(24)
        cw = d.textlength(qr[1], font=cap_f)
        d.text((qx + (qs - cw) / 2, qy + qs + 16), qr[1], fill=MUTE, font=cap_f)
        text_w = qx - x - 60
    tf = _font(54, True)
    for ln in _wrap(d, title, tf, text_w):
        d.text((x, y), ln, fill=FG, font=tf)
        y += 66
    y += 24
    for text, color, size, bold in lines:
        if not text:
            y += size // 2
            continue
        f = _font(size, bold)
        for ln in _wrap(d, text, f, text_w):
            d.text((x, y), ln, fill=color, font=f)
            y += int(size * 1.35)
    if footer:
        f = _font(22)
        d.text((x, H - 60), footer, fill=MUTE, font=f)
    return img


def render(state: dict, path: str) -> bool:
    """Draw the screen for a network/setup state. Returns False if it can't be drawn."""
    if Image is None or qrcode is None:
        return False
    mode = state.get("mode")
    try:
        if mode == "setup":
            lines = []
            if state.get("error"):
                lines += [(state["error"], ERR, 30, True), ("", FG, 30, False)]
            lines += [
                ("1.  Scan the code with your phone's camera to join the setup Wi-Fi.", FG, 32, False),
                ("", FG, 20, False),
                ("2.  The setup page opens by itself. If it doesn't, go to:", FG, 32, False),
                (f"     {state.get('portal', 'http://10.42.0.1/')}", ACCENT, 32, True),
                ("", FG, 20, False),
                (f"Or join Wi-Fi “{state['ssid']}”", MUTE, 28, False),
                (f"with password  {state['psk']}", MUTE, 28, False),
            ]
            img = _card("Let's get this looper online", lines,
                        qr=(wifi_qr_payload(state["ssid"], state["psk"]), "Scan to join setup Wi-Fi"),
                        footer="Video Looper setup")
        elif mode == "connecting":
            img = _card("Connecting…", [(f"Joining “{state.get('network', '')}”", FG, 36, False),
                                           ("", FG, 20, False),
                                           ("This takes up to a minute.", MUTE, 30, False)])
        elif mode == "online":
            url = f"http://{state.get('ip')}:{state.get('web_port', 8080)}/"
            lines = [(f"Connected to “{state.get('network') or 'your network'}”", FG, 34, False),
                     ("", FG, 20, False),
                     ("Control it from your phone:", MUTE, 30, False),
                     (url, ACCENT, 36, True)]
            if state.get("mqtt"):
                lines += [("", FG, 20, False), (state["mqtt"], MUTE, 28, False)]
            if state.get("note"):
                lines += [("", FG, 20, False), (state["note"], FG, 30, False)]
            img = _card(state.get("title") or "You're all set!", lines, qr=(url, "Scan for controls"))
        else:
            return False
        tmp = path + ".tmp.png"
        img.save(tmp, "PNG", optimize=False, compress_level=1)
        os.replace(tmp, path)
        return True
    except Exception:
        log.exception("could not draw %s screen", mode)
        return False
