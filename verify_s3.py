"""S3 — DELIVERABLES, R7, R10, R5 EMAILS, R3 EVENTS. Real output.

    python verify_s3.py              every check
    python verify_s3.py --only iv,v  some of them (i ii iii iv v w)

Each day is lived three ways — the live sweep (a dry run), "make it <date>" and
"simulate <date>" — and the posts are compared.

  (i)   Monday: deliverables with team, due date and links; an Action Item on
        two rows appears once, a link shared by two items appears once
  (ii)  Monday: R7 with 7 eligible contacts -> 5 lines, longest wait first,
        founder first on a tie, "+2 more next Monday"
  (iii) Monday: R10 with Prospect Status and Closure Prob% empty -> the message;
        with values and nothing qualifying -> silence, and a log line saying why
  (iv)  Tuesday: R5 names never-contacted rows that are NOT "active"; one email
        found with its source, one "no public email found" (the model's guessed
        address is thrown out); "yes" writes only that one cell; a second yes
        does nothing because the cell is filled; undo empties it again; the
        two-companies-a-week and "skip them?" counters move
  (v)   Wednesday: R3 with a registered event in 10 days, an unregistered event
        next Tuesday, a past event and one whose deadline has passed -> only the
        right two show, the offer appears, "yes" creates Monday's reminder, and
        Monday 14:00 posts it once, outside the cap
  (w)   the Bot Rules tab wording for rules 3, 4, 5, 7 and 10

NOTHING REACHES DISCORD, THE REAL SHEET OR THE WEB. The sheet is a FIXTURE: grids
of cells parsed by the real `gtm_sheet` parser (so the roles, the header names
and the column letters are the real ones) behind a stand-in worksheet that
records every cell it is asked to update. The search is a stand-in too
(`search_backend.search` answers from a table) and so is the model: it echoes
for composed messages, and for the email extraction it answers from a table —
including one GUESSED address, to show the verifier throwing it out. Each run
gets a fresh database. Settings are pinned to the shipped defaults, except
EMAIL_WRITE_ALLOWED, which is on so the write can be shown.
"""
import asyncio
import copy
import hashlib
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
os.chdir(HERE)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ONLY: set = set()
for _i, _a in enumerate(sys.argv):
    if _a == "--only" and _i + 1 < len(sys.argv):
        ONLY = {x.strip().lower() for x in sys.argv[_i + 1].split(",") if x.strip()}

TMP = tempfile.mkdtemp(prefix="saley-s3-")
os.environ["STATE_DIR"] = os.path.join(TMP, "state")
os.environ["DB_PATH"] = os.path.join(TMP, "base_test.db")

import os as _os  # noqa: E402  NFT2-1064 offline guard: canned source statuses, real Sheets/Drive calls blocked
import sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "tests"))
import offline_guard  # noqa: E402
GUARD = offline_guard.install_script()
import config  # noqa: E402

SID, VAISHNAVI, KUSHAL = 111, 222, 333
SETTINGS = {
    "DB_PATH": os.environ["DB_PATH"], "STATE_DIR": os.environ["STATE_DIR"],
    "TEAM_ROSTER_IDS": [SID, VAISHNAVI, KUSHAL], "SALES_APPROVER_IDS": [SID, VAISHNAVI],
    "SALES_FINAL_SAY_ID": SID,
    "ROSTER_DISPLAY_NAMES": {str(SID): "Sid", str(VAISHNAVI): "Vaishnavi",
                             str(KUSHAL): "Kushal"},
    "DAILY_MESSAGE_CAP": 5, "SALES_DRIP_START": "14:00", "SALES_DRIP_END": "18:30",
    "MESSAGE_GAP_MINUTES": 90, "MESSAGE_JITTER_MINUTES": 15,
    "MESSAGE_GAP_MIN_MINUTES": 30, "DRIP_REASK_DAYS": 2, "DRIP_WEEKDAYS_ONLY": True,
    "SUNDAY_RULE_IDS": ["R4"], "NEWS_MAIN_TIME": "14:00", "MEETING_DAYOF_TIME": "10:00",
    "TEST_MORNING_TIME": "10:00", "TEST_AFTERNOON_TIME": "14:00",
    "NEXT_ACTION_ENABLED": True, "NEXT_ACTION_WEEKEND_SHIFT": True,
    "WEEKLY_FUNNEL_ENABLED": False, "SALES_DMS_ENABLED": False,
    "DRIP_LLM_COMPOSE": True, "TEST_POST_GAP_SECONDS": 0,
    "SIMULATION_FAST_GAP_SECONDS": 0, "SIMULATION_REAL_MENTIONS": False,
    "REMINDER_DEFAULT_TIME": "14:00", "SHEET_WRITE_UNDO_HOURS": 24,
    # the settings under test
    "DELIVERABLE_NEAR_DAYS": 3, "DELIVERABLE_DEFAULT_OWNER": "Vaishnavi",
    "DM_NO_MEETING_DAYS": 7, "DM_NO_MEETING_MAX_CONTACTS": 5,
    "CLOSURE_SUPPORT_MIN": 50, "PROSPECT_COMPANIES_PER_WEEK": 2,
    "PROSPECT_REPEAT_ASK_AT": 3, "EMAIL_LOOKUP_MAX_PER_POST": 5,
    "EMAIL_WRITE_ALLOWED": True, "SHEET_WRITES_ENABLED": True,
    "EVENTS_WINDOW_DAYS": 14, "EVENTS_REMIND_AGAIN_WEEKDAY": 0,
    "RESEARCH_CACHE_DAYS": 7,
    # a search backend is "configured"; the search itself is a stand-in
    "WEB_SEARCH_ENABLED": True, "SEARCH_BACKEND": "searxng",
    "SEARXNG_URL": "http://127.0.0.1:9", "SEARCH_FALLBACKS": [],
}
for _k, _v in SETTINGS.items():
    setattr(config, _k, _v)
config.digest_enabled = lambda: True      # the kill switch is not what is tested

