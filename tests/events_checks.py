"""R3 "AI EVENTS & SUMMITS" (rebuilt 14 Oct 2026) — the checks, shared by verify_events.py and
tests/test_events.py.

    sys.path.insert(0, <repo>/tests); import events_checks as evc
    evc.run(check)                 # check(name, got, want)

WHAT RUNS HERE IS THE REAL THING: rows through the real sheet parser, the real rule (`nextaction.run`), the real
renderer (`drip`, `wording.events_post`), the real suggestion gates (`events_discovery.choose`) and a real bot
behind a fake Discord with a throwaway database for the search step, the send, the proposals and the replies. ONE
THING IS A STAND-IN, because it is the network: the discovery search answers from a fixed text and counts how
often it was asked. No network, no model, no real sheet.

Every R3 setting is pinned to its .env.example default, never read from this machine's .env.

THE MAIN FIXTURE (`tab_rows`) IS SEVEN ROWS SHAPED LIKE THE LIVE TAB AS THE TICKET DESCRIBES IT: its seven date
strings word for word, the three event names and places the ticket gives, six rows with no usable deadline, two
registered and more than 14 days out. THE REST OF THOSE CELLS ARE STAND-INS (the other four names, which row has
the one deadline): this script does not read the live sheet. Every suggested event, link and date is made up (the
links are on events.example).
"""
import asyncio
import contextlib
import logging
import os
import re
import sys
from datetime import date, timedelta

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
import events_discovery  # noqa: E402
import gtm_sheet  # noqa: E402
import links  # noqa: E402
import nextaction  # noqa: E402
import rules as rules_mod  # noqa: E402
import wording  # noqa: E402

WED = date(2026, 10, 14)                       # a Wednesday
HEADERS = ["Event", "Date", "Last day for registration", "Registered?", "Link", "Location"]
BLOCK_A = "We have registered for these events and they are coming up soon:"
BLOCK_B = "Registration closes soon for these events:"
BLOCK_C = "Events not in the sheet, but worth looking at:"
ALONE = "No events on the tab for the next 14 days. A few worth a look that we could still register for:"
NOTHING = "No events on the tab for the next 14 days, and nothing new worth suggesting."
REMIND = "Shall I remind you again on Monday?"
ADD_AND_REMIND = "Want me to add any of these to the GTM Playbook? And shall I remind you again on Monday?"
LIVE_DATES = (                                  # the six the ticket lists, with what each must parse to
    ("15 October 2026", date(2026, 10, 15), date(2026, 10, 15)),
    ("October 20–21,2026", date(2026, 10, 20), date(2026, 10, 21)),
    ("4-5 November 2026", date(2026, 11, 4), date(2026, 11, 5)),
    ("13-15 Oct, 2026", date(2026, 10, 13), date(2026, 10, 15)),
    ("25-26 November 2026", date(2026, 11, 25), date(2026, 11, 26)),
    ("20 November 2026", date(2026, 11, 20), date(2026, 11, 20)),
)
_ABSENT = object()


def _example(name: str) -> str:
    for line in open(os.path.join(ROOT, ".env.example"), encoding="utf-8"):
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip()
    raise AssertionError(f"{name} is not in .env.example")


def _list(name: str) -> list:
    return [x.strip() for x in _example(name).split(",") if x.strip()]


def pins() -> dict:
    return {
        "EVENTS_WINDOW_DAYS": int(_example("EVENTS_WINDOW_DAYS")),
        "EVENTS_REMIND_AGAIN_WEEKDAY": 0,
        "EVENTS_DISCOVERY_MAX_PER_RUN": int(_example("EVENTS_DISCOVERY_MAX_PER_RUN")),
        "EVENTS_DISCOVERY_MAX_PER_MONTH": int(_example("EVENTS_DISCOVERY_MAX_PER_MONTH")),
        "EVENTS_DEADLINE_RECHECK_DAYS": int(_example("EVENTS_DEADLINE_RECHECK_DAYS")),
        "EVENTS_SUGGEST_LOCATIONS": _list("EVENTS_SUGGEST_LOCATIONS"),
        "EVENTS_CALENDAR_URLS": [],
    }


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

def on(n, day=WED) -> str:
    return (day + timedelta(days=n)).strftime("%d-%m-%Y")


def ev(name, when, deadline="", registered="", location="Bengaluru", link="https://events.example/on-the-sheet"):
    return [name, when, deadline, registered, link, location]


def tab_rows() -> list:
    """Seven rows shaped like the live tab (see the module docstring for what is a stand-in)."""
    return [
        ev("Stand-in Event One", "15 October 2026", "10 October 2026"),          # the one deadline: passed
        ev("Stand-in Event Two", "October 20–21,2026", "not available"),
        ev("Future of AI Summit", "4-5 November 2026", "not available", location="London"),
        ev("Yourstory Techsparks", "13-15 Oct, 2026", "not available"),
        ev("Stand-in Event Five", "25-26 November 2026", "not available", "Yes"),
        ev("Symphony — India's Voice AI Ecosystem", "20 November 2026", "not available", "Yes"),
        ev("Stand-in Event Seven", "23 September", "not available"),
    ]


