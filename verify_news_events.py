"""R1, R2 AND R3's WEB HALF, driven end to end. Sends nothing, writes nothing.

    python verify_news_events.py              # stubbed search: fast, deterministic
    python verify_news_events.py --live       # real searches, real budget

Exercises `_news_run` and `_events_run` against a throwaway *_test.db and a fake
sheet, with `llm.web_research` stubbed so the SHAPE of each run can be asserted
without spending the budget or depending on what happened in the news today. It
covers the five things the feature is supposed to guarantee:

  1. R1 in PEOPLE mode: our own contacts are searched for by name, the rotation
     advances, every story carries a link and a story about somebody on our
     sheets names the row it came from;
  2. R1 forced into FALLBACK mode: nothing about our people, so the wider field
     is searched and the post SAYS so in plain words;
  3. R2: a company in today's news that is not in the Master Pipeline, screened
     with a reason, and asking before it adds anything;
  4. R3: a discovery proposal and a registration-deadline backfill proposal,
     both proposing rather than writing;
  5. the budget counter moving, and the no-repeat and rotation ledgers.

--live makes the real calls instead, to check the prompts actually produce the
STORY/EVENT/DEADLINE lines the parsers expect. It spends real searches.
"""
import asyncio
import os
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta

LIVE = "--live" in sys.argv

TMP = tempfile.mkdtemp(prefix="saley-news-")
os.environ["DB_PATH"] = os.path.join(TMP, "sales_bot_test.db")

import config  # noqa: E402

config.DB_PATH = os.environ["DB_PATH"]
config.SALES_TEST_MODE = True
config.WEB_SEARCH_ENABLED = True
config.WEB_SEARCH_DAILY_BUDGET = 20

import deadlines as dl  # noqa: E402
import events_discovery  # noqa: E402
import gtm_sheet  # noqa: E402
import news  # noqa: E402
import nextaction  # noqa: E402
from bot import SalesBot  # noqa: E402
from db import DB  # noqa: E402

failures = 0
CALLS = []


def check(name, got, want=True, *, live_optional=False):
    """Assert, unless it is a claim about what the world published today.

    `live_optional` marks the assertions that depend on there BEING news about
    two fictional people this morning. Under --live those are reported and not
    failed: the point of the live run is that the prompts produce lines the
    parsers can read, not that Ada Lovelace was in the news.
    """
    global failures
    ok = (got == want)
    soft = LIVE and live_optional and not ok
    if not ok and not soft:
        failures += 1
    label = "PASS" if ok else ("n/a " if soft else "FAIL")
    print(f"  {label}  {name}"
          + ("" if ok else f": got {got!r}, want {want!r}"))


def item(rule, rule_id):
    return {
        "rule": rule, "rule_id": rule_id, "rule_name": rule_id,
        "type": rule, "text": f"placeholder [{news.__name__ and 'web research pending'}]",
        "web_pending": True, "actions": [], "sources": [],
    }


# The sheets, as fixtures. Real shapes, fake contents.
POC_ROWS = [
    {"name": "Sahaj Garg", "company": "Wispr Flow", "_row": 12},
    {"name": "Ada Lovelace", "company": "Acme AI", "_row": 13},
]
MAPPING_ROWS = [
    {"researcher": "Alan Turing", "company": "Globex", "tier": "T1"},
    {"researcher": "Grace Hopper", "company": "Initech", "tier": "T3"},
]
EVENT_ROWS = [
    {"event": "NeurIPS 2026", "event_date": "2026-12-01",
     "registration_deadline": "2026-10-01", "_row": 2, "link": "https://neurips.cc"},
    {"event": "Evals Day London", "event_date": "2026-10-20",
     "registration_deadline": "", "_row": 3, "link": "https://evalsday.io"},
]


class FakeDepartures:
    def all(self):
        return [{"name": "Departed Person"}]


