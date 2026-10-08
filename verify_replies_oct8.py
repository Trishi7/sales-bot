"""REPLIES-OCT8: replies to "give me a moment", the "today" answer, the grouped Next steps post, the test clock.

    python verify_replies_oct8.py            every check
    python verify_replies_oct8.py --show     also print the 11:21 exchange, a "today" answer and a Next steps post

Offline, made-up names. From docs/plans/REPLIES-OCT8.md:

  1. "take ur time", "sure take ur time", "no rush", "ok no rush thanks" replied to an interim line (and to the
     answer, and to a quiet line): one reaction, no text, no model call, no second answer. "take ur time, also any
     news on ElevenLabs?" is answered as its own question. "no rush" under an open offer does not decline it.
     THE 8 OCT 11:21 EXCHANGE replayed live and in test mode: exactly one news answer.
  2. "what is the team working on today?" on a fixture day with a meeting note, channel messages, a deliverable due
     in 2 working days and an event registration closing tomorrow: all four appear, the day's posts are one or two
     lines, and there is no AI news, time of day, "scheduled" or rule number. A day with nothing gives one line.
  3. A Next steps post with 4 people on "Send email 1" and 1 on "Send email 2": two groups, each ask once; an
     "advance" group with each person's date on their line; a "done" reply still gets the right line.
  4. The test clock: a pretend day set "yesterday" is gone on today's first read; one set today stays.

The checks are in tests/oct8_checks.py (shared with tests/test_replies_oct8.py): the whole bot behind fake Discord
(tests/replies_world.py), a throwaway database, a stand-in sheet parsed by the real parser, a model that records
what it was asked. EVERY SOURCE PROBE IS A STAND-IN — the meeting notes, the channel history and the news store are
fixtures. Nothing reaches Discord, the real sheet, the web or a model; tests/offline_guard.py fails the run if a
real Sheets or Drive call is attempted.
"""
import asyncio
import logging
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "tests"))
os.chdir(HERE)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.environ.setdefault("DISCORD_TOKEN", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import offline_guard  # noqa: E402

GUARD = offline_guard.install_script()

import oct8_checks as oc  # noqa: E402

failures = 0
passed = 0


def check(name, got, want=True):
    global failures, passed
    ok = got == want
    failures += 0 if ok else 1
    passed += 1 if ok else 0
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f": got {got!r}, want {want!r}"))


def say(title: str) -> None:
    print(f"\n{title}")


def block(text: str) -> None:
    for line in text.split("\n"):
        print("    | " + line)


async def show() -> None:
    print("\nTHE 8 OCT 11:21 EXCHANGE — AFTER (live)")
    ex = await oc.exchange(False)
    for text in ex["said"]:
        block(text)
        print()
    for label, reactions, replies_, _model in ex["steps"]:
        print(f"    {label}: {reactions} reaction(s), {replies_} message(s)")
    print("\nA 'TODAY' ANSWER — AFTER (live)")
    block((await oc.today_answer(False))["text"])
    print("\nA DAY WITH NOTHING")
    block((await oc.today_answer(False, empty=True))["text"])
    print("\nA NEXT STEPS POST — AFTER (live)")
    block((await oc.next_steps_post(False))["body"])


def main() -> int:
    logging.basicConfig(level=logging.ERROR, format="%(levelname)-7s %(message)s")
    asyncio.run(oc.run(check, say))
    if "--show" in sys.argv:
        asyncio.run(show())
    print(f"\n{passed} check(s) passed" + (f", {failures} failed" if failures else ""))
    print("ALL PASSED" if not failures else f"{failures} FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
