"""R10 "CLOSURE SUPPORT" (rebuilt 13 Oct 2026) — the checks, shared by verify_closure_support.py and
tests/test_closure_support.py.

    sys.path.insert(0, <repo>/tests); import closure_support_checks as csc
    csc.run(check)                 # check(name, got, want)

WHAT RUNS HERE IS THE REAL THING: made-up rows through the real sheet parser, the real rule (`nextaction.run`),
the real renderer (`drip`, `wording.closure_post`), and a real bot behind a fake Discord with a throwaway database
for the news lookup (`bot._closure_news`), the send and the marker (`rule_last_posted`). The news is rows put into
the real `news_feed_items` table the way the feed poller stores them. No network, no model, no real sheet.

Every R10 setting is pinned to its .env.example default, never read from this machine's .env.

EVERY NAME, HEADLINE, LINK AND DATE HERE IS MADE UP (the links are on news.example).
"""
import asyncio
import contextlib
import logging
import os
import re
import sys
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)
os.environ.setdefault("DISCORD_TOKEN", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import config  # noqa: E402
import deadlines as dl  # noqa: E402
import drip  # noqa: E402
import feeds  # noqa: E402
import links  # noqa: E402
import news  # noqa: E402
import nextaction  # noqa: E402
import rules as rules_mod  # noqa: E402
import rule13_fixtures as fx  # noqa: E402
import wording  # noqa: E402

MON = date(2026, 10, 19)                       # a Monday
NEXT_MON = MON + timedelta(days=7)
COLUMN_NAMES = ("Prospect Status", "Closure Prob", "Deal Status")
OPENER = "These deals are in the closure stage:"
CLOSE = ("Can I help with any of these? I can pull together the PoC's background, a note on the company, "
         "or a package summary")
NOTICE_BOTH = ("No closure support this week — Prospect Status and Closure Prob% have empty values in the "
               "GTM sheet.")
_ABSENT = object()


def _example(name: str) -> str:
    for line in open(os.path.join(ROOT, ".env.example"), encoding="utf-8"):
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip()
    raise AssertionError(f"{name} is not in .env.example")


def _list(name: str) -> list:
    return [x.strip() for x in _example(name).split(",") if x.strip()]


def pins() -> dict:
    return {
        "CLOSURE_SUPPORT_MIN": int(_example("CLOSURE_SUPPORT_MIN")),
        "CLOSURE_SUPPORT_STAGES": _list("CLOSURE_SUPPORT_STAGES"),
        "CLOSURE_STOP_MARKERS": _list("CLOSURE_STOP_MARKERS"),
        "CLOSURE_NEWS_FIRST_RUN_DAYS": int(_example("CLOSURE_NEWS_FIRST_RUN_DAYS")),
        "DRIP_REASK_DAYS": int(_example("DRIP_REASK_DAYS")),
    }


@contextlib.contextmanager
def pinned(**extra):
    values = dict(pins(), **extra)
    before = {k: getattr(config, k, _ABSENT) for k in values}
    for k, v in values.items():
        setattr(config, k, v)
    try:
        yield
    finally:
        for k, v in before.items():
            if v is _ABSENT:
                with contextlib.suppress(AttributeError):
                    delattr(config, k)
            else:
                setattr(config, k, v)


# -- rows -------------------------------------------------------------------------------------------------------------

def deal(n, name, company, status="", pct="", **cells):
    """One ACTIVE row: contacted, with a connection date. NOT marked Connected unless asked, so rule 13 has no
    say in who this rule names (see `the_list` for what happens when it does)."""
    cells.setdefault("connected", "")
    return fx.person(n, name=name, company=company, first_contact="TRUE", li_date=date(2026, 9, 1),
                     prospect=status, closure=pct, **cells)


def filler(n=90):
    """A row that keeps both columns non-empty, so a silent Monday is a silent Monday and not the notice."""
    return deal(n, "Lee Lead", "Elsewhere Co", "Lead", "10%")


def example_values() -> list:
    return [deal(1, "Bo Chen", "Borealis", "Demo", "65%"), deal(2, "Ada Lovelace", "Acme AI", "Quote", "70%")]


def rows_of(values) -> list:
    return list(fx.parse(values).rows)


def engine(values, day=MON):
    rows = rows_of(values)
    return nextaction.run(today=day, rows=rows, prospect_rows=rows, day_rules=[rules_mod.by_id("R10")],
                          next_step_state={}, dm_no_meeting_state={})


def r10(result) -> list:
    return [a for a in result["actions"] if a.get("rule_id") == "R10"]


def named(result) -> list:
    return [a["poc"] for a in drip.closure_order(r10(result))]


def message_of(result, day=MON):
    for m in drip.group(result["actions"], day=day):
        if m.get("type") == nextaction.R_CLOSURE_SUPPORT:
            return m
    return None


RENDERED: list = []      # every deals post
NOTICES: list = []       # every empty-columns notice


def post_of(result, day=MON) -> str:
    m = message_of(result, day)
    text = drip.compose_fallback(m) if m else ""
    if text:
        (NOTICES if drip.notice_of(m) else RENDERED).append(text)
    return text


def one(values, day=MON) -> tuple:
    result = engine(values, day)
    return named(result), post_of(result, day)


def story(title, company, *, seen, published=None, importance=3, url=None, kind="poc", ref=None) -> dict:
    """A row of the news store, as the poller writes one for a company search."""
    url = url if url is not None else "https://news.example/" + re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return {"url": url, "url_key": news.url_key(url) or url, "headline_key": news.headline_key(title),
            "title": title, "summary": "", "source": "Example Wire",
            "published_at": feeds.utc_iso(published or seen), "topic_hint": "", "seen_at": feeds.utc_iso(seen),
            "kind": kind, "sheet_ref": (f"{company} — on Outreach PoCs" if ref is None else ref),
            "_importance": importance}


def seed(w, stories) -> None:
    w.bot.db.news_feed_add([{k: v for k, v in s.items() if not k.startswith("_")} for s in stories])
    w.bot.db.news_feed_set_scores(
        [{"url_key": s["url_key"], "importance": s["_importance"], "topic": "funding", "what": ""} for s in stories],
        scored_at=feeds.utc_iso(dl.real_now_ist()))


def markers(w) -> dict:
    with w.bot.db.conn() as c:
        return {r["rule_id"]: r["posted_at"] for r in c.execute("SELECT rule_id, posted_at FROM rule_last_posted")}


def when(d) -> str:
    return wording.short_date(dl.iso(d.astimezone(dl.IST).date()))


class LogTap(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.INFO)
        self.lines = []

    def emit(self, record):
        with contextlib.suppress(Exception):
            self.lines.append((record.levelname, record.getMessage()))


@contextlib.contextmanager
def tapped():
    tap, logger = LogTap(), logging.getLogger()
    was = logger.level
    logger.addHandler(tap)
    logger.setLevel(logging.INFO)
    try:
        yield tap
    finally:
        logger.removeHandler(tap)
        logger.setLevel(was)


# -- the checks -----------------------------------------------------------------------------------------------------------

def who_is_picked(check):
    print("\nWHO IS PICKED — a closure stage AND strictly above 50%")
    check("a closure stage at 70%: in. At 50%: out. At 51%: in",
          [named(engine([deal(1, "Ann Day", "Co", "Demo", p), filler()])) for p in ("70%", "50%", "51%", "49%")],
          [["Ann Day"], [], ["Ann Day"], []])
    check("...and the reason line says exactly 50 would not qualify",
          "exactly 50% would not qualify" in r10(engine([deal(1, "Ann Day", "Co", "Demo", "70%")]))[0]["why"], True)
    with tapped() as tap:
        names, post = one([deal(1, "Blank Bea", "Hollow Co", "Demo", ""), deal(2, "Ann Day", "Co", "Quote", "70%")])
    check("a closure stage with a blank percentage: out, and NOT mentioned anywhere in the post",
          (names, "Blank Bea" in post, "Hollow Co" in post, post.count("\n1. "), "2. " in post),
          (["Ann Day"], False, False, 1, False))
    check("...it is logged, not posted",
          [("Blank Bea" in m and "left out" in m) for lvl, m in tap.lines if "NO Closure Prob%" in m], [True])
    check("a closure stage at 0%: out (zero is terminal for every rule)",
          (named(engine([deal(1, "Zed Zero", "Co", "Demo", "0%"), filler()])),
           bool(nextaction.stop_reason(rows_of([deal(1, "Zed Zero", "Co", "Demo", "0%")])[0]))), ([], True))
    check("a percentage but no closure stage: out",
          named(engine([deal(1, "Lia Lead", "Co", "Lead", "80%"), deal(2, "No Status", "Co", "", "80%")])), [])
    check('Prospect Status "No deal": out. "Deal": in',
          [named(engine([deal(1, "Ann Day", "Co", s, "70%"), filler()])) for s in ("No deal", "Deal", "deal")],
          [[], ["Ann Day"], ["Ann Day"]])
    check("the stage is matched on whole words, never inside a longer word, and not when negated",
          [bool(nextaction.closure_stage(s)) for s in (
              "Demo", "Demo done", "Quote sent", "In negotiation", "Proposal", "Demoted", "Dealer", "Misquoted",
              "No deal", "Not a demo yet", "no quote", "")],
          [True, True, True, True, True, False, False, False, False, False, False, False])
    check("Won, Lost, Dead, Unresponsive and Not interested: out, as everywhere",
          [named(engine([deal(1, "Ann Day", "Co", "Demo", "70%", **kw), filler()]))
           for kw in ({"deal": "Won"}, {"deal": "Lost"}, {"deal": "Dead"}, {"deal": "Unresponsive"},
                      {"deal": "Not interested"})], [[]] * 5)
    for status in ("Lost", "Dead"):
        check(f'...and a Prospect Status of "{status}" stops the row too',
              named(engine([deal(1, "Ann Day", "Co", status, "70%"), filler()])), [])
    inactive = fx.values_row(1, name="Never Touched", company="Co", prospect="Demo", closure="90%")
    rows = rows_of([inactive])
    import activation
    check("a row with no first-contact or connection date is not active, so this rule never sees it",
          (len(activation.split(rows)[0]), "prospect_rows" in nextaction._r_closure_support.__code__.co_names), (0, False))


def notice_or_silence(check):
    print("\nTHE EMPTY-COLUMNS NOTICE — or nothing at all")
    blank = [deal(1, "Ann Day", "Co"), deal(2, "Bo Chen", "Borealis")]
    names, post = one(blank)
    check("both columns blank on every active row: the notice, in the new wording",
          (names, post), ([], NOTICE_BOTH))
    check('...without the old "Fill them in" sentence', [w for w in ("Fill them in", "next Monday") if w in post], [])
    check("Closure Prob% blank on every active row: the notice naming that column",
          one([deal(1, "Ann Day", "Co", "Demo"), deal(2, "Bo Chen", "Borealis", "Lead")])[1],
          "No closure support this week — Closure Prob% has empty values in the GTM sheet.")
    check("Prospect Status blank on every active row: the notice naming that column",
          one([deal(1, "Ann Day", "Co", "", "70%"), deal(2, "Bo Chen", "Borealis", "", "20%")])[1],
          "No closure support this week — Prospect Status has empty values in the GTM sheet.")
    check("the notice keeps posting: the Monday after says it again, word for word",
          (one(blank, NEXT_MON)[1], one(blank, NEXT_MON + timedelta(days=7))[1]), (NOTICE_BOTH, NOTICE_BOTH))
    check("...and the re-ask clock does not hold a post that went out last Monday",
          drip.stage_for("closure_support|*", history={"closure_support|*": {"last_nudge": dl.iso(MON)}},
                         today=NEXT_MON)[0] is not None, True)
    with tapped() as tap:
        quiet = one([deal(1, "Ann Day", "Co", "Demo", "40%"), deal(2, "Lia Lead", "Co", "Lead", "80%")])
    check("the columns have values and nothing qualifies: NO post at all",
          quiet, ([], ""))
    check("...and the log says why", [("Nothing posted" in m) for _l, m in tap.lines if "no closure support today" in m],
          [True])
    with tapped() as tap:
        empty = nextaction.run(today=MON, rows=[], day_rules=[rules_mod.by_id("R10")])
    check("no rows at all (the sheet is empty or unreadable): no post, logged at ERROR",
          (r10(empty), [lvl for lvl, m in tap.lines if "[R10]" in m and "nothing is posted" in m]), ([], ["ERROR"]))
    check("a deal qualifies while a column is otherwise sparse: the deals post, never both",
          one([deal(1, "Ann Day", "Co", "Demo", "70%"), deal(2, "Bo Chen", "Borealis")])[0], ["Ann Day"])


def the_list(check):
    print("\nTHE LIST — person first, highest percentage first, no cap, no rotation")
    names, post = one(example_values())
    check("the post: the opener, the numbered deals, a blank line, the close",
          post, f"{OPENER}\n1. Ada Lovelace (Acme AI) — 70% at Quote\n2. Bo Chen (Borealis) — 65% at Demo\n\n{CLOSE}")
    check("the line format: the person comes first, the company in brackets, then the percentage and the stage",
          (post.split("\n")[1], post.split("\n")[1].index("Ada Lovelace") < post.split("\n")[1].index("Acme AI")),
          ("1. Ada Lovelace (Acme AI) — 70% at Quote", True))
    check("the close has lost 'just tell me which' and has no full stop after 'summary'",
          (post.split("\n")[-1], "just tell me which" in post), (CLOSE, False))
    solo = engine([deal(1, "Ada Lovelace", "Acme AI", "Quote", "70%"), filler()])
    m = message_of(solo)
    check("ONE deal is still a numbered list of one, with the opener and the close",
          post_of(solo), f"{OPENER}\n1. Ada Lovelace (Acme AI) — 70% at Quote\n\n{CLOSE}")
    check("...rendered in code, not composed: R10 is verbatim, one post whatever the owners, with no template",
          (drip.is_verbatim(m), nextaction.R_CLOSURE_SUPPORT in drip.VERBATIM_TYPES,
           nextaction.R_CLOSURE_SUPPORT in drip.RULE_ONLY_TYPES, nextaction.R_CLOSURE_SUPPORT in drip._ASK),
          (True, True, True, False))
    check("the other openers and closes are gone",
          [n for n in ("CLOSURE_OPENERS", "CLOSURE_CLOSES") if hasattr(drip, n)], [])
    twelve = [deal(i, f"Person {chr(64 + i)}", f"Company {chr(64 + i)}", "Demo", f"{50 + i}%") for i in range(1, 13)]
    names, post = one(twelve)
    check("twelve qualifying deals: all twelve are named, though the rule's max_items_per_post is 5",
          (len(names), len(re.findall(r"^\d+\. ", post, re.MULTILINE)), rules_mod.by_id("R10").max_items_per_post),
          (12, 12, 5))
    check("...no truncation and no '+N more'", [w for w in ("more", "+", "…") if w in post], [])
    check("...and the message's own count of people is all twelve", len(drip.shown_contacts(message_of(engine(twelve)))), 12)
    order = [deal(1, "Sixty A", "Co A", "Demo", "60%"), deal(2, "Ninety", "Co B", "Quote", "90%"),
             deal(3, "Sixty B", "Co C", "Deal", "60%"), deal(4, "Seventy", "Co D", "Proposal", "70%"),
             deal(5, "Fraction", "Co E", "Demo", "0.95")]
    check("the order is strictly by percentage, highest first; a tie goes to sheet order",
          one(order)[0], ["Fraction", "Ninety", "Seventy", "Sixty A", "Sixty B"])
    first, second = one(order, MON), one(order, NEXT_MON)
    check("two consecutive Mondays with no sheet change name the same deals in the same order: no rotation",
          (first[0] == second[0], first[1] == second[1], len(first[0])), (True, True, 5))
    src = open(os.path.join(ROOT, "nextaction.py"), encoding="utf-8").read()
    body = src[src.index("def closure_order("):src.index("def _r_new_pipeline_company(")]
    check("...R10 keeps no state and reads none, and the code says why",
          ("_state" in body.replace("dm_no_meeting_state", "").replace("next_step_state", ""),
           "NO ROTATION, DELIBERATELY" in body), (False, True))
    pair = [deal(1, "Ada Lovelace", "Acme AI", "Quote", "70%"), deal(2, "Alan Turing", "Acme AI", "Demo", "60%")]
    check("two deals at the same company are two lines", one(pair)[0], ["Ada Lovelace", "Alan Turing"])
    # UNCHANGED, AND WORTH KNOWING: one person is named once a day and rule 13 is listed first. A deal whose
    # person rule 13 names that Monday is NOT in this list that day.
    shared = [deal(1, "Ada Lovelace", "Acme AI", "Quote", "70%", connected="Connected", step="Send email 1"),
              deal(2, "Bo Chen", "Borealis", "Demo", "65%")]
    rows = rows_of(shared)
    both = nextaction.run(today=MON, rows=rows, prospect_rows=rows, next_step_state={}, dm_no_meeting_state={},
                          day_rules=[rules_mod.by_id("R13"), rules_mod.by_id("R10")])
    check("which rule wins a contact is unchanged: a person rule 13 names that Monday is rule 13's, not this list's",
          sorted((a["rule_id"], a["poc"]) for a in both["actions"]),
          [("R10", "Bo Chen"), ("R13", "Ada Lovelace")])


def the_news(check):
    import replies_world as rw

    print("\nTHE NEWS LINE — read from the stored news, since the last closure support post")
    now = dl.real_now_ist()
    today = dl.real_today_ist()

    async def post_with(stories, values, *, marker=_ABSENT, ref_r2=False):
        with rw.World(False, pins=dict(pins())) as w:
            seed(w, stories)
            if marker is not _ABSENT:
                w.bot.db.set_rule_last_posted("R10", posted_at=feeds.utc_iso(marker), message_id="1")
            msg = message_of(engine(values, today), today)
            before, calls = markers(w), w.counts()
            await w.bot._research_message(msg, today=today)
            text = drip.compose_fallback(msg)
            RENDERED.append(text)
            after = w.counts()
            return {"text": text, "moved": markers(w) != before,
                    "spent": (after["model"] - calls["model"], after.get("searches", 0) - calls.get("searches", 0)),
                    "pending": msg.get("web_pending")}

    def run(*a, **k):
        with pinned():
            return asyncio.run(post_with(*a, **k))

    def news_lines(text):
        return [ln for ln in text.split("\n") if ln.startswith("   In the news: ")]

    values = example_values()
    fresh = story("Acme AI raises $40M", "Acme AI", seen=now - timedelta(days=1), published=now - timedelta(days=2))
    old = story("Borealis opens a Pune office", "Borealis", seen=now - timedelta(days=5))
    got = run([fresh, old], values, marker=now - timedelta(days=3))
    want_line = f"   In the news: {links.link(fresh['title'], fresh['url'])}, {when(now - timedelta(days=2))}"
    check("a story first seen AFTER the marker appears, indented under its deal, as "
          "'In the news: [headline](link), <day> <month>'",
          got["text"].split("\n")[:4],
          [OPENER, "1. Ada Lovelace (Acme AI) — 70% at Quote", want_line,
           "2. Bo Chen (Borealis) — 65% at Demo"])
    check("...the headline is the link text, through links.link, with a scheme and the embed suppressed",
          (want_line, bool(re.search(r"\[Acme AI raises \$40M\]\(<https://news\.example/[^>]+>\)", want_line))),
          (f"   In the news: [Acme AI raises $40M](<{fresh['url']}>), {when(now - timedelta(days=2))}", True))
    check("...and a story first seen BEFORE the marker does not", "Pune" in got["text"], False)
    check("nothing was fetched: no model call, no search, and the message waits on no research",
          (got["spent"], got["pending"], got["moved"]), ((0, 0), False, False))

    gap = story("Borealis signs a telco", "Borealis", seen=now - timedelta(hours=2), published=now - timedelta(days=10))
    got = run([gap], values, marker=now - timedelta(days=3))
    check("a story PUBLISHED before the marker but first SEEN after it DOES appear",
          news_lines(got["text"]),
          [f"   In the news: {links.link(gap['title'], gap['url'])}, {when(now - timedelta(days=10))}"])

    six = story("Acme AI hires an evals lead", "Acme AI", seen=now - timedelta(days=6))
    eight = story("Borealis ships a new model", "Borealis", seen=now - timedelta(days=8))
    got = run([six, eight], values)
    check("the first ever run, with no marker: seven days back, and no further",
          (len(news_lines(got["text"])), "evals lead" in got["text"], "ships a new model" in got["text"]),
          (1, True, False))
    fortnight = story("Acme AI opens in Berlin", "Acme AI", seen=now - timedelta(days=12))
    got = run([fortnight], values, marker=now - timedelta(days=14))
    check("a Monday was missed: the window is the last post, so a story from twelve days ago is still shown",
          "opens in Berlin" in got["text"], True)

    pair = [deal(1, "Ada Lovelace", "Acme AI", "Quote", "70%"), deal(2, "Alan Turing", "Acme AI", "Demo", "60%")]
    got = run([fresh], pair, marker=now - timedelta(days=3))
    check("two deals at the same company each get a news line (the same story under both)",
          (len(news_lines(got["text"])), got["text"].split("\n")[1::2][:2]),
          (2, ["1. Ada Lovelace (Acme AI) — 70% at Quote", "2. Alan Turing (Acme AI) — 60% at Demo"]))

    got = run([], values, marker=now - timedelta(days=3))
    check("no news for any deal: the post renders with no news lines and no 'no news' text",
          (got["text"], [w for w in ("no news", "No news", "In the news") if w in got["text"]]),
          (f"{OPENER}\n1. Ada Lovelace (Acme AI) — 70% at Quote\n2. Bo Chen (Borealis) — 65% at Demo\n\n{CLOSE}",
           []))

    newer = deal(3, "New Nia", "Nimbus Labs", "Demo", "80%")
    got = run([story("Nimbus Labs raises a seed round", "Nimbus Labs", seen=now - timedelta(days=2))],
              values + [newer], marker=now - timedelta(days=3))
    check("a deal that first qualifies this week still gets its news: the window is the marker, not the deal",
          got["text"].split("\n")[1:3],
          ["1. New Nia (Nimbus Labs) — 80% at Demo",
           "   In the news: " + links.link("Nimbus Labs raises a seed round",
                                           "https://news.example/nimbus-labs-raises-a-seed-round")
           + f", {when(now - timedelta(days=2))}"])

    low = story("Acme AI sponsors a meetup", "Acme AI", seen=now - timedelta(hours=3), importance=1)
    high = story("Acme AI raises $40M", "Acme AI", seen=now - timedelta(days=2), importance=5)
    got = run([low, high], values, marker=now - timedelta(days=3))
    check("one story a deal: the most important one, by the importance R1 gave it",
          (len(news_lines(got["text"])), "raises $40M" in got["text"], "meetup" in got["text"]), (1, True, False))

    wrong = [story("Nudge raises a round", "Nudge", seen=now - timedelta(days=1)),
             story("Borealis Robotics launches", "Borealis Robotics", seen=now - timedelta(days=1)),
             story("Ada Lovelace gives a keynote", "x", seen=now - timedelta(days=1),
                   ref="Ada Lovelace, Acme AI — on Outreach PoCs"),
             story("Acme AI is in this industry headline", "x", seen=now - timedelta(days=1), kind="industry", ref="")]
    got = run(wrong, values + [deal(3, "Nia Shah", "Nudge Security", "Demo", "55%")], marker=now - timedelta(days=3))
    check("the company is matched on its FULL name: not a shorter name, not a longer one, not a story about a "
          "person there, and not a headline that merely mentions it", news_lines(got["text"]), [])
    same = run([story("ACME-AI ships", "x", seen=now - timedelta(days=1), ref="acme-ai inc"),
                story("Borealis grows", "x", seen=now - timedelta(days=1), ref="BOREALIS Ltd \u2014 on Master Pipeline")],
               values, marker=now - timedelta(days=3))
    check("...while the same name cased, spaced or suffixed differently is the same company (R2's own key)",
          len(news_lines(same["text"])), 2)
    long_title = story("Acme AI raises forty million dollars in a new round", "Acme AI", seen=now - timedelta(days=1))
    got = run([long_title], values, marker=now - timedelta(days=3))
    check("a long headline is shortened by links.link (six words), never rewritten",
          news_lines(got["text"]),
          [f"   In the news: {links.link(long_title['title'], long_title['url'])}, {when(now - timedelta(days=1))}"])
    bad = [story("Acme AI has a link with no scheme", "Acme AI", seen=now - timedelta(days=1),
                 url="news.example/acme-no-scheme")]
    got = run(bad, values, marker=now - timedelta(days=3))
    check("a stored link with no http(s) scheme is never emitted: that story is not shown", news_lines(got["text"]), [])
    every = [m for t in RENDERED for m in re.findall(r"\]\(([^)]*)\)", t)]
    check("every link in every rendered message carries a scheme and is masked",
          (len(every) >= 5, [u for u in every if not re.match(r"^<https?://[^\s>]+>$", u)],
           [t for t in RENDERED if links.bare_urls(t)]), (True, [], []))


def the_marker(check):
    import replies_world as rw

    print("\nTHE MARKER — R10's own row in rule_last_posted, moved only by a live deals post")
    today = dl.real_today_ist()
    r2_at = "2026-10-01T10:00:00+00:00"

    async def live(test_mode=False):
        out = {}
        with rw.World(test_mode, pins=dict(pins())) as w:
            w.bot.db.set_rule_last_posted("R2", posted_at=r2_at, message_id="r2")
            msg = message_of(engine(example_values(), today), today)
            before = markers(w)
            # A PREVIEW: the queue, the news lookup and the text, and nothing sent.
            await w.bot._research_message(msg, today=today)
            text = drip.compose_fallback(msg)
            out["preview_moved"] = markers(w) != before
            # THE POST LANDS, and the sender's after-send bookkeeping runs (as R2's checks stage it).
            n = len(w.posted)
            post = w.bot_says(drip.with_heading(text, drip.heading("R10")))
            await w.bot._after_send(msg, sent=post, marker=dl.iso(today))
            out["posts"] = [rw.strip_tag(m.content) for m in w.posted[n:]]
            out["after"] = markers(w)

            # THE NOTICE names no deal and shows no news: it does not move the marker.
            with w.bot.db.conn() as c:
                c.execute("DELETE FROM rule_last_posted WHERE rule_id = 'R10'")
            notice = message_of(engine([deal(1, "Ann Day", "Co")], today), today)
            await w.bot._after_send(notice, sent=post, marker=dl.iso(today))
            out["after_notice"] = markers(w)
            # A DAY THAT IS NOT TODAY (a test of last Monday) does not move it either.
            await w.bot._after_send(msg, sent=post, marker=dl.iso(today - timedelta(days=7)))
            out["after_other_day"] = markers(w)
        return out

    async def test_day():
        with rw.World(True, pretend=(MON, 13, 0), rules={"R10"}, pins=dict(pins())) as w:
            w.sheet.set(pocs=example_values())
            w.bot.db.set_rule_last_posted("R2", posted_at=r2_at, message_id="r2")
            before = markers(w)
            await w.bot._run_next_actions(today=MON)
            planned = await w.bot._plan_drip(today=MON, already=[])
            msg = next((m for m in (planned or {}).get("messages") or []
                        if m.get("type") == nextaction.R_CLOSURE_SUPPORT), None)
            n, model = len(w.posted), w.counts()["model"]
            if msg:
                await w.bot._research_message(msg, today=MON)
                await w.bot._send_drip_message(w.chan, msg, marker=dl.iso(MON), channel_id=w.chan.id)
            return {"planned": bool(msg), "posts": [rw.strip_tag(m.content) for m in w.posted[n:]],
                    "tagged": [m.content.startswith("[TEST") for m in w.posted[n:]],
                    "model": w.counts()["model"] - model,
                    "moved": markers(w) != before, "after": markers(w)}

    with pinned():
        got = asyncio.run(live())
        pretend = asyncio.run(test_day())
    body = f"{OPENER}\n1. Ada Lovelace (Acme AI) — 70% at Quote\n2. Bo Chen (Borealis) — 65% at Demo\n\n{CLOSE}"
    check("a preview (the queue, the news lookup, the text) leaves rule_last_posted untouched", got["preview_moved"], False)
    check("the live post: the heading, then the list",
          ([p.split("\n")[0] for p in got["posts"]], [p.endswith(body) for p in got["posts"]]),
          (["**Closure support**"], [True]))
    RENDERED.extend(got["posts"])
    check("...and it writes R10's own row, and never R2's",
          (sorted(got["after"]), got["after"].get("R2"), bool(got["after"].get("R10"))), (["R10", "R2"], r2_at, True))
    check("the empty-columns notice does not move the marker", got["after_notice"], {"R2": r2_at})
    check("a send for a day that is not today does not move it", got["after_other_day"], {"R2": r2_at})
    check("a test day (a pretend Monday) through the real planner and sender: the same post goes out, tagged "
          "[TEST], with no model call, and the marker does not move",
          (pretend["planned"], [p.endswith(body) for p in pretend["posts"]], pretend["tagged"], pretend["model"],
           pretend["moved"], pretend["after"]),
          (True, [True], [True], 0, False, {"R2": r2_at}))
    src = open(os.path.join(ROOT, "bot.py"), encoding="utf-8").read()
    check("R10 writes the marker in ONE place, behind the live-send gate, and builds no second table",
          (len(re.findall(r'set_rule_last_posted\("R10"', src)),
           len(re.findall(r"CREATE TABLE IF NOT EXISTS rule_last_posted", open(os.path.join(
               ROOT, "db.py"), encoding="utf-8").read()))), (1, 1))
    body_src = src[src.index("async def _closure_news("):src.index("async def _record_dm_no_meeting(")
                   if src.index("async def _closure_news(") < src.index("async def _record_dm_no_meeting(")
                   else src.index("async def _closure_news(") + 3000]
    check("the news lookup fetches nothing: no search, no feed poll, no model call in it",
          [w for w in ("self.search", "_research_items", "feeds.poll", "self.llm", "aiohttp", "requests.")
           if w in body_src], [])


def never_in_the_message(check):
    print("\nNEVER IN A DEALS POST — a column name")
    for values in (example_values(), [deal(1, "Ada Lovelace", "Acme AI", "Quote", "70%"), filler()]):
        one(values)
    check("some deals posts were rendered to look through", len(RENDERED) >= 10, True)
    check("no deals post names a column", sorted({w for t in RENDERED for w in COLUMN_NAMES if w in t}), [])
    check("...the empty-columns notice names its two columns on purpose, and nothing else does",
          (len(NOTICES) >= 3, sorted({w for t in NOTICES for w in COLUMN_NAMES if w in t})),
          (True, ["Closure Prob", "Prospect Status"]))
    check("no deals post says there is no news", [t for t in RENDERED if re.search(r"\bno news\b", t, re.I)], [])


def settings(check):
    print("\nSETTINGS — the one new variable, the boot lines, the rules file")
    src = open(os.path.join(ROOT, "config.py"), encoding="utf-8").read()
    check("CLOSURE_NEWS_FIRST_RUN_DAYS is 7 in the code and in .env.example",
          (bool(re.search(r'_int\("CLOSURE_NEWS_FIRST_RUN_DAYS", 7\)', src)), _example("CLOSURE_NEWS_FIRST_RUN_DAYS")),
          (True, "7"))
    check("the unchanged ones are unchanged",
          (_example("CLOSURE_SUPPORT_MIN"), _example("CLOSURE_SUPPORT_STAGES"), _example("CLOSURE_STOP_MARKERS")),
          ("50", "deal,demo,quote,demo done,quote sent,proposal,negotiation",
           "dead,unresponsive,won,lost,closed won,closed lost,closed-won,closed-lost,not interested,"
           "do not contact,dnc"))
    with pinned(), tapped() as tap:
        saved = config.NEXT_ACTION_ENABLED
        config.NEXT_ACTION_ENABLED = True
        try:
            config.validate()
        finally:
            config.NEXT_ACTION_ENABLED = saved
    line = [m for _l, m in tap.lines if "[config] R10 closure support" in m]
    check("the boot line prints the new behaviour: no cap, highest first, where the news comes from",
          [("no cap" in m, "highest percentage first" in m, "rule_last_posted 'R10'" in m, "nothing fetched" in m,
            "looks back 7d" in m, "strictly above 50%" in m) for m in line], [(True,) * 6])
    bot_src = open(os.path.join(ROOT, "bot.py"), encoding="utf-8").read()
    check("the startup log counts the rows whose Closure Prob% is exactly 0",
          "row(s) have Closure Prob%% = 0 and are stopped for " in bot_src, True)
    rule = rules_mod.by_id("R10")
    check("R10: Mondays, counted toward the cap, still last in Monday's order",
          (rule.weekdays, rule.counts_toward_cap, rule.enabled), ((0,), True, True))
    wordy = " ".join(str(getattr(rule, "sheet_wording", "") or "").split())
    check("the Bot Rules wording says what the rule now does",
          [w for w in ("Every Monday", "above 50", "50 itself doesn't count", "highest first", "no cap",
                       "since the last closure support post", "blank on every active row") if w not in wordy], [])
    check("...and not the old notice or the old cap", [w for w in ("Fill them in", "5 deals") if w in wordy], [])


def run(check):
    with pinned():
        who_is_picked(check)
        notice_or_silence(check)
        the_list(check)
    the_news(check)
    the_marker(check)
    with pinned():
        never_in_the_message(check)
    settings(check)
