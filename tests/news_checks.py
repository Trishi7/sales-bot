"""THE CHECKS for the 8 Oct news template and news answer, written once.

`run(check)` is called by verify_news_format.py (which prints and counts) and
by tests/test_news_format.py (which asserts). `replay(check)` is the 8 Oct
11:22 exchange, called from verify_replay_oct6.py as one more section of the
shared replay harness. `check(name, got, want=True)`.

Every scenario (tests/news_cases.py) is run LIVE and then in SALES_TEST_MODE,
and the two must give the same text apart from the tag on proactive posts.
"""
import re
from datetime import date

import news
import news_cases as nc

LINE_RE = re.compile(r"^- \*\*[^*\n]+\*\* \(\[[^\]\n]+\]\(<https?://[^>\s]+>\)\)$")


def _shape(check, label: str, text: str, heading: str, n: int) -> None:
    lines = text.split("\n")
    check(f"{label}: the bold heading, with the day", lines[0], heading)
    check(f"{label}: then a blank line", lines[1] if len(lines) > 1 else None, "")
    items = nc.bullets(text)
    check(f"{label}: {n} bullet(s)", len(items), n)
    check(f"{label}: a blank line between the stories", text.count("\n\n- "), n)
    check(f"{label}: nothing after the last story", lines[-1].startswith("- "), True)


async def answers(check) -> None:
    print("\nANSWERS IN THE CHANNEL — \"any AI news?\" asked three times, nine stories collected")
    live = await nc.plain_three_times(False)
    test = await nc.plain_three_times(True)
    first, second, third = (a for a in live["answers"])
    check("each answer is ONE Discord message", [len(a) for a in live["answers"]], [1, 1, 1])
    print("   the first answer:")
    print("\n".join("   " + ln for ln in first[0].split("\n")))
    _shape(check, "first answer", first[0], nc.HEADING, 5)
    check("every line is a bold headline and its outlet, nothing else",
          all(nc_line for nc_line in map(LINE_RE.match, nc.bullets(first[0]))), True)
    by_score = dict(nc.NINE)
    got = nc.headlines(first[0])
    check("9 unsent: the 5 highest-scored (three 5s, and the two newest 4s)",
          sorted(by_score[h] for h in got), [4, 4, 5, 5, 5])
    check("...listed newest first",
          got, ["Open weights model tops a coding benchmark",
                "Search engine adds an agent mode",
                "Startup releases an open speech dataset",
                "Cloud provider cuts inference prices",
                "Frontier lab ships a reasoning model"])
    check("the answer's stories are recorded as sent (kind=answer)",
          (len(live["records"][0]), set(live["records"][0].values())), (5, {"answer"}))
    got2 = nc.headlines(second[0])
    check("asked again: none of the first answer's stories", sorted(set(got) & set(got2)), [])
    check("...the 4 that were left, and only those (no padding)",
          sorted(got2), sorted(h for h in by_score if h not in got))
    check("...newest first", got2, ["Consortium drafts an agent safety standard",
                                    "University publishes an RLHF study",
                                    "Chipmaker unveils an inference accelerator",
                                    "Regulator opens an inquiry into model evals"])
    got3 = nc.headlines(third[0])
    check("asked a third time, nothing new left: the 5 highest-scored again", got3, got)
    check("...and a repeat is not recorded twice", len(live["records"][2]), 9)
    for i, a in enumerate(live["answers"], start=1):
        check(f"answer {i} says nothing about 2 PM, a schedule, 'already', 'since' or 'ran'",
              nc.banned_in(a[0]), [])
    check("a plain news question makes ZERO model calls — engine, router, extractor, scorer",
          live["calls"][-1], {"model": 0, "router": 0, "extractor": 0, "scorer": 0,
                              "sheet_reads": 0})
    check("...and is timed as its own route", live["routes"], ["news", "news", "news"])
    check("test mode == live: the same three answers", test["answers"], live["answers"])
    check("...the same records and the same zero calls",
          (test["records"], test["calls"]), (live["records"], live["calls"]))
    check("an answer carries no [TEST] tag in either mode (replies never do)",
          (live["tagged"], test["tagged"]), ([[False]] * 3, [[False]] * 3))

    print("\nANSWERS — a question that names a company goes to the engine, with the same list")
    live, test = await nc.topic_question(False), await nc.topic_question(True)
    check("one reply", len(live["replies"]), 1)
    reply = live["replies"][0]
    _shape(check, "topic answer", reply, nc.HEADING, 2)
    check("only the stories that name it (one by its summary), newest first",
          nc.headlines(reply), ["ElevenLabs launches a Hindi voice agent",
                                "Voice startup closes a round"])
    check("the model is told the list is in the reply and is NOT shown the stories",
          [(h.get("added_to_reply"), h.get("stories"),
            sorted(k for k in h if k in ("items", "window", "list", "posted")))
           for h in live["handed"]], [(True, 2, [])])
    check("what the model wrote about a schedule is not sent",
          (nc.banned_in(reply), "VOI.ID" in reply), ([], False))
    check("two model calls (the tool, then its text), no scoring call",
          (live["calls"]["model"], live["calls"]["scorer"]), (2, 0))
    check("the stories it gave are recorded as sent",
          sorted(live["records"].values()), ["answer", "answer"])
    check("test mode == live", nc.same(live, test, ("replies", "calls", "records", "routes")), [])

    live = await nc.topic_and_something_else(False)
    test = await nc.topic_and_something_else(True)
    reply = live["replies"][0] if live["replies"] else ""
    print("   with another tool used (" + (live["other"] or "none offered") + "):")
    print("\n".join("   " + ln for ln in reply.split("\n")))
    check("the list first, then what the model found elsewhere",
          (reply.startswith(nc.HEADING + "\n\n- **ElevenLabs launches a Hindi voice agent**"),
           reply.endswith("On the sheet they are at the DM stage.")), (True, True))
    check("...still one message", len(live["replies"]), 1)
    check("test mode == live", nc.same(live, test, ("replies", "calls", "records")), [])

    live, test = await nc.quiet(False), await nc.quiet(True)
    check("nothing collected: the quiet line, alone, and no model call",
          (live["replies"], live["calls"]["model"], live["records"]),
          ([news.quiet_line(nc.DAY)], 0, {}))
    check("test mode == live", nc.same(live, test, ("replies", "calls", "records")), [])


