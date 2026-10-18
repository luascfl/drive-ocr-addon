"""Tests for the OCR server against local fake Drive and tokeninfo servers (no network, no Google account).

Run: python3 -m unittest server.test_server -v   (needs tesseract, poppler-utils and ImageMagick `convert`)
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
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
    after_download = None  # callable(file): runs right after the PDF is served

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
        m = re.match(r"/drive/v3/files/([^/?]+)/revisions/([^/?]+)", u.path)
        if m:  # download one revision
            f = FakeGoogle.files.get(m.group(1))
            rev = f["revisions"].get(m.group(2)) if f else None
            return self._reply(200, rev, "application/pdf") if rev is not None else self._reply(404, b"{}")
        m = re.match(r"/drive/v3/files/([^/?]+)", u.path)
        f = FakeGoogle.files.get(m.group(1)) if m else None
        if not f:
            return self._reply(404, b"{}")
        if "alt=media" in u.query:
            if FakeGoogle.gate:
                FakeGoogle.gate.wait(10)
            data = f["data"]
            if FakeGoogle.after_download:
                FakeGoogle.after_download(f)  # simulates an edit that happens while the job is processing
            return self._reply(200, data, "application/pdf")
        meta = {k: v for k, v in f["meta"].items() if not k.startswith("_")}
        meta["md5Checksum"] = hashlib.md5(f["data"]).hexdigest()
        meta["headRevisionId"] = f["head"]
        meta["appProperties"] = dict(f["props"])
        return self._reply(200, json.dumps(meta).encode())

    def do_PATCH(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        path = urlparse(self.path).path
        m = re.match(r"/drive/v3/files/([^/?]+)/revisions/([^/?]+)", path)
        if m:  # pin / unpin a revision (keepForever)
            f = FakeGoogle.files.get(m.group(1))
            if not f or m.group(2) not in f["revisions"]:
                return self._reply(404, b"{}")
            if f["meta"].get("_no_revisions"):
                return self._reply(403, b"{}")
            keep = bool(json.loads(body)["keepForever"])
            (f["pinned"].add if keep else f["pinned"].discard)(m.group(2))
            FakeGoogle.pins.append((m.group(2), keep))
            return self._reply(200, json.dumps({"id": m.group(2), "keepForever": keep}).encode())
        m = re.match(r"/upload/drive/v3/files/([^/?]+)", path)  # replace content (multipart with appProperties)
        f = FakeGoogle.files.get(m.group(1)) if m else None
        if not f:
            return self._reply(404, b"{}")
        if f["meta"].get("_readonly"):
            return self._reply(403, b"{}")
        boundary = re.search(r"boundary=(\S+)", self.headers["Content-Type"]).group(1).encode()
        parts = body.split(b"--" + boundary)
        props = json.loads(parts[1].split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n", 1)[0]).get("appProperties", {})
        pdf = parts[2].split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n", 1)[0]
        f["data"] = pdf
        f["meta"]["size"] = str(len(pdf))
        f["head"] = "rev%d" % (len(f["revisions"]) + 1)
        f["revisions"][f["head"]] = pdf
        for k, v in props.items():
            if v is None:
                f["props"].pop(k, None)
            else:
                f["props"][k] = v
        FakeGoogle.uploads.append((self.headers.get("Authorization"), {"replaced": m.group(1), "appProperties": props}, pdf))
        self._reply(200, json.dumps({"id": m.group(1), "headRevisionId": f["head"], "md5Checksum": hashlib.md5(pdf).hexdigest()}).encode())

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
        FakeGoogle.files, FakeGoogle.uploads, FakeGoogle.gate, FakeGoogle.after_download = {}, [], None, None
        FakeGoogle.pins = []
        S.IDENT_CACHE.clear()
        S.USER_JOBS.clear()
        S.USER_ACTIVE_MAX, S.USER_DAILY_MAX = 2, 30
        self.logs.clear()

    def add(self, fid, data=None, **meta):
        data = self.pdf_bytes if data is None else data
        m = {"id": fid, "name": "Relatorio.pdf", "mimeType": "application/pdf", "parents": ["PASTA_ORIGINAL_1"], "size": str(len(data))}
        m.update(meta)
        FakeGoogle.files[fid] = {"meta": m, "data": data, "revisions": {"rev1": data}, "head": "rev1", "pinned": set(), "props": {}}

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

    def test_replace_overwrites_the_original_content_with_the_user_token(self):
        self.add("ARQUIVO_0001")
        t = tok("ana")
        code, r = self.submit(t)  # replace is the default action
        self.assertEqual(code, 202)
        st = self.wait(r["jobId"], t)
        self.assertEqual(st["status"], "done", st)
        self.assertEqual(len(FakeGoogle.uploads), 1)
        auth, meta, pdf = FakeGoogle.uploads[0]
        self.assertEqual(auth, "Bearer " + t)
        self.assertEqual(meta["replaced"], "ARQUIVO_0001")  # same file id: no new file
        self.assertEqual(FakeGoogle.files["ARQUIVO_0001"]["meta"]["name"], "Relatorio.pdf")  # name and folder stay
        self.assertEqual(FakeGoogle.files["ARQUIVO_0001"]["data"], pdf)  # the content in Drive is now the searchable PDF
        self.assertNotEqual(pdf, self.pdf_bytes)
        out = os.path.join(self.tmp, "out.pdf")
        with open(out, "wb") as f:
            f.write(pdf)
        self.assertIn("sintetico", subprocess.run(["pdftotext", out, "-"], capture_output=True, text=True).stdout)
        self.assertEqual((st["result_id"], st["action"]), ("ARQUIVO_0001", "replace"))

    def test_replace_does_not_need_parents_but_flatten_does(self):
        self.add("ARQUIVO_0002", parents=None)
        t = tok("ana")
        st = self.wait(self.submit(t, "ARQUIVO_0002")[1]["jobId"], t)
        self.assertEqual(st["status"], "done", st)  # a replace creates nothing, so there is no folder to confirm
        uploads = len(FakeGoogle.uploads)
        st = self.wait(self.submit(t, "ARQUIVO_0002", action="flatten")[1]["jobId"], t)
        self.assertEqual((st["status"], st["error"]), ("error", "no_parents"))  # a new file without parents would land in the Drive root
        self.assertEqual(len(FakeGoogle.uploads), uploads)

    def test_flatten_creates_a_new_file_and_leaves_the_original_alone(self):
        self.add("ARQUIVO_0011")
        t = tok("ana")
        st = self.wait(self.submit(t, "ARQUIVO_0011", action="flatten")[1]["jobId"], t, 90)
        self.assertEqual((st["status"], st["action"], st["mode"]), ("done", "flatten", "full"), st)
        auth, meta, pdf = FakeGoogle.uploads[-1]
        self.assertEqual(meta["name"], "Relatorio (flatten).pdf")
        self.assertEqual(meta["parents"], ["PASTA_ORIGINAL_1"])
        self.assertEqual(FakeGoogle.files["ARQUIVO_0011"]["data"], self.pdf_bytes)  # the original is untouched
        self.assertTrue(st["result_id"].startswith("created_"))

    @unittest.skipUnless(shutil.which("qpdf"), "qpdf missing")
    def test_flatten_redoes_ocr_on_a_pdf_that_already_has_text_while_replace_refuses_it(self):
        _, out = self._run_pdf(self._two_page_pdf())  # a searchable PDF made by the server itself
        with open(out, "rb") as f:
            searchable = f.read()
        self.add("ARQUIVO_0012", data=searchable)
        t = tok("ana")
        st = self.wait(self.submit(t, "ARQUIVO_0012")[1]["jobId"], t)
        self.assertEqual((st["status"], st["error"]), ("error", "already_searchable"))
        uploads = len(FakeGoogle.uploads)
        st = self.wait(self.submit(t, "ARQUIVO_0012", action="flatten")[1]["jobId"], t, 120)
        self.assertEqual((st["status"], st["mode"], st["skipped"]), ("done", "full", 0), st)  # nothing skipped: every page re-OCR'd
        self.assertEqual(len(FakeGoogle.uploads), uploads + 1)
        self.assertEqual(FakeGoogle.files["ARQUIVO_0012"]["data"], searchable)

    def test_replace_is_refused_if_the_file_changed_while_processing(self):
        self.add("ARQUIVO_0013")
        FakeGoogle.after_download = lambda f: f.__setitem__("data", f["data"] + b"\n% edited by someone else\n")
        t = tok("ana")
        st = self.wait(self.submit(t, "ARQUIVO_0013")[1]["jobId"], t)
        self.assertEqual((st["status"], st["error"]), ("error", "file_changed"))
        self.assertEqual(FakeGoogle.uploads, [])

    def test_replace_without_edit_permission_fails_the_original_stays_and_the_pin_is_released(self):
        self.add("ARQUIVO_0014", _readonly=True)
        t = tok("ana")
        st = self.wait(self.submit(t, "ARQUIVO_0014")[1]["jobId"], t)
        self.assertEqual((st["status"], st["error"]), ("error", "drive_forbidden"))
        self.assertEqual(FakeGoogle.files["ARQUIVO_0014"]["data"], self.pdf_bytes)
        self.assertEqual(FakeGoogle.uploads, [])
        self.assertEqual(FakeGoogle.pins, [("rev1", True), ("rev1", False)])  # pinned for the attempt, released after the failure
        self.assertEqual(FakeGoogle.files["ARQUIVO_0014"]["pinned"], set())

    def test_replace_is_refused_when_the_original_cannot_be_kept_for_revert(self):
        self.add("ARQUIVO_0016", _no_revisions=True)
        t = tok("ana")
        st = self.wait(self.submit(t, "ARQUIVO_0016")[1]["jobId"], t)
        self.assertEqual((st["status"], st["error"]), ("error", "revision_unavailable"))  # never replace what cannot be restored
        self.assertEqual(FakeGoogle.uploads, [])
        self.assertEqual(FakeGoogle.files["ARQUIVO_0016"]["data"], self.pdf_bytes)

    def _replace_then(self, fid):
        self.add(fid)
        t = tok("ana")
        st = self.wait(self.submit(t, fid)[1]["jobId"], t)
        self.assertEqual(st["status"], "done", st)
        return t

    def test_revert_restores_the_original_and_cleans_up(self):
        t = self._replace_then("ARQUIVO_0017")
        f = FakeGoogle.files["ARQUIVO_0017"]
        self.assertNotEqual(f["data"], self.pdf_bytes)
        self.assertEqual(f["pinned"], {"rev1"})  # the original is pinned beyond Drive's purge
        self.assertEqual(set(f["props"]), {"ocrPrevRev", "ocrMd5", "ocrAt"})
        self.assertEqual((f["props"]["ocrPrevRev"], f["props"]["ocrMd5"]), ("rev1", hashlib.md5(f["data"]).hexdigest()))
        st = self.wait(self.submit(t, "ARQUIVO_0017", action="revert")[1]["jobId"], t)
        self.assertEqual((st["status"], st["action"], st["result_id"]), ("done", "revert", "ARQUIVO_0017"), st)
        self.assertEqual(f["data"], self.pdf_bytes)  # the original content is back
        self.assertEqual(f["props"], {})  # the OCR marker is gone, so the add-on offers Start OCR again
        self.assertEqual(f["pinned"], set())  # the pin was released
        self.assertEqual(f["meta"]["name"], "Relatorio.pdf")

    def test_revert_never_throws_away_an_edit_made_after_the_ocr(self):
        t = self._replace_then("ARQUIVO_0018")
        f = FakeGoogle.files["ARQUIVO_0018"]
        f["data"] = f["data"] + b"\n% a later edit\n"  # someone edited the OCR result
        edited = f["data"]
        st = self.wait(self.submit(t, "ARQUIVO_0018", action="revert")[1]["jobId"], t)
        self.assertEqual((st["status"], st["error"]), ("error", "file_changed"))
        self.assertEqual(f["data"], edited)

    def test_revert_with_no_ocr_on_the_file_has_nothing_to_revert(self):
        self.add("ARQUIVO_0019")
        t = tok("ana")
        st = self.wait(self.submit(t, "ARQUIVO_0019", action="revert")[1]["jobId"], t)
        self.assertEqual((st["status"], st["error"]), ("error", "nothing_to_revert"))
        self.assertEqual(FakeGoogle.uploads, [])

    def test_revert_fails_clearly_if_the_original_revision_is_gone(self):
        t = self._replace_then("ARQUIVO_0020")
        f = FakeGoogle.files["ARQUIVO_0020"]
        del f["revisions"]["rev1"]  # Drive purged it
        ocr = f["data"]
        st = self.wait(self.submit(t, "ARQUIVO_0020", action="revert")[1]["jobId"], t)
        self.assertEqual((st["status"], st["error"]), ("error", "revision_unavailable"))
        self.assertEqual(f["data"], ocr)  # nothing was lost

    def test_invalid_action_is_rejected(self):
        self.add("ARQUIVO_0015")
        self.assertEqual(self.submit(tok("ana"), "ARQUIVO_0015", action="delete")[0], 400)

    def test_non_pdf_and_inaccessible_file(self):
        self.add("ARQUIVO_0003", mimeType="application/vnd.google-apps.folder")
        t = tok("ana")
        self.assertEqual(self.wait(self.submit(t, "ARQUIVO_0003")[1]["jobId"], t)["error"], "not_pdf")
        self.assertEqual(self.wait(self.submit(t, "ARQUIVO_INEXISTENTE")[1]["jobId"], t)["error"], "drive_not_found")
        self.assertEqual(FakeGoogle.uploads, [])

    def _run_pdf(self, data, fid="ARQUIVO_0006", action="replace"):
        self.add(fid, data=data)
        t = tok("ana")
        st = self.wait(self.submit(t, fid, action=action)[1]["jobId"], t, 120)
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
        full_st, _ = self._run_pdf(data, "ARQUIVO_0007", action="flatten")  # full mode on the SAME file, for comparison
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
            full_st, full_out = self._run_pdf(data, f"ARQUIVO_R{rotate:03d}_02", action="flatten")  # full mode on the SAME file, the position reference
            self.assertEqual(full_st["mode"], "full")
            a, b = self._words(full_out), self._words(kept)
            self.assertGreaterEqual(len(a), 3, rotate)  # the comparison must not be empty
            self.assertEqual([w[0] for w in a], [w[0] for w in b], rotate)
            dev = max(abs(x[i] - y[i]) for x, y in zip(a, b) for i in range(1, 5))
            self.assertLess(dev, 1.0, f"rotation {rotate}: deviation {dev} pt")

    @unittest.skipUnless(shutil.which("qpdf"), "qpdf missing")
    def test_cropbox_different_from_mediabox_refuses_replace_and_flatten_still_works(self):
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
            data = f.read()
        self.add("ARQUIVO_CROP_01", data=data)
        t = tok("ana")
        st = self.wait(self.submit(t, "ARQUIVO_CROP_01")[1]["jobId"], t, 120)
        self.assertEqual((st["status"], st["error"]), ("error", "overlay_unavailable"))  # replacing would re-encode the page
        self.assertEqual(FakeGoogle.uploads, [])
        self.assertEqual(FakeGoogle.files["ARQUIVO_CROP_01"]["data"], data)
        st, _ = self._run_pdf(data, "ARQUIVO_CROP_02", action="flatten")  # a separate copy is fine
        self.assertEqual(st["mode"], "full")

    @unittest.skipUnless(shutil.which("qpdf"), "qpdf missing")
    def test_overlay_failure_never_replaces_the_original(self):
        data = self._two_page_pdf()
        self.add("ARQUIVO_0010", data=data)
        old = S.page_count
        S.page_count = lambda p: -1  # validation of the qpdf result fails
        try:
            t = tok("ana")
            st = self.wait(self.submit(t, "ARQUIVO_0010")[1]["jobId"], t, 120)
        finally:
            S.page_count = old
        self.assertEqual((st["status"], st["error"]), ("error", "overlay_unavailable"))
        self.assertEqual(FakeGoogle.uploads, [])
        self.assertEqual(FakeGoogle.files["ARQUIVO_0010"]["data"], data)  # the original is untouched
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

    # ---- preparation: orientation and double-page split ----

    LEFT_TEXT = "alpha bravo charlie\\ndelta echo foxtrot\\ngolf hotel india"
    RIGHT_TEXT = "juliet kilo lima\\nmike november oscar\\npapa quebec romeo"

    def _to_pdf(self, png, name):
        pdf = os.path.join(self.tmp, name)
        subprocess.run(["convert", png, "-units", "PixelsPerInch", "-density", "150", pdf], check=True)
        with open(pdf, "rb") as f:
            return f.read()

    def _spread_pdf(self, shadow=False, name="spread.pdf", grain=0.0):
        png = os.path.join(self.tmp, name + ".png")
        cmd = ["convert", "-size", "2000x1000", "xc:white"]
        if grain:  # paper grain: a real scan is never pure white; 8 px dashes put about 0.9% of ink in every gap column
            dashes = " ".join(f"rectangle {x},{(x * 37) % 980} {x + 2},{(x * 37) % 980 + 7}" for x in range(700, 1300, 3))
            cmd += ["-fill", "black", "-draw", dashes]
        cmd += ["-font", "DejaVu-Sans", "-pointsize", "44", "-fill", "black",
                "-annotate", "+80+300", self.LEFT_TEXT, "-annotate", "+1250+300", self.RIGHT_TEXT]
        if shadow:
            cmd += ["-draw", "rectangle 975,0 1025,999"]
        subprocess.run(cmd + ["-units", "PixelsPerInch", "-density", "150", png], check=True)
        return self._to_pdf(png, name)

    def _gutter_of(self, data, name):
        src = os.path.join(self.tmp, name)
        with open(src, "wb") as f:
            f.write(data)
        base = os.path.join(self.tmp, name + "_g")
        subprocess.run(["pdftoppm", "-r", "60", "-gray", "-singlefile", src, base], check=True)
        return S.find_gutter(*S.read_pgm(base + ".pgm"))

    def test_gutter_blank_gap_between_two_pages_is_found_in_the_middle_of_the_gap(self):
        cut = self._gutter_of(self._spread_pdf(), "g1.pdf")
        self.assertIsNotNone(cut)
        self.assertTrue(0.30 < cut < 0.60, cut)  # inside the blank gap (text ends near 0.28 and restarts near 0.62)

    def test_gutter_dark_shadow_band_wins_and_is_cut_through_its_center(self):
        cut = self._gutter_of(self._spread_pdf(shadow=True), "g2.pdf")
        self.assertIsNotNone(cut)
        self.assertAlmostEqual(cut, 0.5, delta=0.02)

    def test_gutter_is_not_invented_on_a_landscape_page_of_single_page_text(self):
        png = os.path.join(self.tmp, "wide.png")
        text = "the quick brown fox jumps over the lazy dog and keeps running until the very end of this long line"
        subprocess.run(["convert", "-size", "2000x1000", "xc:white", "-font", "DejaVu-Sans", "-pointsize", "40", "-fill", "black",
                        "-annotate", "+60+300", text, "-annotate", "+60+450", text[::-1][:96], "-annotate", "+60+600", text,
                        "-units", "PixelsPerInch", "-density", "150", png], check=True)
        self.assertIsNone(self._gutter_of(self._to_pdf(png, "wide.pdf"), "g3.pdf"))

    def test_a_title_line_with_word_gaps_on_a_landscape_slide_is_not_split(self):
        png = os.path.join(self.tmp, "slide.png")
        subprocess.run(["convert", "-size", "2000x1000", "xc:white", "-font", "DejaVu-Sans", "-pointsize", "60", "-fill", "black",
                        "-annotate", "+80+450", "Quarterly results    and    outlook", "-units", "PixelsPerInch", "-density", "150", png], check=True)
        self.assertIsNone(self._gutter_of(self._to_pdf(png, "slide.pdf"), "g4.pdf"))

    def test_gutter_is_found_on_a_grainy_scan_where_the_gap_is_not_pure_white(self):
        data = self._spread_pdf(grain=True, name="grain.pdf")
        base = os.path.join(self.tmp, "grain_probe")
        with open(os.path.join(self.tmp, "grain.pdf"), "wb") as f:
            f.write(data)
        subprocess.run(["pdftoppm", "-r", "60", "-gray", "-singlefile", os.path.join(self.tmp, "grain.pdf"), base], check=True)
        w, h, px = S.read_pgm(base + ".pgm")
        gap_ink = [px[x::w][:h].translate(S.PGM_DARK).count(1) / h for x in range(int(w * 0.40), int(w * 0.60))]
        self.assertGreater(min(gap_ink), 0.004)  # the fixture is noisy enough that the old 0.2% rule could not see the gap
        cut = S.find_gutter(w, h, px)
        self.assertIsNotNone(cut)
        self.assertTrue(0.30 < cut < 0.60, cut)

    def test_settle_cuts_fills_the_pages_that_found_nothing_and_fixes_the_outliers(self):
        landscape = [0, 1, 2, 3, 4]
        settled = S.settle_cuts({1: 0.48, 2: 0.50, 3: 0.51, 4: 0.69}, landscape)  # page 0 found nothing; page 4 is far off
        self.assertEqual(sorted(settled), landscape)
        self.assertEqual(settled[0], 0.51)  # the (upper) median of the four detections
        self.assertEqual(settled[4], 0.51)  # 0.69 is 0.18 from the median: a detection error, not a real gutter
        self.assertEqual((settled[1], settled[2], settled[3]), (0.48, 0.50, 0.51))  # honest detections are kept

    def test_settle_cuts_does_not_force_a_split_on_a_mixed_document(self):
        # 2 of 8 landscape pages found a gutter: most of the document is not a book scan, so only those 2 are cut
        cuts = {0: 0.48, 5: 0.50}
        self.assertEqual(S.settle_cuts(dict(cuts), list(range(8))), cuts)
        # a single detection never drags the other landscape pages along
        self.assertEqual(S.settle_cuts({2: 0.5}, [2, 3]), {2: 0.5})

    @unittest.skipUnless(shutil.which("qpdf"), "qpdf missing")
    def test_replace_splits_a_double_page_scan_after_ocr(self):
        pytest_pypdf = __import__("importlib").util.find_spec("pypdf")
        if not pytest_pypdf:
            self.skipTest("pypdf missing")
        spread = self._spread_pdf()
        st, out = self._run_pdf(spread, "ARQUIVO_SPLIT_01")
        self.assertEqual((st["split"], st["pages"], st["rotated"]), (1, 2, 0), st)
        info = subprocess.run(["pdfinfo", "-f", "1", "-l", "2", "-box", out], capture_output=True, text=True).stdout
        sizes = re.findall(r"Page\s+\d+ size:\s+([\d.]+) x ([\d.]+)", info)
        self.assertEqual(len(sizes), 2)
        for w, h in sizes:
            self.assertAlmostEqual(float(h), 1000 * 72 / 150, delta=1)  # same height as the spread
        self.assertAlmostEqual(sum(float(w) for w, _ in sizes), 2000 * 72 / 150, delta=2)  # the two halves cover the spread
        self.assertRegex(info, r"Page\s+2 MediaBox:\s+(?!0\.00\s)\d+\.\d+\s+0\.00")  # the right half's box starts at the cut, not at 0: the content is shared, not moved
        page = lambda n: subprocess.run(["pdftotext", "-f", str(n), "-l", str(n), out, "-"], capture_output=True, text=True).stdout
        self.assertIn("alpha", page(1))
        self.assertNotIn("juliet", page(1))
        self.assertIn("juliet", page(2))
        self.assertNotIn("alpha", page(2))
        imgs = subprocess.run(["pdfimages", "-list", out], capture_output=True, text=True).stdout.splitlines()[2:]
        self.assertEqual({l.split()[3] for l in imgs}, {"2000"})  # the original image, not re-rasterized
        self.assertLess(len(FakeGoogle.uploads[-1][2]) - len(spread), 2 * 4000)  # one spread text layer and cloned page dictionaries; the image remains shared

    @unittest.skipUnless(shutil.which("qpdf"), "qpdf missing")
    def test_split_after_ocr_keeps_the_text_layer_aligned_with_full_mode(self):
        if not __import__("importlib").util.find_spec("pypdf"):
            self.skipTest("pypdf missing")
        src = os.path.join(self.tmp, "sp_src.pdf")
        with open(src, "wb") as f:
            f.write(self._spread_pdf(name="sp_src_raw.pdf"))
        result_work = tempfile.mkdtemp(dir=self.tmp)
        info = {}
        overlay, pages, *_ = S.ocr_pdf(src, result_work, "por+eng", 300, action="replace", prep_info=info)
        self.assertEqual((info["split"], pages, S.page_count(overlay)), (1, 2, 2))

        reference_work = tempfile.mkdtemp(dir=self.tmp)
        prepared, cuts = S.prepare_pdf(src, reference_work, 1, {})
        self.assertEqual((S.page_count(prepared), len(cuts)), (1, 1))
        full, *_ = S.ocr_pdf(prepared, tempfile.mkdtemp(dir=self.tmp), "por+eng", 300, overlay=False)
        reference = os.path.join(reference_work, "reference.pdf")
        S.rewrite_pages(full, reference, cuts)

        a, b = self._words(reference), self._words(overlay)
        self.assertGreaterEqual(len(a), 12)
        self.assertEqual([w[0] for w in a], [w[0] for w in b])
        dev = max(abs(x[i] - y[i]) for x, y in zip(a, b) for i in range(1, 5))
        self.assertLess(dev, 1.0, f"deviation {dev} pt")

    def _sideways_pdf(self):
        png = os.path.join(self.tmp, "side.png")
        para = ("The committee reviewed the annual report and approved the budget for the next fiscal year. "
                "Several members asked for more detail about the infrastructure projects, and the chair promised "
                "a written summary before the end of the month. The meeting was adjourned at five in the afternoon.")
        subprocess.run(["convert", "-size", "1300x900", "xc:white", "-font", "DejaVu-Sans", "-pointsize", "38", "-fill", "black",
                        "-gravity", "NorthWest", "-annotate", "+40+40", "\n".join(textwrap.wrap(para, 52)), "-rotate", "90",
                        "-units", "PixelsPerInch", "-density", "150", png], check=True)
        return self._to_pdf(png, "side.pdf")

    @unittest.skipUnless(shutil.which("qpdf"), "qpdf missing")
    def test_replace_turns_a_sideways_scan_upright_before_ocr(self):
        st, out = self._run_pdf(self._sideways_pdf(), "ARQUIVO_ROT_01")
        self.assertEqual((st["rotated"], st["split"]), (270, 0), st)  # the image was turned 90 degrees clockwise
        text = subprocess.run(["pdftotext", out, "-"], capture_output=True, text=True).stdout
        self.assertIn("committee", text)  # OCR only reads this if the page was upright
        self.assertIn("infrastructure", text)

    def test_an_upright_page_is_left_alone(self):
        work = tempfile.mkdtemp(dir=self.tmp)
        info = {}
        src = os.path.join(self.tmp, "up.pdf")
        with open(src, "wb") as f:
            f.write(self._to_pdf(self._png_text(), "up_raw.pdf"))
        prepared, cuts = S.prepare_pdf(src, work, 1, info)
        self.assertEqual((prepared, cuts), (src, {}))
        self.assertEqual((info["rotated"], info["split"]), (0, 0))

    def _png_text(self):
        png = os.path.join(self.tmp, "upright.png")
        para = ("The committee reviewed the annual report and approved the budget for the next fiscal year. "
                "Several members asked for more detail about the infrastructure projects, and the chair promised "
                "a written summary before the end of the month.")
        subprocess.run(["convert", "-size", "900x1100", "xc:white", "-font", "DejaVu-Sans", "-pointsize", "34", "-fill", "black",
                        "-gravity", "NorthWest", "-annotate", "+40+40", "\n".join(textwrap.wrap(para, 40)),
                        "-units", "PixelsPerInch", "-density", "150", png], check=True)
        return png

    def test_version_file_constant_health_and_changelog_agree(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "VERSION")) as f:
            file_version = f.read().strip()
        self.assertRegex(file_version, r"^\d+\.\d+\.\d+$")
        self.assertEqual(S.VERSION, file_version)  # the server is deployed as one file, so it carries its own copy
        code, body = call(self.base, "GET", "/health")
        self.assertEqual((code, body["version"]), (200, file_version))
        with open(os.path.join(root, "CHANGELOG.md")) as f:
            self.assertIn(f"## [{file_version}]", f.read())  # every released version has a changelog entry

    def test_token_id_name_and_user_do_not_stay_in_state_or_log(self):
        self.add("ARQUIVO_0004")
        t = tok("bia")
        jid = self.submit(t, "ARQUIVO_0004")[1]["jobId"]
        st = self.wait(jid, t)
        everything = json.dumps(st) + json.dumps(self.logs)
        for secret in (t, "Relatorio", "perm_bia", "bia"):
            self.assertNotIn(secret, everything)
        self.assertNotIn("ARQUIVO_0004", json.dumps(self.logs))  # the id is returned to the owner as result_id, but never logged
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
            c, r = self.submit(ana, "ARQUIVO_0005", action="flatten")  # repeatable: a replace would make the file searchable
            self.assertEqual(c, 202)
            ids.append((r["jobId"], ana))
        c, _ = self.submit(ana, "ARQUIVO_0005", action="flatten")
        self.assertEqual(c, 429)
        c, r = self.submit(bia, "ARQUIVO_0005", action="flatten")
        self.assertEqual(c, 202)
        ids.append((r["jobId"], bia))
        FakeGoogle.gate.set()
        for jid, t in ids:
            self.assertEqual(self.wait(jid, t, 120)["status"], "done")
        self.assertEqual(self.submit(ana, "ARQUIVO_0005", action="flatten")[0], 202)  # freed up after finishing

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
            c, r = self.submit(t, "ARQUIVO_0005", action="flatten")  # flatten is repeatable: a replace would make the file searchable
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
