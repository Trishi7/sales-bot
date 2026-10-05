"""S1 — THE CAP, THE SCHEDULE, ONE REMINDER LANE, R9's LADDER, FREE SEARCH. Real output.

    python verify_s1.py            every check
    python verify_s1.py --only i,v some of them (0 i ii iii iv v vi 6 w)
    python verify_s1.py --no-ddg   skip the one live call in (vi), the ddg fallback

  (0)   the migration: an existing database gains drip_sends.counts_toward_cap
  (i)   a Monday with 6 countable groups + an R8 + an R9 -> 5 counted posts plus
        the R8 and the R9; the live sweep, the test day and the simulation send
        the same messages in the same order and make the same cap decisions
  (ii)  R1 posts Monday, Tuesday and Wednesday (and Thursday, Friday) in a
        simulated week
  (iii) a Sunday with a P1 due Monday -> one post, three ways; Saturday is
        silent; Monday's checklist still goes out
  (iv)  a reminder fires exactly once — on time, and late with "(this was due …)"
  (v)   an R9 contact climbs a rung on each follow-up, then the chase stops;
        Next Steps filled clears the ladder
  (vi)  a searxng query returns results, and with searxng stopped ddg answers
  (6)   the RSS news path runs with WEB_SEARCH_ENABLED=false
  (w)   the Bot Rules / Global Rules wording, printed from the rules the bot loads

NOTHING REACHES DISCORD AND NO SHEET IS READ. The bot is never logged in; every
send lands in a recording channel. The sheet is a FIXTURE built here — the
Outreach PoCs rows, the Deliverables Checklist and the Master Pipeline are
handed to the real `_run_next_actions`, so the real rules, the real planner and
the real sender do the work on known rows. Each run gets its own fresh database
in a temp folder. The settings the checks depend on (cap 5, the 14:00 window)
are SET HERE to the shipped defaults, so a local .env tuned for testing does
not change what is being proved.

THE MODEL IS AN ECHO (as in verify_parity.py): a real model does not write the
same sentence twice, so identical bodies could never be asserted of it. Stubbed
identically on every path, because they need Discord or spend budget: the leave
read, the channel-history evidence check, the web research, the hourly news
check and the approvals sweep (which is counted, to show it rides the first
post once a day on every path).

(vi) IS HONEST ABOUT WHAT IT IS: there is no Docker on this machine, so the
"searxng" in it is a small local server speaking SearXNG's JSON API on
127.0.0.1. It proves the bot's side — the GET it sends, the JSON it reads, the
fallback when the server is gone. It does not prove a deployed SearXNG; DEPLOY.md
section 4b has the one-line curl for that. The ddg half is a real, free request.
"""
import asyncio
import hashlib
import http.server
import json
import logging
import os
import random
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import urllib.parse
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ONLY: set = set()
for _i, _a in enumerate(sys.argv):
    if _a == "--only" and _i + 1 < len(sys.argv):
        ONLY = {x.strip().lower() for x in sys.argv[_i + 1].split(",") if x.strip()}
NO_DDG = "--no-ddg" in sys.argv

TMP = tempfile.mkdtemp(prefix="saley-s1-")
os.environ["STATE_DIR"] = os.path.join(TMP, "state")
os.environ["DB_PATH"] = os.path.join(TMP, "base_test.db")

import config  # noqa: E402

# THE SHIPPED DEFAULTS, pinned — not whatever the local .env says.
SETTINGS = {
    "DB_PATH": os.environ["DB_PATH"], "STATE_DIR": os.environ["STATE_DIR"],
    "DAILY_MESSAGE_CAP": 5, "SALES_DRIP_START": "14:00", "SALES_DRIP_END": "18:30",
    "MESSAGE_GAP_MINUTES": 90, "MESSAGE_JITTER_MINUTES": 15,
    "MESSAGE_GAP_MIN_MINUTES": 30, "DRIP_REASK_DAYS": 2, "DRIP_WEEKDAYS_ONLY": True,
    "SUNDAY_RULE_IDS": ["R4"], "NEWS_MAIN_TIME": "14:00", "MEETING_DAYOF_TIME": "10:00",
    "TEST_MORNING_TIME": "10:00", "TEST_AFTERNOON_TIME": "14:00",
    "MEETING_FOLLOWUP_AFTER_DAYS": 3, "MEETING_FOLLOWUP_EVERY_DAYS": 3,
    "MEETING_FOLLOWUP_LADDER": ["channel", "dm", "dm", "escalation"],
    "SALES_DMS_ENABLED": False, "NEXT_ACTION_ENABLED": True,
    "NEXT_ACTION_WEEKEND_SHIFT": True, "DELIVERABLE_NEAR_DAYS": 3,
    "DM_NO_MEETING_DAYS": 7, "CLOSURE_SUPPORT_MIN": 50, "NEW_COMPANY_WINDOW_DAYS": 7,
    "WEEKLY_FUNNEL_ENABLED": True, "WEEKLY_FUNNEL_WEEKDAY": 0, "EVENTS_ENABLED": False,
    "SHEET_WRITES_ENABLED": False, "DRIP_LLM_COMPOSE": True,
    "TEST_POST_GAP_SECONDS": 0, "SIMULATION_FAST_GAP_SECONDS": 0,
    "SIMULATION_REAL_MENTIONS": False, "REMINDER_DEFAULT_TIME": "14:00",
}
for _k, _v in SETTINGS.items():
    setattr(config, _k, _v)
config.digest_enabled = lambda: True      # the kill switch is not what is tested

import activation  # noqa: E402
import clock  # noqa: E402
import deadlines as dl  # noqa: E402
import drip  # noqa: E402
import events as events_mod  # noqa: E402
import gtm_sheet  # noqa: E402
import leave  # noqa: E402
import nextaction  # noqa: E402
import persona  # noqa: E402
import rules as rules_mod  # noqa: E402
import search_backend  # noqa: E402
import simulation  # noqa: E402
import tone  # noqa: E402
import voice  # noqa: E402
from db import DB  # noqa: E402

simulation.pace_seconds = lambda **_k: 0.0

failures = 0
POSTED: list = []         # every body any path "sent"
SENT: list = []           # one dict per drip message, for the path being run
SWEEPS: list = []         # the approvals sweep's calls: the day each one rode
LOGS: list = []           # log lines worth quoting
FRESH = [0]               # a counter, so no two runs share a database file


def want(name: str) -> bool:
    return not ONLY or name.lower() in ONLY


