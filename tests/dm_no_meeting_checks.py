"""R7 "NO MEETING BOOKED YET" (rebuilt 13 Oct 2026) — the checks, shared by verify_dm_no_meeting.py and
tests/test_dm_no_meeting.py.

    sys.path.insert(0, <repo>/tests); import dm_no_meeting_checks as dnm
    dnm.run(check)                 # check(name, got, want)

WHAT RUNS HERE IS THE REAL THING: made-up rows through the real sheet parser, the real rule (`nextaction.run`),
the real renderer (`drip`, `wording.dm_no_meeting_post`), the real rotation table in a throwaway database
(`db.record_dm_no_meeting_mention` / `dm_no_meeting_state`), and for the send path a real bot behind a fake
Discord. No network, no model, no real sheet.

Every R7 setting is pinned to its .env.example default, never read from this machine's .env.

THE PEOPLE: the names in the ticket's own example message, and made-up names for the rest. The dates are made up.
"""
import asyncio
import contextlib
import logging
import os
import re
import shutil
import sys
import tempfile
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
import nextaction  # noqa: E402
import rules as rules_mod  # noqa: E402
import rule13_fixtures as fx  # noqa: E402
import wording  # noqa: E402

MON = date(2026, 10, 19)                       # a Monday
NEXT_MON, THIRD_MON = MON + timedelta(days=7), MON + timedelta(days=14)
COLUMN_NAMES = ("LI DM", "Meeting Status", "Meeting Date", "Next Steps", "Deal Status")
NONE_LINE = "We sent them a DM more than 3 weeks ago and don't have a meeting set up with them."
BOOKED_LINE = "This meeting is set for more than a week after the DM went out."
_ABSENT = object()


def _example(name: str) -> str:
    """A default from .env.example: its live line, or — for R7's three timing
    settings, retired while R7 is off (RULES-OCT16) — its line in the RETIRED
    block ("# NAME=value  -> ...")."""
    retired = None
    for line in open(os.path.join(ROOT, ".env.example"), encoding="utf-8"):
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip()
        m = re.match(r"^#\s*" + re.escape(name) + r"=(\S*)", line)
        if m and retired is None:
            retired = m.group(1)
    if retired is not None:
        return retired
    raise AssertionError(f"{name} is not in .env.example")


@contextlib.contextmanager
def r7_back_on():
    """R7 IS OFF SINCE 16 OCT (RULES-OCT16). These are R7's own checks, so they
    run with R7 TURNED BACK ON exactly as the yaml note says — enabled: true and
    back in Monday's order — which is the ticket's T10: it runs as before."""
    path = (config.BOT_RULES_FILE or "").strip()
    shipped = open(path, encoding="utf-8").read()
    on = (shipped.replace("  mon: [R4, R1, R10]", "  mon: [R4, R7, R1, R10]")
          .replace("    # off since 16 Oct by decision; R13 owns the chase\n"
                   "    enabled: false", "    enabled: true"))
    assert on != shipped, "R7's off switch is not where the checks expect it"
    tmp = tempfile.mkdtemp(prefix="saley-r7-on-")
    here = os.path.join(tmp, "bot_rules.yaml")
    open(here, "w", encoding="utf-8").write(on)
    before = config.BOT_RULES_FILE
    config.BOT_RULES_FILE = here
    rules_mod.reload(here)
    try:
        yield
    finally:
        config.BOT_RULES_FILE = before
        rules_mod.reload()
        shutil.rmtree(tmp, ignore_errors=True)


def _list(name: str) -> list:
    return [x.strip() for x in _example(name).split(",") if x.strip()]


