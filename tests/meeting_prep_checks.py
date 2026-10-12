"""R8 "MEETING PREPARATION" (rebuilt 12 Oct 2026) — the checks, shared by verify_meeting_prep.py and
tests/test_meeting_prep.py.

    sys.path.insert(0, <repo>/tests); import meeting_prep_checks as mpc
    mpc.run(check)                 # check(name, got, want)

WHAT RUNS HERE IS THE REAL THING: rows through the real sheet parser, the real rule (`nextaction.run`,
`meeting_prep.pick`), the real renderer (`drip`, `wording.meeting_today_post` / `meeting_prep_post`), and a real
bot behind a fake Discord with a throwaway database for the plan, the news lookup (`bot._meeting_news`), the send,
the touch record, the reply that asks for a reminder (`bot._maybe_meeting_reminder`) and the one reminder sender
(`bot._fire_due_reminders`). The news is rows put into the real `news_feed_items` table the way the feed poller
stores them. No network, no model, no real sheet.

Every R8 setting is pinned to its .env.example default, never read from this machine's .env.

THE PEOPLE ARE THE TICKET'S OWN EXAMPLES (Ada Lovelace, Sujith P, Dr. David Gamez) or made up; every headline and
link is made up (the links are on news.example). The weekday in front of a date is the real one for 2026.
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
import feeds  # noqa: E402
import links  # noqa: E402
import meeting_prep  # noqa: E402
import news  # noqa: E402
import nextaction  # noqa: E402
import rules as rules_mod  # noqa: E402
import rule13_fixtures as fx  # noqa: E402
import wording  # noqa: E402

MON = date(2026, 10, 12)                       # a Monday
TODAY_OPENER = "we have a meeting today with:"
TODAY_CLOSE = ("Are we all set on the deck, the package and the demo? Let me know if you need anything from me, "
               "or if you'd like another nudge before it starts.")
PREP_CLOSE = ("Are we all set on the deck, the package and the demo? Let me know if you need anything from me, "
              "or if you'd like me to remind you again nearer the day.")
START_CLOSE = ("Good time to start on the deck, the package and the demo. Let me know if you need anything from "
               "me, or if you'd like me to remind you again nearer the day.")
NEWS_TODAY = "   They've been in the news recently — worth a skim before you go in:"
NEWS_PREP = "   They've been in the news recently — worth a skim before the call:"
_ABSENT = object()


def _example(name: str) -> str:
    for line in open(os.path.join(ROOT, ".env.example"), encoding="utf-8"):
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip()
    raise AssertionError(f"{name} is not in .env.example")


def pins() -> dict:
    return {"MEETING_PREP_DAYS_BEFORE": [x.strip() for x in _example("MEETING_PREP_DAYS_BEFORE").split(",")],
            "MEETING_DAYOF_TIME": _example("MEETING_DAYOF_TIME"),
            "MEETING_PREP_TIME": _example("MEETING_PREP_TIME"),
            "REMINDER_DEFAULT_TIME": _example("REMINDER_DEFAULT_TIME"),
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

def day(n, base=MON) -> date:
    return base + timedelta(days=n)


def meet(n, name, company, when, **cells) -> list:
    """A row with a Meeting Date and NOTHING ELSE: no first contact, no connection, no status."""
    return fx.values_row(n, name=name, company=company, meeting_date=when, **cells)


ADA = ("Ada Lovelace", "Acme AI")
SUJITH = ("Sujith P", "ARTPARK")
DAVID = ("Dr. David Gamez", "Middlesex University London")


def rows_of(values, headers=None) -> list:
    return list(fx.parse(values, headers).rows) if values else []


def engine(values, today=MON, state=None, headers=None):
    return nextaction.run(today=today, rows=[], meeting_rows=rows_of(values, headers), meeting_state=state,
                          day_rules=[rules_mod.by_id("R8")])


def r8(result) -> list:
    return sorted((a for a in result["actions"] if a.get("rule_id") == "R8"), key=lambda a: a["pick_order"])


def touches(result) -> list:
    return [(a["poc"], a["touch"]) for a in r8(result)]


def messages_of(result, today=MON) -> dict:
    """{"today": message, "prep": message} for whichever of R8's two posts the day has."""
    return {drip.meeting_post(m): m for m in drip.group(result["actions"], day=today)
            if m.get("type") == nextaction.R_MEETING_PREP}


RENDERED: list = []


def text_of(message) -> str:
    text = drip.compose_fallback(message) if message else ""
    if text:
        RENDERED.append(text)
    return text


def posts(values, today=MON, state=None) -> dict:
    return {k: text_of(m) for k, m in messages_of(engine(values, today, state), today).items()}


class Memory:
    """R8's memory as `db.meeting_prep_state()` returns it, kept in a dict: what the sender would have recorded."""

    def __init__(self):
        self.state = {"touches": {}, "seen": {}}

    def live_day(self, values, today, *, send=True):
        """One day of the live sweep: read, note the dates, post, record. The (name, touch) pairs posted."""
        result = engine(values, today, self.state)
        for key, iso in result["reports"]["R8"]["dates"]:
            self.state["seen"].setdefault(key, {}).setdefault(iso, dl.iso(today))
        if send:
            for a in r8(result):
                self.state["touches"].setdefault((a["row_key"], a["meeting_date"]), {})[a["touch"]] = dl.iso(today)
        return touches(result)


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


def story(title, company, *, seen, importance=3, url=None, ref=None) -> dict:
    """A row of the news store, as the poller writes one for a company search."""
    url = url if url is not None else "https://news.example/" + re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return {"url": url, "url_key": news.url_key(url) or url, "headline_key": news.headline_key(title),
            "title": title, "summary": "", "source": "Example Wire", "published_at": feeds.utc_iso(seen),
            "topic_hint": "", "seen_at": feeds.utc_iso(seen), "kind": "poc",
            "sheet_ref": (f"{company} — on Outreach PoCs" if ref is None else ref), "_importance": importance}


def seed(w, stories) -> None:
    w.bot.db.news_feed_add([{k: v for k, v in s.items() if not k.startswith("_")} for s in stories])
    w.bot.db.news_feed_set_scores(
        [{"url_key": s["url_key"], "importance": s["_importance"], "topic": "funding", "what": ""} for s in stories],
        scored_at=feeds.utc_iso(dl.real_now_ist()))


# -- the checks -----------------------------------------------------------------------------------------------------------

