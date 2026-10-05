"""THE PRETEND CLOCK OWNS THE DATE ONLY — driven on the REAL clock. `python verify_clock.py`

No model calls, no sheet, a fake channel that COLLECTS, a throwaway *_test.db.
Takes about five minutes, because two reminders are set two minutes ahead and
the script waits for the real minute, polling the same function the 60 s loop
runs (`_fire_due_reminders`).

  (i)   "make it Monday" -> Monday 28 Sep, <the real IST time> (test time);
  (ii)  a reminder two minutes ahead is confirmed, and fires at that minute
        (test time) within 60 s, tagging the asker — WHILE a test run is marked
        active, because the reminder loop is no longer held by one;
  (iii) the test day's stops say 10:00 AM then 2:00 PM; afterwards "what time
        is it" says the real IST time with "(test time)";
  (iv)  "back to today" -> the real IST date and time;
  (v)   the same reminder flow with no pretend date -> "(IST)", fires on time;
  plus: the current minute is scheduled "right away" (now + 1 min), and a
  time typed "1: 17 pm" / "1.17pm" parses.
"""
import asyncio
import logging
import os
import re
import shutil
import sys
import tempfile
import time as _t
from datetime import timedelta

TMP = tempfile.mkdtemp(prefix="saley-verify-clock-")
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

import clock  # noqa: E402
import deadlines as dl  # noqa: E402
import sheetwrite  # noqa: E402
from bot import SalesBot  # noqa: E402

LOGS: list = []


class _Grab(logging.Handler):
    def emit(self, record):
        LOGS.append(record.getMessage())


logging.basicConfig(level=logging.WARNING)
logging.getLogger().addHandler(_Grab(level=logging.INFO))
logging.getLogger().setLevel(logging.INFO)
for _n in ("httpx", "httpcore", "anthropic", "discord", "gtm_sheet", "mapping_sheet"):
    logging.getLogger(_n).setLevel(logging.WARNING)

import tone  # noqa: E402

# THE FIRST VARIANT OF EVERY LINE, so an exact sentence can be asserted.
tone.pin(0)

failures = 0
POSTED: list = []


def check(name, got, want=True):
    global failures
    ok = (got == want)
    failures += 0 if ok else 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f": got {got!r}, want {want!r}"),
          flush=True)


class FakeChannel:
    id = 4242
    name = "sales-test"

    async def send(self, body, **kw):
        POSTED.append((dl.real_now_ist(), body))
        return type("M", (), {"id": 5000 + len(POSTED), "channel": self})()


class FakeAuthor:
    id = 111
    display_name = "Vaishnavi"
    name = "vaishnavi"
    bot = False


class Msg:
    channel = FakeChannel()
    author = FakeAuthor()
    reference = None

    def __init__(self, content):
        self.content = content
        self.id = 9000 + len(POSTED)

    async def reply(self, body, **kw):
        POSTED.append((dl.real_now_ist(), body))
        return type("S", (), {"id": 7000 + len(POSTED), "channel": FakeChannel()})()


def words(moment):
    h = moment.strftime("%I").lstrip("0") or "12"
    return f"{h}:{moment.strftime('%M')} {moment.strftime('%p').lower()}"


def real_hhmm():
    return clock.format_moment(dl.real_now_ist()).split(", ")[1]


async def reminder_flow(bot, label, *, test_run_active):
    tools = {t["schema"]["name"]: t["handler"] for t in bot._sheet_tools(Msg("x"))}
    target = (dl.real_now_ist() + timedelta(minutes=2)).replace(second=0, microsecond=0)
    asked = words(target)
    print(f'   "remind me at {asked} today" (typed at {dl.real_now_ist():%H:%M:%S} IST)')
    out = await tools["schedule_reminder"](
        {"what": "check the pulse doc", "date": "today", "time": asked})
    print(f"   confirm: {out.get('confirm') or out}")
    tag = "(test time)" if clock.pretending() else "(IST)"
    want = f"Got it — {clock.format_moment(dl.now_ist().replace(hour=target.hour, minute=target.minute))} {tag}."
    check(f"{label}: confirmed with the date, the time and the clock", out.get("confirm"), want)
    bot._news_hold_depth = 1 if test_run_active else 0
    POSTED.clear()
    fired_at = None
    deadline = _t.monotonic() + 240
    while _t.monotonic() < deadline:
        ids = await bot._fire_due_reminders()      # the 60 s loop's body
        if ids:
            fired_at = dl.real_now_ist()
            break
        await asyncio.sleep(5)
    bot._news_hold_depth = 0
    body = POSTED[-1][1] if POSTED else ""
    print(f"   fired at {fired_at:%H:%M:%S} IST: {body!r}" if fired_at else "   never fired")
    check(f"{label}: fired", fired_at is not None)
    if fired_at:
        late = (fired_at - target).total_seconds()
        check(f"{label}: at the minute, within 60 s (was {late:.0f} s)", 0 <= late < 60)
        check(f"{label}: tags the asker", "<@111> — you asked me to remind you: check the "
                                          "pulse doc" in body)


