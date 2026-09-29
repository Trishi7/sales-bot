"""THE TEST DAY TALKS, STARTS CLEAN, AND CHECKS THE NEWS ONCE. Sends nothing.

    python verify_testday_talk.py

Drives "make it Monday" twice on the same pretend Monday against a throwaway
*_test.db and a fake test channel that records what it was sent, and when. The
rules queue and the plan are stubbed (the run's shape is under test, not the
spreadsheet); the news check is the REAL `_maybe_breaking_news` with only the
search call stubbed. It shows, with real output:

  (i)   a date with stale sends from an earlier run: the clearing line appears,
        the posts go out anyway, and the footer says "Sent: N"; another date's
        rows are untouched;
  (ii)  the footer is at most six lines;
  (iii) exactly one news check during the test day — a live tick fired in the
        middle of it and one right after are both held, and a live tick after
        the hold finds the slot already done (log lines and news_checks rows);
  (iv)  a story with a digest url, and one on NEWS_BLOCKED_DOMAINS, are dropped
        and logged;
  (v)   the progress lines appear within 5 s of the start, inside the typing
        indicator, and the plan is made from ONE read of the rules.
"""
import asyncio
import logging
import os
import re
import shutil
import sys
import tempfile
from time import monotonic

TMP = tempfile.mkdtemp(prefix="saley-testtalk-")
os.environ["DB_PATH"] = os.path.join(TMP, "sales_bot_test.db")

import config  # noqa: E402

config.DB_PATH = os.environ["DB_PATH"]
config.SALES_TEST_MODE = True
config.SALES_TEST_CHANNEL_ID = 4242
config.SALES_CHANNEL_IDS = [4242]
config.SALES_CHANNEL_ID_SET = {4242}
config.TEAM_ROSTER_IDS = [111]
config.SALES_APPROVER_IDS = [111]
config.SALES_DMS_ENABLED = False
config.TEST_POST_GAP_SECONDS = 0
config.WEB_SEARCH_DAILY_BUDGET = 60
config.NEWS_CHECK_TIMES = ["11:00", "12:00", "13:00", "15:00", "16:00", "17:00"]
config.NEWS_BLOCKED_DOMAINS = ["theneuron.ai", "aiagentsdirectory.com"]
config.digest_enabled = lambda: True

import clock  # noqa: E402
import deadlines as dl  # noqa: E402
import news  # noqa: E402
import websearch  # noqa: E402
from bot import SalesBot  # noqa: E402

websearch.enabled = lambda: True