async def daily(check) -> None:
    print("\nTHE DAILY POST AND ITS FOLLOW-UP — after somebody asked at 13:55")
    live = await nc.answer_then_daily_post(False)
    test = await nc.answer_then_daily_post(True)
    check("the answer gave the five newest 5s", sorted(nc.headlines(live["answer"][0])),
          sorted(nc.ASKED))
    check("two messages follow: the daily post and ONE follow-up", len(live["posts"]), 2)
    post, more = (live["posts"] + ["", ""])[:2]
    print("   the daily post:")
    print("\n".join("   " + ln for ln in post.split("\n")))
    print("   the follow-up:")
    print("\n".join("   " + ln for ln in more.split("\n")))
    check("the daily post is ONE Discord message, under 2,000 characters",
          len(live["raw"][0]) <= 2000, True)
    check("its heading", post.split("\n")[0], nc.HEADING)
    head = post.split("\n\n")[0].split("\n")
    check("whatever the post carries above the list (the people it tags) stays above it, "
          "then ONE blank line", (head[0], post.split("\n\n")[1].startswith("- ")),
          (nc.HEADING, True))
    lines = nc.bullets(post)
    check("five stories", len(lines), 5)
    check("the top PoC story goes first (NEWS_POC_SLOTS), in its own line, word for word",
          lines[0], f"- {nc.POC} — what happened. ({nc.POC_REF}) "
                    f"[example.org](<https://example.org/synthflow-raises-20m-series-a>)")
    check("...the rest are industry lines", all(LINE_RE.match(ln) for ln in lines[1:]), True)
    check("a blank line between every two stories", post.count("\n\n- "), 5)
    check("NOT ONE story the 13:55 answer gave is in the daily post",
          sorted(set(nc.headlines(post)) & set(nc.ASKED)), [])
    _shape(check, "follow-up", more, "**More AI News, Wed 7 Oct**", 5)
    check("7 major stories did not fit: the follow-up carries 5, never more",
          len(nc.bullets(more)), 5)
    check("an importance-4 story never goes in the follow-up",
          sorted(set(nc.headlines(more)) & set(nc.MIDDLING)), [])
    check("...nor anything the answer gave", sorted(set(nc.headlines(more)) & set(nc.ASKED)), [])
    check("no story is in both the post and its follow-up",
          sorted(set(nc.headlines(post)) & set(nc.headlines(more))), [])
    nxt = nc.headlines(live["next_answer"][0])
    check("the next answer gives what is still unsent — the two 4s and the two 5s with no room",
          sorted(nxt), sorted(nc.MIDDLING + ["Foxtrot raises a billion dollar round",
                                             "Golf raises a billion dollar round"]))
    kinds = sorted(set(live["records"].values()))
    check("everything sent is recorded by how it went out", kinds,
          ["answer", "main", "overflow"])
    check("no model call anywhere in this: not for the answer, the post or the follow-up",
          (live["calls"]["model"], live["calls"]["router"], live["calls"]["scorer"]), (0, 0, 0))
    check("test mode == live: the same answer, post, follow-up and next answer",
          nc.same(live, test, ("answer", "posts", "next_answer", "records", "calls")), [])
    check("in test mode the two proactive posts carry the tag, on the heading line",
          [r.split("\n")[0] for r in test["raw"]],
          [f"{test['prefix']} {nc.HEADING}", f"{test['prefix']} **More AI News, Wed 7 Oct**"])


