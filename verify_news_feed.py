"""R1 AS A NEWS FEED - the hourly cycle and the daily post, end to end (NFT2-1087).

    python verify_news_feed.py

Sends nothing to Discord and spends no budget. It runs the REAL
`bot._news_cycle` (read -> store -> flag -> score -> check) and the REAL
`bot._news_run` against a throwaway *_test.db, a pretend clock and a fake
sales-test channel that records what it was sent. The ONE socket in feeds.py
(`feeds._get`) is replaced by a table of canned RSS, the Google gap clock
(`feeds._sleep` / `feeds._monotonic`) by a fake, and the Anthropic client by a
table that answers the real scoring prompt. It shows:

  (i)   the 14:00 BIG PULL on a day with no cycle yet: every Google query
        carries when:1d, a feed that times out costs one line, a story seen
        in two places is ONE row (Google's copy wins), the flag is set by
        rule and the model is only asked for the score;
  (ii)  a BREAKING story is posted in the SAME cycle that finds it - "**Breaking
        news**", the one line shape, no date - outside drip_sends;
  (iii) the hourly pulls: when:2h, no daily limit (a third breaking post the
        same day), a second outlet's copy of a sent story is not sent again;
  (iv)  Google answers 429: the pull stops at once, what was read is stored,
        the cycle is partial, nothing is retried inside the hour, and the next
        cycle's window reaches back over it;
  (v)   the kill switch off: the cycle stores and scores, posts and records
        nothing; when it is back, the story still inside the age limit goes out;
  (vi)  the DAILY POST: one message, ours first under "Our PoCs are in the
        news:", the industry under "Elsewhere in AI", nothing already sent as
        breaking; and the morning after, with nothing left, NO post at all;
  (vii) two back-to-back ticks run one cycle, and it runs BEFORE the drip;
        a Sunday has its cycle too;
  (viii) an R3 run and an R6 run both log the lean prompt size (< 3,000 chars)
        - under SEARCH_BACKEND=anthropic, the one path that still searches.
"""
import asyncio
import email.utils
import logging
import os
import re
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import unquote_plus

TMP = tempfile.mkdtemp(prefix="saley-newsfeed-")
os.environ["DB_PATH"] = os.path.join(TMP, "sales_bot_test.db")

import os as _os  # noqa: E402  NFT2-1064 offline guard: canned source statuses, real Sheets/Drive calls blocked
import sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "tests"))
import offline_guard  # noqa: E402
GUARD = offline_guard.install_script()
import config  # noqa: E402
import canned_sheet  # noqa: E402  NFT2-1064: canned rows for the real-sheet read the offline guard blocks
canned_sheet.install()

config.DB_PATH = os.environ["DB_PATH"]
config.SALES_TEST_MODE = True
config.SALES_TEST_CHANNEL_ID = 4242
config.SALES_CHANNEL_IDS = [4242]
config.SALES_CHANNEL_ID_SET = {4242}
config.WEB_SEARCH_ENABLED = True
config.WEB_SEARCH_DAILY_BUDGET = 60
config.WEB_SEARCH_ALLOWED_DOMAINS = []
config.WEB_SEARCH_BLOCKED_DOMAINS = []
config.SEARCH_BACKEND = "searxng"            # the feed path; (viii) switches it for a moment
config.digest_enabled = lambda: True
# THE SHIPPED DEFAULTS, pinned, so a local .env cannot make this another test.
config.NEWS_TOPICS = ["evals", "voice agent"]
config.NEWS_RSS_FEEDS = ["https://outlet.example/feed"]
config.NEWS_PULL_FIRST = "14:00"
config.NEWS_PULL_UNTIL = "23:00"
config.NEWS_PULL_WINDOW_HOURS = 2
config.NEWS_NAME_BATCH_SIZE = 15
config.NEWS_GENERIC_NAMES = []
config.NEWS_WATCH_NAMES = []
config.NEWS_REQUEST_GAP_SECONDS = 1.5
config.NEWS_BREAKING_MAX_PER_CYCLE = 3
config.NEWS_POC_BREAKING_MIN = 4
config.NEWS_BREAKING_MIN_IMPORTANCE = 5
config.NEWS_BREAKING_MAX_AGE_HOURS = 3
config.NEWS_CARRY_MAX_DAYS = 3
config.NEWS_MAX_ITEMS = 5
config.NEWS_PER_TOPIC_PER_DAY = 2
config.NEWS_TOPICS_PER_WEEK = 6
config.NEWS_OFFTOPIC_BYPASS_IMPORTANCE = 4
config.NEWS_REPEAT_DAYS = 30
config.NEWS_FEED_KEEP_DAYS = 14
config.NEWS_SCORE_MAX_ITEMS = 40
config.NEWS_HOLD_AFTER_TEST_SECONDS = 0
config.ARXIV_ENABLED = False
config.TOKEN_DAILY_BUDGET = 0

