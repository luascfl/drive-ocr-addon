#!/usr/bin/env bash
# Opens a PDF in an interactive reader inside an ISOLATED Xvfb (own clipboard and focus, without touching
# the user's session), presses Ctrl+A and Ctrl+C and prints what ended up in the clipboard.
# Usage: reader_copy.sh evince|qpdfview FILE.pdf [CAPTURE.png]
# Requires: Xvfb, xdotool, xclip, the reader and (for the capture) ImageMagick `import`.
set -euo pipefail
reader=${1:?leitor: evince|qpdfview}; pdf=${2:?pdf}; shot=${3:-}
display=:99
Xvfb "$display" -screen 0 1280x1024x24 -nolisten tcp >/dev/null 2>&1 &
xpid=$!
rpid=
cleanup() { [ -n "$rpid" ] && kill "$rpid" 2>/dev/null || true; kill "$xpid" 2>/dev/null || true; wait 2>/dev/null || true; }
trap cleanup EXIT
export DISPLAY=$display
for _ in $(seq 1 20); do xdotool getdisplaygeometry >/dev/null 2>&1 && break; sleep 0.5; done
"$reader" "$pdf" >/dev/null 2>&1 &
rpid=$!
wid=$(timeout 40 xdotool search --sync --onlyvisible --class "$reader" | head -1)
[ -n "$wid" ] || { echo "$reader window did not appear" >&2; exit 2; }
sleep 4                       # the document needs to finish loading and extracting the text
xdotool windowfocus "$wid"; sleep 0.5
xdotool key ctrl+a; sleep 0.7
[ -n "$shot" ] && import -window root "$shot"
xdotool key ctrl+c; sleep 1
xclip -selection clipboard -o
