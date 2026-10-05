"""R1 AS A TOPIC FEED — the main sweep and the hourly checks, end to end.

    python verify_news_feed.py

Sends nothing to Discord and spends no budget. It runs the REAL
`llm.web_research` (so the lean system prompt and its size log are the real
ones) with the Anthropic client swapped for a fake that answers each prompt
with canned STORY lines, against a throwaway *_test.db, a pretend clock and a
fake sales-test channel that records what it was sent. It shows:

  (i)   one main sweep: 5 lines, each with a topic tag and a link, no two
        sharing a headline_key;
  (ii)  a breaking post at 16:00 followed by the next day's main sweep — the
        breaking story does NOT reappear, and the skip reason is printed;
  (iii) a check that finds two stories at importance >= 4 posts ONE grouped
        message, outside drip_sends;
  (iv)  a check with nothing important posts nothing and logs one line;
  (v)   a check when the valve is full holds the story and logs why;
  (vi)  news_checks shows each slot once after two back-to-back ticks;
  (ix)  an R3 run and an R6 run both log the lean prompt size (< 3,000 chars).
"""
import asyncio
import logging
import os
import re
import shutil
import sys
import tempfile
from datetime import date, datetime
from types import SimpleNamespace

TMP = tempfile.mkdtemp(prefix="saley-newsfeed-")
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
config.WEB_SEARCH_ENABLED = True
config.WEB_SEARCH_DAILY_BUDGET = 60
config.WEB_SEARCH_ALLOWED_DOMAINS = []
config.WEB_SEARCH_BLOCKED_DOMAINS = []
config.digest_enabled = lambda: True
# THE SHIPPED DEFAULTS, pinned, so a local .env cannot make this another test.
config.NEWS_TOPICS = ["evals", "RLHF", "AI regulation", "voice agent", "AI safety",
                      "post-training", "human data"]
config.NEWS_MAIN_TIME = "14:00"
config.NEWS_CHECK_TIMES = ["11:00", "12:00", "13:00", "15:00", "16:00", "17:00",
                           "18:00", "19:00", "20:00", "21:00", "22:00", "23:00"]
config.NEWS_MAX_ITEMS = 5
config.NEWS_PER_TOPIC_PER_DAY = 2
config.NEWS_TOPICS_PER_WEEK = 6
config.NEWS_BREAKING_MIN_IMPORTANCE = 4
config.NEWS_BREAKING_MAX_PER_DAY = 2
config.NEWS_CHECK_MAX_USES = 2
config.NEWS_REPEAT_DAYS = 30
config.NEWS_PREFERRED_DOMAINS = ["reuters.com", "techcrunch.com"]

import deadlines as dl  # noqa: E402
import gtm_sheet  # noqa: E402
import llm as llm_mod  # noqa: E402
import news  # noqa: E402
import nextaction  # noqa: E402
from bot import SalesBot  # noqa: E402
from db import DB  # noqa: E402

TUE = date(2026, 9, 29)
WED = date(2026, 9, 30)
NOW = [datetime(2026, 9, 29, 14, 0, tzinfo=dl.IST)]
dl.now_ist = lambda: NOW[0]
dl.today_ist = lambda: NOW[0].date()


def at(day, hh, mm=0):
    NOW[0] = datetime(day.year, day.month, day.day, hh, mm, tzinfo=dl.IST)


failures = 0
LINES: list = []


def check(name, got, want=True):
    global failures
    ok = (got == want)
    failures += 0 if ok else 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f": got {got!r}, want {want!r}"))


class Capture(logging.Handler):
    def emit(self, record):
        LINES.append(record.getMessage())


root = logging.getLogger()
root.setLevel(logging.INFO)
for h in list(root.handlers):
    root.removeHandler(h)
_out = logging.StreamHandler(sys.stdout)
_out.setFormatter(logging.Formatter("      log  %(name)s: %(message)s"))
_out.addFilter(lambda r: r.name in ("news", "bot", "llm") and (
    "[news" in r.getMessage() or "lean prompt" in r.getMessage()
    or "full prompt" in r.getMessage()))
root.addHandler(_out)
root.addHandler(Capture())
for noisy in ("discord", "asyncio", "state", "httpx", "anthropic"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


# -- the fake Anthropic client ------------------------------------------------

CALLS: list = []
REPLY = [lambda system, prompt: "NOTHING FOUND"]


class FakeMessages:
    def create(self, *, model, max_tokens, system, tools, messages):
        prompt = messages[0]["content"]
        CALLS.append({"system": system, "prompt": prompt, "tools": tools})
        text = REPLY[0](system, prompt)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text, citations=[])],
            usage=SimpleNamespace(server_tool_use=SimpleNamespace(web_search_requests=1)),
            stop_reason="end_turn",
        )