import deadlines as dl  # noqa: E402
import drip  # noqa: E402
import feeds  # noqa: E402
import gtm_sheet  # noqa: E402
import llm as llm_mod  # noqa: E402
import news  # noqa: E402
import nextaction  # noqa: E402
from bot import SalesBot  # noqa: E402
from db import DB  # noqa: E402

TUE = date(2026, 9, 29)
WED = date(2026, 9, 30)
SUN = date(2026, 10, 4)
NOW = [datetime(2026, 9, 29, 14, 0, tzinfo=dl.IST)]
# The cycle runs on the REAL clock; this script owns it.
dl.now_ist = lambda: NOW[0]
dl.today_ist = lambda: NOW[0].date()
dl.real_now_ist = lambda: NOW[0]
dl.real_today_ist = lambda: NOW[0].date()


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
_out.addFilter(lambda r: r.name in ("news", "bot", "llm", "feeds") and (
    "[news" in r.getMessage() or "lean prompt" in r.getMessage()
    or "full prompt" in r.getMessage() or "[feeds]" in r.getMessage()))
root.addHandler(_out)
root.addHandler(Capture())
for noisy in ("discord", "asyncio", "state", "httpx", "anthropic"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


# -- the canned feeds: the one socket, and the clock the Google gap is measured on ---------

SERVED: list = []            # (decoded url) of every request, in order
GOOGLE_AT: list = []         # fake-clock time of each Google request
CLOCK = [1000.0]
SERVE: dict = {}             # substring of the decoded url -> [item specs] or an Exception


def spec(title, source, minutes_ago, link, summary=""):
    return {"title": title, "source": source, "ago": minutes_ago, "link": link,
            "summary": summary}


def google(slug):
    return f"https://news.google.com/rss/articles/{slug}?oc=5"


def rss(specs) -> bytes:
    now = NOW[0].astimezone(timezone.utc)
    parts = ['<?xml version="1.0"?><rss version="2.0"><channel><title>Feed</title>']
    for s in specs:
        when = email.utils.format_datetime(now - timedelta(minutes=s["ago"]), usegmt=True)
        google_item = "news.google.com" in s["link"]
        title = f"{s['title']} - {s['source']}" if google_item else s["title"]
        src = f'<source url="https://x.example">{s["source"]}</source>' if google_item else ""
        parts.append(f"<item><title>{title}</title><link>{s['link']}</link>"
                     f"<description>{s['summary']}</description><pubDate>{when}</pubDate>"
                     f"{src}</item>")
    parts.append("</channel></rss>")
    return "".join(parts).encode("utf-8")


def fake_get(url):
    decoded = unquote_plus(url)
    SERVED.append(decoded)
    if "news.google.com" in url:
        GOOGLE_AT.append(CLOCK[0])
    for key, value in SERVE.items():
        if key in decoded:
            if isinstance(value, Exception):
                raise value
            return rss(value)
    return rss([])


def fake_sleep(seconds):
    CLOCK[0] += seconds


feeds._get = fake_get
feeds._sleep = fake_sleep
feeds._monotonic = lambda: CLOCK[0]
feeds._last_google[0] = None


# -- the fake Anthropic client: answers the real scoring prompt from VERDICTS ---------------

CALLS: list = []
VERDICTS: dict = {}          # title -> (topic, importance, what)
REPLY = [None]               # a callable (system, prompt) -> text, for the (viii) searches


class FakeMessages:
    def create(self, **kw):
        prompt = str((kw.get("messages") or [{}])[-1].get("content") or "")
        CALLS.append({"system": kw.get("system"), "prompt": prompt, "tools": kw.get("tools")})
        if REPLY[0] is not None:
            text = REPLY[0](kw.get("system"), prompt)
        else:
            lines = []
            for line in prompt.split("ITEMS:", 1)[-1].splitlines():
                m = re.match(r"\s*(\d+) \|[^|]*\| (.+)", line)
                if not m:
                    continue
                verdict = VERDICTS.get(m.group(2).split(" [ABOUT US")[0].strip())
                if verdict:
                    lines.append(f"SCORE | {m.group(1)} | {verdict[0]} | {verdict[1]} | {verdict[2]}")
            text = "\n".join(lines) or "NOTHING FOUND"
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text, citations=[])],
            usage=SimpleNamespace(input_tokens=900, output_tokens=120,
                                  server_tool_use=SimpleNamespace(web_search_requests=1)),
            stop_reason="end_turn")


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