def check(name, got, expected=True):
    global failures
    ok = (got == expected)
    failures += 0 if ok else 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}"
          + ("" if ok else f": got {got!r}, want {expected!r}"))


def say(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


class Capture(logging.Handler):
    KEEP = ("[rules] R9 ladder", "[reminders] #", "[drip] ", "[search]", "[news",
            "[test-day]", "[sim]")

    def emit(self, record):
        try:
            line = record.getMessage()
        except Exception:
            return
        if line.startswith(self.KEEP):
            LOGS.append(line)


root = logging.getLogger()
root.setLevel(logging.INFO)
for h in list(root.handlers):
    root.removeHandler(h)
root.addHandler(Capture())
for noisy in ("discord", "asyncio", "httpx", "httpcore", "anthropic", "googleapiclient",
              "google", "urllib3", "ddgs", "primp", "rquest", "cookie_store"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


# -- Discord, recorded ---------------------------------------------------------


class FakeTyping:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeChannel:
    name = "recording"
    guild = SimpleNamespace(id=1)

    def __init__(self, cid):
        self.id = int(cid)

    async def send(self, body):
        POSTED.append(str(body))
        return SimpleNamespace(id=len(POSTED), jump_url="https://discord/x")

    def typing(self):
        return FakeTyping()


TEST_CHANNEL = FakeChannel(config.SALES_TEST_CHANNEL_ID or 4242)


class FakeAuthor:
    id = int((list(config.approver_ids() or []) or [111])[0])
    display_name = "verify_s1"
    name = "verify_s1"
    bot = False


class FakeMessage:
    id = 999
    channel = TEST_CHANNEL
    author = FakeAuthor()
    content = ""

    async def reply(self, body, mention_author=False):
        POSTED.append("REPLY: " + str(body))
        return SimpleNamespace(id=len(POSTED), jump_url="https://discord/x")


class EchoMessages:
    """The composer, replaced by a fingerprint of what the composer was given."""

    def create(self, **kw):
        system = persona.blocks_text(kw.get("system"))
        prompt = str((kw.get("messages") or [{}])[-1].get("content") or "")
        text = "{}"
        if prompt.startswith("Write ONE short proactive message"):
            ref = hashlib.sha1((system + "\n@@\n" + prompt).encode("utf-8")).hexdigest()[:10]
            m = re.search(r"Address them as EXACTLY this, once, at the start: (.+)", prompt)
            who = (m.group(1).strip() if m else "")
            who = "" if who.startswith("(") else who + ", "
            block = re.search(r"<<<\n(.*?)\n>>>", prompt, re.S)
            if block and "ALREADY HAS ITS OPENER" in prompt:
                text = f"{block.group(1)}\nwant me to look into these, echo {ref}?"
            elif block:
                text = (f"{who}here's the list, echo {ref}.\n{block.group(1)}\n"
                        "tell me if any of it has moved.")
            else:
                c = re.search(r"in one sentence\): (.+)", prompt)
                text = (f"{who}{(c.group(1).strip() if c else 'this one')} could use a "
                        f"look this week, echo {ref}. no rush.")
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=1, output_tokens=1))


def strip(body: str) -> str:
    """A body with the "[TEST]" prefix off and every mention written one way."""
    out = str(body or "")
    tag = str(config.SIMULATION_PREFIX or "").strip()
    if tag and out.startswith(tag):
        out = out[len(tag):].lstrip()

    def _name(m):
        known = str(config.ROSTER_DISPLAY_NAMES.get(str(m.group(1))) or "").strip()
        return f"@{known}" if known else f"@user{m.group(1)}"

    return re.sub(r"<@!?(\d+)>", _name, out)


# -- the fixture sheet ---------------------------------------------------------

MON = date(2026, 9, 28)                 # the pretend Monday (already in the past)
SAT, SUN = MON - timedelta(days=2), MON - timedelta(days=1)
SHEET = {"rows": [], "deliverables": [], "pipeline": [], "funnel": {}}


def cell(d: date) -> str:
    return d.strftime("%d-%m-%Y")


def poc(n, company, name, **kw):
    """One Outreach PoCs row. First contact is already made on all of them, so
    R5 (prospects) does not also pick the contact and win the one-mention-a-day
    dedup over the rule the row is here to exercise."""
    r = {"_row": n, "_extra": {}, "company": company, "name": name,
         "first_contact": "TRUE"}
    r.update(kw)
    return r


def deliverable(n, title, pri, status, due, team=""):
    return {"_row": n, "_extra": {}, "action_item": title, "priority": pri,
            "status": status, "deadline": f"{due.day}-{due.strftime('%b')}",
            "dependency": team, "remarks": "", "link": ""}


def monday_sheet(day: date) -> None:
    """Six countable groups on a Monday — R1, R4, R7, R10, R11 and the weekly
    line — plus one meeting today (R8) and one meeting done with no next steps
    (R9)."""
    SHEET["rows"] = [
        poc(2, "Echo Labs", "Eve Rao", designation="CTO",
            li_dm_date=cell(day - timedelta(days=11))),                      # R7
        poc(3, "Fathom AI", "Fay Lin", designation="Founder",
            prospect_status="Demo", closure_prob="70%"),                     # R10
        poc(4, "Gantry Systems", "Gil Shah", designation="Head of ML",
            meeting_date=cell(day)),                                         # R8, day-of
        poc(5, "Harbor Robotics", "Hal Iyer", designation="CEO",
            meeting_date=cell(day - timedelta(days=5)),
            meeting_status="Completed"),                                     # R9
    ]
    SHEET["deliverables"] = [
        deliverable(2, "Pulse Product Overview Document", "P1", "In progress",
                    day - timedelta(days=3), "Sales"),
        deliverable(3, "MSA template", "P1", "", day + timedelta(days=3), "Legal"),
        deliverable(4, "Case study: Hinglish STT", "P2", "", day + timedelta(days=2)),
    ]
    SHEET["pipeline"] = ["Old Co", "Nova Robotics"]
    SHEET["funnel"] = {"outreach_sent": 12, "replies": 4, "meetings_booked": 2,
                       "followups_done": 5, "pilots": 1}


def seed(path: str, day: date) -> None:
    """A database in which "Nova Robotics" first appeared in the pipeline on the
    Friday before `day`, so R11 is due on the Monday."""
    db = DB(path)
    db.pipeline_snapshot(["Old Co"], today=dl.iso(day - timedelta(days=14)))
    db.pipeline_snapshot(["Old Co", "Nova Robotics"],
                         today=dl.iso(day - timedelta(days=3)))


