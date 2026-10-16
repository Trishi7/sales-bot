"""THE CHECKS for NEWS-ANSWER-OCT16-B "a named day is that day only": B1 to B20.

The ticket numbers its cases T1-T20; here they are B1-B20, so they do not
collide with NEWS-ANSWER-OCT16's T1-T16 in the same script. `run(check)` is
called by verify_news_window.py (which prints both tables) and each group by
tests/test_news_window.py.

TWO OF THE TICKET'S DATES ARE CALENDAR SLIPS, and the checks follow the
calendar: 15 Oct 2026 is a Thursday, so B2 ("last wednesday" asked on a
Wednesday) stands on Wed 14 Oct and gets 7 Oct, as edge cases 1 and 12 say;
and "last week" seen from Fri 16 Oct is Mon 5 to Sun 11 Oct (B8), not 6-12.

OFFLINE: the pure functions on a pinned clock, and questions through the REAL
message handling of a whole bot behind fake Discord (tests/replies_world.py)
on a throwaway database with a seeded, scored feed store. Days are counted
from the real today, so the checks hold on any date.
"""
import logging
import re
from datetime import date, datetime, time, timedelta, timezone

import replies_world as rw

import deadlines as dl
import feeds
import news
import news_window_checks as nw

IST = dl.IST
FRI = datetime(2026, 10, 16, 18, 33, tzinfo=IST)
_B = ("anchor", "beacon", "canyon", "delta", "estuary", "fjord", "glacier", "harbor",
      "island", "jetty", "knoll", "lagoon")


def last_weekday(today: date, wd: int) -> date:
    return today - timedelta(days=(today.weekday() - wd) % 7 or 7)


def day_rows(day: date, n: int, imp: int, *, start: int = 0, hour: int = 12,
             what: str = "startup expands quietly") -> list:
    """`n` different stories on `day`, `imp` each, a few minutes apart."""
    out = []
    for i in range(start, start + n):
        head = f"{nw._A[i % 15].title()} {_B[i // 15]} {what}"
        out.append((head, imp, datetime.combine(day, time(hour, 0), IST)
                    + timedelta(minutes=i), "evals"))
    return out


# =============================================================================
# THE WINDOW — B1-B10, B19
# =============================================================================