REBUILDS: list = []
ORDER: list = []


def make_bot():
    bot = SalesBot()
    bot.db = DB(config.DB_PATH)
    bot._durable_db = None
    engine = llm_mod.LLM("sk-fake-not-used", config.MODEL)
    engine._client = SimpleNamespace(messages=FakeMessages())
    bot.llm = engine
    channel = FakeChannel()
    bot.get_channel = lambda cid: channel if int(cid) == 4242 else None

    # THE NAME LIST: built from two canned tabs and stored, as the 14:00 rebuild
    # does. The rebuild itself reads the real sheets; here it only counts.
    class Nobody:
        def lookup(self, name):
            return False

    names = feeds.build_names(
        [{"company": "PolyAI"}, {"company": "Cartesia"}],
        [{"company": "PolyAI", "name": "Daisy Leigh"}], departures=Nobody())
    bot.db.news_names_replace(names, built_on=dl.iso(TUE))

    async def rebuilt(*_a, **_k):
        REBUILDS.append(NOW[0].strftime("%Y-%m-%d %H:%M"))

    bot._rebuild_news_names = rebuilt
    return bot, channel


def breaking_heading() -> str:
    tags = drip.news_tags()
    return "**Breaking news**" + (f" {tags}" if tags else "")


def tagged(text: str) -> str:
    return f"{config.SIMULATION_PREFIX} {text}"


def google_urls(since: int) -> list:
    return [u for u in SERVED[since:] if "news.google.com" in u]


