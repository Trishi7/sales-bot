"""THE CHECKS for NEWS-PATCH-OCT16 "the Gemini 4 miss", written once: T1 to T14.

`run(check)` is called by verify_news_patch.py (which prints the T1-T14 table)
and each group by tests/test_news_patch.py (which asserts). A check that IS one
of the ticket's test cases is named "T<n> <case>"; the ticket's edge cases are
named "edge <n> ...".

OFFLINE, on the world of tests/news_v2_checks.py: a whole bot behind fake
Discord on a throwaway database, canned feeds (`Net`), a stand-in scorer that
answers from a table and records every call, and a cycle handed the real "now"
it should stand at. No feed is fetched, no model is called.

THE PATCH'S DEFAULTS ARE PINNED HERE (`PATCH`), on top of the NFT2-1087 pins
— which turn the patch's behaviour off so the older checks keep testing what
they were written for.
"""
import asyncio
import logging
import os
import re
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone

import config
import deadlines as dl
import feeds
import news
import news_v2_checks as v2

ROOT = v2.ROOT
TUE, WED = v2.TUE, v2.WED
ist, utc, link, G = v2.ist, v2.utc, v2.link, v2.G

PREFERRED = ["techcrunch.com", "reuters.com", "theverge.com"]

# THE PATCH'S SHIPPED DEFAULTS (.env.example), pinned over the NFT2-1087 ones.
PATCH = {
    "NEWS_CYCLE_LATE_SKIP_HOURS": 3, "NEWS_BREAKING_MAX_PER_DAY": 4,
    "NEWS_BREAKING_MIN_OUTLETS": 2, "NEWS_BREAKING_MAX_PER_CYCLE": 1,
    "NEWS_SCORE_MAX_ITEMS": 40, "NEWS_SCORE_MAX_CALLS": 3, "NEWS_NAME_FLOOD_MAX": 30,
    "NEWS_NAME_MIN_CHARS": 4, "NEWS_CYCLE_MAX_SECONDS": 600, "NEWS_HEALTH_TIME": "23:45",
    "NEWS_PREFERRED_DOMAINS": list(PREFERRED), "COS_FOLLOWUP_CHECK_INTERVAL_MINUTES": 15,
    # The health line's test channel is NOT the sales channel here, so a post
    # to the wrong one shows.
    "SALES_CHANNEL_IDS": [v2.rw.CHANNEL, v2.rw.OTHER_CHANNEL],
    "SALES_CHANNEL_ID_SET": {v2.rw.CHANNEL, v2.rw.OTHER_CHANNEL},
    "SALES_TEST_CHANNEL_ID": v2.rw.OTHER_CHANNEL,
}

# Titles that are different stories to every key: digits are dropped from the
# keys, so they differ by WORDS (two lists, one word from each).
_A = ("amber", "basil", "cedar", "dahlia", "ember", "fennel", "garnet", "hazel", "iris",
      "juniper", "kestrel", "linden", "maple", "nectar", "olive", "pepper", "quartz",
      "rowan", "saffron", "tamarind")
_B = ("anchor", "beacon", "canyon", "delta", "estuary", "fjord", "glacier", "harbor",
      "island", "jetty", "knoll", "lagoon")


def filler(n: int) -> list:
    """`n` made-up industry headlines, every one its own story."""
    out = []
    for b in _B:
        for a in _A:
            out.append(f"{a.title()} {b} startup expands quietly")
            if len(out) == n:
                return out
    raise ValueError("not enough words for %d titles" % n)


class Logs(v2.Logs):
    """`v2.Logs`, with the root logger at INFO while it is open (the runs keep
    it at ERROR)."""

    def __enter__(self):
        self._level = logging.getLogger().level
        logging.getLogger().setLevel(logging.INFO)
        return super().__enter__()

    def __exit__(self, *exc):
        logging.getLogger().setLevel(self._level)
        return super().__exit__(*exc)


class Bot(v2.Bot):
    """The NFT2-1087 world with the patch's defaults pinned, and the bot's
    `get_channel` answering for the two fake channels."""

    def __init__(self, **kw):
        pins = dict(PATCH, **(kw.pop("pins", None) or {}))
        names = kw.pop("names", None)
        super().__init__(names=v2.base_names() if names is None else names, pins=pins, **kw)

    def __enter__(self):
        super().__enter__()
        chans = {self.w.chan.id: self.w.chan, self.w.other.id: self.w.other}
        self.w.bot.get_channel = lambda cid: chans.get(int(cid or 0))
        return self