import clock  # noqa: E402
import deadlines as dl  # noqa: E402
import drip  # noqa: E402
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
POSTED: list = []
SENT: list = []
LOGS: list = []
SEARCHES: list = []       # every query the email lookup searched for
RULES: set = set()        # the rule ids the day under test plans
FRESH = [0]


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
    KEEP = ("[R3]", "[R4]", "[R10]", "[rules]", "[email]", "[approvals]", "[gtm]",
            "[reminders]", "[drip] slot", "[events]", "[research-cache]")

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
              "google", "urllib3"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


# -- the fixture sheet: real parser, stand-in worksheet ------------------------

POC_HEADERS = ["Sr No", "Company/Uni", "Industry", "Name", "Designation", "Email id",
               "Based", "Research Paper Link", "LI Url", "First Contact",
               "First Contact Type", "First Contact Date", "Sid - LI Addition",
               "LI Connected Date", "LI DM Sent", "LI DM Date", "Meeting Date",
               "Meeting Status", "Next Steps/Notes", "Package", "Prospect Status",
               "Closure Prob%", "Estd. Deal Size (USD)", "Deal Status"]
COL = {h: i for i, h in enumerate(POC_HEADERS)}
DELIV_HEADERS = ["Action Item", "Priority", "Status", "Tentative Deadline",
                 "Functional Dependency", "Remarks", "Link/Destination"]
EVENT_HEADERS = ["Event", "Date", "Last day for registration", "Registered?", "Link",
                 "Location"]
GRIDS = {"Outreach PoCs": [POC_HEADERS], "Deliverables Checklist": [DELIV_HEADERS],
         "AI Events & Summits": [EVENT_HEADERS]}
UPDATES: list = []        # every cell the stand-in worksheet was asked to update
ACTIVE = [True]           # does the activation gate let the PoC rows through?


def poc(n, company, name, designation="", **cells) -> list:
    """One Outreach PoCs row, by header name."""
    row = [""] * len(POC_HEADERS)
    row[COL["Sr No"]], row[COL["Company/Uni"]] = str(n), company
    row[COL["Name"]], row[COL["Designation"]] = name, designation
    for header, value in cells.items():
        row[COL[header.replace("_", " ")] if header.replace("_", " ") in COL
            else COL[header]] = value
    return row


def cell(d: date) -> str:
    return d.strftime("%d-%m-%Y")


def bare(d: date) -> str:
    return f"{d.day}-{d.strftime('%b')}"


def set_sheet(*, pocs=(), deliverables=(), events=()) -> None:
    GRIDS["Outreach PoCs"] = [POC_HEADERS] + [list(r) for r in pocs]
    GRIDS["Deliverables Checklist"] = [DELIV_HEADERS] + [list(r) for r in deliverables]
    GRIDS["AI Events & Summits"] = [EVENT_HEADERS] + [list(r) for r in events]
    UPDATES.clear()


def read_sheet(which=gtm_sheet.ORIGINAL, *, force: bool = False) -> dict:
    """The fixture, parsed FRESH by the real parser on every read."""
    out = {}
    for title, grid in GRIDS.items():
        tab = gtm_sheet.SHEETS._parse_values(title, copy.deepcopy(grid),
                                             read_at=_time.time())
        if tab is not None:
            out[tab.kind] = tab
    return out


class FakeWorksheet:
    def __init__(self, title):
        self.title = title

    def update(self, values=None, range_name="", value_input_option=""):
        m = re.fullmatch(r"([A-Z]+)(\d+)", str(range_name))
        col = 0
        for ch in m.group(1):
            col = col * 26 + (ord(ch) - 64)
        row = int(m.group(2))
        grid = GRIDS[self.title]
        grid[row - 1][col - 1] = values[0][0]
        UPDATES.append((self.title, range_name, values[0][0]))


gtm_sheet.SHEETS.read = read_sheet
gtm_sheet.SHEETS.cadence_tab = lambda *a, **k: (read_sheet().get(gtm_sheet.POCS), "fixture")
gtm_sheet.SHEETS._open = lambda *a, **k: SimpleNamespace(worksheet=FakeWorksheet)
gtm_sheet.SHEETS._refuse_if_read_only = lambda *a, **k: ""
gtm_sheet.SHEETS.staleness_note = lambda *a, **k: ""


# -- the search and the model, answering from tables ---------------------------

SEARCH_RESULTS = {
    "Ada Lovelace": [
        {"title": "Acme AI | Team", "url": "https://acme.ai/team",
         "snippet": "Ada Lovelace, CTO. Leads the speech models group."},
        {"title": "Ada Lovelace — speaker at Voice Summit",
         "url": "https://voicesummit.example/speakers/ada-lovelace",
         "snippet": "Ada Lovelace is CTO of Acme AI. Contact: ada@acme.ai"},
    ],
    "Bo Chen": [
        {"title": "Borealis raises seed round", "url": "https://news.example/borealis",
         "snippet": "Founder Bo Chen said the round will fund hiring. Press: "
                    "press@borealis.com"},
    ],
}
EMAIL_ANSWERS = {
    "Ada Lovelace": "EMAIL | ada@acme.ai | 2",
    # A GUESS: built from the name and the company's domain, in no snippet.
    "Bo Chen": "EMAIL | bo.chen@borealis.com | 1",
}


def fake_search(query, *, n=10, news=False, site=None, days=None, rule="search"):
    SEARCHES.append(str(query))
    for name, results in SEARCH_RESULTS.items():
        if name in str(query):
            return [dict(r) for r in results][:n]
    return []


search_backend.search = fake_search


