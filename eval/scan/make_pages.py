"""Make phone photos of a real-looking shop notebook, for the scan eval (stage 23).

    python eval/scan/make_pages.py

Three pages of an Iraqi shop's debt notebook, written in Ruqaa (Aref Ruqaa, OFL) (the hand most Iraqis write
in) with a shaky baseline, uneven pen pressure and blue/black ballpoint; then "photographed":
the page on a table, at an angle, a shadow across it, a little blur and JPEG noise.
What a shopkeeper actually writes: "25" for 25 thousand, Indic digits, "واصل" for a payment,
a minus sign, dollars, a crossed-out line, a name that comes back on another page, dates.

It is still a font, not a person's hand: the true test is photos of a real notebook.
Writes page_N.jpg + page_N.expected.json next to this file (fixed seed: same pages each time).
"""

import json
import random
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = Path(__file__).resolve().parent
FONT = HERE.parent / "images" / "fonts" / "ArefRuqaa-Regular.ttf"
INDIC = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")

# (text as written, expected row or None for a line that is not a row)
# expected: (name, amount, currency, type, crossed)
PAGES = [
    ("١٢/١٠", [
        ("ابو علي ابو الخضرة    25", ("ابو علي", 25000, "IQD", "debt", False)),
        ("ام حسين    ١٥", ("ام حسين", 15000, "IQD", "debt", False)),
        ("الحجي كريم    150 الف", ("الحجي كريم", 150000, "IQD", "debt", False)),
        ("واصل من سعد    10", ("سعد", 10000, "IQD", "payment", False)),
        ("حيدر التكسي    7500", ("حيدر التكسي", 7500, "IQD", "debt", False)),
        ("ابو زينب    50 $", ("ابو زينب", 50, "USD", "debt", False)),
        ("مصطفى الحلاق    ٣٠", ("مصطفى الحلاق", 30000, "IQD", "debt", True)),
        ("جاسم    -20", ("جاسم", 20000, "IQD", "payment", False)),
        ("ام زهراء    ٨", ("ام زهراء", 8000, "IQD", "debt", False)),
        ("علي الصباغ    45000", ("علي الصباغ", 45000, "IQD", "debt", False)),
    ]),
    ("١٣/١٠", [
        ("ابو مريم    ٢٢", ("ابو مريم", 22000, "IQD", "debt", False)),
        ("سدد ابو علي    15", ("ابو علي", 15000, "IQD", "payment", False)),
        ("كرار    ١٢٥٠٠", ("كرار", 12500, "IQD", "debt", False)),
        ("الحجي كريم    واصل 50", ("الحجي كريم", 50000, "IQD", "payment", False)),
        ("ام حسين    ٦", ("ام حسين", 6000, "IQD", "debt", False)),
        ("منتظر    100 $", ("منتظر", 100, "USD", "debt", False)),
        ("نور الهدى    ١٨", ("نور الهدى", 18000, "IQD", "debt", False)),
        ("ابو حيدر الفيتر    35", ("ابو حيدر الفيتر", 35000, "IQD", "debt", False)),
    ]),
    ("١٤/١٠", [
        ("سعد    ٢٠", ("سعد", 20000, "IQD", "debt", False)),
        ("ام زهراء    واصل ٨", ("ام زهراء", 8000, "IQD", "payment", False)),
        ("رسول    9", ("رسول", 9000, "IQD", "debt", False)),
        ("ابو زينب    -50 $", ("ابو زينب", 50, "USD", "payment", False)),
        ("حسين البقال    ٤٠", ("حسين البقال", 40000, "IQD", "debt", True)),
        ("حسين البقال    ٤٥", ("حسين البقال", 45000, "IQD", "debt", False)),
        ("ضياء    16500", ("ضياء", 16500, "IQD", "debt", False)),
        ("ابو مريم    سدد ١٠", ("ابو مريم", 10000, "IQD", "payment", False)),
        ("زهير    ٧", ("زهير", 7000, "IQD", "debt", False)),
        ("ام علي    ٣٠", ("ام علي", 30000, "IQD", "debt", False)),
        ("عباس    55", ("عباس", 55000, "IQD", "debt", False)),
    ]),
]


def shape(text: str) -> str:
    return text  # raqm joins the letters and lays them right to left


def font(size: int):
    # RAQM: real Arabic shaping (Ruqaa has no presentation-form glyphs for the BASIC trick)
    return ImageFont.truetype(str(FONT), size, layout_engine=ImageFont.Layout.RAQM)


