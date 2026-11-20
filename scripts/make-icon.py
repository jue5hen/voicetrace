"""Generate the 1024x1024 VoiceTrace app icon.

Usage: python make-icon.py [output]
Design: #0B0D12 rounded square, warm amber (#FFB454) waveform bars with a soft glow.
Requires Pillow.
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

SIZE = 1024
SS = 2  # supersampling factor
W = SIZE * SS
BG = (11, 13, 18, 255)
AMBER = (255, 180, 84, 255)
AMBER_LIGHT = (255, 205, 138, 255)
RADIUS = 210 * SS


def lerp(a: int, b: int, t: float) -> int:
    return round(a + (b - a) * t)


def vertical_gradient() -> Image.Image:
    top = (21, 25, 34, 255)
    bottom = (9, 11, 15, 255)
    img = Image.new("RGBA", (W, W))
    px = img.load()
    for y in range(W):
        t = y / (W - 1)
        color = tuple(lerp(top[i], bottom[i], t) for i in range(4))
        for x in range(W):
            px[x, y] = color
    return img


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "assets" / "icon.png"
    out.parent.mkdir(parents=True, exist_ok=True)

    # Rounded-square mask so the icon looks intentional on light and dark shells.
    mask = Image.new("L", (W, W), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, W - 1, W - 1], radius=RADIUS, fill=255)

    canvas = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    canvas.paste(vertical_gradient(), (0, 0), mask)

    # Soft warm glow behind the waveform.
    glow = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for r in range(560 * SS, 0, -12 * SS):
        alpha = max(1, int(5 * (1 - r / (560 * SS))))
        gd.ellipse(
            [W // 2 - r, W // 2 + 16 * SS - r, W // 2 + r, W // 2 + 16 * SS + r],
            fill=(255, 180, 84, alpha),
        )
    glow = glow.filter(ImageFilter.GaussianBlur(70 * SS))
    glow.putalpha(glow.getchannel("A").point(lambda a: min(255, int(a * 0.85))))
    canvas = Image.alpha_composite(canvas, Image.composite(glow, Image.new("RGBA", (W, W), (0, 0, 0, 0)), mask))

    # Five rounded waveform bars, symmetric, tallest in the middle.
    heights = [168, 300, 442, 300, 168]
    bar_w = 74 * SS
    gap = 38 * SS
    total_w = len(heights) * bar_w + (len(heights) - 1) * gap
    x = (W - total_w) // 2
    center_y = W // 2 + 18 * SS

    bars = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    bd = ImageDraw.Draw(bars)
    for i, h in enumerate(heights):
        h *= SS
        top = center_y - h // 2
        bottom = center_y + h // 2
        color = AMBER_LIGHT if i == 2 else AMBER
        bd.rounded_rectangle([x, top, x + bar_w - 1, bottom], radius=bar_w // 2, fill=color)
        x += bar_w + gap

    # Bloom pass then crisp pass.
    bloom = bars.filter(ImageFilter.GaussianBlur(26 * SS))
    bloom.putalpha(bloom.getchannel("A").point(lambda a: int(a * 0.55)))
    canvas = Image.alpha_composite(canvas, bloom)
    canvas = Image.alpha_composite(canvas, bars)

    # Hairline inner border adds a little structure at large sizes.
    border = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    ImageDraw.Draw(border).rounded_rectangle(
        [3 * SS, 3 * SS, W - 3 * SS, W - 3 * SS],
        radius=RADIUS - 3 * SS,
        outline=(255, 180, 84, 34),
        width=2 * SS,
    )
    canvas = Image.alpha_composite(canvas, border)

    icon = canvas.resize((SIZE, SIZE), Image.LANCZOS)
    icon.save(out, "PNG")
    print(f"wrote {out} ({icon.size[0]}x{icon.size[1]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
