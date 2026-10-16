"""NEWS-ANSWER-OCT16 "A QUESTION GETS THE REAL TOP 5" — every test case, T1 to T16.

    python verify_news_window.py            every check, then the T1-T16 and B1-B20 tables
    python verify_news_window.py --table    the tables only

NEWS-ANSWER-OCT16-B ("a named day is that day only") is checked here too, as
B1-B20 (the ticket's T1-T20), in tests/news_window_b_checks.py.

What is checked (the ticket's test cases and its edge cases):

  the window      news.parse_window: last N hours, today, yesterday, this
                  morning, past N days, this week, last month (capped)
  the choice      news.choose_answer: the top of the window by score then newest,
                  posted stories included; total and remaining; the next page
  the tail        "There are N more in the <window>. These are the top 5. Would
                  you like to see the rest?" in an answer only, never when
                  nothing is left
  the bot         the plain path with a window and no model call; "yes" / "show
                  me the rest" / "next" within 30 minutes, after 30 minutes, after
                  the last page; two channels; "top 3 news today"; a topic with
                  nothing in the window; the todays_news tool's hours and count;
                  what an answer records
  T16 templates   the daily post and the breaking post byte-identical to
                  docs/test-reports/templates-main.txt (re-renders every post:
                  a few minutes)

OFFLINE. A whole bot behind fake Discord on a throwaway database with a seeded,
scored feed store: no feed is fetched, no search is made, no model is called,
and a real Sheets or Drive call fails the run (tests/offline_guard.py). The
checks are in tests/news_window_checks.py, shared with tests/test_news_window.py.

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

import news_window_b_checks  # noqa: E402
import news_window_checks  # noqa: E402

logging.getLogger().setLevel(logging.ERROR)
QUIET = "--table" in sys.argv
CASES = range(1, 17)
failures = 0
passed = 0
B_CASES = range(1, 21)
results: dict = {n: [] for n in CASES}
b_results: dict = {n: [] for n in B_CASES}

_T_RE = re.compile(r"^((?:[TB]\d+)(?:/[TB]\d+)*)\b")


def check(name, got, want=True):
    global failures, passed
    ok = (got == want)
    failures += 0 if ok else 1
    passed += 1 if ok else 0
    found = _T_RE.match(str(name))
    for t in (found.group(1).split("/") if found else []):
        (b_results if t[0] == "B" else results).setdefault(int(t[1:]), []).append(ok)
    if not QUIET or not ok:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}"
              + ("" if ok else f": got {got!r}, want {want!r}"))


def table(title: str, prefix: str, cases, res: dict) -> int:
    print(f"\n{title}")
    bad = 0
    for n in cases:
        got = res.get(n) or []
        word = "PASS" if got and all(got) else ("FAIL" if got else "MISSING")
        bad += 0 if word == "PASS" else 1
        print(f"  {prefix}{n:<2} {word:<7} ({sum(got)}/{len(got)} checks)")
    return bad


async def both() -> None:
    await news_window_checks.run(check)
    await news_window_b_checks.run(check)


if QUIET:
    import contextlib
    import io

    with contextlib.redirect_stdout(io.StringIO()):
        asyncio.run(both())
else:
    asyncio.run(both())
uncovered = (sum(1 for n in CASES if not results.get(n))
             + sum(1 for n in B_CASES if not b_results.get(n)))
table("NEWS-ANSWER-OCT16: T1-T16", "T", CASES, results)
table("NEWS-ANSWER-OCT16-B (the ticket's T1-T20): B1-B20", "B", B_CASES, b_results)
print(f"\n{passed} check(s) passed" + (f", {failures} failed" if failures else "")
      + (f", {uncovered} test case(s) not covered" if uncovered else ""))
bad = failures + uncovered
print("ALL PASSED" if not bad else f"{bad} FAILED")
sys.exit(1 if bad else 0)
