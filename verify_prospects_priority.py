"""R5 "PoCs TO CONTACT" (11 Oct 2026): one company at a time, six stacking rules, the same five. OFFLINE.

    python verify_prospects_priority.py            # every check
    python verify_prospects_priority.py --show     # also print the messages as they would be posted

Fake Discord, temp database, made-up sheet rows through the real parser; no network, no model, no real sheet.
The checks themselves are in tests/prospects_checks.py (shared with tests/test_prospects_priority.py).

WHAT IT PROVES
  SCORING   the six rules stack and an earlier one outweighs every later one together; loose industry matching;
            fewer-left then sheet order on a tie; a company matching nothing is queued last and counted.
  PEOPLE    seniority, then the three person signals, then sheet order; the new role order, matched as whole
            words; a long or multi-line Designation prints no bracket.
  MESSAGES  exactly one company open; Thursday repeats Tuesday; a contacted person drops out of the five, the
            rest and the counts; R13's person is deduped and five are still posted; "yes" lists the rest from 6;
            "move on" (bare, and to a named company) switches, pauses and later returns; no skip line anywhere.
  MONTHLY   the two lines only on the month's last Tuesday-or-Thursday, only when no First Contact Date was
            added that month, only at the foot; two and three stalled companies share one number.
  NEVER DROPPED  nothing but "everyone contacted" or "move on" takes a company out of the open slot.
  ELSEWHERE "who's left at X?" gives the order the post gives.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "tests"))
os.environ.setdefault("DISCORD_TOKEN", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import prospects_checks as pc  # noqa: E402

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
    import prospects

    with pc.pinned():
        print("\n" + "=" * 100 + "\nTuesday / Thursday\n" + "-" * 100)
        print(pc.post_of(pc.engine(pc.message_values(), pc.TUE), pc.TUE))
        print("\n" + "=" * 100 + "\nOn a yes\n" + "-" * 100)
        q = prospects.build_queue(pc.rows_of(pc.message_values()), today=pc.TUE)
        print(prospects.rest_text(q[0]["company"], prospects.pairs(q[0]["left"][5:]), start=6))
        print("\n" + "=" * 100 + "\nThe month's last post, nothing moved\n" + "-" * 100)
        print(pc.post_of(pc.engine(pc.message_values(), pc.THU3), pc.THU3))
        print("\n" + "=" * 100 + "\nThe queue (made-up fixture to the ticket's counts)\n" + "-" * 100)
        q = prospects.build_queue(pc.rows_of(pc.queue_values()), today=pc.TUE)
        for e in q:
            print(f"  {e['score']:>2}  {e['company']:<18} {len(e['left']):>2} left  {'+'.join(e['flags']) or '(no rule)'}")


if __name__ == "__main__":
    pc.run(check)
    if "--show" in sys.argv:
        show()
    print(f"\n{TOTAL} checks. " + ("ALL PASSED" if not FAILED else f"{FAILED} FAILED"))
    sys.exit(1 if FAILED else 0)
