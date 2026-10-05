"""ONE SENDER, THREE WAYS IN: a real day, a test day and a simulation say the same thing.

    python verify_parity.py                 the real sheet, then a fixture checklist
    python verify_parity.py --fixture-only  the fixture checklist alone (no sheet rows for R4)
    python verify_parity.py 2026-10-05      another Monday (default: this real week's Monday)

The same pretend date is lived three times, each on its own fresh copy of the
same database:

  REAL       `_maybe_send_drip` — the live sweep's own method — ticked once per
             planned slot, with SALES_TEST_MODE off. No "[TEST]" tag.
  TEST DAY   "make it <date>" through `_handle_test_command`.
  SIMULATION "simulate <date>" through `_handle_simulation` (its own sandbox
             copy of that database).

and the bodies are compared. They must be IDENTICAL apart from the "[TEST]"
prefix and how a mention is written (a simulation shows "@Name") — the same
messages, in the same order — and the three must have made THE SAME CAP
DECISIONS: who goes, which of them count against DAILY_MESSAGE_CAP, who rolls
to tomorrow and who is held.

NOTHING REACHES DISCORD. The bot is never logged in; every send lands in a
recording channel. The sheet is read and never written (SHEET_WRITES_ENABLED is
forced off). Stubbed, because they need Discord or spend search budget: the
leave read, the channel-history evidence search, the web research, the hourly
news check and the proposal sweep — stubbed identically for all three paths.

THE MODEL IS REPLACED BY AN ECHO, on purpose. A real model does not write the
same sentence twice, so "identical bodies" could never be asserted of it. The
echo writes a fixed sentence carrying a FINGERPRINT of everything it was given
— the whole system prompt (strategy, tone, the voice profile's note and its six
rotated examples, the recent openers) and the compose prompt. Two paths
produce the same composed body only if they handed the composer the same
bytes, which is the property being checked. It costs nothing.

  (ii)  R4 through all three paths: identical bodies, P1 only, each item its
        title, team, due date and link;
  (iii) a P2 row with a deadline this week is skipped, and logged, on every path;
  (vi)  one composed message through all three paths: identical bodies — and
        every other drip message of the day too;
  (vii) the Bot Rules tab wording for rule 4 and for the Global Rules rows
        "Voice" and "Daily cap", printed from the rules the bot loads;
  (viii) the cap, three ways: the same plan, the same counted posts, the same
        rolled and held groups — and `counted_today` agreeing with what the
        live day wrote to drip_sends.

`python verify_s1.py` is the companion: the same three paths on a fixture
Monday built to hit the cap (six countable groups, an R8 and an R9).
"""
import asyncio
import hashlib
import inspect
import logging
import os
import random
import re
import shutil
import sys
import tempfile
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

FIXTURE_ONLY = "--fixture-only" in sys.argv
ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]

TMP = tempfile.mkdtemp(prefix="saley-parity-")
SOURCE_DB = os.path.join(HERE, "sales_bot_test.db")
os.environ["STATE_DIR"] = os.path.join(TMP, "state")
os.environ["DB_PATH"] = os.path.join(TMP, "base_test.db")

import config  # noqa: E402

BASE_DB = os.environ["DB_PATH"]
for suffix in ("", "-wal", "-shm"):
    if os.path.exists(SOURCE_DB + suffix):
        shutil.copy2(SOURCE_DB + suffix, BASE_DB + suffix)

config.DB_PATH = BASE_DB
config.STATE_DIR = os.environ["STATE_DIR"]
config.SHEET_WRITES_ENABLED = False
config.TEST_POST_GAP_SECONDS = 0
config.SIMULATION_FAST_GAP_SECONDS = 0
config.SIMULATION_REAL_MENTIONS = False
config.DRIP_LLM_COMPOSE = True
config.digest_enabled = lambda: True      # the kill switch is not what is tested

import clock  # noqa: E402
import deadlines as dl  # noqa: E402
import drip  # noqa: E402
import gtm_sheet  # noqa: E402
import leave  # noqa: E402
import nextaction  # noqa: E402
import persona  # noqa: E402
import rules as rules_mod  # noqa: E402
import simulation  # noqa: E402
import tone  # noqa: E402
import voice  # noqa: E402
from db import DB  # noqa: E402

simulation.pace_seconds = lambda **_k: 0.0

