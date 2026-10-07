"""R1, R2 AND R3's WEB HALF, driven end to end. Sends nothing, writes nothing.

    python verify_news_events.py              # stubbed search: fast, deterministic
    python verify_news_events.py --live       # real searches, real budget

Exercises `_news_run` and `_events_run` against a throwaway *_test.db and a fake
sheet, with `llm.web_research` stubbed so the SHAPE of each run can be asserted
without spending the budget or depending on what happened in the news today. It
covers what the feature is supposed to guarantee:

  1. R1's MAIN SWEEP: one lean search on the topic list, every story tagged and
     linked, and NOT ONE read of the PoCs, the mapping, the pipeline or the
     departures list (those readers are booby-trapped here);
  3. R2: a company in today's posted news that is not in the Master Pipeline,
     screened against the use-case table, asking before it adds anything;
  4. R3: a discovery proposal and a registration-deadline backfill proposal,
     both proposing rather than writing, both on the lean prompt;
  5. the budget counter moving.

The hourly breaking checks, the no-repeats ledger across main and breaking
posts, the valve and the news_checks ledger are in verify_news_feed.py.

--live makes the real calls instead, to check the prompts actually produce the
STORY/EVENT/DEADLINE lines the parsers expect. It spends real searches.

Assertions about WHAT was found are marked `live_optional` and report as `n/a`
under --live rather than failing: what the world published this morning is not
a property of this code. What --live does assert is the SHAPE — that the
prompts come back as lines the parsers read, that every story and every
screened company carries a link, and that the budget ledger moves.
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

import os as _os  # noqa: E402  NFT2-1064 offline guard: canned source statuses, real Sheets/Drive calls blocked
import sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "tests"))
import offline_guard  # noqa: E402
if "--live" not in _sys.argv:
    GUARD = offline_guard.install_script()
import config  # noqa: E402

# THIS SCRIPT CHECKS THE SERVER-SIDE SEARCH PATH (SEARCH_BACKEND=anthropic), with
# the search itself stubbed. The default path — search outside the model, the
# feeds, the light model — is verify_search_backend.py's to check.
config.SEARCH_BACKEND = "anthropic"

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

import tone  # noqa: E402

# THE FIRST VARIANT OF EVERY LINE, so an exact sentence can be asserted.
tone.pin(0)

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


EVENT_ROWS = [
    {"event": "NeurIPS 2026", "event_date": "2026-12-01",
     "registration_deadline": "2026-10-01", "_row": 2, "link": "https://neurips.cc"},
    {"event": "Evals Day London", "event_date": "2026-10-20",
     "registration_deadline": "", "_row": 3, "link": "https://evalsday.io"},
]

# THE SHEETS R1 MUST NEVER READ. Each reader records the attempt; the main
# sweep scenario asserts the list stays empty.
FORBIDDEN_READS = []


def install_fakes(bot, *, reply_for):
    """Stub every I/O boundary except SQLite, which is real and throwaway."""
    async def rows(why):
        FORBIDDEN_READS.append(f"Outreach PoCs ({why})")
        return [], None

    async def known():
        return ["Acme AI", "Globex", "Initech"]

    async def tab_rows(kind, _for):
        return EVENT_ROWS if kind == gtm_sheet.EVENTS else []

    bot._active_rows_of_canonical_tab = rows
    bot._known_companies = known
    bot._rule_tab_rows = tab_rows

    import mapping_sheet

    def _mapping():
        FORBIDDEN_READS.append("researcher mapping")
        return []

    def _departures():
        FORBIDDEN_READS.append("departures list")
        return None

    mapping_sheet.MAPPING.researchers = _mapping
    mapping_sheet.MAPPING.departures = _departures

    # THE RECORDER GOES ON IN BOTH MODES, so an assertion about what was ASKED
    # can pass on a --live run where the product behaved perfectly.
    real = bot.llm

    class RecordingLLM:
        async def web_research(self, *, rule, prompt, max_uses=0, lean=False,
                           **_snippet_path):
            CALLS.append({"rule": rule, "prompt": prompt, "lean": lean})
            if LIVE:
                return await real.web_research(
                    rule=rule, prompt=prompt, max_uses=max_uses, lean=lean,
                )
            text = reply_for(rule, prompt)
            return {
                "ok": bool(text), "text": text or "", "sources": [],
                "searches": 1, "errors": [], "note": "" if text else "nothing came back",
            }

    bot.llm = RecordingLLM()


async def scenario_main(bot, today):
    print("1. R1's MAIN SWEEP — the topic feed, tagged and linked")

    def reply(rule, prompt):
        if rule == "R1":
            return (
                "STORY | evals | Lab releases open agent eval suite | a public "
                "benchmark for tool-using agents | https://techcrunch.com/evals | 4\n"
                "STORY | RLHF | Nebius raises $700m | to build RLHF and inference "
                "capacity | https://reuters.com/nebius | 5\n"
                "STORY | OTHER | Chipmaker posts record quarter | AI demand "
                "| https://reuters.com/chips | 3\n"
            )
        if rule == "R2":
            return ("SCREEN | Nebius | builds inference infrastructure | fits evals: "
                    "buys eval data for its hosted models | https://reuters.com/nebius\n"
                    "SKIP | EU AI Act phase two | regulation, not a specific company\n")
        return ""

    install_fakes(bot, reply_for=reply)
    CALLS.clear()
    FORBIDDEN_READS.clear()
    items = [item(nextaction.R_AI_NEWS, "R1"), item(nextaction.R_NEWS_SCREEN, "R2")]
    await bot._news_run(items, today=today)

    r1 = items[0]
    r1_calls = [c for c in CALLS if c["rule"] == "R1"]
    check("ONE search for the main sweep", len(r1_calls), 1)
    check("...on the lean prompt", bool(r1_calls and r1_calls[0]["lean"]))
    check("...covering the last 24 hours",
          bool(r1_calls) and "last 24 hours" in r1_calls[0]["prompt"])
    check("the topics are seeds, not limits",
          bool(r1_calls) and "not limited to these" in r1_calls[0]["prompt"])
    check("NO read of the PoCs, mapping, pipeline or departures", FORBIDDEN_READS, [])

    check("the item is no longer waiting on research", r1["web_pending"], False,
          live_optional=True)
    bullets = [l for l in r1["text"].splitlines() if l.startswith("• ")]
    check("every story is a bullet with a masked link and no topic tag",
          bool(bullets) and all("](<http" in l and not l.startswith("• [")
                                for l in bullets),
          live_optional=True)
    check("three stories", len(bullets), 3, live_optional=True)
    check("the sources are attached for the send path",
          len(r1["sources"]), len(bullets), live_optional=True)
    print("   " + "\n   ".join(r1["text"].splitlines()))

    print("\n3. R2 — a company in today's posted news that we do not track")
    r2 = items[1]
    check("the screen ran", r2["web_pending"], False)
    check("it names the company", "Nebius" in r2["text"], live_optional=True)
    check("with what they do and why, in words",
          "fits evals" in r2["text"] and "builds inference" in r2["text"],
          live_optional=True)
    check("the regulation story is not screened", "EU AI Act" in r2["text"], False)
    check("and its link", "<https://reuters.com/nebius>" in r2["text"],
          live_optional=True)
    check("it asks before adding anything", "without a yes" in r2["text"])
    r2_calls = [c for c in CALLS if c["rule"] == "R2"]
    check("R2 read today's posted stories", bool(r2_calls) and
          "Nebius raises $700m" in r2_calls[0]["prompt"], live_optional=True)
    check("...on the lean prompt, with what we sell in the question",
          bool(r2_calls) and r2_calls[0]["lean"]
          and "WHAT MEMBRANE SELLS" in r2_calls[0]["prompt"])
    if LIVE and r2.get("text"):
        bullets = [l for l in r2["text"].splitlines() if l.strip().startswith("•")]
        check("live: every screened company carries a link",
              all("<http" in b for b in bullets))
    print("   " + "\n   ".join(r2["text"].splitlines()))

    print("\n5. the budget moved")
    # THE LEDGER IS BANKED AGAINST THE REAL DAY, whatever date the run is
    # pretending: a search is real money on the day it is made.
    marker = dl.iso(dl.real_today_ist())
    spent = bot.db.web_searches_today(marker)
    check("the budget counter moved", spent >= 2)
    print(f"   {spent} search(es) banked; "
          f"{bot.db.web_search_budget_left(marker)} left of "
          f"{config.WEB_SEARCH_DAILY_BUDGET}")
    return items


async def scenario_events(bot, today):
    print("\n4. R3 — a discovery proposal and a deadline backfill")

    def reply(rule, prompt):
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
    check("both R3 searches used the lean prompt",
          [c["lean"] for c in CALLS if c["rule"].startswith("R3")], [True, True])
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
    # A FRESH DAY: `today`'s news is already in the research cache from
    # scenario 1, and a cache hit rightly needs no search at all. A PAST one —
    # a date after the real today is never researched at all ("No news yet").
    await bot._news_run(items, today=today - timedelta(days=4))
    check("search off is said plainly",
          "WEB_SEARCH_ENABLED is off" in items[0]["research_note"])
    check("...and the item keeps its place in the queue",
          items[0]["web_pending"])
    config.WEB_SEARCH_ENABLED = True

    # THE BUDGET IS THE REAL DAY'S, whatever date the run pretends.
    marker = dl.iso(dl.real_today_ist())
    bot.db.record_web_search(on_date=marker, rule_id="drain",
                             searches=config.WEB_SEARCH_DAILY_BUDGET, errors=0)
    items = [item(nextaction.R_AI_NEWS, "R1")]
    await bot._news_run(items, today=today - timedelta(days=5))
    check("a spent budget is said plainly",
          "budget is spent" in items[0]["research_note"])
    print(f"   note: {items[0]['research_note']}")
    with bot.db.conn() as c:
        c.execute("DELETE FROM web_search_usage WHERE rule_id = 'drain'")

    def fails(rule, prompt):
        return ""

    install_fakes(bot, reply_for=fails)
    items = [item(nextaction.R_AI_NEWS, "R1")]
    await bot._news_run(items, today=today - timedelta(days=6))
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
            "research": "• [RLHF] Wispr Flow raises $30m — Series B "
                        "<https://techcrunch.com/wispr>",
            "sources": [{"url": "https://techcrunch.com/wispr",
                         "title": "Wispr Flow raises $30m"}],
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

    await scenario_main(bot, today)
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
