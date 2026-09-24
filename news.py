"""R1 AND R2 — our people first, then the wider field.

WHAT CHANGED AND WHY. R1 used to be a single placeholder item that said "AI
news: funding, hires, papers by our PoCs…" and waited for a research layer to
expand it. The research layer arrived and searched for "AI industry news from
the last 24 hours", which is a reading list. The rule on the Bot Rules tab is
not about AI news in general — it is about *our* people: a funding round at a
company we are talking to, a paper by a PoC, somebody we have been chasing
changing jobs. Those are things the team can act on this week. A bigger story
about a stranger is not.

So R1 searches for OUR PEOPLE FIRST:

    1. the PoCs on ACTIVE Outreach PoCs rows
    2. the T1 and T2 people in the researcher/buyer mapping
    3. the companies in the Master Pipeline

THE ROTATION IS THE WHOLE TRICK. There are several hundred of those and one
day's search budget carries eight, so whose turn it is has to be remembered.
`news_targets` in SQLite keeps a last-searched date per person and company and
the least-recently-searched come up first — everybody comes round, and a PoC
added this morning (last_searched = '', which sorts first) is picked up on the
next run rather than after everyone else has had a turn.

NOBODY ON THE DEPARTURES LIST IS EVER SEARCHED. The mapping sheet flags people
who have left, and news about where somebody used to work is worse than no news:
it reads as a live contact.

THE FALLBACK IS NOT A FAILURE. Most days nothing at all is written about eight
particular mid-market AI people, and a rule that went silent on those days would
look broken. When the people pass finds nothing, R1 searches the wider field for
the day and SAYS SO in plain words — "nothing on our contacts today, so here is
what moved in AI". Which mode a post is in is never left for the reader to infer.

PREFERRED SITES ARE A PREFERENCE. `NEWS_PREFERRED_DOMAINS` restricts the FIRST
search; if that comes back thin (< NEWS_MIN_ITEMS) a SECOND open search runs
across the web. Getting the domain list wrong must cost relevance, never the
day's news — so it can never be the reason nothing was found.

R2 IS THE SAME SEARCH, READ DIFFERENTLY. The news-company screen wants companies
that turned up in today's news and are NOT in the Master Pipeline. That is the
result set R1 already paid for, filtered — so R2 costs no extra searches on a
day R1 has run, and asks before anything is added to a sheet. It never writes.

NOTHING HERE OPENS A SOCKET. This module selects, builds prompts and parses; the
one search call is `llm.web_research`, made by the caller in bot.py, which also
banks the budget. Keeping the I/O out means the selection and the parsing can be
tested without a network or an API key — see `_self_test`.
"""
import logging
import re
from datetime import date, timedelta
from typing import Optional

import config
import deadlines as dl
import gtm_sheet

log = logging.getLogger(__name__)

# The three tiers, in the order the rules sheet puts them. The selector fills
# its quota from the first tier that still has somebody due before moving on,
# so a day never spends all eight searches on pipeline companies while a PoC
# waits.
KIND_POC = "poc"
KIND_RESEARCHER = "researcher"
KIND_COMPANY = "company"
KIND_ORDER = (KIND_POC, KIND_RESEARCHER, KIND_COMPANY)

# Which mapping tiers are worth a search. T3 is "possible fit, thin evidence" —
# searching those would spend the rotation on people we are not yet pursuing.
MAPPING_TIERS = ("T1", "T2")

# The two modes a post can be in, named so the message can say which.
MODE_PEOPLE = "people"
MODE_FIELD = "field"

# What counts as news about one of our people. Straight off the Bot Rules tab —
# and it is a list of SEVEN THINGS rather than "news", because "any mention"
# returns a directory listing and a conference programme from 2019.
NEWS_CATEGORIES = (
    "a funding round (raised, Series A/B/C, seed, valuation)",
    "an AI/ML hire or a leadership hire (joined, appointed, named as, promoted to)",
    "a paper they published or co-authored",
    "them speaking at, keynoting or appearing at an event",
    "them changing company (joined, left, departed, moving to)",
    "a job post for evaluations, annotation, data labelling or model training",
    "competitor news — another company doing what we do, for anyone",
)