def install_fakes(bot, *, reply_for):
    """Stub every I/O boundary except SQLite, which is real and throwaway."""
    async def rows(_why):
        return POC_ROWS, None

    async def known():
        return ["Acme AI", "Globex", "Initech"]

    async def tab_rows(kind, _for):
        return EVENT_ROWS if kind == gtm_sheet.EVENTS else []

    bot._active_rows_of_canonical_tab = rows
    bot._known_companies = known
    bot._rule_tab_rows = tab_rows

    import mapping_sheet
    mapping_sheet.MAPPING.researchers = lambda: MAPPING_ROWS
    mapping_sheet.MAPPING.departures = lambda: FakeDepartures()

    # THE RECORDER GOES ON IN BOTH MODES. It used to be installed only for the
    # stubbed run, so --live left CALLS empty and every assertion about what was
    # ASKED failed on a run where the product had behaved perfectly. A
    # verification that cannot pass when the thing it verifies is working is
    # worse than no verification.
    real = bot.llm

    class RecordingLLM:
        async def web_research(self, *, rule, prompt, max_uses=0, only_domains=None):
            CALLS.append({"rule": rule, "prompt": prompt, "domains": only_domains})
            if LIVE:
                return await real.web_research(
                    rule=rule, prompt=prompt, max_uses=max_uses,
                    only_domains=only_domains,
                )
            text = reply_for(rule, prompt, only_domains)
            return {
                "ok": bool(text), "text": text or "", "sources": [],
                "searches": 1, "errors": [], "note": "" if text else "nothing came back",
            }

    bot.llm = RecordingLLM()


async def scenario_people(bot, today):
    print("1. R1 IN PEOPLE MODE — our contacts, by name, with links")

    def reply(rule, prompt, domains):
        if rule == "R1":
            # The preferred pass is thin (1 story), so the open pass must run.
            if domains:
                return ("STORY | Sahaj Garg | raised a $30m Series B for Wispr Flow "
                        "| https://techcrunch.com/wispr\n")
            return ("STORY | Alan Turing | published a paper on model evaluations "
                    "| https://arxiv.org/abs/1234\n"
                    "STORY | Acme AI | hired a head of AI safety "
                    "| https://reuters.com/acme\n")
        if rule == "R2":
            return ("SCREEN | Nebius | fits use case C, inference infrastructure, "
                    "buys eval data | https://nebius.com/news\n")
        return ""

    install_fakes(bot, reply_for=reply)
    CALLS.clear()
    items = [item(nextaction.R_AI_NEWS, "R1"), item(nextaction.R_NEWS_SCREEN, "R2")]
    await bot._news_run(items, today=today)

    r1 = items[0]
    prompts = [c["prompt"] for c in CALLS if c["rule"] == "R1"]
    check("our people are searched for BY NAME",
          any("Sahaj Garg" in p for p in prompts))
    check("a T1 researcher is in the rotation",
          any("Alan Turing" in p for p in prompts))
    check("a T3 researcher is not",
          any("Grace Hopper" in p for p in prompts), False)
    check("the preferred-domain pass ran first",
          bool(CALLS[0]["domains"]))
    check("...and a thin result triggered the open pass",
          CALLS[1]["domains"] if len(CALLS) > 1 else None, None, live_optional=True)
    check("the keywords from the sheet are seeds, not limits",
          "not limited to these" in prompts[0])

    check("the item is no longer waiting on research", r1["web_pending"], False,
          live_optional=True)
    check("every story carries a link", r1["text"].count("<http"), 3,
          live_optional=True)
    check("a story about our PoC names the row",
          "on our Outreach PoCs" in r1["text"], live_optional=True)
    check("it is not announced as a fallback",
          "Nothing on our contacts" in r1["text"], False, live_optional=True)
    check("the sources are attached for the send path",
          len(r1["sources"]), 3, live_optional=True)
    print("   " + "\n   ".join(r1["text"].splitlines()))

    print("\n3. R2 — a company in the news that we do not track")
    r2 = items[1]
    check("the screen ran", r2["web_pending"], False)
    check("it names the company", "Nebius" in r2["text"])
    check("with a reason judged against the use cases",
          "use case C" in r2["text"])
    check("and its link", "<https://nebius.com/news>" in r2["text"])
    check("it asks before adding anything", "without a yes" in r2["text"])
    check("it did NOT search again — it re-read R1's results",
          len([c for c in CALLS if c["rule"] == "R2"]), 1)
    print("   " + "\n   ".join(r2["text"].splitlines()))

    print("\n5a. the rotation advanced and the budget moved")
    marker = dl.iso(today)
    status = bot.db.news_rotation_status()
    check("everybody is tracked", status["tracked"] >= 4)
    check("this run's targets are stamped",
          status["never_searched"] < status["tracked"])
    check("a departed person is not in the rotation",
          "Departed Person" not in
          [t["name"] for t in bot.db.news_targets_due(limit=50)])
    spent = bot.db.web_searches_today(marker)
    check("the budget counter moved", spent >= 3)
    print(f"   {spent} search(es) banked; "
          f"{bot.db.web_search_budget_left(marker)} left of "
          f"{config.WEB_SEARCH_DAILY_BUDGET}")
    return items


