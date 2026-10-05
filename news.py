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


# THE OFFERINGS R2 MAPS A COMPANY ONTO, in the words the team uses. Named in the
# prompt so "why it fits" is an offering, never a letter from a table.
SCREEN_OFFERINGS = (
    "evals", "post-training preference data", "red-teaming",
    "voice & multilingual speech", "agent trajectories", "Gen-Z research",
)


def screen_prompt(stories: list, known_companies: list, *, use_cases: str = "",
                  allow_lookup: bool = False) -> str:
    """R2 — which of today's news companies are NOT in the pipeline.

    READS THE STORIES R1 ALREADY POSTED. The question is about a result set we
    are holding, and searching again to answer it would spend the budget twice.

    JUDGED IN WORDS. The strategy's "What we sell" section (offerings, use
    cases, Phase 1 focus) rides in the user prompt, because the lean system
    prompt no longer carries the strategy doc. Only that section, capped.

    ONLY STORIES ABOUT ONE SPECIFIC COMPANY. Regulation, a city, a government
    or an industry-wide trend is not a company to screen; each such story comes
    back as a SKIP line with its reason, which the caller logs.
    """
    lines = [
        "Below are news stories posted today, and the list of companies the team "
        "already tracks in its Master Pipeline.",
        "",
        "Name the companies that a story is SPECIFICALLY ABOUT and that are NOT in "
        "the TRACKED list. For each one say, in plain words: what the company does "
        "(one clause), which membrane offering it maps to — "
        + ", ".join(SCREEN_OFFERINGS) + " — and why, or why it does not fit. Judge "
        "against the offerings and the Phase 1 focus in the strategy text below. "
        "Never refer to a use case by a letter or a number.",
        "",
        "SKIP any story that is not about one specific company — regulation, a "
        "city or government, or an industry-wide trend — with one SKIP line "
        "saying why.",
        "",
        "FORMAT, one per line and nothing else:",
        "  SCREEN | <company> | <what they do, one clause> | "
        "<why it fits or doesn't> | <url>",
        "  SKIP | <story headline> | <why it is not about a specific company>",
        "",
        "DO NOT propose adding anything to any sheet. A human is asked before any "
        "row is added and that is a separate step you are not part of.",
        "If every company in the news is already tracked, reply: NOTHING NEW",
    ]
    if allow_lookup:
        # ONLY WHEN THE STORY DOES NOT SAY. The stories are titles and one-line
        # summaries; most say what the company does. For one that does not, a
        # LOOKUP line asks the caller for a five-result search rather than
        # letting the model fill the gap from memory.
        lines += [
            "",
            "If a story names a company but does NOT say what it does, do not "
            "guess — instead of its SCREEN line write:",
            "  LOOKUP | <company>",
        ]
    if use_cases.strip():
        lines += ["", "WHAT MEMBRANE SELLS (from the sales strategy):", use_cases.strip()]
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
    """The SCREEN lines, as [{company, what, why, verdict, url}].

    Five fields (company | what | why | url). An older four-field line
    (company | verdict | url) still reads, with the verdict as `why`.
    """
    out: list = []
    for line in str(text or "").splitlines():
        if not _SCREEN_RE.match(line):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            continue
        found = _URL_RE.search(line)
        url = found.group(0).rstrip(".,;)") if found else ""
        fields = [_URL_RE.sub("", p).strip(" -–—|<>") for p in parts[1:]]
        fields = [f for f in fields if f]
        if not fields:
            continue
        company = fields[0]
        if len(fields) >= 3:
            what, why = fields[1], fields[2]
        else:
            what, why = "", (fields[1] if len(fields) > 1 else "")
        if company and (what or why):
            out.append({"company": company, "what": what, "why": why,
                        "verdict": why, "url": url})
    return out


_SKIP_RE = re.compile(r"^\s*SKIP\s*\|", re.IGNORECASE)


def parse_screen_skips(text: str) -> list:
    """The SKIP lines, as [{story, why}] — stories not about one company."""
    out: list = []
    for line in str(text or "").splitlines():
        if not _SKIP_RE.match(line):
            continue
        parts = [p.strip() for p in line.split("|")]
        out.append({"story": parts[1] if len(parts) > 1 else "",
                    "why": " ".join(parts[2:]).strip() if len(parts) > 2 else ""})
    return out