def _norm(text: str) -> str:
    """The matching form of a name. Case, punctuation and spacing collapsed."""
    return " ".join(re.sub(r"[^\w\s]", " ", str(text or "")).split()).strip().lower()


def target_key(kind: str, name: str) -> str:
    return f"{kind}|{_norm(name)}"


# -- normalising a story's URL ------------------------------------------------

_TRACKING = re.compile(r"[?&](utm_[^=]+|ref|ref_src|fbclid|gclid|mc_cid|mc_eid)=[^&]*",
                       re.IGNORECASE)


def url_key(url: str) -> str:
    """The identity of a story, for the no-repeats check.

    THE SAME STORY ARRIVES WEARING DIFFERENT CLOTHES: with and without "www.",
    http and https, with a tracking query bolted on by whoever linked it, with
    and without a trailing slash. Those are one story and must collapse to one
    key, or the dedup silently does nothing and the same funding round is posted
    on Monday, Tuesday and Wednesday.

    The TITLE is deliberately not part of the key — four outlets give the same
    round four headlines, and an outlet re-running its own piece changes its
    own. The URL is the only stable identity a story has.
    """
    raw = str(url or "").strip()
    if not raw:
        return ""
    raw = _TRACKING.sub("", raw)
    raw = re.sub(r"^https?://", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"^www\.", "", raw, flags=re.IGNORECASE)
    raw = raw.split("#", 1)[0].rstrip("/?&")
    return raw.lower()


# -- who to search for --------------------------------------------------------


def collect_targets(*, poc_rows: list, mapping_rows: list,
                    pipeline_companies: list, departed: set) -> list:
    """Everybody R1 could search for, as rows for `db.sync_news_targets`.

    THE CALLER HAS ALREADY SPLIT ACTIVE FROM INACTIVE. Only active Outreach PoCs
    rows arrive here: a row somebody stopped is a row we are not pursuing, and
    news about them is not something anybody is going to act on.

    DEPARTURES ARE DROPPED HERE AND RETURNED SEPARATELY, so the caller can also
    delete anyone already in the rotation who has since left. Both halves
    matter: not adding them is not enough once they are in.
    """
    out: list = []
    seen: set = set()
    dropped: list = []

    def _add(kind: str, name: str, company: str = "", source: str = "",
             sheet_row=None) -> None:
        clean = gtm_sheet.clean_cell(name)
        if not clean or len(clean) < 3:
            return
        key = target_key(kind, clean)
        if key in seen:
            return
        seen.add(key)
        if _norm(clean) in departed:
            dropped.append(key)
            return
        out.append({
            "target_key": key, "kind": kind, "name": clean,
            "company": gtm_sheet.clean_cell(company), "source": source,
            "sheet_row": sheet_row,
        })

    for row in poc_rows or ():
        _add(KIND_POC, row.get("name") or row.get("poc"),
             company=row.get("company"), source="Outreach PoCs",
             sheet_row=row.get("_row"))

    for row in mapping_rows or ():
        tier = str(row.get("tier") or "").strip().upper()
        if tier not in MAPPING_TIERS:
            continue
        if row.get("do_not_recommend"):
            dropped.append(target_key(KIND_RESEARCHER,
                                      gtm_sheet.clean_cell(row.get("researcher")
                                                           or row.get("name"))))
            continue
        _add(KIND_RESEARCHER, row.get("researcher") or row.get("name"),
             company=row.get("org") or row.get("company"),
             source=f"researcher mapping ({tier})", sheet_row=row.get("_row"))

    for name in pipeline_companies or ():
        _add(KIND_COMPANY, name, company=name, source="Master Pipeline")

    return out, [k for k in dropped if k]


