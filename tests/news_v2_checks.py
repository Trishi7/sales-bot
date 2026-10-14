"""THE CHECKS for NFT2-1087 "Saley AI News Flow", written once: T1 to T83.

`run(check)` is called by verify_news_v2.py (which prints and counts) and each
group by tests/test_news_v2.py (which asserts). `check(name, got, want=True)`;
a check that IS one of the ticket's test cases is named "T<n> <case>", and the
ticket's edge cases (section 14) are named "edge <n> ...".

OFFLINE, AND ON A CLOCK OF ITS OWN. A whole bot behind fake Discord
(tests/replies_world.py) on a throwaway database:

  the feeds    `feeds._get` and `arxiv._get` are replaced by `Net`, which serves
               canned RSS by what the query asks for and records every request.
               `feeds._sleep` / `_monotonic` (and arxiv's) are a pretend clock,
               so the 1.5 s and 3 s gaps are measured without waiting.
  the scorer   `llm.score_news` is a stand-in that answers from a table of
               title -> score and records what it was shown. Only its ANSWERS
               are made up: the prompt, the parsing and the writing back are
               the real ones.
  the clock    a cycle is handed the real "now" it should stand at (`now=`),
               the daily post runs on a pinned pretend day, and every story is
               stamped relative to those — the result is the same whenever
               this runs.
  the sheets   the name list is built by the real `feeds.build_names` from
               made-up rows; where the bot's own reader is under test the two
               tabs and the departures list are stand-ins.

Every setting a check leans on is pinned here to its .env.example default, so
the laptop's .env cannot answer for it.
"""
import contextlib
import inspect
import logging
import os
import re
import subprocess
import sys
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote_plus, urlsplit

import replies_world as rw

import arxiv
import config
import deadlines as dl
import drip
import feeds
import news
import nextaction

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

WED, THU, FRI = date(2026, 10, 7), date(2026, 10, 8), date(2026, 10, 9)
SAT, SUN, MON, TUE = (date(2026, 10, 10), date(2026, 10, 11), date(2026, 10, 12),
                      date(2026, 10, 13))
TAGS = f"<@{rw.APPROVER}> <@{rw.APPROVER2}>"
G = "https://news.google.com/rss/articles/"

GENERIC = ["Google", "Meta", "Microsoft", "NVIDIA", "Apple", "OpenAI", "Amazon", "Arcade",
           "Multiple", "Cleo"]
WATCH = ["Scale AI", "Surge AI", "Mercor", "Toloka", "Appen", "Labelbox", "Prolific",
         "Handshake AI", "Invisible Technologies"]
INSTITUTION = ["university", "institute of", "college", "school of", "IIT", "IISc"]

# THE SHIPPED DEFAULTS (.env.example), pinned.
PINS = {
    "NEWS_MAX_ITEMS": 5, "NEWS_REPEAT_DAYS": 30, "NEWS_FEED_KEEP_DAYS": 14,
    "NEWS_PER_TOPIC_PER_DAY": 2, "NEWS_TOPICS_PER_WEEK": 6,
    "NEWS_BREAKING_MIN_IMPORTANCE": 5, "NEWS_OFFTOPIC_BYPASS_IMPORTANCE": 4,
    "NEWS_SCORE_MAX_ITEMS": 40, "TOKEN_DAILY_BUDGET": 0,
    "NEWS_PULL_FIRST": "14:00", "NEWS_PULL_UNTIL": "23:00", "NEWS_PULL_WINDOW_HOURS": 2,
    "NEWS_NAME_BATCH_SIZE": 15, "NEWS_GENERIC_NAMES": list(GENERIC),
    "NEWS_WATCH_NAMES": list(WATCH), "NEWS_INSTITUTION_WORDS": list(INSTITUTION),
    "NEWS_REQUEST_GAP_SECONDS": 1.5, "NEWS_BREAKING_MAX_PER_CYCLE": 3,
    "NEWS_POC_BREAKING_MIN": 4, "NEWS_CARRY_MAX_DAYS": 3,
    "NEWS_BREAKING_MAX_AGE_HOURS": 3, "ARXIV_ENABLED": True, "ARXIV_BATCH_SIZE": 20,
    "NEWS_TOPICS": ["evals", "RLHF", "voice agent", "AI safety", "TTS", "STT"],
    "NEWS_RSS_FEEDS": ["https://outlet.example/feed"],
    "NEWS_BLOCKED_DOMAINS": [], "NEWS_FEED_TIMEOUT_SECONDS": 10,
    "NEWS_HOLD_AFTER_TEST_SECONDS": 300,
    "SALES_ALWAYS_TAG_IDS": [rw.APPROVER, rw.APPROVER2],
}

# Every message any scenario here sent, for T82.
ALL_POSTED: list = []


# -- the templates, word for word from the ticket (section 16) ----------------

TEMPLATE_A = """**AI News, Tue 13 Oct** @Vaishnavi @Sid
Hey here's today's round-up:
Our PoCs are in the news:
- Sarvam AI raises $90M to scale Indic language models ([Economic Times](<url>))

- Dr. David Gamez on machine consciousness at the Royal Institution ([The Guardian](<url>))
Elsewhere in AI
- State AGs launch investigations into OpenAI AI safety practices ([Reuters](<url>))

- UK AI Security Institute publishes evaluation standards draft ([gov.uk](<url>))

- 'Brutal session' for chip stocks shows AI trade is turning ([Reuters](<url>))
Let me know if you want me to dig into any of these"""

TEMPLATE_B = """**AI News, Tue 13 Oct** @Vaishnavi @Sid
Hey here's today's round-up:
Our PoCs are in the news:
- Sarvam AI raises $90M to scale Indic language models ([Economic Times](<url>))

- Dr. David Gamez on machine consciousness at the Royal Institution ([The Guardian](<url>))
Let me know if you want me to dig into any of these"""

TEMPLATE_C = """**AI News, Tue 13 Oct** @Vaishnavi @Sid
Hey here's today's round-up:
- State AGs launch investigations into OpenAI AI safety practices ([Reuters](<url>))

- UK AI Security Institute publishes evaluation standards draft ([gov.uk](<url>))

- 'Brutal session' for chip stocks shows AI trade is turning ([Reuters](<url>))
Let me know if you want me to dig into any of these"""

TEMPLATE_E = """**Breaking news** @Vaishnavi @Sid
- OpenAI releases GPT-6 with native tool use ([The Verge](<url>))"""

TEMPLATE_F = """**Breaking news** @Vaishnavi @Sid
- Sarvam AI acquired by Reliance Jio for $400M ([Economic Times](<url>))

- OpenAI releases GPT-6 with native tool use ([The Verge](<url>))

- Scale AI lays off 20% of its data-labeling workforce ([Reuters](<url>))"""

TEMPLATE_G = """**AI News, Tue 13 Oct**
- Sarvam AI raises $90M to scale Indic language models ([Economic Times](<url>))

- State AGs launch investigations into OpenAI AI safety practices ([Reuters](<url>))"""

# A WORD PER STORY. `news.headline_key` drops digits, so "story 1" and "story
# 2" are ONE story to the store; made-up headlines differ by a word instead.
W = ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india",
     "juliet", "kilo", "lima")
CITIES = ("Delhi", "Mumbai", "Pune", "Chennai", "Kochi")

SARVAM = "Sarvam AI raises $90M to scale Indic language models"
GAMEZ = "Dr. David Gamez on machine consciousness at the Royal Institution"
STATE_AGS = "State AGs launch investigations into OpenAI AI safety practices"
UK_AISI = "UK AI Security Institute publishes evaluation standards draft"
BRUTAL = "'Brutal session' for chip stocks shows AI trade is turning"
GPT6 = "OpenAI releases GPT-6 with native tool use"
JIO = "Sarvam AI acquired by Reliance Jio for $400M"
LAYOFFS = "Scale AI lays off 20% of its data-labeling workforce"


def slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(title).lower()).strip("-")


def link(title: str, *, direct: str = "") -> str:
    """The made-up link of a story: a Google News one, or one on `direct`."""
    return f"https://{direct}/{slug(title)}" if direct else f"{G}{slug(title)}?oc=5"


def template(text: str, titles: list, *, tags: str = TAGS, direct: dict = None) -> str:
    """One of the ticket's templates with its two placeholders filled in:
    "@Vaishnavi @Sid" becomes `tags` and each "<url>" the story's link, in the
    order the stories are listed. Every other character is the ticket's."""
    out = text.replace("@Vaishnavi @Sid", tags)
    for t in titles:
        out = out.replace("(<url>)", f"(<{link(t, direct=(direct or {}).get(t, ''))}>)", 1)
    return out


# -- time ---------------------------------------------------------------------


