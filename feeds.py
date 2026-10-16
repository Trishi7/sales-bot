"""THE FEED LAYER — RSS in, rows out. No LLM in this file, and no search API.

THE NEWS IS NOT SEARCHED FOR BY A MODEL. The outlets publish it as RSS, free,
and Google News turns any query into a feed. Once an hour from 14:00 IST
(`bot._news_cycle`) `pull()` reads, IN THIS ORDER, and stores what is NEW in
`news_feed_items`:

    1  Google News, OUR NAMES     every company on Master Pipeline, every
                                  company on Outreach PoCs and the watch list,
                                  NEWS_NAME_BATCH_SIZE names a search
    2  Google News, RESEARCHERS   once a day, at the big pull
    3  Google News, OUR TOPICS    one query per NEWS_TOPICS entry
    4  the outlets' own feeds     NEWS_RSS_FEEDS
    5  arXiv                      our researchers as authors (arxiv.py), once a
                                  day, at the big pull

The number is the item's `source_rank`. THE STORE KEEPS THE FIRST COPY of a
story, so the order is also who wins: Google's copy, found by searching for
one of our names, is the row that is kept, and a later copy that names us
marks it (`db.news_feed_add`).

THE FLAG IS SET HERE, BY RULE (NFT2-1087). Every item, whichever source it
came from, is checked in code for every name on the list — a whole phrase, on
word boundaries, any case (`names_in`) — and an item that names one of them
carries the `names` it found and `flag = 1`. Google matches a quoted name
anywhere in an article, and an item that merely mentions "Pulse" somewhere is
not news about the company called Pulse; and "Pulse" inside "Pulsed" is not a
match at all. No model is asked whether a story is about us.

    PEOPLE have no search of their own. An item that names a company is also
    checked for the people of that company. A WARM person — a connection, a DM
    or a meeting on their row — gets a direct search once a day, so a move to
    another company is not missed.

    A GENERIC NAME (NEWS_GENERIC_NAMES: Google, Meta, Cleo ...) is searched on
    its own, counts only in the TITLE, and flags the story only when one of
    our people there is on it too — or the scorer later gives it a 4.

    A RESEARCHER'S item is kept only when it also carries an AI word from
    NEWS_TOPICS, the person's institution or the person's company: two people
    share a name far more often than two companies do.

THE NAME LIST is built by `build_names` from the two tabs' rows — handed in by
the bot, which reads the sheets and stores the list in `news_names` — with the
"active row" test left out on purpose: every person on Outreach PoCs is
watched. Nobody on the mapping's departures list is; and when that list cannot
be read, NO PERSON is listed at all.

STAYING UNBLOCKED. Google requests go ONE AT A TIME with
NEWS_REQUEST_GAP_SECONDS between two of them (the outlets' feeds are different
hosts and stay in a thread pool). On an HTTP 429 the pull STOPS AT ONCE: what
has been read is stored, the result says `partial`, and nothing is retried —
the next cycle's window reaches back over this one.

A PULL MAKES ZERO API CALLS. It is plain HTTP GETs. What the light model later
sees is titles and summaries of the stored rows — and only the ones it has not
scored.

ONE STORY, ONE ROW. Deduplicated on the two identities the posted stories use
— `news.url_key` (the link, tracking stripped) and `news.headline_key` (who
did what) — plus, since NEWS-PATCH-OCT16, `news.story_key` (the same words in
any order) and `news.same_story` against the last 36 hours, with this pull's
names as the entities (`NameIndex.entities`). A funding round carried by four
feeds is stored once, and the row counts the four outlets.

FLAG DISCIPLINE (NEWS-PATCH-OCT16). A name shorter than NEWS_NAME_MIN_CHARS (4)
is never matched, and a name that has flagged NEWS_NAME_FLOOD_MAX (30) items in
one pull counts in the title only for the rest of it ("noisy_names").

A DIGEST IS NOT A STORY. A link `news.digest_link_reason` refuses (a roundup, a
newsletter, a host on NEWS_BLOCKED_DOMAINS) is dropped here, before it can be
scored or posted.

NEVER RAISES. A feed that times out or returns nonsense is one line in the log
and one entry in the result's `errors`; the others still land.
"""
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional
from urllib.parse import quote_plus

import config
import news

log = logging.getLogger(__name__)

SUMMARY_MAX_CHARS = 300
GOOGLE_NEWS = "https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"

# Where an item came from, 1 the highest. Ties in score are broken by it.
RANK_NAMES = 1
RANK_RESEARCHERS = 2
RANK_TOPICS = 3
RANK_OUTLETS = 4
RANK_ARXIV = 5

_db_getter: Optional[Callable] = None


def bind(getter: Optional[Callable]) -> None:
    """Where `news_feed_items` lives: `getter()` returns the DB. A getter, so
    a simulation's throwaway database never becomes the feed store."""
    global _db_getter
    _db_getter = getter


def _db():
    try:
        return _db_getter() if _db_getter is not None else None
    except Exception:
        log.debug("[feeds] no database handle", exc_info=True)
        return None


def utc_iso(when: datetime) -> str:
    """The one timestamp format the feed store uses: UTC, to the second."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc).isoformat(timespec="seconds")


def when_param(hours: int) -> str:
    """Google News's own window word: `when:1d` for the big pull (the last 24
    hours), `when:2h` for an hourly one. Google answers a bare query with up to
    a hundred items, some of them weeks old; this keeps a pull to what could
    still be news."""
    hours = max(1, int(hours))
    return "when:1d" if hours >= 24 else f"when:{hours}h"


def google_news_url(query: str, *, hours: int = 24) -> str:
    """The Google News RSS url for a finished query, in India's edition."""
    query = " ".join(str(query or "").split())
    return GOOGLE_NEWS.format(q=quote_plus(f"{query} {when_param(hours)}"))


def topic_query(topic: str) -> str:
    """A topic as a query: quoted when it is more than one word."""
    topic = " ".join(str(topic or "").split()).replace('"', "")
    return f'"{topic}"' if " " in topic else topic


# -- the name list ------------------------------------------------------------

KIND_INDUSTRY = "industry"
KIND_POC = "poc"
KIND_RESEARCHER = "researcher"

NAME_COMPANY = "company"
NAME_PERSON = "person"
NAME_RESEARCHER = "researcher"
NAME_WATCH = "watch"

# A name shorter than this is not watched: "AI", "Go" and "X" are words, and a
# query for one returns the whole day's news tagged as ours. FOUR since
# NEWS-PATCH-OCT16 (NEWS_NAME_MIN_CHARS; it was 3): on 14 Oct short sheet names
# flagged 564-603 items an hour as ours.
NAME_MIN_CHARS = max(1, int(getattr(config, "NEWS_NAME_MIN_CHARS", 4)))
POC_NAME_MIN_CHARS = NAME_MIN_CHARS          # the old name, still read by checks