def seed_unscored(b, titles, *, seen: datetime, source: str = "Reuters",
                  hint: str = "evals") -> None:
    b.seed([{"title": t, "when": seen, "seen": seen, "source": source} for t in titles])
    if hint:
        with b.db.conn() as c:
            c.executemany("UPDATE news_feed_items SET topic_hint = ? WHERE title = ?",
                          [(hint, t) for t in titles])


def feed_item(title: str, *, url: str, source: str, seen: datetime, flag: int = 0,
              names: str = "", hint: str = "", rank: int = 3, outlets: int = 1) -> dict:
    return {"url": url, "url_key": news.url_key(url),
            "headline_key": news.headline_key(title), "story_key": news.story_key(title),
            "title": title, "summary": "", "source": source,
            "published_at": feeds.utc_iso(seen), "seen_at": feeds.utc_iso(seen),
            "topic_hint": hint, "kind": "poc" if flag else "industry", "sheet_ref": "",
            "names": names, "flag": flag, "source_rank": rank, "outlet_count": outlets}


# =============================================================================
# F1 — ONE STORY, MANY OUTLETS (T1-T3)
# =============================================================================

REWORDED = ["Google launches Gemini 4 Aragon",
            "Gemini 4 Aragon: Google launches",
            "Google Launches 'Aragon' Gemini 4",
            "Aragon — Google's Gemini 4 launches",
            "GEMINI 4 ARAGON, GOOGLE LAUNCHES"]


async def story_keys(check) -> None:
    print("\nF1 ONE STORY, MANY OUTLETS (T1-T3)")
    keys = {news.story_key(t) for t in REWORDED}
    check("T1 story_key on 5 reworded Gemini headlines -> one key for all 5",
          (len(keys), sorted(keys)[0]), (1, "aragon gemini google launches"))
    check("T1 ...where the old headline key saw several stories",
          len({news.headline_key(t) for t in REWORDED}) > 1, True)
    check("T1 ...the key keeps 12 tokens, sorted",
          news.story_key("zulu yankee xray whiskey victor uniform tango sierra romeo "
                         "quebec papa oscar november mike"),
          " ".join(sorted("zulu yankee xray whiskey victor uniform tango sierra romeo "
                          "quebec papa oscar".split())))

    now = utc(TUE, 14)
    a = {"title": "Gemini 4 launched", "seen_at": feeds.utc_iso(now)}
    b = {"title": "Gemini 4 fails safety test", "seen_at": feeds.utc_iso(now)}
    check("T2 same_story on 'Gemini 4 launched' vs 'Gemini 4 fails safety test' -> False",
          news.same_story(a, b), False)
    check("edge 2 ...and with two shared names (Google, Gemini) still two stories",
          news.same_story({"title": "Google launches Gemini 4 Aragon", "seen_at": a["seen_at"]},
                          {"title": "Google's Gemini 4 Aragon fails safety test",
                           "seen_at": a["seen_at"]}), False)
    check("T2 ...two names and most of the words -> one story",
          news.same_story({"title": "Google unveils Gemini 4 Aragon model",
                           "seen_at": a["seen_at"]},
                          {"title": "Google Gemini 4 Aragon model unveiled today",
                           "seen_at": a["seen_at"]}), True)
    check("T2 ...but not 36 hours apart",
          news.same_story({"title": REWORDED[0], "seen_at": feeds.utc_iso(now)},
                          {"title": REWORDED[1],
                           "seen_at": feeds.utc_iso(now + timedelta(hours=37))}), False)

    # T3: 29 copies, as on 14 Oct — five wordings, 29 outlets, the 10th from a
    # preferred domain; two of them only `same_story` can join.
    import db as dbmod

    tmp = tempfile.mkdtemp(prefix="saley-patch-")
    saved = config.NEWS_PREFERRED_DOMAINS
    config.NEWS_PREFERRED_DOMAINS = list(PREFERRED)
    try:
        store = dbmod.DB(os.path.join(tmp, "t.db"))
        items = []
        for i in range(29):
            seen = now + timedelta(minutes=i)
            if i == 9:
                url, source = "https://techcrunch.com/2026/10/14/gemini-4-aragon", "TechCrunch"
            else:
                url, source = f"{G}gemini-copy-{i}?oc=5", f"Outlet {_A[i % 20]} {i // 20}"
            title = REWORDED[i % 5]
            if i in (27, 28):
                title = ("Google unveils Gemini 4 Aragon launches model" if i == 27
                         else "Google Gemini 4 Aragon launches, model unveiled")
            items.append(feed_item(title, url=url, source=source, seen=seen))
        stats: dict = {}
        added = store.news_feed_add(items, stats=stats)
        with store.conn() as c:
            rows = [dict(r) for r in c.execute("SELECT * FROM news_feed_items").fetchall()]
        row = rows[0] if rows else {}
        check("T3 news_feed_add with 29 Gemini copies -> 1 row, outlet_count 29",
              (added, len(rows), row.get("outlet_count"), stats.get("merged")),
              (1, 1, 29, 28))
        check("T3 ...best_url from a preferred domain when present; the row shows it",
              (row.get("best_url"), row.get("url"), row.get("source"),
               row.get("url_key") == items[0]["url_key"]),
              (items[9]["url"], items[9]["url"], "TechCrunch", True))
        check("T3 ...every copy is listed once in `outlets`",
              len([p for p in row.get("outlets", "").split(",") if p]), 29)
        again = store.news_feed_add(items, stats=stats)
        with store.conn() as c:
            n = c.execute("SELECT outlet_count FROM news_feed_items").fetchall()
        check("T3 ...the same 29 links an hour later are not new copies",
              (again, [r[0] for r in n], stats.get("merged")), (0, [29], 0))
        other = feed_item("Gemini 4 fails safety test", url=f"{G}gemini-safety?oc=5",
                          source="Wired", seen=now + timedelta(hours=1))
        store.news_feed_add([other])
        check("edge 2 'Gemini 4 fails safety test' the same day -> a second row",
              store.news_feed_count(), 2)
        old = feed_item("Big lab ships a model", url="https://x.example/old", source="X",
                        seen=now)
        old["story_key"] = ""
        with store.conn() as c:
            c.execute("INSERT INTO news_feed_items (url_key, title) VALUES (?, ?)",
                      ("x.example/backfill", "Big lab ships a model"))
            c.execute("DELETE FROM meta WHERE key = ?", ("news_feed_story_key_backfill",))
        logs = Logs()
        with logs:
            filled = store.news_feed_backfill_story_keys(batch=500)
            twice = store.news_feed_backfill_story_keys(batch=500)
        with store.conn() as c:
            key = c.execute("SELECT story_key FROM news_feed_items WHERE url_key = ?",
                            ("x.example/backfill",)).fetchone()[0]
        check("edge 13 the story-key backfill fills old rows once, and says so once",
              (filled, twice, key, sum(1 for _l, m in logs.lines if "back-filled" in m)),
              (1, 0, "big lab model ships", 1))
    finally:
        config.NEWS_PREFERRED_DOMAINS = saved

    # edge 13 at size: 20,000 rows, in batches, within one start.
    big = dbmod.DB(os.path.join(tmp, "big.db"))
    with big.conn() as c:
        c.executemany("INSERT INTO news_feed_items (url_key, title) VALUES (?, ?)",
                      [(f"x.example/{i}", f"{_A[i % 20]} {_B[i % 12]} story number")
                       for i in range(20000)])
        c.execute("DELETE FROM meta WHERE key = ?", ("news_feed_story_key_backfill",))
    t0 = datetime.now()
    filled = big.news_feed_backfill_story_keys()
    with big.conn() as c:
        blank = c.execute("SELECT COUNT(*) FROM news_feed_items WHERE story_key = ''"
                          ).fetchone()[0]
    check("edge 13 backfill of story_key on a 20,000-row table -> all filled in one start",
          (filled, blank, (datetime.now() - t0).total_seconds() < 120), (20000, 0, True))