async def main():
    bot, channel = make_bot()
    ledger = bot.db

    # ------------------------------------------------------------------ (i)
    print("(i) THE 14:00 BIG PULL - Tue 29 Sep, no cycle yet today")
    A = "PolyAI raises $50M Series C"
    B = "Cartesia ships a new voice model"
    C = "Frontier lab ships open-weights reasoning model"
    D = "EU publishes AI Act code of practice"
    E = "Chipmaker posts record data-centre quarter"
    F = "Ten prompts to try this weekend"
    VERDICTS.update({
        A: ("voice agent", 5, "a funding round"), B: ("voice agent", 3, "a new model"),
        C: ("evals", 5, "open weights released"), D: ("evals", 4, "final text published"),
        E: ("OTHER", 3, "AI demand"),
    })
    SERVE.clear()
    SERVE.update({
        '"PolyAI"': [spec(A, "Reuters", 20, google("polyai-series-c")),
                     spec(B, "Reuters", 300, google("cartesia-voice-model"))],
        "voice agent": TimeoutError("slow"),
        "evals": [spec(C, "TechCrunch", 30, google("open-weights")),
                  spec(D, "Reuters", 90, google("eu-code")),
                  spec(F, "Medium", 40, google("ten-prompts"))],
        "outlet.example": [spec(A, "Reuters", 20, "https://outlet.example/polyai-series-c"),
                           spec(E, "Reuters", 60, "https://outlet.example/chips")],
    })
    at(TUE, 14, 0)
    mark = len(SERVED)
    out = await bot._news_cycle()
    pull = out["pull"]
    print("   the cycle:", {k: out[k] for k in ("slot", "big", "hours", "posted", "status")})
    check("the cycle ran at the 14:00 slot, and it is the BIG pull (no cycle yet today)",
          (out["slot"], out["big"], out["hours"]), ("14:00", True, 24))
    check("the name list was rebuilt for the big pull", REBUILDS, ["2026-09-29 14:00"])
    check("every Google query carries when:1d",
          all("when:1d" in u for u in google_urls(mark)), True)
    check("Google is read first (names, then topics), the outlet last",
          ["news.google.com" in u for u in SERVED[mark:]], [True, True, True, False])
    check("the names go in one batched search, every name quoted",
          '"Cartesia" OR "PolyAI"' in google_urls(mark)[0], True)
    check("a feed that times out costs one error; the others still landed",
          (len(pull["errors"]), pull["new"]), (1, 6))
    check("the outlet's copy of a story Google carried is the same row",
          ledger.news_feed_count(), 6)
    row = ledger.news_feed_get([news.url_key(google("polyai-series-c"))])[0]
    check("...Google's copy won: its link and its source rank (1 = our names)",
          (row["url"], row["source_rank"]), (google("polyai-series-c"), 1))
    check("the flag was set BY RULE at store time: flagged, with the name found",
          (row["flag"], row["names"]), (1, "PolyAI"))
    check("an industry item is not flagged",
          ledger.news_feed_get([news.url_key(google("eu-code"))])[0]["flag"], 0)
    check("ONE scoring call, however many items", len(CALLS), 1)
    check("...the scorer is told which item is about us, and is not asked to decide it",
          "[ABOUT US: PolyAI]" in CALLS[0]["prompt"], True)
    scores = {r["title"]: r["importance"] for r in ledger.news_feed_between("2026-01-01", "2027-01-01")}
    check("scores written back; the item the model left out is filler (1)",
          (scores[A], scores[B], scores[C], scores[D], scores[E], scores[F]),
          (5, 3, 5, 4, 3, 1))
    row_cycle = ledger.news_cycles_on("2026-09-29")[0]
    check("the cycle is recorded: (date, hour), big, done",
          (row_cycle["hour"], row_cycle["big"], row_cycle["status"]), ("14:00", 1, "done"))
    check("a second tick in the same hour does not run it again",
          await bot._news_cycle(), None)

    # ------------------------------------------------------------------ (ii)
    print("\n(ii) A BREAKING STORY, POSTED IN THE CYCLE THAT FOUND IT")
    check("two stories are breaking: ours at 5 and an industry 5; one message",
          (out["posted"], len(channel.sent)), (2, 1))
    post = channel.sent[-1]
    print("   the post:\n   " + "\n   ".join(post.splitlines()))
    check("a test run carries the tag; the heading is '**Breaking news**' with no date",
          post.split("\n")[0], tagged(breaking_heading()))
    check("the stories in the one order (score, source rank): ours first, no blank under the heading",
          post.split("\n")[1:], [
              f"- {A} ([Reuters](<{google('polyai-series-c')}>))", "",
              f"- {C} ([TechCrunch](<{google('open-weights')}>))"])
    check("no opener, no closer, no label, no bold line but the heading",
          [x in post for x in (news.OPENER, news.CLOSER, news.LABEL_OURS,
                               news.LABEL_INDUSTRY)] + [post.count("**")], [False] * 4 + [2])
    check("it is NOT a drip message: nothing in drip_sends", ledger.drip_sent_today(dl.iso(TUE)), [])
    check("recorded as sent, kind=breaking, so the daily post cannot repeat it",
          sorted(r["kind"] for r in ledger.news_stories_on(dl.iso(TUE))), ["breaking", "breaking"])
    check("the importance-4 industry story and the 3s stayed for the daily post",
          [ledger.news_story_seen(news.url_key(google(s)), "", since_iso="2000-01-01")
           for s in ("eu-code", "cartesia-voice-model")], [None, None])

    # ------------------------------------------------------------------ (iii)
    print("\n(iii) THE HOURLY PULLS - 15:00 and 16:00, no daily limit")
    G = "Cartesia names a new CEO"
    H = "Regulator opens an inquiry into model evals"
    VERDICTS.update({G: ("voice agent", 4, "a new CEO")})      # H: the model leaves it out
    SERVE.clear()
    SERVE.update({
        '"PolyAI"': [spec(G, "Reuters", 10, google("cartesia-ceo")),
                     spec(A, "Reuters", 80, google("polyai-series-c"))],
        "evals": [spec(C.title(), "Reuters", 20, google("open-weights-reuters")),
                  spec(H, "Reuters", 15, google("inquiry"))],
    })
    at(TUE, 15, 0)
    mark = len(SERVED)
    sent_before = len(channel.sent)
    out = await bot._news_cycle()
    check("15:00 is a small pull: when:2h on every Google query, window 2h",
          (all("when:2h" in u for u in google_urls(mark)), out["big"], out["hours"]),
          (True, False, 2))
    check("the name list is NOT rebuilt (only the 14:00 pull does that)", len(REBUILDS), 1)
    check("the overlap is dropped by the store: only the two new stories are new",
          out["pull"]["new"], 2)
    check("a new story about us scored 4 is breaking; a second outlet's copy of a sent "
          "breaking story is not sent again", (len(channel.sent) - sent_before, out["posted"]),
          (1, 1))
    check("...the post carries just that one story",
          channel.sent[-1].split("\n")[1:], [f"- {G} ([Reuters](<{google('cartesia-ceo')}>))"])
    SERVE.clear()
    SERVE.update({"evals": [spec("Model provider pauses release over safety finding", "Reuters",
                                 5, google("pause"))]})
    VERDICTS["Model provider pauses release over safety finding"] = ("AI safety", 5, "a rare pause")
    at(TUE, 16, 0)
    sent_before = len(channel.sent)
    out = await bot._news_cycle()
    check("16:00: the THIRD breaking post of the day goes out - there is no daily limit",
          (len(channel.sent) - sent_before, len(channel.sent)), (1, 3))
    check("...none of them is a drip message: drip_sends is empty",
          ledger.drip_sent_today(dl.iso(TUE)), [])
    check("every cycle is on the books: 14:00, 15:00, 16:00, all done",
          [(c["hour"], c["status"]) for c in ledger.news_cycles_on("2026-09-29")],
          [("14:00", "done"), ("15:00", "done"), ("16:00", "done")])

    # ------------------------------------------------------------------ (iv)
    print("\n(iv) GOOGLE ANSWERS 429 - Tue 17:00")
    J = "Cartesia opens a London office"
    VERDICTS[J] = ("voice agent", 3, "a new office")
    SERVE.clear()
    SERVE.update({
        '"PolyAI"': [spec(J, "Reuters", 10, google("cartesia-london"))],
        "evals": RuntimeError("429 Client Error: Too Many Requests for url"),
    })
    at(TUE, 17, 0)
    mark = len(SERVED)
    gap_from = len(GOOGLE_AT)
    out = await bot._news_cycle()
    print("   log: " + next((l for l in LINES if "429" in l and "[feeds]" in l), "(none)"))
    check("the pull stopped at once: 2 Google requests, no outlet read, no later topic",
          (len(SERVED) - mark, out["pull"]["google_requests"]), (2, 2))
    check("what was read before the 429 is stored", out["pull"]["new"], 1)
    check("the cycle is marked partial", (out["partial"], out["status"]), (True, "partial"))
    check("there is at least 1.5 s between two Google requests",
          min(b - a for a, b in zip(GOOGLE_AT[gap_from:], GOOGLE_AT[gap_from + 1:])) >= 1.5, True)
    mark = len(SERVED)
    at(TUE, 17, 40)
    check("no retry inside the hour: the same slot is not run again",
          (await bot._news_cycle(), len(SERVED) - mark), (None, 0))
    SERVE.clear()
    at(TUE, 19, 0)                           # the bot was down for the 18:00 hour
    mark = len(SERVED)
    out = await bot._news_cycle()
    check("the 19:00 window reaches back to the last COMPLETED cycle (16:00): 3 hours",
          (out["hours"], all("when:3h" in u for u in google_urls(mark))), (3, True))
    check("...and the cycle in the middle, which was partial, is not what it measures from",
          [(c["hour"], c["status"]) for c in ledger.news_cycles_on("2026-09-29")][-3:],
          [("16:00", "done"), ("17:00", "partial"), ("19:00", "done")])

    # ------------------------------------------------------------------ (v)
    print("\n(v) THE KILL SWITCH - Tue 20:00 off, 21:00 on")
    K = "PolyAI acquires a voice startup"
    VERDICTS[K] = ("voice agent", 5, "an acquisition")
    SERVE.clear()
    SERVE.update({'"PolyAI"': [spec(K, "Reuters", 5, google("polyai-acquires"))]})
    config.digest_enabled = lambda: False
    at(TUE, 20, 0)
    sent_before = len(channel.sent)
    out = await bot._news_cycle()
    config.digest_enabled = lambda: True
    check("it pulled and stored the story", ledger.news_feed_get(
        [news.url_key(google("polyai-acquires"))])[0]["importance"], 5)
    check("...but posted nothing and recorded nothing",
          (len(channel.sent) - sent_before, out["posted"], bool(out["silent"]),
           ledger.news_story_seen(news.url_key(google("polyai-acquires")), "",
                                  since_iso="2000-01-01")), (0, 0, True, None))
    SERVE.clear()
    at(TUE, 21, 0)
    sent_before = len(channel.sent)
    out = await bot._news_cycle()
    check("21:00, back on: the story first seen an hour ago (inside NEWS_BREAKING_MAX_AGE_HOURS) "
          "goes out",
          (len(channel.sent) - sent_before, channel.sent[-1].split("\n")[1:]),
          (1, [f"- {K} ([Reuters](<{google('polyai-acquires')}>))"]))

    # ------------------------------------------------------------------ (vi)
    print("\n(vi) THE DAILY POST - Tue 21:30, one message")
    at(TUE, 21, 30)
    items = [item(nextaction.R_AI_NEWS, "R1")]
    calls_before, web_before = len(CALLS), len(SERVED)
    await bot._news_run(items, today=TUE)
    body = items[0]["text"]
    print("   the post:\n   " + "\n   ".join(body.splitlines()))
    check("the daily post reads the store: no feed fetched, no model call (all scored)",
          (len(CALLS) - calls_before, len(SERVED) - web_before), (0, 0))
    lines = body.split("\n")
    check("the opener, then our label, with no blank line", lines[:2],
          [news.OPENER, news.LABEL_OURS])
    check("ours first - what was not sent as breaking, newest first at the same score",
          [ln for ln in lines if ln.startswith("- ")][:2], [
              f"- {J} ([Reuters](<{google('cartesia-london')}>))",
              f"- {B} ([Reuters](<{google('cartesia-voice-model')}>))"])
    check("'Elsewhere in AI' follows, then the industry in score order (the model left H out: "
          "industry at 1 is not posted)",
          lines[lines.index(news.LABEL_INDUSTRY) + 1:-1], [
              f"- {D} ([Reuters](<{google('eu-code')}>))", "",
              f"- {E} ([outlet.example](<https://outlet.example/chips>))"])
    check("the closer ends it", lines[-1], news.CLOSER)
    sent_titles = body
    check("nothing already sent as breaking is in it (A, C, G, K), and not the filler",
          [t for t in (A, C, G, K, F, H, "pauses release") if t in sent_titles], [])
    check("one message: no 'More AI News' anywhere", "More AI News" in body, False)
    check("four stories: what there is, no padding to five",
          sum(1 for ln in lines if ln.startswith("- ")), 4)
    check("recorded as the daily post (kind=main) so they are not shown twice",
          sorted(r["kind"] for r in ledger.news_stories_on(dl.iso(TUE)))
          .count("main"), 4)
    check("the retired follow-up has no row: no kind=overflow", "overflow" in
          {r["kind"] for r in ledger.news_stories_on(dl.iso(TUE))}, False)

    print("\n   the next morning, nothing left to say")
    at(WED, 10, 0)
    items = [item(nextaction.R_AI_NEWS, "R1")]
    await bot._news_run(items, today=WED)
    check("nothing to send: the text is empty", items[0]["text"], "")
    check("...so the sender spends the slot in silence - no heading, no quiet line",
          drip.nothing_to_say({"type": nextaction.R_AI_NEWS, "items": items}), True)

    # ------------------------------------------------------------------ (vii)
    print("\n(vii) TWO BACK-TO-BACK TICKS, THE ORDER, A SUNDAY")

    async def drip_stub(*_a, **_k):
        ORDER.append("drip")

    real_cycle = bot._news_cycle

    async def cycle_spy(*a, **k):
        ORDER.append("cycle")
        return await real_cycle(*a, **k)

    bot._maybe_send_drip = drip_stub
    bot._news_cycle = cycle_spy
    bot._maybe_write_daily_summary = lambda: None
    SERVE.clear()
    at(TUE, 22, 5)
    mark = len(SERVED)
    await bot._sweep_once()
    ran = len(SERVED) - mark
    await bot._sweep_once()
    check("the first tick pulled; the second pulled nothing", (ran > 0, len(SERVED) - mark - ran),
          (True, 0))
    check("the cycle runs BEFORE the drip on every tick", ORDER, ["cycle", "drip", "cycle", "drip"])
    hours = [c["hour"] for c in ledger.news_cycles_on("2026-09-29")]
    check("each slot appears once", len(hours), len(set(hours)))
    check("the slots that ran today", hours, ["14:00", "15:00", "16:00", "17:00", "19:00",
                                              "20:00", "21:00", "22:00"])
    at(SUN, 15, 0)
    mark = len(SERVED)
    out = await real_cycle()
    check("Sunday 15:00: a cycle runs - the first of its day, so the big pull",
          (out is not None and out["slot"], out["big"] if out else None), ("15:00", True))
    at(SUN, 11, 0)
    check("Sunday 11:00: before 14:00, no cycle", await real_cycle(), None)
    at(TUE, 13, 59)
    check("13:59 on a weekday: no cycle", await real_cycle(), None)

    # ------------------------------------------------------------------ (viii)
    print("\n(viii) R3 AND R6 USE THE LEAN PROMPT (SEARCH_BACKEND=anthropic, the one path that searches)")
    config.SEARCH_BACKEND = "anthropic"
    config.NEWS_PREFERRED_DOMAINS = ["reuters.com", "techcrunch.com"]

    async def known():
        return ["Acme AI", "Globex"]

    async def event_rows(kind, _for):
        return [{"event": "NeurIPS 2026", "event_date": "2026-12-01",
                 "registration_deadline": "", "_row": 2, "link": "https://neurips.cc"}]

    bot._known_companies = known
    bot._rule_tab_rows = event_rows
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
    at(WED, 15, 0)
    mark = len(SERVED)
    check("under SEARCH_BACKEND=anthropic there is no cycle: no feed is read",
          (await real_cycle(), len(SERVED) - mark), (None, 0))
    config.SEARCH_BACKEND = "searxng"


try:
    asyncio.run(main())
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
sys.exit(1 if failures else 0)