def windows(check) -> None:
    print("\nNEWS-ANSWER-OCT16-B: THE WINDOW (B1-B10, B19)")
    w = news.parse_window("news from last wednesday", now=FRI)
    check("B1 parse_window('news from last wednesday') on Fri 16 Oct -> day, 14 Oct "
          "00:00-23:59:59 IST, 'Wed 14 Oct'",
          (w["kind"], w["start"], w["end"], w["label"]),
          ("day", datetime(2026, 10, 14, 0, 0, tzinfo=IST),
           datetime(2026, 10, 14, 23, 59, 59, tzinfo=IST), "Wed 14 Oct"))
    wed = datetime(2026, 10, 14, 10, 0, tzinfo=IST)
    check("B2 parse_window('last wednesday') asked on a Wednesday (14 Oct) -> 7 Oct "
          "(edge 1: a week ago, never today)",
          news.parse_window("last wednesday", now=wed)["start"].date(), date(2026, 10, 7))
    check("edge 2 'wednesday' asked on a Thursday -> yesterday's Wednesday",
          news.parse_window("wednesday", now=datetime(2026, 10, 15, 9, 0, tzinfo=IST)
                            )["start"].date(), date(2026, 10, 14))
    got = {q: news.parse_window(q, now=FRI) for q in
           ("14th oct", "14 oct", "oct 14", "14/10", "14-10-2026", "2026-10-14")}
    check("B3 parse_window('14th oct'), ('14 oct'), ('oct 14'), ('14/10') (and the two "
          "numeric forms) -> all 14 Oct 2026",
          {q: (g["kind"], g["start"].date()) for q, g in got.items()},
          {q: ("day", date(2026, 10, 14)) for q in got})
    w = news.parse_window("give me the most important news of last wednesday-14th oct",
                          now=FRI)
    check("B4 parse_window('last wednesday-14th oct') -> day, 14 Oct",
          (w["kind"], w["label"]), ("day", "Wed 14 Oct"))
    w = news.parse_window("wednesday 15 oct", now=FRI)
    check("B5 parse_window('wednesday 15 oct') -> unclear, the ask names 14 and 15",
          (w["kind"], w["ask"]),
          ("unclear", "Wednesday was 14 Oct. Did you mean 14 or 15 Oct?"))
    w = news.parse_window("20th oct", now=FRI)
    check("B6 parse_window('20th oct') on 16 Oct -> unclear, 'has not happened yet'",
          (w["kind"], w["ask"]), ("unclear", "20 Oct has not happened yet."))
    got = [news.parse_window(q, now=FRI) for q in
           ("from monday to wednesday", "monday-wednesday", "12 to 14 oct")]
    check("B7 parse_window('from monday to wednesday') -> range 12-14 Oct, '12–14 Oct' (and "
          "'monday-wednesday', '12 to 14 oct')",
          [(g["kind"], g["start"], g["end"], g["label"]) for g in got],
          [("range", datetime(2026, 10, 12, 0, 0, tzinfo=IST),
            datetime(2026, 10, 14, 23, 59, 59, tzinfo=IST), "12–14 Oct")] * 3)
    w = news.parse_window("last week", now=FRI)
    check("B8 parse_window('last week') on Fri 16 Oct -> Mon 5 to Sun 11 Oct (the calendar; "
          "the ticket wrote 6-12)",
          (w["kind"], w["start"], w["end"]),
          ("range", datetime(2026, 10, 5, 0, 0, tzinfo=IST),
           datetime(2026, 10, 11, 23, 59, 59, tzinfo=IST)))
    w = news.parse_window("this week so far", now=FRI)
    check("B8 ...'this week so far' -> Monday 00:00 to now",
          (w["kind"], w["start"], w["end"], w["label"]),
          ("range", datetime(2026, 10, 12, 0, 0, tzinfo=IST), FRI, "12–16 Oct"))
    w = news.parse_window("any news?", now=FRI)
    check("B9 parse_window('any news?') -> rolling 24 h",
          (w["kind"], w["end"] - w["start"]), ("rolling", timedelta(hours=24)))
    check("edge B-unclear a weekday or date that cannot be read -> the ask, never a guess",
          news.parse_window("news from 31 feb", now=FRI),
          {"kind": "unclear", "ask": news.UNCLEAR_DAY})
    check("edge 5 'yesterday' at 00:30 IST -> the previous IST day",
          news.parse_window("yesterday", now=datetime(2026, 10, 16, 0, 30, tzinfo=IST)
                            )["start"].date(), date(2026, 10, 15))

    s = {"headline": "Lab ships evals", "url": "https://example.org/x", "url_key": "x",
         "importance": 4}
    heads = [news.render([s], mode=news.MODE_ANSWER, day=date(2026, 10, 16),
                         when=label).split("\n")[0] for label in ("Wed 14 Oct", "12–14 Oct", "")]
    check("B10 heading for day / range / rolling -> 'Wed 14 Oct' / '12–14 Oct' / today",
          heads, ["**AI News, Wed 14 Oct**", "**AI News, 12–14 Oct**",
                  "**AI News, Fri 16 Oct**"])
    long = [dict(s, headline=f"Lab {i} " + "x" * 300, url=f"https://example.org/{i}",
                 url_key=str(i), headline_key=str(i)) for i in range(12)]
    parts = news.split_message(news.render(long, mode=news.MODE_ANSWER, when="Wed 14 Oct",
                                           every=True))
    check("B10 ...a continuation page keeps the heading with '(continued)'",
          (len(parts) > 1, parts[1].split("\n")[0]),
          (True, "**AI News, Wed 14 Oct (continued)**"))

    t14 = datetime(2026, 10, 14, 10, 0, tzinfo=IST)
    check("B19 test clock 14 Oct 10:00 -> 'yesterday' 13 Oct, 'last wednesday' 7 Oct",
          (news.parse_window("yesterday", now=t14)["start"].date(),
           news.parse_window("last wednesday", now=t14)["start"].date()),
          (date(2026, 10, 13), date(2026, 10, 7)))


