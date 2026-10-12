"""R3 "AI EVENTS & SUMMITS" (rebuilt 14 Oct 2026). OFFLINE.

    python verify_events.py            # every check
    python verify_events.py --show     # also print the rendered messages

Rows through the real parser, the real rule, the real renderer and the real suggestion gates; a real bot behind a
fake Discord with a throwaway database for the search step, the send, the proposals and the replies. The discovery
search is a stand-in that answers from a fixed text. No network, no model, no real sheet. The checks are in
tests/events_checks.py (shared with tests/test_events.py).

WHAT IT PROVES
  WHO IS PICKED     registered and starting within 14 days (A); not registered with a deadline still ahead (B);
                    no usable deadline, a passed deadline, an ended event and an unreadable date are skipped and
                    logged; a started event is kept.
  DATES             the six live date strings parse; a yearless date says the year it assumed; no "date unclear".
  THE LIVE TAB      seven rows shaped like the tab today: A and B empty, three suggestions.
  THE MESSAGE       three lines in the whole post, the sheet first by urgency, blocks always A, B, C, one
                    numbering, empty blocks without their heading, the two closing lines, "tomorrow".
  THE SUGGESTIONS   Bangalore, Karnataka and London only (a filter in code); declined is final; the ceilings.
  THE BOT           the search step fills only the room left; nothing is written without a yes; "add 2".
  SETTINGS          the two changed defaults, the new variable, the boot checks, the rules file.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "tests"))
os.environ.setdefault("DISCORD_TOKEN", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import events_checks as evc  # noqa: E402

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
    import drip

    def block(title, text):
        print("\n" + "=" * 100 + f"\n{title}\n" + "-" * 100)
        print(f"**{drip.HEADINGS['R3']}**\n{text}" if text else "(nothing is posted)")

    ev, on = evc.ev, evc.on
    with evc.pinned():
        block("The tab as it is today (seven rows, see tests/events_checks.py): suggestions only. The tags line "
              "goes under the heading at send; every suggested event is made up",
              evc.one(evc.tab_rows(), found=evc.suggestions()))
        block("All three blocks", evc.one(
            [ev("Beta Forum", on(8), "", "Yes"), ev("Epsilon Summit", on(10), on(6), location="London")],
            found=evc.suggestions()))
        block("The sheet fills all three lines", evc.one(
            [ev("Delta Summit", on(20), on(2), location="London"), ev("Epsilon Summit", on(10), on(6), location="London"),
             ev("Alpha Forum", on(3), "", "Yes"), ev("Beta Forum", on(8), "", "Yes")], found=evc.suggestions()))
        block("An event under way (run on 16 Sep)", evc.one(
            [ev("Under Way", "15-17 September 2026", "", "Yes")], evc.date(2026, 9, 16), found=[]))
        # A yearless date resolves to its next occurrence from the REAL today, so the run is staged a week before it.
        nxt = evc.gtm_sheet.parse_event_date("23 September")["start"]
        block(f"A date with no year on the sheet (run on {nxt - evc.timedelta(days=7):%d %b %Y})", evc.one(
            [ev("Yearless Forum", "23 September", "", "Yes", location="Pune")], nxt - evc.timedelta(days=7), found=[]))
        block("Nothing anywhere", evc.one([ev("Far Forum", on(40), "", "Yes")], found=[]))
        block("Nothing from the sheet and the search could not run",
              evc.one([ev("Far Forum", on(40), "", "Yes")], found=[], searched=False))


if __name__ == "__main__":
    evc.run(check)
    if "--show" in sys.argv:
        show()
    print(f"\n{TOTAL} checks. " + ("ALL PASSED" if not FAILED else f"{FAILED} FAILED"))
    sys.exit(1 if FAILED else 0)