def hand_line(text: str, rnd: random.Random, ink) -> Image.Image:
    """One line as a hand writes it: words at slightly different sizes and heights."""
    words = text.split("    ")  # name part and amount part, far apart like in a notebook
    size = rnd.randint(54, 62)
    pieces = []
    for w in words:
        f = font(size + rnd.randint(-3, 3))
        t = shape(w)
        # an amount like "-20" or "50 $" is written left to right; Arabic words right to left
        direction = "ltr" if w.strip()[0] in "-0123456789$" else "rtl"
        box = f.getbbox(t, direction=direction)
        im = Image.new("RGBA", (box[2] + 20, int(size * 2.4)), (0, 0, 0, 0))
        ImageDraw.Draw(im).text((10, int(size * 0.4)), t, font=f, fill=ink + (rnd.randint(200, 250),), direction=direction)
        im = im.rotate(rnd.uniform(-2.5, 2.5), resample=Image.BICUBIC, expand=True)
        pieces.append(im.crop(im.getbbox()))  # just the ink, so pieces line up on the ruling
    return pieces


def page(date: str, lines, rnd: random.Random) -> Image.Image:
    w, line_h, top = 1200, 118, 230
    img = Image.new("RGB", (w, top + line_h * (len(lines) + 1)), (246, 241, 226))
    noise = np.random.default_rng(rnd.randint(0, 9999)).normal(0, 4, (img.height, img.width, 1))
    img = Image.fromarray(np.clip(np.asarray(img, dtype=float) + noise, 0, 255).astype("uint8"))
    d = ImageDraw.Draw(img)
    for y in range(top - 10, img.height, line_h):
        d.line((30, y, w - 30, y), fill=(160, 185, 220), width=2)
    d.line((w - 120, 0, w - 120, img.height), fill=(220, 130, 130), width=2)
    blue, black = (28, 44, 120), (30, 30, 35)
    d.text((w - 150, 80), shape(date), font=font(66), fill=blue, anchor="ra", direction="rtl")
    for i, (text, exp) in enumerate(lines):
        ink = black if rnd.random() < 0.3 else blue
        name_im, *amount_im = hand_line(text, rnd, ink)
        mid = top + i * line_h + line_h // 2 - 12 + rnd.randint(-5, 5)  # middle of the written line
        img.paste(name_im, (w - 140 - name_im.width + rnd.randint(-10, 10), mid - name_im.height // 2), name_im)
        if amount_im:
            a = amount_im[0]
            img.paste(a, (rnd.randint(90, 200), mid - a.height // 2 + rnd.randint(-6, 6)), a)
        if exp and exp[4]:  # crossed out: a quick stroke through the line
            d.line((80, mid + rnd.randint(-10, 10), w - 160, mid + rnd.randint(-10, 10)), fill=ink, width=5)
    return img


def _coeffs(src, dst):
    """Perspective coefficients for Image.transform (maps dst -> src)."""
    m = []
    for (x, y), (u, v) in zip(dst, src):
        m.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
        m.append([0, 0, 0, x, y, 1, -v * x, -v * y])
    return np.linalg.solve(np.array(m, dtype=float), np.array(src, dtype=float).reshape(8)).tolist()


def photograph(pg: Image.Image, rnd: random.Random) -> Image.Image:
    """The page lying on a table, shot by a phone held a bit crooked, with a shadow."""
    W, H = 1500, int(1500 * pg.height / pg.width * 1.1) + 200
    table = Image.new("RGB", (W, H), (92, 70, 52))
    j = lambda: rnd.randint(-60, 60)  # noqa: E731
    dst = [(150 + j(), 120 + j()), (W - 150 + j(), 140 + j()), (W - 120 + j(), H - 110 + j()), (130 + j(), H - 140 + j())]
    src = [(0, 0), (pg.width, 0), (pg.width, pg.height), (0, pg.height)]
    warped = pg.transform((W, H), Image.PERSPECTIVE, _coeffs(src, dst), Image.BICUBIC)
    mask = Image.new("L", pg.size, 255).transform((W, H), Image.PERSPECTIVE, _coeffs(src, dst), Image.BICUBIC)
    table.paste(warped, (0, 0), mask)
    # a soft shadow (the phone / a hand) from one side, and light falling off at the other
    xs = np.linspace(0, 1, W)[None, :, None]
    ys = np.linspace(0, 1, H)[:, None, None]
    light = 1.0 - 0.38 * np.clip((xs - 0.55) * 2.4 + (ys - 0.5) * 0.6, 0, 1) - 0.12 * ys
    out = np.clip(np.asarray(table, dtype=float) * light, 0, 255).astype("uint8")
    img = Image.fromarray(out).filter(ImageFilter.GaussianBlur(1.1))
    return img.resize((W * 2 // 3, H * 2 // 3), Image.LANCZOS)


def main() -> None:
    rnd = random.Random(2026)
    for n, (date, lines) in enumerate(PAGES, start=1):
        photo = photograph(page(date, lines, rnd), rnd)
        photo.save(HERE / f"page_{n}.jpg", quality=72)
        rows = [{"name": e[0], "amount": e[1], "currency": e[2], "type": e[3], "crossed_out": e[4], "written": t}
                for t, e in lines if e]
        (HERE / f"page_{n}.expected.json").write_text(json.dumps({"date": date, "rows": rows}, ensure_ascii=False, indent=2),
                                                    encoding="utf-8")
        print(f"page_{n}.jpg: {len(rows)} سطر")


if __name__ == "__main__":
    main()
