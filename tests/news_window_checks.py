"""THE CHECKS for NEWS-ANSWER-OCT16 "a question gets the real top 5": T1 to T16.

`run(check)` is called by verify_news_window.py (which prints the T1-T16 table)
and each group by tests/test_news_window.py (which asserts). A check that IS
one of the ticket's test cases is named "T<n> <case>"; its edge cases "edge <n>".

OFFLINE: the pure functions on fixtures, and the questions through the REAL
message handling of a whole bot behind fake Discord (tests/replies_world.py)
on a throwaway database — the feed store seeded with scored stories, so no
scoring call, no feed, no search and no model is needed.
"""
import logging
import os
import re
from datetime import date, datetime, time, timedelta, timezone

import replies_world as rw

import config
import deadlines as dl
import feeds
import news

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IST = dl.IST

PINS = {"NEWS_ASK_DEFAULT_HOURS": 24, "NEWS_ASK_REST_MINUTES": 30,
        "NEWS_FEED_KEEP_DAYS": 14, "NEWS_REPEAT_DAYS": 30, "TOKEN_DAILY_BUDGET": 0,
        "SALES_CHANNEL_IDS": [rw.CHANNEL, rw.OTHER_CHANNEL],
        "SALES_CHANNEL_ID_SET": {rw.CHANNEL, rw.OTHER_CHANNEL}}

# Headlines that are different stories to every key: different WORDS.
_A = ("amber", "basil", "cedar", "dahlia", "ember", "fennel", "garnet", "hazel", "iris",
      "juniper", "kestrel", "linden", "maple", "nectar", "olive")
BANNED = ("posted", "already", "earlier", "sent before", "again")


def title(i: int, what: str = "lab opens a research office") -> str:
    return f"{_A[i].title()} {what}"


def story(i: int, imp: int, *, minutes: int = 0, sent: bool = False, what: str = "") -> dict:
    """A story dict as `news.choose_answer` gets them."""
    head = title(i, what or "lab opens a research office")
    url = f"https://example.org/{i}"
    return {"headline": head, "url": url, "url_key": news.url_key(url),
            "headline_key": news.headline_key(head), "importance": imp,
            "published_at": f"2026-10-15T{10 + minutes // 60:02d}:{minutes % 60:02d}:00+00:00",
            "source": "TechCrunch", "flag": 0, "kind": "industry", "_sent": sent}


def seed(w, rows) -> list:
    """Store scored stories: [(title, importance, when IST, topic)]. Returns the
    url keys in order."""
    items, scores = [], []
    for head, imp, when, topic in rows:
        url = "https://example.org/" + re.sub(r"[^a-z0-9]+", "-", head.lower()).strip("-")
        it = {"url": url, "url_key": news.url_key(url),
              "headline_key": news.headline_key(head), "title": head, "summary": "",
              "source": "TechCrunch", "published_at": feeds.utc_iso(when),
              "topic_hint": "", "seen_at": feeds.utc_iso(when),
              "kind": feeds.KIND_INDUSTRY, "sheet_ref": ""}
        items.append(it)
        scores.append({"url_key": it["url_key"], "importance": imp, "topic": topic,
                       "what": "what happened"})
    w.bot.db.news_feed_add(items)
    w.bot.db.news_feed_set_scores(scores, scored_at=feeds.utc_iso(dl.real_now_ist()))
    w.bot.__dict__.pop("_news_tools", None)          # the real one, over the store
    return [it["url_key"] for it in items]


def world():
    return rw.World(test_mode=False, pins=dict(rw.PINS, **PINS))


def lines(text: str) -> list:
    return [ln for ln in str(text or "").split("\n") if ln.startswith("- ")]


def last_line(text: str) -> str:
    return str(text or "").rstrip().split("\n")[-1]


# =============================================================================
# THE PURE PARTS — T1-T7
# =============================================================================


