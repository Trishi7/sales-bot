"""R2 "NEW COMPANIES IN THE NEWS" (rebuilt 12 Oct 2026) — the checks, shared by verify_news_screen.py and
tests/test_news_screen.py.

    sys.path.insert(0, <repo>/tests); import news_screen_checks as nsc
    nsc.run(check)                 # check(name, got, want)

WHAT RUNS HERE IS THE REAL THING: the real screen (`bot._screen_companies`), the real matcher and renderer
(`news_screen.py`), the real sheet parser on made-up grids, the real feed store and ledger in a throwaway
database, and a real bot behind a fake Discord (`replies_world.World`). The model is a SCRIPT: it answers the
two R2 calls from a table and records what it was asked, including a wrong answer where a check needs one. No
network, no real sheet, no search (the one lookup is a stand-in).

THE SHEET IS MADE UP TO THE REAL NUMBERS: 281 companies on the Master Pipeline, 212 on Outreach PoCs, 111 on
both, so 382 known and 101 on Outreach PoCs only. ElevenLabs is number 1 of the 382 and Yellow.ai number 368,
which are the two companies the 11 Oct post got wrong.

Every R2 setting is pinned to its .env.example default, never read from this machine's .env.
"""
import asyncio
import contextlib
import logging
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)
os.environ.setdefault("DISCORD_TOKEN", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import canned_sheet  # noqa: E402
import config  # noqa: E402
import deadlines as dl  # noqa: E402
import drip  # noqa: E402
import feeds  # noqa: E402
import gtm_sheet  # noqa: E402
import news  # noqa: E402
import news_screen as ns  # noqa: E402
import nextaction  # noqa: E402
import rule13_fixtures as fx  # noqa: E402

TUE, FRI = date(2026, 10, 6), date(2026, 10, 9)             # both already past: research runs for them
PREV_FRI, OLD_FRI = date(2026, 10, 2), date(2026, 9, 18)
PINS = {"NEWS_SCREEN_STALE_DAYS": 14, "NEWS_SCREEN_FIRST_RUN_DAYS": 7, "NEWS_SCORE_MAX_ITEMS": 40,
        "NEWS_FEED_KEEP_DAYS": 14, "SHEET_CACHE_SECONDS": 60, "SHEET_WRITES_ENABLED": True,
        "SHEET_ROW_ADDITIONS_ENABLED": True,
        "SHEET_APPENDABLE_TABS": ["outreach_pocs", "researcher_lines", "events_summits"],
        "WEB_SEARCH_ENABLED": True, "SEARCH_BACKEND": "searxng"}


# -- the sheet: 281 + 212, 111 on both, 382 known ---------------------------------------------------------------

def company_lists() -> tuple:
    """(pocs, pipeline) names. `known_order()` is how `_known_companies` walks them."""
    both = ["ElevenLabs"] + [f"Shared Co {i:03d}" for i in range(1, 111)]                     # 111
    pocs_only = ["Vapi.ai"] + [f"Contact Co {i:03d}" for i in range(1, 101)]                   # 101
    pipe_only = [f"Pipeline Co {i:03d}" for i in range(1, 171)]                                # 170
    pipe_only[155] = "Yellow.ai"                # 212 + 156 = number 368 of 382
    pipe_only[10] = "Nudge - AI Commerce Discovery"
    pipe_only[11] = "Google DeepMind"
    return both + pocs_only, both + pipe_only


def grids(*, extra_pipeline=(), extra_pocs=()) -> dict:
    pocs, pipeline = company_lists()
    return {
        "Outreach PoCs": [list(fx.HEADERS_7OCT)] + [
            fx.values_row(i, company=c, name=f"Person {i:03d}", designation="CTO")
            for i, c in enumerate(list(pocs) + list(extra_pocs), 1)],
        "Master Pipeline": [list(canned_sheet.PIPELINE_HEADERS)] + [
            [str(i), c, "AI", "US", "", "", ""] for i, c in enumerate(list(pipeline) + list(extra_pipeline), 1)],
    }


# -- the scripted model -------------------------------------------------------------------------------------------

class Model:
    """Answers R2's two calls from tables, and remembers what it was asked."""

    def __init__(self, about: dict, verdicts: dict = None, lookups=()):
        self.about = dict(about)                 # story title -> "Company; Company" | "NONE"
        self.verdicts = dict(verdicts or {})     # company -> (what, GOOD|WEAK|anything, reason)
        self.lookups = set(lookups)              # companies it says the story does not describe
        self.name_calls, self.verdict_calls = [], []

    async def screen_names(self, *, stories):
        self.name_calls.append([s.get("title") for s in stories])
        return "\n".join(f"NAMES | {i} | {self.about.get(s.get('title'), 'NONE')}"
                         for i, s in enumerate(stories, 1))

    async def screen_verdicts(self, *, candidates, use_cases="", allow_lookup=False):
        self.verdict_calls.append({"companies": [c["company"] for c in candidates],
                                   "lookup": [c["company"] for c in candidates if c.get("lookup")],
                                   "use_cases": bool(use_cases)})
        out = []
        for c in candidates:
            name = c["company"]
            if allow_lookup and name in self.lookups:
                out.append(f"LOOKUP | {name}")
                continue
            what, word, why = self.verdicts.get(name, ("was in the news", "WEAK", "not clearly an AI team"))
            out.append(f"VERDICT | {name} | {what} | {word} | {why}")
        return "\n".join(out)


def story(title, *, importance=3, seen=None, published=None) -> dict:
    url = "https://news.example/" + re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    seen = seen or datetime(2026, 10, 8, 10, 0, tzinfo=dl.IST)
    return {"url": url, "url_key": news.url_key(url), "headline_key": news.headline_key(title), "title": title,
            "summary": "", "source": "TechCrunch", "published_at": feeds.utc_iso(published or seen),
            "topic_hint": "", "seen_at": feeds.utc_iso(seen), "kind": feeds.KIND_INDUSTRY, "sheet_ref": "",
            "_importance": importance}


def seed(w, stories) -> None:
    w.bot.db.news_feed_add([{k: v for k, v in s.items() if not k.startswith("_")} for s in stories])
    w.bot.db.news_feed_set_scores(
        [{"url_key": s["url_key"], "importance": s["_importance"], "topic": "funding", "what": ""} for s in stories],
        scored_at=feeds.utc_iso(dl.real_now_ist()))


class LogTap(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.INFO)
        self.records = []

    def emit(self, record):
        with contextlib.suppress(Exception):
            self.records.append((record.levelname, record.getMessage()))


class Stage:
    """One bot, the 382-company sheet, a scripted model, a log tap."""

    def __init__(self, model: Model, *, test_mode=False, sheet=None):
        import replies_world as rw
        self.rw = rw
        self.w = rw.World(test_mode, pins=dict(PINS))
        self.model = model
        self.sheet = sheet
        self.tap = LogTap()

    def __enter__(self):
        self.w.__enter__()
        self.w.sheet.grids.update(self.sheet or grids())
        self.w.bot.llm.screen_names = self.model.screen_names
        self.w.bot.llm.screen_verdicts = self.model.screen_verdicts
        logging.getLogger("sales-bot").addHandler(self.tap)
        logging.getLogger().addHandler(self.tap)
        return self

    def __exit__(self, *exc):
        logging.getLogger("sales-bot").removeHandler(self.tap)
        logging.getLogger().removeHandler(self.tap)
        return self.w.__exit__(*exc)

    def item(self) -> dict:
        return {"rule": nextaction.R_NEWS_SCREEN, "rule_id": "R2", "type": nextaction.R_NEWS_SCREEN,
                "web_pending": True, "text": "", "contact_key": ""}

    async def screen(self, day=FRI) -> dict:
        item = self.item()
        await self.w.bot._screen_companies([item], today=day, marker=dl.iso(day))
        return item

    async def post(self, item, *, marker=None, live=True):
        """Put the message in the channel and run the after-send bookkeeping, as the sender does."""
        post = self.w.bot_says(drip.with_heading(item["research"], drip.heading("R2")))
        message = {"type": nextaction.R_NEWS_SCREEN, "rule_id": "R2", "actions": [item]}
        await self.w.bot._after_send(message, sent=post,
                                     marker=marker or dl.iso(dl.real_today_ist() if live else FRI))
        return post

    def marker(self) -> str:
        return self.w.bot.db.rule_last_posted("R2")

    def ledger(self) -> dict:
        return {r["company"]: r["status"] for r in self.w.bot.db.screen_ledger()}

    def errors(self) -> list:
        return [m for lvl, m in self.tap.records if lvl == "ERROR"]

    async def say(self, text, **kw):
        before = len(self.w.posted)
        await self.w.say(text, **kw)
        return [self.rw.strip_tag(m.content) for m in self.w.posted[before:]], self.w.posted[before:]


@contextlib.contextmanager
def pinned(**extra):
    values = dict(PINS, **extra)
    absent = object()
    before = {k: getattr(config, k, absent) for k in values}
    for k, v in values.items():
        setattr(config, k, v)
    try:
        yield
    finally:
        for k, v in before.items():
            if v is absent:
                with contextlib.suppress(AttributeError):
                    delattr(config, k)
            else:
                setattr(config, k, v)


def links_in(text) -> list:
    return re.findall(r"\[[^\]\n]+\]\(<(https?://[^>\s]+)>\)", text or "")


def bare_urls(text) -> list:
    """Any url or host in the text that is NOT inside a masked link."""
    stripped = re.sub(r"\[[^\]\n]+\]\(<https?://[^>\s]+>\)", "", text or "")
    return re.findall(r"https?://\S+|\b[\w-]+\.(?:com|ai|io|org|example)\b/\S*", stripped)


NUDGE_STORY = "Nudge Security raises $25M to secure SaaS sprawl"
MIRAGE_STORY = "Mirage Labs launches an Indic speech model"
ELEVEN_STORY = "ElevenLabs ships a new dubbing studio"
YELLOW_STORY = "Yellow.ai signs a telecom customer"
VAPI_STORY = "Vapi raises a Series B for voice agents"
EU_STORY = "EU publishes its AI Act guidance"
OCT11 = {NUDGE_STORY: "Nudge Security", MIRAGE_STORY: "Mirage Labs", ELEVEN_STORY: "ElevenLabs",
         YELLOW_STORY: "Yellow.ai", VAPI_STORY: "Vapi", EU_STORY: "NONE"}
VERDICTS = {"Nudge Security": ("raised $25M", "WEAK", "sells to enterprise security teams, not AI teams"),
            "Mirage Labs": ("launched an Indic speech model", "GOOD",
                            "Phase 1 voice focus, and evals are close to Pulse")}
EXPECTED = """**New companies in the news**
Two companies in today's news aren't on the Master Pipeline yet:

- **[Mirage Labs](<https://news.example/mirage-labs-launches-an-indic-speech-model>)** — launched an Indic speech model. **Good fit:** Phase 1 voice focus, and evals are close to Pulse.

- **[Nudge Security](<https://news.example/nudge-security-raises-25m-to-secure-saas-sprawl>)** — raised $25M. **Weak fit:** sells to enterprise security teams, not AI teams.

Would you like me to add either of them to the Master Pipeline?

Vapi.ai isn't on the Master Pipeline, but it is on Outreach PoCs. Shall I add a pipeline row?"""


def oct11_stories() -> list:
    return [story(NUDGE_STORY, importance=3), story(MIRAGE_STORY, importance=4), story(ELEVEN_STORY, importance=5),
            story(YELLOW_STORY, importance=3), story(VAPI_STORY, importance=3), story(EU_STORY, importance=4)]


# -- the checks ------------------------------------------------------------------------------------------------------

def matching(check):
    print("\nMATCHING — the full name decides; a partial match is a question")
    pocs, pipeline = company_lists()
    idx = ns.build_index(pocs, pipeline)
    check("the fixture is the real shape: 281 + 212, 111 on both, 382 known, 101 on Outreach PoCs only",
          (len(pipeline), len(pocs), len(set(pocs) & set(pipeline)), idx["count"],
           len([k for k in idx["pocs"] if k not in idx["pipeline"]])), (281, 212, 111, 382, 101))
    check('"Nudge Security" is not matched to "Nudge - AI Commerce Discovery"',
          (ns.classify("Nudge Security", idx)["outcome"], ns.classify("Nudge Security", idx)["similar"]),
          (ns.NEW, []))
    only_security = ns.build_index([], ["Nudge Security"])
    check("...in the other direction either",
          (ns.classify("Nudge - AI Commerce Discovery", only_security)["outcome"],
           ns.classify("Nudge - AI Commerce Discovery", only_security)["similar"]), (ns.NEW, []))
    check('"Google" is not matched to "Google DeepMind"', ns.classify("Google", idx)["outcome"], ns.NEW)
    check("...it is a QUESTION: the tracked company it resembles is named, and it is not dropped",
          ns.classify("Google", idx)["similar"], ["Google DeepMind"])
    check("the same company spelt differently IS the same company",
          [ns.classify(n, idx)["outcome"] for n in ("Eleven Labs", "ELEVENLABS", "ElevenLabs Inc.", "Yellow AI",
                                                    "yellow.ai")], [ns.KNOWN] * 5)
    check("the substring matcher this must not use would have merged them (why find_company is not the filter)",
          "nudge" in gtm_sheet.normalise_header("Nudge - AI Commerce Discovery")
          and "nudge" in gtm_sheet.normalise_header("Nudge Security"), True)
    check("on Outreach PoCs and not on the Master Pipeline", ns.classify("Vapi", idx)["outcome"], ns.POCS_ONLY)


def window(check):
    print("\nWINDOW — from the last post to now, never past the previous scheduled day; by first-seen time")
    now = datetime(2026, 10, 9, 14, 0, tzinfo=dl.IST)
    floor = datetime(2026, 10, 6, 18, 0, tzinfo=dl.IST)
    marker = datetime(2026, 10, 6, 18, 5, tzinfo=dl.IST)
    check("it starts at the moment of the last post and ends now",
          ns.window(now=now, marker=marker, floor=floor), (marker, now))
    check("Tuesday posted nothing (the marker is the Friday before): Friday still starts at Tuesday",
          ns.window(now=now, marker=datetime(2026, 10, 2, 14, 3, tzinfo=dl.IST), floor=floor), (floor, now))
    check("a fortnight's outage: no wider than the previous scheduled day",
          ns.window(now=now, marker=datetime(2026, 9, 18, 14, 0, tzinfo=dl.IST), floor=floor), (floor, now))
    check("the first run ever, no marker: seven days",
          ns.window(now=now, marker=None, floor=floor, first_days=7), (now - timedelta(days=7), now))
    check("the previous scheduled day: Friday's is Tuesday, Tuesday's is Friday",
          (ns.previous_run_day(FRI, (1, 4)), ns.previous_run_day(TUE, (1, 4))), (TUE, PREV_FRI))

    async def through_the_bot():
        out = {}
        about = {"seen after": "After Co", "seen before": "Before Co", "published before, seen after": "Late Co",
                 "exactly on the marker": "Edge Co", "exactly now": "Now Co", "seen after now": "Future Co"}
        with Stage(Model(about)) as s:
            w = s.w
            mark = datetime(2026, 10, 6, 18, 5, tzinfo=dl.IST)
            until = drip.order_slot("R2", FRI)
            w.bot.db.set_rule_last_posted("R2", posted_at=feeds.utc_iso(mark), message_id="1")
            seed(w, [
                story("seen after", seen=mark + timedelta(hours=3)),
                story("seen before", seen=mark - timedelta(minutes=1)),
                story("published before, seen after", seen=mark + timedelta(minutes=10),
                      published=mark - timedelta(hours=5)),
                story("exactly on the marker", seen=mark),
                story("exactly now", seen=until),
                story("seen after now", seen=until + timedelta(minutes=1)),
            ])
            await s.screen(FRI)
            out["shown"] = sorted(s.model.name_calls[0])
            out["window"] = w.bot._screen_window(FRI)[:2]
            out["mark"], out["until"] = mark, until
            # A QUIET TUESDAY: the marker is still the Friday before.
            w.bot.db.set_rule_last_posted("R2", posted_at=feeds.utc_iso(
                datetime(2026, 10, 2, 14, 2, tzinfo=dl.IST)), message_id="0")
            out["quiet"] = w.bot._screen_window(FRI)[0]
            w.bot.db.set_rule_last_posted("R2", posted_at=feeds.utc_iso(
                datetime(2026, 9, 18, 14, 2, tzinfo=dl.IST)), message_id="0")
            out["outage"] = w.bot._screen_window(FRI)[0]
            out["floor"] = drip.order_slot("R2", TUE)
        with Stage(Model({})) as s:
            since, until, why = s.w.bot._screen_window(FRI)
            out["first"] = (until - since, why)
        return out

    with pinned():
        got = asyncio.run(through_the_bot())
    check("the bot's window for Fri 9 Oct with a marker on Tue 6 Oct 18:05",
          got["window"], (got["mark"], got["until"]))
    check("a story SEEN after the marker is in; one seen before it is out; one seen after 'now' is out",
          ("seen after" in got["shown"], "seen before" in got["shown"], "seen after now" in got["shown"]),
          (True, False, False))
    check("a story PUBLISHED before the marker but first SEEN after it is IN",
          "published before, seen after" in got["shown"], True)
    check("a story timed exactly on the marker is IN, and so is one timed exactly at 'now'",
          ("exactly on the marker" in got["shown"], "exactly now" in got["shown"]), (True, True))
    check("Tuesday posting nothing does not widen Friday's window past Tuesday's slot",
          got["quiet"], got["floor"])
    check("a fortnight's outage still starts at the previous scheduled day", got["outage"], got["floor"])
    check("the first run, with no marker, looks back seven days and says so",
          (got["first"][0], "first run" in got["first"][1]), (timedelta(days=7), True))


def known_list(check):
    print("\nKNOWN LIST — all 382, both sheets, read fresh, and the code-side filter")

    async def scenario():
        out = {}
        model = Model(OCT11, VERDICTS)
        with Stage(model) as s:
            w = s.w
            known = await w.bot._known_companies()
            out["count"] = len(known)
            out["yellow_at"] = known.index("Yellow.ai") + 1
            out["eleven_at"] = known.index("ElevenLabs") + 1
            ctx = await w.bot._research_context({"rule": "news_company_screen"})
            out["context_has_all"] = all(n in ctx for n in ("Yellow.ai", known[-1], known[200], known[120]))
            out["prompt_has_all"] = all(n in news.screen_prompt([], known) for n in known)
            seed(w, oct11_stories())
            item = await s.screen(FRI)
            out["body"] = item.get("research") or ""
            out["verdict_asked"] = list(model.verdict_calls)
            out["name_calls"] = len(model.name_calls)
            out["log"] = [m for _l, m in s.tap.records if "known companies" in m][-1:]
            out["dropped_log"] = [m for _l, m in s.tap.records if "dropped in code" in m][-1:]
        return out

    async def failures():
        out = {}
        for label, breaker in (("raises", "raise"), ("empty", "empty"), ("stale", "stale"), ("one tab", "onetab")):
            model = Model(OCT11, VERDICTS)
            with Stage(model) as s:
                w = s.w
                seed(w, oct11_stories())
                real = gtm_sheet.SHEETS.read

                def read(which=None, force=False, _real=real, _how=breaker, **k):
                    if _how == "raise":
                        raise gtm_sheet.SheetAccessError("the sheet is unreachable")
                    tabs = _real(force=force)
                    if _how == "empty":
                        tabs[gtm_sheet.RESEARCHER_LINES].rows = []
                    if _how == "stale":
                        for t in tabs.values():
                            t.read_at -= 600
                    if _how == "onetab":
                        tabs.pop(gtm_sheet.POCS, None)
                    return tabs
                gtm_sheet.SHEETS.read = read
                item = await s.screen(FRI)
                gtm_sheet.SHEETS.read = real
                message = {"type": nextaction.R_NEWS_SCREEN, "actions": [item]}
                out[label] = {"body": item.get("research") or "", "silent": drip.nothing_to_say(message),
                              "pending": item.get("web_pending"),
                              "errors": [e for e in s.errors() if "R2 CANCELLED" in e],
                              "model_calls": len(model.name_calls) + len(model.verdict_calls)}
        return out

    async def race():
        """A company added AFTER the cache was filled is caught, because the screen forces a fresh read."""
        out = {}
        model = Model({MIRAGE_STORY: "Mirage Labs"}, VERDICTS)
        with Stage(model) as s:
            w = s.w
            seed(w, [story(MIRAGE_STORY, importance=4)])
            parse = gtm_sheet.SHEETS.read
            cache, forced = {}, []

            def read(which=None, force=False, **k):
                forced.append(bool(force))
                if force or "tabs" not in cache:
                    cache["tabs"] = parse(force=True)
                return cache["tabs"]
            gtm_sheet.SHEETS.read = read
            gtm_sheet.SHEETS.read()                                  # the background read fills the cache
            w.sheet.grids.update(grids(extra_pipeline=["Mirage Labs"]))   # ...and then somebody adds the company
            stale_copy = [r.get("company") for r in gtm_sheet.SHEETS.read()[gtm_sheet.RESEARCHER_LINES].rows]
            forced.clear()
            item = await s.screen(FRI)
            gtm_sheet.SHEETS.read = parse
            out["cached_copy_lacks_it"] = "Mirage Labs" not in stale_copy
            out["forced"] = forced[:1]
            out["body"] = item.get("research") or ""
            out["verdicts"] = len(model.verdict_calls)
        return out

    with pinned():
        got = asyncio.run(scenario())
        bad = asyncio.run(failures())
        fresh = asyncio.run(race())

    check("_known_companies() returns all 382, not 200 or 120", got["count"], 382)
    check("Yellow.ai is number 368 of them, and ElevenLabs number 1", (got["yellow_at"], got["eleven_at"]), (368, 1))
    check("no truncation anywhere: the research context and the old prompt both carry every name",
          (got["context_has_all"], got["prompt_has_all"]), (True, True))
    check("the count is logged, both sheets and the PoCs-only number",
          ["382 known companies (281 on the Master Pipeline, 212 on Outreach PoCs, 101 on Outreach PoCs only)"
           in m for m in got["log"]], [True])
    check("the model wrongly returns ElevenLabs (number 1): dropped in code, never in the message",
          ("ElevenLabs" in got["body"], any("ElevenLabs" in m for m in got["dropped_log"])), (False, True))
    check("the model returns Yellow.ai (number 368): recognised as known and dropped", "Yellow.ai" in got["body"],
          False)
    check("...and neither was sent for a verdict: ONE names call, ONE verdict call, for the two survivors only",
          (got["name_calls"], [sorted(c["companies"]) for c in got["verdict_asked"]]),
          (1, [["Mirage Labs", "Nudge Security"]]))
    for label in ("raises", "empty", "stale", "one tab"):
        b = bad[label]
        check(f"the sheet read {label}: the message is CANCELLED (nothing to post, nothing pending, no model call)",
              (b["body"], b["silent"], b["pending"], b["model_calls"]), ("", True, False, 0))
        check(f"...and it is logged at ERROR, with the reason ({label})", len(b["errors"]), 1)
    check("a company added after the background read is caught by the forced read",
          (fresh["cached_copy_lacks_it"], fresh["forced"], fresh["body"], fresh["verdicts"]),
          (True, [True], "", 0))


def outcomes_and_message(check):
    print("\nOUTCOMES AND MESSAGE — three outcomes, two verdicts, masked links, five at most")

    async def scenario(test_mode=False):
        out = {}
        model = Model(OCT11, VERDICTS)
        with Stage(model, test_mode=test_mode) as s:
            seed(s.w, oct11_stories())
            item = await s.screen(FRI)
            out["body"] = item["research"]
            out["full"] = drip.with_heading(item["research"], drip.heading("R2"))
            out["sources"] = [x["url"] for x in item["sources"]]
            message = {"type": nextaction.R_NEWS_SCREEN, "rule_id": "R2", "actions": [item], "owner": "",
                       "max_items_per_post": 5}
            out["verbatim"] = drip.is_verbatim(message)
            out["composed"] = drip.compose_fallback(message)
        return out

    async def many():
        out = {}
        names = [f"Fresh Co {i}" for i in range(1, 8)]
        titles = ["Alpha raises a seed round", "Bravo ships an agent", "Charlie opens in Pune",
                  "Delta hires a research lead", "Echo launches a voice model", "Foxtrot signs a bank",
                  "Golf publishes a benchmark"]
        about = dict(zip(titles, names))
        verdicts = {n: ("did a thing", w, "a reason") for n, w in zip(
            names, ("GOOD", "WEAK", "Maybe", "Strong fit", "good", "Borderline", "N/A"))}
        model = Model(about, verdicts)
        with Stage(model) as s:
            seed(s.w, [story(t, importance=5 if i < 3 else 4 if i < 5 else 3) for i, t in enumerate(titles)])
            item = await s.screen(FRI)
            out["body"] = item["research"]
            out["held"] = [r["company"] for r in item["screen"]["held"]]
            out["asked"] = [c["companies"] for c in model.verdict_calls]
        return out

    async def three_stories():
        about = {"small mention": "Trio Labs", "the big one": "Trio Labs", "older big one": "Trio Labs"}
        model = Model(about, {"Trio Labs": ("raised a round", "GOOD", "voice evals")})
        with Stage(model) as s:
            t = datetime(2026, 10, 8, 9, 0, tzinfo=dl.IST)
            seed(s.w, [story("small mention", importance=2, seen=t + timedelta(hours=5)),
                       story("the big one", importance=5, seen=t + timedelta(hours=2)),
                       story("older big one", importance=5, seen=t)])
            item = await s.screen(FRI)
            return item["research"]

    async def nothing():
        model = Model({ELEVEN_STORY: "ElevenLabs", YELLOW_STORY: "Yellow.ai", EU_STORY: "NONE"})
        with Stage(model) as s:
            seed(s.w, [story(ELEVEN_STORY), story(YELLOW_STORY), story(EU_STORY)])
            item = await s.screen(FRI)
            message = {"type": nextaction.R_NEWS_SCREEN, "actions": [item]}
            return {"body": item.get("research") or "", "silent": drip.nothing_to_say(message),
                    "verdict_calls": len(model.verdict_calls), "pending": item.get("web_pending")}

    async def lookup():
        import search_backend
        model = Model({"Opaque raises": "Opaque Systems"},
                      {"Opaque Systems": ("raised a round", "WEAK", "builds data clean rooms")},
                      lookups={"Opaque Systems"})
        searched = []
        real = search_backend.search
        search_backend.search = lambda q, **k: (searched.append(q) or [
            {"title": "Opaque Systems", "url": "https://opaque.example", "snippet": "Confidential data clean rooms"}])
        try:
            with Stage(model) as s:
                seed(s.w, [story("Opaque raises")])
                item = await s.screen(FRI)
                return {"searched": searched, "calls": model.verdict_calls, "body": item["research"]}
        finally:
            search_backend.search = real

    with pinned():
        got = asyncio.run(scenario(False))
        test = asyncio.run(scenario(True))
        seven = asyncio.run(many())
        trio = asyncio.run(three_stories())
        none = asyncio.run(nothing())
        look = asyncio.run(lookup())

    check("the message, character for character (the tags line goes between the heading and the list at send)",
          got["full"], EXPECTED)
    check("a Master Pipeline company is dropped silently: ElevenLabs and Yellow.ai are nowhere",
          [n for n in ("ElevenLabs", "Yellow") if n in got["full"]], [])
    check("a company on neither sheet is proposed normally, as a bullet",
          [ln[:22] for ln in got["body"].split("\n") if ln.startswith("- **[")],
          ["- **[Mirage Labs](<htt", "- **[Nudge Security](<"])
    check("an Outreach-PoCs-only company gets the pipeline-row wording, in the sheet's spelling, not a bullet",
          ([ln for ln in got["body"].split("\n") if "Vapi" in ln], "- **[Vapi" in got["body"]),
          (["Vapi.ai isn't on the Master Pipeline, but it is on Outreach PoCs. Shall I add a pipeline row?"], False))
    check("the story about regulation names no company and is not in the message", "EU" in got["body"], False)
    check("R2 is posted exactly as rendered, never composed by the model",
          (got["verbatim"], got["composed"] == got["body"]), (True, True))
    check("test mode gives the same message", test["full"], got["full"])

    words = set(re.findall(r"\*\*([A-Za-z ]+ fit)[:.]", "\n".join([got["body"], seven["body"], trio, look["body"]])))
    check("exactly two verdict wordings across every case, whatever the model wrote "
          "(Maybe, Strong fit, Borderline, N/A)", sorted(words), ["Good fit", "Weak fit"])
    every = "\n".join([got["full"], seven["body"], trio, look["body"]])
    check("every link is masked, with a scheme: no bare url and no scheme-less host", bare_urls(every), [])
    check("...and every company bullet carries one", all(
        re.match(r"^- \*\*\[[^\]]+\]\(<https://[^>\s]+>\)\*\*", ln) for ln in every.split("\n")
        if ln.startswith("- ")), True)
    check("...and a url with no scheme never becomes a link (it would go out as dead text)",
          ns.company_link("Acme", "acme.com/news"), "**Acme**")
    check("a company in three stories links the most important one, and on a tie the newest",
          links_in(trio), ["https://news.example/the-big-one"])

    bullets = [ln for ln in seven["body"].split("\n") if ln.startswith("- ")]
    check("seven survivors: five named", len(bullets), 5)
    check("...and one line for the two held back",
          seven["body"].split("\n")[-1], "Two more companies are waiting — I'll bring them next time.")
    check("...the five most important are the ones shown, and only they were sent for a verdict",
          (seven["held"], [len(c) for c in seven["asked"]]), (["Fresh Co 6", "Fresh Co 7"], [5]))
    check("three or more: 'any of them'",
          "Would you like me to add any of them to the Master Pipeline?" in seven["body"], True)
    check("nothing survives: NO POST AT ALL, no verdict call, nothing left pending",
          (none["body"], none["silent"], none["verdict_calls"], none["pending"]), ("", True, 0, False))
    check("a story that does not say what the company does: ONE search, then the verdict",
          (look["searched"], [c["companies"] for c in look["calls"]], [c["lookup"] for c in look["calls"]]),
          (['"Opaque Systems" company'], [["Opaque Systems"], ["Opaque Systems"]], [[], ["Opaque Systems"]]))


def marker_and_repeats(check):
    print("\nMARKER AND REPEATS — only a live post moves the marker; unanswered companies come back")
    import simulation

    async def marker():
        out = {}
        with Stage(Model(OCT11, VERDICTS)) as s:
            w = s.w
            seed(w, oct11_stories())
            item = await s.screen(FRI)
            out["before"] = (s.marker(), s.ledger())
            # a test day ("make it Friday")
            w.bot._news_hold_depth = 1
            await s.post(item, marker=dl.iso(dl.real_today_ist()))
            w.bot._news_hold_depth = 0
            out["test_day"] = (s.marker(), s.ledger())
            # a simulation
            with simulation.simulating():
                await s.post(item, marker=dl.iso(dl.real_today_ist()))
            out["simulation"] = (s.marker(), s.ledger())
            # a pretend clock, and a day that is not today
            w.pretend_day(FRI, 14, 0)
            await s.post(item, marker=dl.iso(FRI))
            w.real_clock()
            await s.post(item, marker=dl.iso(FRI))
            out["pretend"] = (s.marker(), s.ledger())
            # the real thing
            t0 = feeds.utc_iso(dl.real_now_ist())
            post = await s.post(item, live=True)
            out["live"] = (s.marker() >= t0, s.ledger())
            out["said"] = [m for _l, m in s.tap.records if "NOT touched" in m][:1]
        return out

    async def repeats():
        out = {}
        with Stage(Model(OCT11, VERDICTS)) as s:
            w = s.w
            seed(w, oct11_stories())
            first = await s.screen(FRI)
            post = await s.post(first, live=True)
            calls = len(s.model.verdict_calls)
            # NEXT RUN, NOBODY ANSWERED, and the stories are now behind the marker.
            again = await s.screen(dl.real_today_ist())
            out["again"] = again.get("research") or ""
            out["first"] = first["research"]
            out["paid_again"] = len(s.model.verdict_calls) - calls
            # SOMEBODY ANSWERS, CHOOSING ONE: the other two are dropped.
            said, _ = await s.say("add Mirage Labs", who="member", reply_to=post)
            out["confirm"] = said
            out["after_reply"] = s.ledger()
            third = await s.screen(dl.real_today_ist())
            out["third"] = third.get("research") or ""
            # ...and one that is in the news AGAIN inside the two weeks is not proposed again.
            seed(w, [story("Nudge Security opens a London office", importance=5, seen=dl.real_now_ist())])
            s.model.about["Nudge Security opens a London office"] = "Nudge Security"
            fourth = await s.screen(dl.real_today_ist())
            out["fourth"] = fourth.get("research") or ""
            out["fourth_read"] = "Nudge Security opens a London office" in s.model.name_calls[-1]
        with Stage(Model(OCT11, VERDICTS)) as s:
            w = s.w
            seed(w, oct11_stories())
            first = await s.screen(FRI)
            await s.post(first, live=True)
            # TWO WEEKS LATER, NO ANSWER.
            with w.bot.db.conn() as c:
                c.execute("UPDATE news_screen_ledger SET first_at = ?", (feeds.utc_iso(
                    dl.real_now_ist() - timedelta(days=15)),))
            late = await s.screen(dl.real_today_ist())
            out["stale"] = late.get("research") or ""
            out["stale_log"] = [m for _l, m in s.tap.records if "unanswered for 14 days and dropped" in m][-1:]
            # ...and after those two weeks, in the news again: it may be proposed again.
            with w.bot.db.conn() as c:
                c.execute("UPDATE news_screen_ledger SET status = 'stale'")
            seed(w, [story("Mirage Labs signs a telco", importance=4, seen=dl.real_now_ist())])
            s.model.about["Mirage Labs signs a telco"] = "Mirage Labs"
            back = await s.screen(dl.real_today_ist())
            out["returns"] = "Mirage Labs" in (back.get("research") or "")
        return out

    async def through_the_sender():
        """The real planner, research step and sender on a pretend Friday: a test day, end to end."""
        import replies_world as rw
        out = {}
        tags = config.always_tag_ids
        config.always_tag_ids = lambda: [rw.APPROVER]
        try:
            with Stage(Model(OCT11, VERDICTS)) as s:
                w = s.w
                seed(w, oct11_stories())
                w.rules = {"R2"}
                w.pretend_day(FRI, 14, 0)
                planned = await w.bot._plan_drip(today=FRI, already=[])
                msg = next(m for m in (planned or {}).get("messages") or []
                           if m.get("type") == nextaction.R_NEWS_SCREEN)
                await w.bot._research_message(msg, today=FRI)
                before = len(w.posted)
                await w.bot._send_drip_message(w.chan, msg, marker=dl.iso(FRI), channel_id=w.chan.id)
                out["posts"] = [rw.strip_tag(m.content) for m in w.posted[before:]]
                out["state"] = (s.marker(), s.ledger())
                out["said"] = (w.bot._said.get(str(w.posted[-1].id)) or {}).get("kind")
        finally:
            config.always_tag_ids = tags
        return out

    with pinned():
        got = asyncio.run(marker())
        rep = asyncio.run(repeats())
        sent = asyncio.run(through_the_sender())

    lines = (sent["posts"] or [""])[0].split("\n")
    check("through the real sender: ONE message; the heading, the tags line, a blank line, then the list",
          (len(sent["posts"]), lines[0], lines[1].startswith(("<@", "@")), lines[2], lines[3]),
          (1, "**New companies in the news**", True, "",
           "Two companies in today's news aren't on the Master Pipeline yet:"))
    check("...the rest is the agreed message, unchanged", "\n".join(lines[3:]), EXPECTED.split("\n", 1)[1])
    check("...and that test-day send, end to end, left the marker and the ledger untouched",
          (sent["state"], sent["said"]), (("", {}), "news_screen"))

    empty = ("", {})
    check("before any post there is no marker and no ledger", got["before"], empty)
    check("a test day leaves the live marker and the ledger untouched", got["test_day"], empty)
    check("a simulation leaves them untouched", got["simulation"], empty)
    check("a pretend clock, and a post dated any day but today, leave them untouched", got["pretend"], empty)
    check("...and the log says so", len(got["said"]), 1)
    check("a live post moves the marker and records what it asked about",
          got["live"], (True, {"Mirage Labs": "open", "Nudge Security": "open", "Vapi": "open"}))
    check("the marker is written in exactly one place, behind the live gate",
          _marker_writers(), ["_after_send"])

    check("an unanswered company returns in the next message (all three, the same wording)",
          rep["again"], rep["first"])
    check("...with the verdict it already had: nothing is paid for twice", rep["paid_again"], 0)
    check('a reply that chooses one ("add Mirage Labs") drops the others',
          rep["after_reply"], {"Mirage Labs": "open", "Nudge Security": "dropped", "Vapi": "dropped"})
    check("...so the next message does not ask about them again", [n for n in ("Nudge Security", "Vapi")
                                                                     if n in rep["third"]], [])
    check("...not even when the dropped company is in the news again inside the two weeks",
          (rep["fourth_read"], "Nudge Security" in rep["fourth"]), (True, False))
    check("two weeks with no answer: dropped, and nothing is posted for it",
          (rep["stale"], [all(n in m for n in ("Mirage Labs", "Nudge Security", "Vapi")) and
                          "3 unanswered for 14 days and dropped" in m for m in rep["stale_log"]]), ("", [True]))
    check("after the two weeks, a company that is in the news again may be proposed again", rep["returns"], True)


def _marker_writers() -> list:
    src = open(os.path.join(ROOT, "bot.py"), encoding="utf-8").read()
    out = []
    for m in re.finditer(r"set_rule_last_posted\(", src):
        head = src.rfind("\n    async def ", 0, m.start())
        out.append(re.match(r"\n    async def (\w+)", src[head:]).group(1))
    return sorted(set(out))


def replies(check):
    print("\nREPLY — the company name is read out of the reply; nothing is written on a doubt")
    two_nudges = {"Nudge Security raises": "Nudge Security", "Nudge Labs launches": "Nudge Labs",
                  MIRAGE_STORY: "Mirage Labs"}
    verdicts = dict(VERDICTS, **{"Nudge Labs": ("launched a product", "WEAK", "consumer app")})

    async def scenario():
        out = {}
        with Stage(Model(two_nudges, verdicts)) as s:
            w = s.w
            seed(w, [story("Nudge Security raises"), story("Nudge Labs launches"), story(MIRAGE_STORY, importance=4)])
            item = await s.screen(FRI)
            post = await s.post(item, live=True)
            out["message"] = item["research"]

            out["unknown"], _ = await s.say("add Foo Corp", who="approver", reply_to=post)
            out["after_unknown"] = (list(w.sheet.writes), dict(w.proposals()), s.ledger())
            out["ambiguous"], _ = await s.say("add Nudge", who="approver", reply_to=post)
            out["after_ambiguous"] = (list(w.sheet.writes), dict(w.proposals()), s.ledger())
            out["bare"], _ = await s.say("yes", who="approver", reply_to=post)
            out["after_bare"] = (list(w.sheet.writes), dict(w.proposals()))
            out["question"], _ = await s.say("what does Mirage Labs do?", who="member", reply_to=post)
            out["after_question"] = (dict(w.proposals()), s.ledger())

            out["chosen"], msgs = await s.say("yes to Mirage but not Nudge", who="member", reply_to=post)
            out["after_chosen"] = (list(w.sheet.writes), sorted(w.proposals().values()), s.ledger())
            confirm = msgs[-1]
            out["member_yes"], _ = await s.say("yes", who="member", reply_to=confirm)
            out["after_member_yes"] = list(w.sheet.writes)
            out["approver_yes"], _ = await s.say("yes", who="approver", reply_to=confirm)
            out["writes"] = [x for x in w.sheet.writes if x[0] == "append_row"]
            out["final"] = (sorted(w.proposals().values()), s.ledger())
        return out

    with pinned():
        got = asyncio.run(scenario())

    open_all = {"Mirage Labs": "open", "Nudge Labs": "open", "Nudge Security": "open"}
    check("a name that was not in the message is refused with an explanation",
          got["unknown"], ["I didn't recognise \"foo corp\": it wasn't one of the companies in my message, "
                           "so I've left it."])
    check("...and nothing is written, nothing is proposed, nothing is dropped",
          got["after_unknown"], ([], {}, open_all))
    check('"add Nudge" with two Nudges asks which',
          got["ambiguous"], ["\"nudge\" could be Nudge Security and Nudge Labs. Which one did you mean? "
                             "I haven't added anything for it."])
    check("...and writes nothing, proposes nothing, drops nothing", got["after_ambiguous"], ([], {}, open_all))
    check("a bare yes with three companies in the message asks which, by name",
          (got["bare"], got["after_bare"]),
          (["Which would you like me to add: Mirage Labs, Nudge Security and Nudge Labs? Please reply with the "
            "company's name."], ([], {})))
    check("a question about a company is not an answer: nothing is proposed or dropped",
          got["after_question"], ({}, open_all))
    check('"yes to Mirage but not Nudge" is read by name: Mirage Labs, said back, and asked once more',
          got["chosen"], ["I understood: add Mirage Labs to the Master Pipeline. Shall I go ahead?"])
    check("...nothing is written yet; one proposal is open; the companies not chosen are dropped",
          got["after_chosen"], ([], ["open"], {"Mirage Labs": "open", "Nudge Labs": "dropped",
                                               "Nudge Security": "dropped"}))
    check("a yes from somebody who is not an approver writes nothing", got["after_member_yes"], [])
    check("an approver's yes writes ONE row, through the proposal", len(got["writes"]), 1)
    check("...and the company is closed as added", got["final"],
          (["applied"], {"Mirage Labs": "added", "Nudge Labs": "dropped", "Nudge Security": "dropped"}))
    check("the reply reader never goes through approvals.read_vote",
          "read_vote" in open(os.path.join(ROOT, "news_screen.py"), encoding="utf-8").read().split(
              "def read_reply")[1].split("def confirm_text")[0].replace("`approvals.read_vote`", ""), False)


def settings(check):
    print("\nSETTINGS — the per-company news search, and the contract")
    example = {}
    for line in open(os.path.join(ROOT, ".env.example"), encoding="utf-8"):
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            example[k.strip()] = v.strip()
    import ast
    src = open(os.path.join(ROOT, "config.py"), encoding="utf-8").read()

    def default(name):
        m = re.search(rf'{name} = _int\("{name}", (\d+)\)', src)
        return int(m.group(1)) if m else None
    check("NEWS_POC_TARGETS_PER_DAY is 40 in the code (not an env override) and in .env.example",
          (default("NEWS_POC_TARGETS_PER_DAY"), example.get("NEWS_POC_TARGETS_PER_DAY")), (40, "40"))
    check("382 names at 40 a day come round inside the 10 days the ticket asks for", -(-382 // 40), 10)
    check("the two new settings are in the contract with the code's defaults",
          (default("NEWS_SCREEN_STALE_DAYS"), example.get("NEWS_SCREEN_STALE_DAYS"),
           default("NEWS_SCREEN_FIRST_RUN_DAYS"), example.get("NEWS_SCREEN_FIRST_RUN_DAYS")), (14, "14", 7, "7"))
    del ast


def run(check):
    with pinned():
        matching(check)
    window(check)
    known_list(check)
    outcomes_and_message(check)
    marker_and_repeats(check)
    replies(check)
    settings(check)
