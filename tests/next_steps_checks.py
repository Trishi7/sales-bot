"""R13 "NEXT STEPS" AS A REMINDER (rebuilt 12 Oct 2026) — the checks, shared by verify_next_steps.py and
tests/test_next_steps.py.

    sys.path.insert(0, <repo>/tests); import next_steps_checks as nsx
    nsx.run(check)                 # check(name, got, want)

WHAT RUNS HERE IS THE REAL THING: made-up rows through the real sheet parser, the real rule
(`nextaction.run`), the real renderer (`drip`, `wording`), the real state table in a throwaway database
(`db.record_next_step_mention` / `next_step_state`), and for the send path a real bot behind a fake Discord
(`replies_world.World`). No network, no model, no real sheet.

EVERY R13 SETTING IS PINNED to its .env.example default, never read from this machine's .env.

THE PEOPLE ARE MADE UP, except the names the ticket's own example message uses and the four companies it says
have a Connected row with no LI Connected Date.
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

DAY = date(2026, 10, 12)                      # a Monday
OLD = date(2026, 9, 1)
COLUMN_NAMES = ("Email Sent", "Email Date", "Next Steps", "LI DM", "Prospect Status", "Meeting Date")
_ABSENT = object()


def _example(name: str) -> str:
    """A default from .env.example: its live line, or its line in the RETIRED block (R7's three timing
    settings, retired while R7 is off since 16 Oct — RULES-OCT16)."""
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


def _list(name: str) -> list:
    return [x.strip() for x in _example(name).split(",") if x.strip()]


def pins() -> dict:
    return {
        "NEXT_STEP_TIME": _example("NEXT_STEP_TIME"),
        "NEXT_STEP_CONNECTED_MARKERS": _list("NEXT_STEP_CONNECTED_MARKERS"),
        # R7's clock, so its boot check (against R13's call clock) reads the contract and not this machine's .env.
        "DM_NO_MEETING_DAYS": int(_example("DM_NO_MEETING_DAYS")),
        "NEXT_STEP_FIRST_DAYS": int(_example("NEXT_STEP_FIRST_DAYS")),
        "NEXT_STEP_AFTER_PREVIOUS_DAYS": int(_example("NEXT_STEP_AFTER_PREVIOUS_DAYS")),
        "NEXT_STEP_AFTER_EMAIL_DAYS": int(_example("NEXT_STEP_AFTER_EMAIL_DAYS")),
        "NEXT_STEP_CALL_AFTER_DM_DAYS": int(_example("NEXT_STEP_CALL_AFTER_DM_DAYS")),
        "NEXT_STEP_CALL_EVERY_DAYS": int(_example("NEXT_STEP_CALL_EVERY_DAYS")),
        "NEXT_STEP_CALL_UNTIL_DAYS": int(_example("NEXT_STEP_CALL_UNTIL_DAYS")),
        "NEXT_STEP_DM_REPLIED_MARKERS": _list("NEXT_STEP_DM_REPLIED_MARKERS"),
        "NEXT_STEP_DM_SENT_MARKERS": _list("NEXT_STEP_DM_SENT_MARKERS"),
        "NEXT_STEP_PAUSE_FOR_BOOKED_MEETING": True,
        "PROSPECT_VOICE_INDUSTRIES": _list("PROSPECT_VOICE_INDUSTRIES"),
        "PROSPECT_LAB_INDUSTRIES": _list("PROSPECT_LAB_INDUSTRIES"),
        "PROSPECT_KEYWORDS": _list("PROSPECT_KEYWORDS"),
        "PROSPECT_UK_MARKERS": _list("PROSPECT_UK_MARKERS"),
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

_N = [0]


def person(name, company, step="", *, li=OLD, n=None, **cells):
    """One Connected person. `li` is the LI Connected Date (None for a row with no date)."""
    _N[0] += 1
    more = dict(cells)
    if li is not None:
        more["li_date"] = li
    return fx.person(n or _N[0], name=name, company=company, step=step, first_contact="TRUE", **more)


def rows_of(values) -> list:
    return list(fx.parse(values).rows)


R13 = lambda: rules_mod.by_id("R13")  # noqa: E731


def engine(values, day=DAY, state=None, rule_ids=("R13",)):
    rows = rows_of(values)
    return nextaction.run(today=day, rows=rows, prospect_rows=rows,
                          day_rules=[rules_mod.by_id(r) for r in rule_ids],
                          next_step_state={} if state is None else state)


def post_of(result, day=DAY) -> str:
    for m in drip.group(result["actions"], day=day):
        if m.get("type") == nextaction.R_NEXT_STEPS:
            return drip.compose_fallback(m)
    return ""


def asks(result) -> list:
    return [(a["poc"], a["ask"]) for a in sorted(result["actions"], key=lambda a: a["pick_order"])
            if a.get("rule_id") == "R13"]


def ask_of(values, day=DAY, entry=None) -> tuple:
    row = rows_of(values)[0]
    f = nextaction.next_step_for(row, today=day, entry=entry)
    return f["status"], f["ask"], f["n"], f["due"]


class Ledger:
    """The real `next_step_followups` table in a throwaway database, written the way the bot's sender writes it."""

    def __enter__(self):
        from db import DB
        self.tmp = tempfile.mkdtemp(prefix="saley-r13-")
        self.db = DB(os.path.join(self.tmp, "r13_test.db"))
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.tmp, ignore_errors=True)
        return False

    def day(self, values, day, *, send=True) -> tuple:
        """(post text, result). With `send`, records who the post named, as `bot._record_next_steps` does."""
        result = engine(values, day, state=self.db.next_step_state())
        text = post_of(result, day)
        if send and text:
            msg = next(m for m in drip.group(result["actions"], day=day)
                       if m.get("type") == nextaction.R_NEXT_STEPS)
            for a in drip.shown_contacts(msg):
                self.db.record_next_step_mention(a["row_key"], signature=a["signature"], on_date=dl.iso(day),
                                                 ask=a["ask"])
        return text, result

    def state(self) -> dict:
        return self.db.next_step_state()


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


