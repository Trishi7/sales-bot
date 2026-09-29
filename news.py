"""R1 AND R2 — a daily AI industry feed on a fixed topic list.

THE DECISION FROM THE 24 AND 29 SEP CALLS. R1 is no longer about our people.
It searched the Outreach PoCs, the researcher mapping and the Master Pipeline
by name, and most days nothing is written about eight particular mid-market AI
people — so the post was either empty or a fallback that announced itself. The
team asked for the other thing: what moved in AI, on the topics they care
about, once a day, and a tap on the shoulder when something big happens.

So R1 is now two shapes of ONE search:

    MAIN   at NEWS_MAIN_TIME (14:00), the last 24 hours, up to NEWS_MAX_ITEMS
           stories. It is R1's drip slot, so it counts toward the day's cap.

    CHECK  hourly at NEWS_CHECK_TIMES, silent unless something is MAJOR
           (importance >= NEWS_BREAKING_MIN_IMPORTANCE). A check that finds
           something posts ONE grouped "Worth knowing now:" message straight to
           the channel — never through drip_sends, never against the cap — and
           at most NEWS_BREAKING_MAX_PER_DAY of them a day.

NOTHING IS SEARCHED FOR FROM OUR SHEETS. No PoCs, no mapping, no pipeline, no
departures list. The topics come from config (NEWS_TOPICS), and they are SEEDS,
NOT LIMITS: important news off the list is welcome, tagged OTHER.

ONE STORY IS POSTED ONCE. `news_stories` remembers every story that went out,
main or breaking, by normalised URL AND by headline key — four outlets give one
funding round four URLs, and the headline key is what catches the second one.
It is also what keeps a story that broke at 16:00 out of the next day's 14:00
post: `choose` skips it and says so.

THE SPREAD RULES YIELD TO IMPORTANCE. At most NEWS_PER_TOPIC_PER_DAY stories on
one topic a day and NEWS_TOPICS_PER_WEEK distinct topics a week, so a feed does
not become "evals, evals, evals" — but a story at or above the breaking bar
walks past both, because a spread rule that hid the day's biggest story would
be the wrong rule.

R2 READS WHAT R1 POSTED. The news-company screen wants companies in today's
news that are NOT in the Master Pipeline. It reads today's rows from
`news_stories` rather than searching again, and asks before anything is added
to a sheet. It never writes.

NOTHING HERE OPENS A SOCKET. This module builds prompts, parses and chooses; the
one search call is `llm.web_research(lean=True)`, made by the caller in bot.py,
which also banks the budget. Keeping the I/O out means everything here can be
tested without a network or an API key — see `_self_test`.
"""
import logging
import re
from datetime import date, datetime, timedelta
from typing import Optional
from urllib.parse import urlsplit

import config
import deadlines as dl

log = logging.getLogger(__name__)

# The modes. MAIN and CHECK are the two prompts; BREAKING is how a check's post
# renders and what it is recorded as.
MODE_MAIN = "main"
MODE_CHECK = "check"
MODE_BREAKING = "breaking"

# The tag for a story that is not on the topic list. Welcome — the list is
# seeds, not limits — but named as such.
TOPIC_OTHER = "OTHER"

# The most headlines the prompt is told not to return. The day's main post plus
# a couple of breaking messages never gets near it; it is a ceiling on prompt
# size, not a working number.
ALREADY_MAX = 25

# How many significant tokens make a headline key.
HEADLINE_KEY_TOKENS = 8


# -- normalising a story's URL ------------------------------------------------

_TRACKING = re.compile(r"[?&](utm_[^=]+|ref|ref_src|fbclid|gclid|mc_cid|mc_eid)=[^&]*",
                       re.IGNORECASE)


def url_key(url: str) -> str:
    """The identity of a story's link, for the no-repeats check.

    THE SAME STORY ARRIVES WEARING DIFFERENT CLOTHES: with and without "www.",
    http and https, with a tracking query bolted on by whoever linked it, with
    and without a trailing slash. Those are one story and must collapse to one
    key, or the dedup silently does nothing and the same funding round is posted
    on Monday, Tuesday and Wednesday.

    Two outlets are still two URLs — that is what `headline_key` is for.
    """
    raw = str(url or "").strip()
    if not raw:
        return ""
    raw = _TRACKING.sub("", raw)
    raw = re.sub(r"^https?://", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"^www\.", "", raw, flags=re.IGNORECASE)
    raw = raw.split("#", 1)[0].rstrip("/?&")
    return raw.lower()


