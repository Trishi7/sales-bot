"""R6 "EMAIL CHECK: CONNECTED, NO DM YET" (rebuilt 12 Oct 2026; RULES-OCT16 on 16 Oct). OFFLINE.

RULES-OCT16 (T1-T8 in the check names): the post is the email work only. A person whose sheet email passes
the checks is not named (block 1 is behind LI_NO_DM_SHOW_WITH_EMAIL, off); no LI Connected Date is not
eligible; the order is a rotation, least recently named first; the all-clear is "Everyone connected has a
working email on file."; an address on the sheet that cannot receive mail is shown with the reason. Some of
the paragraphs below describe the 12 Oct shape and are kept for the checks that still hold.

    python verify_li_no_dm.py            # every check
    python verify_li_no_dm.py --show     # also print the rendered messages

Fake Discord, temp database, made-up rows through the real parser. The email search and the DNS lookup are
stand-ins that answer from a table; nothing else is. No network, no model, no real sheet. The checks themselves
are in tests/li_no_dm_checks.py (shared with tests/test_li_no_dm.py).

WHAT IT PROVES
  WHO       LI DM Sent blank; three days connected, or no connected date at all; sent, replied and received
            are out; the WHOLE TAB is read, so a row with no activating date is picked; rule 13 keeps anybody
            it named; no rotation and no state.
  MESSAGE   four blocks in order, numbered straight through, each person once; an empty block is omitted; the
            closing question only with a found address; two found addresses on one line.
  CAPS      five in the plain list and five across the three email blocks, counted separately.
  CHECKS    a dead domain is named by its DOMAIN only; "Multiple", a university, a personal address and a
            mismatch say nothing; dnspython missing or the check switched off skips it and reports nothing dead.
  REPEATS   the same people on the next post; a logged DM takes a person out; nobody qualifies -> the all-clear
            post; an empty tab -> no post, logged at ERROR.
  THE ADD   one email_write proposal with every address shown; only an approver's yes writes; two addresses go
            in one cell on two lines.
  NEVER     an address already on the sheet, a column name, or one of the old DM openers, in any message.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "tests"))
os.environ.setdefault("DISCORD_TOKEN", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import li_no_dm_checks as lnd  # noqa: E402

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
    def block(title, text):
        print("\n" + "=" * 100 + f"\n{title}\n" + "-" * 100)
        print(text)

    with lnd.pinned():
        block("The post (the tags line goes under the heading at send)", lnd.one_post(lnd.example_values())["text"])
        block("Everybody's email is fine", lnd.one_post([
            lnd.poc(1, "Daisy Leigh", "PolyAI", days=34, email=lnd.EXISTING["Daisy Leigh"]),
            lnd.poc(2, "Ioana Mocanu", "Agoda", days=10, email=lnd.EXISTING["Ioana Mocanu"])])["text"])
        block("Nobody qualifies", lnd.one_post([lnd.poc(1, "Sent Sue", "Co", days=30, dm_sent="Sent")])["text"])


if __name__ == "__main__":
    lnd.run(check)
    if "--show" in sys.argv:
        show()
    print(f"\n{TOTAL} checks. " + ("ALL PASSED" if not FAILED else f"{FAILED} FAILED"))
    sys.exit(1 if FAILED else 0)
