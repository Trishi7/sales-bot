"""R4 "DELIVERABLES TO CHASE" (rebuilt 12 Oct 2026) — the checks, shared by verify_deliverables.py and
tests/test_deliverables.py.

    sys.path.insert(0, <repo>/tests); import deliverables_checks as dc
    dc.run(check)                 # check(name, got, want)

WHAT RUNS HERE IS THE REAL THING: rows through the real sheet parser, the real rule (`nextaction.run`), the real
renderer (`drip.render_deliverables`, `wording.deliverables_post`), and a real bot behind a fake Discord with a
throwaway database for the plan, the send and the live sweep's cutoff. No network, no model, no real sheet.

Every R4 setting is pinned to its .env.example default, never read from this machine's .env (which may still carry
the old, wider marker lists).

EVERY ACTION ITEM, TEAM AND DATE HERE IS MADE UP, except "New Hinglish STT Demo/Sales Package", the ticket's own
example. `todays_shape()` reproduces only the COUNTS the ticket gives for the tab on 12 Oct (17 rows: 11 open P1,
4 not P1, 2 Completed, and a third item of 181 characters); its contents are invented.
"""
import asyncio
import contextlib
import logging
import os
import re
import sys
from datetime import date, datetime, time, timedelta

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
import gtm_sheet  # noqa: E402
import links  # noqa: E402
import nextaction  # noqa: E402
import rules as rules_mod  # noqa: E402
import wording  # noqa: E402

MON = date(2026, 10, 12)                       # a Monday
SUN = date(2026, 10, 18)
HEADERS = ["Sr No", "Action Item", "Priority", "Status", "Tentative Deadline", "Functional Dependency",
           "Reminder Freq", "Remarks", "Link/Destination"]
HEADING = "**Deliverables to chase**"
NO_DEADLINE = "   There's no deadline for this"
_ABSENT = object()


def _example(name: str) -> str:
    for line in open(os.path.join(ROOT, ".env.example"), encoding="utf-8"):
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip()
    raise AssertionError(f"{name} is not in .env.example")


def _list(name: str) -> list:
    return [x.strip() for x in _example(name).split(",") if x.strip()]


def pins() -> dict:
    return {"DELIVERABLE_P1_MARKERS": _list("DELIVERABLE_P1_MARKERS"),
            "DELIVERABLE_DONE_MARKERS": _list("DELIVERABLE_DONE_MARKERS"),
            "DELIVERABLE_DEFAULT_OWNER": _example("DELIVERABLE_DEFAULT_OWNER"),
            "SUNDAY_RULE_IDS": _list("SUNDAY_RULE_IDS"),
            "SALES_DRIP_START": _example("SALES_DRIP_START"), "SALES_DRIP_END": _example("SALES_DRIP_END"),
            "DAILY_MESSAGE_CAP": int(_example("DAILY_MESSAGE_CAP"))}


@contextlib.contextmanager
def pinned(**extra):
    values = dict(pins(), **extra)
    before = {k: getattr(config, k, _ABSENT) for k in values}
    for k, v in values.items():
        setattr(config, k, v)
    try:
        yield
    finally:
        for k, v in before.items():
            if v is _ABSENT:
                with contextlib.suppress(AttributeError):
                    delattr(config, k)
            else:
                setattr(config, k, v)


# -- rows -------------------------------------------------------------------------------------------------------------

def item(n, title, priority="P1", status="", deadline="", team="Sales", link="", remarks="", headers=None) -> list:
    cells = {"Sr No": str(n), "Action Item": title, "Priority": priority, "Status": status,
             "Tentative Deadline": deadline, "Functional Dependency": team, "Remarks": remarks,
             "Link/Destination": link}
    return [cells.get(h, "") for h in (headers or HEADERS)]


def parsed(rows, headers=None) -> list:
    tab = gtm_sheet.SHEETS._parse_values("Deliverables Checklist",
                                         [list(headers or HEADERS)] + [list(r) for r in rows],
                                         read_at=dl.real_epoch())
    return list(tab.rows) if tab is not None else []