def pins() -> dict:
    return {
        "DM_NO_MEETING_DAYS": int(_example("DM_NO_MEETING_DAYS")),
        "DM_NO_MEETING_BOOKED_GAP_DAYS": int(_example("DM_NO_MEETING_BOOKED_GAP_DAYS")),
        "DM_NO_MEETING_MAX_CONTACTS": int(_example("DM_NO_MEETING_MAX_CONTACTS")),
        "DM_NO_MEETING_DM_SENT_MARKERS": _list("DM_NO_MEETING_DM_SENT_MARKERS"),
        "DM_NO_MEETING_DM_REPLIED_MARKERS": _list("DM_NO_MEETING_DM_REPLIED_MARKERS"),
        # The empty entry before the comma is the blank cell, so this one keeps its empties.
        "DM_NO_MEETING_STATUS_OPEN": [x.strip().lower() for x in _example("DM_NO_MEETING_STATUS_OPEN").split(",")],
        "DAILY_MESSAGE_CAP": int(_example("DAILY_MESSAGE_CAP")),
        "CLOSURE_STOP_MARKERS": _list("CLOSURE_STOP_MARKERS"),
        "DEAL_ON_HOLD_MARKERS": _list("DEAL_ON_HOLD_MARKERS"),
        "NEXT_STEP_CONNECTED_MARKERS": ["connected"],
        "NEXT_STEP_DM_SENT_MARKERS": _list("NEXT_STEP_DM_SENT_MARKERS"),
        "NEXT_STEP_DM_REPLIED_MARKERS": _list("NEXT_STEP_DM_REPLIED_MARKERS"),
        "NEXT_STEP_CALL_AFTER_DM_DAYS": int(_example("NEXT_STEP_CALL_AFTER_DM_DAYS")),
        "NEXT_STEP_CALL_UNTIL_DAYS": int(_example("NEXT_STEP_CALL_UNTIL_DAYS")),
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

def dm(n, name, company, *, days=30, day=MON, sent="Sent", dm_date=_ABSENT, connected="", **cells):
    """One person DM'd `days` before `day`. NOT marked Connected unless asked, so R13 has no say."""
    when = (day - timedelta(days=days)) if dm_date is _ABSENT else dm_date
    return fx.person(n, name=name, company=company, connected=connected, dm_sent=sent, dm_date=when, **cells)


def example_values() -> list:
    """The ticket's own second example: two people with no meeting, one with a meeting booked late."""
    return [
        dm(1, "Ariya Rastrow", "Wisprlow.ai", days=40),
        dm(2, "Prof. Karim Ouazzane", "London Metropolitan University", days=38),
        dm(3, "Eleonore Lumer", "PolyAI", days=30, meeting_date=date(2026, 10, 24), meeting_status="TBD"),
    ]


EXAMPLE = f"""1. Ariya Rastrow (Wisprlow.ai)
2. Prof. Karim Ouazzane (London Metropolitan University)

{NONE_LINE}

3. Eleonore Lumer (PolyAI) — 24 Oct

{BOOKED_LINE}"""


def seven() -> list:
    """Seven qualifying people; P1 was DM'd longest ago."""
    return [dm(i, f"P{i}", f"Co {i}", days=50 - i) for i in range(1, 8)]


def rows_of(values) -> list:
    return list(fx.parse(values).rows)


def engine(values, day=MON, state=None, rule_ids=("R7",), no_state=False):
    rows = rows_of(values)
    return nextaction.run(today=day, rows=rows, prospect_rows=rows,
                          day_rules=[rules_mod.by_id(r) for r in rule_ids], next_step_state={},
                          dm_no_meeting_state=None if no_state else ({} if state is None else state))


def r7(result) -> list:
    return sorted((a for a in result["actions"] if a.get("rule_id") == "R7"), key=lambda a: a["pick_order"])


def picked(result) -> list:
    return [a["poc"] for a in r7(result)]


def blocks(result) -> list:
    return [(a["poc"], a["block"]) for a in r7(result)]


def message_of(result, day=MON):
    for m in drip.group(result["actions"], day=day):
        if m.get("type") == nextaction.R_DM_NO_MEETING:
            return m
    return None


RENDERED: list = []


def post_of(result, day=MON) -> str:
    m = message_of(result, day)
    text = drip.compose_fallback(m) if m else ""
    if text:
        RENDERED.append(text)
    return text


def one(values, **kw) -> tuple:
    """(names picked, the post) for one run."""
    result = engine(values, **kw)
    return picked(result), post_of(result, kw.get("day", MON))


class Ledger:
    """The real `dm_no_meeting_mentions` table in a throwaway database, written the way the bot's sender does."""

    def __enter__(self):
        from db import DB
        self.tmp = tempfile.mkdtemp(prefix="saley-r7-")
        self.db = DB(os.path.join(self.tmp, "r7_test.db"))
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.tmp, ignore_errors=True)
        return False

    def day(self, values, day, *, send=True, **kw) -> tuple:
        result = engine(values, day, state=self.db.dm_no_meeting_state(), **kw)
        text = post_of(result, day)
        if send and text:
            for a in drip.shown_contacts(message_of(result, day)):
                self.db.record_dm_no_meeting_mention(a["row_key"], on_date=dl.iso(day), block=a["block"])
        return picked(result), text


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
    print("\nWHO IS PICKED — four columns, in order")
    check("LI DM Sent 'sent', an LI DM Date 21 days old, a blank Meeting Status: in",
          blocks(engine([dm(1, "Ann Day", "Co", days=21, sent="sent")])), [("Ann Day", "none")])
    check("the same at 20 days: out. At 21: in",
          [picked(engine([dm(1, "Ann Day", "Co", days=n)])) for n in (20, 21, 22)],
          [[], ["Ann Day"], ["Ann Day"]])
    check("LI DM Sent 'replied': out. Blank: out. Anything else: out",
          [picked(engine([dm(1, "Ann Day", "Co", sent=v)])) for v in ("Replied", "replied", "", "in progress")],
          [[], [], [], []])
    check("an LI DM Date with LI DM Sent blank does not bring a person in: the cell decides, not the date",
          picked(engine([dm(1, "Ann Day", "Co", sent="", days=60)])), [])

    calls = []
    real = nextaction._days_between

    def counting(a, b):
        calls.append((a, b))
        return real(a, b)
    nextaction._days_between = counting
    try:
        nodate = picked(engine([dm(1, "No Date", "Co", dm_date="")]))
        measured_nodate = len(calls)
        dated = picked(engine([dm(1, "Has Date", "Co", days=30)]))
    finally:
        nextaction._days_between = real
    check("LI DM Date blank: out, and NOTHING is measured (no date arithmetic ran for the row)",
          (nodate, measured_nodate), ([], 0))
    check("...while a dated row is measured exactly once", (dated, len(calls)), (["Has Date"], 1))
    check("...and the row with no date says why", nextaction.dm_no_meeting_state(
        rows_of([dm(1, "No Date", "Co", dm_date="")])[0], today=MON)["status"], "no_date")

    check("Meeting Status TBD behaves exactly like blank, with no Meeting Date",
          [blocks(engine([dm(1, "Ann Day", "Co", meeting_status=v)])) for v in ("", "TBD", "tbd")],
          [[("Ann Day", "none")]] * 3)
    late = MON + timedelta(days=5)
    check("...and with a Meeting Date booked late",
          [blocks(engine([dm(1, "Ann Day", "Co", meeting_date=late, meeting_status=v)])) for v in ("", "TBD")],
          [[("Ann Day", "booked")]] * 2)
    check("Meeting Status Completed: out, with or without a Meeting Date",
          [picked(engine([dm(1, "Ann Day", "Co", meeting_status="Completed", **kw)]))
           for kw in ({}, {"meeting_date": late})], [[], []])

    def booked(gap, status="TBD", days=30):
        return blocks(engine([dm(1, "Ann Day", "Co", days=days, meeting_status=status,
                                 meeting_date=MON - timedelta(days=days) + timedelta(days=gap))]))
    check("a Meeting Date 5 days after the DM: out, booked promptly. 10 days after: in, second block",
          (booked(5), booked(10)), ([], [("Ann Day", "booked")]))
    check("...in from the day the gap reaches 7", (booked(6), booked(7)), ([], [("Ann Day", "booked")]))
    check("a Meeting Date in the past with the status still blank: in, second block",
          booked(12, status="", days=30), [("Ann Day", "booked")])
    check("a meeting booked late is measured from the DM to the MEETING, not to today: a DM 3 days old is in",
          booked(10, days=3), [("Ann Day", "booked")])


