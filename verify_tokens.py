"""THE TOKEN WORK, VERIFIED — prompt caching, trimming, the prefilter, sizes.

    python verify_tokens.py            # offline checks only
    python verify_tokens.py --live     # also (i) and (iv) against the real API

Nothing is posted anywhere and nothing is written to a sheet. Throwaway
*_test.db.

  (i)   LIVE. One engine question asked twice within a minute, against the real
        API with the real system prompt and the real tool list (handlers stubbed,
        web search off): the second question's calls read the strategy, the
        policy and the tools from the prompt cache — cache_read > 0. The
        capability reply, asked twice, shows the same for the reply path.
  (ii)  An engine question with 3+ tool calls: iteration 3's request carries
        the truncation marker on iteration 1's result, and the one history
        breakpoint has moved to the newest tool_result.
  (iii) "what did Ananda say about pricing?" does NOT call
        extract_sheet_update; "mark Acme as replied" does; "remind me tomorrow
        about the Acme deck" does not.
  (iv)  LIVE (count_tokens, which runs nothing and bills nothing). One hourly
        news check's request, exactly as `llm.web_research(lean=True)` builds
        it: its input tokens.
"""
import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
import time
from datetime import date
from types import SimpleNamespace

LIVE = "--live" in sys.argv

TMP = tempfile.mkdtemp(prefix="saley-tokens-")
os.environ["DB_PATH"] = os.path.join(TMP, "sales_bot_test.db")

import config  # noqa: E402

# THIS SCRIPT CHECKS THE SERVER-SIDE SEARCH PATH (SEARCH_BACKEND=anthropic), with
# the search itself stubbed. The default path — search outside the model, the
# feeds, the light model — is verify_search_backend.py's to check.
config.SEARCH_BACKEND = "anthropic"

config.DB_PATH = os.environ["DB_PATH"]
config.SALES_CHANNEL_IDS = [4242]
config.SALES_CHANNEL_ID_SET = {4242}

import gtm_sheet  # noqa: E402
import news  # noqa: E402
import query_engine  # noqa: E402
import usage  # noqa: E402
import websearch  # noqa: E402
from bot import SalesBot  # noqa: E402
from db import DB  # noqa: E402

failures = 0
ROWS: list = []
LOG: list = []


def check(name, got, want=True):
    global failures
    ok = (got == want)
    failures += 0 if ok else 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f": got {got!r}, want {want!r}"))


class Capture(logging.Handler):
    def emit(self, record):
        LOG.append(record.getMessage())


logging.basicConfig(level=logging.WARNING, format="      log  %(name)s: %(message)s")
for name in ("usage", "bot"):
    logging.getLogger(name).setLevel(logging.INFO)
logging.getLogger().addHandler(Capture())
logging.getLogger("bot").addFilter(lambda r: r.getMessage().startswith("[sheetwrite] prefilter"))


class FakeChannel:
    id = 4242
    name = "sales"
    guild = SimpleNamespace(id=1)

    async def send(self, body):
        return SimpleNamespace(id=1)


def message(text, mid=1):
    async def reply(body, mention_author=False):
        return SimpleNamespace(id=2)
    return SimpleNamespace(id=mid, content=text, channel=FakeChannel(), reference=None,
                           mentions=[], reply=reply,
                           author=SimpleNamespace(id=7, bot=False, name="v",
                                                  display_name="Vaishnavi"))


def stub_tools(bot, msg, *, large=False):
    tools = (bot._discord_tools(msg) + bot._notes_tools("pricing") + bot._sheet_tools(msg)
             + bot._mapping_tools() + bot._todo_tools() + bot._strategy_tools())

    async def small(_inp):
        return {"rows": [{"company": "Acme AI", "stage": "DM sent", "next": "demo Thu"}]}

    async def big(_inp):
        return {"rows": ["a long result line about a company and its deal " * 3
                         for _ in range(40)]}

    for t in tools:
        if t.get("handler"):
            t["handler"] = big if large else small
    return tools


def _usage():
    return SimpleNamespace(input_tokens=0, output_tokens=0, cache_creation_input_tokens=0,
                           cache_read_input_tokens=0,
                           server_tool_use=SimpleNamespace(web_search_requests=0))