failures = 0
POSTED: list = []          # (seconds since start, body)
LINES: list = []
T0 = [monotonic()]


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
for noisy in ("discord", "asyncio", "state", "httpx", "anthropic"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

TYPING = {"entered": 0, "exited": 0}


class FakeTyping:
    async def __aenter__(self):
        TYPING["entered"] += 1

    async def __aexit__(self, *a):
        TYPING["exited"] += 1


class FakeChannel:
    id = 4242
    name = "sales-test"

    async def send(self, body):
        POSTED.append((monotonic() - T0[0], str(body)))
        return type("M", (), {"id": len(POSTED)})()

    def typing(self):
        return FakeTyping()


class FakeAuthor:
    id = 111
    display_name = "Vaishnavi"
    name = "vaishnavi"
    bot = False


class FakeMessage:
    id = 999
    channel = FakeChannel()
    author = FakeAuthor()
    content = ""

    async def reply(self, body, mention_author=False):
        POSTED.append((monotonic() - T0[0], str(body)))
        return type("M", (), {"id": len(POSTED)})()


def _msg(slot, hh, mm, rule_id, owner):
    day = dl.today_ist()
    from datetime import datetime
    return {
        "slot": slot, "group_key": f"li_no_dm|{owner.lower()}{slot}", "type": "li_no_dm",
        "rule_id": rule_id, "rule_name": "", "owner": owner,
        "owner_key": owner.lower(), "companies": ["Acme"],
        "counts_toward_cap": True, "destination": "channel",
        "send_at": datetime(day.year, day.month, day.day, hh, mm, tzinfo=dl.IST),
        "send_at_hhmm": f"{hh:02d}:{mm:02d}", "actions": [], "why": "",
    }


SEARCH_TEXT = "\n".join([
    "STORY | evals | Everything that happened in AI today | a roundup | "
    "https://www.theneuron.ai/explainer-articles/everything-today | 5",
    "STORY | evals | Weekly agent digest | a digest | "
    "https://example.com/ai-daily-digest/2026-10-05 | 5",
    "STORY | evals | Lab ships eval suite | a benchmark | "
    "https://lab.example.org/blog/eval-suite | 3",
])


async def main():
    bot = SalesBot()
    bot.llm = None

    QUEUE_READS = [0]
    PLAN_GOT_QUEUE = []

    async def fake_queue(*, today):
        QUEUE_READS[0] += 1
        await asyncio.sleep(1.5)       # a slow sheet read, so "talks while it works" shows
        return {"actions": [], "rules_run": [
            {"id": "R12", "name": "Sales packages", "ran": False,
             "why": "does not run on Monday (runs Thursday)"},
            {"id": "R4", "name": "Deliverables checklist", "ran": True, "items": 0},
            {"id": "R6", "name": "LinkedIn connected, no DM", "ran": True, "items": 2},
        ]}

    async def fake_plan(*, today, already, queue=None):
        PLAN_GOT_QUEUE.append(queue is not None)
        return {
            "messages": [_msg(1, 14, 0, "R6", "Vaishnavi"),
                         _msg(2, 15, 30, "R6", "Sid")],
            "rolled": [{"rule_id": "R11", "owner": "Sid", "actions": [1, 2]}],
            "held": [],
        }

    LIVE_TICK_RESULTS = []

    async def fake_send(channel, message, *, marker, channel_id):
        # THE SAME SLOT GUARD AS THE REAL SEND: a slot already in drip_sends
        # does not go out. This is what the stale rows used to block.
        ok = await asyncio.to_thread(lambda: bot.db.record_drip_send(
            on_date=marker, slot=message["slot"], group_key=message["group_key"],
            action_type=message["type"], owner_label=message["owner"]))
        if not ok:
            raise RuntimeError(f"slot {message['slot']} already taken")
        POSTED.append((monotonic() - T0[0], f"<post slot {message['slot']}>"))
        # A LIVE SWEEP TICK LANDS MID-DAY, following the pretend clock.
        LIVE_TICK_RESULTS.append(await bot._maybe_breaking_news())

    async def fake_search(prompt, **kw):
        fake_search.prompts.append(prompt)
        return {"ok": True, "text": SEARCH_TEXT, "searches": 1}
    fake_search.prompts = []

    bot._run_next_actions = fake_queue
    bot._plan_drip = fake_plan
    bot._send_drip_message = fake_send
    bot._one_search = fake_search
    bot._sweep_proposals = lambda **kw: asyncio.sleep(0)

    # -- a first run of the pretend Monday leaves its sends behind ----------
    import simulation
    monday = dl.iso(simulation.parse_test_command("make it Monday")["date"])
    other = "2026-09-01"
    for slot in (1, 2):
        bot.db.record_drip_send(on_date=monday, slot=slot, group_key=f"old{slot}",
                                action_type="li_no_dm")
    bot.db.record_drip_send(on_date=other, slot=1, group_key="keep", action_type="x")
    bot.db.claim_news_check(monday, "13:00", ran_at="earlier")
    bot.db.claim_news_check(other, "13:00", ran_at="earlier")
    print(f"seeded: 2 stale sends + 1 news check on {monday}; 1 send + 1 check on {other}")

    # -- "make it Monday" --------------------------------------------------
    print('\n"make it Monday" on a date with stale sends')
    T0[0] = monotonic()
    handled = await bot._handle_test_command(FakeMessage(), "make it Monday")
    check("handled", handled)
    check("the pretend Monday is the seeded one", dl.iso(dl.today_ist()), monday)

    print("\nwhat the tester saw (seconds since the command)")
    for t, body in POSTED:
        for i, sub in enumerate(body.splitlines()):
            print(f"   {t:5.1f}s | {sub}" if i == 0 else f"          | {sub}")

    bodies = [b for _, b in POSTED]
    text = "\n".join(bodies)

    print("\n(i) stale sends")
    check("the clearing line appears",
          f"2 posts were already recorded for {dl.today_ist().strftime('%A %d %b')} "
          "from an earlier run — clearing them so today starts clean." in text)
    check("both posts went out", sum(1 for b in bodies if b.startswith("<post slot")), 2)
    check("the footer says Sent: 2", "• Sent: 2" in text)
    check("the other date's send is untouched",
          [r["group_key"] for r in bot.db.drip_sent_today(other)], ["keep"])
    check("the other date's news check is untouched",
          bot.db.news_check_done(other, "13:00"), True)
    cleared = [l for l in LINES if "cleared stale state" in l]
    print("   log: " + (cleared[0] if cleared else "(none)"))
    check("...and it was logged", len(cleared), 1)

    print("\n(ii) the footer")
    footer = next((b for b in bodies if "— done" in b), "")
    check("at most six lines", 0 < len(footer.splitlines()) <= 6)
    check("no rule codes", re.findall(r"\bR\d{1,2}\b", footer), [])
    check("every bullet at most 15 words",
          all(len(l.split()) - 1 <= 15 for l in footer.splitlines() if l.startswith("•")))

    print("\n(iii) one news check")
    # A LIVE TICK RIGHT AFTER — inside the 5-minute hold.
    after = await bot._maybe_breaking_news()
    check("the live ticks during the test day were held",
          LIVE_TICK_RESULTS, [None, None])
    check("the live tick right after was held too", after, None)
    # AFTER THE HOLD: the live check at the same pretend hour finds it done.
    bot._news_hold_until = 0.0
    late = await bot._maybe_breaking_news()
    check("after the hold, the live check finds the hour already done", late, None)
    with bot.db.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT slot_hhmm, ran_at FROM news_checks WHERE on_date = ?", (monday,))]
    print("   news_checks for", monday, "->", rows)
    ran = [l for l in LINES if l.startswith(f"[news-check] {monday} ")]
    for l in ran:
        print("   log:", l)
    check("exactly one news check ran (log)", len(ran), 1)
    check("exactly one news_checks row", len(rows), 1)
    live_slot = news.latest_slot(dl.now_ist())
    check("recorded under the live slot key", rows[0]["slot_hhmm"] if rows else "",
          live_slot)
    check("exactly one search was spent", len(fake_search.prompts), 1)
    check("the prompt asks for the primary report",
          "must be the PRIMARY report" in (fake_search.prompts or [""])[0])

    print("\n(iv) digest links")
    drops = [l for l in LINES if "dropped a story with a digest link" in l]
    for l in drops:
        print("   log:", l)
    check("the blocked-domain story was dropped",
          any("theneuron.ai is on NEWS_BLOCKED_DOMAINS" in l for l in drops))
    check("the digest-path story was dropped",
          any("digest page" in l and "ai-daily-digest" in l for l in drops))
    kept = news.parse_stories(SEARCH_TEXT)
    check("the primary report survives", [s["headline"] for s in kept],
          ["Lab ships eval suite"])

    print("\n(v) it talks while it works")
    first = POSTED[0] if POSTED else (99, "")
    check("the opening + working line lands within 5 s",
          first[0] < 5 and "Working through the day" in first[1])
    progress = [(t, b) for t, b in POSTED
                if b.startswith(("[TEST] Rules read", "[TEST] Plan made",
                                 "[TEST] News check done"))
                or "Working through the day" in b]
    check("four progress lines, no more", len(progress), 4)
    check("the first stage line lands within 5 s",
          len(progress) > 1 and progress[1][0] < 5)
    check("the whole run sat inside the typing indicator",
          (TYPING["entered"], TYPING["exited"]), (1, 1))
    check("the rules were read ONCE", QUEUE_READS[0], 1)
    check("...and the plan was made from that read", PLAN_GOT_QUEUE, [True])
    stages = [l for l in LINES if l.startswith("[test-day]") and " took " in l]
    for l in stages:
        print("   log:", l)
    check("each stage's seconds were logged", len(stages) >= 4)


try:
    asyncio.run(main())
finally:
    config.SALES_TEST_MODE = False
    clock.forget()
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
sys.exit(1 if failures else 0)