RENDERED: list = []          # every message any check rendered, for the "no column name" sweep


def keep(text: str) -> str:
    if text:
        RENDERED.append(text)
    return text


# -- the ticket's own example ----------------------------------------------------------------------------------------

def example_values(day=DAY) -> list:
    return [
        person("Arjun Aryaa", "Gnani.ai", "Send email 1", li=date(2026, 9, 3)),
        person("Oliver Shoulson", "PolyAI", "Send email 1", li=date(2026, 9, 10)),
        person("Ioana Mocanu", "Agoda", "Send email 1", e1="Yes", e1d=day - timedelta(days=9)),
        person("Angelos Perivolaropoulos", "ElevenLabs", "Reach by LI DM", dm_sent="Sent",
               dm_date=day - timedelta(days=8)),
        person("Ariya Rastrow", "Wisprflow.ai", "Reach by LI DM", dm_sent="Received"),
    ]


EXAMPLE = """The people below have replied to us! We need to set up a meeting with them:
- Ariya Rastrow (Wisprflow.ai)

We need to call these PoCs:
- Angelos Perivolaropoulos (ElevenLabs)

We sent the first email to these people over a week ago — time for the second:
- Ioana Mocanu (Agoda)

We need to send out the first email to these people:
- Arjun Aryaa (Gnani.ai)
- Oliver Shoulson (PolyAI)"""

GROUP_LINES = {
    "replied": "The people below have replied to us! We need to set up a meeting with them:",
    "call": "We need to call these PoCs:",
    "advance1": "We sent the first email to these people over a week ago — time for the second:",
    "advance2": "We sent the second email to these people over a week ago — time for the third:",
    "email1": "We need to send out the first email to these people:",
    "email2": "We need to send out the second email to these people:",
    "email3": "We need to send out the third email to these people:",
    "research": "We need to research these PoCs:",
    "blank": "We're connected with these people — next thing is to research them:",
    "unresponsive": "No reply after the calls. These look unresponsive:",
}
DROP_WORDING = ("stop reminding", "three weeks", "nothing has moved", "dropped", "last chance")


# -- the checks -----------------------------------------------------------------------------------------------------------

