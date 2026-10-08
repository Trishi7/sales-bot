"""THE ORDER OF THE DAY'S POSTS (NFT2-1069) — the checks, shared by verify_day_order.py and tests/test_day_order.py.

Written from the team's decisions of 8 Oct 2026 (docs/plans/NFT2-1069.md, section "8 Oct decisions").

    sys.path.insert(0, <repo>/tests); import day_order_checks as doc
    doc.run(check)                 # check(name, got, want)

WHAT RUNS HERE IS THE REAL THING, ON FIXED DATES: the real rule evaluators (`nextaction.run`), the real planner
(`drip.plan`), the real rules file (bot_rules.yaml), the real news-window function (`bot.SalesBot._main_window`)
and the real SQLite snapshot (`db.DB.pipeline_snapshot`), fed a made-up sheet in which every rule that runs on
a day has something due. Nothing is sent, no sheet is read, no network, no model.

EVERY SETTING THE SCHEDULE DEPENDS ON IS PINNED to its .env.example default for the length of a check and put
back afterwards (`pinned`), never inherited from this machine's .env: the laptop's own values (a jitter of 120, a
cap of 15, a window ending 18:30) would otherwise decide what these checks see.

The whole-bot half — the live sweep, a test day and a simulation giving the same times — needs a bot behind fake
Discord and lives in verify_s1.py, check (o).
"""
import contextlib
import os
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)
os.environ.setdefault("DISCORD_TOKEN", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import config  # noqa: E402
import deadlines as dl  # noqa: E402
import drip  # noqa: E402
import nextaction  # noqa: E402
import rules as rules_mod  # noqa: E402

# One fixed week, Monday to Sunday, and the Wednesday after it.
MON, TUE, WED, THU, FRI = (date(2026, 10, 12), date(2026, 10, 13), date(2026, 10, 14),
                           date(2026, 10, 15), date(2026, 10, 16))
SAT, SUN = date(2026, 10, 17), date(2026, 10, 18)
WEEK = (MON, TUE, WED, THU, FRI)

# The .env.example defaults the schedule is written against.
PINS = {
    "SALES_DRIP_START": "14:00", "SALES_DRIP_END": "20:00",
    "MESSAGE_GAP_MINUTES": 120, "MESSAGE_GAP_MIN_MINUTES": 120, "MESSAGE_JITTER_MINUTES": 0,
    "DAILY_MESSAGE_CAP": 5, "DRIP_WEEKDAYS_ONLY": True, "SUNDAY_RULE_IDS": ["R4"], "DRIP_REASK_DAYS": 2,
    "MEETING_DAYOF_TIME": "10:00", "NEXT_STEP_TIME": "15:00", "MEETING_PREP_DAYS_BEFORE": ["5", "3"],
    "MEETING_FOLLOWUP_AFTER_DAYS": 3, "MEETING_FOLLOWUP_EVERY_DAYS": 3,
    "MEETING_FOLLOWUP_LADDER": ["channel", "dm", "dm", "escalation"], "SALES_DMS_ENABLED": False,
    "NEW_COMPANY_AFTER_WORKING_DAYS": 1, "NEW_COMPANY_WINDOW_DAYS": 7,
    "DM_NO_MEETING_DAYS": 7, "LI_NO_DM_DAYS": 3, "CLOSURE_SUPPORT_MIN": 50,
    "NEXT_ACTION_WEEKEND_SHIFT": True, "EVENTS_WINDOW_DAYS": 14,
    "NEXT_STEP_CONNECTED_MARKERS": ["connected"], "NEXT_STEP_FIRST_DAYS": 2,
    "NEWS_FEED_KEEP_DAYS": 7,
}
_ABSENT = object()

# What the team asked for, with every rule due. (rule, time) in the order they post.
SPACED = {
    MON: [("R4", "14:00"), ("R7", "16:00"), ("R1", "18:00"), ("R10", "20:00")],
    TUE: [("R5", "14:00"), ("R6", "16:00"), ("R2", "18:00"), ("R1", "20:00")],
    WED: [("R11", "14:00"), ("R1", "16:00"), ("R3", "18:00")],
    THU: [("R5", "14:00"), ("R1", "16:00"), ("R12", "18:00")],
    FRI: [("R2", "14:00"), ("R6", "16:00"), ("R1", "18:00")],
}
FIXED = [("R8", "10:00"), ("R9", "10:00"), ("R13", "15:00")]


@contextlib.contextmanager
def pinned(**extra):
    """The schedule settings at their shipped defaults (plus `extra`), restored on the way out."""
    values = dict(PINS, **extra)
    before = {k: getattr(config, k, _ABSENT) for k in values}
    for k, v in values.items():
        setattr(config, k, v)
    rules_mod.reload()
    try:
        yield
    finally:
        for k, v in before.items():
            if v is _ABSENT:
                if hasattr(config, k):
                    delattr(config, k)
            else:
                setattr(config, k, v)
        rules_mod.reload()


# -- the made-up sheet ---------------------------------------------------------------------------------------------

def cell(d: date) -> str:
    return d.strftime("%d-%m-%Y")


def poc(n, company, name, **kw):
    """One Outreach PoCs row. First contact is made on all but R5's, so R5 does not pick the contact up too."""
    row = {"_row": n, "_extra": {}, "company": company, "name": name, "first_contact": "TRUE",
           "designation": "Founder"}
    row.update(kw)
    return row


def connected(n, company, name, day, **kw):
    """A row Rule 13 covers: "Sid - LI Addition" says Connected and LI Connected Date is filled."""
    base = dict(sid_li_added="Connected", li_connected_date=cell(day - timedelta(days=10)),
                **{role: "" for role in nextaction.NEXT_STEP_REQUIRED_ROLES})
    base.update(kw)
    return poc(n, company, name, **base)


def world(day: date, *, without=()) -> dict:
    """`nextaction.run` inputs for `day` in which EVERY rule has something due. `without` drops a rule's inputs."""
    rows = [
        poc(2, "Prospect Co", "Pia Rao", first_contact="FALSE"),                               # R5
        poc(3, "Linked Co", "Lev Shah", li_connected_date=cell(day - timedelta(days=6))),      # R6 (not "Connected")
        poc(4, "Echo Labs", "Eve Rao", li_dm_date=cell(day - timedelta(days=11))),             # R7
        poc(5, "Gantry Systems", "Gil Shah", meeting_date=cell(day)),                          # R8, on the day
        poc(6, "Ibex AI", "Ian Bose", meeting_date=cell(day + timedelta(days=3))),             # R8, 3 days before
        poc(7, "Harbor Robotics", "Hal Iyer", meeting_date=cell(day - timedelta(days=5)),
            meeting_status="Completed"),                                                       # R9
        poc(8, "Fathom AI", "Fay Lin", prospect_status="Demo", closure_prob="70%"),            # R10
        connected(9, "Juniper Labs", "Jo Nair", day),                                          # R13
        connected(10, "Kite Systems", "Kai Menon", day, li_dm_date=cell(day - timedelta(days=11))),   # R13, DM'd
    ]
    drop = {
        "R5": [2], "R6": [3], "R7": [4], "R8": [5, 6], "R9": [7], "R10": [8], "R13": [9, 10],
    }
    gone = {n for rule in without for n in drop.get(rule, [])}
    rows = [r for r in rows if r["_row"] not in gone]
    return {
        "today": day, "rows": rows, "prospect_rows": rows, "next_step_state": {},
        "deliverables": [] if "R4" in without else [
            {"_row": 2, "_extra": {}, "action_item": "MSA template", "priority": "P1", "status": "",
             "deadline": f"{(day + timedelta(days=2)).day}-{(day + timedelta(days=2)).strftime('%b')}",
             "dependency": "Legal", "remarks": "", "link": ""}],
        "packages": [] if "R12" in without else [
            {"_row": 2, "_extra": {}, "name": "Hinglish STT", "ready": "", "status": "In progress"}],
        "events": [] if "R3" in without else [
            {"_row": 2, "_extra": {}, "event": "Voice AI Summit", "event_date": cell(day + timedelta(days=9)),
             "registration_deadline": "", "registered": "Yes", "link": ""}],
        "pipeline_companies": ["Old Co", "Nova Robotics"],
        "new_companies": [] if "R11" in without else [
            {"company": "Nova Robotics", "first_seen": dl.iso(day - timedelta(days=2))}],
    }


def queue(day: date, *, without=(), only=None) -> dict:
    """The real queue for `day`. `without` empties those rules' inputs; `only` runs just those rules."""
    day_rules = [r for r in rules_mod.for_day(day)
                 if r.id not in without and (only is None or r.id in only)]
    return nextaction.run(day_rules=day_rules, **world(day, without=without))


def plan(day: date, **kw) -> dict:
    already = kw.pop("already_sent", None)
    return drip.plan(queue(day, **kw)["actions"], day=day, already_sent=already)


def posts(planned: dict, *, pinned_only=None) -> list:
    """[(rule id, HH:MM)] in the order the plan sends them."""
    return [(m["rule_id"], m["send_at_hhmm"]) for m in planned["messages"]
            if pinned_only is None or bool(m["pinned"]) == pinned_only]


def gaps(planned: dict) -> list:
    times = [m["send_at"] for m in planned["messages"] if not m["pinned"]]
    return [int((b - a).total_seconds() // 60) for a, b in zip(times, times[1:])]


def sent_row(message: dict, day: date, sent_hhmm: str) -> dict:
    """The `drip_sends` row a message leaves behind, sent at `sent_hhmm` on `day`."""
    return {"slot": message["slot"], "group_key": message["group_key"], "action_type": message["type"],
            "counts_toward_cap": 1 if message["counts_toward_cap"] else 0,
            "pinned": 1 if message["pinned"] else 0, "planned_at": message["send_at_hhmm"],
            "sent_at": f"{dl.iso(day)}T{sent_hhmm}:00"}


def day_table(day: date) -> list:
    """One planned day as printable lines, for the test report."""
    planned = plan(day)
    lines = []
    for m in planned["messages"]:
        lines.append(f"{m['send_at_hhmm']}  {m['rule_id']:<4} {m['rule_name']:<34} "
                     + ("fixed time, outside the order and the cap" if m["pinned"]
                        else f"slot {1 + [x for x in planned['messages'] if not x['pinned']].index(m)} of the day's order"))
    return lines


# -- the checks -----------------------------------------------------------------------------------------------------

def every_weekday(check) -> None:
    """Each weekday with every rule due: the exact times; R13 at 15:00, R8 and R9 at 10:00, moving nothing."""
    with pinned():
        for day in WEEK:
            name = day.strftime("%A")
            planned = plan(day)
            check(f"{name}, every rule due: the spaced posts, in the team's order, two hours apart",
                  posts(planned, pinned_only=False), SPACED[day])
            check(f"{name}: meeting prep and the meeting follow-up at 10:00, next steps at 15:00",
                  sorted(posts(planned, pinned_only=True)), sorted(FIXED))
            check(f"{name}: sent in time order — 10:00, 10:00, 14:00, 15:00, then the rest",
                  [t for _r, t in posts(planned)],
                  sorted(t for _r, t in SPACED[day] + FIXED))
            check(f"{name}: every gap between spaced posts is exactly 120 minutes",
                  gaps(planned), [120] * (len(SPACED[day]) - 1))
            check(f"{name}: nothing is left over and nothing is held",
                  (len(planned["rolled"]), len(planned["held"])), (0, 0))
            without_fixed = plan(day, without=("R8", "R9", "R13"))
            check(f"{name}: the fixed-time posts move no spaced post (the same day without them)",
                  posts(without_fixed), SPACED[day])
            only_fixed = plan(day, only=("R8", "R9", "R13"))
            check(f"{name}: ...and no spaced post moves them (the same day with nothing else)",
                  sorted(posts(only_fixed)), sorted(FIXED))
            check(f"{name}: the fixed-time posts do not count toward the cap; the spaced ones do",
                  sorted({(m["pinned"], m["counts_toward_cap"]) for m in planned["messages"]}),
                  [(False, True), (True, False)])
            check(f"{name}: nothing after 20:00",
                  [p for p in posts(planned) if p[1] > "20:00"], [])
        check("the longest day has four spaced posts, under the cap of five",
              (max(len(v) for v in SPACED.values()), config.DAILY_MESSAGE_CAP), (4, 5))


def moving_up(check) -> None:
    """A rule with nothing to post takes no slot and the next one moves up."""
    with pinned():
        check("Monday with no deliverables: R7 14:00, R1 16:00, R10 18:00",
              posts(plan(MON, without=("R4",)), pinned_only=False),
              [("R7", "14:00"), ("R1", "16:00"), ("R10", "18:00")])
        check("Monday with nobody for R7: R4, R1, R10 at 14:00, 16:00, 18:00",
              posts(plan(MON, without=("R7",)), pinned_only=False),
              [("R4", "14:00"), ("R1", "16:00"), ("R10", "18:00")])
        check("Monday with no deliverables and nobody for R7 either: R1 14:00, R10 16:00",
              posts(plan(MON, without=("R4", "R7")), pinned_only=False),
              [("R1", "14:00"), ("R10", "16:00")])
        check("Monday with only AI news due: it goes at 14:00",
              posts(plan(MON, without=("R4", "R7", "R10")), pinned_only=False), [("R1", "14:00")])
        check("Tuesday with no prospects and no LinkedIn check: R2 14:00, R1 16:00",
              posts(plan(TUE, without=("R5", "R6")), pinned_only=False),
              [("R2", "14:00"), ("R1", "16:00")])
        check("Wednesday with no new company: R1 14:00, R3 16:00",
              posts(plan(WED, without=("R11",)), pinned_only=False),
              [("R1", "14:00"), ("R3", "16:00")])
        for day in WEEK:
            for missing in SPACED[day]:
                rule = missing[0]
                if rule in ("R1", "R2"):
                    continue             # always have something: a carrier item for the research
                left = [r for r, _t in SPACED[day] if r != rule]
                got = posts(plan(day, without=(rule,)), pinned_only=False)
                check(f"{day:%A} without {rule}: the others keep their order and close the gap",
                      got, list(zip(left, ["14:00", "16:00", "18:00", "20:00"])))

        # A HIGH-PRIORITY ITEM THAT APPEARS AFTER THE DAY WAS PLANNED takes the next free slot.
        morning = plan(MON, without=("R4",))
        first = morning["messages"][[m["pinned"] for m in morning["messages"]].index(False)]
        sent = [sent_row(first, MON, "14:00")]
        check("Monday planned with no deliverables: R7 went at 14:00",
              (first["rule_id"], first["send_at_hhmm"]), ("R7", "14:00"))
        later = plan(MON, already_sent=sent)
        check("...then a P1 deliverable appears: the checklist takes the next free slot, 16:00, "
              "and the rest follow two hours apart",
              posts(later, pinned_only=False), [("R4", "16:00"), ("R1", "18:00"), ("R10", "20:00")])
        check("...and what has already gone is not sent twice",
              [m["rule_id"] for m in later["messages"] if m["rule_id"] == "R7"], [])


def the_gap(check) -> None:
    """The gap never shrinks; jitter only adds; nothing after 20:00; what is left is not sent that day."""
    with pinned():
        check("the gap is 120 minutes and the window holds four slots",
              (drip.gap_minutes(), drip.min_gap_minutes(), drip.slots_in_window(MON)), (120, 120, 4))
        check("the slots are 14:00, 16:00, 18:00 and 20:00",
              [t.strftime("%H:%M") for t in drip.slot_times(MON, count=4)],
              ["14:00", "16:00", "18:00", "20:00"])

        # SIX SPACED GROUPS ON A FOUR-SLOT DAY. Two more rules than Monday has, run by naming them.
        extra = [r for r in rules_mod.safe_load() if r.id in ("R12", "R5")]
        items = nextaction.run(day_rules=rules_mod.for_day(MON) + extra, **world(MON))["actions"]
        crowded = drip.plan(items, day=MON)
        check("six spaced groups on Monday: four go, at the same four times, gaps unchanged",
              (posts(crowded, pinned_only=False), gaps(crowded)), (SPACED[MON], [120, 120, 120]))
        check("...the other two are not sent today (they would land at 22:00 and 00:00)",
              sorted((g["rule_id"], g["rolled_why"]) for g in crowded["rolled"]),
              [("R12", "cap"), ("R5", "window")])
        check("...and the plan says why in plain words",
              [g["why"] for g in crowded["rolled"] if g["rolled_why"] == "window"],
              ["it would land at 22:00, after 20:00, so it waits for the next day its rule runs"])

        # A POST THAT LEFT ON A SWEEP TICK A FEW MINUTES AFTER ITS SLOT keeps the day on its slots.
        whole = plan(MON)
        spaced = [m for m in whole["messages"] if not m["pinned"]]
        on_tick = plan(MON, already_sent=[sent_row(spaced[0], MON, "14:14")])
        check("the first post left on the 14:14 tick: the rest keep 16:00, 18:00 and 20:00",
              posts(on_tick, pinned_only=False), SPACED[MON][1:])

        # A POST THAT WENT LATE (the bot was down): the next is a full gap after it, and never past 20:00.
        late = plan(MON, already_sent=[sent_row(spaced[0], MON, "17:20")])
        check("the first post left at 17:20: the next is at 19:20, a full 120 minutes later",
              posts(late, pinned_only=False), [("R7", "19:20")])
        check("...and the two that would land after 20:00 are not sent today",
              sorted((g["rule_id"], g["rolled_why"]) for g in late["rolled"]),
              [("R1", "window"), ("R10", "window")])
        very_late = plan(MON, already_sent=[sent_row(spaced[0], MON, "19:30")])
        check("the first post left at 19:30: nothing else spaced goes today",
              (posts(very_late, pinned_only=False), len(very_late["rolled"])), ([], 3))

        # A FIXED-TIME ROW IS NOT WHAT THE GAP IS MEASURED FROM.
        fixed = [m for m in whole["messages"] if m["pinned"]]
        after_fixed = plan(MON, already_sent=[sent_row(m, MON, "13:40") for m in fixed])
        check("the 10:00 and 15:00 posts all went at 13:40 (a late start): the spaced posts keep their slots",
              posts(after_fixed, pinned_only=False), SPACED[MON])
        check("the guard measures from the last SPACED post only",
              (drip.last_spaced_sent_at([sent_row(m, MON, "13:40") for m in fixed]),
               drip.last_spaced_sent_at([sent_row(spaced[0], MON, "14:03")]).strftime("%H:%M")),
              (None, "14:03"))

        # EVERY DAY, EVERY WAY IT CAN START: no gap under 120 and nothing after 20:00.
        worst_gap, latest = 10 ** 6, "00:00"
        for day in WEEK:
            base = [m for m in plan(day)["messages"] if not m["pinned"]]
            for minutes_late in (0, 5, 14, 29, 31, 60, 121, 200, 301):
                when = (datetime(day.year, day.month, day.day, 14, 0) + timedelta(minutes=minutes_late))
                got = plan(day, already_sent=[sent_row(base[0], day, when.strftime("%H:%M"))])
                times = [when.replace(tzinfo=dl.IST)] + [m["send_at"] for m in got["messages"] if not m["pinned"]]
                steps = [int((b - a).total_seconds() // 60) for a, b in zip(times, times[1:])]
                # an on-time first post (within the slack) is measured from its slot, 14:00
                if minutes_late <= drip.ON_TIME_SLACK_MINUTES and steps:
                    steps[0] += minutes_late
                worst_gap = min([worst_gap] + steps)
                latest = max([latest] + [m["send_at_hhmm"] for m in got["messages"]])
        check("across every weekday and nine ways the first post can be late: no gap under 120 minutes",
              worst_gap >= 120, True)
        check("...and nothing is ever planned after 20:00", latest <= "20:00", True)

    with pinned(MESSAGE_JITTER_MINUTES=25):
        for day in WEEK:
            times = drip.slot_times(day, count=6)
            steps = [int((b - a).total_seconds() // 60) for a, b in zip(times, times[1:])]
            check(f"{day:%A} with a jitter of 25: every gap is 120 to 145, never less",
                  all(120 <= s <= 145 for s in steps), True)
            jittered = plan(day)
            check(f"{day:%A} with a jitter of 25: still nothing after 20:00",
                  [p for p in posts(jittered) if p[1] > "20:00"], [])
    with pinned(MESSAGE_JITTER_MINUTES=-40):
        check("a negative jitter is read as none: the slots are exact",
              [t.strftime("%H:%M") for t in drip.slot_times(MON, count=4)],
              ["14:00", "16:00", "18:00", "20:00"])
    with pinned(MESSAGE_GAP_MINUTES=45):
        check("MESSAGE_GAP_MIN_MINUTES is a floor: a gap set to 45 is still 120",
              (drip.gap_minutes(), posts(plan(MON), pinned_only=False)), (120, SPACED[MON]))


def rule_7(check) -> None:
    """R7 on Mondays: never a person Rule 13 covers; anyone else it still lists."""
    with pinned():
        got = queue(MON)
        r7 = [a for a in got["by_rule"].get("R7", [])]
        r13 = [a for a in got["by_rule"].get("R13", [])]
        check("R7 runs on a Monday, and lists the DM'd person Rule 13 does not cover",
              [(a["company"], a["poc"]) for a in r7], [("Echo Labs", "Eve Rao")])
        check("...never the DM'd person Rule 13 covers (Connected, with an LI Connected Date)",
              [a["poc"] for a in r7 if a["poc"] == "Kai Menon"], [])
        check("...who is named by Rule 13 instead",
              "Kai Menon" in [a["poc"] for a in r13], True)
        rows = world(MON)["rows"]
        kai = next(r for r in rows if r["name"] == "Kai Menon")
        eve = next(r for r in rows if r["name"] == "Eve Rao")
        check("the two rules ask ONE function who is covered",
              (nextaction.covered_by_next_steps(kai), nextaction.covered_by_next_steps(eve)), (True, False))

        r7_rule = [rules_mod.by_id("R7")]

        def listed(row):
            return [a["poc"] for a in nextaction.run(today=MON, rows=[row], day_rules=r7_rule)["actions"]]

        dm = cell(MON - timedelta(days=11))
        check("Connected but NO LI Connected Date: Rule 13 skips them, so R7 lists them",
              listed(poc(20, "Lumen", "Lia Das", li_dm_date=dm, sid_li_added="Connected")), ["Lia Das"])
        check("a date but not marked Connected: R7 lists them",
              listed(poc(21, "Mason", "Max Roy", li_dm_date=dm,
                         li_connected_date=cell(MON - timedelta(days=20)))), ["Max Roy"])
        check("Connected with a date: never R7's, however old the DM",
              listed(connected(22, "Nimbus", "Nia Sen", MON, li_dm_date=cell(MON - timedelta(days=60)))), [])
        check("a different spelling of Connected is still covered (' connected ')",
              listed(connected(23, "Orbit", "Oz Kar", MON, li_dm_date=dm, sid_li_added=" connected ")), [])
        check("a meeting booked: not R7's either way",
              listed(poc(24, "Pine", "Pam Jha", li_dm_date=dm, meeting_date=cell(MON + timedelta(days=2)))), [])
        check("R7 is Mondays only",
              [d.strftime("%a") for d in WEEK + (SAT, SUN) if "R7" in [r.id for r in rules_mod.for_day(d)]],
              ["Mon"])
        check("R7's post is a spaced one, second in Monday's order, and counts",
              [(m["send_at_hhmm"], m["pinned"], m["counts_toward_cap"])
               for m in plan(MON)["messages"] if m["rule_id"] == "R7"], [("16:00", False, True)])


def rule_11(check) -> None:
    """R11 on Wednesdays only: a company that appears on ANY day is asked about on the next Wednesday, once."""
    from db import DB

    tmp = tempfile.mkdtemp(prefix="saley-1069-")
    try:
        with pinned():
            db = DB(os.path.join(tmp, "r11_test.db"))
            # The table is seeded on its first run, a week before anything "appears".
            db.pipeline_snapshot(["Old Co"], today=dl.iso(date(2026, 10, 1)))
            appears = {date(2026, 10, 8): "Thursday Co", date(2026, 10, 9): "Friday Co",
                       date(2026, 10, 10): "Saturday Co", date(2026, 10, 11): "Sunday Co",
                       date(2026, 10, 12): "Monday Co", date(2026, 10, 13): "Tuesday Co",
                       date(2026, 10, 14): "Wednesday Co"}
            on_sheet = ["Old Co"]
            asked: dict = {}
            day = date(2026, 10, 8)
            while day <= date(2026, 10, 28):
                if day in appears:
                    on_sheet.append(appears[day])
                if drip.is_sending_day(day):
                    # exactly what the bot does on a tick: snapshot first, then the queue
                    new = db.pipeline_snapshot(list(on_sheet), today=dl.iso(day))
                    got = nextaction.run(today=day, rows=[], pipeline_companies=list(on_sheet),
                                         new_companies=new, next_step_state={})
                    names = sorted(a["company"] for a in got["by_rule"].get("R11", []))
                    if names:
                        asked[day] = names
                day += timedelta(days=1)

            check("a company that appears on Thursday 8 Oct is asked about on Wednesday 14 Oct",
                  "Thursday Co" in asked.get(date(2026, 10, 14), []), True)
            check("...and on no other day",
                  [d.isoformat() for d, names in asked.items() if "Thursday Co" in names], ["2026-10-14"])
            check("R11 posts on Wednesdays and on no other day of three weeks",
                  sorted({d.strftime("%A") for d in asked}), ["Wednesday"])
            check("Wednesday 14 Oct: everything that appeared Thursday to Tuesday, weekend included",
                  asked.get(date(2026, 10, 14)),
                  ["Friday Co", "Monday Co", "Saturday Co", "Sunday Co", "Thursday Co", "Tuesday Co"])
            check("a company that appears ON a Wednesday waits for the next one (not due the day it appears)",
                  asked.get(date(2026, 10, 21)), ["Wednesday Co"])
            check("every company is asked about exactly once",
                  sorted(n for names in asked.values() for n in names), sorted(appears.values()))
            check("nothing is asked a third Wednesday running", date(2026, 10, 28) in asked, False)

            r11 = rules_mod.by_id("R11")
            check("R11 runs on Wednesday only",
                  [d.strftime("%a") for d in WEEK + (SAT, SUN) if r11.runs_on(d)], ["Wed"])
            check("six companies fit one post (the rule's limit is 10, was 3)",
                  (r11.max_items_per_post, drip.plan(
                      nextaction.run(today=date(2026, 10, 14), rows=[], next_step_state={},
                                     new_companies=[{"company": n, "first_seen": dl.iso(d)}
                                                    for d, n in appears.items() if n != "Wednesday Co"],
                                     )["actions"], day=date(2026, 10, 14))["groups"][0]["overflow"]),
                  (10, 0))
            check("R11 is first in Wednesday's order: 14:00",
                  [(m["send_at_hhmm"], m["pinned"]) for m in plan(WED)["messages"] if m["rule_id"] == "R11"],
                  [("14:00", False)])
            check("the window a weekly rule needs is the shipped one",
                  (config.NEW_COMPANY_AFTER_WORKING_DAYS, config.NEW_COMPANY_WINDOW_DAYS), (1, 7))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def news_window(check) -> None:
    """R1's "since the previous post" is R1's slot on the previous R1 day, read off the schedule."""
    from bot import SalesBot

    real_today, real_now = dl.real_today_ist, dl.real_now_ist
    # A FIXED "REAL" CLOCK, later than the week under test, so every day below is a past pretend day and the answer
    # does not depend on when this is run.
    fixed_now = datetime(2026, 10, 20, 12, 0, tzinfo=dl.IST)
    dl.real_today_ist = lambda: fixed_now.date()
    dl.real_now_ist = lambda: fixed_now
    try:
        with pinned():
            def window(day):
                since, until = SalesBot._main_window(None, day)
                return since.strftime("%a %H:%M"), until.strftime("%a %H:%M")

            check("Tuesday's post (fourth, 20:00) covers from Monday's slot (third, 18:00)",
                  window(TUE), ("Mon 18:00", "Tue 20:00"))
            check("Monday's post covers from Friday's slot (third, 18:00): the whole weekend",
                  window(MON), ("Fri 18:00", "Mon 18:00"))
            check("Wednesday's (second, 16:00) covers from Tuesday's 20:00",
                  window(WED), ("Tue 20:00", "Wed 16:00"))
            check("Thursday's (second, 16:00) covers from Wednesday's 16:00",
                  window(THU), ("Wed 16:00", "Thu 16:00"))
            check("Friday's (third, 18:00) covers from Thursday's 16:00",
                  window(FRI), ("Thu 16:00", "Fri 18:00"))
            check("the windows of a week join end to end: no hour is in two posts or in none",
                  [window(d)[0] == window(p)[1] for p, d in zip(WEEK, WEEK[1:])], [True] * 4)
            check("R1's slot is read off the day's order",
                  [drip.order_slot("R1", d).strftime("%H:%M") for d in WEEK],
                  ["18:00", "20:00", "16:00", "16:00", "18:00"])
            since, until = SalesBot._main_window(None, fixed_now.date())
            check("on the real day the window ends now, and still starts at the previous R1 slot",
                  (since.strftime("%a %d %H:%M"), until), ("Mon 19 18:00", fixed_now))
            check("the same answer with no bot at all: it reads the schedule, not what was sent",
                  SalesBot._main_window(object(), TUE) == SalesBot._main_window(None, TUE), True)

            # R1 itself: one item, no fixed time, never held.
            item = [a for a in queue(TUE)["actions"] if a["rule_id"] == "R1"]
            check("R1 is one item with no fixed time", [(len(item), item[0].get("dayof_time", ""))], [(1, "")])
            key = drip.group(item, day=TUE)[0]["group_key"]
            held = drip.plan(item, day=TUE, history={key: {"last_nudge": dl.iso(MON), "last_sent": dl.iso(MON)}})
            check("posted yesterday, it still posts today, as a fresh post",
                  [(m["rule_id"], m["stage"]) for m in held["messages"]], [("R1", drip.STAGE_NUDGE)])
        check("NEWS_MAIN_TIME is retired: the code has no such setting", hasattr(config, "NEWS_MAIN_TIME"), False)
    finally:
        dl.real_today_ist, dl.real_now_ist = real_today, real_now


def startup(check) -> None:
    """The startup check: a rule's `weekdays` and its days in `daily_order` must agree, and the error names both."""
    path = (config.BOT_RULES_FILE or "").strip()
    with open(path, encoding="utf-8") as fh:
        shipped = fh.read()
    tmp = tempfile.mkdtemp(prefix="saley-1069-rules-")
    # Each refusal below is logged as an ERROR, as it would be at boot. That is the behaviour under test, not a
    # failure of this run, so the log is quietened for the length of the check.
    import logging
    rules_log = logging.getLogger("rules")
    was_level = rules_log.level
    rules_log.setLevel(logging.CRITICAL)

    def refused(old: str, new: str) -> str:
        assert shipped.count(old) == 1, (old, shipped.count(old))
        target = os.path.join(tmp, "bot_rules.yaml")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(shipped.replace(old, new))
        try:
            rules_mod.load(target, force=True)
        except rules_mod.RulesError as e:
            return f"{e} | {e.remedy}"
        return ""

    try:
        err = refused("    weekdays: [wed]\n    trigger: new_pipeline_company",
                      "    weekdays: [mon, tue, wed, thu, fri]\n    trigger: new_pipeline_company")
        check("R11 set back to every weekday while daily_order lists it on Wednesday: refused at startup",
              bool(err), True)
        check("...the error names the rule, its weekdays and its days in daily_order",
              ("R11 (New company in the Master Pipeline)" in err or "R11 (" in err,
               "its weekdays say [mon, tue, wed, thu, fri]" in err, "daily_order lists it on [wed]" in err),
              (True, True, True))
        check("...and says what to do", "Change one of them in bot_rules.yaml" in err, True)
        err = refused("  mon: [R4, R7, R1, R10]", "  mon: [R4, R1, R10]")
        check("R7 runs on Monday but is missing from Monday's order: refused, naming both",
              ("R7 (" in err, "its weekdays say [mon]" in err, "daily_order lists it on [no day]" in err),
              (True, True, True))
        err = refused("  tue: [R5, R6, R2, R1]", "  tue: [R5, R6, R2, R1, R12]")
        check("R12 listed on a Tuesday it does not run: refused, naming both",
              ("R12 (" in err, "its weekdays say [thu]" in err, "daily_order lists it on [tue, thu]" in err),
              (True, True, True))
        for rule, name in (("R8", "meeting prep"), ("R9", "the meeting follow-up"), ("R13", "next steps")):
            err = refused("  wed: [R11, R1, R3]", f"  wed: [R11, R1, R3, {rule}]")
            check(f"{rule} ({name}) in a day's order: refused, it has its own fixed time",
                  (rule in err, "fixed time" in err), (True, True))
        check("a rule listed twice in a day: refused",
              "R1 twice" in refused("  fri: [R2, R6, R1]", "  fri: [R2, R6, R1, R1]"), True)
        check("an id that is not a rule: refused",
              "'R77'" in refused("  fri: [R2, R6, R1]", "  fri: [R2, R6, R1, R77]"), True)
        check("an order for a Saturday: refused",
              "'sat'" in refused("  fri: [R2, R6, R1]", "  fri: [R2, R6, R1]\n  sat: [R1]"), True)
        rules_mod.reload(os.path.join(tmp, "bot_rules.yaml"))
        check("a refused file loads NO rules, so nothing proactive runs on it",
              (rules_mod.safe_load(), bool(rules_mod.status()["error"])), ([], True))
    finally:
        rules_mod.reload(path)
        rules_log.setLevel(was_level)
        shutil.rmtree(tmp, ignore_errors=True)
    check("the shipped file loads, with the order the team gave",
          [rules_mod.order_for(d) for d in WEEK],
          [["R4", "R7", "R1", "R10"], ["R5", "R6", "R2", "R1"], ["R11", "R1", "R3"],
           ["R5", "R1", "R12"], ["R2", "R6", "R1"]])
    check("R8, R9 and R13 appear in no day's order",
          [r for d in WEEK for r in rules_mod.order_for(d) if r in ("R8", "R9", "R13")], [])
    check("every rule's weekdays agree with its days in the order (the shipped file, checked directly)",
          [r.id for r in rules_mod.safe_load() if not rules_mod.is_fixed_time(r)
           and {d.weekday() for d in WEEK if r.id in rules_mod.order_for(d)} != set(r.weekdays)], [])


def weekend(check) -> None:
    """Sunday is unchanged: SUNDAY_RULE_IDS=R4, one post at 14:00, only for a P1 due on the Monday. Saturday: none."""
    with pinned():
        def sunday(due: date) -> dict:
            deliverables = [{"_row": 2, "_extra": {}, "action_item": "MSA template", "priority": "P1",
                             "status": "", "deadline": f"{due.day}-{due.strftime('%b')}",
                             "dependency": "Legal", "remarks": "", "link": ""}]
            base = world(SUN)
            base["deliverables"] = deliverables
            return drip.plan(nextaction.run(**base)["actions"], day=SUN)

        due_monday = sunday(SUN + timedelta(days=1))
        check("a Sunday with a P1 due on the Monday: one post, the checklist, at 14:00",
              posts(due_monday), [("R4", "14:00")])
        check("...no AI news, no next steps; the meeting posts wait for Monday",
              sorted({g["rule_id"] for g in due_monday["held"]}), ["R8", "R9"])
        check("a Sunday with nothing due on the Monday: silent",
              posts(sunday(SUN + timedelta(days=4))), [])
        check("Sunday has no order of its own", (rules_mod.order_for(SUN), rules_mod.order_for(SAT)), ([], []))
        sat = drip.plan(nextaction.run(**world(SAT))["actions"], day=SAT)
        check("Saturday: nothing", (posts(sat), drip.is_sending_day(SAT)), ([], False))
        check("Sunday is a sending day only because of SUNDAY_RULE_IDS",
              (drip.is_sending_day(SUN), sorted(drip.sunday_rule_ids())), (True, ["R4"]))


def settings(check) -> None:
    """The defaults in the code and in .env.example, and the retired name."""
    import inspect
    import re

    source = inspect.getsource(config)
    example = open(os.path.join(ROOT, ".env.example"), encoding="utf-8").read()
    # The banner is the LAST whole line starting "# RETIRED": the word is all over the file's ordinary comments.
    banner = list(re.finditer(r"^#\s*RETIRED\b.*$", example, re.M))[-1].start()
    live, retired = example[:banner], example[banner:]

    def env(name):
        m = re.search(rf"^{name}=(.*)$", live, re.M)
        return m.group(1).strip() if m else None

    def code(name):
        m = re.search(rf'^{name} = _int\("{name}", (\d+)\)', source, re.M)
        return int(m.group(1)) if m else None

    check(".env.example: gap 120, floor 120, jitter 0, window 14:00 to 20:00, cap 5",
          [env(n) for n in ("MESSAGE_GAP_MINUTES", "MESSAGE_GAP_MIN_MINUTES", "MESSAGE_JITTER_MINUTES",
                            "SALES_DRIP_START", "SALES_DRIP_END", "DAILY_MESSAGE_CAP")],
          ["120", "120", "0", "14:00", "20:00", "5"])
    check("config.py: the same defaults in code",
          [code(n) for n in ("MESSAGE_GAP_MINUTES", "MESSAGE_GAP_MIN_MINUTES", "MESSAGE_JITTER_MINUTES",
                             "DAILY_MESSAGE_CAP")], [120, 120, 0, 5])
    check("config.py: SALES_DRIP_END falls back to 20:00", ').strip() or "20:00"' in source, True)
    check(".env.example: fixed times 10:00 (meetings) and 15:00 (next steps); R11 after 1 working day, window 7",
          [env(n) for n in ("MEETING_DAYOF_TIME", "NEXT_STEP_TIME", "NEW_COMPANY_AFTER_WORKING_DAYS",
                            "NEW_COMPANY_WINDOW_DAYS", "SUNDAY_RULE_IDS")], ["10:00", "15:00", "1", "7", "R4"])
    check("NEWS_MAIN_TIME is not a live setting any more", env("NEWS_MAIN_TIME"), None)
    check("...it is in the RETIRED block, commented, saying what replaced it",
          bool(re.search(r"^# NEWS_MAIN_TIME=14:00\s+-> `daily_order`", retired, re.M)), True)
    code_files = [f for f in os.listdir(ROOT) if f.endswith(".py") and not f.startswith("verify_")]
    reads = []
    for name in code_files:
        text = open(os.path.join(ROOT, name), encoding="utf-8").read()
        if re.search(r"config\.NEWS_MAIN_TIME|getenv\(\s*[\"']NEWS_MAIN_TIME", text):
            reads.append(name)
    check("no product file reads NEWS_MAIN_TIME", reads, [])
    with pinned():
        check("the earliest anything may go out is the 10:00 fixed time", drip.earliest_send_ist(), (10, 0))


def wording(check) -> None:
    """The words for the sheet say the new order and no longer say "2 PM" for the news."""
    rules_mod.reload()
    ai = rules_mod.global_rule("ai_news")
    check("Global Rules, AI news: no \"every weekday at 2 PM\", no \"planned first\"",
          [p for p in ("every weekday at 2 PM", "planned first", "no 2 PM post") if p in ai], [])
    check("...it gives AI news its place in the day's order",
          all(p in ai for p in ("in its place in the day's order", "6 PM on Monday and Friday", "8 PM on Tuesday",
                                "4 PM on Wednesday and Thursday")), True)
    check("...and the weekend is as it was",
          "there is no AI news post at the weekend, and Monday's post covers it" in ai, True)
    order = rules_mod.global_rule("day_order")
    check("Global Rules has the order of the day, weekday by weekday",
          all(d in order for d in ("Monday:", "Tuesday:", "Wednesday:", "Thursday:", "Friday:")), True)
    for rule, must in (("R1", "in its place in the day's order"), ("R7", "Rule 13 comes first"),
                       ("R8", "Every touch posts at 10 AM"), ("R9", "Posts at 10 AM"),
                       ("R11", "every Wednesday")):
        text = rules_mod.sheet_wording_for(rule)
        check(f"Bot Rules, rule {rule[1:]}: the wording for the sheet says \"{must}\"", must in text, True)
        check(f"...and no longer gives the news a fixed 2 PM",
              [p for p in ("at 2 PM with", "No 2 PM post") if p in text], [])
    strategy = open(os.path.join(ROOT, "sales_strategy.md"), encoding="utf-8").read()
    check("sales_strategy.md: the weekly schedule is the team's order",
          all(p in strategy for p in (
              "2 PM deliverables checklist · 4 PM DM sent, no meeting · 6 PM AI news · 8 PM closure support",
              "2 PM new companies in the pipeline (asks first) · 4 PM AI news · 6 PM AI events")), True)
    check("...and nothing in it gives the news a fixed time or says R7 is replaced",
          [p for p in ("One post at 2:00 PM", "planned first", "Replaced by rule 13",
                       "Friday 2 PM to Monday 2 PM") if p in strategy], [])
    sheet_doc = open(os.path.join(ROOT, "docs", "rules-for-the-sheet-oct8.md"), encoding="utf-8").read()
    check("docs/rules-for-the-sheet-oct8.md has the Weekly Schedule tab and each changed rule",
          [p for p in ("Weekly Schedule tab", "Rule 7 (DM sent, no meeting)", "Rule 8 (Meeting preparation)",
                       "Rule 9 (Meeting done, no next steps)", "Rule 11 (New company in the Master Pipeline)",
                       "Rule 1 (AI news)") if p not in sheet_doc], [])
    for rule in ("R1", "R7", "R8", "R9", "R11"):
        check(f"...and its rule {rule[1:]} wording is the rules file's, word for word",
              rules_mod.sheet_wording_for(rule) in " ".join(sheet_doc.replace("> ", " ").split()), True)


SECTIONS = (
    ("EVERY WEEKDAY, EVERY RULE DUE", every_weekday),
    ("A RULE WITH NOTHING TO POST TAKES NO SLOT", moving_up),
    ("THE GAP NEVER SHRINKS, AND NOTHING AFTER 20:00", the_gap),
    ("RULE 7 IS BACK ON MONDAYS, MINUS RULE 13's PEOPLE", rule_7),
    ("RULE 11 ON WEDNESDAYS ONLY", rule_11),
    ("AI NEWS: SINCE THE PREVIOUS POST'S SLOT", news_window),
    ("THE STARTUP CHECK: weekdays AND daily_order MUST AGREE", startup),
    ("THE WEEKEND IS UNCHANGED", weekend),
    ("THE SETTINGS", settings),
    ("THE WORDING", wording),
)


def run(check, say=None) -> None:
    for title, fn in SECTIONS:
        if say:
            say(title)
        fn(check)
