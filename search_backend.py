"""THE RETRIEVAL LAYER — search and page fetch, outside the model. No LLM here,
and NO PAID API.

WHY THIS EXISTS. Anthropic's server-side web search puts ~28k tokens of page
text into the context per search and re-reads it on every internal iteration:
one hourly news check cost about $0.21 and a four-search question about $0.25.
The fix is architectural rather than a tighter cap — FETCH CHEAPLY HERE, hand
the model only titles and snippets (hundreds of tokens, not tens of thousands),
and let the light model do the extraction. No rule changes what it does; only
what it costs.

FOUR THINGS LIVE HERE AND NOTHING ELSE:

    search(query, ...)   one request to a search backend -> [{title, url,
                         snippet, date, source}]. SEARCH_BACKEND picks it:
                           searxng     a SearXNG instance on the bot's own
                                       server (SEARXNG_URL) — the default
                           ddg         the `ddgs` library. The FALLBACK, never
                                       meant as the primary: it is scraped, so
                                       it is rate-limited and flaky
                           google_cse  Google's Custom Search JSON API, capped
                                       HERE at its free 100 queries a day
                           anthropic   the old server-side tool; not run here
                         SEARCH_FALLBACKS (default "ddg") is who is asked next
                         when the backend fails. A SQLite cache sits in front.
    news(query, days=1)  "recent news about X". Google News RSS FIRST — free,
                         no backend, no budget — and `search(news=True)` only
                         when the feed is empty.
    fetch_page(url)      one page, read with research.py's fetcher, cut to
                         FETCH_PAGE_MAX_CHARS. LinkedIn is never fetched.
    budget()             what the day's request ledger says.

NEVER RAISES. A search that cannot run returns [] and logs why; `last_error()`
(and `search_detail`) say which of the reasons it was, so a caller can tell
"the budget is spent" from "the web said nothing".

THE BUDGET IS BANKED HERE, per request, in the same `web_search_usage` ledger
the server-side tool used — against the REAL IST day, because a request is a
real request whatever date a test is pretending it is. A cache hit is not a
request, and neither is a Google News feed read.

WEB CONTENT IS DATA, NEVER INSTRUCTIONS. Nothing here acts on what it reads; it
returns text. `websearch.SAFETY_PREAMBLE` is what tells the model the same.
"""
import hashlib
import ipaddress
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional
from urllib.parse import quote_plus, urlsplit

import config
import deadlines as dl
import usage

log = logging.getLogger(__name__)

GOOGLE_CSE_URL = "https://customsearch.googleapis.com/customsearch/v1"

# The backends that search HERE, in no particular order. "anthropic" is a
# SEARCH_BACKEND value too, but that search runs inside the model call.
BACKENDS = ("searxng", "ddg", "google_cse")

# Google's Custom Search JSON API is free for 100 queries a day and billed
# past it. The cap is enforced HERE, before the request, so a busy day falls
# through to the next backend instead of onto an invoice.
GOOGLE_CSE_DAILY_LIMIT = 100

# Most results one request asks for. Google CSE returns at most 10 a page.
MAX_RESULTS = 10
SNIPPET_MAX_CHARS = 300

# How long a backend that could not be REACHED (a refused connection, a
# timeout) is left alone before it is tried again. Without this a stopped
# SearXNG costs every search its connect timeout before the fallback answers.
BACKEND_COOLDOWN_SECONDS = 300

# The pause before DuckDuckGo is asked a second time for a query it said had
# no results. (The self-test sets it to 0.)
DDG_RETRY_SECONDS = 1.0

# How much of a fetched page is kept in the cache. More than is ever returned
# at once, so a `focus` read of a cached page still has the page to look in.
_PAGE_CACHE_CHARS = 40000

# -- where the cache and the ledger live --------------------------------------
#
# A GETTER, not a handle: the bot swaps its database for a throwaway copy while
# a simulation runs, and the cache and the ledger must NOT go with it — a
# request made inside a simulation was still made.

_db_getter: Optional[Callable] = None
_last_error: str = ""
_down_until: dict = {}          # backend -> time.monotonic() it may be retried
_quota_memory: dict = {}        # (day, backend) -> requests, when no DB is bound


def bind(getter: Optional[Callable]) -> None:
    """Where the cache and the ledger live: `getter()` returns the DB. Without
    one (a self-test) searches still run, uncached and unbanked."""
    global _db_getter
    _db_getter = getter


def _db():
    try:
        return _db_getter() if _db_getter is not None else None
    except Exception:
        log.debug("[search] no database handle", exc_info=True)
        return None


def last_error() -> str:
    """Why the most recent `search` returned nothing, or "" when it ran."""
    return _last_error


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat(timespec="seconds")


def _marker() -> str:
    """The REAL IST day the ledger banks against."""
    return dl.iso(dl.real_today_ist())


def _pacific_day() -> str:
    """The day Google's quota counts in: it resets at midnight Pacific time."""
    try:
        from zoneinfo import ZoneInfo
        zone = ZoneInfo("America/Los_Angeles")
    except Exception:
        zone = timezone(timedelta(hours=-8))     # no tz database: PST, near enough
    return _utc_now().astimezone(zone).date().isoformat()


def _scrub(text) -> str:
    """An error message with the Google key taken out — `requests` puts the
    whole URL, query string and all, into what it raises."""
    text = str(text or "")
    key = str(config.GOOGLE_CSE_KEY or "")
    return text.replace(key, "***") if key else text


# -- what is configured -------------------------------------------------------


def backend() -> str:
    return str(config.SEARCH_BACKEND or "searxng").strip().lower()


