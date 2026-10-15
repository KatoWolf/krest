"""Relic V2: half-size mark, real fragment transforms and moving steel light.
All fragments partition the exact approved alpha mask; no substitute silhouette.
"""

from pathlib import Path
import math
from PIL import Image, ImageDraw, ImageFilter, ImageChops

ASSET = Path(__file__).parent / "assets/relic/indicator-metal.png"
MARK = Image.open(ASSET).convert("RGBA")
SS = 3
ICON = 92
BASE = MARK.resize((ICON * SS, ICON * SS), Image.Resampling.LANCZOS)
# Partition original 384-unit artwork into six complementary pieces.
MASKS = [Image.new("L", BASE.size) for _ in range(6)]
for y in range(BASE.height):
    sy = y * 384 / BASE.height
    for x in range(BASE.width):
        sx = x * 384 / BASE.width
        if sy < 142 and sx < 178:
            i = 1
        elif sy < 142 and sx > 208:
            i = 2
        elif sy < 118:
            i = 0
        elif sy < 204:
            i = 3
        elif sy < 286:
            i = 4
        else:
            i = 5
        MASKS[i].putpixel((x, y), 255)
OFFSETS = [(2, -12), (-13, 3), (13, -2), (-4, 12), (5, 17), (-3, 24)]


def smooth(x):
    x = max(0.0, min(1.0, x))
    return x * x * (3 - 2 * x)


def cross_frame(age, state, end_age, reduced_motion=False):
    """Assemble actual masked shards; slow moving highlight remains while busy."""
    mark = BASE.copy()
    if not reduced_motion and state == "busy":
        # A diagonal steel/violet reflection visibly travels UP the cross.
        phase = (age / 2.7) % 1
        cy = (1.2 - 1.4 * phase) * ICON * SS
        light = Image.new("L", BASE.size)
        d = ImageDraw.Draw(light)
        band = 13 * SS
        d.polygon(
            [
                (-20, cy - band),
                (BASE.width + 20, cy - band - 16 * SS),
                (BASE.width + 20, cy + band - 16 * SS),
                (-20, cy + band),
            ],
            fill=155,
        )
        light = light.filter(ImageFilter.GaussianBlur(4 * SS))
        light = ImageChops.multiply(light, BASE.getchannel("A"))
        sheen = Image.new("RGBA", BASE.size, (207, 187, 236, 0))
        sheen.putalpha(light)
        mark.alpha_composite(sheen)
    canvas = Image.new("RGBA", (280 * SS, 256 * SS))
    for i, mask in enumerate(MASKS):
        assembly = 1 if reduced_motion else smooth((age - i * 0.075) / 0.70)
        exit = 0 if reduced_motion or state == "busy" else smooth((end_age - 1.2) / 0.65)
        scatter = (1 - assembly) + exit * 0.7
        dx, dy = OFFSETS[i]
        # Tiny coherent flex of the physical facets, not global opacity pulsing.
        flex = (
            0
            if reduced_motion or state != "busy"
            else math.sin(age * math.tau / 2.7 - i * 0.5) * 0.65 * smooth((age - 1.1) / 0.5)
        )
        px = round((94 + dx * scatter + flex * (0.5 if i in (1, 2) else 0.2)) * SS)
        py = round((134 + dy * scatter + flex) * SS)
        piece = mark.copy()
        alpha = ImageChops.multiply(mark.getchannel("A"), mask)
        piece.putalpha(alpha)
        canvas.alpha_composite(piece, (px, py))
    return canvas.resize((280, 256), Image.Resampling.LANCZOS)


def render(age, state="busy", end_age=0.0, scale=1.0, reduced_motion=False):
    if state not in ("busy", "success", "error", "cancelled"):
        raise ValueError(state)
    image = Image.new("RGBA", (280, 256))
    appear = smooth(age / 0.35)
    finish = 1 if state == "busy" else 1 - smooth((end_age - 1.2) / 0.65)
    opacity = appear * finish
    if opacity <= 0:
        return image.resize((round(280 * scale), round(256 * scale)))
    color = {
        "busy": (140, 111, 172),
        "success": (161, 185, 177),
        "error": (190, 121, 127),
        "cancelled": (142, 140, 152),
    }[state]
    glow = Image.new("RGBA", image.size)
    g = ImageDraw.Draw(glow)
    g.ellipse((120, 151, 160, 232), fill=(*color, 28))
    g.ellipse((105, 225, 175, 239), fill=(*color, 36))
    image.alpha_composite(glow.filter(ImageFilter.GaussianBlur(14)))
    image.alpha_composite(cross_frame(age, state, end_age, reduced_motion))
    d = ImageDraw.Draw(image)
    y = 237
    d.line((115, y, 127, y), fill=(*color, 65))
    d.line((153, y, 165, y), fill=(*color, 65))
    if state == "success":
        d.line((136, y, 139, y + 3, 145, y - 4), fill=(*color, 220), width=1)
    elif state == "error":
        d.line((140, y - 4, 140, y + 1), fill=(*color, 230))
        d.point((140, y + 4), fill=(*color, 230))
    elif state == "cancelled":
        d.line((137, y, 143, y), fill=(*color, 170))
    # Busy has no distracting dots: motion is in the cross itself.
    image.putalpha(image.getchannel("A").point(lambda a: round(a * opacity)))
    if scale != 1:
        image = image.resize((round(280 * scale), round(256 * scale)), Image.Resampling.LANCZOS)
    return image


from functools import lru_cache


@lru_cache(maxsize=96)
def animation_frame(mode, frame, reduced=False, assembled=22):
    """Cache small animation frames; no image processing repeats while listening."""
    age = frame / 20 if mode == "assemble" else 1.1 + frame / 20
    if mode == "leave":
        image = render(assembled / 20, "cancelled", 1.2 + frame / 20, reduced_motion=reduced)
    else:
        image = render(age, reduced_motion=reduced)
    return image.crop((55, 104, 225, 256))