def select_for_run(db, *, limit: int) -> list:
    """The targets this run searches for, in tier order then oldest-first.

    TIER ORDER FIRST, ROTATION WITHIN IT. Filling the quota from PoCs before
    researchers before companies is the rules sheet's own priority; the
    least-recently-searched rule then decides which PoCs. Doing it the other way
    round — pure oldest-first across everything — would let four hundred
    pipeline companies crowd out the people we are actually talking to.
    """
    want = max(1, int(limit))
    picked: list = []
    for kind in KIND_ORDER:
        if len(picked) >= want:
            break
        try:
            rows = db.news_targets_due(limit=want - len(picked), kinds=(kind,))
        except Exception:
            log.exception("[news] could not read the rotation for %s", kind)
            rows = []
        picked.extend(rows)
    return picked[:want]


# -- building the search ------------------------------------------------------


def _keyword_seeds() -> list:
    return [k for k in (config.NEWS_KEYWORDS or []) if str(k).strip()]


def people_prompt(targets: list, *, today: date, keywords: Optional[list] = None,
                  domains: Optional[list] = None) -> str:
    """The search for OUR people. One call, every target named.

    NAMED INDIVIDUALLY rather than as "our contacts", because a search engine
    cannot resolve "our contacts" and a model asked to search for them will
    invent a plausible set. The names are the query.
    """
    seeds = keywords if keywords is not None else _keyword_seeds()
    who = []
    for t in targets:
        label = str(t.get("name") or "").strip()
        org = str(t.get("company") or "").strip()
        if org and org.lower() != label.lower():
            label += f" ({org})"
        who.append(label)

    lines = [
        "Search the news for ANY of these specific people and companies. They are "
        "contacts and prospects of the team you work for, listed from their own "
        "sheets — search for each one BY NAME:",
        "",
    ]
    lines += [f"  - {w}" for w in who]
    lines += [
        "",
        "WHAT COUNTS AS A STORY, and nothing else does:",
    ]
    lines += [f"  - {c}" for c in NEWS_CATEGORIES]
    lines += [
        "",
        f"RECENCY: prefer the last 7 days. Nothing older than 30 days "
        f"(today is {dl.iso(today)}).",
    ]
    if seeds:
        lines += [
            "",
            "These terms are what the team's own rules sheet lists as relevant. They "
            "are SEEDS AND NOT LIMITS — the sheet says 'not limited to these', so "
            "closely adjacent terms are fair game:",
            "  " + ", ".join(seeds),
        ]
    if domains:
        lines += [
            "",
            "Restrict this search to these sites: " + ", ".join(domains),
        ]
    lines += [
        "",
        "FOR EACH STORY, one line in exactly this shape and nothing else:",
        "  STORY | <who or which company it is about> | <what happened, one clause> | <url>",
        "",
        "The first field MUST be the name from the list above that the story is "
        "about, spelled as it is above. If a story is not about one of them, leave "
        "it out — a story about the wider industry is a different search.",
        "EVERY LINE NEEDS A REAL URL you actually found. No url, no line.",
        "If you found nothing about any of them, reply with exactly: NOTHING FOUND",
    ]
    return "\n".join(lines)


def field_prompt(*, today: date, keywords: Optional[list] = None,
                 domains: Optional[list] = None) -> str:
    """The fallback: the wider AI field for the day.

    ONLY REACHED WHEN THE PEOPLE PASS FOUND NOTHING, and the message says so.
    This is a sales team's brief, not a research digest — what moved, who has
    money, and what a regulator did.
    """
    seeds = keywords if keywords is not None else _keyword_seeds()
    lines = [
        "Search for the most significant AI news of the last 48 hours "
        f"(today is {dl.iso(today)}). This is a briefing for a SALES team at an "
        "AI-data company, so rank by what would change a sales conversation:",
        "",
        "  - major model or product releases",
        "  - funding rounds and acquisitions",
        "  - AI regulation, compliance and safety rulings",
        "  - anything about evaluations, annotation, human data or model training",
        "  - notable leadership moves at AI companies",
    ]
    if seeds:
        lines += [
            "",
            "Relevant terms from the team's own rules sheet — SEEDS, NOT LIMITS "
            "('not limited to these'), so adjacent terms are fair game:",
            "  " + ", ".join(seeds),
        ]
    if domains:
        lines += ["", "Restrict this search to these sites: " + ", ".join(domains)]
    lines += [
        "",
        "FOR EACH STORY, one line in exactly this shape and nothing else:",
        "  STORY | <the company or person it is about> | <what happened, one clause> | <url>",
        "",
        "EVERY LINE NEEDS A REAL URL you actually found. No url, no line.",
        "If you genuinely found nothing, reply with exactly: NOTHING FOUND",
    ]
    return "\n".join(lines)


