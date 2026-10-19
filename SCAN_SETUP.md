# AI Marksheet Scanning — setup notes

## 1. Install
    pip install Pillow          # required: validates, auto-rotates, strips EXIF, re-encodes photos
    # optional, only for SCAN_OCR_PROVIDER=tesseract:  apt install tesseract-ocr

## 2. Environment variables (.env)
    SCAN_OCR_PROVIDER=anthropic            # recommended: reads handwritten tables
    SCAN_ANTHROPIC_API_KEY=sk-ant-...      # (ANTHROPIC_API_KEY also accepted). Server-side only.
    SCAN_ANTHROPIC_MODEL=claude-sonnet-5-5 # optional
    SCAN_RESTRICT_TEACHERS=1               # 1 = teachers import only for their own classes/subjects (default)
    SCAN_MAX_IMAGE_MB=8  SCAN_MAX_IMAGES=20  SCAN_OCR_TIMEOUT=90  SCAN_TMP_MAX_AGE=300
    SCAN_TMP_DIR=/var/tmp/arnesens_scan    # optional; must NOT be inside the web folder
Without credentials the scanner shows a clear "not set up" message; nothing pretends to work.
`SCAN_OCR_PROVIDER=tesseract` keeps photos on the server (no external service) but only reads
printed text reliably — handwriting accuracy is poor, so every value gets flagged.

## 3. Privacy (what actually happens)
* Photo → browser shrinks it → sent as a raw request body (no multipart temp spooling) → validated →
  written to a private 0700 temp folder under a random name → read by the OCR provider → **deleted in a
  `finally` block** (success, error, timeout). The reply says whether deletion was verified.
* A janitor deletes strays older than SCAN_TMP_MAX_AGE; the folder is purged at start-up and exit.
* Images are never in the database, Backblaze, logs, backups or any public URL. Only extracted
  text/marks are kept (in the browser, until you import); only verified marks reach the `marks` store.
* With the `anthropic` provider the photo leaves the server for the moment of reading. The UI says so.
  For strict retention, enable zero-data-retention on your Anthropic account (an account setting, not code).

## 4. Using it
Marks Entry → **Scan Paper Marksheet**. Admin sets abbreviations / admission width under
**Settings → Marksheet Scanning Settings**. Default width 4: paper "12" → "0012". Registered
admission numbers are never modified.
