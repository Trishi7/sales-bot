"""MODEL vs TEMPLATE ON A REAL TEST DAY, AND WHAT THE MESSAGES SOUND LIKE.

    python verify_voice.py                       "make it monday", "make it tuesday"
    python verify_voice.py "make it 2026-09-28"  any test commands, in order
    python verify_voice.py --offline             no model: the checks that need none

The REAL test day (`_handle_test_command` -> `_live_test_day`), the REAL sheet
(read-only), the REAL rules, the REAL composer and the REAL model — against a
THROWAWAY COPY of the test database and a recording channel. Nothing reaches
Discord, the sheet is never written (SHEET_WRITES_ENABLED is forced off and the
events run, which appends rows, is skipped), and the audit lines go to a temp
STATE_DIR so the real audit log's MODEL/TEMPLATE history is not polluted.

Stubbed, because they need Discord or spend search budget: the leave read, the
channel-history evidence search, the per-row web research (items go out
un-researched, with no note) and the hourly breaking-news check. The news
sweep (feeds + the light model, no search) runs for real.

It prints, per command: the date the command resolved to, every post verbatim,
and whether each drip message was composed by the MODEL or fell back to the
TEMPLATE — with the reason, and any retry, from the log. Then the checks that
need no sheet: the banned phrases, a forced tone failure (one retry before any
template), and three quiet-news days in a row.

PART A runs first, on the same throwaway copy: the pretend clock is parked on
NEXT week's Monday and every weekday word is resolved, to show it counts from
the real date.
"""
import asyncio
import json
import logging
import os
import re
import shutil
import sys
import tempfile
from datetime import date, timedelta
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OFFLINE = "--offline" in sys.argv
COMMANDS = [a for a in sys.argv[1:] if not a.startswith("--")] or [
    "make it monday", "make it tuesday"]

TMP = tempfile.mkdtemp(prefix="saley-voice-")
SOURCE_DB = os.path.join(HERE, "sales_bot_test.db")
COPY_DB = os.path.join(TMP, "voice_test.db")
for suffix in ("", "-wal", "-shm"):
    if os.path.exists(SOURCE_DB + suffix):
        shutil.copy2(SOURCE_DB + suffix, COPY_DB + suffix)
os.environ["DB_PATH"] = COPY_DB
os.environ["STATE_DIR"] = os.path.join(TMP, "state")

import config  # noqa: E402

config.DB_PATH = COPY_DB
config.STATE_DIR = os.environ["STATE_DIR"]
config.SALES_TEST_MODE = True
config.SHEET_WRITES_ENABLED = False
config.TEST_POST_GAP_SECONDS = 0
config.SIMULATION_FAST_GAP_SECONDS = 0
config.DRIP_LLM_COMPOSE = not OFFLINE

import clock  # noqa: E402
import deadlines as dl  # noqa: E402
import drip  # noqa: E402
import leave  # noqa: E402
import llm as llm_mod  # noqa: E402
import news  # noqa: E402
import nextaction  # noqa: E402
import simulation  # noqa: E402
import tone  # noqa: E402

failures = 0
POSTED: list = []
LINES: list = []
SENT: list = []          # (date, slot, type, [the posts that slot produced])

# The phrases the proactive voice must never use (persona.PROACTIVE_VOICE).
BANNED = ("nothing to act on", "quiet cycle", "worth flagging", "as per",
          "kindly", "please note")


def check(name, got, want=True):
    global failures
    ok = (got == want)
    failures += 0 if ok else 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}"
          + ("" if ok else f": got {got!r}, want {want!r}"))


class Capture(logging.Handler):
    def emit(self, record):
        try:
            LINES.append(record.getMessage())
        except Exception:
            pass


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
    id = int(config.SALES_TEST_CHANNEL_ID or 4242)
    name = "sales-test"
    guild = SimpleNamespace(id=1)

    async def send(self, body):
        POSTED.append(str(body))
        return SimpleNamespace(id=len(POSTED), jump_url="https://discord/x")

    def typing(self):
        return FakeTyping()


CHANNEL = FakeChannel()


class FakeAuthor:
    id = int((list(config.SALES_APPROVER_IDS or []) or [111])[0])
    display_name = "verify_voice"
    name = "verify_voice"
    bot = False