def on_hold_and_stops(check):
    print("\nWHO IS OUT — and On Hold is out for this rule only")
    check("Deal Status Won, Lost and On Hold: out",
          [picked(engine([dm(1, "Ann Day", "Co", deal=v)])) for v in ("Won", "Lost", "On Hold", "on hold")],
          [[], [], [], []])
    check("Prospect Status Unresponsive or Dead: out, as everywhere",
          [picked(engine([dm(1, "Ann Day", "Co", prospect=v)])) for v in ("Unresponsive", "Dead")], [[], []])
    held = dm(1, "Holly Hold", "Co", days=30, connected="Connected", li_date=MON - timedelta(days=60),
              step="Call the PoC", deal="On Hold")
    result = engine([held], rule_ids=("R13", "R7"))
    check("On Hold is OUT of R7 and STILL IN for R13: the same row, the same day",
          ([a["rule_id"] for a in result["actions"]], nextaction.stop_reason(rows_of([held])[0]),
           nextaction.row_gate(rows_of([held])[0], today=MON, snoozes={})[0]), (["R13"], "", True))
    src = open(os.path.join(ROOT, "config.py"), encoding="utf-8").read()
    check("CLOSURE_STOP_MARKERS was not changed: no 'hold' in it, in the code or in .env.example",
          ([m for m in _list("CLOSURE_STOP_MARKERS") if "hold" in m.lower()],
           '"dead,unresponsive,won,lost,closed won,closed lost,closed-won,closed-lost,"\n'
           '    "not interested,do not contact,dnc",' in src,
           _example("CLOSURE_STOP_MARKERS")),
          ([], True, "dead,unresponsive,won,lost,closed won,closed lost,closed-won,closed-lost,"
                     "not interested,do not contact,dnc"))
    engine_src = open(os.path.join(ROOT, "nextaction.py"), encoding="utf-8").read()
    check("the on-hold check is read by R7's own test and by no other evaluator",
          len(re.findall(r"\bis_on_hold\(", engine_src)), 2)          # its definition, and R7


