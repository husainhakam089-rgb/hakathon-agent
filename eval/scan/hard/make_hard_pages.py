"""Harder pages for the scan eval: messier than eval/scan/page_N.jpg.

    python eval/scan/hard/make_hard_pages.py

Same Ruqaa hand and phone photo as make_pages.py, then made rougher, closer to a hurried
shopkeeper: every word warped in waves (no straight strokes), pen pressure that thickens and
thins, words tilted more, the amount written right after the name (not in its own column), a
second crossed-out line written over, and the page shot darker and blurrier.
Still a font, not a person's hand: photos of a real notebook remain the true test.
Writes hard_N.jpg + hard_N.expected.json next to this file (fixed seed).
"""

import json
import random
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from make_pages import font, photograph  # noqa: E402

PAGES = [
    ("١٥/١٠", [
        ("ابو سجاد 20", ("ابو سجاد", 20000, "IQD", "debt", False)),
        ("ام فاطمة ١٢", ("ام فاطمة", 12000, "IQD", "debt", False)),
        ("واصل حمودي 15", ("حمودي", 15000, "IQD", "payment", False)),
        ("الاستاذ رعد ٧٥", ("الاستاذ رعد", 75000, "IQD", "debt", False)),
        ("ابو كرار 30 $", ("ابو كرار", 30, "USD", "debt", False)),
        ("مرتضى ٩", ("مرتضى", 9000, "IQD", "debt", True)),
        ("مرتضى ١٩", ("مرتضى", 19000, "IQD", "debt", False)),
        ("ام سيف -10", ("ام سيف", 10000, "IQD", "payment", False)),
    ]),
    ("١٦/١٠", [
        ("ابو سجاد سدد 20", ("ابو سجاد", 20000, "IQD", "payment", False)),
        ("زينب ٤٥٠٠", ("زينب", 4500, "IQD", "debt", False)),
        ("الحاج عبود 120", ("الحاج عبود", 120000, "IQD", "debt", False)),
        ("محمد الكهربائي ٢٥", ("محمد الكهربائي", 25000, "IQD", "debt", False)),
        ("ام فاطمة واصل ٦", ("ام فاطمة", 6000, "IQD", "payment", False)),
        ("علاوي 14", ("علاوي", 14000, "IQD", "debt", False)),
        ("ابو تبارك ٣٣", ("ابو تبارك", 33000, "IQD", "debt", False)),
    ]),
]


def wave(im: Image.Image, rnd: random.Random) -> Image.Image:
    """Bend the strokes: every row and column shifted by a slow wave, like a hand, not a printer."""
    a = np.asarray(im)
    h, w = a.shape[:2]
    out = np.zeros_like(a)
    ax, ay = rnd.uniform(1.5, 3.5), rnd.uniform(1.5, 3.0)
    fx, fy = rnd.uniform(0.02, 0.05), rnd.uniform(0.03, 0.06)
    px, py = rnd.uniform(0, 6.3), rnd.uniform(0, 6.3)
    ys, xs = np.mgrid[0:h, 0:w]
    sx = np.clip((xs + ax * np.sin(ys * fy + px)).astype(int), 0, w - 1)
    sy = np.clip((ys + ay * np.sin(xs * fx + py)).astype(int), 0, h - 1)
    out = a[sy, sx]
    return Image.fromarray(out)


def word(text: str, size: int, ink, rnd: random.Random) -> Image.Image:
    f = font(size)
    direction = "ltr" if text.strip()[0] in "-0123456789$" else "rtl"
    box = f.getbbox(text, direction=direction)
    im = Image.new("RGBA", (box[2] + 30, int(size * 2.4)), (0, 0, 0, 0))
    ImageDraw.Draw(im).text((15, int(size * 0.4)), text, font=f, fill=ink + (rnd.randint(190, 245),), direction=direction)
    im = wave(im, rnd)
    if rnd.random() < 0.5:  # pressed harder: thicker strokes
        r, g, b, alpha = im.split()
        im = Image.merge("RGBA", (r, g, b, alpha.filter(ImageFilter.MaxFilter(3))))
    im = im.rotate(rnd.uniform(-5, 5), resample=Image.BICUBIC, expand=True)
    return im.crop(im.getbbox())


def page(date: str, lines, rnd: random.Random) -> Image.Image:
    w, line_h, top = 1200, 122, 220
    img = Image.new("RGB", (w, top + line_h * (len(lines) + 1)), (243, 238, 222))
    noise = np.random.default_rng(rnd.randint(0, 9999)).normal(0, 5, (img.height, img.width, 1))
    img = Image.fromarray(np.clip(np.asarray(img, dtype=float) + noise, 0, 255).astype("uint8"))
    d = ImageDraw.Draw(img)
    for y in range(top - 10, img.height, line_h):
        d.line((30, y, w - 30, y), fill=(165, 188, 222), width=2)
    blue, black = (30, 46, 125), (28, 28, 34)
    d.text((w - 120, 70), date, font=font(64), fill=blue, anchor="ra", direction="rtl")
    for i, (text, exp) in enumerate(lines):
        ink = black if rnd.random() < 0.35 else blue
        size = rnd.randint(52, 64)
        # name and amount as one written line, right to left, the amount close after the name
        x = w - 70 + rnd.randint(-15, 15)
        mid = top + i * line_h + line_h // 2 - 10 + rnd.randint(-8, 8)
        for piece in text.split(" "):  # word by word, each one bent and tilted its own way
            piece_im = word(piece, size + rnd.randint(-4, 4), ink, rnd)
            x -= piece_im.width + rnd.randint(10, 36)
            img.paste(piece_im, (x, mid - piece_im.height // 2 + rnd.randint(-7, 7)), piece_im)
        if exp and exp[4]:  # crossed out with two quick strokes
            for _ in range(2):
                d.line((x - 10, mid + rnd.randint(-14, 14), w - 60, mid + rnd.randint(-14, 14)), fill=ink, width=4)
    return img


def main() -> None:
    rnd = random.Random(1510)
    for n, (date, lines) in enumerate(PAGES, start=1):
        photo = photograph(page(date, lines, rnd), rnd)
        dark = np.clip(np.asarray(photo, dtype=float) * 0.82, 0, 255).astype("uint8")  # a dim shop
        Image.fromarray(dark).filter(ImageFilter.GaussianBlur(0.7)).save(HERE / f"hard_{n}.jpg", quality=65)
        rows = [{"name": e[0], "amount": e[1], "currency": e[2], "type": e[3], "crossed_out": e[4], "written": t}
                for t, e in lines if e]
        (HERE / f"hard_{n}.expected.json").write_text(
            json.dumps({"date": date, "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"hard_{n}.jpg: {len(rows)} سطر")


if __name__ == "__main__":
    main()
