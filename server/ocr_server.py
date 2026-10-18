#!/usr/bin/env python3
"""Add-on OCR service.

Routes:
- POST /jobs      Authorization: Bearer <user's OAuth token, drive.file scope>; JSON {"fileId", "action"?, "lang"?, "psm"?}.
                  The token is validated (aud = OCR_ALLOWED_AUD, drive.file scope) and the user is identified by the
                  Drive permissionId (only an in-memory HMAC). Queues the job and answers 202 right away.
                  action "replace" (default): downloads the PDF, straightens sideways pages, runs OCR on each intact
                  spread, then splits double-page spreads and REPLACES the file's content, after pinning the original
                  revision (keepForever) and recording it in the file's appProperties. action "revert": restores that
                  revision, only while the file still equals our result. action "flatten": ignores existing text,
                  re-runs OCR on every page and writes a NEW "<name> (flatten).pdf" in the original's folder (it needs
                  confirmed `parents`: a create call without them lands in the root of My Drive, measured in US-002).
                  Per-user limits: 2 active jobs and 30 per 24 h (429).
- GET  /jobs/<id> same header; only the owner sees the job (another user gets 404): queued, running, done or error
                  (with a short code, no document text).
- POST /process   Authorization: Bearer <OCR_TOKEN>; PDF in the body, returns the searchable PDF (synchronous; test only).
- GET  /health

Privacy: the user's token and the PDF exist only in memory and in a temporary directory, deleted when the job ends
(including on error). No file name, ID, e-mail or text goes to the log, only sizes, page counts and timings. Jobs
live only in memory (a restart loses them) and expire after 1 hour.
Environment: OCR_TOKEN (required, /process only), OCR_ALLOWED_AUD (required, the add-on's client ID; several separated by
comma), OCR_HOST (127.0.0.1; the HTTPS proxy sits in front), OCR_PORT (8080), OCR_MAX_MB (50), OCR_MAX_PAGES (100),
OCR_DPI (300), OCR_QUEUE_MAX (5), OCR_USER_ACTIVE_MAX (2), OCR_USER_DAILY_MAX (30), OCR_TOKENINFO_URL.
"""
import collections
import hashlib
import hmac
import json
import os
import queue
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

VERSION = "0.1.4"  # keep equal to the VERSION file at the repository root (a test checks it); the server is deployed as one file
TOKEN = os.environ.get("OCR_TOKEN", "")
PORT = int(os.environ.get("OCR_PORT", "8080"))
HOST = os.environ.get("OCR_HOST", "127.0.0.1")
MAX_BYTES = int(os.environ.get("OCR_MAX_MB", "50")) * 1024 * 1024
MAX_PAGES = int(os.environ.get("OCR_MAX_PAGES", "100"))
DPI = int(os.environ.get("OCR_DPI", "300"))
QUEUE_MAX = int(os.environ.get("OCR_QUEUE_MAX", "5"))
TEXT_MIN_CHARS = int(os.environ.get("OCR_TEXT_MIN_CHARS", "25"))  # a page with this much extractable text already counts as searchable
OSD_MIN_CONF = float(os.environ.get("OCR_OSD_MIN_CONF", "3"))  # Tesseract orientation confidence needed for a page to vote
SPREAD_MIN_ASPECT = float(os.environ.get("OCR_SPREAD_MIN_ASPECT", "1.2"))  # width/height from which a page may be a two-page spread
DRIVE_API = os.environ.get("OCR_DRIVE_API", "https://www.googleapis.com/drive/v3")
DRIVE_UPLOAD = os.environ.get("OCR_DRIVE_UPLOAD", "https://www.googleapis.com/upload/drive/v3")
JOB_TTL = 3600
LANG_RE = re.compile(r"^[a-z]{3}(_[A-Za-z]+)?(\+[a-z]{3}(_[A-Za-z]+)?)*$")
ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,120}$")
BUSY = threading.Semaphore(1)  # one OCR CPU: the worker and /process do not run together
JOBS = {}
JOBS_LOCK = threading.Lock()
QUEUE = queue.Queue(maxsize=QUEUE_MAX)
USER_JOBS = {}  # anonymous user key -> creation times of the user's jobs in the last 24 h
IDENT_CACHE = {}  # sha256(token) -> (user key, expiry); in memory only
KEY_SALT = secrets.token_bytes(32)  # changes on every start: user keys are not comparable across runs
IDENT_TTL = 300
ALLOWED_AUD = {a.strip() for a in os.environ.get("OCR_ALLOWED_AUD", "").split(",") if a.strip()}
TOKENINFO = os.environ.get("OCR_TOKENINFO_URL", "https://oauth2.googleapis.com/tokeninfo")
USER_ACTIVE_MAX = int(os.environ.get("OCR_USER_ACTIVE_MAX", "2"))
USER_DAILY_MAX = int(os.environ.get("OCR_USER_DAILY_MAX", "30"))
REQUIRED_SCOPE = os.environ.get("OCR_REQUIRED_SCOPE", "https://www.googleapis.com/auth/drive.file")


