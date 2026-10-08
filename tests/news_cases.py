"""THE NEWS TEMPLATE AND THE NEWS ANSWER, END TO END (docs/plans/NEWS-OCT8.md).

Scenarios shared by tests/test_news_format.py, verify_news_format.py and the
8 Oct section of verify_replay_oct6.py (tests/replay_news.py). Each one runs a
whole bot behind fake Discord (tests/replies_world.py): the real `on_message`,
the real router gate, the real `_news_answer`, `news.choose_answer`,
`news.render`, the real planner and `_send_drip_message`.

OFFLINE. A throwaway database with made-up stories, no feed fetched (the poll
is stubbed), no search, no model — the engine's model is a script that records
what it was asked, and the light scorer is the world's FakeLLM, which counts
any call it gets. Every setting is pinned by the world to .env.example.

A PINNED DAY: Wed 7 Oct 2026, with every story stamped on that day, so the
result is the same whenever this runs. (The 8 Oct live test ran with exactly
that leftover pretend day, at 11:22.)
"""
import re
from datetime import date, datetime, time, timedelta

import replies_world as rw

import config
import deadlines as dl
import feeds
import news
import nextaction

DAY = date(2026, 10, 7)                 # a Wednesday; R1 runs
HEADING = "**AI News, Wed 7 Oct**"
# What an answer must never say (the human, 8 Oct), matched as whole words.
BANNED = ("2 PM", "already", "scheduled", "since", "ran")


# THE SHIPPED DEFAULTS (.env.example), pinned: the laptop's own .env has the
# follow-up switched off and a different bar, and a test must not inherit that.
NEWS_PINS = {
    "NEWS_MAX_ITEMS": 5, "NEWS_POC_SLOTS": 2, "NEWS_REPEAT_DAYS": 30,
    "NEWS_FEED_KEEP_DAYS": 14, "NEWS_PER_TOPIC_PER_DAY": 2, "NEWS_TOPICS_PER_WEEK": 6,
    "NEWS_BREAKING_MIN_IMPORTANCE": 5, "NEWS_OFFTOPIC_BYPASS_IMPORTANCE": 4,
    "NEWS_OVERFLOW_ENABLED": True, "NEWS_OVERFLOW_MIN_IMPORTANCE": 5,
    "NEWS_OVERFLOW_MAX_ITEMS": 5, "NEWS_SCORE_MAX_ITEMS": 40,
    "TOKEN_DAILY_BUDGET": 0,
}


def banned_in(text: str) -> list:
    return [w for w in BANNED
            if re.search(r"(?<![A-Za-z0-9])" + re.escape(w) + r"(?![A-Za-z0-9])",
                         str(text or ""), re.IGNORECASE)]


def at(hh: int, mm: int = 0) -> datetime:
    return datetime.combine(DAY, time(hh, mm), dl.IST)