# -- normalising a story's headline -------------------------------------------

# Words that carry no identity. Two outlets writing "OpenAI raises $40bn in a
# new round" and "OpenAI Raises $40 Billion In New Funding Round" should land on
# the same key, and it is the nouns and verbs that make that happen.
_STOPWORDS = frozenset("""
a an the and or but nor of in on at to for from by with as into onto over under
about after before than then that this these those is are was were be been being
has have had do does did will would can could may might should shall its it
their his her our your my we you they he she them us new says said report reports
reportedly just now today amid via vs versus up out off per
""".split())


def headline_key(headline: str) -> str:
    """The first 8 significant lowercase tokens of a headline.

    Stopwords, digits and punctuation are dropped: "$40bn" and "$40 billion"
    differ only in the parts that go, and the key is about WHO did WHAT.
    """
    tokens: list = []
    for raw in re.findall(r"[A-Za-z0-9']+", str(headline or "").lower()):
        tok = re.sub(r"[\d']", "", raw)
        if len(tok) < 2 or tok in _STOPWORDS:
            continue
        tokens.append(tok)
        if len(tokens) >= HEADLINE_KEY_TOKENS:
            break
    return " ".join(tokens)


# -- building the search ------------------------------------------------------


def sweep_prompt(topics: list, *, today: date, since_hours: int, mode: str,
                 already: Optional[list] = None,
                 preferred: Optional[list] = None) -> str:
    """ONE prompt for both the main sweep and the hourly check.

    THE TOPICS ARE SEEDS, NOT LIMITS, and the prompt says so in the sheet's own
    words. A regulation that lands off the list is exactly what the team wants
    to hear about; it is tagged OTHER rather than left out.

    `already` is every headline posted today, main AND breaking. The model is
    told not to return them — and `choose` enforces it anyway, because a prompt
    is a request and the database is a fact.

    `preferred` is a PREFERENCE LINE, never a tool domain restriction: a site on
    it that blocks the crawler must not be able to fail the call.
    """
    hours = max(1, int(since_hours))
    seeds = [str(t).strip() for t in (topics or []) if str(t).strip()]
    if mode == MODE_CHECK:
        lines = [
            f"Search for ONLY things in AI that are MAJOR in the last {hours} hours "
            f"(today is {dl.iso(today)}) — a big launch, a large round, a regulation, "
            "a leadership move the whole industry is discussing. If nothing is "
            "major, reply with exactly: NOTHING FOUND",
        ]
    else:
        lines = [
            f"Search for the most significant AI news of the last {hours} hours "
            f"(today is {dl.iso(today)}) for a sales team at an AI-data company.",
        ]
    if seeds:
        lines += [
            "",
            "TOPICS — these are SEEDS, NOT LIMITS. The team's sheet says 'not limited "
            "to these', so important news off this list is welcome; tag it OTHER:",
            "  " + ", ".join(seeds),
        ]
    prior = [str(h).strip() for h in (already or []) if str(h).strip()][:ALREADY_MAX]
    if prior:
        lines += ["", "ALREADY POSTED TODAY — do not return these, or the same story "
                      "from another outlet:"]
        lines += [f"  - {h}" for h in prior]
    domains = [str(d).strip() for d in (preferred or []) if str(d).strip()]
    if domains:
        lines += ["", "Prefer these sources when they have the story: "
                      + ", ".join(domains)]
    lines += [
        "",
        "IMPORTANCE, 1-5:",
        "  5 = the whole industry is talking about it today",
        "  4 = a sales team must know this week",
        "  3 = useful",
        "  2-1 = filler",
        "",
        "FOR EACH STORY, one line in exactly this shape and nothing else:",
        "  STORY | <topic from the list, or OTHER> | <headline> | "
        "<what happened, one clause> | <url> | <importance 1-5>",
        "",
        "EVERY LINE NEEDS A REAL URL you actually found. No url, no line.",
        "The url must be the PRIMARY report — the company's own announcement or "
        "a named outlet's article. Never a roundup, digest, newsletter or "
        "'everything that happened today' page.",
        "One line per story — the same story from two outlets is ONE line.",
    ]
    if mode != MODE_CHECK:
        lines.append("If you genuinely found nothing, reply with exactly: NOTHING FOUND")
    return "\n".join(lines)