def chain() -> list:
    """Who is asked, in order: SEARCH_BACKEND, then SEARCH_FALLBACKS. [] under
    the anthropic backend, whose searches run inside the model call."""
    first = backend()
    if first == "anthropic":
        return []
    out: list = []
    for name in [first] + [str(f or "").strip().lower()
                           for f in (config.SEARCH_FALLBACKS or [])]:
        if name in BACKENDS and name not in out:
            out.append(name)
    return out


def _usable(name: str) -> tuple:
    """(ok, why) — is this backend configured at all? Not whether it is up."""
    if name == "searxng":
        if not str(config.SEARXNG_URL or "").strip():
            return False, "SEARXNG_URL is not set"
        return True, ""
    if name == "ddg":
        try:
            import ddgs  # noqa: F401
        except ImportError:
            return False, "the ddgs library is not installed (pip install ddgs)"
        return True, ""
    if name == "google_cse":
        if not (config.GOOGLE_CSE_KEY and config.GOOGLE_CSE_CX):
            return False, "GOOGLE_CSE_KEY and GOOGLE_CSE_CX are not both set"
        return True, ""
    return False, f"{name} is not a search backend"


def available() -> tuple:
    """(ok, why). Can a search request be made at all right now?"""
    if not config.WEB_SEARCH_ENABLED:
        return False, "WEB_SEARCH_ENABLED is off"
    if backend() == "anthropic":
        return False, ("SEARCH_BACKEND is anthropic, so searches run inside the "
                       "model call rather than here")
    whys: list = []
    for name in chain():
        ok, why = _usable(name)
        if ok:
            return True, ""
        whys.append(why)
    return False, "; ".join(whys) or "no search backend is configured"


def budget() -> dict:
    """{"used", "left", "budget"} for the real day. Zero left when unreadable."""
    total = config.search_daily_budget()
    db = _db()
    if db is None:
        return {"used": 0, "left": total, "budget": total}
    used = db.web_searches_today(_marker())
    return {"used": used, "left": max(0, total - used), "budget": total}


# What a request cost on the backends that charged. The three that search here
# are free; serper and brave are priced only so that "what did you cost" still
# reads the ledger rows they left behind correctly.
_COST_PER_REQUEST = {"anthropic": 0.01, "serper": 0.001, "brave": 0.005}


def cost_per_request(name: str) -> float:
    """Dollars for ONE request on `name`. searxng, ddg and google_cse (inside
    its free quota, which is all this module ever uses) cost nothing;
    Anthropic's server tool is $10 per thousand."""
    return _COST_PER_REQUEST.get(str(name or "").strip().lower(), 0.0)


def _quota_used(name: str, day: str) -> int:
    db = _db()
    if db is None:
        return int(_quota_memory.get((day, name), 0))
    return db.search_quota_used(day, name)


def _quota_add(name: str, day: str) -> None:
    db = _db()
    if db is None:
        _quota_memory[(day, name)] = int(_quota_memory.get((day, name), 0)) + 1
    else:
        db.search_quota_add(day, name)


# -- the HTTP boundary --------------------------------------------------------


def _http(method: str, url: str, *, headers: dict, params: Optional[dict] = None,
          body: Optional[dict] = None) -> tuple:
    """(status, json). The ONE place a search socket opens — a verify script
    replaces this function and nothing else. Raises what `requests` raises."""
    import requests

    resp = requests.request(
        method, url, headers=headers, params=params, json=body,
        timeout=max(1, int(config.SEARCH_TIMEOUT_SECONDS)),
    )
    try:
        data = resp.json()
    except ValueError:
        data = {}
    return resp.status_code, data


def _ddgs_client():
    """The `ddgs` client. The library opens its own sockets, so this — not
    `_http` — is what a verify script replaces to stub DuckDuckGo."""
    from ddgs import DDGS

    return DDGS(timeout=max(1, int(config.SEARCH_TIMEOUT_SECONDS)))