def screen_prompt(stories: list, known_companies: list) -> str:
    """R2 — which of today's news companies are NOT in the pipeline.

    READS THE STORIES R1 ALREADY PAID FOR. No second search: the question is
    about the companies in a result set we are holding, and searching again to
    answer it would spend the budget twice for the same information.
    """
    lines = [
        "Below are news stories found today, and the list of companies the team "
        "already tracks in its Master Pipeline.",
        "",
        "Name the companies that appear in the STORIES but are NOT in the TRACKED "
        "list. For each one, ONE line saying whether it is relevant to membrane and "
        "why, judged against the use-case table (A-J and who buys each) in the "
        "sales strategy above. Name the use case you matched, or say plainly that it "
        "matches none.",
        "",
        "FORMAT, one per line and nothing else:",
        "  SCREEN | <company> | <fit or no fit, and which use case> | <url>",
        "",
        "DO NOT propose adding anything to any sheet. A human is asked before any "
        "row is added and that is a separate step you are not part of.",
        "If every company in the news is already tracked, reply: NOTHING NEW",
        "",
        "TODAY'S STORIES:",
    ]
    for s in stories[:20]:
        lines.append(f"  - {s.get('about', '?')}: {s.get('what', '')} <{s.get('url', '')}>")
    lines += ["", "ALREADY TRACKED:", "  " + ", ".join(known_companies[:200])]
    return "\n".join(lines)


# -- reading what came back ---------------------------------------------------

_STORY_RE = re.compile(r"^\s*STORY\s*\|", re.IGNORECASE)
_SCREEN_RE = re.compile(r"^\s*SCREEN\s*\|", re.IGNORECASE)
_URL_RE = re.compile(r"https?://[^\s<>)\]]+")


def parse_stories(text: str, *, sources: Optional[list] = None) -> list:
    """The STORY lines, as [{about, what, url, url_key}].

    A LINE WITHOUT A URL IS DROPPED, silently and on purpose. The prompt says
    every line needs one; a line that arrives without one is a claim nobody can
    check, and "never post a story with no source link" is the rule this
    enforces rather than hopes for.

    `sources` is the structured citation list from `websearch.parse_results`,
    used to rescue a line whose URL the model put somewhere else — matched by
    position, which is the best available and is why it is a fallback.
    """
    out: list = []
    spare = [s.get("url") for s in (sources or []) if s.get("url")]
    for line in str(text or "").splitlines():
        if not _STORY_RE.match(line):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            continue
        about = parts[1] if len(parts) > 1 else ""
        what = parts[2] if len(parts) > 2 else ""
        tail = " ".join(parts[3:]) if len(parts) > 3 else ""
        found = _URL_RE.search(tail) or _URL_RE.search(line)
        url = found.group(0).rstrip(".,;)") if found else ""
        if not url and spare:
            url = spare.pop(0)
        if not url:
            log.info("[news] dropped a story with no source link: %r", line[:120])
            continue
        # A url that crept into the "what" field would be repeated in the line.
        what = _URL_RE.sub("", what).strip(" -–—|")
        if not about or not what:
            continue
        out.append({"about": about, "what": what, "url": url,
                    "url_key": url_key(url)})
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


# -- what the message says ----------------------------------------------------


def attribute(story: dict, targets: list) -> str:
    """"Sahaj Garg — Wispr Flow, on our Outreach PoCs", or "".

    NAMING THE ROW IS THE POINT. A story about somebody on our sheet is worth
    more than the same story about a stranger, and the reader can only tell the
    difference if the message says which row it came from. Without it the news
    post is a newsletter.
    """
    about = _norm(story.get("about"))
    if not about:
        return ""
    for t in targets:
        name = _norm(t.get("name"))
        if not name:
            continue
        if name == about or name in about or about in name:
            org = str(t.get("company") or "").strip()
            src = str(t.get("source") or "").strip()
            bits = str(t.get("name") or "").strip()
            if org and org.lower() != bits.lower():
                bits += f" — {org}"
            return f"{bits}, on our {src}" if src else bits
    return ""


