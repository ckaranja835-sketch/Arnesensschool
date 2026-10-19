#!/usr/bin/env python3
"""Self-contained checks for the marksheet-scanner logic in app.py.
Needs only Python 3 (no Flask / SQLAlchemy / database): the pure functions are cut out of app.py and run.
Usage:  python test_scan_logic.py [path/to/app.py]
These use SYNTHETIC data shaped like the Grade 8 merit list. They do NOT prove accuracy on a real photo."""
import re, sys, json, difflib

SRC = open(sys.argv[1] if len(sys.argv) > 1 else "app.py", encoding="utf-8").read()
def between(a, b):
    i = SRC.index(a); return SRC[i:SRC.index(b, i)]
ns = {"re": re, "json": json, "difflib": difflib, "to_float": lambda v, d=0.0: (float(v) if v not in (None, "") else d)}
exec(between("def scan_norm_header", "def scan_validate_settings")
     + between("# ------------------------------------------------------- pure matching logic", "def scan_parse_provider_json")
     + between("def scan_normalize_extraction", "# ------------------------------------------------------------- OCR providers")
     + between("_SCAN_NUM_FIX = ", "def _scan_call_tesseract"), ns)
G = ns.__getitem__
fails = []
def check(name, got, want):
    if got != want:
        fails.append(name); print("FAIL", name, "got", got, "want", want)
    else:
        print("ok  ", name)

# 1. marks: digits only, performance code dropped
for raw, want in [("76 EE2", 76), ("97 EE1", 97), ("100 EE1", 100), ("60 ME1", 60), ("32 AE2", 32), ("18 BE2", 18),
                  ("76EE2", 76), ("18BE2", 18), ("41ME2~", 41), ("97 EEl", 97)]:
    k, v, _ = G("scan_parse_mark")(raw, 100); check(f"mark {raw!r}", (k, v), ("score", float(want)))
for raw in ["-", "", "   "]:
    k, v, _ = G("scan_parse_mark")(raw, 100); check(f"missing {raw!r} is not zero", (k in ("dash", "empty"), v), (True, None))
check("code alone is not a mark", G("scan_parse_mark")("EE2", 100)[0], "invalid")
check("two numbers in one cell flagged", G("scan_parse_mark")("76 97 EE2", 100)[0], "invalid")
check("above exam max flagged", G("scan_parse_mark")("76 EE2", 50)[0], "invalid")
check("BE2 is not read as 8", G("scan_split_mark")("BE2")["kind"], "nonnumeric")
check("O read for 0 is flagged", G("scan_split_mark")("7O")["substituted"], True)

# 2. admission numbers: four-digit strings
for raw, want in [("158", "0158"), ("170", "0170"), ("178", "0178"), ("185", "0185"), ("182", "0182"), ("951", "0951"),
                  ("964", "0964"), ("0001", "0001"), ("1234", "1234")]:
    check(f"admission {raw}", G("scan_normalize_admission")(raw, 4), (want, "ok"))
for raw in ["12345", "00158", "0000", "15A"]:
    check(f"admission {raw!r} rejected, not truncated", G("scan_normalize_admission")(raw, 4)[0], None)

# 3. columns: GEN / PTS / TOTAL / POINTS / RANK are never subjects
role = G("_scan_header_role")
for h in ["GEN", "PTS", "TOTAL", "POINTS", "RANK"]:
    check(f"{h} is not a subject", role(h) != "subject", True)
for h in ["CAS", "ISCI", "MATH", "KISW", "AGN", "ENGL", "REL", "SST"]:
    check(f"{h} is a subject", role(h), "subject")

# 4. synthetic word boxes laid out like the merit list
CW, H = 9, 16
def w(t, x, y): return {"t": t, "x0": x, "y0": y, "x1": x + CW * len(t), "y1": y + H, "c": 95}
heads = ["ADM NO", "LEARNER NAME", "GEN", "CAS", "ISCI", "MATH", "KISW", "AGN", "ENGL", "REL", "SST", "PTS", "TOTAL", "POINTS", "RANK"]
xs = [10, 90, 330, 400, 480, 560, 640, 720, 800, 880, 960, 1040, 1110, 1200, 1290]
words = []
for h, x in zip(heads, xs):
    off = 0
    for part in h.split(" "):
        words.append(w(part, x + off, 40)); off += CW * len(part) + 6