def _clip(text, limit: int = SNIPPET_MAX_CHARS) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _host(url: str) -> str:
    host = (urlsplit(str(url or "")).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _normalise(rows, *, title: str, url: str, snippet: str, date: str,
               source) -> list:
    out: list = []
    seen: set = set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        link = str(row.get(url) or "").strip()
        if not link.lower().startswith(("http://", "https://")) or link in seen:
            continue
        seen.add(link)
        out.append({
            "title": _clip(row.get(title), 200),
            "url": link,
            "snippet": _clip(row.get(snippet)),
            "date": _clip(row.get(date), 40),
            "source": _clip(source(row) or _host(link), 60),
        })
    return out


def _range(days: Optional[int]) -> str:
    """The smallest named window that covers `days`: day, week, month or year.
    SearXNG and DuckDuckGo take a named range, not a number of days."""
    if not days:
        return ""
    days = int(days)
    return "day" if days <= 1 else "week" if days <= 7 else \
        "month" if days <= 31 else "year"


# -- the three backends -------------------------------------------------------
#
# Each returns (status, results, error). 200 with no error is an answer — an
# empty one included. Anything else sends the search to the next backend.


def _searxng(query: str, *, n: int, news: bool, days: Optional[int]) -> tuple:
    """SearXNG: GET {SEARXNG_URL}/search?q=…&format=json&categories=…"""
    params: dict = {"q": query, "format": "json",
                    "categories": "news" if news else "general"}
    window = _range(days)
    if window:
        params["time_range"] = window
    status, data = _http(
        "GET", str(config.SEARXNG_URL).strip().rstrip("/") + "/search",
        headers={"Accept": "application/json"}, params=params,
    )
    if status == 403:
        # What a stock SearXNG answers to format=json: only html is enabled.
        return status, [], ("searxng answered HTTP 403 — add `json` to "
                            "search.formats in its settings.yml (see DEPLOY.md)")
    data = data if isinstance(data, dict) else {}
    results = _normalise(
        data.get("results") or [], title="title", url="url", snippet="content",
        date="publishedDate", source=lambda r: "")[:n]
    if status == 200 and not results and data.get("unresponsive_engines"):
        # SearXNG is up but every engine behind it refused or timed out. That
        # is an outage, not "the web said nothing".
        dead = ", ".join(str(e[0] if isinstance(e, (list, tuple)) else e)
                         for e in data["unresponsive_engines"][:4])
        return status, [], f"searxng's engines were unresponsive ({dead})"
    return status, results, ""


# A scraped results page sometimes runs one LinkedIn title into the next
# ("… | LinkedInDhruv R…"). The profile's own title ends at "| LinkedIn".
_LINKEDIN_TAIL_RE = re.compile(r"(\|\s*LinkedIn)\S.*$")


def _ddg(query: str, *, n: int, news: bool, days: Optional[int]) -> tuple:
    """DuckDuckGo through the `ddgs` library: text() and news(). ANY exception
    is [] and a log line — it is the fallback, and it must never be the reason
    a rule fails."""
    window = _range(days)
    limit = window[:1] or None                     # d | w | m | y
    rows: list = []
    for tries in (1, 2):
        try:
            client = _ddgs_client()
            if news:
                rows = client.news(query, max_results=n, timelimit=limit)
            else:
                rows = client.text(query, max_results=n, timelimit=limit)
            break
        except Exception as e:
            said = _scrub(e)[:160]
            if "no results" not in said.lower():
                log.info("[search] ddg raised %s: %s", type(e).__name__, said)
                return 0, [], f"ddg raised {type(e).__name__}"
            # The library RAISES for an empty page — and an empty page is as
            # often a bad minute as a fact: the same query a second later
            # commonly answers. So it is asked ONCE more, and then believed.
            if tries == 2:
                log.info("[search] ddg found nothing for %r (asked twice)",
                         query[:120])
                return 200, [], ""
            time.sleep(DDG_RETRY_SECONDS)
    rows = [dict(r, title=_LINKEDIN_TAIL_RE.sub(r"\1", str(r.get("title") or "")))
            for r in (rows or []) if isinstance(r, dict)]
    if news:
        results = _normalise(rows, title="title", url="url", snippet="body",
                             date="date", source=lambda r: r.get("source") or "")
    else:
        results = _normalise(rows, title="title", url="href", snippet="body",
                             date="date", source=lambda r: "")
    return 200, results[:n], ""


def _google_cse(query: str, *, n: int, news: bool, days: Optional[int]) -> tuple:
    """Google Custom Search JSON API. The free 100 a day is checked and banked
    HERE, before the request leaves — Google bills the 101st."""
    day = _pacific_day()
    used = _quota_used("google_cse", day)
    if used >= GOOGLE_CSE_DAILY_LIMIT:
        return 0, [], (f"google_cse's free quota is spent ({used}/"
                       f"{GOOGLE_CSE_DAILY_LIMIT} today)")
    params: dict = {"key": config.GOOGLE_CSE_KEY, "cx": config.GOOGLE_CSE_CX,
                    "q": query, "num": min(n, 10)}
    if days:
        params["dateRestrict"] = f"d{int(days)}"
    if news:
        params["sort"] = "date"                    # there is no news index
    _quota_add("google_cse", day)                  # a failed call counts too
    status, data = _http("GET", GOOGLE_CSE_URL,
                         headers={"Accept": "application/json"}, params=params)
    data = data if isinstance(data, dict) else {}
    return status, _normalise(
        data.get("items") or [], title="title", url="link", snippet="snippet",
        date="date", source=lambda r: r.get("displayLink") or ""), ""


_CALLERS = {"searxng": _searxng, "ddg": _ddg, "google_cse": _google_cse}


def _attempt(name: str, query: str, **kw) -> tuple:
    """(status, results, error). One retry on a TIMEOUT; a refused connection
    is not retried (it will be refused again), and an HTTP status is returned
    as it came."""
    error = ""
    for tries in (1, 2):
        try:
            return _CALLERS[name](query, **kw)
        except Exception as e:
            error = type(e).__name__
            log.info("[search] %s raised %s on try %d: %s", name, error, tries,
                     _scrub(e)[:160])
            if tries == 1 and "timeout" in error.lower():
                time.sleep(0.5)
                continue
            break
    return 0, [], error


# -- search -------------------------------------------------------------------


def _cache_key(query: str, n: int, news: bool, days: Optional[int]) -> str:
    raw = json.dumps([" ".join(query.lower().split()), int(n), bool(news),
                      int(days or 0)])
    return "q|" + hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _cached(key: str) -> Optional[dict]:
    db = _db()
    hours = max(0, int(config.SEARCH_CACHE_HOURS))
    if db is None or not hours:
        return None
    return db.search_cache_get(
        key, since_utc=_iso(_utc_now() - timedelta(hours=hours)))


def search_detail(query: str, *, n: int = 10, news: bool = False,
                  site: Optional[str] = None, days: Optional[int] = None,
                  rule: str = "search") -> dict:
    """`search`, with what happened: {"results", "cached", "backend",
    "requests", "error"}. `requests` is what was banked — 0 on a cache hit."""
    global _last_error
    out = {"results": [], "cached": False, "backend": "", "requests": 0,
           "error": ""}
    query = " ".join(str(query or "").split())
    if site and "site:" not in query:
        query = f"site:{str(site).strip()} {query}".strip()
    n = max(1, min(int(n or MAX_RESULTS), MAX_RESULTS))
    if not query:
        out["error"] = "empty query"
        _last_error = out["error"]
        return out

    ok, why = available()
    if not ok:
        out["error"] = why
        _last_error = why
        log.info("[search] query=%r backend=%s n=0 cached=no — not run: %s",
                 query[:120], backend(), why)
        return out

    db = _db()
    key = _cache_key(query, n, news, days)
    hit = _cached(key)
    if hit is not None:
        out.update(results=list(hit["results"] or []), cached=True,
                   backend=hit["backend"])
        _last_error = ""
        usage.count("cache_hits")
        log.info("[search] query=%r backend=%s n=%d cached=yes", query[:120],
                 hit["backend"], len(out["results"]))
        return out

    if db is not None:
        state = budget()
        if state["left"] <= 0:
            out["error"] = (f"the daily search budget is spent ({state['used']}/"
                            f"{state['budget']} requests used)")
            _last_error = out["error"]
            log.info("[search] query=%r backend=%s n=0 cached=no — not run: %s",
                     query[:120], backend(), out["error"])
            return out

    order = chain()
    results: list = []
    used = ""
    failed: list = []
    for at, name in enumerate(order):
        ok, why = _usable(name)
        wait = _down_until.get(name, 0) - time.monotonic()
        if ok and wait > 0:
            ok, why = False, f"unreachable; not retried for another {int(wait)}s"
        if not ok:
            failed.append(f"{name}: {why}")
            continue
        status, results, error = _attempt(name, query, n=n, news=news, days=days)
        if status == 200 and not error:
            used = name
            break
        error = error or f"answered HTTP {status}"
        failed.append(f"{name}: {error}")
        if status == 0 and name != "ddg" and "quota" not in error:
            _down_until[name] = time.monotonic() + BACKEND_COOLDOWN_SECONDS
        if db is not None:
            db.record_web_search(on_date=_marker(), rule_id=rule, searches=0,
                                 errors=1, backend=name)
        if at + 1 < len(order):
            log.warning("[search] %s failed (%s); falling back to %s", name,
                        error, order[at + 1])

    if not used:
        out.update(error="; ".join(failed) or "the search did not succeed",
                   backend=order[-1] if order else backend())
        _last_error = out["error"]
        log.warning("[search] query=%r backend=%s n=0 cached=no — failed: %s",
                    query[:120], out["backend"], out["error"])
        return out

    out.update(results=results, backend=used, requests=1)
    _last_error = ""
    usage.count("requests")
    usage.spend(cost_per_request(used))
    if db is not None:
        db.record_web_search(on_date=_marker(), rule_id=rule, searches=1,
                             errors=0, backend=used)
        # AN EMPTY ANSWER IS NOT CACHED. From a scraped backend "nothing" is
        # as often a bad minute as a fact, and six hours is a long time to
        # keep believing it.
        if results:
            db.search_cache_put(key, backend=used, query=query, results=results,
                                fetched_at=_iso(_utc_now()))
    log.info("[search] query=%r backend=%s n=%d cached=no", query[:120], used,
             len(results))
    return out


def search(query: str, *, n: int = 10, news: bool = False,
           site: Optional[str] = None, days: Optional[int] = None,
           rule: str = "search") -> list:
    """One search -> [{title, url, snippet, date, source}]. Never raises.

    `site` becomes a `site:` prefix; `days` limits to the last N days; `news`
    asks the news index. `rule` is only the name the request is banked under.
    [] when it could not run or found nothing — `last_error()` says which.
    """
    try:
        return search_detail(query, n=n, news=news, site=site, days=days,
                             rule=rule)["results"]
    except Exception:
        log.exception("[search] search raised; returning nothing")
        return []


# -- news ---------------------------------------------------------------------
#
# "RECENT NEWS ABOUT X" DOES NOT NEED A SEARCH BACKEND. Google News turns any
# query into an RSS feed — the same one feeds.py polls per topic — so R8, R10
# and R11's company news read that first: free, no key, no SearXNG, no budget.


def google_news_url(query: str, days: int) -> str:
    query = " ".join(str(query or "").split())
    # A bare multi-word name is a phrase; anything already carrying quotes or
    # an operator is left as its author wrote it.
    if " " in query and not re.search(r'["():]|\bOR\b|\bAND\b', query):
        query = f'"{query}"'
    import feeds
    return feeds.GOOGLE_NEWS.format(q=quote_plus(f"{query} when:{int(days)}d"))


def _google_news(query: str, *, days: int, n: int) -> tuple:
    """(results, error) from the Google News RSS feed for `query`."""
    import feeds

    try:
        content = feeds._get(google_news_url(query, days))
    except Exception as e:
        return [], f"{type(e).__name__}: {str(e)[:120]}"
    since = feeds.utc_iso(_utc_now() - timedelta(days=days))
    rows = [r for r in feeds.parse(content) if r["published_at"] >= since]
    # HEADLINES THAT NAME IT COME FIRST, newest first within each group. Google
    # News also matches a story that mentions the query once in its body, and
    # the feed carries no body — so for those the headline says nothing about
    # the query, and they must not crowd out the ones that do.
    named = " ".join(str(query or "").replace('"', " ").lower().split())
    rows.sort(key=lambda r: r["published_at"], reverse=True)
    rows.sort(key=lambda r: named not in r["title"].lower())
    out: list = []
    seen: set = set()
    for row in rows:
        if row["headline_key"] in seen:            # one story, several outlets
            continue
        seen.add(row["headline_key"])
        out.append({
            "title": _clip(row["title"], 200), "url": row["url"],
            "snippet": _clip(row["summary"]), "date": row["published_at"],
            "source": _clip(row["source"] or _host(row["url"]), 60),
        })
    return out[:n], ""


def news_detail(query: str, *, days: int = 1, n: int = 10,
                rule: str = "news") -> dict:
    """`news`, with what happened — the same dict `search_detail` returns.
    `backend` is "google_news" when the feed answered, and then `requests` is
    0: a feed read is not a search request."""
    global _last_error
    out = {"results": [], "cached": False, "backend": "google_news",
           "requests": 0, "error": ""}
    query = " ".join(str(query or "").split())
    days = max(1, int(days or 1))
    n = max(1, min(int(n or MAX_RESULTS), MAX_RESULTS))
    if not query:
        out["error"] = "empty query"
        _last_error = out["error"]
        return out
    if not config.WEB_SEARCH_ENABLED:
        out["error"] = "WEB_SEARCH_ENABLED is off"
        _last_error = out["error"]
        return out

    raw = json.dumps([" ".join(query.lower().split()), n, days])
    key = "news|" + hashlib.sha1(raw.encode("utf-8")).hexdigest()
    hit = _cached(key)
    if hit is not None:
        out.update(results=list(hit["results"] or []), cached=True)
        _last_error = ""
        usage.count("cache_hits")
        log.info("[search] query=%r backend=google_news n=%d cached=yes",
                 query[:120], len(out["results"]))
        return out

    results, error = _google_news(query, days=days, n=n)
    if results:
        out["results"] = results
        _last_error = ""
        db = _db()
        if db is not None:
            db.search_cache_put(key, backend="google_news", query=query,
                                results=results, fetched_at=_iso(_utc_now()))
        log.info("[search] query=%r backend=google_news n=%d cached=no (a feed "
                 "read, not a search request)", query[:120], len(results))
        return out

    # ONLY WHEN THE FEED IS EMPTY (or could not be read) does this cost a
    # search request.
    log.info("[search] query=%r backend=google_news n=0 — %s; asking search()",
             query[:120], error or f"nothing in the last {days} day(s)")
    return search_detail(query, n=n, news=True, days=days, rule=rule)


def news(query: str, days: int = 1, *, n: int = 10, rule: str = "news") -> list:
    """Recent news about `query` -> [{title, url, snippet, date, source}],
    newest first. Never raises.

    Google News RSS first (India's edition, the last `days` days); `search(...,
    news=True)` only when the feed is empty. A Google News item's url is
    Google's redirect to the outlet and its snippet is usually empty — the
    headline, the outlet and the date are what the feed carries.
    """
    try:
        return news_detail(query, days=days, n=n, rule=rule)["results"]
    except Exception:
        log.exception("[search] news raised; returning nothing")
        return []


# -- fetch_page ---------------------------------------------------------------

_TITLE_RE = re.compile(r"(?is)<title[^>]*>(.*?)</title>")

# NEVER FETCHED, whatever asks. LinkedIn's terms forbid scraping; a search
# result that LINKS there is a citation, and that is the whole of it.
NEVER_FETCH = ("linkedin.com",)


def refusal_reason(url: str) -> str:
    """Why this url is not fetched, or "" when it may be.

    The same digest/host block-list news.py applies to a story's link
    (NEWS_BLOCKED_DOMAINS, digest and newsletter pages), LinkedIn always, and
    anything that is not a public http(s) address — a url can come from a
    model, and a model must not be able to point this at the machine it runs
    on (which is also where SearXNG listens).
    """
    import news as news_mod

    parts = urlsplit(str(url or "").strip())
    if parts.scheme.lower() not in ("http", "https"):
        return "not an http(s) address"
    host = (parts.hostname or "").lower()
    if not host:
        return "no host"
    bare = host[4:] if host.startswith("www.") else host
    for blocked in NEVER_FETCH:
        if bare == blocked or bare.endswith("." + blocked):
            return f"{blocked} is never fetched"
    if bare in ("localhost",) or bare.endswith((".local", ".internal")):
        return "not a public address"
    try:
        ip = ipaddress.ip_address(host)
        if not ip.is_global:
            return "not a public address"
    except ValueError:
        pass
    return news_mod.digest_link_reason(url)


def _focused(text: str, focus, cap: int) -> str:
    """The passages around the `focus` words, or the head when none is there."""
    terms = [str(t).lower() for t in (focus or ()) if str(t).strip()]
    if not terms:
        return text[:cap]
    low = text.lower()
    spans: list = []
    for term in terms:
        at = 0
        while len(spans) < 40:
            at = low.find(term, at)
            if at < 0:
                break
            spans.append((max(0, at - 300), min(len(text), at + 400)))
            at += len(term)
    if not spans:
        return text[:cap]
    spans.sort()
    merged = [list(spans[0])]
    for start, end in spans[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return " … ".join(text[a:b] for a, b in merged)[:cap]


def fetch_page(url: str, *, focus=()) -> dict:
    """One page -> {"ok", "title", "text"} (plus "url", "error", "cached").

    Read with research.py's fetcher (its timeout and byte ceiling), tags
    stripped, cut to FETCH_PAGE_MAX_CHARS. `focus` — a few words — returns the
    passages around them instead of the top of the page, for a caller after one
    fact ("registration", "deadline"). Never raises; costs no search request.
    """
    import news as news_mod
    import research

    url = str(url or "").strip()
    out = {"ok": False, "title": "", "text": "", "url": url, "error": "",
           "cached": False}
    why = refusal_reason(url)
    if why:
        out["error"] = why
        log.info("[search] fetch_page refused %s — %s", url[:160], why)
        return out

    cap = max(200, int(config.FETCH_PAGE_MAX_CHARS))
    db = _db()
    key = "page|" + news_mod.url_key(url)
    hit = _cached(key)
    if hit is not None and isinstance(hit["results"], dict):
        page = hit["results"]
        out.update(ok=True, title=str(page.get("title") or ""), cached=True,
                   text=_focused(str(page.get("text") or ""), focus, cap))
        usage.count("cache_hits")
        log.info("[search] fetch_page url=%s ok=yes chars=%d cached=yes",
                 url[:160], len(out["text"]))
        return out

    raw = research.fetch_raw(url, agent="page-reader")
    if not raw["ok"]:
        out["error"] = raw["error"]
        log.info("[search] fetch_page url=%s ok=no cached=no — %s", url[:160],
                 raw["error"])
        return out
    # A REDIRECT IS CHECKED TOO: a page that bounced to LinkedIn, or inwards,
    # is refused exactly as if it had been asked for directly.
    landed = str(raw.get("final_url") or url)
    why = refusal_reason(landed) if landed != url else ""
    if why:
        out["error"] = f"redirected to a page that is not fetched ({why})"
        log.info("[search] fetch_page refused %s — redirected to %s: %s", url[:160],
                 landed[:160], why)
        return out
    html = raw["html"]
    m = _TITLE_RE.search(html)
    title = _clip(research._readable(m.group(1), cap=200) if m else "", 200)
    text = research._readable(html, cap=_PAGE_CACHE_CHARS)
    if db is not None:
        db.search_cache_put(key, backend="page", query=url,
                            results={"title": title, "text": text},
                            fetched_at=_iso(_utc_now()))
    out.update(ok=True, title=title, text=_focused(text, focus, cap))
    log.info("[search] fetch_page url=%s ok=yes chars=%d cached=no", url[:160],
             len(out["text"]))
    return out


def _self_test() -> int:
    """`python -m search_backend` — offline: every socket is stubbed."""
    import os
    import tempfile

    import feeds

    logging.basicConfig(level=logging.ERROR, format="%(levelname)-7s %(message)s")
    failures = 0

    def check(name, got, want):
        nonlocal failures
        ok = got == want
        failures += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")

    global _http, _ddgs_client, DDG_RETRY_SECONDS
    real_http, real_ddgs, real_get = _http, _ddgs_client, feeds._get
    real_pause, DDG_RETRY_SECONDS = DDG_RETRY_SECONDS, 0
    saved = {k: getattr(config, k) for k in (
        "SEARCH_BACKEND", "SEARCH_FALLBACKS", "SEARXNG_URL", "GOOGLE_CSE_KEY",
        "GOOGLE_CSE_CX", "SEARCH_DAILY_BUDGET", "SEARCH_CACHE_HOURS",
        "WEB_SEARCH_ENABLED")}
    calls: list = []
    script: list = []
    ddg_calls: list = []
    ddg_script: list = []
    fed: list = []

    def fake_http(method, url, *, headers, params=None, body=None):
        calls.append({"method": method, "url": url, "params": params or {}})
        return script.pop(0) if script else (200, {"results": []})

    class FakeDDGS:
        def _answer(self, kind, query, **kw):
            ddg_calls.append({"kind": kind, "query": query, **kw})
            got = ddg_script.pop(0) if ddg_script else []
            if isinstance(got, Exception):
                raise got
            return got

        def text(self, query, **kw):
            return self._answer("text", query, **kw)

        def news(self, query, **kw):
            return self._answer("news", query, **kw)

    import db as dbmod
    tmp = tempfile.mkdtemp(prefix="saley-search-")
    store = dbmod.DB(os.path.join(tmp, "t.db"))
    try:
        _http, _ddgs_client = fake_http, lambda: FakeDDGS()
        bind(lambda: store)
        config.SEARCH_BACKEND, config.SEARCH_FALLBACKS = "searxng", ["ddg"]
        config.SEARXNG_URL = "http://127.0.0.1:8888/"
        config.GOOGLE_CSE_KEY = config.GOOGLE_CSE_CX = ""
        config.SEARCH_DAILY_BUDGET, config.SEARCH_CACHE_HOURS = 5, 6
        config.WEB_SEARCH_ENABLED = True

        print("a searxng search")
        script.append((200, {"results": [
            {"title": "Ritu M - Co-founder - Shunya Labs | LinkedIn",
             "url": "https://www.linkedin.com/in/ritu-m", "content": "x" * 400},
            {"title": "no link"}]}))
        got = search('"Shunya Labs" research', site="linkedin.com/in", n=10, rule="R11")
        check("one result, the linkless one dropped", len(got), 1)
        check("the shape", sorted(got[0]), ["date", "snippet", "source", "title", "url"])
        check("the snippet is capped", len(got[0]["snippet"]), SNIPPET_MAX_CHARS)
        check("the source is the host", got[0]["source"], "linkedin.com")
        check("GET {SEARXNG_URL}/search", (calls[0]["method"], calls[0]["url"]),
              ("GET", "http://127.0.0.1:8888/search"))
        check("site became a prefix", calls[0]["params"]["q"],
              'site:linkedin.com/in "Shunya Labs" research')
        check("format=json, categories=general",
              (calls[0]["params"]["format"], calls[0]["params"]["categories"]),
              ("json", "general"))
        check("it was banked", budget()["used"], 1)

        print("\nthe cache")
        before = len(calls)
        again = search_detail('"Shunya Labs" research', site="linkedin.com/in", rule="R11")
        check("a repeat is a cache hit", again["cached"], True)
        check("...with no request", (len(calls) - before, again["requests"]), (0, 0))
        check("...and no budget", budget()["used"], 1)

        print("\nnews and days")
        script.append((200, {"results": [
            {"title": "T", "url": "https://a.com/1", "content": "s",
             "publishedDate": "2026-09-30T08:00:00"}]}))
        got = search("Acme", news=True, days=7, n=8, rule="R8")
        check("categories=news", calls[-1]["params"]["categories"], "news")
        check("days became a time_range", calls[-1]["params"].get("time_range"), "week")
        check("the date is kept", got[0]["date"], "2026-09-30T08:00:00")
        script.append((200, {"results": [
            {"title": f"t{i}", "url": f"https://a.com/n{i}"} for i in range(9)]}))
        check("n cuts what searxng returns", len(search("many", n=3)), 3)

        print("\nthe fallback — SEARCH_FALLBACKS")
        script.append((403, {}))
        ddg_script.append([
            {"title": "Asha Rao - CEO - Shunya Labs | LinkedInVikram N",
             "href": "https://in.linkedin.com/in/asha-rao", "body": "d"}])
        d = search_detail("fallback query", days=30, rule="t")
        check("searxng 403 -> ddg answered", (d["backend"], len(d["results"])),
              ("ddg", 1))
        check("ddg text(), with a timelimit",
              (ddg_calls[-1]["kind"], ddg_calls[-1]["timelimit"]), ("text", "m"))
        check("a run-on LinkedIn title is cut", d["results"][0]["title"],
              "Asha Rao - CEO - Shunya Labs | LinkedIn")
        check("one request banked, not two", budget()["used"], 4)

        print("\nddg never raises")
        config.SEARCH_DAILY_BUDGET = 99
        config.SEARCH_BACKEND, config.SEARCH_FALLBACKS = "ddg", []
        ddg_script.append(RuntimeError("202 Ratelimit"))
        d = search_detail("rate limited", rule="t")
        check("an exception is []", d["results"], [])
        check("...named", d["error"], "ddg: ddg raised RuntimeError")
        ddg_script.extend([RuntimeError("No results found."),
                           [{"title": "Second time", "href": "https://d.com/s"}]])
        d = search_detail("a bad minute", rule="t")
        check('"No results found" is asked once more', len(d["results"]), 1)
        before = len(ddg_calls)
        ddg_script.extend([RuntimeError("No results found.")] * 2)
        d = search_detail("truly nothing", news=True, rule="t")
        check("...and then believed: an answer, not a failure",
              (d["results"], d["error"], d["backend"], len(ddg_calls) - before),
              ([], "", "ddg", 2))
        check("ddg news()", ddg_calls[-1]["kind"], "news")
        before = len(ddg_calls)
        ddg_script.extend([RuntimeError("No results found.")] * 2)
        search("truly nothing", news=True, rule="t")
        check("an empty answer was not cached", len(ddg_calls) - before, 2)

        print("\ngoogle_cse and its 100 a day")
        config.SEARCH_BACKEND = "google_cse"
        check("no key says so", search_detail("x y")["error"],
              "GOOGLE_CSE_KEY and GOOGLE_CSE_CX are not both set")
        config.GOOGLE_CSE_KEY, config.GOOGLE_CSE_CX = "secret-key", "cx1"
        script.append((200, {"items": [
            {"title": "G", "link": "https://g.com/x", "snippet": "sn",
             "displayLink": "g.com"}]}))
        d = search_detail("cse query", days=3, n=8, rule="t")
        check("it answered", (d["backend"], d["results"][0]["source"]),
              ("google_cse", "g.com"))
        check("key, cx, num and dateRestrict are sent",
              [calls[-1]["params"].get(k) for k in ("key", "cx", "num", "dateRestrict")],
              ["secret-key", "cx1", 8, "d3"])
        check("the call was counted", _quota_used("google_cse", _pacific_day()), 1)
        for _ in range(GOOGLE_CSE_DAILY_LIMIT - 1):
            store.search_quota_add(_pacific_day(), "google_cse")
        before = len(calls)
        d = search_detail("the 101st", rule="t")
        check("the 101st is refused locally", "free quota is spent (100/100" in d["error"],
              True)
        check("...without a request", len(calls) - before, 0)
        check("the key is scrubbed from an error",
              _scrub("GET /v1?key=secret-key&cx=cx1 failed"),
              "GET /v1?key=***&cx=cx1 failed")

        print("\nan unreachable searxng is left alone for a while")
        config.SEARCH_BACKEND, config.SEARCH_FALLBACKS = "searxng", ["ddg"]

        def refused(*a, **k):
            calls.append({"method": "GET", "url": "refused", "params": {}})
            raise ConnectionError("refused")

        _http = refused
        ddg_script.append([{"title": "D", "href": "https://d.com/1", "body": "b"}])
        d = search_detail("searxng is down", rule="t")
        check("ddg answered", d["backend"], "ddg")
        before = len(calls)
        ddg_script.append([{"title": "D", "href": "https://d.com/2", "body": "b"}])
        d = search_detail("searxng is still down", rule="t")
        check("the next search did not knock again",
              (len(calls) - before, d["backend"]), (0, "ddg"))
        _down_until.clear()
        _http = fake_http

        print("\nthe budget")
        config.SEARCH_DAILY_BUDGET = budget()["used"]
        before = len(calls)
        d = search_detail("one more", rule="t")
        check("a spent budget returns nothing", d["results"], [])
        check("...without a request", len(calls) - before, 0)
        check("...and says why", "budget is spent" in d["error"], True)
        check("last_error agrees", "budget is spent" in last_error(), True)

        print("\nnews() — Google News RSS first")
        rss = b"""<?xml version="1.0"?><rss version="2.0"><channel>
<title>"Sarvam AI" when:7d - Google News</title>
<item><title>Sarvam AI opens its speech models - Reuters</title>
<link>https://news.google.com/rss/articles/CBMiabc?oc=5</link>
<pubDate>%s</pubDate><source url="https://www.reuters.com">Reuters</source></item>
<item><title>Sarvam AI Opens Its Speech Models - Mint</title>
<link>https://news.google.com/rss/articles/CBMidef?oc=5</link>
<pubDate>%s</pubDate><source url="https://www.livemint.com">Mint</source></item>
<item><title>An old Sarvam AI story - Mint</title>
<link>https://news.google.com/rss/articles/CBMiold?oc=5</link>
<pubDate>Mon, 01 Jan 2024 06:00:00 GMT</pubDate>
<source url="https://www.livemint.com">Mint</source></item>
</channel></rss>""" % ((_utc_now().strftime("%a, %d %b %Y %H:%M:%S GMT").encode(),) * 2)
        feed_script: list = [rss]

        def fake_get(url):
            fed.append(url)
            got = feed_script.pop(0) if feed_script else b"<rss><channel/></rss>"
            if isinstance(got, Exception):
                raise got
            return got

        feeds._get = fake_get
        before = (len(calls), len(ddg_calls), budget()["used"])
        d = news_detail("Sarvam AI", days=7, n=8, rule="R8")
        check("the feed answered", (d["backend"], d["requests"]), ("google_news", 0))
        check("India's edition, quoted, with the window", fed[-1],
              "https://news.google.com/rss/search?q=%22Sarvam+AI%22+when%3A7d"
              "&hl=en-IN&gl=IN&ceid=IN:en")
        check("one story from two outlets, and the old one dropped", len(d["results"]), 1)
        check("the outlet is the source", (d["results"][0]["title"],
                                           d["results"][0]["source"]),
              ("Sarvam AI opens its speech models", "Reuters"))
        check("no backend was asked and no budget spent — though it is SPENT",
              (len(calls), len(ddg_calls), budget()["used"]), before)
        check("news() is the list", news("Sarvam AI", days=7, n=8), d["results"])
        check("...from the cache the second time", len(fed), 1)

        print("\nnews() falls back to search() only when the feed is empty")
        config.SEARCH_DAILY_BUDGET = 99
        script.append((200, {"results": [
            {"title": "From searxng", "url": "https://b.com/1", "content": "c"}]}))
        d = news_detail("Quiet Co", days=1, rule="R10")
        check("an empty feed -> search(news=True, days=1)",
              (d["backend"], calls[-1]["params"]["categories"],
               calls[-1]["params"]["time_range"], d["requests"]),
              ("searxng", "news", "day", 1))
        feed_script.append(TimeoutError("slow"))
        script.append((200, {"results": [
            {"title": "Also searxng", "url": "https://b.com/2", "content": "c"}]}))
        check("a feed that cannot be read -> search() too",
              news_detail("Other Co", days=1)["backend"], "searxng")
        config.WEB_SEARCH_ENABLED = False
        check("search off is off for news too", news_detail("Sarvam AI")["error"],
              "WEB_SEARCH_ENABLED is off")
        config.WEB_SEARCH_ENABLED = True

        print("\nnever raises")

        def boom(*a, **k):
            raise TimeoutError("slow")

        _http = boom
        config.SEARCH_FALLBACKS = []
        check("a timeout is []", search("anything new"), [])
        check("...and named", last_error(), "searxng: TimeoutError")
        _down_until.clear()
        _http = fake_http
        config.SEARCH_BACKEND = "anthropic"
        check("the anthropic backend does not search here",
              "inside the model call" in search_detail("x y")["error"], True)

        print("\nfetch_page refusals (no socket)")
        check("linkedin is never fetched",
              fetch_page("https://www.linkedin.com/in/someone")["error"],
              "linkedin.com is never fetched")
        check("a digest page is refused",
              bool(fetch_page("https://x.com/newsletter/today")["error"]), True)
        check("localhost is refused", fetch_page("http://localhost:8080/")["error"],
              "not a public address")
        check("searxng's own address is refused",
              fetch_page("http://127.0.0.1:8888/search?q=x")["error"],
              "not a public address")
        check("a private ip is refused", fetch_page("http://10.0.0.1/")["error"],
              "not a public address")
        check("a file url is refused", fetch_page("file:///etc/passwd")["error"],
              "not an http(s) address")

        print("\nfocus")
        page = "intro " * 200 + "Registration closes 12 October 2026. " + "outro " * 200
        check("the passage around the word", "12 October 2026" in
              _focused(page, ("registration",), 800), True)
        check("the head when the word is absent",
              _focused(page, ("zebra",), 30), page[:30])
    finally:
        _http, _ddgs_client, feeds._get = real_http, real_ddgs, real_get
        DDG_RETRY_SECONDS = real_pause
        _down_until.clear()
        bind(None)
        for k, v in saved.items():
            setattr(config, k, v)
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