class StandInModel:
    """Echoes a fingerprint for a composed message (as verify_parity does), and
    answers the email extraction from EMAIL_ANSWERS."""

    def create(self, **kw):
        system = persona.blocks_text(kw.get("system"))
        prompt = str((kw.get("messages") or [{}])[-1].get("content") or "")
        text = "{}"
        if prompt.startswith("Find the PUBLISHED email address of this person:"):
            who = prompt.split("this person:", 1)[1].split(" at ")[0].strip()
            text = EMAIL_ANSWERS.get(who, "NONE")
        elif prompt.startswith("Write ONE short proactive message"):
            ref = hashlib.sha1((system + "\n@@\n" + prompt).encode("utf-8")).hexdigest()[:8]
            m = re.search(r"Address them as EXACTLY this, once, at the start: (.+)", prompt)
            who = (m.group(1).strip() if m else "")
            who = "" if who.startswith("(") else who + ", "
            block = re.search(r"<<<\n(.*?)\n>>>", prompt, re.S)
            if block:
                text = (f"{who}a quick list for you, echo {ref}.\n{block.group(1)}\n"
                        "tell me if any of it has moved.")
            else:
                c = re.search(r"in one sentence\): (.+)", prompt)
                text = (f"{who}{(c.group(1).strip() if c else 'this one')} could use a "
                        f"look this week, echo {ref}. no rush.")
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=400, output_tokens=40))


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
        return SimpleNamespace(id=len(POSTED), jump_url="https://discord/x",
                               channel=self)

    def typing(self):
        return FakeTyping()


TEST_CHANNEL = FakeChannel(config.SALES_TEST_CHANNEL_ID or 4242)


def person(uid: int):
    return SimpleNamespace(id=uid, bot=False,
                           display_name=config.ROSTER_DISPLAY_NAMES[str(uid)],
                           name=config.ROSTER_DISPLAY_NAMES[str(uid)])


class FakeMessage:
    """A message from one of the team — optionally a REPLY to one of the bot's."""

    def __init__(self, uid=SID, text="", reply_to=None, mid=9000):
        self.id = mid
        self.channel = TEST_CHANNEL
        self.author = person(uid)
        self.content = text
        self.reference = SimpleNamespace(message_id=reply_to) if reply_to else None
        self.replies: list = []

    async def reply(self, body, mention_author=False):
        self.replies.append(str(body))
        POSTED.append("REPLY: " + str(body))
        return SimpleNamespace(id=len(POSTED), jump_url="https://discord/x",
                               channel=self.channel)


def strip(body: str) -> str:
    out = str(body or "")
    tag = str(config.SIMULATION_PREFIX or "").strip()
    if tag and out.startswith(tag):
        out = out[len(tag):].lstrip()
    return re.sub(r"<@!?(\d+)>", lambda m: "@" + str(
        config.ROSTER_DISPLAY_NAMES.get(str(m.group(1))) or "user"), out)


def fresh_db(name: str) -> str:
    FRESH[0] += 1
    path = os.path.join(TMP, f"{name}{FRESH[0]}_test.db")
    config.DB_PATH = path
    clock.forget()
    voice.invalidate()
    return path


def new_bot(path: str):
    from bot import SalesBot

    bot = SalesBot()
    bot.db = DB(path)
    if bot.llm is None:
        print("No model client (ANTHROPIC_API_KEY is unset): the composer and the "
              "email extraction cannot run even against the stand-in.")
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
            queue["actions"] = [a for a in queue["actions"]
                                if str(a.get("rule_id") or "") in RULES]
        return queue

    real_send = bot._send_drip_message

    async def recording_send(channel, message, **kw):
        start = len(POSTED)
        await real_send(channel, message, **kw)
        SENT.append({"day": kw.get("marker"), "rule": message.get("rule_id") or "—",
                     "type": message.get("type"), "at": message.get("send_at_hhmm"),
                     "first_id": start + 1 if len(POSTED) > start else None,
                     "posts": list(POSTED[start:])})

    leave.who_is_away = nobody_away
    # THE ACTIVATION GATE: every row through, or (for R5) none of them.
    bot._split_active = lambda rows, why: (
        (list(rows), []) if ACTIVE[0] else ([], list(rows)))
    bot._run_next_actions = the_rules_under_test
    bot._send_drip_message = recording_send
    bot._convert_if_already_done = nothing
    bot._sweep_proposals = nothing
    bot._maybe_breaking_news = nothing
    bot._maybe_poll_feeds = nothing
    # R3's web half is stubbed to "found nothing": it searches, and that is not
    # what is being checked here (verify_news_events.py covers it).
    bot._discover_events = no_new_events
    bot._backfill_deadlines = no_missing_deadlines
    bot.get_channel = lambda cid: FakeChannel(cid)
    return bot


def begin(label: str) -> None:
    POSTED.clear()
    SENT.clear()
    LOGS.clear()
    tone.RNG = random.Random(20260921)
    voice._last_choice.clear()
    print(f"\n   ---- {label} ----")


def pretend(day: date, hh: int, mm: int = 0) -> None:
    was = config.SALES_TEST_MODE
    config.SALES_TEST_MODE = True              # only to be allowed to set the day
    clock.set_day(day, by="verify_s3")
    config.SALES_TEST_MODE = was
    clock.set_time_of_day(time(hh, mm), by="verify_s3")


def collect(bot) -> dict:
    return {"posts": {s["rule"]: [strip(p) for p in s["posts"]] for s in SENT},
            "raw": [dict(s) for s in SENT], "bot": bot, "logs": list(LOGS),
            "searches": list(SEARCHES)}


async def live_path(day: date) -> dict:
    begin(f"LIVE (dry run) — _maybe_send_drip ticked through {dl.iso(day)}")
    config.SALES_TEST_MODE = False
    bot = new_bot(fresh_db("live"))
    pretend(day, 9, 0)
    bot._live_loop_held = lambda what: False
    planned = await bot._plan_drip(today=day, already=[])
    step = timedelta(minutes=drip.min_gap_minutes() + 1)
    at = None
    for target in [m["send_at"] for m in (planned or {}).get("messages") or []] + [None]:
        base = target or (at + step if at else datetime.combine(day, time(22, 0), dl.IST))
        at = base if at is None else max(base, at + step)
        clock.set_time_of_day(at.timetz().replace(tzinfo=None), by="verify_s3")
        await bot._maybe_send_drip()
    clock.clear_time_override(why="verify_s3")
    return collect(bot)