def fresh_db(name: str, *, seeded_for: date = None) -> str:
    FRESH[0] += 1
    path = os.path.join(TMP, f"{name}{FRESH[0]}_test.db")
    if seeded_for is not None:
        seed(path, seeded_for)
    config.DB_PATH = path
    clock.forget()
    voice.invalidate()
    return path


def new_bot(path: str, *, news_stubbed: bool = True):
    from bot import SalesBot

    bot = SalesBot()
    bot.db = DB(path)
    if bot.llm is not None:
        bot.llm._client = SimpleNamespace(messages=EchoMessages())

    async def nobody_away(*_a, **_k):
        return {}

    async def nothing(*_a, **_k):
        return None

    async def unresearched(message, *, today):
        return message

    async def swept(*, today, channel):
        SWEEPS.append(dl.iso(today))
        return False

    async def tab_rows(kind, for_rules):
        if kind == gtm_sheet.DELIVERABLES:
            return list(SHEET["deliverables"])
        if kind == gtm_sheet.RESEARCHER_LINES:
            return [{"_row": i + 2, "company": c} for i, c in enumerate(SHEET["pipeline"])]
        return []

    async def funnel(*, today):
        return events_mod.funnel_action(dict(SHEET["funnel"]), today=today)

    real_send = bot._send_drip_message

    async def recording_send(channel, message, **kw):
        start = len(POSTED)
        await real_send(channel, message, **kw)
        SENT.append({
            "day": kw.get("marker"), "slot": message.get("slot"),
            "type": message.get("type"), "rule": message.get("rule_id") or "—",
            "counted": drip.counts(message), "pinned": bool(message.get("pinned")),
            "at": message.get("send_at_hhmm"), "stage": message.get("stage"),
            "posts": list(POSTED[start:]),
        })

    leave.who_is_away = nobody_away
    gtm_sheet.SHEETS.cadence_tab = lambda *a, **k: (
        SimpleNamespace(rows=list(SHEET["rows"]), title="Outreach PoCs"), "fixture")
    bot._split_active = lambda rows, why: (list(rows), [])
    bot._rule_tab_rows = tab_rows
    bot._funnel_action = funnel
    bot._send_drip_message = recording_send
    bot._research_message = unresearched
    bot._convert_if_already_done = nothing
    bot._sweep_proposals = swept
    if news_stubbed:
        bot._maybe_breaking_news = nothing
    bot.get_channel = lambda cid: FakeChannel(cid)
    return bot


def begin(label: str) -> None:
    POSTED.clear()
    SENT.clear()
    SWEEPS.clear()
    LOGS.clear()
    tone.RNG = random.Random(20260928)
    voice._last_choice.clear()
    print(f"\n   ---- {label} ----")


def pretend(day: date, hh: int = 9, mm: int = 0) -> None:
    """Stand the bot's clock at `day` HH:MM. The pretend clock owns the date;
    the time of day is a stop set on top of it, as the test day does."""
    was = config.SALES_TEST_MODE
    config.SALES_TEST_MODE = True              # only to be allowed to set the day
    clock.set_day(day, by="verify_s1")
    config.SALES_TEST_MODE = was
    clock.set_time_of_day(time(hh, mm), by="verify_s1")


def decisions(planned: dict) -> dict:
    """What a plan decided: who goes (and whether each counts), who rolls, who
    is held. The thing "the same cap decisions" is asserted of."""
    planned = planned or {}
    return {
        "go": [(m.get("rule_id") or m["type"], m["type"], bool(m["counts_toward_cap"]),
                bool(m.get("pinned")), m["send_at_hhmm"])
               for m in planned.get("messages") or []],
        "rolled": sorted((g["type"], g.get("rolled_why", "")) for g in
                         planned.get("rolled") or []),
        "held": sorted(g["type"] for g in planned.get("held") or []),
        "counted": planned.get("counted"), "cap": planned.get("cap"),
    }


def collect(planned: dict) -> dict:
    return {
        "sent": [(s["type"], [strip(p) for p in s["posts"]]) for s in SENT],
        "raw": [dict(s) for s in SENT],
        "decisions": decisions(planned),
        "sweeps": list(SWEEPS),
    }


async def live_path(day: date, *, seeded: bool = True, label: str = "") -> dict:
    """The live sweep's own method, ticked through the day. A dry run: every
    send lands in the recording channel."""
    begin(label or f"LIVE (dry run) — _maybe_send_drip ticked through {dl.iso(day)}")
    path = fresh_db("live", seeded_for=day if seeded else None)
    config.SALES_TEST_MODE = False
    bot = new_bot(path)
    pretend(day, 9, 0)
    bot._live_loop_held = lambda what: False     # the live loop IS the thing under test
    planned = await bot._plan_drip(today=day, already=[])
    times = [m["send_at"] for m in (planned or {}).get("messages") or []]
    step = timedelta(minutes=drip.min_gap_minutes() + 1)
    ticks: list = []
    at = None
    for target in times + [None, None]:
        base = target or (at + step if at else datetime.combine(day, time(22, 0), dl.IST))
        at = base if at is None else max(base, at + step)
        at = min(at, datetime.combine(day, time(23, 58), dl.IST))
        clock.set_time_of_day(at.timetz().replace(tzinfo=None), by="verify_s1")
        before = len(SENT)
        await bot._maybe_send_drip()
        ticks.append((at.strftime("%H:%M"), len(SENT) - before))
    clock.clear_time_override(why="verify_s1")
    got = collect(planned)
    got["ticks"] = ticks
    got["rows"] = bot.db.drip_sent_today(dl.iso(day))
    return got


async def test_path(day: date, *, bot=None, seeded: bool = True, label: str = "") -> dict:
    begin(label or f"TEST DAY — \"make it {dl.iso(day)}\"")
    config.SALES_TEST_MODE = True
    if bot is None:
        bot = new_bot(fresh_db("testday", seeded_for=day if seeded else None))
    await bot._handle_test_command(FakeMessage(), "make it " + dl.iso(day))
    got = collect((bot._last_test_plan or {}).get("planned"))
    got["bot"] = bot
    got["note"] = (bot._last_test_plan or {}).get("note", "")
    return got


async def sim_path(day: date, *, command: str = "", seeded: bool = True,
                   label: str = "") -> dict:
    command = command or "simulate " + dl.iso(day)
    begin(label or f"SIMULATION — \"{command}\"")
    config.SALES_TEST_MODE = True
    bot = new_bot(fresh_db("sim", seeded_for=day if seeded else None))
    await bot._handle_simulation(FakeMessage(), command)
    return collect((bot._last_test_plan or {}).get("planned"))