def pure(check) -> None:
    print("\nTHE WINDOW, THE CHOICE, THE TAIL (T1-T7)")
    now = datetime(2026, 10, 15, 15, 30, tzinfo=IST)
    got = news.parse_window("top news in the last 24 hours", now=now)
    check("T1 parse_window('top news in the last 24 hours') -> 24, 'last 24 hours'",
          (got["hours"], got["label"]), (24, "last 24 hours"))
    check("T2 parse_window('any AI news?') -> None (the caller uses 24)",
          news.parse_window("any AI news?", now=now), None)
    want = {
        "what's the news today": (16, "day so far", datetime(2026, 10, 15, 0, 0, tzinfo=IST)),
        "any news since yesterday": (40, "period from yesterday",
                                     datetime(2026, 10, 14, 0, 0, tzinfo=IST)),
        "news from yesterday": (40, "period from yesterday",
                                datetime(2026, 10, 14, 0, 0, tzinfo=IST)),
        "what happened this morning": (10, "morning so far",
                                       datetime(2026, 10, 15, 6, 0, tzinfo=IST)),
        "AI news in the past 2 days": (48, "past 2 days", None),
        "top stories this week": (168, "past week", None),
        "news from the last month": (336, "last month", None),
    }
    got = {q: news.parse_window(q, now=now, keep_days=14) for q in want}
    check("T3 parse_window for today / yesterday / this morning / past 2 days / this week "
          "/ last month -> the right hours in IST; last month capped at 14 days",
          {q: (g["hours"], g["label"], g["since"]) for q, g in got.items()}, want)
    check("T3 ...and 'last month' says it was capped",
          (got["news from the last month"]["capped"], got["top stories this week"]["capped"]),
          (True, False))
    check("edge 7 'today' follows the clock it is handed (a test day's)",
          news.parse_window("news today", now=datetime(2026, 10, 3, 9, 0, tzinfo=IST))["since"],
          datetime(2026, 10, 3, 0, 0, tzinfo=IST))

    imps = [5, 5, 4, 4, 4, 4, 3, 3, 3, 3, 3, 3]
    sent = {0, 2, 6, 9}
    fixture = [story(i, imp, minutes=i, sent=i in sent) for i, imp in enumerate(imps)]
    got = news.choose_answer(fixture, is_sent=lambda s: s["_sent"], cap=5)
    check("T4 choose_answer on 12 stories, 4 sent -> top 5 by score, sent included; total "
          "12; remaining 7",
          ([s["headline"] for s in got["stories"]], got["total"], got["remaining"],
           sorted(got["sent_before"])),
          ([title(1), title(0), title(5), title(4), title(3)], 12, 7,
           sorted([fixture[0]["url_key"]])))
    check("edge 9 ties on importance -> newer first",
          [s["importance"] for s in got["stories"]][:2] == [5, 5]
          and got["stories"][0]["published_at"] > got["stories"][1]["published_at"], True)
    page2 = news.choose_answer(fixture, is_sent=lambda s: s["_sent"], cap=5, offset=5)
    check("T4 ...the next page from offset 5",
          ([s["headline"] for s in page2["stories"]], page2["remaining"]),
          ([title(2), title(11), title(10), title(9), title(8)], 2))

    top = [story(i, 5, minutes=i, sent=True) for i in range(5)] + \
          [story(i, 3, minutes=i, sent=False) for i in range(5, 8)]
    got = news.choose_answer(top, is_sent=lambda s: s["_sent"], cap=5)
    block = news.render(got["stories"], mode=news.MODE_ANSWER, day=date(2026, 10, 15),
                        remaining=got["remaining"], label="last 24 hours", cap=5)
    check("T5 all five top are sent -> five shown, no marker, no 'posted' word",
          (len(lines(block)), all(s["importance"] == 5 for s in got["stories"]),
           [w for w in BANNED if w in block.lower()], "*" in "".join(lines(block))),
          (5, True, [], False))
    check("edge 1 ...and every one of them is a repeat",
          len(got["sent_before"]), 5)

    seven = news.render([story(i, 4, minutes=i) for i in range(5)], mode=news.MODE_ANSWER,
                        day=date(2026, 10, 15), remaining=7, label="last 24 hours", cap=5)
    check("T6 render MODE_ANSWER with remaining 7 -> the tail, exact text",
          last_line(seven), "There are 7 more in the last 24 hours. These are the top 5. "
                            "Would you like to see the rest?")
    check("T6 ...after the list, on its own line, nothing else changed",
          seven.rsplit("\n", 1)[0], news.render([story(i, 4, minutes=i) for i in range(5)],
                                                mode=news.MODE_ANSWER, day=date(2026, 10, 15)))
    none = news.render([story(i, 4, minutes=i) for i in range(5)], mode=news.MODE_ANSWER,
                       day=date(2026, 10, 15), remaining=0, label="last 24 hours", cap=5)
    check("T7 remaining 0 -> no tail", "Would you like" in none, False)
    check("T6/T7 the daily post and the breaking post never carry it",
          ["Would you like" in news.render([story(1, 5)], mode=m, remaining=7, label="x")
           for m in (news.MODE_MAIN, news.MODE_BREAKING)], [False, False])
    check("edge 3 parse_count: 'top 20' -> 20 (the bot caps it at 5); 'top 3' -> 3; "
          "'five headlines' -> 5; none -> None",
          [news.parse_count(q) for q in ("top 20 news", "top 3 news today",
                                         "give me five headlines", "any news?")],
          [20, 3, 5, None])


