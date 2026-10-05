"""ONE-OFF REMINDERS AT AN EXACT MINUTE — with real output and a real loop.

    python verify_reminders.py        # about 70 s: check (ii) runs the real 60 s loop

Nothing reaches Discord or the sheet. The REAL `schedule_reminder` /
`list_reminders` / `cancel_reminder` tool handlers run (built by
`_sheet_tools` for a fake message in the test channel), the REAL
`_reminder_loop` runs at its real REMINDER_CHECK_SECONDS=60, and the REAL
pretend clock (clock.py, SALES_TEST_MODE) is moved the way `make it …` /
`next day` and the test day's 14:00 stop move it. Throwaway *_test.db; the
channel records what it was sent and when.

  (i)   "remind me tomorrow at 2pm about the pulse doc" — the confirmation
        names the date AND the time; a time already past is refused;
  (ii)  advance the test clock — the LIVE loop stays held (the test run owns
        the day); the test run's own firing posts it, "[TEST]"-tagged, tagging
        the asker, in the same channel;
  (iii) a Saturday reminder fires on Saturday (a day the drip is silent);
  (iv)  the row is closed and does not fire twice;
  (v)   a reminder WITH a company still appears in the drip preview, and is
        closed after the exact-time firing;
  plus  list_reminders / cancel_reminder, and the engine prompt's new line.
"""
import asyncio
import logging
import os
import shutil
import sys
import tempfile
import time as _time
from datetime import date, time
from types import SimpleNamespace

TMP = tempfile.mkdtemp(prefix="saley-reminders-")
os.environ["DB_PATH"] = os.path.join(TMP, "sales_bot_test.db")

import config  # noqa: E402

config.DB_PATH = os.environ["DB_PATH"]
config.SALES_TEST_MODE = True
config.SALES_TEST_CHANNEL_ID = 4242
config.SALES_CHANNEL_IDS = [4242]
config.SALES_CHANNEL_ID_SET = {4242}
VAISHNAVI = 1002
config.TEAM_ROSTER_IDS = [VAISHNAVI]
config.ROSTER_DISPLAY_NAMES = {str(VAISHNAVI): "Vaishnavi"}
# THE SHIPPED DEFAULTS, pinned.
config.REMINDER_DEFAULT_TIME = "14:00"
config.REMINDER_CHECK_SECONDS = 60

import clock  # noqa: E402
import drip  # noqa: E402
import gtm_sheet  # noqa: E402
import nextaction  # noqa: E402
import query_engine  # noqa: E402
from bot import SalesBot  # noqa: E402
from db import DB  # noqa: E402

import tone  # noqa: E402

# THE FIRST VARIANT OF EVERY LINE, so an exact sentence can be asserted.
tone.pin(0)

failures = 0


def check(name, got, want=True):
    global failures
    ok = (got == want)
    failures += 0 if ok else 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f": got {got!r}, want {want!r}"))


logging.basicConfig(level=logging.WARNING, format="      log  %(name)s: %(message)s")
logging.getLogger("bot").setLevel(logging.INFO)
logging.getLogger("bot").addFilter(lambda r: r.getMessage().startswith("[reminders]"))

T0 = [_time.monotonic()]


class FakeChannel:
    id = 4242
    name = "sales-test"
    guild = SimpleNamespace(id=1)

    def __init__(self):
        self.sent = []

    async def send(self, body):
        self.sent.append((_time.monotonic() - T0[0], clock.format_moment(), body))
        return SimpleNamespace(id=len(self.sent), jump_url="https://discord/x")


ACME_ROW = {"company": "Acme AI", "name": "Ada Lovelace", "poc": "Ada Lovelace",
            "_row": 12, "designation": "Head of Research"}


class FakeTab:
    title = "Outreach PoCs"
    kind = gtm_sheet.POCS
    rows = [ACME_ROW]


def say(title):
    print(f"\n{title}")


def pretend(day, hh, mm=0):
    ok, line = clock.set_day(day, at=time(hh, mm), by="verify")
    assert ok, line