def screen_prompt(stories: list, known_companies: list, *, use_cases: str = "") -> str:
    """R2 — which of today's news companies are NOT in the pipeline.

    READS THE STORIES R1 ALREADY POSTED. The question is about a result set we
    are holding, and searching again to answer it would spend the budget twice.

    THE USE-CASE TABLE RIDES IN HERE, in the user prompt, because the lean
    system prompt no longer carries the strategy doc. Only that section, capped.
    """
    lines = [
        "Below are news stories posted today, and the list of companies the team "
        "already tracks in its Master Pipeline.",
        "",
        "Name the companies that appear in the STORIES but are NOT in the TRACKED "
        "list. For each one, ONE line saying whether it is relevant to membrane and "
        "why, judged against the use-case table (A-J and who buys each) below. "
        "Name the use case you matched, or say plainly that it matches none.",
        "",
        "FORMAT, one per line and nothing else:",
        "  SCREEN | <company> | <fit or no fit, and which use case> | <url>",
        "",
        "DO NOT propose adding anything to any sheet. A human is asked before any "
        "row is added and that is a separate step you are not part of.",
        "If every company in the news is already tracked, reply: NOTHING NEW",
    ]
    if use_cases.strip():
        lines += ["", "MEMBRANE'S USE CASES (from the sales strategy):", use_cases.strip()]
    lines += ["", "TODAY'S STORIES:"]
    for s in stories[:20]:
        head = s.get("headline") or s.get("title") or s.get("about") or "?"
        what = s.get("what") or ""
        lines.append(f"  - {head}: {what} <{s.get('url', '')}>")
    lines += ["", "ALREADY TRACKED:", "  " + ", ".join(known_companies[:200])]
    return "\n".join(lines)


def extract_section(text: str, heading: str, *, cap: int = 4000) -> str:
    """One `#`-headed section of a markdown document, by its heading's words.

    Everything from the heading line to the next heading of the same or a
    higher level. Matched case-insensitively on the words, so "## 2. What we
    sell" is found by "What we sell". "" when there is no such heading.
    """
    want = " ".join(str(heading or "").lower().split())
    if not want:
        return ""
    out: list = []
    level = 0
    for line in str(text or "").splitlines():
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            this = len(m.group(1))
            if level and this <= level:
                break
            if not level and want in " ".join(m.group(2).lower().split()):
                level = this
        if level:
            out.append(line)
    body = "\n".join(out).strip()
    return body[:max(0, int(cap))]


# -- reading what came back ---------------------------------------------------

_STORY_RE = re.compile(r"^\s*[-*•]?\s*STORY\s*\|", re.IGNORECASE)
_SCREEN_RE = re.compile(r"^\s*SCREEN\s*\|", re.IGNORECASE)
_URL_RE = re.compile(r"https?://[^\s<>)\]|]+")
_IMPORTANCE_RE = re.compile(r"^\D*([1-5])\b")

# A DIGEST IS NOT A SOURCE. A roundup page links to the story; it is not the
# story, and a sales team clicking through lands on twenty other things.
_DIGEST_RE = re.compile(
    r"digest|roundup|newsletter|everything-that-happened|news-brief|daily-brief",
    re.IGNORECASE,
)


def digest_link_reason(url: str, *, blocked: Optional[list] = None) -> str:
    """Why this url is not a primary report, or "" when it may be one.

    The HOST or PATH naming a digest/roundup/newsletter, or a host on
    NEWS_BLOCKED_DOMAINS (a subdomain of a blocked domain counts).
    """
    parts = urlsplit(str(url or "").strip())
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    for d in (blocked if blocked is not None else (config.NEWS_BLOCKED_DOMAINS or [])):
        d = str(d or "").strip().lower().lstrip(".")
        if d.startswith("www."):
            d = d[4:]
        if d and (host == d or host.endswith("." + d)):
            return f"{host} is on NEWS_BLOCKED_DOMAINS"
    m = _DIGEST_RE.search(host) or _DIGEST_RE.search(parts.path or "")
    if m:
        return f"the link looks like a {m.group(0).lower()} page, not the primary report"
    return ""


