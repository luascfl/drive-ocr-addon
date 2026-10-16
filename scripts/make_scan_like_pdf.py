#!/usr/bin/env python3
"""Generates a 1-page A4 PDF imitating a typewritten scan, with known reference text.

The page is a 600 DPI image (monospaced text, blur, noise, slight rotation and reduced contrast), so it
can be rendered at 300, 200, 150 or 100 DPI and the CER measured against the truth. The text is made up; the document is
synthetic and does NOT represent the variety of real scans (yellowed paper, stamps, handwriting).
Outputs in OUTDIR: scanlike.pdf and scanlike_gt.txt (reference, with line breaks).
Uso: make_scan_like_pdf.py OUTDIR [SEED]
"""
import random
import subprocess
import sys
import textwrap
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

PARAGRAPHS = [
    "Ofício nº 128/2026. Salvador, 14 de outubro de 2026. Assunto: solicitação de cópia autenticada dos autos do processo administrativo referente à reforma do prédio da Secretaria Municipal de Educação.",
    "Senhor Diretor, em atenção ao pedido formulado pela comissão de moradores, informo que a documentação está arquivada na Coordenação de Obras e pode ser consultada em horário comercial, mediante agendamento prévio.",
    "As plantas originais, assinadas pelo engenheiro responsável, encontram-se em mau estado de conservação, razão pela qual a reprodução será feita por digitalização e não por fotocópia, conforme orientação do setor de arquivo.",
    "Foram localizados quarenta e dois volumes, numerados de 1 a 42, além de três caixas com correspondência avulsa dos anos de 1978 a 1984. A relação completa segue anexa, em duas vias, com a descrição sumária de cada peça.",
    "Cumpre esclarecer que alguns documentos contêm informações pessoais e dependem de autorização expressa dos interessados antes de qualquer divulgação, nos termos da legislação vigente sobre acesso e proteção de dados.",
    "Reitero a disposição desta Coordenação para esclarecimentos adicionais e solicito que a resposta seja encaminhada por escrito, com indicação do nome completo e do número de matrícula do servidor que retirar o material.",
    "Anexo I. Relação sumária: volume 1, memorial descritivo e cronograma físico-financeiro; volume 2, planilha de quantitativos e orçamento estimativo; volume 3, pareceres técnicos da Procuradoria e da Secretaria da Fazenda.",
    "Anexo II. Ocorrências: em vistoria realizada na segunda-feira, constatou-se infiltração na cobertura do bloco B, rachaduras nas escadas de acesso ao segundo pavimento e ausência de extintores de incêndio nos corredores.",
    "Anexo III. Providências: contratação emergencial de serviços de impermeabilização, escoramento provisório das escadas e aquisição de equipamentos de combate a incêndio, com prazo de cento e vinte dias para conclusão.",
    "Atenciosamente, Maria das Graças Albuquerque, Coordenadora de Obras e Patrimônio.",
]


def main():
    if len(sys.argv) not in (2, 3):
        sys.exit(__doc__)
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    seed = int(sys.argv[2]) if len(sys.argv) == 3 else 7
    rng = np.random.default_rng(seed)
    random.seed(seed)
    font_path = subprocess.run(["fc-match", "-f", "%{file}", "FreeMono"], capture_output=True, text=True, check=True).stdout
    dpi, width, height = 600, 4960, 7016  # A4
    font = ImageFont.truetype(font_path, round(11 / 72 * dpi))  # 11 pt
    char_w = font.getlength("M")
    cols = int((width - 2 * 550) / char_w)
    lines = []
    for p in PARAGRAPHS:
        lines += textwrap.wrap(p, cols) + [""]
    gt = [ln for ln in lines if ln]
    im = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(im)
    y = 700
    step = round(font.size * 1.55)
    for ln in lines:
        if ln:
            draw.text((550 + random.randint(-3, 3), y), ln, font=font, fill=20)
        y += step
    im = im.rotate(0.35, resample=Image.BICUBIC, fillcolor=255)
    im = im.filter(ImageFilter.GaussianBlur(2.1))
    arr = np.asarray(im).astype(np.float32)
    arr = 255 - (255 - arr) * 0.72  # reduced contrast, gray ink
    arr += rng.normal(0, 20, arr.shape)
    speck = rng.random(arr.shape) < 0.001
    arr[speck] = 40
    im = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
    jpg = out / "scanlike.jpg"
    im.save(jpg, quality=72, dpi=(dpi, dpi))
    (out / "scanlike_gt.txt").write_text("\n".join(gt) + "\n", encoding="utf-8")
    # -units PixelsPerInch: without it ImageMagick reads 600 as pixels per centimeter
    subprocess.run(["convert", str(jpg), "-units", "PixelsPerInch", "-density", str(dpi), str(out / "scanlike.pdf")], check=True)
    jpg.unlink()
    print("lines:", len(gt), "columns:", cols, "characters:", sum(len(x) for x in gt))


if __name__ == "__main__":
    main()