def show_run(run: dict) -> None:
    for s in run["raw"]:
        first = (s["posts"][0] if s["posts"] else "(nothing posted)").splitlines()
        head = next((line for line in first if line.strip()), "")
        print(f"     slot {s['slot']}  {s['at']}  {s['rule']:<3} {s['type']:<20} "
              f"{'counted' if s['counted'] else 'OUTSIDE the cap':<16}"
              f"{' fixed time' if s['pinned'] else '':<11} | {head[:56]}")


# -- (0) the migration ---------------------------------------------------------


def migration() -> None:
    say("(0) THE MIGRATION — an existing database gains counts_toward_cap")
    source = os.path.join(HERE, "sales_bot_test.db")
    if not os.path.exists(source):
        print("   no sales_bot_test.db here to migrate; skipped")
        return
    path = os.path.join(TMP, "migrate_test.db")
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(source + suffix):
            shutil.copy2(source + suffix, path + suffix)
    con = sqlite3.connect(path)
    before = [r[1] for r in con.execute("PRAGMA table_info(drip_sends)")]
    rows_before = con.execute("SELECT COUNT(*) FROM drip_sends").fetchone()[0]
    con.close()

    db = DB(path)                                   # the "columns added later" pass
    con = sqlite3.connect(path)
    after = [r[1] for r in con.execute("PRAGMA table_info(drip_sends)")]
    nulls = con.execute(
        "SELECT COUNT(*) FROM drip_sends WHERE counts_toward_cap IS NULL").fetchone()[0]
    last = con.execute("SELECT on_date FROM drip_sends ORDER BY on_date DESC "
                       "LIMIT 1").fetchone()
    con.close()
    print(f"   a copy of sales_bot_test.db: {rows_before} drip_sends row(s)")
    print(f"   columns before: {', '.join(before)}")
    print(f"   columns added : {', '.join(c for c in after if c not in before) or '(none)'}")
    check("counts_toward_cap and pinned are there after opening it",
          ("counts_toward_cap" in after, "pinned" in after), (True, True))
    check("every older row carries NULL, not a guess", nulls, rows_before)
    if last:
        rows = db.drip_sent_today(last[0])
        types = sorted({r["action_type"] for r in rows})
        outside = [r for r in rows if r["action_type"] in drip.NEVER_COUNTED]
        print(f"   {last[0]}: {len(rows)} older row(s) of type(s) {types}; "
              f"counted_today -> {drip.counted_today(rows)}")
        check("an older row is counted by its rule's own flag",
              drip.counted_today(rows), sum(1 for r in rows if drip.counts(r)))
        check("...and no older meeting-prep or follow-up row is counted",
              [r for r in outside if drip.counts(r)], [])

    db.record_drip_send(on_date="2000-01-03", slot=1, group_key="meeting_prep|",
                        action_type="meeting_prep", sent_at="2000-01-03T10:00:00",
                        counts_toward_cap=False, pinned=True)
    db.record_drip_send(on_date="2000-01-03", slot=2, group_key="dm_no_meeting|",
                        action_type="dm_no_meeting", sent_at="2000-01-03T14:00:00",
                        counts_toward_cap=True)
    new = db.drip_sent_today("2000-01-03")
    print("   two new rows written and read back: "
          + ", ".join(f"{r['action_type']} counts={r['counts_toward_cap']} "
                      f"pinned={r['pinned']}" for r in new))
    check("a new row carries its own answer",
          [(r["counts_toward_cap"], r["pinned"]) for r in new], [(0, 1), (1, 0)])
    check("counted_today reads it: 1 of the 2", drip.counted_today(new), 1)


# -- (i) the Monday ------------------------------------------------------------


async def the_monday() -> None:
    say(f"(i) A MONDAY WITH 6 COUNTABLE GROUPS + AN R8 + AN R9 — {MON:%A %d %b %Y}, "
        f"cap {config.DAILY_MESSAGE_CAP}")
    monday_sheet(MON)

    live = await live_path(MON)
    show_run(live)
    print("     ticks (time -> messages sent): "
          + ", ".join(f"{t}->{n}" for t, n in live["ticks"]))
    test = await test_path(MON)
    show_run(test)
    sim = await sim_path(MON)
    show_run(sim)

    d = live["decisions"]
    print("\n   the plan the live sweep made:")
    for rule, kind, counted, pinned, hhmm in d["go"]:
        print(f"     go    {hhmm}  {rule:<3} {kind:<20} "
              f"{'counted' if counted else 'outside the cap'}"
              f"{', fixed time' if pinned else ''}")
    for kind, why in d["rolled"]:
        print(f"     ROLL  {kind} ({why}) — over the cap, goes tomorrow")
    for kind in d["held"]:
        print(f"     held  {kind}")
    print(f"     counted {d['counted']} of cap {d['cap']}")

    groups = len(d["go"]) + len(d["rolled"]) + len(d["held"])
    check("8 groups were eligible: 6 that count, the R8 and the R9", groups, 8)
    check("5 counted posts went out — the cap, exactly",
          sum(1 for s in live["raw"] if s["counted"]), 5)
    check("...plus the R8 and the R9, outside it",
          sorted(s["rule"] for s in live["raw"] if not s["counted"]), ["R8", "R9"])
    check("7 messages in all", len(live["raw"]), 7)
    check("exactly one group rolled, and it rolled for the cap",
          [why for _k, why in d["rolled"]], ["cap"])
    check("nothing was held", d["held"], [])
    r1 = [s for s in live["raw"] if s["rule"] == "R1"]
    check("R1 went, at NEWS_MAIN_TIME, as a fixed-time post that counts",
          [(s["at"], s["pinned"], s["counted"]) for s in r1],
          [(config.NEWS_MAIN_TIME, True, True)])
    check("R8's day-of touch went first, at MEETING_DAYOF_TIME",
          (live["raw"][0]["rule"], live["raw"][0]["at"]),
          ("R8", config.MEETING_DAYOF_TIME))
    check("...and the live sweep sent it at 10:00, before the window opened",
          live["ticks"][0], (config.MEETING_DAYOF_TIME, 1))
    check("the two ticks after the last post sent nothing",
          [n for _t, n in live["ticks"][-2:]], [0, 0])

    rows = live["rows"]
    print("\n   drip_sends after the live day:")
    for r in rows:
        print(f"     slot {r['slot']}  planned {r['planned_at']}  {r['action_type']:<20} "
              f"counts_toward_cap={r['counts_toward_cap']} pinned={r['pinned']}")
    check("every row has counts_toward_cap filled in",
          [r["counts_toward_cap"] for r in rows if r["counts_toward_cap"] is None], [])
    check("counted_today(rows) is 5", drip.counted_today(rows), 5)
    check("the day is full, by the one counter", drip.cap_reached(rows, MON), True)

    print("\n   THREE WAYS")
    check("the same messages, in the same order, with identical bodies "
          "(apart from [TEST] and mentions)",
          live["sent"] == test["sent"] == sim["sent"])
    if not (live["sent"] == test["sent"] == sim["sent"]):
        for name, run in (("live", live), ("test", test), ("sim", sim)):
            print(f"     {name}: " + ", ".join(k for k, _p in run["sent"]))
    check("the same cap decisions: who goes, who counts, who rolls, who is held",
          live["decisions"] == test["decisions"] == sim["decisions"])
    check("the live run carries no [TEST] tag; the other two tag every message",
          (any(p.startswith("[TEST]") for s in live["raw"] for p in s["posts"]),
           all(p.startswith("[TEST]") for s in test["raw"] for p in s["posts"]),
           all(p.startswith("[TEST]") for s in sim["raw"] for p in s["posts"])),
          (False, True, True))
    check("the approvals sweep rode the first post once on each path (and took "
          "no slot)", (live["sweeps"], test["sweeps"], sim["sweeps"]),
          ([dl.iso(MON)], [dl.iso(MON)], [dl.iso(MON)]))

    print("\n   one message, as posted on each path:")
    pick = next((i for i, s in enumerate(live["raw"]) if s["rule"] == "R9"), 0)
    for name, run in (("live", live), ("test", test), ("sim", sim)):
        if pick < len(run["raw"]):
            for line in "\n".join(run["raw"][pick]["posts"]).splitlines():
                print(f"     {name:<4} | {line}")