_LOOKUP_RE = re.compile(r"^\s*LOOKUP\s*\|\s*(.+?)\s*$", re.IGNORECASE)


def parse_screen_lookups(text: str) -> list:
    """The LOOKUP lines: companies the stories name without describing."""
    out: list = []
    for line in str(text or "").splitlines():
        m = _LOOKUP_RE.match(line)
        if m:
            name = m.group(1).split("|")[0].strip()
            if name and name not in out:
                out.append(name)
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


# What a quiet main sweep posts under its heading. A quiet hourly check posts
# nothing at all.
#
# THREE LINES, IN ROTATION BY DATE (`quiet_line`): three quiet days in a row
# read as three different sentences, and the same day always gets the same one,
# so a restart or a re-run does not change what was said. None of them says
# "nothing to act on" — news never asks for an action in the first place.
QUIET_LINES = (
    "Quiet day in AI — nothing worth your time today.",
    "Nothing new on the AI front since yesterday.",
    "Checked the news: nothing you need to see today.",
)
QUIET_MAIN = QUIET_LINES[0]
BREAKING_HEADING = "**Breaking AI news**"


def quiet_line(day: date) -> str:
    """The quiet-day line for `day` — one of QUIET_LINES, rotating by working
    day, so Friday and the Monday after it differ too (`tone.rotate`)."""
    import tone
    return tone.rotate(QUIET_LINES, day)


def render(stories: list, *, mode: str = MODE_MAIN) -> str:
    """One bullet per story: `• Headline — what happened. [site](<url>)`.

    NO TOPIC TAG AND NO CLOSING LINE — news never needs an action, so nothing
    after the bullets asks for one. The main post's heading ("AI news, Tue 29
    Sep") is added by the drip sender; a breaking post carries its own heading
    here, because it is sent outside the drip, and every story it is given, in
    ONE message.
    """
    import links

    picked = [s for s in (stories or []) if s.get("url")]
    if not picked:
        return ""
    lines: list = []
    if mode == MODE_BREAKING:
        lines.append(BREAKING_HEADING)
    for s in picked:
        head = _no_pings(s.get("headline") or "").strip()
        what = _no_pings(s.get("what") or "").strip().rstrip(".")
        body = f"{head} — {what}." if what else head
        lines.append(f"• {body} {links.link(_link_label(s), s['url'])}")
    return "\n".join(lines)


def _link_label(story: dict) -> str:
    """The masked link's name. "" (the site name) for an outlet's own link; the
    OUTLET for a Google News link, whose host would otherwise read
    "news.google.com" under every story it carried."""
    host = (urlsplit(str(story.get("url") or "")).hostname or "").lower()
    if host.endswith("news.google.com"):
        return _no_pings(story.get("source") or "").strip()
    return ""


# -- R1 from the feeds: choosing what the scorer sees, and reading its verdicts --
#
# THE NEWS IS NOT SEARCHED FOR ANY MORE. `feeds.poll()` stores what the outlets
# published; the light model is shown TITLES AND SUMMARIES ONLY — about 3k
# tokens for forty items — and gives each a topic and an importance. Nothing
# here opens a socket or calls a model; bot.py does both.

SCORE_TITLE_CHARS = 140
SCORE_SUMMARY_CHARS = 120


def future_note(day: date) -> str:
    """What R1's slot says for a date that has not happened. No model, no feed,
    no cost — there is no news from the future to report."""
    return (f"No news yet — {day.strftime('%a')} {day.day} {day.strftime('%b')} "
            "hasn't happened.")


