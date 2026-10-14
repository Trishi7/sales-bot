"""arXiv — OUR RESEARCHERS' NEW PAPERS. No LLM in this file, and no API key.

A researcher on Outreach PoCs (a person at a university, or a person whose
Research Paper Link is filled) is news when they publish. arXiv's public API
answers an author query as an Atom feed, free, so once a day — at the big
pull, never hourly (`feeds.pull`) — the researchers are asked for as AUTHORS,
ARXIV_BATCH_SIZE to a query:

    search_query=au:"David Gamez" OR au:"Jane Roe" ...
    sortBy=submittedDate, sortOrder=descending, max_results=50

and every paper that is theirs comes back in THE SAME DICT SHAPE
`feeds.parse` returns, so the store, the scorer and the posts treat a paper
like any other story: source "arXiv", kind researcher, flag 1, `names` the
matched author(s), source rank 5.

TWO PEOPLE SHARE A NAME. A paper counts only when (a) one of its authors is
written exactly as one of ours and (b) its PRIMARY category is one of ours
(CATEGORIES: cs.AI, cs.CL, cs.LG, cs.CV, cs.SD, eess.AS, stat.ML). A David
Gamez publishing in q-bio is somebody else.

ONLY WHAT IS NEW. The query returns an author's papers newest first, however
old; a paper submitted more than MAX_AGE_DAYS ago is not news and is left out.

POLITE, AND NEVER IN THE WAY. arXiv asks for three seconds between requests
(GAP_SECONDS). A 503 or a timeout is one line in the log: the rest of today's
queries are skipped and tomorrow's pull tries again. Nothing here raises.

`_get` IS THE ONE SOCKET — a verify script replaces it and nothing else.
"""
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote_plus

import config

log = logging.getLogger(__name__)

API = ("http://export.arxiv.org/api/query?search_query={q}"
       "&sortBy=submittedDate&sortOrder=descending&max_results=50")
SOURCE = "arXiv"
SOURCE_RANK = 5
KIND = "researcher"

# The fields a paper has to be in to be ours: AI, language, learning, vision,
# sound, speech, and statistical ML.
CATEGORIES = ("cs.AI", "cs.CL", "cs.LG", "cs.CV", "cs.SD", "eess.AS", "stat.ML")

# arXiv's terms of use: no more than one request every three seconds.
GAP_SECONDS = 3.0
# How old a submission may be and still be news. Four days, so Friday's paper
# announced on Monday is not lost to the weekend.
MAX_AGE_DAYS = 4
SUMMARY_MAX_CHARS = 300

# The clock the gap is measured on, and how it is waited out. Module names so
# a check can hand in a pretend clock and wait for nothing.
_monotonic = time.monotonic
_sleep = time.sleep
_last_request: list = [None]


def _get(url: str) -> bytes:
    """One query's bytes. The ONE socket in this module. Raises what
    `requests` raises (a 503 included)."""
    import requests

    resp = requests.get(
        url, timeout=max(1, int(config.NEWS_FEED_TIMEOUT_SECONDS)) * 2,
        headers={"User-Agent": f"{config.COS_NAME}/feed-reader (+arxiv)"},
    )
    resp.raise_for_status()
    return resp.content


def _spaced_get(url: str) -> bytes:
    if _last_request[0] is not None:
        wait = GAP_SECONDS - (_monotonic() - _last_request[0])
        if wait > 0:
            _sleep(wait)
    try:
        return _get(url)
    finally:
        _last_request[0] = _monotonic()


def _clean(name) -> str:
    return " ".join(str(name or "").replace('"', " ").split())


def _key(name) -> str:
    """An author's name as it is compared: case, dots and spacing ignored."""
    return re.sub(r"[^a-z0-9]+", " ", _clean(name).lower()).strip()


def author_names(entries: list) -> list:
    """The names to ask for, each once, in a stable order."""
    out: dict = {}
    for e in entries or []:
        name = _clean(e.get("name") if isinstance(e, dict) else e)
        if len(name) >= 3 and _key(name) not in out:
            out[_key(name)] = name
    return [out[k] for k in sorted(out)]


def batches(names: list, size: Optional[int] = None) -> list:
    size = max(1, int(size if size is not None else config.ARXIV_BATCH_SIZE))
    return [names[i:i + size] for i in range(0, len(names), size)]


def query_url(names: list) -> str:
    """One author query: `au:"A" OR au:"B" ...`, newest submissions first."""
    q = " OR ".join(f'au:"{_clean(n)}"' for n in names)
    return API.format(q=quote_plus(q))