# -- (ii) the week -------------------------------------------------------------


async def the_week() -> None:
    say("(ii) R1 EVERY WEEKDAY — \"simulate week\" of " + dl.iso(MON))
    monday_sheet(MON)
    was = simulation.this_week_day
    # "THIS WEEK" IS THE FIXTURE'S WEEK, so the command is the plain one a tester
    # types and the dates are the ones the fixture was built around.
    simulation.this_week_day = lambda index, **_k: MON + timedelta(days=int(index))
    try:
        week = await sim_path(MON, command="simulate week")
    finally:
        simulation.this_week_day = was

    by_day: dict = {}
    for s in week["raw"]:
        by_day.setdefault(s["day"], []).append(s)
    days = [MON + timedelta(days=i) for i in range(7)]
    for day in days:
        got = by_day.get(dl.iso(day), [])
        print(f"   {day:%a %d %b}: " + (", ".join(
            f"{s['rule']}@{s['at']}" + ("" if s["counted"] else "*") for s in got)
            or "(nothing)"))
    print("   (* = outside the cap)")

    r1_days = [d.strftime("%a") for d in days
               if any(s["rule"] == "R1" for s in by_day.get(dl.iso(d), []))]
    check("R1 posted Monday, Tuesday and Wednesday", r1_days[:3], ["Mon", "Tue", "Wed"])
    check("...and Thursday and Friday: every weekday, never held", r1_days,
          ["Mon", "Tue", "Wed", "Thu", "Fri"])
    r1 = [s for s in week["raw"] if s["rule"] == "R1"]
    check("always a fresh post, never a \"re-ask\"",
          sorted({s["stage"] for s in r1}), [drip.STAGE_NUDGE])
    check("always at NEWS_MAIN_TIME", sorted({s["at"] for s in r1}),
          [config.NEWS_MAIN_TIME])
    check("no day went over the cap",
          max(sum(1 for s in v if s["counted"]) for v in by_day.values()) <= 5)
    check("no 2 PM post at the weekend",
          [s for d in days[5:] for s in by_day.get(dl.iso(d), []) if s["rule"] == "R1"],
          [])

    key = "ai_news|"
    before = [drip.stage_for(key, history={key: {"last_nudge": dl.iso(MON)}},
                             today=MON + timedelta(days=n))[0] for n in (1, 2)]
    print(f"   for comparison, the re-ask clock R1 used to go through: Tuesday -> "
          f"{before[0]!r} (held), Wednesday -> {before[1]!r}")


# -- (iii) the Sunday ----------------------------------------------------------


async def the_sunday() -> None:
    say(f"(iii) A SUNDAY WITH A P1 DUE MONDAY — {SUN:%A %d %b %Y}")
    SHEET["rows"] = [
        poc(4, "Gantry Systems", "Gil Shah", meeting_date=cell(MON)),
        poc(5, "Harbor Robotics", "Hal Iyer", meeting_date=cell(SUN - timedelta(days=6)),
            meeting_status="Completed"),        # an R9 that is due — and must wait
    ]
    SHEET["deliverables"] = [
        deliverable(2, "MSA template", "P1", "", MON, "Legal"),
        deliverable(3, "Pricing one-pager", "P1", "", MON + timedelta(days=3), "Sales"),
        deliverable(4, "Case study: Hinglish STT", "P2", "", MON),
    ]
    SHEET["pipeline"], SHEET["funnel"] = [], {}
    print(f"   is_sending_day: Saturday {drip.is_sending_day(SAT)}, Sunday "
          f"{drip.is_sending_day(SUN)} (SUNDAY_RULE_IDS={config.SUNDAY_RULE_IDS})")
    print("   rules that get to look on a Sunday: "
          + ", ".join(r.id for r in rules_mod.for_day(SUN)))

    live = await live_path(SUN, seeded=False)
    show_run(live)
    print("     ticks: " + ", ".join(f"{t}->{n}" for t, n in live["ticks"]))
    test = await test_path(SUN, seeded=False)
    show_run(test)
    sim = await sim_path(SUN, seeded=False)
    show_run(sim)

    check("one post on the live path", [s["rule"] for s in live["raw"]], ["R4"])
    check("...at SALES_DRIP_START", [s["at"] for s in live["raw"]],
          [config.SALES_DRIP_START])
    check("...and the later ticks sent nothing more",
          sum(n for _t, n in live["ticks"]), 1)
    check("the same one post on the test day and in the simulation",
          live["sent"] == test["sent"] == sim["sent"])
    check("the same decisions: one goes, the R9 that was due waits for Monday",
          (live["decisions"] == test["decisions"] == sim["decisions"],
           live["decisions"]["held"]), (True, [nextaction.R_MEETING_FOLLOWUP]))
    body = "\n".join(live["raw"][0]["posts"]) if live["raw"] else ""
    for line in body.splitlines():
        print("     | " + line)
    check("it names the P1 due Monday", "MSA template" in body)
    check("...not the P1 due Thursday, and never the P2",
          [t for t in ("Pricing one-pager", "Hinglish") if t in body], [])

    # THE NEXT MORNING, on the test day's own database: Sunday's row is there.
    monday = await test_path(
        MON, bot=test["bot"],
        label=f"THE MONDAY AFTER — \"make it {dl.iso(MON)}\" on the same database")
    show_run(monday)
    check("Monday's checklist still goes out — Sunday's heads-up did not hold it",
          [s["rule"] for s in monday["raw"] if s["rule"] == "R4"], ["R4"])
    r4 = next(("\n".join(s["posts"]) for s in monday["raw"] if s["rule"] == "R4"), "")
    check("...and it is the whole week: both open P1s",
          [t for t in ("MSA template", "Pricing one-pager") if t in r4],
          ["MSA template", "Pricing one-pager"])

    # A SUNDAY WITH NOTHING DUE MONDAY, and a Saturday.
    SHEET["deliverables"] = [deliverable(3, "Pricing one-pager", "P1", "",
                                         MON + timedelta(days=3), "Sales")]
    quiet = await live_path(SUN, seeded=False,
                            label="A SUNDAY WITH NOTHING DUE MONDAY — live")
    check("silent", len(quiet["raw"]), 0)
    SHEET["deliverables"] = [deliverable(2, "MSA template", "P1", "", MON, "Legal")]
    sat_live = await live_path(SAT, seeded=False, label="THE SATURDAY — live")
    sat_test = await test_path(SAT, seeded=False, label="THE SATURDAY — test day")
    check("Saturday is silent on the live path and on the test day",
          (len(sat_live["raw"]), len(sat_test["raw"])), (0, 0))
    print(f"   why was it quiet: {sat_test['note']}")


