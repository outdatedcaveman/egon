"""Generate a simple Egon app icon: dark teal background, satellite emoji-style mark.

Outputs egon.ico in this same directory. One-shot script — re-run if you want
to tweak the design. Not invoked at runtime; ico is committed.
"""
from PIL import Image, ImageDraw, ImageFont
from pathlib import Path

OUT = Path(__file__).parent / "egon.ico"
SIZES = [16, 24, 32, 48, 64, 128, 256]

def render(sz: int) -> Image.Image:
    # Egon palette — dark teal background, soft cream "E"
    bg = (16, 47, 60, 255)        # deep teal
    fg = (240, 233, 213, 255)     # warm cream
    ring = (96, 165, 168, 255)    # accent ring

    img = Image.new("RGBA", (sz, sz), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # rounded-square background
    radius = max(2, sz // 5)
    d.rounded_rectangle([(0, 0), (sz - 1, sz - 1)], radius=radius, fill=bg)

    # accent ring (orbit-ish, evokes 🛰️ without needing an emoji font)
    pad = max(2, sz // 6)
    ring_w = max(1, sz // 24)
    d.ellipse(
        [(pad, pad), (sz - pad, sz - pad)],
        outline=ring, width=ring_w,
    )

    # bold "E" centered
    try:
        font_path = "C:/Windows/Fonts/segoeuib.ttf"  # Segoe UI Bold
        font = ImageFont.truetype(font_path, int(sz * 0.55))
    except Exception:
        font = ImageFont.load_default()
    text = "E"
    bbox = d.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx = (sz - tw) // 2 - bbox[0]
    ty = (sz - th) // 2 - bbox[1]
    d.text((tx, ty), text, fill=fg, font=font)

    return img


def main():
    base = render(256)
    base.save(OUT, format="ICO", sizes=[(s, s) for s in SIZES])
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