async def test_path(day: date, *, bot=None, label: str = "") -> dict:
    begin(label or f"TEST DAY — \"make it {dl.iso(day)}\"")
    config.SALES_TEST_MODE = True
    if bot is None:
        bot = new_bot(fresh_db("testday"))
    await bot._handle_test_command(FakeMessage(SID), "make it " + dl.iso(day))
    return collect(bot)


async def sim_path(day: date) -> dict:
    begin(f"SIMULATION — \"simulate {dl.iso(day)}\"")
    config.SALES_TEST_MODE = True
    bot = new_bot(fresh_db("sim"))
    await bot._handle_simulation(FakeMessage(SID), "simulate " + dl.iso(day))
    return collect(bot)


async def three_ways(day: date, sheet) -> tuple:
    """The same day, the same sheet, three times. `sheet()` rebuilds the fixture
    so no run sees a cell an earlier one wrote."""
    runs = []
    for path in (live_path, test_path, sim_path):
        sheet()
        SEARCHES.clear()
        runs.append(await path(day))
    return tuple(runs)


def show(run: dict, rule: str) -> None:
    for body in run["posts"].get(rule) or ["(no post)"]:
        for line in body.splitlines():
            print("     | " + line)


def same(rule: str, live: dict, test: dict, sim: dict) -> None:
    check(f"{rule}: the same post, word for word, on the live sweep, the test day "
          "and the simulation",
          live["posts"].get(rule) == test["posts"].get(rule) == sim["posts"].get(rule)
          and bool(live["posts"].get(rule)))


# -- (i) (ii) (iii) the Monday -------------------------------------------------

MON = date(2026, 9, 21)                 # all of it is already in the past
TUE, WED, THU = (MON + timedelta(days=n) for n in (1, 2, 3))
TRACKER = "https://docs.google.com/spreadsheets/d/1TrAcKeR/edit"
OVERVIEW = "https://docs.google.com/document/d/1OvErViEw/edit"


def monday_sheet() -> None:
    def ago(n):
        return cell(MON - timedelta(days=n))

    set_sheet(
        pocs=[
            # R7: seven contacts whose DM has had no meeting for 9 to 30 days.
            poc(1, "Acme AI", "Ada Lovelace", "CTO", First_Contact="TRUE",
                LI_DM_Date=ago(30), **{"Next Steps/Notes": "waiting on their legal"}),
            poc(2, "Borealis", "Bo Chen", "Founder", First_Contact="TRUE",
                LI_DM_Date=ago(25)),
            poc(3, "Cinder", "Cy Diaz", "Research Scientist", First_Contact="TRUE",
                LI_DM_Date=ago(21), **{"Next Steps/Notes": "asked for the deck"}),
            poc(4, "Cinder", "Dee Evans", "Co-Founder", First_Contact="TRUE",
                LI_DM_Date=ago(21)),
            poc(5, "Echo Labs", "Eve Rao", "CEO", First_Contact="TRUE",
                LI_DM_Date=ago(14), **{"Next Steps/Notes": "out until the 25th"}),
            poc(6, "Fathom", "Fay Lin", "Head of ML", First_Contact="TRUE",
                LI_DM_Date=ago(10)),
            poc(7, "Gantry", "Gil Shah", "VP Eng", First_Contact="TRUE",
                LI_DM_Date=ago(9)),
            # ...and one whose DM is too recent to chase.
            poc(8, "Harbor", "Hal Iyer", "CTO", First_Contact="TRUE", LI_DM_Date=ago(3)),
        ],
        deliverables=[
            ["Pulse Product Overview Document", "P1", "In progress",
             bare(MON - timedelta(days=3)), "Sales", "waiting on pricing", OVERVIEW],
            ["MSA template", "P1", "", bare(MON + timedelta(days=3)), "", "", TRACKER],
            ["Pricing page copy", "P1", "Not started", bare(MON + timedelta(days=4)),
             "Marketing", "", TRACKER],                    # the same link as the MSA
            ["Pulse Product Overview Document", "P1", "In progress",
             bare(MON + timedelta(days=1)), "Product", "", OVERVIEW],   # the same item
            ["Security questionnaire", "P1", "", bare(MON + timedelta(days=2)), "Legal",
             "", ""],                                      # no link at all
            ["Case study: Hinglish STT", "P2", "", bare(MON + timedelta(days=1)), "Sales",
             "", "https://docs.google.com/document/d/1CaSe/edit"],
            ["NDA", "P1", "Done", bare(MON + timedelta(days=1)), "Legal", "", ""],
        ],
    )


def shipped_rules_with_r7_on() -> str:
    """A TEMP COPY of bot_rules.yaml with R7 switched on, for check (ii) only.

    WHY (RULE 13, 7 Oct 2026): Vaishnavi decided "rule 13 supercedes" rule 7, so the shipped file now has
    `enabled: false` on R7. The evaluator, the renderer and the overflow line are KEPT (switching the rule back on is
    one word), and this check still proves they work — against a copy of the real file with exactly that one word
    changed. The shipped file is untouched, and the check after it proves R7 posts nothing as shipped.
    """
    src = open(os.path.join(HERE, "bot_rules.yaml"), encoding="utf-8").read()
    start = src.index("  - id: R7\n")
    end = src.index("  - id: R8\n")
    block = src[start:end]
    assert "enabled: false" in block, "R7 is expected to be disabled in the shipped file"
    path = os.path.join(TMP, "bot_rules_r7_on.yaml")
    with open(path, "w", encoding="utf-8") as f:
        f.write(src[:start] + block.replace("enabled: false", "enabled: true") + src[end:])
    return path


