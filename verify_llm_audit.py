"""EVERY ANTHROPIC CALL THE BOT MAKES, MEASURED — not estimated.

    python verify_llm_audit.py            # prints the table
    python verify_llm_audit.py --json X   # also writes the rows to X

Each call site is DRIVEN through its real code path with the Anthropic client
swapped for a recorder, so the numbers are what the bot would actually send:
the system prompt's size in characters (all blocks), max_tokens, whether the
strategy and the policy ride in it, how many tools, how many cache_control
breakpoints, and the size of the messages. Nothing reaches the API; the
database is a throwaway *_test.db; web search is "on" but every search comes
back "NOTHING FOUND".

The frequency column is the call's schedule, from the code — it cannot be
measured from one run.

It also asserts the three things that must make ZERO calls: the reminder
loop, the interim line and the deliverables renderer.
"""
import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
from datetime import date
from types import SimpleNamespace

TMP = tempfile.mkdtemp(prefix="saley-audit-")
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
config.SALES_CHANNEL_ID_SET = {4242}
config.WEB_SEARCH_ENABLED = True
config.WEB_SEARCH_DAILY_BUDGET = 500
config.DRIP_LLM_COMPOSE = True
config.digest_enabled = lambda: True

logging.basicConfig(level=logging.ERROR)

import drip  # noqa: E402
import gtm_sheet  # noqa: E402
import llm as llm_mod  # noqa: E402
import nextaction  # noqa: E402
import persona  # noqa: E402
import query_engine  # noqa: E402
import research  # noqa: E402
import sources  # noqa: E402
from bot import SalesBot  # noqa: E402
from db import DB  # noqa: E402

ROWS: list = []
SITE = ["?"]
ENGINE_SCRIPT: list = []

FREQ = {
    "parse_query": "per addressed message (router)",
    "extract_sheet_update": "per addressed message, before the router",
    "social_reply": "per greeting / unclear message",
    "capability_reply": "per capability question",
    "proactive_message": "per drip slot (and per simulated slot)",
    "classify_leave": "per drip slot, cached HOLIDAY_CACHE_MINUTES",
    "detect_commitment": "per unaddressed msg passing looks_like_commitment",
    "research_brief": "per research_brief tool call",
    "chase_nudge": "never (no callers)",
    "engine:iteration": "per engine iteration (<= MAX_TOOL_ITERATIONS)",
    "engine:final": "per question that hits the iteration cap",
    "web_research:R1": "per weekday (main post, 14:00)",
    "web_research:R1-check": "per hourly check (up to 12/day, every day)",
    "web_research:R2": "per R2 slot (Tue, Fri)",
    "web_research:R3": "per R3 slot (every other Wed)",
    "web_research:R3-deadlines": "per R3 slot with a missing deadline",
    "web_research:R6": "per R6 item at its slot",
    "web_research:R8": "per R8 item at its slot",
    "web_research:R10": "per R10 item at its slot (Mon)",
    "web_research:R11": "per R11 item at its slot",
}


def _text_of_system(system) -> str:
    if isinstance(system, list):
        return "".join(str(b.get("text") or "") for b in system if isinstance(b, dict))
    return str(system or "")


def _count_cache(obj) -> int:
    if isinstance(obj, dict):
        return (1 if "cache_control" in obj else 0) + sum(_count_cache(v) for v in obj.values())
    if isinstance(obj, list):
        return sum(_count_cache(v) for v in obj)
    return 0


def _msg_chars(messages) -> int:
    total = 0
    for m in messages or []:
        c = m.get("content")
        if isinstance(c, str):
            total += len(c)
        elif isinstance(c, list):
            for b in c:
                if isinstance(b, dict):
                    total += len(str(b.get("text") or b.get("content") or ""))
                else:
                    total += len(str(getattr(b, "text", "") or ""))
    return total


def _usage(**kw):
    return SimpleNamespace(input_tokens=1, output_tokens=1,
                           cache_creation_input_tokens=0, cache_read_input_tokens=0,
                           server_tool_use=SimpleNamespace(web_search_requests=0))


class Recorder:
    """messages.create for every client the bot holds."""

    def create(self, **kw):
        system = kw.get("system")
        stext = _text_of_system(system)
        site = SITE[0]
        tools = kw.get("tools") or []
        if site == "engine":
            site = "engine:iteration" if tools else "engine:final"
        ROWS.append({
            "site": site,
            "system_chars": len(stext),
            "system_blocks": len(system) if isinstance(system, list) else 1,
            "max_tokens": kw.get("max_tokens"),
            "strategy": persona.STRATEGY_MARKER in stext,
            "policy": "=== SALES POLICY" in stext,
            "tools": len(tools),
            "tool_chars": len(json.dumps(tools, default=str)) if tools else 0,
            "cache_breakpoints": _count_cache(system) + _count_cache(tools)
            + _count_cache(kw.get("messages")),
            "message_chars": _msg_chars(kw.get("messages")),
            "truncated_marker": "(truncated — already read)" in json.dumps(
                kw.get("messages"), default=str, ensure_ascii=False),
        })
        if SITE[0] == "engine" and ENGINE_SCRIPT:
            return ENGINE_SCRIPT.pop(0)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="NOTHING FOUND", citations=[])],
            stop_reason="end_turn", usage=_usage())