def render(stories: list, *, mode: str, targets: list, limit: int = 0) -> str:
    """The news item's text: one short line per story, each with its link.

    ONE LINE PER STORY, and the link is on the line rather than in a footnote
    block — a reader scanning six lines should be able to click the one that
    matters without matching numbers to a list at the bottom.
    """
    cap = max(1, int(limit or config.NEWS_MAX_ITEMS))
    picked = stories[:cap]
    if not picked:
        return ""
    lines: list = []
    if mode == MODE_FIELD:
        lines.append("Nothing on our contacts today, so here is what moved in AI:")
    for s in picked:
        who = attribute(s, targets)
        head = f"{s['about']} — {s['what']}" if s.get("about") else s.get("what", "")
        line = f"• {head} <{s['url']}>"
        if who:
            line += f"\n  ({who})"
        lines.append(line)
    return "\n".join(lines)


def render_screen(rows: list, *, limit: int = 5) -> str:
    """R2's text: companies in the news that we do not track."""
    picked = [r for r in rows if r.get("company")][:max(1, int(limit))]
    if not picked:
        return ""
    lines = ["In the news today and not in the Master Pipeline:"]
    for r in picked:
        link = f" <{r['url']}>" if r.get("url") else ""
        lines.append(f"• {r['company']} — {r['verdict']}{link}")
    lines.append("Say the word and I'll add any of these — I won't add anything "
                 "without a yes.")
    return "\n".join(lines)


def cutoff_iso(today: date) -> str:
    """The date before which a posted story is allowed to be posted again."""
    return dl.iso(today - timedelta(days=max(1, int(config.NEWS_REPEAT_DAYS))))