def pure(check) -> None:
    print("\nTHE TEMPLATE — news.render in every mode, exact text")
    day = date(2026, 10, 8)
    G = "https://news.google.com/rss/articles/"
    s1 = {"headline": "State AGs launch investigations into OpenAI AI safety - Reuters",
          "source": "Reuters", "url": G + "CBMi1?oc=5", "url_key": "k1",
          "headline_key": "state ags launch", "importance": 5, "kind": "industry",
          "published_at": "2026-10-08T05:00:00Z"}
    s2 = {"headline": "TM Forum and Accenture launch AI trust framework for telecoms.",
          "source": "AI News & Artificial Intelligence | TechCrunch",
          "url": "https://www.techcrunch.com/2026/10/08/tm-forum", "url_key": "k2",
          "headline_key": "tm forum accenture", "importance": 4, "kind": "industry",
          "published_at": "2026-10-08T04:00:00Z"}
    s3 = {"headline": "ElevenLabs Doubles Its Valuation in Employee Tender",
          "what": "ElevenLabs doubled its valuation in employee tender offer.",
          "source": "streamlinefeed.co.ke", "url": "https://streamlinefeed.co.ke/el",
          "url_key": "k3", "headline_key": "elevenlabs doubles", "importance": 4,
          "kind": "poc", "sheet_ref": "Elevenlabs — on Master Pipeline and Outreach PoCs",
          "published_at": "2026-10-08T03:00:00Z"}
    s4 = {"headline": "As AI gains autonomy, enterprises must retain authority | Hindustan Times",
          "source": "Hindustan Times", "url": G + "CBMi4?oc=5", "url_key": "k4",
          "headline_key": "ai gains autonomy", "importance": 3, "kind": "industry",
          "published_at": "2026-10-08T02:00:00Z"}
    s5 = {"headline": "GPT-5 - what we know so far", "source": "Wired",
          "url": "https://www.wired.com/story/gpt-5", "url_key": "k5",
          "headline_key": "gpt 5 what know", "importance": 3, "kind": "industry",
          "published_at": "2026-10-08T01:00:00Z"}
    five = [s1, s2, s3, s4, s5]
    lines = [
        f"- **State AGs launch investigations into OpenAI AI safety** ([Reuters](<{G}CBMi1?oc=5>))",
        "- **TM Forum and Accenture launch AI trust framework for telecoms** "
        "([techcrunch.com](<https://www.techcrunch.com/2026/10/08/tm-forum>))",
        "- ElevenLabs Doubles Its Valuation in Employee Tender — ElevenLabs doubled its "
        "valuation in employee tender offer. (Elevenlabs — on Master Pipeline and Outreach "
        "PoCs) [streamlinefeed.co.ke](<https://streamlinefeed.co.ke/el>)",
        f"- **As AI gains autonomy, enterprises must retain authority** "
        f"([Hindustan Times](<{G}CBMi4?oc=5>))",
        "- **GPT-5 - what we know so far** ([wired.com](<https://www.wired.com/story/gpt-5>))",
    ]
    body = "\n\n".join(lines)
    check("daily post (MODE_MAIN): the five lines, a blank line between, no heading of its own",
          news.render(five, mode=news.MODE_MAIN), body)
    check("...with the drip sender's heading and tags line on it (news.layout)",
          news.layout("**AI News, Thu 8 Oct**\n<@111> <@222>\n" + news.render(five)),
          "**AI News, Thu 8 Oct**\n<@111> <@222>\n\n" + body)
    check("follow-up (MODE_OVERFLOW)", news.render(five, mode=news.MODE_OVERFLOW, day=day),
          "**More AI News, Thu 8 Oct**\n\n" + body)
    check("breaking (MODE_BREAKING)", news.render(five, mode=news.MODE_BREAKING, day=day),
          "**Breaking AI News, Thu 8 Oct**\n\n" + body)
    check("answer (MODE_ANSWER)", news.render(five, mode=news.MODE_ANSWER, day=day),
          "**AI News, Thu 8 Oct**\n\n" + body)
    check("the daily post's heading is the same label (drip.HEADINGS)",
          __import__("drip").heading("R1", day=day), "**AI News, Thu 8 Oct**")
    check("an industry line has no '— what happened' clause", " — " in lines[0] + lines[1],
          False)
    check("the PoC line is the old line word for word; only the marker changed",
          lines[2][2:], "ElevenLabs Doubles Its Valuation in Employee Tender — ElevenLabs "
                        "doubled its valuation in employee tender offer. (Elevenlabs — on "
                        "Master Pipeline and Outreach PoCs) "
                        "[streamlinefeed.co.ke](<https://streamlinefeed.co.ke/el>)")
    check("link previews stay suppressed: every link is <url>",
          len(re.findall(r"\]\(<https?://[^>]+>\)", body)), 5)

    six = [dict(s1, url=f"{G}six{i}", url_key=f"six{i}", headline_key=f"six key {w}",
                headline=f"The {w} lab ships a model", importance=5)
           for i, w in enumerate(("alpha", "bravo", "charlie", "delta", "echo", "foxtrot"))]
    check("six stories handed to any mode give five",
          [len(nc.bullets(news.render(six, mode=m, day=day)))
           for m in (news.MODE_MAIN, news.MODE_OVERFLOW, news.MODE_BREAKING, news.MODE_ANSWER)],
          [5, 5, 5, 5])
    twin = dict(s1, url="https://apnews.com/x", url_key="twin", headline_key="another key",
                source="AP News",
                headline="State AGs launch investigations into OpenAI AI safety | AP News")
    check("two items about the same story give one (same headline, two outlets)",
          len(nc.bullets(news.render([s1, twin, s2]))), 2)
    check("...and by the same link, and by the same headline key",
          (len(nc.bullets(news.render([s1, dict(s2, url_key="k1")]))),
           len(nc.bullets(news.render([s1, dict(s2, headline_key="state ags launch")])))),
          (1, 1))
    check("a mention in feed text pings nobody",
          "<@" in news.render([dict(s1, headline="Lab <@123> ships @everyone a model")]), False)

    print("\nTHE FOLLOW-UP'S BAR — only stories scored above 4, at most 5")
    import config
    check("the shipped defaults: NEWS_OVERFLOW_MIN_IMPORTANCE=5, NEWS_OVERFLOW_MAX_ITEMS=5",
          _env_example(("NEWS_OVERFLOW_MIN_IMPORTANCE", "NEWS_OVERFLOW_MAX_ITEMS")),
          {"NEWS_OVERFLOW_MIN_IMPORTANCE": "5", "NEWS_OVERFLOW_MAX_ITEMS": "5"})
    check("...and the code's own defaults agree (read with no .env value set)",
          _code_defaults(config), (5, 5))

    print("\nONE MESSAGE — split only past 2,000 characters, and never without a heading")
    post = news.layout("**AI News, Thu 8 Oct**\n<@111>\n" + news.render(five))
    check("a full daily post of five is one message", news.split_message(post), [post])
    long = [dict(s1, url=G + "y" * 480 + str(i), url_key=f"L{i}", headline_key=f"long {w}",
                 headline=f"The {w} story with a long tracking link")
            for i, w in enumerate(("first", "second", "third", "fourth", "fifth"))]
    whole = news.layout("**AI News, Thu 8 Oct**\n<@111>\n" + news.render(long))
    parts = news.split_message(whole, limit=2000)
    check("five long links are past 2,000 characters", len(whole) > 2000, True)
    check("...split BETWEEN stories, every part within the limit",
          (len(parts), all(len(p) <= 2000 for p in parts),
           sum(len(nc.bullets(p)) for p in parts)), (2, True, 5))
    check("...and the second part opens with the heading again, never a bare list",
          parts[1].split("\n")[:2], ["**AI News, Thu 8 Oct (continued)**", ""])
    check("...the tags line is not repeated", "<@111>" in parts[1], False)


