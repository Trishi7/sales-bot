"""THE TYPING INDICATOR, THE INTERIM LINE AND THE LATENCY LOG — with real timings.

    python verify_interim.py

Nothing is sent to Discord and nothing is billed. The REAL question path runs —
`_handle_query` -> the router -> `_answer_with_engine` -> the REAL QueryEngine
tool loop -> `_reply` -> `guardrails.send` — with three things faked: the
Anthropic client (it SLEEPS for real, so every number below is wall-clock), the
router/social model, and the Discord channel (it records the typing indicator
and every message with the second it arrived). Throwaway *_test.db.

  (i)   "hi" — typing indicator only, no interim, reply under 8 s;
  (ii)  an engine question with 3+ tool calls taking > 10 s — exactly one
        interim at ~10 s, then the answer;
  (iii) a web question — interim at ~6 s;
  (iv)  the reply_latency rows for all of them;
  (v)   a forced API failure after the interim — the honest failure line
        follows, not silence;
  plus  the "what did you cost" answer's latency section, and the drip path
        touching neither the typing indicator nor the interim line.
"""
import asyncio
import logging
import os
import re
import shutil
import sys
import tempfile
import time
from types import SimpleNamespace

TMP = tempfile.mkdtemp(prefix="saley-interim-")
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
config.SALES_CHANNEL_IDS = [4242]
config.SALES_CHANNEL_ID_SET = {4242}
config.WEB_SEARCH_ENABLED = True
config.WEB_SEARCH_DAILY_BUDGET = 60
# THE SHIPPED DEFAULTS, pinned.
config.INTERIM_ENABLED = True
config.INTERIM_AFTER_SECONDS = 10.0
config.INTERIM_AFTER_WEB_SECONDS = 6.0

import persona  # noqa: E402
from bot import SalesBot  # noqa: E402
from db import DB  # noqa: E402

failures = 0


def check(name, got, want=True):
    global failures
    ok = (got == want)
    failures += 0 if ok else 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f": got {got!r}, want {want!r}"))


logging.basicConfig(level=logging.WARNING, format="      log  %(name)s: %(message)s")
for name in ("bot",):
    lg = logging.getLogger(name)
    lg.setLevel(logging.INFO)
    lg.addFilter(lambda r: r.getMessage().startswith(("[interim]", "[latency]")))

T0 = [0.0]


def now():
    return time.monotonic() - T0[0]


# -- the fake Discord side ----------------------------------------------------

class Typing:
    def __init__(self, channel):
        self.channel = channel

    async def __aenter__(self):
        self.channel.events.append((now(), "typing on"))

    async def __aexit__(self, *exc):
        self.channel.events.append((now(), "typing off"))


class FakeChannel:
    id = 4242
    name = "sales-test"
    guild = SimpleNamespace(id=1)

    def __init__(self):
        self.events = []

    def typing(self):
        return Typing(self)

    async def send(self, body):
        self.events.append((now(), "send: " + body))
        return SimpleNamespace(id=int(now() * 1000), jump_url="https://discord/x")


class FakeMessage:
    _next = [1000]

    def __init__(self, channel, content):
        FakeMessage._next[0] += 1
        self.id = FakeMessage._next[0]
        self.content = content
        self.channel = channel
        self.author = SimpleNamespace(id=7, bot=False, name="vaishnavi",
                                      display_name="Vaishnavi", global_name="Vaishnavi")
        self.reference = None
        self.mentions = []
        self.mention_everyone = False
        self.guild = channel.guild

    async def reply(self, body, mention_author=False):
        self.channel.events.append((now(), "reply: " + body))
        return SimpleNamespace(id=int(now() * 1000), jump_url="https://discord/x")


# -- the fake models ----------------------------------------------------------

class FakeLLM:
    """The router and the social voice. A real call takes a second or two."""

    async def parse_query(self, *, text, requester, history):
        await asyncio.sleep(0.8)
        kind = "greeting" if text.strip().lower() in ("hi", "hello") else "question"
        return {"message_kind": kind, "is_query": kind == "question"}

    async def social_reply(self, *, kind, text, requester):
        await asyncio.sleep(1.4)
        return "Hi Vaishnavi — what do you need?"


SCRIPT: list = []          # [(seconds, response-or-exception)], consumed in order
CALLS = [0]


class APIConnectionError(Exception):
    """Named like the SDK's, so the failure sentence carries a real class name."""


def tool_use(i, name):
    return SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", id=f"tu_{i}", name=name,
                                 input={"company": "Acme"})],
        stop_reason="tool_use",
        usage=SimpleNamespace(server_tool_use=SimpleNamespace(web_search_requests=0)),
    )


def final(text, searches=0):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text, citations=[])],
        stop_reason="end_turn",
        usage=SimpleNamespace(server_tool_use=SimpleNamespace(web_search_requests=searches)),
    )


class FakeMessages:
    def create(self, **_kw):
        delay, out = SCRIPT.pop(0)
        CALLS[0] += 1
        time.sleep(delay)                 # the SDK is sync and runs on a thread
        if isinstance(out, Exception):
            raise out
        return out


def fake_tool(name):
    async def handler(_tool_input):
        return {"rows": [{"company": "Acme", "stage": "DM sent"}]}

    return {"schema": {"name": name, "description": "stub",
                       "input_schema": {"type": "object", "properties": {}}},
            "handler": handler}