def tool_use(i, name):
    return SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", id=f"tu{i}", name=name, input={})],
        stop_reason="tool_use", usage=_usage())


def final_text():
    return SimpleNamespace(content=[SimpleNamespace(type="text", text="An answer.",
                                                    citations=[])],
                           stop_reason="end_turn", usage=_usage())


class FakeChannel:
    id = 4242
    name = "sales-test"
    guild = SimpleNamespace(id=1)

    async def send(self, body):
        return SimpleNamespace(id=1, jump_url="x")


def item(rule, rule_id, **extra):
    return {"rule": rule, "rule_id": rule_id, "rule_name": rule_id, "type": rule,
            "text": "x [web research pending]", "web_pending": True, "actions": [],
            "sources": [], **extra}


async def main(out_json):
    bot = SalesBot()
    bot.db = DB(config.DB_PATH)
    rec = Recorder()
    bot.llm._client = SimpleNamespace(messages=rec)
    bot.query_engine._client = SimpleNamespace(messages=rec)
    bot.get_channel = lambda cid: FakeChannel()
    L = bot.llm
    today = date(2026, 10, 5)

    # ---- llm._create and its callers ---------------------------------------
    SITE[0] = "parse_query"
    await L.parse_query(text="where are we with Acme?", requester="Vaishnavi", history=[])
    SITE[0] = "social_reply"
    await L.social_reply(kind="greeting", text="hi", requester="Vaishnavi")
    SITE[0] = "capability_reply"
    await L.capability_reply(text="what can you do?", requester="Vaishnavi")
    SITE[0] = "proactive_message"
    await L.proactive_message(prompt="Write ONE short proactive message …",
                              fallback="template", recent_openers=["worth a look"])
    SITE[0] = "classify_leave"
    await L.classify_leave(prompt="#leave, last 7 days:\nKushal: off Thursday")
    SITE[0] = "extract_sheet_update"
    await L.extract_sheet_update(text="mark Acme as replied", today="2026-10-05",
                                 requester="Vaishnavi")
    SITE[0] = "detect_commitment"
    await L.detect_commitment(text="I'll send the deck tomorrow", author="Sid",
                              channel="sales")
    if hasattr(L, "chase_nudge"):
        SITE[0] = "chase_nudge"
        await L.chase_nudge(mention="<@1>", what="the deck", when="yesterday")

    # research_brief, through the real tool: a row, two fetched links, the
    # strategy doc connected, so every part of `material` is exercised.
    class Tab:
        title = "Outreach PoCs"
        rows = [{"company": "Acme AI", "name": "Ada Lovelace", "poc": "Ada Lovelace",
                 "_row": 12}]

    gtm_sheet.SHEETS.tab = lambda kind, *a, **k: Tab()
    research.gather = lambda row: {
        "person": "Ada Lovelace", "org": "Acme AI",
        "sheet_facts": {"designation": "Head of Research"},
        "linkedin": {"note": "not fetched", "available": False},
        "links_refused": [], "no_links": False,
        "links_fetched": [{"url": f"https://arxiv.org/abs/{i}", "column": "research",
                           "ok": True, "error": "", "text": "paper text " * 1000}
                          for i in range(3)],
    }
    sources.STRATEGY_DOC = SimpleNamespace(connected=True,
                                           text=lambda: persona.load_strategy())
    msg = SimpleNamespace(id=1, channel=FakeChannel(), content="",
                          author=SimpleNamespace(id=7, bot=False, name="v",
                                                 display_name="Vaishnavi"))
    tools = {t["schema"]["name"]: t["handler"] for t in bot._sheet_tools(msg)}
    SITE[0] = "research_brief"
    await tools["research_brief"]({"person": "Ada Lovelace", "org": "Acme AI"})

    # ---- web_research, through each caller ---------------------------------
    real_wr = L.web_research

    async def tagged(*, rule, **kw):
        SITE[0] = f"web_research:{rule}"
        return await real_wr(rule=rule, **kw)

    L.web_research = tagged

    async def known():
        return ["Acme AI", "Globex"]

    async def ev_rows(kind, _for):
        return [{"event": "NeurIPS 2026", "event_date": "2026-12-01",
                 "registration_deadline": "", "_row": 2, "link": "https://neurips.cc"}]

    bot._known_companies = known
    bot._rule_tab_rows = ev_rows
    await bot._news_run([item(nextaction.R_AI_NEWS, "R1")], today=today)
    await bot._maybe_breaking_news(force=True, channel=FakeChannel())
    bot.db.record_news_stories([{"url_key": "x.com/a", "url": "https://x.com/a",
                                 "headline": "Nebius raises", "what": "a round",
                                 "topic": "OTHER", "headline_key": "nebius raises"}],
                               on_date="2026-10-05", rule_id="R1", kind="main")
    await bot._news_run([item(nextaction.R_NEWS_SCREEN, "R2")], today=today)
    await bot._events_run([item(nextaction.R_EVENTS, "R3")], today=today)
    await bot._research_uncached([
        item(nextaction.R_LI_NO_DM, "R6", company="Acme AI", poc="Ada Lovelace"),
        item(nextaction.R_MEETING_PREP, "R8", company="Acme AI", poc="Ada Lovelace"),
        item(nextaction.R_CLOSURE_SUPPORT, "R10", company="Acme AI"),
        item(nextaction.R_NEW_COMPANY, "R11", company="Acme AI"),
    ], today=today)

    # ---- the question engine ------------------------------------------------
    engine_tools = (bot._discord_tools(msg) + bot._notes_tools("pricing")
                    + bot._sheet_tools(msg) + bot._mapping_tools() + bot._todo_tools()
                    + bot._strategy_tools())
    web_tools, web_note, web_tail = await bot._websearch_tools()
    engine_tools += web_tools

    async def big(_inp):
        return {"rows": ["a result line " * 60 for _ in range(10)]}

    names = [t["schema"]["name"] for t in engine_tools if t.get("handler")]
    for t in engine_tools:
        if t.get("handler"):
            t["handler"] = big
    SITE[0] = "engine"
    ENGINE_SCRIPT[:] = [tool_use(1, names[0]), tool_use(2, names[1]),
                        tool_use(3, names[2]), final_text()]
    await bot.query_engine.answer(question="where are we with Acme and Globex?",
                                  requester_name="Vaishnavi", tools=engine_tools,
                                  extra_system=web_note, extra_tail=web_tail, outcome={})
    cap = query_engine.MAX_TOOL_ITERATIONS
    ENGINE_SCRIPT[:] = [tool_use(10 + i, names[i % len(names)]) for i in range(cap)]
    await bot.query_engine.answer(question="everything about everyone",
                                  requester_name="Vaishnavi", tools=engine_tools,
                                  extra_system=web_note, extra_tail=web_tail, outcome={})

    # ---- the three that must make ZERO calls -------------------------------
    before = len(ROWS)
    SITE[0] = "MUST-NOT-CALL"
    await bot._fire_due_reminders(channel=FakeChannel())
    msg.id = 99

    async def _reply(body, mention_author=False):
        return SimpleNamespace(id=2, jump_url="x")

    msg.reply = _reply
    bot._qstate[99] = {"interim": False}
    await bot._send_interim(msg, web=False, after=10)
    drip.render_deliverables([{"item": "MSA", "team": "Legal", "deadline": "2026-10-06",
                               "deadline_pretty": "Tue 6 Oct", "is_p1": True}])
    zero = len(ROWS) - before

    # ---- the table ----------------------------------------------------------
    order = list(FREQ)
    seen: dict = {}
    for r in ROWS:
        seen.setdefault(r["site"], []).append(r)

    def key(s):
        return (order.index(s) if s in order else 999, s)

    print(f"{'site':28} {'system':>7} {'blocks':>6} {'max_tok':>7} {'strat':>5} "
          f"{'policy':>6} {'tools':>5} {'tool_ch':>7} {'cache':>5} {'msgs':>7}  frequency")
    for s in sorted(seen, key=key):
        rows = seen[s]
        r = rows[-1] if s != "engine:iteration" else rows[2] if len(rows) > 2 else rows[-1]
        print(f"{s:28} {r['system_chars']:>7} {r['system_blocks']:>6} "
              f"{str(r['max_tokens']):>7} {'yes' if r['strategy'] else '-':>5} "
              f"{'yes' if r['policy'] else '-':>6} {r['tools']:>5} {r['tool_chars']:>7} "
              f"{r['cache_breakpoints']:>5} {r['message_chars']:>7}  {FREQ.get(s, '')}")
    it = seen.get("engine:iteration", [])
    print("\nengine iterations (question 1): "
          + ", ".join(f"#{i + 1} msgs={r['message_chars']} cache={r['cache_breakpoints']}"
                      f"{' TRUNC' if r['truncated_marker'] else ''}"
                      for i, r in enumerate(it[:4])))
    print(f"\nreminder loop + interim line + deliverables renderer: {zero} API call(s)")
    if out_json:
        with open(out_json, "w", encoding="utf-8") as f:
            json.dump({"rows": ROWS, "zero": zero}, f, indent=1, default=str)
    return zero


try:
    out = sys.argv[sys.argv.index("--json") + 1] if "--json" in sys.argv else ""
    zero = asyncio.run(main(out))
finally:
    shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if zero else 0)