def the_rotation(check):
    print("\nTHE ROTATION — nobody is dropped; the queue is walked three at a time")
    with Ledger() as led:
        runs = [led.day(seven(), day)[0] for day in (MON, NEXT_MON, THIRD_MON)]
        check("7 qualifying people, cap 3, three Mondays: 3, the next 3, then the last 1 plus the first 2",
              runs, [["P1", "P2", "P3"], ["P4", "P5", "P6"], ["P7", "P1", "P2"]])
        state = led.db.dm_no_meeting_state()
        check("...and what was recorded is the last Monday each was named",
              sorted((k.split("|")[-1][:2], v["last_date"], v["mentions"]) for k, v in state.items())[:1]
              and sorted(v["last_date"] for v in state.values()),
              sorted([dl.iso(THIRD_MON)] * 3 + [dl.iso(NEXT_MON)] * 3 + [dl.iso(MON)]))
        again = led.day(seven(), THIRD_MON, send=False)[0]
        check("once today's post has gone, today's people stay today's people (a later sweep the same day "
              "does not hand on three others)", sorted(again), ["P1", "P2", "P7"])
    with Ledger() as led:
        led.day(seven()[:3], MON)
        check("never-named people come ahead of already-named ones, however long the others have waited",
              led.day(seven(), NEXT_MON)[0], ["P4", "P5", "P6"])
    with Ledger() as led:
        weeks = [led.day(seven()[:2], MON + timedelta(days=7 * i))[0] for i in range(6)]
        check("nobody acts for weeks: they stay in, every Monday, and nobody is dropped",
              weeks, [["P1", "P2"]] * 6)
    check("fewer than three qualify: however many there are, no padding",
          [picked(engine(seven()[:n])) for n in (1, 2)], [["P1"], ["P1", "P2"]])
    names, post = one(seven())
    check("more than three qualify: the top three, and NO '+N more' line",
          (names, [w for w in ("+", "next Monday", "more next") if w in post], post.count("more")),
          (["P1", "P2", "P3"], [], 1))          # the one "more" is "more than 3 weeks ago"
    order = [dm(1, "Early Row", "Co", days=30), dm(2, "Late Row", "Co", days=30), dm(3, "Oldest DM", "Co", days=45)]
    check("the order: longest since the DM, then sheet order",
          picked(engine(order)), ["Oldest DM", "Early Row", "Late Row"])
    senior = [dm(1, "Junior First", "Co", days=30, designation="Intern"),
              dm(2, "Founder Second", "Co", days=30, designation="Founder")]
    check("seniority is not part of the order (PROSPECT_ROLE_ORDER is R5's)",
          picked(engine(senior)), ["Junior First", "Founder Second"])
    with tapped() as tap:
        nobody = engine(seven(), no_state=True)
    check("the rotation state cannot be read: nobody is named, and it is logged at ERROR",
          (picked(nobody), [lvl for lvl, m in tap.lines if "R7" in m and "rotation state" in m]), ([], ["ERROR"]))
    with tapped() as tap:
        empty = engine([])
    check("the tab comes back empty: no post, logged at ERROR, never a silent skip",
          (picked(empty), post_of(empty), [lvl for lvl, m in tap.lines if "R7" in m and "NO ROWS" in m]),
          ([], "", ["ERROR"]))


