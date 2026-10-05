"""A TEST RUN POSTS ONLY THE MESSAGES, TAGGED [TEST], FOR NO EXTRA CREDITS.

    python verify_test_output.py

Nothing reaches Discord, the sheet or the API. The REAL test day
(`_handle_test_command` -> `_live_test_day`), the REAL simulation
(`_handle_simulation` -> the same `_live_test_day` on a sandbox), the REAL
planner (`_plan_drip` -> `drip.plan`), the REAL research cache, the REAL
composer check (`llm.proactive_message`, Anthropic client faked), the REAL
sender (`_send_drip_message`), the REAL reminders, approval sweep and hourly
news check run against a throwaway *_test.db and a recording channel. Stubbed:
the rules queue (the sheet), the leave read, the channel-history evidence
search, the uncached web search and the news search — the network edges.

  (i)    "make it Monday" — the channel shows ONLY messages, each starting
         with [TEST], and nothing before or after them;
  (ii)   "simulate monday" on the same date — the same messages, mentions
         stripped;
  (iii)  a message with 3+ facts renders as points in both;
  (iv)   no rule code (R4, R5 ...) appears in either;
  (v)    a quiet day posts nothing, and "why was it quiet" explains it;
  (vi)   "make it Monday" twice — the second run logs searches=0;
  (vii)  during a test day the live loop logs the hold and sends nothing;
  (viii) a reminder, a breaking post and the approvals sweep fired during a
         test carry [TEST].
"""
import asyncio
import logging
import os
import re
import shutil
import sys
import tempfile
from datetime import date, timedelta
from types import SimpleNamespace

TMP = tempfile.mkdtemp(prefix="saley-testout-")
os.environ["DB_PATH"] = os.path.join(TMP, "sales_bot_test.db")

import config  # noqa: E402

# THIS SCRIPT CHECKS THE SERVER-SIDE SEARCH PATH (SEARCH_BACKEND=anthropic), with
# the search itself stubbed. The default path — search outside the model, the
# feeds, the light model — is verify_search_backend.py's to check.
config.SEARCH_BACKEND = "anthropic"

config.DB_PATH = os.environ["DB_PATH"]
config.SALES_TEST_MODE = True         # the pretend clock refuses without it
config.SALES_TEST_CHANNEL_ID = 4242
config.SALES_CHANNEL_IDS = [4242]
config.SALES_CHANNEL_ID_SET = {4242}
config.TEAM_ROSTER_IDS = [111, 222]
config.SALES_APPROVER_IDS = [111]
config.ROSTER_DISPLAY_NAMES = {"111": "Kushal", "222": "Vaishnavi"}
config.SALES_DMS_ENABLED = False
config.SIMULATION_REAL_MENTIONS = False
config.TEST_POST_GAP_SECONDS = 0
config.SIMULATION_FAST_GAP_SECONDS = 0
config.WEB_SEARCH_DAILY_BUDGET = 60
config.NEWS_BREAKING_MIN_IMPORTANCE = 4
config.NEWS_BREAKING_MAX_PER_DAY = 5
config.DRIP_LLM_COMPOSE = True
config.EVENTS_ENABLED = False
config.WEEKLY_FUNNEL_ENABLED = False
config.digest_enabled = lambda: True

import clock  # noqa: E402
import deadlines as dl  # noqa: E402
import leave  # noqa: E402
import llm as llm_mod  # noqa: E402
import nextaction  # noqa: E402
import simulation  # noqa: E402
import usage  # noqa: E402
import websearch  # noqa: E402
from bot import SalesBot  # noqa: E402

websearch.enabled = lambda: True

import tone  # noqa: E402

# THE FIRST VARIANT OF EVERY LINE, so an exact sentence can be asserted.
tone.pin(0)

failures = 0
POSTED: list = []
LINES: list = []
PREFIX = config.SIMULATION_PREFIX


def check(name, got, want=True):
    global failures
    ok = (got == want)
    failures += 0 if ok else 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}"
          + ("" if ok else f": got {got!r}, want {want!r}"))


class Capture(logging.Handler):
    def emit(self, record):
        LINES.append(record.getMessage())


root = logging.getLogger()
root.setLevel(logging.INFO)
for h in list(root.handlers):
    root.removeHandler(h)