def who_is_picked(check):
    print("\nWHAT IT READS — one column, on every row")
    bare = meet(1, *ADA, day(3))
    check("a row with ONLY a Meeting Date qualifies: no first contact, no connection, no Meeting Status",
          (touches(engine([bare])), [k for k in ("first_contact", "li_connected_date", "meeting_status")
                                     if rows_of([bare])[0].get(k)]), ([("Ada Lovelace", "3")], []))
    check("...and it is handed to the rule though it is not an active row",
          len(nextaction.run(today=MON, rows=[], meeting_rows=rows_of([bare]),
                             day_rules=[rules_mod.by_id("R8")])["actions"]), 1)
    check("a Completed meeting with a date still to come is STILL chased (accepted 12 Oct)",
          touches(engine([meet(1, *ADA, day(3), meeting_status="Completed", deal="Lost", prospect="Lost")])),
          [("Ada Lovelace", "3")])
    check("a past date is never posted",
          touches(engine([meet(1, *ADA, day(-1)), meet(2, *SUJITH, day(-30))])), [])
    check("7, 6, 4, 2 and 1 days out: nothing is said that day",
          touches(engine([meet(i, f"Person {i}", f"Company {i}", day(i)) for i in (7, 6, 4, 2, 1)])), [])
    check("beyond the 7-day window: not looked at", touches(engine([meet(1, *ADA, day(8))])), [])
    check("5 days, 3 days and the day itself each produce their touch, the closest first",
          touches(engine([meet(1, *SUJITH, day(5)), meet(2, *DAVID, day(3)), meet(3, *ADA, day(0))])),
          [("Ada Lovelace", "0"), ("Dr. David Gamez", "3"), ("Sujith P", "5")])
    with tapped() as tap:
        blank = engine([meet(1, "", "Nameless Co", day(3)), meet(2, *ADA, day(3))])
    check("a Meeting Date with a blank Name is skipped and logged",
          (touches(blank), [lvl for lvl, m in tap.lines if "[R8]" in m and "no Name" in m]),
          ([("Ada Lovelace", "3")], ["WARNING"]))
    with tapped() as tap:
        twice = engine([meet(1, *ADA, day(3)), meet(2, *ADA, day(3)), meet(3, *ADA, day(5))])
    check("a true duplicate (same person, same date) is one line and is logged; the same person on two dates is "
          "two lines",
          (touches(twice), len([m for _l, m in tap.lines if "is on the sheet twice" in m])),
          ([("Ada Lovelace", "3"), ("Ada Lovelace", "5")], 1))
    moved = fx.HEADERS_PRE_7OCT
    old_row = [""] * len(moved)
    old_row[moved.index("Name")], old_row[moved.index("Company/Uni")] = ADA
    old_row[moved.index("Meeting Date")] = fx.cell(day(3))
    check("the column is found by its HEADER wherever it sits (the pre-7 Oct layout has it somewhere else)",
          (moved.index("Meeting Date") != fx.HEADERS_7OCT.index("Meeting Date"),
           touches(engine([old_row], headers=moved))), (True, [("Ada Lovelace", "3")]))
    with tapped() as tap:
        none = nextaction.run(today=MON, rows=[], meeting_rows=[{"_row": 2, "name": "Ada", "company": "Acme"}],
                              day_rules=[rules_mod.by_id("R8")])
    check("no Meeting Date column at all: no post, logged at ERROR",
          (none["actions"], [lvl for lvl, m in tap.lines if "[R8]" in m and "no Meeting Date column" in m]),
          ([], ["ERROR"]))
    with tapped() as tap:
        lost = engine([meet(1, *ADA, day(0))], state={"unreadable": True})
    check("the record of what has gone out cannot be read: nothing is posted (no touch can repeat)",
          (lost["actions"], [lvl for lvl, m in tap.lines if "[R8]" in m and "cannot be read" in m]), ([], ["ERROR"]))
    # THE SAME PERSON UNDER AN EARLIER-LISTED RULE (R7: DM'd six weeks ago, the meeting booked long after it).
    shared = fx.person(1, name=ADA[0], company=ADA[1], first_contact="TRUE", li_date=day(-60), dm_sent="Sent",
                       dm_date=day(-42), meeting_date=day(3))
    rows = rows_of([shared])
    both = nextaction.run(today=MON, rows=rows, prospect_rows=rows, meeting_rows=rows, dm_no_meeting_state={},
                          next_step_state={}, day_rules=[rules_mod.by_id("R7"), rules_mod.by_id("R8")])
    check("an earlier-listed rule naming the same person does NOT drop the meeting (it used to)",
          (sorted(a["rule_id"] for a in both["actions"]), [d["rule_id"] for d in both["deduped"]]),
          (["R7", "R8"], []))
    rule = rules_mod.by_id("R8")
    check("R8: anchored to the meeting, in the channel, outside the day's count and the day's order",
          (rule.weekdays, rule.anchored, rule.destination, rule.counts_toward_cap, rules_mod.is_fixed_time(rule),
           nextaction.R_MEETING_PREP in drip.NEVER_COUNTED, nextaction.R_MEETING_PREP in drip.NEVER_HELD),
          ((), True, "channel", False, True, True, True))
    check("no web search is asked for: the item is not web-pending",
          [a["web_pending"] for a in r8(engine([meet(1, *ADA, day(3))]))], [False])