# A company written two ways on two rows is ONE name: "CoRover.ai" and
# "CoRover". `gtm_sheet.normalise_header` alone turns the first into "corover
# ai", so the domain ending comes off before it is asked.
_DOMAIN_TAIL_RE = re.compile(r"\.(ai|io|com|co|app|dev|net|org|in|tech|xyz)$", re.I)

# What a row's cell has to hold to count as filled: a date, a "Yes", a note.
_BLANK_CELLS = frozenset({"", "no", "n", "false", "0", "-", "na", "n/a", "none",
                          "not yet", "pending"})


# The names already logged as too short, so each is said once per process.
_SHORT_LOGGED: set = set()


def _too_short(name: str) -> bool:
    """Is this name too short to match? Logged once per name: "too short to
    match" (NEWS-PATCH-OCT16, edge case 3)."""
    if not name or len(name) >= NAME_MIN_CHARS:
        return False
    if name.lower() not in _SHORT_LOGGED:
        _SHORT_LOGGED.add(name.lower())
        log.info("[feeds] name list: %r is too short to match (under %d characters, "
                 "NEWS_NAME_MIN_CHARS) — never matched", name, NAME_MIN_CHARS)
    return True


def clean_name(name) -> str:
    """A name as it is searched and stored: whitespace squeezed and double
    quotes removed (a quote inside a quoted query ends the phrase early). A
    trailing dot STAYS — "Acme Inc." is matched as it is written."""
    return " ".join(str(name or "").replace('"', " ").replace("“", " ")
                    .replace("”", " ").split())


def name_key(name) -> str:
    """One key per company however a row spells it."""
    import gtm_sheet

    return gtm_sheet.normalise_header(_DOMAIN_TAIL_RE.sub("", clean_name(name)))


def _filled(value) -> bool:
    return " ".join(str(value or "").split()).lower() not in _BLANK_CELLS


def names_in(text: str, name: str) -> bool:
    """Is `name` in `text` as a WHOLE PHRASE — on word boundaries, any case?
    "Pulse" is not in "Pulsed", and "Scale AI" is not in "upscale AIs"."""
    name = clean_name(name)
    if len(name) < NAME_MIN_CHARS:
        return False
    return re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", str(text or ""),
                     re.I) is not None


def _text_of(item: dict) -> str:
    return f"{item.get('title') or ''} \n {item.get('summary') or ''}"


def names_it(item: dict, target: dict) -> bool:
    """Does this item NAME the target in its title or summary? (`names_in`,
    for the target's name or any other spelling it has.)"""
    spellings = [target.get("name")] + list(target.get("aliases") or [])
    text = _text_of(item)
    return any(names_in(text, s) for s in spellings if s)


def is_institution(cell, words: Optional[list] = None) -> bool:
    """Is a Company/Uni cell a university rather than a company? It contains
    one of NEWS_INSTITUTION_WORDS as a whole phrase: "Middlesex University
    London" does ("university"), "IIT Bombay" does, and "AI Security Institute
    (UK Govt.)" does NOT — the word on the list is "institute of"."""
    text = clean_name(cell)
    if not text:
        return False
    for word in (words if words is not None else (config.NEWS_INSTITUTION_WORDS or [])):
        word = " ".join(str(word or "").split())
        if word and re.search(r"(?<!\w)" + re.escape(word) + r"(?!\w)", text, re.I):
            return True
    return False


# Words an institution's name shares with every other institution's.
_INSTITUTION_FILLER = frozenset("""
university universities institute institutes college school of the for and at
in de la technology science sciences
""".split())


def institution_in(text: str, institution: str) -> bool:
    """Does `text` name this institution? The whole name, or two words of it
    that stand together and are not both filler: the sheet says "Middlesex
    University London" and the article says "Middlesex University"."""
    institution = clean_name(institution)
    if not institution:
        return False
    if names_in(text, institution):
        return True
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9.&'-]*", institution)
    for a, b in zip(words, words[1:]):
        if a.lower() in _INSTITUTION_FILLER and b.lower() in _INSTITUTION_FILLER:
            continue
        if names_in(text, f"{a} {b}"):
            return True
    return False


def build_names(pipeline_rows: Optional[list], poc_rows: Optional[list], *,
                departures=None, pipeline_title: str = "Master Pipeline",
                poc_title: str = "Outreach PoCs", watch: Optional[list] = None,
                generic: Optional[list] = None,
                institution_words: Optional[list] = None) -> list:
    """THE NAME LIST, from the two tabs' rows. Pure: the bot reads the sheets.

    WHO IS ON IT:
      - every COMPANY on Master Pipeline, and every company on Outreach PoCs;
      - every PERSON on Outreach PoCs. THE "ACTIVE ROW" TEST IS NOT APPLIED
        (NFT2-1087): a row nobody has contacted yet, or one marked lost, is
        still somebody the team wants to hear about. Neither is a stop reason;
      - the WATCH names (NEWS_WATCH_NAMES — the competitors). One that is also
        on a sheet is one entry, in the sheet's spelling.

    WHO IS NOT:
      - a name shorter than NAME_MIN_CHARS;
      - a person on the mapping's departures list (`departures.lookup`). Their
        company is still watched;
      - ANY person, when `departures` is None — the list could not be read.
        "Never" cannot be honoured against a list the bot does not have, and
        news about somebody who has left, posted as news about our contact, is
        the mistake the list exists to prevent. Companies still work.

    RESEARCHERS. A person whose Company/Uni cell is an institution
    (`is_institution`) is kind `researcher`: the institution is kept as a
    filter word and is NOT listed as a company. A person at a company whose
    Research Paper Link is filled is in BOTH lanes: kind `person`, with
    `researcher` set, and the company listed as usual.

    Each entry: {key, kind, name, aliases, company, institution, researcher,
    warm, generic, source}. One entry per key — a company spelt two ways is
    one name with the other spelling in `aliases`; a person on two rows is one
    person.
    """
    generic_keys = {name_key(g) for g in
                    (generic if generic is not None else (config.NEWS_GENERIC_NAMES or []))}
    companies: dict = {}
    people: dict = {}

    def add_company(raw, source: str, kind: str = NAME_COMPANY) -> None:
        name = clean_name(raw)
        key = name_key(name)
        if not key or _too_short(name):
            return
        have = companies.get(key)
        if have is None:
            companies[key] = {
                "key": f"company|{key}", "kind": kind, "name": name, "aliases": [],
                "company": name, "institution": "", "researcher": False,
                "warm": False, "generic": key in generic_keys, "source": source}
            return
        if kind == NAME_WATCH:
            return                       # already on a sheet: the sheet's spelling
        if name.lower() != have["name"].lower() \
                and name.lower() not in {a.lower() for a in have["aliases"]}:
            have["aliases"].append(name)
        if source and source not in have["source"].split(" and "):
            have["source"] = f"{have['source']} and {source}" if have["source"] else source

    for row in pipeline_rows or []:
        add_company(row.get("company"), pipeline_title)

    left: list = []
    for row in poc_rows or []:
        company = clean_name(row.get("company"))
        name = clean_name(row.get("name"))
        campus = is_institution(company, institution_words)
        if company and not campus:
            add_company(company, poc_title)
        if departures is None or not name or _too_short(name):
            continue
        try:
            gone = departures.lookup(name)
        except Exception:
            log.exception("[feeds] the departures list could not be asked about %r; "
                          "the person is left out", name)
            gone = True
        if gone:
            left.append(name)
            continue
        import gtm_sheet

        key = "person|%s|%s" % (gtm_sheet.normalise_header(name), name_key(company))
        warm = any(_filled(row.get(role)) for role in
                   ("li_connected_date", "li_dm_sent", "li_dm_date", "meeting_date"))
        paper = _filled(row.get("paper_links"))
        have = people.get(key)
        if have is not None:
            have["warm"] = have["warm"] or warm
            have["researcher"] = have["researcher"] or paper or campus
            continue
        people[key] = {
            "key": key, "kind": NAME_RESEARCHER if campus else NAME_PERSON,
            "name": name, "aliases": [], "company": company,
            "institution": company if campus else "",
            "researcher": bool(campus or paper), "warm": warm, "generic": False,
            "source": poc_title}
    if left:
        log.info("[feeds] name list: %d person/people on the departures list left "
                 "out: %s", len(left), ", ".join(left[:8]))

    for raw in (watch if watch is not None else (config.NEWS_WATCH_NAMES or [])):
        add_company(raw, "", NAME_WATCH)
    return list(companies.values()) + list(people.values())


