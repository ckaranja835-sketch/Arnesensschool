# Marksheet Scanning — digit-only marks and four-digit admission numbers

Free OCR (Tesseract.js in the teacher's browser, no API key) is unchanged. This update teaches the scanner to read the
school's printed stream merit lists (e.g. Grade 8 Red): `ADM NO | LEARNER NAME | GEN | CAS | ISCI | MATH | KISW | AGN | ENGL | REL | SST | PTS | TOTAL | POINTS | RANK`.
The four steps (Class & exam, Photos & reading, Review & fix, Imported), the import route, permissions and Manual Marks Entry are unchanged.

## Files to replace
| File | Action |
|---|---|
| `app.py` | **Replace** (scanner section 12b only; nothing else touched) |
| `index.html` | **Replace** (scanner script + its CSS only) |
| `SCAN_SETUP.md` | Replace the old notes |
| `test_scan_logic.py` | Optional: a checker you can run anywhere with `python test_scan_logic.py app.py` (needs no database) |

No new Python package, no database migration, no new environment variable.

## Deploy on Render
1. Replace `app.py` and `index.html` in your repo, commit, push. Render redeploys.
2. Wait until the service shows **Live**. Phones that cached the old page: close and reopen the app (or reload twice).
3. Optional, admins only: open `https://<your-app>/api/marksheet-scan/admission-audit` while signed in (see "Older admission numbers").
4. Do the Android test below before relying on it.

## What changed
**Marks (digits only).** `76 EE2`→76, `97 EE1`→97, `100 EE1`→100, `60 ME1`→60, `32 AE2`→32, `18 BE2`→18. Performance codes are never imported.
A number and the code beside it are kept together, so neither lands in a neighbouring column or in the learner's name.
The original OCR text is shown under every mark (`OCR: 76 EE2`) and stays available while the teacher corrects the value.
A letter read in place of a digit (O for 0, l for 1…) is corrected only inside the mark and the cell is flagged orange for checking.
Two numbers in one cell, a code with no number, or a mark above the exam maximum are flagged red.
The maximum is the exam's configured maximum (and any per-subject limit saved in scan settings), not a fixed 100.

**Columns.** GEN is gender and is ignored. PTS, TOTAL, POINTS and RANK are never subject marks. Headings are detected on each page, not by position.
Standard abbreviations (MATH, ENGL, KISW, ISCI, CAS, AGN, SST, REL…) map to your subject only when **exactly one** subject fits; the review screen lists these so the teacher can change them.
`REL` with both CRE and IRE in the system stays "choose subject". Saved abbreviations and subject codes still win over the standard list.

**Missing marks.** A dash or blank is *missing*, never 0. It is not imported. The teacher can press **Record as absent** on that one cell, or type a mark.
Missing marks are counted on the review summary. An existing saved mark is never overwritten by a dash; replacing a different saved mark still needs an explicit "Replace".

**Admission numbers.** Always handled as text and shown as four digits: 158→`0158`, 951→`0951`, 0001→`0001`.
More than four digits (`12345`, `00158`) is flagged, never truncated. `0000` is rejected. The "On paper" cell is editable; editing it re-matches against the selected class.
No student is ever created by the scanner. No match = "Student not found" for the teacher to fix by choosing the pupil.

**Review screen.** Four-digit admission, learner name, subject (column heading), raw OCR text, extracted mark (editable), match status, confidence, maximum.
Nothing is saved until **Confirm and Import Marks** and its confirmation prompt.

## Older admission numbers (no leading zeros in the database)
Matching now works without changing any record: a stored `951` is found by the paper's `951`/`0951`. If a stored `158` and `0158` both exist in a class, the row is shown **Ambiguous** and the teacher chooses.
Optional clean-up plan (do not run unless the report is clean):
1. As Administrator/Head/Deputy open `/api/marksheet-scan/admission-audit` (read-only; changes nothing).
2. `safeToMigrate: true` means every stored number can be padded to four digits with no collisions.
3. Take a backup, then correct the listed students' admission numbers in the Students screen. Marks and fees are linked by student ID, not admission number, so history is kept.
4. If `collisions` or `unusable` are listed, fix those by hand first.

## Android test
1. Chrome → sign in → **Marks Entry → Scan Paper Marksheet**. Pick year, term, class, exam → **Next**.
2. **Take photo**/**Upload photos** (flat page, good light, headings in frame) → **Process images** (first use downloads the reader; needs internet).
3. **Review results**: check `76 EE2`→76, `100 EE1`→100, admission `158`→`0158`, dashes marked "missing", PTS/TOTAL/POINTS/RANK absent.
4. Correct anything orange/red, then **Confirm and Import Marks**. Open Marks Entry to confirm. Also try **Type marks by hand**.

## What was and was not tested
Run in a sandbox with synthetic data shaped like the merit list (hand-made word boxes, and a rendered table image read by the real `tesseract` program):
the mark parser (including glued text like `60ME1`, `41ME2~`, `97 EEl`), admission normalising, column roles, table building (codes not leaking into names; GEN/PTS/TOTAL/POINTS/RANK excluded),
matching (legacy numbers, other class, unknown number, ambiguity, dash/absent), and the browser's ingest → match → import-list logic. `python test_scan_logic.py app.py` repeats the core checks.

**Not tested:** the real Grade 8 Red Merit List photo (no image was supplied, so **OCR accuracy on your real sheets is unverified**); the real Tesseract.js in a browser; the Flask routes and your database (no SQLAlchemy/network in the sandbox — the new audit route and changed routes were only syntax/name-checked); the on-screen layout in a browser or phone; Render itself.
The optional per-digit confidence uses Tesseract.js symbol data if present; if your version does not provide it, the scanner falls back to word confidence.