def parsed(rows) -> list:
    tab = gtm_sheet.SHEETS._parse_values("AI Events & Summits", [list(HEADERS)] + [list(r) for r in rows],
                                         read_at=dl.real_epoch())
    return list(tab.rows)


def engine(rows, day=WED):
    return nextaction.run(today=day, rows=[], events=parsed(rows) if rows else [],
                          day_rules=[rules_mod.by_id("R3")])


def r3(result) -> list:
    return [a for a in result["actions"] if a.get("rule_id") == "R3"]


def sheet_lines(result) -> list:
    """[(block, line)] in the order the rule ranked them."""
    rows = [a for a in r3(result) if a.get("event_kind") != nextaction.EVENT_CARRIER]
    return [(a["event_block"], a["event_line"]) for a in sorted(rows, key=lambda a: a["pick_order"])]


def names(result) -> list:
    return [line.split(" — ")[0].split(" (")[0] for _b, line in sheet_lines(result)]


def suggestion(name, location, when, link=None, deadline=None) -> dict:
    link = link if link is not None else "https://events.example/" + re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return {"name": name, "location": location, "date": when, "deadline": deadline, "link": link,
            "event_key": events_discovery.event_key(name, when)}


def suggestions() -> list:
    return [suggestion("Cypher 2026", "Bengaluru, India", date(2026, 11, 5)),
            suggestion("London AI Forum", "London, UK", date(2026, 11, 12)),
            suggestion("Karnataka Data Day", "Mysuru, Karnataka", date(2026, 11, 19))]


RENDERED: list = []


def post_of(result, day=WED, *, found=_ABSENT, searched=True) -> str:
    """The post for one run. `found` are the suggestions the search step put on the carrier (omit for "no search
    step at all"); `searched` says whether the search ran."""
    message = next((m for m in drip.group(result["actions"], day=day) if m.get("type") == nextaction.R_EVENTS), None)
    if message is None:
        return ""
    carrier = drip.event_carrier(message)
    if found is not _ABSENT and carrier:
        carrier["event_proposals"] = list(found)
        carrier["event_search"] = "ok" if searched else "unavailable"
        carrier["web_pending"] = False
    if drip.nothing_to_say(message):
        return ""
    text = drip.compose_fallback(message)
    if text:
        RENDERED.append(text)
    return text


def one(rows, day=WED, **kw) -> str:
    return post_of(engine(rows, day), day, **kw)


def numbered(text) -> list:
    return [(int(m.group(1)), m.group(2)) for m in re.finditer(r"^(\d+)\. (.+)$", str(text or ""), re.MULTILINE)]


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

def who_is_picked(check):
    print("\nWHO IS PICKED — block A, block B, or skipped")
    check("registered, 5 days out: block A. Registered, 30 days out: absent",
          sheet_lines(engine([ev("Near Forum", on(5), "", "Yes"), ev("Far Forum", on(30), "", "Yes")])),
          [("A", "Near Forum (Bengaluru) — Mon 19 Oct")])
    check("not registered, deadline in 6 days: block B, and the line shows both dates",
          sheet_lines(engine([ev("Data Summit", on(9), on(6), location="London")])),
          [("B", "Data Summit (London) — Fri 23 Oct, closes Tue 20 Oct")])
    with tapped() as tap:
        closed = engine([ev("Closed Conf", on(9), on(-1))])
    check('not registered, deadline passed: absent, logged "registration closed"',
          (sheet_lines(closed), [("registration closed" in m) for _l, m in tap.lines if "Closed Conf" in m]),
          ([], [True]))
    with tapped() as tap:
        unknown = engine([ev("Blank Deadline", on(5), ""), ev("Not Available", on(6), "not available"),
                          ev("To Be Decided", on(7), "TBD"), ev("Prose Deadline", on(8), "soon, we think")])
    check('not registered, deadline blank / "not available" / "TBD" / unreadable: all absent',
          sheet_lines(unknown), [])
    check("...and each one is logged",
          sorted(m.split("'")[1] for _l, m in tap.lines if "no usable registration deadline" in m),
          ["Blank Deadline", "Not Available", "Prose Deadline", "To Be Decided"])
    check("...by the two existing definitions and the date parser, not a third list",
          [nextaction.event_deadline_unknown(v) for v in ("", "not available", "TBD", "N/A", "20 October 2026")],
          [True, True, True, True, False])
    check("a deadline in 6 days for an event in December: block B",
          sheet_lines(engine([ev("December Summit", "9-10 December 2026", on(6), location="London")])),
          [("B", "December Summit (London) — Wed 9 to Thu 10 Dec, closes Tue 20 Oct")])
    check("not registered, deadline and event both more than 14 days out: not mentioned yet",
          sheet_lines(engine([ev("Far Both", on(40), on(30))])), [])
    with tapped() as tap:
        ended = engine([ev("Ended Yesterday", on(-1), "", "Yes"),
                        ev("Under Way", "13-15 Oct, 2026", "", "Yes")])
    check("an event that ended yesterday: absent. One that started yesterday and ends tomorrow: present, and "
          "the line says when it started",
          sheet_lines(ended), [("A", "Under Way (Bengaluru) — Tue 13 to Thu 15 Oct, started Tuesday")])
    check("...the ended one is logged", [("ended on" in m) for _l, m in tap.lines if "Ended Yesterday" in m], [True])
    check("the Attended column is not read",
          sheet_lines(engine([ev("Near Forum", on(5), "", "Yes")])) ==
          [(b, l) for b, l in sheet_lines(nextaction.run(
              today=WED, rows=[], day_rules=[rules_mod.by_id("R3")],
              events=[dict(r, attended="Yes") for r in parsed([ev("Near Forum", on(5), "", "Yes")])]))], True)
    src = open(os.path.join(ROOT, "nextaction.py"), encoding="utf-8").read()
    body = src[src.index("def _r_events("):src.index("def _r_deliverables(")]
    check("...the rule never looks at it", '"attended"' in body, False)


