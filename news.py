"""R1 AND R2 — the AI news: what is about us first, then the industry.

THE FLOW (NFT2-1087). The feeds are read once an hour from 14:00 IST
(feeds.py: Google News for our names, our researchers and our topics, then the
outlets, then arXiv). Every stored story has TWO NUMBERS:

    flag        is it ABOUT US — a company or a person on Master Pipeline or
                Outreach PoCs, a name on the watch list, one of our
                researchers? Set BY RULE when the item is stored. No model.
    importance  1-5, the light model's, one call a cycle.

The flag decides whether the team MUST see a story; the score decides WHEN:

    BREAKING   about us at 4 or 5, industry at 5 from two outlets (or a
               frontier launch, by rule) — posted in the cycle that finds it,
               any day, straight to the channel: never through drip_sends,
               never against the cap. At most NEWS_BREAKING_MAX_PER_CYCLE
               stories a cycle and NEWS_BREAKING_MAX_PER_DAY a day
               (NEWS-PATCH-OCT16), and only while the story is new
               (NEWS_BREAKING_MAX_AGE_HOURS).

    DAILY      every weekday, in R1's place in the day's order: at most
               NEWS_MAX_ITEMS stories, ours first, then the industry. A story
               about us that does not fit is CARRIED to the next daily post;
               industry news that does not fit is dropped. There is no second
               message, and a day with nothing to send posts nothing.

ONE LINE SHAPE, EVERYWHERE (`story_line`): `- Headline ([Outlet](<url>))`. No
bold, no "— what" clause, no sheet tag. The messages are the team's templates
(`render`), fixed text and never written by a model.

THE TOPICS ARE SEEDS, NOT LIMITS (NEWS_TOPICS): important news off the list is
welcome, tagged OTHER.

ONE STORY IS STORED ONCE (NEWS-PATCH-OCT16). On 14 Oct the Gemini 4 launch
came in 29 times and was 29 stories, none of them scored. A feed item is now
the same story as a stored one by link, by headline key, by STORY KEY (the
words sorted, so word order is gone — `story_key`), or by `same_story` (two
shared names and most of the words, within 36 hours); every copy is counted on
the row (`outlet_count`). A title that names a frontier model and says it
launched is a 5 BY RULE, in code, before the model (`launch_rule`).

ONE STORY IS POSTED ONCE. `news_stories` remembers every story that went out,
daily or breaking, by normalised URL AND by headline key — four outlets give
one funding round four URLs, and the headline key is what catches the second
one. It is also what keeps a story that broke at 16:00 out of the daily post.

THE SPREAD RULES ARE FOR THE INDUSTRY PART, AND YIELD TO IMPORTANCE. At most
NEWS_PER_TOPIC_PER_DAY stories on one topic a day and NEWS_TOPICS_PER_WEEK
distinct topics a week, so a feed does not become "evals, evals, evals" — but
a story at or above the breaking bar walks past both, and a story about us is
never rationed at all.

R2 READS WHAT R1 COLLECTED. The news-company screen wants companies in the
news that are NOT in the Master Pipeline. It never searches again, and it asks
before anything is added to a sheet. It never writes.

NOTHING HERE OPENS A SOCKET OR CALLS A MODEL. This module builds prompts,
parses, chooses and renders; the pull is feeds.py's and the one scoring call is
bot.py's. Keeping the I/O out means everything here can be tested without a
network or an API key — see `_self_test`.
"""
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlsplit

import config
import deadlines as dl

log = logging.getLogger(__name__)

# The modes. MAIN and CHECK are the two prompts; BREAKING is how a check's post
# renders and what it is recorded as.
MODE_MAIN = "main"
MODE_CHECK = "check"            # SEARCH_BACKEND=anthropic only: the old search prompt
MODE_CYCLE = "cycle"            # the hourly cycle's scoring call
MODE_BREAKING = "breaking"
MODE_ANSWER = "answer"          # the list given to somebody who asked

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


# -- one story, many outlets (NEWS-PATCH-OCT16) -------------------------------
#
# ON 14 OCT THE GEMINI 4 LAUNCH WAS STORED 29 TIMES. `headline_key` is the
# first eight words IN ORDER, and "Google launches Gemini 4 Aragon" and "Gemini
# 4 Aragon: Google launches its new model" share the words, not the order. So
# every copy was a new story, none of them was scored, and nothing was posted.

# How many significant tokens make a story key (sorted, so order is gone).
STORY_KEY_TOKENS = 12

# How close in time two copies of one story are: first seen within this many
# hours of each other.
SAME_STORY_HOURS = 36

# The share of significant tokens two headlines must have in common, on top of
# two shared names, to be one story: the words they share over ALL the words
# either uses. Over the union, so "Google launches Gemini 4 Aragon" and
# "Google's Gemini 4 Aragon fails safety test" (3 shared of 7) stay apart.
SAME_STORY_OVERLAP = 0.6

# THE NAMES THE INDUSTRY IS ABOUT. Two headlines that share two of these (or two
# names from the sheets, `feeds.NameIndex.entities`) and most of their words are
# one story. One shared name is never enough: "Gemini 4 launched" and "Gemini 4
# fails safety test" are two stories on the same day.
MODEL_ENTITIES = ("Gemini", "GPT", "Claude", "Llama", "Grok", "Mistral", "DeepSeek",
                  "Qwen", "OpenAI", "DeepMind", "Anthropic", "Meta", "Microsoft",
                  "NVIDIA", "Google")
_MODEL_ENTITY_RE = re.compile(
    r"(?<![A-Za-z0-9])(" + "|".join(re.escape(e) for e in MODEL_ENTITIES)
    + r")(?![a-z])", re.IGNORECASE)


def _significant(headline: str) -> list:
    """The headline's significant tokens, in order: lower case, no punctuation,
    no digits, no stopwords, a possessive "'s" dropped ("Google's" is Google)."""
    out: list = []
    for raw in re.findall(r"[A-Za-z0-9'’]+", str(headline or "").lower()):
        raw = re.sub(r"['’]s$", "", raw)
        tok = re.sub(r"[\d'’]", "", raw)
        if len(tok) < 2 or tok in _STOPWORDS:
            continue
        out.append(tok)
    return out


def story_key(headline: str) -> str:
    """WHO DID WHAT, WHATEVER THE ORDER: the first 12 significant tokens,
    SORTED, joined with spaces. "Google launches Gemini 4 Aragon" and "Gemini 4
    Aragon: Google launches" are one key. Digits go, as in `headline_key`."""
    return " ".join(sorted(dict.fromkeys(_significant(headline)[:STORY_KEY_TOKENS])))


def model_entities(text: str) -> set:
    """The industry names (`MODEL_ENTITIES`) in a text, lower case."""
    return {m.group(1).lower() for m in _MODEL_ENTITY_RE.finditer(str(text or ""))}


def _when(value) -> Optional[datetime]:
    try:
        when = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def same_story(a: dict, b: dict, *, entities_of=None,
               hours: int = SAME_STORY_HOURS) -> bool:
    """Are two feed items ONE STORY? `a` and `b` carry `title` (or
    `headline`) and `seen_at`. `entities_of(title)` gives the names in a title
    (default: `model_entities` only; feeds.py adds the sheets' names).

    TRUE WHEN the story keys match, OR the two share at least TWO names AND
    `SAME_STORY_OVERLAP` of their significant tokens — and either way they were
    first seen within `hours` of each other. A shared name alone is never a
    match: that is what keeps "Gemini 4 launched" and "Gemini 4 fails safety
    test" apart.
    """
    ta = str(a.get("title") or a.get("headline") or "")
    tb = str(b.get("title") or b.get("headline") or "")
    wa, wb = _when(a.get("seen_at")), _when(b.get("seen_at"))
    if wa is not None and wb is not None and abs((wa - wb).total_seconds()) > hours * 3600:
        return False
    ka = a.get("story_key") or story_key(ta)
    kb = b.get("story_key") or story_key(tb)
    if ka and ka == kb:
        return True
    names = entities_of or model_entities
    if len(set(names(ta)) & set(names(tb))) < 2:
        return False
    sa, sb = set(_significant(ta)), set(_significant(tb))
    if not sa or not sb:
        return False
    return len(sa & sb) / len(sa | sb) >= SAME_STORY_OVERLAP


def preferred_domain(url: str, domains: Optional[list] = None) -> bool:
    """Is this link on one of NEWS_PREFERRED_DOMAINS (a subdomain counts)?"""
    host = (urlsplit(str(url or "")).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    for d in (domains if domains is not None else (config.NEWS_PREFERRED_DOMAINS or [])):
        d = str(d or "").strip().lower().lstrip(".")
        if d and (host == d or host.endswith("." + d)):
            return True
    return False


# -- the launch rule (NEWS-PATCH-OCT16) ---------------------------------------
#
# A FRONTIER MODEL LAUNCH IS SCORED IN CODE, before the model is asked. On 14
# Oct the Gemini 4 launch sat at 0 for ten hours because the scorer never
# reached it; a title that names a frontier model and says it launched is a 5
# whatever the scorer would say, and it costs nothing.

LAUNCH_TOPIC = "frontier model"
SCORED_BY_RULE = "rule"
SCORED_BY_MODEL = "model"

_FRONTIER_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:(?i:gemini|gpt|claude|llama|grok|mistral|deepseek|qwen)"
    r"(?![a-z])|o[1-9](?![0-9A-Za-z]))")
_LAUNCH_RE = re.compile(
    r"(?<![a-z])(?:launch|launches|launched|release|releases|released|unveil|unveils"
    r"|unveiled|announce|announces|announced|introduces|introducing|new model)(?![a-z])",
    re.IGNORECASE)


def launch_rule(item: dict) -> bool:
    """Does this item's TITLE name a frontier model (Gemini, GPT, Claude,
    Llama, Grok, Mistral, DeepSeek, Qwen, or an o-series name like "o5") AND a
    launch word? Then it is importance 5 by rule. The summary is not read: a
    review that mentions the launch is not the launch."""
    title = str((item or {}).get("title") or (item or {}).get("headline") or "")
    return bool(_FRONTIER_RE.search(title)) and bool(_LAUNCH_RE.search(title))