# -- (iv) reminders ------------------------------------------------------------


async def the_reminders() -> None:
    say("(iv) A REMINDER FIRES EXACTLY ONCE — one lane")
    monday_sheet(MON)
    begin(f"LIVE — the exact-minute loop on {dl.iso(MON)}")
    path = fresh_db("reminders", seeded_for=MON)
    config.SALES_TEST_MODE = False
    bot = new_bot(path)
    bot._live_loop_held = lambda what: False
    echo = SHEET["rows"][0]
    on_time = bot.db.add_scheduled_reminder(
        row_key=activation.row_key(echo), company="Echo Labs", poc="Eve Rao",
        due_date=dl.iso(MON), due_time="15:00", what="send Echo Labs the pricing deck",
        requested_by="Kushal", on_date=dl.iso(MON - timedelta(days=3)))
    late = bot.db.add_scheduled_reminder(
        due_date=dl.iso(SAT), due_time="10:00", what="chase the Fathom NDA",
        requested_by="Vaishnavi", on_date=dl.iso(MON - timedelta(days=4)))
    print(f"   #{on_time}: due {dl.iso(MON)} 15:00, attached to the Echo Labs row "
          "(the kind the drip used to post as well)")
    print(f"   #{late}: due {dl.iso(SAT)} 10:00 — its date passed while nobody was "
          "looking")

    def reminder_posts() -> list:
        return [p for p in POSTED if "pricing deck" in p or "Fathom NDA" in p]

    pretend(MON, 9, 0)
    first = await bot._fire_due_reminders()
    print(f"   Mon 09:00 tick fired {first}")
    for p in reminder_posts():
        for line in p.splitlines():
            print("     | " + line)
    check("the late one fired on the first tick, and only it", first, [late])
    check("...saying when it was due",
          f"(this was due {dl.format_date(SAT)})" in "\n".join(reminder_posts()))
    again = await bot._fire_due_reminders()
    check("the next tick fires nothing — never twice", again, [])
    check("it is closed", bot.db.scheduled_reminder(late)["status"], "done")

    queue = await bot._run_next_actions(today=MON)
    kinds = sorted({a["type"] for a in queue["actions"]})
    print(f"   the drip's queue for the day: {kinds}")
    check("the drip's queue carries no reminder — it is not the drip's to send",
          nextaction.SCHEDULED_REMINDER in kinds, False)
    check("...and the rules report no reminder lane",
          [r["name"] for r in queue["rules_run"] if "Reminder" in r["name"]], [])

    pretend(MON, 14, 59)
    early = await bot._fire_due_reminders()
    pretend(MON, 15, 0)
    due = await bot._fire_due_reminders()
    pretend(MON, 15, 1)
    after = await bot._fire_due_reminders()
    print(f"   Mon 14:59 -> {early}   15:00 -> {due}   15:01 -> {after}")
    print("     | " + reminder_posts()[-1].replace("\n", "\n     | "))
    check("not a minute early, then once, then never again",
          (early, due, after), ([], [on_time], []))
    check("the on-time one carries no \"this was due\"",
          "this was due" in reminder_posts()[-1], False)
    check("two reminders, two posts — one each",
          (len(reminder_posts()), sum("pricing deck" in p for p in POSTED),
           sum("Fathom NDA" in p for p in POSTED)), (2, 1, 1))

    # ...AND THE DAY'S DRIP, on the same database: no second copy of either.
    clock.clear_time_override(why="verify_s1")
    planned = await bot._plan_drip(today=MON, already=[])
    step = timedelta(minutes=drip.min_gap_minutes() + 1)
    at = None
    for target in [m["send_at"] for m in planned["messages"]]:
        at = target if at is None else max(target, at + step)
        clock.set_time_of_day(at.timetz().replace(tzinfo=None), by="verify_s1")
        await bot._maybe_send_drip()
    clock.clear_time_override(why="verify_s1")
    print(f"   the same day's drip then sent {len(SENT)} message(s): "
          + ", ".join(s["rule"] for s in SENT))
    check("still one post each after the whole drip day",
          (sum("pricing deck" in p for p in POSTED),
           sum("Fathom NDA" in p for p in POSTED)), (1, 1))
    check("neither reminder is open",
          [r["id"] for r in bot.db.list_scheduled_reminders()], [])
    for line in [x for x in LOGS if x.startswith("[reminders] #")]:
        print("   log: " + line)


# -- (v) R9's ladder -----------------------------------------------------------