def engine(rows, day=MON, headers=None):
    return nextaction.run(today=day, rows=[], deliverables=parsed(rows, headers) if rows else [],
                          day_rules=[rules_mod.by_id("R4")])


def r4(result) -> list:
    return sorted((a for a in result["actions"] if a.get("rule_id") == "R4"), key=lambda a: a["pick_order"])


def named(result) -> list:
    return [a["deliverable"] for a in r4(result)]


def message_of(result, day=MON):
    return next((m for m in drip.group(result["actions"], day=day)
                 if m.get("type") == nextaction.R_DELIVERABLES), None)


RENDERED: list = []


def post_of(result, day=MON) -> str:
    m = message_of(result, day)
    text = drip.compose_fallback(m) if m else ""
    if text:
        RENDERED.append(text)
    return text


def one(rows, day=MON, **kw) -> str:
    return post_of(engine(rows, day, **kw), day)


def blocks(text) -> list:
    """The numbered items of a rendered post, each as its three lines."""
    out, lines = [], text.split("\n")
    for i, line in enumerate(lines):
        if re.match(r"^\d+\. ", line):
            out.append(lines[i:i + 3])
    return out


def todays_shape() -> list:
    """17 rows with the counts the ticket gives for 12 Oct: 11 open P1, 4 not P1, 2 Completed. Made-up contents."""
    long_title = ("Third item on the tab, whose title runs to one hundred and eighty-one characters so that it is "
                  "the first one the trim touches: " + "x" * 200)[:181]
    return [
        item(1, "New Hinglish STT Demo/Sales Package", "P1", "In progress", "14-Sep", "Sales"),
        item(2, "Pricing page copy", "P1", "", "30-Sep", "Marketing", link="https://docs.example/pricing"),
        item(3, long_title, "P1", "Not started", "20-Oct", "Engineering"),
        item(4, "Case study: voice agents", "P2", "", "15-Oct", "Sales"),
        item(5, "MSA template", "P1", "Completed", "1-Oct", "Legal"),
        item(6, "Security questionnaire", "P1", "", "31-Dec", "Legal"),
        item(7, "Partner one-pager", "P3", "In progress", "5-Oct", "Marketing"),
        item(8, "Demo environment", "P1", "Blocked", "20-Oct", ""),
        item(9, "Onboarding checklist", "P1", "", "", "Ops"),
        item(10, "Investor update", "", "", "16-Oct", "Sid"),
        item(11, "Evals benchmark write-up", "P1", "In progress", "2-Nov", "Research"),
        item(12, "DPA review", "P1", "Completed", "9-Oct", "Legal"),
        item(13, "Website refresh", "P2", "Not started", "30-Nov", "Marketing"),
        item(14, "Sales deck v3", "P1", "", "12-Oct", "Sales"),
        item(15, "Customer reference list", "P1", "", "TBD", "Sales"),
        item(16, "Q4 pipeline review", "P1", "In progress", "26-Oct", "Sales"),
        item(17, "Support runbook", "P1", "", "15-Dec", "Ops"),
    ]


class LogTap(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.INFO)
        self.lines = []

    def emit(self, record):
        with contextlib.suppress(Exception):
            self.lines.append((record.levelname, record.getMessage()))


@contextlib.contextmanager
def tapped():
    tap, logger = LogTap(), logging.getLogger()
    was = logger.level
    logger.addHandler(tap)
    logger.setLevel(logging.INFO)
    try:
        yield tap
    finally:
        logger.removeHandler(tap)
        logger.setLevel(was)


# -- the checks -----------------------------------------------------------------------------------------------------------