def dates(check):
    print("\nDATES — the parser is untouched; the year it assumes is said out loud")
    got = []
    for text, start, end in LIVE_DATES:
        when = gtm_sheet.parse_event_date(text)
        got.append((text, when["start"], when["end"] or when["start"]))
    check("every date string on the live tab parses to the right start and end (all six)",
          got, [(t, s, e) for t, s, e in LIVE_DATES])
    # THE PARSER RESOLVES A YEARLESS DATE TO ITS NEXT OCCURRENCE FROM THE REAL TODAY, so the year is read back
    # from it rather than written here, and the run is staged a week before that day.
    next_one = gtm_sheet.parse_event_date("23 September")
    week_before = next_one["start"] - timedelta(days=7)
    yearless = engine([ev("Yearless Forum", "23 September", "", "Yes", location="Pune")], week_before)
    check("'23 September' with no year: present, and the line says the year was assumed",
          (next_one["year_assumed"], sheet_lines(yearless)),
          (True, [("A", "Yearless Forum (Pune) \u2014 23 September (no year on the sheet \u2014 assuming "
                        f"{next_one['start'].year})")]))
    check("...a registration deadline with no year is read the same way and still shows its close date",
          [b for b, _l in sheet_lines(engine(
              [ev("Yearless Deadline", next_one["start"].strftime("%d %B %Y"), "20 September")], week_before))],
          ["B"])
    with tapped() as tap:
        unreadable = engine([ev("Mystery Day", "sometime in spring", "", "Yes"), ev("Near Forum", on(5), "", "Yes")])
    text = post_of(unreadable, found=[])
    check("an unreadable date: absent and logged", (names(unreadable),
          [("cannot be read" in m) for _l, m in tap.lines if "Mystery Day" in m]), (["Near Forum"], [True]))
    check('...and no "date unclear" text anywhere', [w for w in ("unclear", "Mystery") if w in text], [])
    check("the unclear-date plumbing is gone",
          [n for n in ("unclear_event_key", "EVENT_UNCLEAR", "EVENT_NO_DEADLINE") if hasattr(nextaction, n)]
          + [n for n in ("EVENT_OPENERS",) if hasattr(drip, n)]
          + (["events_unclear_seen"] if "events_unclear_seen" in open(os.path.join(ROOT, "bot.py"),
                                                                    encoding="utf-8").read() else []), [])


def the_live_tab(check):
    print("\nTHE TAB AS IT IS TODAY — blocks A and B empty, three suggestions")
    with tapped() as tap:
        result = engine(tab_rows())
    check("none of the seven rows has a line: two registered beyond 14 days, one closed, four with no deadline",
          sheet_lines(result), [])
    logged = [m for _l, m in tap.lines if "[R3] skipped" in m]
    why = {r[0]: next((m for m in logged if r[0] in m), "") for r in tab_rows()}
    check("...and every one of the seven is in the log with its reason", (len(logged), all(why.values())), (7, True))
    check("...'registration closed' for the passed deadline, 'no usable registration deadline' for four, and "
          "'more than 14 days out' for the two we are registered for",
          ("registration closed" in why["Stand-in Event One"],
           all("no usable registration deadline" in why[n] for n in (
               "Stand-in Event Two", "Future of AI Summit", "Yourstory Techsparks", "Stand-in Event Seven")),
           all("more than 14 days out" in why[n] for n in (
               "Stand-in Event Five", "Symphony \u2014 India's Voice AI Ecosystem"))), (True, True, True))
    check("the sheet used none of the three places", drip.event_carrier(
        next(m for m in drip.group(result["actions"], day=WED)))["event_room"], 3)
    text = post_of(result, found=suggestions())
    check("the post: three suggestions under their own lead line, then the two-part closing line",
          text, ALONE + "\n"
          "1. [Cypher 2026](<https://events.example/cypher-2026>) — Thu 5 Nov, Bengaluru\n"
          "2. [London AI Forum](<https://events.example/london-ai-forum>) — Thu 12 Nov, London\n"
          "3. [Karnataka Data Day](<https://events.example/karnataka-data-day>) — Thu 19 Nov, Mysuru\n\n"
          + ADD_AND_REMIND)
    check("...the deleted 'no registration deadline on the sheet' line has not come back",
          "no registration deadline" in text, False)


