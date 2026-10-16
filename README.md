# OCR for Drive

A Google Drive add-on that makes scanned PDFs searchable. Select a PDF in Drive, click **Start OCR**, and a copy named `<name> (OCR).pdf` appears next to the original. The copy keeps the original images and adds an invisible text layer, so it stays about 1% larger than the source instead of re-encoding every page.

**Status: testing phase.** The add-on has not been through Google's OAuth verification, so Google shows an "unverified app" screen. It works for the developer's own account; it is not published on the Marketplace.

## How it works

```
Drive card (Apps Script)        OCR server (Oracle Always Free VM)
  "Start OCR"  ── file id + user's OAuth token ──►  POST /jobs   (202, returns at once)
                                                      │ validate token (aud + drive.file), identify user
                                                      │ download the PDF from Drive with the user's token
                                                      │ per page: pdftoppm → tesseract (text layer only)
                                                      │ qpdf --overlay on the ORIGINAL pdf
                                                      └ upload "<name> (OCR).pdf" into the original's folder
  "View OCR status" ──────────────────────────────►  GET /jobs/<id>   (only the owner can see a job)
```

- The card callback never carries the PDF: Apps Script card callbacks stop at 30 s, so it only sends the file id and returns in about one second.
- The server writes the result only if Drive confirms the original's parent folder. Creating a file without `parents` lands in the root of My Drive, so the job fails with `no_parents` instead.
- Pages that already have text are skipped, and a PDF that is searchable on every page is refused (`already_searchable`).
- If the overlay is unsafe (a page whose CropBox differs from its MediaBox) or `qpdf` fails, the server falls back to full mode, which re-embeds the page images and makes the file roughly 2.7 times larger.

## Privacy

Document content is never stored: the PDF and rendered pages live in a temporary directory for the duration of the job and are removed afterwards (a plain delete, not a secure wipe). The user's token is kept only in memory during the job. Logs hold sizes, page counts, timings and error codes, never file names, ids, e-mail addresses or text. See [the privacy policy](docs/privacy.html).

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

## Running the tests

The server tests run against a fake Google (token info, Drive and upload endpoints), so they need no network and no Google account.

```bash
sudo apt-get install tesseract-ocr tesseract-ocr-por tesseract-ocr-eng poppler-utils qpdf imagemagick
python3 -m unittest server.test_server -v
```

Tests that depend on `qpdf` are skipped when it is missing, and the server then always uses full mode.

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

Not chosen yet. Until a license file is added, all rights are reserved.