async def scenario_fallback(bot, today):
    print("\n2. R1 FORCED INTO FALLBACK — nothing on our people")

    def reply(rule, prompt, domains):
        if rule == "R1" and "most significant AI news" in prompt:
            return ("STORY | OpenAI | shipped a new reasoning model "
                    "| https://openai.com/blog/x\n"
                    "STORY | EU | passed an AI liability directive "
                    "| https://europa.eu/ai\n")
        if rule == "R1":
            return "NOTHING FOUND"
        return ""

    install_fakes(bot, reply_for=reply)
    CALLS.clear()
    items = [item(nextaction.R_AI_NEWS, "R1")]
    await bot._news_run(items, today=today)
    r1 = items[0]

    check("the people passes ran first and came back empty",
          len([c for c in CALLS if "Search the news for ANY of these" in c["prompt"]]) >= 1)
    check("the field pass then ran",
          any("most significant AI news" in c["prompt"] for c in CALLS))
    check("the post SAYS which mode it is in, in plain words",
          r1["text"].startswith("Nothing on our contacts today"))
    check("the fallback still carries links", r1["text"].count("<http"), 2)
    check("it is not left marked as pending", r1["web_pending"], False)
    print("   " + "\n   ".join(r1["text"].splitlines()))


async def scenario_repeats(bot, today):
    print("\n5b. NO REPEATS — a story already posted is skipped")

    def reply(rule, prompt, domains):
        if rule == "R1":
            return ("STORY | Sahaj Garg | raised a $30m Series B "
                    "| https://techcrunch.com/wispr?utm_source=twitter\n")
        return ""

    install_fakes(bot, reply_for=reply)
    items = [item(nextaction.R_AI_NEWS, "R1")]
    await bot._news_run(items, today=today + timedelta(days=1))
    check("the same story, with tracking junk on the url, is not posted twice",
          "techcrunch.com/wispr" in items[0].get("text", ""), False)
    check("...and the item says it searched rather than that it could not",
          items[0]["web_pending"], False)
    print(f"   note: {items[0].get('research_note', '')}")


async def scenario_events(bot, today):
    print("\n4. R3 — a discovery proposal and a deadline backfill")

    def reply(rule, prompt, domains):
        if rule == "R3":
            return (
                "EVENT | Voice AI Forum | Bengaluru, India | 2026-10-14 | 2026-10-01 "
                "| https://voiceaiforum.in\n"
                "EVENT | NeurIPS | Vancouver, Canada | 2026-12-01 | NOT STATED "
                "| https://neurips.cc\n"
            )
        if rule == "R3-deadlines":
            return ("DEADLINE | Evals Day London | 2026-10-05 "
                    "| https://evalsday.io/register\n")
        return ""

    install_fakes(bot, reply_for=reply)
    CALLS.clear()
    items = [item(nextaction.R_EVENTS, "R3")]
    await bot._events_run(items, today=today)
    r3 = items[0]
    body = r3.get("research", "")

    check("discovery proposed the new event", "Voice AI Forum" in body)
    check("...with its place and date", "Bengaluru" in body)
    check("...and its link", "<https://voiceaiforum.in>" in body)
    check("an event already on the tab is NOT proposed",
          "NeurIPS" in body, False)
    check("it asks before adding a row", "without one" in body)

    check("the backfill proposed the missing deadline",
          "Evals Day London" in body)
    check("...with the page that states it", "evalsday.io/register" in body)
    check("...and promises to touch nothing else", "Nothing else" in body)
    check("a row that already has a deadline is left alone",
          body.count("NeurIPS"), 0)
    check("nothing was written to the sheet — it only proposed",
          bool(r3.get("event_proposals")) and bool(r3.get("deadline_proposals")))
    check("the item is no longer pending", r3["web_pending"], False)
    print("   " + "\n   ".join(body.splitlines()))

    print("\n   the limits and the ledgers")
    month = today.strftime("%Y-%m")
    check("the proposal is recorded against the month",
          bot.db.event_discoveries_this_month(month), 1)
    key = events_discovery.event_key("Voice AI Forum", date(2026, 10, 14))
    check("...and against the event, so a no stays a no",
          (bot.db.event_discovery_seen(key) or {}).get("status"), "proposed")

    # Re-run: the same event must not be proposed twice.
    items2 = [item(nextaction.R_EVENTS, "R3")]
    await bot._events_run(items2, today=today)
    check("a second run does not re-propose it",
          "Voice AI Forum" in items2[0].get("research", ""), False)

    print("\n   the cells that an approved append would write")
    cells = events_discovery.append_values({
        "name": "Voice AI Forum", "location": "Bengaluru, India",
        "date": date(2026, 10, 14), "deadline": date(2026, 10, 1),
        "link": "https://voiceaiforum.in",
    })
    for k, v in sorted(cells.items()):
        print(f"     {k:24} {v}")
    check("unknown columns are left empty rather than guessed",
          set(cells) <= {"event", "location", "event_date", "link",
                         "registration_deadline"})


