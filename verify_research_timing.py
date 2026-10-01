"""RESEARCH RUNS AT SEND TIME, driven through real sweep ticks. Sends nothing.

    python verify_research_timing.py
    python verify_research_timing.py --report-only   # measure, never fail

A whole working day of `_sweep_once` ticks, every
COS_FOLLOWUP_CHECK_INTERVAL_MINUTES, against a throwaway *_test.db, a pretend
clock, a stubbed queue (R1, R2, R6 web-pending; R7 not) and a stubbed
`llm.web_research` that bills one search per call. The send itself is stubbed
to claim the slot and log what the message carried. It asserts:

  (i)   a tick with nothing due makes ZERO `[websearch]` log lines;
  (ii)  the tick that sends slot 1 researches R1 once, immediately before the
        send, and researches nothing else in the plan;
  (iii) a restart between the research and the send (the process dying while
        the message is composed) does not search again — the cache hit is
        logged — and neither does a restart between two slots;
  (iv)  the day's billed searches, printed with the per-rule breakdown.

R1 is given the top priority band so it takes slot 1; everything else ranks
as the engine ranks it. THE QUEUE IS SYNTHETIC: the real R1 item is pinned to
NEWS_MAIN_TIME (14:00) by `nextaction._r_ai_news`, and that pin — with the
hourly news checks — is exercised in verify_news_feed.py. Here R1 is left
unpinned so slot 1 stays the research-timing case it was written for.
"""
import asyncio
import logging
import os
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta
from types import SimpleNamespace

# --report-only prints the checks without failing: used to measure the OLD
# code on the same day, for the before/after budget.
REPORT_ONLY = "--report-only" in sys.argv

TMP = tempfile.mkdtemp(prefix="saley-research-")
os.environ["DB_PATH"] = os.path.join(TMP, "sales_bot_test.db")
os.environ["SALES_DIGEST_ENABLED"] = "true"

import config  # noqa: E402

config.DB_PATH = os.environ["DB_PATH"]
config.SALES_TEST_MODE = True
config.SALES_TEST_CHANNEL_ID = 4242
config.SALES_CHANNEL_IDS = [4242]
config.SALES_CHANNEL_ID_SET = {4242}
config.SALES_DIGEST_ENABLED = True
config.WEB_SEARCH_ENABLED = True
config.WEB_SEARCH_DAILY_BUDGET = 60
config.NEWS_PREFERRED_DOMAINS = []
config.DAILY_MESSAGE_CAP = 4
config.DAILY_MESSAGE_CAPS = {}
config.DRIP_WEEKDAYS_ONLY = True
# THE PRODUCTION DEFAULTS, pinned. A local .env tuned for fast testing (1-minute
# ticks and gaps) would otherwise make this a different day entirely.
config.COS_FOLLOWUP_CHECK_INTERVAL_MINUTES = 15
config.SALES_DRIP_START = "10:00"
config.SALES_DRIP_END = "19:00"
config.MESSAGE_GAP_MINUTES = 90
config.MESSAGE_JITTER_MINUTES = 15
config.MESSAGE_GAP_MIN_MINUTES = 30
config.DRIP_REASK_DAYS = 2
# R1's HOURLY NEWS CHECKS search on their own slots by design (they are
# verified in verify_news_feed.py). This script is about the drip's research
# timing, so they are switched off here or every check slot would be a tick
# with a `[websearch]` line in it.
config.NEWS_CHECK_TIMES = []

import deadlines as dl  # noqa: E402
import mapping_sheet  # noqa: E402
import nextaction  # noqa: E402
from bot import SalesBot  # noqa: E402

DAY = date(2026, 9, 29)                 # a Tuesday
NOW = [datetime(DAY.year, DAY.month, DAY.day, 9, 30, tzinfo=dl.IST)]
dl.now_ist = lambda: NOW[0]
dl.today_ist = lambda: NOW[0].date()

failures = 0
LINES: list = []                         # (tick label, logger, message)
TICK = ["boot"]


def check(name, got, want=True):
    global failures
    ok = (got == want)
    if not ok and not REPORT_ONLY:
        failures += 1
    print(f"  {'PASS' if ok else ('info' if REPORT_ONLY else 'FAIL')}  {name}"
          + ("" if ok else f": got {got!r}, want {want!r}"))


class Capture(logging.Handler):
    def emit(self, record):
        LINES.append((TICK[0], record.name, record.getMessage()))


class FakeClock(logging.Filter):
    def filter(self, record):
        record.fake = NOW[0].strftime("%H:%M")
        return True


root = logging.getLogger()
root.setLevel(logging.INFO)
for h in list(root.handlers):
    root.removeHandler(h)