def _plain(text) -> str:
    return " ".join(re.sub(r"(?s)<[^>]+>", " ", str(text or "")).split())


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def parse(content, names: list, *, now: Optional[datetime] = None) -> list:
    """One answer's bytes -> the papers that are OURS, as feed items.

    Kept only when an author matches one of `names` exactly (case and dots
    aside), the primary category is one of CATEGORIES, and it was submitted
    within MAX_AGE_DAYS. Never raises; [] for nonsense.
    """
    import feedparser

    import news

    now = now or datetime.now(timezone.utc)
    try:
        parsed = feedparser.parse(content)
    except Exception:
        log.exception("[arxiv] an answer could not be parsed")
        return []
    ours = {_key(n): _clean(n) for n in names or []}
    oldest = now - timedelta(days=MAX_AGE_DAYS)
    out: list = []
    for entry in parsed.entries or []:
        url = str(entry.get("link") or entry.get("id") or "").strip()
        title = _plain(entry.get("title"))
        if not url.lower().startswith(("http://", "https://")) or not title:
            continue
        primary = entry.get("arxiv_primary_category") or {}
        category = str(primary.get("term") if hasattr(primary, "get") else "" or "")
        if not category:
            tags = entry.get("tags") or []
            category = str(tags[0].get("term") or "") if tags else ""
        if category not in CATEGORIES:
            log.debug("[arxiv] dropped %r — primary category %s is not ours",
                      title[:80], category or "(none)")
            continue
        authors = [str(a.get("name") or "") for a in (entry.get("authors") or [])
                   if hasattr(a, "get")]
        matched = [ours[_key(a)] for a in authors if _key(a) in ours]
        if not matched:
            continue
        when = now
        t = entry.get("published_parsed") or entry.get("updated_parsed")
        if t:
            try:
                when = min(datetime(*t[:6], tzinfo=timezone.utc), now)
            except (TypeError, ValueError):
                when = now
        if when < oldest:
            continue
        out.append({
            "url": url, "url_key": news.url_key(url),
            "headline_key": news.headline_key(title),
            "title": _clip(title, 300),
            "summary": _clip(_plain(entry.get("summary")), SUMMARY_MAX_CHARS),
            "source": SOURCE,
            "published_at": when.astimezone(timezone.utc).isoformat(timespec="seconds"),
            "topic_hint": "",
            "seen_at": now.astimezone(timezone.utc).isoformat(timespec="seconds"),
            "kind": KIND, "sheet_ref": "",
            "names": ", ".join(n.replace(",", " ") for n in dict.fromkeys(matched)),
            "flag": 1, "source_rank": SOURCE_RANK,
        })
    return out


def fetch(entries: list, *, now: Optional[datetime] = None) -> list:
    """Every new paper by our researchers. Never raises.

    ARXIV_BATCH_SIZE authors a query, GAP_SECONDS between two queries. THE
    FIRST FAILURE ENDS TODAY'S RUN — a 503 or a timeout means arXiv is busy,
    and asking thirteen more times would not help it or us: one line in the
    log, what was already read is returned, tomorrow tries again.
    """
    if not config.ARXIV_ENABLED:
        return []
    names = author_names(entries)
    if not names:
        return []
    now = now or datetime.now(timezone.utc)
    groups = batches(names)
    out: list = []
    seen: set = set()
    asked = 0
    for group in groups:
        asked += 1
        try:
            content = _spaced_get(query_url(group))
        except Exception as e:
            log.warning("[arxiv] query %d of %d failed (%s: %s); arXiv is skipped "
                        "until tomorrow's pull", asked, len(groups), type(e).__name__,
                        str(e)[:120])
            break
        for item in parse(content, group, now=now):
            if item["url_key"] not in seen:
                seen.add(item["url_key"])
                out.append(item)
    log.info("[arxiv] %d author(s) in %d of %d quer(y/ies): %d new paper(s) in %s",
             len(names), asked, len(groups), len(out), ", ".join(CATEGORIES))
    return out


