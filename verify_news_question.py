"""NEWS QUESTIONS ARE ANSWERED FROM THE NEWS ALREADY COLLECTED. Real output.

    python verify_news_question.py

"What is in today's AI news?" used to go to a web search while the feed store
held the day's stories, scored. It is answered from that store, with R1's own
scorer. SINCE 8 OCT THE LIST IS CHOSEN AND WRITTEN BY CODE (`_news_answer`,
`news.choose_answer`, `news.render`), never by the model, and the answer says
nothing about when anything is posted (docs/plans/NEWS-OCT8.md).

OFFLINE: a throwaway database per check, no feed fetched, no search made, and
the model client replaced by a table that answers the real scoring prompt and
counts how often it was asked.

  (a) routing: a news question gets the "news" group, and select() hands the
      engine todays_news;
  (b) the 5 highest-scored stories NOT sent before, listed newest first, in
      the one template (bold headline, outlet link, a blank line between
      stories); a PoC story keeps its own line; fewer than 5 unsent gives
      those; nothing unsent gives the top 5 again; an answer's stories are
      recorded as sent;
  (c) topic="ElevenLabs" gives only the stories that name it;
  (d) unrated rows cost exactly one scoring call, rated rows none, and past
      the token budget there is no call and nothing is guessed at;
  (e) on a pretend past date the news it reads ends at that day's
      NEWS_MAIN_TIME, and the answer never says so;
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
    "SALES_DRIP_START": "14:00", "SALES_DRIP_END": "20:00", "MESSAGE_GAP_MINUTES": 120,
    "MESSAGE_GAP_MIN_MINUTES": 120, "MESSAGE_JITTER_MINUTES": 0,
    "NEWS_TOPICS": ["evals", "RLHF", "voice agent"],
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
    """What the picker chose and rendered (`_news_answer`), as the answer path
    uses it: {"block", "stories", "repeat", "quiet", ...}."""
    return await bot._news_answer(topic=str(inp.get("topic") or ""),
                                  days=inp.get("days") or 0)


async def tool(bot: SalesBot, **inp) -> dict:
    """What the MODEL is handed back by todays_news."""
    return await bot._news_tools()[0]["handler"](inp)


def titles(result: dict) -> list:
    return [s["headline"] for s in result["stories"]]


BANNED = ("2 PM", "already", "scheduled", "since", "ran", "posted", "window")


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
    say("(b) WHICH STORIES — the 5 highest-scored not sent before, newest first")
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
    fours = filler[:4]
    bot = new_bot(
        sent + [five, three, two, poc] + filler,
        [scored(s, 4) for s in sent] + [scored(five, 5), scored(three, 3),
                                        scored(two, 2), scored(poc, 5, "OTHER")]
        + [scored(f, 4) for f in fours] + [scored(f, 3) for f in filler[4:]])
    bot.db.record_news_stories(
        [news.story_from_feed({**s, "importance": 4, "topic": "evals", "what": "posted"})
         for s in sent], on_date=dl.iso(TODAY[0]), rule_id="R1", kind=news.MODE_MAIN)
    CALLS.clear()
    result = await ask(bot)
    print("\n".join("   " + line for line in result["block"].splitlines()))
    got = titles(result)
    check("exactly five", len(got), 5)
    check("the 5 is in", five["title"] in got, True)
    check("what was already sent is NOT", [s["title"] in got for s in sent], [False, False])
    check("the two 5s, then the 4s — at a tie the newer ones",
          sorted(got), sorted([five["title"], poc["title"]] + [f["title"] for f in fours[:3]]))
    check("the 3s and the 2 are not there",
          (three["title"] in got, two["title"] in got), (False, False))
    check("listed newest first",
          got, [f["title"] for f in fours[:3]] + [poc["title"], five["title"]])
    lines = [ln for ln in result["block"].split("\n") if ln.startswith("- ")]
    check("the heading is bold, with the day", result["block"].split("\n")[0],
          news.heading(news.MODE_ANSWER, TODAY[0]))
    check("then a blank line", result["block"].split("\n")[1], "")
    check("a blank line between the stories", "\n\n- " in result["block"], True)
    check("an industry story is a bold headline and its outlet",
          lines[0], f"- **{fours[0]['title']}** ([example.org](<{fours[0]['url']}>))")
    check("the PoC story keeps its own line and its sheet row",
          lines[3], f"- {poc['title']} — what happened. (Synthflow AI — on Master "
                    f"Pipeline) [example.org](<{poc['url']}>)")
    check("it is not a repeat", result["repeat"], False)
    check("nothing in it about when anything is posted",
          [w for w in BANNED if w.lower() in result["block"].lower()], [])
    check("no scoring call — everything was scored", len(CALLS), 0)
    handed = await tool(bot)
    print("   the model is handed: " + json.dumps(handed, ensure_ascii=False)[:160])
    check("with no reply to put it in, the tool hands the list over whole",
          handed.get("list"), result["block"])
    check("...and nothing called `window`, `items` or `posted`",
          [k for k in ("window", "items", "posted", "more") if k in handed], [])

    say("(b2) FEWER THAN FIVE, AND NOTHING LEFT")
    a, b, c = (item(f"{w} startup raises a seed round", recent(60 + n))
               for n, w in enumerate(("Xray", "Yankee", "Zulu")))
    old = [item(f"{w} model tops a benchmark", recent(120 + n))
           for n, w in enumerate("Mike November Oscar Papa Quebec Romeo".split())]
    bot = new_bot([a, b, c] + old,
                  [scored(a, 3), scored(b, 5), scored(c, 4)]
                  + [scored(o, imp) for o, imp in zip(old, (5, 5, 4, 4, 3, 3))])
    bot.db.record_news_stories(
        [news.story_from_feed({**o, "importance": imp, "topic": "evals", "what": ""})
         for o, imp in zip(old, (5, 5, 4, 4, 3, 3))],
        on_date=dl.iso(TODAY[0]), rule_id="R1", kind=news.MODE_MAIN)
    result = await ask(bot)
    check("three unsent: those three and no padding, newest first",
          titles(result), [a["title"], b["title"], c["title"]])
    wrote = await bot._record_news_answer(result)
    check("an answer's stories are recorded as sent, kind=answer",
          (wrote, bot.db.news_story_seen(a["url_key"], a["headline_key"],
                                         since_iso="2000-01-01")["kind"]),
          (3, news.MODE_ANSWER))
    again = await ask(bot)
    check("asked again with nothing unsent: the five highest-scored, again",
          (again["repeat"], sorted(titles(again))),
          (True, sorted([b["title"], c["title"], old[0]["title"], old[1]["title"],
                         old[2]["title"]])))
    check("...listed newest first",
          titles(again), [b["title"], c["title"], old[0]["title"], old[1]["title"],
                          old[2]["title"]])
    check("a repeat is not recorded a second time",
          await bot._record_news_answer(again), 0)
    newcomer = item("Regulator opens an inquiry into model evals", recent(2))
    bot.db.news_feed_add([newcomer])
    bot.db.news_feed_set_scores([scored(newcomer, 3)],
                                scored_at=feeds.utc_iso(dl.real_now_ist()))
    result = await ask(bot)
    check("one new story arrives: it alone is given, however low it scored",
          (titles(result), result["repeat"]), ([newcomer["title"]], False))
    empty = await ask(new_bot([]))
    check("nothing collected at all: no list, and the quiet line",
          (empty["block"], empty["quiet"]), ("", news.quiet_line(TODAY[0])))

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
    check('topic="ElevenLabs" gives only the stories that name it, newest first',
          titles(result), [by_summary["title"], eleven["title"]])
    result = await ask(bot, topic="voice agents")
    check('"voice agents" finds the "voice agent" stories', len(result["stories"]), 2)
    handed = await tool(bot, topic="Globex")
    check("nothing on a name: no stories, and the model may search for it",
          (handed["stories"], "web_search" in handed.get("note", "")), (0, True))

    # ------------------------------------------------------------------ (d)
    say("(d) WHAT IT COSTS")
    known = item("Already scored story", recent(90))
    new_one = item("Regulator publishes AI data rules", recent(15))
    VERDICTS[new_one["title"]] = ("OTHER", 4, "new rules for training data")
    bot = new_bot([known, new_one], [scored(known, 3)])
    CALLS.clear()
    result = await ask(bot)
    check("unrated rows: exactly one scoring call", len(CALLS), 1)
    check("...and the new story is in the answer, with its score",
          [(s["headline"], s["importance"]) for s in result["stories"]][0],
          (new_one["title"], 4))
    CALLS.clear()
    again = await ask(bot)
    check("asked again, all rated: zero calls", len(CALLS), 0)
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
    check("...the rated story is still given; the unrated one is not guessed at",
          (titles(result), result["unscored"]), ([known["title"]], 1))
    check("no search was made anywhere", SEARCHES, [])

    # ------------------------------------------------------------------ (e)
    say("(e) A PRETEND PAST DATE")
    past = TODAY[0] - timedelta(days=7)
    while past.weekday() != 0:
        past -= timedelta(days=1)                       # a Monday
    at = lambda hh: datetime.combine(past, time(hh, 0), dl.IST)   # noqa: E731
    # AI NEWS IS THIRD IN MONDAY'S ORDER (NFT2-1069): its slot is 18:00, where it
    # used to be a fixed 14:00. One story before the slot, one after it.
    before = item("Monday morning story", at(13))
    after = item("Monday evening story", at(19))
    weekend = item("Saturday story", at(13) - timedelta(days=2))
    bot = new_bot([before, after, weekend],
                  [scored(before, 4), scored(after, 5), scored(weekend, 3)])
    TODAY[0] = past
    try:
        _since, until = bot._main_window(past)
        result = await ask(bot)
    finally:
        TODAY[0] = dl.real_today_ist()
    print("\n".join("   " + line for line in result["block"].splitlines()))
    check("on a pretend past day the news it reads ends at R1's slot in that day's "
          "order (Monday: third, 18:00)", until, at(18))
    check("...so a story from after 18:00 that day is not there",
          titles(result), [before["title"], weekend["title"]])
    check("...the heading names the pretend day",
          result["block"].split("\n")[0], news.heading(news.MODE_ANSWER, past))
    check("...and the answer still says nothing about a time or a window",
          [w for w in BANNED if w.lower() in result["block"].lower()], [])

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