def _canonical_topic(raw: str, topics: Optional[list]) -> str:
    """The topic as the list spells it, or OTHER."""
    text = " ".join(str(raw or "").split()).strip(" []")
    if not text:
        return TOPIC_OTHER
    if text.upper() == TOPIC_OTHER:
        return TOPIC_OTHER
    for t in (topics if topics is not None else (config.NEWS_TOPICS or [])):
        if str(t).strip().lower() == text.lower():
            return str(t).strip()
    return TOPIC_OTHER


def parse_stories(text: str, *, topics: Optional[list] = None) -> list:
    """The STORY lines, as [{topic, headline, what, url, url_key, headline_key,
    importance}].

    A LINE WITHOUT A URL IS DROPPED, and logged. "No url, no line" is a rule
    this enforces rather than hopes for: a claim nobody can check does not go
    into a sales channel.

    FORGIVING ABOUT EVERYTHING ELSE. An unreadable importance is 3 ("useful"),
    which can never break through on its own; a topic not on the list, or
    missing, is OTHER.
    """
    out: list = []
    for line in str(text or "").splitlines():
        if not _STORY_RE.match(line):
            continue
        parts = [p.strip() for p in line.split("|")]
        # parts[0] is "STORY"
        fields = parts[1:]
        found = None
        url_at = -1
        for i, f in enumerate(fields):
            m = _URL_RE.search(f)
            if m:
                found, url_at = m, i
                break
        if found is None:
            log.info("[news] dropped a story with no source link: %r", line[:120])
            continue
        url = found.group(0).rstrip(".,;)")
        why = digest_link_reason(url)
        if why:
            log.info("[news] dropped a story with a digest link — %s: %s | %r",
                     why, url, line[:120])
            continue
        before = fields[:url_at]
        after = fields[url_at + 1:]
        topic = _canonical_topic(before[0] if len(before) >= 3 else "", topics)
        head_fields = before[1:] if len(before) >= 3 else before
        headline = head_fields[0] if head_fields else ""
        what = " — ".join(head_fields[1:]) if len(head_fields) > 1 else ""
        headline = _URL_RE.sub("", headline).strip(" -–—")
        what = _URL_RE.sub("", what).strip(" -–—")
        if not headline:
            continue
        importance = 3
        if after:
            m = _IMPORTANCE_RE.match(after[0])
            if m:
                importance = int(m.group(1))
        out.append({
            "topic": topic, "headline": headline, "what": what, "url": url,
            "url_key": url_key(url), "headline_key": headline_key(headline),
            "importance": importance,
        })
    return out


def parse_screen(text: str) -> list:
    """The SCREEN lines, as [{company, verdict, url}]."""
    out: list = []
    for line in str(text or "").splitlines():
        if not _SCREEN_RE.match(line):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            continue
        company = parts[1]
        verdict = parts[2]
        tail = " ".join(parts[3:]) if len(parts) > 3 else ""
        found = _URL_RE.search(tail) or _URL_RE.search(line)
        url = found.group(0).rstrip(".,;)") if found else ""
        verdict = _URL_RE.sub("", verdict).strip(" -–—|")
        if company and verdict:
            out.append({"company": company, "verdict": verdict, "url": url})
    return out


def found_nothing(text: str) -> bool:
    body = " ".join(str(text or "").split()).upper()
    return "NOTHING FOUND" in body or "NOTHING NEW" in body


# -- choosing what goes out ---------------------------------------------------


def iso_week(day: date) -> str:
    y, w, _ = day.isocalendar()
    return f"{y}-W{w:02d}"


def cutoff_iso(today: date) -> str:
    """The date before which a posted story is allowed to be posted again."""
    return dl.iso(today - timedelta(days=max(1, int(config.NEWS_REPEAT_DAYS))))


