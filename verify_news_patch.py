"""NEWS-PATCH-OCT16 "THE GEMINI 4 MISS" — every test case of the ticket, T1 to T19.

    python verify_news_patch.py            every check, then the T1-T19 table
    python verify_news_patch.py --table    the T1-T19 table only

What is checked (the ticket's test cases and its edge cases):

  F1 one story    story_key (word order gone), same_story (two names AND most of
                  the words, within 36 h), 29 copies -> one row with
                  outlet_count 29 and a preferred outlet's link; the backfill
  F2 flags        NEWS_NAME_MIN_CHARS=4 (a 3-letter name never flags, logged
                  once); the per-pull flood cap and noisy_names
  F3 scoring      the pre-rank; 3 batches of 40, the rest carried and scored
                  first next cycle; a failed batch 2; the launch rule
  F4 breaking     1 a cycle, 4 a day, reset at IST midnight; an industry 5
                  needs two outlets unless the launch rule scored it
  F5 every hour   the oldest unclaimed slot, "late by N min", skipped after 3 h;
                  the cycle lock; the time cap
  F6 health       the 23:45 line's numbers against the temp DB; the test
                  channel only; the daily cap untouched
  F7 research     "Research published:": our researchers' papers and keyword
                  papers 4 by rule, never breaking, at most 2 a post, the rest
                  carried; no research, no heading
  T14 templates   tools/render_templates.py re-run and compared byte for byte
                  with docs/test-reports/templates-main.txt (a few minutes)

OFFLINE. A whole bot behind fake Discord on a throwaway database; the feeds are
canned, the scorer is a stand-in that answers from a table, and a real Sheets
or Drive call fails the run (tests/offline_guard.py). No feed is fetched, no
search is made, no model is called. The checks are in tests/news_patch_checks.py
and are shared with tests/test_news_patch.py.

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

GUARD = offline_guard.install_script()      # a real Sheets/Drive call fails the run

import logging  # noqa: E402

import news_patch_checks  # noqa: E402

logging.getLogger().setLevel(logging.ERROR)
QUIET = "--table" in sys.argv
CASES = range(1, 20)
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
        print(f"  {'PASS' if ok else 'FAIL'}  {name}"
              + ("" if ok else f": got {got!r}, want {want!r}"))


def table() -> int:
    print("\nT1-T19")
    bad = 0
    for n in CASES:
        got = results.get(n) or []
        word = "PASS" if got and all(got) else ("FAIL" if got else "MISSING")
        bad += 0 if word == "PASS" else 1
        print(f"  T{n:<2} {word:<7} ({sum(got)}/{len(got)} checks)")
    return bad


if QUIET:
    import contextlib
    import io

    with contextlib.redirect_stdout(io.StringIO()):
        asyncio.run(news_patch_checks.run(check))
else:
    asyncio.run(news_patch_checks.run(check))
uncovered = sum(1 for n in CASES if not results.get(n))
table()
print(f"\n{passed} check(s) passed" + (f", {failures} failed" if failures else "")
      + (f", {uncovered} test case(s) not covered" if uncovered else ""))
bad = failures + uncovered
print("ALL PASSED" if not bad else f"{bad} FAILED")
sys.exit(1 if bad else 0)