def who_is_in(check):
    print("\nWHO IS IN — every Connected row, dated or not; the usual stops")
    nodate = [person(f"Contact {i}", c, "", li=None) for i, c in enumerate(
        ("Middlesex University London", "London Metropolitan University", "University of East London",
         "Telekinesis AI"), 1)]
    res = engine(nodate)
    text = keep(post_of(res))
    check("the four Connected rows with no LI Connected Date appear in the post",
          [c for c in ("Middlesex University London", "London Metropolitan University",
                       "University of East London", "Telekinesis AI") if c in text], [
              "Middlesex University London", "London Metropolitan University", "University of East London",
              "Telekinesis AI"])
    check("...due straight away, under the 'connected' line, and still listed in the preview as undated",
          (text.split("\n")[0], len(res["reports"]["R13"]["no_date"])), (GROUP_LINES["blank"], 4))
    dmd = rows_of([person("No Date", "Co", "Reach by LI DM", li=None, dm_sent="Sent",
                          dm_date=DAY - timedelta(days=30))])[0]
    both = nextaction.run(today=DAY, rows=[dmd], prospect_rows=[dmd], next_step_state={}, dm_no_meeting_state={},
                          day_rules=[rules_mod.by_id("R13"), rules_mod.by_id("R7")])
    # CHANGED 13 Oct 2026: Rule 7 no longer hands every Connected row to Rule 13 up front (that was every DM'd
    # row, so it named nobody). What keeps them apart now is the day's dedup: Rule 13 is listed first.
    check("a Connected row with no date that both rules pick is named once, by Rule 13",
          (nextaction.covered_by_next_steps(dmd), [a["rule_id"] for a in both["actions"]]), (True, ["R13"]))
    not_connected = fx.values_row(900, company="Co", name="Stranger", step="Send email 1", connected="Requested",
                                  li_date=OLD)
    check("a row that is not Connected is not in", asks(engine([not_connected])), [])
    for label, cells in (("Prospect Status Unresponsive", {"prospect": "Unresponsive"}),
                         ("Deal Status Dead", {"deal": "Dead"}), ("Prospect Status Not interested",
                                                                   {"prospect": "Not interested"})):
        check(f"{label}: out, as for every rule", asks(engine([person("Sam Stop", "Co", "Send email 1", **cells)])), [])
    done = person("Mia Met", "Co", "Send email 1", meeting_date=DAY - timedelta(days=3), meeting_status="Completed")
    check("a Completed meeting leaves for good", (asks(engine([done])), engine([done])["reports"]["R13"]["completed"]),
          ([], 1))
    booked = person("Bo Booked", "Co", "Send email 1", meeting_date=DAY + timedelta(days=2))
    check("a meeting booked for today or later pauses the person",
          (asks(engine([booked])), len(engine([booked])["reports"]["R13"]["paused"])), ([], 1))
    happened = person("Hal Happened", "Co", "Send email 1", meeting_date=DAY - timedelta(days=3))
    check("a meeting that happened with Meeting Status blank: keeps reminding, following the sheet",
          asks(engine([happened])), [("Hal Happened", "email_out")])


def what_is_asked(check):
    print("\nWHAT EACH PERSON IS REMINDED ABOUT — the table, row by row")
    p = lambda step, **c: [person("Pat", "Co", step, **c)]  # noqa: E731
    week = DAY - timedelta(days=8)
    check("blank -> research them", ask_of(p(""))[:2], ("due", "blank"))
    check("Research the PoC -> research them", ask_of(p("Research the PoC"))[:2], ("due", "research"))
    check("Send email 1/2/3, not marked sent -> send that email",
          [ask_of(p(f"Send email {n}"))[:3] for n in (1, 2, 3)],
          [("due", "email_out", 1), ("due", "email_out", 2), ("due", "email_out", 3)])
    check("Send email 1/2, marked sent a week ago -> send the next email",
          [ask_of(p(f"Send email {n}", **{f"e{n}": "Yes", f"e{n}d": week}))[:3] for n in (1, 2)],
          [("due", "advance", 1), ("due", "advance", 2)])
    check("...not yet, inside the week",
          ask_of(p("Send email 1", e1="Yes", e1d=DAY - timedelta(days=3)))[0], "waiting")
    check("an email marked sent with NO date: the step is done, the next email is due now",
          ask_of(p("Send email 1", e1="Yes")), ("due", "advance", 1, DAY))
    check("Send email 3, marked sent a week ago -> nothing",
          ask_of(p("Send email 3", e3="Yes", e3d=week))[0], "silent")
    check("Reach by LI DM, LI DM Sent blank -> nothing (the known gap)",
          ask_of(p("Reach by LI DM"))[0], "silent")
    check("...'Not sent' is not 'sent'", ask_of(p("Reach by LI DM", dm_sent="Not sent"))[0], "silent")
    check("LI DM Sent says sent, NO LI DM Date -> a call, due today",
          ask_of(p("Reach by LI DM", dm_sent="sent")), ("due", "call", 0, DAY))
    check("...'Yes', 'Done' and a ticked box read as sent too",
          [ask_of(p("Reach by LI DM", dm_sent=v))[1] for v in ("Yes", "Done", "TRUE")], ["call"] * 3)
    check("Call the PoC is the call stage, with or without a DM logged",
          [ask_of(p("Call the PoC", **c))[:2] for c in ({}, {"dm_sent": "Sent"})],
          [("due", "call"), ("due", "call")])
    check("LI DM Sent says Replied -> the replied line, never the call line, for both steps",
          [ask_of(p(step, dm_sent="Replied", dm_date=DAY - timedelta(days=40)))[:2]
           for step in ("Reach by LI DM", "Call the PoC")], [("due", "replied"), ("due", "replied")])
    dated = p("Reach by LI DM", dm_sent="Sent", dm_date=DAY - timedelta(days=5))
    check("with an LI DM Date the call waits its seven days",
          (ask_of(dated)[0], ask_of(dated)[3]), ("waiting", DAY + timedelta(days=2)))
    check("the call ladder ran out (more than 21 days since the DM) -> unresponsive",
          ask_of(p("Reach by LI DM", dm_sent="Sent", dm_date=DAY - timedelta(days=22)))[:2],
          ("due", "unresponsive"))
    with tapped() as tap:
        res = engine(p("ping them on Friday"))
    check("free text in Next Steps: skipped, no guessed ask, and logged",
          (asks(res), len(res["reports"]["R13"]["unknown_step"]),
           any("Next Steps value I don't know" in m for _l, m in tap.lines)), ([], 1, True))
    row = rows_of(p("Reach by LI DM", dm_sent="Sent", dm_date=DAY - timedelta(days=10)))[0]
    sig = nextaction.step_signature(row)
    edited = dict(row, li_dm_sent="yes")
    entry = {"signature": sig, "calls": 2, "last_call_date": dl.iso(DAY - timedelta(days=1))}
    check("LI DM Sent edited part-way through the calls: a new signature, so the call count starts again",
          (nextaction.next_step_for(row, today=DAY, entry=entry)["status"],
           nextaction.next_step_for(edited, today=DAY, entry=entry)["status"],
           nextaction.step_signature(edited) == sig), ("waiting", "due", False))


