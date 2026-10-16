#!/usr/bin/env python3
"""Add-on OCR service.

Routes:
- POST /jobs      Authorization: Bearer <user's OAuth token, drive.file scope>; JSON {"fileId", "lang"?, "psm"?}.
                  The token is validated (aud = OCR_ALLOWED_AUD, drive.file scope) and the user is identified by the
                  Drive permissionId (only an in-memory HMAC). Queues the job and answers 202 right away. The server
                  downloads the PDF from Drive with that same token, runs OCR in the background and writes
                  "<name> (OCR).pdf" to the SAME folder as the original. Without confirmed `parents` the job fails:
                  a create call without `parents` lands in the root of My Drive (measured in the US-002 spike).
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

TOKEN = os.environ.get("OCR_TOKEN", "")
PORT = int(os.environ.get("OCR_PORT", "8080"))
HOST = os.environ.get("OCR_HOST", "127.0.0.1")
MAX_BYTES = int(os.environ.get("OCR_MAX_MB", "50")) * 1024 * 1024
MAX_PAGES = int(os.environ.get("OCR_MAX_PAGES", "100"))
DPI = int(os.environ.get("OCR_DPI", "300"))
QUEUE_MAX = int(os.environ.get("OCR_QUEUE_MAX", "5"))
TEXT_MIN_CHARS = int(os.environ.get("OCR_TEXT_MIN_CHARS", "25"))  # a page with this much extractable text already counts as searchable
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


def ocr_pdf(src, work, langs, dpi, psm=None, overlay=True):
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
    has_text = pages_with_text(src, pages)
    if len(has_text) == pages:
        raise PdfRejected("already_searchable", "PDF already has text on every page")
    use_overlay = overlay and overlay_safe(src, pages)
    todo = [n for n in range(1, pages + 1) if not (use_overlay and n in has_text)]
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
    # page i of the text layer goes to page todo[i] of the original
    r = run(["qpdf", src, "--overlay", merged, f"--from=1-{len(todo)}", "--to=" + ",".join(map(str, todo)), "--", out])
    if r.returncode not in (0, 3) or not os.path.exists(out) or page_count(out) != pages:  # 3 = warnings, output generated
        log(ev="overlay_fallback", pages=pages)
        return ocr_pdf(src, work, langs, dpi, psm, overlay=False)
    return out, pages, per_page, "overlay", pages - len(todo)


# ---- Drive ----

def _drive(url, token, *, method="GET", data=None, headers=None, timeout=120):
    req = urllib.request.Request(url, data=data, method=method, headers={"Authorization": "Bearer " + token, **(headers or {})})
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        e.read()
        if e.code in (401, 403):
            raise JobError("drive_auth")
        if e.code == 404:
            raise JobError("drive_not_found")  # Drive hides files without access behind a 404
        raise JobError("drive_error")
    except (urllib.error.URLError, TimeoutError, OSError):
        raise JobError("drive_unreachable")


def drive_meta(file_id, token):
    url = f"{DRIVE_API}/files/{quote(file_id)}?fields=id,name,parents,size,mimeType&supportsAllDrives=true"
    with _drive(url, token) as r:
        return json.loads(r.read())


def drive_download(file_id, token, dest):
    url = f"{DRIVE_API}/files/{quote(file_id)}?alt=media&supportsAllDrives=true"
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
        raise AuthError(401 if e.code == "drive_auth" else 503)
    except (KeyError, TypeError, ValueError):
        raise AuthError(401)
    key = hmac.new(KEY_SALT, str(pid).encode(), "sha256").hexdigest()[:16]
    with JOBS_LOCK:
        IDENT_CACHE[h] = (key, now + min(IDENT_TTL, left))
    return key


def process_job(job):
    """Runs a whole job. The token lives only in `job['_token']` and is cleared by the caller."""
    token = job["_token"]
    work = tempfile.mkdtemp(prefix="job_")
    try:
        meta = drive_meta(job["_file_id"], token)
        if meta.get("mimeType") != "application/pdf":
            raise JobError("not_pdf")
        if int(meta.get("size", 0) or 0) > MAX_BYTES:
            raise JobError("too_large")
        parents = meta.get("parents")
        if not parents:
            raise JobError("no_parents")  # never create without `parents`: it would land in the root of My Drive
        src = os.path.join(work, "in.pdf")
        t_dl = time.time()
        job["bytes_in"] = drive_download(job["_file_id"], token, src)
        job["download_s"] = round(time.time() - t_dl, 2)
        t_ocr = time.time()
        try:
            out, pages, per_page, mode, skipped = ocr_pdf(src, work, job["lang"], DPI, job.get("psm"))
        except ValueError as e:
            code = e.code if isinstance(e, PdfRejected) else "internal"
            raise JobError(code) from e
        job["pages"], job["mode"], job["skipped"] = pages, mode, skipped
        job["ocr_s"] = round(time.time() - t_ocr, 2)
        base = re.sub(r"\.pdf$", "", meta.get("name") or "document", flags=re.I)
        t_up = time.time()
        created, size_out = drive_upload(base + " (OCR).pdf", parents, out, token)
        job["upload_s"] = round(time.time() - t_up, 2)
        if created.get("parents") != parents:
            raise JobError("created_elsewhere")
        job["bytes_out"] = size_out
        job["result_id"] = created["id"]
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
    keys = ("status", "error", "pages", "skipped", "mode", "result_id", "bytes_in", "bytes_out", "download_s", "ocr_s", "upload_s")
    out = {k: job[k] for k in keys if k in job}
    now = time.time()
    out["age_s"] = round(now - job["created"], 1)
    if job.get("started"):
        out["run_s"] = round((job.get("finished") or now) - job["started"], 1)
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "ocr/2"

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
            return self._json(200, {"ok": True, "busy": BUSY._value == 0, "queued": QUEUE.qsize()})
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
        except (ValueError, KeyError, TypeError):
            return self._json(400, {"error": "invalid JSON: fileId is required"})
        if not isinstance(file_id, str) or not ID_RE.match(file_id):
            return self._json(400, {"error": "invalid fileId"})
        if not LANG_RE.match(langs) or (psm is not None and str(psm) not in {"3", "4", "6"}):
            return self._json(400, {"error": "invalid lang or psm"})
        now = time.time()
        job = {"id": secrets.token_urlsafe(9), "status": "queued", "created": now, "lang": langs,
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
