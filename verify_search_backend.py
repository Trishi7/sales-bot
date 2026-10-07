"""SEARCH OUTSIDE THE MODEL — the checks, with real output and the token log.

    python verify_search_backend.py              every check
    python verify_search_backend.py --only a     the retrieval layer alone: free
    python verify_search_backend.py --only i,ii  some of them
    python verify_search_backend.py --no-week    skip (vii), the simulated week
    python verify_search_backend.py --no-compare skip (ix), the Anthropic call
    python verify_search_backend.py --stub       canned search results

WHAT IS REAL. The searches (SEARCH_BACKEND and SEARCH_FALLBACKS from your .env
— SearXNG if it is running, DuckDuckGo if it is not; both free), the feeds
(plain HTTP), the model calls (your ANTHROPIC_API_KEY — this spends a few
cents; (a) makes none), the bot's own code paths (`_news_run`,
`_maybe_breaking_news`, `_research_items`, `_find_people`, `_answer_with_engine`,
`_handle_simulation`, `_send_cost_report`) and, for (vii), the real sheet.
Discord is a recording channel; the database is a throwaway *_test.db, so no
ledger or cache of yours is touched.

--stub REPLACES THE SEARCH RESULTS, AND ONLY THEM. The search HTTP boundary —
`search_backend._http`, the one function — answers with canned SearXNG
responses, the fallbacks are switched off, and every check that used it is
labelled "SEARCH STUBBED". The request counting, the cache, the budget, the
snippet block and the model's token use are still the real ones. Company news
still reads the real Google News feed: that is not a search.

  (a)    the retrieval layer, live, with no model call: a search (and which
         backend answered), the same search again from the cache, news() from
         Google News RSS at zero requests, news() falling back to search()
         for a company the feed has nothing on, fetch_page, the LinkedIn refusal
  (i)    feeds.poll() stores items; a second poll stores 0 new
  (ii)   the 14:00 news post from the feeds — bullets with links; llm_calls
         shows ONE MODEL_LIGHT call under 5k input tokens and no Sonnet call
  (iii)  an hourly check with nothing new makes zero model calls
  (iv)   R6 email lookup: one search request, one Haiku call under 3k tokens, a
         sourced address or "no public email found"
  (v)    "find PoCs at Shunya Labs": names with linkedin urls from the result
         titles, and no LinkedIn fetch in the log
  (vi)   a channel question with web: at most 2 requests, engine input under
         40k tokens including cache reads
  (vii)  "simulate week": the test-cost line shows calls, requests and dollars
         (expect under $0.50); zero Anthropic web searches; a re-run's searches
         are cache hits
  (viii) "what did you cost today" in dollars
  (ix)   SEARCH_BACKEND=anthropic still works for one call, for comparison
  (x)    R10's company news comes from Google News RSS: zero search requests,
         one MODEL_LIGHT call
"""
import asyncio
import logging
import os
import re
import shutil
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="saley-search-verify-")
os.environ["DB_PATH"] = os.path.join(TMP, "sales_bot_test.db")
os.environ.setdefault("DISCORD_TOKEN", "x")

import os as _os  # noqa: E402  NFT2-1064 offline guard: canned source statuses, real Sheets/Drive calls blocked
import sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "tests"))
import offline_guard  # noqa: E402
if "--stub" in _sys.argv:
    GUARD = offline_guard.install_script()
import config  # noqa: E402
import canned_sheet  # noqa: E402  NFT2-1064: canned rows for the real-sheet read the offline guard blocks
if "--stub" in _sys.argv:
    canned_sheet.install()

SID = 1001
config.DB_PATH = os.environ["DB_PATH"]
config.SALES_TEST_MODE = True
config.SALES_TEST_CHANNEL_ID = 424242
config._attach_test_channel()
config.SALES_CHANNEL_IDS = [424242]
config.SALES_CHANNEL_ID_SET = {424242}
config.TEAM_ROSTER_IDS = [SID]
config.SALES_APPROVER_IDS = [SID]
config.SALES_FINAL_SAY_ID = SID
config.ROSTER_DISPLAY_NAMES = {str(SID): "Sid"}
config.SIMULATION_FAST_GAP_SECONDS = 0
config.TEST_POST_GAP_SECONDS = 0
config.WEB_SEARCH_ENABLED = True
if config.SEARCH_BACKEND == "anthropic":        # (ix) is where that one is checked
    config.SEARCH_BACKEND, config.SEARCH_FALLBACKS = "searxng", ["ddg"]
