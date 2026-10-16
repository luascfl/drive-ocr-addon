#!/usr/bin/env python3
"""Generates synthetic image-only PDFs (no text layer) with ground-truth text in .gt.txt.
Used to validate the pipeline without personal documents. Does NOT replace real PDFs."""
import random, sys, textwrap
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "samples/synthetic")
OUT.mkdir(parents=True, exist_ok=True)
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"
W, H = 2480, 3508  # A4 at 300 DPI

PT = ["A educação à distância exige atenção à acessibilidade dos documentos digitalizados.",
      "Órgãos públicos frequentemente publicam relatórios escaneados sem texto pesquisável.",
      "O reconhecimento óptico de caracteres converte imagens em texto selecionável e indexável.",
      "Ação, coração, informação, não, avó, José, ônibus, família, lição e pretensão.",
      "Salvador, Bahia: orçamento de R$ 1.250,75 aprovado em 03/09/2026 conforme a cláusula 4ª."]
EN = ["Searchable PDF files let readers find words quickly without retyping the document.",
      "Tesseract reads scanned pages and returns plain text together with word positions.",
      "The quick brown fox jumps over the lazy dog while the pipeline measures memory.",
      "Totals: 1,204 pages processed, 98.6 percent accepted on the first attempt.",
      "Privacy matters, so the service deletes every temporary file after processing."]

def page(lines, size=44, noisy=False, angle=0.0):
    img = Image.new("L", (W, H), 255)
    d = ImageDraw.Draw(img)
    f = ImageFont.truetype(FONT, size)
    y = 300
    gt = []
    for ln in lines:
        for chunk in textwrap.wrap(ln, 70):
            d.text((250, y), chunk, font=f, fill=0); gt.append(chunk); y += int(size * 1.7)
        y += size
    if angle: img = img.rotate(angle, expand=False, fillcolor=255)
    if noisy:
        px = img.load(); rnd = random.Random(7)
        for _ in range(60000): px[rnd.randrange(W), rnd.randrange(H)] = rnd.randrange(120, 255)
        img = img.filter(ImageFilter.GaussianBlur(1.2))
    return img, gt

def build(name, pages):
    imgs, gts = [], []
    for kw in pages:
        im, gt = page(**kw); imgs.append(im); gts.append("\n".join(gt))
    imgs[0].save(OUT / f"{name}.pdf", save_all=True, append_images=imgs[1:], resolution=300.0)
    (OUT / f"{name}.gt.txt").write_text("\n".join(gts) + "\n", encoding="utf-8")

build("por_limpo", [dict(lines=PT), dict(lines=PT[::-1])])
build("eng_limpo", [dict(lines=EN), dict(lines=EN[::-1])])
build("misto_ruido", [dict(lines=PT[:3] + EN[:3], noisy=True, angle=0.6)])
print("ok", sorted(p.name for p in OUT.iterdir()))
