"""LINKS, HEADINGS AND LAYOUTS, on a test day and in a simulation. `python verify_layouts.py`

Drives the ONE sender (`_send_drip_message`) through "make it Tuesday" and then
"simulate tuesday", against a fake channel that COLLECTS, on a throwaway
*_test.db. The model composes live where it normally would (R6, R11); R1 and R4
are verbatim. The queue is stubbed — what is under test is what the sender
makes of it:

  (i)   deliverables in the three-line layout with masked links;
  (ii)  an R1 main post: one story per bullet, no topic tags, no closing line;
        and a quiet sweep posting exactly its one line;
  (iii) a breaking post with its heading (the hourly check, search stubbed);
  (iv)  an R6 message in the new plain wording;
  (v)   every message opens with its bold heading;
  (vi)  no bare url anywhere in what was sent;
  (vii) "simulate tuesday" produces the same layouts.
"""
import asyncio
import logging
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime

TMP = tempfile.mkdtemp(prefix="saley-verify-layouts-")
os.environ["DB_PATH"] = os.path.join(TMP, "sales_bot_test.db")

import config  # noqa: E402

# THIS SCRIPT CHECKS THE SERVER-SIDE SEARCH PATH (SEARCH_BACKEND=anthropic), with
# the search itself stubbed. The default path — search outside the model, the
# feeds, the light model — is verify_search_backend.py's to check.
config.SEARCH_BACKEND = "anthropic"

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
import drip  # noqa: E402
import links  # noqa: E402
import news  # noqa: E402
import nextaction  # noqa: E402
import rules as rules_mod  # noqa: E402
import simulation  # noqa: E402
import tone  # noqa: E402
from bot import SalesBot  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
for _noisy in ("httpx", "httpcore", "anthropic", "discord", "gtm_sheet", "mapping_sheet"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

# THE FIRST VARIANT OF EVERY LINE, so an exact sentence can be asserted.
tone.pin(0)

failures = 0
POSTED: list = []


def check(name, got, want=True):
    global failures
    ok = (got == want)
    failures += 0 if ok else 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f": got {got!r}, want {want!r}"))


def show(text):
    for line in str(text).splitlines():
        print("   | " + line)


class FakeChannel:
    id = 4242
    name = "sales-test"

    async def send(self, body, **kw):
        POSTED.append(body)
        return type("M", (), {"id": 5000 + len(POSTED), "channel": self})()


class FakeAuthor:
    id = 111
    display_name = "Vaishnavi"
    name = "vaishnavi"
    bot = False


class Cmd:
    id = 999
    channel = FakeChannel()
    author = FakeAuthor()
    reference = None

    def __init__(self, content):
        self.content = content

    async def reply(self, body, **kw):
        POSTED.append(body)
        return type("S", (), {"id": 7000 + len(POSTED), "channel": FakeChannel()})()


STORIES = [
    {"headline": "Wispr Flow raises $30M Series B", "what": "to build voice dictation for teams",
     "url": "https://techcrunch.com/2026/09/29/wispr-flow-series-b/", "topic": "RLHF"},
    {"headline": "ElevenLabs hires an evals lead", "what": "a new role in its research team",
     "url": "https://www.theinformation.com/articles/elevenlabs-evals", "topic": "evals"},
]