def the_selection(check):
    print("\nTHE SELECTION — Priority is P1, Status is not Completed, and nothing else")
    check("a P1 due in 2028 is listed: there is no deadline window",
          named(engine([item(1, "Far future", deadline="14-03-2028")])), ["Far future"])
    check("a P1 overdue by a year is listed",
          named(engine([item(1, "Long overdue", deadline="10-10-2025")])), ["Long overdue"])
    with tapped() as tap:
        got = engine([item(1, "High one", "High"), item(2, "One", "1"), item(3, "Critical one", "Critical"),
                      item(4, "Spaced", "P 1"), item(5, "Urgent one", "urgent"), item(6, "Real", "P1"),
                      item(7, "Lower case", " p1 "), item(8, "Two", "P2"), item(9, "Both", "P1/P2")])
    skipped = [m for _l, m in tap.lines if "[R4] skipped" in m]
    check('"High", "1", "Critical", "P 1" and "urgent" do NOT count as P1; "P1" and " p1 " do',
          named(got), ["Real", "Lower case"])
    check("...and every row skipped for its priority is logged with that priority",
          sorted(re.search(r"priority '([^']*)'", m).group(1) for m in skipped),
          sorted(["High", "1", "Critical", "P 1", "urgent", "P2", "P1/P2"]))
    with tapped() as tap:
        blank = engine([item(1, "No priority", ""), item(2, "Kept", "P1")])
    check("a blank Priority is not P1: out, and logged",
          (named(blank), [m for _l, m in tap.lines if "[R4] skipped 'No priority'" in m and "(blank)" in m] != []),
          (["Kept"], True))
    with tapped() as tap:
        got = engine([item(1, "Says done", status="Done"), item(2, "Says closed", status="Closed"),
                      item(3, "Says shipped", status="Shipped"), item(4, "Says complete", status="Complete"),
                      item(5, "Says live", status="live"), item(6, "In flight", status="In progress")])
    unrecognised = [m for lvl, m in tap.lines if lvl == "WARNING" and "not recognised as Completed" in m]
    check('"Done", "Closed", "Shipped", "Complete" and "live" do NOT drop a row',
          named(got), ["Says done", "Says closed", "Says shipped", "Says complete", "Says live", "In flight"])
    check("...and each is logged as a status not recognised as Completed (an ordinary status is not)",
          sorted(re.search(r"status '([^']*)'", m).group(1) for m in unrecognised),
          sorted(["Done", "Closed", "Shipped", "Complete", "live"]))
    check('"Completed " with a trailing space drops a row; so does "completed" in lower case',
          named(engine([item(1, "Trailing", status="Completed "), item(2, "Lower", status="completed"),
                        item(3, "Upper", status="COMPLETED"), item(4, "Open", status="")])), ["Open"])
    check('the EXACT word: "Not completed" and "Completed?" are still open',
          named(engine([item(1, "Not yet", status="Not completed"), item(2, "Query", status="Completed?")])),
          ["Not yet", "Query"])
    check("a blank Status is open", named(engine([item(1, "Blank status", status="")])), ["Blank status"])
    check("a blank Action Item is skipped",
          (named(engine([item(1, ""), item(2, "   "), item(3, "Named")])),
           len(nextaction.run(today=MON, rows=[], day_rules=[rules_mod.by_id("R4")], deliverables=[
               {"_row": 2, "action_item": " ", "priority": "P1", "status": ""}])["actions"])), (["Named"], 0))
    check("nothing else is read: the team, the link, the remarks and the Reminder Freq change nothing",
          named(engine([item(1, "Bare", team="", link="", remarks=""),
                        item(2, "Full", team="Legal", link="https://docs.example/x", remarks="waiting on Sid")])),
          ["Bare", "Full"])
    moved = ["Link/Destination", "Status", "Tentative Deadline", "Action Item", "Remarks", "Priority",
             "Functional Dependency"]
    check("columns the team MOVED are found by their headers",
          named(engine([item(1, "Moved", "P1", "", "20-Oct", "Sales", headers=moved),
                        item(2, "Moved and done", "P1", "Completed", "20-Oct", "Sales", headers=moved)],
                       headers=moved)), ["Moved"])
    report = engine(todays_shape())["reports"]["R4"]
    check("THE SHAPE OF TODAY'S TAB: 17 rows in, 11 items out, 4 skipped for priority, 2 dropped for Completed",
          (report["rows"], report["listed"], len(report["not_p1"]), len(report["completed"])), (17, 11, 4, 2))
    check("nothing drops on a clock: a year of Mondays later the same tab gives the same eleven",
          (len(named(engine(todays_shape(), MON + timedelta(days=364)))),
           sorted(named(engine(todays_shape(), MON + timedelta(days=364)))) == sorted(named(engine(todays_shape())))),
          (11, True))
    done = todays_shape()
    done[0] = item(1, "New Hinglish STT Demo/Sales Package", "P1", "Completed", "14-Sep", "Sales")
    check("ONE thing drops a deliverable: its Status becomes Completed",
          (len(named(engine(done))), "New Hinglish STT Demo/Sales Package" in named(engine(done))), (10, False))
    rule = rules_mod.by_id("R4")
    check("R4: Mondays only, twenty a post, in the channel, and it counts toward the day's cap",
          (rule.weekdays, rule.max_items_per_post, rule.destination, rule.counts_toward_cap),
          ((0,), 20, "channel", True))