def item(title: str, when: datetime, *, ref: str = "", source: str = "Reuters",
         google: bool = True, summary: str = "") -> dict:
    """One feed item as `feeds.parse` stores it. A Google News link by default,
    so the outlet shown is the item's `source`, not its host."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    url = ("https://news.google.com/rss/articles/" + slug + "?oc=5") if google \
        else ("https://example.org/" + slug)
    return {"url": url, "url_key": news.url_key(url),
            "headline_key": news.headline_key(title), "title": title,
            "summary": summary, "source": source,
            "published_at": feeds.utc_iso(when), "topic_hint": "",
            "seen_at": feeds.utc_iso(when),
            "kind": feeds.KIND_POC if ref else feeds.KIND_INDUSTRY, "sheet_ref": ref}


def seed(world, rows: list) -> list:
    """rows: [(item, importance)]. Stored and rated, so no scoring call is owed."""
    db = world.bot.db
    db.news_feed_add([it for it, _imp in rows])
    db.news_feed_set_scores(
        [{"url_key": it["url_key"], "importance": imp, "topic": "OTHER",
          "what": "what happened"} for it, imp in rows],
        scored_at=feeds.utc_iso(at(13, 30)))
    return [it for it, _imp in rows]


def real_news(world) -> None:
    """The world stands in for todays_news and the breaking check; these
    scenarios are about the real ones."""
    world.bot.__dict__.pop("_news_tools", None)


def bullets(text: str) -> list:
    return [ln for ln in str(text or "").split("\n") if ln.startswith("- ")]


def headlines(text: str) -> list:
    """The headline of each bullet, industry (bold) or PoC (plain, to the dash)."""
    out = []
    for ln in bullets(text):
        m = re.match(r"^- \*\*(.+?)\*\* \(\[", ln)
        out.append(m.group(1) if m else ln[2:].split(" — ")[0])
    return out


def recorded(world) -> dict:
    with world.bot.db.conn() as c:
        return {r[0]: r[1] for r in c.execute(
            "SELECT title, kind FROM news_stories ORDER BY rowid").fetchall()}


NINE = [("Regulator opens an inquiry into model evals", 3), ("Frontier lab ships a reasoning model", 5),
        ("Chipmaker unveils an inference accelerator", 4), ("University publishes an RLHF study", 3),
        ("Cloud provider cuts inference prices", 5), ("Startup releases an open speech dataset", 4),
        ("Consortium drafts an agent safety standard", 3), ("Search engine adds an agent mode", 4),
        ("Open weights model tops a coding benchmark", 5)]


def nine_items() -> list:
    """Nine stories, 08:00 to 12:00, the last one the newest."""
    return [(item(title, at(8) + timedelta(minutes=30 * n)), imp)
            for n, (title, imp) in enumerate(NINE)]


def _mode(world) -> dict:
    return {"model": world.model.calls, "router": world.llm.parse_calls,
            "extractor": world.llm.extract_calls, "scorer": world.llm.other_calls,
            "sheet_reads": world.sheet.reads}


async def plain_three_times(test_mode: bool, question: str = "any AI news?") -> dict:
    """The same plain question three times: 5 new, then the 4 left, then — with
    nothing new left — the top 5 again."""
    with rw.World(test_mode=test_mode, pins=NEWS_PINS, pretend=(DAY, 11, 22)) as w:
        real_news(w)
        seed(w, nine_items())
        out = {"answers": [], "records": [], "calls": [], "tagged": []}
        for _ in range(3):
            n = w.n_posted
            await w.say(question, mention=True)
            new = w.replies_after(n)
            out["answers"].append(new)
            out["tagged"].append([m.content.startswith("[TEST") for m in w.posted[n:]])
            out["records"].append(dict(recorded(w)))
            out["calls"].append(_mode(w))
        out["routes"] = w.routes()
        return out


async def topic_question(test_mode: bool) -> dict:
    """"any news on ElevenLabs?" — the engine, with the same rendered list. The
    model tries to narrate the schedule; none of it is sent."""
    model = rw.ScriptModel()
    model.script = [("use", [("todays_news", {"topic": "ElevenLabs"})]),
                    ("say", "All of today's stories were already posted at 2 PM. "
                            "Here's what ran:\n- ElevenLabs something — VOI.ID")]
    with rw.World(test_mode=test_mode, pins=NEWS_PINS, pretend=(DAY, 11, 22), model=model) as w:
        real_news(w)
        seed(w, nine_items() + [
            (item("ElevenLabs launches a Hindi voice agent", at(10, 10)), 4),
            (item("Voice startup closes a round", at(9, 40),
                  summary="Investors back an ElevenLabs rival in India"), 3)])
        await w.say("any news on ElevenLabs?", mention=True)
        return {"replies": w.replies_after(0), "calls": _mode(w),
                "handed": [r for name, r in w.model.results if name == "todays_news"],
                "records": recorded(w), "routes": w.routes(),
                "offered": [list(o) for o in w.model.offered]}


class _TwoTools(rw.ScriptModel):
    """Calls todays_news and ONE other tool it was offered, then says a line."""

    def create(self, **kw):
        if self.calls == 0:
            names = [t.get("name") for t in (kw.get("tools") or []) if t.get("name")]
            other = next((n for n in names if n not in ("todays_news", "web_search",
                                                        "fetch_page")), "")
            use = [("todays_news", {"topic": "ElevenLabs"})] + ([(other, {})] if other else [])
            self.script = [("use", use), ("say", "On the sheet they are at the DM stage.")]
            self.other = other
        return super().create(**kw)


async def topic_and_something_else(test_mode: bool) -> dict:
    """The list, then what the model found with another tool."""
    model = _TwoTools()
    with rw.World(test_mode=test_mode, pins=NEWS_PINS, pretend=(DAY, 11, 22), model=model) as w:
        real_news(w)
        seed(w, [(item("ElevenLabs launches a Hindi voice agent", at(10, 10)), 4)])
        await w.say("where are we with ElevenLabs, and any news on them?", mention=True)
        return {"replies": w.replies_after(0), "other": getattr(model, "other", ""),
                "calls": _mode(w), "records": recorded(w)}


async def quiet(test_mode: bool) -> dict:
    with rw.World(test_mode=test_mode, pins=NEWS_PINS, pretend=(DAY, 11, 22)) as w:
        real_news(w)
        await w.say("any AI news?", mention=True)
        return {"replies": w.replies_after(0), "calls": _mode(w), "records": recorded(w)}


# -- the daily post, the follow-up, and what an earlier answer already gave --------------

ASKED = [f"{w} lab announces a frontier model" for w in ("Alpha", "Bravo", "Charlie", "Delta", "Echo")]
MAJOR = [f"{w} raises a billion dollar round" for w in (
    "Foxtrot", "Golf", "Hotel", "India", "Juliet", "Kilo", "Lima", "Mike", "November",
    "Oscar", "Papa")]
MIDDLING = ["Quebec publishes a tooling guide", "Romeo opens a regional office"]
POC = "Synthflow raises $20M Series A"
POC_REF = "Synthflow AI — on Master Pipeline"


def daily_items() -> list:
    rows = []
    for n, title in enumerate(MAJOR):                       # 08:00 ... the oldest first
        rows.append((item(title, at(8) + timedelta(minutes=5 * n)), 5))
    for n, title in enumerate(MIDDLING):
        rows.append((item(title, at(9, 30) + timedelta(minutes=5 * n)), 4))
    rows.append((item(POC, at(10), ref=POC_REF, source="Inc42", google=False), 4))
    for n, title in enumerate(ASKED):                       # the newest five 5s
        rows.append((item(title, at(12) + timedelta(minutes=5 * n)), 5))
    return rows


async def answer_then_daily_post(test_mode: bool) -> dict:
    """Somebody asks at 13:55; the 14:00 post and its follow-up must not carry
    what that answer gave."""
    with rw.World(test_mode=test_mode, pins=NEWS_PINS, pretend=(DAY, 13, 55), rules={"R1"}) as w:
        real_news(w)
        seed(w, daily_items())
        await w.say("any AI news?", mention=True)
        answer = w.replies_after(0)
        n = w.n_posted
        planned = await w.bot._plan_drip(today=DAY, already=[])
        msg = next((m for m in (planned or {}).get("messages") or []
                    if m.get("type") == nextaction.R_AI_NEWS), None)
        assert msg is not None, "the planner made no R1 post for the seeded stories"
        # The sweep runs when the post is about to go (`_research_message`), as
        # `_maybe_send_drip` does it: feed store, choose_main, render.
        await w.bot._research_message(msg, today=DAY)
        await w.bot._send_drip_message(w.chan, msg, marker=dl.iso(DAY), channel_id=w.chan.id)
        sent = w.replies_after(n)
        raw = [m.content for m in w.posted[n:]]
        after = w.n_posted
        await w.say("any AI news?", mention=True)
        return {"answer": answer, "posts": sent, "raw": raw, "next_answer": w.replies_after(after),
                "records": recorded(w), "calls": _mode(w),
                "prefix": config.SIMULATION_PREFIX}


def same(live: dict, test: dict, keys) -> list:
    """The keys on which live and test mode differ ([] when they agree)."""
    return [k for k in keys if live.get(k) != test.get(k)]
