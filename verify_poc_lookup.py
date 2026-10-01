"""R11 ASKS FIRST, FIND-PEOPLE, AND R2 IN WORDS — driven live. `python verify_poc_lookup.py`

LIVE: real model calls and real web searches (a handful, banked against a
throwaway *_test.db budget). Discord is a fake channel that COLLECTS; nothing is
sent and no sheet is read or written.

  (i)   the R11 message: two companies, one per line, no links, ends asking;
  (ii)  an approver's "yes" -> named people with urls, each checked against the
        pages the search actually returned (the raw results are printed);
  (iii) "find PoCs at Synthflow in the research team" through the engine;
  (iv)  an R2 screen skips a story that is not about one company, and logs it;
  (vi)  the same R11 flow on a test day ("make it Tuesday"), with "yes for Shunya".
"""
import asyncio
import logging
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timedelta

TMP = tempfile.mkdtemp(prefix="saley-verify-poc-")
os.environ["DB_PATH"] = os.path.join(TMP, "sales_bot_test.db")

import config  # noqa: E402

config.DB_PATH = os.environ["DB_PATH"]
config.SALES_TEST_MODE = True
config.SALES_TEST_CHANNEL_ID = 4242
config.SALES_CHANNEL_IDS = [4242]
config.SALES_CHANNEL_ID_SET = {4242}
config.TEAM_ROSTER_IDS = [111]
config.SALES_APPROVER_IDS = [111]
config.SALES_FINAL_SAY_ID = 111
config.SALES_DMS_ENABLED = False
config.TEST_POST_GAP_SECONDS = 0

import clock  # noqa: E402
import deadlines as dl  # noqa: E402
import drip  # noqa: E402
import nextaction  # noqa: E402
import rules as rules_mod  # noqa: E402
import websearch  # noqa: E402
from bot import SalesBot  # noqa: E402

failures = 0
POSTED: list = []
LOGS: list = []
SEARCHES: list = []


class _Grab(logging.Handler):
    def emit(self, record):
        LOGS.append(record.getMessage())


logging.basicConfig(level=logging.WARNING)
logging.getLogger().addHandler(_Grab(level=logging.INFO))
logging.getLogger().setLevel(logging.INFO)
for noisy in ("httpx", "httpcore", "anthropic", "discord"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


def check(name, got, want=True):
    global failures
    ok = (got == want)
    failures += 0 if ok else 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f": got {got!r}, want {want!r}"))


def show(text, pad="   | "):
    for line in str(text).splitlines():
        print(pad + line)


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


def reply_msg(content, parent_id):
    class Ref:
        message_id = parent_id

    class M:
        id = 9000 + len(POSTED)
        channel = FakeChannel()
        author = FakeAuthor()
        reference = Ref()

        async def reply(self, body, **kw):
            POSTED.append(body)
            return type("S", (), {"id": 6000 + len(POSTED), "channel": FakeChannel()})()

    M.content = content
    return M()


def r11_message(companies, *, day):
    rule = rules_mod.by_id("R11")
    first = dl.subtract_working_days(day, 1)
    items = nextaction._r_new_pipeline_company(rule, {
        "today": day,
        "new_companies": [{"company": c, "first_seen": dl.iso(first)} for c in companies],
    })
    g = drip.group(items)[0]
    at = datetime(day.year, day.month, day.day, 14, 0, tzinfo=dl.IST)
    return {**g, "slot": 1, "stage": drip.STAGE_NUDGE if hasattr(drip, "STAGE_NUDGE")
            else "nudge", "stage_why": "", "send_at": at, "send_at_hhmm": "14:00"}, items


def wrap_search(bot):
    real = bot.llm.web_research

    async def recorded(**kw):
        out = await real(**kw)
        SEARCHES.append({"rule": kw.get("rule"), "prompt": kw.get("prompt", ""), **out})
        return out
    bot.llm.web_research = recorded


def print_search(s):
    print(f"   search [{s['rule']}] billed={s.get('searches')} ok={s.get('ok')}")
    print("   raw model lines:")
    show("\n".join(l for l in (s.get("text") or "").splitlines()
                   if l.strip().upper().startswith(("PERSON", "NOBODY", "SCREEN", "SKIP")))
         or "(no PERSON/SCREEN lines)", pad="     > ")
    pool = s.get("pool") or []
    print(f"   pages the search returned ({len(pool)}):")
    for p in pool[:12]:
        print(f"     - {p.get('title','')[:70]} <{p.get('url')}>")
    if len(pool) > 12:
        print(f"     ...and {len(pool) - 12} more")


