"""Generate Egon's simple app icon.

Outputs both:
  - shell/egon.png, used inside the native app and repo docs
  - shell/egon.ico, used by Windows shortcuts/taskbar/builds

This is a one-shot asset builder, not runtime code.
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


OUT_ICO = Path(__file__).parent / "egon.ico"
OUT_PNG = Path(__file__).parent / "egon.png"
SIZES = [16, 24, 32, 48, 64, 128, 256]


def render(sz: int) -> Image.Image:
    bg = (13, 22, 28, 255)
    fg = (228, 239, 236, 255)
    accent = (64, 211, 168, 255)
    shadow = (0, 0, 0, 70)

    img = Image.new("RGBA", (sz, sz), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    inset = max(1, sz // 32)
    radius = max(4, sz // 7)
    d.rounded_rectangle(
        [(inset, inset), (sz - inset - 1, sz - inset - 1)],
        radius=radius,
        fill=shadow,
    )
    d.rounded_rectangle(
        [(0, 0), (sz - inset - 2, sz - inset - 2)],
        radius=radius,
        fill=bg,
    )

    stroke = max(1, sz // 32)
    d.rounded_rectangle(
        [(stroke, stroke), (sz - inset - stroke - 2, sz - inset - stroke - 2)],
        radius=max(3, radius - stroke),
        outline=accent,
        width=stroke,
    )

    try:
        font = ImageFont.truetype("C:/Windows/Fonts/segoeuib.ttf", int(sz * 0.58))
    except Exception:
        font = ImageFont.load_default()

    text = "E"
    bbox = d.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx = (sz - tw) // 2 - bbox[0]
    ty = (sz - th) // 2 - bbox[1] - max(0, sz // 70)
    d.text((tx, ty), text, fill=fg, font=font)

    return img


def main() -> None:
    base = render(256)
    base.save(OUT_PNG, format="PNG")
    base.save(OUT_ICO, format="ICO", sizes=[(s, s) for s in SIZES])
    print(f"wrote {OUT_PNG} ({OUT_PNG.stat().st_size} bytes)")
    print(f"wrote {OUT_ICO} ({OUT_ICO.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
