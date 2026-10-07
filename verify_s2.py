"""S2 — AI NEWS: THE INDUSTRY AND OUR PoCs, NOTHING LOST. Real output.

    python verify_s2.py            every offline check
    python verify_s2.py --only i,iii   some of them (p i ii iii iv w)
    python verify_s2.py --live     also (L): a real poll with the real sheet's PoC
                                   names and ONE real MODEL_LIGHT scoring call

  (p)   the PoC machinery: who is looked up (never the departures list), the
        rotation (15 a day, least recently checked first), and a poll that tags
        an item `poc` only when it names them
  (i)   a Monday with 3 PoC + 3 industry stories -> the post shows the top 2 PoC
        + the best 3 of the rest, and the 6th is in "More AI news today";
  (v)   ...identically on the live sweep, "make it <monday>" and "simulate
        <monday>"
  (ii)  an OTHER story at importance 4 gets in past a full topic cap
  (iii) a Saturday story the breaking valve held appears in Monday's post
  (iv)  llm_calls shows only MODEL_LIGHT scoring calls, and no search was made
  (w)   the Bot Rules tab row 1 wording, printed from the rules the bot loads

NOTHING REACHES DISCORD, NO SHEET IS READ AND NO FEED IS FETCHED (without
--live). The feed store is filled here with known items, exactly as a poll
would have stored them; the real `_news_run`, `news.choose_main`, `news.render`,
`_send_drip_message` and `_post_news_overflow` do the rest, on a fresh database
per run. Settings are pinned to the shipped defaults.

THE SCORER IS A STAND-IN, AND ONLY ITS ANSWERS ARE. The real `llm.score_news`
runs — the real prompt, the real MODEL_LIGHT route, the real `llm_calls` ledger
row — but the client answers from a table (title -> topic, importance), because
"the top 2 PoC by importance" cannot be asserted of scores that change between
runs. (L) uses the real model.
"""
import asyncio
import logging
import os
import random
import re
import shutil
import sqlite3
import sys
import tempfile
import urllib.parse
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ONLY: set = set()
for _i, _a in enumerate(sys.argv):
    if _a == "--only" and _i + 1 < len(sys.argv):
        ONLY = {x.strip().lower() for x in sys.argv[_i + 1].split(",") if x.strip()}
LIVE = "--live" in sys.argv

TMP = tempfile.mkdtemp(prefix="saley-s2-")
os.environ["STATE_DIR"] = os.path.join(TMP, "state")
os.environ["DB_PATH"] = os.path.join(TMP, "base_test.db")

import os as _os  # noqa: E402  NFT2-1064 offline guard: canned source statuses, real Sheets/Drive calls blocked
import sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "tests"))
import offline_guard  # noqa: E402
if "--live" not in _sys.argv:
    GUARD = offline_guard.install_script()
import config  # noqa: E402

LIVE_SETTINGS = {k: getattr(config, k) for k in (
    "NEWS_RSS_FEEDS", "NEWS_TOPICS", "WEB_SEARCH_ENABLED")}

# THE SHIPPED DEFAULTS, pinned — not whatever the local .env says.
SETTINGS = {
    "DB_PATH": os.environ["DB_PATH"], "STATE_DIR": os.environ["STATE_DIR"],
    "DAILY_MESSAGE_CAP": 5, "SALES_DRIP_START": "14:00", "SALES_DRIP_END": "18:30",
    "MESSAGE_GAP_MINUTES": 90, "MESSAGE_JITTER_MINUTES": 15,
    "MESSAGE_GAP_MIN_MINUTES": 30, "DRIP_REASK_DAYS": 2, "DRIP_WEEKDAYS_ONLY": True,
    "SUNDAY_RULE_IDS": ["R4"], "NEWS_MAIN_TIME": "14:00", "MEETING_DAYOF_TIME": "10:00",
    "TEST_MORNING_TIME": "10:00", "TEST_AFTERNOON_TIME": "14:00",
    "NEXT_ACTION_ENABLED": True, "WEEKLY_FUNNEL_ENABLED": False, "EVENTS_ENABLED": False,
    "SHEET_WRITES_ENABLED": False, "DRIP_LLM_COMPOSE": True, "SALES_DMS_ENABLED": False,
    "TEST_POST_GAP_SECONDS": 0, "SIMULATION_FAST_GAP_SECONDS": 0,
    "SIMULATION_REAL_MENTIONS": False,
    # the news settings under test
    "NEWS_TOPICS": ["evals", "RLHF", "voice agent", "AI regulation"],
    "NEWS_MAX_ITEMS": 5, "NEWS_POC_SLOTS": 2, "NEWS_POC_TARGETS_PER_DAY": 15,
    "NEWS_POC_LOOKBACK_DAYS": 7, "NEWS_PER_TOPIC_PER_DAY": 2, "NEWS_TOPICS_PER_WEEK": 6,
    "NEWS_BREAKING_MIN_IMPORTANCE": 5, "NEWS_BREAKING_MAX_PER_DAY": 2,
    "NEWS_OFFTOPIC_BYPASS_IMPORTANCE": 4, "NEWS_OVERFLOW_ENABLED": True,
    "NEWS_OVERFLOW_MIN_IMPORTANCE": 3, "NEWS_OVERFLOW_MAX_ITEMS": 8,
    "NEWS_REPEAT_DAYS": 30, "NEWS_FEED_KEEP_DAYS": 14, "NEWS_SCORE_MAX_ITEMS": 40,
    "NEWS_CHECK_TIMES": ["11:00", "12:00", "13:00", "15:00", "16:00", "17:00", "18:00"],
    # OFF, to show the news needs neither: it reads RSS and searches nothing.
    "WEB_SEARCH_ENABLED": False, "SEARCH_BACKEND": "searxng",
}
for _k, _v in SETTINGS.items():
    setattr(config, _k, _v)
