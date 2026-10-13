"""R14 "RECONNECT DATE FOLLOW-UP" (NFT2-1086). OFFLINE.

    python verify_reconnect.py            # every check, then the ticket's table: # | case | PASS/FAIL

Rows through the real sheet parser, the real rule and renderer, and a real bot behind a fake Discord with a throwaway
database for the plan, the send and the "yes". No network, no model, no real sheet (the shape of the real tab is a
FIXTURE; `python -m gtm_sheet` is never run). The checks are in tests/reconnect_checks.py (shared with
tests/test_reconnect.py).

THE 35 NUMBERED CASES ARE THE TICKET'S TABLE, one check each, named with its number:
  01-06  WHICH RULE WINS   Reconnect Date, then Meeting Date + 45, then the newest contact + 45; FALSE is empty;
                           no date anywhere never fires; the meeting status is not read.
  07-16  DATE FORMATS      each lands on 13 Dec 2026; 13th dec fills the year; 03/04/2026 is 3 April; banana is listed
                           as unclear.
  17-22  TIMING            a Saturday posts on the Monday; a past date is skipped; nothing due is no post; 15:30 outside
                           the cap; R13's people are not repeated; once a day.
  23-30  THE POST          5 names + "there are 36 more"; exactly 5 has no more line; one reads in the singular; a yes
                           lists all 41 split between names; a yes elsewhere and a no do nothing; a blank company is
                           skipped; a wrong channel id is a startup error and no post anywhere.
  31-35  STOPPING          once; Lost; On Hold; a new date fires once more; a restart posts once.
Then, unnumbered: the measured shape (670 / 528 / 142 / 2), the column lock, the settings and the edge cases.
"""
import logging
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "tests"))
os.chdir(HERE)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.environ.setdefault("DISCORD_TOKEN", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import offline_guard  # noqa: E402

GUARD = offline_guard.install_script()

import reconnect_checks as rcx  # noqa: E402

FAILED = 0
TOTAL = 0
RESULTS = []                                      # (name, ok)


def check(name, got, want=True):
    global FAILED, TOTAL
    TOTAL += 1
    ok = got == want
    FAILED += 0 if ok else 1
    RESULTS.append((name, ok))
    print(("  ok    " if ok else "  FAIL  ") + name)
    if not ok:
        print(f"        got   {got!r}\n        want  {want!r}")


def table() -> None:
    print("\n" + "=" * 100 + "\nNFT2-1086  #  |  case  |  PASS/FAIL\n" + "-" * 100)
    numbered = {}
    for name, ok in RESULTS:
        m = re.match(r"^(\d\d) (.*)$", name)
        if m:
            numbered[int(m.group(1))] = (m.group(2), ok)
    for n in range(1, 36):
        case, ok = numbered.get(n, ("(no check ran)", False))
        print(f"{n:>2} | {case[:78]:<78} | {'PASS' if ok else 'FAIL'}")
    extras = [(name, ok) for name, ok in RESULTS if not re.match(r"^\d\d ", name)]
    print("-" * 100 + f"\n   {len(extras)} further check(s) beyond the 35: "
          f"{sum(1 for _n, ok in extras if ok)} PASS, {sum(1 for _n, ok in extras if not ok)} FAIL")
    for name, ok in extras:
        if not ok:
            print(f"   FAIL  {name}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.ERROR, format="%(levelname)-7s %(message)s")
    rcx.run(check)
    table()
    print(f"\n{TOTAL} checks. " + ("ALL PASSED" if not FAILED else f"{FAILED} FAILED"))
    sys.exit(1 if FAILED else 0)