async def the_ladder() -> None:
    say("(v) R9 — ONE RUNG PER FOLLOW-UP, THEN STOP")
    harbor = poc(5, "Harbor Robotics", "Hal Iyer", designation="CEO",
                 meeting_date=cell(MON - timedelta(days=5)), meeting_status="Completed")
    SHEET["rows"] = [harbor]
    SHEET["deliverables"], SHEET["pipeline"], SHEET["funnel"] = [], [], {}
    key = activation.row_key(harbor)
    ladder = list(config.MEETING_FOLLOWUP_LADDER)
    print(f"   {harbor['company']} · {harbor['name']}: meeting {harbor['meeting_date']} "
          f"completed, no next steps. Ladder: {' > '.join(ladder)}, every "
          f"{config.MEETING_FOLLOWUP_EVERY_DAYS} days.")

    config.SALES_TEST_MODE = True
    bot = new_bot(fresh_db("ladder"))
    days = [MON, MON + timedelta(days=1), MON + timedelta(days=3),
            MON + timedelta(days=7), MON + timedelta(days=10), MON + timedelta(days=14)]
    rungs: list = []
    for day in days:
        run = await test_path(day, bot=bot, label=f"test day {day:%a %d %b}")
        posts = [s for s in run["raw"] if s["rule"] == "R9"]
        state = bot.db.meeting_followups().get(key) or {}
        rungs.append((len(posts), int(state.get("sent") or 0)))
        if posts:
            where = "outside the cap" if not posts[0]["counted"] else "COUNTED"
            print(f"     R9 posted ({where}) -> ladder now at rung {state.get('sent')} "
                  f"of {len(ladder)}, last {state.get('last_iso')}")
            for line in "\n".join(posts[0]["posts"]).splitlines()[:4]:
                print("       | " + line)
        else:
            print(f"     no R9 post -> ladder stays at rung {state.get('sent', 0)}")
        for line in [x for x in LOGS if x.startswith("[rules] R9 ladder")]:
            print("       log: " + line)
    check("a rung climbed on each follow-up, and only then", rungs,
          [(1, 1), (0, 1), (1, 2), (1, 3), (1, 4), (0, 4)])
    check("four follow-ups — the ladder's length — and then it stopped",
          (sum(p for p, _r in rungs), rungs[-1][0]), (len(ladder), 0))

    print("\n   NEXT STEPS FILLED — the chase is over")
    bot2 = new_bot(fresh_db("ladder2"))
    met = dl.iso(MON - timedelta(days=5))
    bot2.db.advance_meeting_followup(key, on_date=dl.iso(MON), meeting_date=met)
    bot2.db.advance_meeting_followup(key, on_date=dl.iso(MON + timedelta(days=3)),
                                     meeting_date=met)
    print(f"   before: {bot2.db.meeting_followups()}")
    SHEET["rows"] = [dict(harbor, next_steps="sending the pilot proposal Friday")]
    LOGS.clear()
    queue = await bot2._run_next_actions(today=MON + timedelta(days=7))
    print(f"   after Next Steps is filled and the queue is read: "
          f"{bot2.db.meeting_followups()}")
    for line in [x for x in LOGS if x.startswith("[rules] R9 ladder")]:
        print("   log: " + line)
    check("the ladder row is gone", bot2.db.meeting_followups(), {})
    check("...and R9 asks nothing",
          [a for a in queue["actions"] if a["type"] == nextaction.R_MEETING_FOLLOWUP], [])

    SHEET["rows"] = [dict(harbor, meeting_date=cell(MON + timedelta(days=2)))]
    bot2.db.advance_meeting_followup(key, on_date=dl.iso(MON), meeting_date=met)
    await bot2._run_next_actions(today=MON + timedelta(days=7))
    check("a new meeting date starts a new ladder too", bot2.db.meeting_followups(), {})


# -- (vi) search ---------------------------------------------------------------


class SearxStandIn(http.server.BaseHTTPRequestHandler):
    """SearXNG's JSON API, answered locally. NOT SearXNG — see the module note."""
    seen: list = []

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(parsed.query)
        SearxStandIn.seen.append(self.path)
        if parsed.path != "/search" or q.get("format") != ["json"]:
            self.send_response(403)
            self.end_headers()
            return
        query = (q.get("q") or [""])[0]
        body = json.dumps({"query": query, "number_of_results": 2, "results": [
            {"title": f"{query} — overview", "url": "https://example.org/overview",
             "content": "A stand-in result, shaped like SearXNG's.",
             "engine": "stand-in", "category": (q.get("categories") or ["general"])[0]},
            {"title": f"{query} — second result", "url": "https://example.com/second",
             "content": "Another one.", "engine": "stand-in"},
        ], "unresponsive_engines": []}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def the_search() -> None:
    say("(vi) FREE SEARCH — searxng first, ddg when searxng is gone")
    print(f"   backends in search_backend.py: {', '.join(search_backend.BACKENDS)}")
    db = DB(fresh_db("search"))
    search_backend.bind(lambda: db)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), SearxStandIn)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    saved = (config.SEARXNG_URL, config.SEARCH_BACKEND, config.SEARCH_FALLBACKS,
             config.WEB_SEARCH_ENABLED)
    config.SEARXNG_URL = f"http://127.0.0.1:{port}"
    config.SEARCH_BACKEND, config.SEARCH_FALLBACKS = "searxng", ["ddg"]
    config.WEB_SEARCH_ENABLED = True
    stopped = False
    try:
        print(f"   SEARCH_BACKEND=searxng  SEARCH_FALLBACKS=ddg  SEARXNG_URL="
              f"{config.SEARXNG_URL}  (a LOCAL STAND-IN speaking SearXNG's JSON API)")
        print(f"   chain: {search_backend.chain()}   available: "
              f"{search_backend.available()}")
        got = search_backend.search_detail("AI data labelling vendors", n=5)
        sent = SearxStandIn.seen[-1] if SearxStandIn.seen else ""
        print(f"   the request it sent: GET {sent or '?'}")
        print(f"   answered by: {got.get('backend')}   results: "
              f"{len(got.get('results') or [])}")
        for r in (got.get("results") or [])[:3]:
            print(f"     - {r.get('title')}  <{r.get('url')}>")
        check("searxng answered, with results",
              (got.get("backend"), len(got.get("results") or []) > 0), ("searxng", True))
        check("it asked for /search?q=…&format=json&categories=general",
              all(x in sent for x in ("/search?", "format=json", "categories=general")))
        news = search_backend.search_detail("AI funding round", n=5, news=True, days=1)
        check("a news search asks for categories=news",
              (news.get("backend"), "categories=news" in SearxStandIn.seen[-1]),
              ("searxng", True))

        server.shutdown()
        server.server_close()
        stopped = True
        print(f"\n   searxng STOPPED (nothing listens on 127.0.0.1:{port} now)")
        if NO_DDG:
            print("   --no-ddg: the live ddg call is skipped")
            return
        LOGS.clear()
        fell = search_backend.search_detail("Anthropic Claude latest model", n=5)
        for line in [x for x in LOGS if x.startswith("[search]")][:4]:
            print("   log: " + line[:200])
        print(f"   answered by: {fell.get('backend')}   results: "
              f"{len(fell.get('results') or [])}   (a live, free DuckDuckGo request)")
        for r in (fell.get("results") or [])[:3]:
            print(f"     - {str(r.get('title'))[:70]}  <{str(r.get('url'))[:60]}>")
        check("with searxng stopped, ddg answered with results",
              (fell.get("backend"), len(fell.get("results") or []) > 0), ("ddg", True))
    finally:
        if not stopped:
            server.shutdown()
            server.server_close()
        (config.SEARXNG_URL, config.SEARCH_BACKEND, config.SEARCH_FALLBACKS,
         config.WEB_SEARCH_ENABLED) = saved
        search_backend.bind(None)


