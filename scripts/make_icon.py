#!/usr/bin/env python3
"""Generate the Monteur app icon from the brand mark.

    python scripts/make_icon.py

Writes ``packaging/monteur.ico`` (multi-size, picked up by the PyInstaller spec
and the Inno Setup installer) and ``packaging/monteur.png`` (512px, for docs /
future macOS iconset). The mark mirrors the Studio's brand: a framed viewport
with an ascending pacing line, in Monteur's ember on a charcoal tile.

Needs Pillow (a build-time tool, not a runtime dep): pip install pillow
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
OUT_ICO = ROOT / "packaging" / "monteur.ico"
OUT_PNG = ROOT / "packaging" / "monteur.png"

CHARCOAL = (23, 23, 26, 255)
EMBER = (232, 130, 60, 255)
EMBER_HI = (245, 170, 110, 255)


def _render(px: int) -> Image.Image:
    """Draw the icon at 4x then downsample — cheap anti-aliasing."""
    s = px * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # charcoal app tile
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.20), fill=CHARCOAL)

    # the framed viewport (a monitor / clip frame)
    fw = int(s * 0.046)  # frame stroke
    d.rounded_rectangle(
        [int(s * 0.20), int(s * 0.27), int(s * 0.80), int(s * 0.70)],
        radius=int(s * 0.055), outline=EMBER, width=fw,
    )

    # the pacing line inside: low → up → dip → high (the brand's curve)
    lw = int(s * 0.050)
    pts = [
        (int(s * 0.28), int(s * 0.60)),
        (int(s * 0.42), int(s * 0.44)),
        (int(s * 0.54), int(s * 0.53)),
        (int(s * 0.72), int(s * 0.38)),
    ]
    d.line(pts, fill=EMBER_HI, width=lw, joint="curve")
    # round the vertices so the line reads clean at small sizes
    r = lw // 2
    for x, y in pts:
        d.ellipse([x - r, y - r, x + r, y + r], fill=EMBER_HI)

    return img.resize((px, px), Image.LANCZOS)


def main() -> int:
    sizes = [16, 32, 48, 64, 128, 256]
    frames = [_render(px) for px in sizes]
    OUT_ICO.parent.mkdir(parents=True, exist_ok=True)
    # Pillow writes a multi-size .ico from the largest frame + a sizes list
    frames[-1].save(OUT_ICO, format="ICO", sizes=[(p, p) for p in sizes])
    _render(512).save(OUT_PNG, format="PNG")
    print(f"Icon → {OUT_ICO}  (sizes: {', '.join(map(str, sizes))})")
    print(f"PNG  → {OUT_PNG}  (512px)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