_out = logging.StreamHandler(sys.stdout)
_out.addFilter(FakeClock())
_out.setFormatter(logging.Formatter("%(fake)s IST  %(levelname)-5s %(name)s: %(message)s"))
root.addHandler(_out)
root.addHandler(Capture())
for noisy in ("discord", "asyncio", "gtm_sheet", "sources", "state"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


def rule(rid, name):
    return SimpleNamespace(id=rid, name=name, destination="channel",
                           counts_toward_cap=True, max_items_per_post=5)


def queue():
    """A FRESH queue each call, as the engine hands back on every tick."""
    today = NOW[0].date()
    mk = nextaction._item
    return [
        mk(rule=rule("R1", "AI news"), trigger=nextaction.R_AI_NEWS, today=today,
           due=today, why="R1 runs every weekday", text="Today's AI news",
           owner="Vaishnavi", web_pending=True,
           extra={"priority": 0}),
        mk(rule=rule("R2", "News companies to screen"),
           trigger=nextaction.R_NEWS_SCREEN, today=today, due=today,
           why="R2 runs every weekday", text="Companies in the news",
           owner="Sid", web_pending=True),
        mk(rule=rule("R6", "Connected, no DM yet"), trigger=nextaction.R_LI_NO_DM,
           today=today, due=today - timedelta(days=4), why="connected 4d ago",
           text="Acme AI — Ada Lovelace", owner="Kushal", company="Acme AI",
           poc="Ada Lovelace", row_key="acme ai|ada lovelace", web_pending=True),
        mk(rule=rule("R7", "DM sent, no meeting"), trigger=nextaction.R_DM_NO_MEETING,
           today=today, due=today - timedelta(days=6), why="DM 6d ago",
           text="Globex — Alan Turing", owner="Trishi", company="Globex",
           poc="Alan Turing", row_key="globex|alan turing"),
    ]


class StubLLM:
    """One billed search per call, and a `[websearch]` line like the real one."""
    wlog = logging.getLogger("llm")

    async def web_research(self, *, rule, prompt, max_uses=0, lean=False):
        self.wlog.info("[websearch] %s: 1 search(es) billed (stub)", rule)
        if rule == "R1":
            text = ("STORY | RLHF | Wispr Flow raises $30m | a Series B "
                    "| https://techcrunch.com/wispr | 4\n")
        elif rule == "R2":
            text = ("SCREEN | Nebius | builds inference infrastructure | fits evals, "
                    "buys eval data | https://nebius.com/news\n")
        else:
            text = "no public email found for Ada Lovelace"
        return {"ok": True, "text": text, "sources": [], "searches": 1,
                "errors": [], "note": ""}


class FakeChannel:
    id = 4242
    name = "sales-test"


class Killed(Exception):
    """The process dying mid-compose, after research and before the claim."""


KILL_NEXT_SEND = [False]


def make_bot():
    bot = SalesBot()
    bot.llm = StubLLM()

    async def run_next_actions(*, today):
        return {"actions": queue(), "rules_run": []}

    async def rows(_why):
        return [{"name": "Sahaj Garg", "company": "Wispr Flow", "_row": 12}], None

    async def known():
        return ["Acme AI", "Globex"]

    async def nothing(**_kw):
        return []

    async def no_funnel(**_kw):
        return None

    async def no_sweep(**_kw):
        return None

    async def send(channel, message, *, marker, channel_id):
        acts = message.get("actions") or []
        researched = [a.get("rule_id") for a in acts if a.get("research")]
        pending = [a.get("rule_id") for a in acts if a.get("web_pending")]
        if KILL_NEXT_SEND[0]:
            KILL_NEXT_SEND[0] = False
            logging.getLogger("verify").info(
                "[verify] slot %s: process killed while composing (after research, "
                "before the slot is claimed)", message.get("slot"))
            raise Killed()
        bot.db.record_drip_send(
            on_date=marker, slot=int(message["slot"]),
            group_key=message["group_key"], action_type=message["type"],
            owner_key=message.get("owner_key", ""),
            owner_label=message.get("owner", ""),
            companies=", ".join(message.get("companies") or []),
            planned_at=message["send_at"].isoformat(),
            sent_at=NOW[0].isoformat(),
        )
        logging.getLogger("verify").info(
            "[verify] SENT slot %s %s x %s — researched=%s still_pending=%s "
            "sources=%d", message.get("slot"), message.get("rule_id"),
            message.get("owner"), researched or "-", pending or "-",
            sum(len(a.get("sources") or []) for a in acts))

    bot._run_next_actions = run_next_actions
    bot._active_rows_of_canonical_tab = rows
    bot._known_companies = known
    bot._event_actions = nothing
    bot._funnel_action = no_funnel
    bot._sweep_proposals = no_sweep
    bot._send_drip_message = send
    bot._maybe_write_daily_summary = lambda: None
    bot.get_channel = lambda cid: FakeChannel()
    mapping_sheet.MAPPING.researchers = lambda: []
    mapping_sheet.MAPPING.departures = lambda: None
    return bot


def lines_of(tick, needle):
    return [m for (t, _n, m) in LINES if t == tick and needle in m]


async def main():
    interval = max(1, config.COS_FOLLOWUP_CHECK_INTERVAL_MINUTES)
    bot = make_bot()
    end = datetime(DAY.year, DAY.month, DAY.day, 18, 0, tzinfo=dl.IST)
    ticks: list = []
    sent_on: dict = {}                    # slot -> tick label
    restarted_after_slot1 = False

    while NOW[0] <= end:
        label = NOW[0].strftime("%H:%M")
        TICK[0] = label
        before = len(bot.db.drip_sent_today(dl.iso(DAY)))
        print(f"\n---- sweep tick {label} ----")

        # (iii-a) THE FIRST TIME SLOT 1 IS DUE, the process dies mid-compose.
        if label == "10:00":
            KILL_NEXT_SEND[0] = True
        try:
            await bot._sweep_once()
        except Killed:
            pass
        if label == "10:00":
            print("---- RESTART (new SalesBot, same DB) ----")
            bot = make_bot()
            TICK[0] = label + "+restart"
            label = TICK[0]
            print(f"\n---- sweep tick {label} ----")
            await bot._sweep_once()

        after = bot.db.drip_sent_today(dl.iso(DAY))
        for row in after[before:]:
            sent_on[int(row["slot"])] = label
        ticks.append(label)

        # (iii-b) A RESTART BETWEEN TWO SLOTS, once slot 1 has gone out.
        if sent_on.get(1) and not restarted_after_slot1:
            restarted_after_slot1 = True
            print("---- RESTART between slots (new SalesBot, same DB) ----")
            bot = make_bot()
        NOW[0] = NOW[0] + timedelta(minutes=interval)

    marker = dl.iso(DAY)
    print("\n================ RESULTS ================")
    print("slots sent at:", {k: sent_on[k] for k in sorted(sent_on)})
    send_ticks = set(sent_on.values()) | {"10:00"}
    idle = [t for t in ticks if t not in send_ticks
            and t >= "10:00"]
    websearch_on_idle = {t: len(lines_of(t, "[websearch]")) for t in idle}

    print("\n(i) ticks with nothing due")
    # IDLE TICKS THAT STILL PLANNED — past the gap guard, so the whole day was
    # re-planned on them — are the ones that used to research.
    replanned = [t for t in idle if t > sent_on.get(1, "99")
                 and lines_of(t, "[drip]   slot")]
    shown = replanned[:3]
    for t in shown:
        print(f"    tick {t}: {websearch_on_idle[t]} [websearch] line(s)")
    check("three consecutive idle ticks after slot 1 make no [websearch] lines",
          sum(websearch_on_idle[t] for t in shown), 0)
    check("...and no idle tick all day does",
          sum(websearch_on_idle.values()), 0)

    print("\n(ii) the tick that sends slot 1")
    t1 = sent_on.get(1, "")
    seq = [m for (t, _n, m) in LINES if t == t1 and (
        "[websearch]" in m or "[research-cache]" in m or "[verify] SENT" in m
        or "researching its" in m)]
    for m in seq:
        print("    " + m)
    r1_calls = [m for m in lines_of(t1, "[websearch] R1:") if "(stub)" in m]
    check("slot 1 is R1", "SENT slot 1 R1" in " ".join(seq))
    check("the research came immediately before the send",
          bool(seq) and "SENT slot 1" in seq[-1] and "researching" in " ".join(seq))
    check("R1 was researched on its own slot (research, not the plan)",
          bool(lines_of("10:00", "researching its")))
    check("nothing else in the plan was researched on that tick",
          [m for m in lines_of(t1, "[websearch]") if "(stub)" in m
           and "R1" not in m], [])
    check("R1 searched once on its slot, across the kill and the restart",
          len([m for (t, _n, m) in LINES if "[websearch] R1:" in m and "(stub)" in m]),
          1)

    print("\n(iii) restarts")
    for m in [m for (t, _n, m) in LINES if "[research-cache] hit" in m]:
        print("    " + m)
    check("the retry after the kill hit the cache",
          bool(lines_of("10:00+restart", "[research-cache] hit")))
    t2 = next((t for (t, _n, m) in LINES if "SENT slot" in m and " R2 x " in m), "")
    print(f"    R2 went at {t2}, after a restart; its tick:")
    for m in [m for (t, _n, m) in LINES if t == t2 and (
            "[research-cache]" in m or "[websearch]" in m or "SENT" in m)]:
        print("      " + m)
    # R2 READS TODAY'S POSTED STORIES from news_stories and makes its one
    # screen call; what it must not do is run R1's news search again.
    check("R2's slot did not re-run the news search",
          lines_of(t2, "[websearch] R1:"), [])
    check("...and made exactly its one screen call",
          len(lines_of(t2, "[websearch] R2:")), 1)

    print("\n(iv) the day's budget")
    used = bot.db.web_searches_today(marker)
    for row in bot.db.web_search_breakdown(marker):
        print(f"    {row['rule_id']:6} searches={row['searches']} calls={row['calls']}")
    print(f"    TOTAL billed searches: {used} of {config.WEB_SEARCH_DAILY_BUDGET}")
    print(f"RESULT_SEARCHES={used}")


NEWS_KEY = "R1|news-run"

try:
    asyncio.run(main())
finally:
    config.SALES_TEST_MODE = False
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
sys.exit(1 if failures else 0)