def the_message(check):
    print("\nTHE MESSAGE — three lines, three blocks, one numbering")
    a1, a2, a3 = (ev("Alpha Forum", on(3), "", "Yes"), ev("Beta Forum", on(8), "", "Yes"),
                  ev("Gamma Forum", on(12), "", "Yes"))
    b1 = ev("Delta Summit", on(20), on(2), location="London")
    b2 = ev("Epsilon Summit", on(10), on(6), location="London")
    three = one([a1, a2, a3], found=suggestions())
    check("three from the sheet: no suggestions, and only the remind half of the closing line",
          three, BLOCK_A + "\n1. Alpha Forum (Bengaluru) — Sat 17 Oct\n2. Beta Forum (Bengaluru) — Thu 22 Oct\n"
          "3. Gamma Forum (Bengaluru) — Mon 26 Oct\n\n" + "Shall I remind you again tomorrow?")
    two = one([a2, b2], found=suggestions())
    check("two from the sheet: ONE suggestion; the blocks print A, B, C; one numbering from 1",
          two, BLOCK_A + "\n1. Beta Forum (Bengaluru) — Thu 22 Oct\n\n" + BLOCK_B
          + "\n2. Epsilon Summit (London) — Sat 24 Oct, closes Tue 20 Oct\n\n" + BLOCK_C
          + "\n3. [Cypher 2026](<https://events.example/cypher-2026>) — Thu 5 Nov, Bengaluru\n\n" + ADD_AND_REMIND)
    one_row = one([a2], found=suggestions())
    check("one from the sheet: two suggestions", [n for n, _l in numbered(one_row)], [1, 2, 3])
    check("...and an empty block omits its heading", (BLOCK_A in one_row, BLOCK_B in one_row, BLOCK_C in one_row),
          (True, False, True))
    none = one([ev("Far Forum", on(40), "", "Yes")], found=suggestions())
    check("none from the sheet: three suggestions under the alternate lead line, and no block C heading",
          (none.split("\n")[0], len(numbered(none)), BLOCK_C in none), (ALONE, 3, False))
    only_b = one([b2], found=[])
    check("only block B: its heading, one line, the remind half",
          only_b, BLOCK_B + "\n1. Epsilon Summit (London) — Sat 24 Oct, closes Tue 20 Oct\n\n" + REMIND)
    nothing = one([ev("Far Forum", on(40), "", "Yes")], found=[])
    check("nothing anywhere: exactly the one-line message, and nothing else", nothing, NOTHING)
    check("...but when the search could NOT run, nothing is claimed and nothing is posted",
          one([ev("Far Forum", on(40), "", "Yes")], found=[], searched=False), "")
    with pinned(EVENTS_WINDOW_DAYS=21):
        check("the number in those two lines follows EVENTS_WINDOW_DAYS",
              one([ev("Far Forum", on(40), "", "Yes")], found=[]),
              "No events on the tab for the next 21 days, and nothing new worth suggesting.")

    urgent = engine([ev("Starts In Three", on(3), "", "Yes"), ev("Closes In Two", on(30), on(2), location="London")])
    check("urgency: a deadline closing in 2 days outranks an event starting in 3 days",
          [n for n in names(urgent)], ["Closes In Two", "Starts In Three"])
    text = post_of(urgent, found=[])
    check("...and the blocks still print A then B, whatever the ranking picked",
          (text.index(BLOCK_A) < text.index(BLOCK_B), numbered(text)[0][1].startswith("Starts In Three")),
          (True, True))
    five = [a1, a2, a3, b1, b2]
    crowded = engine(five)
    check("more than three qualify: closing deadlines first (by close date), then the soonest start",
          names(crowded), ["Delta Summit", "Epsilon Summit", "Alpha Forum"])
    text = post_of(crowded, found=suggestions())
    check("...three lines in the WHOLE post, no suggestion, and no '+N more' line",
          (len(numbered(text)), BLOCK_C in text, [w for w in ("more", "+", "ask and I'll list") if w in text]),
          (3, False, []))
    tie = engine([ev("Second Row", on(5), "", "Yes"), ev("First Row", on(5), "", "Yes")])
    check("a tie goes to sheet order", names(tie), ["Second Row", "First Row"])
    again = engine(five, WED + timedelta(days=0))
    check("nothing rotates: the same tab on the same day ranks the same way", names(again), names(crowded))

    check("the add half of the closing line appears only with block C; the remind half always",
          ([t.split("\n")[-1] for t in (three, two, one_row, none, only_b)]),
          ["Shall I remind you again tomorrow?", ADD_AND_REMIND, ADD_AND_REMIND, ADD_AND_REMIND, REMIND])
    soon = one([ev("Friday Forum", on(2), "", "Yes")], found=[])
    later = one([ev("Later Forum", on(10), "", "Yes")], found=[])
    check('"tomorrow" replaces "Monday" when something listed falls before Monday',
          (soon.split("\n")[-1], later.split("\n")[-1]), ("Shall I remind you again tomorrow?", REMIND))
    early = one([ev("Later Forum", on(10), "", "Yes")],
                found=[suggestion("Weekend Meetup", "London, UK", WED + timedelta(days=3))])
    check("...a SUGGESTED event before Monday counts too", early.split("\n")[-1],
          "Want me to add any of these to the GTM Playbook? And shall I remind you again tomorrow?")
    check("the post is rendered in code: R3 is verbatim, and both closing lines are recognised as offers",
          (nextaction.R_EVENTS in drip.VERBATIM_TYPES, drip.is_offer_line(REMIND), drip.is_offer_line(ADD_AND_REMIND),
           drip.is_offer_line("Shall I remind you again tomorrow?")), (True, True, True, True))
    check("the rule's cap is three, in the rules file", rules_mod.by_id("R3").max_items_per_post, 3)