# =============================================================================
# F2 — FLAG DISCIPLINE (T4, T5)
# =============================================================================


async def flags(check) -> None:
    print("\nF2 FLAG DISCIPLINE (T4, T5)")
    feeds._SHORT_LOGGED.clear()
    logs = Logs()
    with logs:
        names = v2.names_from(pipeline=["Ola", "PolyAI"],
                              pocs=[{"company": "PolyAI", "name": "Bo"}])
        v2.names_from(pipeline=["Ola"])
    keys = [e["name"] for e in names]
    index = feeds.NameIndex(names)
    item = index.tag({"title": "Ola raises a new round", "summary": "Ola, the startup"})
    check("T4 NAME_MIN_CHARS=4 -> a 3-letter name never flags",
          (feeds.NAME_MIN_CHARS, "Ola" in keys, item["flag"], item["names"]),
          (4, False, 0, ""))
    check("T4 ...logged once as too short to match",
          sum(1 for _l, m in logs.lines if "'Ola' is too short to match" in m), 1)
    check("edge 3 ...a 4-letter generic name (Cleo) is still a name",
          "Cleo" in [e["name"] for e in v2.names_from(pipeline=["Cleo"])], True)

    index = feeds.NameIndex(v2.names_from(pipeline=["PolyAI"]), flood_max=30)
    tagged = [index.tag({"title": f"Voice market story {_A[i % 20]} {_B[i // 20]}",
                         "summary": "PolyAI was mentioned in passing"}) for i in range(31)]
    titled = index.tag({"title": "PolyAI raises a round", "summary": ""})
    check("T5 flood cap: hit 31 -> 30 flagged normally, the 31st title-only",
          ([t["flag"] for t in tagged].count(1), tagged[30]["flag"], titled["flag"]),
          (30, 0, 1))
    check("T5 ...and the name is in noisy_names", index.noisy, ["PolyAI"])

    # The same through a real pull: the cycle and its row carry the noisy name.
    now = ist(TUE, 14, 0)
    flood = [{"title": f"Voice market story {_A[i % 20]} {_B[i // 20]}",
              "when": now - timedelta(minutes=10), "source": "Reuters",
              "summary": "PolyAI was mentioned in passing"} for i in range(35)]
    with Bot() as b, v2.Net([('"PolyAI"', flood)]):
        out = await b.cycle(now)
        cyc = b.cycles(TUE)[0]
        check("T5 ...a pull lists it under noisy_names, and the cycle's row keeps it",
              (out["pull"]["noisy_names"], cyc["noisy"], out["pull"]["flagged"]),
              (["PolyAI"], "PolyAI", 30))