def preselect(items: list, *, cap: int, per_hint: int = 3) -> list:
    """The feed items worth showing the scorer, at most `cap`, newest first.

    THE OUTLETS' OWN FEEDS GO FIRST (no `topic_hint`): they are AI desks and
    nearly everything on them is in scope — but they get at most 60% of the
    call, so a busy day on five desks cannot crowd out the topic queries, which
    are where a story off those desks (a regulator, an Indian outlet) arrives.
    The Google News queries fill the rest, at most `per_hint` per topic, so one
    busy topic ("robot") cannot spend the whole call; any room still left goes
    back to the outlets. Two rows with one headline key count once.
    """
    cap = max(1, int(cap))
    ordered = sorted(items or [], key=lambda r: str(r.get("published_at") or ""),
                     reverse=True)
    out: list = []
    heads: set = set()
    per: dict = {}

    def take(row, limit: int) -> bool:
        hkey = row.get("headline_key") or row.get("url_key")
        if hkey in heads or len(out) >= limit:
            return False
        heads.add(hkey)
        out.append(row)
        return True

    outlets = [r for r in ordered if not (r.get("topic_hint") or "").strip()]
    queries = [r for r in ordered if (r.get("topic_hint") or "").strip()]
    share = cap if not queries else max(1, int(cap * 0.6))
    for row in outlets:
        take(row, share)
    for row in queries:
        hint = row["topic_hint"].strip()
        if per.get(hint, 0) >= int(per_hint):
            continue
        if take(row, cap):
            per[hint] = per.get(hint, 0) + 1
    for row in outlets:
        take(row, cap)
    return out


def score_prompt(items: list, topics: list, *, today: date, mode: str) -> str:
    """ONE scoring call for a batch of feed items: titles and summaries only.

    The model does not search and is given no page text. It returns one SCORE
    line per item worth a 3 or more; everything it leaves out is recorded as
    filler (1) by the caller, so no item is ever paid for twice.
    """
    seeds = [str(t).strip() for t in (topics or []) if str(t).strip()]
    lines = [
        "Below are news items from RSS feeds — a title and sometimes a summary "
        f"each. Today is {dl.iso(today)}. Score them for a sales team at an "
        "AI-data company (human data, evals, RLHF, voice and speech data, "
        "red-teaming).",
        "",
        "TOPICS — these are SEEDS, NOT LIMITS. The team's sheet says 'not limited "
        "to these', so important news off this list is welcome; tag it OTHER:",
        "  " + ", ".join(seeds),
        "",
        "IMPORTANCE, 1-5:",
        "  5 = the whole industry is talking about it today",
        "  4 = a sales team must know this week",
        "  3 = useful",
        "  2-1 = filler",
    ]
    if mode == MODE_CHECK:
        lines.append("BE STRICT WITH 4 AND 5: this is an hourly check for MAJOR news "
                     "only — a big launch, a large round, a regulation, a leadership "
                     "move the whole industry is discussing.")
    lines += ["", "ITEMS:"]
    for i, it in enumerate(items, 1):
        title = " ".join(str(it.get("title") or "").split())[:SCORE_TITLE_CHARS]
        summary = " ".join(str(it.get("summary") or "").split())[:SCORE_SUMMARY_CHARS]
        source = " ".join(str(it.get("source") or "").split())[:30]
        lines.append(f"{i} | {source} | {title}" + (f" — {summary}" if summary else ""))
    lines += [
        "",
        "FOR EACH ITEM THAT IS AI NEWS WORTH 3 OR MORE, one line in exactly this "
        "shape and nothing else:",
        "  SCORE | <item number> | <topic from the list, or OTHER> | <importance 1-5> "
        "| <what happened, one clause>",
        "",
        "THE CLAUSE COMES FROM THAT ITEM'S TITLE AND SUMMARY ONLY — add nothing you "
        "know from elsewhere. Leave out everything else: items not about AI, "
        "opinion, how-to guides, listicles, deals and discounts.",
        "One line per story — when two items are the same story, score the first "
        "and leave the other out.",
        "If nothing is worth 3, reply with exactly: NOTHING FOUND",
    ]
    return "\n".join(lines)


_SCORE_RE = re.compile(r"^\s*[-*•]?\s*SCORE\s*\|", re.IGNORECASE)


def story_from_feed(row: dict, *, topics: Optional[list] = None) -> dict:
    """A stored, scored feed row as the story dict `choose` and `render` use."""
    return {
        "topic": _canonical_topic(row.get("topic") or "", topics),
        "headline": str(row.get("title") or "").strip(),
        "what": str(row.get("what") or "").strip(),
        "url": str(row.get("url") or "").strip(),
        "url_key": str(row.get("url_key") or ""),
        "headline_key": str(row.get("headline_key") or ""),
        "importance": max(1, min(5, int(row.get("importance") or 3))),
        "source": str(row.get("source") or "").strip(),
    }


