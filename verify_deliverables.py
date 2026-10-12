"""R4 "DELIVERABLES TO CHASE" (rebuilt 12 Oct 2026). OFFLINE.

    python verify_deliverables.py            # every check
    python verify_deliverables.py --show     # also print the rendered messages

Rows through the real parser, the real rule and the real renderer; a real bot behind a fake Discord with a
throwaway database for the plan, the send and the live sweep's cutoff. No network, no model, no real sheet. The
checks are in tests/deliverables_checks.py (shared with tests/test_deliverables.py).

WHAT IT PROVES
  THE SELECTION   Priority is exactly P1 and Status is not exactly Completed, and nothing else: a P1 due in 2028
                  and one a year overdue are both listed; "High", "1" and "Critical" are not P1 and are logged;
                  "Done" and "Closed" do not drop a row and are logged as unrecognised; "Completed " and
                  "completed" do; a blank Status is open, a blank Priority is out, a blank Action Item is skipped;
                  the shape of today's tab (17 rows in, 11 out, 4 not P1, 2 Completed).
  THE ORDER       earliest deadline first; a tie in sheet order; no deadline, or an unreadable one, last.
  THE MESSAGE     the exact heading and "{n} open:"; three lines an item; "There's no deadline for this"; no
                  overdue count, no link, no closing line; a 179-character title whole and a 181-character one
                  trimmed; the same Action Item once; 23 items as 20 blocks and the exact overflow line; a
                  40-item list split between lines with nothing dropped.
  THE DAY         Monday through the real planner and sender, counted toward the cap; a Sunday run produces NO
                  post; the bot down until 7 PM posts on restart, down until 9:30 PM does not.
  SETTINGS        the three changed defaults, DELIVERABLE_NEAR_DAYS retired, the boot line, the sheet wording.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "tests"))
os.environ.setdefault("DISCORD_TOKEN", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import deliverables_checks as dc  # noqa: E402

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

    def block(title, rows):
        print("\n" + "=" * 100 + f"\n{title}\n" + "-" * 100)
        text = dc.one(rows)
        print(f"**{drip.HEADINGS['R4']}**\n{text}" if text else "(nothing is posted)")

    item = dc.item
    with dc.pinned():
        block("The shape of today's tab: 17 rows, 11 open P1 (the tags line goes under the heading at send; "
              "every item but the first is made up)", dc.todays_shape())
        block("One item", [item(1, "Only one", deadline="20-Oct")])
        block("23 items", [item(i, f"Deliverable number {i:02d}", deadline=f"{i}-Nov") for i in range(1, 24)])
        block("Nothing open", [item(1, "Done", status="Completed"), item(2, "Not P1", "P2")])


if __name__ == "__main__":
    dc.run(check)
    if "--show" in sys.argv:
        show()
    print(f"\n{TOTAL} checks. " + ("ALL PASSED" if not FAILED else f"{FAILED} FAILED"))
    sys.exit(1 if FAILED else 0)