async def main():
    bot = SalesBot()
    wrap_search(bot)
    channel = FakeChannel()
    today = dl.today_ist()
    marker = dl.iso(today)

    # ------------------------------------------------------------- (i)
    print("(i) the R11 message — two new companies")
    message, items = r11_message(["Shunya Labs", "Synthflow AI"], day=today)
    check("R11 items no longer wait on web research",
          [i["web_pending"] for i in items], [False, False])
    check("R11 is not web-dependent", rules_mod.by_id("R11").needs_web, False)
    print("   deterministic fallback:")
    show(drip.compose_fallback(message))
    check("the fallback is exactly the agreed shape",
          drip.compose_fallback(message),
          "Hey team — two new companies landed in the pipeline:\n• Shunya Labs\n"
          "• Synthflow AI\nWant me to look for relevant PoCs for outreach? Say yes "
          "and I'll dig in.")
    one, _ = r11_message(["Shunya Labs"], day=today)
    check("singular for one company", drip.compose_fallback(one).splitlines()[0],
          "Hey team — a new company landed in the pipeline:")
    check("required lines carry the opener and the companies verbatim",
          drip.required_lines(message),
          ["Hey team — two new companies landed in the pipeline:",
           "• Shunya Labs", "• Synthflow AI"])

    POSTED.clear()
    await bot._send_drip_message(channel, message, marker=marker, channel_id=4242)
    sent_body = "\n".join(str(p) for p in POSTED)
    print("   what the channel got (composed live, through the real send path):")
    show(sent_body)
    check("one company per line",
          "\n• Shunya Labs\n• Synthflow AI\n" in sent_body)
    check("no links", re.search(r"https?://", sent_body) is None)
    check("ends with the question",
          sent_body.rstrip().endswith("?") or "Say yes" in sent_body.splitlines()[-1])
    row = bot.db.find_drip_by_message_id(str(5000 + 1))
    proposal = bot.db.open_proposal_for_message(str(5000 + 1))
    check("a poc_lookup proposal is keyed to that message",
          (proposal or {}).get("kind"), "poc_lookup")
    check("its payload is the company names",
          (proposal or {}).get("payload"), {"companies": ["Shunya Labs", "Synthflow AI"]})
    check("the drip send was recorded", bool(row))

    # ------------------------------------------------------------- (ii)
    print('\n(ii) Vaishnavi replies "yes" — named people, nothing invented')
    POSTED.clear()
    SEARCHES.clear()
    LOGS.clear()
    handled = await bot._maybe_vote_on_proposal(reply_msg("yes", 5001), "yes")
    check("the yes was handled", handled)
    check("one search per company", [s["rule"] for s in SEARCHES], ["R11", "R11"])
    check("each search capped at max_uses=2 (billed <= 2)",
          all(int(s.get("searches") or 0) <= 2 for s in SEARCHES))
    for s in SEARCHES:
        print_search(s)
    for line in LOGS:
        if line.startswith("[people]"):
            print("   log:", line[:220])
    print("   the replies:")
    for p in POSTED:
        show(p)
        print("   |")
    replies = [str(p) for p in POSTED]
    check("two replies, one per company", len(replies), 2)
    check("the Shunya reply leads with the company",
          replies[0].startswith(("Shunya Labs — people worth a look:",
                                 "I couldn't find named people for Shunya Labs")))
    all_ok = True
    for s, body in zip(SEARCHES, replies):
        ev = websearch.evidence_urls(s)
        for url in re.findall(r"<(https?://[^>]+)>", body):
            if websearch._url_norm(url) not in ev:
                all_ok = False
                print("   NOT A SEARCH RESULT:", url)
    check("every url shown is a page the search returned", all_ok)
    check("no email addresses", re.search(r"[\w.+-]+@[\w-]+\.\w", "\n".join(replies)) is None)
    check("the proposal is closed", bot.db.proposal(proposal["proposal_key"])["status"],
          "applied")

    # ------------------------------------------------------------- (iii)
    print('\n(iii) "find PoCs at Synthflow in the research team" — the engine')
    SEARCHES.clear()
    outcome: dict = {}
    answer = await bot.query_engine.answer(
        question="find PoCs at Synthflow in the research team",
        requester_name="Vaishnavi", tools=bot._people_tools(), history=[],
        outcome=outcome,
    )
    print("   tools used:", outcome.get("tools_used"))
    for s in SEARCHES:
        print_search(s)
    print("   the answer:")
    show(answer or "(none)")
    check("the engine used find_people", "find_people" in (outcome.get("tools_used") or []))
    check("department words went into the query",
          bool(SEARCHES) and "research team" in SEARCHES[0]["prompt"])
    if SEARCHES:
        ev = websearch.evidence_urls(SEARCHES[0])
        stray = [u for u in re.findall(r"<(https?://[^>]+)>", answer or "")
                 if websearch._url_norm(u) not in ev]
        check("every url in the answer is a page the search returned", stray, [])

    # ------------------------------------------------------------- (iv)
    print("\n(iv) R2 screens only stories about a specific company")
    stories = [
        {"url": "https://example.com/synthflow-series-b", "url_key": "ex/synthflow",
         "headline": "Synthflow AI raises $20m to scale voice agents for call centres",
         "what": "the Berlin startup builds no-code voice AI agents", "topic": "voice"},
        {"url": "https://example.com/meity-ai-rules", "url_key": "ex/meity",
         "headline": "MeitY issues draft AI governance guidelines for India",
         "what": "the ministry proposes disclosure rules for AI systems", "topic": "policy"},
        {"url": "https://example.com/bengaluru-ai-city", "url_key": "ex/blr",
         "headline": "Bengaluru announces an AI city project on its outskirts",
         "what": "the state government plans a 2,000-acre AI hub", "topic": "policy"},
    ]
    bot.db.record_news_stories(stories, on_date=marker, rule_id="R1")

    async def known():
        return ["Anthropic", "OpenAI", "Sarvam AI"]
    bot._known_companies = known
    SEARCHES.clear()
    LOGS.clear()
    item = {"rule": "news_company_screen", "rule_id": "R2", "web_pending": True,
            "text": "", "type": nextaction.R_NEWS_SCREEN}
    await bot._screen_companies([item], today=today, marker=marker)
    for s in SEARCHES:
        print_search(s)
    skips = [l for l in LOGS if l.startswith("[news] R2 skipped")]
    for l in skips:
        print("   log:", l)
    print("   the R2 message body:")
    show(item.get("text") or "(empty)")
    check("the regulation / city stories were skipped and logged", len(skips) >= 1)
    check("they are not screened", "MeitY" in (item.get("text") or "")
          or "Bengaluru" in (item.get("text") or ""), False)
    check("Synthflow is screened", "Synthflow" in (item.get("text") or ""))
    check("no use-case letters", re.search(r"\buse case [A-J]\b", item.get("text") or "",
                                           re.I) is None)

    # ------------------------------------------------------------- (vi)
    print('\n(vi) the same R11 flow on a test day — "make it Tuesday", then "yes for Shunya"')
    POSTED.clear()
    SEARCHES.clear()

    async def fake_queue(*, today):
        return {"actions": [], "rules_run": []}

    async def fake_plan(*, today, already, queue=None, only_rule=""):
        m, _ = r11_message(["Shunya Labs", "Synthflow AI"], day=today)
        return {"messages": [m], "rolled": [], "held": []}

    bot._run_next_actions = fake_queue
    bot._plan_drip = fake_plan
    bot._sweep_proposals = lambda **kw: asyncio.sleep(0)

    class Cmd:
        id = 999
        channel = FakeChannel()
        author = FakeAuthor()
        content = "make it Tuesday"

        async def reply(self, body, **kw):
            POSTED.append(body)
            return type("S", (), {"id": 7000 + len(POSTED), "channel": FakeChannel()})()

    handled = await bot._handle_test_command(Cmd(), "make it Tuesday")
    check("the test day ran", handled)
    check("the clock is a Tuesday", dl.today_ist().weekday(), 1)
    show("\n".join(str(p) for p in POSTED))
    sent_id = 5000 + len(POSTED)
    tproposal = bot.db.open_proposal_for_message(str(sent_id))
    check("the test-day R11 post opened its poc_lookup proposal",
          (tproposal or {}).get("kind"), "poc_lookup")
    POSTED.clear()
    await bot._maybe_vote_on_proposal(reply_msg("yes for Shunya", sent_id), "yes for Shunya")
    check('"yes for Shunya" searched only Shunya',
          [s["prompt"].split(" who work at ")[1].split(" ")[0] for s in SEARCHES
           if " who work at " in s["prompt"]], ["Shunya"])
    for s in SEARCHES:
        print_search(s)
    show("\n".join(str(p) for p in POSTED))
    check("one reply", len(POSTED), 1)

    print("\n(silence) a poc_lookup nobody answers is never nudged")
    bot.db.open_proposal(
        proposal_key="poc_lookup:old", kind="poc_lookup", tab="x", sheet_row=0,
        row_key="", company="", poc="", payload={"companies": ["Old Co"]},
        reply_text="", trigger="R11", proposed_text="look for PoCs at Old Co",
        requested_by="R11", channel_id=4242, message_id="1",
        created_at=(dl.now_ist() - timedelta(days=10)).isoformat(timespec="seconds"))
    POSTED.clear()
    del bot._sweep_proposals
    posted = await bot._sweep_proposals(today=dl.today_ist(), channel=FakeChannel())
    check("nothing was posted", (bool(posted), POSTED), (False, []))
    check("it expired quietly", bot.db.proposal("poc_lookup:old")["status"], "expired")


try:
    asyncio.run(main())
finally:
    config.SALES_TEST_MODE = False
    clock.forget()
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
sys.exit(1 if failures else 0)