class FakeMessage:
    id = 999
    channel = CHANNEL
    author = FakeAuthor()
    content = ""

    async def reply(self, body, mention_author=False):
        POSTED.append("REPLY: " + str(body))
        return SimpleNamespace(id=len(POSTED), jump_url="https://discord/x")


def show(bodies):
    for b in bodies:
        print("   " + "-" * 72)
        for sub in b.splitlines():
            print("   | " + sub)
    if bodies:
        print("   " + "-" * 72)


def audit_rows() -> list:
    path = os.path.join(config.STATE_DIR, "audit.jsonl")
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def composed_by(rows: list) -> dict:
    """{"MODEL": n, "TEMPLATE": n, "VERBATIM": n} for a list of drip_message rows.

    VERBATIM types (the news, the deliverables) are rendered in code and never
    shown to the composer, so they are counted apart: they are not fallbacks.
    """
    out = {"MODEL": 0, "TEMPLATE": 0, "VERBATIM": 0}
    for r in rows:
        if r.get("action_type") in drip.VERBATIM_TYPES:
            out["VERBATIM"] += 1
        elif r.get("composed_by_model"):
            out["MODEL"] += 1
        else:
            out["TEMPLATE"] += 1
    return out


async def run_days(bot) -> list:
    """Run each command through the real test path. The composed bodies."""
    composed: list = []
    totals = {"MODEL": 0, "TEMPLATE": 0, "VERBATIM": 0}
    for cmd in COMMANDS:
        seen_rows = len([r for r in audit_rows() if r.get("event") == "drip_message"])
        seen_lines = len(LINES)
        POSTED.clear()
        await bot._handle_test_command(FakeMessage(), cmd)
        day = dl.today_ist()
        print(f'\n"{cmd}" -> {dl.iso(day)} ({day.strftime("%A")}); '
              f"real date {dl.iso(dl.real_today_ist())}")
        show(POSTED)
        rows = [r for r in audit_rows() if r.get("event") == "drip_message"][seen_rows:]
        got = composed_by(rows)
        for k, v in got.items():
            totals[k] += v
        for r in rows:
            how = ("VERBATIM" if r.get("action_type") in drip.VERBATIM_TYPES
                   else "MODEL" if r.get("composed_by_model") else "TEMPLATE")
            extra = ""
            if r.get("compose_retries"):
                extra += f" retries={r['compose_retries']}"
            if r.get("fallback_reason"):
                extra += f" reason={r['fallback_reason']!r}"
            print(f"   slot {r.get('slot')}: {r.get('action_type')} x {r.get('owner')} "
                  f"[{r.get('stage')}] -> {how}{extra}")
        for l in LINES[seen_lines:]:
            if l.startswith("[llm.proactive]") and ("reject" in l or "retry" in l
                                                    or "raised" in l
                                                    or "soft failure" in l):
                print("   log: " + l[:400])
            elif l.startswith("[test-cmd]"):
                print("   log: " + l)
        print(f"   {cmd}: MODEL {got['MODEL']} / TEMPLATE {got['TEMPLATE']} "
              f"(+{got['VERBATIM']} posted verbatim, never composed)")
        by_slot = {(d, slot): posts for d, slot, _t, posts in SENT}
        for r in rows:
            if r.get("composed_by_model"):
                composed.append(chr(10).join(
                    by_slot.get((r.get("date"), r.get("slot")), [])))
    n = totals["MODEL"] + totals["TEMPLATE"]
    print(f"\nRATIO over {len(COMMANDS)} test day(s): MODEL {totals['MODEL']} / "
          f"TEMPLATE {totals['TEMPLATE']}"
          + (f" — {100 * totals['MODEL'] // n}% composed by the model" if n else "")
          + f" (+{totals['VERBATIM']} verbatim)")
    return composed