# =============================================================================
# F3 — SCORE EVERYTHING THAT MATTERS (T6-T8)
# =============================================================================


class GatedScorer(v2.Scorer):
    """A scorer that fails from call `fail_from` on, or waits on `gate`."""

    def __init__(self, scores=None, *, fail_from=None, gate=None):
        super().__init__(scores)
        self.fail_from = fail_from
        self.gate = gate
        self.entered = asyncio.Event()

    async def __call__(self, *, items, topics, today, mode="main"):
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.fail_from is not None and len(self.shown) + 1 >= self.fail_from:
            self.shown.append([i["title"] for i in items])
            return None
        return await super().__call__(items=items, topics=topics, today=today, mode=mode)


async def scoring(check) -> None:
    print("\nF3 SCORE EVERYTHING THAT MATTERS (T6-T8)")
    t = "2026-10-13T08:%02d:00+00:00"
    rows = [
        {"title": "Rest story", "url_key": "r", "topic_hint": "", "published_at": t % 59,
         "flag": 0, "names": "", "outlet_count": 1},
        {"title": "Topic story", "url_key": "t", "topic_hint": "evals",
         "published_at": t % 50, "flag": 0, "names": ""},
        {"title": "Topic story copy", "url_key": "t2", "story_key": news.story_key("Topic story"),
         "topic_hint": "evals", "published_at": t % 40, "flag": 0, "names": ""},
        {"title": "Two outlet story", "url_key": "o", "topic_hint": "",
         "published_at": t % 10, "flag": 0, "names": "", "outlet_count": 2},
        {"title": "Company story old", "url_key": "c1", "topic_hint": "",
         "published_at": t % 1, "flag": 1, "names": "PolyAI", "kind": "poc"},
        {"title": "Company story new", "url_key": "c2", "topic_hint": "",
         "published_at": t % 5, "flag": 1, "names": "PolyAI", "kind": "poc"},
        {"title": "Person story", "url_key": "p", "topic_hint": "", "published_at": t % 0,
         "flag": 1, "names": "PolyAI, Daisy Leigh", "kind": "poc"},
    ]
    for r in rows:
        r.setdefault("story_key", news.story_key(r["title"]))
        r.setdefault("headline_key", news.headline_key(r["title"]))
    got = [r["url_key"] for r in news.preselect(rows, cap=40, people={"daisy leigh"})]
    check("T6 preselect order: people-flagged, company-flagged, outlet 2+, topics, rest; "
          "story_key duplicates collapse (the newer copy stays)", got,
          ["p", "c2", "c1", "o", "t", "r"])
    check("T6 ...an item an earlier cycle could not reach goes first",
          [r["url_key"] for r in news.preselect(
              [dict(r, seen_at="2026-10-13T09:00:00+00:00") for r in rows[:1]]
              + [dict(r, seen_at="2026-10-13T08:00:00+00:00") for r in rows[1:2]],
              cap=40, carried_before="2026-10-13T08:30:00+00:00")], ["t", "r"])

    now = ist(TUE, 14, 0)
    for n, want_calls, want_carried in ((120, 3, 0), (200, 3, 80)):
        titles = filler(n)
        logs = Logs()
        with Bot() as b, v2.Net(), logs:
            seed_unscored(b, titles, seen=now - timedelta(minutes=20))
            out = await b.cycle(now)
            scored = sum(1 for r in b.rows() if int(r["importance"] or 0) > 0)
            check(f"T7 {n} unscored -> {want_calls} calls of 40, {want_carried} carried",
                  (b.scorer.calls(), [len(x) for x in b.scorer.shown],
                   out["score"]["carried"], scored),
                  (want_calls, [40, 40, 40], want_carried, n - want_carried))
            if want_carried:
                check("T7 ...logged as carried",
                      logs.has("carried 80", "NEWS_SCORE_MAX_CALLS"), True)
                left = {r["title"] for r in b.rows() if not int(r["importance"] or 0)}
                b.scorer.shown.clear()
                await b.cycle(ist(TUE, 15, 0))
                check("T7 ...and the next cycle scores them first",
                      (set(b.scorer.shown[0]) <= left, b.scorer.calls(),
                       sum(1 for r in b.rows() if not int(r["importance"] or 0))),
                      (True, 2, 0))

    # edge 6: the model call fails on batch 2.
    titles = filler(120)
    with Bot() as b, v2.Net():
        b.scorer = GatedScorer(fail_from=2)
        b.w.llm.score_news = b.scorer
        seed_unscored(b, titles, seen=now - timedelta(minutes=20))
        out = await b.cycle(now)
        scored = sum(1 for r in b.rows() if int(r["importance"] or 0) > 0)
        check("edge 6 the model call fails on batch 2 -> batch 1's scores stand; 2 and 3 "
              "carried; the cycle is 'unscored'",
              (scored, out["score"]["carried"], b.cycles(TUE)[0]["status"]),
              (40, 80, "unscored"))

    launch = "Google launches Gemini 4 Aragon"
    review = "Gemini 4 review: is it worth it?"
    check("T8 launch_rule: 'Google launches Gemini 4 Aragon' -> True; 'Gemini 4 review' "
          "-> False",
          (news.launch_rule({"title": launch}), news.launch_rule({"title": review})),
          (True, False))
    check("T8 ...the o-series, 'new model', and a word that only contains a name",
          (news.launch_rule({"title": "OpenAI releases o5 to all users"}),
           news.launch_rule({"title": "DeepSeek ships a new model for coding"}),
           news.launch_rule({"title": "ChatGPTs everywhere: a launch party"}),
           news.launch_rule({"title": "O2 launches a new tariff"})),
          (True, True, False, False))
    with Bot() as b, v2.Net():
        seed_unscored(b, [launch, review], seen=now - timedelta(minutes=20))
        out = await b.cycle(now)
        row = b.row(launch)
        check("T8 'Google launches Gemini 4 Aragon' -> 5 by rule, scored_by 'rule', never "
              "sent to the model",
              (row.get("importance"), row.get("scored_by"), row.get("topic"),
               row.get("what"), b.scorer.saw(launch)),
              (5, "rule", "frontier model", launch, False))
        check("T8 'Gemini 4 review' -> goes to the model like any item",
              (b.scorer.saw(review), b.row(review).get("scored_by")), (True, "model"))
        check("edge 7 ...and it is breaking with one outlet",
              [s["headline"] for s in out["kept"]], [launch])


