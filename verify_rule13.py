"""RULE 13 (Next Steps follow-ups) AND THE 7 OCT SHEET LAYOUT — the bot-level checks. Offline, made-up names.

    python verify_rule13.py                       everything below, offline
    python verify_rule13.py --real-sheet --date 2026-10-08
                                                  THE FINAL DRY RUN: exactly ONE read-only read of the real
                                                  Outreach PoCs tab, Rule 13 evaluated with an EMPTY state,
                                                  nothing posted, nothing written, no database opened, no
                                                  model, no Discord. Run once, last, and only on the lead's "go".

Written from docs/plans/RULE13.md. The pure logic (every row of the timing table, the call clock, the rotation,
the shipped rules, the plan) is in tests/test_rule13.py; this script is what needs a bot:

  (a) the role map on both header rows, and the [gtm.window] line
  (b) the ten-weekday rotation as a table, and the call clock, and the preview block
  (c) ONE POST, FOUR WAYS: the live sweep, the live sweep in SALES_TEST_MODE, a test day, a simulation. The same
      post apart from the [TEST...] tag, at 15:00, outside the cap, zero model calls, zero searches
  (S1) state is written only after a real send, for the people named, with one posts row; a refused send writes none
  (S2) the preview / the queue read twice leaves both tables byte-identical
  (S3) a test day on a database that is not *_test.db sends the post and records nothing, and says so in the log
  (S4) a simulation leaves the real database unchanged
  (P3) a spaced post sent 5 minutes earlier does not hold the 15:00 post; the 15:00 row does not delay the next
       spaced post
  (G1, G2) a "yes" and a "done" replied to a Rule 13 post: nothing written, no proposal applied, no Rule 13 state moved
  (env) the lines for both .env files

(R) the replies, built after NFT2-1063 landed (plan section 14): "done" under a one-person research post gives the
    "Nice, can you set Next Steps for Priya to Send email 1?" line, two research people ask "Which one?" and a number or a
    name picks, a real question reaches the engine with the post; live == test mode; zero writes, proposals, votes, model calls.

NOTHING REACHES DISCORD, THE REAL SHEET OR THE WEB (tests/offline_guard.py counts and fails any real call). The
sheet is a fixture parsed by the real parser on the real 7 Oct header row.
"""
import asyncio
import copy
import hashlib
import json
import logging
import os
import random
import re
import shutil
import sqlite3
import sys
import tempfile
import time as _time
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "tests"))
os.chdir(HERE)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REAL_SHEET = "--real-sheet" in sys.argv
REAL_DATE = ""
for _i, _a in enumerate(sys.argv):
    if _a == "--date" and _i + 1 < len(sys.argv):
        REAL_DATE = sys.argv[_i + 1]

TMP = tempfile.mkdtemp(prefix="saley-r13-")
os.environ["STATE_DIR"] = os.path.join(TMP, "state")
os.environ["DB_PATH"] = os.path.join(TMP, "base_test.db")
os.environ.setdefault("DISCORD_TOKEN", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import offline_guard  # noqa: E402

# THE REAL-SHEET MODE IS THE ONE PLACE A REAL READ IS ALLOWED, so the guard is not installed there; the mode
# itself counts reads and refuses a second (see real_sheet_dry_run). Everything else is offline-guarded.
GUARD = None if REAL_SHEET else offline_guard.install_script()

import config  # noqa: E402

SID, VAISHNAVI, KUSHAL = 111, 222, 333
SETTINGS = {
    "DB_PATH": os.environ["DB_PATH"], "STATE_DIR": os.environ["STATE_DIR"],
    "TEAM_ROSTER_IDS": [SID, VAISHNAVI, KUSHAL], "SALES_APPROVER_IDS": [SID, VAISHNAVI],
    "SALES_FINAL_SAY_ID": SID, "SALES_ALWAYS_TAG_IDS": [VAISHNAVI],
    "ROSTER_DISPLAY_NAMES": {str(SID): "Sid", str(VAISHNAVI): "Vaishnavi", str(KUSHAL): "Kushal"},
    "DAILY_MESSAGE_CAP": 5, "SALES_DRIP_START": "14:00", "SALES_DRIP_END": "18:30",
    "MESSAGE_GAP_MINUTES": 90, "MESSAGE_JITTER_MINUTES": 15,
    "MESSAGE_GAP_MIN_MINUTES": 30, "DRIP_REASK_DAYS": 2, "DRIP_WEEKDAYS_ONLY": True,
    "SUNDAY_RULE_IDS": ["R4"], "NEWS_MAIN_TIME": "14:00", "MEETING_DAYOF_TIME": "10:00",
    "TEST_MORNING_TIME": "10:00", "TEST_AFTERNOON_TIME": "14:00",
    "NEXT_ACTION_ENABLED": True, "NEXT_ACTION_WEEKEND_SHIFT": True,
    "WEEKLY_FUNNEL_ENABLED": False, "SALES_DMS_ENABLED": False,
    "DRIP_LLM_COMPOSE": True, "TEST_POST_GAP_SECONDS": 0,
    "SIMULATION_FAST_GAP_SECONDS": 0, "SIMULATION_REAL_MENTIONS": False,
    # channels pinned here, not read from this machine's .env: 4343 is the sales channel, 4242 the test channel
    "SALES_CHANNEL_IDS": [4242, 4343], "SALES_CHANNEL_ID_SET": {4242, 4343}, "SALES_DIGEST_CHANNEL_ID": 4343,
    "WEEKLY_DIGEST_CHANNEL_ID": 0, "SALES_TEST_CHANNEL_ID": 4242,
    "SIMULATION_PREFIX": "[TEST]",           # the laptop's is [TEST-live]; the contract default is [TEST]
    "REMINDER_DEFAULT_TIME": "14:00", "SHEET_WRITE_UNDO_HOURS": 24,
    "EMAIL_LOOKUP_MAX_PER_POST": 5, "EMAIL_WRITE_ALLOWED": False, "SHEET_WRITES_ENABLED": True,
    "PROSPECT_COMPANIES_PER_WEEK": 2, "PROSPECT_REPEAT_ASK_AT": 3,
    "WEB_SEARCH_ENABLED": True, "SEARCH_BACKEND": "searxng", "SEARXNG_URL": "http://127.0.0.1:9",
    "SEARCH_FALLBACKS": [],
    # Rule 13's own contract defaults (.env.example)
    "NEXT_STEP_TIME": "15:00", "NEXT_STEP_FIRST_DAYS": 2, "NEXT_STEP_AFTER_PREVIOUS_DAYS": 2,
    "NEXT_STEP_AFTER_EMAIL_DAYS": 7, "NEXT_STEP_CALL_AFTER_DM_DAYS": 7, "NEXT_STEP_CALL_EVERY_DAYS": 3,
    "NEXT_STEP_CALL_UNTIL_DAYS": 21, "NEXT_STEP_CONNECTED_MARKERS": ["connected"],
    "NEXT_STEP_DM_REPLIED_MARKERS": ["replied", "responded"], "NEXT_STEP_PAUSE_FOR_BOOKED_MEETING": True,
}
if not REAL_SHEET:
    for _k, _v in SETTINGS.items():
        setattr(config, _k, _v)
    config.digest_enabled = lambda: True      # the kill switch is not what is tested
    # the write bands, pinned to the contract rather than to this machine's .env (the laptop's new-row band is A:R)
    config.RESTRICTED_COLUMN_RANGES = "A:I,Q:W,Z:AE"
    config.RESTRICTED_COLUMN_BANDS = config.parse_column_ranges("A:I,Q:W,Z:AE")
    config.RESTRICTED_COLUMN_INDEXES = frozenset(i for lo, hi in config.RESTRICTED_COLUMN_BANDS
                                                 for i in range(lo, hi + 1))
    config.NEW_ROW_WRITABLE_RANGES = "A:P,X:Y"
    config._NEW_ROW_INDEXES = None

import clock  # noqa: E402
import deadlines as dl  # noqa: E402
import drip  # noqa: E402
import gtm_sheet  # noqa: E402
import leave  # noqa: E402
import nextaction  # noqa: E402
import persona  # noqa: E402
import rules as rules_mod  # noqa: E402
import rule13_fixtures as fx  # noqa: E402
import search_backend  # noqa: E402
import simulation  # noqa: E402
import tone  # noqa: E402
import voice  # noqa: E402
from db import DB  # noqa: E402

simulation.pace_seconds = lambda **_k: 0.0

TRIGGER = "next_step_followups"
BOT_ID = 999
THURSDAY = date(2026, 10, 15)
failures = 0
POSTED: list = []
SENT: list = []
LOGS: list = []
SEARCHES: list = []
MODEL_CALLS: list = []
RULES: set = set()
FRESH = [0]
SKIPPED: list = []


def check(name, got, expected=True):
    global failures
    ok = (got == expected)
    failures += 0 if ok else 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f": got {got!r}, want {expected!r}"))