def the_order(check):
    print("\nTHE ORDER — earliest deadline first, no deadline last, a tie in sheet order")
    rows = [item(1, "Third", deadline="20-Oct"), item(2, "First", deadline="14-Sep"),
            item(3, "No date", deadline=""), item(4, "Second", deadline="12-Oct"),
            item(5, "Unreadable", deadline="sometime soon"), item(6, "Last dated", deadline="31-Dec")]
    check("earliest deadline first; no deadline and an unreadable one last, in sheet order",
          named(engine(rows)), ["First", "Second", "Third", "Last dated", "No date", "Unreadable"])
    same = [item(1, "Zebra", deadline="20-Oct"), item(2, "Apple", deadline="20-Oct"),
            item(3, "Mango", deadline="20-Oct"), item(4, "Earlier", deadline="19-Oct")]
    check("two rows on one date keep the SHEET'S order (it used to be alphabetical)",
          named(engine(same)), ["Earlier", "Zebra", "Apple", "Mango"])
    check("...the same next Monday: the list does not reshuffle",
          named(engine(same, MON + timedelta(days=7))), ["Earlier", "Zebra", "Apple", "Mango"])
    check('a year-less deadline that has just passed ("25-Sep" on 12 Oct) is this year\'s, and sorts first',
          ([(a["deliverable"], a["deadline"]) for a in r4(engine([item(1, "Next month", deadline="5-Nov"),
                                                                  item(2, "Just gone", deadline="25-Sep")]))]),
          [("Just gone", "2026-09-25"), ("Next month", "2026-11-05")])
    check("...and the date never decides whether an item appears",
          sorted(named(engine([item(1, "A", deadline="1-Jan"), item(2, "B", deadline="31-Dec"),
                               item(3, "C", deadline="")]))), ["A", "B", "C"])