# -- (6) the feed path without WEB_SEARCH_ENABLED ------------------------------


async def the_feed_path() -> None:
    say("(6) THE RSS NEWS PATH DOES NOT NEED WEB_SEARCH_ENABLED")
    monday_sheet(MON)
    saved = (config.WEB_SEARCH_ENABLED, config.SEARCH_BACKEND)
    config.WEB_SEARCH_ENABLED, config.SEARCH_BACKEND = False, "searxng"
    config.SALES_TEST_MODE = True
    try:
        bot = new_bot(fresh_db("feeds"), news_stubbed=False)
        polled: list = []

        async def no_network(**_k):
            polled.append(1)
            return None

        async def one_scored_story(**_k):
            # ONE ITEM ALREADY IN THE FEED STORE AND ALREADY SCORED, so the
            # path is walked end to end with no network and no model call.
            return [{"url": "https://example.org/a-big-model-release",
                     "url_key": "example.org/a-big-model-release",
                     "title": "A lab releases a big new model",
                     "headline_key": "a lab releases a big new model",
                     "summary": "It is big.", "source": "Example",
                     "published_at": "2026-09-28T06:00:00Z", "importance": 5,
                     "topic": "models", "what": "A lab released a big new model."}]

        bot._maybe_poll_feeds = no_network
        bot._feed_candidates = one_scored_story
        item = {"rule": nextaction.R_AI_NEWS, "type": nextaction.R_AI_NEWS,
                "text": "AI news", "web_pending": True}
        print(f"   WEB_SEARCH_ENABLED={config.WEB_SEARCH_ENABLED}  "
              f"SEARCH_BACKEND={config.SEARCH_BACKEND}")
        touched = await bot._news_run([item], today=MON)
        print("   R1's main post, as _news_run filled it in:")
        for line in str(item.get("text") or "").splitlines()[:6]:
            print("     | " + line)
        check("the main sweep ran: the feeds were polled", bool(polled))
        check("the item was filled in, not marked \"web search is off\"",
              (len(touched), item.get("web_pending"),
               "WEB_SEARCH_ENABLED" in json.dumps(item, default=str)), (1, False, False))
        check("...with the story from the feed store",
              "example.org/a-big-model-release" in str(item.get("text")))

        hourly = await bot._maybe_breaking_news(
            force=True, channel=TEST_CHANNEL,
            at=datetime.combine(MON, time(15, 0), dl.IST))
        shown = None if hourly is None else {
            k: hourly[k] for k in ("slot", "found", "posted", "held")}
        print(f"   the hourly check at 15:00: {shown}")
        check("the hourly check ran too (it used to return before claiming its slot)",
              hourly is not None)
        for line in [x for x in LOGS if x.startswith("[news")][:6]:
            print("   log: " + line[:170])
    finally:
        config.WEB_SEARCH_ENABLED, config.SEARCH_BACKEND = saved


# -- (w) the wording -----------------------------------------------------------


def the_wording() -> None:
    say("(w) THE WORDING FOR THE SHEET — printed from the rules the bot loads")
    rules_mod.reload()
    cap_row = rules_mod.global_rule("daily_cap")
    print("\n   Global Rules tab — row \"Daily cap\":")
    print("     " + cap_row)
    check("the Global Rules row is the agreed sentence", cap_row,
          "Max 5 posts a day; meeting prep, meeting follow-ups, reminders, urgent "
          "news and answers to questions don't count.")
    for key, label in (("ai_news", "AI news"), ("weekends", "Weekends"),
                       ("reminders", "Reminders")):
        print(f"\n   Global Rules tab — row \"{label}\":")
        print("     " + rules_mod.global_rule(key))
        check(f"the \"{label}\" row is there", bool(rules_mod.global_rule(key)))
    for rid in ("R1", "R9"):
        print(f"\n   Bot Rules tab — rule {rid[1:]}, column \"What the Bot Shares / "
              "Checks\":")
        print("     " + rules_mod.sheet_wording_for(rid))
    print()
    check("R8 and R9 are outside the cap in bot_rules.yaml",
          (rules_mod.by_id("R8").counts_toward_cap,
           rules_mod.by_id("R9").counts_toward_cap), (False, False))
    strategy = persona.load_strategy()
    check("sales_strategy.md carries the same cap sentence", cap_row in strategy)
    check("sales_strategy.md no longer describes a per-day cap",
          [x for x in ("DAILY_MESSAGE_CAP_BY_DAY", "the cap rises to match")
           if x in strategy], [])


async def main() -> None:
    print(f"real date {dl.iso(dl.real_today_ist())} | pretend Monday {dl.iso(MON)} | "
          f"cap {config.DAILY_MESSAGE_CAP} | window {config.SALES_DRIP_START}-"
          f"{config.SALES_DRIP_END} | news {config.NEWS_MAIN_TIME} | day-of "
          f"{config.MEETING_DAYOF_TIME} | tone: {tone.describe()}")
    if want("0"):
        migration()
    if want("i"):
        await the_monday()
    if want("ii"):
        await the_week()
    if want("iii"):
        await the_sunday()
    if want("iv"):
        await the_reminders()
    if want("v"):
        await the_ladder()
    if want("vi"):
        the_search()
    if want("6"):
        await the_feed_path()
    if want("w"):
        the_wording()


try:
    asyncio.run(main())
finally:
    clock.forget()
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
sys.exit(1 if failures else 0)
