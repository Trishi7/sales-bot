"""THE CHECKS for the news template (NFT2-1087, section 16) and the news answer.

ONE LINE SHAPE: `- Headline ([Outlet](<url>))` for every story - no bold, no
"- what" clause, no sheet tag - and a blank line between two stories and
nowhere else. The retired shapes (the bold line, the PoC line, "More AI News",
"Breaking AI News, <day>") are asserted gone.


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

LINE_RE = re.compile(r"^- [^*\n]+ \(\[[^\]\n]+\]\(<https?://[^>\s]+>\)\)$")


def _shape(check, label: str, text: str, heading: str, n: int, tail: str = "") -> None:
    lines = text.split("\n")
    check(f"{label}: the bold heading, with the day", lines[0], heading)
    check(f"{label}: NO blank line under the heading",
          (lines[1] if len(lines) > 1 else "").startswith("- "), True)
    items = nc.bullets(text)
    check(f"{label}: {n} bullet(s)", len(items), n)
    check(f"{label}: a blank line between two stories, and only there",
          (text.count("\n\n"), text.count("\n\n- ")), (n - 1, n - 1))
    check(f"{label}: only the heading is bold", text.count("**"), 2)
    if tail:
        # NEWS-ANSWER-OCT16: an answer with more left ends on the fixed tail.
        check(f"{label}: the tail, and nothing else, after the last story",
              (lines[-1], lines[-2].startswith("- ")), (tail, True))
    else:
        check(f"{label}: nothing after the last story", lines[-1].startswith("- "), True)


async def answers(check) -> None:
    print("\nANSWERS IN THE CHANNEL — \"any AI news?\" asked three times, nine stories collected")
    live = await nc.plain_three_times(False)
    test = await nc.plain_three_times(True)
    first, second, third = (a for a in live["answers"])
    check("each answer is ONE Discord message", [len(a) for a in live["answers"]], [1, 1, 1])
    print("   the first answer:")
    print("\n".join("   " + ln for ln in first[0].split("\n")))
    _shape(check, "first answer", first[0], nc.HEADING, 5,
           tail=news.answer_tail(2, "last 24 hours", 5))
    check("every line is a headline and its outlet, no bold, nothing else",
          all(nc_line for nc_line in map(LINE_RE.match, nc.bullets(first[0]))), True)
    by_score = dict(nc.NINE)
    got = nc.headlines(first[0])
    # NEWS-ANSWER-OCT16: the top of the last 24 hours up to the bot's now —
    # the test clock's 11:22 — so the 11:30 and 12:00 stories are not in it.
    check("the 5 highest-scored of the window (the two 5s, the two 4s, the newest 3)",
          sorted(by_score[h] for h in got), [3, 4, 4, 5, 5])
    check("...ranked by score, then newest",
          got, ["Cloud provider cuts inference prices",
                "Frontier lab ships a reasoning model",
                "Startup releases an open speech dataset",
                "Chipmaker unveils an inference accelerator",
                "Consortium drafts an agent safety standard"])
    check("the answer's stories are recorded as sent (kind=answer)",
          (len(live["records"][0]), set(live["records"][0].values())), (5, {"answer"}))
    got2 = nc.headlines(second[0])
    check("asked again: the same top five, repeats included (NEWS-ANSWER-OCT16)", got2, got)
    got3 = nc.headlines(third[0])
    check("asked a third time: the same again", got3, got)
    check("...and a repeat is not recorded twice",
          (len(live["records"][1]), len(live["records"][2])), (5, 5))
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
          (reply.startswith(nc.HEADING + "\n- ElevenLabs launches a Hindi voice agent (["),
           reply.endswith("On the sheet they are at the DM stage.")), (True, True))
    check("...still one message", len(live["replies"]), 1)
    check("test mode == live", nc.same(live, test, ("replies", "calls", "records")), [])

    live, test = await nc.quiet(False), await nc.quiet(True)
    check("nothing collected: an ANSWER still gets the quiet line, alone, and no model call",
          (live["replies"], live["calls"]["model"], live["records"]),
          ([news.quiet_line(nc.DAY)], 0, {}))
    check("test mode == live", nc.same(live, test, ("replies", "calls", "records")), [])


async def daily(check) -> None:
    print("\nTHE DAILY POST - ONE MESSAGE, after somebody asked at 13:55")
    live = await nc.answer_then_daily_post(False)
    test = await nc.answer_then_daily_post(True)
    check("the answer gave the five newest 5s", sorted(nc.headlines(live["answer"][0])),
          sorted(nc.ASKED))
    check("ONE message follows: the daily post, and no follow-up", len(live["posts"]), 1)
    post = (live["posts"] + [""])[0]
    print("   the daily post:")
    print("\n".join("   " + ln for ln in post.split("\n")))
    check("the daily post is ONE Discord message, under 2,000 characters",
          (len(live["raw"]), len(live["raw"][0]) <= 2000), (1, True))
    lines = post.split("\n")
    head = lines[0]
    check("its heading is bold and names the day (any tags ride on this same line)",
          head.startswith(nc.HEADING), True)
    check("the opener is the line right under the heading (no blank line)",
          lines[1], news.OPENER)
    check("both kinds of story: our label comes next, with no blank line",
          lines[2], news.LABEL_OURS)
    stories = nc.bullets(post)
    check("five stories in all", len(stories), 5)
    check("ours first: the flagged stories are the first two, in the score order",
          nc.headlines(post)[:2], [nc.POC, nc.POC2])
    check("...a flagged story the model scored 1 is still there (the flag wins)",
          nc.POC2 in nc.headlines(post), True)
    check("...each in the one line shape, no sheet tag, no '- what' clause, no bold",
          [bool(LINE_RE.match(ln)) for ln in stories], [True] * 5)
    check("...the PoC line word for word",
          stories[0], f"- {nc.POC} ([example.org](<https://example.org/synthflow-raises-20m-series-a>))")
    check("'Elsewhere in AI' follows our last story, then the industry stories",
          (lines[lines.index(news.LABEL_INDUSTRY) - 1].startswith("- "),
           [ln.startswith("- ") for ln in lines[lines.index(news.LABEL_INDUSTRY) + 1:]
            if ln.strip()][:-1]), (True, [True] * 3))
    check("the closer is the last line", lines[-1], news.CLOSER)
    check("a blank line between two stories, nowhere else",
          (post.count("\n\n"), post.count("\n\n- ")), (3, 3))
    check("only the heading is bold, and no line starts '- **'",
          (post.count("**"), "- **" in post), (2, False))
    check("the retired follow-up is not sent: no 'More AI News', no second message",
          ("More AI News" in "\n".join(live["posts"]), len(live["raw"])), (False, 1))
    check("NOT ONE story the 13:55 answer gave is in the daily post",
          sorted(set(nc.headlines(post)) & set(nc.ASKED)), [])
    check("the industry stories are the highest-scored left, never a 4 while a 5 waits",
          all(h in nc.MAJOR for h in nc.headlines(post)[2:]), True)
    nxt = nc.headlines(live["next_answer"][0])
    check("the next answer is the top of the window again, repeats included "
          "(NEWS-ANSWER-OCT16): the five newest 5s",
          sorted(nxt), sorted(nc.ASKED))
    check("...and what the daily post took is not hidden from it either: the post's "
          "stories are ranked like any other",
          all(h in nc.MAJOR + nc.ASKED for h in nxt), True)
    kinds = sorted(set(live["records"].values()))
    check("everything sent is recorded by how it went out - answer and main, no overflow",
          kinds, ["answer", "main"])
    check("no model call anywhere in this: not for the answer, the post or the scoring",
          (live["calls"]["model"], live["calls"]["router"], live["calls"]["scorer"]), (0, 0, 0))
    check("test mode == live: the same answer, post, next answer, records and calls",
          nc.same(live, test, ("answer", "posts", "next_answer", "records", "calls")), [])
    check("in test mode the proactive post carries the tag, on the heading line",
          [r.split("\n")[0] for r in test["raw"]][0].startswith(f"{test['prefix']} {nc.HEADING}"),
          True)
    check("in live mode it carries none", live["raw"][0].startswith(nc.HEADING), True)


def pure(check) -> None:
    import drip

    print("\nTHE TEMPLATE - news.render in every mode, exact text (NFT2-1087 section 16)")
    day = date(2026, 10, 8)
    G = "https://news.google.com/rss/articles/"
    s1 = {"headline": "State AGs launch investigations into OpenAI AI safety - Reuters",
          "source": "Reuters", "url": G + "CBMi1?oc=5", "url_key": "k1",
          "headline_key": "state ags launch", "importance": 5, "flag": 0,
          "kind": "industry", "published_at": "2026-10-08T05:00:00Z"}
    s2 = {"headline": "TM Forum and Accenture launch AI trust framework for telecoms.",
          "source": "AI News & Artificial Intelligence | TechCrunch",
          "url": "https://www.techcrunch.com/2026/10/08/tm-forum", "url_key": "k2",
          "headline_key": "tm forum accenture", "importance": 4, "flag": 0,
          "kind": "industry", "published_at": "2026-10-08T04:00:00Z"}
    s3 = {"headline": "ElevenLabs Doubles Its Valuation in Employee Tender",
          "what": "ElevenLabs doubled its valuation in employee tender offer.",
          "source": "streamlinefeed.co.ke", "url": "https://streamlinefeed.co.ke/el",
          "url_key": "k3", "headline_key": "elevenlabs doubles", "importance": 4,
          "flag": 1, "names": "ElevenLabs", "kind": "poc",
          "sheet_ref": "Elevenlabs - on Master Pipeline and Outreach PoCs",
          "published_at": "2026-10-08T03:00:00Z"}
    s4 = {"headline": "As AI gains autonomy, enterprises must retain authority | Hindustan Times",
          "source": "Hindustan Times", "url": G + "CBMi4?oc=5", "url_key": "k4",
          "headline_key": "ai gains autonomy", "importance": 3, "flag": 0,
          "kind": "industry", "published_at": "2026-10-08T02:00:00Z"}
    s5 = {"headline": "GPT-5 - what we know so far", "source": "Wired",
          "url": "https://www.wired.com/story/gpt-5", "url_key": "k5",
          "headline_key": "gpt 5 what know", "importance": 3, "flag": 0,
          "kind": "industry", "published_at": "2026-10-08T01:00:00Z"}
    five = [s1, s2, s3, s4, s5]
    L1 = f"- State AGs launch investigations into OpenAI AI safety ([Reuters](<{G}CBMi1?oc=5>))"
    L2 = ("- TM Forum and Accenture launch AI trust framework for telecoms "
          "([techcrunch.com](<https://www.techcrunch.com/2026/10/08/tm-forum>))")
    L3 = ("- ElevenLabs Doubles Its Valuation in Employee Tender "
          "([streamlinefeed.co.ke](<https://streamlinefeed.co.ke/el>))")
    L4 = (f"- As AI gains autonomy, enterprises must retain authority "
          f"([Hindustan Times](<{G}CBMi4?oc=5>))")
    L5 = "- GPT-5 - what we know so far ([wired.com](<https://www.wired.com/story/gpt-5>))"
    tags = "<@111> <@222>"
    head = "**AI News, Thu 8 Oct**"

    # A. both kinds (ours: s3; industry: s1 s2 s4 s5, only the first three fit after it)
    both = news.render(five, mode=news.MODE_MAIN, day=day)
    check("A. daily post, both kinds: opener, label, ours, 'Elsewhere in AI', industry, closer",
          both, "\n".join([
              news.OPENER, news.LABEL_OURS, L3, news.LABEL_INDUSTRY, L1, "", L2, "", L4, "",
              L5, news.CLOSER]))
    check("...the fixed text is the ticket's, word for word",
          (news.OPENER, news.LABEL_OURS, news.LABEL_INDUSTRY, news.CLOSER),
          ("Hey here's today's round-up:", "Our PoCs are in the news:", "Elsewhere in AI",
           "Let me know if you want me to dig into any of these"))
    check("...the whole daily post (news.daily_message): heading with the tags ON its line",
          news.daily_message(five, day=day, tags=tags), f"{head} {tags}\n{both}")
    check("...the drip sender's heading and tags line through news.layout is the same text",
          news.layout(f"{head} {tags}\n" + news.render(five)), f"{head} {tags}\n{both}")
    check("the daily post's heading is the same label (drip.HEADINGS)",
          drip.heading("R1", day=day), head)
    check("the breaking heading in the drip table is 'Breaking news'",
          drip.HEADINGS.get("R1_breaking"), "Breaking news")

    # B. only ours
    only_ours = news.render([s3, dict(s3, url="https://streamlinefeed.co.ke/el2", url_key="k3b",
                                      headline_key="elevenlabs two", headline="Eleven more news")])
    check("B. only our stories: 'Our PoCs are in the news:' and no 'Elsewhere in AI'",
          only_ours, "\n".join([news.OPENER, news.LABEL_OURS, L3, "",
                                "- Eleven more news ([streamlinefeed.co.ke](<https://streamlinefeed.co.ke/el2>))",
                                news.CLOSER]))
    # C. only industry
    only_ind = news.render([s1, s2, s4])
    check("C. only industry: opener, lines, closer - no label line at all",
          only_ind, "\n".join([news.OPENER, L1, "", L2, "", L4, news.CLOSER]))
    # D. nothing
    check("D. nothing to send: no text at all (the quiet line is retired for the daily post)",
          (news.render([]), news.daily_message([], day=day, tags=tags)), ("", ""))

    # E, F. breaking
    one = news.render([s1], mode=news.MODE_BREAKING, day=day, tags=tags)
    check("E. breaking, one story: '**Breaking news** <tags>' then the line, no date",
          one, f"**Breaking news** {tags}\n{L1}")
    many = news.render([s1, s2, s3], mode=news.MODE_BREAKING, day=day, tags=tags)
    check("F. breaking, several stories: heading, lines with a blank between, no opener or closer",
          many, f"**Breaking news** {tags}\n{L1}\n\n{L2}\n\n{L3}")
    check("...no opener, no label, no closer, no date in a breaking post",
          [x in many for x in (news.OPENER, news.LABEL_OURS, news.LABEL_INDUSTRY, news.CLOSER,
                               "Oct")], [False] * 5)
    check("a breaking post without tags is just '**Breaking news**'",
          news.render([s1], mode=news.MODE_BREAKING).split("\n")[0], "**Breaking news**")

    # G. answer
    ans = news.render([s1, s2], mode=news.MODE_ANSWER, day=day)
    check("G. an answer: '**AI News, <day>**', no blank line, same lines, no tags/opener/closer/labels",
          ans, f"{head}\n{L1}\n\n{L2}")
    check("...an answer even with a flagged story has no label and no tags",
          news.render([s3, s1], mode=news.MODE_ANSWER, day=day, tags=tags),
          f"{head}\n{L3}\n\n{L1}")

    # H. the line
    check("H. the line: no bold, no '- what' clause, no sheet tag, whatever the story is",
          [news.story_line(s) for s in (s1, s3)], [L1, L3])
    check("...no line of any rendered message starts '- **' or carries a sheet tag",
          [("- **" in t, "on Master Pipeline" in t, " — " in t)
           for t in (both, only_ours, only_ind, one, many, ans)], [(False, False, False)] * 6)
    check("the PoC branch is gone: a story with a kind and a sheet_ref still gets the one shape",
          news.story_line(dict(s1, kind="poc", sheet_ref="X - on Master Pipeline", what="Did a thing.")),
          L1)
    check("is_ours reads the flag, not the kind",
          (news.is_ours(dict(s1, kind="poc")), news.is_ours(dict(s1, flag=1, kind="industry")),
           news.is_poc(dict(s3))), (False, True, True))
    check("the retired names are gone from news.py",
          [n for n in ("MODE_OVERFLOW", "OVERFLOW_HEADING", "check_slots", "latest_slot",
                       "hours_since_previous", "POC_PER_NAME_IN_POST") if hasattr(news, n)], [])
    check("link previews stay suppressed: every link is <url>",
          len(re.findall(r"\]\(<https?://[^>]+>\)", both)), 5)

    six = [dict(s1, url=f"{G}six{i}", url_key=f"six{i}", headline_key=f"six key {w}",
                headline=f"The {w} lab ships a model", importance=5)
           for i, w in enumerate(("alpha", "bravo", "charlie", "delta", "echo", "foxtrot"))]
    check("six stories handed to any mode give five",
          [len(nc.bullets(news.render(six, mode=m, day=day)))
           for m in (news.MODE_MAIN, news.MODE_BREAKING, news.MODE_ANSWER)], [5, 5, 5])
    twin = dict(s1, url="https://apnews.com/x", url_key="twin", headline_key="another key",
                source="AP News",
                headline="State AGs launch investigations into OpenAI AI safety | AP News")
    check("two items about the same story give one (same headline, two outlets)",
          len(nc.bullets(news.render([s1, twin, s2]))), 2)
    check("...and by the same link, and by the same headline key",
          (len(nc.bullets(news.render([s1, dict(s2, url_key="k1")]))),
           len(nc.bullets(news.render([s1, dict(s2, headline_key="state ags launch")])))),
          (1, 1))
    check("a mention or an asterisk in feed text pings nobody and makes nothing bold",
          (news.story_line(dict(s1, headline="Lab <@123> ships @everyone **a** model")).count("<@"),
           "*" in news.story_line(dict(s1, headline="Lab <@123> ships **a** model"))),
          (0, False))
    check("a story whose line is over 1,500 characters is left out, the rest stay",
          nc.headlines(news.render([dict(s1, url=G + "z" * 1600), s2])),
          ["TM Forum and Accenture launch AI trust framework for telecoms"])

    print("\nONE MESSAGE - split only past 2,000 characters, between stories, with the heading again")
    post = news.layout(f"{head} <@111>\n" + news.render(five))
    check("a full daily post of five is one message", news.split_message(post), [post])
    long = [dict(s1, url=G + "y" * 480 + str(i), url_key=f"L{i}", headline_key=f"long {w}",
                 headline=f"The {w} story with a long tracking link")
            for i, w in enumerate(("first", "second", "third", "fourth", "fifth"))]
    whole = news.layout(f"{head} <@111>\n" + news.render(long))
    parts = news.split_message(whole, limit=2000)
    check("five long links are past 2,000 characters", len(whole) > 2000, True)
    check("...split BETWEEN stories, every part within the limit",
          (len(parts), all(len(p) <= 2000 for p in parts),
           sum(len(nc.bullets(p)) for p in parts)), (2, True, 5))
    check("...the second part opens with the heading again, no blank under it",
          parts[1].split("\n")[0], "**AI News, Thu 8 Oct (continued)**")
    check("...and goes straight to a story", parts[1].split("\n")[1].startswith("- "), True)
    check("...a (continued) part has no tags, opener, label or closer",
          [x in parts[1] for x in ("<@111>", news.OPENER, news.LABEL_OURS,
                                   news.LABEL_INDUSTRY, news.CLOSER)], [False] * 5)
    brk = news.layout("**Breaking news** <@111>\n" + news.render(long, mode=news.MODE_BREAKING)
                      .split("\n", 1)[1])
    check("...a breaking post that long continues as '**Breaking news (continued)**'",
          news.split_message(brk)[1].split("\n")[0], "**Breaking news (continued)**")


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
    check("8 Oct: five stories, each a headline with a whole link (no bold)",
          [bool(LINE_RE.match(ln)) for ln in nc.bullets(reply)], [True] * 5)
    check("8 Oct: no 'already posted at 2 PM', no 'Here's what ran', no window",
          (nc.banned_in(reply), "Here's" in reply, "news.google.com\n" in reply), ([], False, False))
    check("8 Oct: asked again, the same top five (repeats included since NEWS-ANSWER-OCT16)",
          nc.headlines(live["answers"][1][0]), nc.headlines(reply))
    check("8 Oct: test mode == live — answers, records and calls",
          nc.same(live, test, ("answers", "records", "calls", "routes")), [])


async def run(check) -> None:
    pure(check)
    await answers(check)
    await daily(check)