# =============================================================================
# F4 — THE BREAKING BUDGET (T9, T10)
# =============================================================================


def ours(title: str, when: datetime) -> dict:
    return {"title": title, "when": when, "seen": when, "imp": 5, "flag": 1,
            "names": "PolyAI", "rank": 1}


async def budget(check) -> None:
    print("\nF4 THE BREAKING BUDGET (T9, T10)")
    stories = ["PolyAI acquires a speech startup", "PolyAI names a new chief executive",
               "PolyAI raises a mega round", "PolyAI closes its Berlin office",
               "PolyAI buys an annotation firm"]
    with Bot() as b, v2.Net():
        outs = []
        for i, title in enumerate(stories):
            at = ist(TUE, 14 + i, 0)
            b.seed([ours(title, at - timedelta(minutes=5))])
            outs.append(await b.cycle(at))
        check("T9 breaking per cycle 1, per day 4 -> four posts, one story each",
              [[s["headline"] for s in o["kept"]] for o in outs[:4]],
              [[t] for t in stories[:4]])
        check("T9 ...the 5th candidate is held: not posted, not recorded, budget spent",
              (outs[4]["posted"], outs[4]["budget_spent"],
               stories[4] in b.recorded(), b.db.news_breaking_count_on(dl.iso(TUE))),
              (0, True, False, 4))
        check("edge 9 ...the hold is logged",
              any("NEWS_BREAKING_MAX_PER_DAY" in line for line in outs[4]["skipped"]), True)
        held = [s for s in outs[4]["held"]]
        check("edge 9 ...and the held story is still a candidate for the daily post",
              ([s["headline"] for s in held], b.row(stories[4]).get("importance")),
              ([stories[4]], 5))
        stats = b.w.bot._news_health_stats(dl.iso(TUE))
        check("edge 9 the health line says when the budget was spent (the 17:00 cycle)",
              (stats["breaking"], stats["budget"], stats["spent_at"]), (4, 4, "17:00"))
        b.seed([ours("PolyAI opens a Delhi office", ist(WED, 13, 55))])
        nxt = await b.cycle(ist(WED, 14, 0))
        check("T9 ...counts reset at IST midnight: the next day posts again",
              ([s["headline"] for s in nxt["kept"]],
               b.db.news_breaking_count_on(dl.iso(WED))),
              (["PolyAI opens a Delhi office"], 1))

    story = "Frontier lab ships an open reasoning system"
    with Bot() as b, v2.Net():
        b.seed([{"title": story, "when": ist(TUE, 13, 55), "seen": ist(TUE, 13, 55),
                 "imp": 5, "source": "Reuters", "rank": 4}])
        one = await b.cycle(ist(TUE, 14, 0))
        check("T10 industry needs 2 outlets: a one-outlet 5 is held",
              (one["posted"], any("NEWS_BREAKING_MIN_OUTLETS" in s for s in one["skipped"])),
              (0, True))
        copy = feed_item("Open reasoning system ships from frontier lab",
                         url="https://www.theverge.com/frontier-reasoning", source="The Verge",
                         seen=utc(TUE, 14, 30))
        copy["story_key"] = news.story_key(story)      # the same words, another order
        b.db.news_feed_add([copy])
        two = await b.cycle(ist(TUE, 15, 0))
        check("T10 ...a second outlet arrives: the two-outlet 5 is posted",
              ([s["headline"] for s in two["kept"]], b.row(story).get("outlet_count")),
              ([story], 2))
        rule = "OpenAI releases GPT-6 with native tool use"
        seed_unscored(b, [rule], seen=ist(TUE, 15, 50), hint="")
        three = await b.cycle(ist(TUE, 16, 0))
        check("T10 ...a launch-rule item is posted with one outlet",
              ([s["headline"] for s in three["kept"]], b.row(rule).get("outlet_count")),
              ([rule], 1))