config.digest_enabled = lambda: True      # the kill switch is not what is tested

import clock  # noqa: E402
import deadlines as dl  # noqa: E402
import drip  # noqa: E402
import feeds  # noqa: E402
import gtm_sheet  # noqa: E402
import leave  # noqa: E402
import mapping_sheet  # noqa: E402
import news  # noqa: E402
import persona  # noqa: E402
import rules as rules_mod  # noqa: E402
import search_backend  # noqa: E402
import simulation  # noqa: E402
import tone  # noqa: E402
import voice  # noqa: E402
from db import DB  # noqa: E402

simulation.pace_seconds = lambda **_k: 0.0

# The real sheet readers, kept for (L): the fixture replaces them below.
REAL = {"cadence_tab": gtm_sheet.SHEETS.cadence_tab, "tab": gtm_sheet.SHEETS.tab,
        "departures": mapping_sheet.MAPPING.departures, "get": feeds._get,
        "http": search_backend._http}

failures = 0
POSTED: list = []         # every body any path "sent"
SENT: list = []           # one dict per drip message
LOGS: list = []
SEARCHES: list = []       # every search request anything tried to make
FRESH = [0]


def want(name: str) -> bool:
    return not ONLY or name.lower() in ONLY


def check(name, got, expected=True):
    global failures
    ok = (got == expected)
    failures += 0 if ok else 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}"
          + ("" if ok else f": got {got!r}, want {expected!r}"))


def say(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


class Capture(logging.Handler):
    KEEP = ("[news]", "[news-check]", "[feeds]", "[research-cache]")

    def emit(self, record):
        try:
            line = record.getMessage()
        except Exception:
            return
        if line.startswith(self.KEEP):
            LOGS.append(line)


root = logging.getLogger()
root.setLevel(logging.INFO)
for h in list(root.handlers):
    root.removeHandler(h)
root.addHandler(Capture())
for noisy in ("discord", "asyncio", "httpx", "httpcore", "anthropic", "googleapiclient",
              "google", "urllib3"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


def _no_search(*a, **k):
    SEARCHES.append(a[:2])
    raise RuntimeError("verify_s2: a search request was attempted")


search_backend._http = _no_search


# -- Discord, recorded ---------------------------------------------------------


class FakeTyping:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeChannel:
    name = "recording"
    guild = SimpleNamespace(id=1)

    def __init__(self, cid):
        self.id = int(cid)

    async def send(self, body):
        POSTED.append(str(body))
        return SimpleNamespace(id=len(POSTED), jump_url="https://discord/x")

    def typing(self):
        return FakeTyping()


TEST_CHANNEL = FakeChannel(config.SALES_TEST_CHANNEL_ID or 4242)


class FakeAuthor:
    id = int((list(config.approver_ids() or []) or [111])[0])
    display_name = "verify_s2"
    name = "verify_s2"
    bot = False


class FakeMessage:
    id = 999
    channel = TEST_CHANNEL
    author = FakeAuthor()
    content = ""

    async def reply(self, body, mention_author=False):
        POSTED.append("REPLY: " + str(body))
        return SimpleNamespace(id=len(POSTED), jump_url="https://discord/x")


# -- the scorer's answers ------------------------------------------------------

# title -> (topic, importance, what happened). A title not here is filler.
VERDICTS: dict = {}


class ScoreTable:
    """The model client. It answers the real scoring prompt from VERDICTS and
    says nothing useful to anything else — R1 is posted verbatim and composes
    nothing, so nothing else is asked."""

    def create(self, **kw):
        prompt = str((kw.get("messages") or [{}])[-1].get("content") or "")
        text = "{}"
        if prompt.startswith("Below are news items from RSS feeds"):
            lines = []
            for line in prompt.split("ITEMS:", 1)[1].splitlines():
                m = re.match(r"\s*(\d+) \|[^|]*\| (.+)", line)
                if not m:
                    continue
                title = m.group(2).split(" [ABOUT OUR CONTACT")[0].strip()
                verdict = VERDICTS.get(title)
                if verdict:
                    lines.append(f"SCORE | {m.group(1)} | {verdict[0]} | {verdict[1]} "
                                 f"| {verdict[2]}")
            text = "\n".join(lines) or "NOTHING FOUND"
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=900, output_tokens=120))


def strip(body: str) -> str:
    out = str(body or "")
    tag = str(config.SIMULATION_PREFIX or "").strip()
    if tag and out.startswith(tag):
        out = out[len(tag):].lstrip()
    return re.sub(r"<@!?(\d+)>", lambda m: "@" + str(
        config.ROSTER_DISPLAY_NAMES.get(str(m.group(1))) or "user"), out)


# -- the feed store, filled as a poll would fill it ----------------------------

MON = date(2026, 9, 28)                 # the pretend Monday (already in the past)
FRI, SAT, SUN, TUE = (MON - timedelta(days=3), MON - timedelta(days=2),
                      MON - timedelta(days=1), MON + timedelta(days=1))