def parse_scores(text: str, items: list, *, topics: Optional[list] = None) -> list:
    """The SCORE lines, as story dicts — in the order the items were shown.

    THE HEADLINE AND THE LINK COME FROM THE FEED ROW, never from the model: it
    is given a number to point at and cannot return a url it made up. A line
    whose number is not one of the items shown is dropped.
    """
    out: list = []
    seen: set = set()
    for line in str(text or "").splitlines():
        if not _SCORE_RE.match(line):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 4:
            continue
        m = re.search(r"\d+", parts[1])
        if not m:
            continue
        index = int(m.group(0)) - 1
        if index < 0 or index >= len(items) or index in seen:
            continue
        imp = _IMPORTANCE_RE.match(parts[3])
        if not imp:
            continue
        seen.add(index)
        row = dict(items[index])
        row["topic"] = parts[2]
        row["importance"] = int(imp.group(1))
        row["what"] = _URL_RE.sub("", " ".join(parts[4:])).strip(" -–—") \
            if len(parts) > 4 else ""
        out.append(story_from_feed(row, topics=topics))
    return out


def render_screen(rows: list, *, limit: int = 5) -> str:
    """R2's text: companies in the news that we do not track."""
    picked = [r for r in rows if r.get("company")][:max(1, int(limit))]
    if not picked:
        return ""
    import links

    lines = ["In the news today and not in the Master Pipeline:"]
    for r in picked:
        # ONE COMPANY PER BULLET, TWO SHORT LINES: what they do, then why it
        # fits (or doesn't). The link rides on the first line, masked.
        link = f" {links.link('', r['url'])}" if r.get("url") else ""
        what = _no_pings(r.get("what") or "")
        why = _no_pings(r.get("why") or r.get("verdict") or "")
        lines.append(f"• {_no_pings(r['company'])}" + (f" — {what}" if what else "") + link)
        if why:
            lines.append(f"  {why}")
    lines.append("Tell me which ones to add. I will not add anything without a yes.")
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
    first = body.splitlines()[0]
    check("one bullet per story", body.count("• "), 2)
    check("no topic tag", "[evals]" in body or "[Other]" in body, False)
    check("headline — what. [site](<url>)",
          first.startswith("• Lab ships — w. [") and first.endswith(">)"), True)
    check("no bare url", "<http" in body.replace("(<http", ""), False)
    check("no pings survive", "<@123>" in body or "@everyone" in body, False)
    check("no closing line", body.splitlines()[-1].startswith("• "), True)
    b = render([S("evals", "A", 5), S("RLHF", "B", 4)], mode=MODE_BREAKING)
    check("breaking opens with its heading", b.splitlines()[0], "**Breaking AI news**")
    check("...and carries every story as a bullet", b.count("• "), 2)
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
    text = (
        "SCREEN | Shunya Labs | builds Indic speech models | fits voice & multilingual "
        "speech: they need Hinglish preference data | https://n.com/1\n"
        "SCREEN | Priority Tech | payments software | no fit, nothing AI-training | "
        "https://p.com/2\n"
        "SKIP | EU AI Act enters phase two | regulation, not a specific company\n"
    )
    rows = parse_screen(text)
    check("both screens read", len(rows), 2)
    check("what they do is read", rows[0]["what"], "builds Indic speech models")
    check("the url is read", rows[0]["url"], "https://n.com/1")
    check("the skip is read", parse_screen_skips(text)[0]["why"],
          "regulation, not a specific company")
    prompt = screen_prompt([S("evals", "x")], ["Acme"], use_cases="Offerings: Pulse")
    check("the screen prompt carries the strategy text",
          "WHAT MEMBRANE SELLS" in prompt, True)
    check("it names the offerings in words", "agent trajectories" in prompt, True)
    check("it forbids letters", "Never refer to a use case by a letter" in prompt, True)
    check("it asks for skips", "SKIP |" in prompt, True)
    body = render_screen(rows)
    check("one bullet, two lines",
          body.splitlines()[1:3],
          ["• Shunya Labs — builds Indic speech models [n.com](<https://n.com/1>)",
           "  fits voice & multilingual speech: they need Hinglish preference data"])
    check("it asks before adding", "without a yes" in body, True)

    print("\nscoring feed items")
    feed = [
        {"url": "https://techcrunch.com/a", "url_key": "techcrunch.com/a",
         "headline_key": headline_key("Lab ships eval suite"),
         "title": "Lab ships eval suite", "summary": "A public benchmark.",
         "source": "TechCrunch", "published_at": "2026-09-29T06:00:00+00:00",
         "topic_hint": ""},
        {"url": "https://news.google.com/rss/articles/x", "url_key": "news.google.com/x",
         "headline_key": headline_key("Nebius raises seven hundred million"),
         "title": "Nebius raises seven hundred million", "summary": "",
         "source": "Reuters", "published_at": "2026-09-29T07:00:00+00:00",
         "topic_hint": "RLHF"},
        {"url": "https://news.google.com/rss/articles/y", "url_key": "news.google.com/y",
         "headline_key": headline_key("Lab ships eval suite"),
         "title": "Lab Ships Eval Suite", "summary": "", "source": "Wired",
         "published_at": "2026-09-29T08:00:00+00:00", "topic_hint": "evals"},
    ] + [
        {"url": f"https://news.google.com/rss/articles/r{i}",
         "url_key": f"news.google.com/r{i}", "headline_key": f"robot story {i}",
         "title": f"Robot story {i}", "summary": "", "source": "X",
         "published_at": f"2026-09-29T0{i}:30:00+00:00", "topic_hint": "robot"}
        for i in range(1, 6)
    ]
    picked = preselect(feed, cap=40, per_hint=3)
    check("the outlet's own feed leads", picked[0]["source"], "TechCrunch")
    check("one busy topic is capped at three",
          sum(1 for p in picked if p["topic_hint"] == "robot"), 3)
    check("the same headline from a second outlet is not shown twice",
          sum(1 for p in picked if p["headline_key"] == feed[0]["headline_key"]), 1)
    check("the cap holds", len(preselect(feed, cap=2)), 2)
    sp = score_prompt(picked[:2], topics, today=today, mode=MODE_MAIN)
    check("it shows titles, numbered", "1 | TechCrunch | Lab ships eval suite — "
          "A public benchmark." in sp, True)
    check("it carries no url", "http" in sp, False)
    check("it asks for SCORE lines", "SCORE | <item number> |" in sp, True)
    check("seeds, not limits", "SEEDS, NOT LIMITS" in sp, True)
    check("the check is strict",
          "BE STRICT WITH 4 AND 5" in score_prompt(picked[:2], topics, today=today,
                                                   mode=MODE_CHECK), True)
    scored = parse_scores(
        "SCORE | 2 | RLHF | 5 | raised $700m for inference capacity\n"
        "SCORE | 1 | evals | 4 | a public benchmark for agents\n"
        "SCORE | 9 | evals | 5 | not an item that was shown\n"
        "SCORE | 1 | evals | 3 | a second line for the same item\n",
        picked[:2], topics=topics)
    check("two stories, the out-of-range and the repeat dropped", len(scored), 2)
    check("the headline is the feed's, not the model's",
          scored[0]["headline"], "Nebius raises seven hundred million")
    check("the link is the feed's", scored[0]["url"],
          "https://news.google.com/rss/articles/x")
    check("the importance is read", [s["importance"] for s in scored], [5, 4])
    check("the topic is the list's spelling", scored[1]["topic"], "evals")
    body = render(scored)
    check("a Google News link is named for its outlet",
          "[Reuters](<https://news.google.com/rss/articles/x>)" in body, True)
    check("an outlet's own link keeps its site name",
          "[techcrunch.com](<https://techcrunch.com/a>)" in body, True)
    check("a future date says so", future_note(date(2026, 10, 5)),
          "No news yet — Mon 5 Oct hasn't happened.")
    check("a lookup is read", parse_screen_lookups("LOOKUP | Shunya Labs\nSCREEN | a | b | c"),
          ["Shunya Labs"])
    check("the lookup line is offered only when asked for",
          ("LOOKUP |" in screen_prompt([S("evals", "x")], ["Acme"], allow_lookup=True),
           "LOOKUP |" in screen_prompt([S("evals", "x")], ["Acme"])), (True, False))

    print("\nfound-nothing")
    check("an honest empty", found_nothing("NOTHING FOUND"), True)
    check("...and the screen too", found_nothing("NOTHING NEW"), True)
    check("a real answer is not empty", found_nothing("STORY | a | b | c | d | 3"), False)

    print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