# =============================================================================
# F5 — RUN EVERY HOUR (T11, T12)
# =============================================================================


async def every_hour(check) -> None:
    print("\nF5 RUN EVERY HOUR (T11, T12)")
    plan = news.due_slots(ist(TUE, 15, 40), ["14:00"], skip_hours=3)
    check("T11 due_slots: a 15:40 tick with 14:00 done -> 15:00, 40 minutes late",
          (plan["run"], plan["late"], plan["skip"], news.late_note(40)),
          ("15:00", 40, [], "late by 40 min"))
    plan = news.due_slots(ist(TUE, 19, 10), ["14:00"], skip_hours=3)
    check("T11 due_slots: a 19:10 tick with 15:00-18:00 unclaimed -> skip 15:00, run 16:00",
          (plan["run"], plan["skip"]), ("16:00", [("15:00", 250)]))

    with Bot() as b, v2.Net():
        await b.cycle(ist(TUE, 14, 0))
        late = await b.cycle(ist(TUE, 15, 40))
        row = [c for c in b.cycles(TUE) if c["hour"] == "15:00"][0]
        check("T11 missed slot catch-up: the 15:40 tick runs 15:00, noted late",
              (late["slot"], late["late"], row["status"], row["note"]),
              ("15:00", "late by 40 min", "done", "late by 40 min"))
        check("T11 ...an on-time tick is not late",
              [c["note"] for c in b.cycles(TUE) if c["hour"] == "14:00"], [""])

    with Bot() as b, v2.Net():
        await b.cycle(ist(TUE, 14, 0))
        ran = []
        for hh, mm in ((19, 10), (19, 25), (19, 40), (19, 55), (20, 5)):
            got = await b.cycle(ist(TUE, hh, mm))
            ran.append((got or {}).get("slot"))
        by_hour = {c["hour"]: c["status"] for c in b.cycles(TUE)}
        check("T11 the 19:10 tick skips 15:00 and runs 16:00; the next ticks run 17:00, "
              "18:00, 19:00, 20:00",
              (ran, by_hour.get("15:00")), (["16:00", "17:00", "18:00", "19:00", "20:00"],
                                            "skipped"))
        check("edge 11 ...the skipped hour says why",
              "more than 3 h late" in [c["note"] for c in b.cycles(TUE)
                                        if c["hour"] == "15:00"][0], True)
        check("T11 ...a skipped hour is never run later",
              await b.cycle(ist(TUE, 20, 10)), None)

    with Bot() as b, v2.Net():
        gate = asyncio.Event()
        b.scorer = GatedScorer(gate=gate)
        b.w.llm.score_news = b.scorer
        seed_unscored(b, filler(3), seen=ist(TUE, 13, 50))
        first = asyncio.ensure_future(b.cycle(ist(TUE, 14, 0)))
        await asyncio.wait_for(b.scorer.entered.wait(), 30)
        second = await b.cycle(ist(TUE, 15, 10))
        rows = b.cycles(TUE)
        gate.set()
        done = await first
        check("T12 cycle lock: a second call while one runs returns None, claims nothing",
              (second, [c["hour"] for c in rows], done["slot"], b.w.bot._news_cycle_busy),
              (None, ["14:00"], "14:00", False))

    # The time cap (F5): the pull stops at the next source boundary.
    with Bot(pins={"NEWS_CYCLE_MAX_SECONDS": 2}) as b, v2.Net():
        out = await b.cycle(ist(TUE, 14, 0))
        check("F5 a pull past NEWS_CYCLE_MAX_SECONDS stops at a source boundary; the "
              "cycle is partial",
              (out["pull"]["timed_out"], out["status"],
               "stopped after 2s" in b.cycles(TUE)[0]["note"],
               out["pull"]["google_requests"] < len(feeds.plan(
                   v2.base_names(), big=True, hours=24))),
              (True, "partial", True, True))