def ist(day: date, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(day, time(hh, mm), dl.IST)


def story(title: str, when: datetime, verdict: tuple, *, ref: str = "",
          source: str = "TechCrunch") -> dict:
    """One feed item as `feeds.parse` would have produced it, plus what the
    scorer will say about it."""
    VERDICTS[title] = verdict
    url = "https://example.org/" + re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return {"url": url, "url_key": news.url_key(url),
            "headline_key": news.headline_key(title), "title": title, "summary": "",
            "source": source, "published_at": feeds.utc_iso(when), "topic_hint": "",
            "seen_at": feeds.utc_iso(when),
            "kind": feeds.KIND_POC if ref else feeds.KIND_INDUSTRY, "sheet_ref": ref}


def fresh_db(name: str, items: list) -> str:
    FRESH[0] += 1
    path = os.path.join(TMP, f"{name}{FRESH[0]}_test.db")
    DB(path).news_feed_add(items)
    config.DB_PATH = path
    clock.forget()
    voice.invalidate()
    return path


def new_bot(path: str):
    from bot import SalesBot

    bot = SalesBot()
    bot.db = DB(path)
    if bot.llm is None:
        print("No model client (ANTHROPIC_API_KEY is unset), so llm.score_news cannot "
              "run even against the stand-in. Nothing to verify.")
        sys.exit(2)
    bot.llm._client = SimpleNamespace(messages=ScoreTable())

    async def nobody_away(*_a, **_k):
        return {}

    async def nothing(*_a, **_k):
        return None

    async def no_rows(kind, for_rules):
        return []

    real_send = bot._send_drip_message

    async def recording_send(channel, message, **kw):
        start = len(POSTED)
        await real_send(channel, message, **kw)
        SENT.append({"day": kw.get("marker"), "slot": message.get("slot"),
                     "rule": message.get("rule_id") or "—", "type": message.get("type"),
                     "posts": list(POSTED[start:])})

    leave.who_is_away = nobody_away
    # AN EMPTY SHEET: the only rule with anything to say is R1.
    gtm_sheet.SHEETS.cadence_tab = lambda *a, **k: (
        SimpleNamespace(rows=[], title="Outreach PoCs"), "fixture")
    bot._split_active = lambda rows, why: (list(rows), [])
    bot._rule_tab_rows = no_rows
    bot._send_drip_message = recording_send
    bot._convert_if_already_done = nothing
    bot._sweep_proposals = nothing
    bot._maybe_poll_feeds = nothing          # the store is already filled
    # ONLY R1 IS PLANNED. On a Tuesday R2 (the company screen) runs too, and it
    # asks the model a question this script's stand-in cannot answer.
    real_plan = bot._plan_drip

    async def only_news(**kw):
        return await real_plan(**{**kw, "only_rule": "R1"})

    bot._plan_drip = only_news
    bot.get_channel = lambda cid: FakeChannel(cid)
    return bot


def begin(label: str) -> None:
    POSTED.clear()
    SENT.clear()
    LOGS.clear()
    tone.RNG = random.Random(20260928)
    voice._last_choice.clear()
    print(f"\n   ---- {label} ----")


def pretend(day: date, hh: int, mm: int = 0) -> None:
    was = config.SALES_TEST_MODE
    config.SALES_TEST_MODE = True              # only to be allowed to set the day
    clock.set_day(day, by="verify_s2")
    config.SALES_TEST_MODE = was
    clock.set_time_of_day(time(hh, mm), by="verify_s2")


def collect(bot) -> dict:
    return {"posts": [strip(p) for s in SENT for p in s["posts"]],
            "raw": [p for s in SENT for p in s["posts"]], "bot": bot,
            "logs": list(LOGS)}


async def live_path(day: date, items: list) -> dict:
    """The live sweep's own method, ticked at 14:00 and twice more."""
    begin(f"LIVE (dry run) — _maybe_send_drip on {dl.iso(day)}")
    config.SALES_TEST_MODE = False
    bot = new_bot(fresh_db("live", items))
    bot._live_loop_held = lambda what: False
    bot._maybe_breaking_news = bot.__class__._maybe_breaking_news.__get__(bot)
    sent: list = []
    for hh, mm in ((14, 0), (14, 31), (15, 2)):
        pretend(day, hh, mm)
        before = len(POSTED)
        await bot._maybe_send_drip()
        sent.append((f"{hh}:{mm:02d}", len(POSTED) - before))
    clock.clear_time_override(why="verify_s2")
    got = collect(bot)
    got["ticks"] = sent
    return got


async def test_path(day: date, items: list, *, bot=None, label: str = "") -> dict:
    begin(label or f"TEST DAY — \"make it {dl.iso(day)}\"")
    config.SALES_TEST_MODE = True
    if bot is None:
        bot = new_bot(fresh_db("testday", items))
    bot._maybe_breaking_news = _quiet_check
    await bot._handle_test_command(FakeMessage(), "make it " + dl.iso(day))
    return collect(bot)


async def sim_path(day: date, items: list) -> dict:
    begin(f"SIMULATION — \"simulate {dl.iso(day)}\"")
    config.SALES_TEST_MODE = True
    bot = new_bot(fresh_db("sim", items))
    bot._maybe_breaking_news = _quiet_check
    await bot._handle_simulation(FakeMessage(), "simulate " + dl.iso(day))
    return collect(bot)


async def _quiet_check(*_a, **_k):
    return None


def show(posts: list) -> None:
    for n, body in enumerate(posts, 1):
        for line in body.splitlines():
            print(f"     {n} | {line}")
        print("       |")


def bullets(body: str) -> list:
    """The stories in a post, by headline, in the order posted."""
    return [line[2:].split(" — ")[0].strip() for line in str(body).splitlines()
            if line.startswith("• ")]


# -- (p) the PoC machinery -----------------------------------------------------


def rss(entries: list) -> bytes:
    """A feed with these (title, link, outlet) entries, published now."""
    stamp = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    items = "".join(
        f"<item><title>{t} - {o}</title><link>{u}</link><pubDate>{stamp}</pubDate>"
        f"<source url=\"https://{o.lower().replace(' ', '')}.test\">{o}</source></item>"
        for t, u, o in entries)
    return (f"<?xml version=\"1.0\"?><rss version=\"2.0\"><channel>"
            f"<title>Feed</title>{items}</channel></rss>").encode("utf-8")


async def the_poc_machinery() -> None:
    say("(p) OUR PoCs — who is looked up, whose turn it is, and what counts as theirs")
    from bot import SalesBot

    pipeline = SimpleNamespace(title="Master Pipeline", rows=[
        {"_row": 2, "company": "Synthflow AI"}, {"_row": 3, "company": "PolyAI"},
        {"_row": 4, "company": "AI"}])
    pocs = SimpleNamespace(title="Outreach PoCs", rows=[
        {"_row": 2, "company": "PolyAI", "name": "Maya Rao"},
        {"_row": 3, "company": "Deepgram", "name": "Priya Nair"},
        {"_row": 4, "company": "Deepgram", "name": "Barret Zoph"},
        {"_row": 5, "company": "Lost Co", "name": "Lee Park", "deal_status": "Lost"},
        {"_row": 6, "company": "Quiet Co", "name": "Not Active"}])
    gone = {"name": "Barret Zoph", "move": "Deepgram->OpenAI"}

    def departures(known: bool):
        people = {"barret zoph": gone} if known else {}
        return SimpleNamespace(
            people=people, lookup=lambda n: people.get(gtm_sheet.normalise_header(n)))

    gtm_sheet.SHEETS.tab = lambda kind, *a, **k: (
        pipeline if kind == gtm_sheet.RESEARCHER_LINES else None)
    gtm_sheet.SHEETS.cadence_tab = lambda *a, **k: (pocs, "fixture")
    path = fresh_db("poc", [])
    bot = SalesBot()
    bot.db = DB(path)
    # "Not Active" has not passed the activation gate; everybody else has.
    bot._split_active = lambda rows, why: (
        [r for r in rows if r.get("name") != "Not Active"],
        [r for r in rows if r.get("name") == "Not Active"])

    mapping_sheet.MAPPING.departures = lambda: departures(True)
    LOGS.clear()
    targets = await bot._news_poc_targets()
    print("   the fixture sheet: Master Pipeline has Synthflow AI, PolyAI and \"AI\"; "
          "Outreach PoCs has Maya Rao (PolyAI), Priya Nair (Deepgram), Barret Zoph "
          "(Deepgram — ON THE DEPARTURES LIST), Lee Park (Lost Co — deal Lost) and "
          "one row that is not active.")
    for t in targets:
        print(f"     {t['kind']:<8} {feeds.poc_query(t):<32} -> ({feeds.sheet_ref(t)})")
    for line in [x for x in LOGS if "departures" in x]:
        print("   log: " + line)
    names = sorted(t["name"] for t in targets)
    check("nobody on the departures list is looked up", "Barret Zoph" in names, False)
    check("...nor a stopped row, nor a row that is not active",
          [n for n in ("Lee Park", "Lost Co", "Not Active", "Quiet Co") if n in names], [])
    check("the people on active rows are", sorted(
        t["name"] for t in targets if t["kind"] == "poc"), ["Maya Rao", "Priya Nair"])
    check("a company on both tabs says both",
          next(t["source"] for t in targets if t["name"] == "PolyAI"),
          "Master Pipeline and Outreach PoCs")
    check("a person's query is \"<person>\" \"<company>\"; a company's is \"<company>\"",
          (feeds.poc_query(next(t for t in targets if t["name"] == "Priya Nair")),
           feeds.poc_query(next(t for t in targets if t["name"] == "Synthflow AI"))),
          ('"Priya Nair" "Deepgram"', '"Synthflow AI"'))

    mapping_sheet.MAPPING.departures = lambda: departures(False)
    LOGS.clear()
    blind = await bot._news_poc_targets()
    for line in [x for x in LOGS if "departures" in x]:
        print("   log: " + line[:170])
    check("with the departures list unreadable, NO person is looked up — companies only",
          sorted({t["kind"] for t in blind}), ["company"])
    mapping_sheet.MAPPING.departures = lambda: departures(True)

    print("\n   THE ROTATION — 40 names, NEWS_POC_TARGETS_PER_DAY=15")
    store = DB(fresh_db("rotation", []))
    forty = [{"key": f"company|c{i:02d}", "kind": "company", "name": f"Company {i:02d}",
              "company": f"Company {i:02d}", "source": "Master Pipeline",
              "sheet_row": i + 2} for i in range(40)]
    print(f"   sync: {store.news_targets_sync(forty)}")
    day1 = [t["name"] for t in store.news_targets_for_day("2026-09-28", 15)]
    again = [t["name"] for t in store.news_targets_for_day("2026-09-28", 15)]
    day2 = [t["name"] for t in store.news_targets_for_day("2026-09-29", 15)]
    day3 = [t["name"] for t in store.news_targets_for_day("2026-09-30", 15)]
    print(f"   Mon: {', '.join(n[-2:] for n in sorted(day1))}")
    print(f"   Tue: {', '.join(n[-2:] for n in sorted(day2))}")
    print(f"   Wed: {', '.join(n[-2:] for n in sorted(day3))}")
    check("the order is a fixed shuffle, not the alphabet",
          sorted(day1) == [f"Company {i:02d}" for i in range(15)], False)
    check("15 names a day", (len(day1), len(day2), len(day3)), (15, 15, 15))
    check("the same 15 all day — a second poll, or a restart, does not move it on",
          sorted(again), sorted(day1))
    check("the next day takes the next 15, none of them yesterday's",
          set(day1) & set(day2), set())
    check("the third day finishes the list, then starts again with the oldest",
          (len(set(day3) - set(day1) - set(day2)), len(set(day3) & set(day1)),
           len(set(day3) & set(day2))), (10, 5, 0))
    left = forty[:39]
    print(f"   one name leaves the sheet -> sync: {store.news_targets_sync(left)}")
    check("it is out of the rotation", store.news_targets_count(), 39)

    print("\n   A POLL — the outlet's feed, then the PoC queries (HTTP stubbed)")
    store = DB(fresh_db("poll", []))
    store.news_targets_sync([t for t in targets
                             if t["name"] in ("Synthflow AI", "Priya Nair")])
    asked: list = []

    def fake_get(url: str) -> bytes:
        asked.append(urllib.parse.unquote_plus(url.split("q=", 1)[1].split("&")[0])
                     if "q=" in url else url)
        if "outlet.test" in url:
            return rss([("Synthflow AI raises $20M Series A",
                         "https://outlet.test/synthflow-series-a", "Outlet")])
        if "Synthflow" in url:
            return rss([
                ("Synthflow AI raises $20M Series A",
                 "https://news.google.com/rss/articles/abc", "Wire"),
                ("Voice startups to watch in 2026",
                 "https://news.google.com/rss/articles/def", "Blog")])
        return rss([
            ("Deepgram names Priya Nair chief revenue officer",
             "https://news.google.com/rss/articles/ghi", "Wire"),
            ("Deepgram ships Nova-4", "https://news.google.com/rss/articles/jkl", "Wire")])

    saved = (config.NEWS_RSS_FEEDS, config.NEWS_TOPICS)
    config.NEWS_RSS_FEEDS, config.NEWS_TOPICS = ["https://outlet.test/feed"], []
    feeds._get = fake_get
    feeds.bind(lambda: store)
    LOGS.clear()
    try:
        result = feeds.poll()
    finally:
        config.NEWS_RSS_FEEDS, config.NEWS_TOPICS = saved
        feeds._get = REAL["get"]
    for q in asked:
        print(f"     GET {q}")
    for line in [x for x in LOGS if x.startswith("[feeds] poll")]:
        print("   log: " + line)
    rows = store.news_feed_between("2000-01-01", "2100-01-01")
    for r in sorted(rows, key=lambda r: r["title"]):
        print(f"     stored  {r['kind']:<8} {r['title']:<50} ({r['sheet_ref']})")
    check("three feeds read: the outlet and two PoC names", result["feeds"], 3)
    check("the PoC queries ask for the quoted name, 7 days back",
          sorted(asked)[:2], ['"Priya Nair" "Deepgram" when:7d', '"Synthflow AI" when:7d'])
    check("an item is PoC news only when it NAMES them: 2 of the 4 the queries "
          "returned", result["poc_items"], 2)
    check("\"Voice startups to watch\" and \"Deepgram ships Nova-4\" are not stored",
          sorted(r["title"] for r in rows),
          ["Deepgram names Priya Nair chief revenue officer",
           "Synthflow AI raises $20M Series A"])
    synth = next(r for r in rows if r["title"].startswith("Synthflow"))
    check("the outlet's own row was kept (its link) and marked as ours",
          (synth["url"], synth["kind"], synth["sheet_ref"]),
          ("https://outlet.test/synthflow-series-a", "poc",
           "Synthflow AI — on Master Pipeline"))
    check("a person's story carries the person and the tab",
          next(r["sheet_ref"] for r in rows if "Priya" in r["title"]),
          "Priya Nair, Deepgram — on Outreach PoCs")
    check("no search request was made", SEARCHES, [])


# -- (i) (iv) (v) the Monday ---------------------------------------------------


def monday_items() -> list:
    VERDICTS.clear()
    return [
        story("Synthflow AI raises $20M Series A", ist(SAT, 11),
              ("voice agent", 4, "Accel led the round"),
              ref="Synthflow AI — on Master Pipeline"),
        story("Deepgram names Priya Nair chief revenue officer", ist(MON, 9),
              ("OTHER", 3, "she joins from Twilio"),
              ref="Priya Nair, Deepgram — on Outreach PoCs"),
        story("PolyAI opens a Bengaluru office", ist(FRI, 18),
              ("OTHER", 3, "its first engineering site in India"),
              ref="PolyAI — on Master Pipeline and Outreach PoCs"),
        story("OpenAI ships a new evals suite for agents", ist(MON, 8),
              ("evals", 4, "it scores multi-step tool use")),
        story("Scale AI cuts a fifth of its RLHF contractors", ist(SUN, 20),
              ("RLHF", 4, "the cuts follow a lost contract")),
        story("ElevenLabs adds Hindi to its voice agent platform", ist(MON, 10),
              ("voice agent", 3, "Hindi and Tamil are live")),
        story("Ten prompts every marketer should try", ist(MON, 7), ("OTHER", 1, "")),
    ]


def ledger(path: str) -> dict:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    out = {
        "llm": [dict(r) for r in con.execute(
            "SELECT site, model, input_tokens, output_tokens, ok FROM llm_calls")],
        "quota": con.execute("SELECT COALESCE(SUM(requests), 0) FROM "
                             "search_quota_usage").fetchone()[0],
        "web": con.execute("SELECT COALESCE(SUM(searches), 0) FROM "
                           "web_search_usage").fetchone()[0],
        "stories": [dict(r) for r in con.execute(
            "SELECT title, kind, news_kind, sheet_ref, importance FROM news_stories "
            "ORDER BY rowid")],
        "drip": [dict(r) for r in con.execute(
            "SELECT action_type, counts_toward_cap FROM drip_sends")],
        "checks": con.execute("SELECT COUNT(*) FROM news_checks WHERE posted > 0"
                              ).fetchone()[0],
    }
    con.close()
    return out


async def the_monday() -> None:
    say(f"(i) A MONDAY WITH 3 PoC + 3 INDUSTRY STORIES — {MON:%A %d %b %Y}")
    items = monday_items()
    print("   in the feed store, unscored, as a poll left them:")
    for it in items:
        v = VERDICTS[it["title"]]
        print(f"     {it['kind']:<8} imp {v[1]}  {v[0]:<12} "
              f"{dl.parse_date(it['published_at'][:10]):%a} {it['title']}"
              + (f"  ({it['sheet_ref']})" if it["sheet_ref"] else ""))

    live = await live_path(MON, items)
    show(live["raw"])
    print("     ticks (time -> messages posted): "
          + ", ".join(f"{t}->{n}" for t, n in live["ticks"]))
    for line in [x for x in live["logs"] if "main sweep" in x and " to " in x]:
        print("   log: " + line)

    check("two messages: the news post, then \"More AI news today\"",
          (len(live["posts"]), live["posts"][1].splitlines()[0] if len(live["posts"]) > 1
           else ""), (2, news.OVERFLOW_HEADING))
    main, over = (live["posts"] + ["", ""])[:2]
    check("the post: the top 2 PoC stories first, then the best 3 of the rest",
          bullets(main), [
              "Synthflow AI raises $20M Series A",                 # PoC, 4
              "Deepgram names Priya Nair chief revenue officer",   # PoC, 3, the newer
              "OpenAI ships a new evals suite for agents",         # 4, the newer
              "Scale AI cuts a fifth of its RLHF contractors",     # 4
              "PolyAI opens a Bengaluru office"])                  # 3: PoC beats industry
    check("the 6th is in \"More AI news today\"", bullets(over),
          ["ElevenLabs adds Hindi to its voice agent platform"])
    check("the filler is in neither", "Ten prompts" in main + over, False)
    check("each PoC story carries its sheet row, in brackets",
          [ref for ref in ("(Synthflow AI — on Master Pipeline)",
                           "(Priya Nair, Deepgram — on Outreach PoCs)",
                           "(PolyAI — on Master Pipeline and Outreach PoCs)")
           if ref in main], ["(Synthflow AI — on Master Pipeline)",
                             "(Priya Nair, Deepgram — on Outreach PoCs)",
                             "(PolyAI — on Master Pipeline and Outreach PoCs)"])
    check("an industry story has none",
          "(" in next(l for l in main.splitlines() if "OpenAI ships" in l).split(" [")[0],
          False)
    check("the later ticks posted nothing more", [n for _t, n in live["ticks"]], [2, 0, 0])

    book = ledger(live["bot"].db.path)
    print("\n   news_stories after the live day:")
    for r in book["stories"]:
        print(f"     kind={r['kind']:<9} news_kind={r['news_kind']:<9} imp {r['importance']}  "
              f"{r['title'][:48]}")
    check("recorded: 5 in the main post, 1 as overflow — so none repeats",
          sorted(r["kind"] for r in book["stories"]),
          ["main"] * 5 + ["overflow"])
    check("3 of them tagged poc, 3 industry",
          sorted(r["news_kind"] for r in book["stories"]),
          ["industry"] * 3 + ["poc"] * 3)
    check("the overflow post is outside the cap: one drip_sends row, the news post",
          book["drip"], [{"action_type": "ai_news", "counts_toward_cap": 1}])
    check("...and not part of the breaking valve: no breaking message counted",
          book["checks"], 0)

    say("(iv) WHAT IT COST — llm_calls, and the search ledgers")
    for r in book["llm"]:
        print(f"     llm_calls: site={r['site']:<18} model={r['model']:<22} "
              f"in={r['input_tokens']} out={r['output_tokens']} ok={r['ok']}")
    print(f"     search_quota_usage requests: {book['quota']}   web_search_usage "
          f"searches: {book['web']}   search requests attempted: {len(SEARCHES)}   "
          f"WEB_SEARCH_ENABLED={config.WEB_SEARCH_ENABLED}")
    check("one model call for the day's news, and it is the scoring call",
          [r["site"] for r in book["llm"]], ["score_news:main"])
    check("...on MODEL_LIGHT", {r["model"] for r in book["llm"]}, {config.MODEL_LIGHT})
    check("no search call: nothing in either search ledger, nothing attempted",
          (book["quota"], book["web"], SEARCHES), (0, 0, []))

    say("(v) THE SAME ON \"make it <monday>\" AND \"simulate <monday>\"")
    test = await test_path(MON, monday_items())
    show(test["raw"])
    sim = await sim_path(MON, monday_items())
    show(sim["raw"])
    check("the same two messages, word for word, on all three (apart from [TEST])",
          live["posts"] == test["posts"] == sim["posts"])
    check("the live run carries no [TEST] tag; the other two tag both messages",
          ([p.startswith("[TEST]") for p in live["raw"]],
           [p.startswith("[TEST]") for p in test["raw"]],
           [p.startswith("[TEST]") for p in sim["raw"]]),
          ([False, False], [True, True], [True, True]))
    tbook = ledger(test["bot"].db.path)
    check("the test day cost the same: one MODEL_LIGHT scoring call, no search",
          ([r["site"] for r in tbook["llm"]], {r["model"] for r in tbook["llm"]},
           tbook["quota"] + tbook["web"]), (["score_news:main"], {config.MODEL_LIGHT}, 0))
    again = await test_path(MON, [], bot=test["bot"],
                            label=f"THE SAME TEST DAY AGAIN — \"make it {dl.iso(MON)}\"")
    check("a second run of the same pretend day posts the same two messages",
          again["posts"], test["posts"])
    check("...from the day's cache: still one scoring call in the ledger",
          len(ledger(test["bot"].db.path)["llm"]), 1)


# -- (ii) OTHER past a full topic cap ------------------------------------------


async def the_offtopic() -> None:
    say(f"(ii) AN OTHER STORY AT IMPORTANCE 4, PAST A FULL TOPIC CAP — {TUE:%A %d %b}")
    VERDICTS.clear()
    items = [
        story("A court rules model weights are not copyrightable", ist(TUE, 9),
              ("OTHER", 4, "the ruling covers trained weights")),
        story("A chip startup demos an analog inference board", ist(TUE, 10),
              ("OTHER", 3, "it runs a 7B model at 4 watts")),
        story("A new evals leaderboard for coding agents", ist(TUE, 8),
              ("evals", 3, "it ranks 40 agents on repo tasks")),
    ]
    config.SALES_TEST_MODE = True
    bot = new_bot(fresh_db("offtopic", items))
    earlier = [
        {"url": f"https://example.org/earlier-{i}", "url_key": f"example.org/earlier-{i}",
         "headline": f"Earlier OTHER story {i}", "headline_key": f"earlier other story {i}",
         "topic": "OTHER", "importance": 5, "kind": "industry"} for i in (1, 2)]
    bot.db.record_news_stories(earlier, on_date=dl.iso(TUE), rule_id="R1", kind="breaking")
    print(f"   already posted today: 2 OTHER stories (a breaking message this morning) — "
          f"OTHER is at {bot.db.news_topic_count_today('OTHER', dl.iso(TUE))} of "
          f"NEWS_PER_TOPIC_PER_DAY={config.NEWS_PER_TOPIC_PER_DAY}")
    run = await test_path(TUE, [], bot=bot)
    show(run["raw"])
    for line in [x for x in run["logs"] if "main sweep: " in x]:
        print("   log: " + line[:210])
    main, over = (run["posts"] + ["", ""])[:2]
    check("the OTHER story at importance 4 is in the post, past the full cap",
          "A court rules model weights are not copyrightable" in bullets(main), True)
    check("the OTHER story at 3 is not — the cap held it",
          "A chip startup demos an analog inference board" in bullets(main), False)
    check("...and it is not lost: it is in \"More AI news today\"", bullets(over),
          ["A chip startup demos an analog inference board"])
    check("the on-topic story got in as usual",
          "A new evals leaderboard for coding agents" in bullets(main), True)


# -- (iii) held on Saturday, posted on Monday ----------------------------------


async def the_weekend() -> None:
    say("(iii) A SATURDAY STORY THE VALVE HELD APPEARS ON MONDAY")
    VERDICTS.clear()
    big = story("Anthropic and Google DeepMind merge their safety labs", ist(SAT, 15, 30),
                ("OTHER", 5, "the two teams combine under one director"))
    small = story("A new evals leaderboard for coding agents", ist(MON, 8),
                  ("evals", 3, "it ranks 40 agents on repo tasks"))
    config.SALES_TEST_MODE = True
    bot = new_bot(fresh_db("weekend", [big, small]))
    real_check = bot.__class__._maybe_breaking_news.__get__(bot)
    sat = dl.iso(SAT)
    for slot in ("11:00", "12:00"):
        bot.db.claim_news_check(sat, slot, ran_at=ist(SAT, int(slot[:2])).isoformat())
        bot.db.finish_news_check(sat, slot, searches=0, found=1, posted=1)
    print(f"   Saturday {sat}: {bot.db.news_breaking_messages_today(sat)} breaking "
          f"message(s) already sent — the valve (NEWS_BREAKING_MAX_PER_DAY="
          f"{config.NEWS_BREAKING_MAX_PER_DAY}) is full")
    print(f"   15:30 IST: \"{big['title']}\" is published")

    begin(f"THE SATURDAY 16:00 CHECK — _maybe_breaking_news on {sat}")
    outcome = await real_check(force=True, channel=TEST_CHANNEL, at=ist(SAT, 16))
    for line in [x for x in LOGS if x.startswith("[news-check]")]:
        print("   log: " + line[:230])
    print(f"   outcome: found={outcome['found']} kept={len(outcome['kept'])} "
          f"posted={outcome['posted']} held={outcome['held']}; messages posted: "
          f"{len(POSTED)}")
    check("the check found it, at importance 5", [s["importance"] for s in outcome["kept"]],
          [5])
    check("...and held it: the valve was full, nothing was posted",
          (outcome["held"], outcome["posted"], len(POSTED)), (True, 0, 0))
    check("it was not recorded as posted",
          bot.db.news_story_seen(big["url_key"], big["headline_key"],
                                 since_iso="2026-01-01"), None)

    since, until = bot._main_window(MON)
    print(f"\n   Monday's window: {since:%a %d %b %H:%M} -> {until:%a %d %b %H:%M} IST "
          f"(a fixed 24 hours would have begun {ist(SUN, 14):%a %d %b %H:%M})")
    check("the window runs from Friday's post to Monday's",
          (since, until), (ist(FRI, 14), ist(MON, 14)))
    check("the story is inside it — and was outside a 24-hour one",
          (since <= ist(SAT, 15, 30) <= until, ist(SAT, 15, 30) >= ist(SUN, 14)),
          (True, False))
    tue_since, _tue_until = bot._main_window(TUE)
    check("an ordinary day's window is still the previous post to this one",
          tue_since, ist(MON, 14))

    run = await test_path(MON, [], bot=bot,
                          label=f"THE MONDAY — \"make it {dl.iso(MON)}\", same database")
    show(run["raw"])
    for line in [x for x in run["logs"] if "main sweep, " in x]:
        print("   log: " + line)
    main = (run["posts"] + [""])[0]
    check("Saturday's story is in Monday's post, and leads it", bullets(main)[:1],
          ["Anthropic and Google DeepMind merge their safety labs"])
    check("...with Monday's own news after it", bullets(main)[1:],
          ["A new evals leaderboard for coding agents"])
    row = bot.db.news_story_seen(big["url_key"], big["headline_key"],
                                 since_iso="2026-01-01") or {}
    check("now it is recorded, as part of Monday's main post",
          (row.get("posted_on"), row.get("kind")), (dl.iso(MON), "main"))


# -- (w) the wording -----------------------------------------------------------


def the_wording() -> None:
    say("(w) THE WORDING FOR THE SHEET — printed from the rules the bot loads")
    rules_mod.reload()
    r1 = rules_mod.by_id("R1")
    print("\n   Bot Rules tab — rule 1, column \"What the Bot Shares / Checks\":")
    print("     " + rules_mod.sheet_wording_for("R1"))
    print("\n   Global Rules tab — row \"AI news\":")
    print("     " + rules_mod.global_rule("ai_news"))
    print()
    wording = rules_mod.sheet_wording_for("R1")
    check("row 1 says both kinds, the sheet row, the overflow and the window",
          [x for x in ("our own PoCs", "(Synthflow AI — on Master Pipeline)",
                       "More AI news today", "Monday's covers the weekend",
                       "departures list") if x not in wording], [])
    check("bot_rules.yaml R1 plain", r1.plain,
          "today's AI news worth reading — the industry, and our own PoCs")
    strategy = persona.load_strategy()
    check("sales_strategy.md rule 1 says the same things",
          [x for x in ("our own PoCs", "(Synthflow AI — on Master Pipeline)",
                       "More AI news today", "Friday 2 PM to Monday 2 PM",
                       "departures list") if x not in strategy], [])


# -- (L) live ------------------------------------------------------------------


async def the_live_run() -> None:
    say("(L) LIVE — the real sheet's PoC names, a real poll, one real scoring call")
    from bot import SalesBot

    gtm_sheet.SHEETS.cadence_tab = REAL["cadence_tab"]
    gtm_sheet.SHEETS.tab = REAL["tab"]
    mapping_sheet.MAPPING.departures = REAL["departures"]
    feeds._get = REAL["get"]
    for k, v in LIVE_SETTINGS.items():
        if k != "WEB_SEARCH_ENABLED":
            setattr(config, k, v)
    config.SALES_TEST_MODE = True
    path = fresh_db("livepoll", [])
    bot = SalesBot()
    bot.db = DB(path)
    LOGS.clear()
    synced = await bot._sync_news_targets(force=True)
    for line in [x for x in LOGS if "PoC" in x]:
        print("   log: " + line[:230])
    if not synced:
        print("   the rotation could not be built from the sheet; nothing to poll for")
        return
    result = await asyncio.to_thread(feeds.poll)
    for line in [x for x in LOGS if x.startswith("[feeds] poll")]:
        print("   log: " + line[:300])
    print(f"   poll: {result['ok']} of {result['feeds']} feed(s) read, "
          f"{result['fetched']} item(s), {result['poc_targets']} PoC name(s) looked up, "
          f"{result['poc_items']} item(s) naming one, {len(result['errors'])} error(s)")
    now = dl.real_now_ist()
    rows = bot.db.news_feed_between(feeds.utc_iso(now - timedelta(days=3)),
                                    feeds.utc_iso(now))
    mine = [r for r in rows if news.is_poc(r)]
    print(f"   in the store from the last 3 days: {len(rows)} item(s), {len(mine)} "
          "about our PoCs")
    for r in mine[:8]:
        print(f"     poc  {r['title'][:70]}  ({r['sheet_ref']})")
    check("the poll read feeds and made no search request",
          (result["ok"] > 0, SEARCHES), (True, []))
    if bot.llm is None:
        print("   no model client; the scoring call is skipped")
        return
    stories = await bot._score_feed(rows, today=now.date(), mode=news.MODE_MAIN)
    if stories is None:
        print("   the scoring call did not run (budget or failure)")
        return
    picked = news.choose_main(
        stories, today=now.date(), db=bot.db, cap=config.NEWS_MAX_ITEMS,
        poc_slots=config.NEWS_POC_SLOTS, per_topic=config.NEWS_PER_TOPIC_PER_DAY,
        topics_per_week=config.NEWS_TOPICS_PER_WEEK,
        min_importance=config.NEWS_BREAKING_MIN_IMPORTANCE,
        offtopic_bypass=config.NEWS_OFFTOPIC_BYPASS_IMPORTANCE,
        overflow_min=config.NEWS_OVERFLOW_MIN_IMPORTANCE,
        overflow_max=config.NEWS_OVERFLOW_MAX_ITEMS)
    print(f"   scored: {len(stories)} worth 3 or more -> {len(picked['keep'])} in the "
          f"post, {len(picked['overflow'])} for \"More AI news today\"")
    print("   what today's post would be:")
    for line in news.render(picked["keep"], mode=news.MODE_MAIN).splitlines():
        print("     | " + line[:230])
    for line in news.render(picked["overflow"], mode=news.MODE_OVERFLOW).splitlines():
        print("     | " + line[:230])
    book = ledger(path)
    for r in book["llm"]:
        print(f"     llm_calls: site={r['site']:<18} model={r['model']:<22} "
              f"in={r['input_tokens']} out={r['output_tokens']} ok={r['ok']}")
    check("the only model call was MODEL_LIGHT scoring; no search",
          ({r["site"] for r in book["llm"]} <= {"score_news:main"},
           {r["model"] for r in book["llm"]} <= {config.MODEL_LIGHT},
           book["quota"] + book["web"], SEARCHES), (True, True, 0, []))


async def main() -> None:
    print(f"real date {dl.iso(dl.real_today_ist())} | pretend Monday {dl.iso(MON)} | "
          f"NEWS_MAX_ITEMS {config.NEWS_MAX_ITEMS} | NEWS_POC_SLOTS "
          f"{config.NEWS_POC_SLOTS} | overflow max {config.NEWS_OVERFLOW_MAX_ITEMS} | "
          f"MODEL_LIGHT {config.MODEL_LIGHT} | WEB_SEARCH_ENABLED "
          f"{config.WEB_SEARCH_ENABLED}")
    if LIVE:
        await the_live_run()         # first: before the fixture replaces the sheet
    if want("p"):
        await the_poc_machinery()
    if want("i") or want("iv") or want("v"):
        await the_monday()
    if want("ii"):
        await the_offtopic()
    if want("iii"):
        await the_weekend()
    if want("w"):
        the_wording()


try:
    asyncio.run(main())
finally:
    clock.forget()
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
sys.exit(1 if failures else 0)
