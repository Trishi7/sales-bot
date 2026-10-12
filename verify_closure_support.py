"""R10 "CLOSURE SUPPORT" (rebuilt 13 Oct 2026). OFFLINE.

    python verify_closure_support.py            # every check
    python verify_closure_support.py --show     # also print the rendered messages

Made-up rows through the real parser, the real rule and the real renderer; a real bot behind a fake Discord with a
throwaway database for the news lookup, the send and the marker. The news is rows in the real news store. No
network, no model, no real sheet. The checks are in tests/closure_support_checks.py (shared with
tests/test_closure_support.py).

WHAT IT PROVES
  WHO IS PICKED   a closure stage (whole words, "No deal" is not a deal) AND strictly above 50%; a stage with no
                  percentage is left out and never mentioned; 0% and the stop words are out.
  THE NOTICE      either column blank on every active row: the one-line notice naming it, every Monday; columns
                  with values and nothing qualifying: no post at all.
  THE LIST        person first; one deal is a list of one; twelve deals are twelve lines; highest percentage
                  first; the same list on consecutive Mondays (no rotation).
  THE NEWS LINE   read from the stored news, first SEEN since the last closure support post (seven days on the
                  first run), matched on the full company name, one story a deal, nothing fetched.
  THE MARKER      R10's own row in rule_last_posted, moved only by a live deals post; never R2's row.
  SETTINGS        the one new variable, the boot lines, the rules file.
"""
import os
import sys
from datetime import timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "tests"))
os.environ.setdefault("DISCORD_TOKEN", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import closure_support_checks as csc  # noqa: E402

FAILED = 0
TOTAL = 0


def check(name, got, want):
    global FAILED, TOTAL
    TOTAL += 1
    ok = got == want
    FAILED += 0 if ok else 1
    print(("  ok    " if ok else "  FAIL  ") + name)
    if not ok:
        print(f"        got   {got!r}\n        want  {want!r}")


def show():
    import asyncio

    import deadlines as dl
    import drip
    import replies_world as rw

    def block(title, text):
        print("\n" + "=" * 100 + f"\n{title}\n" + "-" * 100)
        print(f"**{drip.HEADINGS['R10']}**\n{text}" if text else "(nothing is posted)")

    async def with_news():
        now, today = dl.real_now_ist(), dl.real_today_ist()
        with rw.World(False, pins=dict(csc.pins())) as w:
            csc.seed(w, [csc.story("Acme AI raises $40M", "Acme AI", seen=now - timedelta(days=1))])
            msg = csc.message_of(csc.engine(csc.example_values(), today), today)
            await w.bot._research_message(msg, today=today)
            return drip.compose_fallback(msg)

    with csc.pinned():
        block("The deals post, one deal with a stored story (the tags line goes under the heading at send; "
              "the story and its date are made up)", asyncio.run(with_news()))
        block("One deal", csc.one([csc.deal(1, "Ada Lovelace", "Acme AI", "Quote", "70%"), csc.filler()])[1])
        block("Both columns empty on every active row",
              csc.one([csc.deal(1, "Ann Day", "Co"), csc.deal(2, "Bo Chen", "Borealis")])[1])
        block("Only Closure Prob% empty", csc.one([csc.deal(1, "Ann Day", "Co", "Demo")])[1])
        block("The columns have values and nothing qualifies",
              csc.one([csc.deal(1, "Ann Day", "Co", "Demo", "40%")])[1])


if __name__ == "__main__":
    csc.run(check)
    if "--show" in sys.argv:
        show()
    print(f"\n{TOTAL} checks. " + ("ALL PASSED" if not FAILED else f"{FAILED} FAILED"))
    sys.exit(1 if FAILED else 0)