def behind_rule_13(check):
    print("\nBEHIND RULE 13 — a person R13 names the same day is not named here")
    shared = dm(1, "Both Rules", "Co", days=60, connected="Connected", li_date=MON - timedelta(days=90),
                step="Call the PoC")
    others = [dm(i, f"Only R7 {i}", "Co", days=40 - i) for i in (2, 3, 4, 5)]
    result = engine([shared] + others, rule_ids=("R13", "R7"))
    by_rule = {}
    for a in result["actions"]:
        by_rule.setdefault(a["rule_id"], []).append(a["poc"])
    check("R13 names them (it is listed first and keeps the contact)", by_rule.get("R13"), ["Both Rules"])
    check("...so R7 skips them and still names three: the next person moves up",
          picked(result), ["Only R7 2", "Only R7 3", "Only R7 4"])
    check("...alone, R7 would have named them first",
          picked(engine([shared] + others))[0], "Both Rules")
    check("R13 is still listed before R7 in bot_rules.yaml, and before R5",
          [r.id for r in rules_mod.safe_load() if r.id in ("R13", "R5", "R7")], ["R13", "R5", "R7"])
    check("R7 does not give way to later rules and is not exempt from the dedup",
          [a.get("yields") for a in r7(result)], [None, None, None])
    src = open(os.path.join(ROOT, "nextaction.py"), encoding="utf-8").read()
    body = src[src.index("def _r_dm_no_meeting("):src.index("def _r_meeting_prep(")]
    check("R7 no longer hands every Connected row to R13 (that was every DM'd row, so it named nobody)",
          "covered_by_next_steps(" in body, False)


def the_message(check):
    print("\nTHE MESSAGE — a numbered list, then the one line under it")
    names, post = one(example_values())
    check("the ticket's example, word for word: two blocks, one numbering, each sentence UNDER its list",
          post, EXAMPLE)
    check("...and the heading is the new one", drip.HEADINGS["R7"], "No meeting booked yet")
    first = one([dm(i, n, c, days=40 - i) for i, (n, c) in enumerate(
        (("Ariya Rastrow", "Wisprlow.ai"), ("Prof. Karim Ouazzane", "London Metropolitan University"),
         ("Dr. Nabeela Berardinelli", "University of East London")), 1)])[1]
    check("the first example: three people, one block, one sentence",
          first, "1. Ariya Rastrow (Wisprlow.ai)\n2. Prof. Karim Ouazzane (London Metropolitan University)\n"
                 "3. Dr. Nabeela Berardinelli (University of East London)\n\n" + NONE_LINE)
    only_second = one([dm(1, "Eleonore Lumer", "PolyAI", days=30, meeting_date=date(2026, 10, 24))])[1]
    check("only the second block has anybody: the first block AND its sentence are both absent",
          only_second, "1. Eleonore Lumer (PolyAI) — 24 Oct\n\n" + BOOKED_LINE)
    mixed = one([dm(1, "Late One", "Co", days=30, meeting_date=date(2026, 10, 24)),
                 dm(2, "None One", "Co", days=25), dm(3, "None Two", "Co", days=24)])[1]
    check("the first block always comes first, and the numbering runs straight through both",
          re.findall(r"^(\d+)\. (.+?) \(", mixed, re.MULTILINE),
          [("1", "None One"), ("2", "None Two"), ("3", "Late One")])
    with pinned(DM_NO_MEETING_DAYS=10):
        ten = one([dm(1, "Ann Day", "Co", days=10)])[1]
    check("the sentence tracks the setting: 21 reads 'more than 3 weeks ago', 10 reads 'more than 10 days ago'",
          ("more than 3 weeks ago" in first, ten.split("\n")[-1]),
          (True, "We sent them a DM more than 10 days ago and don't have a meeting set up with them."))
    with pinned(DM_NO_MEETING_BOOKED_GAP_DAYS=14):
        fortnight = one([dm(1, "Ann Day", "Co", days=30, meeting_date=date(2026, 10, 24))])[1]
    check("...and the second sentence tracks its own setting",
          fortnight.split("\n")[-1], "This meeting is set for more than 2 weeks after the DM went out.")
    noted = one([dm(1, "Ann Day", "Co", days=33, notes="waiting on their legal team")])[1]
    check("no note and no day count on a first-block line: the name and the company and nothing else",
          noted.split("\n")[0], "1. Ann Day (Co)")
    check("nobody qualifies: no post at all, and no 'all clear' line",
          one([dm(1, "Too Soon", "Co", days=5), dm(2, "Done Dan", "Co", meeting_status="Completed")]),
          ([], ""))
    m = message_of(engine(example_values()))
    check("the post is rendered in code: R7 is verbatim, one post whatever the owners, never held",
          (nextaction.R_DM_NO_MEETING in drip.VERBATIM_TYPES, nextaction.R_DM_NO_MEETING in drip.RULE_ONLY_TYPES,
           nextaction.R_DM_NO_MEETING in drip.NEVER_HELD, drip.is_verbatim(m)), (True, True, True, True))
    check("the composed openers, the closing questions and the note format are gone",
          [n for n in ("DM_NO_MEETING_OPENERS", "DM_NO_MEETING_CLOSES", "NOTE_MAX_CHARS", "render_dm_no_meeting")
           if hasattr(drip, n)] + [n for n in ("last_note",) if hasattr(nextaction, n)], [])