# =============================================================================
# THROUGH THE BOT — T8-T15
# =============================================================================


def _twelve(w, *, minutes_ago=10) -> list:
    now = dl.real_now_ist()
    imps = [5, 5, 4, 4, 4, 4, 3, 3, 3, 3, 3, 3]
    return seed(w, [(title(i), imp, now - timedelta(minutes=minutes_ago + i), "evals")
                    for i, imp in enumerate(imps)])


async def through_the_bot(check) -> None:
    print("\nTHROUGH THE BOT (T8-T15)")

    # ---- T8 / T9 / T13: pages by "yes" ----------------------------------------
    with world() as w:
        _twelve(w)
        n = w.n_posted
        q = await w.say("top news in the last 24 hours", mention=True)
        first = w.posted[n] if w.n_posted > n else None
        check("T13 plain path: 'top news in the last 24 hours' is plain_question -> no model "
              "call", (news.plain_question("top news in the last 24 hours"),
                       w.counts()["model"], w.counts()["router"], w.n_posted - n),
              (True, 0, 0, 1))
        body = rw.strip_tag(first.content) if first else ""
        check("T8 the first page: five, and the tail counts the other seven",
              (len(lines(body)), last_line(body)),
              (5, news.answer_tail(7, "last 24 hours", 5)))
        n = w.n_posted
        await w.say("yes", reply_to=first)
        second = w.posted[n] if w.n_posted > n else None
        body2 = rw.strip_tag(second.content) if second else ""
        page = w.bot._news_pages.get(w.chan.id) or {}
        check("T8 'yes' within 30 min -> the next 5, offset 5",
              ([ln.split(" (")[0][2:] for ln in lines(body2)], page.get("offset"),
               last_line(body2)),
              ([title(5), title(6), title(7), title(8), title(9)], 5,
               news.answer_tail(2, "last 24 hours", 5)))
        n = w.n_posted
        await w.say("show me the rest", reply_to=second)
        third = w.posted[n] if w.n_posted > n else None
        body3 = rw.strip_tag(third.content) if third else ""
        check("T8 'show me the rest' -> the last page, with no tail",
              (len(lines(body3)), "Would you like" in body3), (2, False))
        n = w.n_posted
        await w.say("next", reply_to=third)
        check("T8 one more 'yes' -> 'That was all of it.'",
              w.replies_after(n), [news.ANSWER_ALL_SHOWN])

    with world() as w:
        _twelve(w)
        n = w.n_posted
        await w.say("any AI news?", mention=True)
        first = w.posted[n]
        w.bot._news_pages[w.chan.id]["at"] -= 31 * 60
        n = w.n_posted
        await w.say("yes", reply_to=first)
        asked = w.posted[n] if w.n_posted > n else None
        check("T9 'yes' after 30 min -> the question back, no list",
              (w.replies_after(n), lines(rw.strip_tag(asked.content)) if asked else None),
              (["The rest of the last-24-hour news?"], []))
        n = w.n_posted
        await w.say("yes", reply_to=asked)
        body = w.replies_after(n)
        check("edge 4 ...and a yes to that question gives the next page",
              len(lines(body[0])) if body else 0, 5)

    # ---- T10: two channels ------------------------------------------------------
    with world() as w:
        _twelve(w)
        n = w.n_posted
        await w.say("any AI news?", mention=True)
        here = w.posted[n]
        n = w.n_posted
        await w.say("any AI news?", who="approver2", channel=w.other, mention=True)
        there = w.posted[n]
        await w.say("yes", reply_to=here)
        check("T10 two channels -> independent offsets",
              (w.bot._news_pages[w.chan.id]["offset"],
               w.bot._news_pages[w.other.id]["offset"]), (5, 0))
        n = w.n_posted
        await w.say("yes", who="approver2", reply_to=there, channel=w.other)
        check("edge 5 ...the second asker's yes pages their own list",
              (w.bot._news_pages[w.other.id]["offset"],
               len(lines(w.replies_after(n)[0]))), (5, 5))

    # ---- T11 and edges 2, 3, 6, 8 ---------------------------------------------------
    with world() as w:
        now = dl.real_now_ist()
        midnight = datetime.combine(now.date(), time(0, 0), IST)
        span = max(60, int((now - midnight).total_seconds()) - 60)
        rows = [(title(i, "chip startup ships a sensor"), 5,
                 midnight - timedelta(hours=2, minutes=i), "evals") for i in range(2)]
        rows += [(title(i), 4, now - timedelta(seconds=span * (i + 1) // 7), "evals")
                 for i in range(2, 7)]
        seed(w, rows)
        n = w.n_posted
        await w.say("top 3 news today", mention=True)
        body = w.replies_after(n)[0] if w.n_posted > n else ""
        check("T11 'top 3 news today' -> cap 3, since 00:00 IST (yesterday's 5s are not in "
              "it); the tail counts the rest",
              (len(lines(body)), "chip startup" in body, last_line(body)),
              (3, False, news.answer_tail(2, "day so far", 3)))
        n = w.n_posted
        await w.say("top 20 news", mention=True)
        body = w.replies_after(n)[0] if w.n_posted > n else ""
        check("edge 3 'top 20' -> 5, the most; the tail offers the rest",
              (len(lines(body)), last_line(body)),
              (5, news.answer_tail(2, "last 24 hours", 5)))
        plain = (await w.bot._news_answer())["block"]
        n = w.n_posted
        await w.say("any news?", mention=True)
        check("edge 6 'any news?' -> 24 h, top 5, the same bytes as the plain path",
              rw.strip_tag(w.posted[n].content), plain)
        month = await w.bot._news_answer(hours=720)
        check("edge 8 a window longer than NEWS_FEED_KEEP_DAYS -> capped, one line says so",
              (last_line(month["block"]) == "I keep 14 days of news.",
               month["since"] >= month["until"] - timedelta(days=14, seconds=1)), (True, True))

    # ---- T12: a topic with nothing in the window ---------------------------------------
    with world() as w:
        old = dl.real_now_ist() - timedelta(days=3)
        seed(w, [("ElevenLabs opens a Bengaluru office", 4, old, "voice agent"),
                 (title(1), 4, dl.real_now_ist() - timedelta(minutes=5), "evals")])
        got = await w.bot._news_answer(topic="ElevenLabs")
        line = (f"Nothing on ElevenLabs in the last 24 hours. The latest I have is from "
                f"{dl.format_date(old.astimezone(timezone.utc).date())}:")
        check("T12 a topic with an empty window -> 'Nothing on X in the last 24 hours. The "
              "latest I have is from <date>:' and the story",
              (got["block"].split("\n")[0], lines(got["block"])[0].split(" (")[0]),
              (line, "- ElevenLabs opens a Bengaluru office"))
        none = await w.bot._news_answer(topic="Globex")
        check("T12 ...and with nothing at all: 'Nothing on X in the last 14 days.'",
              none["block"], "Nothing on Globex in the last 14 days.")

    # ---- T14: the tool -------------------------------------------------------------------
    with world() as w:
        now = dl.real_now_ist()
        seed(w, [(title(0), 4, now - timedelta(hours=30), "evals"),
                 (title(1), 4, now - timedelta(hours=2), "evals"),
                 (title(2), 3, now - timedelta(hours=3), "evals")])
        tool = w.bot._news_tools()[0]
        props = tool["schema"]["input_schema"]["properties"]
        logs = []
        handler = logging.Handler()
        handler.emit = lambda r: logs.append(r.getMessage())
        root = logging.getLogger()
        level = root.level
        root.addHandler(handler)
        root.setLevel(logging.INFO)
        try:
            both = await tool["handler"]({"hours": 48, "days": 1})
            days = await tool["handler"]({"days": 1})
            two = await tool["handler"]({"count": 2})
        finally:
            root.removeHandler(handler)
            root.setLevel(level)
        check("T14 tool schema: hours and count accepted",
              (props["hours"]["type"], props["count"]["type"], props["count"]["maximum"],
               "default 24" in props["hours"]["description"]), ("integer", "integer", 5, True))
        check("T14 ...hours wins over days (the 30-hour-old story is in), and it is logged",
              (both["stories"], days["stories"], any("hours wins" in m for m in logs)),
              (3, 2, True))
        check("T14 ...count sets the cap", two["stories"], 2)

    # ---- T15: what an answer records -------------------------------------------------------
    with world() as w:
        keys = _twelve(w)
        sent = [news.story_from_feed(r) for r in w.bot.db.news_feed_get(keys[:2])]
        w.bot.db.record_news_stories(sent, on_date=dl.iso(dl.today_ist()), rule_id="R1",
                                     kind=news.MODE_BREAKING)
        got = await w.bot._news_answer()
        wrote = await w.bot._record_news_answer(got)
        with w.bot.db.conn() as c:
            kinds = {r[0]: r[1] for r in c.execute("SELECT url_key, kind FROM news_stories")}
        check("T15 _record_news_answer: new stories recorded, repeats not",
              (wrote, kinds.get(keys[0]), kinds.get(keys[2])),
              (3, news.MODE_BREAKING, news.MODE_ANSWER))
        check("T15 ...the answer had both: the two posted stories lead it",
              len(got["sent_before"]), 2)


# =============================================================================
# T16 — THE DAILY POST AND THE BREAKING POST
# =============================================================================


def templates(check) -> None:
    """T16: the daily post and the breaking post, as `news.render` writes them for
    every fixture the checks render (tools/render_templates.py), are byte-identical
    to docs/test-reports/templates-main.txt. Shared with NEWS-PATCH-OCT16's T14."""
    import news_patch_checks

    got = news_patch_checks.render_news_posts()
    check("T16 daily post and breaking post -> byte-identical to "
          "docs/test-reports/templates-main.txt",
          (got["returncode"], got["daily"] > 0, got["breaking"] > 0, got["same"]),
          (0, True, True, True))


async def run(check) -> None:
    logging.getLogger().setLevel(logging.ERROR)
    pure(check)
    await through_the_bot(check)
    templates(check)