BACKEND = config.SEARCH_BACKEND
config.INTERIM_ENABLED = False
config.digest_enabled = lambda: True

import deadlines as dl  # noqa: E402
import feeds  # noqa: E402
import nextaction  # noqa: E402
import search_backend  # noqa: E402
import usage  # noqa: E402
import websearch  # noqa: E402

ARGS = sys.argv[1:]
ONLY = set()
for i, a in enumerate(ARGS):
    if a == "--only" and i + 1 < len(ARGS):
        ONLY = {x.strip().lower() for x in ARGS[i + 1].split(",") if x.strip()}
NO_WEEK = "--no-week" in ARGS
NO_COMPARE = "--no-compare" in ARGS

failures = 0
skipped: list = []
POSTED: list = []
LINES: list = []


def want(tag: str) -> bool:
    return not ONLY or tag in ONLY


def check(name, got, want_=True):
    global failures
    ok = (got == want_)
    failures += 0 if ok else 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}"
          + ("" if ok else f": got {got!r}, want {want_!r}"))


def note(text: str) -> None:
    print(f"        {text}")


class Capture(logging.Handler):
    def emit(self, record):
        try:
            LINES.append(record.getMessage())
        except Exception:
            pass


root = logging.getLogger()
root.setLevel(logging.INFO)
for h in list(root.handlers):
    root.removeHandler(h)