def build_messages(day, *, quiet_news=False):
    R = rules_mod.by_id
    items = []
    body = (news.quiet_line(day) if quiet_news
            else news.render(STORIES, mode=news.MODE_MAIN))
    items.append(nextaction._item(
        rule=R("R1"), trigger=nextaction.R_AI_NEWS, today=day, due=day, why="R1",
        text=body, extra={"research": body, "sources": [] if quiet_news else [
            {"url": s["url"], "title": s["headline"]} for s in STORIES]}))
    for name, team, due_in, link, remarks in (
        ("Pulse Product Overview Document", "Sales", -3,
         "https://docs.google.com/document/d/1AbCdEf/edit", ""),
        ("Legal compliance checklist", "", 2, "", "waiting on the lawyer"),
    ):
        due = dl.add_working_days(day, due_in) if due_in > 0 else day.fromordinal(
            day.toordinal() + due_in)
        items.append(nextaction._item(
            rule=R("R4"), trigger=nextaction.R_DELIVERABLES, today=day, due=due,
            why="R4", text=name, owner=team or config.DELIVERABLE_DEFAULT_OWNER,
            company=name, extra={
                "item": name, "deliverable": name, "team": team,
                "deadline": dl.iso(due),
                "deadline_pretty": f"{due.strftime('%a')} {due.day} {due.strftime('%b')}",
                "days_left": (due - day).days, "remarks": remarks, "link": link,
                "is_p1": True}))
    items.append(nextaction._item(
        rule=R("R6"), trigger=nextaction.R_LI_NO_DM, today=day, due=day, why="R6",
        text="Acme AI", owner="Vaishnavi", company="Acme AI", poc="Ada Lovelace",
        contact_key="acme|ada"))
    items.append(nextaction._item(
        rule=R("R11"), trigger=nextaction.R_NEW_COMPANY, today=day, due=day,
        why="R11", text="Shunya Labs", company="Shunya Labs"))
    msgs = []
    for i, g in enumerate(drip.group(items), 1):
        at = datetime(day.year, day.month, day.day, 14, 0, tzinfo=dl.IST)
        msgs.append({**g, "slot": i, "stage": drip.STAGE_NUDGE, "stage_why": "",
                     "send_at": at, "send_at_hhmm": "14:00"})
    return msgs


def assert_layouts(bodies, label):
    by_head = {}
    for b in bodies:
        first = re.sub(r"^\[TEST\]\s*", "", str(b).splitlines()[0])
        by_head.setdefault(first, str(b))
    heads = list(by_head)
    print(f"   headings seen ({label}):", heads)
    check(f"{label}: every message opens with a bold heading",
          all(re.match(r"^\*\*[^*]+\*\*$", h) for h in heads))
    news_body = next((b for h, b in by_head.items() if h.startswith("**AI News, ")), "")
    check(f"{label}: R1 heading carries the day", bool(news_body))
    # 8 OCT (docs/plans/NEWS-OCT8.md): "- **Headline** ([Outlet](<url>))", a blank line between stories.
    bullets = [l for l in news_body.splitlines() if l.startswith("- ")]
    check(f"{label}: one story per bullet", len(bullets), 2)
    check(f"{label}: no topic tags", any(re.match(r"- \[[A-Za-z]+\] ", l) for l in bullets),
          False)
    check(f"{label}: headline — what. [site](<url>)",
          bullets[:1] == ["- **Wispr Flow raises $30M Series B** ([techcrunch.com]"
                          "(<https://techcrunch.com/2026/09/29/wispr-flow-series-b/>))"])
    check(f"{label}: no closing line after the stories",
          news_body.rstrip().splitlines()[-1].startswith("- "))
    deliv = next((b for h, b in by_head.items() if h == "**This week's deliverables**"),
                 "")
    lines = deliv.splitlines()
    check(f"{label}: deliverable point, line 1", "1. Pulse Product Overview Document" in lines)
    check(f"{label}: line 2 — the due date, with the days overdue",
          any(re.fullmatch(r"   Due: \w{3} \d{1,2} \w{3} · 3 days overdue", l)
              for l in lines))
    check(f"{label}: no overdue note when not past due",
          any(re.fullmatch(r"   Due: \w{3} \d{1,2} \w{3}", l) for l in lines))
    check(f"{label}: no remarks", "waiting on the lawyer" in deliv, False)
    check(f"{label}: a Team line and a Due line under each title (S3)",
          (len([l for l in lines if l.startswith("   Team: ")]),
           len([l for l in lines if l.startswith("   Due: ")])), (2, 2))
    check(f"{label}: the plain close",
          lines[-1] == drip.DELIVERABLES_CLOSE)
    r6 = next((b for h, b in by_head.items() if h == "**LinkedIn connected, no DM yet**"), "")
    check(f"{label}: R6 has its heading", bool(r6))
    check(f"{label}: R11 has its heading", "**New in the pipeline**" in heads)
    stray = [u for b in bodies for u in links.bare_urls(b)]
    check(f"{label}: no bare url anywhere", stray, [])
    return by_head