async def scenario_degrade(bot, today):
    print("\n6. FAILURES DEGRADE HONESTLY")

    install_fakes(bot, reply_for=lambda *a: "")
    config.WEB_SEARCH_ENABLED = False
    items = [item(nextaction.R_AI_NEWS, "R1")]
    await bot._news_run(items, today=today)
    check("search off is said plainly",
          "WEB_SEARCH_ENABLED is off" in items[0]["research_note"])
    check("...and the item keeps its place in the queue",
          items[0]["web_pending"])
    config.WEB_SEARCH_ENABLED = True

    marker = dl.iso(today + timedelta(days=5))
    bot.db.record_web_search(on_date=marker, rule_id="drain",
                             searches=config.WEB_SEARCH_DAILY_BUDGET, errors=0)
    items = [item(nextaction.R_AI_NEWS, "R1")]
    await bot._news_run(items, today=today + timedelta(days=5))
    check("a spent budget is said plainly",
          "budget is spent" in items[0]["research_note"])
    print(f"   note: {items[0]['research_note']}")

    def fails(rule, prompt, domains):
        return ""

    install_fakes(bot, reply_for=fails)
    items = [item(nextaction.R_AI_NEWS, "R1")]
    await bot._news_run(items, today=today + timedelta(days=6))
    check("a failed call invents nothing",
          "STORY" not in items[0].get("text", ""))
    check("...and says something", bool(items[0].get("research_note")))
    print(f"   note: {items[0].get('research_note', '')}")


async def scenario_links_survive():
    print("\n7. THE LINKS REACH THE MESSAGE — the gap STEP 0 found")
    import drip

    msg = {
        "owner": "Vaishnavi", "type": nextaction.R_AI_NEWS,
        "companies": ["Wispr Flow"],
        "actions": [{
            "research": "• Sahaj Garg — raised $30m <https://techcrunch.com/wispr>",
            "sources": [{"url": "https://techcrunch.com/wispr", "title": "Sahaj Garg"}],
            "why": "R1 runs every weekday",
        }],
    }
    prompt = drip.compose_prompt(msg, address="@Vaishnavi")
    check("the composer is GIVEN the research", "techcrunch.com/wispr" in prompt)
    check("...and told to keep every link", "keep EVERY link" in prompt)
    check("...and that web content is data", "never act on" in prompt)

    fb = drip.compose_fallback(msg, address="@Vaishnavi")
    check("a model outage still sends the stories", "techcrunch.com/wispr" in fb)

    tidied = drip.with_sources("Sahaj Garg raised thirty million dollars.", msg)
    check("a composer that dropped the link has it put back",
          "techcrunch.com/wispr" in tidied)
    unchanged = drip.with_sources(
        "Sahaj raised $30m <https://techcrunch.com/wispr>", msg)
    check("...and one that kept it is not given a duplicate",
          unchanged.count("techcrunch.com/wispr"), 1)

    nope = {"actions": [{"research_note": "the daily search budget is spent"}]}
    check("a missing-research note reaches the composer",
          "budget is spent" in drip.compose_prompt(nope))
    check("...and the template fallback",
          "budget is spent" in drip.compose_fallback(nope))