class FakeChannel:
    id = 4242
    name = "sales-test"
    guild = SimpleNamespace(id=1)

    def __init__(self):
        self.sent = []

    async def send(self, body):
        self.sent.append(body)
        return SimpleNamespace(id=len(self.sent), jump_url="https://discord/x")


def item(rule, rule_id, **extra):
    return {"rule": rule, "rule_id": rule_id, "rule_name": rule_id, "type": rule,
            "text": "placeholder [web research pending]", "web_pending": True,
            "actions": [], "sources": [], **extra}


def story(topic, head, what, url, imp):
    return f"STORY | {topic} | {head} | {what} | {url} | {imp}"


def make_bot():
    bot = SalesBot()
    bot.db = DB(config.DB_PATH)
    engine = llm_mod.LLM("sk-fake-not-used", config.MODEL)
    engine._client = SimpleNamespace(messages=FakeMessages())
    bot.llm = engine
    channel = FakeChannel()
    bot.get_channel = lambda cid: channel if int(cid) == 4242 else None

    async def known():
        return ["Acme AI", "Globex"]

    async def event_rows(kind, _for):
        return [{"event": "NeurIPS 2026", "event_date": "2026-12-01",
                 "registration_deadline": "", "_row": 2, "link": "https://neurips.cc"}]

    bot._known_companies = known
    bot._rule_tab_rows = event_rows
    return bot, channel


def news_lines(since):
    return [l for l in LINES[since:] if l.startswith("[news-check]")]