def the_message(check):
    print("\nTHE MESSAGE — the agreed lines, one name a line")
    text = post_of(engine(example_values()))
    check("the ticket's example, character for character (the heading and tags go on at send)", keep(text), EXAMPLE)

    every = [{"who": f"P{i} (Co)", "ask": ask, "n": n} for i, (ask, n) in enumerate(reversed([
        ("replied", 0), ("call", 0), ("advance", 1), ("advance", 2), ("email_out", 1), ("email_out", 2),
        ("email_out", 3), ("research", 0), ("blank", 0), ("unresponsive", 0)]))]
    lines = wording.next_step_post(every)
    keep("\n".join(lines))
    check("the ten group lines, word for word, in the agreed order whatever order people arrive in",
          [ln for ln in lines if ln and not ln.startswith("- ")], list(GROUP_LINES.values()))
    check("...they live in wording.py and nowhere else", [line for _k, line in wording.NEXT_STEP_GROUPS],
          list(GROUP_LINES.values()))
    check("a blank line between groups and none after the last", (lines.count(""), lines[-1].startswith("- ")),
          (9, True))
    check("each person is ONE line, '- Name (Company)', and nothing else",
          [ln for ln in "\n".join([text] + lines).split("\n")
           if ln.startswith("- ") and not re.fullmatch(r"- [^():]+( \([^()]+\))+", ln)], [])
    check("ordinal words, not digits, and no opener sentence",
          (re.findall(r"email \d", "\n".join(GROUP_LINES.values())), text.split("\n")[0]),
          ([], GROUP_LINES["replied"]))
    check("nobody due: no post at all",
          post_of(engine([person("Wai Ting", "Co", "Send email 1", li=DAY)])), "")
    old_layout = [dict(r) for r in rows_of([person("Pat", "Co", "Send email 1")])]
    for r in old_layout:
        for role in nextaction.NEXT_STEP_REQUIRED_ROLES:
            r.pop(role, None)
    with tapped() as tap:
        res = nextaction.run(today=DAY, rows=old_layout, prospect_rows=old_layout, day_rules=[R13()],
                             next_step_state={})
    check("the Next Steps column renamed or missing: nothing posted, and the log names the columns",
          (res["actions"], res["reports"]["R13"]["state"],
           any("no column mapped to outreach_step" in m for _l, m in tap.lines)), ([], "no_step_column", True))
    check("the state cannot be read: nobody is named",
          nextaction.run(today=DAY, rows=rows_of(example_values()), day_rules=[R13()])["actions"], [])