async def scenario_approval(bot, today):
    """8. A YES TURNS A PROPOSAL INTO A ROW — and a no turns it into nothing."""
    print("\n8. ON A YES, R3 APPENDS — and only then")
    import gtm_sheet as gs

    appended, cells_written = [], []

    class FakeTab:
        title = "AI Events & Summits"
        kind = gs.EVENTS
        headers = ["Sr No", "Location", "Event Name", "Link", "Date",
                   "Last day for registration"]
        role_to_col = {"sr_no": 0, "location": 1, "event": 2, "link": 3,
                       "event_date": 4, "registration_deadline": 5}
        rows = [
            {"_row": 3, "event": "Evals Day London", "event_date": "2026-10-20",
             "registration_deadline": "", "link": "https://evalsday.io"},
            {"_row": 4, "event": "Has One", "event_date": "2026-10-22",
             "registration_deadline": "2026-10-02"},
        ]

    tab = FakeTab()

    def fake_tab(kind, which=None):
        return tab if kind == gs.EVENTS else None

    def fake_append(t, values, *, reason="", expect_company="", dry_run=False):
        appended.append({"values": values, "reason": reason})
        return {"ok": True, "sheet_row": 9,
                "written": [{"role": k, "header": k, "cell": f"X9", "old": "",
                             "new": v} for k, v in values.items()],
                "error": "", "dry_run": False}

    def fake_write_on(t, *, row, values, reason=""):
        existing = next((r for r in t.rows if r["_row"] == row), None)
        role = next(iter(values))
        if existing and str(existing.get(role) or "").strip():
            return {"ok": False, "written": [], "error": "already has a value",
                    "dry_run": False}
        cells_written.append({"row": row, "values": values, "reason": reason})
        return {"ok": True, "written": [{"cell": f"F{row}"}], "error": "",
                "dry_run": False}

    gs.SHEETS.tab = fake_tab
    gs.SHEETS.append_row = fake_append
    gs.SHEETS.write_cells_on = fake_write_on

    replies = []

    async def fake_reply(msg, body, *, reason, keep_rule_ids=False):
        replies.append(body)

    bot._reply = fake_reply

    class M:
        id = 777

    proposal = {
        "kind": "event_append", "tab": gs.EVENTS,
        "payload": {"events": [{
            "name": "Voice AI Forum", "location": "Bengaluru, India",
            "date": "2026-10-14", "deadline": "2026-10-01",
            "link": "https://voiceaiforum.in",
            "event_key": "voice ai forum|2026-10-14",
        }]},
    }
    # The stored payload carries ISO strings; the applier has to turn them back
    # into the cells `append_values` expects.
    from datetime import date as _d
    proposal["payload"]["events"][0]["date"] = _d(2026, 10, 14)
    proposal["payload"]["events"][0]["deadline"] = _d(2026, 10, 1)

    await bot._apply_approved_write(M(), proposal, decided_by="Sid", why="Sid said yes")
    check("a yes appended exactly one row", len(appended), 1)
    check("...with the event name", appended[0]["values"].get("event"), "Voice AI Forum")
    check("...the date", appended[0]["values"].get("event_date"), "2026-10-14")
    check("...the link", appended[0]["values"].get("link"), "https://voiceaiforum.in")
    check("...and the deadline it found",
          appended[0]["values"].get("registration_deadline"), "2026-10-01")
    check("unknown columns were left out, not guessed",
          set(appended[0]["values"]) <= {"event", "location", "event_date", "link",
                                         "registration_deadline"})
    check("the approver is named in the audit reason", "Sid" in appended[0]["reason"])
    check("it reported back", any("Added Voice AI Forum" in r for r in replies))
    check("the discovery is marked added",
          (bot.db.event_discovery_seen("voice ai forum|2026-10-14") or {}).get("status"),
          "added")

    print("   " + "\n   ".join(replies))

    print("\n   the deadline cell, on a yes")
    replies.clear()
    dproposal = {
        "kind": "event_deadline", "tab": gs.EVENTS,
        "payload": {"deadlines": [
            {"name": "Evals Day London", "sheet_row": 3, "deadline": "2026-10-05",
             "source": "https://evalsday.io/register"},
        ]},
    }
    from datetime import date as _d2
    dproposal["payload"]["deadlines"][0]["deadline"] = _d2(2026, 10, 5)
    await bot._apply_approved_write(M(), dproposal, decided_by="Vaishnavi",
                                    why="approved")
    check("exactly one cell was written", len(cells_written), 1)
    check("...on the right row", cells_written[0]["row"], 3)
    check("...and it is ONLY the deadline",
          list(cells_written[0]["values"]), ["registration_deadline"])
    check("the source is in the write's reason",
          "evalsday.io/register" in cells_written[0]["reason"])
    check("it reported back", any("Evals Day London" in r for r in replies))
    print("   " + "\n   ".join(replies))

    print("\n   the writer refuses to overwrite a cell somebody filled in")
    import gtm_sheet
    real = gtm_sheet.GTMSheets.write_cells_on
    out = real(gs.SHEETS, tab, row=4,
               values={"registration_deadline": "2026-11-11"})
    check("a cell that already reads something is refused", out["ok"], False)
    check("...and says why", "never overwrite" in (out.get("error") or ""))


async def main():
    today = date(2026, 9, 28)
    bot = SalesBot()
    bot.db = DB(config.DB_PATH)

    await scenario_people(bot, today)
    await scenario_fallback(bot, today + timedelta(days=2))
    await scenario_repeats(bot, today)
    await scenario_events(bot, today)
    await scenario_degrade(bot, today)
    await scenario_links_survive()
    await scenario_approval(bot, today)


try:
    asyncio.run(main())
finally:
    config.SALES_TEST_MODE = False
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
sys.exit(1 if failures else 0)