failures = 0
POSTED: list = []
SENT: list = []           # (slot, type, [posts]) for the path being run
SKIPS: list = []          # "[R4] skipped ..." log lines for the path being run
PROMPTS: list = []        # (system sha, prompt sha) per compose call


def check(name, got, want=True):
    global failures
    ok = (got == want)
    failures += 0 if ok else 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}"
          + ("" if ok else f": got {got!r}, want {want!r}"))


class Capture(logging.Handler):
    def emit(self, record):
        try:
            line = record.getMessage()
        except Exception:
            return
        if line.startswith("[R4] skipped"):
            SKIPS.append(line)


root = logging.getLogger()
root.setLevel(logging.INFO)
for h in list(root.handlers):
    root.removeHandler(h)
root.addHandler(Capture())
for noisy in ("discord", "asyncio", "httpx", "httpcore", "anthropic", "googleapiclient",
              "google", "urllib3"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


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
    display_name = "verify_parity"
    name = "verify_parity"
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
            s_sha = hashlib.sha1(system.encode("utf-8")).hexdigest()[:10]
            p_sha = hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:10]
            PROMPTS.append((s_sha, p_sha, voice.WRAPPER in system))
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


def show(label, bodies):
    print(f"   {label}:")
    for b in bodies:
        for sub in str(b).splitlines():
            print("     | " + sub)


FIXTURE: list = []
USE_FIXTURE = [False]


def fixture_for(day: date) -> list:
    """A checklist around `day`'s week: two open P1s (one overdue), a P2 and a
    P3 due this week, a done P1, and a P1 due next week."""
    def cell(d):
        return f"{d.day}-{d.strftime('%b')}"

    def row(n, title, pri, status, due, team="", remarks="", link=""):
        return {"_row": n, "_extra": {}, "action_item": title, "priority": pri,
                "status": status, "deadline": cell(due), "dependency": team,
                "remarks": remarks, "link": link}

    return [
        row(2, "Pulse Product Overview Document", "P1", "In progress",
            day - timedelta(days=3), "Sales", "waiting on the pricing table",
            "https://docs.google.com/document/d/1AbCdEf/edit"),
        row(3, "MSA template", "P1", "", day + timedelta(days=3), "Legal"),
        row(4, "Case study: Hinglish STT", "P2", "", day + timedelta(days=2), "Sales"),
        row(5, "Dashboard SSO", "P3", "Not started", day - timedelta(days=10),
            "Engineering"),
        row(6, "NDA", "P1", "Done", day + timedelta(days=1), "Legal"),
        row(7, "Q1 plan", "P1", "", day + timedelta(days=9), "Sales"),
    ]


def new_bot():
    from bot import SalesBot

    bot = SalesBot()
    bot.llm._client = SimpleNamespace(messages=EchoMessages())

    async def nobody_away(*_a, **_k):
        return {}

    async def nothing(*_a, **_k):
        return None

    async def unresearched(message, *, today):
        return message

    real_rows = bot._rule_tab_rows

    async def rows(kind, for_rules):
        if USE_FIXTURE[0] and kind == gtm_sheet.DELIVERABLES:
            return list(FIXTURE)
        return await real_rows(kind, for_rules)

    real_send = bot._send_drip_message

    async def recording_send(channel, message, **kw):
        start = len(POSTED)
        await real_send(channel, message, **kw)
        SENT.append((message.get("slot"), message.get("type"), list(POSTED[start:])))

    leave.who_is_away = nobody_away
    bot._rule_tab_rows = rows
    bot._send_drip_message = recording_send
    bot._research_message = unresearched
    bot._convert_if_already_done = nothing
    bot._maybe_breaking_news = nothing
    bot._sweep_proposals = nothing
    bot._fire_due_reminders = nothing
    return bot


def fresh_db(name: str) -> str:
    """A fresh copy of the base database for one path, and the bot pointed at it."""
    path = os.path.join(TMP, f"{name}_test.db")
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)
        if os.path.exists(BASE_DB + suffix):
            shutil.copy2(BASE_DB + suffix, path + suffix)
    config.DB_PATH = path
    clock.forget()
    voice.invalidate()
    return path