def the_message(check):
    print("\nTHE MESSAGE — the count, then three lines an item, and nothing after the list")
    post = one([item(1, "New Hinglish STT Demo/Sales Package", deadline="14-Sep", team="Sales"),
                item(2, "MSA template", deadline="", team="Legal"),
                item(3, "Pricing pge copy (typo and all)", deadline="soonish", team="")])
    check("the exact opener and the three-line items",
          post, "3 open:\n1. New Hinglish STT Demo/Sales Package\n   Team: Sales\n   Due: Mon 14 Sep\n"
                "2. MSA template\n   Team: Legal\n" + NO_DEADLINE + "\n"
                "3. Pricing pge copy (typo and all)\n   Team: Vaishnavi\n" + NO_DEADLINE)
    check("a row with no deadline prints \"There's no deadline for this\" IN PLACE OF the Due line; an "
          "unreadable one does the same",
          [b[2] for b in blocks(post)], ["   Due: Mon 14 Sep", NO_DEADLINE, NO_DEADLINE])
    check("the title is the Action Item exactly as the sheet spells it; a blank team is the default owner",
          (blocks(post)[2][0], blocks(post)[2][1]), ("3. Pricing pge copy (typo and all)", "   Team: Vaishnavi"))
    solo = one([item(1, "Only one", deadline="20-Oct")])
    check("one item reads the same way: '1 open:'", solo, "1 open:\n1. Only one\n   Team: Sales\n   Due: Tue 20 Oct")
    m = message_of(engine([item(1, "Only one", deadline="20-Oct")]))
    check("the heading is 'Deliverables to chase', put on by the sender and not part of the block",
          (drip.heading_for(m, day=MON), drip.HEADINGS["R4"], "Deliverables to chase" in solo),
          (HEADING, "Deliverables to chase", False))
    check("ONE opener, every Monday: no variants are picked, whatever the day or the slot",
          sorted({drip.compose_fallback(dict(m, _voice_seed=seed, _voice={})).split("\n")[0]
                  for seed in ("2026-10-12|1", "2026-10-19|2", "2026-10-26|3", "2026-11-02|4")}), ["1 open:"])
    check("NO CLOSING LINE: the post ends on its last item, and the old closes are gone from the code",
          (solo.split("\n")[-1], drip.points_of(m)["close"],
           [n for n in ("DELIVERABLES_CLOSES", "DELIVERABLES_CLOSE", "DELIVERABLES_OPENERS") if hasattr(drip, n)],
           nextaction.R_DELIVERABLES in drip._ASK), ("   Due: Tue 20 Oct", "", [], False))
    late = one([item(1, "A year late", deadline="10-10-2025"), item(2, "Late last week", deadline="5-Oct")])
    check("NO OVERDUE COUNT: a past date shows plainly as its date",
          [b[2] for b in blocks(late)], ["   Due: Fri 10 Oct", "   Due: Mon 5 Oct"])
    linked = one([item(1, "Has a doc", deadline="20-Oct", link="https://docs.google.com/document/d/abc"),
                  item(2, "Has a tracker", deadline="21-Oct", link="https://tracker.example/1",
                       remarks="waiting on legal", status="Blocked")])
    check("NO LINKS, and no status or remarks: an item with a link is still three lines",
          ([len(b) for b in blocks(linked)], len(linked.split("\n")),
           [w for w in ("[Doc]", "http", "waiting on legal", "Blocked") if w in linked]), ([3, 3], 7, []))
    check("...the link, the remarks and the status do not even travel on the item",
          sorted({k for a in r4(engine([item(1, "Has a doc", link="https://docs.example/a", remarks="r",
                                              status="Blocked")])) for k in ("link", "remarks", "status") if k in a}),
          [])
    t179, t180, t181 = ("T" + "i" * 178), ("T" + "i" * 179), ("T" + "i" * 180)
    cut = one([item(1, t179, deadline="20-Oct"), item(2, t180, deadline="21-Oct"), item(3, t181, deadline="22-Oct")])
    shown = [b[0].split(". ", 1)[1] for b in blocks(cut)]
    check("a 179-character title prints whole, and so does one of exactly 180; a 181-character one is trimmed "
          "with an ellipsis",
          ([len(t) for t in (t179, t180, t181)], shown[0] == t179, shown[1] == t180, shown[2] == t181[:180] + "…"),
          ([179, 180, 181], True, True, True))
    twice = one([item(1, "Overview doc", deadline="20-Oct"), item(2, "Pricing page", deadline="21-Oct"),
                 item(3, "overview  doc", deadline="22-Oct")])
    check("the same Action Item on two rows appears ONCE",
          (twice.split("\n")[0], [b[0] for b in blocks(twice)]), ("2 open:", ["1. Overview doc", "2. Pricing page"]))
    many = [item(i, f"Deliverable number {i:02d}", deadline=f"{i}-Nov") for i in range(1, 24)]
    post23 = one(many)
    check("23 items: the count is 23, twenty blocks are listed, then the exact overflow line",
          (post23.split("\n")[0], len(blocks(post23)), post23.split("\n")[-1], len(post23.split("\n"))),
          ("23 open:", 20, "(+3 more — the rest are on the Deliverables Checklist.)", 62))
    check("...which asks for nothing, and is absent at twenty or fewer",
          ("ask" in post23.split("\n")[-1].lower() or "?" in post23.split("\n")[-1],
           "more — the rest" in one(many[:20])), (False, False))
    forty = [item(i, f"Deliverable {i:02d} " + "with a long descriptive title about the thing to ship " * 2,
                  deadline=f"{(i % 28) + 1}-Nov", team="Engineering") for i in range(1, 41)]
    post40 = one(forty)
    parts = drip.split_on_lines(post40, limit=1900)
    check("a 40-item list: twenty blocks and '(+20 more …)', split BETWEEN LINES, nothing truncated or dropped",
          (len(post40) > 2000, len(parts) > 1, max(len(p) for p in parts) <= 1900, "\n".join(parts) == post40,
           len(blocks(post40)), [b for b in blocks(post40) if len(b) != 3 or not b[2].startswith("   Due: ")],
           post40.split("\n")[-1]),
          (True, True, True, True, 20, [], "(+20 more — the rest are on the Deliverables Checklist.)"))
    every = drip.render_deliverables(r4(engine(forty)), limit=40)
    whole = "\n".join(every)
    check("...and with room for all forty, all forty survive the split",
          (len(blocks(whole)), "\n".join(drip.split_on_lines(whole, limit=1900)) == whole,
           sorted(int(b[0].split(".")[0]) for b in blocks(whole)) == list(range(1, 41))), (40, True, True))
    check("nothing qualifies: no post at all, and no 'all clear' line",
          one([item(1, "Done", status="Completed"), item(2, "Not P1", "P2")]), "")
    check("rendered in code: R4 is verbatim, and what the renderer returns is what is sent",
          (nextaction.R_DELIVERABLES in drip.VERBATIM_TYPES, drip.is_verbatim(m),
           "\n".join(drip.render_deliverables(m["actions"])) == drip.compose_fallback(m)), (True, True, True))
    check("no 'overdue', no '[Doc]', no 'http' and no question anywhere in any rendered post",
          [w for w in ("overdue", "[Doc]", "http", "?", "I'll", "this week") if any(w in t for t in RENDERED)], [])
    check("nothing in any rendered post is a link", [t for t in RENDERED if links.bare_urls(t) or "](" in t], [])
    check("the fixed lines are in the register check",
          [l for l in ("3 open:", "   Team: Sales", "   Due: Mon 14 Sep", NO_DEADLINE,
                       "(+1 more — the rest are on the Deliverables Checklist.)") if l not in wording.all_lines()],
          [])