def spellings(entry: dict) -> list:
    """Every way a name is written on the sheets, the first one first."""
    out = [clean_name(entry.get("name"))]
    out += [clean_name(a) for a in (entry.get("aliases") or [])]
    return [s for s in dict.fromkeys(out) if len(s) >= NAME_MIN_CHARS]


def batch_names(names: list) -> list:
    """The names that go in the batched searches: companies and watch names
    that are not generic. In a stable order, so a name is in the same batch
    from one cycle to the next."""
    picked = [e for e in names or [] if e.get("kind") in (NAME_COMPANY, NAME_WATCH)
              and not e.get("generic") and spellings(e)]
    return sorted(picked, key=lambda e: str(e.get("key") or ""))


def generic_names(names: list) -> list:
    return sorted([e for e in names or [] if e.get("kind") in (NAME_COMPANY, NAME_WATCH)
                   and e.get("generic") and spellings(e)],
                  key=lambda e: str(e.get("key") or ""))


def researchers(names: list) -> list:
    """Everybody in the researcher lane: a person at an institution, or a
    person at a company with a paper link."""
    return sorted([e for e in names or [] if e.get("researcher")
                   and e.get("kind") in (NAME_PERSON, NAME_RESEARCHER) and spellings(e)],
                  key=lambda e: str(e.get("key") or ""))


def warm_people(names: list) -> list:
    return sorted([e for e in names or [] if e.get("warm")
                   and e.get("kind") in (NAME_PERSON, NAME_RESEARCHER) and spellings(e)],
                  key=lambda e: str(e.get("key") or ""))


def batches(entries: list, size: int) -> list:
    """`entries` in groups of `size`: every entry in exactly one group."""
    size = max(1, int(size))
    return [entries[i:i + size] for i in range(0, len(entries), size)]


def batch_query(entries: list) -> str:
    """One search for a batch of names: `"Sarvam AI" OR "PolyAI" OR ...` —
    every spelling quoted, so each is matched as a phrase."""
    terms: list = []
    for e in entries:
        terms += [f'"{s}"' for s in spellings(e)]
    return " OR ".join(dict.fromkeys(terms))


def warm_query(entries: list) -> str:
    """The direct search for warm people: `("Daisy Leigh" "PolyAI") OR (...)` —
    the person and where the sheet says they are, both quoted. An article
    saying they have LEFT for somewhere else still names both."""
    terms: list = []
    for e in entries:
        who = spellings(e)[0]
        where = clean_name(e.get("company"))
        terms.append(f'("{who}" "{where}")' if where else f'"{who}"')
    return " OR ".join(terms)