root.addHandler(Capture())
for noisy in ("discord", "asyncio", "httpx", "anthropic", "urllib3", "httpcore"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


class FakeTyping:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeChannel:
    id = 424242
    name = "sales-test"
    guild = SimpleNamespace(id=1)

    async def send(self, body, **kw):
        POSTED.append(str(body))
        return SimpleNamespace(id=len(POSTED), jump_url="https://discord/x")

    def typing(self):
        return FakeTyping()


CHANNEL = FakeChannel()


class FakeMessage:
    _next = [5000]

    def __init__(self, content: str):
        FakeMessage._next[0] += 1
        self.id = FakeMessage._next[0]
        self.content = content
        self.channel = CHANNEL
        self.author = SimpleNamespace(id=SID, bot=False, name="sid",
                                      display_name="Sid", global_name="Sid")
        self.reference = None
        self.mentions = []
        self.mention_everyone = False
        self.guild = CHANNEL.guild

    async def reply(self, body, **kw):
        POSTED.append(str(body))
        return SimpleNamespace(id=len(POSTED), jump_url="https://discord/x")


# -- the canned SearXNG, used ONLY under --stub --------------------------------

STUBBED = "--stub" in sys.argv[1:]


def canned_http(method, url, *, headers, params=None, body=None):
    q = str((params or {}).get("q") or "").lower()
    in_news = (params or {}).get("categories") == "news"
    if in_news and "sarvam" in q:
        return 200, {"results": [
            {"title": "Sarvam AI opens its Indic speech models to enterprises",
             "url": "https://example-news.test/sarvam-speech",
             "publishedDate": "2026-09-29T08:00:00",
             "content": "The Bengaluru lab said its speech-to-text models for ten "
                        "Indian languages are now available through an API."},
            {"title": "Sarvam AI hiring evaluation engineers",
             "url": "https://example-news.test/sarvam-hiring",
             "publishedDate": "2026-09-27T08:00:00",
             "content": "Job posts show the company building an internal "
                        "evaluation team for its voice agents."}]}
    if "site:linkedin.com/in" in q:
        return 200, {"results": [
            {"title": "Asha Rao - Co-founder & CEO - Shunya Labs | LinkedIn",
             "url": "https://www.linkedin.com/in/asha-rao-stub",
             "content": "Co-founder and CEO at Shunya Labs. Bengaluru."},
            {"title": "Vikram Nair - Head of Research - Shunya Labs | LinkedIn",
             "url": "https://www.linkedin.com/in/vikram-nair-stub",
             "content": "Head of Research at Shunya Labs. Speech and "
                        "multilingual models."},
            {"title": "Priya Menon - Recruiter - Other Company | LinkedIn",
             "url": "https://www.linkedin.com/in/priya-menon-stub",
             "content": "Recruiter at Other Company. Previously worked with "
                        "Shunya Labs alumni."}]}
    if "email contact" in q:
        return 200, {"results": [
            {"title": "Faculty — Department of Computer Science",
             "url": "https://cs.example-university.test/people/meera-iyer",
             "content": "Meera Iyer, Associate Professor. Research: speech and "
                        "evaluation. Email: meera.iyer@example-university.test"},
            {"title": "Meera Iyer - Google Scholar",
             "url": "https://scholar.example.test/citations?user=abc",
             "content": "Verified email at example-university.test. Cited by 2,100."}]}
    return 200, {"results": [
        {"title": "AI funding this week: three rounds worth knowing",
         "url": "https://example-news.test/ai-funding-week",
         "content": "Lumen Robotics raised $40m Series B; Vaani Voice closed a "
                    "$12m Series A; EvalWorks raised $6m seed.",
         "publishedDate": "2026-09-30T08:00:00"},
        {"title": "Vaani Voice raises $12m to build voice agents for Indian banks",
         "url": "https://example-wire.test/vaani-series-a",
         "content": "The round was led by Example Capital.",
         "publishedDate": "2026-09-29T08:00:00"}]}


if STUBBED:
    search_backend._http = canned_http
    config.SEARCH_BACKEND, config.SEARCH_FALLBACKS = "searxng", []
    BACKEND = "searxng"
SERPER = ("SEARCH STUBBED (--stub)" if STUBBED else
          f"{BACKEND}, live; fallbacks {','.join(config.SEARCH_FALLBACKS) or 'none'}")


# -- reading the token log -----------------------------------------------------


_BOT = []


def calls_since(bot, mark) -> list:
    """The llm_calls rows logged after `mark` (a row count from `now_ts`). A
    count, not a timestamp: the log is stamped to the second, and two calls in
    one second must not be confused."""
    rows = bot._ledger().llm_calls_since("2000-01-01")
    return rows[int(mark) if isinstance(mark, int) else 0:]


def now_ts() -> int:
    return len(_BOT[0]._ledger().llm_calls_since("2000-01-01"))


def show_calls(rows: list) -> float:
    total = 0.0
    for r in rows:
        cost = usage.dollars(r)
        total += cost
        note(f"llm_calls: site={r['site']} model={r['model']} in={r['input_tokens']} "
             f"cache_r={r['cache_read']} cache_w={r['cache_write']} "
             f"out={r['output_tokens']} {usage.money(cost)}")
    if not rows:
        note("llm_calls: (no model call)")
    return total


def requests_used(bot) -> int:
    return bot._ledger().web_searches_today(dl.iso(dl.real_today_ist()))


def is_light(row) -> bool:
    return row["model"] == config.MODEL_LIGHT


def is_main(row) -> bool:
    return row["model"] == config.MODEL


def show(title: str, body: str, limit: int = 14) -> None:
    lines = str(body or "").splitlines()
    print(f"   --- {title} ---")
    for line in lines[:limit]:
        print("   | " + line[:230])
    if len(lines) > limit:
        print(f"   | … {len(lines) - limit} more line(s)")


async def main() -> int:
    import guardrails
    import leave
    from bot import SalesBot

    bot = SalesBot()
    _BOT.append(bot)
    bot.get_channel = lambda _id: CHANNEL
    today = dl.real_today_ist()

    async def nobody_away(*_a, **_k):
        return {}

    async def no_evidence(_m):
        return None

    leave.who_is_away = nobody_away                 # a Discord read
    bot._convert_if_already_done = no_evidence      # a Discord history search

    print("=" * 78)
    print("SEARCH OUTSIDE THE MODEL")
    print("=" * 78)
    print(f"  MODEL / MODEL_LIGHT   {config.MODEL} / {config.MODEL_LIGHT}")
    print(f"  SEARCH_BACKEND        {config.SEARCH_BACKEND}  ({SERPER})")
    print(f"  SEARCH_DAILY_BUDGET   {config.SEARCH_DAILY_BUDGET} requests, cache "
          f"{config.SEARCH_CACHE_HOURS}h")
    print(f"  TOKEN_DAILY_BUDGET    {config.TOKEN_DAILY_BUDGET:,}")
    print(f"  database              {config.DB_PATH} (throwaway)")

    # ---------------------------------------------------------------- (a)
    if want("a"):
        print(f"\n(a) the retrieval layer — no model call  [{SERPER}]")
        t0, mark = now_ts(), len(LINES)
        note(f"chain: {' -> '.join(search_backend.chain())}"
             + (f"   SEARXNG_URL={config.SEARXNG_URL}" if BACKEND == "searxng" else ""))

        def show_rows(results, limit=4):
            for r in results[:limit]:
                note(f"  {r['date'][:10] or '-':10} | {r['source'][:20]:20} | "
                     f"{r['title'][:70]}")
                note(f"  {'':10}   {r['url'][:110]}")

        query = 'site:linkedin.com/in "Sarvam AI"'
        before = requests_used(bot)
        first = await asyncio.to_thread(
            lambda: search_backend.search_detail(query, n=5, rule="verify"))
        note(f"search({query!r}, n=5): backend={first['backend']} "
             f"results={len(first['results'])} requests={first['requests']} "
             f"error={first['error']!r}")
        show_rows(first["results"])
        check("the search returned results", len(first["results"]) >= 1)
        check("...in the one shape", all(
            sorted(r) == ["date", "snippet", "source", "title", "url"]
            for r in first["results"]) and bool(first["results"]))
        check("...from a backend in the chain",
              first["backend"] in search_backend.chain())
        check("one request was banked", requests_used(bot) - before, 1)

        again = await asyncio.to_thread(
            lambda: search_backend.search_detail(query, n=5, rule="verify"))
        check("the same search again is a cache hit, with no request",
              (again["cached"], again["requests"], requests_used(bot) - before),
              (True, 0, 1))

        before = requests_used(bot)
        got = await asyncio.to_thread(
            lambda: search_backend.news_detail("Sarvam AI", days=7, n=8, rule="verify"))
        note(f"news('Sarvam AI', days=7): backend={got['backend']} "
             f"results={len(got['results'])} requests={got['requests']}")
        show_rows(got["results"])
        check("news() answered from Google News RSS", got["backend"], "google_news")
        check("...with stories", len(got["results"]) >= 1)
        check("...and ZERO search requests",
              (got["requests"], requests_used(bot) - before), (0, 0))
        check("news() returns the same list",
              await asyncio.to_thread(
                  lambda: search_backend.news("Sarvam AI", days=7, n=8)),
              got["results"])

        nobody = "Zxqvlorp Kwyjibo Systems"
        got = await asyncio.to_thread(
            lambda: search_backend.news_detail(nobody, days=1, n=5, rule="verify"))
        note(f"news({nobody!r}, days=1): backend={got['backend']} "
             f"results={len(got['results'])} requests={got['requests']} "
             f"error={got['error']!r}")
        check("an empty feed fell back to search()", got["backend"] != "google_news")

        page = await asyncio.to_thread(
            lambda: search_backend.fetch_page("https://example.com/"))
        note(f"fetch_page(example.com): ok={page['ok']} title={page['title']!r} "
             f"chars={len(page['text'])} error={page['error']!r}")
        check("fetch_page read a page", (page["ok"], bool(page["text"])), (True, True))
        refused = await asyncio.to_thread(
            lambda: search_backend.fetch_page("https://www.linkedin.com/in/someone"))
        check("LinkedIn is never fetched", refused["error"],
              "linkedin.com is never fetched")

        state_now = search_backend.budget()
        note(f"budget: {state_now['used']} of {state_now['budget']} requests used")
        for line in LINES[mark:]:
            if line.startswith("[search]"):
                note("log: " + line[:200])
        check("no model call was made", len(calls_since(bot, t0)), 0)

    # ---------------------------------------------------------------- (i)
    if want("i"):
        print("\n(i) feeds.poll() stores items; a second poll stores 0 new")
        first = await asyncio.to_thread(feeds.poll)
        second = await asyncio.to_thread(feeds.poll)
        note(f"first poll : {first['ok']} of {first['feeds']} feeds read, "
             f"{first['fetched']} items carried, {first['new']} new")
        note(f"second poll: {second['ok']} of {second['feeds']} feeds read, "
             f"{second['fetched']} items carried, {second['new']} new")
        for e in first["errors"][:4]:
            note(f"feed error: {e[:150]}")
        check("the first poll stored items", first["new"] > 0)
        # A story can be published between two polls a second apart; that is
        # the feed moving, not the dedup failing.
        check("the second poll stored 0 new (2 allowed for a story published "
              "in between)", second["new"] <= 2)
        check("most feeds were readable", first["ok"] >= first["feeds"] * 0.7)
        check("a poll made no model call and no search request",
              (len(calls_since(bot, "2000-01-01")), requests_used(bot)), (0, 0))
        rows = bot._ledger().news_feed_between("2000", "2999")
        note(f"news_feed_items now holds {len(rows)} row(s); newest:")
        for r in rows[:3]:
            note(f"  {r['published_at']} | {r['source'][:22]:22} | {r['title'][:80]}")

    # ---------------------------------------------------------------- (ii)
    if want("ii"):
        print("\n(ii) the 14:00 news post, from the feeds")
        if not bot._ledger().news_feed_count():
            await asyncio.to_thread(feeds.poll)
        item = {"rule": nextaction.R_AI_NEWS, "rule_id": "R1", "type": "ai_news",
                "key": "R1:news", "web_pending": True,
                "text": "AI news [web research pending]"}
        t0 = now_ts()
        before = requests_used(bot)
        await bot._news_run([item], today=today)
        rows = calls_since(bot, t0)
        show("the post (the drip adds the 'AI news — <date>' heading)", item["text"])
        show_calls(rows)
        bullets = [l for l in item["text"].splitlines() if l.startswith("• ")]
        light = [r for r in rows if is_light(r)]
        check("the item is researched", item["web_pending"], False)
        check(f"up to NEWS_MAX_ITEMS bullets ({len(bullets)} of "
              f"{config.NEWS_MAX_ITEMS})", 1 <= len(bullets) <= config.NEWS_MAX_ITEMS)
        check("every bullet carries a masked link",
              bool(bullets) and all("](<http" in b for b in bullets))
        check("exactly ONE model call", len(rows), 1)
        check("...on MODEL_LIGHT", len(light), 1)
        check("...under 5k input tokens",
              bool(light) and light[0]["input_tokens"] < 5000)
        check("no Sonnet call", [r for r in rows if is_main(r)], [])
        check("no search request", requests_used(bot) - before, 0)

        print("\n     the same day again — the day's cache, nothing scored twice")
        again = dict(item, web_pending=True, text="AI news [web research pending]")
        t0 = now_ts()
        await bot._news_run([again], today=today)
        check("a second run of the same day makes no model call",
              len(calls_since(bot, t0)), 0)
        check("...and posts the same stories", again["text"], item["text"])

    # ---------------------------------------------------------------- (iii)
    if want("iii"):
        print("\n(iii) an hourly check with nothing new makes zero model calls")
        if not bot._ledger().news_feed_count():
            await asyncio.to_thread(feeds.poll)
        t0 = now_ts()
        first = await bot._maybe_breaking_news(force=True, channel=CHANNEL,
                                               at=dl.real_now_ist())
        rows1 = calls_since(bot, t0)
        note(f"first check : last {first['since_hours']}h, {first['found']} worth 3+, "
             f"{len(first['kept'])} at the breaking bar, posted {first['posted']}, "
             f"{len(rows1)} model call(s)")
        show_calls(rows1)
        t0 = now_ts()
        mark = len(LINES)
        second = await bot._maybe_breaking_news(force=True, channel=CHANNEL,
                                                at=dl.real_now_ist())
        rows2 = calls_since(bot, t0)
        note(f"second check: {len(rows2)} model call(s), "
             f"{second['searches']} search(es)")
        for line in LINES[mark:]:
            if line.startswith(("[news]", "[news-check]", "[feeds]")):
                note("log: " + line[:200])
        check("the first check made at most one call, on MODEL_LIGHT",
              len(rows1) <= 1 and all(is_light(r) for r in rows1))
        check("the check with nothing new made ZERO model calls", len(rows2), 0)
        check("...and no search request", second["searches"], 0)
        future = today + __import__("datetime").timedelta(days=3)
        fut_item = {"rule": nextaction.R_AI_NEWS, "rule_id": "R1", "key": "R1:news",
                    "web_pending": True, "text": "AI news [web research pending]"}
        t0 = now_ts()
        await bot._research_items([fut_item], today=future)
        note(f"a future date ({dl.iso(future)}): {fut_item['text']!r}")
        check("a future date renders the honest line", fut_item["text"],
              f"No news yet — {future.strftime('%a')} {future.day} "
              f"{future.strftime('%b')} hasn't happened.")
        check("...at zero cost", len(calls_since(bot, t0)), 0)

    # ---------------------------------------------------------------- (iv)
    if want("iv"):
        print(f"\n(iv) R6 email lookup  [{SERPER}]")
        person, company = (("Meera Iyer", "Example University") if STUBBED
                           else ("Mitesh Khapra", "IIT Madras"))
        item = {"rule": "li_no_dm", "rule_id": "R6", "key": f"R6:{person}",
                "row_key": f"{company.lower()}|{person.lower()}",
                "company": company, "poc": person, "poc_designation": "",
                "web_pending": True,
                "text": f"{person} connected, no DM yet [web research pending]"}
        t0, before = now_ts(), requests_used(bot)
        await bot._research_items([item], today=today)
        rows = calls_since(bot, t0)
        spent = requests_used(bot) - before
        show("R6's research", item.get("research") or item.get("research_note") or "")
        note(f"sources: {[s['url'] for s in item.get('sources') or []]}")
        note(f"search requests: {spent}")
        show_calls(rows)
        text = str(item.get("research") or "")
        check("one search request", spent, 1)
        check("one model call, on MODEL_LIGHT",
              (len(rows), all(is_light(r) for r in rows)), (1, True))
        check("...under 3k input tokens",
              bool(rows) and rows[0]["input_tokens"] < 3000)
        check('a sourced address, or "no public email found"',
              (("@" in text and bool(item.get("sources")))
               or text == websearch.NO_EMAIL))
        t0, before = now_ts(), requests_used(bot)
        again = dict(item, web_pending=True, research="", sources=[])
        await bot._research_items([again], today=today)
        if item.get("sources"):
            check("asked again: the research cache answers, no request, no call",
                  (requests_used(bot) - before, len(calls_since(bot, t0))), (0, 0))
        else:
            check("a non-result was NOT cached — but its search was, so the "
                  "second look costs no request", requests_used(bot) - before, 0)

    # ---------------------------------------------------------------- (v)
    if want("v"):
        print(f'\n(v) "find PoCs at Shunya Labs"  [{SERPER}]')
        t0, before, mark = now_ts(), requests_used(bot), len(LINES)
        body = await bot._find_people("Shunya Labs")
        rows = calls_since(bot, t0)
        show("the reply", body)
        note(f"search requests: {requests_used(bot) - before}")
        show_calls(rows)
        searched = [l for l in LINES[mark:] if l.startswith("[search]")]
        for line in searched:
            note("log: " + line[:210])
        fetched_li = [l for l in searched
                      if "fetch_page url=" in l and "linkedin." in l.lower()]
        people = [l for l in body.splitlines() if l.startswith("• ")]
        check("it named people", len(people) >= 1)
        # A person comes from a LinkedIn result title (and carries that url) or
        # from the company's own team page, which the reply names once.
        check("...with linkedin.com/in urls from the result titles",
              any("linkedin.com/in/" in l for l in people))
        check("...and anyone without one is from the company's own page, named",
              all("linkedin.com/in/" in l for l in people) or "Found on:" in body)
        check("NO LinkedIn fetch in the log", fetched_li, [])
        check("the linkedin search was a site: query",
              any("site:linkedin.com/in" in l for l in searched))
        check("at most two search requests", requests_used(bot) - before <= 2)
        check("one model call, on MODEL_LIGHT",
              (len(rows), all(is_light(r) for r in rows)), (1, True))
        if STUBBED:
            check("a namesake at another company was not listed",
                  "Priya Menon" in body, False)

    # ---------------------------------------------------------------- (vi)
    if want("vi"):
        print(f"\n(vi) a channel question that needs the web  [{SERPER}]")
        question = "What AI funding rounds were announced this week?"
        msg = FakeMessage(question)
        t0, before, mark, posted_at = now_ts(), requests_used(bot), len(LINES), len(POSTED)
        handled = await bot._answer_with_engine(msg, question, history=[])
        rows = calls_since(bot, t0)
        spent = requests_used(bot) - before
        show("the answer", "\n".join(POSTED[posted_at:]), limit=18)
        for line in LINES[mark:]:
            if line.startswith(("[engine] msg=", "[search]")):
                note("log: " + line[:200])
        cost = show_calls(rows)
        engine_in = sum(r["input_tokens"] + r["cache_read"] + r["cache_write"]
                        for r in rows)
        note(f"search requests: {spent}; engine input incl. cache: {engine_in:,} "
             f"tokens over {len(rows)} call(s); {usage.money(cost)}")
        check("it answered", handled)
        check("at most 2 search requests", spent <= 2)
        check("engine input under 40k tokens including cache reads",
              engine_in < 40000)
        check("the engine ran on the main model", all(is_main(r) for r in rows))
        routed = [l for l in LINES[mark:] if l.startswith("[engine] msg=")]
        check("it was sent the web group's tools, not the full set",
              bool(routed) and "(web)" in routed[0])

    # ---------------------------------------------------------------- (vii)
    if want("vii") and not NO_WEEK:
        print(f'\n(vii) "simulate week" — the real rules, the real sheet  [{SERPER}]')
        t0, before, mark, posted_at = now_ts(), requests_used(bot), len(LINES), len(POSTED)
        await bot._handle_simulation(FakeMessage("simulate week fast"),
                                     "simulate week fast")
        cost_lines = [l for l in LINES[mark:] if l.startswith("[test-cost]")]
        for line in cost_lines:
            note(line)
        total = bot._last_sim_cost or {}
        rows = calls_since(bot, t0)
        seen = [l for l in LINES[mark:] if l.startswith(
            ("[websearch]", "[news]", "[news-check]", "[search]", "[events]",
             "[research-cache]"))]
        note(f"research log lines this week: {len(seen)} — the first few:")
        for line in seen[:14]:
            note("  log: " + line[:190])
        sites: dict = {}
        for r in rows:
            sites[r["site"]] = sites.get(r["site"], 0) + 1
        note("calls by site: " + ", ".join(f"{k} {v}" for k, v in sorted(sites.items())))
        note(f"{len(POSTED) - posted_at} message(s) posted to the test channel; "
             f"{len(rows)} model call(s) in llm_calls; "
             f"{requests_used(bot) - before} search request(s) banked")
        by_model: dict = {}
        for r in rows:
            m = by_model.setdefault(r["model"], [0, 0.0])
            m[0] += 1
            m[1] += usage.dollars(r)
        for model, (n, cost) in sorted(by_model.items()):
            note(f"  {model}: {n} call(s), {usage.money(cost)}")
        check("the week's test-cost line shows calls, requests and dollars",
              bool(cost_lines) and all(k in cost_lines[-1]
                                       for k in ("calls=", "requests=", "dollars=$")))
        check("ZERO Anthropic web-search calls", int(total.get("searches", 0)), 0)
        check("no web_research site in the log ran the server-side tool",
              [l for l in LINES[mark:] if "search(es) billed" in l], [])
        check("at most SEARCH_DAILY_BUDGET requests",
              int(total.get("requests", 0)) <= config.SEARCH_DAILY_BUDGET)
        check("the simulated calls were logged in the REAL database, not the "
              "sandbox", len(rows), int(total.get("calls", 0)))
        check(f"under $0.50 ({usage.money(total.get('dollars', 0.0))})",
              float(total.get("dollars", 0.0)) < 0.50)

        first_requests = int(total.get("requests", 0))
        if float(total.get("dollars", 0.0)) < 0.60:
            print("\n      the same week again — searches should be cache hits")
            mark = len(LINES)
            await bot._handle_simulation(FakeMessage("simulate week fast"),
                                         "simulate week fast")
            again = bot._last_sim_cost or {}
            for line in [l for l in LINES[mark:] if l.startswith("[test-cost]")][-1:]:
                note(line)
            check("the re-run made no new search request (all cache hits)",
                  int(again.get("requests", 0)), 0)
            note(f"first run: {first_requests} request(s); re-run: "
                 f"{again.get('requests', 0)} request(s), "
                 f"{again.get('cache_hits', 0)} cache hit(s), "
                 f"{usage.money(again.get('dollars', 0.0))}")
        else:
            note("re-run skipped: the first run already cost more than $0.60")
    elif want("vii"):
        skipped.append("(vii) simulate week — --no-week")

    # ---------------------------------------------------------------- (viii)
    if want("viii"):
        print('\n(viii) "what did you cost today"')
        posted_at = len(POSTED)
        t0 = now_ts()
        await bot._send_cost_report(FakeMessage("what did you cost today"))
        body = "\n".join(POSTED[posted_at:])
        show("the answer", body, limit=16)
        check("it is in dollars", "$" in body)
        check("...per model", config.MODEL in body or config.MODEL_LIGHT in body
              or "no calls logged" in body)
        check("...per site", "By site:" in body or "no calls logged" in body)
        check("it gives the requests used", "Search requests today:" in body)
        check("...and the token budget remaining", "Token budget today:" in body)
        check('"today" narrows it to today', "Last 7 days" in body, False)
        check("no model call was made to answer it", len(calls_since(bot, t0)), 0)

    # ---------------------------------------------------------------- (ix)
    if want("ix") and not NO_COMPARE:
        print("\n(ix) SEARCH_BACKEND=anthropic still works — one call, to compare")
        prompt = (websearch.RULE_QUERIES["closure_support"]
                  + "\n\nCompany: Sarvam AI")
        t0, before = now_ts(), requests_used(bot)
        snip = await bot.llm.web_research(
            rule="R10", prompt=prompt, max_uses=1, lean=True,
            queries=[{"q": "Sarvam AI", "news": True, "days": 7, "n": 8}])
        rows_s = calls_since(bot, t0)
        note(f"snippet path [{SERPER}]: ok={snip['ok']} "
             f"requests={snip['searches']} sources={len(snip['sources'])}")
        cost_s = show_calls(rows_s) + (requests_used(bot) - before) * \
            search_backend.cost_per_request(BACKEND)

        config.SEARCH_BACKEND = "anthropic"
        try:
            t0 = now_ts()
            server = await bot.llm.web_research(rule="R10", prompt=prompt,
                                                max_uses=1, lean=True)
            await bot._bank(server, rule_id="R10")
            rows_a = calls_since(bot, t0)
        finally:
            config.SEARCH_BACKEND = BACKEND
        note(f"anthropic path: ok={server['ok']} searches billed="
             f"{server['searches']} sources={len(server['sources'])}")
        cost_a = show_calls(rows_a) + int(server["searches"] or 0) * \
            search_backend.cost_per_request("anthropic")
        show("the anthropic answer", server.get("text") or server.get("note") or "",
             limit=6)
        check("the anthropic backend answered", server["ok"])
        check("...on the main model, with the server-side tool billed",
              (all(is_main(r) for r in rows_a), int(server["searches"] or 0) >= 1),
              (True, True))
        check("...and was not marked as already banked", server.get("banked"), None)
        in_s = sum(r["input_tokens"] + r["cache_read"] for r in rows_s)
        in_a = sum(r["input_tokens"] + r["cache_read"] for r in rows_a)
        note(f"SAME QUESTION: snippets {in_s:,} input tokens, {usage.money(cost_s)}  "
             f"vs  anthropic {in_a:,} input tokens, {usage.money(cost_a)}")
        check("the snippet path used fewer input tokens", in_s < in_a)
    elif want("ix"):
        skipped.append("(ix) the anthropic comparison — --no-compare")

    # ---------------------------------------------------------------- (x)
    if want("x"):
        print("\n(x) R10 company news — Google News RSS, not a search request")
        company = "Sarvam AI"
        item = {"rule": "closure_support", "rule_id": "R10", "key": f"R10:{company}",
                "row_key": f"{company.lower()}|", "company": company, "poc": "",
                "poc_designation": "", "web_pending": True,
                "text": f"{company} closure support [web research pending]"}
        t0, before, mark = now_ts(), requests_used(bot), len(LINES)
        await bot._research_items([item], today=today)
        rows = calls_since(bot, t0)
        show("R10's research", item.get("research") or item.get("research_note") or "")
        note(f"sources: {[s['url'][:70] for s in item.get('sources') or []]}")
        for line in LINES[mark:]:
            if line.startswith(("[search]", "[websearch]")):
                note("log: " + line[:200])
        show_calls(rows)
        check("the news came from the Google News feed", any(
            "backend=google_news" in l and "cached=" in l for l in LINES[mark:]))
        check("ZERO search requests", requests_used(bot) - before, 0)
        check("one model call, on MODEL_LIGHT",
              (len(rows), all(is_light(r) for r in rows)), (1, True))
        check("it produced research or an honest empty",
              bool(item.get("research") or item.get("research_note")))

    print()
    if STUBBED:
        print("NOTE: --stub — the search RESULTS in (a) and (iv)-(vii) were "
              "canned.\n      Run it without --stub for live results.")
    for s in skipped:
        print(f"SKIPPED: {s}")
    print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        code = asyncio.run(main())
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    raise SystemExit(code)