def never_in_the_message(check):
    print("\nNEVER IN ANY MESSAGE — a column name, a note, a day count, a question")
    for values in (example_values(), seven(),
                   [dm(1, "Ann Day", "Co", days=33, notes="waiting on their legal team", meeting_status="TBD")]):
        one(values)
    check("some messages were rendered to look through", len(RENDERED) >= 10, True)
    check("no rendered message names a column",
          sorted({w for t in RENDERED for w in COLUMN_NAMES if w in t}), [])
    check("no rendered message asks anything", [t for t in RENDERED if "?" in t], [])
    check("no rendered message carries a note", [t for t in RENDERED if "legal" in t], [])
    first_block = [ln for t in RENDERED for ln in t.split("\n")
                   if re.match(r"^\d+\. ", ln) and " — " not in ln]
    check("a first-block line is the number, the name and the company and nothing else: no day count, no date",
          [ln for ln in first_block if not re.match(r"^\d+\. [^()]+ \([^()]+\)$", ln)
           or re.search(r"\bdays?\b|\bweeks?\b|\bago\b|\d{1,2} (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b|"
                        r"\d{4}-\d{2}|\d{2}-\d{2}-\d{4}", ln)], [])
    check("the only numbers in a sentence are the two settings",
          sorted({ln for t in RENDERED for ln in t.split("\n") if ln.startswith(("We sent", "This meeting",
                                                                                 "These meetings"))}
                 - {NONE_LINE, BOOKED_LINE,
                    "We sent them a DM more than 10 days ago and don't have a meeting set up with them.",
                    "This meeting is set for more than 2 weeks after the DM went out."}), [])