async def the_monday() -> None:
    say(f"(i) (ii) (iii) A MONDAY — {MON:%A %d %b %Y}: R4, R7 and R10")
    shipped_file = config.BOT_RULES_FILE
    config.BOT_RULES_FILE = shipped_rules_with_r7_on()
    rules_mod.reload()
    check("(ii) is run against a copy of the shipped rules with ONLY R7 switched on",
          (rules_mod.by_id("R7").enabled, "R7" in [r.id for r in rules_mod.for_day(MON)]), (True, True))
    RULES.clear()
    RULES.update({"R4", "R7", "R10"})
    ACTIVE[0] = True
    live, test, sim = await three_ways(MON, monday_sheet)

    print("\n   (i) R4 — DELIVERABLES, as posted by the live sweep:")
    show(live, "R4")
    body = "\n".join(live["posts"].get("R4") or [])
    lines = body.splitlines()
    items = [i for i, l in enumerate(lines) if re.match(r"^\d+\. ", l)]
    blocks = [lines[i:(items[n + 1] if n + 1 < len(items) else len(lines) - 1)]
              for n, i in enumerate(items)]
    titles = [re.sub(r"^\d+\. ", "", b[0]) for b in blocks]
    check("four items: the P1s that are open — no P2, no Done",
          titles, ["Pulse Product Overview Document", "Security questionnaire",
                   "MSA template", "Pricing page copy"])
    check("the same Action Item on two rows appears once",
          titles.count("Pulse Product Overview Document"), 1)
    check("every item has its Team line, then its Due line",
          all(b[1].startswith("   Team: ") and b[2].startswith("   Due: ")
              for b in blocks), True)
    check("the team is the Functional Dependency — or the default owner when blank",
          [b[1].strip() for b in blocks],
          ["Team: Sales", "Team: Legal", "Team: Vaishnavi", "Team: Marketing"])
    check("overdue says by how much; the rest just the date",
          [b[2].strip() for b in blocks],
          ["Due: Fri 18 Sep · 3 days overdue", "Due: Wed 23 Sep", "Due: Thu 24 Sep",
           "Due: Fri 25 Sep"])
    check("a link only where the row has one",
          [len(b) for b in blocks], [4, 3, 4, 3])
    check("each link appears once in the whole message — two items share the tracker",
          (body.count(OVERVIEW), body.count(TRACKER)), (1, 1))
    check("links are written [Doc](<…>)",
          [b[3].strip() for b in blocks if len(b) > 3],
          [f"[Doc](<{OVERVIEW}>)", f"[Doc](<{TRACKER}>)"])
    check("no remarks anywhere", "waiting on pricing" in body, False)
    same("R4", live, test, sim)

    print("\n   (ii) R7 — DM SENT, NO MEETING, as posted by the live sweep:")
    show(live, "R7")
    body = "\n".join(live["posts"].get("R7") or [])
    numbered = [l for l in body.splitlines() if re.match(r"^\d+\. ", l)]
    check("7 contacts were eligible; 5 are listed",
          (len(nextaction.EVALUATORS[nextaction.R_DM_NO_MEETING](
              rules_mod.by_id("R7"), {"today": MON, "rows": read_sheet()[gtm_sheet.POCS].rows,
                                      "snoozes": {}})), len(numbered)), (7, 5))
    check("longest since the DM first; on the 21-day tie the founder leads",
          [l.split(" — ")[0].split(". ", 1)[1] for l in numbered],
          ["Ada Lovelace", "Bo Chen", "Dee Evans", "Cy Diaz", "Eve Rao"])
    check("each line: name — company — DM sent N days ago — last note",
          numbered[0], "1. Ada Lovelace — Acme AI — DM sent 30 days ago — waiting on "
                       "their legal")
    check("a contact with no note says so",
          numbered[1], "2. Bo Chen — Borealis — DM sent 25 days ago — no note logged")
    check("contacts, not companies: both people at Cinder are listed",
          sum("— Cinder —" in l for l in numbered), 2)
    check("the overflow line", "(+2 more next Monday)" in body, True)
    check("the DM sent 3 days ago is not chased", "Hal Iyer" in body, False)
    same("R7", live, test, sim)

    # BACK TO THE SHIPPED FILE. As shipped, R7 is off (replaced by rule 13 on 7 Oct 2026): the same Monday, the same
    # seven eligible contacts, and the live sweep posts nothing for R7.
    config.BOT_RULES_FILE = shipped_file
    rules_mod.reload()
    check("as shipped, R7 is off and is not one of Monday's rules",
          (rules_mod.by_id("R7").enabled, "R7" in [r.id for r in rules_mod.for_day(MON)]), (False, False))
    RULES.clear()
    RULES.add("R7")
    monday_sheet()
    shipped_run = await live_path(MON)
    check("as shipped, a Monday with seven eligible DM'd contacts posts no R7 at all",
          shipped_run["posts"].get("R7"), None)
    RULES.clear()
    RULES.update({"R4", "R7", "R10"})

    print("\n   (iii) R10 — CLOSURE SUPPORT, as posted by the live sweep:")
    show(live, "R10")
    body = "\n".join(live["posts"].get("R10") or [])
    check("the columns are empty on every active row -> the message says so",
          nextaction.CLOSURE_EMPTY_NOTICE in body, True)
    check("...word for word, never composed", "echo " in body, False)
    same("R10", live, test, sim)

    LOGS.clear()
    rows = copy.deepcopy(read_sheet()[gtm_sheet.POCS].rows)
    rows[0]["prospect_status"], rows[0]["closure_prob"] = "Lead", "20%"
    rows[1]["prospect_status"], rows[1]["closure_prob"] = "Demo", "40%"
    quiet = nextaction.run(today=MON, rows=rows, day_rules=[rules_mod.by_id("R10")])
    for line in [x for x in LOGS if x.startswith("[R10]")]:
        print("   log: " + line)
    check("values exist and no deal qualifies -> silent", quiet["actions"], [])
    check("...and the log says why", any("Nothing posted" in x for x in LOGS), True)


# -- (iv) the Tuesday: R5 ------------------------------------------------------


def tuesday_sheet() -> None:
    set_sheet(pocs=[
        poc(1, "Acme AI", "Cy Diaz", "Research Scientist", **{"Email id": "cy@acme.ai"}),
        poc(2, "Acme AI", "Ada Lovelace", "CTO"),
        poc(3, "Borealis", "Bo Chen", "Founder"),
        poc(4, "Cinder", "Dee Evans", "Co-Founder"),              # a third company
        poc(5, "Acme AI", "Al Dead", "CEO", Deal_Status="Dead"),  # a stop rule
        poc(6, "Borealis", "Done Already", "CTO", First_Contact="TRUE"),
    ])