# =============================================================================
# F6 — THE HEALTH LINE (T13)
# =============================================================================


async def health(check) -> None:
    print("\nF6 THE HEALTH LINE (T13)")
    now = ist(TUE, 14, 0)
    flood = [{"title": f"Voice market story {_A[i % 20]} {_B[i // 20]}",
              "when": now - timedelta(minutes=10), "source": "Reuters",
              "summary": "PolyAI was mentioned in passing"} for i in range(32)]
    copies = [{"title": "Google launches Gemini 4 Aragon", "when": now - timedelta(minutes=9),
               "source": "Reuters"},
              {"title": "Gemini 4 Aragon: Google launches", "when": now - timedelta(minutes=8),
               "source": "Mint"}]
    with Bot(pins={"NEWS_SCORE_MAX_CALLS": 1}) as b, \
            v2.Net([('"PolyAI"', flood), ("evals", copies)]):
        seed_unscored(b, filler(60), seen=now - timedelta(minutes=30))
        await b.cycle(now)
        await b.cycle(ist(TUE, 19, 10))          # 15:00 skipped, 16:00 late
        drips = b.db.conn
        with drips() as c:
            before = c.execute("SELECT COUNT(*) FROM drip_sends").fetchone()[0]
        n = b.w.n_posted
        early = await b.w.bot._news_health(now=ist(TUE, 23, 30))
        out = await b.w.bot._news_health(now=ist(TUE, 23, 45))
        again = await b.w.bot._news_health(now=ist(TUE, 23, 50))
        with drips() as c:
            after = c.execute("SELECT COUNT(*) FROM drip_sends").fetchone()[0]
            cyc = [dict(r) for r in c.execute(
                "SELECT * FROM news_cycles WHERE on_date = ? AND hour NOT LIKE 'test%'",
                (dl.iso(TUE),)).fetchall()]
            by = {r[0] or "": r[1] for r in c.execute(
                "SELECT scored_by, COUNT(*) FROM news_feed_items WHERE scored_at <> '' "
                "GROUP BY scored_by").fetchall()}
            brk = c.execute("SELECT COUNT(*) FROM news_stories WHERE kind = 'breaking' "
                            "AND posted_on = ?", (dl.iso(TUE),)).fetchone()[0]
        posted = b.w.posted[n:]
        st = out["stats"]
        want = {
            "planned": 10, "run": sum(1 for c in cyc if c["status"] != "skipped"),
            "late": sum(1 for c in cyc if "late by" in c["note"] and c["status"] != "skipped"),
            "skipped": sum(1 for c in cyc if c["status"] == "skipped"),
            "partial": sum(1 for c in cyc if c["status"] == "partial"),
            "fetched": sum(c["fetched"] for c in cyc),
            "new": sum(c["new_items"] for c in cyc),
            "merged": sum(c["merged"] for c in cyc),
            "flagged": sum(c["flagged"] for c in cyc),
            "scored_model": sum(c["scored_model"] for c in cyc),
            "scored_rule": sum(c["scored_rule"] for c in cyc),
            "carried": [c for c in cyc if c["status"] != "skipped"][-1]["carried"],
            "breaking": brk,
        }
        check("T13 health line: the numbers equal the news_cycles and news_feed_items "
              "counts in the temp DB",
              {k: st[k] for k in want}, want)
        check("T13 ...and what the cycles say they scored is what news_feed_items holds",
              (want["scored_model"], want["scored_rule"]),
              (by.get("model", 0), by.get("rule", 0)))
        check("T13 ...and they are not all zero (a merge, a skip, a late hour, a rule "
              "score, a carry, a noisy name)",
              (st["merged"] > 0, st["skipped"], st["late"], st["scored_rule"] > 0,
               st["carried"] > 0, st["noisy"]),
              (True, 1, 1, True, True, ["PolyAI"]))
        body = out["body"]
        check("T13 ...the line says them, one item a line, no emoji",
              (body.splitlines()[1],
               f"Scored: {want['scored_model']} by the model, {want['scored_rule']} by "
               f"the launch rule, {want['carried']} carried" in body,
               "Noisy names: PolyAI" in body, len(body.splitlines()),
               all(ord(ch) < 0x2000 for ch in body)),
              (f"Cycles: 10 planned, {want['run']} run, {want['late']} late, "
               f"{want['skipped']} skipped, {want['partial']} partial", True, True, 8, True))
        check("T13 ...posted once, to the test channel (never the sales channel), at 23:45",
              (early, len(posted), [m.channel.id for m in posted], again,
               out["posted"]),
              (None, 1, [v2.rw.OTHER_CHANNEL], None, True))
        check("T13 ...the daily cap is untouched: no drip row", after - before, 0)
        check("edge 14 ...with the real date", out["date"], dl.iso(TUE))

    with Bot(pins={"SALES_TEST_CHANNEL_ID": 0}) as b, v2.Net():
        n = b.w.n_posted
        out = await b.w.bot._news_health(now=ist(TUE, 23, 45))
        check("T13 no test channel set -> nothing posted anywhere (never the sales channel)",
              (out["posted"], b.w.n_posted - n), (False, 0))


