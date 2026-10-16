#!/usr/bin/env python3
"""Generates synthetic A4 pages in Arabic and Hebrew (300 DPI) to test the RTL text layer.

Outputs in OUTDIR: ar, he (pure RTL) and arm, hem (RTL with Latin text and numbers inline), each as
<name>.png, <name>.pdf (image without text) and <name>_gt.txt (logical reference text).
The text is made up and rendered with DejaVu Sans via Pillow+raqm; it is not a real scan.
Uso: make_rtl_pages.py OUTDIR
"""
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, features

PAGES = {
    "ar": ("ar", [
        "مرحبا بكم في مدينة سلفادور الجميلة",
        "تقع المدينة على ساحل البرازيل الأطلسي",
        "يزورها الناس من جميع أنحاء العالم كل عام",
        "وتشتهر بالموسيقى والطعام والتاريخ العريق",
    ]),
    "he": ("he", [
        "שלום וברוכים הבאים לעיר סלבדור היפה",
        "העיר שוכנת על החוף האטלנטי של ברזיל",
        "אנשים מכל העולם מבקרים בה בכל שנה",
        "היא מפורסמת במוזיקה באוכל ובהיסטוריה",
    ]),
    "arm": ("ar", [
        "العنوان الإلكتروني هو example.com وفي عام 2024",
        "مدينة Salvador تقع في البرازيل وفيها 2900000 نسمة",
        "أرسلها Carlos Silva في 15 أكتوبر",
        "فريق Google حدد موعدا في الساعة 10 صباحا",
    ]),
    "hem": ("he", [
        "הכתובת שלי היא example.com ובשנת 2024",
        "Salvador היא עיר בברזיל ובה 2900000 תושבים",
        "נשלח ב 15 באוקטובר על ידי Maria Silva",
        "הצוות של Google קבע פגישה בשעה 10 בבוקר",
    ]),
}


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    if not features.check("raqm"):
        sys.exit("Pillow without raqm: no Arabic shaping and no RTL direction")
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    font_path = subprocess.run(["fc-match", "-f", "%{file}", "DejaVu Sans:lang=ar"], capture_output=True, text=True, check=True).stdout
    width, height = 2480, 3508
    font = ImageFont.truetype(font_path, 64)
    for name, (lang, lines) in PAGES.items():
        im = Image.new("L", (width, height), 255)
        draw = ImageDraw.Draw(im)
        y = 300
        for line in lines:
            w = draw.textlength(line, font=font, direction="rtl", language=lang)
            draw.text((width - 250 - w, y), line, font=font, fill=0, direction="rtl", language=lang)
            y += 140
        png = out / f"{name}.png"
        im.save(png, dpi=(300, 300))
        (out / f"{name}_gt.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        # -units PixelsPerInch: without it ImageMagick reads 300 as pixels per centimeter and the page comes out at 234 pt
        subprocess.run(["convert", str(png), "-units", "PixelsPerInch", "-density", "300", str(out / f"{name}.pdf")], check=True)
        print(name, png)


if __name__ == "__main__":
    main()