def begin(label: str):
    POSTED.clear()
    SENT.clear()
    SKIPS.clear()
    PROMPTS.clear()
    # THE SAME RANDOM SEQUENCE for each path: which of a line's three wordings
    # a message gets is drawn at random, by design, and must not be the reason
    # two paths differ.
    tone.RNG = random.Random(20260928)
    voice._last_choice.clear()
    print(f"\n   ---- {label} ----")


def decisions(planned) -> dict:
    """What a plan decided about the cap: who goes (and whether each counts),
    who rolls and why, who is held. Compared across the three paths."""
    planned = planned or {}
    return {
        "go": [(m["type"], bool(m.get("counts_toward_cap", True)),
                bool(m.get("pinned")), m.get("send_at_hhmm"))
               for m in planned.get("messages") or []],
        "rolled": sorted((g["type"], g.get("rolled_why", ""))
                         for g in planned.get("rolled") or []),
        "held": sorted(g["type"] for g in planned.get("held") or []),
        "counted": planned.get("counted"), "cap": planned.get("cap"),
    }


def collect(planned=None) -> dict:
    return {"sent": [(slot, kind, [strip(p) for p in posts]) for slot, kind, posts in SENT],
            "raw": [(slot, kind, list(posts)) for slot, kind, posts in SENT],
            "skips": sorted(set(SKIPS)), "prompts": list(PROMPTS),
            "decisions": decisions(planned)}


async def real_path(day: date) -> dict:
    """The live sweep's own method, one tick per planned slot."""
    begin(f"REAL — _maybe_send_drip, SALES_TEST_MODE off, {dl.iso(day)}")
    path = fresh_db("real")
    bot = new_bot()
    bot.db = DB(path)
    config.SALES_TEST_MODE = True                  # only to be allowed to set the day
    clock.set_day(day, by="verify_parity")
    config.SALES_TEST_MODE = False
    # THE HOLD THAT KEEPS THE LIVE LOOP OFF A PRETEND DAY IS LIFTED: here the
    # live loop is the thing under test.
    bot._live_loop_held = lambda what: False
    channel_id = config.digest_channel_id()
    bot.get_channel = lambda cid: FakeChannel(cid)
    planned = await bot._plan_drip(today=day, already=[])
    times = [m["send_at"] for m in (planned or {}).get("messages") or []]
    step = timedelta(minutes=drip.min_gap_minutes() + 1)
    at = None
    for want in times + [None]:
        base = want or (at + step if at else datetime.combine(day, time(23, 0), dl.IST))
        at = base if at is None else max(base, at + step)
        at = min(at, datetime.combine(day, time(23, 58), dl.IST))
        clock.set_time_of_day(at.timetz().replace(tzinfo=None), by="verify_parity")
        await bot._maybe_send_drip()
    clock.clear_time_override(why="verify_parity")
    print(f"   {len(SENT)} drip message(s) into channel {channel_id} (recording); "
          f"tagged [TEST]: {sum(1 for p in POSTED if p.startswith('[TEST]'))}")
    got = collect(planned)
    got["rows"] = bot.db.drip_sent_today(dl.iso(day))
    return got


async def test_path(day: date, command: str) -> dict:
    begin(f"TEST DAY — \"{command}\"")
    path = fresh_db("testday")
    config.SALES_TEST_MODE = True
    bot = new_bot()
    bot.db = DB(path)
    await bot._handle_test_command(FakeMessage(), command)
    print(f"   resolved to {dl.iso(dl.today_ist())}; {len(SENT)} drip message(s); "
          f"tagged [TEST]: {sum(1 for _s, _k, ps in SENT for p in ps if p.startswith('[TEST]'))}")
    got = collect((bot._last_test_plan or {}).get("planned"))
    got["day"] = dl.today_ist()
    return got


async def sim_path(day: date, command: str) -> dict:
    begin(f"SIMULATION — \"{command}\"")
    path = fresh_db("sim")
    config.SALES_TEST_MODE = True
    bot = new_bot()
    bot.db = DB(path)
    await bot._handle_simulation(FakeMessage(), command)
    plan = (bot._last_test_plan or {})
    print(f"   resolved to {dl.iso(plan.get('date')) if plan.get('date') else '?'}; "
          f"{len(SENT)} drip message(s); the sandbox was a copy of {os.path.basename(path)}")
    got = collect(plan.get("planned"))
    got["day"] = plan.get("date")
    return got


def of_type(run: dict, kind: str) -> list:
    return [posts for _slot, k, posts in run["sent"] if k == kind]