cells = ["76 EE2", "97 EE1", "100 EE1", "60 ME1", "32 AE2", "18 BE2", "88 EE2", "-"]
words += [w("158", 10, 100), w("ALICE", 90, 100), w("WANJIRU", 150, 100), w("F", 335, 100)]
for x, c in zip(xs[3:11], cells):
    if c == "-": words.append(w("-", x + 10, 100))
    else:
        n, code = c.split(" "); words += [w(n, x - 6, 100), w(code, x - 6 + CW * len(n) + 8, 100)]
words += [w("40", 1045, 100), w("571", 1115, 100), w("40", 1205, 100), w("1", 1295, 100)]
ex = G("scan_words_to_extraction")(words)
row = ex["rows"][0]["cells"]; cols = ex["columns"]
got = {cols[i]["header"]: row[i]["text"] for i in range(len(cols)) if cols[i]["role"] == "subject"}
check("table: only subject columns kept", list(got), ["CAS", "ISCI", "MATH", "KISW", "AGN", "ENGL", "REL", "SST"])
check("table: marks extracted", list(got.values()), ["76", "97", "100", "60", "32", "18", "88", "-"])
check("table: codes not pushed into the name", [row[i]["text"] for i in range(len(cols)) if cols[i]["role"] == "name"][0], "ALICE WANJIRU")

# 5. matching: legacy unpadded numbers, wrong class, missing marks, nothing guessed
subjects = [{"id": "m", "name": "Mathematics"}, {"id": "e", "name": "English"}]
students = {"a": {"id": "a", "admissionNo": "0158", "classId": "c8", "firstName": "Alice", "lastName": "W", "status": "Active"},
            "b": {"id": "b", "admissionNo": "951", "classId": "c8", "firstName": "John", "lastName": "O", "status": "Active"},
            "x": {"id": "x", "admissionNo": "0170", "classId": "c7", "firstName": "Other", "lastName": "C", "status": "Active"}}
ctx = {"width": 4, "threshold": 0.8, "exam": {"maxMarks": 100}, "class_id": "c8", "students": students, "subjects": subjects,
       "abbreviations": [], "existing": {}, "can_subject": lambda s: True, "privileged": True}
def cell(t, raw=None, conf=0.95, **k): return dict(text=t, raw=raw or t, conf=conf, **k)
payload = {"columns": [{"key": "M", "header": "MATH"}, {"key": "E", "header": "ENGL"}],
           "rows": [{"rid": 1, "admRaw": "158", "name": "ALICE", "cells": {"M": cell("76", "76 EE2"), "E": cell("-")}},
                    {"rid": 2, "admRaw": "951", "name": "JOHN", "cells": {"M": cell("60", "60 ME1"), "E": cell("-", asAbsent=True)}},
                    {"rid": 3, "admRaw": "170", "name": "OTHER", "cells": {"M": cell("80", "80 EE2")}},
                    {"rid": 4, "admRaw": "999", "name": "NOBODY", "cells": {"M": cell("80", "80 EE2")}}]}
R = {r["rid"]: r for r in G("scan_match_rows")(payload, ctx)["rows"]}
check("158 -> 0158 -> matched", (R[1]["normalizedAdm"], R[1]["studentId"]), ("0158", "a"))
check("older '951' record found through 0951", (R[2]["normalizedAdm"], R[2]["studentId"]), ("0951", "b"))
check("76 EE2 ready as 76", (R[1]["cells"]["M"]["status"], R[1]["cells"]["M"]["score"]), ("ready", 76.0))
check("dash stays missing (not zero, not imported)", (R[1]["cells"]["E"]["status"], R[1]["cells"]["E"]["score"]), ("missing", None))
check("dash becomes absent only when teacher chooses it", (R[2]["cells"]["E"]["status"], R[2]["cells"]["E"]["absent"]), ("ready", True))
check("other-class pupil refused", R[3]["idStatus"], "not_in_class")
check("unknown number: no student created or guessed", (R[4]["idStatus"], R[4]["studentId"]), ("not_found", None))

print("\nALL PASSED" if not fails else f"\n{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
