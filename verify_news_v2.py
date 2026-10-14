"""NFT2-1087 "SALEY AI NEWS FLOW" — every test case of the ticket, T1 to T83.

    python verify_news_v2.py            every check, then the T1-T83 table
    python verify_news_v2.py --table    the T1-T83 table only

What is checked (the ticket's section 15, and the edge cases of section 14):

  the name list   every company on Master Pipeline and every company and person
                  on Outreach PoCs, active row or not; the departures list; two
                  spellings, generic names, the watch list, researchers
  collection      the 14:00 big pull (when:1d) and the hourly small one
                  (when:2h); the window after a missed hour; a restart at
                  16:30; Google first; the 1.5 s gap; a 429
  flag and score  the flag is set by rule, with no model; the model's score;
                  a scorer that cannot run; the scoring prompt's table
  breaking        about us at 4, industry at 5, within the hour, any day, no
                  daily limit; the flood guard; never a drip message; the kill
                  switch; a story that is no longer new
  the daily post  five stories, ours first; the carry queue; nothing to send
                  is no post; the spread limits; the split
  rendering       the one line shape; an answer in the channel; a test run
  researchers     arXiv authors, the category filter, a 503, the 3 s gap; the
                  Google filter
  config          .env.example, a retired variable left in .env, the module
                  self-tests
  the templates   A, B, C, E, F and G compared to the ticket's text character
                  for character, with the test world's tags and a fixed day;
                  D is "no message at all"

OFFLINE. A whole bot behind fake Discord on a throwaway database; the feeds
and arXiv are canned (`feeds._get` and `arxiv._get` are replaced and every
request is recorded), the gaps are measured on a pretend clock, and the scorer
is a stand-in that answers from a table. No feed is fetched, no search is
made, no model is called, and a real Sheets or Drive call fails the run
(tests/offline_guard.py). The checks themselves are in tests/news_v2_checks.py
and are shared with tests/test_news_v2.py.

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

GUARD = offline_guard.install_script()      # canned source statuses; a real Sheets/Drive call fails the run

import logging  # noqa: E402

import news_v2_checks  # noqa: E402

logging.getLogger().setLevel(logging.ERROR)
QUIET = "--table" in sys.argv
CASES = range(1, 84)
failures = 0
passed = 0
results: dict = {n: [] for n in CASES}      # T number -> [ok, ok, ...]

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
        print(f"  {'PASS' if ok else 'FAIL'}  {name}"
              + ("" if ok else f": got {got!r}, want {want!r}"))


def table() -> int:
    """The T1-T83 table: PASS when every check named for the case passed."""
    print("\nT1-T83")
    bad = 0
    row = []
    for n in CASES:
        got = results.get(n) or []
        word = "PASS" if got and all(got) else ("FAIL" if got else "MISSING")
        bad += 0 if word == "PASS" else 1
        row.append(f"T{n:<2} {word:<7}")
        if len(row) == 6:
            print("  " + "  ".join(row))
            row = []
    if row:
        print("  " + "  ".join(row))
    return bad


if QUIET:
    import contextlib
    import io

    with contextlib.redirect_stdout(io.StringIO()):
        asyncio.run(news_v2_checks.run(check))
else:
    asyncio.run(news_v2_checks.run(check))
uncovered = sum(1 for n in CASES if not results.get(n))
table()
print(f"\n{passed} check(s) passed" + (f", {failures} failed" if failures else "")
      + (f", {uncovered} test case(s) not covered" if uncovered else ""))
bad = failures + uncovered
print("ALL PASSED" if not bad else f"{bad} FAILED")
sys.exit(1 if bad else 0)