root.addHandler(Capture())
for noisy in ("discord", "asyncio", "httpx", "anthropic"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


class FakeTyping:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeChannel:
    id = 4242
    name = "sales-test"
    guild = SimpleNamespace(id=1)
    on_send = None

    async def send(self, body):
        POSTED.append(str(body))
        if FakeChannel.on_send is not None:
            await FakeChannel.on_send()
        return SimpleNamespace(id=len(POSTED), jump_url="https://discord/x")

    def typing(self):
        return FakeTyping()


CHANNEL = FakeChannel()


class FakeAuthor:
    id = 111
    display_name = "Kushal"
    name = "kushal"
    bot = False


class FakeMessage:
    id = 999
    channel = CHANNEL
    author = FakeAuthor()
    content = ""

    async def reply(self, body, mention_author=False):
        POSTED.append("REPLY: " + str(body))
        return SimpleNamespace(id=len(POSTED), jump_url="https://discord/x")


# -- the model: echoes the numbered lines it was handed, and slips a rule code in
class FakeMessages:
    def create(self, **kw):
        prompt = str(kw.get("messages", [{}])[0].get("content", ""))
        lines = [l.strip() for l in prompt.splitlines() if re.match(r"^\s*\d+\. ", l)]
        seen, keep = set(), []
        for l in lines:
            if l not in seen:
                seen.add(l)
                keep.append(l)
        reply = ("Kushal — three prospects from R5 still waiting on a first touch.\n"
                 "Prospects to reach —\n" + "\n".join(keep)
                 + "\nShout if any of these have moved.")
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=reply)],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=1, output_tokens=1))


def action(company, poc, owner="Kushal"):
    today = dl.today_ist()
    return {
        "type": nextaction.R_PROSPECTS, "rule": nextaction.R_PROSPECTS, "rule_id": "R5",
        "rule_name": nextaction.TYPE_LABELS[nextaction.R_PROSPECTS],
        "label": nextaction.TYPE_LABELS[nextaction.R_PROSPECTS], "owner": owner,
        "due_date": today, "due_iso": dl.iso(today), "priority": 2,
        "priority_label": nextaction.BAND_LABELS[2], "overdue_days": 0,
        "company": company, "poc": poc, "poc_designation": "", "sheet_row": 2,
        "row_key": f"{company.lower()}|{poc.lower()}",
        "contact_key": f"{company.lower()}|{poc.lower()}",
        "max_items_per_post": 5, "counts_toward_cap": True,
        "destination": "channel", "web_pending": True,
        "why": f"{company} has no first contact yet",
        "text": f"{company} has no first contact yet",
        "key": f"R5:{company.lower()}",
    }


SEARCH_TEXT = ("STORY | evals | Lab ships open eval suite | a benchmark release | "
               "https://lab.example.org/blog/eval-suite | 5")


def show(title, bodies):
    print(f"   --- {title}: {len(bodies)} post(s) ---")
    for b in bodies:
        for i, sub in enumerate(b.splitlines()):
            print(("   | " if i == 0 else "   : ") + sub)


def cost_line(marker):
    got = [l for l in LINES if l.startswith(f"[test-cost] date={marker}")]
    return got[-1] if got else ""