def make_bot():
    bot = SalesBot()
    bot.db = DB(config.DB_PATH)
    bot.llm = FakeLLM()
    bot.query_engine._client = SimpleNamespace(messages=FakeMessages())
    bot._discord_tools = lambda m: [fake_tool("search_channels")]
    bot._sheet_tools = lambda m: [fake_tool("lookup_sheet")]
    bot._notes_tools = lambda t: [fake_tool("search_notes")]
    bot._mapping_tools = lambda: []
    bot._todo_tools = lambda: []
    bot._strategy_tools = lambda: []

    async def not_an_update(_m, _t):
        return False

    bot._maybe_apply_sheet_update = not_an_update
    return bot


async def ask(bot, text):
    channel = FakeChannel()
    msg = FakeMessage(channel, text)
    T0[0] = time.monotonic()
    await bot._handle_query(msg)
    total = now()
    print(f"   timeline for {text!r}:")
    for t, ev in channel.events:
        print(f"     {t:5.1f}s  {ev[:110]}")
    return channel, total


def of(channel, prefix):
    return [(t, e) for t, e in channel.events if e.startswith(prefix)]


def interims(channel):
    lines = persona.INTERIM_LINES_WEB + persona.INTERIM_LINES_ENGINE
    return [(t, e) for t, e in of(channel, "reply: ") if e[len("reply: "):] in lines]


async def main():
    bot = make_bot()

    print("(i) \"hi\"")
    ch, total = await ask(bot, "hi")
    check("the typing indicator came on", bool(of(ch, "typing on")))
    check("no interim line", interims(ch), [])
    first = of(ch, "reply: ")
    check("the reply came in under 8 s", bool(first) and first[0][0] < 8.0)

    print("\n(ii) an engine question: 3 tool calls, ~14 s")
    SCRIPT[:] = [(3.6, tool_use(1, "lookup_sheet")), (3.6, tool_use(2, "search_notes")),
                 (3.6, tool_use(3, "search_channels")),
                 (3.0, final("Acme is at DM sent since 22 Sep; Globex has a meeting "
                             "on Thursday."))]
    ch, total = await ask(bot, "where are we with Acme and Globex?")
    its = interims(ch)
    check("exactly one interim", len(its), 1)
    check("...at ~10 s", bool(its) and 9.8 <= its[0][0] <= 11.0)
    check("...in the engine voice (no web search ran or was asked for)",
          bool(its) and its[0][1][len("reply: "):] in persona.INTERIM_LINES_ENGINE)
    ans = [e for e in of(ch, "reply: ") if "Acme is at DM sent" in e[1]]
    check("then the answer", bool(ans) and ans[0][0] > its[0][0] if its else False)

    print("\n(iii) a web question, ~9 s")
    SCRIPT[:] = [(9.0, final("Sarvam raised a $41m Series A this week "
                             "<https://techcrunch.com/sarvam>", searches=2))]
    ch, total = await ask(bot, "any funding news on Sarvam this week?")
    its = interims(ch)
    check("exactly one interim", len(its), 1)
    check("...at ~6 s", bool(its) and 5.8 <= its[0][0] <= 7.0)
    check("...in the web voice",
          bool(its) and its[0][1][len("reply: "):] in persona.INTERIM_LINES_WEB)
    check("then the answer", any("Sarvam raised" in e for _t, e in of(ch, "reply: ")))

    print("\n(v) the API fails AFTER the interim")
    SCRIPT[:] = [(3.0, tool_use(1, "lookup_sheet")),
                 (8.5, APIConnectionError("Connection error."))]
    ch, total = await ask(bot, "what's the status across the pipeline?")
    its = interims(ch)
    check("one interim went out first", len(its), 1)
    fail = [e for _t, e in of(ch, "reply: ")
            if e[len("reply: "):] == persona.model_failure_reply("APIConnectionError")]
    check("the honest failure line follows — not silence", len(fail), 1)

    print("\n(iv) reply_latency")
    rows = bot.db.reply_latency_since("2000-01-01")
    print("     ts                         route       seconds used_web tool_calls interim")
    for r in rows:
        print(f"     {r['ts']:26} {r['route']:11} {r['seconds']:7.2f} {r['used_web']:8} "
              f"{r['tool_calls']:10} {r['interim_sent']:7}")
    check("one row per question", len(rows), 4)
    check("routes", [r["route"] for r in rows], ["social", "engine", "engine", "engine"])
    check("the web question is marked used_web", [r["used_web"] for r in rows],
          [0, 0, 1, 0])
    check("tool calls counted (3 client calls; 2 billed searches)",
          [r["tool_calls"] for r in rows], [0, 3, 2, 1])
    check("interims recorded", [r["interim_sent"] for r in rows], [0, 1, 1, 1])

    print("\n(vii) \"what did you cost\" — the latency section")
    ch, _ = await ask(bot, "what did you cost?")
    body = next((e for _t, e in of(ch, "reply: ")), "")
    check("it reports p50/p90 for the engine route", "engine: p50" in body)
    check("...and the interims", "lines sent: 3 of 4 answers" in body)
    check("no model call for it", CALLS[0], 7)

    print("\n(D) the drip path")
    src = open("bot.py", encoding="utf-8").read()
    drip_src = src[src.index("    async def _maybe_send_drip"):src.index("    async def _plan_drip")]
    send_src = src[src.index("    async def _send_drip_message"):]
    send_src = send_src[:send_src.index("\n    async def ", 10)]
    check("_maybe_send_drip opens no typing indicator",
          "typing" in drip_src, False)
    check("...and sends no interim",
          "_send_interim" in drip_src or "_send_interim" in send_src, False)


try:
    asyncio.run(main())
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
sys.exit(1 if failures else 0)
