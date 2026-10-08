"""EVERY NEWS MESSAGE HAS ONE SHAPE, AND AN ANSWER NEVER TALKS ABOUT THE SCHEDULE.

    python verify_news_format.py

The team, 8 Oct: "just make it headlines in bold with a link, each of the 5
news should be bulleted and have spacing between each of the points ... make
sure this template is followed throughout for all the news messages - when
asked in the channel, daily news messages, breaking news, even test
simulations". And of an answer: "it should never mention that it is scheduled
for today or AI news since 2pm ... if anything has already been sent out in
the chat before, make sure to not send it out again (unless everything has
already been sent)". Plan: docs/plans/NEWS-OCT8.md.

OFFLINE: a throwaway database of made-up stories on a pinned day, a whole bot
behind fake Discord, no feed fetched, no search, no model (the engine's model
is a script; the scorer counts any call it gets). Every source probe is
stubbed and a real Sheets or Drive call fails the run (tests/offline_guard.py).

  the template   news.render in all four modes, exact text for five stories:
                 a Google News title ending " - Reuters", a direct-feed item,
                 a headline ending in a full stop, a pipe tail, a PoC story
  the cap        six stories give five; the follow-up's bar is 5
  one story once the same link, the same headline key, the same headline
  one message    a full daily post is one message; past 2,000 characters the
                 split is between stories and the heading is repeated
  answers        9 unsent give the 5 highest-scored, newest first; then the 4
                 left; then, with nothing new, the top 5 again; recorded as
                 sent; zero model calls; no "2 PM", "already", "scheduled",
                 "since" or "ran"; a topic question goes to the engine with
                 the same list; a quiet day
  the daily post skips what an answer gave; top PoC story first; the follow-up
                 carries only 5s, at most 5
  parity         every scenario live and in SALES_TEST_MODE
"""
import asyncio
import os
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

import news_checks  # noqa: E402

logging.getLogger().setLevel(logging.ERROR)
failures = 0


def check(name, got, want=True):
    global failures
    ok = (got == want)
    failures += 0 if ok else 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}"
          + ("" if ok else f": got {got!r}, want {want!r}"))


asyncio.run(news_checks.run(check))
print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
sys.exit(1 if failures else 0)