def the_message(check):
    print("\nTHE MESSAGE — the four shapes, word for word")
    got = posts([meet(1, *ADA, MON), meet(2, *SUJITH, MON)])
    check("meeting today: the opener, numbered names with NO date, a blank line, the close",
          got, {"today": "we have two meetings today with:\n1. Ada Lovelace (Acme AI)\n2. Sujith P (ARTPARK)\n\n"
                         + TODAY_CLOSE})
    solo = posts([meet(1, *ADA, MON)])
    check("a list of ONE is the same shape, and reads right in the singular",
          solo, {"today": TODAY_OPENER + "\n1. Ada Lovelace (Acme AI)\n\n" + TODAY_CLOSE})
    three = posts([meet(1, *DAVID, day(3))])
    check("three days away: the date on the line, the 'check' close",
          three, {"prep": "we have a meeting in 3 days with:\n1. Dr. David Gamez (Middlesex University London) — "
                          "Thu 15 Oct\n\n" + PREP_CLOSE})
    five = posts([meet(1, *SUJITH, day(5))])
    check("five days away: the 'start' close",
          five, {"prep": "we have a meeting in 5 days with:\n1. Sujith P (ARTPARK) — Sat 17 Oct\n\n" + START_CLOSE})
    both = posts([meet(1, *SUJITH, day(5)), meet(2, *DAVID, day(3))])
    check("both on one day: ONE post, the 3-day block first, numbered straight through, the 'check' close",
          both, {"prep": "we have a meeting in 3 days with:\n1. Dr. David Gamez (Middlesex University London) — "
                         "Thu 15 Oct\n\nand a meeting in 5 days with:\n2. Sujith P (ARTPARK) — Sat 17 Oct\n\n"
                         + PREP_CLOSE})
    plural = posts([meet(1, *SUJITH, day(5)), meet(2, "Bo Chen", "Borealis", day(5)), meet(3, *DAVID, day(3)),
                    meet(4, "Cy Dee", "Cinder", day(3)), meet(5, "Eve Fay", "Echo", day(3))])["prep"].split("\n")
    check("singular and plural both read right: 'three meetings in 3 days', 'two meetings in 5 days'",
          [ln for ln in plural if ln.endswith("with:")],
          ["we have three meetings in 3 days with:", "and two meetings in 5 days with:"])
    check("...and nothing anywhere says 'meeting(s)' or '1 meetings'",
          [t for t in RENDERED if "(s)" in t or re.search(r"\b1 meetings\b|\b1 days\b", t)], [])
    two_posts = posts([meet(1, *ADA, MON), meet(2, *DAVID, day(3)), meet(3, *SUJITH, day(5))])
    check("a meeting today AND a 3/5-day touch: TWO posts, today's never folded in with the others",
          (sorted(two_posts), "today with" in two_posts["prep"], "in 3 days" in two_posts["today"]),
          (["prep", "today"], False, False))
    msgs = messages_of(engine([meet(1, *ADA, MON), meet(2, *DAVID, day(3))]))
    check("the two headings, put on by the sender and not part of the block",
          ([drip.heading_for(msgs[k], day=MON) for k in ("today", "prep")],
           [h for h in ("Meeting today", "Meeting preparation") if any(h in t for t in RENDERED)]),
          (["**Meeting today**", "**Meeting preparation**"], []))
    check("today's post at MEETING_DAYOF_TIME and the afternoon one at MEETING_PREP_TIME",
          {k: m["dayof_time"] for k, m in msgs.items()}, {"today": "10:00", "prep": "14:00"})
    m = msgs["prep"]
    check("rendered in code: R8 is verbatim, has no composed template, and never reaches the generic tail",
          (nextaction.R_MEETING_PREP in drip.VERBATIM_TYPES, nextaction.R_MEETING_PREP in drip._ASK,
           drip.is_verbatim(m), drip.points_of(m)["header"], drip.points_of(m)["close"]),
          (True, False, True, "we have a meeting in 3 days with:", PREP_CLOSE))
    thirty = [meet(i, f"Person Number {i:02d}", f"A Rather Long Company Name For A Research Institute Limited {i:02d}", MON)
              for i in range(1, 31)]
    big = messages_of(engine(thirty))["today"]
    text = drip.compose_fallback(big)
    parts = drip.split_on_lines(text, limit=1900)
    check("30 meetings: NO CAP, every one is named, and the post is split between lines, never truncated",
          (len(drip.shown_contacts(big)), [i for i in range(1, 31) if f"Person Number {i:02d} (" not in text],
           len(text) > 2000, len(parts) > 1, max(len(p) for p in parts) <= 1900, "\n".join(parts) == text,
           big["overflow"]),
          (30, [], True, True, True, True, 0))
    check("...opened with the count as a number once it is past ten", text.split("\n")[0],
          "we have 30 meetings today with:")
    check("every fixed line is in the register check",
          [l for l in (TODAY_OPENER, TODAY_CLOSE, PREP_CLOSE, START_CLOSE, NEWS_TODAY, NEWS_PREP,
                       "we have a meeting in 3 days with:", "and two meetings in 5 days with:")
           if l not in wording.all_lines()], [])


def the_memory(check):
    print("\nTHE MEMORY — a touch goes out once, for one date")
    mem = Memory()
    rows = [meet(1, *ADA, day(5))]
    check("the 5-day touch goes out", mem.live_day(rows, MON), [("Ada Lovelace", "5")])
    check("...and not again on the next run that day (a restart)", mem.live_day(rows, MON), [])
    check("the bot down all day on the 3-day touch's day: that touch is gone, not caught up",
          (mem.live_day(rows, day(3)), mem.live_day(rows, day(4))), ([], []))
    check("...and the day itself still gets its post", mem.live_day(rows, day(5)), [("Ada Lovelace", "0")])

    mem = Memory()
    first = [meet(1, *ADA, date(2026, 10, 15))]
    moved = [meet(1, *ADA, date(2026, 10, 17))]
    check("A RESCHEDULED MEETING: 15 Oct gets its 5-day touch on the 10th",
          mem.live_day(first, date(2026, 10, 10)), [("Ada Lovelace", "5")])
    check("...moved to 17 Oct on the 11th: 6 days out, nothing", mem.live_day(moved, date(2026, 10, 11)), [])
    check("...the NEW date gets its own fresh 5-day touch on the 12th, once",
          (mem.live_day(moved, date(2026, 10, 12)), mem.live_day(moved, date(2026, 10, 12))),
          ([("Ada Lovelace", "5")], []))
    check("...moved BACK to the 15th: its 5-day touch is not repeated; what is due is its 3-day one, once",
          (mem.live_day(first, date(2026, 10, 12)), mem.live_day(first, date(2026, 10, 12))),
          ([("Ada Lovelace", "3")], []))

    mem = Memory()
    far = [meet(1, *ADA, day(10))]
    near = [meet(1, *ADA, day(2))]
    mem.live_day(far, day(-2))
    check("A DATE MOVED SHARPLY CLOSER (10 days out to 2): exactly one catch-up touch",
          (mem.live_day(near, MON), mem.live_day(near, MON), mem.live_day(near, day(1))),
          ([("Ada Lovelace", "catchup")], [], []))
    check("...and the day itself is still its own touch", mem.live_day(near, day(2)), [("Ada Lovelace", "0")])
    key = engine(near, MON)["reports"]["R8"]["dates"][0][0]
    catchup = posts(near, MON, {"touches": {}, "seen": {
        key: {dl.iso(day(10)): dl.iso(day(-2)), dl.iso(day(2)): dl.iso(MON)}}})
    check("the catch-up says the real number of days, and checks rather than starts",
          catchup, {"prep": "we have a meeting in 2 days with:\n1. Ada Lovelace (Acme AI) — Wed 14 Oct\n\n"
                            + PREP_CLOSE})
    mem = Memory()
    check("a meeting newly BOOKED 2 days out (never moved) says nothing until its day",
          (mem.live_day(near, MON), mem.live_day(near, day(1)), mem.live_day(near, day(2))),
          ([], [], [("Ada Lovelace", "0")]))

    mem = Memory()
    odd = [meet(1, *ADA, "sometime next week"), meet(2, *DAVID, day(3))]
    check("A DATE THAT CANNOT BE READ is still named, in the afternoon post, after the blocks",
          (mem.live_day(odd, MON, send=False), posts(odd, MON)["prep"].split("\n")[-3:]),
          ([("Dr. David Gamez", "3"), ("Ada Lovelace", "unreadable")],
           ["2. Ada Lovelace (Acme AI) — I couldn't read the Meeting Date on the sheet. Could you put the day in?",
            "", PREP_CLOSE]))
    alone = [meet(1, *ADA, "sometime next week")]
    check("...on its own it is the whole post, with no close about a meeting day nobody knows",
          posts(alone, MON), {"prep": "1. Ada Lovelace (Acme AI) — I couldn't read the Meeting Date on the sheet. "
                                      "Could you put the day in?"})
    asked = [mem.live_day(alone, day(n)) for n in range(0, 15)]
    check("...and it is asked ONCE A WEEK, not every day (days 0, 7 and 14 of fifteen)",
          [n for n, got in enumerate(asked) if got], [0, 7, 14])
    late = Memory()
    late.live_day([meet(1, *ADA, MON)], MON)
    check("A MEETING DATE TYPED IN LATE (for today, after today's post has gone) is posted at the next run, once",
          (late.live_day([meet(1, *ADA, MON), meet(2, *SUJITH, MON)], MON),
           late.live_day([meet(1, *ADA, MON), meet(2, *SUJITH, MON)], MON)), ([("Sujith P", "0")], []))
    late2 = Memory()
    late2.live_day([meet(1, *ADA, MON)], MON)
    again = messages_of(engine([meet(1, *ADA, MON), meet(2, *SUJITH, MON)], MON, late2.state))
    check("...its key says it is the late one", [m["group_key"] for m in again.values()],
          ["meeting_prep|today|late1"])
    sat = date(2026, 10, 17)
    check("WEEKENDS: a Saturday meeting is picked on the Saturday, and its 3-day touch on the Wednesday",
          (touches(engine([meet(1, *ADA, sat)], sat)), touches(engine([meet(1, *ADA, sat)], day(2)))),
          ([("Ada Lovelace", "0")], [("Ada Lovelace", "3")]))