def links_and_never(check):
    print("\nLINKS, AND WHAT IS NEVER IN A POST")
    text = one([ev("Beta Forum", on(8), "", "Yes", link="https://events.example/beta")], found=suggestions())
    check("a sheet event is NOT a link; a suggestion's name IS its link",
          (numbered(text)[0][1], numbered(text)[1][1]),
          ("Beta Forum (Bengaluru) — Thu 22 Oct",
           links.link("Cypher 2026", "https://events.example/cypher-2026") + " — Thu 5 Nov, Bengaluru"))
    bad = one([ev("Far Forum", on(40), "", "Yes")],
              found=[suggestion("No Scheme Summit", "London, UK", date(2026, 11, 3), link="events.example/no-scheme"),
                     suggestion("Good Summit", "London, UK", date(2026, 11, 4))])
    check("a suggestion whose link has no http(s) scheme is not shown at all",
          [l for _n, l in numbered(bad)], ["[Good Summit](<https://events.example/good-summit>) — Wed 4 Nov, London"])
    every = [u for t in RENDERED for u in re.findall(r"\]\(([^)]*)\)", t)]
    check("every link in every rendered message carries a scheme and went through links.link",
          (len(every) >= 8, [u for u in every if not re.match(r"^<https?://[^\s>]+>$", u)],
           [t for t in RENDERED if links.bare_urls(t)]), (True, [], []))
    check("no rendered message has a '+N more' line, a 'date unclear' line or the old no-deadline line",
          [t for t in RENDERED if re.search(r"\+\d+ more|date unclear|no registration deadline|ask and I'll list", t)],
          [])
    check("no rendered message uses a bullet: every event line is numbered", [t for t in RENDERED if "•" in t], [])


def the_suggestions(check):
    print("\nTHE SUGGESTIONS — three places and nothing else")
    check("Mumbai is out; Bengaluru, Bangalore, Karnataka and London pass; whole words only",
          [events_discovery.location_allowed(p) for p in (
              "Mumbai, India", "Bengaluru, India", "Bangalore", "Mysuru, Karnataka", "London, UK", "london",
              "Londonderry, UK", "New Delhi", "")],
          [False, True, True, True, True, True, False, False, False])

    class Db:
        def __init__(self, used=0, seen=None):
            self.used, self.seen = used, seen or {}

        def event_discoveries_this_month(self, month):
            return self.used

        def event_discovery_seen(self, key):
            return self.seen.get(key)

    found = suggestions()
    mumbai = suggestion("Mumbai AI Week", "Mumbai, India", date(2026, 11, 10))
    keep, skipped = events_discovery.choose([mumbai] + found, existing=[], today=WED, db=Db())
    check("the filter is in code: a Mumbai event the search returned is dropped, with a reason",
          ([e["name"] for e in keep], [s for s in skipped if "Mumbai" in s and "EVENTS_SUGGEST_LOCATIONS" in s] != []),
          (["Cypher 2026", "London AI Forum", "Karnataka Data Day"], True))
    keep, _s = events_discovery.choose(found, existing=[], today=WED, db=Db(), per_run=1)
    check("the room the sheet left is the ceiling for one run", [e["name"] for e in keep], ["Cypher 2026"])
    declined = {found[0]["event_key"]: {"status": "declined"}}
    keep, skipped = events_discovery.choose(found, existing=[], today=WED, db=Db(seen=declined))
    check("a declined event is never suggested again",
          ([e["name"] for e in keep], [s for s in skipped if "Cypher" in s]),
          (["London AI Forum", "Karnataka Data Day"], ["Cypher 2026 — already declined; not asking again"]))
    keep, skipped = events_discovery.choose(found, existing=[], today=WED, db=Db(used=12))
    check("the monthly ceiling (12) reached: nothing is suggested", ([e["name"] for e in keep], len(skipped)), ([], 3))
    keep, _s = events_discovery.choose(found, existing=parsed([ev("Cypher", "5 November 2026")]), today=WED, db=Db())
    check("an event already on the tab is not suggested", [e["name"] for e in keep],
          ["London AI Forum", "Karnataka Data Day"])
    check("the search prompt names the same places",
          all(p in events_discovery.discovery_prompt([], today=WED) for p in ("Bangalore", "Karnataka", "London")),
          True)