def r6_message(day: date) -> dict:
    """An R6 group of two companies and three people — three facts, no points."""
    kind = nextaction.R_LI_NO_DM
    band = nextaction.RULE_BANDS[kind]

    def item(company, poc, row):
        return {
            "rule": kind, "rule_id": "R6", "type": kind,
            "rule_name": nextaction.TYPE_LABELS[kind],
            "label": nextaction.TYPE_LABELS[kind], "owner": "Vaishnavi",
            "priority": band, "priority_label": nextaction.BAND_LABELS[band],
            "due_date": day, "due_iso": dl.iso(day), "overdue_days": 2,
            "company": company, "poc": poc, "poc_designation": "", "sheet_row": row,
            "row_key": f"{company.lower()}|{poc.lower()}",
            "contact_key": f"{company.lower()}|{poc.lower()}",
            "max_items_per_post": config.DRIP_MAX_ITEMS_PER_POST,
            "counts_toward_cap": True, "destination": "channel", "web_pending": False,
            "why": f"{poc} at {company} accepted the connection; no DM logged",
            "text": f"{company} · {poc} — connected, no DM",
            "key": f"R6:{company.lower()}|{poc.lower()}",
        }

    planned = drip.plan([item("PolyAI", "Nikola", 5), item("PolyAI", "Tsung", 6),
                         item("Agoda", "Idan", 7)], day=day)
    return planned["messages"][0]


class ScriptedMessages:
    """A model that answers from a script, one reply per call."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts: list = []

    def create(self, **kw):
        self.prompts.append(str(kw.get("messages", [{}])[-1].get("content", "")))
        text = self.replies.pop(0) if self.replies else "ok."
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=1, output_tokens=1))


async def forced_tone_failure():
    print("\n(iii) a forced tone failure: one retry before any template")
    if not hasattr(llm_mod, "proactive_verdict"):
        print("   (this build has no retry — a soft failure goes straight to the "
              "template)")
    msg = r6_message(date(2026, 9, 29))
    fallback = drip.compose_fallback(msg, address="Vaishnavi")
    seven = ("Vaishnavi, quick one. PolyAI connected. Agoda connected too. Nobody has "
             "sent a DM. It has been two days. Want to send one? Say skip if not.")
    good = ("Vaishnavi, PolyAI and Agoda are connected but nobody's sent a DM yet. "
            "Worth one this week? Say skip if not.")
    config.DRIP_LLM_COMPOSE = True
    for label, script, want_model in (
            ("too long, then fine", [seven, good], True),
            ("too long twice", [seven, seven], False)):
        engine = llm_mod.LLM("sk-fake-not-used", config.MODEL)
        fake = ScriptedMessages(script)
        engine._client = SimpleNamespace(messages=fake)
        start = len(LINES)
        got = await engine.proactive_message(
            prompt=drip.compose_prompt(msg, address="Vaishnavi"), fallback=fallback,
            facts=drip.fact_count(msg), required_lines=drip.required_lines(msg))
        text, used = got[0], got[1]
        print(f"   -- {label}: {len(fake.prompts)} model call(s), sent "
              f"{'the MODEL text' if used else 'the TEMPLATE'}")
        for l in LINES[start:]:
            if l.startswith("[llm.proactive]"):
                print("   log: " + l[:300])
        print("   | " + text.replace("\n", "\n   | "))
        check(f"{label}: two model calls (the compose and ONE retry)",
              len(fake.prompts), 2)
        check(f"{label}: the retry names the complaint",
              len(fake.prompts) > 1 and "7 sentences" in fake.prompts[-1])
        check(f"{label}: {'the model text' if want_model else 'the template'} went out",
              used, want_model)
    config.DRIP_LLM_COMPOSE = not OFFLINE


def quiet_news_days():
    print("\n(iv) three quiet-news days in a row use three different lines")
    if not hasattr(news, "quiet_line"):
        print("   (this build has one fixed line: " + repr(news.QUIET_MAIN) + ")")
        check("three different lines", 1, 3)
        return
    # SENDING DAYS ONLY: Monday to Friday, then the Monday and Tuesday after.
    days = [date(2026, 9, 28) + timedelta(days=i) for i in (0, 1, 2, 3, 4, 7, 8)]
    lines = [news.quiet_line(d) for d in days]
    for d, l in zip(days, lines):
        print(f"   {drip.heading('R1', day=d)}  {l}")
    for i in range(len(days) - 2):
        check(f"{days[i].strftime('%a %d')} to {days[i + 2].strftime('%a %d')}: "
              "three different",
              len(set(lines[i:i + 3])), 3)
    check("the same day always gets the same line (a restart does not change it)",
          news.quiet_line(days[0]), lines[0])
    check("none uses a banned phrase",
          [p for l in lines for p in BANNED if p in l.lower()], [])


def part_a():
    """Weekdays resolve from the REAL date, with the pretend clock parked on
    next week's Monday. The real `clock`, on the throwaway copy."""
    print()
    print("PART A — weekdays resolve from the real date")
    real = dl.real_today_ist()
    this_monday = real - timedelta(days=real.weekday())
    parked = this_monday + timedelta(days=7)
    ok, _line = clock.set_day(parked, by="verify_voice")
    print(f"   real date {dl.iso(real)} ({real.strftime('%a')}); pretend clock parked "
          f"on {dl.iso(dl.today_ist())}")
    if hasattr(simulation, "dates_line"):
        print("   the line boot and every test command log: " + simulation.dates_line())
    check("the pretend clock is parked on next Monday", (ok, dl.today_ist()),
          (True, parked))

    def made(cmd):
        got = simulation.parse_test_command(cmd)["date"]
        print(f"   {cmd!r:24} -> {dl.iso(got)}")
        return got

    check("make it monday -> this real week's Monday", made("make it monday"),
          this_monday)
    check("make it tuesday", made("make it tuesday"), this_monday + timedelta(days=1))
    check("make it next monday", made("make it next monday"),
          this_monday + timedelta(days=7))
    check("make it today -> the real date", made("make it today"), real)
    check("make it tomorrow -> the real tomorrow", made("make it tomorrow"),
          real + timedelta(days=1))
    week = simulation.week_of(simulation.parse("simulate week")["date"])
    print(f"   'simulate week'          -> {dl.iso(week[0])} to {dl.iso(week[-1])}")
    check("simulate week -> the real week, Monday to Sunday", (week[0], week[-1]),
          (this_monday, this_monday + timedelta(days=6)))
    check("simulate monday", simulation.parse("simulate monday")["date"], this_monday)

    # "next day" and "back to today" keep working from the pretend date.
    clock.next_day(by="verify_voice")
    print(f"   'next day'               -> {dl.iso(dl.today_ist())} (from the pretend date)")
    check("next day moves on from the PRETEND date", dl.today_ist(),
          parked + timedelta(days=1))
    clock.set_day(this_monday, by="verify_voice")
    if hasattr(clock, "describe_with_real"):
        print("   the reply when a day is named: " + clock.describe_with_real())
        check("the reply names the test day AND the real date",
              ("(test time)" in clock.describe_with_real(),
               clock.describe_with_real().endswith(
                   f"the real date is {clock.real_date_label()}.")), (True, True))
    clock.back_to_today(by="verify_voice")
    print(f"   'back to today'          -> {dl.iso(dl.today_ist())}")
    check("back to today -> the real date", dl.today_ist(), real)
    # Parked again, so the test days below start from the awkward place.
    clock.set_day(parked, by="verify_voice")


