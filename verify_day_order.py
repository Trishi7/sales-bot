"""THE ORDER OF THE DAY'S POSTS (NFT2-1069). Offline, fixed dates, made-up names.

    python verify_day_order.py            every check, then one planned day for each weekday
    python verify_day_order.py --days     only the planned days (the table in the test report)

The team's decisions of 8 Oct 2026: posts are two hours apart from 14:00, in an order written per weekday in
bot_rules.yaml (`daily_order`); a rule with nothing to post takes no slot; the gap never shrinks and nothing in
the order posts after 20:00; AI news (R1) takes its place in the order instead of a fixed 14:00; meeting prep
and meeting follow-ups (10:00) and next-step follow-ups (15:00) sit outside the order; R7 is back on Mondays but
never for a person Rule 13 covers; R11 runs on Wednesdays only.

  EVERY WEEKDAY, EVERY RULE DUE   the exact times; R13 at 15:00, R8 and R9 at 10:00, moving no spaced post
  NOTHING TO POST                 Monday with no deliverables, with nobody for R7, with only AI news; a P1 that
                                  appears after the day was planned takes the next free slot
  THE GAP                         never under 120 minutes; jitter only adds; nothing after 20:00; a late post
  RULE 7                          the DM'd person Rule 13 covers is not listed; one it does not cover is
  RULE 11                         a company that appears on a Thursday is asked about the next Wednesday only
  AI NEWS                         Tuesday's 20:00 post covers from Monday's 18:00 slot, Monday's from Friday's
  THE STARTUP CHECK               weekdays and daily_order disagreeing is refused, naming both
  THE WEEKEND                     Sunday: one post, R4, only for a P1 due Monday. Saturday: nothing
  THE SETTINGS, THE WORDING       the defaults in code and .env.example; NEWS_MAIN_TIME retired; the sheet words

The checks are in tests/day_order_checks.py (shared with tests/test_day_order.py): the REAL rule evaluators, the
REAL planner and the REAL rules file on a made-up sheet. Nothing is sent, nothing is read from Google, there is
no network and no model; tests/offline_guard.py fails the run if a real Sheets or Drive call is attempted. The
schedule settings are pinned to .env.example, never read from this machine's .env.

THE WHOLE-BOT HALF — the live sweep ticked through a day, a test day and a simulation giving the same times — is
verify_s1.py check (o), which has the bot behind fake Discord.
"""
import logging
import os
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

import day_order_checks as doc  # noqa: E402

failures = 0
passed = 0


def check(name, got, want=True):
    global failures, passed
    ok = got == want
    failures += 0 if ok else 1
    passed += 1 if ok else 0
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f": got {got!r}, want {want!r}"))


def say(title: str) -> None:
    print(f"\n{title}")


def the_days() -> None:
    """One planned day for each weekday, with every rule due. Printed for the test report."""
    print("\nONE PLANNED DAY FOR EACH WEEKDAY (every rule due; made-up sheet; the real rules and planner)")
    with doc.pinned():
        for day in doc.WEEK:
            print(f"\n  {day:%A %d %b %Y}")
            for line in doc.day_table(day):
                print("    " + line)
        print(f"\n  {doc.SUN:%A %d %b %Y}  (unchanged: one post, only when a P1 deliverable is due on the Monday)")
        print("    14:00  R4   Deliverables checklist             the Sunday exception (SUNDAY_RULE_IDS=R4)")
        print(f"\n  {doc.SAT:%A %d %b %Y}")
        print("    (nothing)")


def main() -> int:
    logging.basicConfig(level=logging.ERROR, format="%(levelname)-7s %(message)s")
    if "--days" not in sys.argv:
        doc.run(check, say)
    the_days()
    print()
    if "--days" in sys.argv:
        return 0
    print(f"{passed} check(s) passed" + (f", {failures} failed" if failures else ""))
    print("ALL PASSED" if not failures else f"{failures} FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