def the_news(check):
    import replies_world as rw

    print("\nTHE NEWS LINE — read from the stored news of the last 14 days; nothing is fetched")
    now = dl.real_now_ist()
    today = dl.real_today_ist()

    async def post_with(stories, values, *, which="prep", break_lookup=False):
        with rw.World(False, pins=dict(pins())) as w:
            seed(w, stories)
            msg = messages_of(engine(values, today), today)[which]
            calls = w.counts()
            if break_lookup:
                def boom(*_a, **_k):
                    raise RuntimeError("the news store is locked")
                w.bot.db.news_feed_seen_after = boom
            with tapped() as tap:
                await w.bot._research_message(msg, today=today)
            text = drip.compose_fallback(msg)
            RENDERED.append(text)
            after = w.counts()
            return {"text": text, "lines": text.split("\n"),
                    "spent": (after["model"] - calls["model"], after.get("searches", 0) - calls.get("searches", 0)),
                    "pending": msg.get("web_pending"), "log": tap.lines}

    def run(*a, **k):
        with pinned():
            return asyncio.run(post_with(*a, **k))

    def at(n):
        return today + timedelta(days=n)

    recent = now - timedelta(days=2)
    raised = story("Acme AI raises a seed round", "Acme AI", seen=recent)
    link = "[Acme AI raises a seed round](<https://news.example/acme-ai-raises-a-seed-round>)"
    got = run([raised], [meet(1, *ADA, at(3))])
    check("a story in the last 14 days: the sentence ends on a colon and the headline is on the next line, both "
          "indented under the name",
          got["lines"][1:4], [f"1. Ada Lovelace (Acme AI) — {wording.day_date(dl.iso(at(3)))}", NEWS_PREP,
                              "   " + link])
    check("...the link text is THE HEADLINE, masked so there is no embed, with no date and no bare url",
          (link in got["text"], links.bare_urls(got["text"]), re.findall(r"\d{1,2} (?:Jan|Feb|Mar|Apr|May|Jun|Jul|"
                                                                      r"Aug|Sep|Oct|Nov|Dec)", got["lines"][3])),
          (True, [], []))
    check("...it fetched nothing: no search, no model call, and the post is not waiting on the web",
          (got["spent"], got["pending"]), ((0, 0), False))
    got = run([raised], [meet(1, *ADA, today)], which="today")
    check("on the day's post the sentence says 'before you go in'",
          got["lines"][1:4], ["1. Ada Lovelace (Acme AI)", NEWS_TODAY, "   " + link])
    got = run([], [meet(1, *ADA, at(3))])
    check("no story: no line at all, and nothing says 'no news'",
          ([ln for ln in got["lines"] if "news" in ln.lower()], len(got["lines"])), ([], 4))
    old = story("Acme AI opens an office", "Acme AI", seen=now - timedelta(days=15))
    check("a story older than 14 days is not shown",
          [ln for ln in run([old], [meet(1, *ADA, at(3))])["lines"] if "news" in ln.lower()], [])
    long_title = "Acme AI " + "announces a very long and winding headline about its new platform " * 3
    got = run([story(long_title, "Acme AI", seen=recent)], [meet(1, *ADA, at(3))])
    shown = re.search(r"\[([^\]]+)\]\(<", got["lines"][3]).group(1)
    check("a very long headline is trimmed to about 100 characters with the ellipsis INSIDE the link text",
          (len(long_title.strip()) > 150, len(shown) <= 101, shown.endswith("…"), shown.startswith("Acme AI announces"),
           got["lines"][3].endswith(">)")), (True, True, True, True, True))
    bad = story("Acme AI signs a partner", "Acme AI", seen=recent, url="//news.google.com/rss/articles/abc")
    got = run([bad], [meet(1, *ADA, at(3))])
    check("a link with no https://: the line is skipped, and that is logged",
          ([ln for ln in got["lines"] if "news" in ln.lower()],
           [lvl for lvl, m in got["log"] if "R8" in m and "no http(s) scheme" in m]), ([], ["WARNING"]))
    other = story("Nudge Security closes a round", "Nudge Security", seen=recent)
    got = run([other], [meet(1, "Nia Day", "Nudge", at(3))])
    check("a story not about them: the FULL name is matched, never a substring (Nudge Security is not Nudge)",
          [ln for ln in got["lines"] if "news" in ln.lower()], [])
    minor = story("Acme AI hires a designer", "Acme AI", seen=now - timedelta(hours=3), importance=2)
    major = story("Acme AI is acquired", "Acme AI", seen=now - timedelta(days=5), importance=5)
    got = run([minor, major, raised], [meet(1, *ADA, at(3))])
    check("ONE story a company: the most important by R1's ranking, not simply the newest",
          [ln.strip() for ln in got["lines"] if ln.startswith("   [")],
          ["[Acme AI is acquired](<https://news.example/acme-ai-is-acquired>)"])
    got = run([raised], [meet(1, *ADA, at(3)), meet(2, *DAVID, at(3)), meet(3, "Sam Lee", "Acme AI", at(3))])
    check("TWO PEOPLE AT ONE COMPANY are numbered one after the other, with ONE news line AFTER BOTH",
          [ln.split(" — ")[0] for ln in got["lines"][1:6]],
          ["1. Ada Lovelace (Acme AI)", "2. Sam Lee (Acme AI)", NEWS_PREP.split(" — ")[0], "   " + link,
           "3. Dr. David Gamez (Middlesex University London)"])
    got = run([raised], [meet(1, *ADA, at(3)), meet(2, "Sam Lee", "Acme AI", at(5))])
    check("THE SAME COMPANY IN BOTH BLOCKS: the news line appears once in the post, after the first of them",
          (got["text"].count(link), got["text"].count("been in the news"),
           got["lines"].index("   " + link) < got["lines"].index("and a meeting in 5 days with:")), (1, 1, True))
    again = run([raised], [meet(1, *ADA, at(3))])["text"].count(link) + run(
        [raised], [meet(1, *ADA, at(5))])["text"].count(link)
    check("A REPEATED STORY is shown again: still the best at the 3-day touch as it was at the 5-day one",
          again, 2)
    got = run([raised], [meet(1, *ADA, at(3))], break_lookup=True)
    check("the lookup fails: the post goes out WITHOUT the line, and the failure is logged",
          (got["lines"][0], [ln for ln in got["lines"] if "news" in ln.lower()],
           [lvl for lvl, m in got["log"] if "R8's news lookup failed" in m]),
          ("we have a meeting in 3 days with:", [], ["ERROR"]))
    check("every link in every rendered post carries a scheme, and nothing is a bare url",
          ([u for t in RENDERED for u in re.findall(r"\]\(([^)]*)\)", t) if not re.match(r"^<https?://[^\s>]+>$", u)],
           [t for t in RENDERED if links.bare_urls(t)]), ([], []))
    check("feeds: a protocol-relative feed url gets its scheme, and anything else without one is left out",
          (feeds.feed_url("//news.google.com/rss/search?q=evals"), feeds.feed_url("news.google.com/rss")),
          ("https://news.google.com/rss/search?q=evals", ""))