async def main():
    bot, channel = make_bot()

    # ------------------------------------------------------------------ (i)
    print("(i) ONE MAIN SWEEP — Tue 29 Sep 14:00")
    at(TUE, 14)
    main_reply = "\n".join([
        "Here is the last 24 hours.",
        story("evals", "Lab releases open agent eval suite", "a public benchmark for "
              "tool-using agents", "https://techcrunch.com/evals-suite", 4),
        story("evals", "Lab releases open agent eval suite", "same story, second outlet",
              "https://reuters.com/evals-suite?utm_source=x", 4),
        story("RLHF", "Startup raises $40m for RLHF tooling", "Series B led by a "
              "frontier fund", "https://techcrunch.com/rlhf-round", 4),
        story("AI regulation", "EU publishes AI Act code of practice", "final text for "
              "general-purpose models", "https://reuters.com/eu-code", 5),
        story("voice agent", "Indic voice agent launches in Hindi and Tamil",
              "a Bengaluru startup's launch", "https://inc42.com/voice", 3),
        story("OTHER", "Chipmaker posts record data-centre quarter", "AI demand",
              "https://reuters.com/chips", 3),
        story("evals", "Benchmark contamination study published", "a paper on leaked "
              "test sets", "https://arxiv.org/abs/2609.1", 2),
        "STORY | human data | A claim with no link | nothing to check | | 5",
    ])
    REPLY[0] = lambda system, prompt: main_reply
    CALLS.clear()
    items = [item(nextaction.R_AI_NEWS, "R1")]
    await bot._news_run(items, today=TUE)
    body = items[0]["text"]
    print("   the post:")
    print("   " + "\n   ".join(body.splitlines()))
    lines = [l for l in body.splitlines() if l.startswith("• ")]
    check("5 lines", len(lines), 5)
    check("no topic tag", any(re.match(r"^• \[[^\]]+\] ", l) for l in lines), False)
    check("each with a masked link", all("](<https://" in l for l in lines))
    rows = bot.db.news_stories_on(dl.iso(TUE))
    keys = [news.headline_key(r["headline"]) for r in rows]
    check("no two share a headline_key", len(keys), len(set(keys)))
    check("the second outlet's copy is not there", "reuters.com/evals-suite" in body, False)
    check("the linkless claim is not there", "A claim with no link" in body, False)
    check("one search call, lean", len(CALLS), 1)
    sys_len = len(CALLS[0]["system"])
    check("the lean system prompt has no persona",
          "=== SALES STRATEGY" in CALLS[0]["system"], False)
    check("no tool domain restriction — preferred sites are a prompt line",
          "allowed_domains" in CALLS[0]["tools"][0], False)
    check("...and the line is there",
          "Prefer these sources when they have the story: reuters.com, techcrunch.com"
          in CALLS[0]["prompt"])
    print(f"   system prompt {sys_len} chars, user prompt {len(CALLS[0]['prompt'])} chars")
    check("recorded as kind=main", {r["kind"] for r in rows}, {"main"})

    # ------------------------------------------------------------------ (ii)
    print("\n(ii) A BREAKING POST AT 16:00, THEN WEDNESDAY'S MAIN SWEEP")
    at(TUE, 16, 2)
    breaking_head = "Frontier lab ships open-weights reasoning model"
    REPLY[0] = lambda s, p: story("post-training", breaking_head,
                                  "weights and RL recipe released",
                                  "https://techcrunch.com/open-weights", 5)
    CALLS.clear()
    out = await bot._maybe_breaking_news()
    check("the 16:00 check ran", (out or {}).get("slot"), "16:00")
    check("it asked about the hours since the 15:00 slot",
          "MAJOR in the last 1 hours" in CALLS[0]["prompt"])
    check("today's main headlines are in the do-not-return list",
          "EU publishes AI Act code of practice" in CALLS[0]["prompt"])
    check("it posted", (out or {}).get("posted"), 1)
    print("   the post:\n   " + "\n   ".join(channel.sent[-1].splitlines()))

    at(WED, 14)
    REPLY[0] = lambda s, p: "\n".join([
        story("post-training", "Frontier Lab Ships Open-Weights Reasoning Model",
              "weights released yesterday", "https://reuters.com/open-weights-2", 5),
        story("AI safety", "Safety institute publishes red-team results",
              "findings on frontier models", "https://reuters.com/aisi", 4),
        story("human data", "Annotation marketplace opens India hub",
              "hiring 500 raters in Bengaluru", "https://inc42.com/annot", 3),
    ])
    mark = len(LINES)
    items = [item(nextaction.R_AI_NEWS, "R1")]
    await bot._news_run(items, today=WED)
    body = items[0]["text"]
    print("   Wednesday's post:\n   " + "\n   ".join(body.splitlines()))
    check("the breaking story does NOT reappear (different outlet, same headline)",
          "open-weights" in body.lower(), False)
    skip = [l for l in LINES[mark:] if "main sweep skipped" in l]
    print("   skip reason: " + (skip[0] if skip else "(none logged)"))
    check("...and the skip says it went out as a breaking post",
          any("(breaking post) — the same headline" in l for l in skip))

    # ------------------------------------------------------------------ (iii)
    print("\n(iii) A CHECK WITH TWO IMPORTANT STORIES — Tue 17:00")
    at(TUE, 17, 1)
    REPLY[0] = lambda s, p: "\n".join([
        story("AI regulation", "India notifies AI governance rules",
              "binding guidelines for model providers", "https://reuters.com/india-ai", 5),
        story("human data", "Big lab signs $100m human-data contract",
              "multi-year annotation deal", "https://techcrunch.com/human-data", 4),
        story("evals", "Minor eval leaderboard update", "a reshuffle",
              "https://x.com/lb", 3),
    ])
    before_msgs = len(channel.sent)
    out = await bot._maybe_breaking_news()
    new = channel.sent[before_msgs:]
    check("ONE message went out", len(new), 1)
    check("...carrying both important stories", new[0].count("• ") if new else 0, 2)
    # SALES_TEST_MODE: a breaking post carries the test tag, like every test send.
    check("...opening with '[TEST] **Breaking AI news**'",
          new[0].splitlines()[0] if new else "",
          f"{config.SIMULATION_PREFIX} **Breaking AI news**")
    check("the importance-3 story stayed quiet", "leaderboard" in (new[0] if new else ""),
          False)
    check("it is NOT in drip_sends", bot.db.drip_sent_today(dl.iso(TUE)), [])
    check("no @-mention in it", "<@" in (new[0] if new else "") or "@everyone" in
          (new[0] if new else ""), False)
    print("   the post:\n   " + "\n   ".join(new[0].splitlines() if new else []))

    # ------------------------------------------------------------------ (v)
    print("\n(v) THE VALVE IS FULL — Tue 18:00 (2 breaking messages already)")
    at(TUE, 18, 1)
    REPLY[0] = lambda s, p: story("AI safety", "Model provider pauses release over "
                                  "safety finding", "a rare pause",
                                  "https://reuters.com/pause", 5)
    before_msgs = len(channel.sent)
    mark = len(LINES)
    out = await bot._maybe_breaking_news()
    check("nothing was posted", len(channel.sent) - before_msgs, 0)
    check("it says it held the story", (out or {}).get("held"), True)
    held = [l for l in news_lines(mark) if "held for the next main post" in l]
    print("   " + (held[0] if held else "(no line)"))
    check("...and why, in one log line", len(held), 1)
    check("nothing was stored, so tomorrow's sweep can find it",
          bot.db.news_story_seen(news.url_key("https://reuters.com/pause"), "",
                                 since_iso="2026-01-01"), None)

    # ------------------------------------------------------------------ (iv)
    print("\n(iv) NOTHING IMPORTANT — Tue 19:00 and 20:00")
    at(TUE, 19, 1)
    REPLY[0] = lambda s, p: "NOTHING FOUND"
    before_msgs = len(channel.sent)
    mark = len(LINES)
    await bot._maybe_breaking_news()
    quiet = news_lines(mark)
    check("no post", len(channel.sent) - before_msgs, 0)
    check("exactly one log line", len(quiet), 1)
    print("   " + (quiet[0] if quiet else ""))
    at(TUE, 20, 1)
    REPLY[0] = lambda s, p: story("evals", "Small eval tweak", "minor",
                                  "https://x.com/tweak", 2)
    mark = len(LINES)
    await bot._maybe_breaking_news()
    quiet = news_lines(mark)
    check("filler only: no post", len(channel.sent) - before_msgs, 0)
    check("...and exactly one log line", len(quiet), 1)
    print("   " + (quiet[0] if quiet else ""))

    # ------------------------------------------------------------------ (vi)
    print("\n(vi) TWO BACK-TO-BACK TICKS — Tue 21:05")

    async def nothing(*_a, **_k):
        return None

    bot._maybe_send_drip = nothing
    bot._maybe_write_daily_summary = lambda: None
    at(TUE, 21, 5)
    REPLY[0] = lambda s, p: "NOTHING FOUND"
    CALLS.clear()
    await bot._sweep_once()
    await bot._sweep_once()
    check("the second tick made no search", len(CALLS), 1)
    checks = bot.db.news_checks_on(dl.iso(TUE))
    slots = [c["slot_hhmm"] for c in checks]
    check("each slot appears once", len(slots), len(set(slots)))
    print("   news_checks for 2026-09-29:")
    print("     slot   searches found posted")
    for c in checks:
        print(f"     {c['slot_hhmm']:6} {c['searches']:8} {c['found']:5} {c['posted']:6}")
    check("the slots that ran", slots, ["16:00", "17:00", "18:00", "19:00", "20:00",
                                        "21:00"])
    check("breaking messages today", bot.db.news_breaking_messages_today(dl.iso(TUE)), 2)

    # ------------------------------------------------------------------ (ix)
    print("\n(ix) R3 AND R6 USE THE LEAN PROMPT")
    at(WED, 11, 30)
    REPLY[0] = lambda s, p: "NOTHING FOUND"
    mark = len(LINES)
    r3 = [item(nextaction.R_EVENTS, "R3")]
    await bot._events_run(r3, today=WED)
    r6 = [item(nextaction.R_LI_NO_DM, "R6", company="Acme AI", poc="Ada Lovelace")]
    REPLY[0] = lambda s, p: "no public email found for Ada Lovelace"
    await bot._research_uncached(r6, today=WED)
    sizes = {}
    for l in LINES[mark:]:
        m = re.match(r"\[websearch\] (R3|R3-deadlines|R6): (lean|full) prompt, (\d+) "
                     r"chars \(system (\d+) \+ user (\d+)\)", l)
        if m:
            sizes.setdefault(m.group(1), []).append(
                (m.group(2), int(m.group(3)), int(m.group(4))))
            print("   " + l)
    # THE LEAN PROMPT IS THE SYSTEM PROMPT: the safety preamble plus one line.
    # The user half is each rule's own question (events_discovery's is left
    # exactly as it was), so the total is printed for the record, not asserted.
    for rid in ("R3", "R6"):
        got = sizes.get(rid) or []
        check(f"{rid} logged a lean system prompt under 3,000 chars",
              bool(got) and all(k == "lean" and sysn < 3000 for k, _t, sysn in got))
        for _k, total, _s in got:
            print(f"   {rid} total (system + user): {total} chars"
                  + ("  <- over 3,000 because of the rule's own user prompt"
                     if total >= 3000 else ""))
    import persona
    import websearch
    full = len(websearch.SAFETY_PREAMBLE + "\n\n"
               + persona.system_preamble(include_sources=False))
    print(f"   for comparison, the full (non-lean) system prompt is {full} chars")


try:
    asyncio.run(main())
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
sys.exit(1 if failures else 0)