def the_order(check):
    print("\nTHE ORDER — six tiers; inside a tier whoever was named least recently")
    week = DAY - timedelta(days=8)
    one_each = [
        person("F Unresponsive", "Co F", "Reach by LI DM", dm_sent="Sent", dm_date=DAY - timedelta(days=22)),
        person("E Research", "Co E", "Research the PoC"),
        person("D Email", "Co D", "Send email 2"),
        person("C Advance", "Co C", "Send email 1", e1="Yes", e1d=week),
        person("B Call", "Co B", "Call the PoC"),
        person("A Replied", "Co A", "Reach by LI DM", dm_sent="Replied"),
    ]
    res = engine(one_each, rule_ids=("R13",))
    report = res["reports"]["R13"]
    order = [e["poc"] for e in report["picked"]] + [e["poc"] for e in report["queued"]]
    check("exactly six tiers, with somebody in each: replied, call, next email due, email not sent, "
          "research, unresponsive",
          order, ["A Replied", "B Call", "C Advance", "D Email", "E Research", "F Unresponsive"])
    check("...numbered 1 to 6, and there is no seventh",
          ([a["tier"] for a in sorted(res["actions"], key=lambda a: a["pick_order"])],
           (nextaction.TIER_REPLIED, nextaction.TIER_CALL, nextaction.TIER_ADVANCE, nextaction.TIER_EMAIL,
            nextaction.TIER_RESEARCH, nextaction.TIER_UNRESPONSIVE),
           [n for n in ("TIER_LAST_CHANCE", "LAST_CHANCE_DAYS", "DROP_LINE_MAX", "ASK_DROPPED")
            if hasattr(nextaction, n)]),
          ([1, 2, 3, 4, 5], (1, 2, 3, 4, 5, 6), []))

    dated = [person("Newest", "Co", "Send email 1", li=date(2026, 10, 1), n=1),
             person("No Date B", "Co", "Send email 1", li=None, n=2),
             person("Oldest", "Co", "Send email 1", li=date(2026, 9, 3), n=3),
             person("No Date A", "Co", "Send email 1", li=None, n=4),
             person("Middle", "Co", "Send email 1", li=date(2026, 9, 20), n=5)]
    check("equally recently named: oldest LI Connected Date first; rows with no date last, in sheet order",
          [n for n, _a in asks(engine(dated))], ["Oldest", "Middle", "Newest", "No Date B", "No Date A"])
    tie = [person("Plain Pat", "Plain Logistics", "Send email 1", li=date(2026, 9, 3), n=1, industry="Logistics"),
           person("Voice Vic", "Voice Co", "Send email 1", li=date(2026, 9, 3), n=2, industry="AI Voice Agents"),
           person("Lab Lou", "Lab Co", "Send email 1", li=date(2026, 9, 3), n=3, industry="AI Labs (frontier)")]
    check("...the same date: R5's company score decides (voice, then lab, then nothing), then sheet order",
          [n for n, _a in asks(engine(tie))], ["Voice Vic", "Lab Lou", "Plain Pat"])
    import prospects
    scores = nextaction.company_scores(rows_of(tie + [
        fx.person(50, name="W1", company="Warm Co", first_contact="TRUE"),
        fx.person(51, name="W2", company="Warm Co", first_contact="TRUE")]))
    check("the company score uses R5's own weights: warm 32, voice 16, lab 8",
          [scores[prospects.company_key(c)] for c in ("Warm Co", "Voice Co", "Lab Co", "Plain Logistics")],
          [prospects.WEIGHTS["warm"], prospects.WEIGHTS["voice"], prospects.WEIGHTS["lab"], 0])
    rows = rows_of(dated)
    newest = next(r for r in rows if r["name"] == "Newest")
    oldest = next(r for r in rows if r["name"] == "Oldest")
    state = {nextaction._contact_key(oldest): {"signature": nextaction.step_signature(oldest),
                                               "last_date": dl.iso(DAY - timedelta(days=1))},
             nextaction._contact_key(newest): {"signature": nextaction.step_signature(newest),
                                               "last_date": dl.iso(DAY - timedelta(days=3))}}
    check("never-named people come AHEAD of anybody already named; then the least recently named",
          [n for n, _a in asks(engine(dated, state=state))],
          ["Middle", "No Date B", "No Date A", "Newest", "Oldest"])
    check("Priority (AF) is read and shown, and not used for the order",
          ("poc_priority" in engine(dated)["actions"][0],
           "poc_priority" in nextaction._next_step_order.__code__.co_consts), (True, False))