async def the_tuesday() -> None:
    say(f"(iv) A TUESDAY — {TUE:%A %d %b %Y}: R5, the emails, and the yes")
    RULES.clear()
    RULES.add("R5")
    ACTIVE[0] = False                        # NOT ONE ROW IS "ACTIVE"
    print("   the activation gate lets NO row through today — R5 used to see only "
          "the rows it did")
    live, test, sim = await three_ways(TUE, tuesday_sheet)
    print("\n   R5 — PoCs TO CONTACT, as posted by the live sweep:")
    show(live, "R5")
    for line in [x for x in live["logs"] if x.startswith("[email]")]:
        print("   log: " + line[:230])
    body = "\n".join(live["posts"].get("R5") or [])
    numbered = [l for l in body.splitlines() if re.match(r"^\d+\. ", l)]
    check("three contacts, none of them on an active row",
          [l.split(" — ")[0].split(". ", 1)[1] for l in numbered],
          ["Ada Lovelace (CTO)", "Cy Diaz (Research Scientist)", "Bo Chen (Founder)"])
    check("two companies a week: Cinder waits; the stopped and the contacted rows "
          "are not there",
          [n for n in ("Dee Evans", "Al Dead", "Done Already") if n in body], [])
    check("one email found, with the page it was found on",
          numbered[0], "1. Ada Lovelace (CTO) — Acme AI — email found: ada@acme.ai "
                       "([voicesummit.example](<https://voicesummit.example/speakers/"
                       "ada-lovelace>))")
    check("one \"no public email found\" — the model's bo.chen@borealis.com was in "
          "no snippet, so it was thrown out",
          (numbered[2], "bo.chen@borealis.com" in body),
          ("3. Bo Chen (Founder) — Borealis — no public email found", False))
    check("a contact whose email is on file is not looked up and gets no email note",
          numbered[1], "2. Cy Diaz (Research Scientist) — Acme AI")
    check("exactly two searches, in the spec's words",
          live["searches"], ['"Ada Lovelace" "Acme AI" email',
                             '"Bo Chen" "Borealis" email'])
    check("the post ends on the offer",
          body.splitlines()[-1], "Want me to add the email I found to the sheet? Say yes.")
    same("R5", live, test, sim)
    check("nothing has been written to the sheet by any of the three", UPDATES, [])

    # ---- the yes, on the test day's own post ----
    tuesday_sheet()
    SEARCHES.clear()
    run = await test_path(TUE, label=f"THE YES — \"make it {dl.iso(TUE)}\", then a reply")
    bot = run["bot"]
    post_id = run["raw"][0]["first_id"]
    proposals = bot.db.open_proposals_for_message(str(post_id))
    print(f"   open proposals keyed to the post: "
          f"{[(p['kind'], p['proposed_text']) for p in proposals]}")
    check("ONE proposal, kind email_write, carrying the one found address",
          [(p["kind"], [e["email"] for e in p["payload"]["emails"]]) for p in proposals],
          [("email_write", ["ada@acme.ai"])])

    def email_cells() -> dict:
        tab = read_sheet()[gtm_sheet.POCS]
        return {r["name"]: r.get("email") or "" for r in tab.rows}

    before = copy.deepcopy(GRIDS["Outreach PoCs"])
    stranger = FakeMessage(KUSHAL, "yes", reply_to=post_id, mid=9001)
    await bot._maybe_vote_on_proposal(stranger, "yes")
    print(f"   Kushal (not an approver): \"yes\" -> {stranger.replies[-1][:110]}")
    check("a non-approver's yes writes nothing", UPDATES, [])

    yes = FakeMessage(SID, "yes", reply_to=post_id, mid=9002)
    handled = await bot._maybe_vote_on_proposal(yes, "yes")
    print(f"   Sid: \"yes\" -> {yes.replies[-1]}")
    for line in [x for x in LOGS if x.startswith("[gtm]")]:
        print("   log: " + line[:230])
    check("the yes was taken as a vote on that proposal", handled, True)
    check("ONE cell was written: F3, the Email cell of Ada's row",
          UPDATES, [("Outreach PoCs", "F3", "ada@acme.ai")])
    after = GRIDS["Outreach PoCs"]
    changed = [(r, c) for r in range(len(after)) for c in range(len(after[r]))
               if after[r][c] != before[r][c]]
    check("...and nothing else on the sheet changed", changed, [(2, COL["Email id"])])
    check("Bo's row, where nothing was found, is untouched", email_cells()["Bo Chen"], "")
    con = sqlite3.connect(bot.db.path)
    logged = con.execute("SELECT tab, sheet_row, cell, old_value, new_value, trigger "
                         "FROM sheet_writes").fetchall()
    con.close()
    print(f"   sheet_writes: {logged}")
    check("it is logged in sheet_writes, with what was there before (nothing)",
          [(r[1], r[2], r[3], r[4], r[5]) for r in logged],
          [(3, "F3", "", "ada@acme.ai", "email_write")])

    again = FakeMessage(SID, "yes", reply_to=post_id, mid=9003)
    handled2 = await bot._maybe_vote_on_proposal(again, "yes")
    check("a second yes finds nothing open on that post, and writes nothing",
          (handled2, len(UPDATES)), (False, 1))
    proposal = bot.db.proposal(proposals[0]["proposal_key"])
    replay = FakeMessage(SID, "yes", reply_to=post_id, mid=9004)
    await bot._apply_email_write(replay, proposal, decided_by="Sid", why="replayed")
    print(f"   the same proposal applied again -> {replay.replies[-1]}")
    check("...and even applied again it does nothing: the cell is filled",
          (len(UPDATES), "already holds ada@acme.ai" in replay.replies[-1]), (1, True))

    print("\n   THE GUARD — what else can reach the restricted band")
    direct = gtm_sheet.SHEETS.write_cells(row=4, values={"email": "x@borealis.com"},
                                          expect_company="Borealis")
    check("the ordinary write path still refuses the Email column",
          (direct["ok"], "restricted band" in str(direct["refused"])), (False, True))
    other = gtm_sheet.SHEETS.write_cells(row=4, values={"designation": "CEO"},
                                         expect_company="Borealis")
    check("...and every other column in A:I", other["ok"], False)
    config.EMAIL_WRITE_ALLOWED = False
    off = gtm_sheet.SHEETS.write_email(row=4, email="bo@borealis.com",
                                       expect_company="Borealis", expect_name="Bo Chen")
    config.EMAIL_WRITE_ALLOWED = True
    check("with EMAIL_WRITE_ALLOWED=false the email write is refused too",
          (off["ok"], "EMAIL_WRITE_ALLOWED=false" in off["error"]), (False, True))
    wrong = gtm_sheet.SHEETS.write_email(row=4, email="bo@borealis.com",
                                         expect_company="Borealis", expect_name="Ada")
    check("a row that is no longer that person is refused",
          (wrong["ok"], "the sheet has changed under me" in wrong["error"]), (False, True))
    check("none of those wrote anything", len(UPDATES), 1)

    undo = FakeMessage(VAISHNAVI, "undo", mid=9005)
    await bot._undo_last_sheet_write(undo)
    print(f"   Vaishnavi: \"undo\" -> {undo.replies[-1]}")
    check("undo empties that one cell again",
          (UPDATES[-1], email_cells()["Ada Lovelace"]),
          (("Outreach PoCs", "F3", ""), ""))

    # ---- the counters, across the fortnight ----
    print("\n   THE COUNTERS — the same sheet on Thursday, and the next Tuesday and "
          "Thursday")
    key_bo = "borealis|bo chen"
    week = "%d-W%02d" % TUE.isocalendar()[:2]
    print(f"   after Tuesday: week {week} companies = "
          f"{bot.db.prospect_week_companies(week)}; times named = "
          f"{bot.db.prospect_repeats()}")
    check("Tuesday's post opened this week's two companies",
          bot.db.prospect_week_companies(week), ["Acme AI", "Borealis"])
    check("...and counted each contact it named once",
          sorted(bot.db.prospect_repeats().values()), [1, 1, 1])
    searched = len(SEARCHES)
    listed: list = []
    for day in (THU, TUE + timedelta(days=7), THU + timedelta(days=7)):
        run = await test_path(day, bot=bot, label=f"test day {day:%a %d %b}")
        body = "\n".join(run["posts"].get("R5") or [])
        listed.append((day.strftime("%a %d %b"), bot.db.prospect_repeats().get(key_bo),
                       "Skip them?" in body, "Dee Evans" in body))
        print(f"     Bo Chen named {bot.db.prospect_repeats().get(key_bo)} time(s); "
              + next((l for l in body.splitlines() if "Bo Chen" in l), "(not listed)"))
    check("Thursday stays with the week's two companies — Cinder still waits",
          listed[0][3], False)
    check("the count climbs one a post: 2, 3, 4", [n for _d, n, _s, _c in listed], [2, 3, 4])
    check("at 3 listings with nothing changed the next post asks \"Skip them?\"",
          [s for _d, _n, s, _c in listed], [False, False, True])
    check("the email lookups were cached: no search after Tuesday's two",
          len(SEARCHES), searched)


