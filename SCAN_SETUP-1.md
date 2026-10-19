# Marksheet Scanning — free OCR setup (replaces the old Anthropic-based notes)

## What changed
The scanner no longer needs Anthropic, OpenAI, Google or any paid key. Photos are read by **Tesseract.js**
(open source) **inside the teacher's browser**. Only the recognised text boxes are sent to your server, which
builds the table, matches pupils and (after the teacher confirms) saves marks through the existing routes.
Wizard steps, layout, colours, matching rules, review rules and the import route are unchanged.

| File | Action |
|---|---|
| `app.py` (uploaded as app-38.py) | **Replace.** Anthropic code removed; new `/api/marksheet-scan/structure` route; optional server Tesseract mode. |
| `index.html` (uploaded as index-53.html) | **Replace.** Browser OCR, photo clean-up, "Type marks by hand", confidence/max display, clearer import summary. |
| `SCAN_SETUP.md` | Replace the old notes. |

I did not receive `requirements.txt`, `render.yaml`, a Procfile or a Dockerfile, so I could not inspect your actual
Render configuration. Nothing in the default (browser) mode needs a new Python package or a new build step.
Pillow is only used by the optional server mode.

## Deploy on Render
1. Replace `app.py` and `index.html` in your repo with these files, commit, push (Render redeploys).
2. In Render → your service → Environment, **delete** `SCAN_OCR_PROVIDER` and `SCAN_ANTHROPIC_API_KEY`
   (and `ANTHROPIC_API_KEY` if you only added it for scanning). Leaving them is harmless: old values are ignored.
3. Wait for the deploy to go "Live". Phones that already have the app cached may need to close and reopen it
   (or reload twice) so the new `index.html` replaces the cached copy.

## First use needs internet
The first scan downloads Tesseract.js (cdnjs) plus the English language data (tesseract.js's default data host).
Browsers normally keep these afterwards, but this is not guaranteed, so do not rely on offline scanning.
If the download fails the teacher sees a clear message and can still use **Type marks by hand**.

### Optional: host the OCR files yourself
Copy these into `vendor/tesseract/` next to `index.html`; the app then tries them before the CDN:
`tesseract.min.js`, `worker.min.js`, `tesseract-core.wasm.js` (plus the `-simd` variants), `eng.traineddata.gz`.
The server serves that folder with the right headers. **I could not test self-hosting here** (no internet in my sandbox).

### Optional: OCR on the server instead
Render's normal Python runtime cannot install the `tesseract-ocr` program. Only if you move to a Docker deploy:
`apt-get install -y tesseract-ocr`, `pip install Pillow`, and set `SCAN_OCR_PROVIDER=tesseract`. Not needed otherwise.

## Test it on an Android phone
1. Open the app in Chrome, sign in, **Marks Entry → Scan Paper Marksheet**.
2. Pick year, term, class, exam → **Next**. 3. **Take photo** (or **Upload photos**). Flat page, good light, whole table, headings included.
4. Use Rotate / Crop / Clearer if needed → **Process images** (first time: wait for the download).
5. **Review results**: orange = check, red = fix. Choose the pupil for any unmatched row. Nothing is saved yet.
6. **Confirm and Import Marks**; check the summary, then open Marks Entry to see the marks.
7. Also try **Type marks by hand** and, with mobile data off, confirm manual entry still works.

## What to expect (honest limits)
* Clear **printed** tables: good. Ruled lines, mild tilt and shadows are cleaned up automatically.
* **Handwriting: unreliable** with free general-purpose OCR. Expect misread digits and names. That is why every value is shown with a confidence figure, uncertain/invalid ones are held back, and the teacher corrects them.
* A misread admission number is never guessed: the row is flagged "not found" and the teacher picks the pupil.
* The table needs a heading row (ADM NO, NAME, subjects). Unknown subject headings are never guessed; the teacher maps them.

## What I tested, and what I did not
Run in my sandbox, using real headless Chromium, the real `tesseract` program as a stand-in for Tesseract.js, and your real
matching/validation code: **23 backend checks and 35 browser checks passed** (scanner opens with no key, no red error,
photo preview, tilted/shadowed printed photo read, matching with leading zeros, wrong-class/unknown/weak-name safety,
over-maximum and non-numeric marks flagged, nothing saved before confirm, retry creates no duplicates, manual entry,
engine failure and config failure fallbacks).

**Not tested:** downloading and running the real Tesseract.js from the CDN; your real Flask + database import route
(unchanged, and my sandbox had no SQLAlchemy, so the harness used an emulation of its duplicate rules);
Render itself; a real Android device; real handwriting. Acceptance item 12 (builds and runs on Render) is therefore **unverified** until you deploy and try it.