def choose(stories: list, *, today: date, db, cap: int, per_topic: int,
           topics_per_week: int, min_importance: int,
           breaking: bool = False) -> tuple:
    """(keep, skipped). Every skip is a sentence saying why.

    THE GATES, IN ORDER:
      0. (breaking only) importance below the bar — a check posts only MAJOR;
      1. posted within NEWS_REPEAT_DAYS, by url_key OR headline_key — this is
         what keeps a story that broke at 16:00 out of tomorrow's main post;
      2. per-topic-per-day room;       } a story at or above min_importance
      3. topics-per-week room;         } walks past both
      4. the cap.

    Most important first, so the cap trims filler rather than the big one. Two
    lines in one batch that are the same story (same link or same headline
    key) count once.
    """
    since = cutoff_iso(today)
    marker = dl.iso(today)
    bar = int(min_importance)
    ranked = sorted(enumerate(stories or []),
                    key=lambda p: (-int(p[1].get("importance") or 3), p[0]))

    try:
        week_topics = set(db.news_topics_this_week(iso_week(today)))
    except Exception:
        log.exception("[news] could not read this week's topics; treating the week "
                      "as full so only the important breaks through")
        week_topics = None
    topic_counts: dict = {}

    keep: list = []
    skipped: list = []
    batch_urls: set = set()
    batch_heads: set = set()

    def _skip(s, why):
        skipped.append(f"{s.get('headline', '?')!r} — {why}")

    for _i, s in ranked:
        imp = int(s.get("importance") or 3)
        big = imp >= bar
        topic = s.get("topic") or TOPIC_OTHER
        if breaking and not big:
            _skip(s, f"importance {imp} is below the breaking bar of {bar}")
            continue
        ukey, hkey = s.get("url_key") or "", s.get("headline_key") or ""
        if (ukey and ukey in batch_urls) or (hkey and hkey in batch_heads):
            _skip(s, "the same story is already in this batch")
            continue
        seen = db.news_story_seen(ukey, hkey, since_iso=since)
        if seen:
            how = "the same link" if seen.get("url_key") == ukey and ukey else \
                "the same headline"
            _skip(s, f"already posted on {seen.get('posted_on', '?')} "
                     f"({seen.get('kind') or 'main'} post) — {how}, within "
                     f"{config.NEWS_REPEAT_DAYS} days")
            continue
        if topic not in topic_counts:
            try:
                topic_counts[topic] = int(db.news_topic_count_today(topic, marker))
            except Exception:
                topic_counts[topic] = int(per_topic)
        if topic_counts[topic] >= int(per_topic) and not big:
            _skip(s, f"{topic} already has {topic_counts[topic]} story/stories today "
                     f"(NEWS_PER_TOPIC_PER_DAY={per_topic})")
            continue
        if not big:
            full = week_topics is None or (
                topic not in week_topics and len(week_topics) >= int(topics_per_week))
            if full:
                _skip(s, f"{topic} would be topic number "
                         f"{len(week_topics or ()) + 1} this week "
                         f"(NEWS_TOPICS_PER_WEEK={topics_per_week})")
                continue
        if len(keep) >= int(cap):
            _skip(s, f"the post is full (cap {cap})")
            continue
        keep.append(s)
        batch_urls.add(ukey)
        if hkey:
            batch_heads.add(hkey)
        topic_counts[topic] = topic_counts.get(topic, 0) + 1
        if week_topics is not None:
            week_topics.add(topic)
    return keep, skipped


# -- what the message says ----------------------------------------------------

_PING_RE = re.compile(r"<@[!&]?\d+>|@(everyone|here)\b", re.IGNORECASE)


def _no_pings(text: str) -> str:
    """Web text never pings anybody: mention tokens are removed outright."""
    return _PING_RE.sub(lambda m: ("@​" + m.group(1)) if m.group(1) else "",
                        str(text or ""))


def _topic_label(topic: str) -> str:
    return "Other" if (topic or TOPIC_OTHER) == TOPIC_OTHER else topic


def render(stories: list, *, mode: str = MODE_MAIN) -> str:
    """One line per story: `• [Topic] Headline — what happened <url>`.

    `mode="breaking"` opens with "Worth knowing now:" and carries every story
    it is given, in ONE message. No attribution block, no fallback
    announcement: the feed is the feed.
    """
    picked = [s for s in (stories or []) if s.get("url")]
    if not picked:
        return ""
    lines: list = []
    if mode == MODE_BREAKING:
        lines.append("Worth knowing now:")
    for s in picked:
        head = _no_pings(s.get("headline") or "")
        what = _no_pings(s.get("what") or "")
        body = f"{head} — {what}" if what else head
        lines.append(f"• [{_topic_label(s.get('topic'))}] {body} <{s['url']}>")
    return "\n".join(lines)


def render_screen(rows: list, *, limit: int = 5) -> str:
    """R2's text: companies in the news that we do not track."""
    picked = [r for r in rows if r.get("company")][:max(1, int(limit))]
    if not picked:
        return ""
    lines = ["In the news today and not in the Master Pipeline:"]
    for r in picked:
        link = f" <{r['url']}>" if r.get("url") else ""
        lines.append(f"• {_no_pings(r['company'])} — {_no_pings(r['verdict'])}{link}")
    lines.append("Say the word and I'll add any of these — I won't add anything "
                 "without a yes.")
    return "\n".join(lines)