# -- (v) the Wednesday: R3 -----------------------------------------------------


def wednesday_sheet() -> None:
    def on(n):
        return cell(WED + timedelta(days=n))

    set_sheet(events=[
        ["Voice AI Forum", on(10), "", "Yes", "https://voiceaiforum.example", "Bengaluru"],
        ["Data Summit", on(6), on(2), "", "https://datasummit.example/register", "Online"],
        ["Old Expo", on(-3), "", "", "", "Delhi"],
        ["Closed Conf", on(12), on(-2), "No", "https://closedconf.example", "Mumbai"],
        ["Far Away Con", on(40), on(30), "", "", "London"],
    ])


async def the_wednesday() -> None:
    say(f"(v) A WEDNESDAY — {WED:%A %d %b %Y}: R3 and the offer")
    RULES.clear()
    RULES.add("R3")
    ACTIVE[0] = True
    print("   the tab: Voice AI Forum (registered, in 10 days), Data Summit (next "
          "Tuesday, register by Friday), Old Expo (3 days ago), Closed Conf "
          "(registration closed Monday), Far Away Con (40 days out)")
    live, test, sim = await three_ways(WED, wednesday_sheet)
    print("\n   R3 — AI EVENTS, as posted by the live sweep:")
    show(live, "R3")
    for line in [x for x in live["logs"] if x.startswith("[R3]")]:
        print("   log: " + line)
    body = "\n".join(live["posts"].get("R3") or [])
    bullets = [l for l in body.splitlines() if l.startswith("• ")]
    check("only the right two show, the nearer date first",
          bullets,
          ["• Register for Data Summit by Fri 25 Sep (event on Tue 29 Sep) "
           "[datasummit.example](<https://datasummit.example/register>)",
           "• You're registered for Voice AI Forum on Sat 3 Oct "
           "[voiceaiforum.example](<https://voiceaiforum.example>)"])
    check("the past event, the closed one and the far one are not mentioned",
          [n for n in ("Old Expo", "Closed Conf", "Far Away Con") if n in body], [])
    check("...and the log says why two of them were skipped",
          (any("Old Expo" in x and "passed" in x for x in live["logs"]),
           any("Closed Conf" in x and "registration closed" in x for x in live["logs"])),
          (True, True))
    check("an event next Tuesday is in THIS Wednesday's post", "Data Summit" in body, True)
    check("the post ends on the offer — Monday, since nothing falls before it",
          body.splitlines()[-1], "Want me to remind you again on Monday?")
    check("posted as rendered, never composed", "echo " in body, False)
    same("R3", live, test, sim)

    # ---- the yes ----
    wednesday_sheet()
    run = await test_path(WED, label=f"THE YES — \"make it {dl.iso(WED)}\", then a reply")
    bot = run["bot"]
    post_id = run["raw"][0]["first_id"]
    proposals = bot.db.open_proposals_for_message(str(post_id))
    check("one offer is open on the post: events_remind, for Monday",
          [(p["kind"], p["payload"]["on"]) for p in proposals],
          [("events_remind", dl.iso(MON + timedelta(days=7)))])
    yes = FakeMessage(VAISHNAVI, "yes please", reply_to=post_id, mid=9100)
    yes_sid = FakeMessage(SID, "yes", reply_to=post_id, mid=9101)
    await bot._maybe_vote_on_proposal(yes_sid, "yes")
    print(f"   Sid: \"yes\" -> {yes_sid.replies[-1]}")
    reminders = bot.db.list_scheduled_reminders()
    for r in reminders:
        print(f"   scheduled_reminders #{r['id']}: due {r['due_date']} {r['due_time']} "
              f"in channel {r['channel_id']} — {r['what']!r}"[:260])
    monday = MON + timedelta(days=7)
    check("\"yes\" created ONE reminder, for Monday at 14:00, in this channel",
          [(r["due_date"], r["due_time"], r["channel_id"]) for r in reminders],
          [(dl.iso(monday), "14:00", str(TEST_CHANNEL.id))])
    check("...listing those two events",
          [n for n in ("Data Summit", "Voice AI Forum") if n in reminders[0]["what"]],
          ["Data Summit", "Voice AI Forum"])
    await bot._maybe_vote_on_proposal(yes, "yes please")
    check("a second yes does not create a second reminder",
          len(bot.db.list_scheduled_reminders()), 1)

    begin(f"MONDAY {dl.iso(monday)} 14:00 — the reminder loop")
    config.SALES_TEST_MODE = True
    pretend(monday, 13, 59)
    early = await bot._fire_due_reminders(from_test=True)
    pretend(monday, 14, 0)
    fired = await bot._fire_due_reminders(from_test=True)
    again = await bot._fire_due_reminders(from_test=True)
    clock.clear_time_override(why="verify_s3")
    for body in POSTED:
        for line in body.splitlines():
            print("     | " + line)
    check("not a minute early, then once, then never again",
          (early, len(fired), again), ([], 1, []))
    check("it lists the events", all(n in POSTED[-1] for n in ("Data Summit",
                                                              "Voice AI Forum")), True)
    rows = bot.db.drip_sent_today(dl.iso(monday))
    check("it is not a drip message: nothing counted toward Monday's cap",
          (len(rows), drip.counted_today(rows)), (0, 0))

    # ---- a Wednesday with nothing close enough ----
    def empty_week():
        set_sheet(events=[["Far Away Con", cell(WED + timedelta(days=40)),
                           cell(WED + timedelta(days=30)), "", "", "London"]])

    empty_week()
    quiet = await live_path(WED)
    for line in [x for x in quiet["logs"] if "nothing to say" in x]:
        print("   log: " + line)
    rows = quiet["bot"].db.drip_sent_today(dl.iso(WED))
    check("a Wednesday with nothing inside the window posts nothing",
          quiet["posts"].get("R3"), [])
    check("...and its slot is recorded, uncounted, so the sweep does not come back "
          "to it", [(r["action_type"], r["counts_toward_cap"]) for r in rows],
          [("events", 0)])

    # ---- the next Wednesday: weekly, not fortnightly ----
    wednesday_sheet()
    nxt = await live_path(WED + timedelta(days=7))
    body = "\n".join(nxt["posts"].get("R3") or [])
    print("   the Wednesday after:")
    show(nxt, "R3")
    check("R3 runs the next Wednesday too — Data Summit has passed, the Forum is "
          "still listed, and the offer is \"tomorrow\" because it is on Saturday",
          ("Voice AI Forum" in body, "Data Summit" in body, body.splitlines()[-1]),
          (True, False, "Want me to remind you again tomorrow?"))


