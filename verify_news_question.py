"""NEWS QUESTIONS ARE ANSWERED FROM THE NEWS ALREADY COLLECTED. Real output.

    python verify_news_question.py

"What is in today's AI news?" used to go to a web search while the feed store
held the day's stories, scored. It now goes to `todays_news`, which reads that
store with R1's own window, not-yet-posted check and scorer.

OFFLINE: a throwaway database per check, no feed fetched, no search made, and
the model client replaced by a table that answers the real scoring prompt and
counts how often it was asked.

  (a) routing: a news question gets the "news" group, and select() hands the
      engine todays_news;
  (b) posted first, then 5 before 3, the 2 dropped, the PoC row with its sheet
      row, at most 12;
  (c) topic="ElevenLabs" returns only the stories that name it;
  (d) unscored rows cost exactly one scoring call, scored rows none, and past
      the token budget the unscored rows come back flagged with no call;
  (e) on a pretend past date the window ends at that day's NEWS_MAIN_TIME;
  (f) the web tail carries no counts, the engine prompt has the never-mention
      rule, and the news block rides only with todays_news.
"""
import asyncio
import json
import logging
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, time, timedelta
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TMP = tempfile.mkdtemp(prefix="saley-newsq-")
os.environ["STATE_DIR"] = os.path.join(TMP, "state")
os.environ["DB_PATH"] = os.path.join(TMP, "base_test.db")

import os as _os  # noqa: E402  NFT2-1064 offline guard: canned source statuses, real Sheets/Drive calls blocked
import sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "tests"))
import offline_guard  # noqa: E402
GUARD = offline_guard.install_script()
import config  # noqa: E402

# THE SHIPPED DEFAULTS, pinned — not whatever the local .env says.
SETTINGS = {
    "DB_PATH": os.environ["DB_PATH"], "STATE_DIR": os.environ["STATE_DIR"],
    "NEWS_MAIN_TIME": "14:00", "NEWS_TOPICS": ["evals", "RLHF", "voice agent"],
    "NEWS_REPEAT_DAYS": 30, "NEWS_FEED_KEEP_DAYS": 14, "NEWS_SCORE_MAX_ITEMS": 40,
    "WEB_SEARCH_ENABLED": True, "SEARCH_BACKEND": "searxng",
    "WEB_QUESTION_MAX_SEARCHES": 3, "TOKEN_DAILY_BUDGET": 0,
}
for _k, _v in SETTINGS.items():
    setattr(config, _k, _v)

import deadlines as dl  # noqa: E402
import feeds  # noqa: E402
import llm as llm_mod  # noqa: E402
import news  # noqa: E402
import query_engine  # noqa: E402
import search_backend  # noqa: E402
import toolsets  # noqa: E402
import usage  # noqa: E402
from bot import SalesBot  # noqa: E402
from db import DB  # noqa: E402

logging.getLogger().setLevel(logging.WARNING)

failures = 0
CALLS: list = []          # one entry per scoring call the model was asked for
SEARCHES: list = []
FRESH = [0]
VERDICTS: dict = {}       # title -> (topic, importance, what happened)
TODAY = [dl.real_today_ist()]
dl.today_ist = lambda: TODAY[0]


def check(name, got, expected=True):
    global failures
    ok = (got == expected)
    failures += 0 if ok else 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}"
          + ("" if ok else f": got {got!r}, want {expected!r}"))


def say(title: str) -> None:
    print("\n" + title)


def _no_search(*a, **k):
    SEARCHES.append(a[:2])
    raise RuntimeError("verify_news_question: a search request was attempted")


search_backend._http = _no_search
search_backend.available = lambda: (True, "")


class ScoreTable:
    """The model client: answers the real scoring prompt from VERDICTS."""

    def create(self, **kw):
        prompt = str((kw.get("messages") or [{}])[-1].get("content") or "")
        CALLS.append(prompt)
        lines = []
        for line in prompt.split("ITEMS:", 1)[-1].splitlines():
            m = re.match(r"\s*(\d+) \|[^|]*\| (.+)", line)
            if not m:
                continue
            verdict = VERDICTS.get(m.group(2).split(" [ABOUT OUR CONTACT")[0].strip())
            if verdict:
                lines.append(f"SCORE | {m.group(1)} | {verdict[0]} | {verdict[1]} "
                             f"| {verdict[2]}")
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="\n".join(lines) or "NOTHING FOUND")],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=900, output_tokens=120))


def item(title: str, when: datetime, *, ref: str = "", source: str = "TechCrunch",
         summary: str = "") -> dict:
    """One feed item as `feeds.parse` would have stored it."""
    url = "https://example.org/" + re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return {"url": url, "url_key": news.url_key(url),
            "headline_key": news.headline_key(title), "title": title,
            "summary": summary, "source": source, "published_at": feeds.utc_iso(when),
            "topic_hint": "", "seen_at": feeds.utc_iso(when),
            "kind": feeds.KIND_POC if ref else feeds.KIND_INDUSTRY, "sheet_ref": ref}