async def _tick(w, today, hh, mm) -> list:
    """One tick of the live sweep at a pretend minute: plan, then send what is due. [(heading, body, slot time)]."""
    import replies_world as rw

    w.pretend_day(today, hh, mm)
    now = datetime.combine(today, time(hh, mm), dl.IST)
    marker = dl.iso(today)
    already = w.bot.db.drip_sent_today(marker)
    planned = await w.bot._plan_drip(today=today, already=already)
    out = []
    for msg in [m for m in (planned or {}).get("messages") or [] if m["send_at"] <= now]:
        n = len(w.posted)
        await w.bot._research_message(msg, today=today)
        await w.bot._send_drip_message(w.chan, msg, marker=marker, channel_id=w.chan.id)
        parts = [rw.strip_tag(p.content) for p in w.posted[n:]]
        if parts:
            lines = "\n".join(parts).split("\n")
            out.append((lines[0], "\n".join(l for l in lines[1:] if not l.startswith("<@")), msg["send_at_hhmm"]))
    return out


def the_day(check):
    import replies_world as rw

    print("\nTHE DAY — two posts, at their own times, through the real planner and sender")
    sat = date(2026, 10, 17)
    sun = date(2026, 10, 18)

    async def scenario(test_mode=False):
        out = {}
        with rw.World(test_mode, pretend=(MON, 9, 0), rules={"R8"}, pins=dict(pins())) as w:
            w.sheet.set(pocs=[meet(1, *ADA, MON), meet(2, *DAVID, day(3)), meet(3, *SUJITH, day(5)),
                              meet(4, "Far Off", "Later Co", day(6))])
            model = w.counts()["model"]
            out["early"] = await _tick(w, MON, 9, 45)
            out["ten"] = await _tick(w, MON, 10, 0)
            out["ten_again"] = await _tick(w, MON, 10, 15)
            out["two"] = await _tick(w, MON, 14, 0)
            out["two_again"] = await _tick(w, MON, 14, 15)
            out["tagged"] = [p.content.startswith("[TEST") for p in w.posted]
            out["model"] = w.counts()["model"] - model
            rows = w.bot.db.drip_sent_today(dl.iso(MON))
            out["rows"] = [(r["action_type"], r["planned_at"], bool(r["counts_toward_cap"]), bool(r["pinned"]))
                           for r in rows]
            out["counted"] = drip.counted_today(rows)
            with w.bot.db.conn() as c:
                out["touches"] = sorted((r["touch"], r["sent_on"]) for r in c.execute(
                    "SELECT touch, sent_on FROM meeting_touches"))
            # A MEETING DATE TYPED IN AT 4 PM, for today and for three days out.
            w.sheet.set(pocs=[meet(1, *ADA, MON), meet(2, *DAVID, day(3)), meet(3, *SUJITH, day(5)),
                              meet(4, "Late Today", "Tardy Co", MON), meet(5, "Late Three", "Tardy Co", day(3))])
            out["late"] = await _tick(w, MON, 16, 0)
            out["late_again"] = await _tick(w, MON, 16, 15)
            out["writes"] = len(w.sheet.writes)
        return out

    async def restart():
        with rw.World(False, pretend=(MON, 12, 30), rules={"R8"}, pins=dict(pins())) as w:
            w.sheet.set(pocs=[meet(1, *ADA, MON)])
            first = await _tick(w, MON, 12, 30)                 # the bot was down at 10:00
            return first, await _tick(w, MON, 12, 45)

    async def weekend():
        out = {}
        with rw.World(False, pretend=(sat, 9, 0), rules={"R8"}, pins=dict(pins())) as w:
            w.sheet.set(pocs=[meet(1, *ADA, sat), meet(2, *SUJITH, sat + timedelta(days=4))])
            out["sat"] = await _tick(w, sat, 10, 0) + await _tick(w, sat, 14, 0)
            out["sun"] = await _tick(w, sun, 10, 0) + await _tick(w, sun, 14, 0)
            out["plans"] = (drip.is_sending_day(sat), drip.plans_on(sat))
        return out

    async def quiet():
        with rw.World(False, pretend=(MON, 9, 0), rules={"R8"}, pins=dict(pins())) as w:
            w.sheet.set(pocs=[meet(1, *ADA, day(4)), meet(2, *SUJITH, day(-2)), meet(3, "No Date", "Blank Co", "")])
            return await _tick(w, MON, 10, 0) + await _tick(w, MON, 14, 0), len(w.posted)

    with pinned():
        live = asyncio.run(scenario(False))
        test = asyncio.run(scenario(True))
        down = asyncio.run(restart())
        wknd = asyncio.run(weekend())
        silent = asyncio.run(quiet())
    check("before 10:00 nothing goes out", live["early"], [])
    check("at 10:00: 'Meeting today', today's meetings ONLY, on its own",
          live["ten"], [("**Meeting today**", TODAY_OPENER + "\n1. Ada Lovelace (Acme AI)\n\n" + TODAY_CLOSE,
                         "10:00")])
    check("after 14:00: 'Meeting preparation', the 3-day block then the 5-day block, in ONE post",
          live["two"], [("**Meeting preparation**",
                         "we have a meeting in 3 days with:\n1. Dr. David Gamez (Middlesex University London) — "
                         "Thu 15 Oct\n\nand a meeting in 5 days with:\n2. Sujith P (ARTPARK) — Sat 17 Oct\n\n"
                         + PREP_CLOSE, "14:00")])
    check("neither is said twice on the ticks that follow", (live["ten_again"], live["two_again"]), ([], []))
    check("both are fixed-time posts outside DAILY_MESSAGE_CAP, and neither is composed by the model",
          (live["rows"], live["counted"], live["model"]),
          ([("meeting_prep", "10:00", False, True), ("meeting_prep", "14:00", False, True)], 0, 0))
    check("each touch is recorded against its meeting, once",
          live["touches"], [("0", dl.iso(MON)), ("3", dl.iso(MON)), ("5", dl.iso(MON))])
    check("a Meeting Date typed in at 4 PM (for today, and for 3 days out) is POSTED LATE, once, each in its "
          "own post",
          ([(h, b.split("\n")[1]) for h, b, _t in live["late"]], live["late_again"]),
          ([("**Meeting today**", "1. Late Today (Tardy Co)"),
            ("**Meeting preparation**", "1. Late Three (Tardy Co) — Thu 15 Oct")], []))
    check("nothing is written to the sheet", live["writes"], 0)
    check("test mode is identical apart from the [TEST] tag",
          (test["ten"], test["two"], test["late"], test["touches"], set(live["tagged"]), set(test["tagged"])),
          (live["ten"], live["two"], live["late"], live["touches"], {False}, {True}))
    check("the bot down at 10:00: today's post still goes out ONCE when it is back",
          ([(h, t) for h, _b, t in down[0]], down[1]), ([("**Meeting today**", "10:00")], []))
    check("WEEKENDS: a Saturday meeting gets its post on the Saturday, though the drip does not send that day",
          ([(h, b.split("\n")[1]) for h, b, _t in wknd["sat"]], wknd["plans"]),
          ([("**Meeting today**", "1. Ada Lovelace (Acme AI)")], (False, True)))
    check("...and a 3-day touch that falls on a Sunday goes out on the Sunday",
          [(h, b.split("\n")[1]) for h, b, _t in wknd["sun"]],
          [("**Meeting preparation**", "1. Sujith P (ARTPARK) — Wed 21 Oct")])
    check("nothing qualifies (4 days out, a past date, no date): no post, and no 'no meetings today' line",
          silent, ([], 0))
    with pinned():
        items = [dict(a) for a in r8(engine([meet(1, *ADA, MON), meet(2, *DAVID, day(3))]))]
        full = [{"slot": i, "group_key": f"x|{i}", "action_type": nextaction.R_AI_NEWS, "counts_toward_cap": 1}
                for i in range(1, config.DAILY_MESSAGE_CAP + 1)]
        planned = drip.plan(items, day=MON, already_sent=full)
    check("with the day's cap already full, both R8 posts still go, at their own times",
          [(m["send_at_hhmm"], m["pinned"], m["counts_toward_cap"]) for m in planned["messages"]],
          [("10:00", True, False), ("14:00", True, False)])


