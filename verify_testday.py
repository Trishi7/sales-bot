"""THE PLAIN-LANGUAGE TEST DAY, driven end to end. Sends nothing, writes nothing.

    python verify_testday.py

Exercises `_handle_test_command` -> `_run_test_day` -> the footer against a fake
Discord channel that COLLECTS instead of sending, on a throwaway *_test.db in a
temp directory. It asserts the contract a tester depends on:

  - "make it Monday" moves the persistent clock and posts that day;
  - the run stops at 10:00 for the morning item and 14:00 for the rest;
  - each stop opens with "It's now Monday 28 Sep, 2:00 PM (test time)";
  - the footer counts the posts and names what rolled and what was skipped,
    WITH REASONS and in plain words;
  - NO rule code (R1, R6, ...) appears anywhere the tester can read;
  - "next day" and "back to today" move and clear the clock;
  - "start over" asks before it wipes, and "no" cancels;
  - every command is SILENT outside the test channel.

The queue and the drip plan are stubbed: what is under test is the RUN'S SHAPE,
not the spreadsheet. `verify_simulation.py` covers the throwaway-preview path,
and `python -m clock` covers the clock arithmetic on its own.
"""
import asyncio
import os
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta

TMP = tempfile.mkdtemp(prefix="saley-verify5-")
os.environ["DB_PATH"] = os.path.join(TMP, "sales_bot_test.db")

import config  # noqa: E402

config.DB_PATH = os.environ["DB_PATH"]
config.SALES_TEST_MODE = True
config.SALES_TEST_CHANNEL_ID = 4242
config.SALES_CHANNEL_IDS = [4242]
config.SALES_CHANNEL_ID_SET = {4242}   # the predicate reads the SET, not the list
config.TEAM_ROSTER_IDS = [111]
config.SALES_APPROVER_IDS = [111]
config.SALES_DMS_ENABLED = False
config.TEST_POST_GAP_SECONDS = 0          # do not make the verification wait

import clock  # noqa: E402
import deadlines as dl  # noqa: E402
import rules  # noqa: E402
from bot import SalesBot  # noqa: E402

failures = 0
POSTED = []


def check(name, got, want=True):
    global failures
    ok = (got == want)
    failures += 0 if ok else 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}"
          + ("" if ok else f": got {got!r}, want {want!r}"))


class FakeChannel:
    id = 4242
    name = "sales-test"

    async def send(self, body):
        POSTED.append(body)
        return type("M", (), {"id": len(POSTED)})()


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
        POSTED.append(body)
        return type("M", (), {"id": len(POSTED)})()


def _msg(slot, hh, mm, rule_id, rule_name, owner, why=""):
    day = dl.today_ist()
    return {
        "slot": slot, "group_key": f"g{slot}", "type": "li_no_dm",
        "rule_id": rule_id, "rule_name": rule_name, "owner": owner,
        "owner_key": owner.lower(), "companies": ["Acme"],
        "counts_toward_cap": True, "destination": "channel",
        "send_at": datetime(day.year, day.month, day.day, hh, mm, tzinfo=dl.IST),
        "send_at_hhmm": f"{hh:02d}:{mm:02d}",
        "actions": [], "why": why,
    }


