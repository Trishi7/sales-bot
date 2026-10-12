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
  MESSAGE     the eleven agreed group lines word for word, one "- Name (Company)" a person, the drop line
              last, no post when nobody is due or the columns are missing.
  ORDER       seven tiers with someone in each; oldest connection first; R5's company score on a tie; no-date
              rows last; five a day picked by the rule; today's people stay today's people.
  THE DROP    day 20 named, day 21 in the drop line, day 22 absent; a step change resets the clock; a dropped
              person returns when the sheet moves; the call ladder gives the unresponsive line, a call with no
              date gives the drop line; the drop line does not use up the five places.
  THE BOT     one post under its heading; the state recorded after a real send only; "done" under the post
              gets nothing at all; a test day and a preview leave every drop clock untouched.
  NO COLUMN NAME in any rendered message.
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
        with nsx.Ledger() as led:
            harry = [v for v in nsx.example_values() if "Harry Swanson" in v]
            led.day(harry, nsx.DAY - nsx.timedelta(days=21))
            text, _res = led.day(nsx.example_values(), nsx.DAY, send=False)
        block("The post (the tags line goes under the heading at send)", text)
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
