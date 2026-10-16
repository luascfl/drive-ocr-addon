# OCR for Drive

A Google Drive add-on that makes scanned PDFs searchable. Select a PDF in Drive and click **Start OCR**: the add-on straightens sideways pages, splits double-page scans down the middle, runs OCR, and **replaces the file** with the searchable version. The result keeps the original images and adds an invisible text layer, so it stays about 1% larger than the source instead of re-encoding every page. A **Revert OCR** button takes the place of Start OCR afterwards and restores the original byte for byte. **Flatten and re-OCR** writes a separate `<name> (flatten).pdf` so an existing OCR can be compared with ours.

**Status: testing phase.** The add-on has not been through Google's OAuth verification, so Google shows an "unverified app" screen. It works for the developer's own account; it is not published on the Marketplace.

## How it works

```
Drive card (Apps Script)        OCR server (Oracle Always Free VM)
  "Start OCR"  ── file id + action + user's OAuth token ──►  POST /jobs   (202, returns at once)
                                                      │ validate token (aud + drive.file), identify user
                                                      │ download the PDF from Drive with the user's token
                                                      │ straighten sideways pages, split double-page spreads
                                                      │ per page: pdftoppm → tesseract (text layer only)
                                                      │ qpdf --overlay on the (prepared) original
                                                      └ pin the original revision, then replace the file
  "Revert OCR" ── action revert ──────────────────►  restore the pinned revision (only if the file is still our result)
  "View OCR status" ──────────────────────────────►  GET /jobs/<id>   (only the owner can see a job)
```

- The card callback never carries the PDF: Apps Script card callbacks stop at 30 s, so it only sends the file id and returns in about one second.
- One button does everything. If the add-on has no access to the file yet, Start OCR opens Drive's permission dialog and the OCR starts by itself once you allow it.
- Order of the steps, chosen to do the cheap checks first: (1) refuse a PDF that already has text on every page; (2) straighten sideways pages (Tesseract orientation detection on up to 5 sampled pages, majority vote); (3) split double-page spreads (a dark binding shadow or a blank gap of at least 3% of the width, found on a 60 DPI render, cut through its middle; no reliable gutter means no split); (4) OCR only the pages that have no text; (5) overlay the text layer on the original with `qpdf` (no re-encoding); (6) replace the file.
- **Replace is reversible.** Before replacing, the server pins the original revision (`keepForever`) and stores its id, the md5 of the result and the date in the file's `appProperties`. **Revert OCR** restores that revision, but only while the file is still exactly our result: an edit made after the OCR is never thrown away. If the original cannot be pinned, nothing is replaced.
- Safety stops: the file changed while it was processed (`file_changed`), no permission to edit it (`drive_forbidden`), and a PDF whose text layer cannot be overlaid without re-encoding its pages (a CropBox different from the MediaBox) is left alone (`overlay_unavailable`); use Flatten for those.
- **Flatten and re-OCR** ignores any text the PDF already has, rasterizes every page, runs OCR again and writes a new file next to the original, only when Drive confirms the original's folder (`no_parents` otherwise).

## Privacy

Document content is never stored by the server: the PDF and rendered pages live in a temporary directory for the duration of the job and are removed afterwards (a plain delete, not a secure wipe). The user's token is kept only in memory during the job. In the user's own Drive, replacing a file keeps the original as a pinned earlier version (it counts toward the user's storage until reverted) and adds three small private properties to the file (`ocrPrevRev`, `ocrMd5`, `ocrAt`). Logs hold sizes, page counts, timings and error codes, never file names, ids, e-mail addresses or text. See [the privacy policy](docs/privacy.html).

## Limits

Per user: 2 active jobs and 30 jobs per 24 hours. Per file: 50 MB and 60 pages. A single job runs at a time and up to 5 wait in the queue. Measured on the smallest Oracle Always Free VM (1/8 OCPU, 1 GB): about 15 s per page, 51 pages in 12.6 minutes, around 550 MB of RAM in use.

## Repository layout

| Path | Contents |
|---|---|
| `addon/` | Apps Script add-on (`Code.gs`, `appsscript.json`). `__OCR_URL__` is a placeholder for the server URL, replaced locally when pasting into the editor |
| `server/` | The OCR service (`ocr_server.py`, standard library only) and its tests |
| `scripts/` | Local pipeline, synthetic sample generators and accuracy tooling (CER and WER) |
| `web/` | An in-browser OCR experiment (PDF.js, Tesseract.js, pdf-lib) kept as a measured alternative |
| `samples/` | Synthetic sample PDFs |
| `docs/` | Public pages (home and privacy policy) |
| `LICENSE` | MIT license |

## Running the tests

The server tests run against a fake Google (token info, Drive and upload endpoints), so they need no network and no Google account.

```bash
sudo apt-get install tesseract-ocr tesseract-ocr-por tesseract-ocr-eng tesseract-ocr-osd poppler-utils qpdf python3-pypdf imagemagick
python3 -m unittest server.test_server -v
```

Tests that depend on `qpdf` or `pypdf` are skipped when they are missing. Without `qpdf` the server cannot add a text layer to the original, so Start OCR is refused (`overlay_unavailable`) and only Flatten works; without `pypdf` double-page spreads are not split.

## Running the server

```bash
OCR_TOKEN=<24+ random chars, protects /process only> \
OCR_ALLOWED_AUD=<OAuth client id of your add-on's Cloud project> \
python3 server/ocr_server.py
```

Put an HTTPS reverse proxy in front of it. Other settings: `OCR_REQUIRED_SCOPE` (default `https://www.googleapis.com/auth/drive.file`), `OCR_USER_ACTIVE_MAX`, `OCR_USER_DAILY_MAX`, `OCR_MAX_MB`, `OCR_MAX_PAGES`, `OCR_QUEUE_MAX`, `OCR_DPI`, `OCR_TEXT_MIN_CHARS`. Rejected tokens are logged as `auth_rejected` with the `aud` and scope names (never the token), which is how you read the client id to configure.

## Setting up the add-on

1. Create an Apps Script project, paste `addon/Code.gs` and `addon/appsscript.json`, replace `__OCR_URL__` with your server URL.
2. Link the script to your own Google Cloud project (Project Settings), then run `authorize()` once from the editor to grant every scope.
3. Deploy it as a test add-on and open Drive.

Languages: the add-on currently asks for Portuguese and English (`por+eng`). The server accepts any installed Tesseract language code.

## License

[MIT](LICENSE). Note that Tesseract (Apache-2.0), Poppler (GPL) and qpdf (Apache-2.0) are separate programs the server calls; they are not bundled in this repository.