async def main():
    bot = SalesBot()
    bot.db = DB(config.DB_PATH)
    usage.set_sink(lambda row: (ROWS.append(row), bot.db.record_llm_call(row)))

    # ------------------------------------------------------------------ (ii)
    print("(ii) AN ENGINE QUESTION WITH 3+ TOOL CALLS — what iteration 3 sends")
    sent: list = []

    class Rec:
        def create(self, **kw):
            sent.append(json.loads(json.dumps(kw, default=lambda o: getattr(o, "__dict__", str(o)))))
            n = len(sent)
            if n <= 3:
                return SimpleNamespace(
                    content=[SimpleNamespace(type="tool_use", id=f"tu{n}",
                                             name=["lookup_sheet", "search_notes",
                                                   "search_channel_history"][n - 1],
                                             input={})],
                    stop_reason="tool_use", usage=_usage())
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="done",
                                                            citations=[])],
                                   stop_reason="end_turn", usage=_usage())

    real_client = bot.query_engine._client
    bot.query_engine._client = SimpleNamespace(messages=Rec())
    msg = message("where are we with Acme and Globex?")
    tools = stub_tools(bot, msg, large=True)
    names = {t["schema"]["name"] for t in tools}
    for want in ("lookup_sheet", "search_notes", "search_channel_history"):
        if want not in names:
            tools.append({"schema": {"name": want, "description": "stub",
                                     "input_schema": {"type": "object", "properties": {}}},
                          "handler": tools[0]["handler"]})
    await bot.query_engine.answer(question="where are we with Acme and Globex?",
                                  requester_name="Vaishnavi", tools=tools, outcome={})
    third = sent[2]
    results = [b for m in third["messages"] if m["role"] == "user"
               and isinstance(m["content"], list) for b in m["content"]
               if b.get("type") == "tool_result"]
    for i, b in enumerate(results, 1):
        c = b["content"]
        print(f"   iteration-3 request, tool_result {i}: {len(c)} chars, ends "
              f"{c[-40:]!r}, cache_control={'yes' if 'cache_control' in b else 'no'}")
    check("iteration 1's result is truncated to its first 600 chars + the marker",
          results[0]["content"].endswith("(truncated — already read)")
          and len(results[0]["content"]) <= 600 + 30)
    check("iteration 2's result is kept (capped at 6000 if longer)",
          len(results[1]["content"]) > 600)
    check("the one history breakpoint is on the newest tool_result",
          ["cache_control" in b for b in results], [False, True])
    bps = json.dumps(third, ensure_ascii=False).count('"cache_control"')
    check("four breakpoints in the request, no more", bps, 4)
    bot.query_engine._client = real_client

    # ------------------------------------------------------------------ (iii)
    print("\n(iii) THE SHEET-UPDATE PREFILTER")

    class Tab:
        title = "Outreach PoCs"
        kind = gtm_sheet.POCS
        rows = [{"company": "Acme AI", "name": "Ada Lovelace", "_row": 12},
                {"company": "Acme", "name": "Bob", "_row": 13}]

    gtm_sheet.SHEETS.tab = lambda kind, *a, **k: Tab()
    called: list = []

    async def spy(**kw):
        called.append(kw["text"])
        return None

    bot.llm.extract_sheet_update = spy

    async def no(*_a, **_k):
        return False

    bot._maybe_focus_command = no
    bot._maybe_vote_on_proposal = no
    for i, text in enumerate(["what did Ananda say about pricing?", "mark Acme as replied",
                              "remind me tomorrow about the Acme deck"]):
        before = len(called)
        LOG.clear()
        await bot._maybe_apply_sheet_update(message(text, mid=100 + i), text)
        line = next((l for l in LOG if "[sheetwrite] prefilter" in l), "")
        print(f"   {text!r:45} -> {'CALLED' if len(called) > before else 'skipped'}  | {line}")
    check("the question skips the extractor", "what did Ananda say about pricing?" in called,
          False)
    check("the update calls it", "mark Acme as replied" in called, True)
    check("the reminder skips it", "remind me tomorrow about the Acme deck" in called, False)

    if not LIVE:
        print("\n(i) and (iv) need --live")
        return

    # ------------------------------------------------------------------ (i)
    print("\n(i) LIVE — THE SAME QUESTION TWICE WITHIN A MINUTE")
    config.WEB_SEARCH_ENABLED = False
    bot2 = SalesBot()
    bot2.db = bot.db
    # A NEW SalesBot installs its own sink; put the test's back.
    usage.set_sink(lambda row: (ROWS.append(row), bot.db.record_llm_call(row)))
    msg = message("what is the Acme AI status and what's next?")
    tools = stub_tools(bot2, msg)
    web_tools, note, tail = await bot2._websearch_tools()
    for n in (1, 2):
        start = len(ROWS)
        t0 = time.monotonic()
        answer = await bot2.query_engine.answer(
            question="What is the Acme AI status and what's next? One line.",
            requester_name="Vaishnavi", tools=tools + web_tools, extra_system=note,
            extra_tail=tail, outcome={})
        print(f"   ask #{n} ({time.monotonic() - t0:.1f}s): {str(answer)[:90]!r}")
        for r in ROWS[start:]:
            print(f"      [tokens] site={r['site']} in={r['input_tokens']} "
                  f"cache_r={r['cache_read']} cache_w={r['cache_write']} "
                  f"out={r['output_tokens']} t={r['seconds']}s")
        if n == 1:
            first_rows = ROWS[start:]
        else:
            second_rows = ROWS[start:]
    # A cache written by a run in the last five minutes is read, not rewritten.
    check("the first ask writes the cache (or reads it, warm from a run minutes ago)",
          any(r["cache_write"] > 0 or r["cache_read"] > 0 for r in first_rows))
    check("the second ask's first call reads it (cache_read > 0)",
          second_rows[0]["cache_read"] > 0)
    start = len(ROWS)
    for _ in range(2):
        await bot2.llm.capability_reply(text="what can you do?", requester="Vaishnavi")
    cap = ROWS[start:]
    for r in cap:
        print(f"      [tokens] site={r['site']} in={r['input_tokens']} "
              f"cache_r={r['cache_read']} cache_w={r['cache_write']} out={r['output_tokens']}")
    check("the capability reply, asked twice, reads its cache the second time",
          cap[-1]["cache_read"] > 0)

    # ------------------------------------------------------------------ (iv)
    print("\n(iv) LIVE — ONE HOURLY NEWS CHECK'S INPUT TOKENS (count_tokens)")
    config.WEB_SEARCH_ENABLED = True
    prompt = news.sweep_prompt(config.NEWS_TOPICS, today=date.today(), since_hours=1,
                               mode=news.MODE_CHECK,
                               already=[f"Headline number {i} posted today" for i in range(6)],
                               preferred=config.NEWS_PREFERRED_DOMAINS)
    system = websearch.SAFETY_PREAMBLE + "\n\n" + websearch.LEAN_LINE
    tool = websearch.tool_definition(max_uses=config.NEWS_CHECK_MAX_USES)
    msgs = [{"role": "user", "content": prompt}]
    own = bot2.llm._client.messages.count_tokens(
        model=config.MODEL, system=system, messages=msgs).input_tokens
    full = bot2.llm._client.messages.count_tokens(
        model=config.MODEL, system=system, tools=[tool], messages=msgs).input_tokens
    basic = bot2.llm._client.messages.count_tokens(
        model=config.MODEL, system=system, messages=msgs,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 2}],
    ).input_tokens
    print(f"   the bot's prompt (system {len(system)} + user {len(prompt)} chars): "
          f"{own} input tokens")
    print(f"   + the configured {config.WEB_SEARCH_TOOL_TYPE} tool definition: {full} "
          f"(the tool adds {full - own})")
    print(f"   + the basic web_search_20250305 instead, for comparison: {basic}")
    print("   (search results, when a real search runs, are billed as input on top)")
    check("the bot's own prompt for a check is under ~3,000 input tokens", own < 3000)
    print(f"  {'NOTE' if full >= 3000 else 'PASS'}  the whole request including the "
          f"search tool's own definition: {full} tokens"
          + (" — OVER ~3,000; the overhead is the tool, not the prompt" if full >= 3000
             else ""))

    total = bot.db.llm_usage_by_site("2000-01-01")
    print("\n   everything this run spent, from llm_calls:")
    for r in total:
        print(f"     {r['site']:22} calls={r['calls']} in={r['input_tokens']} "
              f"cache_r={r['cache_read']} cache_w={r['cache_write']} out={r['output_tokens']}")


try:
    asyncio.run(main())
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
sys.exit(1 if failures else 0)