# -- the hourly check's clock -------------------------------------------------


def _minutes(hhmm: str) -> Optional[int]:
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(hhmm or ""))
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return None
    return h * 60 + mi


def check_slots(times: Optional[list] = None) -> list:
    """NEWS_CHECK_TIMES as sorted, de-duplicated "HH:MM" strings."""
    out = {}
    for t in (times if times is not None else config.NEWS_CHECK_TIMES) or []:
        m = _minutes(t)
        if m is None:
            log.warning("[news-check] NEWS_CHECK_TIMES entry %r is not HH:MM; ignored", t)
            continue
        out[m] = f"{m // 60:02d}:{m % 60:02d}"
    return [out[k] for k in sorted(out)]


def latest_slot(now: datetime, times: Optional[list] = None) -> Optional[str]:
    """The latest check slot at or before `now`, or None before the first."""
    at = now.hour * 60 + now.minute
    best = None
    for s in check_slots(times):
        if _minutes(s) <= at:
            best = s
    return best


def hours_since_previous(slot: str, *, times: Optional[list] = None,
                         main_time: Optional[str] = None) -> int:
    """Hours between `slot` and the slot before it — a check slot or the main
    sweep — wrapping to yesterday for the first one. Never below 1."""
    at = _minutes(slot)
    if at is None:
        return 1
    marks = {_minutes(s) for s in check_slots(times)}
    main = _minutes(main_time if main_time is not None else config.NEWS_MAIN_TIME)
    if main is not None:
        marks.add(main)
    marks.discard(None)
    earlier = [m for m in marks if m < at]
    if earlier:
        gap = at - max(earlier)
    elif marks:
        gap = at + 24 * 60 - max(marks)
    else:
        gap = 60
    return max(1, round(gap / 60))