SEARCH_TEXT = "\n".join([
    "EVENT | Cypher 2026 | Bengaluru, India | 2026-11-05 | NOT STATED | https://events.example/cypher-2026",
    "EVENT | Mumbai AI Week | Mumbai, India | 2026-11-10 | NOT STATED | https://events.example/mumbai-ai-week",
    "EVENT | London AI Forum | London, UK | 2026-11-12 | 2026-11-01 | https://events.example/london-ai-forum",
    "EVENT | Karnataka Data Day | Mysuru, Karnataka | 2026-11-19 | NOT STATED | https://events.example/karnataka-data-day",
    "EVENT | Bangalore Agents Meetup | Bangalore | 2026-11-20 | NOT STATED | https://events.example/agents-meetup",
])


class Stage:
    """One bot with the REAL discovery step, whose search is a stand-in that answers from SEARCH_TEXT."""

    def __init__(self, rows, *, test_mode=False, search_ok=True, extra_pins=None):
        import replies_world as rw
        self.rw = rw
        self.w = rw.World(test_mode, pretend=(WED, 13, 0), rules={"R3"}, pins=dict(pins(), **(extra_pins or {})))
        self.rows, self.search_ok = rows, search_ok
        self.searches = []
        self.tap = LogTap()

    def __enter__(self):
        w = self.w.__enter__()
        w.sheet.set(events=self.rows)
        bot = w.bot
        bot._discover_events = type(bot)._discover_events.__get__(bot)      # the real one back

        async def one_search(prompt, **kw):
            self.searches.append([q.get("q") for q in kw.get("queries") or []])
            return {"ok": True, "text": SEARCH_TEXT}

        async def available(**_k):
            return (True, "") if self.search_ok else (False, "the search is switched off")
        bot._one_search = one_search
        bot._search_available = available
        # THE STAGED WEDNESDAY MAY BE AHEAD OF THE REAL CALENDAR, and the bot does not research a day that has not
        # happened. Here the staged day is the day.
        bot._is_future = lambda day: False
        logging.getLogger().addHandler(self.tap)
        return self

    def __exit__(self, *exc):
        logging.getLogger().removeHandler(self.tap)
        return self.w.__exit__(*exc)

    async def post(self):
        """Plan, research and send the Wednesday post. (text, BotMsg, message), or ("", None, message)."""
        w = self.w
        planned = await w.bot._plan_drip(today=WED, already=[])
        msg = next((m for m in (planned or {}).get("messages") or [] if m.get("type") == nextaction.R_EVENTS), None)
        if msg is None:
            return "", None, None
        n = len(w.posted)
        await w.bot._research_message(msg, today=WED)
        await w.bot._send_drip_message(w.chan, msg, marker=dl.iso(WED), channel_id=w.chan.id)
        if len(w.posted) == n:
            return "", None, msg
        return self.rw.strip_tag(w.posted[n].content), w.posted[n], msg

    def logs(self, needle, level=None) -> list:
        return [m for lvl, m in self.tap.lines if needle in m and (level is None or lvl == level)]

    def proposals(self) -> dict:
        return dict(self.w.proposals())

    async def say(self, text, **kw):
        before = len(self.w.posted)
        await self.w.say(text, **kw)
        return [self.rw.strip_tag(m.content) for m in self.w.posted[before:]]


def body_of(text) -> str:
    """A sent post without its heading and tags line."""
    lines = str(text or "").split("\n")
    while lines and (re.fullmatch(r"\*\*.+\*\*", lines[0].strip()) or re.fullmatch(r"(<@!?\d+>\s*)*", lines[0].strip())):
        lines.pop(0)
    return "\n".join(lines)