def the_reminder(check):
    import replies_world as rw

    print("\nTHE REMINDER OFFER — set from a reply, re-anchored when the meeting moves, never after it")

    def reminders(w) -> list:
        with w.bot.db.conn() as c:
            return [tuple(r) for r in c.execute(
                "SELECT poc, due_date, due_time, status, what FROM scheduled_reminders ORDER BY id").fetchall()]

    async def scenario(test_mode=False):
        out = {}
        with rw.World(test_mode, pretend=(MON, 9, 0), rules={"R8"}, pins=dict(pins())) as w:
            w.sheet.set(pocs=[meet(1, *DAVID, day(3)), meet(2, *SUJITH, day(5)), meet(3, "Bo Chen", "Borealis", day(5))])
            await _tick(w, MON, 14, 0)
            post = w.posted[-1]
            w.pretend_day(MON, 14, 30)

            async def say(text, who="member", to=post):
                before = len(w.posted)
                await w.say(text, who=who, reply_to=to)
                return [rw.strip_tag(m.content) for m in w.posted[before:]]

            out["people"] = [(p["n"], p["poc"], p["meeting_date"])
                             for p in w.bot.db.find_drip_by_message_id(str(post.id))["people"]]
            out["by_number"] = await say("remind me about 2 the day before")
            out["after_number"] = reminders(w)
            out["by_name"] = await say("yes please, nudge me on David on Wednesday at 9am")
            out["after_name"] = reminders(w)[1:]
            out["replace"] = await say("actually remind me about Sujith on Thursday instead")
            out["after_replace"] = [(r[0], r[1], r[3]) for r in reminders(w)]
            out["which"] = await say("remind me tomorrow")
            out["when"] = await say("remind me about Bo")
            out["after_meeting"] = await say("remind me about David on 20 Oct")
            out["not_on_post"] = await say("remind me about Ada tomorrow")
            out["plain_yes"] = await say("yes all set")
            out["count"] = len(reminders(w))

            # THE MEETING MOVES: Sujith's from the 17th to the 20th. His reminder was for Thu 15 Oct, two days
            # before; it is re-anchored to two days before the new date, and that is said once.
            w.sheet.set(pocs=[meet(1, *DAVID, day(3)), meet(2, *SUJITH, day(8)), meet(3, "Bo Chen", "Borealis", day(5))])
            n = len(w.posted)
            moved = await w.bot._reanchor_meeting_reminders(now=datetime.combine(day(1), time(11, 0), dl.IST),
                                                            channel=w.chan)
            out["moved"] = (moved, [rw.strip_tag(m.content).split("\n")[-1] for m in w.posted[n:]])
            again = await w.bot._reanchor_meeting_reminders(now=datetime.combine(day(1), time(11, 15), dl.IST),
                                                            channel=w.chan)
            out["moved_again"] = (again, len(w.posted) - n)
            out["after_move"] = [(r[0], r[1], r[2], r[3]) for r in reminders(w) if r[0] == "Sujith P"]
            # ON THE OLD DAY NOTHING FIRES for him; David's goes out the ordinary way at its minute.
            n = len(w.posted)
            wed = day(2)
            fired = await w.bot._fire_due_reminders(channel=w.chan, from_test=True,
                                                    at=datetime.combine(wed, time(9, 5), dl.IST))
            out["fired"] = (len(fired), [rw.strip_tag(m.content).split("\n")[-1] for m in w.posted[n:]])
            n = len(w.posted)
            old_day = await w.bot._fire_due_reminders(channel=w.chan, from_test=True,
                                                      at=datetime.combine(day(3), time(14, 5), dl.IST))
            out["old_day"] = (old_day, len(w.posted) - n)
            n = len(w.posted)
            new_day = await w.bot._fire_due_reminders(channel=w.chan, from_test=True,
                                                      at=datetime.combine(day(6), time(14, 5), dl.IST))
            out["new_day"] = (len(new_day), ["Sujith P (ARTPARK) on Tue 20 Oct" in m.content for m in w.posted[n:]])
            out["writes"] = len(w.sheet.writes)
        return out

    async def at_fire_time():
        """The meeting moves and nobody ticks before the reminder's minute: it is caught at the sender."""
        with rw.World(False, pretend=(MON, 9, 0), rules={"R8"}, pins=dict(pins())) as w:
            w.sheet.set(pocs=[meet(1, *DAVID, day(3))])
            await _tick(w, MON, 14, 0)
            w.pretend_day(MON, 14, 30)
            await w.say("remind me the day before", who="member", reply_to=w.posted[-1])
            w.sheet.set(pocs=[meet(1, *DAVID, day(5))])
            n = len(w.posted)
            fired = await w.bot._fire_due_reminders(channel=w.chan, from_test=True,
                                                    at=datetime.combine(day(2), time(14, 5), dl.IST))
            said = [rw.strip_tag(m.content).split("\n")[-1] for m in w.posted[n:]]
            w.sheet.set(pocs=[meet(1, *DAVID, "")])
            n = len(w.posted)
            await w.bot._fire_due_reminders(channel=w.chan, from_test=True,
                                            at=datetime.combine(day(4), time(14, 5), dl.IST))
            return fired, said, [(r[1], r[3]) for r in reminders(w)], len(w.posted) - n

    async def today_post():
        with rw.World(False, pretend=(MON, 9, 0), rules={"R8"}, pins=dict(pins())) as w:
            w.sheet.set(pocs=[meet(1, *ADA, MON)])
            await _tick(w, MON, 10, 0)
            post = w.posted[-1]
            w.pretend_day(MON, 11, 0)
            n = len(w.posted)
            await w.say("another nudge at 3pm please", who="member", reply_to=post)
            first = [rw.strip_tag(m.content) for m in w.posted[n:]], reminders(w)
            n = len(w.posted)
            await w.say("nudge me at 9am", who="member", reply_to=post)
            past = [rw.strip_tag(m.content) for m in w.posted[n:]]
            w.pretend_day(day(1), 11, 0)
            n = len(w.posted)
            await w.say("remind me again tomorrow", who="member", reply_to=post)
            return first, past, [rw.strip_tag(m.content) for m in w.posted[n:]], len(reminders(w))

    with pinned():
        live = asyncio.run(scenario(False))
        test = asyncio.run(scenario(True))
        late = asyncio.run(at_fire_time())
        nudge = asyncio.run(today_post())
    check("the post remembers who it named, in its order",
          live["people"], [(1, "Dr. David Gamez", "2026-10-15"), (2, "Sujith P", "2026-10-17"),
                           (3, "Bo Chen", "2026-10-17")])
    check("a reply picks the person by their NUMBER on that post: 'the day before' is counted from that meeting",
          (live["by_number"], live["after_number"]),
          (["I'll remind you about the meeting with Sujith P (ARTPARK) on Fri 16 Oct at 2 PM."],
           [("Sujith P", "2026-10-16", "14:00", "open", "the meeting with Sujith P (ARTPARK) on Sat 17 Oct")]))
    check("...or by name, with the day and the time as people type them",
          (live["by_name"], [(r[0], r[1], r[2], r[3]) for r in live["after_name"]]),
          (["I'll remind you about the meeting with Dr. David Gamez (Middlesex University London) on Wed 14 Oct "
            "at 9 AM."], [("Dr. David Gamez", "2026-10-14", "09:00", "open")]))
    check("one reminder a meeting: a newer one replaces the older",
          live["after_replace"], [("Sujith P", "2026-10-16", "cancelled"), ("Dr. David Gamez", "2026-10-14", "open"),
                                  ("Sujith P", "2026-10-15", "open")])
    check("no meeting named under a post of three: one question, nothing booked",
          live["which"], [wording.MEETING_REMIND_WHICH])
    check("no day or time that can be read: one question, nothing booked", live["when"],
          [wording.MEETING_REMIND_WHEN])
    check("A REMINDER ASKED FOR AFTER THE MEETING: said so, another time asked for, nothing booked",
          live["after_meeting"], ["That's after the meeting with Dr. David Gamez (Middlesex University London) on "
                                  "Thu 15 Oct. Could you give me a time before it?"])
    check("somebody who is NOT on that post cannot be matched", live["not_on_post"], [wording.MEETING_REMIND_WHICH])
    check("a plain 'yes all set' books nothing", live["count"], 3)
    check("A REMINDER SET, THEN THE MEETING MOVES: it is re-anchored to the same distance before the new date, "
          "and that is said once",
          (live["moved"][0], [l.split(" ", 1)[1] for l in live["moved"][1]], live["moved_again"], live["after_move"]),
          (1, ["The meeting with Sujith P (ARTPARK) has moved to Tue 20 Oct, so I've moved your reminder to "
               "Sun 18 Oct at 2 PM."], (0, 1),
           [("Sujith P", "2026-10-16", "14:00", "cancelled"), ("Sujith P", "2026-10-15", "14:00", "cancelled"),
            ("Sujith P", "2026-10-18", "14:00", "open")]))
    check("a reminder whose meeting has not moved fires the ordinary way, from the one sender, at its minute",
          (live["fired"][0], ["you asked me to remind you" in l and "Dr. David Gamez" in l
                              for l in live["fired"][1]]), (1, [True]))
    check("...nothing fires on the OLD day for the meeting that moved, and it fires on the new one",
          (live["old_day"], live["new_day"]), (([], 0), (1, [True])))
    check("nothing here writes to the sheet", live["writes"], 0)
    check("test mode books the same reminders and says the same things",
          (test["after_replace"], test["by_number"], test["after_meeting"], test["after_move"]),
          (live["after_replace"], live["by_number"], live["after_meeting"], live["after_move"]))
    check("a move nobody noticed before the reminder's minute is caught by the sender: re-anchored, not fired on "
          "the old date",
          (late[0], [l.split(" ", 1)[1] for l in late[1]]),
          ([], ["The meeting with Dr. David Gamez (Middlesex University London) has moved to Sat 17 Oct, so I've "
                "moved your reminder to Fri 16 Oct at 2 PM."]))
    check("...and a meeting whose date is then cleared closes its reminder quietly",
          (late[2], late[3]), ([("2026-10-14", "cancelled"), ("2026-10-16", "cancelled")], 0))
    check("'Meeting today': a post of one needs no name, and 'at 3pm' is today",
          nudge[0], (["I'll remind you about the meeting with Ada Lovelace (Acme AI) on Mon 12 Oct at 3 PM."],
                     [("Ada Lovelace", "2026-10-12", "15:00", "open",
                       "the meeting with Ada Lovelace (Acme AI) on Mon 12 Oct")]))
    check("a time already gone is asked about again rather than booked", nudge[1], [wording.MEETING_REMIND_WHEN])
    check("the day after the meeting, a reminder is refused: the meeting has been",
          (nudge[2], nudge[3]), (["That's after the meeting with Ada Lovelace (Acme AI) on Mon 12 Oct. Could you "
                                  "give me a time before it?"], 1))
    src = open(os.path.join(ROOT, "bot.py"), encoding="utf-8").read()
    sender = src[src.index("async def _fire_due_reminders("):src.index("async def _sweep_once(")]
    check("there is ONE reminder sender: R8 only moves or closes a row, and the reminder itself goes out from "
          "_fire_due_reminders",
          ("self._meeting_reminder_moved(r, now=now, target=target)" in sender,
           "add_scheduled_reminder" in src[src.index("async def _maybe_meeting_reminder("):
                                           src.index("async def _meeting_reminder_moved(")],
           "kind=\"reminder\"" in src[src.index("async def _maybe_meeting_reminder("):
                                      src.index("async def _meeting_reminder_moved(")]), (True, True, False))