async def main():
    print("=" * 78)
    print(f"real date {dl.iso(dl.real_today_ist())} | pretend date at start: "
          f"{dl.iso(clock.status()['pretend_date']) if clock.pretending() else 'none'} "
          f"| tone: {tone.describe()}")
    print("=" * 78)

    part_a()

    composed: list = []
    if OFFLINE:
        print("\n--offline: the test days are skipped (they need the sheet and the model)")
    else:
        from bot import SalesBot
        bot = SalesBot()

        async def news_only(items, *, today):
            pending = [i for i in (items or []) if i.get("web_pending")]
            try:
                await bot._news_run(pending, today=today)
            except Exception:
                logging.getLogger("verify").exception("the news run failed")
            for i in pending:
                if i.get("web_pending"):
                    bot._mark_unresearched(i, "", still_pending=False)
            return items

        async def nobody_away(*_a, **_k):
            return {}

        async def nothing(*_a, **_k):
            return None

        real_send = bot._send_drip_message

        async def recording_send(channel, message, **kw):
            start = len(POSTED)
            await real_send(channel, message, **kw)
            SENT.append((kw.get("marker"), message.get("slot"), message.get("type"),
                         list(POSTED[start:])))

        bot._send_drip_message = recording_send
        bot._research_uncached = news_only
        bot._convert_if_already_done = nothing
        bot._maybe_breaking_news = nothing
        leave.who_is_away = nobody_away

        composed = await run_days(bot)

        print(f"\n(ii) the {len(composed)} model-composed message(s) and the banned phrases")
        hits = [(p, b[:60]) for b in composed for p in BANNED if p in b.lower()]
        check("no composed message contains a banned phrase", hits, [])

    await forced_tone_failure()
    quiet_news_days()


try:
    asyncio.run(main())
finally:
    clock.forget()
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
sys.exit(1 if failures else 0)