def scored(it: dict, importance: int, topic: str = "evals", what: str = "what happened"):
    return {"url_key": it["url_key"], "importance": importance, "topic": topic,
            "what": what}


def new_bot(items: list, scores: list = ()) -> SalesBot:
    FRESH[0] += 1
    path = os.path.join(TMP, f"q{FRESH[0]}_test.db")
    db = DB(path)
    db.news_feed_add(items)
    db.news_feed_set_scores(list(scores), scored_at=feeds.utc_iso(dl.real_now_ist()))
    config.DB_PATH = path
    bot = SalesBot()
    bot.db = db
    bot._durable_db = None
    bot.llm = llm_mod.LLM("sk-fake-not-used", config.MODEL)
    bot.llm._client = SimpleNamespace(messages=ScoreTable())

    async def no_poll(*_a, **_k):
        return None

    bot._maybe_poll_feeds = no_poll      # the store is filled here, not fetched
    return bot


async def ask(bot: SalesBot, **inp) -> dict:
    return await bot._news_tools()[0]["handler"](inp)


def titles(result: dict) -> list:
    return [i["title"] for i in result["items"]]


async def main():
    now = dl.real_now_ist()
    recent = lambda minutes: now - timedelta(minutes=minutes)   # noqa: E731

    # ------------------------------------------------------------------ (a)
    say("(a) ROUTING")
    bot = new_bot([])
    offered = bot._news_tools() + bot._web_question_tools({})
    question = "what is in todays AI news?"
    check('route() includes "news"', "news" in toolsets.route(question))
    picked, groups, _why = toolsets.select(offered, question)
    names = [t["schema"]["name"] for t in picked]
    check("select() returns todays_news", "todays_news" in names)
    check("...with the web tools behind it", names,
          ["todays_news", "web_search", "fetch_page"])
    full, groups, _why = toolsets.select(offered, "and Globex?")
    check("...and it is in the full set for an unclear question",
          (groups, "todays_news" in [t["schema"]["name"] for t in full]), ([], True))

    # ------------------------------------------------------------------ (b)
    say("(b) THE ORDER — posted, then by importance, the 2 dropped, a PoC row")
    sent = [item("Posted story one", recent(300)), item("Posted story two", recent(290))]
    five = item("Frontier lab ships a new eval suite", recent(200))
    three = item("Startup publishes RLHF tooling notes", recent(30))
    two = item("Ten prompts to try this weekend", recent(20))
    poc = item("Synthflow raises $20M Series A", recent(100),
               ref="Synthflow AI — on Master Pipeline", source="Inc42")
    # Twelve DIFFERENT headlines: the store keeps one row per headline key.
    filler = [item(f"{word} lab opens a research office", recent(40 + n))
              for n, word in enumerate(
                  "Alpha Bravo Charlie Delta Echo Foxtrot Golf Hotel India Juliet "
                  "Kilo Lima".split())]
    bot = new_bot(
        sent + [five, three, two, poc] + filler,
        [scored(s, 4) for s in sent] + [scored(five, 5), scored(three, 3),
                                        scored(two, 2), scored(poc, 4, "OTHER")]
        + [scored(f, 3) for f in filler])
    bot.db.record_news_stories(
        [news.story_from_feed({**s, "importance": 4, "topic": "evals", "what": "posted"})
         for s in sent], on_date=dl.iso(TODAY[0]), rule_id="R1", kind=news.MODE_MAIN)
    CALLS.clear()
    result = await ask(bot)
    got = result["items"]
    print("   window: " + result["window"])
    for i in got:
        print(f"   {'posted' if i['posted'] else '      '} {i['importance']} "
              f"{i['news_kind']:<8} {i['title']}"
              + (f"  ({i['sheet_ref']})" if i["sheet_ref"] else ""))
    check("the posted stories lead", [i["posted"] for i in got[:3]], [True, True, False])
    check("then the 5", got[2]["title"], five["title"])
    check("...before the 3", titles(result).index(five["title"])
          < titles(result).index(filler[0]["title"]), True)
    check("the 2 is dropped", two["title"] in titles(result), False)
    row = next(i for i in got if i["title"] == poc["title"])
    check("the PoC row carries its sheet row",
          (row["news_kind"], row["sheet_ref"]), ("poc", "Synthflow AI — on Master Pipeline"))
    check("at most 12", len(got), 12)
    check("...and it says how many more there are", result["more"], 17 - 12)
    check("every item has a link, a source and a time",
          all(i["url"] and i["source"] and i["published"] for i in got), True)
    size = len(json.dumps(result, ensure_ascii=False))
    print(f"   the result is {size} characters")
    check("the result fits one tool result whole",
          size <= config.QUERY_TOOL_RESULT_MAX_CHARS, True)
    check("the window is named from the previous main post",
          bool(re.match(r"^since \w{3} 2 PM$", result["window"])), True)
    check("no scoring call — everything was scored", len(CALLS), 0)

    # ------------------------------------------------------------------ (c)
    say("(c) A TOPIC")
    eleven = item("ElevenLabs launches a Hindi voice agent", recent(60))
    by_summary = item("Voice startup closes a round", recent(50),
                      summary="Investors back ElevenLabs rival in India")
    other = item("Lab releases an open benchmark", recent(40))
    bot = new_bot([eleven, by_summary, other],
                  [scored(eleven, 4, "voice agent"), scored(by_summary, 3, "voice agent"),
                   scored(other, 4)])
    result = await ask(bot, topic="ElevenLabs")
    check('topic="ElevenLabs" returns only the stories that name it',
          titles(result), [eleven["title"], by_summary["title"]])
    result = await ask(bot, topic="voice agents")
    check('"voice agents" finds the "voice agent" stories', len(result["items"]), 2)
    result = await ask(bot, topic="Globex")
    check("nothing on a name: an empty list and the quiet line",
          (result["items"], result.get("quiet_line")), ([], news.quiet_line(TODAY[0])))

    # ------------------------------------------------------------------ (d)
    say("(d) WHAT IT COSTS")
    known = item("Already scored story", recent(90))
    new_one = item("Regulator publishes AI data rules", recent(15))
    VERDICTS[new_one["title"]] = ("OTHER", 4, "new rules for training data")
    bot = new_bot([known, new_one], [scored(known, 3)])
    CALLS.clear()
    result = await ask(bot)
    check("unscored rows: exactly one scoring call", len(CALLS), 1)
    check("...and the new story is in the answer, scored",
          [(i["title"], i["importance"]) for i in result["items"]][0],
          (new_one["title"], 4))
    CALLS.clear()
    again = await ask(bot)
    check("asked again, all scored: zero calls", len(CALLS), 0)
    check("...the same stories, read from the store", titles(again), titles(result))

    late = item("Late unscored headline", recent(5))
    bot = new_bot([known, late], [scored(known, 3)])
    real_over = usage.over_budget
    usage.over_budget = lambda: True
    CALLS.clear()
    try:
        result = await ask(bot)
    finally:
        usage.over_budget = real_over
    check("over the token budget: zero calls", len(CALLS), 0)
    check("...the scored story first, the unscored one flagged",
          [(i["title"], bool(i.get("unscored"))) for i in result["items"]],
          [(known["title"], False), (late["title"], True)])
    check("no search was made anywhere", SEARCHES, [])

    # ------------------------------------------------------------------ (e)
    say("(e) A PRETEND PAST DATE")
    past = TODAY[0] - timedelta(days=7)
    while past.weekday() != 0:
        past -= timedelta(days=1)                       # a Monday
    at = lambda hh: datetime.combine(past, time(hh, 0), dl.IST)   # noqa: E731
    before = item("Monday morning story", at(13))
    after = item("Monday afternoon story", at(15))
    weekend = item("Saturday story", at(13) - timedelta(days=2))
    bot = new_bot([before, after, weekend],
                  [scored(before, 4), scored(after, 5), scored(weekend, 3)])
    TODAY[0] = past
    try:
        since, until = bot._main_window(past)
        result = await ask(bot)
    finally:
        TODAY[0] = dl.real_today_ist()
    print("   window: " + result["window"])
    check("the window ends at that day's NEWS_MAIN_TIME",
          (until, result["window"].endswith(", to Mon 2 PM")), (at(14), True))
    check("...and starts at the previous main post",
          result["window"].startswith(f"since {since.strftime('%a')} 2 PM"), True)
    check("a story from after 14:00 that day is not there",
          titles(result), [before["title"], weekend["title"]])

    # ------------------------------------------------------------------ (f)
    say("(f) NOTHING ABOUT HOW IT LOOKED")
    bot = new_bot([])
    tools, _note, tail = await bot._websearch_tools({})
    print("   the tail: " + tail)
    check("the web tools are attached", [t["schema"]["name"] for t in tools],
          ["web_search", "fetch_page"])
    check('the tail no longer says "requests are left"', "requests are left" in tail,
          False)
    check("...and carries no number at all", bool(re.search(r"\d", tail)), False)
    check('...but still asks for short, specific queries',
          "short and specific" in tail, True)
    with_news = query_engine._engine_text(
        requester_name="V", today="2026-10-05",
        tool_names=["todays_news", "web_search", "fetch_page"])
    without = query_engine._engine_text(
        requester_name="V", today="2026-10-05", tool_names=["lookup_company"])
    rule = "Do not mention searches, quotas, budgets"
    check("the never-mention rule is in every engine prompt",
          (rule in with_news, rule in without), (True, True))
    check("the news block rides with todays_news",
          "=== NEWS QUESTIONS" in with_news, True)
    check("...and only with it", "=== NEWS QUESTIONS" in without, False)
    check("...the OUTPUT rules still follow it", "=== OUTPUT ===" in without
          and with_news.index("=== NEWS QUESTIONS") < with_news.index("=== OUTPUT ==="),
          True)
    check("todays_news has a one-line description", "todays_news" in toolsets.ONE_LINE,
          True)

    print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")


try:
    asyncio.run(main())
finally:
    shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if failures else 0)