def the_bot(check):
    print("\nTHE BOT — the search step, the send, the proposals, the replies")

    async def live_tab(test_mode=False):
        out = {}
        with Stage(tab_rows(), test_mode=test_mode) as s:
            model = s.w.counts()["model"]
            text, post, _msg = await s.post()
            out["text"], out["heading"] = body_of(text), text.split("\n")[0]
            out["tagged"] = bool(post) and post.content.startswith("[TEST")
            out["model"] = s.w.counts()["model"] - model
            out["searches"] = list(s.searches)
            out["kinds"] = sorted(k.split(":")[0] for k in s.proposals())
            out["not_suggesting"] = s.logs("[events] not suggesting")
            import json
            with s.w.bot.db.conn() as c:
                raw = c.execute("SELECT payload FROM write_proposals WHERE kind = 'event_append'").fetchone()
            events = (json.loads(raw[0]) if raw and raw[0] else {}).get("events", [])
            out["numbers"] = [(e.get("n"), e.get("name")) for e in events]
            with s.w.bot.db.conn() as c:
                out["recorded"] = c.execute("SELECT COUNT(*) FROM event_discoveries").fetchone()[0]
            if post is not None:
                appended = []
                real = gtm_sheet.SHEETS.append_row

                def append_row(tab, values, **kw):
                    appended.append(values.get("event"))
                    return {"ok": True, "sheet_row": 9, "written": [], "dry_run": False, "error": ""}
                gtm_sheet.SHEETS.append_row = append_row
                try:
                    out["member"] = await s.say("yes add 2", who="member", reply_to=post)
                    out["after_member"] = list(appended)
                    out["approver"] = await s.say("yes add 2", who="approver", reply_to=post)
                    out["after_approver"] = list(appended)
                finally:
                    gtm_sheet.SHEETS.append_row = real
                out["remind"] = await s.say("yes, remind me", who="approver", reply_to=post)
            # A SECOND WEDNESDAY RUN the same day's month: what was suggested is never suggested again.
            s.w.posted.clear()
        return out

    async def room_for(rows, **kw):
        with Stage(rows, **kw) as s:
            text, _post, _msg = await s.post()
            return {"text": body_of(text), "searches": len(s.searches), "ceiling": s.logs("monthly limit", "WARNING"),
                    "errors": s.logs("NO ROWS", "ERROR"), "kinds": sorted(k.split(":")[0] for k in s.proposals())}

    async def month_full():
        with Stage(tab_rows(), extra_pins={"EVENTS_DISCOVERY_MAX_PER_MONTH": 3}) as s:
            first, _p, _m = await s.post()
            s.w.rules = {"R3"}
            with s.w.bot.db.conn() as c:
                c.execute("DELETE FROM drip_sends")
            second, _p2, _m2 = await s.post()
            return body_of(first), body_of(second), s.logs("monthly limit", "WARNING")

    with pinned():
        live = asyncio.run(live_tab())
        test = asyncio.run(live_tab(True))
        full = asyncio.run(room_for([ev("Alpha Forum", on(3), "", "Yes"), ev("Beta Forum", on(8), "", "Yes"),
                                     ev("Gamma Forum", on(12), "", "Yes")]))
        one_left = asyncio.run(room_for([ev("Beta Forum", on(8), "", "Yes"),
                                         ev("Epsilon Summit", on(10), on(6), location="London")]))
        no_search = asyncio.run(room_for([ev("Far Forum", on(40), "", "Yes")], search_ok=False))
        no_search_a = asyncio.run(room_for([ev("Beta Forum", on(8), "", "Yes")], search_ok=False))
        empty = asyncio.run(room_for([]))
    with pinned(EVENTS_DISCOVERY_MAX_PER_MONTH=3):
        first, second, ceiling = asyncio.run(month_full())

    want = (ALONE + "\n"
            "1. [Cypher 2026](<https://events.example/cypher-2026>) — Thu 5 Nov, Bengaluru\n"
            "2. [London AI Forum](<https://events.example/london-ai-forum>) — Thu 12 Nov, London\n"
            "3. [Karnataka Data Day](<https://events.example/karnataka-data-day>) — Thu 19 Nov, Mysuru\n\n"
            + ADD_AND_REMIND)
    RENDERED.append(live["text"])
    check("the tab as it is today, through the real planner, search step and sender: three suggestions, no "
          "model call", (live["heading"], live["text"], live["model"]), ("**AI events & summits**", want, 0))
    check("...Mumbai was returned by the search and filtered out in code; the fifth was over the room",
          (len([m for m in live["not_suggesting"] if "Mumbai AI Week" in m and "EVENTS_SUGGEST_LOCATIONS" in m]),
           len([m for m in live["not_suggesting"] if "Bangalore Agents Meetup" in m])), (1, 1))
    check("...the two searches name the places", [all(p in q for p in ("Bangalore", "London")) for q in
                                                  (live["searches"][0] if live["searches"] else [])], [True, True])
    check("...nothing is written: two proposals are opened and wait (add the events; remind again)",
          (live["kinds"], live["numbers"], live["recorded"]),
          (["event_append", "events_remind"],
           [(1, "Cypher 2026"), (2, "London AI Forum"), (3, "Karnataka Data Day")], 3))
    check('"yes add 2" from somebody who is not an approver adds nothing', live["after_member"], [])
    check('"yes add 2" from an approver adds event 2 and only event 2', live["after_approver"], ["London AI Forum"])
    check("a yes to the reminder schedules it and says when",
          [("I'll post the AI events list again on" in t) for t in live["remind"]], [True])
    check("test mode is identical apart from the [TEST] tag",
          (test["text"], test["kinds"], test["after_approver"], live["tagged"], test["tagged"]),
          (live["text"], live["kinds"], live["after_approver"], False, True))
    check("the sheet fills all three places: NO suggestions, and the search is not even run",
          (len(numbered(full["text"])), BLOCK_C in full["text"], full["searches"], full["kinds"]),
          (3, False, 0, ["events_remind"]))
    check("the sheet fills two places: ONE suggestion",
          ([l.split("]")[0] for _n, l in numbered(one_left["text"])][2:], one_left["searches"]),
          (["[Cypher 2026"], 1))
    check("the search cannot run and the sheet has nothing: no post, rather than 'nothing new worth suggesting'",
          no_search["text"], "")
    check("the search cannot run and the sheet has a line: the sheet's lines post without suggestions",
          (numbered(no_search_a["text"])[:1], BLOCK_C in no_search_a["text"]),
          ([(1, "Beta Forum (Bengaluru) — Thu 22 Oct")], False))
    check("the tab is empty or unreadable: no post, logged at ERROR, never a silent skip",
          (empty["text"], len(empty["errors"]) >= 1, empty["searches"]), ("", True, 0))
    check("the monthly ceiling reached: the first post suggests three, the next has none and the log says why",
          (len(numbered(first)), second, len(ceiling) >= 1), (3, NOTHING, True))