async def main():
    bot = SalesBot()
    bot.db = DB(config.DB_PATH)
    channel = FakeChannel()
    bot.get_channel = lambda cid: channel if int(cid) == 4242 else None

    async def ready():
        return None

    bot.wait_until_ready = ready
    gtm_sheet.SHEETS.tab = lambda kind, *a, **k: FakeTab() if kind == gtm_sheet.POCS else None

    msg = SimpleNamespace(
        id=1, channel=channel, content="",
        author=SimpleNamespace(id=VAISHNAVI, bot=False, name="vaishnavi",
                               display_name="Vaishnavi", global_name="Vaishnavi"))
    tools = {t["schema"]["name"]: t["handler"] for t in bot._sheet_tools(msg)}
    schedule, listing, cancel = (tools["schedule_reminder"], tools["list_reminders"],
                                 tools["cancel_reminder"])

    TUE, WED, SAT = date(2026, 9, 29), date(2026, 9, 30), date(2026, 10, 3)
    THU = date(2026, 10, 1)

    # ------------------------------------------------------------------ (i)
    say("(i) \"remind me tomorrow at 2pm about the pulse product overview doc\" — "
        "asked Tue 29 Sep 11:00 (test time)")
    pretend(TUE, 11)
    r1 = await schedule({"what": "the pulse product overview doc",
                         "date": "tomorrow", "time": "2pm"})
    print(f"   tool result: ok={r1['ok']} id={r1.get('id')} confirm={r1.get('confirm')!r}")
    check("scheduled with no company", r1["ok"])
    check("the confirmation names the date and the time",
          r1.get("confirm"), "Got it — Wednesday 30 Sep, 2:00 pm.")
    both = await schedule({"what": "x", "date": "tomorrow at 2pm"})
    check("'tomorrow at 2pm' all in the date field reads the same",
          both.get("confirm"), "Got it — Wednesday 30 Sep, 2:00 pm.")
    await cancel({"id": both["id"]})
    past = await schedule({"what": "the standup notes", "date": "today", "time": "9am"})
    print(f"   a time already past: ok={past['ok']} reason={past['reason']!r}")
    check("a past time is refused, not scheduled", past["ok"], False)
    row = bot.db.scheduled_reminder(r1["id"])
    check("stored with the channel and the asker",
          (row["channel_id"], row["asker_id"], row["due_time"]), ("4242", str(VAISHNAVI), "14:00"))
    prompt = query_engine._system_prompt(requester_name="V", today="2026-09-29",
                                         tool_names=["schedule_reminder"])
    check("the engine prompt carries the new line",
          "use schedule_reminder even if no company is mentioned" in prompt)

    # ------------------------------------------------------------------ (ii)
    say("(ii) THE REAL LOOP, every 60 s — then the tester moves to Wed 30 Sep 14:00")
    T0[0] = _time.monotonic()
    loop_task = asyncio.create_task(bot._reminder_loop())
    await asyncio.sleep(3)
    check("nothing fired on Tuesday", channel.sent, [])
    pretend(WED, 14)
    moved_at = _time.monotonic() - T0[0]
    print(f"   {moved_at:5.1f}s  clock moved to {clock.describe()}")
    # THE LIVE LOOP STANDS DOWN WHILE THE PRETEND CLOCK IS SET — the test run
    # owns the day and fires the reminder itself at its stop.
    while not channel.sent and _time.monotonic() - T0[0] < moved_at + 65:
        await asyncio.sleep(0.5)
    loop_task.cancel()
    check("the live loop, held by the pretend clock, posted nothing", channel.sent, [])
    fired = await bot._fire_due_reminders(from_test=True)
    tag = config.SIMULATION_PREFIX
    if channel.sent:
        t, at, body = channel.sent[0]
        print(f"   {t:5.1f}s  the test run fired it at {at} (test time): {body!r}")
        check("it tags the asker, under [TEST] and its heading",
              body.startswith(f"{tag} **Reminder**\n<@{VAISHNAVI}> — you asked me to "
                              "remind you: "))
        check("...with what they asked for",
              body, f"{tag} **Reminder**\n<@{VAISHNAVI}> — you asked me to remind you: "
                    "the pulse product overview doc")
    else:
        check("the test run fired it", fired, [r1["id"]])
    check("in the same channel it was asked in", len(channel.sent), 1)
    check("not a drip message", bot.db.drip_sent_today("2026-09-30"), [])

    # ------------------------------------------------------------------ (iii)
    say("(iii) A SATURDAY REMINDER — asked Tue, fires Sat 3 Oct 10:00")
    pretend(TUE, 11)
    r3 = await schedule({"what": "send the weekend summary", "date": "saturday",
                         "time": "10:00"})
    print(f"   confirm={r3.get('confirm')!r}  is_weekend={r3.get('is_weekend')}")
    pretend(SAT, 9, 59)
    before = len(channel.sent)
    fired = await bot._fire_due_reminders(from_test=True)
    check("not a minute early", fired, [])
    pretend(SAT, 10, 0)
    fired = await bot._fire_due_reminders(from_test=True)
    print(f"   Sat 10:00 tick fired {fired}: {channel.sent[-1][2]!r}")
    check("it fired on Saturday", fired, [r3["id"]])
    check("...on a day the drip does not send", drip.is_sending_day(SAT), False)

    # ------------------------------------------------------------------ (iv)
    say("(iv) CLOSED, AND NEVER TWICE")
    pretend(SAT, 10, 5)
    again = await bot._fire_due_reminders(from_test=True)
    again2 = await bot._fire_due_reminders(from_test=True)
    check("two more ticks fire nothing", (again, again2), ([], []))
    check("exactly one post for it", len(channel.sent) - before, 1)
    for rid in (r1["id"], r3["id"]):
        st = bot.db.scheduled_reminder(rid)["status"]
        print(f"   reminder #{rid}: status={st}")
        check(f"#{rid} is closed", st, "done")

    # ------------------------------------------------------------------ (v)
    say("(v) A REMINDER WITH A COMPANY — drip preview Thu morning, exact firing 15:00")
    pretend(TUE, 11)
    r5 = await schedule({"what": "send Acme the pricing deck", "company": "Acme AI",
                         "date": "thursday", "time": "3pm"})
    print(f"   confirm={r5.get('confirm')!r}  matched_sheet_row={r5.get('matched_sheet_row')}")
    check("matched the sheet row", r5.get("matched_sheet_row"), 12)

    def drip_items(day):
        return nextaction._scheduled_reminders({
            "today": day, "rows": [ACME_ROW], "snoozes": {},
            "scheduled": bot.db.scheduled_reminders_by_row(),
        })

    pretend(THU, 9)
    items = drip_items(THU)
    planned = drip.plan(items, day=THU, history={}, already_sent=[], cap=5)
    preview = drip.preview_text(planned)
    lines = [l for l in preview.splitlines() if "Acme" in l]
    print("   drip preview, Thu 09:00:")
    for l in lines[:3]:
        print("     " + l.strip())
    check("it appears in the drip preview", bool(lines))
    check("...with its text (not 'the reminder you asked for')",
          any("send Acme the pricing deck" in i["text"] for i in items))
    pretend(THU, 15)
    fired = await bot._fire_due_reminders(from_test=True)
    print(f"   Thu 15:00 tick fired {fired}: {channel.sent[-1][2]!r}")
    check("it fired at the exact time", fired, [r5["id"]])
    check("...naming the company", channel.sent[-1][2].endswith("(Acme AI)"))
    check("closed", bot.db.scheduled_reminder(r5["id"])["status"], "done")
    check("so the drip does not repeat it", drip_items(THU), [])

    # ------------------------------------------------------------------ extras
    say("list_reminders / cancel_reminder")
    pretend(TUE, 11)
    r6 = await schedule({"what": "chase the MSA", "date": "next friday"})
    lst = await listing({})
    print(f"   list_reminders -> {lst['reminders']}")
    check("the open one is listed, with its time in words",
          [x["when"] for x in lst["reminders"]], ["Friday 2 Oct, 2:00 pm"])
    c = await cancel({"id": r6["id"]})
    check("cancelled", c["ok"])
    check("...and no longer listed", (await listing({}))["count"], 0)


try:
    asyncio.run(main())
finally:
    try:
        clock.back_to_today(by="verify")
    except Exception:
        pass
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
sys.exit(1 if failures else 0)
