"""R14 "RECONNECT DATE FOLLOW-UP" (NFT2-1086) — the checks, shared by verify_reconnect.py and
tests/test_reconnect.py.

    sys.path.insert(0, <repo>/tests); import reconnect_checks as rcx
    rcx.run(check)                 # check(name, got, want)

THE 35 NUMBERED CASES ARE THE TICKET'S OWN TABLE, one check each, named with their number so the output maps
1-to-1 onto it. Everything after them (the measured shape, the column lock, the settings, the edge cases the table
does not number) is named without a leading number.

WHAT RUNS HERE IS THE REAL THING: made-up rows through the real sheet parser, the real rule (`nextaction.run`,
`reconnect.survey`), the real planner and renderer (`drip`, `wording`), the real tables in a throwaway database, and
for the send and the reply a real bot behind a fake Discord (`replies_world.World`): `bot._plan_drip`,
`bot._send_drip_message`, and a "yes" through the real `on_message`. No network, no model, no real sheet. The shape
of the real tab (670 rows with a company, 528 without a date anywhere...) is a FIXTURE with that shape; nothing here
reads the real sheet.

Every setting is pinned to its .env.example default, never read from this machine's .env.

THE PEOPLE ARE MADE UP (Ann, Bob, "Prof. Alexandra Montgomery-Whitfield 17"...), and so are the companies.
DATES: "today" is Tuesday 13 Oct 2026, the day the ticket measured the sheet.
"""
import asyncio
import contextlib
import logging
import os
import re
import subprocess
import sys
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)
os.environ.setdefault("DISCORD_TOKEN", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import canned_sheet  # noqa: E402
import config  # noqa: E402
import deadlines as dl  # noqa: E402
import drip  # noqa: E402
import gtm_sheet  # noqa: E402
import nextaction  # noqa: E402
import packages_checks as pkc  # noqa: E402
import reconnect  # noqa: E402
import rule13_fixtures as fx  # noqa: E402
import rules as rules_mod  # noqa: E402
import wording  # noqa: E402

TUE = date(2026, 10, 13)                       # the day the ticket measured the sheet (a Tuesday)
SAT, SUN, MON = date(2026, 10, 17), date(2026, 10, 18), date(2026, 10, 19)
API = 5555                                     # the API Sales channel id in the fake Discord
HEADERS = list(canned_sheet.POCS_HEADERS) + ["Reconnect Date"]
KEYS = {"reconnect": "Reconnect Date", "meeting": "Meeting Date", "mstatus": "Meeting Status",
        "fc": "First Contact Date", "lic": "LI Connected Date", "lidm": "LI DM Date",
        "e1": "1st Email Date", "e2": "2nd Email Date", "e3": "3rd Email Date", "deal": "Deal Status",
        "connected": "Sid - LI Addition", "step": "Next Steps", "e1s": "1st Email Sent",
        "first_contact": "First Contact"}
GREETING = ("Hey <@111>, <@222> — it's been a while since we last connected with these PoCs, "
            "do get in touch when you can:")
GREETING_ONE = ("Hey <@111>, <@222> — it's been a while since we last connected with this PoC, "
                "do get in touch when you can:")
HEADING = "**Time to reconnect**"
REST_HEAD = "everyone due to reconnect today:"
_ABSENT = object()

# The schedule settings at their shipped defaults, so the planner's answers do not depend on this machine's .env.
SCHEDULE = {
    "SALES_DRIP_START": "14:00", "SALES_DRIP_END": "20:00", "MESSAGE_GAP_MINUTES": 120,
    "MESSAGE_GAP_MIN_MINUTES": 120, "MESSAGE_JITTER_MINUTES": 0, "DRIP_WEEKDAYS_ONLY": True,
    "SUNDAY_RULE_IDS": ["R4"], "DRIP_REASK_DAYS": 2, "MEETING_DAYOF_TIME": "10:00",
    "MEETING_PREP_TIME": "14:00", "NEXT_STEP_TIME": "15:00", "NEXT_ACTION_WEEKEND_SHIFT": True,
    "SALES_DMS_ENABLED": False,
}


def _example(name: str, fallback=None) -> str:
    for line in open(os.path.join(ROOT, ".env.example"), encoding="utf-8"):
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip()
    if fallback is not None:
        return fallback
    raise AssertionError(f"{name} is not in .env.example")


def pins() -> dict:
    out = dict(SCHEDULE)
    out["DAILY_MESSAGE_CAP"] = int(_example("DAILY_MESSAGE_CAP"))
    out["RECONNECT_TIME"] = _example("RECONNECT_TIME", "15:30")
    return out


@contextlib.contextmanager
def pinned(**extra):
    values = dict(pins(), **extra)
    before = {k: getattr(config, k, _ABSENT) for k in values}
    for k, v in values.items():
        setattr(config, k, v)
    rules_mod.reload()
    try:
        yield
    finally:
        for k, v in before.items():
            if v is _ABSENT:
                with contextlib.suppress(AttributeError):
                    delattr(config, k)
            else:
                setattr(config, k, v)
        rules_mod.reload()


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


# -- rows -------------------------------------------------------------------------------------------------------------

def vrow(n, name, company, **cells) -> list:
    """One Outreach PoCs row as the sheet would hold it, by short key. Dates are the text the team types."""
    r = [""] * len(HEADERS)
    r[0] = str(n)
    r[HEADERS.index("Company/Uni")] = company
    r[HEADERS.index("Name")] = name
    for key, v in cells.items():
        r[HEADERS.index(KEYS[key])] = v if isinstance(v, str) else str(v)
    return r


def parsed(values, headers=None) -> list:
    """Rows through the REAL sheet parser (roles, not letters), as `nextaction.run` is handed them."""
    tab = gtm_sheet.SHEETS._parse_values("Outreach PoCs", [list(headers or HEADERS)] + [list(r) for r in values],
                                         read_at=dl.real_epoch())
    return list(tab.rows) if tab is not None else []


def one(**cells) -> dict:
    """ONE parsed row (Ann, Acme) with the given cells."""
    return parsed([vrow(1, "Ann", "Acme", **cells)])[0]


def fires_on(rows, *, first=date(2026, 9, 1), last=date(2027, 12, 31), fired=None) -> list:
    """Every day in the range on which `reconnect.due_today` names somebody (shift: weekend to Monday)."""
    out, d = [], first
    while d <= last:
        if reconnect.due_today(rows, today=d, already_fired=fired):
            out.append(d)
        d += timedelta(days=1)
    return out


def r14_engine(rows, day=TUE, state=None, extra_rules=(), **kw):
    return nextaction.run(today=day, rows=[], reconnect_rows=rows, reconnect_state={} if state is None else state,
                          day_rules=[rules_mod.by_id(r) for r in (*extra_rules, "R14")], **kw)


def r14_items(result) -> list:
    return sorted((a for a in result["actions"] if a.get("rule_id") == "R14"), key=lambda a: a["pick_order"])


def r14_message(result, day=TUE):
    return next((m for m in drip.group(result["actions"], day=day) if m.get("type") == nextaction.R_RECONNECT), None)


def r14_text(result, day=TUE) -> str:
    m = r14_message(result, day)
    return drip.compose_fallback(m) if m else ""


def names_of(result) -> list:
    return [a["poc"] for a in r14_items(result)]


# -- the fake Discord world --------------------------------------------------------------------------------------------

def world_pins(**extra) -> dict:
    import replies_world as rw
    out = dict(pins())
    out.update({"SALES_API_CHANNEL_ID": API, "SALES_CHANNEL_IDS": [rw.CHANNEL, API],
                "SALES_CHANNEL_ID_SET": {rw.CHANNEL, API}, "SALES_ALWAYS_TAG_IDS": [rw.APPROVER, rw.APPROVER2]})
    out.update(extra)
    return out


@contextlib.contextmanager
def rworld(values=(), *, test_mode=False, day=TUE, reachable=True, **extra_pins):
    """A real bot behind a fake Discord with TWO channels: the usual one (`w.chan`) and the API Sales channel
    (`w.api`), a throwaway database, and `values` as the Outreach PoCs tab. Only R14 is due."""
    import replies_world as rw
    with rw.World(test_mode, pretend=(day, 15, 30), rules={"R14"}, pins=world_pins(**extra_pins)) as w:
        w.api = rw.Chan(w, API, "api-sales")
        w.reachable = reachable
        w.bot.get_channel = _lookup(w)
        w.sheet.grids["Outreach PoCs"] = [list(HEADERS)] + [list(r) for r in values]
        yield w


def _lookup(w):
    import replies_world as rw

    def get_channel(cid):
        if int(cid) == API:
            return w.api if w.reachable else None
        return {rw.CHANNEL: w.chan}.get(int(cid))
    return get_channel


def another_bot(w):
    """A SECOND bot object on the same database and the same fake Discord: the process after a restart."""
    from bot import SalesBot
    from db import DB
    b = SalesBot()
    b.db = DB(config.DB_PATH)
    b.llm = w.llm
    b.query_engine._client = SimpleNamespace(messages=w.model)
    w._stub_bot(b)
    b.get_channel = w.bot.get_channel
    return b


async def tick(w, day=TUE, hh=15, mm=30, *, bot=None, send=True) -> list:
    """One tick of the live sweep at a pretend minute: plan, then send what is due. The messages planned."""
    bot = bot or w.bot
    w.pretend_day(day, hh, mm)
    now = datetime.combine(day, time(hh, mm), dl.IST)
    marker = dl.iso(day)
    already = bot.db.drip_sent_today(marker)
    planned = await bot._plan_drip(today=day, already=already)
    due = [m for m in (planned or {}).get("messages") or [] if m["send_at"] <= now]
    if send:
        for m in due:
            await bot._send_drip_message(w.chan, m, marker=marker, channel_id=w.chan.id)
    return due


def posts_in(w, chan=None) -> list:
    import replies_world as rw
    return [rw.strip_tag(m.content) for m in w.posted if chan is None or m.channel is chan]


def fired_state(w) -> dict:
    return w.bot.db.reconnect_state()


def mention_line(i, name=None, company=None) -> str:
    return f"{i}. **{name}** ({company})"


def pile_name(i) -> str:
    return f"Prof. Alexandra Montgomery-Whitfield {i:02d}"


def pile_company(i) -> str:
    return f"Northern Institute of Advanced Computing {i:02d}"


def pile_rows() -> list:
    """41 people whose day is Mon 19 Oct: 14 with that date, 14 on Sat 17 and 13 on Sun 18 (both move to the
    Monday). The SHEET lists the Monday ones first, so the post's order (trigger date, then sheet order) is
    visibly not the sheet's: Saturday's 15-28, Sunday's 29-41, then Monday's 1-14."""
    rows, i = [], 0
    for count, when in ((14, "19-10-2026"), (14, "17-10-2026"), (13, "18/10/2026")):
        for _ in range(count):
            i += 1
            rows.append(vrow(i, pile_name(i), pile_company(i), reconnect=when))
    return rows


PILE_ORDER = list(range(15, 42)) + list(range(1, 15))


def pile_lines(upto=None) -> list:
    order = PILE_ORDER if upto is None else PILE_ORDER[:upto]
    return [mention_line(n, pile_name(i), pile_company(i)) for n, i in enumerate(order, 1)]


def due_rows(n, *, base=1, when="13-10-2026", **cells) -> list:
    return [vrow(base + k, f"Person {base + k:02d}", f"Co {base + k:02d}", reconnect=when, **cells) for k in range(n)]


def post_text(lines, *, more=None, greeting=GREETING) -> str:
    out = HEADING + "\n\n" + greeting + "\n\n" + "\n".join(lines)
    if more is not None:
        out += "\n\n" + more
    return out


# -- 01-06  WHICH RULE WINS ----------------------------------------------------------------------------------------------

def which_rule_wins(check):
    print("\nWHICH RULE WINS — Reconnect Date, then Meeting Date + 45, then the newest contact + 45")
    with pinned():
        r1 = one(reconnect="20-10-2026", meeting="01-09-2026")
        check("01 Reconnect 20 Oct, Meeting Date 1 Sep -> fires 20 Oct (rule 1 beats rule 2)",
              (reconnect.trigger_for(r1, today=TUE), fires_on([r1])), ((date(2026, 10, 20), 1, False),
                                                                       [date(2026, 10, 20)]))
        r2 = one(reconnect="", meeting="01-09-2026")
        check("02 Reconnect blank, Meeting Date 1 Sep -> fires 16 Oct (a Friday)",
              (reconnect.trigger_for(r2, today=TUE), fires_on([r2])), ((date(2026, 10, 16), 2, False),
                                                                       [date(2026, 10, 16)]))
        r3 = one(reconnect="FALSE", meeting="01-09-2026")
        check("03 Reconnect FALSE, Meeting Date 1 Sep -> fires 16 Oct (FALSE reads as empty)",
              (reconnect.trigger_for(r3, today=TUE), fires_on([r3])), ((date(2026, 10, 16), 2, False),
                                                                       [date(2026, 10, 16)]))
        r4 = one(reconnect="FALSE", meeting="", lidm="02-09-2026")
        check("04 Reconnect FALSE, Meeting Date blank, LI DM 2 Sep -> trigger 17 Oct by rule 3 "
              "(a Saturday, so it POSTS Mon 19 Oct)",
              (reconnect.trigger_for(r4, today=TUE), fires_on([r4])), ((date(2026, 10, 17), 3, False),
                                                                       [date(2026, 10, 19)]))
        r5 = one(reconnect="FALSE", fc="FALSE", lic="", lidm="", e1="", e2="", e3="", meeting="")
        check("05 No date in any column -> never fires, on any day, ever",
              (reconnect.trigger_for(r5, today=TUE), fires_on([r5], first=date(2026, 1, 1), last=date(2028, 12, 31)),
               reconnect.survey([r5], today=TUE)["never"]), ((None, 0, False), [], 1))
        statuses = [one(meeting="01-09-2026", mstatus=s) for s in ("", "Completed", "Cancelled", "TBD")]
        check("06 Meeting Date 1 Sep, Meeting Status blank -> fires 16 Oct; the status is not checked",
              ([reconnect.trigger_for(r, today=TUE) for r in statuses], fires_on(statuses[:1])),
              ([(date(2026, 10, 16), 2, False)] * 4, [date(2026, 10, 16)]))


# -- 07-16  DATE FORMATS ------------------------------------------------------------------------------------------------

def _reads_as(text, want=date(2026, 12, 13), *, today=TUE):
    """(read_date on the text, the whole trigger when it sits in a Reconnect Date cell)."""
    return (reconnect.read_date(text, today=today),
            reconnect.trigger_for(one(reconnect=text), today=today))


def date_formats(check):
    print("\nDATE FORMATS — typed by hand, day first, each must land on 13 December 2026")
    want = date(2026, 12, 13)
    with pinned():
        for n, text in ((7, "13/12/2026"), (8, "13-12-2026"), (9, "13.12.2026"), (10, "Dec 13 2026"),
                        (11, "December 13, 2026"), (12, "2026-12-13")):
            check(f"{n:02d} {text} -> 13 Dec 2026", _reads_as(text), (want, (want, 1, False)))
        cell_date = reconnect.trigger_for({"name": "Ann", "company": "Acme", "reconnect_date": want}, today=TUE)
        check("13 A real Sheets date cell (a date, and a datetime) -> as it stands",
              (reconnect.read_date(want, today=TUE), reconnect.read_date(datetime(2026, 12, 13, 0, 0), today=TUE),
               cell_date), (want, want, (want, 1, False)))
        check("14 13th dec -> 13 Dec, the right year filled in (next time it comes round, or this year's if it "
              "passed within 90 days)",
              (reconnect.read_date("13th dec", today=TUE),                       # 13 Oct: ahead
               reconnect.read_date("13th dec", today=date(2026, 12, 20)),        # 7 days ago: this year's
               reconnect.read_date("13th dec", today=date(2027, 3, 20)),         # 97 days ago: next year's
               reconnect.trigger_for(one(reconnect="13th dec"), today=TUE)),
              (want, want, date(2027, 12, 13), (want, 1, False)))
        check("15 03/04/2026 -> 3 APRIL, never 4 March (day first, always)",
              ([reconnect.read_date(t, today=TUE) for t in ("03/04/2026", "03-04-2026", "03.04.2026", "3/4/2026")],
               reconnect.trigger_for(one(reconnect="03/04/2026"), today=date(2026, 3, 1))),
              ([date(2026, 4, 3)] * 4, (date(2026, 4, 3), 1, False)))
        banana_meeting = one(reconnect="banana", meeting="29-08-2026")
        banana_contact = one(reconnect="banana", fc="29-08-2026")
        both = parsed([vrow(1, "Ann", "Acme", reconnect="banana", meeting="29-08-2026"),
                       vrow(2, "Bob", "Borealis", reconnect="banana", fc="29-08-2026")])
        result = r14_engine(both)
        check("16 banana -> listed with '(reconnect date unclear)'; the day comes from rule 2 or rule 3",
              (reconnect.read_date("banana", today=TUE),
               reconnect.trigger_for(banana_meeting, today=TUE), reconnect.trigger_for(banana_contact, today=TUE),
               r14_text(result).split("\n")[2:]),
              (None, (date(2026, 10, 13), 2, True), (date(2026, 10, 13), 3, True),
               ["1. **Ann** (Acme) (reconnect date unclear)", "2. **Bob** (Borealis) (reconnect date unclear)"]))


# -- 17-22  TIMING -------------------------------------------------------------------------------------------------------

def timing(check):
    print("\nTIMING — the Monday shift, no catch-up, no empty post, 15:30 outside the cap, R13 first, once a day")
    with pinned():
        sat_row = one(reconnect="17-10-2026")

        async def weekend():
            out = {}
            with rworld([vrow(1, "Ann", "Acme", reconnect="17-10-2026")]) as w:
                out["sat"] = await tick(w, SAT)
                out["sun"] = await tick(w, SUN)
                out["mon"] = await tick(w, MON)
                out["posts"] = posts_in(w, w.api)
                out["where"] = sorted({m.channel.id for m in w.posted})
            return out
        wk = asyncio.run(weekend())
        eng = [len(names_of(r14_engine([sat_row], d))) for d in (date(2026, 10, 16), SAT, SUN, MON, date(2026, 10, 20))]
        check("17 Trigger falls on a Saturday -> posts on the MONDAY: not Saturday, not Sunday, not skipped",
              (fires_on([sat_row]), eng, [len(wk["sat"]), len(wk["sun"]), len(wk["mon"])], len(wk["posts"]),
               wk["where"], "**Ann** (Acme)" in (wk["posts"] or [""])[0]),
              ([MON], [0, 0, 0, 1, 0], [0, 0, 1], 1, [API], True))

        old = [one(reconnect="29-09-2026"), one(fc="01-08-2026")]            # two weeks past; rule 3 -> 15 Sep
        old_survey = [reconnect.survey([r], today=TUE) for r in old]

        async def catchup():
            with rworld([vrow(1, "Ann", "Acme", reconnect="29-09-2026"), vrow(2, "Bob", "Borealis", fc="01-08-2026")]) as w:
                got = [await tick(w, d) for d in (TUE, date(2026, 10, 14), date(2026, 10, 15))]
                return [len(g) for g in got], len(w.posted), w.drip_rows()
        late = asyncio.run(catchup())
        check("18 Reconnect Date two weeks past -> NO post at all, no catch-up (on any later day either)",
              ([(len(s["past"]), len(s["due"])) for s in old_survey], fires_on(old, first=TUE), late),
              ([(1, 0), (1, 0)], [], ([0, 0, 0], 0, [])))

        async def nothing_due():
            far = [vrow(1, "Ann", "Acme", reconnect="01-06-2027"), vrow(2, "Bob", "Borealis"),
                   vrow(3, "Cy", "Cinder", reconnect="29-09-2026"), vrow(4, "Di", "Delta", fc="FALSE")]
            with rworld(far) as w:
                got = await tick(w, TUE)
                return len(got), posts_in(w), w.drip_rows(), fired_state(w)
        quiet = asyncio.run(nothing_due())
        empty = r14_engine([])
        check("19 Nothing due today -> no post, and no 'nobody today' line",
              (quiet, r14_message(empty), names_of(empty), r14_text(empty)),
              ((0, [], [], {}), None, [], ""))

        # -- 20: when it posts, and outside the cap
        weekdays = [date(2026, 10, 12) + timedelta(days=n) for n in range(5)]
        cap = drip.cap_for(TUE)
        full = [{"slot": i, "group_key": f"x|{i}", "action_type": nextaction.R_AI_NEWS, "counts_toward_cap": 1,
                 "pinned": 0, "planned_at": "14:00", "sent_at": f"{dl.iso(TUE)}T1{i}:00:00"} for i in range(1, cap + 1)]
        per_day = []
        for d in weekdays:
            res = r14_engine([one(reconnect=d.strftime("%d-%m-%Y"))], d)
            msgs = [m for m in drip.plan(res["actions"], day=d, already_sent=[])["messages"]]
            per_day.append([(m["type"], m["send_at_hhmm"], m["pinned"], m["counts_toward_cap"]) for m in msgs])
        res = r14_engine([one(reconnect="13-10-2026")], TUE)
        capped = drip.plan(res["actions"], day=TUE, already_sent=full)
        # the thursday with a spaced post (R12) in the queue: R14 must not move it, and the two-hour gap does not apply
        thu = date(2026, 10, 15)
        thu_rows = [one(reconnect="15-10-2026")]
        with_both = nextaction.run(today=thu, rows=[], packages=pkc.parsed(pkc.five()), reconnect_rows=thu_rows,
                                   reconnect_state={}, day_rules=[rules_mod.by_id("R12"), rules_mod.by_id("R14")])
        alone = nextaction.run(today=thu, rows=[], packages=pkc.parsed(pkc.five()),
                               day_rules=[rules_mod.by_id("R12")])
        plan_both = drip.plan(with_both["actions"], day=thu, already_sent=[])
        plan_alone = drip.plan(alone["actions"], day=thu, already_sent=[])
        spaced = lambda p: [(m["type"], m["send_at_hhmm"]) for m in p["messages"] if m["type"] != nextaction.R_RECONNECT]  # noqa: E731
        at = [m["send_at"] for m in plan_both["messages"] if m["type"] == nextaction.R_RECONNECT]

        async def recorded():
            with rworld(due_rows(2)) as w:
                await tick(w, TUE)
                rows = w.bot.db.drip_sent_today(dl.iso(TUE))
                return ([(r["action_type"], r["planned_at"], bool(r["counts_toward_cap"]), bool(r["pinned"]))
                         for r in rows], drip.counted_today(rows))
        stored = asyncio.run(recorded())
        want_row = (nextaction.R_RECONNECT, "15:30", False, True)
        check("20 Any day with people due -> posts 14:00-16:00 (15:30), fixed time, outside the daily cap and the "
              "two-hour gap",
              (per_day, [(m["type"], m["send_at_hhmm"], m["pinned"], m["counts_toward_cap"]) for m in capped["messages"]],
               "14:00" <= config.RECONNECT_TIME <= "16:00",
               spaced(plan_both) == spaced(plan_alone), [t.strftime("%H:%M") for t in at], stored),
              ([[(nextaction.R_RECONNECT, "15:30", True, False)]] * 5,
               [(nextaction.R_RECONNECT, "15:30", True, False)], True, True, ["15:30"], ([want_row], 0)))

        # -- 21: R13 first
        base = date(2026, 10, 12)                                            # a Monday: R13's day
        rows13 = [fx.person(1, name="Ada Named", company="Named Co", step="Send email 1", li_date=date(2026, 9, 1),
                            first_contact="TRUE"),
                  fx.person(2, name="Ben Free", company="Free Co", connected="Not connected")]
        rows13 = [r + [""] * (len(HEADERS) - len(r)) for r in rows13]
        for r in rows13:
            r[HEADERS.index("Reconnect Date")] = "12-10-2026"
        recon_rows = parsed(rows13)
        both = nextaction.run(today=base, rows=recon_rows, prospect_rows=recon_rows, next_step_state={},
                              reconnect_rows=recon_rows, reconnect_state={},
                              day_rules=[rules_mod.by_id("R13"), rules_mod.by_id("R14")])
        only14 = nextaction.run(today=base, rows=recon_rows, prospect_rows=recon_rows, next_step_state={},
                                reconnect_rows=recon_rows, reconnect_state={}, day_rules=[rules_mod.by_id("R14")])
        r13 = [a["poc"] for a in both["actions"] if a.get("rule_id") == "R13"]
        check("21 Somebody named by R13 that day -> NOT listed here as well (R13 and R14 run together)",
              (r13, names_of(both), names_of(only14), [bool(a.get("contact_key")) for a in r14_items(both)]),
              (["Ada Named"], ["Ben Free"], ["Ada Named", "Ben Free"], [False]))

        # -- 22: once a day
        async def once_a_day():
            with rworld(due_rows(2)) as w:
                first = await tick(w, TUE, 15, 30)
                again = await tick(w, TUE, 15, 45)
                # a plan that forgot what was sent must still not double-post (the fired table is the second guard)
                w.pretend_day(TUE, 15, 50)
                replanned = await w.bot._plan_drip(today=TUE, already=[])
                # somebody whose date is typed in AFTER the post went: the day has been spoken for
                w.sheet.grids["Outreach PoCs"].append(vrow(9, "Late Lee", "Late Co", reconnect="13-10-2026"))
                late = await tick(w, TUE, 16, 0)
                return (len(first), len(again), len(late), len(posts_in(w, w.api)),
                        len([m for m in (replanned or {}).get("messages") or []]), len(w.drip_rows()))
        check("22 The rule already posted today -> does not post again the same day (not for a late arrival "
              "either)", asyncio.run(once_a_day()), (1, 0, 0, 1, 0, 1))


# -- 23-30  THE POST -----------------------------------------------------------------------------------------------------

def the_post(check):
    import replies_world as rw

    print("\nTHE POST — five names, the 'more' line only when there are more, a yes lists everybody")
    more36 = "there are 36 more — would you like to look at the rest?"

    async def pile(test_mode=False):
        out = {}
        with rworld(pile_rows(), test_mode=test_mode, day=MON) as w:
            model = w.counts()["model"]
            out["due"] = await tick(w, MON)
            post = w.posted[-1]
            out["post"] = rw.strip_tag(post.content)
            out["tagged"] = post.content.startswith("[TEST")
            out["where"] = post.channel.id
            out["model"] = w.counts()["model"] - model
            out["fired"] = len(fired_state(w))
            out["people"] = w.bot.db.find_drip_by_message_id(str(post.id)).get("people")

            # 27: a yes under an UNRELATED post does nothing
            plain = w.bot_says("Here is something else I posted.", channel=w.api)
            w.bot.db.record_drip_send(on_date=dl.iso(MON), slot=9, group_key="other|*", action_type=nextaction.R_PACKAGES,
                                      stage="nudge", planned_at="14:00", channel_id=API, message_id=None,
                                      sent_at=dl.now_ist().isoformat(timespec="seconds"), counts_toward_cap=True)
            other = w.bot_says("The packages that are not ready.", channel=w.api)
            w.bot.db.attach_drip_message_id(on_date=dl.iso(MON), slot=9, message_id=other.id, body="x")
            before = len(w.posted)
            await w.say("yes", reply_to=plain, channel=w.api)
            await w.say("yes", reply_to=other, channel=w.api)
            await w.say("yes", channel=w.api)                                  # a yes that answers nothing
            out["unrelated"] = (len(w.posted) - before, [p for p in posts_in(w) if p.startswith(REST_HEAD)],
                                w.bot.db.find_drip_by_message_id(str(post.id)).get("rest_listed"))

            # 28: anything but a yes gets no reply
            n, reacts = len(w.posted), len(w.reactions)
            said = ["no", "No.", "nope", "no thanks", "not now", "thanks", "Daisy has moved to Agoda",
                    "I'll do these tomorrow"]
            for text in said:
                await w.say(text, reply_to=post, channel=w.api)
            out["not_yes"] = (len(w.posted) - n, len(w.reactions) - reacts,
                              w.bot.db.find_drip_by_message_id(str(post.id)).get("rest_listed"))

            # 26: the yes
            n = len(w.posted)
            await w.say("yes", reply_to=post, channel=w.api)
            out["parts"] = posts_in(w)[n:]
            out["part_channels"] = sorted({m.channel.id for m in w.posted[n:]})
            n2 = len(w.posted)
            await w.say("yes", reply_to=post, channel=w.api)
            out["second_yes"] = len(w.posted) - n2
            out["fired_after"] = len(fired_state(w))
            out["writes"] = len(w.sheet.writes)
        return out

    with pinned():
        live = asyncio.run(pile(False))
        test = asyncio.run(pile(True))
    shown = pile_lines(5)
    check("23 41 people due -> five names, then 'there are 36 more — would you like to look at the rest?'",
          (live["post"], len(live["due"]), live["where"], live["model"], live["fired"]),
          (post_text(shown, more=more36), 1, API, 0, 41))
    check("26 Reply 'yes' -> all 41 listed, in the post's order, split BETWEEN names across messages, each "
          "within 2,000 characters, nothing dropped, nothing repeated",
          (len(live["parts"]) >= 2, max(len(p) for p in live["parts"]) <= 2000,
           [ln for p in live["parts"] for ln in p.split("\n")], live["part_channels"],
           [sum(p.count(pile_name(i)) for p in live["parts"]) for i in range(1, 42)]),
          (True, True, [REST_HEAD] + pile_lines(), [API], [1] * 41))
    check("27 'yes' under an unrelated post (a plain bot message, another rule's post, or no reply at all) -> "
          "nothing happens", live["unrelated"], (0, [], ""))
    check("28 Reply 'no', or anything else -> no reply from Saley (no message, no reaction, and the offer "
          "still stands)", live["not_yes"][:2] + (bool(live["not_yes"][2]),), (0, 0, False))
    extras_post(check, live, test)

    # -- 24, 25: five and one
    async def sized(n, day=TUE, test_mode=False):
        with rworld(due_rows(n), test_mode=test_mode, day=day) as w:
            await tick(w, day)
            return posts_in(w, w.api if not test_mode else None)[0] if w.posted else ""
    with pinned():
        five = asyncio.run(sized(5))
        solo = asyncio.run(sized(1))
        six = asyncio.run(sized(6))
    five_lines = [mention_line(i, f"Person {i:02d}", f"Co {i:02d}") for i in range(1, 6)]
    check("24 Exactly 5 due -> 5 names and NO 'more' line (the post ends at the last name)",
          (five, five.endswith(five_lines[-1]), "more" in five),
          (post_text(five_lines), True, False))
    check("25 1 person due -> reads naturally in the singular ('this PoC', never '1 person')",
          (solo, "these PoCs" in solo, "1 person" in solo),
          (post_text([mention_line(1, "Person 01", "Co 01")], greeting=GREETING_ONE), False, False))
    check("one name with more behind it, and six due: 'there is 1 more' (singular), and the greeting stays plural",
          six, post_text(five_lines, more="there is 1 more — would you like to look at them too?"))

    # -- 29: a blank Company
    # WHO LOGS WHICH. A row with no Company never reaches a rule: the sheet parser drops it (it is how a
    # spacer line is told from a row), so the parser is what says so. A row with no Name reaches R14,
    # which says so itself. Both lines are tapped: the read and the rule.
    with pinned(), tapped() as tap:
        mixed = parsed([vrow(1, "Ann", "", reconnect="13-10-2026"), vrow(2, "Bob", "Borealis", reconnect="13-10-2026"),
                        vrow(3, "", "Cinder", reconnect="13-10-2026")])
        result = r14_engine(mixed)
    skipped = [m for _l, m in tap.lines if "skipped" in m and ("[rules] R14" in m or "[gtm]" in m)
               and ("blank Company" in m or "blank Name" in m)]
    check("29 A row with a blank Company -> skipped and logged (and a blank Name too: the line is built from both)",
          (names_of(result), r14_text(result).split("\n")[2:], len(skipped),
           [("blank Company" in m, "row 2" in m) for m in skipped if "blank Company" in m],
           [("blank Name" in m, "row 4" in m) for m in skipped if "blank Name" in m]),
          (["Bob"], ["1. **Bob** (Borealis)"], 2, [(True, True)], [(True, True)]))

    # -- 30: the wrong channel
    async def wrong(test_mode=False, **kw):
        sales_ids = kw.pop("sales_ids", None)
        extra = {"SALES_API_CHANNEL_ID": kw.pop("api_id", API)}
        if sales_ids is not None:
            extra["SALES_CHANNEL_IDS"] = sales_ids
        with rworld(due_rows(3), test_mode=test_mode, **kw, **extra) as w:
            with tapped() as tap:
                ok = w.bot._check_reconnect_channel()
            planned = await w.bot._plan_drip(today=TUE, already=[])
            msgs = [m for m in (planned or {}).get("messages") or [] if m.get("type") == nextaction.R_RECONNECT]
            with tapped() as tap2:
                for chan in (w.chan, w.api, w.other):
                    for m in msgs:
                        await w.bot._send_drip_message(chan, m, marker=dl.iso(TUE), channel_id=chan.id)
            errors = [m for lvl, m in tap.lines if lvl == "ERROR" and "SALES_API_CHANNEL_ID" in m]
            return (ok, len(errors) >= 1, len(msgs), len(w.posted), sum(len(c.msgs) for c in (w.chan, w.api, w.other)),
                    w.drip_rows(), fired_state(w), any("NOT sent" in m for lvl, m in tap2.lines if lvl == "ERROR"))
    with pinned():
        unreachable = asyncio.run(wrong(reachable=False))
        not_a_sales_channel = asyncio.run(wrong(api_id=9999, sales_ids=[rw.CHANNEL]))
        unset = asyncio.run(wrong(api_id=0, sales_ids=[rw.CHANNEL]))
        in_test_mode = asyncio.run(wrong(True, reachable=False))
    check("30 Wrong API Sales channel id -> a startup ERROR naming SALES_API_CHANNEL_ID, and no post in any "
          "channel (unreachable, not one of the sales channels, unset, and in test mode), no slot, nobody recorded",
          (unreachable, not_a_sales_channel, unset, in_test_mode),
          tuple((False, True, 1, 0, 0, [], {}, True) for _ in range(4)))


def extras_post(check, live, test):
    """What the pile scenario also shows, named without a number."""
    check("a yes lists ONCE: a second yes under the post lists nothing, and nobody is un-recorded",
          (live["second_yes"], live["fired_after"]), (0, 41))
    check("the post's record keeps everybody due, in order, five of them marked as named",
          ([(p["n"], p["name"], p["shown"]) for p in live["people"]][:6],
           len(live["people"]), sum(1 for p in live["people"] if p["shown"])),
          ([(n, pile_name(i), n <= 5) for n, i in enumerate(PILE_ORDER[:6], 1)], 41, 5))
    check("nothing is written to the sheet (the post, the yes and the replies)", live["writes"], 0)
    check("test mode is identical apart from the [TEST] tag (post, list, and the silence on a no); the post goes "
          "to the channel the test run uses, not the API channel",
          (test["post"], test["parts"], test["tagged"], test["not_yes"][:2], test["where"], live["tagged"]),
          (live["post"], live["parts"], True, (0, 0), 4242, False))


# -- 31-35  STOPPING -------------------------------------------------------------------------------------------------------

def stopping(check):
    print("\nSTOPPING — once, Lost, On Hold, a new date, a restart")
    with pinned():
        async def once():
            with rworld([vrow(1, "Ann", "Acme", fc="29-08-2026")]) as w:        # rule 3: 13 Oct
                first = await tick(w, TUE, 15, 30)
                same_day = await tick(w, TUE, 15, 45)
                blind = await w.bot._plan_drip(today=TUE, already=[])             # forgot what was sent today
                later = [len(await tick(w, d)) for d in (date(2026, 10, 14), date(2026, 10, 15), SAT, MON)]
                state = fired_state(w)
                return (len(first), len(same_day), len((blind or {}).get("messages") or []), later,
                        len(posts_in(w, w.api)), state)
        got = asyncio.run(once())
        rule1 = one(reconnect="20-10-2026")
        check("31 Fired once, nothing on the row changes -> never fires again (same day, a plan that forgot "
              "what was sent, later days, or even by a Reconnect Date that already fired)",
              (got[:5], got[5], fires_on([rule1], fired={"acme|ann": ["2026-10-20"]})),
              ((1, 0, 0, [0, 0, 0, 0], 1), {"acme|ann": ["2026-10-13"]}, []))

        lost = [one(reconnect="20-10-2026", deal=s) for s in ("Lost", "lost", "Closed Lost")]
        free = one(reconnect="20-10-2026", deal="Open")
        late_lost = parsed([vrow(1, "Ann", "Acme", reconnect="20-10-2026", deal="Lost")])
        before = fires_on([one(reconnect="20-10-2026")])
        check("32 Deal Status = Lost before the date -> never fires (whatever the capitalisation)",
              ([fires_on([r]) for r in lost], fires_on(late_lost, first=date(2026, 9, 1)),
               [reconnect.survey([r], today=TUE)["stopped"] and 1 for r in lost], fires_on([free]), before),
              ([[], [], []], [], [1, 1, 1], [date(2026, 10, 20)], [date(2026, 10, 20)]))

        hold = [one(reconnect="20-10-2026", deal=s) for s in ("On Hold", "on hold", "On-Hold", "On  Hold")]
        check("33 Deal Status = On Hold before the date -> never fires (and 'Holdings Ltd' is not On Hold)",
              ([fires_on([r]) for r in hold], fires_on([one(reconnect="20-10-2026", deal="Holdings Ltd")])),
              ([[], [], [], []], [date(2026, 10, 20)]))

        from db import DB
        import tempfile
        import shutil
        tmp = tempfile.mkdtemp(prefix="saley-r14-")
        try:
            db = DB(os.path.join(tmp, "r14_test.db"))
            row = one(fc="29-08-2026")                                              # rule 3: 13 Oct
            key = reconnect.row_key(row)
            first_due = [e["name"] for e in reconnect.due_today([row], today=TUE, already_fired=db.reconnect_state())]
            db.record_reconnect_fired(key, trigger_date="2026-10-13", sent_on="2026-10-13")
            db.record_reconnect_fired(key, trigger_date="2026-10-13", sent_on="2026-10-13")      # twice: still one
            state = db.reconnect_state()
            again = [e["name"] for e in reconnect.due_today([row], today=TUE, already_fired=state)]
            # a NEW Reconnect Date is typed, for a new day
            moved = one(fc="29-08-2026", reconnect="27-10-2026")
            new_day = date(2026, 10, 27)
            res_before = [n for d in (TUE, new_day - timedelta(days=1)) for n in names_of(r14_engine([moved], d, state))]
            res_on = names_of(r14_engine([moved], new_day, state))
            db.record_reconnect_fired(key, trigger_date="2026-10-27", sent_on="2026-10-27")
            state2 = db.reconnect_state()
            res_after = names_of(r14_engine([moved], new_day, state2))
            res_later = [n for d in (new_day + timedelta(days=k) for k in (1, 7, 30)) for n in
                         names_of(r14_engine([moved], d, state2))]
            check("34 Fired, then a NEW Reconnect Date is set -> fires once more, on the new date, and then not again",
                  (first_due, again, res_before, res_on, res_after, res_later, state2[key]),
                  (["Ann"], [], [], ["Ann"], [], [], ["2026-10-13", "2026-10-27"]))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        async def restart():
            out = {}
            with rworld(due_rows(3)) as w:
                w.pretend_day(TUE, 15, 30)
                a_plan = await w.bot._plan_drip(today=TUE, already=[])             # A: triggers, then dies
                msg = next(m for m in a_plan["messages"] if m["type"] == nextaction.R_RECONNECT)
                out["before"] = len(w.posted)
                b = another_bot(w)                                                  # B: the restarted process
                await b._send_drip_message(w.chan, msg, marker=dl.iso(TUE), channel_id=w.chan.id)
                out["after_b"] = len(posts_in(w, w.api))
                await w.bot._send_drip_message(w.chan, msg, marker=dl.iso(TUE), channel_id=w.chan.id)   # A, stale
                out["after_stale_a"] = len(posts_in(w, w.api))
                c = another_bot(w)                                                  # C: plans again
                w.pretend_day(TUE, 15, 45)
                c_plan = await c._plan_drip(today=TUE, already=c.db.drip_sent_today(dl.iso(TUE)))
                out["c_plans"] = len([m for m in (c_plan or {}).get("messages") or []])
                blind = await c._plan_drip(today=TUE, already=[])
                out["c_blind"] = len([m for m in (blind or {}).get("messages") or []])
                out["fired"] = len(fired_state(w))
            # a restart with nothing sent: the new process plans and sends on its own, once
            with rworld(due_rows(3)) as w:
                b = another_bot(w)
                out["own"] = len(await tick(w, TUE, 15, 31, bot=b)), len(await tick(w, TUE, 15, 46, bot=b)), \
                    len(posts_in(w, w.api))
            return out
        r = asyncio.run(restart())
        check("35 Bot restarted between trigger and post -> posts ONCE: not twice (not even a stale process), not "
              "never",
              (r["before"], r["after_b"], r["after_stale_a"], r["c_plans"], r["c_blind"], r["fired"], r["own"]),
              (0, 1, 1, 0, 0, 3, (1, 0, 1)))


# -- everything else -------------------------------------------------------------------------------------------------------

def measured_shape(check):
    """A fixture with the shape of the tab measured on 13 Oct 2026. NOT a read of the real sheet."""
    print("\nTHE MEASURED SHAPE — 670 rows with a company, 528 never, 142 to send, 2 from rule 2, 78 on a weekend")
    # post days: Mon 23 Nov 41, Mon 16 Nov 38, Thu 19 Nov 37, Fri 20 Nov 17 (133), and 9 more elsewhere
    plan = []                                                                       # (trigger date, rule)
    sat_sun_23 = [date(2026, 11, 21)] * 19 + [date(2026, 11, 22)] * 19
    plan += [(d, 3) for d in sat_sun_23] + [(date(2026, 11, 23), 3)] * 3                       # 41 -> Mon 23 Nov
    plan += [(d, 3) for d in [date(2026, 11, 14)] * 18 + [date(2026, 11, 15)] * 18] + [(date(2026, 11, 16), 3)] * 2   # 38
    plan += [(date(2026, 11, 19), 3)] * 37
    plan += [(date(2026, 11, 20), 3)] * 17
    plan += [(date(2026, 10, 24), 3), (date(2026, 10, 25), 3), (date(2026, 11, 7), 3), (date(2026, 11, 8), 3),
             (date(2026, 10, 14), 3), (date(2026, 10, 21), 3), (date(2026, 11, 3), 3),
             (date(2026, 10, 27), 2), (date(2026, 11, 5), 2)]                      # 9 more: 4 weekend, 2 from rule 2
    values, n = [], 0
    for trigger, rule in plan:
        n += 1
        source = trigger - timedelta(days=45)
        if rule == 2:
            values.append(vrow(n, f"Dated {n:03d}", f"Company {n:03d}", meeting=source.strftime("%d-%m-%Y"),
                               fc=(source - timedelta(days=20)).strftime("%d-%m-%Y")))
        elif n % 7 == 0:                                                           # the newest is a text date in a later column
            values.append(vrow(n, f"Dated {n:03d}", f"Company {n:03d}", lidm=source.strftime("%d.%m.%Y"),
                               fc=(source - timedelta(days=9)).strftime("%d-%m-%Y")))
        else:
            values.append(vrow(n, f"Dated {n:03d}", f"Company {n:03d}", fc=source.strftime("%d-%m-%Y")))
    assert len(values) == 142
    for k in range(528):                                                           # 327 carry the checkbox artefact
        n += 1
        values.append(vrow(n, f"Undated {n:03d}", f"Company {n:03d}", fc="FALSE" if k < 327 else ""))
    rows = parsed(values)
    with pinned():
        s = reconnect.survey(rows, today=TUE)
        piles = {d: len(reconnect.due_today(rows, today=d)) for d in
                 (date(2026, 11, 23), date(2026, 11, 16), date(2026, 11, 19), date(2026, 11, 20))}
        names = [len(names_of(r14_engine(rows, d))) for d in piles]
        more = {d: reconnect_post_more(rows, d) for d in piles}
    check("measured shape: 670 rows with a company in (never 528), 528 with no date anywhere, 142 dated, "
          "2 from rule 2 and 140 from rule 3",
          (len(rows), s["with_company"], s["never"], s["dated"], s["by_rule"]),
          (670, 670, 528, 142, {1: 0, 2: 2, 3: 140}))
    check("measured shape: 78 triggers land on a Saturday or Sunday (and post on the Monday)", s["on_weekend"], 78)
    check("measured shape: the November pile-up, after the weekend shift: Mon 23 Nov 41, Mon 16 Nov 38, Thu 19 Nov 37, "
          "Fri 20 Nov 17", list(piles.values()), [41, 38, 37, 17])
    check("measured shape: each pile-up day is five names and 'N more' (36, 33, 32, 12), every person an item",
          (list(more.values()), names), ([36, 33, 32, 12], [41, 38, 37, 17]))


def reconnect_post_more(rows, day) -> int:
    msg = r14_message(r14_engine(rows, day), day)
    return len(drip.reconnect_everyone(msg)) - len(drip.shown_contacts(msg))


def edge_cases(check):
    print("\nEDGE CASES THE TABLE DOES NOT NUMBER")
    with pinned():
        # ordinals and stray punctuation
        tidy = ["13th dec", "13th Dec.", "13  dec", "Dec. 13 2026", "13th December 2026.", " 13 Dec ", "13 DEC"]
        check("edge: ordinals (13th, 1st, 3rd), a trailing full stop and a double space are stripped before reading",
              ([reconnect.read_date(t, today=TUE) for t in tidy],
               [reconnect.read_date(t, today=TUE) for t in ("1st dec", "3rd Dec", "2nd December", "21st dec")]),
              ([date(2026, 12, 13)] * len(tidy), [date(2026, 12, 1), date(2026, 12, 3), date(2026, 12, 2),
                                                  date(2026, 12, 21)]))
        check("edge: a no-year date passed by under 90 days is this year's, older is next year's (the Deliverables "
              "tab's reading of '25-Sep')",
              (reconnect.read_date("13 Sep", today=TUE), reconnect.read_date("15 Jun", today=TUE),
               reconnect.read_date("13 Oct", today=TUE), nextaction._deliverable_due("25-Sep", today=TUE)),
              (date(2026, 9, 13), date(2027, 6, 15), TUE, date(2026, 9, 25)))
        check("edge: nonsense is None, never guessed (month-first numbers, a month 13, a word, empty)",
              [reconnect.read_date(t, today=TUE) for t in ("12/13/2026", "31/02/2026", "soon", "", "   ", None)],
              [None] * 6)
        # edge 2: unreadable with no other date
        lone = one(reconnect="banana")
        check("edge 2: an unreadable Reconnect Date with no other date has no day to anchor to: flagged, never posted",
              (reconnect.trigger_for(lone, today=TUE), fires_on([lone], first=date(2026, 1, 1), last=date(2028, 12, 31))),
              ((None, 0, True), []))
        # edge 4: far in the future
        far = one(reconnect="01-06-2027")
        s = reconnect.survey([far], today=TUE)
        check("edge 4: a date far in the future -> nothing until then, no early warning",
              (len(s["pending"]), len(s["due"]), fires_on([far]), r14_text(r14_engine([far]))),
              (1, 0, [date(2027, 6, 1)], ""))
        # edge 7: a meeting recorded early
        early = one(meeting="01-11-2026")
        with tapped() as tap:
            res = r14_engine([early])
        check("edge 7: Meeting Date in the future -> a future trigger (+45), and it is LOGGED (recorded early)",
              (reconnect.trigger_for(early, today=TUE), fires_on([early]), names_of(res),
               [m for _l, m in tap.lines if "recorded early" in m and "R14" in m] != []),
              ((date(2026, 12, 16), 2, False), [date(2026, 12, 16)], [], True))
        # edges 9, 10: dates as text, two dates on a row
        both = one(fc=date(2026, 8, 20), lic="02.09.2026", e1="")
        check("edge 9: dates stored as text ('02.09.2026') beside real date cells both parse; edge 10: two dates on a "
              "row count from the later one",
              (reconnect.trigger_for(both, today=TUE), reconnect.newest_contact(both, today=TUE),
               reconnect.trigger_for(one(fc="02.09.2026", lic=date(2026, 8, 20)), today=TUE)),
              ((date(2026, 10, 17), 3, False), date(2026, 9, 2), (date(2026, 10, 17), 3, False)))
        each = [(role, reconnect.newest_contact(one(**{role: "05-09-2026", "fc": "01-08-2026"}), today=TUE))
                for role in ("lic", "lidm", "e1", "e2", "e3")]
        check("edge 10: each of the six contact columns can be the newest (First Contact, LI Connected, LI DM, and the "
              "three email dates)", each + [("fc", reconnect.newest_contact(one(fc="01-08-2026"), today=TUE))],
              [(r, date(2026, 9, 5)) for r in ("lic", "lidm", "e1", "e2", "e3")] + [("fc", date(2026, 8, 1))])
        # blank name, duplicate
        dup = parsed([vrow(1, "Ann", "Acme", reconnect="13-10-2026"), vrow(2, "Ann", "Acme", reconnect="13-10-2026")])
        check("edge: the same person on two rows is one person (one line, one record), and an empty row is not a person",
              (len(reconnect.survey(dup, today=TUE)["due"]), len(reconnect.survey(dup, today=TUE)["duplicates"]),
               reconnect.survey(parsed([vrow(1, "", "")]), today=TUE)["with_company"]), (1, 1, 0))
        # R14's items
        res = r14_engine(parsed(due_rows(3)))
        items = r14_items(res)
        check("R14 items: no contact_key (R13 is the only rule it gives way to), the fixed time, the trigger and the rule",
              ([bool(a.get("contact_key")) for a in items], {a["dayof_time"] for a in items}, [a["pick_order"] for a in items],
               {a["trigger_iso"] for a in items}, {a["reconnect_rule"] for a in items}, {a["more"] for a in items}),
              ([False] * 3, {"15:30"}, [0, 1, 2], {"2026-10-13"}, {1}, {0}))
        unreadable = nextaction.run(today=TUE, rows=[], reconnect_rows=parsed(due_rows(3)), reconnect_state=None,
                                    day_rules=[rules_mod.by_id("R14")])
        check("R14: who has fired cannot be read (None) -> it names NOBODY (an empty answer would post everybody again)",
              (names_of(unreadable), unreadable["reports"]["R14"]["state"]), ([], "unreadable"))

        # a wrong day for the live gate: sheet is the only input, so a pretend-clock test day on a live DB records nobody
        async def rehearsal():
            with rworld(due_rows(2)) as w:
                real = config.DB_PATH
                config.DB_PATH = os.path.join(w.tmp, "live.db")              # not a *_test.db
                try:
                    await tick(w, TUE)
                    return len(posts_in(w, w.api)), fired_state(w)
                finally:
                    config.DB_PATH = real
        check("a pretend-clock test day on a database that is not *_test.db posts but records NOBODY as fired "
              "(a rehearsal cannot spend a real person's one reminder)", asyncio.run(rehearsal()), (1, {}))

        # a yes under an older day's post, and a question under today's post
        async def replies():
            import replies_world as rw
            out = {}
            with rworld(due_rows(7)) as w:
                await tick(w, TUE)
                post = w.posted[-1]
                ctx = {"direct_is_bot": True, "drip": w.bot.db.find_drip_by_message_id(str(post.id))}
                chan_msg = SimpleNamespace(id=1, channel=w.api, author=rw._author(rw.APPROVER))
                n = len(w.posted)
                out["question"] = await w.bot._maybe_reconnect_rest(chan_msg, "who is Person 03?", ctx)
                out["statement"] = await w.bot._maybe_reconnect_rest(chan_msg, "no", ctx)
                other = dict(ctx["drip"], action_type=nextaction.R_PACKAGES)
                out["not_r14"] = await w.bot._maybe_reconnect_rest(chan_msg, "yes", dict(ctx, drip=other))
                out["no_bot"] = await w.bot._maybe_reconnect_rest(chan_msg, "yes", dict(ctx, direct_is_bot=False))
                out["quiet"] = len(w.posted) - n
                w.pretend_day(date(2026, 10, 14), 10, 0)                      # the next day
                n = len(w.posted)
                await w.say("yes", reply_to=post, channel=w.api)
                out["older_post"] = (len(w.posted) - n, w.bot.db.find_drip_by_message_id(str(post.id)).get("rest_listed"))
                w.pretend_day(TUE, 16, 0)
                await w.say("yes", reply_to=post, channel=w.api)
                out["same_day_later"] = posts_in(w)[n:]
            return out
        got = asyncio.run(replies())
        check("a yes: a question under the post is NOT swallowed (returned unhandled, answered like any question), a "
              "statement is handled silently, a yes under another rule's post is untouched, and none of them sends",
              (got["question"], got["statement"], got["not_r14"], got["no_bot"], got["quiet"]),
              (False, True, False, False, 0))
        check("a yes under an OLDER day's post sends nothing and does not use up the offer; the same yes on its own "
              "day lists everyone due that day",
              (got["older_post"][0], got["older_post"][1], got["same_day_later"]),
              (0, "", [REST_HEAD + "\n" + "\n".join(f"{i}. **Person {i:02d}** (Co {i:02d})" for i in range(1, 8))]))


def sheet_lock(check):
    import sheet_locks_checks as slc

    print("\nTHE COLUMN LOCK — Reconnect Date is read, never written")
    with pinned(), slc.pinned():
        tab = slc.tab_for(HEADERS, **{"Reconnect Date": "13-12-2026", "Meeting Date": "01-09-2026"})
        col = tab.role_to_col.get("reconnect_date")
        ws = slc.Ws()
        with slc.writing(tab, ws):
            blocked = gtm_sheet.SHEETS.write_cells(row=2, values={"reconnect_date": "20-12-2026"},
                                                   expect_company="Acme Labs")
            control = gtm_sheet.SHEETS.write_cells(row=2, values={"meeting_status": "Held"}, expect_company="Acme Labs")
        check("lock: the Reconnect Date header maps to the reconnect_date role and its column is READ-ONLY on an existing "
              "row and closed on a new row",
              (HEADERS[col] if col is not None else None, col in tab.lock.restricted, col in tab.lock.new_row,
               col in tab.lock.writable(), tab.lock.unmapped), ("Reconnect Date", True, False, False, ()))
        check("lock: a write to it is REFUSED and nothing reaches the sheet; a writable cell beside it still writes",
              (blocked["ok"], [u for u in ws.updates if u[1] == "20-12-2026"], control["ok"]), (False, [], True))
        roles_without = [r for r in config.RESTRICTED_COLUMN_ROLES if r != "reconnect_date"]
        with pinned(RESTRICTED_COLUMN_ROLES=roles_without):
            spelled = slc.tab_for(HEADERS, **{"Reconnect Date": "13-12-2026"})
        check("lock: it is locked even when a .env spells the restricted roles out without it",
              spelled.role_to_col["reconnect_date"] in spelled.lock.restricted, True)
        without = [h for h in HEADERS if h != "Reconnect Date"]
        with tapped() as tap:
            gone = slc.tab_for(without)
            gtm_sheet.SHEETS.log_writable_window(gone)                    # what startup logs after the first read
        check("lock: with NO Reconnect Date column the lock does not fail closed (writes elsewhere go on), and startup "
              "logs an ERROR saying R14 cannot read it",
              (gone.lock.unmapped, gone.lock.failed,
               [m for lvl, m in tap.lines if lvl == "ERROR" and "[gtm.lock]" in m and "econnect" in m] != []),
              ((), False, True))
        decoy = list(HEADERS[:-1]) + ["Notes on follow up date"]
        check("sheet: only a header containing 'reconnect' can take the role (a look-alike header does not)",
              "reconnect_date" in slc.tab_for(decoy).role_to_col, False)
        renamed = list(HEADERS[:-1]) + ["Re-connect Date"]
        check("sheet: 'Re-connect Date' is found by its header text, wherever the team puts the column",
              "reconnect_date" in slc.tab_for(renamed).role_to_col, True)
        moved = ["Reconnect Date"] + list(HEADERS[:-1])
        tab_m = fx.parse([["13-12-2026"] + vrow(1, "Ann", "Acme")[:-1]], moved)
        check("sheet: a moved column is still read by role (the date comes from wherever the header is)",
              tab_m.rows[0].get("reconnect_date"), "13-12-2026")


def settings(check):
    print("\nSETTINGS, THE RULE AND THE WORDING")
    with pinned():
        check("config: RECONNECT_TIME is accepted from 14:00 to 16:00 and anything else falls back to 15:30",
              [config._reconnect_time(v) for v in ("14:00", "15:30", "16:00", "13:59", "16:01", "17:00", "9:00", "banana",
                                                   "")],
              ["14:00", "15:30", "16:00", "15:30", "15:30", "15:30", "15:30", "15:30", "15:30"])
        r14 = rules_mod.by_id("R14")
        order_lists = [r.id for r in rules_mod.safe_load() if False]
        in_orders = [d for d in range(7) if "R14" in rules_mod.order_for(date(2026, 10, 12) + timedelta(days=d))]
        check("rules: R14 is anchored (no weekday), five names, counts_toward_cap false, enabled, in no daily order",
              (r14.trigger, r14.weekdays, r14.max_items_per_post, r14.counts_toward_cap, r14.enabled, in_orders, order_lists),
              ("reconnect", (), 5, False, True, [], []))
        check("rules: 'reconnect' is a known trigger with a fixed time",
              ("reconnect" in rules_mod.KNOWN_TRIGGERS, "reconnect" in rules_mod.FIXED_TIME_TRIGGERS), (True, True))
        check("drip: R14 is one post per rule (group 'reconnect|*'), verbatim, never counted, never held",
              (drip.group_key({"type": nextaction.R_RECONNECT, "company": "A"}),
               nextaction.R_RECONNECT in drip.RULE_ONLY_TYPES, nextaction.R_RECONNECT in drip.NEVER_COUNTED,
               nextaction.R_RECONNECT in drip.NEVER_HELD, nextaction.R_RECONNECT in drip.VERBATIM_TYPES,
               drip.HEADINGS["R14"]), ("reconnect|*", True, True, True, True, "Time to reconnect"))
        lines = wording.all_lines()
        check("wording: every fixed line is in the register check",
              [l for l in wording.reconnect_lines_for_check() if l not in lines], [])
        check("wording: the ticket's lines, character for character (em dash U+2014)",
              (wording.reconnect_more(36), wording.reconnect_more(0), wording.reconnect_line("Dr. David Gamez",
                                                                                             "Middlesex University London"),
               wording.reconnect_post([{"name": "Daisy Leigh", "company": "PolyAI"}], more=3, tags="@Vaishnavi, @Sid")[0]),
              ("there are 36 more — would you like to look at the rest?", "",
               "**Dr. David Gamez** (Middlesex University London)",
               "Hey @Vaishnavi, @Sid — it's been a while since we last connected with these PoCs, do get in touch "
               "when you can:"))
        check("wording: with nobody due there is no post and no line", wording.reconnect_post([], more=0), [])
        check(".env.example lists RECONNECT_TIME=15:30 and SALES_API_CHANNEL_ID, uncommented",
              (_example("RECONNECT_TIME", ""), _example("SALES_API_CHANNEL_ID", "")[:10]), ("15:30", "<REQUIRED:"))
    code = ("import config;"
            "print(config.RECONNECT_TIME, config.SALES_API_CHANNEL_ID in config.SALES_CHANNEL_IDS,"
            " config.SALES_API_CHANNEL_ID)")
    done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                          env=dict(os.environ, RECONNECT_TIME="17:00", SALES_API_CHANNEL_ID="777000111",
                                   DISCORD_TOKEN="x", ANTHROPIC_API_KEY="x", PYTHONIOENCODING="utf-8"))
    check("config: RECONNECT_TIME=17:00 falls back to 15:30 with a warning; SALES_API_CHANNEL_ID is added to "
          "SALES_CHANNEL_IDS at import (the bot reads only those channels)",
          ((done.stdout or "").strip().splitlines()[-1:], "RECONNECT_TIME=17:00 is outside" in (
              (done.stderr or "") + (done.stdout or ""))), (["15:30 True 777000111"], True))
    done = subprocess.run([sys.executable, "-m", "reconnect"], cwd=ROOT, capture_output=True, text=True,
                          env=dict(os.environ, PYTHONIOENCODING="utf-8", DISCORD_TOKEN="x", ANTHROPIC_API_KEY="x"))
    check("reconnect.py's own self-test passes", (done.returncode, (done.stdout or "").strip().splitlines()[-1:]),
          (0, ["ALL PASSED"]))


def run(check):
    which_rule_wins(check)
    date_formats(check)
    timing(check)
    the_post(check)
    stopping(check)
    measured_shape(check)
    edge_cases(check)
    sheet_lock(check)
    settings(check)