def composed(run: dict) -> list:
    return [(slot, k, posts) for slot, k, posts in run["sent"]
            if any("echo " in p for p in posts)]


async def three_ways(day: date, label: str) -> None:
    print("\n" + "=" * 78)
    print(f"{label} — {day.strftime('%A %d %b %Y')}")
    print("=" * 78)
    monday_cmd = "make it " + dl.iso(day)
    sim_cmd = "simulate " + dl.iso(day)
    if day == simulation.this_week_day(0):
        monday_cmd, sim_cmd = "make it monday", "simulate monday"

    real = await real_path(day)
    test = await test_path(day, monday_cmd)
    sim = await sim_path(day, sim_cmd)
    check("\"" + monday_cmd + "\" and \"" + sim_cmd + "\" are the same date as the real run",
          (test.get("day"), sim.get("day")), (day, day))

    # ---- (ii) R4 -----------------------------------------------------------
    print("\n(ii) R4 — THE DELIVERABLES POST, THREE WAYS")
    r4 = {name: of_type(run, nextaction.R_DELIVERABLES)
          for name, run in (("real", real), ("test", test), ("sim", sim))}
    for name, run in (("real", real), ("test", test), ("sim", sim)):
        raw = [posts for _s, k, posts in run["raw"] if k == nextaction.R_DELIVERABLES]
        show(f"{name} (as posted)", [p for posts in raw for p in posts] or ["(no R4 post)"])
    check("R4: the three bodies are identical apart from [TEST] and mentions",
          r4["real"] == r4["test"] == r4["sim"])
    body = "\n".join(p for posts in r4["real"] for p in posts)
    if not body:
        print("   no open P1 deliverable is due this week or overdue on this sheet, so "
              "there is NO R4 message on any path (that is the rule).")
        check("R4: no post on any path", (r4["real"], r4["test"], r4["sim"]), ([], [], []))
    else:
        lines = body.splitlines()
        items = [i for i, l in enumerate(lines) if re.match(r"^\d+\. ", l)]
        check("R4: one R4 post", len(r4["real"]), 1)
        check("R4: every item is its title, then \"Team: ...\", then \"Due: ...\"",
              all(i + 2 < len(lines) and lines[i + 1].startswith("   Team: ")
                  and re.fullmatch(
                      r"   Due: \w{3} \d{1,2} \w{3}(?: · \d+ days? overdue)?",
                      lines[i + 2])
                  for i in items))
        check("R4: a link is a [Doc](<…>) line of its own, and no remarks are shown",
              ([l for l in lines if "http" in l and not l.startswith("   [Doc](<")],
               "waiting on" in body), ([], False))
        urls = re.findall(r"\(<(https?://[^>]+)>\)", body)
        check("R4: no link appears twice", len(urls), len(set(urls)))
        head = [l for l in lines if l.startswith("**")]
        check("R4: the heading, the opener and the close are there",
              (head[:1], bool(re.search(r"^\d+ (still )?open:|what's open", body, re.M)),
               lines[-1] in drip.DELIVERABLES_CLOSES),
              (["**This week's deliverables**"], True, True))
        check("R4: it was posted verbatim, never composed", "echo " in body, False)
        titles = [re.sub(r"^\d+\. ", "", lines[i]) for i in items]
        print(f"   the {len(titles)} item(s): {titles}")

    # ---- (iii) the skipped rows --------------------------------------------
    print("\n(iii) ROWS SKIPPED FOR THEIR PRIORITY")
    for line in real["skips"]:
        print("   log: " + line)
    check("the same rows are skipped, and logged, on all three paths",
          real["skips"] == test["skips"] == sim["skips"])
    skipped = [(l.split("'")[1], l.split("priority '")[1].split("'")[0])
               for l in real["skips"]]
    if USE_FIXTURE[0]:
        check("the P2 due THIS WEEK is skipped and logged with its priority",
              ("Case study: Hinglish STT", "P2") in skipped)
        check("the overdue P3 is skipped and logged too",
              ("Dashboard SSO", "P3") in skipped)
        check("neither skipped title is in the post",
              [t for t, _p in skipped if t in body], [])
    else:
        print(f"   {len(skipped)} row(s) on the real sheet skipped for priority")
        check("no skipped title is in the post", [t for t, _p in skipped if t in body], [])

    # ---- (vi) composed messages --------------------------------------------
    print("\n(vi) COMPOSED MESSAGES, THREE WAYS")
    cr, ct, cs = composed(real), composed(test), composed(sim)
    if not cr:
        print("   no message was handed to the composer on this day")
    else:
        slot, kind, _posts = cr[0]
        for name, got, run in (("real", cr, real), ("test", ct, test), ("sim", cs, sim)):
            raw = next((posts for s, k, posts in run["raw"] if (s, k) == (slot, kind)), [])
            show(f"{name} (as posted) — slot {slot}, {kind}", raw or ["(missing)"])
        check("one composed message: identical bodies apart from [TEST] and mentions",
              cr[0] == (ct[0] if ct else None) == (cs[0] if cs else None))
        check("...and it was composed with the voice profile in its prompt",
              all(p[2] for p in real["prompts"]) and bool(real["prompts"]))
    check(f"every composed message of the day ({len(cr)}) is identical on all three",
          cr == ct == cs)
    check("the composer was handed the same bytes on all three (system + prompt)",
          [p[:2] for p in real["prompts"]] == [p[:2] for p in test["prompts"]]
          == [p[:2] for p in sim["prompts"]])
    check(f"every drip message of the day ({len(real['sent'])}) is identical on all three",
          real["sent"] == test["sent"] == sim["sent"])
    if not (real["sent"] == test["sent"] == sim["sent"]):
        for name, run in (("real", real), ("test", test), ("sim", sim)):
            print(f"   {name}: " + ", ".join(f"slot {s} {k}" for s, k, _p in run["sent"]))
    # ---- (viii) the cap ----------------------------------------------------
    print("\n(viii) THE CAP, THREE WAYS")
    d = real["decisions"]
    print(f"   cap {d['cap']} counted post(s); the real day planned {len(d['go'])} "
          f"message(s), {sum(1 for g in d['go'] if g[1])} counted, "
          f"{sum(1 for g in d['go'] if not g[1])} outside the cap; "
          f"{len(d['rolled'])} rolled, {len(d['held'])} held")
    for kind, counted, pinned, hhmm in d["go"]:
        print(f"     {hhmm}  {kind:<22} {'counted' if counted else 'outside the cap'}"
              f"{', fixed time' if pinned else ''}")
    for kind, why in d["rolled"]:
        print(f"     rolls  {kind} ({why})")
    check("the same cap decisions on all three: who goes, who counts, who rolls, "
          "who is held", real["decisions"] == test["decisions"] == sim["decisions"])
    if not (real["decisions"] == test["decisions"] == sim["decisions"]):
        for name, run in (("real", real), ("test", test), ("sim", sim)):
            print(f"   {name}: {run['decisions']}")
    check("the same messages in the same order (by type)",
          [k for _s, k, _p in real["sent"]] == [k for _s, k, _p in test["sent"]]
          == [k for _s, k, _p in sim["sent"]])
    check("the order sent is the order planned",
          [k for _s, k, _p in real["sent"]], [g[0] for g in d["go"]])
    rows = real.get("rows") or []
    check("every row the real day wrote carries counts_toward_cap",
          [r["action_type"] for r in rows if r.get("counts_toward_cap") is None], [])
    check("counted_today(drip_sends) is what the plan counted, and within the cap",
          (drip.counted_today(rows), drip.counted_today(rows) <= int(d["cap"] or 0)),
          (d["counted"], True))
    check("no meeting-prep or meeting-follow-up row was counted",
          [r["action_type"] for r in rows
           if r["action_type"] in drip.NEVER_COUNTED and r.get("counts_toward_cap")], [])

    check("the real run carries no [TEST] tag; the other two tag every message",
          (any(p.startswith("[TEST]") for _s, _k, ps in real["raw"] for p in ps),
           all(p.startswith("[TEST]") for _s, _k, ps in test["raw"] for p in ps),
           all(p.startswith("[TEST]") for _s, _k, ps in sim["raw"] for p in ps)),
          (False, True, True))