async def main():
    bot = SalesBot()
    channel = FakeChannel()

    async def fake_queue(*, today):
        return {"actions": [], "rules_run": []}

    state = {"quiet": False}

    async def fake_plan(*, today, already, queue=None, only_rule=""):
        return {"messages": build_messages(today, quiet_news=state["quiet"]),
                "rolled": [], "held": []}

    bot._run_next_actions = fake_queue
    bot._plan_drip = fake_plan
    bot._sweep_proposals = lambda **kw: asyncio.sleep(0)
    bot._maybe_breaking_news_real = bot._maybe_breaking_news

    print("(iv) the R6 template, in plain words (the fallback, before the model)")
    r6 = [m for m in build_messages(dl.today_ist()) if m["type"] == nextaction.R_LI_NO_DM][0]
    fb = drip.compose_fallback(r6)
    show(fb)
    check("R6 fallback is the plain wording",
          "accepted your connection a few days ago but there's no DM yet. Want to "
          "send one this week? If you already have, just tell me." in fb)

    print('\n"make it Tuesday" — the real sender, live composition where it applies')
    POSTED.clear()
    await bot._handle_test_command(Cmd("make it Tuesday"), "make it Tuesday")
    check("the clock is a Tuesday", dl.today_ist().weekday(), 1)
    for b in POSTED:
        show(b)
        print("   |")
    # THE ONE LINE THAT IS NOT A MESSAGE: where the clock stands, and the real
    # date the day name was counted from.
    confirm = [b for b in POSTED if "the real date is" in str(b)]
    check("the day is confirmed, with the real date", len(confirm), 1)
    check("...and it is this REAL week's Tuesday",
          dl.today_ist(), simulation.this_week_day(1))
    day_bodies = [b for b in POSTED if "the real date is" not in str(b)]
    assert_layouts(day_bodies, "test day")
    r6_sent = next(b for b in day_bodies if "**LinkedIn connected, no DM yet**" in b)
    check("R6 as sent names the company", "Acme AI" in r6_sent)

    print("\n(ii) a quiet main sweep posts exactly its one line")
    msg = [m for m in build_messages(dl.today_ist(), quiet_news=True)
           if m["type"] == nextaction.R_AI_NEWS][0]
    msg["slot"] = 50
    POSTED.clear()
    await bot._send_drip_message(channel, msg, marker=dl.iso(dl.today_ist()),
                                 channel_id=4242)
    show(POSTED[0] if POSTED else "(nothing)")
    got = [l for l in str(POSTED[0]).splitlines()] if POSTED else []
    day = dl.today_ist()
    check("heading, tags line, one line — nothing else",
          (re.sub(r"^\[TEST\]\s*", "", got[0]) if got else "", got[-1] if got else ""),
          (f"**AI News, {day.strftime('%a')} {day.day} {day.strftime('%b')}**",
           news.quiet_line(day)))
    check("at most three lines (heading, tags, the line)", len(got) <= 3)

    print("\n(iii) a breaking post — the hourly check, search stubbed to one big story")

    async def stub_search(**kw):
        return {"ok": True, "searches": 1, "errors": [], "note": "", "sources": [],
                "pool": [], "citations": [],
                "text": ("STORY | evals | OpenAI releases a new evals suite | a public "
                         "benchmark for agents | https://openai.com/index/evals-suite | 5\n")}
    real_search = bot.llm.web_research
    bot.llm.web_research = stub_search
    POSTED.clear()
    try:
        await bot._maybe_breaking_news(force=True, channel=channel)
    finally:
        bot.llm.web_research = real_search
    show(POSTED[0] if POSTED else "(nothing posted)")
    b = str(POSTED[0]) if POSTED else ""
    check("breaking opens with its heading",
          re.sub(r"^\[TEST\]\s*", "", b.splitlines()[0]).startswith("**Breaking AI News, ")
          if b else False)
    check("the story is a bullet with a masked link",
          "- **OpenAI releases a new evals suite** "
          "([openai.com](<https://openai.com/index/evals-suite>))" in b)
    check("never a paragraph: every line after the heading is a bullet",
          all(l.startswith("- ") for l in b.splitlines()[1:] if l.strip()))
    check("no bare url", links.bare_urls(b), [])

    print('\n(vii) "simulate tuesday" — the same layouts')
    POSTED.clear()
    parsed = simulation.parse("simulate tuesday")
    await bot._run_simulation(Cmd("simulate tuesday"), parsed)
    for x in POSTED:
        show(x)
        print("   |")
    assert_layouts(list(POSTED), "simulation")


try:
    asyncio.run(main())
finally:
    config.SALES_TEST_MODE = False
    clock.forget()
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
sys.exit(1 if failures else 0)