# -- (w) the wording -----------------------------------------------------------


def the_wording() -> None:
    say("(w) THE WORDING FOR THE SHEET — printed from the rules the bot loads")
    rules_mod.reload()
    for rid in ("R3", "R4", "R5", "R7", "R10"):      # R7's is now the one-line "replaced by rule 13" sentence
        text = rules_mod.sheet_wording_for(rid)
        print(f"\n   Bot Rules tab — rule {rid[1:]}, column \"What the Bot Shares / "
              "Checks\":")
        print("     " + text)
        check(f"{rid} has its own wording in bot_rules.yaml",
              bool(rules_mod.by_id(rid).sheet_wording), True)
    print()
    check("R3 runs on Wednesdays", rules_mod.by_id("R3").weekdays, (2,))
    strategy = persona.load_strategy()
    check("sales_strategy.md says the same for rules 3, 4, 5 and 10",
          [x for x in ("every Wednesday", "Team:", "no public email found",
                       "No closure support this week")
           if x not in strategy], [])
    # RULE 7 WAS REPLACED BY RULE 13 ON 7 OCT 2026: its overflow phrase ("+N more next Monday") left the strategy with it.
    check("...and for rule 7: replaced by rule 13, no longer describing the Monday list",
          ("Replaced by rule 13" in strategy, "+N more next Monday" in strategy), (True, False))
    check("...and no longer says \"every other Wednesday\"",
          "every other Wednesday" in strategy, False)


async def main() -> None:
    print(f"real date {dl.iso(dl.real_today_ist())} | the pretend week starts "
          f"{dl.iso(MON)} | cap {config.DAILY_MESSAGE_CAP} | window "
          f"{config.SALES_DRIP_START}-{config.SALES_DRIP_END} | EMAIL_WRITE_ALLOWED "
          f"{config.EMAIL_WRITE_ALLOWED} | restricted bands "
          f"{config.RESTRICTED_COLUMN_RANGES}")
    if want("i") or want("ii") or want("iii"):
        await the_monday()
    if want("iv"):
        await the_tuesday()
    if want("v"):
        await the_wednesday()
    if want("w"):
        the_wording()


try:
    asyncio.run(main())
finally:
    clock.forget()
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
sys.exit(1 if failures else 0)