def log(**kw):
    kw["t"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    print(json.dumps(kw, ensure_ascii=False), flush=True)


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


class JobError(Exception):
    """Expected job failure; `code` is what the client sees (short, no document content)."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class PdfRejected(ValueError):
    """The PDF cannot be processed; `code` maps to a short job error code (no document content)."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def overlay_safe(src, pages):
    """The overlay is used only when qpdf exists and every page has a CropBox equal to its MediaBox.
    Rotations 0, 90, 180 and 270 were measured as aligned (0.0 pt deviation against full mode, with a 96 pt negative control).
    A CropBox different from the MediaBox does NOT align (14 pt deviation, even with `pdftoppm -cropbox`), so it falls back."""
    if not shutil.which("qpdf"):
        return False
    r = run(["pdfinfo", "-box", "-f", "1", "-l", str(pages), src])
    if r.returncode != 0:
        return False
    rots = re.findall(r"^Page\s+\d+\s+rot:\s+(-?\d+)", r.stdout, re.M)
    media = re.findall(r"^Page\s+\d+\s+MediaBox:\s+(.+?)\s*$", r.stdout, re.M)
    crop = re.findall(r"^Page\s+\d+\s+CropBox:\s+(.+?)\s*$", r.stdout, re.M)
    return (len(rots) == len(media) == len(crop) == pages and all(x in ("0", "90", "180", "270") for x in rots)
            and media == crop)


def page_count(path):
    r = run(["pdfinfo", path])
    m = re.search(r"^Pages:\s+(\d+)", r.stdout, re.M) if r.returncode == 0 else None
    return int(m.group(1)) if m else None


def pages_with_text(src, pages):
    """Pages that already have extractable text (at least TEXT_MIN_CHARS non-whitespace characters)."""
    found = set()
    for n in range(1, pages + 1):
        r = run(["pdftotext", "-f", str(n), "-l", str(n), src, "-"])
        if r.returncode == 0 and len(re.sub(r"\s", "", r.stdout)) >= TEXT_MIN_CHARS:
            found.add(n)
    return found


PGM_DARK = bytes(1 if v < 128 else 0 for v in range(256))


def read_pgm(path):
    """Reads a binary PGM (P5) written by pdftoppm -gray: returns (width, height, pixel bytes)."""
    with open(path, "rb") as f:
        data = f.read()
    m = re.match(rb"P5\s+(\d+)\s+(\d+)\s+(\d+)\s", data)
    if not m:
        raise ValueError("not a P5 PGM")
    w, h = int(m.group(1)), int(m.group(2))
    return w, h, data[m.end():m.end() + w * h]


def find_gutter(w, h, px):
    """Looks for a vertical gutter in a rendered spread. Returns the cut as a fraction of the page width, or None.

    Two kinds of gutter are accepted, both only near the middle of the page and both continuous along the whole height
    (a column is 'continuous' because its ink fraction is computed over every row):
    - a low-ink gap at least 3% wide, relative to the page's text density, with content on BOTH sides;
    - a dark shadow band (ink fraction >= 50%) with lighter flanks, centered on the band.
    The cut goes through the middle of the detected gap. Nothing reliable found: None (no split)."""
    if w < 50 or h < 50 or len(px) < w * h:
        return None
    ink = [px[x::w][:h].translate(PGM_DARK).count(1) / h for x in range(w)]
    lo, hi = int(w * 0.35), int(w * 0.65)

    def runs(pred, min_run):
        out, start = [], None
        for x in range(w + 1):
            ok = x < w and pred(ink[x])
            if ok and start is None:
                start = x
            elif not ok and start is not None:
                out.append((start, x))
                start = None
        return [r for r in out if r[1] - r[0] >= min_run and r[0] <= hi and r[1] >= lo]

    def side_ink(a, b):
        return sum(ink[a:b]) / (b - a) if b > a else 0.0

    # 1) dark shadow band (at least 1% wide, 50%+ ink along the whole height): this is the real binding, so it wins
    flank = max(3, int(w * 0.03))
    for a, b in sorted(runs(lambda v: v >= 0.5, max(2, int(w * 0.01))), key=lambda r: r[0] - r[1]):
        if a > flank and b + flank < w and side_ink(a - flank, a) < 0.3 and side_ink(b, b + flank) < 0.3 \
                and side_ink(0, a) >= 0.004 and side_ink(b, w) >= 0.004:
            return (a + b) / 2 / w
    # 2) quiet gap of at least 3% of the width (a word space is about 1%, a pair of page margins is much wider).
    # Real scans are never pure white (paper grain, a soft shadow near the fold: 1-3% of the pixels in a column), so
    # "quiet" is relative to the page's own text: under 30% of the median text-column ink (at most 6%), measured on a
    # moving average a few columns wide so that one stray speck does not split the gap in two.
    k = max(3, int(w * 0.01)) | 1
    smooth = [sum(ink[max(0, x - k // 2):x + k // 2 + 1]) / len(ink[max(0, x - k // 2):x + k // 2 + 1]) for x in range(w)]
    body = sorted(v for v in smooth[int(w * 0.05):int(w * 0.95)] if v >= 0.04)
    if not body:
        return None
    quiet = max(0.002, min(0.06, 0.3 * body[len(body) // 2]))
    gaps = []
    start = None
    for x in range(w + 1):
        ok = x < w and smooth[x] <= quiet
        if ok and start is None:
            start = x
        elif not ok and start is not None:
            gaps.append((start, x))
            start = None
    gaps = [r for r in gaps if r[1] - r[0] >= max(3, int(w * 0.03)) and r[0] <= hi and r[1] >= lo and r[0] > 0 and r[1] < w
            and side_ink(0, r[0]) >= 0.004 and side_ink(r[1], w) >= 0.004]
    if gaps:
        a, b = max(gaps, key=lambda r: r[1] - r[0])
        return (a + b) / 2 / w
    return None


SPREAD_CONSENSUS_MIN = 3  # landscape pages with a detected gutter needed before the others follow them
SPREAD_CONSENSUS_TOL = 0.08  # a cut this far (fraction of the width) from the median is treated as a detection error


def settle_cuts(cuts, landscape):
    """Uses the document as a whole to correct the per-page gutter guesses. `cuts` maps a 0-based page to its cut and
    `landscape` lists the pages that may be spreads. When at least three and at least half of the landscape pages
    found a gutter, missing and outlying cuts use the median position; cuts within 8% of it stay unchanged.
    This assumes a uniform book scan, including chapter openings and sparse facing pages. Mixed-layout documents
    whose landscape pages mostly resemble spreads can therefore be split incorrectly."""
    if len(cuts) < SPREAD_CONSENSUS_MIN or len(cuts) * 2 < len(landscape):
        return cuts
    ordered = sorted(cuts.values())
    median = ordered[len(ordered) // 2]
    return {i: (cuts[i] if i in cuts and abs(cuts[i] - median) <= SPREAD_CONSENSUS_TOL else median) for i in landscape}


def sample_pages(pages, k=5):
    """Up to k page numbers (1-based) spread evenly over the document."""
    if pages <= k:
        return list(range(1, pages + 1))
    return sorted({1 + round(i * (pages - 1) / (k - 1)) for i in range(k)})


def detect_rotation(src, work, pages):
    """Clockwise rotation (0, 90, 180 or 270) that makes most of the document upright, by a vote of Tesseract's
    orientation detection (OSD) over a few sampled pages. Pages where OSD is unsure (blank, few characters, low
    confidence) do not vote. A rotation needs a strict majority of the confident votes."""
    votes = []
    env = dict(os.environ, OMP_THREAD_LIMIT="1")
    for n in sample_pages(pages):
        base = os.path.join(work, f"osd{n:04d}")
        if run(["pdftoppm", "-r", "150", "-gray", "-f", str(n), "-l", str(n), "-singlefile", src, base]).returncode != 0:
            continue
        r = run(["tesseract", base + ".pgm", "stdout", "--psm", "0"], env=env)
        os.remove(base + ".pgm")
        rot = re.search(r"^Rotate:\s+(\d+)", r.stdout, re.M)
        conf = re.search(r"^Orientation confidence:\s+([\d.]+)", r.stdout, re.M)
        if rot and conf and float(conf.group(1)) >= OSD_MIN_CONF:
            votes.append(int(rot.group(1)) % 360)
    if not votes:
        return 0
    rot, count = collections.Counter(votes).most_common(1)[0]
    return rot if rot and count * 2 > len(votes) else 0


def page_sizes(path, pages):
    """Displayed (width, height) in points per page, after /Rotate."""
    r = run(["pdfinfo", "-f", "1", "-l", str(pages), path])
    return [(float(a), float(b)) for a, b in re.findall(r"^Page\s+\d+ size:\s+([\d.]+) x ([\d.]+)", r.stdout, re.M)]


def rewrite_pages(src, dst, cuts):
    """One pass with pypdf: bakes any /Rotate into the content and splits the pages in `cuts` ({0-based page:
    fraction of the width}) by cropping, with no re-rasterizing and no copy of the images.

    Both halves are clones of the SAME page, so they share its content and its images; each one only gets its own
    mediabox/cropbox (the right half's box starts at x > 0). The content is never translated, which is what keeps the
    file small: translating needs one reader per half, which copied the images (measured: 39,747 B became 84,814 B).
    When used after OCR, this clips the scan and its existing text layer in the same coordinate system."""
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import RectangleObject
    reader, writer = PdfReader(src), PdfWriter()
    for i, page in enumerate(reader.pages):
        if page.rotation % 360:
            page.transfer_rotation_to_content()
        if i not in cuts:
            writer.add_page(page)
            continue
        box = page.cropbox
        x0, y0, x1, y1 = (float(v) for v in (box.left, box.bottom, box.right, box.top))
        cut = x0 + (x1 - x0) * cuts[i]
        for left, right in ((x0, cut), (cut, x1)):
            added = writer.add_page(page)
            rect = RectangleObject((left, y0, right, y1))
            for name in ("mediabox", "cropbox", "trimbox", "bleedbox", "artbox"):
                setattr(added, name, rect)
    with open(dst, "wb") as fh:
        writer.write(fh)


def prepare_pdf(src, work, pages, info):
    """Prepares orientation and finds double-page cuts before OCR.

    Returns the oriented PDF and its requested cuts. The cuts are deliberately applied only after OCR: text and image
    then share one coordinate system before both halves are cropped. Optional preparation failures are logged and
    skipped."""
    info.update(rotated=0, split=0)
    cur = src
    try:
        rot = detect_rotation(src, work, pages)
    except Exception as e:  # noqa: BLE001
        log(ev="prep_error", step="rotation", type=type(e).__name__)
        rot = 0
    if rot:
        rotated = os.path.join(work, "rotated.pdf")
        r = run(["qpdf", "--rotate=+%d" % rot, src, "--", rotated])
        if r.returncode in (0, 3) and os.path.exists(rotated) and page_count(rotated) == pages:
            cur, info["rotated"] = rotated, rot
    cuts, landscape = {}, []
    try:
        for i, (w, h) in enumerate(page_sizes(cur, pages)):
            if w / h < SPREAD_MIN_ASPECT:
                continue  # only landscape pages can be two pages side by side
            landscape.append(i)
            base = os.path.join(work, f"gut{i:04d}")
            if run(["pdftoppm", "-r", "60", "-gray", "-f", str(i + 1), "-l", str(i + 1), "-singlefile", cur, base]).returncode != 0:
                continue
            cut = find_gutter(*read_pgm(base + ".pgm"))
            os.remove(base + ".pgm")
            if cut is not None:
                cuts[i] = cut
        cuts = settle_cuts(cuts, landscape)
    except Exception as e:  # noqa: BLE001
        log(ev="prep_error", step="gutter", type=type(e).__name__)
        cuts = {}
    return cur, cuts

def ocr_pdf(src, work, langs, dpi, psm=None, overlay=True, action="auto", prep_info=None):
    """Returns (PDF path, pages, per-OCR'd-page timings, mode, skipped pages).

    mode 'overlay': Tesseract produces only the text layer (textonly_pdf) and qpdf overlays it on the ORIGINAL PDF,
    which keeps its images (output ~1% larger than the input, versus 2.7x for full mode). Pages that already have text
    are left as they are (no duplicate layer) and cost no OCR.
    mode 'full': Tesseract rewrites the image at 300 DPI into the PDF (heavier, works for any PDF; the old text
    is discarded and every page goes through OCR). It is the fallback when the overlay is not safe or fails.
    A PDF with text on EVERY page is refused (PdfRejected 'already_searchable')."""
    info = run(["pdfinfo", src])
    if info.returncode != 0:
        raise PdfRejected("invalid_pdf", "invalid PDF")
    if re.search(r"^Encrypted:\s+yes", info.stdout, re.M):
        raise PdfRejected("invalid_pdf", "PDF is encrypted")
    pages = int(re.search(r"^Pages:\s+(\d+)", info.stdout, re.M).group(1))
    if pages > MAX_PAGES:
        raise PdfRejected("too_many_pages", f"PDF has {pages} pages; the limit is {MAX_PAGES}")
    has_text = pages_with_text(src, pages) if action != "flatten" else set()
    if action != "flatten" and len(has_text) == pages:
        raise PdfRejected("already_searchable", "PDF already has text on every page")
    cuts = {}
    if prep_info is not None and action == "replace":
        # Refuse cheaply first; OCR the intact, upright spread; crop the image and text layer together afterwards.
        prepared, cuts = prepare_pdf(src, work, pages, prep_info)
        if prepared != src:
            src = prepared
            has_text = pages_with_text(src, pages)
    use_overlay = action != "flatten" and overlay and overlay_safe(src, pages)
    if action == "replace" and not use_overlay:
        raise PdfRejected("overlay_unavailable", "the text layer cannot be added to this PDF without re-encoding its pages")
    todo = [n for n in range(1, pages + 1) if not (use_overlay and n in has_text)]
    skipped = pages - len(todo)
    parts, per_page = [], []
    env = dict(os.environ, OMP_THREAD_LIMIT="1")
    for n in todo:
        base = os.path.join(work, f"p{n:04d}")
        t0 = time.time()
        r = run(["pdftoppm", "-r", str(dpi), "-gray", "-f", str(n), "-l", str(n), "-singlefile", src, base])
        if r.returncode != 0:
            raise RuntimeError("pdftoppm failed")
        t1 = time.time()
        cmd = ["tesseract", base + ".pgm", base, "-l", langs, "--dpi", str(dpi), "--psm", psm or "3"]
        r = run(cmd + (["-c", "textonly_pdf=1"] if use_overlay else []) + ["pdf"], env=env)
        if r.returncode != 0 or not os.path.exists(base + ".pdf"):
            raise RuntimeError("tesseract failed")
        os.remove(base + ".pgm")
        parts.append(base + ".pdf")
        per_page.append({"render_s": round(t1 - t0, 2), "ocr_s": round(time.time() - t1, 2)})
    merged = os.path.join(work, "text.pdf" if use_overlay else "out.pdf")
    if len(parts) == 1:
        shutil.copy(parts[0], merged)
    else:
        r = run(["pdfunite", *parts, merged])
        if r.returncode != 0:
            raise RuntimeError("pdfunite failed")
    if not use_overlay:
        return merged, pages, per_page, "full", 0
    out = os.path.join(work, "out.pdf")
    # Page i of the text layer goes to page todo[i] of the original. Split only after this overlay so its coordinates
    # and the scan are cropped together.
    r = run(["qpdf", src, "--overlay", merged, f"--from=1-{len(todo)}", "--to=" + ",".join(map(str, todo)), "--", out])
    if r.returncode not in (0, 3) or not os.path.exists(out) or page_count(out) != pages:  # 3 = warnings, output generated
        log(ev="overlay_fallback", pages=pages)
        if action == "replace":
            raise PdfRejected("overlay_unavailable", "adding the text layer failed")
        return ocr_pdf(src, work, langs, dpi, psm, overlay=False, action=action)
    if cuts:
        split = os.path.join(work, "split.pdf")
        try:
            rewrite_pages(out, split, cuts)
            if page_count(split) != pages + len(cuts):
                raise RuntimeError("unexpected page count")
        except Exception as e:  # noqa: BLE001
            log(ev="prep_error", step="split", type=type(e).__name__)
        else:
            out, pages, prep_info["split"] = split, pages + len(cuts), len(cuts)
    return out, pages, per_page, "overlay", skipped


# ---- Drive ----

def _drive(url, token, *, method="GET", data=None, headers=None, timeout=120):
    req = urllib.request.Request(url, data=data, method=method, headers={"Authorization": "Bearer " + token, **(headers or {})})
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        e.read()
        if e.code == 401:
            raise JobError("drive_auth")
        if e.code == 403:
            raise JobError("drive_forbidden")  # no permission (for example, no edit access to the file)
        if e.code == 404:
            raise JobError("drive_not_found")  # Drive hides files without access behind a 404
        raise JobError("drive_error")
    except (urllib.error.URLError, TimeoutError, OSError):
        raise JobError("drive_unreachable")


def drive_meta(file_id, token):
    url = f"{DRIVE_API}/files/{quote(file_id)}?fields=id,name,parents,size,mimeType,md5Checksum,headRevisionId,appProperties&supportsAllDrives=true"
    with _drive(url, token) as r:
        return json.loads(r.read())


def drive_download(file_id, token, dest, revision=None):
    path = f"/files/{quote(file_id)}/revisions/{quote(revision)}" if revision else f"/files/{quote(file_id)}"
    url = f"{DRIVE_API}{path}?alt=media&supportsAllDrives=true"
    total = 0
    with _drive(url, token, timeout=300) as r, open(dest, "wb") as f:
        while True:
            chunk = r.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_BYTES:
                raise JobError("too_large")
            f.write(chunk)
    return total


def drive_upload(name, parents, path, token):
    boundary = secrets.token_hex(16)
    meta = json.dumps({"name": name, "parents": parents, "mimeType": "application/pdf"}).encode()
    with open(path, "rb") as f:
        pdf = f.read()
    body = (b"--" + boundary.encode() + b"\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n" + meta + b"\r\n--" + boundary.encode()
            + b"\r\nContent-Type: application/pdf\r\n\r\n" + pdf + b"\r\n--" + boundary.encode() + b"--")
    url = f"{DRIVE_UPLOAD}/files?uploadType=multipart&fields=id,parents&supportsAllDrives=true"
    with _drive(url, token, method="POST", data=body, headers={"Content-Type": "multipart/related; boundary=" + boundary}, timeout=300) as r:
        return json.loads(r.read()), len(pdf)


def drive_replace(file_id, path, token, app_properties):
    """Replaces the content of an existing file (same id, name and parents) and sets its appProperties (None deletes
    a key). One multipart call, so content and properties change together."""
    with open(path, "rb") as f:
        pdf = f.read()
    boundary = secrets.token_hex(16)
    meta = json.dumps({"appProperties": app_properties}).encode()
    body = (b"--" + boundary.encode() + b"\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n" + meta + b"\r\n--" + boundary.encode()
            + b"\r\nContent-Type: application/pdf\r\n\r\n" + pdf + b"\r\n--" + boundary.encode() + b"--")
    url = f"{DRIVE_UPLOAD}/files/{quote(file_id)}?uploadType=multipart&fields=id,headRevisionId,md5Checksum&supportsAllDrives=true"
    with _drive(url, token, method="PATCH", data=body, headers={"Content-Type": "multipart/related; boundary=" + boundary}, timeout=300) as r:
        return json.loads(r.read()), len(pdf)


def drive_revision_pin(file_id, revision, token, keep):
    """Marks a revision keepForever (or not). Needed so the original outlives Drive's purge of old versions."""
    url = f"{DRIVE_API}/files/{quote(file_id)}/revisions/{quote(revision)}?fields=id,keepForever"
    with _drive(url, token, method="PATCH", data=json.dumps({"keepForever": bool(keep)}).encode(),
                headers={"Content-Type": "application/json"}) as r:
        return json.loads(r.read())


class AuthError(Exception):
    def __init__(self, http):
        super().__init__(http)
        self.http = http


def identify(token):
    """Validates the user's OAuth token and returns an anonymous user key.

    1. tokeninfo: the `aud` must be the add-on's client ID (OCR_ALLOWED_AUD) and the scope must include drive.file,
       so only someone holding a token issued for our add-on gets in. That response carries no user or e-mail.
    2. Drive about.get (allowed with drive.file): `permissionId` identifies the user. It becomes an HMAC with a
       random key that exists only in process memory; nothing that identifies the person is stored or logged.
    The result is cached (in memory only) for up to 5 min or until the token expires.
    """
    h = hashlib.sha256(token.encode()).hexdigest()
    now = time.time()
    with JOBS_LOCK:
        hit = IDENT_CACHE.get(h)
    if hit and hit[1] > now:
        return hit[0]
    try:
        with urllib.request.urlopen(f"{TOKENINFO}?access_token={quote(token)}", timeout=10) as r:
            info = json.loads(r.read())
    except urllib.error.HTTPError:
        raise AuthError(401)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise AuthError(503)
    scopes = str(info.get("scope", "")).split()
    try:
        left = int(info.get("expires_in", 0))
    except (TypeError, ValueError):
        left = 0
    if info.get("aud") not in ALLOWED_AUD or REQUIRED_SCOPE not in scopes or left <= 0:
        # the client ID (aud) is public and is used to configure OCR_ALLOWED_AUD; never log the token or the user
        log(ev="auth_rejected", aud=str(info.get("aud"))[:100], scopes=[s[-40:] for s in scopes][:8], expired=left <= 0)
        raise AuthError(401)
    try:
        with _drive(f"{DRIVE_API}/about?fields=user(permissionId)", token, timeout=10) as r:
            pid = json.loads(r.read())["user"]["permissionId"]
    except JobError as e:
        raise AuthError(401 if e.code in ("drive_auth", "drive_forbidden") else 503)
    except (KeyError, TypeError, ValueError):
        raise AuthError(401)
    key = hmac.new(KEY_SALT, str(pid).encode(), "sha256").hexdigest()[:16]
    with JOBS_LOCK:
        IDENT_CACHE[h] = (key, now + min(IDENT_TTL, left))
    return key


def revert_job(job, meta, token, work):
    """Restores the revision that the OCR replaced. Only when the file is still exactly our result (its md5 equals the
    `ocrMd5` we stored in the file's appProperties): an edit made after the OCR is never thrown away."""
    props = meta.get("appProperties") or {}
    prev = props.get("ocrPrevRev")
    if not prev:
        raise JobError("nothing_to_revert")
    if props.get("ocrMd5") != meta.get("md5Checksum"):
        raise JobError("file_changed")
    dest = os.path.join(work, "prev.pdf")
    t0 = time.time()
    try:
        job["bytes_in"] = drive_download(job["_file_id"], token, dest, revision=prev)
    except JobError as e:
        raise JobError("revision_unavailable" if e.code in ("drive_not_found", "drive_error") else e.code) from e
    job["download_s"] = round(time.time() - t0, 2)
    with open(dest, "rb") as f:
        if f.read(5) != b"%PDF-":
            raise JobError("revision_unavailable")
    cleared = {"ocrPrevRev": None, "ocrMd5": None, "ocrAt": None}  # null removes the property
    t1 = time.time()
    restored, size_out = drive_replace(job["_file_id"], dest, token, cleared)
    if restored.get("id") != job["_file_id"]:
        raise JobError("replace_failed")
    try:
        drive_revision_pin(job["_file_id"], prev, token, False)  # the original is the head again: free the pin
    except JobError:
        pass
    job["upload_s"] = round(time.time() - t1, 2)
    job["bytes_out"] = size_out
    job["result_id"] = restored["id"]


def process_job(job):
    """Runs a whole job. The token lives only in `job['_token']` and is cleared by the caller.

    action 'replace' (default): the searchable PDF replaces the original file's content (same file id). Before that the
    original revision is pinned (keepForever) and recorded in the file's appProperties, so action 'revert' can restore it.
    It only runs when the text layer can be overlaid on the original, because the full mode re-encodes pages in
    grayscale and would degrade the original.
    action 'revert': restores the pinned original revision (see revert_job).
    action 'flatten': ignores any text the PDF already has, rasterizes every page, runs OCR again and writes a NEW file
    "<name> (flatten).pdf" next to the original, so an existing OCR can be compared with ours."""
    token = job["_token"]
    action = job["action"]
    work = tempfile.mkdtemp(prefix="job_")
    try:
        meta = drive_meta(job["_file_id"], token)
        if meta.get("mimeType") != "application/pdf":
            raise JobError("not_pdf")
        if int(meta.get("size", 0) or 0) > MAX_BYTES:
            raise JobError("too_large")
        if action == "revert":
            revert_job(job, meta, token, work)
            return
        parents = meta.get("parents")
        if action == "flatten" and not parents:
            raise JobError("no_parents")  # a new file without `parents` would land in the root of My Drive
        src = os.path.join(work, "in.pdf")
        t_dl = time.time()
        job["bytes_in"] = drive_download(job["_file_id"], token, src)
        job["download_s"] = round(time.time() - t_dl, 2)
        t_ocr = time.time()
        prep = {}
        try:
            out, pages, per_page, mode, skipped = ocr_pdf(src, work, job["lang"], DPI, job.get("psm"), action=action, prep_info=prep)
        except ValueError as e:
            code = e.code if isinstance(e, PdfRejected) else "internal"
            raise JobError(code) from e
        job["pages"], job["mode"], job["skipped"] = pages, mode, skipped
        job["rotated"], job["split"] = prep.get("rotated", 0), prep.get("split", 0)
        job["ocr_s"] = round(time.time() - t_ocr, 2)
        t_up = time.time()
        if action == "replace":
            fresh = drive_meta(job["_file_id"], token)
            if fresh.get("md5Checksum") != meta.get("md5Checksum"):
                raise JobError("file_changed")  # someone edited the file while it was being processed
            prev_rev = fresh.get("headRevisionId")
            if not prev_rev:
                raise JobError("revision_unavailable")
            try:
                # pin the original revision so Revert still works after Drive's ~30-day purge of old versions
                drive_revision_pin(job["_file_id"], prev_rev, token, True)
            except JobError as e:
                raise JobError("revision_unavailable") from e  # never replace what cannot be restored
            with open(out, "rb") as f:
                new_md5 = hashlib.md5(f.read()).hexdigest()
            props = {"ocrPrevRev": prev_rev, "ocrMd5": new_md5, "ocrAt": time.strftime("%Y-%m-%d")}
            try:
                replaced, size_out = drive_replace(job["_file_id"], out, token, props)
            except JobError:
                try:
                    drive_revision_pin(job["_file_id"], prev_rev, token, False)
                except JobError:
                    pass
                raise
            if replaced.get("id") != job["_file_id"]:
                raise JobError("replace_failed")
            old_prev = (fresh.get("appProperties") or {}).get("ocrPrevRev")
            if old_prev and old_prev != prev_rev:  # an earlier OCR of this file left its original pinned
                try:
                    drive_revision_pin(job["_file_id"], old_prev, token, False)
                except JobError:
                    pass
            job["result_id"] = replaced["id"]
        else:
            base = re.sub(r"\.pdf$", "", meta.get("name") or "document", flags=re.I)
            created, size_out = drive_upload(base + " (flatten).pdf", parents, out, token)
            if created.get("parents") != parents:
                raise JobError("created_elsewhere")
            job["result_id"] = created["id"]
        job["upload_s"] = round(time.time() - t_up, 2)
        job["bytes_out"] = size_out
    finally:
        shutil.rmtree(work, ignore_errors=True)


def worker():
    while True:
        job = QUEUE.get()
        with BUSY:
            job["status"], job["started"] = "running", time.time()
            try:
                process_job(job)
                job["status"] = "done"
            except JobError as e:
                job["status"], job["error"] = "error", e.code
            except Exception as e:  # noqa: BLE001
                job["status"], job["error"] = "error", "internal"
                log(ev="job_internal_error", type=type(e).__name__)
            finally:
                job["finished"] = time.time()
                job["_token"] = None  # the user's token does not outlive the job
        log(ev="job", status=job["status"], error=job.get("error"), pages=job.get("pages"), skipped=job.get("skipped"), mode=job.get("mode"), bytes_in=job.get("bytes_in"),
            bytes_out=job.get("bytes_out"), download_s=job.get("download_s"), ocr_s=job.get("ocr_s"), upload_s=job.get("upload_s"),
            wait_s=round(job["started"] - job["created"], 2), total_s=round(job["finished"] - job["created"], 2))
        QUEUE.task_done()


def start_worker():
    threading.Thread(target=worker, daemon=True).start()


def prune_jobs():
    now = time.time()
    with JOBS_LOCK:
        for jid in [j for j, v in JOBS.items() if now - v["created"] > JOB_TTL and v["status"] in ("done", "error")]:
            del JOBS[jid]
        for u in [u for u, ts in USER_JOBS.items() if not any(now - t < 86400 for t in ts)]:
            del USER_JOBS[u]
        for h in [h for h, (_, exp) in IDENT_CACHE.items() if exp <= now]:
            del IDENT_CACHE[h]


def public_state(job):
    keys = ("status", "error", "action", "pages", "skipped", "rotated", "split", "mode", "result_id", "bytes_in", "bytes_out", "download_s", "ocr_s", "upload_s")
    out = {k: job[k] for k in keys if k in job}
    now = time.time()
    out["age_s"] = round(now - job["created"], 1)
    if job.get("started"):
        out["run_s"] = round((job.get("finished") or now) - job["started"], 1)
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "ocr/" + VERSION

    def log_message(self, *a):  # no default access log: avoids IP and path on disk
        pass

    def _send(self, code, body=b"", ctype="application/json", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj, extra=None):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), extra=extra)

    def _authorized(self):
        got = self.headers.get("Authorization", "")
        return bool(TOKEN) and hmac.compare_digest(got, "Bearer " + TOKEN)

    def _user(self):
        """Identifies the user by the OAuth token in the header; sends the error and returns None on failure."""
        got = self.headers.get("Authorization", "")
        if not got.startswith("Bearer ") or not (20 <= len(got) - 7 <= 4096):
            self._json(401, {"error": "unauthorized"})
            return None
        try:
            return identify(got[7:]), got[7:]
        except AuthError as e:
            self._json(e.http, {"error": "unauthorized" if e.http == 401 else "token verification unavailable"})
            return None

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            return self._json(200, {"ok": True, "version": VERSION, "busy": BUSY._value == 0, "queued": QUEUE.qsize()})
        m = re.fullmatch(r"/jobs/([A-Za-z0-9_-]{6,40})", path)
        if m:
            who = self._user()
            if not who:
                return
            prune_jobs()
            with JOBS_LOCK:
                job = JOBS.get(m.group(1))
            if not job or job["_user"] != who[0]:  # another user's job is indistinguishable from a nonexistent one
                return self._json(404, {"error": "unknown or expired job"})
            return self._json(200, public_state(job))
        self._json(404, {"error": "not found"})

    def _read_body(self, limit):
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            size = 0
        if size <= 0:
            return None, (411, {"error": "Content-Length required"})
        if size > limit:
            return None, (413, {"error": f"body larger than {limit // 1024} KB"})
        return size, None

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/jobs":
            return self._post_job()
        if u.path != "/process":
            return self._json(404, {"error": "not found"})
        if not self._authorized():
            return self._json(401, {"error": "unauthorized"})
        size, err = self._read_body(MAX_BYTES)
        if err:
            return self._json(*err)
        q = parse_qs(u.query)
        langs = (q.get("lang", ["por+eng"])[0]).replace(" ", "+")
        if not LANG_RE.match(langs):
            return self._json(400, {"error": "invalid lang parameter"})
        psm = q.get("psm", [None])[0]
        if psm is not None and psm not in {"3", "4", "6"}:
            return self._json(400, {"error": "invalid psm"})
        if not BUSY.acquire(blocking=False):
            return self._json(503, {"error": "busy, try again shortly"}, extra={"Retry-After": "30"})
        work = tempfile.mkdtemp(prefix="ocr_")
        t0 = time.time()
        try:
            src = os.path.join(work, "in.pdf")
            remaining = size
            with open(src, "wb") as f:
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
            if remaining:
                return self._json(400, {"error": "incomplete body"})
            out, pages, per_page, mode, skipped = ocr_pdf(src, work, langs, DPI, psm)
            data = open(out, "rb").read()
            total = round(time.time() - t0, 2)
            log(ev="ok", bytes_in=size, bytes_out=len(data), pages=pages, skipped=skipped, mode=mode, langs=langs, total_s=total,
                ocr_s=round(sum(p["ocr_s"] for p in per_page), 2))
            self._send(200, data, "application/pdf", {"X-OCR-Pages": str(pages), "X-OCR-Seconds": str(total)})
        except ValueError as e:
            log(ev="rejected", reason=str(e)[:80])
            self._json(422, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            log(ev="error", type=type(e).__name__)
            self._json(500, {"error": "processing failed"})
        finally:
            shutil.rmtree(work, ignore_errors=True)
            BUSY.release()

    def _post_job(self):
        who = self._user()
        if not who:
            return
        user, utoken = who
        size, err = self._read_body(8192)
        if err:
            return self._json(*err)
        try:
            req = json.loads(self.rfile.read(size))
            file_id = req["fileId"]
            langs = str(req.get("lang", "por+eng")).replace(" ", "+")
            psm = req.get("psm")
            action = req.get("action", "replace")
        except (ValueError, KeyError, TypeError):
            return self._json(400, {"error": "invalid JSON: fileId is required"})
        if not isinstance(file_id, str) or not ID_RE.match(file_id):
            return self._json(400, {"error": "invalid fileId"})
        if not LANG_RE.match(langs) or (psm is not None and str(psm) not in {"3", "4", "6"}):
            return self._json(400, {"error": "invalid lang or psm"})
        if action not in ("replace", "flatten", "revert"):
            return self._json(400, {"error": "invalid action: use replace, flatten or revert"})
        now = time.time()
        job = {"id": secrets.token_urlsafe(9), "status": "queued", "created": now, "lang": langs, "action": action,
               "psm": str(psm) if psm is not None else None, "_file_id": file_id, "_token": utoken, "_user": user}
        prune_jobs()
        with JOBS_LOCK:
            recent = [t for t in USER_JOBS.get(user, []) if now - t < 86400]
            active = sum(1 for j in JOBS.values() if j["_user"] == user and j["status"] in ("queued", "running"))
            if active >= USER_ACTIVE_MAX or len(recent) >= USER_DAILY_MAX:
                USER_JOBS[user] = recent
                return self._json(429, {"error": "usage limit reached, try again later"}, extra={"Retry-After": "300"})
            USER_JOBS[user] = recent + [now]
            JOBS[job["id"]] = job
        try:
            QUEUE.put_nowait(job)
        except queue.Full:
            with JOBS_LOCK:
                JOBS.pop(job["id"], None)
                USER_JOBS[user].remove(now)
            return self._json(503, {"error": "queue is full, try again shortly"}, extra={"Retry-After": "60"})
        self._json(202, {"jobId": job["id"], "status": "queued", "queued": QUEUE.qsize()})


def main():
    if not TOKEN or len(TOKEN) < 24:
        sys.exit("set OCR_TOKEN to at least 24 characters (it protects only /process, for testing)")
    if not ALLOWED_AUD:
        sys.exit("set OCR_ALLOWED_AUD to the add-on project's client ID (the aud field from tokeninfo)")
    log(ev="start", port=PORT, max_mb=MAX_BYTES // 1048576, max_pages=MAX_PAGES, dpi=DPI, queue_max=QUEUE_MAX)
    start_worker()
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
