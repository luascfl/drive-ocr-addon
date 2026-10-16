"""Tests for the OCR server against local fake Drive and tokeninfo servers (no network, no Google account).

Run: python3 -m unittest server.test_server -v   (needs tesseract, poppler-utils and ImageMagick `convert`)
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from server import ocr_server as S

SERVICE_TOKEN = "t" * 32
AUD = "111111111111-addon.apps.googleusercontent.com"
DRIVE_FILE = "https://www.googleapis.com/auth/drive.file"
HAVE_TOOLS = all(shutil.which(c) for c in ("tesseract", "pdftoppm", "pdfunite", "pdfinfo", "convert"))


def tok(name, aud=AUD, scope=DRIVE_FILE):
    """Fake token: the fake Drive decodes name, aud and scope from the text itself."""
    return f"ya29.{name}|{aud}|{scope}".ljust(40, "x")


class FakeGoogle(BaseHTTPRequestHandler):
    files = {}      # id -> {meta, data}
    uploads = []    # (token, metadata, bytes)
    gate = None     # threading.Event: if set, the download waits for it

    def log_message(self, *a):
        pass

    def _reply(self, code, body=b"", ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _decode(token):
        m = re.match(r"ya29\.([^|]+)\|([^|]+)\|(\S+?)x*$", token or "")
        return m.groups() if m else None

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/tokeninfo":
            d = self._decode(parse_qs(u.query).get("access_token", [""])[0])
            if not d:
                return self._reply(400, b'{"error":"invalid_token"}')
            return self._reply(200, json.dumps({"azp": d[1], "aud": d[1], "scope": d[2], "exp": "9999999999", "expires_in": "3500",
                                                "access_type": "online"}).encode())
        d = self._decode((self.headers.get("Authorization") or "")[7:])
        if not d:
            return self._reply(401, b"{}")
        if u.path == "/drive/v3/about":
            return self._reply(200, json.dumps({"user": {"permissionId": "perm_" + d[0]}}).encode())
        m = re.match(r"/drive/v3/files/([^?]+)", u.path)
        f = FakeGoogle.files.get(m.group(1)) if m else None
        if not f:
            return self._reply(404, b"{}")
        if "alt=media" in u.query:
            if FakeGoogle.gate:
                FakeGoogle.gate.wait(10)
            return self._reply(200, f["data"], "application/pdf")
        return self._reply(200, json.dumps(f["meta"]).encode())

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        boundary = re.search(r"boundary=(\S+)", self.headers["Content-Type"]).group(1).encode()
        parts = body.split(b"--" + boundary)
        meta = json.loads(parts[1].split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n", 1)[0])
        pdf = parts[2].split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n", 1)[0]
        FakeGoogle.uploads.append((self.headers.get("Authorization"), meta, pdf))
        self._reply(200, json.dumps({"id": "created_" + str(len(FakeGoogle.uploads)).zfill(8), "parents": meta.get("parents")}).encode())


def synthetic_pdf(path, text="relatorio sintetico de teste"):
    subprocess.run(["convert", "-size", "900x300", "xc:white", "-font", "DejaVu-Sans", "-pointsize", "44", "-fill", "black",
                    "-annotate", "+30+150", text, "-units", "PixelsPerInch", "-density", "150", path], check=True)


def call(base, method, path, body=None, token=None):
    req = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + token} if token else {})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


@unittest.skipUnless(HAVE_TOOLS, "tesseract, poppler or ImageMagick missing")
class JobFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.google = ThreadingHTTPServer(("127.0.0.1", 0), FakeGoogle)
        threading.Thread(target=cls.google.serve_forever, daemon=True).start()
        port = cls.google.server_port
        S.TOKEN = SERVICE_TOKEN
        S.ALLOWED_AUD = {AUD}
        S.TOKENINFO = f"http://127.0.0.1:{port}/tokeninfo"
        S.DRIVE_API = f"http://127.0.0.1:{port}/drive/v3"
        S.DRIVE_UPLOAD = f"http://127.0.0.1:{port}/upload/drive/v3"
        cls.logs = []
        S.log = lambda **kw: cls.logs.append(kw)
        S.start_worker()
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), S.Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_port}"
        cls.tmp = tempfile.mkdtemp()
        cls.pdf = os.path.join(cls.tmp, "a.pdf")
        synthetic_pdf(cls.pdf)
        with open(cls.pdf, "rb") as f:
            cls.pdf_bytes = f.read()

    @classmethod
    def tearDownClass(cls):
        cls.google.shutdown()
        cls.srv.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        FakeGoogle.files, FakeGoogle.uploads, FakeGoogle.gate = {}, [], None
        S.IDENT_CACHE.clear()
        S.USER_JOBS.clear()
        S.USER_ACTIVE_MAX, S.USER_DAILY_MAX = 2, 30
        self.logs.clear()

    def add(self, fid, data=None, **meta):
        data = self.pdf_bytes if data is None else data
        m = {"id": fid, "name": "Relatorio.pdf", "mimeType": "application/pdf", "parents": ["PASTA_ORIGINAL_1"], "size": str(len(data))}
        m.update(meta)
        FakeGoogle.files[fid] = {"meta": m, "data": data}

    def wait(self, jid, token, timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            code, st = call(self.base, "GET", f"/jobs/{jid}", token=token)
            self.assertEqual(code, 200)
            if st["status"] in ("done", "error"):
                return st
            time.sleep(0.3)
        self.fail("job did not finish")

    def submit(self, token, fid="ARQUIVO_0001", **extra):
        return call(self.base, "POST", "/jobs", {"fileId": fid, **extra}, token=token)

    def test_job_writes_next_to_original_with_user_token(self):
        self.add("ARQUIVO_0001")
        t = tok("ana")
        code, r = self.submit(t)
        self.assertEqual(code, 202)
        st = self.wait(r["jobId"], t)
        self.assertEqual(st["status"], "done", st)
        self.assertEqual(len(FakeGoogle.uploads), 1)
        auth, meta, pdf = FakeGoogle.uploads[0]
        self.assertEqual(auth, "Bearer " + t)
        self.assertEqual(meta["name"], "Relatorio (OCR).pdf")
        self.assertEqual(meta["parents"], ["PASTA_ORIGINAL_1"])
        out = os.path.join(self.tmp, "out.pdf")
        with open(out, "wb") as f:
            f.write(pdf)
        self.assertIn("sintetico", subprocess.run(["pdftotext", out, "-"], capture_output=True, text=True).stdout)
        self.assertEqual(st["result_id"], "created_00000001")

    def test_without_parents_job_fails_and_nothing_is_created(self):
        self.add("ARQUIVO_0002", parents=None)
        t = tok("ana")
        st = self.wait(self.submit(t, "ARQUIVO_0002")[1]["jobId"], t)
        self.assertEqual((st["status"], st["error"]), ("error", "no_parents"))
        self.assertEqual(FakeGoogle.uploads, [])

    def test_non_pdf_and_inaccessible_file(self):
        self.add("ARQUIVO_0003", mimeType="application/vnd.google-apps.folder")
        t = tok("ana")
        self.assertEqual(self.wait(self.submit(t, "ARQUIVO_0003")[1]["jobId"], t)["error"], "not_pdf")
        self.assertEqual(self.wait(self.submit(t, "ARQUIVO_INEXISTENTE")[1]["jobId"], t)["error"], "drive_not_found")
        self.assertEqual(FakeGoogle.uploads, [])

    def _run_pdf(self, data, fid="ARQUIVO_0006"):
        self.add(fid, data=data)
        t = tok("ana")
        st = self.wait(self.submit(t, fid)[1]["jobId"], t, 120)
        self.assertEqual(st["status"], "done", st)
        out = os.path.join(self.tmp, "o.pdf")
        with open(out, "wb") as f:
            f.write(FakeGoogle.uploads[-1][2])
        return st, out

    def _two_page_pdf(self):
        a, b, pdf = (os.path.join(self.tmp, n) for n in ("a2.png", "b2.png", "two.pdf"))
        for path, text, size in ((a, "primeira pagina laranja com texto bastante longo", "1800x300"), (b, "segunda pagina banana com texto bastante longo", "1900x400")):
            subprocess.run(["convert", "-size", size, "xc:white", "-font", "DejaVu-Sans", "-pointsize", "44", "-fill", "black",
                            "-annotate", "+30+150", text, "-units", "PixelsPerInch", "-density", "150", path], check=True)
        subprocess.run(["convert", a, b, "-units", "PixelsPerInch", "-density", "150", pdf], check=True)
        with open(pdf, "rb") as f:
            return f.read()

    @unittest.skipUnless(shutil.which("qpdf"), "qpdf missing")
    def test_overlay_keeps_original_images_and_size(self):
        data = self._two_page_pdf()
        st, out = self._run_pdf(data)
        self.assertEqual(st["mode"], "overlay")
        overlay_size = len(FakeGoogle.uploads[-1][2])
        kept = os.path.join(self.tmp, "overlay.pdf")
        shutil.copy(out, kept)  # _run_pdf reuses the same output file
        self.assertLess(overlay_size - len(data), 2 * 3000)  # only the text layer (font + words) per page
        old = S.page_count
        S.page_count = lambda p: -1  # forces the fallback to get full mode on the SAME file
        try:
            full_st, _ = self._run_pdf(data, "ARQUIVO_0007")
        finally:
            S.page_count = old
        self.assertEqual(full_st["mode"], "full")
        self.assertLess(overlay_size, len(FakeGoogle.uploads[-1][2]))
        src = os.path.join(self.tmp, "src.pdf")
        with open(src, "wb") as f:
            f.write(data)
        dims = lambda p: [l.split()[3:5] for l in subprocess.run(["pdfimages", "-list", p], capture_output=True, text=True).stdout.splitlines()[2:]]
        self.assertEqual(dims(kept), dims(src))  # the same images, not resampled
        text = subprocess.run(["pdftotext", kept, "-"], capture_output=True, text=True).stdout
        self.assertIn("laranja", text)
        self.assertIn("banana", text)  # the page 2 text layer (different size) also stayed in place
        self.assertRegex(subprocess.run(["pdfinfo", kept], capture_output=True, text=True).stdout, r"Pages:\s+2\b")

    def _words(self, pdf):
        html = subprocess.run(["pdftotext", "-bbox", pdf, "-"], capture_output=True, text=True).stdout
        return [(m.group(5), *map(float, m.groups()[:4])) for m in re.finditer(
            r'xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">([^<]*)</word>', html)]

    def _single_page_pdf(self, rotate=0):
        """A page that shows upright after /Rotate: the image is rotated the opposite way and /Rotate undoes it."""
        png, raw, out = (os.path.join(self.tmp, n) for n in (f"sp{rotate}.png", f"sp_raw{rotate}.pdf", f"sp{rotate}.pdf"))
        subprocess.run(["convert", "-size", "900x300", "xc:white", "-font", "DejaVu-Sans", "-pointsize", "44", "-fill", "black",
                        "-annotate", "+30+150", "relatorio sintetico de teste", "-rotate", str((360 - rotate) % 360),
                        "-units", "PixelsPerInch", "-density", "150", png], check=True)
        subprocess.run(["convert", png, "-units", "PixelsPerInch", "-density", "150", raw], check=True)
        if rotate:
            subprocess.run(["qpdf", f"--rotate=+{rotate}", raw, out], check=True)
            raw = out
        with open(raw, "rb") as f:
            return f.read()

    @unittest.skipUnless(shutil.which("qpdf"), "qpdf missing")
    def test_rotated_pages_use_overlay_aligned_with_full_mode(self):
        for rotate in (90, 180, 270):
            data = self._single_page_pdf(rotate)
            st, out = self._run_pdf(data, f"ARQUIVO_R{rotate:03d}_01")
            self.assertEqual(st["mode"], "overlay", rotate)
            kept = os.path.join(self.tmp, f"ov{rotate}.pdf")
            shutil.copy(out, kept)
            old = S.page_count
            S.page_count = lambda p: -1  # forces full mode on the SAME file, the position reference
            try:
                full_st, full_out = self._run_pdf(data, f"ARQUIVO_R{rotate:03d}_02")
            finally:
                S.page_count = old
            self.assertEqual(full_st["mode"], "full")
            a, b = self._words(full_out), self._words(kept)
            self.assertGreaterEqual(len(a), 3, rotate)  # the comparison must not be empty
            self.assertEqual([w[0] for w in a], [w[0] for w in b], rotate)
            dev = max(abs(x[i] - y[i]) for x, y in zip(a, b) for i in range(1, 5))
            self.assertLess(dev, 1.0, f"rotation {rotate}: deviation {dev} pt")

    @unittest.skipUnless(shutil.which("qpdf"), "qpdf missing")
    def test_cropbox_different_from_mediabox_falls_back_to_full_mode(self):
        src, qdf, cropped = (os.path.join(self.tmp, n) for n in ("c_src.pdf", "c.qdf", "c_crop.pdf"))
        with open(src, "wb") as f:
            f.write(self._single_page_pdf(0))
        text = subprocess.run(["qpdf", "--qdf", "--object-streams=disable", src, "-"], capture_output=True).stdout
        box = rb"/CropBox [ 10 10 400 130 ]"
        text = re.sub(rb"/CropBox \[[^\]]*\]", box, text) if b"/CropBox" in text else re.sub(rb"(/MediaBox \[[^\]]*\])", rb"\1\n  " + box, text)
        with open(qdf, "wb") as f:
            f.write(text)
        subprocess.run(["qpdf", qdf, cropped], check=False, capture_output=True)  # xref warnings from the edited QDF are expected
        info = subprocess.run(["pdfinfo", "-box", cropped], capture_output=True, text=True).stdout
        self.assertRegex(info, r"CropBox:\s+10\.00\s+10\.00\s+400\.00\s+130\.00")  # precondition: the CropBox was really applied (the page is 432 pt)
        with open(cropped, "rb") as f:
            st, _ = self._run_pdf(f.read(), "ARQUIVO_CROP_01")
        self.assertEqual(st["mode"], "full")

    @unittest.skipUnless(shutil.which("qpdf"), "qpdf missing")
    def test_overlay_failure_falls_back_to_full_mode_and_still_delivers_text(self):
        old = S.page_count
        S.page_count = lambda p: -1  # validation of the qpdf result fails
        try:
            st, out = self._run_pdf(self._two_page_pdf())
        finally:
            S.page_count = old
        self.assertEqual(st["mode"], "full")
        self.assertIn("banana", subprocess.run(["pdftotext", out, "-"], capture_output=True, text=True).stdout)
        self.assertIn("overlay_fallback", [l.get("ev") for l in self.logs])

    def test_already_searchable_pdf_is_refused_without_creating_a_file(self):
        raw = self._two_page_pdf()
        _, out = self._run_pdf(raw)  # the server's own result already has text on every page
        with open(out, "rb") as f:
            searchable = f.read()
        uploads = len(FakeGoogle.uploads)
        self.add("ARQUIVO_0008", data=searchable)
        t = tok("ana")
        st = self.wait(self.submit(t, "ARQUIVO_0008")[1]["jobId"], t)
        self.assertEqual((st["status"], st["error"]), ("error", "already_searchable"))
        self.assertEqual(len(FakeGoogle.uploads), uploads)

    @unittest.skipUnless(shutil.which("qpdf"), "qpdf missing")
    def test_page_that_already_has_text_gets_no_duplicate_layer(self):
        raw = self._two_page_pdf()
        _, out = self._run_pdf(raw)
        searchable, rawp, mixed = (os.path.join(self.tmp, n) for n in ("s.pdf", "raw.pdf", "mixed.pdf"))
        shutil.copy(out, searchable)
        with open(rawp, "wb") as f:
            f.write(raw)
        subprocess.run(["qpdf", "--empty", "--pages", searchable, "1", rawp, "2", "--", mixed], check=True)  # page 1 with text, page 2 without
        with open(mixed, "rb") as f:
            st, out2 = self._run_pdf(f.read(), "ARQUIVO_0009")
        self.assertEqual((st["mode"], st["skipped"], st["pages"]), ("overlay", 1, 2))
        page = lambda n: subprocess.run(["pdftotext", "-f", str(n), "-l", str(n), out2, "-"], capture_output=True, text=True).stdout
        self.assertIn("banana", page(2))  # the page without text was OCR'd and the layer landed on the right page
        # pdftotext hides identical overlapping layers (tested: 1 and 2 layers give the same output), so the layers are
        # counted through the `Do` operator in the content, whose per-layer count comes from the server itself in this environment.
        dos = lambda p: len(re.findall(rb" Do\r?\n", subprocess.run(["qpdf", "--qdf", "--object-streams=disable", "--stream-data=uncompress", p, "-"], capture_output=True).stdout))
        per_layer, rest = divmod(dos(searchable) - dos(rawp), 2)  # searchable has 2 layers over raw
        self.assertEqual(rest, 0)
        self.assertGreater(per_layer, 0)
        self.assertEqual(dos(out2) - dos(mixed), per_layer)  # only page 2 gained a layer; page 1 did not gain a second one

    def test_token_id_name_and_user_do_not_stay_in_state_or_log(self):
        self.add("ARQUIVO_0004")
        t = tok("bia")
        jid = self.submit(t, "ARQUIVO_0004")[1]["jobId"]
        st = self.wait(jid, t)
        everything = json.dumps(st) + json.dumps(self.logs)
        for secret in (t, "ARQUIVO_0004", "Relatorio", "perm_bia", "bia"):
            self.assertNotIn(secret, everything)
        self.assertIsNone(S.JOBS[jid]["_token"])
        self.assertNotIn("bia", json.dumps(S.USER_JOBS))  # the user key is an HMAC, not the permissionId

    def test_only_accepts_token_issued_for_the_addon_with_drive_file(self):
        self.add("ARQUIVO_0001")
        before = len(S.JOBS)
        self.assertEqual(self.submit(None)[0], 401)
        self.assertEqual(self.submit("curto")[0], 401)
        self.assertEqual(self.submit(SERVICE_TOKEN)[0], 401)  # the service secret no longer works on the job routes
        self.assertEqual(self.submit("ya29.invalido-sem-formato-nenhum-xx")[0], 401)
        self.assertEqual(self.submit(tok("ana", aud="999-outro-app.apps.googleusercontent.com"))[0], 401)
        self.assertEqual(self.submit(tok("ana", scope="https://www.googleapis.com/auth/drive.readonly"))[0], 401)
        self.assertEqual(FakeGoogle.uploads, [])
        self.assertEqual(len(S.JOBS), before)  # no refused attempt created a job
        rej = [l for l in self.logs if l.get("ev") == "auth_rejected"]
        self.assertTrue(any(l["aud"] == "999-outro-app.apps.googleusercontent.com" for l in rej))  # the rejected aud appears, for configuration
        self.assertNotIn("ya29", json.dumps(self.logs))  # but never the token
        self.assertNotIn("ana", json.dumps(rej))  # nor the user

    def test_other_users_job_looks_nonexistent(self):
        self.add("ARQUIVO_0001")
        ana, bia = tok("ana"), tok("bia")
        jid = self.submit(ana)[1]["jobId"]
        self.assertEqual(call(self.base, "GET", f"/jobs/{jid}", token=bia)[0], 404)
        self.assertEqual(call(self.base, "GET", f"/jobs/{jid}", token=None)[0], 401)
        self.assertEqual(self.wait(jid, ana)["status"], "done")

    def test_input_validation(self):
        t = tok("ana")
        self.assertEqual(self.submit(t, "../x")[0], 400)
        self.assertEqual(self.submit(t, "ARQUIVO_0001", lang="../x")[0], 400)
        self.assertEqual(self.submit(t, "ARQUIVO_0001", psm=11)[0], 400)
        self.assertEqual(call(self.base, "GET", "/jobs/desconhecido1", token=t)[0], 404)

    def test_google_down_returns_503_not_401(self):
        old = S.TOKENINFO
        S.TOKENINFO = "http://127.0.0.1:9/tokeninfo"  # port with no service
        try:
            self.assertEqual(self.submit(tok("ana"))[0], 503)
        finally:
            S.TOKENINFO = old

    def test_active_jobs_limit_per_user_does_not_affect_another_user(self):
        self.add("ARQUIVO_0005")
        FakeGoogle.gate = threading.Event()  # holds the first job at the download
        ana, bia = tok("ana"), tok("bia")
        ids = []
        for _ in range(S.USER_ACTIVE_MAX):
            c, r = self.submit(ana, "ARQUIVO_0005")
            self.assertEqual(c, 202)
            ids.append((r["jobId"], ana))
        c, _ = self.submit(ana, "ARQUIVO_0005")
        self.assertEqual(c, 429)
        c, r = self.submit(bia, "ARQUIVO_0005")
        self.assertEqual(c, 202)
        ids.append((r["jobId"], bia))
        FakeGoogle.gate.set()
        for jid, t in ids:
            self.assertEqual(self.wait(jid, t, 120)["status"], "done")
        self.assertEqual(self.submit(ana, "ARQUIVO_0005")[0], 202)  # freed up after finishing

    def test_daily_limit_per_user(self):
        self.add("ARQUIVO_0005")
        S.USER_DAILY_MAX = 2
        ana = tok("ana")
        for _ in range(2):
            c, r = self.submit(ana, "ARQUIVO_0005")
            self.assertEqual(c, 202)
            self.wait(r["jobId"], ana)
        self.assertEqual(self.submit(ana, "ARQUIVO_0005")[0], 429)
        self.assertEqual(self.submit(tok("bia"), "ARQUIVO_0005")[0], 202)

    def test_full_queue_returns_503_and_queued_jobs_finish(self):
        self.add("ARQUIVO_0005")
        FakeGoogle.gate = threading.Event()
        ids, codes = [], []
        for i in range(S.QUEUE_MAX + 3):
            t = tok(f"u{i}")  # one user per job: what fills the queue is the queue, not the per-user limit
            c, r = self.submit(t, "ARQUIVO_0005")
            codes.append(c)
            if c == 202:
                ids.append((r["jobId"], t))
        self.assertIn(503, codes)
        self.assertGreaterEqual(codes.count(202), S.QUEUE_MAX)
        FakeGoogle.gate.set()
        for jid, t in ids:
            self.assertEqual(self.wait(jid, t, 120)["status"], "done")


if __name__ == "__main__":
    unittest.main()