async def main():
    bot = SalesBot()
    bot.llm = None                      # no model calls; the template composes

    # Stub the two expensive reads. The run's shape is what is under test.
    async def fake_queue(*, today):
        return {"actions": [], "rules_run": [
            {"id": "R12", "name": "Sales packages", "ran": False,
             "why": "does not run on a Monday"},
            {"id": "R4", "name": "Deliverables checklist", "ran": True, "items": 0},
        ]}

    async def fake_plan(*, today, already):
        return {
            "messages": [
                _msg(1, 10, 0, "R8", "Meeting preparation", "Vaishnavi"),
                _msg(2, 14, 0, "R6", "LinkedIn connected, no DM", "Vaishnavi"),
                _msg(3, 15, 30, "R7", "DM sent, no meeting", "Sid"),
            ],
            "rolled": [{"rule_id": "R11", "rule_name": "New company in Master Pipeline",
                        "owner": "Sid", "companies": ["Globex"],
                        "why": "over the cap"}],
            "held": [],
        }

    bot._run_next_actions = fake_queue
    bot._plan_drip = fake_plan
    bot._sweep_proposals = lambda **kw: asyncio.sleep(0)

    sent_slots = []

    async def fake_send(channel, message, *, marker, channel_id):
        sent_slots.append(message["slot"])
        POSTED.append(f"<post slot {message['slot']} for {message['owner']}>")

    bot._send_drip_message = fake_send

    print('"make it Monday"')
    handled = await bot._handle_test_command(FakeMessage(), "make it Monday")
    check("the command was handled", handled)
    check("the clock moved to a Monday", dl.today_ist().weekday(), 0)
    check("all three slots were sent through the REAL send path",
          sent_slots, [1, 2, 3])

    print("\nwhat the tester saw")
    for line in POSTED:
        for sub in str(line).splitlines():
            print("   |", sub)

    text = "\n".join(str(p) for p in POSTED)
    check("it opened at 10:00 for the morning item",
          "It's now Monday" in text and "10:00 AM (test time)" in text)
    check("...and again at 2:00 PM for the rest",
          "2:00 PM (test time)" in text)
    check("the opening line is the agreed wording",
          any(l.strip().startswith("[TEST] It's now Monday")
              and "(test time)" in l for l in text.splitlines()))
    check("the footer counts the posts", "3 posts went out" in text)
    check("the footer says what rolled over and why",
          "companies that just appeared in the pipeline" in text
          and "over the cap" in text)
    check("the footer says what was skipped and why",
          "sales packages that aren't ready yet" in text
          and "does not run on a Monday" in text)
    check("...including a rule that ran and found nothing",
          "deliverables due or overdue" in text
          and "there was nothing due" in text)
    check("the footer says the state is real", "real" in text.lower())

    import re
    codes = re.findall(r"\bR\d{1,2}\b", text)
    check("NO rule codes anywhere in what the tester read", codes, [])

    print('\n"next day"')
    POSTED.clear()
    sent_slots.clear()
    handled = await bot._handle_test_command(FakeMessage(), "next day")
    check("handled", handled)
    check("the day advanced to Tuesday", dl.today_ist().weekday(), 1)

    print('\n"back to today"')
    POSTED.clear()
    handled = await bot._handle_test_command(FakeMessage(), "back to today")
    check("handled", handled)
    check("the real date is back", dl.today_ist(), dl.real_today_ist())

    print('\n"start over" asks before it wipes')
    POSTED.clear()
    handled = await bot._handle_test_command(FakeMessage(), "start over")
    check("handled", handled)
    check("nothing was wiped yet", bot._pending_start_over is not None)
    check("it asked", "confirm" in "\n".join(POSTED).lower()
          or "say yes" in "\n".join(POSTED).lower())
    print("   |", "\n   | ".join("\n".join(POSTED).splitlines()))

    POSTED.clear()
    handled = await bot._handle_test_command(FakeMessage(), "no")
    check("'no' cancels it", handled)
    check("the pending wipe is gone", bot._pending_start_over, None)
    check("it said so", "Left everything" in "\n".join(POSTED))

    print("\na stray yes cannot wipe anything")
    POSTED.clear()
    handled = await bot._handle_test_command(FakeMessage(), "yes")
    check("a bare 'yes' with nothing pending is not a command", handled, False)

    print("\nthe silent gate still holds outside the test channel")

    class OtherChannel(FakeChannel):
        id = 777

    class OtherMessage(FakeMessage):
        channel = OtherChannel()

    POSTED.clear()
    handled = await bot._handle_test_command(OtherMessage(), "make it Monday")
    check("not handled in another channel", handled, False)
    check("and it said nothing at all", POSTED, [])
    check("the clock did not move", dl.today_ist(), dl.real_today_ist())


try:
    asyncio.run(main())
finally:
    config.SALES_TEST_MODE = False
    clock.forget()
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
sys.exit(1 if failures else 0)