# =============================================================================
# THROUGH THE BOT — B11-B18
# =============================================================================


async def through_the_bot(check) -> None:
    print("\nNEWS-ANSWER-OCT16-B: THROUGH THE BOT (B11-B18)")
    today = dl.real_today_ist()
    wed = last_weekday(today, 2)
    label = news.day_label(wed)

    # ---- B11 / B17: the pool is the day ---------------------------------------------
    with nw.world() as w:
        inside = day_rows(wed, 2, 4)
        before = [("Kestrel glacier lab ships a robot", 5,
                   datetime.combine(wed, time(0, 0), IST) - timedelta(minutes=30), "evals")]
        after = [("Linden harbor lab ships a robot", 5,
                  datetime.combine(wed, time(0, 0), IST) + timedelta(days=1, minutes=30),
                  "evals")] if wed + timedelta(days=1) < today else []
        nw.seed(w, inside + before + after)
        ours = {"url": "https://example.org/ours-polyai", "title": "PolyAI opens a Pune office",
                "summary": "", "source": "Inc42",
                "published_at": feeds.utc_iso(datetime.combine(wed, time(9, 0), IST)
                                              - timedelta(days=2)),
                "seen_at": feeds.utc_iso(datetime.combine(wed, time(9, 0), IST)),
                "topic_hint": "", "kind": "poc", "sheet_ref": "", "names": "PolyAI", "flag": 1}
        ours.update(url_key=news.url_key(ours["url"]),
                    headline_key=news.headline_key(ours["title"]))
        late = dict(ours, url="https://example.org/old-industry", title="Old industry tale here",
                    kind="industry", names="", flag=0)
        late.update(url_key=news.url_key(late["url"]),
                    headline_key=news.headline_key(late["title"]))
        w.bot.db.news_feed_add([ours, late])
        w.bot.db.news_feed_set_scores(
            [{"url_key": x["url_key"], "importance": 4, "topic": "evals", "what": ""}
             for x in (ours, late)], scored_at=feeds.utc_iso(dl.real_now_ist()))
        n = w.n_posted
        await w.say("give me news from last wednesday", mention=True)
        body = w.replies_after(n)[0] if w.n_posted > n else ""
        heads = [ln.split(" ([")[0][2:] for ln in nw.lines(body)]
        check("B11 pool for a day -> only items published (or first seen, if flagged) inside "
              "that IST day",
              sorted(heads), sorted([r[0] for r in inside] + [ours["title"]]))
        check("B10 ...headed with that day", body.split("\n")[0], f"**AI News, {label}**")
        check("B17 plain path: 'give me news from last wednesday' is plain -> no model call",
              (news.plain_question("give me news from last wednesday"),
               news.plain_question("give me the most important news of last "
                                   "wednesday-14th oct"),
               w.counts()["model"], w.counts()["router"]), (True, True, 0, 0))

    # ---- B12 / edge 8: most important ---------------------------------------------------
    with nw.world() as w:
        nw.seed(w, day_rows(wed, 1, 5) + day_rows(wed, 1, 4, start=1)
                + day_rows(wed, 3, 3, start=2))
        got = await w.bot._news_answer(window=news.parse_window("last wednesday"),
                                       important=True)
        check("B12 'most important' -> importance 4 or more only",
              sorted(x["importance"] for x in got["stories"]), [4, 5])
    with nw.world() as w:
        nw.seed(w, day_rows(wed, 3, 3))
        n = w.n_posted
        await w.say("most important news of wednesday", mention=True)
        body = w.replies_after(n)[0] if w.n_posted > n else ""
        check("edge 8 nothing at 4+ -> 'Nothing scored 4 or 5 on <day>. The top 3-scored "
              "stories were:' then the stories",
              (body.split("\n")[0], len(nw.lines(body))),
              (f"Nothing scored 4 or 5 on {label}. The top 3-scored stories were:", 3))

    # ---- B13: four copies of one story --------------------------------------------------
    copies = ["Google launches Gemini 4 Aragon", "Gemini 4 Aragon: Google launches",
              "Google Launches 'Aragon' Gemini 4", "Aragon — Google's Gemini 4 launches"]
    with nw.world() as w:
        at = datetime.combine(wed, time(15, 0), IST)
        items = []
        for i, head in enumerate(copies):
            url = f"https://outlet{i}.example/gemini-4"
            items.append({"url": url, "url_key": news.url_key(url),
                          "headline_key": news.headline_key(head),
                          "story_key": news.story_key(head), "title": head, "summary": "",
                          "source": f"Outlet {i}", "published_at": feeds.utc_iso(at),
                          "seen_at": feeds.utc_iso(at + timedelta(minutes=i)),
                          "topic_hint": "", "kind": "industry", "sheet_ref": ""})
        w.bot.db.news_feed_add(items)
        w.bot.db.news_feed_set_scores([{"url_key": items[0]["url_key"], "importance": 5,
                                        "topic": "frontier model", "what": ""}],
                                      scored_at=feeds.utc_iso(dl.real_now_ist()))
        got = await w.bot._news_answer(window=news.parse_window("last wednesday"))
        line = nw.lines(got["block"])
        check("B13 four copies of one story -> one line, '+3 outlets'",
              (len(line), line[0].endswith(" +3 outlets)") if line else None,
               "outlet0.example" in (line[0] if line else "")), (1, True, True))
    raw = [{"headline": copies[0], "url": f"https://o{i}.example/x", "url_key": f"o{i}",
            "headline_key": news.headline_key(copies[0]), "importance": 4 + (i == 2),
            "seen_at": f"2026-10-14T0{i}:00:00+00:00"} for i in range(4)]
    one = news.collapse_copies(raw)
    check("B13 ...collapse_copies: the first-seen copy, the best score, +3",
          ([x["url_key"] for x in one], one[0]["importance"], one[0]["extra_outlets"],
           news.answer_line(one[0]).endswith(" +3 outlets)"),
           news.story_line(one[0]).endswith("+3 outlets)")), (["o0"], 5, 3, True, False))

    # ---- B14 / B15: the tail of a day, and "yes" ------------------------------------------
    with nw.world() as w:
        nw.seed(w, day_rows(wed, 28, 4, what="lab publishes a long evaluation report "
                            "on multilingual speech models for Indic voice agents"))
        n = w.n_posted
        q = await w.say("news from last wednesday", mention=True)
        first = w.posted[n]
        body = rw.strip_tag(first.content)
        check("B14 tail for a day with 23 more at 4+ -> the exact text",
              nw.last_line(body), f"There are 23 more with a score of 4 or 5 on {label}. "
                                  "Would you like to see them all?")
        n = w.n_posted
        await w.say("yes", reply_to=first)
        parts = w.replies_after(n)
        every = [ln for part in parts for ln in nw.lines(part)]
        check("B14 'yes' -> all 23, split across messages with '(continued)', no tail",
              (len(every), len(parts) > 1,
               all(p.split("\n")[0] == f"**AI News, {label} (continued)**" for p in parts[1:]),
               any("Would you like" in p for p in parts),
               len({ln for ln in every} & {ln for ln in nw.lines(body)})),
              (23, True, True, False, 0))
        times = [ln for ln in every]
        n = w.n_posted
        await w.say("yes", reply_to=w.posted[-1])
        check("edge 10 ...and one more 'yes' -> 'That was all of it.'",
              w.replies_after(n), [news.ANSWER_ALL_SHOWN])
    with nw.world() as w:
        nw.seed(w, day_rows(wed, 5, 4) + day_rows(wed, 6, 3, start=5))
        got = await w.bot._news_answer(window=news.parse_window("last wednesday"))
        check("B15 tail for a day with 0 more at 4+ -> no tail",
              (len(got["stories"]), got["remaining"], "Would you like" in got["block"]),
              (5, 0, False))
    with nw.world() as w:
        nw.seed(w, day_rows(wed, 12, 4))
        n = w.n_posted
        first = None
        await w.say("news from last wednesday", mention=True)
        first = w.posted[n]
        w.bot._news_pages[w.chan.id]["at"] -= 31 * 60
        n = w.n_posted
        await w.say("yes", reply_to=first)
        check("edge 11 'yes' after 30 minutes -> the question back",
              w.replies_after(n), [f"The rest of the news from {label}?"])

    # ---- B16: the tool ---------------------------------------------------------------------
    with nw.world() as w:
        nw.seed(w, day_rows(wed, 2, 4) + [("Maple delta lab ships a robot", 4,
                                          dl.real_now_ist() - timedelta(hours=2), "evals")])
        logs = []
        handler = logging.Handler()
        handler.emit = lambda r: logs.append(r.getMessage())
        root = logging.getLogger()
        level = root.level
        root.addHandler(handler)
        root.setLevel(logging.INFO)
        try:
            got = await w.bot._news_tools()[0]["handler"](
                {"date": wed.isoformat(), "hours": 48})
            bad = await w.bot._news_tools()[0]["handler"]({"date": "2099-01-01"})
        finally:
            root.removeHandler(handler)
            root.setLevel(level)
        props = w.bot._news_tools()[0]["schema"]["input_schema"]["properties"]
        check("B16 tool call with date=<last wednesday> and hours=48 -> the date wins, logged",
              (got["stories"], got["list"].split("\n")[0], any("the date wins" in m for m in logs),
               "date" in props and "date_to" in props),
              (2, f"**AI News, {label}**", True, True))
        check("B16 ...a future date is asked back, nothing listed",
              (bad["stories"], bad["ask"]), (0, "1 Jan has not happened yet."))

    # ---- B18: an empty day, a day past the keep window -------------------------------------
    with nw.world() as w:
        other = wed - timedelta(days=1)
        nw.seed(w, day_rows(other, 1, 4) + day_rows(today, 1, 4, start=1, hour=0))
        got = await w.bot._news_answer(window=news.parse_window("last wednesday"))
        check("B18 an empty day -> 'I have nothing stored for <day>.' and the store's range",
              got["block"], f"I have nothing stored for {label}. What I have runs from "
                            f"{dl.format_date(other)} to {dl.format_date(today)}.")
        old = today - timedelta(days=20)
        got = await w.bot._news_answer(window=news.parse_window(old.isoformat()))
        check("B18 a day older than NEWS_FEED_KEEP_DAYS -> 'I keep 14 days of news. The oldest "
              "I have is from <date>.'",
              got["block"], f"I keep 14 days of news. The oldest I have is from "
                            f"{dl.format_date(other)}.")

    # ---- B19 through the bot: the test clock -----------------------------------------------
    with rw.World(test_mode=False, pins=dict(rw.PINS, **nw.PINS),
                  pretend=(date(2026, 10, 14), 10, 0)):
        check("B19 ...through the bot's clock: a test day of 14 Oct 10:00",
              (news.parse_window("yesterday")["start"].date(),
               news.parse_window("last wednesday")["start"].date()),
              (date(2026, 10, 13), date(2026, 10, 7)))


def templates(check) -> None:
    import news_patch_checks

    got = news_patch_checks.render_news_posts()
    check("B20 daily and breaking templates -> byte-identical to "
          "docs/test-reports/templates-main.txt",
          (got["returncode"], got["daily"] > 0, got["breaking"] > 0, got["same"]),
          (0, True, True, True))


async def run(check) -> None:
    logging.getLogger().setLevel(logging.ERROR)
    windows(check)
    await through_the_bot(check)
    templates(check)
