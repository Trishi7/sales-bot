"""NFT2-1095 "SALEY CANNOT ANSWER GENERAL SHEET QUESTIONS: ADD sheet_columns AND sheet_query" — T1 to T17.

    python verify_sheet_query.py            every check, then the T1-T17 and E1-E10 tables
    python verify_sheet_query.py --table    the tables only
    python verify_sheet_query.py --full     also T17's long runs: verify_profile_lookup.py, verify_poc_lookup.py
                                            and pytest -q tests/. verify_poc_lookup.py IS LIVE: real model calls
                                            and real web searches (a few cents). Without --full, T17 covers the
                                            module self-tests and those three are run by hand.

What is checked, on the Outreach PoCs tab as it stood on 13 Oct 2026 (tests/fixture_outreach_2026_10_13.py,
no emails or phone numbers) and the real Master Pipeline tab:

  sheet_columns   35 headers in order, letter and role; the 5 Next Steps values with their counts
  sheet_query     equals / contains_any / not_empty / after / before / limit; the 20 and the 8; only the
                  columns asked for; "researching" -> "Did you mean Research the PoC?"; a column that does not
                  exist names the ones that do; two columns that match are a question; unreadable dates counted
  the engine      the 15 Oct question through on_message -> engine, live and test mode: a bullet list of the 8
  read-only       a write-looking input is refused; nothing reaches a write path; locks and templates untouched
  query_tracker   all 8 filters return what they returned on main
  the row shape   every sheet tool keys rows by the sheet header with the role in brackets

OFFLINE: a whole bot behind fake Discord on a throwaway database (tests/replies_world.py); the sheet is the
fixture, parsed by the real parser; the model is a stand-in; a real Sheets or Drive call fails the run
(tests/offline_guard.py). The checks are in tests/sheet_query_checks.py, shared with tests/test_sheet_query.py.

Ends ALL PASSED or N FAILED; a T that no check covers counts as a failure.
"""
import asyncio
import contextlib
import io
import logging
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "tests"))
os.chdir(HERE)
os.environ.setdefault("DISCORD_TOKEN", "x")
os.environ["ANTHROPIC_API_KEY"] = "x"          # never a real key: nothing in-process calls a model
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import offline_guard  # noqa: E402

GUARD = offline_guard.install_script()

import sheet_query_checks  # noqa: E402

logging.getLogger().setLevel(logging.ERROR)
QUIET = "--table" in sys.argv
FULL = "--full" in sys.argv
CASES = range(1, 18)
EDGES = range(1, 11)
failures = 0
passed = 0
results: dict = {n: [] for n in CASES}
edges: dict = {n: [] for n in EDGES}
_T_RE = re.compile(r"^((?:[TE]\d+)(?:/[TE]\d+)*)\b")


def check(name, got, want=True):
    global failures, passed
    ok = (got == want)
    failures += 0 if ok else 1
    passed += 1 if ok else 0
    found = _T_RE.match(str(name))
    for t in (found.group(1).split("/") if found else []):
        (results if t[0] == "T" else edges).setdefault(int(t[1:]), []).append(ok)
    if not QUIET or not ok:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f": got {got!r}, want {want!r}"))


TITLES = {
    1: "sheet_columns(\"Outreach PoCs\")", 2: "Next Steps equals \"research the poc\"",
    3: "T2 and Based contains_any UK/USA", 4: "columns = Name, Company/Uni, Designation",
    5: "Next Steps equals \"researching\"", 6: "Meeting Date after 2026-10-01", 7: "Deal Status not_empty",
    8: "limit 300", 9: "column \"Nonsense\"", 10: "the 15 Oct question through the engine",
    11: "write op / write-looking input", 12: "query_tracker filters", 13: "row shape",
    14: "two columns match \"Next Steps\"", 15: "Master Pipeline: Priority equals P1",
    16: ".env.example unchanged", 17: "self-tests, verify scripts, pytest",
}
EDGE_TITLES = {
    1: "two columns match the name", 2: "a value not in the dropdown", 3: "\"UK or USA\" in Based",
    4: "mixed date formats", 5: "more than 200 matches", 6: "a tab that is not readable",
    7: "a column that does not exist", 8: "a question across two tabs", 9: "the 327 FALSE-only rows",
    10: "a header that changes",
}


def table(label, cases, got_by, titles) -> int:
    print(f"\n{label}")
    bad = 0
    for n in cases:
        got = got_by.get(n) or []
        word = "PASS" if got and all(got) else ("FAIL" if got else "MISSING")
        bad += word != "PASS"
        print(f"  {label[0]}{n:<3} {word:<8} {titles.get(n, '')}  ({len(got)} check{'s' if len(got) != 1 else ''})")
    return bad


if QUIET:
    with contextlib.redirect_stdout(io.StringIO()):
        asyncio.run(sheet_query_checks.run(check, full=FULL))
else:
    asyncio.run(sheet_query_checks.run(check, full=FULL))
if not FULL:
    print("\n  NOTE  T17 ran the module self-tests only; verify_profile_lookup.py, verify_poc_lookup.py (live) and "
          "pytest -q tests/ are run with --full or by hand.")
bad_t = table("T1-T17", CASES, results, TITLES)
bad_e = table("E1-E10 (the ticket's edge cases)", EDGES, edges, EDGE_TITLES)
print(f"\n{passed} check(s) passed" + (f", {failures} failed" if failures else ""))
bad = failures + sum(1 for n in CASES if not results.get(n)) + sum(1 for n in EDGES if not edges.get(n))
print("ALL PASSED" if not bad else f"{bad} FAILED")
sys.exit(1 if bad else 0)
