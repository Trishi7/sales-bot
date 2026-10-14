"""NFT2-1090 "SALEY VOICE V2: PERSONA, VOICE BOOK, CONTEXT, EMPATHY" — every test case, T1 to T66.

    python verify_voice_book.py              every check, then the T1-T66 table
    python verify_voice_book.py --table      the T1-T66 table only

What is checked (the ticket's section 14, and the edge cases of section 13):

  the voice book      parsed by tag; missing or empty -> the card alone; an instruction-shaped entry
                      skipped; six entries by the path and the label, the same six for the same seed;
                      VOICE_SOURCE=channel keeps the old profile path; "how do you sound", "refresh voice"
  greetings           "Hi Vaishnavi, good morning." — the name from the roster, the time of day in IST,
                      no task, no list, no offer; "Hi there" for a stranger; a greeting in front of a
                      question; small talk once, differently the second time, a 👍 the third
  empathy             the rule signals, the light model's label, the merge, said once, cleared by "I'm
                      fine now", never holding a task back
  the checks in code  clichés, pushy words and shapes, deferrals, assumptions, the Umm rule, one emoji
  the context         the WHAT HAPPENED LATELY block, marked as data; a failed channel read; the sheet diff
  the prompt log      one file per compose, both attempts in it, the daily prune, "show the prompt for that"
  questions not votes R-f; "sure, add them", "not now", "the first two", "all but Acme", two Daisys,
                      "maybe", "why?"
  config, templates   .env.example; the module self-tests; T66 — every R1-R14 post byte-identical to main

T66 READS TWO FILES rendered by tools/render_templates.py: docs/test-reports/templates-main.txt (from a clean
copy of main, before any change) and docs/test-reports/templates-branch.txt (from a clean copy of this
branch). Rendering takes the better part of an hour, so it is done once, beside this script.

OFFLINE: a whole bot behind fake Discord on a throwaway database; the speaking paths are the real
`llm.LLM` over a stand-in client; no model is called, no feed fetched, no channel read; a real Sheets or
Drive call fails the run (tests/offline_guard.py). The checks themselves are in tests/voice_book_checks.py
and are shared with tests/test_voice_book.py. T54 (the judge) needs a real key and is skipped without one.

Ends ALL PASSED or N FAILED; a T that no check covers counts as a failure.
"""
import asyncio
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "tests"))
os.chdir(HERE)
os.environ.setdefault("DISCORD_TOKEN", "x")
os.environ["ANTHROPIC_API_KEY"] = "x"          # never a real key: nothing here calls a model
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import offline_guard  # noqa: E402

GUARD = offline_guard.install_script()

import logging  # noqa: E402

import voice_book_checks  # noqa: E402

logging.getLogger().setLevel(logging.ERROR)
QUIET = "--table" in sys.argv
CASES = range(1, 67)
failures = 0
passed = 0
results: dict = {n: [] for n in CASES}
_T_RE = re.compile(r"^((?:T\d+)(?:/T\d+)*)\b")


def check(name, got, want=True):
    global failures, passed
    ok = (got == want)
    failures += 0 if ok else 1
    passed += 1 if ok else 0
    found = _T_RE.match(str(name))
    for t in (found.group(1).split("/") if found else []):
        results.setdefault(int(t[1:]), []).append(ok)
    if not QUIET or not ok:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f": got {got!r}, want {want!r}"))


def table() -> None:
    print("\nT1-T66")
    row = []
    for n in CASES:
        got = results.get(n) or []
        word = "PASS" if got and all(got) else ("FAIL" if got else "MISSING")
        row.append(f"T{n:<2} {word:<7}")
        if len(row) == 6:
            print("  " + "  ".join(row))
            row = []
    if row:
        print("  " + "  ".join(row))


if QUIET:
    import contextlib
    import io

    with contextlib.redirect_stdout(io.StringIO()):
        asyncio.run(voice_book_checks.run(check))
else:
    asyncio.run(voice_book_checks.run(check))
uncovered = sum(1 for n in CASES if not results.get(n))
table()
print(f"\n{passed} check(s) passed" + (f", {failures} failed" if failures else "")
      + (f", {uncovered} test case(s) not covered" if uncovered else ""))
bad = failures + uncovered
print("ALL PASSED" if not bad else f"{bad} FAILED")
sys.exit(1 if bad else 0)