def _self_test() -> int:
    """`python -m news` — prompts, parsing, choosing, rendering. No I/O."""
    logging.basicConfig(level=logging.ERROR, format="%(levelname)-7s %(message)s")
    failures = 0

    def check(name, got, want):
        nonlocal failures
        ok = got == want
        failures += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")

    today = date(2026, 9, 29)
    topics = ["evals", "RLHF", "AI regulation", "voice agent"]

    print("url identity")
    check("tracking parameters are stripped",
          url_key("https://x.com/a?utm_source=news"), "x.com/a")
    check("www and scheme collapse",
          url_key("http://www.x.com/a/"), url_key("https://x.com/a"))
    check("fragments go", url_key("https://x.com/a#top"), "x.com/a")
    check("case is ignored", url_key("https://X.com/A"), "x.com/a")
    check("empty stays empty", url_key(""), "")
    check("two outlets are two links",
          url_key("https://a.com/x") == url_key("https://b.com/x"), False)

    print("\nheadline identity")
    check("stopwords, digits and case go",
          headline_key("OpenAI raises $40bn in a new round"),
          headline_key("OPENAI RAISES $40 BN IN NEW ROUND"))
    check("capped at 8 tokens",
          len(headline_key("one two three four five six seven eight nine ten").split()),
          8)
    check("different stories differ",
          headline_key("Anthropic ships Claude") == headline_key("Google ships Gemini"),
          False)

    print("\nthe main prompt")
    p = sweep_prompt(topics, today=today, since_hours=24, mode=MODE_MAIN,
                     already=["Old headline"], preferred=["reuters.com"])
    check("it says 24 hours", "last 24 hours" in p, True)
    check("it is for a sales team", "sales team at an AI-data company" in p, True)
    check("topics are seeds, not limits", "SEEDS, NOT LIMITS" in p, True)
    check("in the sheet's words", "not limited to these" in p, True)
    check("it lists the topics", "voice agent" in p, True)
    check("it forbids today's headlines", "Old headline" in p, True)
    check("preferred sites are a preference line",
          "Prefer these sources when they have the story: reuters.com" in p, True)
    check("it defines the scale", "5 = the whole industry is talking about it today" in p,
          True)
    check("it demands the shape",
          "STORY | <topic from the list, or OTHER> | <headline> | "
          "<what happened, one clause> | <url> | <importance 1-5>" in p, True)
    check("no url, no line", "No url, no line." in p, True)
    already = [f"h{i}" for i in range(40)]
    check("already is capped at 25",
          sweep_prompt(topics, today=today, since_hours=24, mode=MODE_MAIN,
                       already=already).count("\n  - h"), 25)

    print("\nthe check prompt")
    c = sweep_prompt(topics, today=today, since_hours=1, mode=MODE_CHECK)
    check("ONLY MAJOR", "ONLY things in AI that are MAJOR in the last 1 hours" in c, True)
    check("the honest empty", "if nothing is major, reply with exactly: NOTHING FOUND"
          in c.replace("If", "if"), True)
    check("no preference line without domains", "Prefer these sources" in c, False)

    print("\nparsing stories")
    text = (
        "Here is what I found.\n"
        "STORY | evals | Lab ships an eval suite | a new benchmark for agents | "
        "https://x.com/a?utm_source=q | 4\n"
        "STORY | Quantum pastry | Bakery raises seed | irrelevant | https://y.com/b | two\n"
        "STORY | RLHF | No link here | nothing to check | | 5\n"
        "STORY | | Untagged | but linked | https://z.com/c | 3\n"
        "not a story line\n"
    )
    got = parse_stories(text, topics=topics)
    check("three stories survive", len(got), 3)
    check("the linkless one is dropped",
          any(s["headline"] == "No link here" for s in got), False)
    check("the topic is the list's spelling", got[0]["topic"], "evals")
    check("an unknown topic is OTHER", got[1]["topic"], TOPIC_OTHER)
    check("a blank topic is OTHER", got[2]["topic"], TOPIC_OTHER)
    check("importance is read", got[0]["importance"], 4)
    check("unreadable importance is 3", got[1]["importance"], 3)
    check("the url key is normalised", got[0]["url_key"], "x.com/a")
    check("the url itself is kept whole", got[0]["url"], "https://x.com/a?utm_source=q")
    check("what happened is read", got[0]["what"], "a new benchmark for agents")
    check("the headline key is set", got[0]["headline_key"], "lab ships eval suite")

    print("\nchoosing")

    class FakeDB:
        def __init__(self, posted=(), topic_today=None, week=()):
            self.posted = list(posted)
            self.topic_today = dict(topic_today or {})
            self.week = list(week)

        def news_story_seen(self, ukey, hkey, *, since_iso):
            for r in self.posted:
                if (ukey and r["url_key"] == ukey) or (hkey and r["headline_key"] == hkey):
                    return r
            return None

        def news_topic_count_today(self, topic, on_date):
            return self.topic_today.get(topic, 0)

        def news_topics_this_week(self, week):
            return self.week

    def S(topic, head, imp=3, url=None):
        u = url or f"https://n.com/{headline_key(head).replace(' ', '-')}"
        return {"topic": topic, "headline": head, "what": "w", "url": u,
                "url_key": url_key(u), "headline_key": headline_key(head),
                "importance": imp}

    kw = dict(today=today, cap=5, per_topic=2, topics_per_week=6, min_importance=4)
    posted = [{"url_key": "other.com/x", "headline_key": headline_key("Big lab raises"),
               "posted_on": "2026-09-28", "kind": "breaking"}]
    keep, skipped = choose([S("evals", "Big lab raises", 5, "https://fresh.com/1"),
                            S("evals", "Eval tooling update")],
                           db=FakeDB(posted=posted), **kw)
    check("a story posted as breaking yesterday is skipped by its headline",
          [s["headline"] for s in keep], ["Eval tooling update"])
    check("...with a sentence that says so",
          "already posted on 2026-09-28 (breaking post) — the same headline" in skipped[0],
          True)

    keep, skipped = choose([S("evals", f"Eval story {w}") for w in
                            ("alpha", "beta", "gamma")], db=FakeDB(), **kw)
    check("per-topic room is two", len(keep), 2)
    check("the third says why", "NEWS_PER_TOPIC_PER_DAY=2" in skipped[0], True)
    keep, _ = choose([S("evals", f"Eval story {w}", 4) for w in
                      ("alpha", "beta", "gamma")], db=FakeDB(), **kw)
    check("importance at the bar bypasses per-topic", len(keep), 3)

    wk = ["t1", "t2", "t3", "t4", "t5", "t6"]
    keep, skipped = choose([S("evals", "Eval story alpha"), S("t1", "T one story")],
                           db=FakeDB(week=wk), **kw)
    check("a seventh topic in a week is refused", [s["topic"] for s in keep], ["t1"])
    check("...and says so", "NEWS_TOPICS_PER_WEEK=6" in skipped[0], True)
    keep, _ = choose([S("evals", "Eval story alpha", 5)], db=FakeDB(week=wk), **kw)
    check("...unless it is important", len(keep), 1)

    keep, skipped = choose([S("OTHER", f"Story {w}") for w in
                            ("alpha", "beta", "gamma", "delta")],
                           db=FakeDB(), **{**kw, "cap": 1, "per_topic": 9})
    check("the cap holds", len(keep), 1)
    check("...and says so", sum("the post is full" in x for x in skipped), 3)

    keep, skipped = choose([S("evals", "Minor thing", 3), S("RLHF", "Major thing", 4),
                            S("AI regulation", "Huge thing", 5)],
                           db=FakeDB(), **{**kw, "cap": 99}, breaking=True)
    check("breaking considers only the important",
          sorted(s["headline"] for s in keep), ["Huge thing", "Major thing"])
    check("...and says why the rest stayed quiet",
          "below the breaking bar of 4" in skipped[0], True)
    keep, _ = choose([S("evals", "Same story", 4, "https://a.com/1"),
                      S("evals", "Same story", 4, "https://b.com/2")],
                     db=FakeDB(), **kw)
    check("two outlets, one story", len(keep), 1)

    print("\nrendering")
    body = render([S("evals", "Lab ships", 4), S(TOPIC_OTHER, "Thing <@123> @everyone")])
    check("topic tag and link on every line", body.count("• ["), 2)
    check("the tag is the topic", body.startswith("• [evals] Lab ships — w <https://"), True)
    check("OTHER reads as Other", "[Other]" in body, True)
    check("no pings survive", "<@123>" in body or "@everyone" in body, False)
    b = render([S("evals", "A", 5), S("RLHF", "B", 4)], mode=MODE_BREAKING)
    check("breaking opens with the line", b.splitlines()[0], "Worth knowing now:")
    check("...and carries every story", b.count("• ["), 2)
    check("no stories, no text", render([]), "")

    print("\nthe check clock")
    slots = ["11:00", "12:00", "13:00", "15:00", "16:00"]
    check("the latest slot at or before now",
          latest_slot(datetime(2026, 9, 29, 15, 20), slots), "15:00")
    check("nothing before the first", latest_slot(datetime(2026, 9, 29, 9, 0), slots),
          None)
    check("15:00 looks back to the 14:00 main sweep",
          hours_since_previous("15:00", times=slots, main_time="14:00"), 1)
    check("11:00 looks back to last night's last slot",
          hours_since_previous("11:00", times=slots, main_time="14:00"), 19)
    check("bad entries are ignored", check_slots(["25:00", "9:05", "x"]), ["09:05"])

    print("\nthe strategy section")
    doc = "# T\n\n## 1. Who\nx\n\n## 2. What we sell\n| A | B |\n### sub\ny\n## 3. Goals\nz"
    sec = extract_section(doc, "What we sell")
    check("it starts at the heading", sec.splitlines()[0], "## 2. What we sell")
    check("it keeps sub-headings", "### sub" in sec, True)
    check("it stops at the next section", "Goals" in sec, False)
    check("it is capped", len(extract_section(doc, "What we sell", cap=10)), 10)

    print("\nthe screen")
    rows = parse_screen(
        "SCREEN | Nebius | fits use case C, inference infra | https://n.com/1\n"
        "SCREEN | Priority Tech | no fit, payments | https://p.com/2\n"
    )
    check("both screens read", len(rows), 2)
    check("the screen prompt carries the use cases",
          "MEMBRANE'S USE CASES" in screen_prompt([S("evals", "x")], ["Acme"],
                                                   use_cases="| A | x |"), True)
    check("it asks before adding", "without a yes" in render_screen(rows), True)

    print("\nfound-nothing")
    check("an honest empty", found_nothing("NOTHING FOUND"), True)
    check("...and the screen too", found_nothing("NOTHING NEW"), True)
    check("a real answer is not empty", found_nothing("STORY | a | b | c | d | 3"), False)

    print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
