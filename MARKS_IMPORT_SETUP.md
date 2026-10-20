# Import Marks from PDF / Excel / CSV  (replaces SCAN_SETUP.md)

## Files to replace
| File | Action |
|---|---|
| `app.py` | **Replace.** Scanner/OCR section removed; new `/api/marks-import/*` routes. Nothing else changed (one `scan_startup()` call removed). |
| `index.html` | **Replace.** Camera/photo/OCR wizard replaced by *Import Marks* (upload -> preview -> confirm). |
| `requirements.txt` | **Edit, don't replace** - add the lines in `requirements_additions.txt`. |
| `SCAN_SETUP.md` | Delete; this file replaces it. |

I did not receive `requirements.txt`, `render.yaml` or a Procfile, so I could not inspect your real Render config.

## Deploy on Render
1. Add `pdfplumber` and `openpyxl` (optionally `xlrd`) to `requirements.txt`; replace `app.py` and `index.html`; commit and push.
2. Render > Environment: delete `SCAN_OCR_PROVIDER`, `SCAN_ANTHROPIC_API_KEY`, `ANTHROPIC_API_KEY` (harmless if left; ignored).
3. Recommended start command (big PDFs can take several seconds): `gunicorn app:app --timeout 120`.
4. After "Live", phones with a cached copy may need to close/reopen the app once.

Optional env vars: `MARKS_IMPORT_MAX_MB` (10), `MARKS_IMPORT_MAX_PDF_PAGES` (40), `MARKS_IMPORT_PARSE_SECONDS` (40), `MARKS_IMPORT_RESTRICT_TEACHERS` (1; old `SCAN_RESTRICT_TEACHERS` still honoured).
Saved subject abbreviations are kept (same settings record). The old "confidence threshold" setting is ignored.
Uploaded files are processed in memory only - never written to disk, B2, the database or logs - so there is nothing to clean up. B2 and all other modules are untouched.

## Test it (use `sample_files/`)
Marks Entry > **Import Marks (PDF / Excel)**.
1. Pick year/term/class/exam > Next (class needs learners 0158, 0951, 0001..., or use your own marksheet).
2. Upload `wide.xlsx` > Read file > Preview. Check: "file 158 -> 0158"; "76 EE2" shows 76; dashes/blanks grey and empty; TOTAL/PTS/RANK absent; unknown headings (ISCI, KISW...) ask you to choose a subject.
3. Upload `ruled_multipage.pdf` (3 pages) and `plain_multipage_header_once.pdf` (heading only on page 1): all pages appear.
4. Upload `scanned_image_only.pdf`: "no selectable text" message with Excel / manual-entry options.
5. Nothing saves until **Confirm and Import Marks**. Re-importing the same file skips same-value marks; different saved values are held back unless you choose Replace.

## What I tested, and what I did not
I ran the real code from your `app.py` and the real scanner script/CSS from your `index.html`:
* 76 backend checks: admission normalisation (158->0158, 951->0951, 0001, 158.0, 12345 flagged), mark cleaning (76 EE2, 100 EE1, ME/AE/BE, dashes, blanks, AB, over-max), matching, wrong-class/archived/unknown learners, name mismatch, conflicts, existing-mark protection, teacher class/subject permissions.
* 13 parser checks: xlsx (wide + long, 2 sheets), csv, text PDFs (ruled, borderless, right-aligned, multi-page, heading on page 1 only), image-only PDF, damaged/encrypted PDF, fake extensions, zip-bomb xlsx, 40-page cap (about 6 s for 40 pages).
* 9 route checks in a real Flask request (size limit, permissions, bad exam/class, filename sanitising).
* 18 headless-Chromium checks at phone width: wizard opens with no OCR/key, bad type rejected, image-only PDF message, xlsx end-to-end, nothing saved before confirm, existing mark not overwritten, no zero marks created.

**Not tested:** your real Flask + SQLAlchemy + database (not installable in my sandbox; the import DB logic ran against a small in-memory stand-in); **.xls** (xlrd not installed - only the "not available" message ran); **Render itself** (acceptance item 15 is unverified until you deploy); your real school files (only my generated ones); PDFs with wrapped multi-line cells beyond simple cases.

## Honest limits
* Only PDFs with selectable text work. Scanned/photographed PDFs are refused with guidance - no OCR, by design.
* PDF layout detection needs a heading row (ADM NO and/or LEARNER NAME plus subject headings). Unusual layouts may need Excel; the preview lets teachers fix mappings.
* Excel formulas are read as their last saved values.

## Left alone on purpose
The general importer (students, teachers, subjects, fees, attendance; "MULTI-FORMAT IMPORT WIZARD" in `index.html`) still has an optional free Tesseract.js fallback for scanned PDFs. It is not used for marks and needs no key. I can remove it if you want.
