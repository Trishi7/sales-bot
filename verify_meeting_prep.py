"""R8 "MEETING PREPARATION" (rebuilt 12 Oct 2026). OFFLINE.

    python verify_meeting_prep.py            # every check
    python verify_meeting_prep.py --show     # also print the rendered messages

Rows through the real parser, the real rule and the real renderer; a real bot behind a fake Discord with a
throwaway database for the plan, the news lookup, the send, the touch record, the reply that asks for a reminder
and the one reminder sender. No network, no model, no real sheet. The checks are in tests/meeting_prep_checks.py
(shared with tests/test_meeting_prep.py).

WHAT IT PROVES
  WHAT IT READS   a row with only a Meeting Date qualifies (no activation, no Meeting Status); a Completed meeting
                  with a date to come is still chased; past dates and 7/6/4/2/1 days out are silent; 5, 3 and 0
                  days out each give their touch; a blank Name is skipped; a duplicate is one line.
  THE MESSAGE     the exact opener, lines and close for each of the four shapes; a list of ONE; singular and
                  plural; 30 meetings with no name dropped and the post split.
  THE MEMORY      a touch goes out once; a rescheduled meeting gets fresh touches and repeats none; a date moved
                  sharply closer gets exactly one catch-up; an unreadable date is named once a week; a date typed
                  in late is posted late, once; a Saturday meeting is picked.
  THE NEWS LINE   present, absent, trimmed, skipped for a scheme-less url, after BOTH names for two people at one
                  company, once a post for a company in both blocks; a failed lookup never holds the post.
  THE DAY         "Meeting today" alone at 10:00 and "Meeting preparation" after 14:00 through the real planner
                  and sender, outside the cap, on a weekend too; the bot down at 10:00.
  THE REMINDER    set from a reply (by number, by name), replaced, refused after the meeting, re-anchored when
                  the meeting moves.
  SETTINGS        MEETING_PREP_TIME, its fallback, the boot line.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "tests"))
os.environ.setdefault("DISCORD_TOKEN", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import meeting_prep_checks as mpc  # noqa: E402

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

    def block(title, values, today=mpc.MON, state=None, news=None):
        print("\n" + "=" * 100 + f"\n{title}\n" + "-" * 100)
        msgs = mpc.messages_of(mpc.engine(values, today, state), today)
        if not msgs:
            print("(nothing is posted)")
        for which in ("today", "prep"):
            m = msgs.get(which)
            if not m:
                continue
            for a in m["actions"]:
                if news and a["company"] in news:
                    a["news"] = news[a["company"]]
            print(f"{drip.heading_for(m, day=today)}\n{drip.compose_fallback(m)}\n")

    meet, day = mpc.meet, mpc.day
    acme = {"Acme AI": {"headline": "Acme AI raises a seed round",
                        "url": "https://news.example/acme-ai-raises-a-seed-round"},
            "Middlesex University London": {"headline": "Middlesex opens a machine consciousness lab",
                                            "url": "https://news.example/middlesex-lab"}}
    with mpc.pinned():
        block("Meeting today (10:00). The tags line goes under the heading at send; the headlines are made up.",
              [meet(1, *mpc.ADA, mpc.MON), meet(2, *mpc.SUJITH, mpc.MON)],
              news={"ARTPARK": {"headline": "ARTPARK backs a robotics cohort",
                                "url": "https://news.example/artpark-cohort"}})
        block("Three days away (after 14:00)", [meet(1, *mpc.DAVID, day(3))], news=acme)
        block("Five days away (after 14:00)", [meet(1, *mpc.SUJITH, day(5))])
        block("Both on the same day, and a meeting today: two posts",
              [meet(1, *mpc.ADA, mpc.MON), meet(2, *mpc.SUJITH, day(5)), meet(3, *mpc.DAVID, day(3))], news=acme)
        block("Two people at one company, and a date nobody can read",
              [meet(1, *mpc.ADA, day(3)), meet(2, *mpc.DAVID, day(3)), meet(3, "Sam Lee", "Acme AI", day(3)),
               meet(4, "Bo Chen", "Borealis", "sometime next week")], news=acme)
        block("Nothing on a touch today (4 days out, and a past date)",
              [meet(1, *mpc.ADA, day(4)), meet(2, *mpc.SUJITH, day(-2))])
    who = wording.next_step_who(*mpc.DAVID)
    print("\n" + "=" * 100 + "\nThe fixed lines outside the two posts\n" + "-" * 100)
    print("a reminder is set:        " + wording.meeting_remind_set(who, "Wed 14 Oct at 2 PM"))
    print("no meeting named:         " + wording.MEETING_REMIND_WHICH)
    print("no day or time:           " + wording.MEETING_REMIND_WHEN)
    print("after the meeting:        " + wording.meeting_remind_after(who, "Thu 15 Oct"))
    print("the meeting moved:        " + wording.meeting_remind_moved(who, "Sat 17 Oct", "Fri 16 Oct at 2 PM"))
    print("no date on that line:     " + wording.meeting_remind_no_date(who))


if __name__ == "__main__":
    mpc.run(check)
    if "--show" in sys.argv:
        show()
    print(f"\n{TOTAL} checks. " + ("ALL PASSED" if not FAILED else f"{FAILED} FAILED"))
    sys.exit(1 if FAILED else 0)
