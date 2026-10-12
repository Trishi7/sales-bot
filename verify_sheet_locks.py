"""THE WRITE LOCK ON OUTREACH PoCs, BY ROLE (12 Oct 2026). OFFLINE.

    python verify_sheet_locks.py            # every check
    python verify_sheet_locks.py --show     # also print the startup lock log for each header order

Header rows through the real parser, the real lock and the real write paths over a worksheet that records what it
is asked to write. No network, no real sheet. The checks are in tests/sheet_locks_checks.py (shared with
tests/test_sheet_locks.py).

WHAT IT PROVES
  RESOLVING       the identity, step and commercial blocks (Priority included) are locked in the 7 Oct order, in
                  the order the 12 Oct move implies (a FIXTURE of an inference, nothing more) and in a shuffled
                  order; the writable set is always the same seven roles.
  FAILING CLOSED  a restricted role whose header is missing: every write to the tab is refused and it is logged
                  at ERROR naming the role.
  RANGES          a letter range adds to the lock and can never unlock a role; NEW_ROW_WRITABLE_ROLES and
                  NEW_ROW_WRITABLE_RANGES never open a step-block or commercial-block column.
  EXCEPTIONS      the Email cell is still written through email_write when blank and EMAIL_WRITE_ALLOWED is
                  true, and left alone when it holds a value; a row_add fills only Name, Company and the
                  LinkedIn URL, signed by a note on the Name cell.
  THE LOG         every restricted role with the letter it sits at today; the writable window as letters and roles.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "tests"))
os.environ.setdefault("DISCORD_TOKEN", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import sheet_locks_checks as slc  # noqa: E402

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
    import gtm_sheet

    with slc.pinned():
        for name, headers in (("OLD order (7 Oct 2026)", slc.OLD),
                              ("NEW order (a fixture of the inferred 12 Oct layout)", slc.NEW),
                              ("SHUFFLED order", slc.SHUFFLED)):
            print("\n" + "=" * 100 + f"\n{name}\n" + "-" * 100)
            tab = slc.tab_for(headers)
            with slc.tapped() as tap:
                gtm_sheet.SHEETS.log_writable_window(tab)
            for _lvl, line in tap.lines:
                print(line)


if __name__ == "__main__":
    slc.run(check)
    if "--show" in sys.argv:
        show()
    print(f"\n{TOTAL} checks. " + ("ALL PASSED" if not FAILED else f"{FAILED} FAILED"))
    sys.exit(1 if FAILED else 0)
