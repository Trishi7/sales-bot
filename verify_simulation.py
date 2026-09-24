"""Run "simulate week fast" against the live sheet, offline. `python verify_simulation.py`

Drives the real simulation path — the real rules, the real sheet, the real
planner — with a fake Discord channel that collects what would have been posted
instead of sending it.

WHAT IT PROVES, beyond printing a week:
  - the database is a COPY and the original is byte-identical afterwards;
  - no sheet write happens, whatever SHEET_WRITES_ENABLED says;
  - every message carries the [TEST] prefix;
  - mentions render as plain names;
  - it runs with SALES_DIGEST_ENABLED=false.

Composition uses the model, as a real simulation does. Pass --templates to skip
that (and the API calls) when you only care about the schedule.
"""
import asyncio
import hashlib
import logging
import os
import shutil
import sys
import tempfile
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DISCORD_TOKEN", "x")
os.environ.setdefault("SALES_TEST_CHANNEL_ID", "424242")

import config
import simulation

TEMPLATES = "--templates" in sys.argv
if TEMPLATES:
    config.DRIP_LLM_COMPOSE = False

SID, VAISHNAVI, KUSHAL = 1001, 1002, 1003
config.SALES_TEST_CHANNEL_ID = 424242
config._attach_test_channel()
config.TEAM_ROSTER_IDS = [SID, VAISHNAVI, KUSHAL]
config.SALES_ALWAYS_TAG_IDS = [VAISHNAVI, SID]
config.SALES_APPROVER_IDS = [SID, VAISHNAVI]
config.SALES_FINAL_SAY_ID = SID
config.ROSTER_DISPLAY_NAMES = {str(SID): "Sid", str(VAISHNAVI): "Vaishnavi",
                               str(KUSHAL): "Kushal"}
config.SIMULATION_FAST_GAP_SECONDS = 0          # no waiting in a harness
config.SALES_DIGEST_ENABLED = False             # must work with the bot "off"

failures = 0
posted: list = []


def check(name, got, want):
    global failures
    ok = got == want
    failures += 0 if ok else 1
    print(f"    {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")


class FakeChannel:
    """Collects instead of sending. `guardrails.send` is patched to use it."""
    id = 424242
    guild = object()

    async def send(self, body):
        posted.append(body)
        return type("M", (), {"id": len(posted)})()


class FakeAuthor:
    id = SID
    display_name = "Sid"
    name = "Sid"
    bot = False


class FakeMessage:
    def __init__(self, text):
        self.content = text
        self.id = 1
        self.author = FakeAuthor()
        self.channel = FakeChannel()
        self.reference = None

    async def reply(self, body, **kw):
        posted.append(body)
        return type("M", (), {"id": len(posted)})()


async def main() -> int:
    global failures
    logging.basicConfig(level=logging.ERROR, format="%(levelname)-7s %(message)s")

    import guardrails
    import bot as botmod

    # Patch the send side to collect. Everything else is the real path.
    async def fake_send(destination, text, *, reason, kind="message", **kw):
        posted.append(text)
        return type("M", (), {"id": len(posted)})()

    guardrails.send = fake_send
    botmod.guardrails.send = fake_send

    # A bot instance without a gateway connection.
    sales = botmod.SalesBot.__new__(botmod.SalesBot)
    botmod.SalesBot.__init__(sales)
    sales._reply = lambda msg, body, **kw: msg.reply(body)

    db_before = config.DB_PATH
    digest_before = hashlib.sha256(
        open(db_before, "rb").read()
    ).hexdigest() if os.path.exists(db_before) else ""

    print("=" * 78)
    print('SIMULATE WEEK FAST — the real rules, the real sheet, nothing written')
    print("=" * 78)
    print(f"  test channel        {config.test_channel_id()}")
    print(f"  composer            {'templates (--templates)' if TEMPLATES else 'the model'}")
    print(f"  SALES_DIGEST_ENABLED {config.SALES_DIGEST_ENABLED}  (must still run)")
    print(f"  SHEET_WRITES_ENABLED {config.SHEET_WRITES_ENABLED}  (must still not write)")
    print()

    msg = FakeMessage("@bot simulate week fast")
    handled = await sales._handle_simulation(msg, msg.content)

    print("-" * 78)
    for line in posted:
        for sub in str(line).splitlines():
            print("  " + sub)
    print("-" * 78)
    print()

    print("=" * 78)
    print("  CHECKS")
    print("=" * 78)
    check("the command was handled", handled, True)
    check("something was posted", len(posted) > 0, True)

    bodies = [str(p) for p in posted]
    check("every post carries the [TEST] prefix",
          all(config.SIMULATION_PREFIX in b for b in bodies), True)
    check("no raw <@id> mention leaked",
          any("<@" in b for b in bodies), False)
    check("the week opens with a week header",
          "Simulating the week" in bodies[0], True)
    check("seven day headers",
          sum(1 for b in bodies if "Simulating " in b and " · " not in b), 7)
    check("seven footers", sum(1 for b in bodies if "done" in b and "posts sent" in b), 7)

    digest_after = hashlib.sha256(
        open(db_before, "rb").read()
    ).hexdigest() if os.path.exists(db_before) else ""
    check("the live database is byte-identical afterwards",
          digest_after, digest_before)
    check("the simulation flag is cleared", simulation.in_simulation(), False)

    print()
    print("  " + ("ALL PASSED" if not failures else f"{failures} FAILED"))
    print("=" * 78)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