# =============================================================================
# T14 — THE TEMPLATES
# =============================================================================


def _news_blocks(text: str) -> dict:
    """The `news.render` blocks of a rendered-templates file, by post: the daily
    post (MODE_MAIN: no heading), the breaking post ("**Breaking news**"), and
    the answers ("**AI News, <day>**")."""
    out = {"daily": [], "breaking": [], "answer": []}
    for part in text.split("=" * 78 + "\n"):
        if not part.startswith("[news.render]"):
            continue
        body = part.split("\n", 1)[1] if "\n" in part else ""
        kind = ("breaking" if body.startswith("**Breaking") else
                "answer" if body.startswith("**AI News") else "daily")
        out[kind].append(part)
    return out


_RENDERED: dict = {}


def render_news_posts() -> dict:
    """Render every post the fixtures make (tools/render_templates.py, minutes;
    once per process) and compare the DAILY POST and the BREAKING POST blocks
    of `news.render` with docs/test-reports/templates-main.txt — the file the
    tool renders from a clean main (re-rendered on 16 Oct: the one in the repo
    predated NFT2-1087 and matched no tree). Those two carry no date, so they
    compare on any day; the answers carry the day in their heading, and
    NEWS-ANSWER-OCT16 changes what they hold, so they are not compared."""
    if _RENDERED:
        return _RENDERED
    golden = os.path.join(ROOT, "docs", "test-reports", "templates-main.txt")
    with tempfile.TemporaryDirectory(prefix="saley-render-") as tmp:
        out = os.path.join(tmp, "templates.txt")
        ran = subprocess.run(
            [sys.executable, os.path.join("tools", "render_templates.py"), out], cwd=ROOT,
            env=dict(os.environ, ANTHROPIC_API_KEY="x", PYTHONIOENCODING="utf-8"),
            capture_output=True, text=True, timeout=1800)
        mine = open(out, encoding="utf-8").read() if os.path.exists(out) else ""
    gold = _news_blocks(open(golden, encoding="utf-8").read())
    have = _news_blocks(mine)
    _RENDERED.update(
        returncode=ran.returncode, daily=len(have["daily"]), breaking=len(have["breaking"]),
        same=(have["daily"] == gold["daily"] and have["breaking"] == gold["breaking"]))
    return _RENDERED


def templates(check) -> None:
    print("\nT14 THE TEMPLATES")
    got = render_news_posts()
    check("T14 templates: news.render output for the daily post and the breaking post is "
          "byte-identical to docs/test-reports/templates-main.txt",
          (got["returncode"], got["daily"] > 0, got["breaking"] > 0, got["same"]),
          (0, True, True, True))


GROUPS = (story_keys, flags, scoring, budget, every_hour, health)


async def run(check) -> None:
    logging.getLogger().setLevel(logging.ERROR)
    for group in GROUPS:
        await group(check)
    templates(check)