def settings(check):
    print("\nSETTINGS — two changed defaults, one new variable, the boot checks, the rules file")
    src = open(os.path.join(ROOT, "config.py"), encoding="utf-8").read()
    check("EVENTS_DISCOVERY_MAX_PER_MONTH is 12 in the code and in .env.example",
          (bool(re.search(r'_int\("EVENTS_DISCOVERY_MAX_PER_MONTH", 12\)', src)),
           _example("EVENTS_DISCOVERY_MAX_PER_MONTH")), (True, "12"))
    check("EVENTS_SUGGEST_LOCATIONS is the four names, in the code and in .env.example",
          ('"EVENTS_SUGGEST_LOCATIONS", "Bangalore,Bengaluru,Karnataka,London")' in src,
           _example("EVENTS_SUGGEST_LOCATIONS")), (True, "Bangalore,Bengaluru,Karnataka,London"))
    check("the unchanged ones are unchanged",
          (_example("EVENTS_WINDOW_DAYS"), _example("EVENTS_DISCOVERY_MAX_PER_RUN"),
           _example("EVENTS_DEADLINE_RECHECK_DAYS")), ("14", "3", "14"))

    def boot(**values):
        with pinned(**values), tapped() as tap:
            saved = config.NEXT_ACTION_ENABLED
            config.NEXT_ACTION_ENABLED = True
            try:
                config.validate()
            finally:
                config.NEXT_ACTION_ENABLED = saved
        warn = [m for lvl, m in tap.lines if lvl == "WARNING"]
        return ([m for m in warn if "EVENTS_SUGGEST_LOCATIONS is empty" in m],
                [m for m in warn if "EVENTS_DISCOVERY_MAX_PER_MONTH=" in m],
                [m for _l, m in tap.lines if "[config] R3 events" in m])

    empty, low, line = boot()
    check("the defaults boot with no R3 warning", (empty, low), ([], []))
    check("the boot line prints the new behaviour",
          [("THREE lines" in m, "Bangalore/Bengaluru/Karnataka/London" in m, "3 a run and 12 a month" in m,
            "skipped" in m) for m in line], [(True, True, True, True)])
    check("EVENTS_SUGGEST_LOCATIONS empty: the boot check warns that nothing could ever be suggested",
          [("no event could ever be suggested" in m) for m in boot(EVENTS_SUGGEST_LOCATIONS=[])[0]], [True])
    check("EVENTS_DISCOVERY_MAX_PER_MONTH below four runs: the boot check warns; at 12 it does not",
          [len(boot(EVENTS_DISCOVERY_MAX_PER_MONTH=n)[1]) for n in (8, 11, 12)], [1, 1, 0])
    rule = rules_mod.by_id("R3")
    check("R3: Wednesdays, three lines a post, counted toward the cap",
          (rule.weekdays, rule.max_items_per_post, rule.counts_toward_cap, rule.enabled), ((2,), 3, True, True))
    wordy = " ".join(str(getattr(rule, "sheet_wording", "") or "").split())
    check("the Bot Rules wording says what the rule now does",
          [w for w in ("Every Wednesday", "three lines in total", "next 14 days", "registration is closing",
                       "not on the sheet", "Bangalore, Karnataka or London",
                       "Rows with no registration deadline are not listed") if w not in wordy], [])
    check("...and not the old table", [w for w in ("date unclear", "no registration deadline on the sheet",
                                                    "You're registered for") if w in wordy], [])
    import subprocess
    done = subprocess.run([sys.executable, "-m", "events_discovery"], cwd=ROOT, capture_output=True, text=True,
                          env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    check("events_discovery's own self-test still passes",
          (done.returncode, (done.stdout or "").strip().splitlines()[-1:]), (0, ["ALL PASSED"]))


def run(check):
    with pinned():
        who_is_picked(check)
        dates(check)
        the_live_tab(check)
        the_message(check)
        the_suggestions(check)
    the_bot(check)
    with pinned():
        links_and_never(check)
    settings(check)