def the_day(check):
    import replies_world as rw

    print("\nTHE DAY — Monday only, through the real planner and sender")

    async def monday(test_mode=False, rows=None):
        out = {}
        with rw.World(test_mode, pretend=(MON, 13, 0), rules={"R4"}, pins=dict(pins())) as w:
            w.sheet.grids["Deliverables Checklist"] = [list(HEADERS)] + (rows if rows is not None else todays_shape())
            planned = await w.bot._plan_drip(today=MON, already=[])
            msgs = [m for m in (planned or {}).get("messages") or [] if m.get("type") == nextaction.R_DELIVERABLES]
            out["planned"] = [(m["send_at_hhmm"], m["counts_toward_cap"], m["pinned"]) for m in msgs]
            n, model = len(w.posted), w.counts()["model"]
            for m in msgs:
                await w.bot._research_message(m, today=MON)
                await w.bot._send_drip_message(w.chan, m, marker=dl.iso(MON), channel_id=w.chan.id)
            out["parts"] = [rw.strip_tag(p.content) for p in w.posted[n:]]
            out["tagged"] = [p.content.startswith("[TEST") for p in w.posted[n:]]
            out["model"] = w.counts()["model"] - model
            out["slots"] = len(w.bot.db.drip_sent_today(dl.iso(MON)))
            out["counted"] = drip.counted_today(w.bot.db.drip_sent_today(dl.iso(MON)))
            again = await w.bot._plan_drip(today=MON, already=w.bot.db.drip_sent_today(dl.iso(MON)))
            out["again"] = len((again or {}).get("messages") or [])
            out["writes"] = len(w.sheet.writes)
        return out

    async def sunday():
        with rw.World(False, pretend=(SUN, 13, 0), rules={"R4"}, pins=dict(pins())) as w:
            # A P1 DUE ON THE MONDAY: exactly what the old Sunday exception posted about.
            w.sheet.grids["Deliverables Checklist"] = [list(HEADERS)] + [
                item(1, "Due tomorrow", deadline=f"{(SUN + timedelta(days=1)).day}-Oct")]
            queue = await w.bot._run_next_actions(today=SUN)
            planned = await w.bot._plan_drip(today=SUN, already=[], queue=queue)
            return ([a["rule_id"] for a in (queue or {}).get("actions") or []],
                    len((planned or {}).get("messages") or []), drip.is_sending_day(SUN),
                    sorted(drip.sunday_rule_ids()), len(w.posted))

    async def sweep(hh, mm):
        """The LIVE sweep itself at a Monday minute, with the bot having been down until then."""
        with rw.World(False, pretend=(MON, hh, mm), rules={"R4"}, pins=dict(pins())) as w:
            w.sheet.grids["Deliverables Checklist"] = [list(HEADERS)] + [item(1, "Still open", deadline="20-Oct")]
            w.bot._live_loop_held = lambda *_a, **_k: False
            w.bot.get_channel = lambda _cid: w.chan

            async def nothing(*_a, **_k):
                return 0
            w.bot._sweep_proposals = nothing
            w.bot._reanchor_meeting_reminders = nothing
            n = len(w.posted)
            with tapped() as tap:
                await w.bot._maybe_send_drip()
            return ([rw.strip_tag(p.content).split("\n")[0] for p in w.posted[n:]],
                    [m for _l, m in tap.lines if "NOT sent late" in m] != [])

    with pinned():
        live = asyncio.run(monday(False))
        test = asyncio.run(monday(True))
        big = asyncio.run(monday(False, [item(i, f"Deliverable {i:02d} " + "with a long descriptive title " * 4,
                                              deadline=f"{(i % 28) + 1}-Nov") for i in range(1, 41)]))
        quiet = asyncio.run(monday(False, [item(1, "Done", status="Completed"), item(2, "Lower", "P3")]))
        sun = asyncio.run(sunday())
        on_time = asyncio.run(sweep(19, 0))
        too_late = asyncio.run(sweep(21, 30))
    text = "\n".join(live["parts"])
    lines = text.split("\n")
    check("Monday: one post, first in the day's order at 14:00, counted toward the cap, with no model call",
          (live["planned"], live["slots"], live["counted"], live["model"]), ([("14:00", True, False)], 1, 1, 0))
    check("the heading, the tags line, then '11 open:' and eleven three-line items",
          (lines[0], lines[1].startswith("<@"), lines[2], len(blocks(text))), (HEADING, True, "11 open:", 11))
    check("the third item on the tab (181 characters) is the one the trim touches",
          [b[0][-1] for b in blocks(text) if len(b[0]) > 150], ["…"])
    check("it is not said twice on the next tick, and nothing is written to the sheet",
          (live["again"], live["writes"]), (0, 0))
    check("test mode is identical apart from the [TEST] tag",
          (["\n".join(test["parts"]) == text], set(live["tagged"]), set(test["tagged"])), ([True], {False}, {True}))
    joined = "\n".join(big["parts"])
    check("a long list goes out as consecutive messages that are ONE slot, each under Discord's limit, with every "
          "item whole",
          (len(big["parts"]) > 1, max(len(p) for p in big["parts"]) <= 2000, big["slots"], big["counted"],
           len(blocks(joined)), [b for b in blocks(joined) if len(b) != 3 or not b[2].startswith("   Due: ")],
           joined.split("\n")[-1]),
          (True, True, 1, 1, 20, [], "(+20 more — the rest are on the Deliverables Checklist.)"))
    check("nothing qualifying: nothing is planned and nothing is posted", (quiet["planned"], quiet["parts"]), ([], []))
    check("A SUNDAY RUN PRODUCES NO POST, even with a P1 due on the Monday: R4 does not run, Sunday is not a "
          "sending day and no rule has the Sunday exception",
          sun, ([], 0, False, [], 0))
    check("the bot down on Monday until 7 PM: the checklist goes out on restart, the same day",
          on_time, ([HEADING], False))
    check("...down until 9:30 PM, past the 8 PM cutoff: NOT sent late; it waits for next Monday",
          too_late, ([], True))