def _self_test() -> int:
    """`python -m news` — selection, prompts, parsing, rendering. No I/O."""
    logging.basicConfig(level=logging.ERROR, format="%(levelname)-7s %(message)s")
    failures = 0

    def check(name, got, want):
        nonlocal failures
        ok = got == want
        failures += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")

    today = date(2026, 9, 28)

    print("url identity")
    check("tracking parameters are stripped",
          url_key("https://x.com/a?utm_source=news"), "x.com/a")
    check("www and scheme collapse",
          url_key("http://www.x.com/a/"), url_key("https://x.com/a"))
    check("fragments go", url_key("https://x.com/a#top"), "x.com/a")
    check("case is ignored", url_key("https://X.com/A"), "x.com/a")
    check("empty stays empty", url_key(""), "")
    check("two outlets are two stories",
          url_key("https://a.com/x") == url_key("https://b.com/x"), False)

    print("\ncollecting targets")
    targets, dropped = collect_targets(
        poc_rows=[{"name": "Ada Lovelace", "company": "Acme", "_row": 4},
                  {"name": "Gone Person", "company": "Old Co", "_row": 5},
                  {"name": "", "company": "No Name"}],
        mapping_rows=[{"researcher": "Alan Turing", "org": "Globex", "tier": "T1"},
                      {"researcher": "Grace Hopper", "org": "Initech", "tier": "T3"},
                      {"researcher": "Departed One", "org": "X", "tier": "T1",
                       "do_not_recommend": "left for Microsoft"}],
        pipeline_companies=["Initech", "Acme"],
        departed={_norm("Gone Person")},
    )
    names = [t["name"] for t in targets]
    check("active PoCs are in", "Ada Lovelace" in names, True)
    check("a departed PoC is not", "Gone Person" in names, False)
    check("...and is reported for removal",
          target_key(KIND_POC, "Gone Person") in dropped, True)
    check("T1 researchers are in", "Alan Turing" in names, True)
    check("T3 researchers are not", "Grace Hopper" in names, False)
    check("a flagged departure is not", "Departed One" in names, False)
    check("pipeline companies are in", "Initech" in names, True)
    check("a nameless row is skipped", "" in names, False)
    check("tiers are tagged",
          [t["kind"] for t in targets if t["name"] == "Alan Turing"], [KIND_RESEARCHER])

    print("\nthe people prompt")
    p = people_prompt(targets[:2], today=today, keywords=["evals", "RLHF"])
    check("it names people individually", "Ada Lovelace" in p, True)
    check("it carries the company", "(Acme)" in p, True)
    check("it lists what counts", "funding round" in p, True)
    check("keywords are seeds, not limits", "not limited to these" in p, True)
    check("it demands the format", "STORY |" in p, True)
    check("it demands a url", "No url, no line." in p, True)
    check("it offers an honest empty", "NOTHING FOUND" in p, True)
    check("no domain line without domains", "Restrict this search" in p, False)
    check("...and one with them",
          "Restrict this search" in people_prompt(
              targets[:1], today=today, domains=["reuters.com"]), True)

    print("\nthe field prompt")
    f = field_prompt(today=today, keywords=["AI safety"])
    check("it is about the field", "SALES team" in f, True)
    check("it still demands links", "No url, no line." in f, True)

    print("\nparsing stories")
    text = (
        "Here is what I found.\n"
        "STORY | Ada Lovelace | raised a $12m Series A | https://x.com/a?utm_source=q\n"
        "STORY | Globex | hired a head of AI | https://y.com/b\n"
        "STORY | Nobody | something with no link\n"
        "not a story line\n"
    )
    got = parse_stories(text)
    check("two stories survive", len(got), 2)
    check("the linkless one is dropped",
          any(s["about"] == "Nobody" for s in got), False)
    check("the url is normalised for the key", got[0]["url_key"], "x.com/a")
    check("the url itself is kept whole",
          got[0]["url"], "https://x.com/a?utm_source=q")
    check("what happened is read", got[0]["what"], "raised a $12m Series A")
    check("a missing url can be rescued from citations",
          len(parse_stories("STORY | A | did a thing",
                            sources=[{"url": "https://z.com/1"}])), 1)

    print("\nparsing the screen")
    rows = parse_screen(
        "SCREEN | Nebius | fits use case C, inference infra | https://n.com/1\n"
        "SCREEN | Priority Tech | no fit, payments | https://p.com/2\n"
    )
    check("both screens read", len(rows), 2)
    check("the verdict survives", "fits use case C" in rows[0]["verdict"], True)

    print("\nfound-nothing")
    check("the people pass can come back empty",
          found_nothing("NOTHING FOUND"), True)
    check("...and the screen too", found_nothing("NOTHING NEW"), True)
    check("a real answer is not empty", found_nothing("STORY | A | b | c"), False)

    print("\nattribution")
    check("a story about our PoC names the row",
          attribute({"about": "Ada Lovelace"}, targets),
          "Ada Lovelace — Acme, on our Outreach PoCs")
    check("a stranger gets nothing", attribute({"about": "Someone Else"}, targets), "")
    check("a partial name still matches",
          bool(attribute({"about": "Ada Lovelace, CEO"}, targets)), True)

    print("\nrendering")
    body = render(got, mode=MODE_PEOPLE, targets=targets)
    check("every story carries its link", body.count("<http"), 2)
    check("the row is named", "on our Outreach PoCs" in body, True)
    check("people mode does not announce a fallback",
          "Nothing on our contacts" in body, False)
    field = render(got, mode=MODE_FIELD, targets=[])
    check("field mode says so in plain words",
          field.startswith("Nothing on our contacts today"), True)
    check("the cap is honoured",
          render(got * 5, mode=MODE_PEOPLE, targets=[], limit=3).count("<http"), 3)
    check("no stories, no text", render([], mode=MODE_PEOPLE, targets=[]), "")

    print("\nthe screen's text")
    screen = render_screen(rows)
    check("it names the company", "Nebius" in screen, True)
    check("it asks before adding", "without a yes" in screen, True)

    print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