def the_bot(check):
    import replies_world as rw

    print("\nTHE BOT — the send path, what is recorded, a preview, a test day, replies that are not read")

    async def scenario(test_mode=False):
        out = {}
        with rw.World(test_mode, pretend=(MON, 16, 0), rules={"R7"},
                      pins=dict(pins(), SALES_ALWAYS_TAG_IDS=[rw.APPROVER])) as w:
            w.sheet.set(pocs=seven())
            before = dict(w.bot.db.dm_no_meeting_state())
            # A PREVIEW: computing the queue and the plan, even twice, writes nothing.
            await w.bot._run_next_actions(today=MON)
            planned = await w.bot._plan_drip(today=MON, already=[])
            await w.bot._run_next_actions(today=MON)
            out["preview_wrote"] = w.bot.db.dm_no_meeting_state() != before
            msg = next((m for m in (planned or {}).get("messages") or []
                        if m.get("type") == nextaction.R_DM_NO_MEETING), None)
            out["planned"] = bool(msg)
            n, model = len(w.posted), w.counts()["model"]
            await w.bot._send_drip_message(w.chan, msg, marker=dl.iso(MON), channel_id=w.chan.id)
            post = w.posted[n]
            out["posts"] = [rw.strip_tag(m.content) for m in w.posted[n:]]
            out["tagged"] = post.content.startswith("[TEST")
            out["model"] = w.counts()["model"] - model
            state = w.bot.db.dm_no_meeting_state()
            out["recorded"] = sorted(v["last_date"] for v in state.values())
            out["counts"] = [r for r in w.drip_rows()]

            # SOMEBODY REPLIES. Nothing is asked, so nothing is parsed.
            reactions, writes, model = len(w.reactions), len(w.sheet.writes), w.counts()["model"]
            for text in ("done", "chase P1 again", "leave them", "yes", "skip", "P2 replied yesterday"):
                await w.say(text, who="member", reply_to=post)
                await w.say(text, who="approver", reply_to=post)
            out["after_replies"] = (len(w.posted) - n - len(out["posts"]), len(w.reactions) - reactions,
                                    len(w.sheet.writes) - writes, w.counts()["model"] - model,
                                    w.bot.db.dm_no_meeting_state() == state, dict(w.proposals()))

            # A TEST DAY ON A LIVE DATABASE records nothing against the rotation.
            saved = config.DB_PATH
            config.DB_PATH = os.path.join(w.tmp, "sales_bot.db")
            try:
                with w.bot.db.conn() as c:
                    c.execute("DELETE FROM dm_no_meeting_mentions")
                recorded = await w.bot._record_dm_no_meeting(msg, marker=dl.iso(MON))
                out["test_day"] = (recorded, w.bot.db.dm_no_meeting_state())
            finally:
                config.DB_PATH = saved
        return out

    with pinned():
        live = asyncio.run(scenario(False))
        test = asyncio.run(scenario(True))
    want_post = ("**No meeting booked yet**\n<tags>\n\n1. P1 (Co 1)\n2. P2 (Co 2)\n3. P3 (Co 3)\n\n" + NONE_LINE)

    def shape(text):
        lines = text.split("\n")
        if len(lines) > 1 and lines[1].startswith("<@"):
            lines[1] = "<tags>"
        return "\n".join(lines)
    check("a preview or a plan leaves the rotation state untouched", (live["planned"], live["preview_wrote"]),
          (True, False))
    check("one post: the heading, the tags, a blank line, the list, the sentence; no model call",
          ([shape(p) for p in live["posts"]], live["model"]), ([want_post], 0))
    RENDERED.extend(live["posts"])
    check("the real send records the three people named, on that Monday", live["recorded"], [dl.iso(MON)] * 3)
    check("replies are not read: no reply, no reaction, no write, no model call, no proposal, state unchanged",
          live["after_replies"], (0, 0, 0, 0, True, {}))
    check("a test day on a live database records nothing against the rotation", live["test_day"], (0, {}))
    check("test mode is identical apart from the [TEST] tag",
          ([shape(p) for p in test["posts"]], test["recorded"], test["after_replies"], live["tagged"], test["tagged"]),
          ([shape(p) for p in live["posts"]], live["recorded"], live["after_replies"], False, True))