def one_code_path() -> None:
    print("\nONE SENDER, ONE SELECTION, ONE RENDERER (read off the source)")
    from bot import SalesBot

    def src(fn):
        return inspect.getsource(fn)

    check("the real drip sends through _send_drip_message",
          "_send_drip_message(" in src(SalesBot._maybe_send_drip))
    check("the test day and the simulation share _live_test_day",
          ("_live_test_day(" in src(SalesBot._run_test_day),
           "_live_test_day(" in src(SalesBot._run_simulation)), (True, True))
    check("...which sends through _send_drip_message",
          "_send_drip_message(" in src(SalesBot._live_test_day))
    here = os.path.dirname(os.path.abspath(__file__))
    callers = []
    for name in sorted(os.listdir(here)):
        if not name.endswith(".py") or name.startswith("verify_"):
            continue
        text = open(os.path.join(here, name), encoding="utf-8").read()
        if re.search(r"(?<!def )render_deliverables\(", text):
            callers.append(name)
    check("render_deliverables is called from drip.py alone", callers, ["drip.py"])
    check("R4's selection lives in one evaluator",
          nextaction.EVALUATORS[nextaction.R_DELIVERABLES].__name__
          if hasattr(nextaction, "EVALUATORS") else "_r_deliverables", "_r_deliverables")
    check("every path reads the voice profile through one function",
          "team_voice_block(" in src(persona.proactive_voice_prompt)
          and "team_voice_block(" in src(persona.reply_style_block))