async def main():
    bot = SalesBot()
    bot.llm = None
    bot.get_channel = lambda cid: FakeChannel()

    async def fake_queue(*, today):
        return {"actions": [], "rules_run": []}

    async def fake_plan(*, today, already, queue=None, only_rule=""):
        return {"messages": [], "rolled": [], "held": []}

    async def no_news(*a, **k):
        return None

    stops = []
    real_fire = bot._fire_due_reminders

    async def record_stops(**kw):
        if kw.get("from_test"):
            stops.append(clock.describe())
        return await real_fire(**kw)

    bot._run_next_actions = fake_queue
    bot._plan_drip = fake_plan
    bot._test_news_check = no_news
    bot._sweep_proposals = lambda **kw: asyncio.sleep(0)

    print("time parsing")
    for raw, want in (("1: 17 pm", "13:17"), ("13:17", "13:17"), ("1.17pm", "13:17"),
                      ("1:17 p.m.", "13:17")):
        check(f"{raw!r} -> {want}", sheetwrite.parse_reminder_time(raw), want)

    print(f'\n(i)+(iii) "make it Monday" at {dl.real_now_ist():%H:%M} IST (a Wednesday: '
          "this week's Monday)")
    bot._fire_due_reminders = record_stops
    LOGS.clear()
    await bot._handle_test_command(Msg("make it Monday"), "make it Monday")
    bot._fire_due_reminders = real_fire
    set_line = next((l for l in LOGS if l.startswith("[clock] the pretend clock is now")), "")
    print("   log:", set_line)
    check("(i) the pretend day, at the REAL IST time",
          set_line.split(" (set by")[0],
          f"[clock] the pretend clock is now Monday 28 Sep, "
          f"{set_line.split('Monday 28 Sep, ')[1].split(' (test time)')[0] if 'Monday 28 Sep, ' in set_line else '?'} (test time)")
    check("(i) ...whose time of day is the real one",
          "Monday 28 Sep, " in set_line and set_line.split("Monday 28 Sep, ")[1].startswith(real_hhmm()[:4]))
    print("   the stops:", stops)
    check("(iii) the stops say 10:00 AM then 2:00 PM",
          [s.split(", ")[1][:8] for s in stops], ["10:00 AM", "2:00 PM "])
    cleared = [l for l in LOGS if "test-day time stop was cleared" in l]
    print("   log:", cleared[0] if cleared else "(no clear logged)")
    check("(iii) the stop was cleared when the run ended, and logged", bool(cleared))
    POSTED.clear()
    await bot._route_query(Msg("what time is it"), "what time is it", "Vaishnavi")
    said = POSTED[-1][1] if POSTED else ""
    print("   what time is it ->", said)
    check("(iii) after the run: the real IST time, marked test time",
          said, f"It's Monday 28 Sep, {real_hhmm()} (test time).")

    print("\n(ii) a reminder two minutes ahead, under the pretend date, with a test run "
          "active")
    await reminder_flow(bot, "(ii)", test_run_active=True)

    print("\nthe current minute means right away")
    tools = {t["schema"]["name"]: t["handler"] for t in bot._sheet_tools(Msg("x"))}
    now = dl.now_ist()
    out = await tools["schedule_reminder"]({"what": "now-ish", "date": "today",
                                            "time": words(now)})
    print(f"   \"{words(now)}\" typed at {now:%H:%M:%S} -> {out.get('confirm') or out}")
    soon = (now + timedelta(minutes=1)).replace(second=0, microsecond=0)
    check("scheduled for now + 1 minute, not refused",
          (out.get("ok"), out.get("time")), (True, soon.strftime("%H:%M")))
    past = now - timedelta(minutes=10)
    out = await tools["schedule_reminder"]({"what": "late", "date": "today",
                                            "time": words(past)})
    print(f"   \"{words(past)}\" -> {out.get('reason')}")
    check("ten minutes ago is still refused, quoting clock.describe()",
          (out.get("ok"), "(test time) now" in str(out.get("reason"))), (False, True))

    print('\n(iv) "back to today"')
    POSTED.clear()
    await bot._handle_test_command(Msg("back to today"), "back to today")
    said = POSTED[-1][1] if POSTED else ""
    print("   ->", said)
    check("(iv) the real IST date and time",
          said, f"Back to the real clock — it's {clock.format_moment(dl.real_now_ist())} (IST).")
    check("(iv) no pretend date left", clock.pretending(), False)

    print("\n(v) the same reminder flow with no pretend date")
    await reminder_flow(bot, "(v)", test_run_active=False)


try:
    asyncio.run(main())
finally:
    config.SALES_TEST_MODE = False
    clock.forget()
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
sys.exit(1 if failures else 0)