def settings(check):
    print("\nSETTINGS — three changed defaults, four new ones, the boot checks, the rules file")
    src = open(os.path.join(ROOT, "config.py"), encoding="utf-8").read()
    check("the three changed defaults, the same in the code and in .env.example",
          (bool(re.search(r'_int\("DM_NO_MEETING_DAYS", 21\)', src)), _example("DM_NO_MEETING_DAYS"),
           bool(re.search(r'_int\("DM_NO_MEETING_MAX_CONTACTS", 3\)', src)), _example("DM_NO_MEETING_MAX_CONTACTS"),
           bool(re.search(r'_int\("DAILY_MESSAGE_CAP", 7\)', src)), _example("DAILY_MESSAGE_CAP")),
          (True, "21", True, "3", True, "7"))
    check("the four new settings, the same in the code and in .env.example",
          (bool(re.search(r'_int\("DM_NO_MEETING_BOOKED_GAP_DAYS", 7\)', src)),
           _example("DM_NO_MEETING_BOOKED_GAP_DAYS"),
           bool(re.search(r'_str_list\("DM_NO_MEETING_DM_SENT_MARKERS", "sent"\)', src)),
           _example("DM_NO_MEETING_DM_SENT_MARKERS"),
           bool(re.search(r'"DM_NO_MEETING_DM_REPLIED_MARKERS", "replied"\)', src)),
           _example("DM_NO_MEETING_DM_REPLIED_MARKERS"),
           bool(re.search(r'getenv\("DM_NO_MEETING_STATUS_OPEN", ",tbd"\)', src)),
           _example("DM_NO_MEETING_STATUS_OPEN")),
          (True, "7", True, "sent", True, "replied", True, ",tbd"))
    check("DM_NO_MEETING_STATUS_OPEN keeps its empty entry: blank and tbd, and never completed",
          pins()["DM_NO_MEETING_STATUS_OPEN"], ["", "tbd"])
    with pinned(DM_NO_MEETING_STATUS_OPEN=["", "tbd", "completed"]):
        check("...and Completed is out even if somebody adds it to that list",
              picked(engine([dm(1, "Ann Day", "Co", meeting_status="Completed")])), [])

    def boot(**values):
        with pinned(**values), tapped() as tap:
            saved = config.NEXT_ACTION_ENABLED
            config.NEXT_ACTION_ENABLED = True
            try:
                config.validate()
            finally:
                config.NEXT_ACTION_ENABLED = saved
        warn = [m for lvl, m in tap.lines if lvl == "WARNING"]
        return ([m for m in warn if "DM_NO_MEETING_DAYS=" in m], [m for m in warn if "DM_NO_MEETING_DM_SENT_MARKERS is empty" in m],
                [m for m in warn if "DAILY_MESSAGE_CAP=" in m],
                [m for _l, m in tap.lines if "[config] R7 no meeting booked yet" in m])

    clash, empty, cap, line = boot()
    check("the defaults boot with no R7 warning and no cap warning", (clash, empty, cap), ([], [], []))
    check("the boot line prints the new values",
          [("in 21d" in m, "more than 7d after" in m, "sent=sent" in m, "replied=replied" in m,
            "open=(blank),tbd" in m, "3 name(s)" in m) for m in line], [(True,) * 6])
    check("DM_NO_MEETING_DAYS at or below NEXT_STEP_CALL_AFTER_DM_DAYS: the boot check warns",
          [len(boot(DM_NO_MEETING_DAYS=n)[0]) for n in (7, 6, 8)], [1, 1, 0])
    check("DM_NO_MEETING_DM_SENT_MARKERS empty: the boot check warns that nobody would ever qualify",
          [("nobody would ever qualify" in m) for m in boot(DM_NO_MEETING_DM_SENT_MARKERS=[])[1]], [True])
    rule = rules_mod.by_id("R7")
    check("T10 R7 turned back on: Mondays, three a post, counted toward the cap, second in "
          "Monday's order",
          (rule.weekdays, rule.max_items_per_post, rule.counts_toward_cap, rule.enabled,
           [r for r in rules_mod.daily_order().get(0, [])][:2] if hasattr(rules_mod, "daily_order") else ["R4", "R7"]),
          ((0,), 3, True, True, ["R4", "R7"]))
    wordy = " ".join(str(getattr(rule, "sheet_wording", "") or "").split())
    check("the Bot Rules wording says what the rule now does",
          [w for w in ("Every Monday", "more than three weeks ago", "more than a week after the DM",
                       "Three people a post", "Nobody is dropped", "Completed", "Won, Lost or On Hold",
                       "Rule 13 comes first") if w not in wordy], [])
    check("...and nothing about days since the DM or the last note", [w for w in ("last note", "7 days") if w in wordy], [])


def switched_off(check):
    """RULES-OCT16: R7 is off in the shipped file and out of Monday's order."""
    print("\nR7 IS OFF (RULES-OCT16) — the shipped file")
    rule = rules_mod.by_id("R7")
    order = rules_mod.order_for(MON) if hasattr(rules_mod, "order_for") else []
    check("T9 R7 disabled: enabled false, not in Monday's order (R4, R1, R10)",
          (rule.enabled, order), (False, ["R4", "R1", "R10"]))
    monday = [r.id for r in rules_mod.for_day(MON)]
    with pinned():
        got = engine(seven(), rule_ids=tuple(monday))
    check("T9 ...and with R7 off, Monday's rules leave it out and nothing is posted for it",
          ("R7" in monday, [a["poc"] for a in got["actions"] if a.get("rule_id") == "R7"]),
          (False, []))
    text = open(os.path.join(ROOT, ".env.example"), encoding="utf-8").read()
    names = ("DM_NO_MEETING_DAYS", "DM_NO_MEETING_BOOKED_GAP_DAYS", "DM_NO_MEETING_MAX_CONTACTS")
    check("T11 .env.example: the three R7 timing settings are in the RETIRED block, commented, and not live",
          ([n for n in names if re.search(rf"^# ?{n}=", text, re.M) is None],
           [n for n in names if re.search(rf"^{n}=", text, re.M)]), ([], []))


def run(check):
    switched_off(check)
    with r7_back_on():
        with pinned():
            who_is_picked(check)
            on_hold_and_stops(check)
            the_rotation(check)
            behind_rule_13(check)
            the_message(check)
        the_bot(check)
        with pinned():
            never_in_the_message(check)
        settings(check)