def settings(check):
    import subprocess

    print("\nSETTINGS — the new variable, the boot line, the rules file")
    src = open(os.path.join(ROOT, "config.py"), encoding="utf-8").read()
    check("MEETING_PREP_TIME=14:00, in the code and in .env.example, beside the two it joins",
          ('_hhmm("MEETING_PREP_TIME", os.getenv("MEETING_PREP_TIME", "14:00"), "14:00")' in src,
           _example("MEETING_PREP_TIME"), _example("MEETING_DAYOF_TIME"), _example("MEETING_PREP_DAYS_BEFORE")),
          (True, "14:00", "10:00", "5,3"))
    done = subprocess.run(
        [sys.executable, "-c", "import config, sys; sys.stdout.write(config.MEETING_PREP_TIME)"], cwd=ROOT,
        capture_output=True, text=True,
        env=dict(os.environ, MEETING_PREP_TIME="teatime", DISCORD_TOKEN="x", ANTHROPIC_API_KEY="x",
                 PYTHONIOENCODING="utf-8"))
    check("MEETING_PREP_TIME=teatime falls back to 14:00, with a warning in the log",
          ((done.stdout or "").strip().splitlines()[-1:],
           "MEETING_PREP_TIME='teatime' is not a time" in ((done.stderr or "") + (done.stdout or ""))),
          (["14:00"], True))
    with pinned(), tapped() as tap:
        saved = config.NEXT_ACTION_ENABLED
        config.NEXT_ACTION_ENABLED = True
        try:
            config.validate()
        finally:
            config.NEXT_ACTION_ENABLED = saved
    line = [m for _l, m in tap.lines if "[config] R8 meeting preparation" in m]
    check("the boot line says the touches and both times",
          [("5,3 days before" in m, "at 10:00 on its own" in m, "after 14:00" in m, "nothing is fetched" in m)
           for m in line], [(True, True, True, True)])
    check("the earliest moment the sweep starts asking still covers 10:00", drip.earliest_send_ist() <= (10, 0), True)
    done = subprocess.run([sys.executable, "-m", "meeting_prep"], cwd=ROOT, capture_output=True, text=True,
                          env=dict(os.environ, PYTHONIOENCODING="utf-8", DISCORD_TOKEN="x", ANTHROPIC_API_KEY="x"))
    check("meeting_prep.py's own self-test passes", (done.returncode, (done.stdout or "").strip().splitlines()[-1:]),
          (0, ["ALL PASSED"]))
    db_src = open(os.path.join(ROOT, "db.py"), encoding="utf-8").read()
    check("the touch record and the dates seen are tables of their own",
          ("CREATE TABLE IF NOT EXISTS meeting_touches" in db_src,
           "CREATE TABLE IF NOT EXISTS meeting_dates_seen" in db_src), (True, True))
    import evidence
    check("evidence that a meeting was booked can no longer turn the post into an offer",
          bool(evidence.stage_of(nextaction.R_MEETING_PREP)), False)


def run(check):
    with pinned():
        who_is_picked(check)
        the_message(check)
        the_memory(check)
    the_news(check)
    the_day(check)
    the_reminder(check)
    settings(check)