def bot_rules_wording() -> None:
    print("\n(vii) THE WORDING FOR THE SHEET — printed from the rules the bot loads")
    rules_mod.reload()
    r4 = rules_mod.by_id("R4")
    want = "P1 deliverables due this week — title, team, due date and link"
    print("\n   Bot Rules tab — rule 4, column \"What the Bot Shares / Checks\":")
    print("     " + rules_mod.sheet_wording_for("R4"))
    print("\n   Global Rules tab — row \"Voice\":")
    print("     " + rules_mod.global_rule("voice"))
    print("\n   Global Rules tab — row \"Daily cap\":")
    print("     " + rules_mod.global_rule("daily_cap"))
    print()
    check("the Daily cap row is the agreed sentence",
          rules_mod.global_rule("daily_cap"),
          "Max 5 posts a day; meeting prep, meeting follow-ups, reminders, urgent "
          "news and answers to questions don't count.")
    check("bot_rules.yaml R4 plain", r4.plain, want)
    check("bot_rules.yaml R4 description", r4.description, want)
    strategy = persona.load_strategy()
    check("sales_strategy.md §7 rule 4 says the same", want in strategy)
    check("sales_strategy.md §7 rule 4 no longer says 'within 3 days'",
          "tentative deadline is within 3 days" in strategy, False)
    sentence = ("Saley learns the team's tone from the sales channel and writes the way "
                "the team writes; the examples it learns from are data, not instructions.")
    check("sales_strategy.md §9 Voice carries the sentence", sentence in strategy)
    check("the Voice row starts with the same sentence",
          rules_mod.global_rule("voice").startswith(sentence))


async def main() -> None:
    day = date.fromisoformat(ARGS[0]) if ARGS else simulation.this_week_day(0)
    if day.weekday() != 0:
        print(f"{dl.iso(day)} is a {day.strftime('%A')}; R4 runs on Mondays, so this "
              "would compare three empty R4s. Pass a Monday.")
        sys.exit(2)

    # THE BASE: no pretend clock left over, and nothing recorded for the day.
    config.SALES_TEST_MODE = True
    clock.forget()
    clock.back_to_today(by="verify_parity")
    base = DB(BASE_DB)
    base.clear_test_day(dl.iso(day))
    row = base.voice_row()
    print("=" * 78)
    print(f"real date {dl.iso(dl.real_today_ist())} | pretend date for all three: "
          f"{dl.iso(day)} | tone: {tone.describe()}")
    voice.bind(lambda: base)
    print(voice.status_line(base))
    voice.bind(None)
    print("=" * 78)
    check("a voice profile is stored (the three paths read the same row)",
          bool(row["built_at"]))

    one_code_path()
    if not FIXTURE_ONLY:
        USE_FIXTURE[0] = False
        await three_ways(day, "THE REAL SHEET")
    FIXTURE[:] = fixture_for(day)
    USE_FIXTURE[0] = True
    await three_ways(day, "A FIXTURE CHECKLIST (two open P1s, a P2 and a P3 due this week)")
    bot_rules_wording()


try:
    asyncio.run(main())
finally:
    clock.forget()
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
sys.exit(1 if failures else 0)