def _self_test() -> int:
    """`python -m arxiv` — offline: the HTTP boundary is stubbed."""
    logging.basicConfig(level=logging.CRITICAL)
    failures = 0

    def check(name, got, want):
        nonlocal failures
        ok = got == want
        failures += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")

    now = datetime(2026, 10, 13, 9, 0, tzinfo=timezone.utc)

    def entry(n, title, authors, category, published="2026-10-12T10:00:00Z"):
        people = "".join(f"<author><name>{a}</name></author>" for a in authors)
        return (f"<entry><id>http://arxiv.org/abs/2610.{n:05d}v1</id>"
                f"<title>{title}</title><summary>An abstract.</summary>"
                f"<published>{published}</published><updated>{published}</updated>"
                f"{people}<link href=\"http://arxiv.org/abs/2610.{n:05d}v1\" "
                f"rel=\"alternate\" type=\"text/html\"/>"
                f"<arxiv:primary_category term=\"{category}\"/>"
                f"<category term=\"{category}\"/></entry>")

    def feed(*entries):
        return ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
                "<feed xmlns=\"http://www.w3.org/2005/Atom\" "
                "xmlns:arxiv=\"http://arxiv.org/schemas/atom\">"
                "<title>ArXiv Query</title>" + "".join(entries) + "</feed>").encode()

    print("reading an answer")
    body = feed(
        entry(1, "Machine consciousness, measured", ["David Gamez", "A. N. Other"], "cs.AI"),
        entry(2, "Gene expression in yeast", ["David Gamez"], "q-bio.GN"),
        entry(3, "Somebody else's vision paper", ["Dana Gamez"], "cs.CV"),
        entry(4, "An old speech paper", ["david gamez"], "eess.AS",
              published="2026-09-01T10:00:00Z"))
    got = parse(body, ["David Gamez"], now=now)
    check("one paper is ours", [i["title"] for i in got],
          ["Machine consciousness, measured"])
    check("it is flagged, by rule, and names the author",
          (got[0]["flag"], got[0]["names"], got[0]["kind"]), (1, "David Gamez", "researcher"))
    check("its source is arXiv, at rank 5", (got[0]["source"], got[0]["source_rank"]),
          ("arXiv", 5))
    check("it has the keys a feed item has",
          sorted(got[0]),
          sorted(["url", "url_key", "headline_key", "title", "summary", "source",
                  "published_at", "topic_hint", "seen_at", "kind", "sheet_ref", "names",
                  "flag", "source_rank"]))
    check("a same-name author in q-bio is somebody else",
          any("yeast" in i["title"] for i in got), False)
    check("a paper older than four days is not news",
          any("old speech" in i["title"] for i in got), False)

    print("\nthe queries")
    authors = [{"name": f"Author {i:03d}"} for i in range(261)]
    check("261 authors, 20 a query: 14 queries", len(batches(author_names(authors), 20)), 14)
    url = query_url(["David Gamez", 'Jane "JR" Roe'])
    check("an author query, newest first",
          url, "http://export.arxiv.org/api/query?search_query=au%3A%22David+Gamez%22+OR+"
               "au%3A%22Jane+JR+Roe%22&sortBy=submittedDate&sortOrder=descending"
               "&max_results=50")

    print("\nthe fetch")
    global _get, _sleep, _monotonic
    real = (_get, _sleep, _monotonic)
    saved = (config.ARXIV_ENABLED, config.ARXIV_BATCH_SIZE)
    config.ARXIV_ENABLED, config.ARXIV_BATCH_SIZE = True, 20
    clock = [50.0]
    stamps: list = []

    def fake_sleep(seconds):
        clock[0] += seconds

    def fake_get(url):
        stamps.append(clock[0])
        return body

    try:
        _sleep, _monotonic, _get = fake_sleep, (lambda: clock[0]), fake_get
        _last_request[0] = None
        papers = fetch(authors + [{"name": "David Gamez"}], now=now)
        check("14 queries for 262 authors", len(stamps), 14)
        check("at least three seconds between two queries",
              min(b - a for a, b in zip(stamps, stamps[1:])) >= 3.0, True)
        check("the paper is returned once", len(papers), 1)

        def down(url):
            stamps.append(clock[0])
            raise RuntimeError("503 Server Error: Service Unavailable")

        stamps.clear()
        _get = down
        check("a 503 is logged and skipped: nothing raised, nothing returned",
              fetch(authors, now=now), [])
        check("...and the rest of today's queries are not sent", len(stamps), 1)
        config.ARXIV_ENABLED = False
        stamps.clear()
        check("ARXIV_ENABLED=false asks nothing", (fetch(authors, now=now), stamps), ([], []))
    finally:
        _get, _sleep, _monotonic = real
        _last_request[0] = None
        config.ARXIV_ENABLED, config.ARXIV_BATCH_SIZE = saved

    print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
