"""R7 "NO MEETING BOOKED YET", THE ESCALATION BEHIND R13 (rebuilt 13 Oct 2026). OFFLINE.

    python verify_dm_no_meeting.py            # every check
    python verify_dm_no_meeting.py --show     # also print the rendered messages

Made-up rows through the real parser, the real rule, the real renderer, the real rotation table in a throwaway
database, and a real bot behind a fake Discord for the send path. No network, no model, no real sheet. The checks
are in tests/dm_no_meeting_checks.py (shared with tests/test_dm_no_meeting.py).

WHAT IT PROVES
  WHO IS PICKED   LI DM Sent says Sent; an LI DM Date (no date, nothing measured); Meeting Status blank or TBD;
                  then 21 days from the DM with no Meeting Date, or a Meeting Date 7 or more days after the DM.
  WHO IS OUT      Replied, blank, Completed, Won, Lost, the usual stops, and On Hold FOR THIS RULE ONLY.
  THE ROTATION    seven people, three a post, three Mondays; never named goes first; nobody is dropped.
  BEHIND R13      a person R13 names the same day is skipped and the next one moves up.
  THE MESSAGE     the ticket's two examples word for word; one numbering; an empty block and its sentence both
                  absent; the sentence follows the setting; no column, note, day count or question anywhere.
  THE BOT         one post, no model call; the send records the three; a preview and a test day do not; replies
                  under the post are not read.
  SETTINGS        the three changed defaults, the four new variables, the two boot checks, the rules file.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "tests"))
os.environ.setdefault("DISCORD_TOKEN", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import dm_no_meeting_checks as dnm  # noqa: E402

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
        print(f"**{drip.HEADINGS['R7']}**\n{text}" if text else "(nothing is posted)")

    with dnm.pinned():
        block("The post, both blocks (the tags line goes under the heading at send)",
              dnm.one(dnm.example_values())[1])
        block("Only the second block", dnm.one(dnm.example_values()[2:])[1])
        with dnm.Ledger() as led:
            for i, day in enumerate((dnm.MON, dnm.NEXT_MON, dnm.THIRD_MON), 1):
                block(f"The rotation, Monday {i} of 3 (seven people qualify, three a post)",
                      led.day(dnm.seven(), day)[1])
        with dnm.pinned(DM_NO_MEETING_DAYS=10):
            block("DM_NO_MEETING_DAYS=10", dnm.one([dnm.dm(1, "Ann Day", "Co", days=10)])[1])
        block("Nobody qualifies", dnm.one([dnm.dm(1, "Too Soon", "Co", days=5)])[1])


if __name__ == "__main__":
    dnm.run(check)
    if "--show" in sys.argv:
        show()
    print(f"\n{TOTAL} checks. " + ("ALL PASSED" if not FAILED else f"{FAILED} FAILED"))
    sys.exit(1 if FAILED else 0)