async def main():
    bot = SalesBot()
    engine = llm_mod.LLM("sk-fake-not-used", config.MODEL)
    engine._client = SimpleNamespace(messages=FakeMessages())
    bot.llm = engine

    SEARCHES = {"research": 0, "news": 0}

    async def fake_queue(*, today):
        if today.weekday() == 0:
            return {"actions": [action("Acme", "Asha"), action("Globex", "Gita"),
                                action("Initech", "Ivan")],
                    "rules_run": [
                        {"id": "R5", "name": "Prospects", "ran": True, "items": 3},
                        {"id": "R6", "name": "LinkedIn", "ran": True, "items": 0},
                        {"id": "R12", "name": "Sales packages", "ran": False,
                         "why": "does not run on Monday (runs Thursday)"}]}
        return {"actions": [], "rules_run": [
            {"id": "R6", "name": "LinkedIn", "ran": True, "items": 0},
            {"id": "R4", "name": "Deliverables", "ran": True, "items": 0},
            {"id": "R12", "name": "Sales packages", "ran": False,
             "why": f"does not run on {today.strftime('%A')} (runs Thursday)"}]}

    async def fake_research(items, *, today):
        # ONE BILLED SEARCH PER ITEM — what the real uncached pass would spend.
        for item in items:
            SEARCHES["research"] += 1
            usage.count("searches", 1)
            item["research"] = f"{item['company']} raised a seed round."
            item["sources"] = []
            item["web_pending"] = False
        return items

    async def fake_news_search(prompt, **kw):
        SEARCHES["news"] += 1
        usage.count("searches", 1)
        return {"ok": True, "text": SEARCH_TEXT, "searches": 1}

    async def nobody_away(*_a, **_k):
        return {}

    async def no_evidence(_m):
        return None

    bot._run_next_actions = fake_queue
    bot._research_uncached = fake_research
    bot._one_search = fake_news_search
    bot._convert_if_already_done = no_evidence
    leave.who_is_away = nobody_away

    monday = simulation.parse_test_command("make it Monday")["date"]
    mk = dl.iso(monday)

    # SEEDED: a reminder for Monday 10:00 and a proposal old enough to nudge.
    bot.db.add_scheduled_reminder(due_date=mk, due_time="10:00",
                                  what="send Acme the deck", requested_by="Kushal",
                                  asker_id="111", channel_id="4242", company="Acme")
    bot.db.open_proposal(
        proposal_key="p1", kind="status", tab="Outreach PoCs", sheet_row=5,
        row_key="globex|gita", company="Globex", poc="Gita", payload={},
        reply_text="set Globex to demo", trigger="reply",
        proposed_text="Globex → Demo", requested_by="Vaishnavi", channel_id="4242",
        message_id="m1", created_at=dl.iso(monday - timedelta(days=10)) + "T10:00:00")

    # (vii) THE LIVE LOOP, ticking on every post the test run makes.
    LIVE = {"drip": [], "news": [], "reminders": []}

    async def live_tick():
        before = len(POSTED)
        await bot._maybe_send_drip()
        LIVE["drip"].append(len(POSTED) - before)
        LIVE["news"].append(await bot._maybe_breaking_news())
        LIVE["reminders"].append(await bot._fire_due_reminders())

    # -- (i) "make it Monday" ------------------------------------------------
    print(f'(i) "make it Monday" ({mk})')
    FakeChannel.on_send = live_tick
    POSTED.clear()
    await bot._handle_test_command(FakeMessage(), "make it Monday")
    FakeChannel.on_send = None
    # THE ONE LINE THAT IS NOT A MESSAGE: the day, and the real date the
    # day name was counted from.
    confirm = [b for b in POSTED if "the real date is" in b]
    show("the confirmation", confirm)
    check("the day is confirmed once, naming the real date", len(confirm), 1)
    check("...and Monday is the REAL week's Monday",
          monday, simulation.this_week_day(0, from_day=dl.real_today_ist()))
    run1 = [b for b in POSTED if "the real date is" not in b]
    show("the channel", run1)
    check("every post starts with [TEST]", all(b.startswith(PREFIX + " ") for b in run1))
    check("no narration, clock line or footer",
          any(re.search(r"It's now|Working through|Rules read|Plan made|News check "
                        r"done|— done|Say \"next day\"|Simulating", b) for b in run1),
          False)
    drip_posts = [b for b in run1 if "Prospects to reach" in b]
    check("the drip message went out", len(drip_posts), 1)

    print("\n(iii) 3+ facts render as points")
    check("three numbered lines in the test-day message",
          sum(1 for l in (drip_posts or [""])[0].splitlines() if re.match(r"^\d+\. ", l)),
          3)

    print("\n(viii) reminders, breaking posts and the approvals sweep carry [TEST]")
    rem = [b for b in run1 if "you asked me to remind you" in b]
    brk = [b for b in run1 if "eval suite" in b.lower()]
    swp = [b for b in run1 if "Globex" in b and "Prospects to reach" not in b]
    check("the reminder fired and is tagged", len(rem) == 1 and rem[0].startswith(PREFIX))
    check("the breaking post went out and is tagged",
          len(brk) == 1 and brk[0].startswith(PREFIX))
    check("the approvals sweep went out and is tagged",
          len(swp) == 1 and swp[0].startswith(PREFIX))

    print("\n(vii) the live loop during the test day")
    holds = [l for l in LINES if l.startswith("[test-hold] live drip, news check and "
                                              "reminders held")]
    for l in holds:
        print("   log:", l)
    check("the live loop ticked during the run", len(LIVE["drip"]) >= 3)
    check("the live drip sent nothing", sum(LIVE["drip"]), 0)
    check("the live news check did not run", set(LIVE["news"]), {None})
    check("the live reminders fired nothing", LIVE["reminders"],
          [[]] * len(LIVE["reminders"]))
    check("the hold was logged once, not once per tick", len(holds), 1)

    first_cost = cost_line(mk)
    print("   log:", first_cost)

    # -- (vi) the same Monday again -----------------------------------------
    print('\n(vi) "make it Monday" again')
    before_search = dict(SEARCHES)
    POSTED.clear()
    await bot._handle_test_command(FakeMessage(), "make it Monday")
    run2 = [b for b in POSTED if "the real date is" not in b]
    show("the channel", run2)
    second_cost = cost_line(mk)
    print("   log:", second_cost)
    check("the second run logs searches=0", "searches=0" in second_cost)
    check("...and nothing was searched", SEARCHES, before_search)
    check("...with the research served from the cache", "cache_hits=3" in second_cost)
    check("the same drip message went out again",
          [b for b in run2 if "Prospects to reach" in b] == drip_posts)
    skipped = [l for l in LINES if "a forced check ran" in l]
    check("the news check was skipped silently (ran < 1 h ago)", len(skipped), 1)

    # -- (ii) "simulate monday" on the same date ----------------------------
    # SALES_TEST_MODE OFF for the simulation, so the tag seen here comes from
    # the test run itself (`_tag_test`), not from test mode.
    print('\n(ii) "simulate monday" on the same date (SALES_TEST_MODE off)')
    config.SALES_TEST_MODE = False
    POSTED.clear()
    await bot._handle_simulation(FakeMessage(), "simulate monday")
    sim = list(POSTED)
    show("the channel", sim)
    sim_cost = cost_line(mk)
    print("   log:", sim_cost)
    sim_drip = [b for b in sim if "Prospects to reach" in b]
    check("only messages, each tagged [TEST]",
          bool(sim) and all(b.startswith(PREFIX + " ") for b in sim))
    check("no opening line, header or footer",
          any(re.search(r"Simulating|— done|I ran one hourly", b) for b in sim), False)
    check("the same message as the test day, mentions stripped",
          sim_drip == [re.sub(r"<@!?111>", "@Kushal", b) for b in drip_posts])
    check("no raw mention anywhere", any(re.search(r"<@!?\d+>", b) for b in sim), False)
    check("3+ facts render as points in the simulation too",
          sum(1 for l in (sim_drip or [""])[0].splitlines() if re.match(r"^\d+\. ", l)),
          3)
    check("the simulation searched nothing (inherited cache)", "searches=0" in sim_cost)

    print("\n(iv) no rule codes")
    everything = run1 + run2 + sim
    codes = [m for b in everything for m in re.findall(r"\bR\d{1,2}\b", b)]
    check("no R-code in any test or simulated post", codes, [])

    # -- (v) a quiet day ------------------------------------------------------
    print('\n(v) a quiet day, then "why was it quiet"')
    config.SALES_TEST_MODE = True
    POSTED.clear()
    await bot._handle_test_command(FakeMessage(), "make it Wednesday")
    quiet = [b for b in POSTED if "eval suite" not in b.lower()
             and "the real date is" not in b]
    show("the channel", POSTED)
    check("the quiet day posted nothing of its own", quiet, [])
    POSTED.clear()
    await bot._handle_test_command(FakeMessage(), "why was it quiet?")
    show("the answer", POSTED)
    ans = POSTED[0] if POSTED else ""
    check("answered", ans.startswith("REPLY: "))
    check("says what looked and found nothing", "Looked, nothing due:" in ans)
    check("says what is not today's rule", "Not a Wednesday rule:" in ans)
    check("no rule codes in the answer", re.findall(r"\bR\d{1,2}\b", ans), [])


try:
    asyncio.run(main())
finally:
    clock.forget()
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
sys.exit(1 if failures else 0)