def settings(check):
    import subprocess

    print("\nSETTINGS — the three changed defaults, the retired one, the boot line")
    src = open(os.path.join(ROOT, "config.py"), encoding="utf-8").read()
    check('DELIVERABLE_P1_MARKERS="p1", DELIVERABLE_DONE_MARKERS="completed" and SUNDAY_RULE_IDS empty, in the code '
          "and in .env.example",
          ('_str_list("DELIVERABLE_P1_MARKERS", "p1")' in src, _example("DELIVERABLE_P1_MARKERS"),
           '_str_list("DELIVERABLE_DONE_MARKERS", "completed")' in src, _example("DELIVERABLE_DONE_MARKERS"),
           '_str_list("SUNDAY_RULE_IDS", "")' in src, _example("SUNDAY_RULE_IDS")),
          (True, "p1", True, "completed", True, ""))
    env = open(os.path.join(ROOT, ".env.example"), encoding="utf-8").read()
    readers = []
    for name in sorted(os.listdir(ROOT)):
        if name.endswith(".py") and not name.startswith(("verify_", "test_")):
            if re.search(r"(?:config\.|getenv\(\s*[\"'])DELIVERABLE_NEAR_DAYS|^DELIVERABLE_NEAR_DAYS\s*=",
                         open(os.path.join(ROOT, name), encoding="utf-8").read(), re.M):
                readers.append(name)
    check("DELIVERABLE_NEAR_DAYS is read by nothing, and is in the RETIRED block, commented",
          (readers, bool(re.search(r"(?m)^DELIVERABLE_NEAR_DAYS=", env)),
           "# DELIVERABLE_NEAR_DAYS=" in env[env.index("# RETIRED — setting any of these does NOTHING"):]),
          ([], False, True))
    done = subprocess.run(
        [sys.executable, "-c", "import config, sys; sys.stdout.write(repr((config.DELIVERABLE_P1_MARKERS, "
                               "config.DELIVERABLE_DONE_MARKERS, config.SUNDAY_RULE_IDS)))"],
        cwd=ROOT, capture_output=True, text=True,
        env={k: v for k, v in dict(os.environ, DISCORD_TOKEN="x", ANTHROPIC_API_KEY="x", PYTHONIOENCODING="utf-8",
                                   DELIVERABLE_P1_MARKERS="p1, P1-urgent").items()
             if k not in ("DELIVERABLE_DONE_MARKERS",)})
    check("the lists are still configurable: a value in the environment is what the code uses",
          "'P1-urgent'" in (done.stdout or "") or "'p1-urgent'" in (done.stdout or ""), True)
    with pinned(), tapped() as tap:
        saved = config.NEXT_ACTION_ENABLED
        config.NEXT_ACTION_ENABLED = True
        try:
            config.validate()
        finally:
            config.NEXT_ACTION_ENABLED = saved
    line = [m for _l, m in tap.lines if "[config] R4 deliverables" in m]
    check("the boot line says the two exact values and that only Completed drops a row",
          [("Mondays only" in m, "exactly p1" in m, "not exactly completed" in m, "whatever its deadline" in m,
            "leaves only when it is Completed" in m) for m in line], [(True, True, True, True, True)])
    check("...and the cap line says Sunday is silent too",
          [m for _l, m in tap.lines if "Sunday is silent too" in m] != [], True)
    yaml = rules_mod.sheet_wording_for("R4")
    check("the wording for the Bot Rules tab says the new rule",
          [p for p in ("whatever the Tentative Deadline", "Status is not Completed", "No overdue count, no link",
                       "only when its Status is set to Completed", "Nothing is posted on Sunday")
           if p not in " ".join(yaml.split())], [])
    check("...and nothing in it promises the old one",
          [p for p in ("due this week", "days overdue", "[Doc]", "end of this week") if p in yaml], [])


def run(check):
    with pinned():
        the_selection(check)
        the_order(check)
        the_message(check)
    the_day(check)
    settings(check)
