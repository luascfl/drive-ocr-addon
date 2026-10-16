#!/usr/bin/env python3
"""Computes CER (Character Error Rate), WER (Word Error Rate) and similarity between a reference text and OCR output.
Collapses repeated whitespace and line breaks so OCR paragraph breaks are not penalized.
Usage: accuracy.py ref.txt ocr.txt [--case-sensitive]
"""
import argparse, difflib, re, sys
from pathlib import Path

def edit_distance(seq1, seq2):
    """Computes the exact edit distance (Levenshtein) between two sequences."""
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
    if not r_clean:
        return 0.0 if not h_clean else 1.0
    return edit_distance(list(r_clean), list(h_clean)) / len(r_clean)

def calculate_wer(ref, hyp):
    r_words = normalize_whitespace(ref).split()
    h_words = normalize_whitespace(hyp).split()
    if not r_words:
        return 0.0 if not h_words else 1.0
    return edit_distance(r_words, h_words) / len(r_words)

def main():
    ap = argparse.ArgumentParser(description="Calcula CER e WER entre referencia e OCR.")
    ap.add_argument("ref", help="File with the reference ground-truth text")
    ap.add_argument("ocr", help="File with the OCR output")
    ap.add_argument("--case-sensitive", action="store_true", help="Do not lowercase")
    a = ap.parse_args()

    ref_text = Path(a.ref).read_text(encoding="utf-8")
    ocr_text = Path(a.ocr).read_text(encoding="utf-8")

    if not a.case_sensitive:
        ref_text = ref_text.lower()
        ocr_text = ocr_text.lower()

    cer = calculate_cer(ref_text, ocr_text)
    wer = calculate_wer(ref_text, ocr_text)

    # Tokens for the difflib ratio (historical compatibility)
    tok = lambda t: re.findall(r"\w+|[^\w\s]", t)
    r_tok, o_tok = tok(ref_text), tok(ocr_text)
    ratio = difflib.SequenceMatcher(None, r_tok, o_tok, autojunk=False).ratio()

    print(f"CER: {cer:.4f} ({cer*100:.2f}%)")
    print(f"WER: {wer:.4f} ({wer*100:.2f}%)")
    print(f"Token similarity (difflib): {ratio:.4f}")
    r_clean = normalize_whitespace(ref_text)
    h_clean = normalize_whitespace(ocr_text)
    print(f"Counts: ref_chars={len(r_clean)} ocr_chars={len(h_clean)} ref_words={len(r_clean.split())} ocr_words={len(h_clean.split())}")

if __name__ == "__main__":
    main()
