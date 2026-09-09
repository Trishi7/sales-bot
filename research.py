"""RESEARCH BRIEFS — copy material for one person, on request only.

"brief me on Sahaj (Acme)" and the bot comes back with: who they are, how much
their role weighs, which of their work maps to our lanes, an angle, and a DRAFT
message to personalise.

    IT IS COPY MATERIAL. Never auto-sent, never written to the sheet, never
    actioned on its own. This bot has no outbound channel to a prospect and this
    does not give it one — it hands a human a draft to edit. The hard limit in
    sales_policy.md ("you never contact a customer, prospect, or anyone outside
    the team") is unchanged and this is deliberately on the safe side of it.

    MENTION-TRIGGERED ONLY. There is no scheduled brief and no brief attached to
    a nudge. Somebody asks, or nothing happens.

WHAT IT MAY FETCH, AND THE THREE THINGS THAT BOUNDS:

    1. ONLY URLS ALREADY ON THE ROW. The bot does not search the web, does not
       follow links out of a page it fetched, and never guesses a URL from a
       name. If the row has no research link, the brief says so and stops.
    2. ONLY RESEARCH_ALLOWED_DOMAINS (default: arxiv.org). Anything else is
       REFUSED WITH A ONE-LINE NOTE NAMING THE DOMAIN — not silently skipped,
       because "I ignored three of your links" is something the asker needs to
       know before they trust the brief as complete.
    3. BOUNDED. A timeout, a byte ceiling and a link cap, so a slow or enormous
       page degrades the brief instead of hanging the bot.

LINKEDIN IS API-OR-NOTHING. With LINKEDIN_API_* credentials the LinkedIn half
runs; without them the brief SAYS "LinkedIn access is pending" in those words.
There is no scraping fallback and there must not be one — LinkedIn's terms
forbid it, and a bot that scrapes on a team's behalf puts the team at risk.

TENURE WEIGHT IS A JUDGEMENT THE BRIEF MAKES EXPLICIT. A person who has led a
lab since its inception is a different prospect from someone who joined as an
MTS eight months ago: the first can say yes, the second has to ask. The brief
states which it thinks it is looking at and why, so a wrong read is arguable
rather than buried in a confident paragraph.

THE LANES COME FROM THE TEAM'S OWN DOCUMENTS — sales_policy.md and the strategy
doc — read at brief time rather than hard-coded, so re-writing the positioning
changes the briefs without touching code.
"""
import logging
import re
from typing import Optional
from urllib.parse import urlparse

import config

log = logging.getLogger(__name__)

# Where a research link might be sitting. The mapped `research_links` role
# first, then any extra column whose header looks like it holds one — tabs grow
# columns, and a brief that missed the paper because somebody added a "Recent
# Work" column would be wrong in a way nobody could see.
_LINK_HEADER_HINTS = (
    "research", "paper", "publication", "arxiv", "scholar", "work", "portfolio",
    "profile", "link", "url", "site", "website",
)

_URL_RE = re.compile(r"https?://[^\s,;<>\"')\]]+", re.IGNORECASE)


def links_on_row(row: dict) -> list:
    """Every URL on this row, with the column it came from.

    Returns [{"url", "column"}]. Deduped on the URL, order preserved — the same
    paper listed in two columns is one link to fetch, but the FIRST column that
    named it is the one reported, because that is where somebody put it.
    """
    seen: set = set()
    out: list = []

    def _take(value, column):
        for url in _URL_RE.findall(str(value or "")):
            clean = url.rstrip(".,;)")
            if clean in seen:
                continue
            seen.add(clean)
            out.append({"url": clean, "column": column})

    _take(row.get("research_links"), "research links")
    for header, value in (row.get("_extra") or {}).items():
        low = str(header or "").lower()
        if any(hint in low for hint in _LINK_HEADER_HINTS):
            _take(value, str(header))
    return out


def domain_of(url: str) -> str:
    """The host of a URL, lower-cased and without "www.". "" when unparseable."""
    try:
        host = (urlparse(str(url)).hostname or "").lower()
    except (ValueError, AttributeError):
        return ""
    return host[4:] if host.startswith("www.") else host


def is_allowed(url: str) -> bool:
    """Is this URL on an allowed domain?

    Matched on the registered domain AND its subdomains, so "arxiv.org" also
    allows "export.arxiv.org". Anything unparseable is NOT allowed — a URL the
    bot cannot read the host of is not one it should fetch.
    """
    host = domain_of(url)
    if not host:
        return False
    for allowed in (config.RESEARCH_ALLOWED_DOMAINS or []):
        key = str(allowed or "").strip().lower().lstrip(".")
        if not key:
            continue
        if host == key or host.endswith("." + key):
            return True
    return False


