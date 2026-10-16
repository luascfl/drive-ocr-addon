#!/usr/bin/env bash
# Local add-on pipeline: pdfinfo -> pdftoppm (per page) -> tesseract -> pdfunite.
# Usage: scripts/ocr_pipeline.sh [-l por+eng] [-r 300] [-o out_dir] [-m metrics.csv] file.pdf
# Persists nothing besides <name>.ocr.pdf, <name>.ocr.txt and the metrics CSV.
set -euo pipefail

LANGS=por+eng; DPI=300; OUT=.; METRICS=""
while getopts "l:r:o:m:" o; do
  case $o in l) LANGS=$OPTARG;; r) DPI=$OPTARG;; o) OUT=$OPTARG;; m) METRICS=$OPTARG;; *) exit 2;; esac
done
shift $((OPTIND-1))
IN=${1:?usage: ocr_pipeline.sh [-l langs] [-r dpi] [-o dir] [-m csv] file.pdf}

info=$(pdfinfo "$IN") || { echo "error: pdfinfo failed (code $?)" >&2; exit 3; }
grep -q '^Encrypted:.*yes' <<<"$info" && { echo "error: encrypted PDF" >&2; exit 4; }
pages=$(awk '/^Pages:/{print $2}' <<<"$info")
base=$(basename "${IN%.*}")
mkdir -p "$OUT"
W=$(mktemp -d); trap 'rm -rf "$W"' EXIT
export OMP_THREAD_LIMIT=${OMP_THREAD_LIMIT:-1}

[ -n "$METRICS" ] && [ ! -s "$METRICS" ] && echo "file,page,pdftoppm_s,pdftoppm_kb,tesseract_s,tesseract_kb" >"$METRICS"
parts=()
for ((n=1; n<=pages; n++)); do
  p=$(printf '%04d' "$n")
  /usr/bin/time -f '%e %M' -o "$W/t1" pdftoppm -r "$DPI" -gray -f "$n" -l "$n" -singlefile "$IN" "$W/p$p"
  /usr/bin/time -f '%e %M' -o "$W/t2" tesseract "$W/p$p.pgm" "$W/o$p" -l "$LANGS" --dpi "$DPI" --psm 3 pdf txt >/dev/null 2>&1
  rm -f "$W/p$p.pgm"
  parts+=("$W/o$p.pdf")
  [ -n "$METRICS" ] && echo "$base,$n,$(cut -d' ' -f1 "$W/t1"),$(cut -d' ' -f2 "$W/t1"),$(cut -d' ' -f1 "$W/t2"),$(cut -d' ' -f2 "$W/t2")" >>"$METRICS"
done

if [ "${#parts[@]}" -eq 1 ]; then cp "${parts[0]}" "$OUT/$base.ocr.pdf"; else pdfunite "${parts[@]}" "$OUT/$base.ocr.pdf"; fi
cat "$W"/o*.txt >"$OUT/$base.ocr.txt"
echo "ok: $OUT/$base.ocr.pdf ($pages pages, langs=$LANGS, dpi=$DPI)"