# A warm pair is two phrases, so fewer of them fit one search than names do.
def warm_batch_size() -> int:
    return max(1, int(config.NEWS_NAME_BATCH_SIZE) // 3)


class NameIndex:
    """The name list, arranged for checking an item: `tag(item)` writes the
    names it carries and sets its flag. Built once a pull.

    THE FLOOD CAP (NEWS-PATCH-OCT16). The index counts, per company or watch
    name, the items of this pull it has flagged. Past `flood_max`
    (NEWS_NAME_FLOOD_MAX, 30) that name counts in the TITLE only for the rest
    of the pull, and it is listed in `noisy` — the pull reports it as
    "noisy_names" and the health line prints it. A short or common name on a
    sheet can no longer flag six hundred items an hour.
    """

    def __init__(self, names: Optional[list], *, flood_max: Optional[int] = None):
        self.names = list(names or [])
        self.flood_max = max(1, int(flood_max if flood_max is not None
                                    else getattr(config, "NEWS_NAME_FLOOD_MAX", 30)))
        self.hits: dict = {}                # company key -> items flagged this pull
        self.noisy: list = []               # names past the cap, in the order hit
        self._by_spelling: dict = {}        # lower spelling -> company entry
        self._generic: list = []
        self._people_at: dict = {}          # company key -> [person entry]
        for e in self.names:
            kind = e.get("kind")
            if kind in (NAME_COMPANY, NAME_WATCH):
                if e.get("generic"):
                    self._generic.append(e)
                else:
                    for s in spellings(e):
                        self._by_spelling.setdefault(s.lower(), e)
            elif kind in (NAME_PERSON, NAME_RESEARCHER) and e.get("company"):
                self._people_at.setdefault(name_key(e["company"]), []).append(e)
        # ONE PASS OVER THE TEXT for every company at once; the longest
        # spelling first, so "Scale AI Labs" is not read as "Scale AI".
        alts = sorted(self._by_spelling, key=len, reverse=True)
        self._re = re.compile(r"(?<!\w)(?:" + "|".join(re.escape(a) for a in alts)
                              + r")(?!\w)", re.I) if alts else None

    def entities(self, text: str) -> set:
        """Every company, watch or generic name of the list in `text` (lower
        case, whole phrases) — what `news.same_story` compares. Counts
        nothing: it is not a flag."""
        found: set = set()
        if self._re is not None:
            for m in self._re.finditer(str(text or "")):
                e = self._by_spelling.get(m.group(0).lower())
                if e is not None:
                    found.add(str(e["name"]).lower())
        for e in self._generic:
            if any(names_in(text, s) for s in spellings(e)):
                found.add(str(e["name"]).lower())
        return found

    def _people(self, company_entry: dict, text: str) -> list:
        key = str(company_entry.get("key") or "").split("|", 1)[-1]
        return [p for p in self._people_at.get(key, []) if names_it({"title": text}, p)]

    def tag(self, item: dict) -> dict:
        """Write `names`, `flag`, `kind` and `sheet_ref` on one item, in place.

        A company or watch name anywhere in the title or summary flags it, and
        so does one of that company's people beside it. A GENERIC name counts
        in the title only and is written on the item WITHOUT the flag, unless
        one of our people there is on the item too. Names already on the item
        (a researcher, a warm person) are kept, and so is a flag already set.
        """
        text = _text_of(item)
        title = str(item.get("title") or "")
        found: list = [n.strip() for n in str(item.get("names") or "").split(",")
                       if n.strip()]
        flag = 1 if int(item.get("flag") or 0) else 0
        first_company = None

        def add(name: str) -> None:
            name = clean_name(name).replace(",", " ")
            if name and name.lower() not in {f.lower() for f in found}:
                found.append(name)

        if self._re is not None:
            hit: dict = {}
            in_title = {m.group(0).lower() for m in self._re.finditer(title)}
            for m in self._re.finditer(text):
                e = self._by_spelling.get(m.group(0).lower())
                if e is not None:
                    hit.setdefault(e["key"], e)
            for e in list(hit.values()):
                count = self.hits.get(e["key"], 0)
                if count >= self.flood_max:
                    if e["name"] not in self.noisy:
                        self.noisy.append(e["name"])
                        log.info("[feeds] %r has named %d items this pull "
                                 "(NEWS_NAME_FLOOD_MAX) — title only from here on",
                                 e["name"], count)
                    if not any(s.lower() in in_title for s in spellings(e)):
                        hit.pop(e["key"])
                        continue
                self.hits[e["key"]] = count + 1
            for e in hit.values():
                add(e["name"])
                flag = 1
                first_company = first_company or e
                for p in self._people(e, text):
                    add(p["name"])
        for e in self._generic:
            if not any(names_in(title, s) for s in spellings(e)):
                continue
            add(e["name"])
            people = self._people(e, text)
            for p in people:
                add(p["name"])
            if people:
                flag = 1
                first_company = first_company or e
        item["names"] = ", ".join(found)
        item["flag"] = flag
        if flag and str(item.get("kind") or KIND_INDUSTRY) == KIND_INDUSTRY:
            item["kind"] = KIND_POC
        if first_company is not None and not item.get("sheet_ref"):
            item["sheet_ref"] = sheet_ref(first_company)
        return item


def sheet_ref(target: dict) -> str:
    """Who a stored story is about and where they are on the sheet: "Synthflow
    AI — on Master Pipeline". No post shows it any more; R10 (closure support)
    reads it to find a deal's news."""
    name = " ".join(str(target.get("name") or "").split())
    company = " ".join(str(target.get("company") or "").split())
    who = name if (str(target.get("kind") or "") in ("company", NAME_WATCH) or not company
                   or company.lower() == name.lower()) else f"{name}, {company}"
    source = " ".join(str(target.get("source") or "").split())
    return f"{who} — on {source}" if source else who


def researcher_names(item: dict, entries: list, *, topics: Optional[list] = None) -> list:
    """The researchers (of `entries`) this item is really about.

    NAMED, AND ANCHORED: the person's name is in the title or summary, AND so
    is an AI word from NEWS_TOPICS, the person's institution, or the person's
    company. The filter is here, in code — Google's OR and AND are not relied
    on, and a David Gamez who plays football is not ours.
    """
    text = _text_of(item)
    words = [" ".join(str(t or "").split()) for t in
             (topics if topics is not None else (config.NEWS_TOPICS or []))]
    ai_word = any(w and re.search(r"(?<!\w)" + re.escape(w) + r"(?!\w)", text, re.I)
                  for w in words)
    out: list = []
    for e in entries or []:
        if not names_it(item, e):
            continue
        if ai_word or institution_in(text, e.get("institution") or "") \
                or names_in(text, e.get("company") or ""):
            out.append(e["name"])
    return out


_BAD_FEEDS_LOGGED: set = set()


def feed_url(raw) -> str:
    """One NEWS_RSS_FEEDS entry as a url `requests` can fetch, or "".

    A PROTOCOL-RELATIVE ENTRY GETS ITS SCHEME. One feed was configured as
    "//news.google.com/rss/search?..." (the "https:" lost in a paste), and
    every poll raised InvalidSchema on it. "//host/path" can only mean
    https here, so it is read as that. Anything else with no http(s) scheme
    is NOT guessed at: it is left out and logged once, naming the entry.
    """
    url = str(raw or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        fixed = "https:" + url
        if url not in _BAD_FEEDS_LOGGED:
            _BAD_FEEDS_LOGGED.add(url)
            log.warning("[feeds] NEWS_RSS_FEEDS entry %r has no scheme; reading it as %r. "
                        "Put https: in front of it in .env.", url[:80], fixed[:80])
        return fixed
    if not url.lower().startswith(("http://", "https://")):
        if url not in _BAD_FEEDS_LOGGED:
            _BAD_FEEDS_LOGGED.add(url)
            log.error("[feeds] NEWS_RSS_FEEDS entry %r is not an http(s) url; skipped", url[:80])
        return ""
    return url


def outlet_urls() -> list:
    """NEWS_RSS_FEEDS as fetchable urls, each once."""
    out: list = []
    for url in config.NEWS_RSS_FEEDS or []:
        url = feed_url(url)
        if url and url not in out:
            out.append(url)
    return out


def plan(names: Optional[list], *, big: bool, hours: int) -> list:
    """THE GOOGLE NEWS REQUESTS OF ONE PULL, IN PRIORITY ORDER. Each is
    {"url", "query", "lane", "rank", "hint", "entries"}.

      names        the batched companies and watch names        } every pull
      generic      one search per generic name                  }
      warm         the warm people's direct search              — big pull only
      researchers  NEWS_NAME_BATCH_SIZE researchers a search    — big pull only
      topics       one query per NEWS_TOPICS entry              every pull

    With no name list (it has never been built) only the topics are read.
    """
    size = max(1, int(config.NEWS_NAME_BATCH_SIZE))
    jobs: list = []

    def job(query: str, lane: str, rank: int, hint: str = "", entries=None) -> None:
        jobs.append({"url": google_news_url(query, hours=hours), "query": query,
                     "lane": lane, "rank": rank, "hint": hint,
                     "entries": list(entries or [])})

    for group in batches(batch_names(names), size):
        job(batch_query(group), "names", RANK_NAMES, entries=group)
    for e in generic_names(names):
        job(batch_query([e]), "generic", RANK_NAMES, entries=[e])
    if big:
        for group in batches(warm_people(names), warm_batch_size()):
            job(warm_query(group), "warm", RANK_NAMES, entries=group)
        for group in batches(researchers(names), size):
            job(batch_query(group), "researchers", RANK_RESEARCHERS, entries=group)
    seen: set = set()
    for topic in config.NEWS_TOPICS or []:
        topic = str(topic or "").strip()
        if topic and topic.lower() not in seen:
            seen.add(topic.lower())
            job(topic_query(topic), "topics", RANK_TOPICS, hint=topic)
    return jobs


# -- the HTTP boundary --------------------------------------------------------


def _get(url: str) -> bytes:
    """One feed's bytes. The ONE socket in this module — a verify script
    replaces this function and nothing else. Raises what `requests` raises."""
    import requests

    resp = requests.get(
        url, timeout=max(1, int(config.NEWS_FEED_TIMEOUT_SECONDS)),
        headers={"User-Agent": f"{config.COS_NAME}/feed-reader (+rss)"},
    )
    resp.raise_for_status()
    return resp.content


# The clock the gap is measured on, and how it is waited out. Module names so
# a check can hand in a pretend clock and wait for nothing.
_monotonic = time.monotonic
_sleep = time.sleep
_last_google: list = [None]


def _google_get(url: str) -> bytes:
    """`_get` for a Google News url, NEWS_REQUEST_GAP_SECONDS after the last
    one. One at a time, by construction: the caller is a plain loop."""
    gap = max(0.0, float(config.NEWS_REQUEST_GAP_SECONDS))
    if _last_google[0] is not None:
        wait = gap - (_monotonic() - _last_google[0])
        if wait > 0:
            _sleep(wait)
    try:
        return _get(url)
    finally:
        _last_google[0] = _monotonic()


def is_rate_limit(error) -> bool:
    """Is this Google saying "too many requests" (HTTP 429)?"""
    status = getattr(getattr(error, "response", None), "status_code", None)
    return status == 429 or bool(re.search(r"(?<!\d)429(?!\d)", str(error)))


# -- reading one feed ---------------------------------------------------------

_TAG_RE = re.compile(r"(?s)<[^>]+>")
_ENTITIES = (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
             ("&#39;", "'"), ("&quot;", '"'), ("&#8217;", "’"), ("&#8230;", "…"))


def _plain(html: str) -> str:
    text = _TAG_RE.sub(" ", str(html or ""))
    for a, b in _ENTITIES:
        text = text.replace(a, b)
    return " ".join(text.split())


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _published(entry, now: datetime) -> datetime:
    for field in ("published_parsed", "updated_parsed", "created_parsed"):
        t = getattr(entry, field, None) or (entry.get(field) if hasattr(entry, "get") else None)
        if t:
            try:
                when = datetime(*t[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
            # A feed with a clock in the future is still not tomorrow's news.
            return min(when, now)
    return now


def parse(content, *, topic_hint: str = "", now: Optional[datetime] = None,
          target: Optional[dict] = None, source_rank: int = 9) -> list:
    """One feed's bytes -> [{url, url_key, headline_key, title, summary, source,
    published_at, topic_hint, seen_at, kind, sheet_ref, names, flag,
    source_rank}]. Never raises; [] for nonsense.

    Every item comes back as industry news with no names: `NameIndex.tag`
    decides what is about us. `target` is for a caller that asked about ONE
    name (R8 and R10's company news): its items are kept only when they NAME
    it (`names_it`), and carry it.

    A link-less or title-less entry is dropped, and so is a digest link. For a
    Google News entry the " - Outlet" tail comes off the title (it is the
    `source`), and a summary that only repeats the title is left empty.
    """
    import feedparser

    now = now or datetime.now(timezone.utc)
    try:
        parsed = feedparser.parse(content)
    except Exception:
        log.exception("[feeds] a feed could not be parsed")
        return []
    feed_title = _plain(getattr(parsed.feed, "title", "") or "")
    out: list = []
    for entry in parsed.entries or []:
        url = str(entry.get("link") or "").strip()
        title = _plain(entry.get("title") or "")
        if not url.lower().startswith(("http://", "https://")) or not title:
            continue
        why = news.digest_link_reason(url)
        if why:
            log.debug("[feeds] dropped %r — %s", title[:80], why)
            continue
        source = ""
        src = entry.get("source")
        if src:
            source = _plain(src.get("title") or "") if hasattr(src, "get") else ""
        if source and title.endswith(" - " + source):
            title = title[: -len(" - " + source)].rstrip()
        if not source:
            source = feed_title
        summary = _plain(entry.get("summary") or entry.get("description") or "")
        if summary.lower().startswith(title.lower()[:60]):
            summary = ""
        item = {
            "url": url, "url_key": news.url_key(url),
            "headline_key": news.headline_key(title),
            "story_key": news.story_key(title),
            "title": _clip(title, 300),
            "summary": _clip(summary, SUMMARY_MAX_CHARS),
            "source": _clip(source, 120),
            "published_at": utc_iso(_published(entry, now)),
            "topic_hint": topic_hint,
            "seen_at": utc_iso(now),
            "kind": KIND_INDUSTRY, "sheet_ref": "",
            "names": "", "flag": 0, "source_rank": int(source_rank),
        }
        if target is not None:
            if not names_it(item, target):
                continue
            item["kind"] = KIND_POC
            item["sheet_ref"] = sheet_ref(target)
            item["names"] = clean_name(target.get("name")).replace(",", " ")
            item["flag"] = 1
        out.append(item)
    return out


# -- the pull -----------------------------------------------------------------


def _keep(job: dict, item: dict, index: NameIndex) -> bool:
    """Tag one item from one Google request and say whether it is kept.

    A TOPIC'S items are all kept. An item from a search FOR A NAME is kept
    only when a name is on it once it has been checked: Google matched the
    name somewhere in the article, and a story that does not name us in its
    title or summary is not news about us — nor is it news on any topic we
    asked for.
    """
    lane = job["lane"]
    if lane == "researchers":
        who = researcher_names(item, job["entries"])
        if not who:
            return False
        item["names"] = ", ".join(n.replace(",", " ") for n in who)
        item["flag"] = 1
        item["kind"] = KIND_RESEARCHER
    elif lane == "warm":
        who = [e["name"] for e in job["entries"] if names_it(item, e)]
        if who:
            item["names"] = ", ".join(n.replace(",", " ") for n in who)
            item["flag"] = 1
            item["kind"] = KIND_POC
    index.tag(item)
    return lane == "topics" or bool(item.get("names"))


def pull(*, now: Optional[datetime] = None, big: bool = False, hours: int = 2,
         names: Optional[list] = None, max_seconds: Optional[float] = None) -> dict:
    """ONE PULL: read every source in priority order, flag, store what is new.
    Zero API calls.

    `big` is the day's first pull: 24 hours, plus the once-a-day lanes (warm
    people, researchers, arXiv). `hours` is how far back the Google queries
    reach (`when_param`). `names` is the name list; None reads `news_names`.

    Returns {"feeds", "ok", "fetched", "new", "flagged", "errors", "partial",
    "google_requests", "names", "big", "hours", "arxiv", "merged",
    "noisy_names", "timed_out"}. `fetched` is every item the sources carried
    inside the keep window; `new` is how many of them the store had not seen —
    so a second pull straight after the first reports new=0. `merged` is how
    many were another outlet's copy of a stored story (`db.news_feed_add`).
    `partial` is True when Google answered 429 and the pull stopped: what had
    been read is stored and nothing is retried.

    THE TIME CAP (NEWS-PATCH-OCT16). With `max_seconds` (the caller passes
    NEWS_CYCLE_MAX_SECONDS) the pull stops at the next SOURCE BOUNDARY once
    that much time has gone — between two Google requests, before the
    outlets, before arXiv — stores what it has, and says `partial` and
    `timed_out`. The drip is waiting behind it.

    `noisy_names` are the names that hit the flood cap this pull
    (NEWS_NAME_FLOOD_MAX): they counted in the title only from then on.
    """
    now = now or datetime.now(timezone.utc)
    result = {"feeds": 0, "ok": 0, "fetched": 0, "new": 0, "flagged": 0, "errors": [],
              "partial": False, "google_requests": 0, "names": 0, "big": bool(big),
              "hours": int(hours), "arxiv": 0, "merged": 0, "noisy_names": [],
              "timed_out": False}
    started = _monotonic()

    def out_of_time(where: str) -> bool:
        if max_seconds is None or result["timed_out"]:
            return result["timed_out"]
        if _monotonic() - started < float(max_seconds):
            return False
        result["timed_out"] = result["partial"] = True
        result["errors"].append(f"stopped after {int(max_seconds)}s, before {where}")
        log.warning("[feeds] the pull has run %ds (NEWS_CYCLE_MAX_SECONDS=%d); it stops "
                    "before %s, stores what it has, and the cycle is partial",
                    int(_monotonic() - started), int(max_seconds), where)
        return True
    try:
        import feedparser  # noqa: F401
    except ImportError:
        result["errors"].append("feedparser is not installed (pip install feedparser)")
        log.error("[feeds] feedparser is not installed; no feed can be read")
        return result

    db = _db()
    if names is None:
        try:
            names = db.news_names_all() if db is not None else []
        except Exception:
            log.exception("[feeds] could not read the name list; topics and outlets only")
            names = []
    result["names"] = len(names)
    index = NameIndex(names)
    jobs = plan(names, big=big, hours=hours)
    outlets = outlet_urls()
    result["feeds"] = len(jobs) + len(outlets)
    keep_from = utc_iso(now - timedelta(days=max(1, int(config.NEWS_FEED_KEEP_DAYS))))
    items: list = []

    # ---- 1-3: Google News, one request at a time ----------------------------
    for job in jobs:
        if out_of_time(f"Google request {result['google_requests'] + 1} of {len(jobs)}"):
            break
        result["google_requests"] += 1
        try:
            content = _google_get(job["url"])
        except Exception as e:
            if is_rate_limit(e):
                result["partial"] = True
                result["errors"].append(f"{job['url']} — HTTP 429")
                log.warning("[feeds] Google News answered 429 on request %d of %d (%s); "
                            "the pull stops here, what was read is stored, and nothing "
                            "is retried before the next cycle",
                            result["google_requests"], len(jobs), job["lane"])
                break
            result["errors"].append(f"{job['url']} — {type(e).__name__}: {str(e)[:120]}")
            log.info("[feeds] could not read %s: %s: %s", job["url"], type(e).__name__,
                     str(e)[:120])
            continue
        result["ok"] += 1
        for item in parse(content, topic_hint=job["hint"], now=now,
                          source_rank=job["rank"]):
            if item["published_at"] >= keep_from and _keep(job, item, index):
                items.append(item)

    # ---- 4: the outlets' own feeds ------------------------------------------
    if not result["partial"] and outlets and not out_of_time("the outlets' feeds"):
        def one(url):
            try:
                return url, _get(url), ""
            except Exception as e:
                return url, b"", f"{type(e).__name__}: {str(e)[:120]}"

        with ThreadPoolExecutor(max_workers=6) as pool:
            fetched = list(pool.map(one, outlets))
        for url, content, error in fetched:
            if error:
                result["errors"].append(f"{url} — {error}")
                log.info("[feeds] could not read %s: %s", url, error)
                continue
            result["ok"] += 1
            for item in parse(content, now=now, source_rank=RANK_OUTLETS):
                if item["published_at"] >= keep_from:
                    items.append(index.tag(item))

    # ---- 5: arXiv, once a day -----------------------------------------------
    authors = researchers(names)
    if big and not result["partial"] and config.ARXIV_ENABLED and authors             and not out_of_time("arXiv"):
        try:
            import arxiv

            papers = arxiv.fetch(authors, now=now)
        except Exception:
            log.exception("[feeds] arXiv could not be read; skipped until tomorrow")
            papers = []
        for item in papers:
            if item["published_at"] >= keep_from:
                items.append(index.tag(item))
        result["arxiv"] = len(papers)

    result["fetched"] = len(items)
    result["flagged"] = sum(1 for i in items if i.get("flag"))
    result["noisy_names"] = list(index.noisy)
    if db is not None:
        try:
            stats: dict = {}
            result["new"] = db.news_feed_add(
                items, entities_of=lambda t: index.entities(t) | news.model_entities(t),
                stats=stats)
            result["merged"] = int(stats.get("merged") or 0)
            db.news_feed_prune(keep_from)
        except Exception:
            log.exception("[feeds] could not store the pull")
            result["errors"].append("the feed store could not be written")
    log.info("[feeds] %s pull (%s): %d Google request(s) for %d name(s) and %d "
             "topic(s), %d outlet feed(s), %d arXiv paper(s) — %d item(s), %d naming "
             "us, %d new, %d copies merged%s%s%s — 0 API calls", "big" if big else "hourly",
             when_param(hours), result["google_requests"], len(names),
             sum(1 for j in jobs if j["lane"] == "topics"), len(outlets),
             result["arxiv"], result["fetched"], result["flagged"], result["new"],
             result["merged"],
             f", {len(result['errors'])} error(s)" if result["errors"] else "",
             f", noisy names: {', '.join(result['noisy_names'])}"
             if result["noisy_names"] else "",
             (" — PARTIAL (time cap)" if result["timed_out"] else " — PARTIAL (429)")
             if result["partial"] else "")
    return result


def _self_test() -> int:
    """`python -m feeds` — offline: the HTTP boundary is stubbed."""
    import os
    import shutil
    import tempfile

    logging.basicConfig(level=logging.ERROR, format="%(levelname)-7s %(message)s")
    failures = 0

    def check(name, got, want):
        nonlocal failures
        ok = got == want
        failures += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")

    now = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
    outlet = b"""<?xml version="1.0"?><rss version="2.0"><channel>
<title>TechCrunch AI</title>
<item><title>Lab ships an open eval suite</title>
<link>https://techcrunch.com/2026/10/01/evals?utm_source=rss</link>
<description>&lt;p&gt;A public benchmark for tool-using agents, with PolyAI.&lt;/p&gt;</description>
<pubDate>Thu, 01 Oct 2026 06:30:00 GMT</pubDate></item>
<item><title>The weekly AI newsletter</title>
<link>https://techcrunch.com/newsletter/week-40</link>
<pubDate>Thu, 01 Oct 2026 05:00:00 GMT</pubDate></item>
<item><title>No link here</title></item>
</channel></rss>"""
    google = b"""<?xml version="1.0"?><rss version="2.0"><channel>
<title>"evals" when:2h - Google News</title>
<item><title>Lab Ships an Open Eval Suite - Reuters</title>
<link>https://news.google.com/rss/articles/CBMiabc?oc=5</link>
<description>&lt;a href="x"&gt;Lab Ships an Open Eval Suite&lt;/a&gt;&amp;nbsp;Reuters</description>
<pubDate>Thu, 01 Oct 2026 07:00:00 GMT</pubDate>
<source url="https://www.reuters.com">Reuters</source></item>
<item><title>Nebius raises $700m for inference - Bloomberg</title>
<link>https://news.google.com/rss/articles/CBMidef?oc=5</link>
<pubDate>Fri, 02 Oct 2026 09:00:00 GMT</pubDate>
<source url="https://www.bloomberg.com">Bloomberg</source></item>
</channel></rss>"""

    print("parsing an outlet's feed")
    got = parse(outlet, now=now)
    check("one story — the newsletter and the linkless one are dropped", len(got), 1)
    check("the summary is plain text", got[0]["summary"],
          "A public benchmark for tool-using agents, with PolyAI.")
    check("the url key drops tracking", got[0]["url_key"],
          "techcrunch.com/2026/10/01/evals")
    check("the source is the feed", got[0]["source"], "TechCrunch AI")
    check("published is UTC ISO", got[0]["published_at"], "2026-10-01T06:30:00+00:00")
    check("no name and no flag until it is checked", (got[0]["names"], got[0]["flag"]),
          ("", 0))

    print("\nparsing a Google News query")
    g = parse(google, topic_hint="evals", now=now, source_rank=RANK_TOPICS)
    check("two stories", len(g), 2)
    check("the outlet comes off the title", g[0]["title"], "Lab Ships an Open Eval Suite")
    check("...and is the source", g[0]["source"], "Reuters")
    check("a summary that repeats the title is empty", g[0]["summary"], "")
    check("the topic it came from is kept", g[0]["topic_hint"], "evals")
    check("...and where it ranks", g[0]["source_rank"], RANK_TOPICS)
    check("a date in the future is clamped to now", g[1]["published_at"], utc_iso(now))
    check("two outlets, one headline key",
          g[0]["headline_key"], got[0]["headline_key"])

    print("\nthe name list")

    class Gone:
        def lookup(self, name):
            return {"name": name} if name == "Left Person" else None

    pipeline = [{"company": "PolyAI"}, {"company": "CoRover.ai"}, {"company": "Google"},
                {"company": "AI"}, {"company": 'Acme "Labs" Inc.'}]
    pocs = [{"company": "PolyAI", "name": "Daisy Leigh", "meeting_date": "12-Oct-2026"},
            {"company": "PolyAI", "name": "Daisy Leigh"},
            {"company": "CoRover", "name": "Left Person"},
            {"company": "Middlesex University London", "name": "David Gamez"},
            {"company": "AI Security Institute (UK Govt.)", "name": "Kim Roe"},
            {"company": "Google", "name": "Jo Googler", "paper_links": "https://arxiv.org/x"}]
    names = build_names(pipeline, pocs, departures=Gone(), watch=["Scale AI", "polyai"],
                        generic=["Google"],
                        institution_words=["university", "institute of", "IIT"])
    by = {e["name"]: e for e in names}
    check("a name under three characters is left out", "AI" in by, False)
    check("two spellings are one name, the other an alias",
          (by["CoRover.ai"]["aliases"], "CoRover" in by), (["CoRover"], False))
    check("quotes come out of a name; the trailing dot stays",
          "Acme Labs Inc." in by, True)
    check("a person on two rows is one entry, and warm if either row says so",
          (sum(1 for e in names if e["name"] == "Daisy Leigh"), by["Daisy Leigh"]["warm"]),
          (1, True))
    check("a person on the departures list is not listed; the company is",
          ("Left Person" in by, by["CoRover.ai"]["kind"]), (False, "company"))
    check("a university is a researcher's filter word, never a company",
          (by["David Gamez"]["kind"], by["David Gamez"]["institution"],
           "Middlesex University London" in by),
          ("researcher", "Middlesex University London", False))
    check("'AI Security Institute (UK Govt.)' stays a company",
          (by["AI Security Institute (UK Govt.)"]["kind"], by["Kim Roe"]["kind"]),
          ("company", "person"))
    check("a paper link at a company puts the person in both lanes",
          (by["Jo Googler"]["kind"], by["Jo Googler"]["researcher"]), ("person", True))
    check("a watch name is listed; one already on a sheet keeps the sheet's entry",
          (by["Scale AI"]["kind"], by["PolyAI"]["kind"]), ("watch", "company"))
    check("the departures list unreadable: companies only",
          sorted({e["kind"] for e in build_names(pipeline, pocs, departures=None,
                                                 watch=[], generic=[])}), ["company"])

    print("\nthe searches")
    saved = (config.NEWS_RSS_FEEDS, config.NEWS_TOPICS, config.NEWS_NAME_BATCH_SIZE,
             config.NEWS_REQUEST_GAP_SECONDS, config.ARXIV_ENABLED)
    config.NEWS_RSS_FEEDS, config.NEWS_TOPICS = ["https://a.com/feed"], ["evals",
                                                                         "voice agent"]
    config.NEWS_NAME_BATCH_SIZE, config.NEWS_REQUEST_GAP_SECONDS = 15, 1.5
    config.ARXIV_ENABLED = False
    check("a protocol-relative feed gets https, and a schemeless one is left out",
          (feed_url("//news.google.com/rss/search?q=x"), feed_url("news.google.com/rss"),
           feed_url(" https://a.com/feed ")),
          ("https://news.google.com/rss/search?q=x", "", "https://a.com/feed"))
    check("the big pull asks for a day, an hourly one for two hours",
          (when_param(24), when_param(2), when_param(3)), ("when:1d", "when:2h", "when:3h"))
    many = build_names([{"company": f"Company {i:03d}"} for i in range(300)], [],
                       departures=None, watch=[], generic=[])
    groups = batches(batch_names(many), 15)
    check("300 names, 15 a search: 20 searches, every name in exactly one",
          (len(groups), sorted(e["name"] for g in groups for e in g)
           == sorted(e["name"] for e in many)), (20, True))
    small = plan(names, big=False, hours=2)
    check("an hourly pull: the names, the generic name, then the topics",
          [j["lane"] for j in small], ["names", "generic", "topics", "topics"])
    check("the batch is one OR query, every spelling quoted",
          small[0]["query"],
          '"Acme Labs Inc." OR "AI Security Institute (UK Govt.)" OR "CoRover.ai" OR '
          '"CoRover" OR "PolyAI" OR "Scale AI"')
    check("a generic name has a search of its own", small[1]["query"], '"Google"')
    check("a multi-word topic is quoted, in India's edition, with the window",
          small[3]["url"], "https://news.google.com/rss/search?q=%22voice+agent%22+when%3A2h"
                           "&hl=en-IN&gl=IN&ceid=IN:en")
    bigger = plan(names, big=True, hours=24)
    check("the big pull adds the warm people and the researchers, in that order",
          [j["lane"] for j in bigger],
          ["names", "generic", "warm", "researchers", "topics", "topics"])
    check("a warm person is searched with their company",
          bigger[2]["query"], '("Daisy Leigh" "PolyAI")')
    check("...and every big-pull query asks for a day",
          all("when%3A1d" in j["url"] for j in bigger), True)

    print("\nthe flag, by rule")
    index = NameIndex(names)

    def tagged(title, summary=""):
        return index.tag({"title": title, "summary": summary, "kind": KIND_INDUSTRY})

    check("a company in the title flags the item",
          (tagged("PolyAI raises $50M")["flag"], tagged("PolyAI raises $50M")["names"]),
          (1, "PolyAI"))
    check("...and its person beside it is named too",
          tagged("PolyAI names Daisy Leigh as CRO")["names"], "PolyAI, Daisy Leigh")
    check("either spelling carries the one name",
          (tagged("CoRover launches a bot")["names"], tagged("CoRover.ai launches")["names"]),
          ("CoRover.ai", "CoRover.ai"))
    check("'Pulse' inside 'Pulsed' is not a match",
          NameIndex([{"key": "company|pulse", "kind": "company", "name": "Pulse"}])
          .tag({"title": "Markets pulsed higher", "summary": ""})["flag"], 0)
    check("a generic name in the summary only: no name, no flag",
          (tagged("Chip stocks fall", "Google and others slid")["names"],
           tagged("Chip stocks fall", "Google and others slid")["flag"]), ("", 0))
    check("a generic name in the title: named, not flagged",
          (tagged("Google ships Gemini 4")["names"], tagged("Google ships Gemini 4")["flag"]),
          ("Google", 0))
    check("...flagged when one of our people there is on it",
          (tagged("Google's Jo Googler on evals")["names"],
           tagged("Google's Jo Googler on evals")["flag"]), ("Google, Jo Googler", 1))
    check("the stored row says which sheet it is on, for R10",
          tagged("PolyAI raises $50M")["sheet_ref"], "PolyAI — on Master Pipeline and "
                                                    "Outreach PoCs")
    gamez = [by["David Gamez"]]
    check("a researcher's item with no AI word, institution or company is dropped",
          researcher_names({"title": "David Gamez scores twice", "summary": "A fine win."},
                           gamez, topics=["evals"]), [])
    check("...kept when the summary names the institution, even shortened",
          researcher_names({"title": "David Gamez on consciousness",
                            "summary": "The Middlesex University lecturer said."},
                           gamez, topics=["evals"]), ["David Gamez"])
    check("...or an AI word from the topics",
          researcher_names({"title": "David Gamez: evals are broken", "summary": ""},
                           gamez, topics=["evals"]), ["David Gamez"])

    print("\nthe pull")
    import db as dbmod
    global _get, _sleep, _monotonic
    real = (_get, _sleep, _monotonic)
    tmp = tempfile.mkdtemp(prefix="saley-feeds-")
    store = dbmod.DB(os.path.join(tmp, "t.db"))
    served: list = []
    clock = [100.0]
    stamps: list = []

    def fake_get(url):
        served.append(url)
        if "news.google.com" in url:
            stamps.append(clock[0])
        if "voice" in url:
            raise TimeoutError("slow")
        return outlet if "a.com" in url else google

    def fake_sleep(seconds):
        clock[0] += seconds

    preferred = config.NEWS_PREFERRED_DOMAINS
    try:
        _get, _sleep, _monotonic = fake_get, fake_sleep, lambda: clock[0]
        _last_google[0] = None
        bind(lambda: store)
        # No preferred outlet here: the first copy keeps its link. (A copy from
        # NEWS_PREFERRED_DOMAINS takes the link over — verify_news_patch T3.)
        config.NEWS_PREFERRED_DOMAINS = []
        first = pull(now=now, big=False, hours=2, names=[by["PolyAI"]])
        check("four requests: the names, two topics, the outlet",
              (first["feeds"], len(served), first["google_requests"]), (4, 4, 3))
        check("Google is asked first, the outlet last",
              ["news.google.com" in u for u in served], [True, True, True, False])
        check("at least 1.5 seconds between two Google requests",
              min(b - a for a, b in zip(stamps, stamps[1:])) >= 1.5, True)
        check("three read, one failed", (first["ok"], len(first["errors"])), (3, 1))
        check("two stored — the outlet's copy is Google's story", first["new"], 2)
        rows = store.news_feed_between("2026-09-01", "2026-12-01")
        evals = [r for r in rows if r["headline_key"] == got[0]["headline_key"]][0]
        check("Google's copy won: its link and its rank",
              (evals["url"].startswith("https://news.google.com/"), evals["source_rank"]),
              (True, RANK_TOPICS))
        check("...and the outlet's copy, which names us, flagged the row",
              (evals["flag"], evals["names"]), (1, "PolyAI"))
        check("...and it counts as a second outlet (NEWS-PATCH-OCT16)",
              (evals["outlet_count"], first["merged"]), (2, 1))
        second = pull(now=now, big=False, hours=2, names=[by["PolyAI"]])
        check("a second pull stores 0 new", second["new"], 0)
        check("the store holds two", store.news_feed_count(), 2)

        def limited(url):
            served.append(url)
            if len(served) == 2:
                raise RuntimeError("429 Client Error: Too Many Requests")
            return google

        served.clear()
        _get = limited
        cut = pull(now=now, big=False, hours=2, names=[by["PolyAI"]])
        check("a 429 stops the pull at once: no later request, the cycle is partial",
              (len(served), cut["partial"]), (2, True))
        store.news_feed_set_scores(
            [{"url_key": evals["url_key"], "importance": 4, "topic": "evals",
              "what": "a public benchmark"}], scored_at=utc_iso(now))
        check("a score is written back",
              [r["importance"] for r in store.news_feed_get([evals["url_key"]])], [4])
    finally:
        _get, _sleep, _monotonic = real
        _last_google[0] = None
        config.NEWS_PREFERRED_DOMAINS = preferred
        bind(None)
        (config.NEWS_RSS_FEEDS, config.NEWS_TOPICS, config.NEWS_NAME_BATCH_SIZE,
         config.NEWS_REQUEST_GAP_SECONDS, config.ARXIV_ENABLED) = saved
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
