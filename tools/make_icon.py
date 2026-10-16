#!/usr/bin/env python3
"""Draw the brand icons (needs Pillow):

    tools/make_icon.py custom_components/time_alignment/brand

An original icon for time_alignment: a reference clock and, in front of it, a second
clock whose hands read slightly differently -- "how far apart are the clocks"."""

import math
import sys

from PIL import Image, ImageDraw

NAVY = (16, 42, 67, 255)
BLUE = (3, 169, 244, 255)
ORANGE = (255, 143, 0, 255)
WHITE = (255, 255, 255, 255)


def _point(cx: float, cy: float, r: float, degrees: float) -> tuple[float, float]:
    a = math.radians(degrees)
    return cx + r * math.sin(a), cy - r * math.cos(a)


def _face(d, u, cx, cy, r, ring, minute_deg, hands) -> None:
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=WHITE, outline=ring, width=int(10 * u))
    for hour in range(12):
        d.line([*_point(cx, cy, r * 0.78, hour * 30), *_point(cx, cy, r * 0.9, hour * 30)],
               fill=ring, width=int(5 * u))
    d.line([cx, cy, *_point(cx, cy, r * 0.72, minute_deg)], fill=hands, width=int(9 * u))
    d.line([cx, cy, *_point(cx, cy, r * 0.45, 300)], fill=hands, width=int(11 * u))
    d.ellipse([cx - 10 * u, cy - 10 * u, cx + 10 * u, cy + 10 * u], fill=hands)


def draw(size: int) -> Image.Image:
    big = size * 4  # drawn large, then scaled down for smooth edges
    im = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    u = big / 256
    _face(d, u, 100 * u, 112 * u, 84 * u, NAVY, 20, NAVY)  # the reference
    _face(d, u, 160 * u, 150 * u, 84 * u, BLUE, 38, ORANGE)  # the clock being checked
    return im.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    for size, name in ((256, "icon.png"), (512, "icon@2x.png")):
        draw(size).save(f"{sys.argv[1]}/{name}", optimize=True)
