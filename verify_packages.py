"""R12 "SALES PACKAGES" (redesigned 12 Oct 2026). OFFLINE.

    python verify_packages.py            # every check
    python verify_packages.py --show     # also print the rendered messages

Rows through the real parser, the real rule and the real renderer; a real bot behind a fake Discord with a
throwaway database for the send, the reply that gives a date, the reminders table and the one reminder sender. No
network, no model, no real sheet. The checks are in tests/packages_checks.py (shared with tests/test_packages.py).

WHAT IT PROVES
  WHO IS PICKED   a blank Ready? qualifies, Yes is excluded (whatever its per cent); a blank name is skipped; a
                  duplicate name is one package; a missing Name or Ready? column, or an unreadable tab, posts
                  nothing.
  THE ORDER       highest % Completion first, a blank per cent last, ties in sheet order; PACKAGES_ORDER flips it;
                  nothing drops on a clock.
  THE MESSAGE     the exact opener, numbered names and close for five packages, for ONE and for two; seven give
                  five lines and "(+2 more next Thursday.)"; no per cent or status anywhere; the name is a link
                  only when the product doc is a real url.
  THE DATE        recorded from a reply (a shortened name, the packages on that post only), replaced by a newer
                  one, several at once; an unreadable or past date gets the ask-again line.
  THE REMINDER    fires once when Ready? is still not Yes; closes silently when it is Yes; is skipped on a
                  Thursday; is closed with one line when the package has left the tab.
  SETTINGS        the two new variables, the fallback, the boot line, the startup check.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "tests"))
os.environ.setdefault("DISCORD_TOKEN", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import packages_checks as pkc  # noqa: E402

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
    import wording

    def block(title, text):
        print("\n" + "=" * 100 + f"\n{title}\n" + "-" * 100)
        print(f"**{drip.HEADINGS['R12']}**\n{text}" if text else "(nothing is posted)")

    pkg = pkc.pkg
    with pkc.pinned():
        block("Five packages (the tags line goes under the heading at send; every name but Evals_Vibe-check is "
              "made up)", pkc.one(pkc.five()))
        block("One package", pkc.one([pkg(1, "Only One", "", "40%"), pkg(2, "Done", "Yes", "100%")]))
        block("Seven packages", pkc.one([pkg(i, f"Package {chr(64 + i)}", "", f"{70 - i * 10}%")
                                         for i in range(1, 8)]))
        block("A product doc that is a real url, one with no scheme, and one that is words", pkc.one(
            [pkg(1, "Linked Pack", "", "40%", doc="https://docs.example/linked-pack"),
             pkg(2, "Plain Pack", "", "30%", doc="docs.example/no-scheme"),
             pkg(3, "Words Pack", "", "20%", doc="see the drive")]))
        block("Everything is ready", pkc.one([pkg(1, "Done A", "Yes", "100%")]))
    print("\n" + "=" * 100 + "\nThe fixed lines outside the weekly post\n" + "-" * 100)
    print("on the promised day:   " + wording.packages_reminder("DPO Pack"))
    print("no date could be read: " + wording.PACKAGES_NO_DATE)
    print("a date, no package:    " + wording.PACKAGES_WHICH)
    print("package left the tab:  " + wording.packages_gone("DPO Pack"))


if __name__ == "__main__":
    pkc.run(check)
    if "--show" in sys.argv:
        show()
    print(f"\n{TOTAL} checks. " + ("ALL PASSED" if not FAILED else f"{FAILED} FAILED"))
    sys.exit(1 if FAILED else 0)
