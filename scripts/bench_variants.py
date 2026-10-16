#!/usr/bin/env python3
"""US-004: compares Tesseract preprocessing/parameter variants on a PDF.
Usage: bench_variants.py file.pdf reference.txt [--pages 1-1] [--variants a,b,c] [--dpi 300]
Metrics: CER (character error rate), WER (word error rate), difflib agreement and mean time per page.
Does not persist extracted text. Temporary images live in --work-dir and are removed at the end."""
import argparse, difflib, os, re, shutil, subprocess, tempfile, time
from pathlib import Path

# name -> (ImageMagick preprocessing or None, tesseract args, uses tessdata_best)
V = {
    "base":      (None, ["--psm", "3"], None),
    "psm4":      (None, ["--psm", "4"], None),
    "psm1":      (None, ["--psm", "1"], None),
    "sauvola":   (None, ["--psm", "3", "-c", "thresholding_method=2"], None),
    "adaptotsu": (None, ["--psm", "3", "-c", "thresholding_method=1"], None),
    "deskew":    (["-deskew", "40%"], ["--psm", "3"], None),
    "normalize": (["-normalize"], ["--psm", "3"], None),
    # grayscale morphology: Erode shrinks light regions (thickens dark text); Dilate does the opposite
    "erode":     (["-morphology", "Erode", "Diamond:1"], ["--psm", "3"], None),
    "dilate":    (["-morphology", "Dilate", "Diamond:1"], ["--psm", "3"], None),
    "clean":     (["-deskew", "40%", "-normalize", "-despeckle"], ["--psm", "3"], None),
    "best":      (None, ["--psm", "3"], True),
    "best+clean": (["-deskew", "40%", "-normalize", "-despeckle"], ["--psm", "3"], True),
    "best+sauvola": (None, ["--psm", "3", "-c", "thresholding_method=2"], True),
    "best+clean+sauvola": (["-deskew", "40%", "-normalize", "-despeckle"], ["--psm", "3", "-c", "thresholding_method=2"], True),
}

def edit_distance(seq1, seq2):
    m, n = len(seq1), len(seq2)
    prev = list(range(n + 1))
    curr = [0] * (n + 1)
    for i in range(1, m + 1):
        curr[0] = i
        s1 = seq1[i - 1]
        for j in range(1, n + 1):
            cost = 0 if s1 == seq2[j - 1] else 1
            curr[j] = min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost)
        prev = curr[:]
    return prev[n]

def normalize_whitespace(text):
    return re.sub(r"\s+", " ", text).strip()

def calculate_cer(ref, hyp):
    r_clean = normalize_whitespace(ref)
    h_clean = normalize_whitespace(hyp)
    if not r_clean: return 0.0 if not h_clean else 1.0
    return edit_distance(list(r_clean), list(h_clean)) / len(r_clean)

def calculate_wer(ref, hyp):
    r_words = normalize_whitespace(ref).split()
    h_words = normalize_whitespace(hyp).split()
    if not r_words: return 0.0 if not h_words else 1.0
    return edit_distance(r_words, h_words) / len(r_words)

def toks(t):
    return re.findall(r"\w+|[^\w\s]", t.lower())

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf"); ap.add_argument("ref")
    ap.add_argument("--pages", default="1-12"); ap.add_argument("--dpi", type=int, default=300)
    ap.add_argument("--variants", default=",".join(V)); ap.add_argument("--langs", default="por+eng")
    ap.add_argument("--best-tessdata-dir", help="Directory with the tessdata_best models")
    ap.add_argument("--work-dir", default=".context/workflow/ralph",
                    help="Parent directory of the temporaries, removed when finished")
    a = ap.parse_args()
    try:
        p1, p2 = (int(x) for x in a.pages.split("-"))
    except ValueError:
        ap.error("--pages deve ter formato inicio-fim")
    if p1 < 1 or p2 < p1 or a.dpi < 1:
        ap.error("Paginas e DPI devem ser positivos; fim >= inicio")
    names = a.variants.split(",")
    if any(name not in V for name in names):
        ap.error("Variante desconhecida; opcoes: " + ",".join(V))
    if any(V[name][2] for name in names) and not a.best_tessdata_dir:
        ap.error("Variantes best exigem --best-tessdata-dir")
    
    ref_raw = Path(a.ref).read_text(encoding="utf-8")
    ref_norm = ref_raw.lower()
    ref_clean = normalize_whitespace(ref_norm)
    ref_t = toks(ref_raw)
    if not ref_t:
        ap.error("Referencia sem tokens")
        
    Path(a.work_dir).mkdir(parents=True, exist_ok=True)
    w = Path(tempfile.mkdtemp(prefix="bench_", dir=a.work_dir))
    env = dict(os.environ, OMP_THREAD_LIMIT="1")
    try:
        for n in range(p1, p2 + 1):
            subprocess.run(["pdftoppm", "-r", str(a.dpi), "-gray", "-f", str(n), "-l", str(n), "-singlefile", a.pdf, str(w / f"p{n:02d}")], check=True)
        print(f"{'variant':20} {'CER':>7} {'WER':>7} {'difflib':>8} {'s/page':>7}  (ref={len(ref_clean)} chars, {len(ref_clean.split())} words, load {os.getloadavg()[0]:.1f})", flush=True)
        for name in names:
            pre, targs, use_best = V[name]
            text, elapsed = [], 0.0
            for n in range(p1, p2 + 1):
                src = w / f"p{n:02d}.pgm"
                t0 = time.perf_counter()
                if pre:
                    dst = w / f"q{n:02d}.pgm"
                    subprocess.run(["convert", str(src), *pre, str(dst)], check=True)
                    src = dst
                cmd = ["tesseract", str(src), "stdout", "-l", a.langs, "--dpi", str(a.dpi), *targs]
                if use_best: cmd += ["--tessdata-dir", a.best_tessdata_dir]
                r = subprocess.run(cmd, capture_output=True, text=True, env=env, check=True)
                elapsed += time.perf_counter() - t0
                text.append(r.stdout)
                if pre: dst.unlink()
            full_hyp = "\n".join(text)
            hyp_norm = full_hyp.lower()
            cer = calculate_cer(ref_norm, hyp_norm)
            wer = calculate_wer(ref_norm, hyp_norm)
            c = difflib.SequenceMatcher(None, ref_t, toks(full_hyp), autojunk=False).ratio()
            print(f"{name:20} {cer:7.4f} {wer:7.4f} {c:8.4f} {elapsed/(p2-p1+1):7.2f}", flush=True)
    finally:
        shutil.rmtree(w)

if __name__ == "__main__":
    main()