def say(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def skip(name: str, why: str) -> None:
    SKIPPED.append(name)
    print(f"  SKIP  {name}: {why}")


class Capture(logging.Handler):
    KEEP = ("[rules]", "[R13]", "[drip]", "[approvals]", "[gtm]", "[gtm.window]", "[test-day]", "[sim")

    def emit(self, record):
        try:
            line = record.getMessage()
        except Exception:
            return
        if line.startswith(self.KEEP) or "R13" in line or "next step" in line.lower():
            LOGS.append(line)


root = logging.getLogger()
root.setLevel(logging.INFO)
for h in list(root.handlers):
    root.removeHandler(h)
root.addHandler(Capture())
for noisy in ("discord", "asyncio", "httpx", "httpcore", "anthropic", "googleapiclient", "google", "urllib3"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

# ---------------------------------------------------------------------------------------------
# the fixture sheet: the real parser on the real 7 Oct row, behind a stand-in worksheet
# ---------------------------------------------------------------------------------------------

DELIV_HEADERS = ["Action Item", "Priority", "Status", "Tentative Deadline", "Functional Dependency", "Remarks",
                 "Link/Destination"]
EVENT_HEADERS = ["Event", "Date", "Last day for registration", "Registered?", "Link", "Location"]
GRIDS: dict = {}
UPDATES: list = []          # every cell the stand-in worksheet is asked to write — must stay empty


def set_sheet(rows) -> None:
    GRIDS["Outreach PoCs"] = [list(fx.HEADERS_7OCT)] + [list(r) for r in rows]
    GRIDS["Deliverables Checklist"] = [DELIV_HEADERS]
    GRIDS["AI Events & Summits"] = [EVENT_HEADERS]
    UPDATES.clear()


def read_sheet(which=None, *, force: bool = False, **_k) -> dict:
    out = {}
    for title, grid in GRIDS.items():
        tab = gtm_sheet.SHEETS._parse_values(title, copy.deepcopy(grid), read_at=_time.time())
        if tab is not None:
            out[tab.kind] = tab
    return out


class FakeWorksheet:
    def __init__(self, title):
        self.title = title

    def update(self, values=None, range_name="", value_input_option=""):
        UPDATES.append((self.title, range_name, values[0][0] if values else None))

    def batch_update(self, *a, **k):
        UPDATES.append((self.title, "batch_update", a))


if not REAL_SHEET:
    gtm_sheet.SHEETS.read = read_sheet
    gtm_sheet.SHEETS.cadence_tab = lambda *a, **k: (read_sheet().get(gtm_sheet.POCS), "fixture")
    gtm_sheet.SHEETS._open = lambda *a, **k: SimpleNamespace(worksheet=FakeWorksheet, batch_update=lambda *a, **k:
                                                              UPDATES.append(("spreadsheet", "batch_update", a)))
    gtm_sheet.SHEETS._refuse_if_read_only = lambda *a, **k: ""
    gtm_sheet.SHEETS.staleness_note = lambda *a, **k: ""


class StandInModel:
    """Counts every call. Rule 13 must make none (its post is verbatim); anything else echoes."""

    def create(self, **kw):
        MODEL_CALLS.append(str((kw.get("messages") or [{}])[-1].get("content") or "")[:60])
        system = persona.blocks_text(kw.get("system"))
        prompt = str((kw.get("messages") or [{}])[-1].get("content") or "")
        text = "{}"
        if prompt.startswith("Write ONE short proactive message"):
            ref = hashlib.sha1((system + "\n@@\n" + prompt).encode("utf-8")).hexdigest()[:8]
            block = re.search(r"<<<\n(.*?)\n>>>", prompt, re.S)
            text = (f"a quick list for you, echo {ref}.\n{block.group(1)}\ntell me if any of it has moved."
                    if block else f"this one could use a look this week, echo {ref}. no rush.")
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn",
                               usage=SimpleNamespace(input_tokens=400, output_tokens=40))


search_backend.search = lambda query, **k: (SEARCHES.append(str(query)) or [])


class FakeTyping:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeChannel:
    name = "recording"
    guild = SimpleNamespace(id=1)

    def __init__(self, cid, *, refuse=False):
        self.id = int(cid)
        self.refuse = refuse

    async def send(self, body):
        if self.refuse:
            raise RuntimeError("Discord refused the message")
        POSTED.append(str(body))
        return SimpleNamespace(id=1000 + len(POSTED), jump_url="https://discord/x", channel=self)

    def typing(self):
        return FakeTyping()


TEST_CHANNEL = FakeChannel(4242)


def person_user(uid: int):
    return SimpleNamespace(id=uid, bot=False, display_name=config.ROSTER_DISPLAY_NAMES[str(uid)],
                           name=config.ROSTER_DISPLAY_NAMES[str(uid)])


class FakeMessage:
    def __init__(self, uid=SID, text="", reply_to=None, mid=9000):
        self.id = mid
        self.channel = TEST_CHANNEL
        self.guild = TEST_CHANNEL.guild
        self.author = person_user(uid)
        self.content = text
        self.reference = SimpleNamespace(message_id=reply_to) if reply_to else None
        self.mentions = []
        self.mention_everyone = False
        self.replies: list = []

    async def reply(self, body, mention_author=False):
        self.replies.append(str(body))
        POSTED.append("REPLY: " + str(body))
        return SimpleNamespace(id=2000 + len(POSTED), jump_url="https://discord/x", channel=self.channel)


def strip(body: str) -> str:
    out = str(body or "")
    tag = str(config.SIMULATION_PREFIX or "").strip()
    if tag and out.startswith(tag):
        out = out[len(tag):].lstrip()
    return re.sub(r"<@!?(\d+)>", lambda m: "@" + str(config.ROSTER_DISPLAY_NAMES.get(str(m.group(1))) or "user"), out)


def fresh_db(name: str, *, suffix: str = "_test.db") -> str:
    FRESH[0] += 1
    path = os.path.join(TMP, f"{name}{FRESH[0]}{suffix}")
    config.DB_PATH = path
    clock.forget()
    voice.invalidate()
    return path


def dump(path: str) -> tuple:
    """Both Rule 13 tables, read straight from the file (not through the bot), as comparable rows."""
    con = sqlite3.connect(path)
    try:
        out = []
        for t in ("next_step_followups", "next_step_posts"):
            try:
                rows = con.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall()
            except sqlite3.Error:
                rows = ["(no such table)"]
            out.append([tuple(r) if not isinstance(r, str) else r for r in rows])
        return tuple(tuple(map(tuple, x)) if x and not isinstance(x[0], str) else tuple(x) for x in out)
    finally:
        con.close()


def new_bot(path: str):
    from bot import SalesBot

    SalesBot.user = property(lambda self: SimpleNamespace(id=BOT_ID))
    bot = SalesBot()
    bot.db = DB(path)
    if bot.llm is None:
        print("No model client (ANTHROPIC_API_KEY is unset): cannot build the bot even against the stand-in.")
        sys.exit(2)
    bot.llm._client = SimpleNamespace(messages=StandInModel())

    async def nobody_away(*_a, **_k):
        return {}

    async def nothing(*_a, **_k):
        return None

    async def no_new_events(rows, *, today, marker):
        return []

    async def no_missing_deadlines(rows, *, today, marker):
        return [], ""

    real_queue = bot._run_next_actions

    async def the_rules_under_test(**kw):
        queue = await real_queue(**kw)
        if queue is not None and RULES:
            queue["actions"] = [a for a in queue["actions"] if str(a.get("rule_id") or "") in RULES]
        return queue

    real_send = bot._send_drip_message

    async def recording_send(channel, message, **kw):
        start = len(POSTED)
        await real_send(channel, message, **kw)
        SENT.append({"day": kw.get("marker"), "rule": message.get("rule_id") or "—", "type": message.get("type"),
                     "at": message.get("send_at_hhmm"), "pinned": message.get("pinned"),
                     "counts": message.get("counts_toward_cap"), "posts": list(POSTED[start:])})

    leave.who_is_away = nobody_away
    bot._split_active = lambda rows, why: (list(rows), [])
    bot._run_next_actions = the_rules_under_test
    bot._send_drip_message = recording_send
    bot._convert_if_already_done = nothing
    bot._sweep_proposals = nothing
    bot._maybe_breaking_news = nothing
    bot._maybe_poll_feeds = nothing
    bot._discover_events = no_new_events
    bot._backfill_deadlines = no_missing_deadlines
    bot.get_channel = lambda cid: FakeChannel(cid)
    return bot


def begin(label: str) -> None:
    POSTED.clear()
    SENT.clear()
    LOGS.clear()
    MODEL_CALLS.clear()
    SEARCHES.clear()
    tone.RNG = random.Random(20261015)
    voice._last_choice.clear()
    print(f"\n   ---- {label} ----")


def pretend(day: date, hh: int, mm: int = 0) -> None:
    was = config.SALES_TEST_MODE
    config.SALES_TEST_MODE = True
    clock.set_day(day, by="verify_rule13")
    config.SALES_TEST_MODE = was
    clock.set_time_of_day(time(hh, mm), by="verify_rule13")


def collect(bot, path) -> dict:
    return {"posts": {s["rule"]: [strip(p) for p in s["posts"]] for s in SENT}, "raw": [dict(s) for s in SENT],
            "bot": bot, "logs": list(LOGS), "path": path, "model_calls": len(MODEL_CALLS),
            "searches": list(SEARCHES), "tables": dump(path)}


async def live_path(day: date, *, test_mode: bool = False, label: str = "") -> dict:
    begin(label or f"LIVE sweep ({'SALES_TEST_MODE=true' if test_mode else 'SALES_TEST_MODE=false'}) — "
                   f"_maybe_send_drip ticked through {dl.iso(day)}")
    config.SALES_TEST_MODE = test_mode
    config.SALES_TEST_CHANNEL_ID = 4242
    path = fresh_db("live")
    bot = new_bot(path)
    pretend(day, 9, 0)
    bot._live_loop_held = lambda what: False
    planned = await bot._plan_drip(today=day, already=[])
    step = timedelta(minutes=drip.min_gap_minutes() + 1)
    at = None
    for target in [m["send_at"] for m in (planned or {}).get("messages") or []] + [None]:
        base = target or (at + step if at else datetime.combine(day, time(22, 0), dl.IST))
        at = base if at is None else max(base, at + step)
        clock.set_time_of_day(at.timetz().replace(tzinfo=None), by="verify_rule13")
        await bot._maybe_send_drip()
    clock.clear_time_override(why="verify_rule13")
    return collect(bot, path)


async def test_day_path(day: date, *, path=None, label: str = "") -> dict:
    begin(label or f"TEST DAY — \"make it {dl.iso(day)}\"")
    config.SALES_TEST_MODE = True
    config.SALES_TEST_CHANNEL_ID = 4242
    path = path or fresh_db("testday")
    config.DB_PATH = path
    bot = new_bot(path)
    await bot._handle_test_command(FakeMessage(SID), "make it " + dl.iso(day))
    return collect(bot, path)


async def sim_path(day: date) -> dict:
    begin(f"SIMULATION — \"simulate {dl.iso(day)}\"")
    config.SALES_TEST_MODE = True
    config.SALES_TEST_CHANNEL_ID = 4242
    path = fresh_db("sim")
    bot = new_bot(path)
    await bot._handle_simulation(FakeMessage(SID), "simulate " + dl.iso(day))
    return collect(bot, path)


def show(run: dict) -> None:
    for body in run["posts"].get("R13") or ["(no R13 post)"]:
        for line in body.splitlines():
            print("     | " + line)


# ---------------------------------------------------------------------------------------------
# (a) (b) the printed, human-readable parts
# ---------------------------------------------------------------------------------------------

def print_role_maps() -> None:
    say("(a) THE ROLE MAP, on the 7 Oct row and on the pre-7 Oct row; and the [gtm.window] line")
    for label, hdr in (("7 Oct 2026 row (A-AF)", fx.HEADERS_7OCT), ("pre-7 Oct row (A-X)", fx.HEADERS_PRE_7OCT)):
        tab = fx.parse([[""] * len(hdr)], hdr)
        got = {r: gtm_sheet._col_letter(i) for r, i in (tab.canonical_role_to_col or tab.role_to_col).items()}
        print(f"\n   {label}: " + ", ".join(f"{r}={c}" for r, c in sorted(got.items(), key=lambda x: (len(x[1]), x[1]))))
        if hdr is fx.HEADERS_7OCT:
            check("next_steps (the notes role) is Z and outreach_step (the dropdown) is Q",
                  (got["next_steps"], got["outreach_step"]), ("Z", "Q"))
            check("the six email roles are R..W and Priority is AF",
                  [got[r] for r in ("email_1_sent", "email_1_date", "email_2_sent", "email_2_date",
                                    "email_3_sent", "email_3_date", "poc_priority")],
                  ["R", "S", "T", "U", "V", "W", "AF"])
        else:
            check("the old notes column is S and none of the new roles is mapped",
                  (got["next_steps"], [r for r in ("outreach_step", "email_1_sent", "poc_priority") if r in got]),
                  ("S", []))
    tab = fx.parse([fx.values_row(1)])
    LOGS.clear()
    gtm_sheet.SHEETS.log_writable_window(tab)
    line = next((m for m in LOGS if "[gtm.window]" in m), "")
    print("\n   " + line)
    check("[gtm.window] logs restricted A:I, Q:W, Z:AE and the window J:P, X:Y",
          ("restricted A:I, Q:W, Z:AE" in line, "Writable window J:P, X:Y" in line), (True, True))


def print_rotation_and_clock() -> None:
    import db as dbmod

    say("(b) THE TEN-WEEKDAY ROTATION: 33 Connected rows, 29 dated, 18 / 11 / 4, 5 a post")
    rows, meta = fx.rotation_values()
    path = os.path.join(TMP, "rot_test.db")
    d = dbmod.DB(path)
    rule = rules_mod.by_id("R13")
    print("   day         post (the dated people are numbered 1..29 in sheet order)")
    index = {n: i + 1 for i, n in enumerate(meta["dated"])}
    table = []
    for day in fx.WEEKDAYS:
        res = nextaction.run(today=day, rows=list(fx.parse(rows).rows), day_rules=[rule],
                             next_step_state=d.next_step_state())
        items = sorted([a for a in res["actions"] if a["rule_id"] == "R13"], key=lambda a: a.get("pick_order", 0))
        for it in items:
            d.record_next_step_mention(it["row_key"], signature=it["signature"], on_date=dl.iso(day), ask=it["ask"])
        nums = [index[it["poc"]] for it in items]
        table.append(nums)
        print(f"   {day:%a %d %b}   {nums}   undated skipped: {len(res['reports']['R13']['no_date'])}")
    d_ = meta["dated"]
    check("days 1-5 are people 1..25; day 6 is 26..29 then 1; day 10 ends at 21",
          (table[:5], table[5], table[9]),
          ([list(range(i, i + 5)) for i in range(1, 26, 5)], [26, 27, 28, 29, 1], [17, 18, 19, 20, 21]))
    check("33 Connected rows, 29 dated, 4 undated", (33, len(d_), len(meta["undated"])), (33, 29, 4))

    say("(b) THE CALL CLOCK: Next Steps 'Reach by LI DM', LI DM sent Mon 21 Sep, a post every weekday")
    cd = dbmod.DB(os.path.join(TMP, "clock_test.db"))
    dm = date(2026, 9, 21)
    tab = fx.parse([fx.person(1, name="Priya Rao", company="Acme Labs", li_date=date(2026, 8, 1), step="Reach by LI DM",
                              dm_sent="Sent", dm_date=dm)])
    seen = []
    day = dm
    while day <= dm + timedelta(days=30):
        if day.weekday() < 5:
            res = nextaction.run(today=day, rows=list(tab.rows), day_rules=[rule], next_step_state=cd.next_step_state())
            for it in [a for a in res["actions"] if a["rule_id"] == "R13"]:
                cd.record_next_step_mention(it["row_key"], signature=it["signature"], on_date=dl.iso(day),
                                            ask=it["ask"])
                seen.append(((day - dm).days, it["ask"]))
                print(f"   day {(day - dm).days:>2}  {day:%a %d %b}  {it['ask']:<13} {it['text']}")
        day += timedelta(days=1)
    check("call reminders on days 7, 10, 14, 17, 21, the Unresponsive reminder on day 22, nothing after",
          seen, [(7, "call"), (10, "call"), (14, "call"), (17, "call"), (21, "call"), (22, "unresponsive")])


# ---------------------------------------------------------------------------------------------
# (c) one post, four ways; S1, S3, S4, P3
# ---------------------------------------------------------------------------------------------

def tag_of(body: str) -> str:
    m = re.match(r"^(\[TEST[^\]]*\])", body or "")
    return m.group(1) if m else ""


async def the_post_four_ways() -> dict:
    say(f"(c) ONE RULE 13 POST, FOUR WAYS — Thursday {THURSDAY:%d %b %Y}, only R13 planned")
    RULES.clear()
    RULES.add("R13")
    rows, meta = fx.rotation_values()
    set_sheet(rows)
    runs = {}
    runs["live"] = await live_path(THURSDAY, test_mode=False)
    print("\n   LIVE (SALES_TEST_MODE=false):")
    show(runs["live"])
    set_sheet(rows)
    runs["test_mode"] = await live_path(THURSDAY, test_mode=True)
    print("\n   TEST MODE (SALES_TEST_MODE=true, same sweep):")
    show(runs["test_mode"])
    set_sheet(rows)
    runs["test_day"] = await test_day_path(THURSDAY)
    print("\n   TEST DAY (\"make it 2026-10-15\"):")
    show(runs["test_day"])
    set_sheet(rows)
    runs["sim"] = await sim_path(THURSDAY)
    print("\n   SIMULATION:")
    show(runs["sim"])

    want = meta["dated"][0:5]
    live = runs["live"]
    check("live: exactly one Rule 13 post", len(live["posts"].get("R13") or []), 1)
    body = (live["posts"].get("R13") or [""])[0]
    bullets = [ln for ln in body.splitlines() if ln.startswith("• ")]
    check("live: five bullets, the first five dated people in sheet order",
          [b.split(" (")[0].replace("• ", "") for b in bullets], want)
    check("live: each bullet names the person's company",
          all(f"Acme Labs {n.split()[-1]}" in b for n, b in zip(want, bullets)), True)
    check("live: the opener is the day's (a function of the date, never random)",
          any(o in body for o in __import__("wording").NEXT_STEP_OPENERS), True)
    check("live: a 'Next steps' heading and the tags line (Vaishnavi) and no other mention",
          ("Next steps" in body, body.count("@Vaishnavi")), (True, 1))
    check("live: no rule number, no schedule talk, no URL, no setting name",
          bool(re.search(r"\bR\d+\b|\brule \d|https?://|NEXT_STEP|every (day|two)|3 ?PM|daily", body, re.I)), False)
    check("live: not tagged", tag_of(body), "")
    check("live: sent at 15:00, pinned, outside the cap",
          (live["raw"][0]["at"], live["raw"][0]["pinned"], live["raw"][0]["counts"]), ("15:00", True, False))
    for k in ("live", "test_mode", "test_day", "sim"):
        r = runs[k]
        check(f"{k}: zero model calls and zero searches for a Rule 13 post", (r["model_calls"], r["searches"]), (0, []))
        check(f"{k}: nothing written to the sheet", UPDATES, [])
    for k in ("test_mode", "test_day", "sim"):
        raw = (runs[k]["raw"][0]["posts"] or [""])[0] if runs[k]["raw"] else ""
        check(f"{k}: the post carries the [TEST...] tag", tag_of(raw) != "", True)
        check(f"{k}: the same post as live, apart from the tag",
              runs[k]["posts"].get("R13") == live["posts"].get("R13"), True)
    check("test day and simulation also post it at the 15:00 stop",
          (runs["test_day"]["raw"][0]["at"], runs["sim"]["raw"][0]["at"]), ("15:00", "15:00"))

    print("\n   (S1) the state after a real send")
    for k in ("live", "test_mode", "test_day"):
        followups, posts = runs[k]["tables"]
        check(f"{k}: one state row per person named (5), and one posts row", (len(followups), len(posts)), (5, 1))
    followups, posts = runs["live"]["tables"]
    check("live: the five recorded are the five named, dated today",
          sorted(r[0] for r in followups) == sorted(
              runs["live"]["bot"].db.next_step_state().keys()) and all(r[2] == "2026-10-15" for r in followups), True)
    people = json.loads(posts[0][3]) if posts else []
    check("live: the posts row carries each person's people/step/ask for later",
          [(p["poc"], bool(p["step"]), bool(p["ask"]), bool(p["signature"])) for p in people],
          [(n, True, True, True) for n in want])
    print("\n   (S4) the simulation")
    check("sim: the real database file has neither table filled",
          runs["sim"]["tables"], ((), ()))
    return runs


async def refused_send() -> None:
    say("(S1) A REFUSED SEND RECORDS NOTHING")
    RULES.clear()
    RULES.add("R13")
    rows, _ = fx.rotation_values()
    set_sheet(rows)
    begin("refused send")
    config.SALES_TEST_MODE = False
    path = fresh_db("refused")
    bot = new_bot(path)
    pretend(THURSDAY, 15, 1)
    planned = await bot._plan_drip(today=THURSDAY, already=[])
    msg = next(m for m in planned["messages"] if m["type"] == TRIGGER)
    # a channel OUTSIDE the sales scope: guardrails.send refuses it (and returns None), which is a real refused send
    await bot._send_drip_message(FakeChannel(9999), msg, marker=dl.iso(THURSDAY), channel_id=9999)
    clock.clear_time_override(why="verify_rule13")
    check("a send the guardrails refuse (a channel outside the sales scope): no state row, no posts row", dump(path), ((), ()))
    check("...and nothing was posted", [p for p in POSTED if not p.startswith("REPLY")], [])


async def test_day_on_a_live_database() -> None:
    say("(S3) A TEST DAY ON A DATABASE THAT IS NOT *_test.db")
    RULES.clear()
    RULES.add("R13")
    rows, _ = fx.rotation_values()
    set_sheet(rows)
    path = fresh_db("notatest", suffix=".db")
    run = await test_day_path(THURSDAY, path=path, label="test day, DB_PATH does not end in _test.db")
    show(run)
    check("the post was sent", len(run["posts"].get("R13") or []), 1)
    check("both tables untouched (the rotation is not recorded)", run["tables"], ((), ()))
    check("the log says why", any("R13" in m and "not recorded" in m for m in run["logs"]), True)
    set_sheet(rows)
    run2 = await test_day_path(THURSDAY, label="test day, DB_PATH ends in _test.db")
    check("with a *_test.db the same test day records the five and the post",
          (len(run2["tables"][0]), len(run2["tables"][1])), (5, 1))
    # the one constant that flips it (bot.NEXT_STEP_TEST_DAY_RECORDS_ON_LIVE_DB): "exactly what R9 does"
    import bot as botmodule
    check("the shipped value of that constant is False", botmodule.NEXT_STEP_TEST_DAY_RECORDS_ON_LIVE_DB, False)
    botmodule.NEXT_STEP_TEST_DAY_RECORDS_ON_LIVE_DB = True
    try:
        set_sheet(rows)
        run3 = await test_day_path(THURSDAY, path=fresh_db("notatest2", suffix=".db"),
                                   label="test day, DB_PATH not *_test.db, NEXT_STEP_TEST_DAY_RECORDS_ON_LIVE_DB=True")
    finally:
        botmodule.NEXT_STEP_TEST_DAY_RECORDS_ON_LIVE_DB = False
    check("with the constant True the same test day does record, on a database that is not *_test.db",
          (len(run3["tables"][0]), len(run3["tables"][1])), (5, 1))


async def queue_is_pure() -> None:
    say("(S2) THE QUEUE / PREVIEW READ TWICE LEAVES BOTH TABLES BYTE-IDENTICAL")
    RULES.clear()
    rows, meta = fx.rotation_values()
    set_sheet(rows)
    begin("preview")
    config.SALES_TEST_MODE = False
    path = fresh_db("preview")
    bot = new_bot(path)
    bot.db.record_next_step_mention("seed|row", signature="research|2026-09-01", on_date="2026-10-14", ask="researched")
    before = dump(path)
    raw = open(path, "rb").read()
    q1 = await bot._run_next_actions(today=THURSDAY)
    q2 = await bot._run_next_actions(today=THURSDAY)
    text = nextaction.preview_text(q2)
    check("two reads of the queue: both tables identical", dump(path), before)
    mine = [a for a in q2["actions"] if a.get("rule_id") == "R13"]
    check("the queue holds Rule 13's five", len(mine), 5)
    print("\n   the preview block for Rule 13:")
    for ln in [l for l in text.splitlines() if "R13" in l or "Next steps" in l or "no date" in l.lower()
               or "not in today" in l.lower()][:14]:
        print("     | " + ln)
    check("the preview names the four rows with no LI Connected Date",
          all(n in text for n in meta["undated"]), True)
    check("the preview has an R13 section", "R13" in text, True)
    check("nothing written to the sheet", UPDATES, [])


async def p3_gap_guard() -> None:
    say("(P3) THE CATCH-UP GUARD AND THE 15:00 POST")
    from bot import SalesBot

    rows, _ = fx.rotation_values()
    for r in rows:
        fx.edit(r, first_contact="TRUE")          # first contact done: R5 has no claim on the Connected people
    for n, co in enumerate(("Orbit Works", "Quill Data", "Rivet Labs"), start=1):     # three fresh prospects for R5
        rows.append(fx.values_row(100 + n, company=co, name=f"Prospect {n}", designation="CEO"))
    set_sheet(rows)
    RULES.clear()
    RULES.update({"R13", "R5"})
    begin("a spaced post, then R13 five minutes later")
    config.SALES_TEST_MODE = False
    path = fresh_db("p3")
    bot = new_bot(path)
    bot._live_loop_held = lambda what: False
    pretend(THURSDAY, 9, 0)
    planned = await bot._plan_drip(today=THURSDAY, already=[])
    spaced = [m for m in planned["messages"] if m["type"] != TRIGGER]
    check("setup: a spaced post and the 15:00 post are both planned", (len(spaced) >= 1, any(
        m["type"] == TRIGGER for m in planned["messages"])), (True, True))
    t_spaced = spaced[0]["send_at"]
    config.NEXT_STEP_TIME = (t_spaced + timedelta(minutes=5)).strftime("%H:%M")      # R13 five minutes after it
    clock.set_time_of_day(t_spaced.timetz().replace(tzinfo=None), by="verify_rule13")
    await bot._maybe_send_drip()
    check("the spaced post went out first", [s["rule"] for s in SENT], [spaced[0]["rule_id"]])
    clock.set_time_of_day((t_spaced + timedelta(minutes=5)).timetz().replace(tzinfo=None), by="verify_rule13")
    await bot._maybe_send_drip()
    check("5 minutes later the Rule 13 post still goes (the gap guard does not hold it)",
          [s["rule"] for s in SENT], [spaced[0]["rule_id"], "R13"])
    clock.clear_time_override(why="verify_rule13")
    config.NEXT_STEP_TIME = "15:00"

    sent_rows = [
        {"action_type": "ai_news", "sent_at": "2026-10-15T14:10:00+05:30"},
        {"action_type": TRIGGER, "sent_at": "2026-10-15T15:00:00+05:30"},
    ]
    last = SalesBot._last_drip_sent_at(sent_rows)
    check("the Rule 13 row does not become the 'last post' that delays the next spaced post",
          last.strftime("%H:%M"), "14:10")
    check("only Rule 13 rows: no last post", SalesBot._last_drip_sent_at([sent_rows[1]]), None)


# ---------------------------------------------------------------------------------------------
# (G1, G2) replies to a Rule 13 post
# ---------------------------------------------------------------------------------------------

async def replies_to_the_post() -> None:
    say("(G1, G2) A \"yes\" AND A \"done\" REPLIED TO A RULE 13 POST — what holds today")
    import approvals  # noqa: F401

    RULES.clear()
    RULES.add("R13")
    rows, _ = fx.rotation_values()
    set_sheet(rows)
    begin("replies")
    config.SALES_TEST_MODE = True
    config.SALES_TEST_CHANNEL_ID = 4242
    path = fresh_db("replies")
    bot = new_bot(path)
    await bot._handle_test_command(FakeMessage(SID), "make it " + dl.iso(THURSDAY))
    post = next((s for s in SENT if s["rule"] == "R13"), None)
    check("setup: the Rule 13 post went out", post is not None, True)
    drip_row = None
    with bot.db.conn() as c:
        got = c.execute("SELECT message_id FROM drip_sends WHERE action_type=? ORDER BY rowid DESC LIMIT 1",
                        (TRIGGER,)).fetchone()
    mid = got[0] if got else None
    check("setup: the post's message id is recorded", bool(mid), True)

    # an unrelated OPEN proposal (F2): a Meeting Date cell on another person, which a stray "yes" could apply
    import sheetwrite
    tab_ = read_sheet()[gtm_sheet.POCS]
    row_ = tab_.rows[5]
    plan_ = sheetwrite.plan_writes(tab=tab_, row=row_, fields=[{"role": "meeting_date", "value": "21-10-2026",
                                                              "supersedes": False, "quote": "met them"}],
                                   trigger=sheetwrite.TRIGGER_REPLY, reply_text="met them")
    bot.db.open_proposal(proposal_key="pCell", kind="cell_update", tab="Outreach PoCs", sheet_row=row_["_row"],
                         row_key=f"{row_['company'].lower()}|{row_['name'].lower()}", company=row_["company"],
                         poc=row_["name"], payload={"writes": plan_["writes"], "applied": plan_["applied"],
                                                    "asks": plan_["asks"], "skipped": plan_["skipped"]},
                         reply_text="met them", trigger="reply", proposed_text="Set Meeting Date? Reply yes.",
                         requested_by="Kushal", channel_id=4242, message_id="m-other",
                         created_at="2026-10-15T14:00:00")

    async def extracts_nothing(**_kw):
        return {"intent": "none"}
    bot.llm.extract_sheet_update = extracts_nothing
    state_before = dump(path)
    UPDATES.clear()
    parent = SimpleNamespace(id=int(mid), author=SimpleNamespace(id=BOT_ID))
    # a "yes" from each approver and a "done" from teammates, all replying to the Rule 13 post
    for who, text in ((VAISHNAVI, "yes"), (SID, "yes"), (KUSHAL, "done"), (VAISHNAVI, "done, researched them"),
                      (SID, "sent")):
        msg = FakeMessage(who, text, reply_to=int(mid), mid=9100 + who)
        msg.reference.cached_message = parent
        await bot._maybe_apply_sheet_update(msg, text)
    prop = bot.db.proposal("pCell")
    check("G1: no vote recorded on the unrelated open proposal", len(prop["votes"] or []), 0)
    check("G1: the unrelated proposal is still open", prop["status"], "open")
    check("G1/G2: zero sheet writes", UPDATES, [])
    check("G1/G2: no Rule 13 state changed", dump(path), state_before)
    # THE CONTROL: an approver's "yes" replied to the PROPOSAL'S OWN message does vote on it, so the zeros above are not
    # a broken setup. (Before NFT2-1063 a "yes" to ANY bot message with no proposal fell back to the newest open
    # proposal, which is how a "yes" to a Rule 13 post could have approved an unrelated write: plan F2. Whichever of
    # the two guards is in the tree, the invariant checked above is the same: a reply to a Rule 13 post votes on nothing.)
    own = SimpleNamespace(id="m-other", author=SimpleNamespace(id=BOT_ID))
    msg = FakeMessage(VAISHNAVI, "yes", reply_to="m-other", mid=9300)
    msg.reference.cached_message = own
    await bot._maybe_apply_sheet_update(msg, "yes")
    check("control: a 'yes' replied to the proposal's own message DOES vote on it (the setup is live)",
          len(bot.db.proposal("pCell")["votes"] or []) >= 1, True)


def replies_proper() -> None:
    say("(R) REPLIES TO A RULE 13 POST — the update-the-sheet line, the question, the engine (plan section 14)")
    import replies_world as rw

    day = THURSDAY

    def rows_of(n, kind):
        people = [("Priya Rao", "Acme Labs"), ("Dev Shah", "Borealis"), ("Mei Lin", "Cinder"),
                  ("Omar Khan", "Delta Co"), ("Lena Fox", "Echo Ltd")]
        out = []
        for i in range(n):
            step = "Research the PoC" if (kind == "research" or i == 0) else "Send email 1"
            out.append(fx.person(i + 1, name=people[i][0], company=people[i][1], li_date=date(2026, 8, 21 + i), step=step))
        return out

    def scenario(test_mode):
        got = {}

        def phase(rows, body):
            """One World (one database, so one post a day) per phase; `body(w, post)` is async."""
            with rw.World(test_mode=test_mode) as w:
                async def go():
                    w.sheet.set(pocs=rows)
                    w.rules = {"R13"}
                    planned = await w.bot._plan_drip(today=day, already=[])
                    msg = next(m for m in planned["messages"] if m["type"] == TRIGGER)
                    before = len(w.posted)
                    await w.bot._send_drip_message(w.chan, msg, marker=dl.iso(day), channel_id=w.chan.id)
                    await body(w, w.posted[before])
                asyncio.run(go())

        def state(w):
            with w.bot.db.conn() as c:
                return [c.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall() for t in
                        ("next_step_followups", "next_step_posts")]

        async def one(w, post):
            snap = (len(w.sheet.writes), dict(w.proposals()), len(w.votes()), w.model.calls, state(w))
            n = w.n_posted
            await w.say("done", reply_to=post)
            got["one"] = w.replies_after(n)
            got["quiet1"] = (len(w.sheet.writes), dict(w.proposals()), len(w.votes()), w.model.calls, state(w)) == snap

        async def two(w, post):
            n = w.n_posted
            await w.say("done", reply_to=post)
            got["two"] = w.replies_after(n)
            which = w.posted[-1]
            n = w.n_posted
            await w.say("2", reply_to=which)
            got["pick_number"] = w.replies_after(n)
            n = w.n_posted
            await w.say("Dev", reply_to=which)
            got["pick_name"] = w.replies_after(n)
            got["writes2"], got["votes2"] = list(w.sheet.writes), len(w.votes())

        async def five(w, post):
            m0 = w.model.calls
            await w.say("which email template should I use for Priya?", reply_to=post)
            got["engine_calls"] = w.model.calls - m0
            got["engine_saw_post"] = "Next Steps says" in w.model.request_text(m0)
            got["writes5"], got["votes5"] = list(w.sheet.writes), len(w.votes())

        phase(rows_of(1, "research"), one)
        phase(rows_of(2, "research"), two)
        phase(rows_of(5, "mixed"), five)
        got["writes"] = got["writes2"] + got["writes5"]
        got["votes"] = got["votes2"] + got["votes5"]
        return got

    live, test = scenario(False), scenario(True)
    check("'done' under a one-person research post: the Send email 1 line", live["one"],
          ["Nice, can you set Next Steps for Priya to Send email 1?"])
    check("...and nothing moved (no write, proposal, vote, model call, Rule 13 state)", live["quiet1"], True)
    check("two research people, an unnamed 'done': the numbered question", live["two"],
          ["Which one?" + chr(10) + "1. Priya Rao (Acme Labs)" + chr(10) + "2. Dev Shah (Borealis)" + chr(10)
           + "A name or a number is fine."])
    check("'2' picks Dev", live["pick_number"], ["Nice, can you set Next Steps for Dev to Send email 1?"])
    check("'Dev' picks Dev", live["pick_name"], ["Nice, can you set Next Steps for Dev to Send email 1?"])
    check("a real question reaches the engine with the post as context", (live["engine_calls"] > 0, live["engine_saw_post"]),
          (True, True))
    check("zero sheet writes and zero votes through all of it", (live["writes"], live["votes"]), ([], 0))
    for k in ("one", "two", "pick_number", "pick_name", "engine_calls", "engine_saw_post", "writes", "votes"):
        check(f"test mode == live: {k}", test[k], live[k])
    print("      one person  : " + " | ".join(live["one"]))
    print("      two people  : " + " | ".join(t.replace(chr(10), " / ") for t in live["two"]))


# ---------------------------------------------------------------------------------------------
# the env lines
# ---------------------------------------------------------------------------------------------

def print_env_lines() -> None:
    say("(env) THE LINES FOR BOTH .env FILES (agents never edit them)")
    print("   laptop .env:   NEW_ROW_WRITABLE_RANGES=A:P,X:Y        (is A:R today)")
    print("                  RESTRICTED_COLUMN_RANGES=A:I,Q:W,Z:AE  (already set)")
    print("                  GTM_COLUMN_MAP=                        (already blank; leave blank)")
    print("   server .env:   RESTRICTED_COLUMN_RANGES=A:I,Q:W,Z:AE")
    print("                  NEW_ROW_WRITABLE_RANGES=A:P,X:Y")
    print("                  GTM_COLUMN_MAP=                        (blank the stop-gap if it is set)")
    print("   optional:      COS_FOLLOWUP_CHECK_INTERVAL_MINUTES=15  (laptop has 150: '15:00' would be up to 2.5h late)")


# ---------------------------------------------------------------------------------------------
# THE FINAL DRY RUN
# ---------------------------------------------------------------------------------------------

def real_sheet_dry_run(date_str: str) -> int:
    """EXACTLY ONE read of the real Outreach PoCs tab, read-only. Rule 13 evaluated with an EMPTY state.

    No database at DB_PATH is opened (the state is the literal {}), no Discord, no model, nothing posted, nothing
    written. A second read is refused by a counter, not by hope. On any error: print its class and stop, no retry.
    """
    day = date.fromisoformat(date_str)
    reads = [0]
    real_read = gtm_sheet.SHEETS.read

    def one_read(*a, **k):
        reads[0] += 1
        if reads[0] > 1:
            raise RuntimeError("verify_rule13 --real-sheet: a second sheet read was refused")
        return real_read(*a, **k)

    gtm_sheet.SHEETS.read = one_read
    for forbidden in ("write_cells", "write_cells_on", "write_email", "append_row", "undo_cells", "clear_cells"):
        if hasattr(gtm_sheet.SHEETS, forbidden):
            setattr(gtm_sheet.SHEETS, forbidden,
                    lambda *a, _n=forbidden, **k: (_ for _ in ()).throw(RuntimeError(f"{_n} is forbidden here")))
    try:
        tabs = gtm_sheet.SHEETS.read()
    except Exception as e:            # one read, no retry
        print(f"REAL-SHEET READ FAILED: {type(e).__name__}. Stopped; not retried. reads={reads[0]}")
        return 3
    tab = tabs.get(gtm_sheet.POCS)
    if tab is None:
        print("REAL-SHEET READ: no Outreach PoCs tab came back. Stopped. reads=%d" % reads[0])
        return 3
    print(f"real-sheet reads: {reads[0]}   (read-only; nothing posted, nothing written, no database opened)")
    print("header row: " + " | ".join(f"{gtm_sheet._col_letter(i)}={h}" for i, h in enumerate(tab.headers)))
    role_cols = tab.canonical_role_to_col or tab.role_to_col
    print("roles: next_steps->%s  outreach_step->%s  email_1_sent->%s  poc_priority->%s" % tuple(
        gtm_sheet._col_letter(role_cols[r]) if r in role_cols else "UNMAPPED"
        for r in ("next_steps", "outreach_step", "email_1_sent", "poc_priority")))
    rule = rules_mod.by_id("R13")
    res = nextaction.run(today=day, rows=list(tab.rows), prospect_rows=list(tab.rows), day_rules=[rule],
                         next_step_state={})
    rep = (res.get("reports") or {}).get("R13") or {}
    connected = [r for r in tab.rows if nextaction._norm(r.get("sid_li_added")) in
                 [nextaction._norm(m) for m in config.NEXT_STEP_CONNECTED_MARKERS]]
    dated = [r for r in connected if nextaction._date(r, "li_connected_date")]
    from collections import Counter
    steps = Counter(nextaction.step_code(r.get("outreach_step")) for r in dated)
    print(f"Connected rows: {len(connected)}   dated: {len(dated)}   undated: {len(connected) - len(dated)}")
    steps_all = Counter(nextaction.step_code(r.get("outreach_step")) for r in connected)
    print(f"steps among ALL {len(connected)} Connected rows (the ticket says 18 send email 1 / 11 research / 4 send email 2): "
          f"{dict(steps_all)}")
    print(f"steps among the {len(dated)} dated: {dict(steps)}")
    print(f"R13 report: due={rep.get('due')} waiting={rep.get('waiting')} paused={len(rep.get('paused') or [])} "
          f"completed={rep.get('completed')} closed={len(rep.get('closed') or [])} "
          f"unknown_step={[(e.get('sheet_row'), e.get('detail')) for e in rep.get('unknown_step') or []]}")
    print("no LI Connected Date: " + ", ".join(f"row {e.get('sheet_row')} {e.get('poc')}" for e in rep.get("no_date") or []))
    items = sorted([a for a in res["actions"] if a.get("rule_id") == "R13"], key=lambda a: a.get("pick_order", 0))
    print(f"\nTHE {len(items)} PEOPLE RULE 13 WOULD PICK FOR {day:%A %d %b %Y}:")
    for it in items:
        print(f"  {it.get('pick_order', 0) + 1}. sheet row {it.get('sheet_row')}  [{it.get('ask')}]  {it['text']}")
    print(f"\nreads={reads[0]}  posted=0  written=0  database opened=none  model calls=0")
    return 0


# ---------------------------------------------------------------------------------------------

async def main() -> None:
    print(f"real date {dl.iso(dl.real_today_ist())} | pretend Thursday {THURSDAY} | cap {config.DAILY_MESSAGE_CAP} | "
          f"window {config.SALES_DRIP_START}-{config.SALES_DRIP_END} | restricted {config.RESTRICTED_COLUMN_RANGES} | "
          f"new-row {config.NEW_ROW_WRITABLE_RANGES} | NEXT_STEP_TIME {config.NEXT_STEP_TIME}")
    print_role_maps()
    print_rotation_and_clock()
    await the_post_four_ways()
    await refused_send()
    await test_day_on_a_live_database()
    await queue_is_pure()
    await p3_gap_guard()
    await replies_to_the_post()
    await asyncio.to_thread(replies_proper)     # it runs its own event loops (one per World)
    print_env_lines()


if REAL_SHEET:
    if not REAL_DATE:
        print("--real-sheet needs --date YYYY-MM-DD")
        sys.exit(2)
    code = real_sheet_dry_run(REAL_DATE)
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)

try:
    asyncio.run(main())
finally:
    clock.forget()
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}"
      + (f"  ({len(SKIPPED)} SKIPPED: deferred to NFT2-1063)" if SKIPPED else ""))
sys.exit(1 if failures else 0)