def split_links(links: list) -> tuple:
    """(allowed, refused) — and the refused ones carry the reason.

    REFUSED, NOT DROPPED. The brief names every domain it would not fetch, in
    one line, so nobody reads a partial brief as a complete one.
    """
    allowed, refused = [], []
    for link in links or []:
        if is_allowed(link["url"]):
            allowed.append(link)
        else:
            refused.append({
                **link,
                "why": (
                    f"{domain_of(link['url']) or 'that link'} is not in "
                    f"RESEARCH_ALLOWED_DOMAINS ("
                    f"{', '.join(config.RESEARCH_ALLOWED_DOMAINS) or 'nothing'})"
                ),
            })
    return allowed, refused


def fetch(url: str) -> dict:
    """Fetch ONE allowed URL. Returns {"ok", "url", "text", "error"}.

    Never raises, and never fetches a URL that has not already passed
    `is_allowed` — checked again HERE rather than trusted from the caller,
    because this is the function that actually opens a socket and a guarantee
    enforced one call up is a guarantee one refactor from being gone.

    Bounded by RESEARCH_FETCH_TIMEOUT_SECONDS and RESEARCH_FETCH_MAX_BYTES. A
    page that exceeds either is truncated or abandoned rather than allowed to
    hold up an answer somebody is waiting on.
    """
    if not is_allowed(url):
        return {"ok": False, "url": url, "text": "",
                "error": f"{domain_of(url) or 'that domain'} is not allowed"}
    try:
        import requests
    except ImportError:
        return {"ok": False, "url": url, "text": "",
                "error": "the HTTP client is not installed (pip install requests)"}
    try:
        resp = requests.get(
            url,
            timeout=max(1, int(config.RESEARCH_FETCH_TIMEOUT_SECONDS)),
            headers={"User-Agent": f"{config.COS_NAME}/research-brief"},
            stream=True,
        )
        resp.raise_for_status()
        body = resp.raw.read(
            max(1000, int(config.RESEARCH_FETCH_MAX_BYTES)), decode_content=True
        )
    except Exception as e:
        log.info("[research] could not fetch %s: %s", url, e)
        return {"ok": False, "url": url, "text": "",
                "error": f"{type(e).__name__} fetching that page"}

    text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else str(body)
    return {"ok": True, "url": url, "text": _readable(text), "error": ""}


def _readable(html: str) -> str:
    """Tags stripped, whitespace squeezed. Not a parser — enough for a model.

    Deliberately crude: an arXiv abstract page is mostly text, and a real HTML
    parser would be a dependency and a maintenance surface for a job that a
    regex does adequately here.
    """
    body = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", str(html or ""))
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    body = body.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
    body = body.replace("&gt;", ">").replace("&#39;", "'").replace("&quot;", '"')
    return " ".join(body.split())[:8000]


def linkedin_status() -> dict:
    """What the brief can say about LinkedIn.

    Two states and no third. Configured: the API half runs. Not configured: the
    brief says "LinkedIn access is pending" IN THOSE WORDS. The absence has to be
    stated, because a career history quietly missing from a brief reads as "this
    person has no notable history", which is a different and wrong claim.
    """
    if config.linkedin_ready():
        return {
            "available": True,
            "note": "LinkedIn API credentials are configured.",
        }
    return {
        "available": False,
        "note": (
            "LinkedIn access is pending — no API credentials are configured "
            "(LINKEDIN_API_ACCESS_TOKEN, or LINKEDIN_API_CLIENT_ID plus "
            "LINKEDIN_API_CLIENT_SECRET). I have NOT scraped anything to fill the "
            "gap and will not: their terms forbid it. So the role and tenure below "
            "come from the sheet alone, and may be out of date."
        ),
    }


def gather(row: dict) -> dict:
    """Everything a brief is built from, for one row. I/O, sends nothing.

    Returns {"person", "org", "role", "sheet_facts", "links_fetched",
    "links_refused", "linkedin", "no_links"}.
    """
    import gtm_sheet

    person = gtm_sheet.clean_cell(row.get("poc"))
    org = gtm_sheet.clean_cell(row.get("company"))
    links = links_on_row(row)
    allowed, refused = split_links(links)

    fetched: list = []
    cap = max(1, int(config.RESEARCH_MAX_LINKS))
    for link in allowed[:cap]:
        result = fetch(link["url"])
        fetched.append({**link, **result})
    if len(allowed) > cap:
        refused.append({
            "url": "", "column": "",
            "why": (f"{len(allowed) - cap} more allowed link(s) were not fetched — "
                    f"RESEARCH_MAX_LINKS is {cap}"),
        })

    facts = {
        role: gtm_sheet.clean_cell(row.get(role))
        for role in ("poc_designation", "poc_vertical", "industry", "use_case",
                     "first_contacted", "connected", "response", "next_steps",
                     "prospect_stage", "other_updates")
        if gtm_sheet.clean_cell(row.get(role))
    }

    return {
        "person": person,
        "org": org,
        "role": facts.get("poc_designation", ""),
        "sheet_facts": facts,
        "links_fetched": fetched,
        "links_refused": refused,
        "linkedin": linkedin_status(),
        "no_links": not links,
    }


