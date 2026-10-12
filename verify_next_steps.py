"""R13 "NEXT STEPS" AS A REMINDER (rebuilt 12 Oct 2026). OFFLINE.

    python verify_next_steps.py            # every check
    python verify_next_steps.py --show     # also print the rendered messages

Fake Discord, temp database, made-up rows through the real parser; no network, no model, no real sheet. The
checks themselves are in tests/next_steps_checks.py (shared with tests/test_next_steps.py).

WHAT IT PROVES
  WHO IS IN   every Connected row, with or without an LI Connected Date (the four no-date rows appear); the
              stop markers, a Completed meeting and a booked meeting are out; R7 leaves all of them alone.
  THE ASKS    the table row by row: research, send the Nth email, send the next one a week later, call,
              replied, unresponsive; a DM marked sent with no date is a call due today; an email marked sent
              with no date moves on; the two silent cases; free text is skipped and logged.
  MESSAGE     the ten agreed group lines word for word, one "- Name (Company)" a person, no post when nobody
              is due or the columns are missing.
  ORDER       six tiers with someone in each; inside a tier never-named people first, then the least recently
              named, then the oldest connection, R5's company score, sheet order; no-date rows last.
  ROTATION    nobody is dropped: twelve people in a tier are walked 5, 5, then 2 + 3; a person named 25 days ago
              is still named; the unresponsive line comes round again; "Received" is the meeting line; a preview
              writes no state.
  THE BOT     one post under its heading; the state recorded after a real send only; "done" under the post
              gets nothing at all; a test day and a preview leave the rotation untouched.
  NO COLUMN NAME and NO DROP WORDING in any rendered message.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "tests"))
os.environ.setdefault("DISCORD_TOKEN", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import next_steps_checks as nsx  # noqa: E402

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
    import wording

    def block(title, text):
        print("\n" + "=" * 100 + f"\n{title}\n" + "-" * 100)
        print("**Next steps**\n" + text)

    with nsx.pinned():
        block("The post (the tags line goes under the heading at send)",
              nsx.post_of(nsx.engine(nsx.example_values())))
        twelve = [nsx.person(f"P{i:02d}", f"Co {i:02d}", "Send email 1", li=nsx.date(2026, 9, i))
                  for i in range(1, 13)]
        with nsx.Ledger() as led:
            for offset in (0, 1, 2):
                text, _res = led.day(twelve, nsx.DAY + nsx.timedelta(days=offset))
                block(f"The rotation, run {offset + 1} of 3 (twelve people in one tier)", text)
        block("Every group there is", "\n".join(wording.next_step_post([
            {"who": "Name (Company)", "ask": ask, "n": n} for (ask, n), _line in wording.NEXT_STEP_GROUPS])))
        nodate = [nsx.person(f"Contact {i}", c, "", li=None) for i, c in enumerate(
            ("Middlesex University London", "London Metropolitan University", "University of East London",
             "Telekinesis AI"), 1)]
        block("Connected, no LI Connected Date", nsx.post_of(nsx.engine(nodate)))


if __name__ == "__main__":
    nsx.run(check)
    if "--show" in sys.argv:
        show()
    print(f"\n{TOTAL} checks. " + ("ALL PASSED" if not FAILED else f"{FAILED} FAILED"))
    sys.exit(1 if FAILED else 0)