def the_rotation(check):
    print("\nTHE ROTATION — nobody is dropped; each tier is walked, least recently named first")
    twelve = [person(f"P{i:02d}", f"Co {i:02d}", "Send email 1", li=date(2026, 9, i)) for i in range(1, 13)]
    with Ledger() as led:
        runs = []
        for offset in (0, 1, 2):
            text, res = led.day(twelve, DAY + timedelta(days=offset))
            runs.append([n for n, _a in asks(res)])
            keep(text)
        after = engine(twelve, DAY + timedelta(days=2), state=led.state())
        before = dict(led.state())
        engine(twelve, DAY + timedelta(days=3), state=led.state())
        post_of(engine(twelve, DAY + timedelta(days=3), state=led.state()))
        preview_same = led.state() == before
    check("twelve people in one tier, five a post: 5, then the next 5, then the last 2 and the first 3",
          runs, [[f"P{i:02d}" for i in range(1, 6)], [f"P{i:02d}" for i in range(6, 11)],
                 ["P11", "P12", "P01", "P02", "P03"]])
    check("once today's post has gone, today's people stay today's people",
          sorted(n for n, _a in asks(after)), sorted(runs[2]))
    check("a preview (computing the queue, rendering the post) writes no state", preview_same, True)

    sol = [person("Sol Solo", "Solo Co", "Send email 1")]
    with Ledger() as led:
        led.day(sol, DAY - timedelta(days=25))
        text, res = led.day(sol, DAY)
        row = dict(led.state()[next(iter(led.state()))])
    keep(text)
    check("named 25 days ago with nothing moved: still named, normally",
          text, GROUP_LINES["email1"] + "\n- Sol Solo (Solo Co)")
    check("...and nothing in the state closes them or starts a clock",
          sorted(row), ["calls", "last_ask", "last_call_date", "last_date", "mentions", "signature"])

    with Ledger() as led:
        led.day(sol, DAY - timedelta(days=5))
        moved = [person("Sol Solo", "Solo Co", "Send email 2", e1="Yes", e1d=DAY - timedelta(days=4))]
        others = [person(f"Other {i}", f"Other Co {i}", "Send email 2", li=date(2026, 8, i + 1)) for i in range(6)]
        for r in rows_of(others):
            led.db.record_next_step_mention(nextaction._contact_key(r), signature=nextaction.step_signature(r),
                                            on_date=dl.iso(DAY - timedelta(days=9)), ask="email_out")
        _t, res = led.day(moved + others, DAY)
    check("the list moves when the sheet moves: a person whose step changed is new again, and goes first",
          asks(res)[0], ("Sol Solo", "email_out"))

    dm_day = date(2026, 9, 1)
    ladder = [person("Cal Ladder", "Ladder Co", "Reach by LI DM", dm_sent="Sent", dm_date=dm_day)]
    with Ledger() as led:
        seen = []
        for offset in (7, 10, 21, 22, 23, 26, 40):
            text, _r = led.day(ladder, dm_day + timedelta(days=offset))
            seen.append((offset, keep(text).split("\n")[0] if text else ""))
        marked = [person("Cal Ladder", "Ladder Co", "Reach by LI DM", dm_sent="Sent", dm_date=dm_day,
                         prospect="Unresponsive")]
        t_marked, _r = led.day(marked, dm_day + timedelta(days=41))
    check("the call ladder runs out on day 22, and THE UNRESPONSIVE LINE COMES ROUND AGAIN on later runs",
          seen, [(7, GROUP_LINES["call"]), (10, GROUP_LINES["call"]), (21, GROUP_LINES["call"]),
                 (22, GROUP_LINES["unresponsive"]), (23, GROUP_LINES["unresponsive"]),
                 (26, GROUP_LINES["unresponsive"]), (40, GROUP_LINES["unresponsive"])])
    check("...until Prospect Status says Unresponsive, which is what ends it", t_marked, "")

    nodate = [person("Dee Nodate", "Nodate Co", "Reach by LI DM", dm_sent="Sent")]
    with Ledger() as led:
        seen = []
        for offset in (0, 1, 3, 30):
            text, _r = led.day(nodate, DAY + timedelta(days=offset))
            seen.append((offset, keep(text).split("\n")[0] if text else ""))
    check('LI DM Sent "Sent" with NO LI DM Date: a call, due today, again every three days, and never dropped',
          seen, [(0, GROUP_LINES["call"]), (1, ""), (3, GROUP_LINES["call"]), (30, GROUP_LINES["call"])])
    received = [person("Rae Received", "Co", "Reach by LI DM", dm_sent="Received")]
    check('LI DM Sent "Received": the meeting line, NOT a call reminder and NOT "the DM has not gone out"',
          (keep(post_of(engine(received))), ask_of(received)[:2]),
          (GROUP_LINES["replied"] + "\n- Rae Received (Co)", ("due", "replied")))
    check('..."received" is in both marker lists, and the replied list is the one checked first',
          ("received" in config.NEXT_STEP_DM_SENT_MARKERS, "received" in config.NEXT_STEP_DM_REPLIED_MARKERS,
           nextaction.dm_sent(rows_of(received)[0]), nextaction.dm_replied(rows_of(received)[0])),
          (True, True, True, True))
    check("no rendered line mentions a date",
          [t for t in RENDERED if re.search(r"\b\d{1,2} (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b|"
                                            r"\d{4}-\d{2}-\d{2}|\d{2}-\d{2}-\d{4}", t)], [])


