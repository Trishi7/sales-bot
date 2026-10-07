"""THE PLAIN-LANGUAGE TEST DAY, driven end to end. Sends nothing, writes nothing.

    python verify_testday.py

Exercises `_handle_test_command` -> `_run_test_day` -> the footer against a fake
Discord channel that COLLECTS instead of sending, on a throwaway *_test.db in a
temp directory. It asserts the contract a tester depends on:

  - "make it Monday" moves the persistent clock and posts that day;
  - the run stops at 10:00 for the morning item and 14:00 for the rest;
  - it posts ONLY the messages — no clock lines, no footer;
  - "why was it quiet" answers in points: Sent, what rolled, what looked
    and found nothing, what is not today's rule — in plain words;
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

import os as _os  # noqa: E402  NFT2-1064 offline guard: canned source statuses, real Sheets/Drive calls blocked
import sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "tests"))
import offline_guard  # noqa: E402
GUARD = offline_guard.install_script()
import config  # noqa: E402

# THIS SCRIPT CHECKS THE SERVER-SIDE SEARCH PATH (SEARCH_BACKEND=anthropic), with
# the search itself stubbed. The default path — search outside the model, the
# feeds, the light model — is verify_search_backend.py's to check.
config.SEARCH_BACKEND = "anthropic"

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

import tone  # noqa: E402

# THE FIRST VARIANT OF EVERY LINE, so an exact sentence can be asserted.
tone.pin(0)

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

    async def fake_plan(*, today, already, queue=None, only_rule=""):
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
    # THE ONE LINE THAT IS NOT A MESSAGE: the day, and the real date it
    # was counted from.
    confirm = [str(p) for p in POSTED if "the real date is" in str(p)]
    check("the day is confirmed once, with the real date", len(confirm), 1)
    check("ONLY the messages after it: three posts and nothing else",
          [str(p) for p in POSTED if "the real date is" not in str(p)],
          ["<post slot 1 for Vaishnavi>", "<post slot 2 for Vaishnavi>",
           "<post slot 3 for Sid>"])
    check("no clock line, no footer", "It's now" in text or "— done" in text, False)
    check("the day was lived through to the 2:00 PM stop",
          dl.now_ist().strftime("%H:%M"), "14:00")

    print('\n"why was it quiet" explains the day instead of a footer')
    POSTED.clear()
    handled = await bot._handle_test_command(FakeMessage(), "why was it quiet?")
    check("handled", handled)
    text = "\n".join(str(p) for p in POSTED)
    for sub in text.splitlines():
        print("   |", sub)
    check("it counts the posts", "• Sent: 3" in text)
    check("it says what rolled over",
          "• Rolled to tomorrow: new companies in the pipeline — asks before looking up PoCs (1)"
          in text)
    check("it says what is not today's rule",
          "• Not a Monday rule: sales packages that aren't ready yet" in text)
    check("...including a rule that ran and found nothing",
          "• Looked, nothing due: P1 deliverables due this week — title and due date only" in text)

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