def _env_example(names) -> dict:
    import os
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        ".env.example")
    out = {}
    for line in open(path, encoding="utf-8").read().splitlines():
        key, _, value = line.partition("=")
        if key in names:
            out[key] = value.strip()
    return out


def _code_defaults(config) -> tuple:
    """The defaults written in config.py, read from its source so the laptop's
    .env cannot answer for them."""
    import inspect
    src = inspect.getsource(config)
    a = re.search(r'_int\("NEWS_OVERFLOW_MIN_IMPORTANCE",\s*(\d+)\)', src)
    b = re.search(r'_int\("NEWS_OVERFLOW_MAX_ITEMS",\s*(\d+)\)', src)
    return (int(a.group(1)) if a else None, int(b.group(1)) if b else None)


async def replay(check) -> None:
    """The 8 Oct 11:22 exchange, as a section of the shared replay harness."""
    print("\n8 Oct 11:22 - NEWS-OCT8 - '@Saley what is in the news forthis hour?' "
          "(before 2 PM, a leftover test day set to Wed 7 Oct)")
    question = "what is in the news forthis hour?"
    live = await nc.plain_three_times(False, question)
    test = await nc.plain_three_times(True, question)
    reply = live["answers"][0][0]
    print("\n".join("   " + ln for ln in reply.split("\n")))
    check("8 Oct: it is read as a plain news question", news.plain_question(question), True)
    check("8 Oct: routed to the news list, not the engine", live["routes"][0], "news")
    check("8 Oct: zero model, router, extractor and scoring calls", live["calls"][0],
          {"model": 0, "router": 0, "extractor": 0, "scorer": 0, "sheet_reads": 0})
    check("8 Oct: one message, headed for the day the bot is on",
          (len(live["answers"][0]), reply.split("\n")[0]), (1, nc.HEADING))
    check("8 Oct: five stories, each a bold headline with a whole link",
          [bool(LINE_RE.match(ln)) for ln in nc.bullets(reply)], [True] * 5)
    check("8 Oct: no 'already posted at 2 PM', no 'Here's what ran', no window",
          (nc.banned_in(reply), "Here's" in reply, "news.google.com\n" in reply), ([], False, False))
    check("8 Oct: asked again, none of those five come back",
          sorted(set(nc.headlines(reply)) & set(nc.headlines(live["answers"][1][0]))), [])
    check("8 Oct: test mode == live — answers, records and calls",
          nc.same(live, test, ("answers", "records", "calls", "routes")), [])


async def run(check) -> None:
    pure(check)
    await answers(check)
    await daily(check)