def the_bot(check):
    import replies_world as rw

    print("\nTHE BOT — the send path, what is recorded, and replies that are not read")

    async def scenario(test_mode=False):
        out = {}
        with rw.World(test_mode, pretend=(DAY, 15, 0), rules={"R13"}, pins=pins()) as w:
            w.sheet.set(pocs=example_values())
            planned = await w.bot._plan_drip(today=DAY, already=[])
            msg = next(m for m in (planned or {}).get("messages") or []
                       if m.get("type") == nextaction.R_NEXT_STEPS)
            before = dict(w.bot.db.next_step_state())
            # A PREVIEW: computing the queue, even twice, writes nothing.
            await w.bot._run_next_actions(today=DAY)
            out["preview_wrote"] = w.bot.db.next_step_state() != before
            n = len(w.posted)
            await w.bot._send_drip_message(w.chan, msg, marker=dl.iso(DAY), channel_id=w.chan.id)
            post = w.posted[n]
            out["posts"] = [rw.strip_tag(m.content) for m in w.posted[n:]]
            out["tagged"] = post.content.startswith("[TEST")
            state = w.bot.db.next_step_state()
            out["recorded"] = sorted((k.split("|")[-1], v["last_date"], v["last_ask"]) for k, v in state.items())
            with w.bot.db.conn() as c:
                out["post_rows"] = c.execute("SELECT COUNT(*) FROM next_step_posts").fetchone()[0]
            out["counts_toward_cap"] = [r for r in w.drip_rows()]

            # SOMEBODY REPLIES "done".
            reactions, writes, model = len(w.reactions), len(w.sheet.writes), w.counts()["model"]
            for text in ("done", "Done for Arjun", "sent it yesterday", "ok", "yes", "thanks, will do"):
                await w.say(text, who="member", reply_to=post)
                await w.say(text, who="approver", reply_to=post)
            out["after_done"] = (len(w.posted) - n - len(out["posts"]), len(w.reactions) - reactions,
                                 len(w.sheet.writes) - writes, w.counts()["model"] - model,
                                 w.bot.db.next_step_state() == state, dict(w.proposals()))
            out["question"] = w.bot._statement_under_next_steps(
                {"direct_is_bot": True, "drip": {"action_type": nextaction.R_NEXT_STEPS}},
                "who is Arjun's manager?")

            # A TEST DAY ON A LIVE DATABASE records nothing.
            saved = config.DB_PATH
            config.DB_PATH = os.path.join(w.tmp, "sales_bot.db")
            try:
                with w.bot.db.conn() as c:
                    c.execute("DELETE FROM next_step_followups")
                recorded = await w.bot._record_next_steps(msg, sent=post, marker=dl.iso(DAY))
                out["test_day"] = (recorded, w.bot.db.next_step_state())
            finally:
                config.DB_PATH = saved
            out["has_reply_plumbing"] = [name for name in ("_maybe_next_step_reply", "_is_next_step_reply")
                                         if hasattr(w.bot, name)]
        return out

    with pinned():
        live = asyncio.run(scenario(False))
        test = asyncio.run(scenario(True))

    text = keep(live["posts"][0] if live["posts"] else "")
    lines = text.split("\n")
    body = "\n".join(lines[1:]).lstrip("\n")
    check("ONE post: the bold heading, then the groups (no opener sentence)",
          (len(live["posts"]), lines[0], body.split("\n")[0]),
          (1, "**Next steps**", GROUP_LINES["replied"]))
    check("...the groups as the rule rendered them",
          body.split("\n\n")[0].split("\n")[1], "- Ariya Rastrow (Wisprflow.ai)")
    check("test mode: the same message, tagged", (test["posts"], test["tagged"], live["tagged"]),
          (live["posts"], True, False))
    check("a preview (computing the queue) writes nothing to the reminder state", live["preview_wrote"], False)
    check("after a real send each person is recorded with the day they were named and what for",
          [(n, d) for n, d, _a in live["recorded"]], [(n, dl.iso(DAY)) for n in sorted(
              ("angelos perivolaropoulos", "ariya rastrow", "arjun aryaa", "ioana mocanu", "oliver shoulson"))])
    check("...and nothing is written to next_step_posts any more", live["post_rows"], 0)
    check('"done", "sent it", "ok", "yes" under the post, from a member and an approver: no reply, no '
          "reaction, nothing written, no model call, nothing recorded, no proposal",
          live["after_done"], (0, 0, 0, 0, True, {}))
    check("a real question under the post is still a question", live["question"], False)
    check("a test day on a live database leaves the rotation untouched",
          live["test_day"], (0, {}))
    check("the reply plumbing is gone from the bot", live["has_reply_plumbing"], [])
    import replies
    check("...and from replies.py",
          [n for n in ("next_step_done", "next_step_all", "next_step_shorts", "next_step_named",
                       "next_step_candidates") if hasattr(replies, n)], [])
    check("...and from wording.py",
          [n for n in ("next_step_done_line", "next_step_which", "next_step_detail", "_also_missing",
                       "CELL_MEETING_DATE") if hasattr(wording, n)], [])