def launch_verdict(item: dict) -> dict:
    """The score the launch rule writes: 5, "frontier model", the title."""
    title = " ".join(str(item.get("title") or "").split())
    return {"url_key": item.get("url_key"), "importance": 5, "topic": LAUNCH_TOPIC,
            "what": title[:300], "scored_by": SCORED_BY_RULE}


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
    """R2's ONE-CALL PROMPT, as it was until 12 Oct 2026. NO LONGER USED BY R2.

    R2 now asks for company names only, filters them against both sheets in
    code and asks for a verdict on the survivors (`news_screen.py`). This is
    kept for the on-demand "screen these stories" path and the checks that
    read it; it lists every tracked company, with no cap.

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
    # EVERY NAME. This was cut at 200 until 12 Oct 2026, and Yellow.ai, number
    # 368 of 382, was proposed as a new company because the model never saw it.
    lines += ["", "ALREADY TRACKED:", "  " + ", ".join(known_companies)]
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


# -- the daily post: ours first, then the industry (NFT2-1087) ----------------

KIND_INDUSTRY = "industry"
KIND_POC = "poc"
KIND_RESEARCHER = "researcher"

# Where a stored item came from, 1 the highest (feeds.py reads them in this
# order). A row stored before the column existed is 9.
SOURCE_RANK_UNKNOWN = 9

# AN INDUSTRY STORY NEEDS A 3 TO BE POSTED. The scorer writes a line only for
# what is AI news worth 3 or more; everything it leaves out — a recipe on an
# outlet's feed, a discount, a listicle — is recorded as 1 so it is never paid
# for twice. "If there is room" therefore means room for a story the scorer
# kept, never for one it threw out. A story ABOUT US has no such floor: the
# flag wins, and a flagged story at 1 is still shown.
INDUSTRY_MIN_IMPORTANCE = 3


def is_ours(story: dict) -> bool:
    """Is this story ABOUT US — a company or a person on our sheets, a name on
    the watch list, one of our researchers? Decided by rule when the item was
    stored (`flag`); never by a model."""
    try:
        return bool(int((story or {}).get("flag") or 0))
    except (TypeError, ValueError):
        return False


# The old name, kept for the callers that still ask it (R10, the checks).
is_poc = is_ours


def is_carried(story: dict) -> bool:
    """Was this story left out of an earlier daily post for want of room?"""
    return bool(str((story or {}).get("carried_from") or "").strip()) \
        and int((story or {}).get("carry_count") or 0) > 0


def _rank(stories: list) -> list:
    """THE ONE ORDER (section 6): higher score, then a carried story before a
    new one, then the higher source rank, then the newest. Stable sorts, so
    the order is the same every time the same stories are ranked."""
    newest = sorted(stories or [], key=lambda s: str(s.get("published_at") or ""),
                    reverse=True)
    return sorted(newest, key=lambda s: (
        -int(s.get("importance") or 3), 0 if is_carried(s) else 1,
        int(s.get("source_rank") or SOURCE_RANK_UNKNOWN)))


def _qualify(stories: list, *, db, today: date, why) -> list:
    """The ranked stories that have not been posted (by link or by headline,
    inside NEWS_REPEAT_DAYS) and are not a second copy of one in this batch."""
    since = cutoff_iso(today)
    pool: list = []
    batch_urls: set = set()
    batch_heads: set = set()
    for s in _rank(stories):
        ukey, hkey = s.get("url_key") or "", s.get("headline_key") or ""
        if (ukey and ukey in batch_urls) or (hkey and hkey in batch_heads):
            why(s, "the same story is already in this batch")
            continue
        seen = db.news_story_seen(ukey, hkey, since_iso=since)
        if seen:
            how = "the same link" if seen.get("url_key") == ukey and ukey else \
                "the same headline"
            why(s, f"already posted on {seen.get('posted_on', '?')} "
                   f"({seen.get('kind') or 'main'} post) — {how}, within "
                   f"{config.NEWS_REPEAT_DAYS} days")
            continue
        if ukey:
            batch_urls.add(ukey)
        if hkey:
            batch_heads.add(hkey)
        pool.append(s)
    return pool


def choose_main(stories: list, *, today: date, db, cap: int, per_topic: int,
                topics_per_week: int, min_importance: int,
                offtopic_bypass: int) -> dict:
    """THE DAILY POST: at most `cap` stories, OURS FIRST, then the industry.

    Returns {"keep", "ours", "industry", "carry", "dropped", "skipped"}.
    `keep` is the post in the order it is posted (`ours` then `industry`);
    `carry` is every story about us that qualified and did not fit — the
    caller carries it to the next daily post (`db.news_feed_carry`); `dropped`
    is the industry news that did not fit, which is not carried; `skipped` is
    a sentence per story that is not in the post, for the log.

    WHO QUALIFIES: a story not already posted (by link or by headline, inside
    NEWS_REPEAT_DAYS — which is what keeps a story sent as breaking out of the
    daily post) and not a second copy of one in this batch.

    1. OURS — every story with the flag, whatever its score (a flagged story
       the scorer left out is a 1, and the flag wins), in the one order: score,
       then a carried story before a new one, then source rank, then newest.
       They take the first slots, up to `cap`. No spread limit ever applies.
    2. THE INDUSTRY — what is left of `cap`, from the stories scored
       INDUSTRY_MIN_IMPORTANCE or more, in the same order. THE SPREAD LIMITS
       APPLY HERE ONLY — at most `per_topic` stories on a topic a day and
       `topics_per_week` topics a week — and two kinds of story walk past
       them: importance at or above `min_importance` (the big one is never
       hidden) and an OTHER story at or above `offtopic_bypass` (the topic
       list is seeds, not limits).

    THERE IS NO SECOND MESSAGE. "More AI News" is gone: what does not fit is
    carried if it is about us and dropped if it is not.
    """
    marker = dl.iso(today)
    bar = int(min_importance)
    cap = max(0, min(int(cap), MAX_PER_MESSAGE))
    skipped: list = []

    def _why(s, why):
        skipped.append(f"{s.get('headline', '?')!r} — {why}")

    pool = _qualify(stories, db=db, today=today, why=_why)

    # ---- 1. ours first ------------------------------------------------------
    mine = [s for s in pool if is_ours(s)]
    ours, carry = mine[:cap], mine[cap:]
    for s in carry:
        _why(s, f"about us, and the post is full (cap {cap}) — carried to the "
                "next daily post")

    # ---- 2. the industry, in what is left -----------------------------------
    try:
        week_topics = set(db.news_topics_this_week(iso_week(today)))
    except Exception:
        log.exception("[news] could not read this week's topics; treating the week "
                      "as full so only the important breaks through")
        week_topics = None
    topic_counts: dict = {}

    def _count(topic) -> int:
        if topic not in topic_counts:
            try:
                topic_counts[topic] = int(db.news_topic_count_today(topic, marker))
            except Exception:
                topic_counts[topic] = int(per_topic)
        return topic_counts[topic]

    industry: list = []
    dropped: list = []
    for s in pool:
        if is_ours(s):
            continue
        imp = int(s.get("importance") or 3)
        topic = s.get("topic") or TOPIC_OTHER
        if imp < INDUSTRY_MIN_IMPORTANCE:
            _why(s, f"industry news at importance {imp}: below the {INDUSTRY_MIN_IMPORTANCE} "
                    "a story needs to be posted")
            continue
        if len(ours) + len(industry) >= cap:
            dropped.append(s)
            _why(s, f"the post is full (cap {cap}) — industry news is not carried")
            continue
        free = imp >= bar or (topic == TOPIC_OTHER and imp >= int(offtopic_bypass))
        if not free:
            if _count(topic) >= int(per_topic):
                dropped.append(s)
                _why(s, f"{topic} already has {topic_counts[topic]} story/stories "
                        f"today (NEWS_PER_TOPIC_PER_DAY={per_topic})")
                continue
            if week_topics is None or (topic not in week_topics
                                       and len(week_topics) >= int(topics_per_week)):
                dropped.append(s)
                _why(s, f"{topic} would be topic number "
                        f"{len(week_topics or ()) + 1} this week "
                        f"(NEWS_TOPICS_PER_WEEK={topics_per_week})")
                continue
        industry.append(s)
        topic_counts[topic] = _count(topic) + 1
        if week_topics is not None:
            week_topics.add(topic)
    return {"keep": ours + industry, "ours": ours, "industry": industry,
            "carry": carry, "dropped": dropped, "skipped": skipped}


def is_breaking(story: dict, *, ours_min: int, industry_min: int) -> bool:
    """Does this story's SCORE make it breaking news? About us: `ours_min`
    (4) or more. Industry: `industry_min` (5)."""
    imp = int((story or {}).get("importance") or 0)
    return imp >= (int(ours_min) if is_ours(story) else int(industry_min))


def outlets_of(story: dict) -> int:
    """How many outlets carried this story (`outlet_count`; 1 when unknown)."""
    try:
        return max(1, int((story or {}).get("outlet_count") or 1))
    except (TypeError, ValueError):
        return 1


def by_rule(story: dict) -> bool:
    """Was this story scored by the launch rule rather than the model?"""
    return str((story or {}).get("scored_by") or "") == SCORED_BY_RULE


def choose_breaking(stories: list, *, today: date, db, now: datetime,
                    max_age_hours: int, ours_min: int, industry_min: int,
                    cap: int, max_per_day: Optional[int] = None,
                    min_outlets: int = 1) -> dict:
    """WHAT ONE CYCLE SENDS AS BREAKING NEWS. {"keep", "held", "skipped",
    "budget_spent"}.

    THE GATES, IN ORDER:
      1. the score — about us 4 or 5, industry 5 (`is_breaking`). AN
         INDUSTRY STORY ALSO NEEDS `min_outlets` OUTLETS (NEWS-PATCH-OCT16:
         NEWS_BREAKING_MIN_OUTLETS, 2) — one outlet's 5 waits for a second
         outlet or the daily post — unless the launch rule scored it;
      2. NEW — first SEEN within `max_age_hours` of `now`. A story that only
         reaches a breaking score hours later is not sent as stale breaking
         news: it is in the next daily post instead. A CARRIED story is never
         breaking — being carried does not make it new;
      3. not already posted, by link or by headline — a second outlet's copy
         of a story that already went out is the same story;
      4. THE FLOOD GUARD — the top `cap` in the one order (score, source rank,
         newest) go now, in ONE message. The rest are `held`: the caller does
         not record them, so the next cycle can send them;
      5. THE DAY'S BUDGET (NEWS-PATCH-OCT16) — at most `max_per_day`
         breaking stories a day, counted from `news_stories` rows of kind
         "breaking" posted on `today` (`db.news_breaking_count_on`), so the
         count starts again at IST midnight. When the budget is spent the
         candidates are held for the daily post, not recorded, and
         `budget_spent` is True. None = no daily budget.
    """
    skipped: list = []

    def _why(s, why):
        skipped.append(f"{s.get('headline', '?')!r} — {why}")

    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    oldest = (now.astimezone(timezone.utc)
              - timedelta(hours=max(1, int(max_age_hours)))).isoformat(timespec="seconds")
    fresh: list = []
    for s in stories or []:
        if not is_breaking(s, ours_min=ours_min, industry_min=industry_min):
            continue
        if not is_ours(s) and not by_rule(s) and outlets_of(s) < int(min_outlets):
            _why(s, f"industry news at {int(s.get('importance') or 0)} from "
                    f"{outlets_of(s)} outlet(s) — breaking needs {int(min_outlets)} "
                    "(NEWS_BREAKING_MIN_OUTLETS); held for a second outlet or the "
                    "daily post")
            continue
        if is_carried(s) or int(s.get("carry_count") or 0) < 0:
            _why(s, "carried from an earlier daily post; a carried story is never "
                    "breaking")
            continue
        seen_at = str(s.get("seen_at") or "")
        if seen_at and seen_at < oldest:
            _why(s, f"first seen more than {int(max_age_hours)}h ago "
                    "(NEWS_BREAKING_MAX_AGE_HOURS) — it goes in the daily post")
            continue
        fresh.append(s)
    pool = _qualify(fresh, db=db, today=today, why=_why)
    room = max(0, min(int(cap), MAX_PER_MESSAGE))
    left = None
    if max_per_day is not None:
        try:
            done = int(db.news_breaking_count_on(dl.iso(today)))
        except Exception:
            log.exception("[news] could not count today's breaking posts; treating "
                          "the day's budget as spent")
            done = int(max_per_day)
        left = max(0, int(max_per_day) - done)
    keep = pool[:min(room, left) if left is not None else room]
    held = pool[len(keep):]
    spent = left is not None and len(keep) < room and len(held) > 0
    for i, s in enumerate(held):
        if spent and (left == 0 or i < room - len(keep)):
            _why(s, f"the day's breaking budget is spent ({int(max_per_day)}, "
                    "NEWS_BREAKING_MAX_PER_DAY) — held for the daily post, not "
                    "recorded")
        else:
            _why(s, f"{room} breaking stories already go out this cycle "
                    "(NEWS_BREAKING_MAX_PER_CYCLE) — not recorded; the next cycle "
                    "can send it")
    return {"keep": keep, "held": held, "skipped": skipped, "budget_spent": spent}


# -- what the message says ----------------------------------------------------

_PING_RE = re.compile(r"<@[!&]?\d+>|@(everyone|here)\b", re.IGNORECASE)


def _no_pings(text: str) -> str:
    """Web text never pings anybody: mention tokens are removed outright."""
    return _PING_RE.sub(lambda m: ("@​" + m.group(1)) if m.group(1) else "",
                        str(text or ""))


def _topic_label(topic: str) -> str:
    return "Other" if (topic or TOPIC_OTHER) == TOPIC_OTHER else topic


# What somebody who ASKS for the news is told when there is none to give.
#
# RETIRED FOR THE DAILY POST (NFT2-1087): a day with nothing to send posts
# NOTHING — no heading over one line saying so. An answer is different:
# somebody asked, and silence is not a reply.
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
# THE TEMPLATES (NFT2-1087, section 16). The heading is the ONLY bold line of a
# news message. The daily post's and an answer's carry the day; a breaking
# post's does not ("**Breaking news**"). The daily post's heading is put on by
# the drip sender (drip.HEADINGS["R1"]) and reads the same.
HEADING_LABELS = {
    MODE_MAIN: "AI News",
    MODE_ANSWER: "AI News",
    MODE_BREAKING: "Breaking news",
}
# Kept as a name other modules and checks read: the bold label.
BREAKING_HEADING = "**Breaking news**"
# THE FIXED TEXT OF THE DAILY POST. No model writes any of these.
OPENER = "Hey here's today's round-up:"
LABEL_OURS = "Our PoCs are in the news:"
LABEL_INDUSTRY = "Elsewhere in AI"
CLOSER = "Let me know if you want me to dig into any of these"
# NO NEWS MESSAGE OF ANY KIND CARRIES MORE THAN THIS MANY STORIES — the daily
# post, a breaking post, an answer. A hard ceiling under every setting:
# NEWS_MAX_ITEMS and NEWS_BREAKING_MAX_PER_CYCLE can lower it, not raise it.
MAX_PER_MESSAGE = 5
# A story whose line could not fit one Discord message even alone (a tracking
# link two thousand characters long) is left out and logged: a link is never
# shortened or rebuilt, so there is no honest way to post it.
MAX_LINE_CHARS = 1500
CONTINUED = "(continued)"


def day_label(day: date) -> str:
    """"Thu 8 Oct" — the way every heading the bot writes names a day."""
    return f"{day.strftime('%a')} {day.day} {day.strftime('%b')}"


def heading(mode: str, day: Optional[date] = None, *, tags: str = "") -> str:
    """The first line of a news message: `**AI News, Thu 8 Oct**`, or
    `**Breaking news**` (no date). `tags` — who the message is for — go on the
    SAME line, after the bold: `**AI News, Tue 13 Oct** <@1> <@2>`."""
    label = HEADING_LABELS.get(mode, HEADING_LABELS[MODE_MAIN])
    bold = f"**{label}**" if mode == MODE_BREAKING \
        else f"**{label}, {day_label(day or dl.today_ist())}**"
    tags = " ".join(str(tags or "").split())
    return f"{bold} {tags}" if tags else bold


def quiet_line(day: date) -> str:
    """The quiet-day line for `day` — one of QUIET_LINES, rotating by working
    day, so Friday and the Monday after it differ too (`tone.rotate`).

    THE VOICE PROFILE SETS THE ORDER, the rotation stays: the wording closest
    to how the team opens a message leads (`voice.order`), and three quiet days
    in a row are still three different lines. With no profile the order is the
    one written above.
    """
    import tone
    lines = QUIET_LINES
    try:
        import voice

        lines = voice.order(QUIET_LINES)
    except Exception:
        log.debug("[news] could not order the quiet lines by voice", exc_info=True)
    return tone.rotate(lines, day)


_TAIL_SEP_RE = re.compile(r"\s+([-–—|])\s+")
_WORD_RE = re.compile(r"[a-z0-9]+")


def _words(text: str) -> list:
    return _WORD_RE.findall(str(text or "").lower())


def _is_google_news(url: str) -> bool:
    host = (urlsplit(str(url or "")).hostname or "").lower()
    return host.endswith("news.google.com")


def _is_outlet_tail(tail: str, sep: str, story: dict) -> bool:
    """Is the text after the last " - " or " | " of a headline the OUTLET's
    name rather than part of the headline?

    Yes when it names the story's source or its site ("… - Reuters", "… |
    Hindustan Times", "… - TechCrunch" on a techcrunch.com link). A short tail
    after a pipe is a site or a section name too ("… | ET Tv", "… | Artificial
    Intelligence"). A tail after a dash that matches nothing is LEFT: "GPT-5 -
    what we know" is a headline, and cutting it would change what was said.
    """
    import links

    tail_words = _words(tail)
    if not tail_words or len(tail_words) > 8:
        return False
    joined = "".join(tail_words)
    source = "".join(_words(story.get("source")))
    if source and (joined == source or joined in source or source in joined):
        return True
    site = links.site_name(story.get("url") or "")
    label = site.split(".")[0] if site else ""
    if label and len(label) > 2 and label != "news" and label in joined:
        return True
    return sep == "|" and len(tail_words) <= 4


def clean_headline(story: dict) -> str:
    """The feed's headline as it is shown: whole, once, with no outlet on the end.

    - a trailing " - Outlet" or " | Outlet" comes off (Google News titles end
      with one, and the outlet is named in the link beside it);
    - a trailing full stop and stray whitespace come off;
    - mention tokens are removed (`_no_pings`) and asterisks with them, so feed
      text can neither ping anybody nor make itself bold;
    - NEVER CUT. A headline is shown to its last word or not at all.
    """
    text = " ".join(_no_pings((story or {}).get("headline")
                              or (story or {}).get("title") or "").split())
    for _ in range(2):
        found = list(_TAIL_SEP_RE.finditer(text))
        if not found:
            break
        last = found[-1]
        head, tail = text[:last.start()].rstrip(), text[last.end():]
        if len(_words(head)) < 3 or not _is_outlet_tail(tail, last.group(1), story):
            break
        text = head
    text = text.replace("*", "").replace("`", "").strip()
    # One trailing full stop goes; "U.S." and "…" stay as written.
    if text.endswith(".") and not text.endswith("..") \
            and not re.search(r"\b[A-Za-z]\.[A-Za-z]\.$", text):
        text = text[:-1].rstrip()
    return text


def outlet(story: dict) -> str:
    """The short, clickable name of where a story is from.

    A GOOGLE NEWS ITEM carries its outlet in `source` ("Reuters"); its host
    would read "news.google.com" under every story. A source that is a whole
    strapline ("ABC News - Breaking News, Latest News and Videos") is cut back
    to the name in front. A DIRECT FEED ITEM is named by its site
    (links.site_name: "techcrunch.com") — its `source` is the feed's own title,
    which is a sentence, not a name. Failing both: the host without "www.".
    """
    import links

    url = str((story or {}).get("url") or "")
    name = ""
    if _is_google_news(url):
        name = " ".join(_no_pings((story or {}).get("source") or "").split())
        name = re.split(r"\s+[-–—|]\s+|:\s+", name)[0].strip()
    if not name:
        name = links.site_name(url)
    name = re.sub(r"[\[\]()<>*`]", "", name).strip()
    return name or "link"


def story_line(story: dict) -> str:
    """One story as one bullet. THE ONLY SHAPE THERE IS (NFT2-1087, section 9):

        - 'Brutal session' for chip stocks shows AI trade is turning ([Reuters](<url>))

    A headline and its outlet, nothing else, for every story in every news
    message — the daily post, a breaking post, an answer in the channel. NO
    BOLD: only a message's heading is bold. No "— what happened" clause: the
    headline is shown once and the story is one click away. NO SHEET TAG: a
    story about us used to have a line of its own ("Headline — what. (Company
    — on Master Pipeline) [site](<url>)"); what says it is ours now is the
    label it stands under, "Our PoCs are in the news:".
    """
    url = str(story["url"]).strip().strip("<>")
    return f"- {clean_headline(story)} ([{outlet(story)}](<{url}>))"


def unique(stories: list) -> list:
    """`stories` with every second copy of a story removed, order kept.

    ONE STORY ONCE PER MESSAGE: the same link (url_key), the same headline key,
    or the same cleaned headline from two outlets. The first copy stays, so a
    caller that has ranked the list keeps the copy it ranked highest.
    """
    out: list = []
    urls: set = set()
    heads: set = set()
    shown: set = set()
    for s in stories or []:
        ukey = str(s.get("url_key") or "").strip() or url_key(s.get("url") or "")
        hkey = str(s.get("headline_key") or "").strip()
        text = " ".join(_words(clean_headline(s)))
        if (ukey and ukey in urls) or (hkey and hkey in heads) or (text and text in shown):
            continue
        if ukey:
            urls.add(ukey)
        if hkey:
            heads.add(hkey)
        if text:
            shown.add(text)
        out.append(s)
    return out


def _is_item(line: str) -> bool:
    return str(line or "").startswith("- ")


def _join(lines: list) -> str:
    """The lines of a news message with its spacing set: ONE blank line
    between two stories, and none anywhere else — not under the heading, not
    under the opener or a label, not above the closer (the templates of
    NFT2-1087, section 16)."""
    out: list = []
    for ln in lines:
        if out and _is_item(out[-1]) and _is_item(ln):
            out.append("")
        out.append(ln)
    return "\n".join(out)


def _picked(stories: list, cap: int) -> list:
    """The stories a message carries: each once (`unique`), with a link, at
    most `cap`, and none whose line cannot fit a Discord message."""
    picked: list = []
    for s in unique([s for s in (stories or []) if s.get("url")]):
        if len(story_line(s)) > MAX_LINE_CHARS:
            log.warning("[news] left out of the message: its line is %d characters "
                        "and cannot fit one Discord message (%r)",
                        len(story_line(s)), str(s.get("headline") or "")[:80])
            continue
        picked.append(s)
        if len(picked) >= cap:
            break
    return picked


def render(stories: list, *, mode: str = MODE_MAIN, day: Optional[date] = None,
           tags: str = "", remaining: int = 0, label: str = "", cap: int = 0) -> str:
    """One news message, in one of the team's templates (NFT2-1087, s.16).
    Never written by a model: the opener, the two labels and the closer are
    fixed text, and every story is `story_line`.

    THE DAILY POST (MODE_MAIN) — everything UNDER its heading, which the drip
    sender puts on with the tags (`heading`). Three shapes:

        Hey here's today's round-up:        both kinds of story
        Our PoCs are in the news:
        - ours
        Elsewhere in AI
        - industry
        Let me know if you want me to dig into any of these

      only ours      the first label and its stories; no "Elsewhere in AI"
      only industry  no label at all

    A BREAKING POST (MODE_BREAKING) — `**Breaking news** <tags>` and the
    stories: no date, no opener, no closer. AN ANSWER (MODE_ANSWER) — `**AI
    News, <day>**` and the stories: no tags, no opener, no closer, no labels —
    and, when `remaining` stories are left after the page, the fixed tail
    (`answer_tail`, NEWS-ANSWER-OCT16). The other modes never carry it.

    AT MOST MAX_PER_MESSAGE STORIES, each once (`unique`), whatever was handed
    in; ours (`is_ours`) before the industry, the order inside each kept as
    given. No stories: "" — there is no quiet line; nothing to send is no post.
    """
    picked = _picked(stories, MAX_PER_MESSAGE)
    if not picked:
        return ""
    if mode != MODE_MAIN:
        head = heading(mode, day or dl.today_ist(),
                       tags=tags if mode == MODE_BREAKING else "")
        body = _join([head] + [story_line(s) for s in picked])
        tail = answer_tail(remaining, label, cap or len(picked)) \
            if mode == MODE_ANSWER else ""
        return body + ("\n" + tail if tail else "")
    ours = [s for s in picked if is_ours(s)]
    industry = [s for s in picked if not is_ours(s)]
    lines = [OPENER]
    if ours:
        lines.append(LABEL_OURS)
        lines += [story_line(s) for s in ours]
        if industry:
            lines.append(LABEL_INDUSTRY)
    lines += [story_line(s) for s in industry]
    lines.append(CLOSER)
    return _join(lines)


def daily_message(stories: list, *, day: date, tags: str = "") -> str:
    """The whole daily post as it is sent: the heading with the tags on its
    line, then `render`. "" when there is nothing to send. The drip sender
    builds the same text from the same two functions."""
    body = render(stories, mode=MODE_MAIN, day=day)
    return f"{heading(MODE_MAIN, day, tags=tags)}\n{body}" if body else ""


def layout(text: str) -> str:
    """A news message with its spacing set (`_join`): one blank line between
    two stories and none anywhere else. Idempotent, so the sender can put the
    heading on a rendered body and run the whole message through it."""
    lines = [ln.rstrip() for ln in str(text or "").split("\n") if ln.strip()]
    return _join(lines)


_BOLD_HEAD_RE = re.compile(r"^(\*\*.+?\*\*)")


def split_message(text: str, *, limit: int = 2000) -> list:
    """`text` as the Discord messages it takes — ONE whenever it fits.

    A NEWS MESSAGE IS ONE MESSAGE. It is split only when it is past `limit`
    characters (five stories with long tracking links can be), and then only
    BETWEEN stories. Every later part opens with the bold heading again,
    marked "(continued)" — `**AI News, Tue 13 Oct (continued)**` — and carries
    STORIES ONLY: no tags, no opener, no label, no closer. A daily post once
    arrived as two messages, the second a bare list with no heading; this is
    what makes that impossible.
    """
    body = str(text or "")
    limit = max(200, int(limit))
    if len(body) <= limit:
        return [body]
    lines = [ln for ln in body.split("\n") if ln.strip()]
    if not any(_is_item(ln) for ln in lines):
        # No bullets at all: nothing of ours to keep together.
        return [body[i:i + limit] for i in range(0, len(body), limit)]
    found = _BOLD_HEAD_RE.match(lines[0])
    again = f"{found.group(1)[:-2]} {CONTINUED}**" if found else ""
    parts: list = []
    current: list = []
    first = True
    for ln in lines:
        if not first and not _is_item(ln):
            continue                        # a later part is stories only
        has_item = any(_is_item(x) for x in current)
        if has_item and len(_join(current + [ln])) > limit:
            # A label left hanging at the foot of a part belongs to the
            # stories that moved on; it goes rather than stand over nothing.
            while current and not _is_item(current[-1]):
                current.pop()
            parts.append(_join(current))
            first = False
            if not _is_item(ln):
                current = []
                continue
            current = ([again] if again else []) + [ln]
        else:
            current.append(ln)
    if any(_is_item(x) for x in current):
        parts.append(_join(current))
    return parts


def choose_answer(stories: list, *, is_sent, cap: int = MAX_PER_MESSAGE,
                  offset: int = 0, mode: str = "window") -> dict:
    """Which stories somebody who ASKED for the news is given.

    THE TOP OF THE WINDOW, REPEATS INCLUDED (NEWS-ANSWER-OCT16). On 15 Oct
    somebody asked for the top AI news of the last 24 hours and was given
    weaker stories nobody had posted, because the old rule handed out unsent
    stories first: the two or three posted the evening before — the ones she
    meant — were left out. A question about a window gets that window's best.

    Returns {"stories", "total", "remaining", "sent_before", "repeat",
    "unsent"}: the page of at most `cap` stories starting at `offset`, ranked
    by importance and then newest (a tie goes to the newer one), one story
    once (`unique`); how many there are in all; how many come after this page;
    and the url keys of the page's stories already sent (`is_sent`), which the
    caller does not record again. Nothing in it says a story was posted.

    `mode="fresh"` is the OLD RULE, kept only for the checks that still pin it:
    the `cap` best unsent stories, the best sent ones only when no unsent one
    is left (`repeat`), listed newest first.

    Pure: `is_sent` is the caller's (the news_stories table, inside
    NEWS_REPEAT_DAYS), so a test can say what was sent without a database.
    """
    cap = max(1, min(int(cap), MAX_PER_MESSAGE))
    newest = sorted([s for s in (stories or []) if s.get("url")],
                    key=lambda s: str(s.get("published_at") or ""), reverse=True)
    ranked = unique(sorted(newest, key=lambda s: -int(s.get("importance") or 3)))
    if mode == "fresh":
        unsent = [s for s in ranked if not is_sent(s)]
        picked = unsent[:cap] if unsent else ranked[:cap]
        picked = sorted(picked, key=lambda s: str(s.get("published_at") or ""),
                        reverse=True)
        return {"stories": picked, "repeat": bool(picked) and not unsent,
                "unsent": len(unsent), "total": len(ranked),
                "remaining": 0, "sent_before": []}
    offset = max(0, int(offset or 0))
    page = ranked[offset:offset + cap]
    return {"stories": page, "total": len(ranked),
            "remaining": max(0, len(ranked) - offset - len(page)),
            "sent_before": [s.get("url_key") for s in page if is_sent(s)],
            "repeat": False, "unsent": sum(1 for s in ranked if not is_sent(s))}


# -- the window a question names (NEWS-ANSWER-OCT16) ----------------------------

# THE ANSWER'S TAIL, after the list, in MODE_ANSWER only: fixed text, never a
# model line. Only when more stories are left than the page shows.
ANSWER_TAIL = ("There are {remaining} more in the {label}. These are the top {cap}. "
               "Would you like to see the rest?")
ANSWER_ALL_SHOWN = "That was all of it."
DEFAULT_WINDOW_LABEL = "last 24 hours"

_NUM_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
              "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "twelve": 12,
              "fifteen": 15, "twenty": 20, "thirty": 30, "few": 3, "couple": 2}
_UNIT_HOURS = {"hour": 1, "day": 24, "week": 168, "month": 720}
_N_RE = (r"(\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|twelve|fifteen"
         r"|twenty|thirty|few|couple(?: of)?)")
_SPAN_RE = re.compile(r"\b(last|past|previous|within(?: the)?(?: last| past)?)\s+"
                      + _N_RE + r"\s*(hours?|hrs?|h|days?|weeks?|months?)\b", re.I)
_ONE_RE = re.compile(r"\b(last|past|previous)\s+(hour|day|week|month)\b", re.I)


def _unit(word: str) -> str:
    w = word.lower().rstrip("s")
    return "hour" if w in ("hr", "h") else w


def _label(word: str, n: int, unit: str) -> str:
    word = word.lower().split()[0]
    word = "last" if word in ("within", "previous") else word
    return f"{word} {unit}" if n == 1 else f"{word} {n} {unit}s"


def parse_window(text: str, *, now: Optional[datetime] = None,
                 keep_days: Optional[int] = None) -> Optional[dict]:
    """The window a news question names, or None (the caller uses 24 hours).

    {"hours", "label", "since", "capped"}: `hours` the span (whole hours,
    rounded up), `label` the phrase the answer's tail uses ("There are 7 more
    in the {label}"), `since` the IST start when the phrase is anchored to the
    clock ("today", "yesterday", "this morning") and None otherwise, `capped`
    True when the span was cut to NEWS_FEED_KEEP_DAYS — the store keeps no
    more. Pure: `now` is the caller's IST time (the test clock when set).

      "last 24 hours" -> 24        "past 2 days" -> 48       "this week" -> 168
      "today" -> since 00:00        "yesterday", "since yesterday" -> since
      "this morning" -> since 06:00 yesterday's 00:00
      "last month" -> 720, capped at NEWS_FEED_KEEP_DAYS x 24
    """
    body = " ".join(str(text or "").split())
    if not body:
        return None
    now = now or dl.now_ist()
    keep = max(1, int(keep_days if keep_days is not None else config.NEWS_FEED_KEEP_DAYS))
    low = body.lower().replace("'", "").replace("’", "")
    since = None
    span, one = _SPAN_RE.search(body), _ONE_RE.search(body)
    if span:
        raw = span.group(2).lower().replace(" of", "")
        n = max(1, int(raw) if raw.isdigit() else _NUM_WORDS.get(raw, 1))
        unit = _unit(span.group(3))
        hours, label = n * _UNIT_HOURS[unit], _label(span.group(1), n, unit)
    elif one:
        unit = _unit(one.group(2))
        hours, label = _UNIT_HOURS[unit], _label(one.group(1), 1, unit)
    elif re.search(r"\b(this|the)\s+week\b", low):
        hours, label = 168, "past week"
    elif re.search(r"\bthis\s+month\b", low):
        hours, label = 720, "past month"
    elif re.search(r"\byesterday\b", low):
        since = datetime.combine(now.date() - timedelta(days=1), datetime.min.time(),
                                 now.tzinfo)
        hours, label = 0, "period from yesterday"
    elif re.search(r"\bthis\s+morning\b", low):
        day = now.date() if now.hour >= 6 else now.date() - timedelta(days=1)
        since = datetime.combine(day, datetime.min.time(), now.tzinfo) + timedelta(hours=6)
        hours, label = 0, "morning so far"
    elif re.search(r"\b(today|todays)\b", low):
        since = datetime.combine(now.date(), datetime.min.time(), now.tzinfo)
        hours, label = 0, "day so far"
    else:
        return None
    if since is not None:
        hours = max(1, int(-(-(now - since).total_seconds() // 3600)))
    capped = hours > keep * 24
    if capped:
        hours, since = keep * 24, None
    return {"hours": int(hours), "label": label, "since": since, "capped": capped}


_COUNT_RE = re.compile(r"\btop\s+" + _N_RE + r"\b|\b" + _N_RE
                       + r"\s+(?:top\s+)?(?:ai\s+)?(?:news\s+)?(?:stories|headlines|items)\b",
                       re.I)


def parse_count(text: str) -> Optional[int]:
    """"top 3" / "five headlines" -> how many the person asked for, or None.
    Not capped here: the caller caps it at MAX_PER_MESSAGE (5)."""
    m = _COUNT_RE.search(str(text or ""))
    if not m:
        return None
    raw = (m.group(1) or m.group(2) or "").lower().replace(" of", "")
    n = int(raw) if raw.isdigit() else _NUM_WORDS.get(raw)
    return n if n and n > 0 else None


def answer_tail(remaining: int, label: str, cap: int) -> str:
    """The tail of a news answer that has more to give; "" when it has not."""
    if int(remaining or 0) <= 0:
        return ""
    return ANSWER_TAIL.format(remaining=int(remaining),
                              label=label or DEFAULT_WINDOW_LABEL, cap=int(cap))


def rest_again(label: str) -> str:
    """What a late "yes" is asked back: "The rest of the last-24-hour news?"."""
    label = label or DEFAULT_WINDOW_LABEL
    m = re.fullmatch(r"(last|past) (\d+) (hour|day|week|month)s", label)
    if m:
        return f"The rest of the {m.group(1)}-{m.group(2)}-{m.group(3)} news?"
    return f"The rest of the news from the {label}?"


# Words a question about "the news" is made of. A question built from these
# alone names no company, person or subject, so the list IS the whole answer.
_PLAIN_NEWS_WORDS = frozenset("""
a about ai all an and any are artificial as at be been big biggest can catch could current
daily day days do does five for forthis from get give going got has have headline
headlines hey hi hello hour hours i in industry intelligence is it just latest
list me might morning my new news newest now of on our out please quick recent
recently right rn round roundup see send share show so some stories story tell
than that the there these this three to today todays top two up update updates
us was we what whats whatever with world would you your happening
last past yesterday since week within previous far
""".split())
_NEWS_ASK_RE = re.compile(
    r"\b(news|headlines?)\b|\bwhat(?:\s+is|'?s|s)\s+(?:new|happening)\b", re.IGNORECASE)


def plain_question(text: str) -> bool:
    """Is this a plain "what's the news?" — no company, person or subject named?

    "any AI news?", "what's in the news", "top 5 headlines", "what is in the
    news for this hour?". Decided by the words alone, no model: it asks for
    news, and every other word is one of the few a news question is made of.
    "any news on ElevenLabs?" is NOT plain ("elevenlabs" is none of them) and
    goes to the engine with the same rendered list. A window ("in the last
    24 hours", "since yesterday", "this week") IS plain since NEWS-ANSWER-OCT16:
    the caller reads it with `parse_window`, still with no model.
    """
    body = str(text or "")
    if not _NEWS_ASK_RE.search(body):
        return False
    words = _words(body.replace("'", "").replace("’", ""))
    if not words or len(words) > 14:
        return False
    return all(w in _PLAIN_NEWS_WORDS or w.isdigit() for w in words)


# -- R1 from the feeds: choosing what the scorer sees, and reading its verdicts --
#
# THE NEWS IS NOT SEARCHED FOR. A cycle (`feeds.pull`) stores what Google News
# and the outlets published; the light model is shown TITLES AND SUMMARIES
# ONLY — about 3k tokens for forty items — and gives each a topic and an
# importance. Nothing here opens a socket or calls a model; bot.py does both.

SCORE_TITLE_CHARS = 140
SCORE_SUMMARY_CHARS = 120


def future_note(day: date) -> str:
    """What R1's slot says for a date that has not happened. No model, no feed,
    no cost — there is no news from the future to report."""
    return (f"No news yet — {day.strftime('%a')} {day.day} {day.strftime('%b')} "
            "hasn't happened.")


def preselect(items: list, *, cap: int, per_hint: int = 3,
              people: Optional[set] = None, carried_before: str = "") -> list:
    """THE PRE-RANK (NEWS-PATCH-OCT16): the feed items in the order the scorer
    should see them, at most `cap`. A RANK, NOT A CUT — the caller scores in
    batches of NEWS_SCORE_MAX_ITEMS down this list until nothing is left or
    NEWS_SCORE_MAX_CALLS is reached.

    THE GROUPS, IN ORDER, each newest first:
      1. flagged, naming one of our PEOPLE (`people`: lower-case names; a
         researcher's item always counts);
      2. flagged, naming a company (or a watch name);
      3. from an outlet's own feed (no `topic_hint`) and carried by TWO OR MORE
         outlets (`outlet_count`);
      4. from a topic query (`topic_hint`);
      5. the rest.

    ON 14 OCT the old order put every flagged item first, 564-603 of them an
    hour, and industry stories never reached the scorer. The flood is now
    capped in feeds.py (NEWS_NAME_FLOOD_MAX), and what is left of it no longer
    hides everything else for good: the next batch reaches it.

    CARRIED FIRST. With `carried_before` (an ISO time: the start of this
    cycle) an item first seen before it — one an earlier cycle could not reach
    — goes ahead of this cycle's, in the same five groups.

    Two rows with one story key (or headline key, or link) count once.
    `per_hint` is no longer read: a topic is not rationed in a rank.
    """
    cap = max(1, int(cap))
    people = {str(p).strip().lower() for p in (people or ()) if str(p).strip()}

    def group(row) -> int:
        if is_ours(row):
            names = {n.strip().lower() for n in str(row.get("names") or "").split(",")}
            if str(row.get("kind") or "") == KIND_RESEARCHER or (names & people):
                return 0
            return 1
        if not (row.get("topic_hint") or "").strip():
            return 2 if outlets_of(row) >= 2 else 4
        return 3

    def carried(row) -> int:
        seen = str(row.get("seen_at") or "")
        return 0 if (carried_before and seen and seen < carried_before) else 1

    ordered = sorted(items or [], key=lambda r: str(r.get("published_at") or ""),
                     reverse=True)
    ordered = sorted(ordered, key=lambda r: (carried(r), group(r)))
    out: list = []
    keys: set = set()
    for row in ordered:
        ids = {k for k in (row.get("story_key"), row.get("headline_key"),
                           row.get("url_key")) if k}
        if not ids:
            ids = {story_key(row.get("title") or "")} - {""}
        if ids & keys:
            continue
        keys |= ids
        out.append(row)
        if len(out) >= cap:
            break
    return out


# WHAT MAKES A STORY BREAKING — the team's table (NFT2-1087, section 8), carried
# in the scoring prompt WORD FOR WORD. A row is (about us, industry).
BREAKING_TABLE = (
    ("About us (score 4 or 5)", "Industry (score 5 only)"),
    ("A funding round, an acquisition, or a merger.",
     "A new frontier model from a top lab."),
    ("A shutdown, or layoffs.",
     "A law or a rule that changes how AI companies buy data or run evals."),
    ("A new CEO, CTO, or head of AI.",
     "A big move by a competitor on the watch list."),
    ("A product launch in data, evals, voice, or annotation.",
     "A market event that changes AI spending for a quarter or more."),
    ("A regulatory action against them.",
     "News that changes the India or UK AI market."),
    ("One of our people changes job or company.", "—"),
    ("One of our researchers publishes a paper in our field.", "—"),
)

# WHO THE SCORE IS FOR: three lines about Membrane, so "important" means
# important to this team and not to the industry at large.
MEMBRANE_LINES = (
    "Membrane sells student-panel human data in Bangalore.",
    "Its work: evals and preference data, Hinglish and Indic speech, voice AI data.",
    "It also does RLHF and red-teaming.",
)


def breaking_table() -> str:
    """The table as the prompt carries it: one `| left | right |` line a row."""
    return "\n".join(f"| {a} | {b} |" for a, b in BREAKING_TABLE)


def score_prompt(items: list, topics: list, *, today: date, mode: str) -> str:
    """ONE scoring call for a batch of feed items: titles and summaries only.

    The model does not search and is given no page text. It returns one SCORE
    line per item worth a 3 or more; everything it leaves out is recorded as
    filler (1) by the caller, so no item is ever paid for twice.

    THE MODEL DOES NOT DECIDE WHAT IS ABOUT US. That was decided by rule when
    the item was stored, and the item says so: `[ABOUT US: names]`. What the
    model decides is how much the story matters, against the team's own table
    of what is breaking (`BREAKING_TABLE`) and three lines on what Membrane
    sells. `mode` is kept for the caller's log line; the prompt is the same
    for a cycle, the daily post and a question.
    """
    seeds = [str(t).strip() for t in (topics or []) if str(t).strip()]
    lines = [
        "Below are news items from RSS feeds — a title and sometimes a summary "
        f"each. Today is {dl.iso(today)}. Score them for the sales team at "
        "Membrane.",
        "",
        "ABOUT MEMBRANE:",
    ]
    lines += [f"  {line}" for line in MEMBRANE_LINES]
    lines += [
        "",
        "TOPICS — these are SEEDS, NOT LIMITS. The team's sheet says 'not limited "
        "to these', so important news off this list is welcome; tag it OTHER:",
        "  " + ", ".join(seeds),
        "",
        "IMPORTANCE, 1-5:",
        "  5 = breaking: the whole industry is talking about it today",
        "  4 = a sales team must know this week",
        "  3 = useful",
        "  2-1 = filler",
        "",
        "WHAT MAKES A STORY BREAKING:",
        breaking_table(),
        "",
        "AN ITEM MARKED [ABOUT US: …] names a company or a person this team sells "
        "to, one of its researchers, or a competitor it watches. Score it 4 or 5 "
        "ONLY for something in the left column; anything else about them is a 3 at "
        "most, and a passing mention is filler. An item NOT marked is industry "
        "news: score it 5 ONLY for something in the right column. Everything not "
        "in the table is a 4 at most.",
        "",
        "ITEMS:",
    ]
    for i, it in enumerate(items, 1):
        title = " ".join(str(it.get("title") or "").split())[:SCORE_TITLE_CHARS]
        summary = " ".join(str(it.get("summary") or "").split())[:SCORE_SUMMARY_CHARS]
        source = " ".join(str(it.get("source") or "").split())[:30]
        about = " ".join(str(it.get("names") or "").split())[:120] if is_ours(it) else ""
        lines.append(f"{i} | {source} | {title}" + (f" — {summary}" if summary else "")
                     + (f" [ABOUT US: {about}]" if about else ""))
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


def story_from_feed(row: dict, *, topics: Optional[list] = None,
                    default_importance: int = 3) -> dict:
    """A stored feed row as the story dict `choose_main` and `render` use.

    BOTH NUMBERS RIDE ON IT: `flag` (about us, by rule) with the `names` it
    carries, and `importance` (the model's). An unscored row takes
    `default_importance` — a flagged story the scorer never reached goes into
    the daily post at 3.
    """
    flag = 1 if is_ours(row) else 0
    kind = str(row.get("kind") or "")
    if not flag:
        kind = KIND_INDUSTRY
    elif kind in ("", KIND_INDUSTRY):
        kind = KIND_POC
    return {
        "topic": _canonical_topic(row.get("topic") or "", topics),
        "headline": str(row.get("title") or "").strip(),
        "what": str(row.get("what") or "").strip(),
        "url": str(row.get("url") or "").strip(),
        "url_key": str(row.get("url_key") or ""),
        "headline_key": str(row.get("headline_key") or ""),
        "importance": max(1, min(5, int(row.get("importance")
                                        or default_importance))),
        "source": str(row.get("source") or "").strip(),
        # industry | poc | researcher, and for a story about a sheet company
        # the row it is about (R10 reads `sheet_ref`; no post shows it).
        "kind": kind,
        "flag": flag,
        "names": str(row.get("names") or "").strip() if flag else "",
        "sheet_ref": str(row.get("sheet_ref") or "").strip() if flag else "",
        "source_rank": int(row.get("source_rank") or SOURCE_RANK_UNKNOWN),
        "carried_from": str(row.get("carried_from") or ""),
        "carry_count": int(row.get("carry_count") or 0),
        "published_at": str(row.get("published_at") or ""),
        "seen_at": str(row.get("seen_at") or ""),
        # NEWS-PATCH-OCT16: how many outlets carried it (the industry's
        # two-outlet breaking rule) and who scored it ("rule" = the launch
        # rule, which needs no second outlet).
        "story_key": str(row.get("story_key") or ""),
        "outlet_count": outlets_of(row),
        "scored_by": str(row.get("scored_by") or ""),
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
        row["scored_by"] = SCORED_BY_MODEL
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
    lines.append("Tell me which ones to add. I won't add anything without a yes.")
    return "\n".join(lines)


# -- the cycle's clock --------------------------------------------------------
#
# THE FEEDS ARE READ ONCE AN HOUR FROM NEWS_PULL_FIRST TO NEWS_PULL_UNTIL
# (NFT2-1087), not every 15 minutes and not at a list of check slots. Nothing
# here reads a clock: the caller hands in the REAL time, because a cycle runs
# on the real clock whatever date a tester is standing on.

PULL_FIRST_DEFAULT = "14:00"
PULL_UNTIL_DEFAULT = "23:00"
MAX_WINDOW_HOURS = 24


def _minutes(hhmm: str) -> Optional[int]:
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(hhmm or ""))
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return None
    return h * 60 + mi


_BAD_TIMES_LOGGED: set = set()


def _pull_time(name: str, default: str) -> int:
    raw = getattr(config, name, default)
    at = _minutes(raw)
    if at is None:
        if (name, raw) not in _BAD_TIMES_LOGGED:
            _BAD_TIMES_LOGGED.add((name, raw))
            log.warning("[news-cycle] %s=%r is not a time like 14:00; using %s", name,
                        raw, default)
        at = _minutes(default)
    return at


def pull_slots() -> list:
    """The day's cycles as "HH:MM" strings: NEWS_PULL_FIRST, then one an hour,
    the last at or before NEWS_PULL_UNTIL. Ten with the defaults."""
    first = _pull_time("NEWS_PULL_FIRST", PULL_FIRST_DEFAULT)
    until = max(first, _pull_time("NEWS_PULL_UNTIL", PULL_UNTIL_DEFAULT))
    return [f"{m // 60:02d}:{m % 60:02d}" for m in range(first, until + 1, 60)]


def cycle_slot(now: datetime) -> Optional[str]:
    """The cycle `now` belongs to: the latest slot at or before it, or None
    before the first one of the day. After the last slot it is still the last
    one, so a bot started at 23:40 runs the 23:00 cycle rather than none."""
    at = now.hour * 60 + now.minute
    best = None
    for s in pull_slots():
        if _minutes(s) <= at:
            best = s
    return best


def due_slots(now: datetime, claimed, *, skip_hours: Optional[int] = None) -> dict:
    """WHICH CYCLE A TICK RUNS (NEWS-PATCH-OCT16): the OLDEST UNCLAIMED slot of
    today at or before `now` — not the latest. {"run", "late", "skip"}.

    `claimed` is the hours already in `news_cycles` for today (any status).
    `run` is the slot to run, or None; `late` its lateness in minutes (0 on
    time); `skip` is [(slot, minutes late)] for every older unclaimed slot more
    than `skip_hours` WHOLE hours late (NEWS_CYCLE_LATE_SKIP_HOURS, 3): the
    caller claims those "skipped" and does not run them, so the bot does not
    replay a whole day after an outage. A tick at 19:10 with 15:00-18:00
    unclaimed skips 15:00 (four hours late) and runs 16:00 (three); the next
    tick runs 17:00.

    WHY. On 14 Oct five of ten cycles never ran: `cycle_slot` only ever
    offered the latest slot, so a tick that came late, or a cycle that ran
    long, stepped over an hour and nothing went back for it.
    """
    if skip_hours is None:
        skip_hours = int(getattr(config, "NEWS_CYCLE_LATE_SKIP_HOURS", 3))
    have = {str(h) for h in (claimed or ())}
    at = now.hour * 60 + now.minute
    skip: list = []
    for s in pull_slots():
        start = _minutes(s)
        if start > at:
            break
        if s in have:
            continue
        late = at - start
        if late // 60 > int(skip_hours):
            skip.append((s, late))
            continue
        return {"run": s, "late": late, "skip": skip}
    return {"run": None, "late": 0, "skip": skip}


def late_note(minutes: int, *, grace: int = 15) -> str:
    """"late by 40 min" — what a catch-up cycle's row says. '' on time: within
    `grace` minutes of the slot is the first tick that could have run it (the
    caller passes the sweep interval, at most 15)."""
    m = int(minutes or 0)
    if m <= max(0, int(grace)):
        return ""
    return f"late by {m} min" if m < 60 else f"late by {m // 60} h {m % 60} min"


def window_hours(last_done: Optional[datetime], now: datetime) -> int:
    """How many hours back a cycle reads: from the last cycle that COMPLETED,
    rounded up — never less than NEWS_PULL_WINDOW_HOURS (so two hourly pulls
    overlap and nothing falls between them), never more than 24. With no
    completed cycle to measure from, the full 24."""
    floor = max(1, min(MAX_WINDOW_HOURS, int(config.NEWS_PULL_WINDOW_HOURS)))
    if last_done is None:
        return MAX_WINDOW_HOURS
    seconds = (now - last_done).total_seconds()
    hours = int(-(-seconds // 3600)) if seconds > 0 else 0
    return max(floor, min(MAX_WINDOW_HOURS, hours))


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

    print("\nthe daily post: ours first, then the industry (NFT2-1087)")

    def st(n, imp, flag=0, topic="evals", when="2026-09-29T08:00:00+00:00", rank=3,
           carried=0):
        return {"headline": f"story {n}", "what": "it happened", "url": f"https://x.com/{n}",
                "url_key": f"x.com/{n}", "headline_key": f"story {n}", "topic": topic,
                "importance": imp, "flag": flag, "kind": "poc" if flag else "industry",
                "names": "Acme" if flag else "", "source": "X", "published_at": when,
                "seen_at": when, "source_rank": rank,
                "carried_from": "2026-09-28" if carried else "", "carry_count": carried}

    class Posted:
        def __init__(self, seen=(), week=(), today_counts=None):
            self.seen, self.week, self.counts = set(seen), list(week), today_counts or {}

        def news_story_seen(self, ukey, hkey, *, since_iso):
            return {"url_key": ukey, "posted_on": "2026-09-28", "kind": "breaking"} \
                if ukey in self.seen else None

        def news_topic_count_today(self, topic, on_date):
            return self.counts.get(topic, 0)

        def news_topics_this_week(self, week):
            return list(self.week)

    def main(stories, db=None, **kw):
        args = dict(today=today, db=db or Posted(), cap=5, per_topic=2,
                    topics_per_week=6, min_importance=5, offtopic_bypass=4)
        args.update(kw)
        return choose_main(stories, **args)

    def names(rows):
        return [s["headline"] for s in rows]

    nine = [st("o3", 3, 1), st("o2", 2, 1), st("o1", 1, 1),
            st("i4a", 4, topic="RLHF"), st("i4b", 4, topic="evals"),
            st("i3a", 3, topic="voice agent"), st("i3b", 3, topic="TTS"),
            st("i2a", 2, topic="STT"), st("i2b", 2, topic="AI trust")]
    got = main(nine)
    check("3 ours (3,2,1) + 6 industry: the 3 of ours first, then the two 4s",
          names(got["keep"]), ["story o3", "story o2", "story o1", "story i4a",
                               "story i4b"])
    check("...industry news that did not fit is dropped, never carried",
          (names(got["dropped"]), got["carry"]), (["story i3a", "story i3b"], []))
    check("...and an industry story below 3 is not padding",
          any("story i2a" in line and "below the 3" in line for line in got["skipped"]),
          True)
    seven_ours = [st(f"m{i}", 3, 1, when=f"2026-09-29T0{9 - i}:00:00+00:00")
                  for i in range(7)]
    got = main(seven_ours)
    check("7 about us, 5 slots: five go and two are carried",
          (len(got["keep"]), names(got["carry"])), (5, ["story m5", "story m6"]))
    got = main([st("new", 3, 1), st("c1", 3, 1, carried=1, when="2026-09-27T08:00:00+00:00"),
                st("c2", 3, 1, carried=1, when="2026-09-26T08:00:00+00:00")]
               + [st(f"i{i}", 4, topic=f"t{i}") for i in range(10)], topics_per_week=99)
    check("a carried story goes before a new one at the same score",
          names(got["keep"])[:3], ["story c1", "story c2", "story new"])
    check("...and the industry takes what is left", len(got["industry"]), 2)
    check("a tie in score goes to the higher source rank, then to the newest",
          names(_rank([st("a", 3, rank=4), st("b", 3, rank=1,
                                                when="2026-09-27T00:00:00+00:00"),
                       st("c", 3, rank=1)])), ["story c", "story b", "story a"])
    check("only ours", (names(main([st("p", 2, 1)])["ours"]),
                        main([st("p", 2, 1)])["industry"]), (["story p"], []))
    check("no stories about us at all: an ordinary post",
          names(main([st("a", 4), st("b", 3, topic="RLHF")])["keep"]),
          ["story a", "story b"])

    full = Posted(today_counts={"evals": 2, "OTHER": 2})
    got = main([st("e", 4, topic="evals"), st("o4", 4, topic="OTHER"),
                st("o3", 3, topic="OTHER"), st("p", 3, 1, topic="evals")], db=full)
    check("an OTHER story at importance 4 gets in past a full topic cap",
          "story o4" in names(got["keep"]), True)
    check("...an OTHER story at 3 does not, and neither does an on-topic 4",
          sorted(names(got["dropped"])), ["story e", "story o3"])
    check("...and a story about us is never held by a topic cap",
          "story p" in names(got["ours"]), True)
    three = main([st(f"t{i}", 3, topic="evals", when=f"2026-09-29T0{i}:00:00+00:00")
                  for i in range(3)])
    check("three industry stories on one topic at 3: only two are posted",
          len(three["keep"]), 2)
    week = Posted(week=["a", "b", "c", "d", "e", "f"])
    got = main([st("n", 4, topic="new topic"), st("o", 4, topic="OTHER")], db=week)
    check("the per-week limit: OTHER at 4 walks past it, a seventh topic does not",
          (names(got["keep"]), names(got["dropped"])), (["story o"], ["story n"]))
    check("importance 5 walks past every spread limit",
          names(main([st("big", 5, topic="evals")], db=full)["keep"]), ["story big"])
    check("a story already sent as breaking is not in the daily post",
          names(main([st("old", 5, 1)], db=Posted(seen={"x.com/old"}))["keep"]), [])

    print("\nbreaking: the score, new, and the flood guard")
    now = datetime(2026, 9, 29, 9, 0, tzinfo=timezone.utc)

    def brk(stories, db=None, **kw):
        args = dict(today=today, db=db or Posted(), now=now, max_age_hours=3,
                    ours_min=4, industry_min=5, cap=3)
        args.update(kw)
        return choose_breaking(stories, **args)

    got = brk([st("ours4", 4, 1), st("ours3", 3, 1), st("ind5", 5), st("ind4", 4)])
    check("about us at 4 and industry at 5 are breaking; ours at 3 and industry at 4 "
          "are not", sorted(names(got["keep"])), ["story ind5", "story ours4"])
    got = brk([st("late", 5, 1, when="2026-09-29T04:00:00+00:00")])
    check("first seen five hours ago: not breaking, however high the score",
          (got["keep"], "NEWS_BREAKING_MAX_AGE_HOURS" in got["skipped"][0]), ([], True))
    check("a carried story is never breaking",
          brk([st("c", 5, 1, carried=1)])["keep"], [])
    five = [st(f"b{i}", 5 if i < 2 else 4, 1, rank=1 + i,
               when="2026-09-29T08:30:00+00:00") for i in range(5)]
    got = brk(five)
    check("five in one cycle: the top three go, by score then source rank",
          names(got["keep"]), ["story b0", "story b1", "story b2"])
    check("...and two are held, for the next cycle", names(got["held"]),
          ["story b3", "story b4"])
    check("a story already posted is not sent again",
          brk([st("old", 5)], db=Posted(seen={"x.com/old"}))["keep"], [])

    print("\nscoring: what is about us is marked, and shown first")
    feed = [{"title": "Robots walk", "summary": "", "source": "A", "url_key": "a",
             "headline_key": "robots walk", "topic_hint": "", "published_at": "2",
             "kind": "industry", "flag": 0, "names": ""},
            {"title": "Acme raises $40M", "summary": "", "source": "B", "url_key": "b",
             "headline_key": "acme raises", "topic_hint": "", "published_at": "1",
             "kind": "poc", "flag": 1, "names": "Acme, Jo Roe",
             "sheet_ref": "Acme — on Master Pipeline"}]
    check("a flagged item is never squeezed out of the scoring call",
          [r["url_key"] for r in preselect(feed, cap=1)], ["b"])
    flood = [{"title": f"Andi story {i}", "url_key": f"p{i}", "headline_key": f"andi {i}",
              "topic_hint": "", "published_at": f"2026-09-29T{i:02d}", "kind": "poc",
              "flag": 1, "names": "Andi"} for i in range(8)] + \
            [{"title": f"Outlet story {i}", "url_key": f"o{i}", "headline_key": f"out {i}",
              "topic_hint": "", "published_at": f"2026-09-28T{i:02d}", "kind": "industry",
              "flag": 0, "names": ""} for i in range(20)]
    shown = preselect(flood, cap=10)
    check("every flagged item is shown, with no per-name limit, then the rest",
          (sum(1 for r in shown if r["flag"]), sum(1 for r in shown if not r["flag"])),
          (8, 2))
    sp = score_prompt(feed, topics, today=today, mode=MODE_CYCLE)
    check("the prompt says who a flagged item names",
          "[ABOUT US: Acme, Jo Roe]" in sp, True)
    check("...and an industry item carries no marker",
          "Robots walk [ABOUT US" in sp, False)
    check("the breaking table is in the prompt, word for word",
          all(f"| {a} | {b} |" in sp for a, b in BREAKING_TABLE), True)
    check("...with the three lines about Membrane",
          all(line in sp for line in MEMBRANE_LINES)
          and "student-panel human data in Bangalore" in sp
          and "Hinglish and Indic speech" in sp and "RLHF and red-teaming" in sp, True)
    check("...and what is not in the table is a 4 at most",
          "Everything not in the table is a 4 at most." in sp, True)
    scored = parse_scores("SCORE | 2 | OTHER | 4 | Acme raised a round", feed)
    check("a scored flagged item keeps its flag and its names",
          [(s["flag"], s["names"], s["kind"]) for s in scored],
          [(1, "Acme, Jo Roe", "poc")])

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
    rows = body.splitlines()
    check("the opener is the first line under the heading", rows[0], OPENER)
    check("only industry stories: no label at all",
          LABEL_OURS in body or LABEL_INDUSTRY in body, False)
    check("a story is its headline and its outlet, nothing else", rows[1],
          "- Lab ships ([n.com](<https://n.com/lab-ships>))")
    check("...no bold anywhere under the heading", "**" in body, False)
    check("...with no '— what happened' clause", " — " in rows[1], False)
    check("no blank line under the opener, one between two stories",
          (rows[1].startswith("- "), rows[2], rows[3].startswith("- ")), (True, "", True))
    check("no topic tag", "[evals]" in body or "[Other]" in body, False)
    check("no bare url", "<http" in body.replace("(<http", ""), False)
    check("no pings survive", "<@123>" in body or "@everyone" in body, False)
    check("the closer is the last line", rows[-1], CLOSER)
    b = render([S("evals", "Alpha lab ships", 5), S("RLHF", "Beta raises funds", 4)],
               mode=MODE_BREAKING, day=date(2026, 10, 8), tags="<@1> <@2>")
    check("breaking opens with its heading: no date, the tags on its line",
          b.splitlines()[0], "**Breaking news** <@1> <@2>")
    check("...and carries every story as a bullet, right under it",
          (b.count("\n- "), b.splitlines()[1].startswith("- ")), (2, True))
    seven = [S("evals", f"The {w} lab ships", 5, f"https://a.com/{w}")
             for w in ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf")]
    check("no news message carries more than five stories",
          render(seven, mode=MODE_BREAKING, day=date(2026, 10, 8)).count("\n- "), 5)
    check("no stories, no text — and no quiet line", render([]), "")

    print("\nthe templates (NFT2-1087, section 16)")
    G = "https://news.google.com/rss/articles/CBMi?oc=5"
    gn = {"headline": "State AGs launch investigations into OpenAI AI safety - Reuters",
          "source": "Reuters", "url": G, "url_key": "g1", "headline_key": "state ags",
          "importance": 4, "kind": KIND_INDUSTRY, "published_at": "2026-10-08T05:00:00Z"}
    check("a Google News title loses its ' - Outlet'", clean_headline(gn),
          "State AGs launch investigations into OpenAI AI safety")
    check("...and is named for the outlet", outlet(gn), "Reuters")
    direct = {"headline": "TM Forum and Accenture launch AI trust framework.",
              "source": "AI News & Artificial Intelligence | TechCrunch",
              "url": "https://www.techcrunch.com/2026/10/08/tm", "url_key": "d1",
              "headline_key": "tm forum", "importance": 4, "kind": KIND_INDUSTRY,
              "published_at": "2026-10-08T04:00:00Z"}
    check("a trailing full stop comes off", clean_headline(direct),
          "TM Forum and Accenture launch AI trust framework")
    check("a direct feed item is named by its site, not its feed's title",
          outlet(direct), "techcrunch.com")
    check("a dash that is part of the headline stays",
          clean_headline({"headline": "GPT-5 - what we know so far", "source": "Wired",
                          "url": "https://wired.com/x"}), "GPT-5 - what we know so far")
    check("a pipe tail naming the outlet goes",
          clean_headline({"headline": "As AI gains autonomy, enterprises must retain "
                                      "authority | Hindustan Times",
                          "source": "Hindustan Times", "url": G}),
          "As AI gains autonomy, enterprises must retain authority")
    check("a strapline source is cut back to the name",
          outlet({"source": "ABC News - Breaking News, Latest News and Videos", "url": G}),
          "ABC News")
    check("a mention token and asterisks in a feed title are removed",
          clean_headline({"headline": "**Lab** ships <@123> evals", "url": G}),
          "Lab ships evals")
    check("the line — the only shape", story_line(gn),
          f"- State AGs launch investigations into OpenAI AI safety ([Reuters](<{G}>))")
    mine = dict(gn, url=G + "m", url_key="m1", headline_key="sarvam raises",
                headline="Sarvam AI raises $90M - Economic Times", source="Economic Times",
                flag=1, names="Sarvam AI", kind=KIND_POC,
                sheet_ref="Sarvam AI — on Master Pipeline", what="a round")
    check("a story about us has the same line: no sheet tag, no clause, no bold",
          story_line(mine), f"- Sarvam AI raises $90M ([Economic Times](<{G}m>))")
    twin = dict(gn, url="https://other.com/a", url_key="g2", headline_key="other key",
                source="Other Wire",
                headline="State AGs launch investigations into OpenAI AI safety | "
                         "Other Wire")
    check("the same headline from two outlets is one story",
          len(unique([gn, twin, direct])), 2)
    day = date(2026, 10, 13)
    check("template A: both kinds — both labels, ours first",
          daily_message([gn, mine, direct], day=day, tags="@V @S"),
          "**AI News, Tue 13 Oct** @V @S\n" + OPENER + "\n" + LABEL_OURS + "\n"
          + story_line(mine) + "\n" + LABEL_INDUSTRY + "\n" + story_line(gn) + "\n\n"
          + story_line(direct) + "\n" + CLOSER)
    check("template B: only ours — no 'Elsewhere in AI'",
          daily_message([mine], day=day, tags="@V @S"),
          "**AI News, Tue 13 Oct** @V @S\n" + OPENER + "\n" + LABEL_OURS + "\n"
          + story_line(mine) + "\n" + CLOSER)
    check("template C: only industry — no label",
          daily_message([gn, direct], day=day, tags="@V @S"),
          "**AI News, Tue 13 Oct** @V @S\n" + OPENER + "\n" + story_line(gn) + "\n\n"
          + story_line(direct) + "\n" + CLOSER)
    check("template D: nothing to send is no message", daily_message([], day=day), "")
    check("template G: an answer — heading, stories, nothing else",
          render([mine, gn], mode=MODE_ANSWER, day=day, tags="@V"),
          "**AI News, Tue 13 Oct**\n" + story_line(mine) + "\n\n" + story_line(gn))
    post = daily_message([gn, mine, direct], day=day, tags="<@1> <@2>")
    check("layout leaves a rendered message as it is", layout(post), post)
    check("...and takes a stray blank line out",
          layout(post.replace(OPENER + "\n", OPENER + "\n\n")), post)
    check("a message that fits is one message", len(split_message(post)), 1)
    long = [dict(gn, url=G + "x" * 500 + str(i), url_key=f"L{i}", headline_key=f"L{i}",
                 headline=f"Long link story number {i}", flag=1 if i < 2 else 0)
            for i in range(5)]
    parts = split_message(daily_message(long, day=day, tags="<@1> <@2>"))
    check("past 2,000 characters it is split, between stories",
          (len(parts) > 1, all(len(x) <= 2000 for x in parts)), (True, True))
    check("...and every later part carries the heading again, with no tags",
          [x.split("\n")[0] for x in parts[1:]],
          ["**AI News, Tue 13 Oct (continued)**"] * (len(parts) - 1))
    check("...and no opener, no label and no closer",
          any(w in x for x in parts[1:] for w in (OPENER, LABEL_OURS, LABEL_INDUSTRY,
                                                  CLOSER)), False)
    check("...with every story still there, once",
          sum(x.count("\n- ") for x in parts), 5)
    check("no part ends on a label with nothing under it",
          any(x.split("\n")[-1] in (LABEL_OURS, LABEL_INDUSTRY) for x in parts), False)
    parts = split_message(render(long, mode=MODE_BREAKING, tags="<@1>"))
    check("a breaking post splits the same way",
          parts[1].split("\n")[0], "**Breaking news (continued)**")

    print("\nwhat somebody who asks is given")

    def A(name, imp, when):
        return {"headline": name, "url": f"https://a.com/{name.replace(' ', '-')}",
                "url_key": name, "headline_key": name, "importance": imp,
                "kind": KIND_INDUSTRY, "published_at": when, "source": "A"}

    nine = [A(f"story {i}", imp, f"2026-10-08T0{i}:00:00Z")
            for i, imp in enumerate([3, 5, 4, 3, 5, 4, 3, 4, 5], start=1)]
    got = choose_answer(nine, is_sent=lambda s: False, mode="fresh")
    check("nine unsent: the five highest-scored",
          sorted(s["importance"] for s in got["stories"]), [4, 4, 5, 5, 5])
    check("...at a tie the newer one", "story 3" in [s["headline"] for s in got["stories"]],
          False)
    check("...listed newest first", [s["headline"] for s in got["stories"]],
          ["story 9", "story 8", "story 6", "story 5", "story 2"])
    sent = {s["url_key"] for s in nine[:6]}
    got = choose_answer(nine, is_sent=lambda s: s["url_key"] in sent, mode="fresh")
    check("three unsent: those three, no padding", [s["headline"] for s in got["stories"]],
          ["story 9", "story 8", "story 7"])
    check("...and it is not a repeat", got["repeat"], False)
    got = choose_answer(nine, is_sent=lambda s: True, mode="fresh")
    check("nothing unsent: the top five again, and it says so",
          (len(got["stories"]), got["repeat"]), (5, True))
    check("nothing at all: nothing", choose_answer([], is_sent=lambda s: False)["stories"], [])

    print("\na plain news question")
    for ask in ("any AI news?", "what's in the news", "top 5 headlines",
                "what is in the news forthis hour?", "what's new in AI?"):
        check(f"plain: {ask!r}", plain_question(ask), True)
    for ask in ("any news on ElevenLabs?", "news about voice agents",
                "where are we with Acme?", "AI news this week on evals"):
        check(f"not plain: {ask!r}", plain_question(ask), False)

    print("\nthe cycle's clock")
    saved_pull = (config.NEWS_PULL_FIRST, config.NEWS_PULL_UNTIL,
                  config.NEWS_PULL_WINDOW_HOURS)
    config.NEWS_PULL_FIRST, config.NEWS_PULL_UNTIL = "14:00", "23:00"
    config.NEWS_PULL_WINDOW_HOURS = 2
    check("ten pulls a day, 14:00 to 23:00",
          (len(pull_slots()), pull_slots()[0], pull_slots()[-1]), (10, "14:00", "23:00"))
    check("the latest slot at or before now",
          cycle_slot(datetime(2026, 9, 29, 16, 30)), "16:00")
    check("nothing before the first", cycle_slot(datetime(2026, 9, 29, 13, 59)), None)
    check("a missed hour runs late (NEWS-PATCH-OCT16)",
          due_slots(datetime(2026, 9, 29, 15, 40), ["14:00"], skip_hours=3)["run"], "15:00")
    check("...and one more than three whole hours late is skipped",
          [s for s, _m in due_slots(datetime(2026, 9, 29, 19, 10), ["14:00"],
                                    skip_hours=3)["skip"]], ["15:00"])
    check("one story key whatever the word order",
          story_key("Google launches Gemini 4 Aragon"),
          story_key("Gemini 4 Aragon: Google launches"))
    check("one shared name is not one story",
          same_story({"title": "Gemini 4 launched"}, {"title": "Gemini 4 fails safety test"}),
          False)
    check("the launch rule reads the title",
          (launch_rule({"title": "Google launches Gemini 4 Aragon"}),
           launch_rule({"title": "Gemini 4 review: is it worth it?"})), (True, False))
    check("after the last slot it is still the last",
          cycle_slot(datetime(2026, 9, 29, 23, 40)), "23:00")
    at = datetime(2026, 9, 29, 12, 30, tzinfo=timezone.utc)
    check("an hourly pull reads two hours back, so two pulls overlap",
          window_hours(at - timedelta(hours=1), at), 2)
    check("three hours down: the window reaches back to the last cycle",
          window_hours(at - timedelta(hours=3), at), 3)
    check("never more than 24", window_hours(at - timedelta(days=3), at), 24)
    check("no completed cycle to measure from: 24", window_hours(None, at), 24)
    config.NEWS_PULL_FIRST = "13:00"
    check("NEWS_PULL_FIRST moves the first pull", pull_slots()[0], "13:00")
    config.NEWS_PULL_FIRST = "2pm"
    check("an unreadable time falls back to 14:00", pull_slots()[0], "14:00")
    (config.NEWS_PULL_FIRST, config.NEWS_PULL_UNTIL,
     config.NEWS_PULL_WINDOW_HOURS) = saved_pull

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
    check("a topic query comes before a story only one outlet carries",
          picked[0]["topic_hint"] != "", True)
    picked = preselect([dict(feed[0], outlet_count=2)] + feed[1:], cap=40)
    check("an outlet's story carried by two outlets leads the industry",
          picked[0]["source"], "TechCrunch")
    check("a busy topic is ranked, not cut (NEWS-PATCH-OCT16)",
          sum(1 for p in picked if p["topic_hint"] == "robot"), 5)
    check("the same headline from a second outlet is not shown twice",
          sum(1 for p in picked if p["headline_key"] == feed[0]["headline_key"]), 1)
    check("the cap holds", len(preselect(feed, cap=2)), 2)
    sp = score_prompt(picked[:2], topics, today=today, mode=MODE_MAIN)
    check("it shows titles, numbered", "1 | TechCrunch | Lab ships eval suite — "
          "A public benchmark." in sp, True)
    check("it carries no url", "http" in sp, False)
    check("it asks for SCORE lines", "SCORE | <item number> |" in sp, True)
    check("seeds, not limits", "SEEDS, NOT LIMITS" in sp, True)
    check("an unflagged batch still carries the breaking table",
          "WHAT MAKES A STORY BREAKING:" in sp, True)
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