def refusal_note(refused: list) -> str:
    """The ONE line naming what was not fetched. "" when nothing was refused."""
    if not refused:
        return ""
    parts = []
    for entry in refused:
        url = str(entry.get("url") or "").strip()
        why = str(entry.get("why") or "").strip()
        parts.append(f"{url or 'one link'} — {why}" if url else why)
    return "I did not fetch: " + "; ".join(parts) + "."


BRIEF_PROMPT = """You are writing a RESEARCH BRIEF for a colleague who is about to
reach out to one person. It is COPY MATERIAL: they will read it, edit it and send
it themselves. You are not sending anything.

Write these five things, in this order, as prose with short headings. No bullets
inside a section, no tables.

1. WHO THEY ARE. Two or three sentences from what you were given. If a fact is
   not in the material, do not state it.

2. HOW MUCH THE ROLE WEIGHS. Say plainly whether this looks like someone who can
   SAY YES or someone who has to ASK. Lead-since-inception, founder, head of lab,
   principal — that is a decision-maker. A recent MTS, a new hire, an IC on a
   large team — that is an internal champion at best, and the outreach should be
   written to be forwarded upward. SAY WHICH YOU THINK IT IS AND WHY, in one
   sentence, so a wrong read can be argued with.

3. WHAT OF THEIR WORK MAPS TO OUR LANES. The lanes are in the policy and
   strategy text you were given — use those words, not your own. Name the
   specific paper or project and the specific lane. If nothing maps, say so
   plainly; a forced connection is worse than none and reads as such.

4. THE ANGLE. One paragraph: what to open with, and why it would land with this
   person specifically. Not a pitch — a reason they would reply.

5. A DRAFT MESSAGE. Ready to edit. Under 120 words, no subject line unless it is
   an email, first person, no flattery, no "I hope this finds you well". It must
   reference something concrete from their actual work. End it where a reply is
   easy.

RULES THAT ARE NOT STYLE:
- Everything you write must come from the material you were given. Do not add a
  paper, an employer, a date or a claim that is not in it.
- If the LinkedIn note says access is pending, SAY SO in the brief — one line,
  where the career history would have gone. Do not fill the gap from memory.
- If links were refused, the refusal line you were given goes in verbatim.
- No emojis."""

def _self_test() -> int:
    """`python -m research` — link extraction and the domain allow-list."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")
    failures = 0

    def check(name, got, want):
        nonlocal failures
        ok = got == want
        failures += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")

    row = {
        "research_links": "https://arxiv.org/abs/2401.00001 and https://arxiv.org/abs/2402.2",
        "_extra": {
            "Profile Link": "https://www.linkedin.com/in/someone",
            "Notes": "chatted at https://medium.com/@x last week",
            "Scholar": "https://export.arxiv.org/abs/9",
        },
    }

    print("links on the row")
    links = links_on_row(row)
    check("only research-shaped columns are read",
          any("medium.com" in l["url"] for l in links), False)
    check("the mapped column and the extra ones are",
          len(links), 4)
    check("the column that named each link is kept",
          links[0]["column"], "research links")

    print(chr(10) + "the allow-list")
    check("an allowed domain", is_allowed("https://arxiv.org/abs/1"), True)
    check("a subdomain of it", is_allowed("https://export.arxiv.org/abs/1"), True)
    check("a lookalike is NOT allowed", is_allowed("https://notarxiv.org/abs/1"), False)
    check("a suffix trick is NOT allowed", is_allowed("https://arxiv.org.evil.com/x"), False)
    check("an unparseable string", is_allowed("not a url"), False)
    allowed, refused = split_links(links)
    check("three arxiv links pass", len(allowed), 3)
    check("linkedin is refused", [domain_of(r["url"]) for r in refused], ["linkedin.com"])

    print(chr(10) + "the refusal line")
    note = refusal_note(refused)
    check("names the domain", "linkedin.com" in note, True)
    check("names the variable", "RESEARCH_ALLOWED_DOMAINS" in note, True)
    check("nothing refused -> no line", refusal_note([]), "")

    print(chr(10) + "linkedin")
    status = linkedin_status()
    check("not configured today", status["available"], False)
    check("says pending in those words", "LinkedIn access is pending" in status["note"], True)
    check("and says it will not scrape", "will not" in status["note"], True)

    print(chr(10) + "fetch refuses a disallowed domain without opening a socket")
    result = fetch("https://medium.com/@x")
    check("refused", result["ok"], False)
    check("...naming the domain", "medium.com" in result["error"], True)

    print(chr(10) + "html to text")
    check("tags stripped",
          _readable("<p>Hello <b>world</b></p><script>bad()</script>"), "Hello world")

    print(chr(10) + f"{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