def no_column_names(check):
    print("\nNO COLUMN NAME AND NO DROP WORDING IN ANY MESSAGE")
    everything = list(RENDERED) + wording.next_step_lines_for_check()
    check("something was rendered to look at", len(RENDERED) > 10, True)
    for name in COLUMN_NAMES:
        check(f'"{name}" appears in no rendered message and no line the post can carry',
              [t for t in everything if name in t], [])
    check('no rendered message says "stop reminding", or carries any other drop wording',
          [(w, t[:50]) for t in everything for w in DROP_WORDING if w in t.lower()], [])


def settings(check):
    print("\nSETTINGS — the two new ones, the boot check, the rules file")
    src = open(os.path.join(ROOT, "config.py"), encoding="utf-8").read()
    check("NEXT_STEP_DROP_AFTER_DAYS is retired: not read by the code, and only in the RETIRED block",
          ("NEXT_STEP_DROP_AFTER_DAYS =" in src, hasattr(config, "NEXT_STEP_DROP_AFTER_DAYS"),
           [ln[:1] for ln in open(os.path.join(ROOT, ".env.example"), encoding="utf-8")
            if "NEXT_STEP_DROP_AFTER_DAYS=" in ln]), (False, False, ["#"]))
    check("the DM markers: sent,received and replied,responded,received, in the code and in .env.example",
          (bool(re.search(r'"NEXT_STEP_DM_SENT_MARKERS", "sent,received"', src)),
           _example("NEXT_STEP_DM_SENT_MARKERS"),
           bool(re.search(r'"NEXT_STEP_DM_REPLIED_MARKERS", "replied,responded,received"', src)),
           _example("NEXT_STEP_DM_REPLIED_MARKERS")),
          (True, "sent,received", True, "replied,responded,received"))

    def warnings(**values):
        with pinned(**values), tapped() as tap:
            saved = config.NEXT_ACTION_ENABLED
            config.NEXT_ACTION_ENABLED = True
            try:
                config.validate()
            finally:
                config.NEXT_ACTION_ENABLED = saved
        return [m for lvl, m in tap.lines if lvl == "WARNING" and "NEXT_STEP_" in m], \
               [m for _l, m in tap.lines if "R13 next steps" in m]

    quiet, boot = warnings()
    check("the defaults raise no R13 warning at boot", quiet, [])
    check("the boot line says nobody is dropped and prints the marker lists",
          [("nobody is dropped, each tier rotates" in m, "dm_sent=sent,received" in m,
            "replied=replied,responded,received" in m, "DROPPED" in m) for m in boot],
          [(True, True, True, False)])
    none, _b = warnings(NEXT_STEP_DM_SENT_MARKERS=[])
    check("an empty NEXT_STEP_DM_SENT_MARKERS warns",
          [("NEXT_STEP_DM_SENT_MARKERS is empty" in m) for m in none], [True])
    rule = rules_mod.by_id("R13")
    order = [r.id for r in rules_mod.safe_load()]
    check("R13 is still listed before R5 (it wins a shared contact), five a post, outside the cap",
          (order.index("R13") < order.index("R5"), rule.max_items_per_post, rule.counts_toward_cap),
          (True, 5, False))
    wordy = str(getattr(rule, "sheet_wording", "") or "")
    check("the Bot Rules wording says: a reminder, five a day, nobody dropped, never fills cells, never replies",
          [w for w in ("reminder", "5 people", "Nobody is dropped", "never fills", "does not reply")
           if w not in wordy], [])
    check("...and no longer says anybody is dropped after three weeks",
          [w for w in ("three weeks", "stop reminding") if w in wordy], [])
    check("...and no longer describes asking for cells",
          [w for w in ("Has it gone out", "mark 1st Email Sent", "set Next Steps to") if w in wordy], [])


def run(check):
    with pinned():
        who_is_in(check)
        what_is_asked(check)
        the_message(check)
        the_order(check)
        the_rotation(check)
    the_bot(check)
    no_column_names(check)
    settings(check)
