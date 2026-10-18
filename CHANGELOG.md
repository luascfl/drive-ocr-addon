# Changelog

All notable changes are listed here. The version lives in `VERSION` and in `server/ocr_server.py` (`VERSION`); a test keeps them equal, and the server reports it at `GET /health`. This project follows [Semantic Versioning](https://semver.org/) and keeps a changelog in the [Keep a Changelog](https://keepachangelog.com/) style. Versions below 1.0.0 may still change behavior between minor releases.
## [0.1.4] - 2026-10-18

### Fixed
- OCR now runs on the intact spread before a detected double-page scan is split. The final crop applies equally to the original scan and its text layer, keeping selection coordinates in the same coordinate system.


## [0.1.3] - 2026-10-18

### Added
- `deploy/update.sh`: checked deployment for the existing Oracle VM. It refuses to restart while jobs are busy or queued, validates release versions, checks public health through the cached host and IP, validates Python before installing, restores the previous server if systemd cannot restart it, then confirms the version. It prints the separate Apps Script update step.

## [0.1.2] - 2026-10-18

### Added
- The add-on displays its installed version below the title on the home, selected-file and job-status cards.

## [0.1.1] - 2026-10-18

### Fixed
- **Double-page scans were not split.** The gutter detector required an almost pure white gap (at most 0.2% dark pixels per column), but real scans have 1 to 3% of grain and shadow, so it found no gutter in any of the 29 pages of a real book scan. A gap is now "quiet" relative to the page's own text (under 30% of the median text-column ink, at most 6%), measured on a moving average. On that scan 27 of 29 spreads were detected correctly by the page alone (one found nothing and one guessed wrong).
- When at least three and at least half of the landscape pages have a detected gutter, missing cuts and guesses more than 8% of the width from the median use that median; the other cuts stay unchanged. This fixed a chapter opening with a narrow gutter and a photo page with an almost empty facing page (the guess was 69% of the width). This assumes a uniform book scan and can misclassify mixed-layout documents.

### Added
- `deploy/setup.sh`: idempotent setup of the server on a fresh Ubuntu 24.04 VM.

## [0.1.0] - 2026-10-16

First public release (testing phase: the add-on is not yet verified by Google, so Google shows an "unverified app" screen).

### Added
- Google Drive add-on (Apps Script) with one **Start OCR** button. If the add-on has no access to the file yet, the same button opens Drive's permission dialog and starts the OCR once the user allows it.
- OCR server (Python standard library, Tesseract, Poppler, qpdf, pypdf): the PDF is downloaded with the user's own OAuth token, prepared, OCR'd and written back. Jobs are asynchronous, so the card callback returns in about a second.
- **Replace in place:** the file's content becomes the searchable version. The original revision is pinned (`keepForever`) first and recorded in the file's `appProperties`; if it cannot be pinned, nothing is replaced.
- **Revert OCR** restores the original, byte for byte, only while the file is still exactly our result.
- **Flatten and re-OCR** writes a separate `<name> (flatten).pdf`, ignoring existing text, so an existing OCR can be compared with ours.
- Preparation steps, cheapest first: refuse a PDF that already has text on every page; straighten sideways pages (Tesseract orientation detection, majority vote over sampled pages); split double-page scans down the middle (dark binding shadow or a blank gap of at least 3% of the width); OCR only the pages without text.
- The text layer is overlaid on the original with `qpdf`, so the file grows about 1% instead of about 2.7 times. The two halves of a split page share the same images.
- Per-user authorization (token `aud` and `drive.file` scope), per-user limits (2 active jobs, 30 per 24 h), job ownership, and no document content, token or e-mail in logs.
- Public home page and privacy policy (GitHub Pages), MIT license.

### Known limits
- PDFs up to 50 MB and 60 pages; one job at a time with a queue of 5.
- A page whose CropBox differs from its MediaBox cannot get an overlay, so Start OCR leaves such a file alone (use Flatten).
- The double-page detector was tested on synthetic scans only.
- Languages requested by the add-on: Portuguese and English.