def ist(day: date, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(day, time(hh, mm), dl.IST)


def utc(day: date, hh: int, mm: int = 0) -> datetime:
    return ist(day, hh, mm).astimezone(timezone.utc)


# -- canned feeds -------------------------------------------------------------


def rss(items: list, *, title: str = "feed") -> bytes:
    """RSS for [{title, when, source?, url?, summary?, direct?}]. With a
    `source` it is a Google News item: " - Source" on the title, as Google
    writes it, and a <source> element."""
    out = [f'<?xml version="1.0"?><rss version="2.0"><channel><title>{title}</title>']
    for it in items:
        head = _x(it["title"]) + (f" - {_x(it['source'])}" if it.get("source") else "")
        url = it.get("url") or link(it["title"], direct=it.get("direct", ""))
        when = it["when"].astimezone(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
        out.append(f"<item><title>{head}</title><link>{_x(url)}</link>"
                   f"<description>{_x(it.get('summary', ''))}</description>"
                   f"<pubDate>{when}</pubDate>"
                   + (f'<source url="https://x.example">{_x(it["source"])}</source>'
                      if it.get("source") else "") + "</item>")
    out.append("</channel></rss>")
    return "".join(out).encode("utf-8")


def _x(text) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def atom(entries: list) -> bytes:
    """An arXiv answer for [{n, title, authors, category, when}]."""
    out = ['<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom" '
           'xmlns:arxiv="http://arxiv.org/schemas/atom"><title>ArXiv Query</title>']
    for e in entries:
        stamp = e["when"].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        people = "".join(f"<author><name>{_x(a)}</name></author>" for a in e["authors"])
        out.append(f"<entry><id>http://arxiv.org/abs/2610.{e['n']:05d}v1</id>"
                   f"<title>{_x(e['title'])}</title><summary>An abstract.</summary>"
                   f"<published>{stamp}</published><updated>{stamp}</updated>{people}"
                   f'<link href="http://arxiv.org/abs/2610.{e["n"]:05d}v1" rel="alternate" '
                   f'type="text/html"/><arxiv:primary_category term="{e["category"]}"/>'
                   f'<category term="{e["category"]}"/></entry>')
    out.append("</feed>")
    return "".join(out).encode("utf-8")


def query_of(url: str) -> str:
    """What a Google News or arXiv url asks for, decoded."""
    q = parse_qs(urlsplit(url).query)
    return unquote_plus((q.get("q") or q.get("search_query") or [""])[0])


class RateLimited(Exception):
    """What `requests` raises for an HTTP 429, as far as feeds.py can tell."""

    def __init__(self):
        super().__init__("429 Client Error: Too Many Requests")
        self.response = SimpleNamespace(status_code=429)


class Net:
    """The network, canned. `routes` is [(needle, items)]: a Google request is
    answered with the items of EVERY route whose needle is in its decoded
    query; "outlet" answers the outlet feed; `arxiv` is a list of entries
    served to every arXiv query. `fail(url, n)` may raise for request n."""

    def __init__(self, routes=None, *, arxiv_entries=None, fail=None):
        self.routes = list(routes or [])
        self.arxiv_entries = list(arxiv_entries or [])
        self.fail = fail
        self.served: list = []          # every url, in order
        self.google: list = []          # (pretend time, url) of each Google request
        self.arxiv: list = []           # (pretend time, url) of each arXiv query
        self.clock = [1000.0]

    def _sleep(self, seconds):
        self.clock[0] += float(seconds)

    def _now(self):
        return self.clock[0]

    def get(self, url):
        self.served.append(url)
        if "news.google.com" in url:
            self.google.append((self.clock[0], url))
        if self.fail is not None:
            self.fail(url, len(self.served))
        if "news.google.com" in url:
            q = query_of(url)
            items = [it for needle, rows in self.routes if needle != "outlet" and needle in q
                     for it in rows]
            return rss(items, title="Google News")
        return rss([it for needle, rows in self.routes if needle == "outlet" for it in rows],
                   title="Outlet AI desk")

    def arxiv_get(self, url):
        self.served.append(url)
        self.arxiv.append((self.clock[0], url))
        if self.fail is not None:
            self.fail(url, len(self.served))
        return atom(self.arxiv_entries)

    def __enter__(self):
        self._saved = (feeds._get, feeds._sleep, feeds._monotonic,
                       arxiv._get, arxiv._sleep, arxiv._monotonic)
        feeds._get, feeds._sleep, feeds._monotonic = self.get, self._sleep, self._now
        arxiv._get, arxiv._sleep, arxiv._monotonic = self.arxiv_get, self._sleep, self._now
        feeds._last_google[0] = None
        arxiv._last_request[0] = None
        return self

    def __exit__(self, *exc):
        (feeds._get, feeds._sleep, feeds._monotonic,
         arxiv._get, arxiv._sleep, arxiv._monotonic) = self._saved
        feeds._last_google[0] = None
        arxiv._last_request[0] = None
        return False

    def whens(self) -> list:
        """The `when:` word of every Google request made."""
        return sorted({re.search(r"when:\w+", query_of(u)).group(0) for _t, u in self.google})


class Scorer:
    """Stands in for `llm.score_news`. `scores` is title -> importance, or
    title -> (importance, topic); a title not in it gets no SCORE line. With
    `down` set it returns None: the call failed."""

    def __init__(self, scores=None):
        self.scores = dict(scores or {})
        self.shown: list = []           # the titles of every call
        self.down = False

    async def __call__(self, *, items, topics, today, mode="main"):
        self.shown.append([i["title"] for i in items])
        if self.down:
            return None
        lines = []
        for n, it in enumerate(items, 1):
            got = self.scores.get(it["title"])
            if got is None:
                continue
            imp, topic = got if isinstance(got, tuple) else (got, "OTHER")
            lines.append(f"SCORE | {n} | {topic} | {imp} | it happened")
        return "\n".join(lines) or "NOTHING FOUND"

    def calls(self) -> int:
        return len(self.shown)

    def saw(self, title: str) -> bool:
        return any(title in batch for batch in self.shown)


class Logs(logging.Handler):
    """Every log line while it is open."""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines: list = []

    def emit(self, record):
        with contextlib.suppress(Exception):
            self.lines.append((record.levelno, record.getMessage()))

    def __enter__(self):
        logging.getLogger().addHandler(self)
        return self

    def __exit__(self, *exc):
        logging.getLogger().removeHandler(self)
        return False

    def has(self, *needles, level=logging.DEBUG) -> bool:
        return any(lvl >= level and all(n in msg for n in needles) for lvl, msg in self.lines)


# -- the name list ------------------------------------------------------------


class Departures:
    def __init__(self, gone=()):
        self.people = {str(n).lower(): {"name": n} for n in gone} or {"somebody else": {}}

    def lookup(self, name):
        return self.people.get(str(name).lower())


def names_from(pipeline=(), pocs=(), *, gone=(), departures="known", watch=None,
               generic=None) -> list:
    """The real builder over made-up rows. `pocs` rows are dicts keyed by the
    sheet's roles (name, company, meeting_date, paper_links, ...)."""
    return feeds.build_names(
        [{"company": c} for c in pipeline], list(pocs),
        departures=Departures(gone) if departures == "known" else None,
        watch=WATCH if watch is None else watch,
        generic=GENERIC if generic is None else generic, institution_words=INSTITUTION)


BASE_NAMES_ROWS = dict(
    pipeline=["PolyAI", "Sarvam AI", "Google", "Cartesia"],
    pocs=[{"company": "PolyAI", "name": "Daisy Leigh", "meeting_date": "06-Oct-2026"},
          {"company": "Google", "name": "Jo Googler"},
          {"company": "Middlesex University London", "name": "David Gamez",
           "meeting_date": "01-Oct-2026"}])


def base_names() -> list:
    return names_from(**BASE_NAMES_ROWS)


# -- one bot ------------------------------------------------------------------


class Bot:
    """One world with the real `_news_cycle`, a stand-in scorer and a name
    list. Use as a context manager; `net` is opened separately so a scenario
    can change what the feeds say between cycles."""

    def __init__(self, *, names=None, scores=None, pins=None, pretend=None,
                 test_mode=False, rules=("R1",)):
        self.names = names
        self.scorer = Scorer(scores)
        self.world = rw.World(test_mode=test_mode, pins=dict(PINS, **(pins or {})),
                              pretend=pretend, rules=set(rules))

    def __enter__(self):
        w = self.w = self.world.__enter__()
        w.bot.__dict__.pop("_news_cycle", None)      # the real cycle
        w.bot.__dict__.pop("_news_tools", None)      # the real todays_news
        w.llm.score_news = self.scorer
        self.rebuilds = 0
        outer = self

        async def read_names():
            outer.rebuilds += 1
            return (list(outer.names), True) if outer.names is not None else (None, False)

        self._real_reader = w.bot._read_news_names
        w.bot._read_news_names = read_names
        self._digest = config.digest_enabled
        config.digest_enabled = lambda: True
        return self

    def __exit__(self, *exc):
        ALL_POSTED.extend(m.content for m in self.w.posted)
        config.digest_enabled = self._digest
        return self.world.__exit__(*exc)

    # ---- the store ----
    @property
    def db(self):
        return self.w.bot.db

    def rows(self) -> list:
        with self.db.conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM news_feed_items ORDER BY rowid").fetchall()]

    def row(self, title: str) -> dict:
        return next((r for r in self.rows() if r["title"] == title), {})

    def recorded(self) -> dict:
        with self.db.conn() as c:
            return {r[0]: r[1] for r in c.execute(
                "SELECT title, kind FROM news_stories ORDER BY rowid").fetchall()}

    def cycles(self, day: date) -> list:
        return self.db.news_cycles_on(dl.iso(day))

    def seed(self, stories: list) -> None:
        """Store made-up rows, scored where `imp` is given. Each is {title,
        when, imp?, topic?, flag?, names?, source?, direct?, url?, rank?,
        seen?}."""
        items = []
        for s in stories:
            url = s.get("url") or link(s["title"], direct=s.get("direct", ""))
            flag = 1 if s.get("flag") else 0
            items.append({
                "url": url, "url_key": news.url_key(url),
                "headline_key": news.headline_key(s["title"]), "title": s["title"],
                "summary": s.get("summary", ""), "source": s.get("source", "Reuters"),
                "published_at": feeds.utc_iso(s["when"]), "topic_hint": "",
                "seen_at": feeds.utc_iso(s.get("seen") or s["when"]),
                "kind": "poc" if flag else "industry", "sheet_ref": "",
                "names": s.get("names", "Acme" if flag else ""), "flag": flag,
                "source_rank": s.get("rank", 3)})
        self.db.news_feed_add(items)
        scored = [{"url_key": i["url_key"], "importance": s["imp"],
                   "topic": s.get("topic", "OTHER"), "what": "it happened"}
                  for i, s in zip(items, stories) if s.get("imp")]
        if scored:
            self.db.news_feed_set_scores(scored, scored_at=items[0]["seen_at"])

    # ---- the cycle and the daily post ----
    async def cycle(self, when: datetime, *, channel="chan", **kw):
        return await self.w.bot._news_cycle(
            now=when, channel=self.w.chan if channel == "chan" else channel, **kw)

    def new_posts(self, n: int) -> list:
        return [m.content for m in self.w.posted[n:]]

    async def daily(self, day: date) -> list:
        """Plan, research and send R1's post for `day`, exactly as the live
        sweep does. The raw text of every Discord message it sent ([] when
        the day had nothing to send)."""
        self.w.pretend_day(day, 13, 55)
        n = self.w.n_posted
        planned = await self.w.bot._plan_drip(today=day, already=[])
        msg = next((m for m in (planned or {}).get("messages") or []
                    if m.get("type") == nextaction.R_AI_NEWS), None)
        if msg is None:
            return []
        await self.w.bot._research_message(msg, today=day)
        await self.w.bot._send_drip_message(self.w.chan, msg, marker=dl.iso(day),
                                            channel_id=self.w.chan.id)
        return self.new_posts(n)


def story_lines(text: str) -> list:
    return [ln for ln in str(text or "").split("\n") if ln.startswith("- ")]


def titles(text: str) -> list:
    return [re.match(r"^- (.*) \(\[[^\]]+\]\(<[^>]+>\)\)$", ln).group(1)
            for ln in story_lines(text)]


def g(title: str, when: datetime, source: str = "Reuters", summary: str = "") -> dict:
    return {"title": title, "when": when, "source": source, "summary": summary}


# =============================================================================
# THE NAME LIST — T1 to T11
# =============================================================================


async def names(check) -> None:
    print("\nTHE NAME LIST (T1-T11)")
    saved = (config.NEWS_NAME_BATCH_SIZE, config.NEWS_TOPICS)
    config.NEWS_NAME_BATCH_SIZE, config.NEWS_TOPICS = 15, []
    try:
        many = names_from([f"Company {i:03d}" for i in range(300)], watch=[])
        jobs = feeds.plan(many, big=False, hours=2)
        seen = Counter(e["name"] for j in jobs for e in j["entries"])
        check("T1 300 companies, batch 15 -> 20 Google searches, every name in exactly "
              "one batch",
              (len(jobs), {j["lane"] for j in jobs}, len(seen), set(seen.values())),
              (20, {"names"}, 300, {1}))
    finally:
        config.NEWS_NAME_BATCH_SIZE, config.NEWS_TOPICS = saved

    # T2-T4 and T8 go through the bot's own reader: the two tabs and the
    # departures list are stand-ins, everything else is real.
    import gtm_sheet
    import mapping_sheet

    pipeline_rows = [{"company": "PolyAI", "_row": 2}, {"company": "Cartesia", "_row": 3}]
    poc_rows = [
        # an INACTIVE row: no first contact, no connection, and marked lost
        {"company": "Cartesia", "name": "Ina Ctive", "next_steps": "Closed - Lost",
         "first_contact_date": "", "li_connected_date": "", "_row": 2},
        {"company": "PolyAI", "name": "Daisy Leigh", "meeting_date": "06-Oct-2026", "_row": 3},
        {"company": "PolyAI", "name": "Left Person", "_row": 4}]

    @contextlib.contextmanager
    def sheets(*, readable=True, departures=Departures(["Left Person"])):
        S, M = gtm_sheet.SHEETS, mapping_sheet.MAPPING
        was = {k: S.__dict__.get(k, rw._ABSENT) for k in ("tab", "cadence_tab")}
        was_dep = M.__dict__.get("departures", rw._ABSENT)

        def tab(kind, which=None):
            if not readable:
                raise RuntimeError("the sheet could not be read")
            return SimpleNamespace(rows=pipeline_rows, title="Master Pipeline") \
                if kind == gtm_sheet.RESEARCHER_LINES else None

        def cadence_tab(*a, **k):
            if not readable:
                raise RuntimeError("the sheet could not be read")
            return SimpleNamespace(rows=poc_rows, title="Outreach PoCs"), "fixture"

        def deps():
            if departures is None:
                raise RuntimeError("the mapping sheet is not shared")
            return departures

        S.tab, S.cadence_tab, M.departures = tab, cadence_tab, deps
        try:
            yield
        finally:
            for k, v in was.items():
                S.__dict__.pop(k, None) if v is rw._ABSENT else setattr(S, k, v)
            M.__dict__.pop("departures", None) if was_dep is rw._ABSENT \
                else setattr(M, "departures", was_dep)

    with Bot() as b:
        bot = b.w.bot
        split_calls = []
        bot._split_active = lambda rows, why: (split_calls.append(why), (list(rows), []))[1]
        with sheets():
            got, complete = await b._real_reader()
        by = {e["name"]: e for e in got}
        check("T2 a person on an inactive row -> the person and the company are on the list",
              (by.get("Ina Ctive", {}).get("kind"), by.get("Cartesia", {}).get("kind"),
               complete), ("person", "company", True))
        check("T2 ...and the 'active row' test is never asked for the name list",
              split_calls, [])
        check("T3 a person on the departures list -> not listed; the company is",
              ("Left Person" in by, by["PolyAI"]["kind"]), (False, "company"))
        check("edge 13 ...and is neither matched nor searched",
              (feeds.NameIndex(got).tag({"title": "PolyAI hires Left Person",
                                         "summary": ""})["names"],
               any("Left Person" in j["query"] for j in feeds.plan(got, big=True, hours=24))),
              ("PolyAI", False))
        with sheets(departures=None), Logs() as logs:
            got, _ = await b._real_reader()
        check("T4 departures list unreadable -> companies listed, no person, warning logged",
              (sorted({e["kind"] for e in got}), sum(1 for e in got if e["kind"] == "company"),
               logs.has("departures list is not loaded", "NO PERSON", level=logging.WARNING)),
              (["company", "watch"], 2, True))
        check("edge 14 ...so no person is on any item and there is no warm-person search",
              (feeds.NameIndex(got).tag({"title": "PolyAI names Daisy Leigh as CRO",
                                         "summary": ""})["names"],
               [j["lane"] for j in feeds.plan(got, big=True, hours=24) if j["lane"] == "warm"]),
              ("PolyAI", []))

        # T8: yesterday's list is stored; today the sheets cannot be read.
        with sheets():
            yesterday, _ = await b._real_reader()
        b.db.news_names_replace(yesterday, built_on="2026-10-12")
        bot._read_news_names = b._real_reader
        with sheets(readable=False), Logs() as logs:
            result = await bot._rebuild_news_names(today=TUE)
        check("T8 sheet read fails at 14:00 with yesterday's list stored -> yesterday's "
              "list used, warning",
              (result, b.db.news_names_built_on(), len(b.db.news_names_all()),
               logs.has("keeping the list built on 2026-10-12", level=logging.WARNING)),
              (None, "2026-10-12", len(yesterday), True))
        with sheets():
            rebuilt = await bot._rebuild_news_names(today=TUE)
        check("edge 17 a readable sheet replaces the list at the next rebuild",
              ((rebuilt or {}).get("total"), b.db.news_names_built_on()),
              (len(yesterday), "2026-10-13"))
    check("edge 9 with no list ever built the pull reads the topics only",
          {j["lane"] for j in feeds.plan([], big=True, hours=24)}, {"topics"})

    two = names_from(["CoRover.ai", "CoRover", "PolyAI"], watch=[])
    corover = [e for e in two if e["name"].startswith("CoRover")]
    index = feeds.NameIndex(two)
    check("T5 'CoRover.ai' and 'CoRover' on two rows -> one key; an item naming either "
          "carries one name",
          (len(corover), corover[0]["aliases"],
           index.tag({"title": "CoRover launches a rail bot", "summary": ""})["names"],
           index.tag({"title": "CoRover.ai launches a rail bot", "summary": ""})["names"]),
          (1, ["CoRover"], "CoRover.ai", "CoRover.ai"))
    check("edge 12 ...and both spellings are in the one batch",
          feeds.batch_query(feeds.batch_names(two)), '"CoRover.ai" OR "CoRover" OR "PolyAI"')

    base = base_names()
    by = {e["name"]: e for e in base}
    jobs = feeds.plan(base, big=False, hours=2)
    index = feeds.NameIndex(base)
    in_batch = [e["name"] for j in jobs if j["lane"] == "names" for e in j["entries"]]
    check("T6 'Google' on NEWS_GENERIC_NAMES -> not in a batch; own search; title-only "
          "match",
          ("Google" in in_batch, [j["query"] for j in jobs if j["lane"] == "generic"],
           index.tag({"title": "Chip stocks fall", "summary": "Google slid"})["names"],
           index.tag({"title": "Google ships Gemini 4", "summary": ""})["names"]),
          (False, ['"Google"'], "", "Google"))
    scale = index.tag({"title": "Scale AI lays off staff", "summary": ""})
    check("T7 'Scale AI' on NEWS_WATCH_NAMES, not on a sheet -> listed, kind watch, "
          "flagged when named",
          (by["Scale AI"]["kind"], "Scale AI" in in_batch, scale["flag"], scale["names"]),
          ("watch", True, 1, "Scale AI"))
    inst = names_from(pocs=[{"company": "AI Security Institute (UK Govt.)", "name": "Kim Roe"},
                            {"company": "Middlesex University London", "name": "David Gamez"},
                            {"company": "PolyAI", "name": "Pat Paper",
                             "paper_links": "https://arxiv.org/abs/2610.1"}], watch=[])
    by = {e["name"]: e for e in inst}
    check("T9 'AI Security Institute (UK Govt.)' -> kind company, not researcher",
          (by["AI Security Institute (UK Govt.)"]["kind"], by["Kim Roe"]["kind"],
           by["Kim Roe"]["researcher"]), ("company", "person", False))
    searched = " ".join(j["query"] for j in feeds.plan(inst, big=True, hours=24)
                        if j["lane"] in ("names", "generic"))
    check("T10 'Middlesex University London' -> researcher; the institution is NOT "
          "searched as a company",
          (by["David Gamez"]["kind"], by["David Gamez"]["institution"],
           "Middlesex University London" in by, "Middlesex" in searched),
          ("researcher", "Middlesex University London", False, False))
    check("T11 Research Paper Link filled at a company -> both lanes",
          (by["Pat Paper"]["kind"], by["Pat Paper"]["researcher"], by["PolyAI"]["kind"],
           "Pat Paper" in [e["name"] for e in feeds.researchers(inst)]),
          ("person", True, "company", True))

    odd = names_from(["AI", "Go", 'Acme "Labs" Inc.', "Scale AI"],
                     pocs=[{"company": "PolyAI", "name": "Daisy Leigh"},
                           {"company": "PolyAI", "name": "Daisy Leigh"}], watch=["scale ai"])
    by = {e["name"]: e for e in odd}
    check("edge 10 a name shorter than 3 characters is left out",
          ("AI" in by, "Go" in by), (False, False))
    check("edge 15 a person who appears twice is one entry",
          sum(1 for e in odd if e["name"] == "Daisy Leigh"), 1)
    check("edge 16 a watch name that is also on a sheet is one entry, in the sheet's "
          "spelling", [(e["name"], e["kind"]) for e in odd if feeds.name_key(e["name"])
                       == "scale ai"], [("Scale AI", "company")])
    check("edge 18 quotes are removed before the search; the trailing dot stays in the "
          "match",
          ('"Acme Labs Inc."' in feeds.batch_query(feeds.batch_names(odd)),
           feeds.NameIndex(odd).tag({"title": "Acme Labs Inc. raises a round",
                                     "summary": ""})["names"]),
          (True, "Acme Labs Inc."))


# =============================================================================
# COLLECTION — T12 to T20, T72
# =============================================================================


async def collection(check) -> None:
    print("\nCOLLECTION (T12-T20, T72)")
    with Bot(names=base_names()) as b, Net() as net:
        check("T19 13:59 on a weekday, 02:00 on a Sunday -> no cycle",
              (await b.cycle(ist(TUE, 13, 59)), await b.cycle(ist(SUN, 2, 0)), net.served),
              (None, None, []))
        first = await b.cycle(ist(TUE, 14, 0))
        check("T12 14:00 cycle, no cycle yet today -> queries carry when:1d",
              (first["big"], first["hours"], net.whens(), first["status"],
               b.cycles(TUE)[0]["big"]), (True, 24, ["when:1d"], "done", 1))
        check("edge 1 ...and the big pull rebuilt the name list",
              (b.rebuilds, len(b.db.news_names_all()) > 0), (1, True))
        check("edge 4 no retry inside the hour: the 14:00 slot runs once",
              await b.cycle(ist(TUE, 14, 20)), None)
        net.google.clear()
        second = await b.cycle(ist(TUE, 15, 0))
        check("T13 15:00 after 14:00 -> when:2h; the window reaches back to 14:00",
              (second["big"], second["hours"], net.whens()), (False, 2, ["when:2h"]))
        check("edge 17 a name added to the sheet at 15:00 is watched from the next 14:00 "
              "rebuild: a small pull does not rebuild", b.rebuilds, 1)
        check("the once-a-day lanes (warm people, researchers) are not in a small pull",
              any('"Daisy Leigh"' in query_of(u) or "David Gamez" in query_of(u)
                  for _t, u in net.google), False)
        net.google.clear()
        third = await b.cycle(ist(TUE, 18, 5))
        check("T14 no cycle 15:00-18:00 -> the 18:00 window reaches back to 15:00",
              (third["slot"], third["hours"], net.whens()), ("18:00", 4, ["when:4h"]))
        net.google.clear()
        sunday = await b.cycle(ist(SUN, 15, 0))      # a different date
        check("T20 15:00 on a Sunday -> a cycle runs",
              ((sunday or {}).get("slot"), (sunday or {}).get("status")), ("15:00", "done"))
    check("edge 2 the window is never less than 2 hours and never more than 24",
          (news.window_hours(utc(TUE, 14), utc(TUE, 14, 30)),
           news.window_hours(utc(MON, 9), utc(TUE, 14))), (2, 24))
    check("edge 7 the 23:00 cycle is the last of ten; the next is the 14:00 big pull",
          (len(news.pull_slots()), news.pull_slots()[-1], news.cycle_slot(ist(TUE, 23, 40)),
           news.cycle_slot(ist(TUE, 0, 30))), (10, "23:00", "23:00", None))

    with Bot(names=base_names()) as b, Net() as net:
        late = await b.cycle(ist(TUE, 16, 30))
        check("T15 restart at 16:30, no cycle today -> big pull (when:1d)",
              (late["slot"], late["big"], net.whens()), ("16:00", True, ["when:1d"]))

    with Bot(names=base_names(), pins={"NEWS_PULL_FIRST": "13:00"}) as b, Net() as net:
        early = await b.cycle(ist(TUE, 13, 0))
        check("T72 NEWS_PULL_FIRST=13:00 -> the big pull runs at 13:00",
              ((early or {}).get("slot"), (early or {}).get("big")), ("13:00", True))

    now = ist(TUE, 14, 0)
    story = "PolyAI ships an open eval suite"
    routes = [('"PolyAI"', [g(story, now - timedelta(minutes=30))]),
              ("outlet", [{"title": story, "when": now - timedelta(minutes=50),
                           "direct": "techcrunch.com"},
                          {"title": "Chipmaker posts record quarter",
                           "when": now - timedelta(hours=6), "direct": "techcrunch.com"}])]
    with Bot(names=base_names()) as b, Net(routes) as net:
        await b.cycle(now)
        mine = [r for r in b.rows() if r["title"] == story]
        check("T16 Google first, outlet second carry one story -> one row, Google wins, "
              "source_rank = Google's",
              (len(mine), mine[0]["url"].startswith(G), mine[0]["source"],
               mine[0]["source_rank"]), (1, True, "Reuters", feeds.RANK_NAMES))
        order = [("news.google.com" in u) for u in net.served]
        check("Google News is read first, the outlets' feeds after it",
              order == sorted(order, reverse=True) and not order[-1], True)
        check("edge 8 an outlet that publishes late is not missed: the scorer is shown "
              "what was SEEN this cycle, not what was published in it",
              (b.row("Chipmaker posts record quarter")["published_at"]
               < feeds.utc_iso(now - timedelta(hours=5)),
               b.scorer.saw("Chipmaker posts record quarter")), (True, True))

    # T17: five name batches (61 companies), then the topics. The 4th Google
    # request is answered 429.
    crowd = [f"Company {i:03d}" for i in range(61)]

    def limited(url, n):
        if n == 4:
            raise RateLimited()

    routes = [(f'"Company {i:03d}"', [g(f"Company {i:03d} opens a {city} office", now)])
              for i, city in zip((0, 15, 30, 45, 60), CITIES)]
    with Bot(names=names_from(crowd, watch=[], generic=[])) as b, \
            Net(routes, fail=limited) as net, Logs() as logs:
        cut = await b.cycle(now)
        check("T17 429 on the 4th search -> stop; 3 searches' items stored; partial; no "
              "retry",
              (len(net.served), sorted(r["title"] for r in b.rows()), cut["status"],
               cut["pull"]["partial"], b.cycles(TUE)[0]["status"]),
              (4, ["Company 000 opens a Delhi office", "Company 015 opens a Mumbai office",
                   "Company 030 opens a Pune office"], "partial", True, "partial"))
        check("T17 ...and nothing is asked again inside the hour",
              (await b.cycle(now + timedelta(minutes=30)), len(net.served)), (None, 4))
        check("edge 4 the next cycle's window reaches back over the partial one: with no "
              "completed cycle today it is the big pull again",
              ((await b.cycle(ist(TUE, 15, 0)))["big"],
               logs.has("429", "stops here", level=logging.WARNING)), (True, True))

    with Bot(names=names_from([f"Company {i:03d}" for i in range(300)], watch=[],
                              generic=[]), pins={"NEWS_TOPICS": []}) as b, Net() as net:
        await b.cycle(now)
        stamps = [t for t, _u in net.google]
        check("T18 20 searches in one cycle -> at least 1.5 s between two consecutive "
              "Google requests",
              (len(stamps), min(y - x for x, y in zip(stamps, stamps[1:])) >= 1.5),
              (20, True))

    def slow(url, n):
        if "outlet.example" in url:
            raise TimeoutError("the feed timed out")

    with Bot(names=base_names()) as b, \
            Net([('"PolyAI"', [g("PolyAI opens a Delhi office", now)])], fail=slow), \
            Logs() as logs:
        got = await b.cycle(now)
        check("edge 5 a feed times out: one log line, the other feeds still land",
              (sum(1 for _l, m in logs.lines if "could not read https://outlet.example" in m),
               len(got["pull"]["errors"]), bool(b.row("PolyAI opens a Delhi office")),
               got["status"]), (1, 1, True, "done"))

    from bot import SalesBot
    tick = inspect.getsource(SalesBot._sweep_once)
    check("edge 3 a cycle and the daily post in one tick: the cycle runs first, then the "
          "drip",
          0 < tick.index("await self._news_cycle()") < tick.index(
              "await self._maybe_send_drip()"), True)


# =============================================================================
# FLAG AND SCORE — T21 to T28
# =============================================================================


async def flags(check) -> None:
    print("\nFLAG AND SCORE (T21-T28)")
    now = ist(TUE, 14, 0)
    base = base_names()
    index = feeds.NameIndex(base)

    def tag(title, summary=""):
        return index.tag({"title": title, "summary": summary, "kind": "industry"})

    with Bot(names=base) as b, \
            Net([('"PolyAI"', [g("PolyAI raises $50M Series C", now)])]):
        b.scorer.down = True                 # the model is unreachable all cycle
        await b.cycle(now)
        row = b.row("PolyAI raises $50M Series C")
        check("T21 title names 'PolyAI' -> flag=1, names=PolyAI, no model call for the flag",
              (row["flag"], row["names"], row["importance"]), (1, "PolyAI", 0))
    both = tag("PolyAI names Daisy Leigh as chief revenue officer")
    check("T22 names 'PolyAI' and 'Daisy Leigh' -> names holds both",
          (both["names"], both["flag"]), ("PolyAI, Daisy Leigh", 1))
    check("T23 summary (not title) names 'Google' -> not flagged",
          (tag("Chip stocks fall", "Google and others slid")["flag"],
           tag("Chip stocks fall", "Google and others slid")["names"]), (0, ""))
    check("T24 title names 'Google' and our person there -> flagged",
          (tag("Google's Jo Googler on the future of evals")["flag"],
           tag("Google's Jo Googler on the future of evals")["names"]),
          (1, "Google, Jo Googler"))
    pulse = feeds.NameIndex(names_from(["Pulse"], watch=[]))
    check("T25 'Pulse' inside 'Pulsed' -> not flagged",
          (pulse.tag({"title": "Markets pulsed higher on AI hopes", "summary": ""})["flag"],
           pulse.tag({"title": "Pulse raises a seed round", "summary": ""})["flag"]), (0, 1))
    two = tag("Sarvam AI and PolyAI sign a speech data deal")
    check("edge 19 an item naming two of our companies holds both names, on one row",
          (sorted(two["names"].split(", ")), two["flag"]), (["PolyAI", "Sarvam AI"], 1))
    check("T28 the scoring prompt contains the breaking table and the three Membrane lines",
          (all(f"| {x} | {y} |" in news.score_prompt([], [], today=TUE, mode="cycle")
               for x, y in news.BREAKING_TABLE),
           [s in news.score_prompt([], [], today=TUE, mode="cycle") for s in (
               "student-panel human data in Bangalore",
               "evals and preference data, Hinglish and Indic speech, voice AI data",
               "RLHF and red-teaming", "Everything not in the table is a 4 at most.")]),
          (True, [True] * 4))
    check("T28 ...word for word: the first and the last row of the table",
          ("| A funding round, an acquisition, or a merger. | A new frontier model from a "
           "top lab. |" in news.breaking_table(),
           "| One of our researchers publishes a paper in our field. | — |"
           in news.breaking_table(), len(news.BREAKING_TABLE)), (True, True, 8))

    # T26 and edge 21: a flagged item the model leaves out, and a generic one.
    routes = [('"PolyAI"', [g("PolyAI sponsors a university hackathon", now)]),
              ('"Google"', [g("Google opens a new campus cafe", now)])]
    with Bot(names=base, scores={}) as b, Net(routes):
        await b.cycle(now)
        left = b.row("PolyAI sponsors a university hackathon")
        generic = b.row("Google opens a new campus cafe")
        post = await b.daily(TUE)
        check("T26 flagged, no SCORE line -> importance 1, still a daily candidate",
              (left["flag"], left["importance"], bool(post)
               and post[0].split("\n")[2:4]),
              (1, 1, [news.LABEL_OURS,
                      "- PolyAI sponsors a university hackathon ([Reuters](<"
                      + link("PolyAI sponsors a university hackathon") + ">))"]))
        check("edge 21 a generic name's item the model leaves out is the exception: "
              "named, not flagged, not posted",
              (generic["names"], generic["flag"], generic["importance"],
               "campus cafe" in "".join(post)), ("Google", 0, 1, False))

    with Bot(names=base, scores={"Google ships a frontier speech model": 4}) as b, \
            Net([('"Google"', [g("Google ships a frontier speech model", now)])]):
        got = await b.cycle(now)
        row = b.row("Google ships a frontier speech model")
        check("edge 11 a generic name's item is ours once the model gives it a 4, and "
              "then it is breaking",
              (row["flag"], row["importance"], got["posted"]), (1, 4, 1))

    # T27: the scorer is down for a cycle, then back.
    title = "PolyAI acquired by a telecom group"
    with Bot(names=base, scores={title: 5}) as b, Net([('"PolyAI"', [g(title, now)])]):
        b.scorer.down = True
        down = await b.cycle(now)
        check("T27 scorer returns None -> unscored; no breaking post",
              (b.row(title)["importance"], down["posted"], down["status"], b.w.n_posted,
               b.recorded()), (0, 0, "unscored", 0, {}))
        b.scorer.down = False
        up = await b.cycle(now + timedelta(hours=1))
        check("T27 ...the next cycle scores it, and it is still new enough to go out",
              (b.row(title)["importance"], up["posted"], b.recorded()),
              (5, 1, {title: "breaking"}))
        check("T27 ...and the day's reading is not repeated for it: the next cycle is a "
              "small pull whose window reaches back", (up["big"], up["hours"]), (False, 24))

    # edge 22: the scorer cannot run at the daily post either.
    with Bot(names=base) as b:
        b.scorer.down = True
        b.seed([{"title": "PolyAI opens a Mumbai office", "when": utc(TUE, 9), "flag": 1,
                 "names": "PolyAI"},
                {"title": "Lab publishes a tooling guide", "when": utc(TUE, 9, 30)},
                {"title": "Regulator opens an evals inquiry", "when": utc(TUE, 8), "imp": 4}])
        post = await b.daily(TUE)
        check("edge 22 the scorer cannot run at the daily post: an unscored story about "
              "us goes in at 3, a scored one stays, an unscored industry item waits",
              titles(post[0] if post else ""),
              ["PolyAI opens a Mumbai office", "Regulator opens an evals inquiry"])
        check("edge 22 ...and the item that waited is still unscored",
              b.row("Lab publishes a tooling guide")["importance"], 0)

    # edge 23: two outlets, one story; only the second copy names us.
    story = "Voice lab ships a Hindi speech model"
    routes = [("evals", [g(story, now)]),
              ("outlet", [{"title": story, "when": now, "direct": "inc42.com",
                           "summary": "Built with PolyAI's speech stack."}])]
    with Bot(names=base) as b, Net(routes):
        await b.cycle(now)
        mine = [r for r in b.rows() if r["title"] == story]
        check("edge 23 two outlets carry one story, one names us: one row, flagged",
              (len(mine), mine[0]["flag"], mine[0]["names"], mine[0]["url"].startswith(G)),
              (1, 1, "PolyAI", True))

    # edge 20: our person, now at a different company — the warm-person search.
    move = "Daisy Leigh joins Deepgram as chief revenue officer"
    with Bot(names=base, scores={move: 4}) as b, \
            Net([('("Daisy Leigh" "PolyAI")', [g(move, now)])]) as net:
        got = await b.cycle(now)
        row = b.row(move)
        check("edge 20 an item names our person at a different company: the warm-person "
              "search finds it, it is flagged, and a job change is breaking",
              (any('("Daisy Leigh" "PolyAI")' in query_of(u) for _t, u in net.google),
               row["flag"], row["names"], got["posted"]), (True, 1, "Daisy Leigh", 1))


# =============================================================================
# BREAKING — T29 to T41, T79, T80
# =============================================================================


async def breaking(check) -> None:
    print("\nBREAKING (T29-T41, T79, T80)")
    now = ist(TUE, 15, 0)
    base = base_names()
    fresh = now - timedelta(minutes=10)

    routes = [('"PolyAI"', [g("PolyAI raises $200M led by a sovereign fund", fresh),
                            g("PolyAI opens a Lisbon office", fresh)]),
              ("evals", [g(GPT6, fresh, "The Verge"),
                         g("Startup releases an evals toolkit", fresh)])]
    scores = {"PolyAI raises $200M led by a sovereign fund": 4,
              "PolyAI opens a Lisbon office": 3, GPT6: 5,
              "Startup releases an evals toolkit": (4, "evals")}
    with Bot(names=base, scores=scores) as b, Net(routes):
        got = await b.cycle(now)
        sent = b.new_posts(0)
        check("T29 flagged, score 4, seen 10 minutes ago -> one breaking post in the same "
              "cycle",
              (len(sent), "PolyAI raises $200M led by a sovereign fund" in titles(sent[0]),
               got["posted"]), (1, True, 2))
        check("T31 industry 5 -> breaking", GPT6 in titles(sent[0]), True)
        check("T30 flagged, score 3 -> no breaking", "PolyAI opens a Lisbon office"
              in titles(sent[0]), False)
        check("T32 industry 4 -> no breaking", "Startup releases an evals toolkit"
              in titles(sent[0]), False)
        check("T39 breaking sent -> no drip_sends row; drip.counted_today unchanged",
              (b.w.drip_rows(), drip.counted_today(b.db.drip_sent_today(dl.iso(TUE)))),
              ([], 0))
        check("edge 31 ...it went straight to the channel, recorded as breaking",
              sorted(set(b.recorded().values())), ["breaking"])
        post = await b.daily(TUE)
        check("T36 breaking at 15:00, daily post later -> not repeated",
              (sorted(set(titles(post[0])) & set(titles(sent[0]))), len(post)), ([], 1))
        check("T30/T32 ...and the two that were not breaking are the daily post: ours "
              "first, then the industry",
              (titles(post[0]), post[0].split("\n")[2], post[0].split("\n")[4]),
              (["PolyAI opens a Lisbon office", "Startup releases an evals toolkit"],
               news.LABEL_OURS, news.LABEL_INDUSTRY))
        check("edge 26 the daily post is one counted drip message of its own",
              [r[2] for r in b.w.drip_rows()], [nextaction.R_AI_NEWS])

    with Bot(names=base, scores={GPT6: 5}) as b, \
            Net([("evals", [g(GPT6, fresh, "The Verge")])]):
        await b.cycle(now)
        check("T79 one breaking story -> equals template E",
              b.new_posts(0), [template(TEMPLATE_E, [GPT6])])
        check("T35 the breaking body is template E exactly: heading with the tags, no "
              "date, no opener, no closer",
              (b.new_posts(0)[0].split("\n")[0], len(b.new_posts(0)[0].split("\n"))),
              (f"**Breaking news** {TAGS}", 2))

    routes = [('"Sarvam AI"', [g(JIO, fresh, "Economic Times")]),
              ('"Scale AI"', [g(LAYOFFS, fresh)]),
              ("evals", [g(GPT6, fresh, "The Verge")])]
    with Bot(names=base, scores={JIO: 5, GPT6: 5, LAYOFFS: 4}) as b, Net(routes):
        await b.cycle(now)
        check("T80 three breaking stories -> equals template F",
              b.new_posts(0), [template(TEMPLATE_F, [JIO, GPT6, LAYOFFS])])
        check("T35 ...ordered by score, then source rank: ours at 5, the industry 5, "
              "then the competitor at 4",
              titles(b.new_posts(0)[0]), [JIO, GPT6, LAYOFFS])

    # T33: five breaking stories in one cycle.
    five = ["PolyAI acquires a speech startup", "PolyAI names a new chief executive",
            "PolyAI raises a mega round", "PolyAI lays off a tenth of its staff",
            "PolyAI launches an evals product"]
    routes = [('"PolyAI"', [g(t, fresh - timedelta(minutes=n)) for n, t in enumerate(five)])]
    with Bot(names=base, scores={t: (5 if n < 2 else 4) for n, t in enumerate(five)}) as b, \
            Net(routes), Logs() as logs:
        got = await b.cycle(now)
        first = b.new_posts(0)
        check("T33 5 breaking in one cycle -> one post, 3 lines; 2 unsent",
              (len(first), titles(first[0]), sorted(b.recorded()), len(got["held"])),
              (1, five[:3], five[:3], 2))
        n = b.w.n_posted
        await b.cycle(now + timedelta(hours=1))
        check("T33 ...and the next cycle sends them",
              ([titles(p) for p in b.new_posts(n)], len(b.recorded())), ([five[3:]], 5))
        check("edge 25 the flood guard says what it held",
              logs.has("NEWS_BREAKING_MAX_PER_CYCLE", "next cycle"), True)

    # T34: four cycles, one breaking story each.
    hourly = ["PolyAI signs a telecom partner", "PolyAI appoints a head of AI",
              "PolyAI buys an annotation firm", "PolyAI closes its Berlin office"]
    with Bot(names=base, scores={t: 4 for t in hourly}) as b:
        for h, t in zip((14, 15, 16, 17), hourly):
            with Net([('"PolyAI"', [g(t, ist(TUE, h) - timedelta(minutes=5))])]):
                await b.cycle(ist(TUE, h))
        check("T34 4 cycles x 1 breaking -> 4 posts in one day (no daily limit)",
              ([titles(p) for p in b.new_posts(0)], len(b.cycles(TUE))),
              ([[t] for t in hourly], 4))

    # T37: a second outlet's copy of a story that already went out.
    with Bot(names=base, scores={JIO: 5}) as b:
        with Net([('"Sarvam AI"', [g(JIO, fresh, "Economic Times")])]):
            await b.cycle(now)
        other = {"title": JIO, "when": now + timedelta(minutes=30), "direct": "livemint.com"}
        with Net([("outlet", [other])]):
            again = await b.cycle(now + timedelta(hours=1))
        check("T37 a second outlet's copy of a sent breaking story -> not sent",
              (len(b.new_posts(0)), again["posted"], len(b.recorded())), (1, 0, 1))
        check("edge 27 ...its headline key is the one already posted",
              bool(b.db.news_story_seen("livemint.com/x", news.headline_key(JIO),
                                        since_iso="2026-09-20")), True)

    # T38: a flagged story first seen four hours ago, scored 5 only now.
    old = "PolyAI acquired by a cloud provider"
    with Bot(names=base, scores={old: 5}) as b, Net():
        b.seed([{"title": old, "when": now - timedelta(hours=4), "flag": 1,
                 "names": "PolyAI", "rank": 1},
                {"title": "Regulator publishes an evals rulebook",
                 "when": now - timedelta(hours=2), "imp": 4, "topic": "evals"}])
        got = await b.cycle(ist(TUE, 15, 0))
        post = await b.daily(TUE)
        check("T38 flagged 5, first seen 4 h ago -> not breaking; daily post first",
              (b.row(old)["importance"], got["posted"], titles(post[0])[0],
               post[0].split("\n")[2]), (5, 0, old, news.LABEL_OURS))
        check("edge 24 ...and the log says it is too old to break",
              any("NEWS_BREAKING_MAX_AGE_HOURS" in s for s in got["skipped"]), True)

    # T40: a Saturday. With the shipped NEWS_PULL_FIRST=14:00 nothing is read at
    # 11:00 (T19); the hour is pinned earlier here to stand at the ticket's 11:00.
    sat = "PolyAI weekend acquisition announced"
    with Bot(names=base, scores={sat: 5}, pins={"NEWS_PULL_FIRST": "11:00"}) as b, \
            Net([('"PolyAI"', [g(sat, ist(SAT, 10, 50))])]):
        got = await b.cycle(ist(SAT, 11, 0))
        check("T40 breaking at 11:00 on a Saturday -> posted",
              (got["posted"], titles(b.new_posts(0)[0])), (1, [sat]))
        import rules
        check("edge 30 a weekend: cycles and breaking run, the daily post does not",
              (rules.by_id("R1").runs_on(SAT), rules.by_id("R1").runs_on(MON)),
              (False, True))
    with Bot(names=base) as b:
        b.seed([{"title": "PolyAI weekend product update", "when": utc(SAT, 16), "flag": 1,
                 "names": "PolyAI", "imp": 2},
                {"title": "Weekend lab ships a benchmark", "when": utc(SAT, 17), "imp": 4}])
        post = await b.daily(MON)
        check("edge 30 ...and the weekend's stories are Monday's candidates",
              titles(post[0]), ["PolyAI weekend product update",
                                "Weekend lab ships a benchmark"])

    # T41 and edge 28: the kill switch.
    kill = "PolyAI raises a Series D"
    with Bot(names=base, scores={kill: 4}) as b, Net([('"PolyAI"', [g(kill, fresh)])]):
        config.digest_enabled = lambda: False
        off = await b.cycle(now)
        check("T41 kill switch off -> stored; nothing posted; nothing recorded",
              (b.row(kill)["flag"], b.row(kill)["importance"], off["posted"],
               b.w.n_posted, b.recorded(), off["silent"], off["status"]),
              (1, 4, 0, 0, {}, "the kill switch is off", "done"))
        config.digest_enabled = lambda: True
        back = await b.cycle(now + timedelta(hours=1))
        check("edge 28 when the switch returns, a story still inside "
              "NEWS_BREAKING_MAX_AGE_HOURS goes out",
              (back["posted"], b.recorded()), (1, {kill: "breaking"}))

    # edge 29: no reachable sales channel.
    with Bot(names=base, scores={kill: 4}) as b, Net([('"PolyAI"', [g(kill, fresh)])]), \
            Logs() as logs:
        b.w.bot.get_channel = lambda _cid: None
        nowhere = await b.cycle(now, channel=None)
        check("edge 29 no reachable sales channel: logged, nothing recorded",
              (nowhere["posted"], b.recorded(),
               logs.has("no reachable sales channel", level=logging.WARNING)),
              (0, {}, True))
        check("edge 29 ...and still unsent for the next cycle",
              (await b.cycle(now + timedelta(hours=1)))["posted"], 1)

    # edge 6: a test run holds the live post; the forced cycle is the test's own.
    real = dl.real_now_ist()
    held = "PolyAI raises a bridge round"
    with Bot(names=base, scores={held: 4}, pins={"NEWS_PULL_FIRST": "00:00"}) as b, \
            Net([('"PolyAI"', [g(held, real - timedelta(minutes=10))])]) as net:
        with b.w.bot._hold_live_loop("a test day"):
            live = await b.cycle(real)
            check("edge 6 during a test the live cycle still reads and stores, and the "
                  "live breaking post stands down",
                  (bool(b.row(held)), live["posted"], live["silent"], b.w.n_posted),
                  (True, 0, "a test run holds the live loop", 0))
            asked = len(net.served)
            forced = await b.w.bot._test_news_check(b.w.chan, at=real)
            check("edge 6 'run one news check now' forces one cycle into the tester's "
                  "channel, tagged",
                  ((forced or {}).get("slot", "").startswith("test "),
                   (forced or {}).get("posted"), len(net.served) > asked,
                   b.new_posts(0)[0].split("\n")[0]),
                  (True, 1, True, f"[TEST] **Breaking news** {TAGS}"))
        check("edge 6 ...and the live post stays down for NEWS_HOLD_AFTER_TEST_SECONDS "
              "after the test", b.w.bot._live_loop_held("breaking news"), True)

    carried = {"headline": "x", "url": "https://a.example/x", "url_key": "a.example/x",
               "headline_key": "x", "importance": 5, "flag": 1, "source_rank": 1,
               "seen_at": feeds.utc_iso(now), "published_at": feeds.utc_iso(now),
               "carried_from": "2026-10-12", "carry_count": 1}
    with Bot() as b:
        got = news.choose_breaking([carried], today=TUE, db=b.db, now=now, max_age_hours=3,
                                   ours_min=4, industry_min=5, cap=3)
        check("edge 39 a carried story never becomes breaking by being carried",
              (got["keep"], "carried" in got["skipped"][0]), ([], True))


# =============================================================================
# THE DAILY POST — T42 to T56, T75 to T78, T83
# =============================================================================


def _template_stories() -> list:
    """The five stories of template A, scored so that they come out in its order."""
    return [
        {"title": SARVAM, "when": utc(TUE, 10), "source": "Economic Times", "flag": 1,
         "names": "Sarvam AI", "imp": 3, "rank": 1},
        {"title": GAMEZ, "when": utc(TUE, 9), "source": "The Guardian", "flag": 1,
         "names": "David Gamez", "imp": 2, "rank": 2},
        {"title": STATE_AGS, "when": utc(TUE, 11), "imp": 4, "topic": "AI safety"},
        {"title": UK_AISI, "when": utc(TUE, 8), "direct": "www.gov.uk", "source": "GOV.UK",
         "imp": 4, "topic": "evals", "rank": 4},
        {"title": BRUTAL, "when": utc(TUE, 7), "imp": 3, "topic": "OTHER"}]


async def daily(check) -> None:
    print("\nTHE DAILY POST (T42-T56, T75-T78, T83)")
    five = _template_stories()
    direct = {UK_AISI: "www.gov.uk"}

    with Bot() as b:
        b.seed(five)
        post = await b.daily(TUE)
        want = template(TEMPLATE_A, [SARVAM, GAMEZ, STATE_AGS, UK_AISI, BRUTAL], direct=direct)
        check("T49 2 ours + 3 industry -> template A, labels in that order",
              post, [want])
        check("T75 render with 2 ours + 3 industry -> equals template A character for "
              "character",
              news.daily_message([news.story_from_feed(r) for r in sorted(
                  b.rows(), key=lambda r: [s["title"] for s in five].index(r["title"]))],
                  day=TUE, tags=TAGS), want)
        rows = post[0].split("\n")
        check("T51 the body: '**AI News, <Day d Mon>** <tags>', the opener, ..., the "
              "closer",
              (rows[0], rows[1], rows[-1]),
              (f"**AI News, Tue 13 Oct** {TAGS}", "Hey here's today's round-up:",
               "Let me know if you want me to dig into any of these"))
        check("only the heading is bold, and one blank line stands between two stories",
              (post[0].count("**"), post[0].count("\n\n"), "\n\n\n" in post[0]),
              (2, 3, False))
        check("the five stories are recorded as the daily post",
              sorted(set(b.recorded().values())), ["main"])

    with Bot() as b:
        b.seed(five[:2])
        post = await b.daily(TUE)
        want = template(TEMPLATE_B, [SARVAM, GAMEZ])
        check("T48 2 ours + 0 industry -> template B", post, [want])
        check("T76 render with 2 ours only -> equals template B",
              news.daily_message([news.story_from_feed(r) for r in b.rows()], day=TUE,
                                 tags=TAGS), want)
        check("edge 32 fewer than 5 stories: what there is, no padding",
              len(story_lines(post[0])), 2)
        check("edge 35 only our stories: no 'Elsewhere in AI'",
              news.LABEL_INDUSTRY in post[0], False)

    with Bot() as b:
        b.seed(five[2:])
        post = await b.daily(TUE)
        want = template(TEMPLATE_C, [STATE_AGS, UK_AISI, BRUTAL], direct=direct)
        check("T77 render with 3 industry only -> equals template C",
              (post, news.daily_message(
                  [news.story_from_feed(r) for r in b.rows()], day=TUE, tags=TAGS)),
              ([want], want))
    with Bot() as b:
        b.seed(five[2:] + [{"title": "Lab ships a red-teaming toolkit", "when": utc(TUE, 6),
                            "imp": 4, "topic": "RLHF"}])
        post = await b.daily(TUE)
        rows = post[0].split("\n")
        check("T47 0 ours + 4 industry -> template C: opener, four lines, closer, no label",
              (rows[1], len(story_lines(post[0])), rows[-1],
               news.LABEL_OURS in post[0] or news.LABEL_INDUSTRY in post[0]),
              (news.OPENER, 4, news.CLOSER, False))
        check("edge 34 only industry stories: the line under the opener is a story",
              rows[2].startswith("- "), True)

    with Bot() as b, Logs() as logs:
        post = await b.daily(TUE)
        check("T50 no candidates -> no post; no quiet line (template D)",
              (post, b.w.n_posted), ([], 0))
        check("T78 render with nothing -> no send at all",
              (news.render([], mode=news.MODE_MAIN), news.daily_message([], day=TUE,
                                                                        tags=TAGS)),
              ("", ""))
        check("edge 33 ...the slot is spent without a word, and it does not count",
              (logs.has("nothing to say today"),
               drip.counted_today(b.db.drip_sent_today(dl.iso(TUE)))), (True, 0))

    ours = [{"title": f"PolyAI story {W[n]}", "when": utc(TUE, 10 - n), "flag": 1,
             "names": "PolyAI", "imp": s, "topic": "evals"} for n, s in enumerate((3, 2, 1))]
    industry = [{"title": f"Industry story {W[n]}", "when": utc(TUE, 9, 50 - n), "imp": s,
                 "topic": t} for n, (s, t) in enumerate(
        [(4, "evals"), (4, "RLHF"), (3, "voice agent"), (3, "TTS"), (2, "STT"),
         (2, "AI safety")])]
    with Bot() as b:
        b.seed(ours + industry)
        post = await b.daily(TUE)
        check("T42 3 ours (3,2,1) + 6 industry (4,4,3,3,2,2) -> 5 lines: 3 ours first, "
              "then industry 4,4",
              titles(post[0]), ["PolyAI story alpha", "PolyAI story bravo",
                                "PolyAI story charlie", "Industry story alpha",
                                "Industry story bravo"])
        check("edge 42 the spread limits are for the industry part only: three of ours on "
              "one topic all go",
              post[0].split("\n")[2:3] + [len(story_lines(post[0].split(
                  news.LABEL_INDUSTRY)[0]))], [news.LABEL_OURS, 3])
        check("industry news that did not fit is dropped, never carried",
              [r["title"] for r in b.rows() if r["carried_from"]], [])

    # T43 and T44: Thursday's post leaves two of ours; Friday's takes them first.
    seven = [{"title": f"PolyAI Thursday story {W[n]}", "when": utc(THU, 12 - n), "flag": 1,
              "names": "PolyAI", "imp": 3} for n in range(7)]
    with Bot() as b:
        b.seed(seven)
        thu = await b.daily(THU)
        carried = sorted(r["title"] for r in b.rows() if r["carried_from"])
        check("T43 7 ours, 0 industry -> 5 lines under 'Our PoCs are in the news:'; 2 "
              "rows carried_from = today",
              (len(story_lines(thu[0])), thu[0].split("\n")[2],
               news.LABEL_INDUSTRY in thu[0], carried,
               {(r["carried_from"], r["carry_count"]) for r in b.rows() if r["carried_from"]}),
              (5, news.LABEL_OURS, False,
               ["PolyAI Thursday story foxtrot", "PolyAI Thursday story golf"],
               {("2026-10-08", 1)}))
        check("edge 36 six about us, five slots: the one left out is carried, not lost",
              len(carried) >= 1, True)
        b.seed([{"title": "PolyAI Friday story", "when": utc(FRI, 11), "flag": 1,
                 "names": "PolyAI", "imp": 3}]
               + [{"title": f"Friday industry story {W[n]}", "when": utc(FRI, 10, n), "imp": 4}
                  for n in range(10)])
        fri = await b.daily(FRI)
        check("T44 next day: 2 carried + 1 new ours + 10 industry -> the carried 2 first, "
              "the new one third, then 2 industry",
              (titles(fri[0])[:3], len(titles(fri[0])), fri[0].split("\n").index(
                  news.LABEL_INDUSTRY) > fri[0].split("\n").index(news.LABEL_OURS),
               [t.startswith("Friday industry story") for t in titles(fri[0])[3:]]),
              (["PolyAI Thursday story foxtrot", "PolyAI Thursday story golf",
                "PolyAI Friday story"], 5, True, [True, True]))
        check("edge 37 a carried story and a new one, both at 3: carried first",
              titles(fri[0]).index("PolyAI Thursday story golf")
              < titles(fri[0]).index("PolyAI Friday story"), True)

    # T45: a story about us at 1 that never finds room: Wed, Thu, Fri, Mon.
    runt = "PolyAI minor mention in a roundup"
    with Bot() as b, Logs() as logs:
        b.seed([{"title": runt, "when": utc(WED, 9), "flag": 1, "names": "PolyAI", "imp": 1}])
        counts = []
        for day in (WED, THU, FRI, MON):
            b.seed([{"title": f"PolyAI {day:%A} story {W[n]}", "when": utc(day, 10, n),
                     "flag": 1, "names": "PolyAI", "imp": 3} for n in range(5)])
            await b.daily(day)
            counts.append(b.row(runt)["carry_count"])
        check("T45 carried through 3 daily posts without room -> dropped and logged",
              (counts, b.row(runt)["carried_from"],
               logs.has("DROPPED after 3 daily posts without room", runt,
                        level=logging.WARNING)),
              ([1, 2, 3, -1], "2026-10-07", True))
        check("edge 38 ...a dropped story is not offered again, though there is room",
              runt in "".join(await b.daily(TUE)), False)
        check("edge 39 ...and its score never changed", b.row(runt)["importance"], 1)

    # T46: Friday to Monday is ONE carry.
    with Bot() as b:
        b.seed([{"title": f"PolyAI Friday item {W[n]}", "when": utc(FRI, 12 - n), "flag": 1,
                 "names": "PolyAI", "imp": 3} for n in range(6)])
        await b.daily(FRI)
        left = "PolyAI Friday item foxtrot"
        before = (b.row(left)["carried_from"], b.row(left)["carry_count"])
        mon = await b.daily(MON)
        check("T46 carried Fri -> Mon -> one carry, not three",
              (before, titles(mon[0]), b.recorded().get(left)),
              (("2026-10-09", 1), [left], "main"))

    with Bot() as b:
        b.seed([{"title": f"Evals story {W[n]}", "when": utc(TUE, 10, n), "imp": 3,
                 "topic": "evals"} for n in range(3)])
        post = await b.daily(TUE)
        check("T52 3 industry on one topic, score 3 -> only 2 in the post",
              len(story_lines(post[0])), 2)
    with Bot() as b:
        b.db.record_news_stories(
            [{"url_key": f"old.example/{n}", "url": f"https://old.example/{n}",
              "headline": f"An earlier evals story {n}", "headline_key": f"earlier evals {n}",
              "topic": "evals", "importance": 4} for n in range(2)],
            on_date=dl.iso(TUE), rule_id="R1", kind="breaking")
        b.seed([{"title": "Evals standard becomes law", "when": utc(TUE, 10), "imp": 5,
                 "topic": "evals"},
                {"title": "Another evals paper", "when": utc(TUE, 9), "imp": 3,
                 "topic": "evals"}])
        post = await b.daily(TUE)
        check("T53 industry 5 on a full topic -> in the post (and a 3 on it is not)",
              titles(post[0]), ["Evals standard becomes law"])

    long = [{"title": f"Long link story {W[n]}", "when": utc(TUE, 10, n), "imp": 4,
             "flag": 1 if n < 2 else 0, "names": "PolyAI" if n < 2 else "",
             "url": G + "y" * 480 + str(n)} for n in range(5)]
    with Bot() as b:
        b.seed(long)
        parts = await b.daily(TUE)
        check("T54 over 2,000 chars -> split between stories; part 2 starts '**AI News, "
              "... (continued)**'",
              (len(parts) > 1, all(len(p) <= 2000 for p in parts),
               parts[1].split("\n")[0], sum(len(story_lines(p)) for p in parts)),
              (True, True, "**AI News, Tue 13 Oct (continued)**", 5))
        check("T83 a '(continued)' part carries the heading and no opener, no closer, no "
              "label",
              ([w in p for p in parts[1:] for w in (news.OPENER, news.CLOSER, news.LABEL_OURS,
                                                    news.LABEL_INDUSTRY, "<@")],
               all(ln.startswith("- ") for p in parts[1:] for ln in p.split("\n")[1:] if ln)),
              ([False] * 5 * (len(parts) - 1), True))
        check("edge 40 ...and the first part keeps the heading with its tags and the "
              "opener", parts[0].split("\n")[:2],
              [f"**AI News, Tue 13 Oct** {TAGS}", news.OPENER])
    with Bot() as b, Logs() as logs:
        b.seed([{"title": "A story with an enormous link", "when": utc(TUE, 10), "imp": 4,
                 "url": G + "z" * 1600},
                {"title": "A story with an ordinary link", "when": utc(TUE, 9), "imp": 4}])
        post = await b.daily(TUE)
        check("T55 a line over 1,500 chars -> left out and logged",
              (titles(post[0]), logs.has("left out of the message", "cannot fit")),
              (["A story with an ordinary link"], True))

    with Bot() as b:
        since, _until = b.w.bot._main_window(MON)
        check("T56 Monday's window starts at Friday's R1 slot",
              since, drip.order_slot("R1", FRI))
        check("edge 43 ...read off the schedule (drip.order_slot)",
              (since.date(), since.tzinfo is not None), (FRI, True))


# =============================================================================
# RENDERING — T57 to T63, T81, T82
# =============================================================================


async def rendering(check) -> None:
    print("\nRENDERING (T57-T63, T81, T82)")
    u = G + "CBMi1?oc=5"
    check("T57 a Google item, source Reuters -> '- Headline ([Reuters](<url>))'",
          news.story_line({"headline": "Lab ships evals - Reuters", "source": "Reuters",
                           "url": u}), f"- Lab ships evals ([Reuters](<{u}>))")
    check("T58 a direct techcrunch.com item -> '- Headline ([techcrunch.com](<url>))'",
          news.story_line({"headline": "Lab ships evals",
                           "source": "AI News & Artificial Intelligence | TechCrunch",
                           "url": "https://techcrunch.com/2026/10/13/evals"}),
          "- Lab ships evals ([techcrunch.com](<https://techcrunch.com/2026/10/13/evals>))")
    check("edge 44 a Google News link is named for Google's source, a direct feed for its "
          "site", (news.outlet({"source": "Reuters", "url": u}),
                   news.outlet({"source": "Feed title", "url": "https://www.inc42.com/a"})),
          ("Reuters", "inc42.com"))
    check("T59 'Lab ships evals - Reuters.' -> 'Lab ships evals'",
          news.clean_headline({"headline": "Lab ships evals - Reuters.", "source": "Reuters",
                               "url": u}), "Lab ships evals")
    check("edge 45 the ' - Outlet' tail and the trailing full stop come off; a dash that "
          "is part of the headline stays",
          news.clean_headline({"headline": "GPT-5 - what we know so far.", "source": "Wired",
                               "url": "https://wired.com/x"}), "GPT-5 - what we know so far")
    check("T60 a title with '<@123>' and '**' -> both removed",
          news.clean_headline({"headline": "**Lab** ships <@123> evals", "url": u}),
          "Lab ships evals")
    check("edge 46 ...and @everyone cannot ping either",
          "@everyone" in news.story_line({"headline": "Lab tells @everyone about evals",
                                          "url": u}), False)
    flagged = {"headline": "Tavus's Griffin convinced 48% in simulated interviews - NewsBytes",
               "what": "Tavus's Griffin AI achieved 48%", "source": "NewsBytes", "url": u,
               "flag": 1, "names": "Tavus", "kind": "poc",
               "sheet_ref": "Tavus — on Master Pipeline"}
    line = news.story_line(flagged)
    check("T61 a flagged story -> no sheet tag, no '— what' clause, no bold",
          (line, "Master Pipeline" in line, " — " in line, "**" in line),
          (f"- Tavus's Griffin convinced 48% in simulated interviews ([NewsBytes](<{u}>))",
           False, False, False))

    with Bot(pretend=(TUE, 11, 22)) as b:
        b.seed([{"title": SARVAM, "when": utc(TUE, 10), "source": "Economic Times",
                 "flag": 1, "names": "Sarvam AI", "imp": 3},
                {"title": STATE_AGS, "when": utc(TUE, 9), "imp": 4}])
        await b.w.say("any AI news today?", mention=True)
        reply = b.new_posts(0)
        want = template(TEMPLATE_G, [SARVAM, STATE_AGS])
        check("T62 a news question in the channel -> template G", reply, [want])
        check("T81 a news question -> equals template G",
              news.render([news.story_from_feed(r) for r in b.rows()], mode=news.MODE_ANSWER,
                          day=TUE, tags=TAGS), want)
        check("edge 47 ...the same line shape, no bold but the heading, no tags, no "
              "opener, no closer",
              (reply[0].count("**"), "<@" in reply[0], news.OPENER in reply[0],
               news.CLOSER in reply[0], b.scorer.calls()), (2, False, False, False, 0))
    with Bot(pretend=(TUE, 11, 22)) as b:
        await b.w.say("any AI news today?", mention=True)
        check("an answer on a day with nothing still says so (the quiet line is retired "
              "for the daily post only)", b.new_posts(0), [news.quiet_line(TUE)])

    import simulation
    long = [{"title": f"Long link story {W[n]}", "when": utc(TUE, 10, n), "imp": 4,
             "flag": 1 if n < 2 else 0, "names": "PolyAI" if n < 2 else "",
             "url": G + "y" * 480 + str(n)} for n in range(5)]
    with Bot(test_mode=True) as b:
        b.seed(long)
        with simulation.simulating():
            parts = await b.daily(TUE)
        check("T63 a test run -> the test prefix on each part; mentions plain",
              (len(parts) > 1, [p.startswith("[TEST] **AI News, Tue 13 Oct") for p in parts],
               any("<@" in p for p in parts), parts[0].split("\n")[0]),
              (True, [True] * len(parts), False,
               "[TEST] **AI News, Tue 13 Oct** @Vaishnavi @Sid"))
        check("edge 48 ...and every part still fits one Discord message",
              all(len(p) <= 2000 for p in parts), True)
    with Bot(test_mode=True) as b:
        b.seed(_template_stories())
        tagged = await b.daily(TUE)
        check("test mode and live differ by the tag alone",
              [rw.strip_tag(p) for p in tagged],
              [template(TEMPLATE_A, [SARVAM, GAMEZ, STATE_AGS, UK_AISI, BRUTAL],
                        direct={UK_AISI: "www.gov.uk"})])


def no_bold_lines(check) -> None:
    """T82, asked of every message every scenario above sent."""
    bullets = [ln for text in ALL_POSTED for ln in str(text).split("\n")
               if ln.startswith("- ")]
    check("T82 no line in any rendered news message starts with '- **'",
          (len(bullets) > 50, [ln for ln in bullets if ln.startswith("- **")],
           [ln for ln in bullets if "**" in ln]), (True, [], []))
    shape = re.compile(r"^- [^\n]+ \(\[[^\]\n]+\]\(<https?://[^>\s]+>\)\)$")
    check("...and every story line in every message is the one shape",
          [ln for ln in bullets if not shape.match(ln)], [])


# =============================================================================
# RESEARCHERS — T64 to T70
# =============================================================================


async def researchers(check) -> None:
    print("\nRESEARCHERS (T64-T70)")
    now = ist(TUE, 14, 0)
    campus = [{"company": "Middlesex University London", "name": "David Gamez"}] + \
             [{"company": f"University of Example {i}", "name": f"Author Number{i:03d}"}
              for i in range(260)]
    paper = "Machine consciousness, measured in a language model"
    entries = [
        {"n": 1, "title": paper, "authors": ["David Gamez", "A. N. Other"],
         "category": "cs.AI", "when": now - timedelta(hours=20)},
        {"n": 2, "title": "Gene expression in yeast under stress", "authors": ["David Gamez"],
         "category": "q-bio.GN", "when": now - timedelta(hours=20)}]
    routes = [('"David Gamez"', [
        g("David Gamez scores twice in the derby", now - timedelta(hours=2), "BBC Sport",
          "A fine win at home."),
        g("David Gamez on what a machine can feel", now - timedelta(hours=3), "The Guardian",
          "The Middlesex University lecturer on consciousness.")])]
    with Bot(names=names_from(pocs=campus, watch=[], generic=[]),
             pins={"NEWS_TOPICS": ["evals"]}) as b, \
            Net(routes, arxiv_entries=entries) as net:
        got = await b.cycle(now)
        stamps = [t for t, _u in net.arxiv]
        asked = Counter(a for _t, u in net.arxiv for a in re.findall(r'au:"([^"]+)"',
                                                                    query_of(u)))
        check("T64 261 authors, batch 20 -> 14 arXiv queries",
              (len(net.arxiv), len(asked), set(asked.values()), got["big"]),
              (14, 261, {1}, True))
        check("T70 at least 3 s between two arXiv queries",
              min(y - x for x, y in zip(stamps, stamps[1:])) >= 3.0, True)
        row = b.row(paper)
        check("T65 arXiv entry by 'David Gamez', primary cs.AI -> stored, flagged, "
              "names=David Gamez, source arXiv",
              (row.get("flag"), row.get("names"), row.get("source"), row.get("kind"),
               row.get("source_rank")), (1, "David Gamez", "arXiv", "researcher", 5))
        check("T66 a same-name author, primary q-bio -> dropped",
              b.row("Gene expression in yeast under stress"), {})
        check("T68 a researcher's Google item with no AI word, institution or company -> "
              "dropped", b.row("David Gamez scores twice in the derby"), {})
        kept = b.row("David Gamez on what a machine can feel")
        check("T69 a researcher's Google item with 'Middlesex University' in the summary "
              "-> kept and flagged",
              (kept.get("flag"), kept.get("names"), kept.get("kind"),
               kept.get("source_rank")),
              (1, "David Gamez", "researcher", feeds.RANK_RESEARCHERS))
        check("the researchers are searched 15 to a Google query, after our names",
              sum(1 for _t, u in net.google if '"Author Number000"' in query_of(u)
                  and query_of(u).count(" OR ") == 14), 1)
        net.arxiv.clear()
        net.google.clear()
        await b.cycle(now + timedelta(hours=1))
        check("T64 ...once, at the 14:00 pull only: the 15:00 cycle asks arXiv nothing, "
              "and Google for no researcher",
              (len(net.arxiv), any("Author Number" in query_of(u) for _t, u in net.google)),
              (0, False))

    def down(url, n):
        if "arxiv.org" in url:
            raise RuntimeError("503 Server Error: Service Unavailable")

    with Bot(names=names_from(pocs=campus[:30], watch=[], generic=[]),
             pins={"NEWS_TOPICS": ["evals"]}) as b, Net(fail=down) as net, Logs() as logs:
        got = await b.cycle(now)
        check("T67 arXiv 503 -> logged, skipped, no crash",
              (got["status"], len(net.arxiv),
               logs.has("[arxiv]", "skipped until tomorrow", level=logging.WARNING)),
              ("done", 1, True))
        check("edge 50 ...and tomorrow's big pull asks again",
              bool(await b.cycle(ist(TUE + timedelta(days=1), 14, 0))) and len(net.arxiv) == 2,
              True)
    check("edge 51 two researchers share a name: a paper counts only in our categories",
          arxiv.CATEGORIES, ("cs.AI", "cs.CL", "cs.LG", "cs.CV", "cs.SD", "eess.AS",
                             "stat.ML"))

    meeting = names_from(pocs=[{"company": "Middlesex University London",
                                "name": "David Gamez", "meeting_date": "09-Oct-2026"},
                               {"company": "IIT Bombay", "name": "Asha Rao"}], watch=[])
    warm = [j["query"] for j in feeds.plan(meeting, big=True, hours=24) if j["lane"] == "warm"]
    check("edge 53 a researcher with a meeting -> the warm-person search runs",
          warm, ['("David Gamez" "Middlesex University London")'])
    check("edge 52/49 the filter is in code: an institution in two words, an AI word, or "
          "nothing",
          (feeds.institution_in("at Middlesex University today", "Middlesex University London"),
           feeds.institution_in("a university in London", "Middlesex University London"),
           feeds.is_institution("AI Security Institute (UK Govt.)", INSTITUTION),
           feeds.is_institution("Indian Institute of Science", INSTITUTION)),
          (True, False, False, True))

    both = names_from(["PolyAI"], pocs=[{"company": "PolyAI", "name": "Pat Paper",
                                         "paper_links": "https://arxiv.org/abs/2610.9"}],
                      watch=[])
    joint = "PolyAI's Pat Paper publishes a dialogue evals benchmark"
    with Bot(names=both, pins={"NEWS_TOPICS": ["evals"]}) as b, \
            Net([('"PolyAI"', [g(joint, now - timedelta(hours=1))])],
                arxiv_entries=[{"n": 9, "title": joint, "authors": ["Pat Paper"],
                                "category": "cs.CL", "when": now - timedelta(hours=5)}]):
        await b.cycle(now)
        mine = [r for r in b.rows() if r["headline_key"] == news.headline_key(joint)]
        check("edge 54 a person at a company with a paper link: both lanes, one item, one "
              "row, both names",
              (len(mine), mine[0]["names"], mine[0]["flag"], mine[0]["source_rank"]),
              (1, "PolyAI, Pat Paper", 1, feeds.RANK_NAMES))


# =============================================================================
# CONFIG — T71, T73, T74
# =============================================================================


def settings(check) -> None:
    print("\nCONFIG (T71, T73, T74)")
    import test_env_example as env

    failed = []
    for cls in (env.TestEveryVariableIsListed, env.TestRetiredVariablesStayRetired,
                env.TestTheFileIsSane):
        for name in sorted(n for n in dir(cls) if n.startswith("test_")):
            try:
                getattr(cls(), name)()
            except AssertionError as e:
                failed.append(f"{name}: {str(e)[:200]}")
    check("T71 .env.example lists every variable the code reads, uncommented; retired "
          "ones in the RETIRED block (tests/test_env_example.py)", failed, [])
    new = ["NEWS_PULL_FIRST", "NEWS_PULL_UNTIL", "NEWS_PULL_WINDOW_HOURS",
           "NEWS_NAME_BATCH_SIZE", "NEWS_GENERIC_NAMES", "NEWS_WATCH_NAMES",
           "NEWS_INSTITUTION_WORDS", "NEWS_REQUEST_GAP_SECONDS",
           "NEWS_BREAKING_MAX_PER_CYCLE", "NEWS_POC_BREAKING_MIN", "NEWS_CARRY_MAX_DAYS",
           "NEWS_BREAKING_MAX_AGE_HOURS", "ARXIV_ENABLED", "ARXIV_BATCH_SIZE"]
    retired = ["NEWS_FEED_POLL_MINUTES", "NEWS_CHECK_TIMES", "NEWS_CHECK_MAX_USES",
               "NEWS_BREAKING_MAX_PER_DAY", "NEWS_POC_TARGETS_PER_DAY", "NEWS_POC_SLOTS",
               "NEWS_POC_LOOKBACK_DAYS", "NEWS_OVERFLOW_ENABLED",
               "NEWS_OVERFLOW_MIN_IMPORTANCE", "NEWS_OVERFLOW_MAX_ITEMS"]
    live, dead = env.live_keys(), env.retired_keys()
    check("T71 ...the fourteen new variables are live lines with their defaults",
          ([v for v in new if v not in live],
           [re.search(rf"^{k}=(.*)$", env._example_text(), re.M).group(1).strip()
            for k in ("NEWS_PULL_FIRST", "NEWS_PULL_UNTIL", "NEWS_PULL_WINDOW_HOURS",
                      "NEWS_NAME_BATCH_SIZE", "NEWS_REQUEST_GAP_SECONDS",
                      "NEWS_BREAKING_MAX_PER_CYCLE", "NEWS_POC_BREAKING_MIN",
                      "NEWS_CARRY_MAX_DAYS", "NEWS_BREAKING_MAX_AGE_HOURS", "ARXIV_ENABLED",
                      "ARXIV_BATCH_SIZE")]),
          ([], ["14:00", "23:00", "2", "15", "1.5", "3", "4", "3", "3", "true", "20"]))
    check("T71 ...and the ten retired ones are in the RETIRED block, not live",
          ([v for v in retired if v not in dead], [v for v in retired if v in live]),
          ([], []))
    text = env._example_text()
    topics = re.search(r"^NEWS_TOPICS=(.*)$", text, re.M).group(1).split(",")
    urls = re.search(r"^NEWS_RSS_FEEDS=(.*)$", text, re.M).group(1).split(",")
    check("the new NEWS_TOPICS default: 38 topics, with leadership, robot and robotics "
          "gone",
          (len(topics), [t for t in ("leadership", "robot", "robotics") if t in topics],
           topics[0], topics[-1]),
          (38, [], "artificial intelligence", "AI data partnership"))
    check("the new NEWS_RSS_FEEDS default: the five, plus the nine from the ticket",
          (len(urls), urls[5:]),
          (14, ["https://inc42.com/feed/", "https://yourstory.com/feed",
                "https://economictimes.indiatimes.com/tech/rssfeeds/13357270.cms",
                "https://analyticsindiamag.com/feed/",
                "https://www.moneycontrol.com/rss/technology.xml", "https://sifted.eu/feed",
                "https://www.uktech.news/feed", "https://tech.eu/feed/",
                "https://news.crunchbase.com/feed/"]))

    env_vars = dict(os.environ, ANTHROPIC_API_KEY="x", PYTHONIOENCODING="utf-8",
                    NEWS_FEED_POLL_MINUTES="7", NEWS_CHECK_TIMES="banana,25:99",
                    NEWS_CHECK_MAX_USES="x", NEWS_BREAKING_MAX_PER_DAY="lots",
                    NEWS_POC_TARGETS_PER_DAY="-1", NEWS_POC_SLOTS="two",
                    NEWS_POC_LOOKBACK_DAYS="", NEWS_OVERFLOW_ENABLED="maybe",
                    NEWS_OVERFLOW_MIN_IMPORTANCE="9", NEWS_OVERFLOW_MAX_ITEMS="x")
    code = ("import config, news, feeds, arxiv; "
            "print('OK', [n for n in %r if hasattr(config, n)], len(news.pull_slots()))"
            % retired)
    ran = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env_vars,
                         capture_output=True, text=True, timeout=180)
    check("T73 a retired variable left in .env -> ignored, no crash",
          (ran.returncode, [ln for ln in ran.stdout.splitlines() if ln.startswith("OK")]),
          (0, ["OK [] 10"]))

    tails = {}
    for module in ("feeds", "news", "arxiv"):
        ran = subprocess.run([sys.executable, "-m", module], cwd=ROOT,
                             env=dict(os.environ, ANTHROPIC_API_KEY="x",
                                      PYTHONIOENCODING="utf-8"),
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=300)
        tails[module] = (ran.returncode, (ran.stdout.strip().splitlines() or [""])[-1])
    check("T74 python -m feeds, python -m news, python -m arxiv -> ALL PASSED (the verify "
          "scripts and pytest are run beside this one)",
          tails, {m: (0, "ALL PASSED") for m in ("feeds", "news", "arxiv")})


GROUPS = (names, collection, flags, breaking, daily, rendering, researchers)


async def run(check) -> None:
    for group in GROUPS:
        await group(check)
    no_bold_lines(check)
    settings(check)
