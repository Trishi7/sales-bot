"""THE RETRIEVAL LAYER — search and page fetch, outside the model. No LLM here.

WHY THIS EXISTS. Anthropic's server-side web search puts ~28k tokens of page
text into the context per search and re-reads it on every internal iteration:
one hourly news check cost about $0.21 and a four-search question about $0.25.
The fix is architectural rather than a tighter cap — FETCH CHEAPLY HERE, hand
the model only titles and snippets (hundreds of tokens, not tens of thousands),
and let the light model do the extraction. No rule changes what it does; only
what it costs.

THREE THINGS LIVE HERE AND NOTHING ELSE:

    search(query, ...)   one request to a search API -> [{title, url, snippet,
                         date, source}]. Serper by default, Brave as the
                         automatic fallback, a SQLite cache in front of both.
    fetch_page(url)      one page, read with research.py's fetcher, cut to
                         FETCH_PAGE_MAX_CHARS. LinkedIn is never fetched.
    budget()             what the day's request ledger says.

NEVER RAISES. A search that cannot run returns [] and logs why; `last_error()`
(and `search_detail`) say which of the reasons it was, so a caller can tell
"the budget is spent" from "the web said nothing".

THE BUDGET IS BANKED HERE, per request, in the same `web_search_usage` ledger
the server-side tool used — against the REAL IST day, because a request is real
money whatever date a test is pretending it is. A cache hit is not a request.

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
from urllib.parse import urlsplit

import config
import deadlines as dl
import usage

log = logging.getLogger(__name__)

SERPER_URL = "https://google.serper.dev"
BRAVE_URL = "https://api.search.brave.com/res/v1"

# Most results one request asks for. Serper bills a second credit past 10.
MAX_RESULTS = 10
SNIPPET_MAX_CHARS = 300

# How much of a fetched page is kept in the cache. More than is ever returned
# at once, so a `focus` read of a cached page still has the page to look in.
_PAGE_CACHE_CHARS = 40000

# -- where the cache and the ledger live --------------------------------------
#
# A GETTER, not a handle: the bot swaps its database for a throwaway copy while
# a simulation runs, and the cache and the cost ledger must NOT go with it — a
# request made inside a simulation was still paid for.

_db_getter: Optional[Callable] = None
_last_error: str = ""


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


# -- what is configured -------------------------------------------------------


def backend() -> str:
    return str(config.SEARCH_BACKEND or "serper").strip().lower()


def available() -> tuple:
    """(ok, why). Can a search request be made at all right now?"""
    if not config.WEB_SEARCH_ENABLED:
        return False, "WEB_SEARCH_ENABLED is off"
    name = backend()
    if name == "anthropic":
        return False, ("SEARCH_BACKEND is anthropic, so searches run inside the "
                       "model call rather than here")
    if name == "serper" and not config.SERPER_API_KEY:
        if config.BRAVE_API_KEY:
            return True, ""
        return False, "SERPER_API_KEY is not set"
    if name == "brave" and not config.BRAVE_API_KEY:
        return False, "BRAVE_API_KEY is not set"
    return True, ""


def budget() -> dict:
    """{"used", "left", "budget"} for the real day. Zero left when unreadable."""
    total = config.search_daily_budget()
    db = _db()
    if db is None:
        return {"used": 0, "left": total, "budget": total}
    used = db.web_searches_today(_marker())
    return {"used": used, "left": max(0, total - used), "budget": total}


def cost_per_request(name: str) -> float:
    """Dollars for ONE request on `name`. Anthropic's server tool is $10/1k."""
    name = str(name or "").strip().lower()
    if name == "serper":
        return float(config.SERPER_COST_PER_1K) / 1000.0
    if name == "brave":
        return float(config.BRAVE_COST_PER_1K) / 1000.0
    return 0.01


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


def _tbs(days: Optional[int]) -> str:
    if not days:
        return ""
    days = int(days)
    named = {1: "qdr:d", 7: "qdr:w", 30: "qdr:m", 31: "qdr:m", 365: "qdr:y"}
    return named.get(days, f"qdr:d{days}")


def _serper(query: str, *, n: int, news: bool, days: Optional[int]) -> tuple:
    """(status, results). Serper: POST /search or /news, X-API-KEY."""
    body: dict = {"q": query, "num": n}
    tbs = _tbs(days)
    if tbs:
        body["tbs"] = tbs
    status, data = _http(
        "POST", SERPER_URL + ("/news" if news else "/search"),
        headers={"X-API-KEY": config.SERPER_API_KEY,
                 "Content-Type": "application/json"},
        body=body,
    )
    rows = (data or {}).get("news" if news else "organic") or []
    return status, _normalise(
        rows, title="title", url="link", snippet="snippet", date="date",
        source=lambda r: r.get("source") or "")


def _brave(query: str, *, n: int, news: bool, days: Optional[int]) -> tuple:
    """(status, results). Brave: GET web/search or news/search."""
    params: dict = {"q": query, "count": n}
    if days:
        days = int(days)
        named = {1: "pd", 7: "pw", 30: "pm", 31: "pm", 365: "py"}
        if days in named:
            params["freshness"] = named[days]
        else:
            end = dl.real_today_ist()
            params["freshness"] = f"{dl.iso(end - timedelta(days=days))}to{dl.iso(end)}"
    status, data = _http(
        "GET", BRAVE_URL + ("/news/search" if news else "/web/search"),
        headers={"X-Subscription-Token": config.BRAVE_API_KEY,
                 "Accept": "application/json"},
        params=params,
    )
    data = data or {}
    rows = data.get("results") if news else (data.get("web") or {}).get("results")
    return status, _normalise(
        rows or [], title="title", url="url", snippet="description", date="age",
        source=lambda r: ((r.get("meta_url") or {}).get("hostname")
                          or (r.get("profile") or {}).get("name") or ""))


_CALLERS = {"serper": _serper, "brave": _brave}


def _attempt(name: str, query: str, **kw) -> tuple:
    """(status, results, error). One retry on a timeout or a dropped
    connection; an HTTP status is returned as it came."""
    for tries in (1, 2):
        try:
            status, results = _CALLERS[name](query, **kw)
            return status, results, ""
        except Exception as e:
            log.info("[search] %s raised %s on try %d: %s", name, type(e).__name__,
                     tries, str(e)[:160])
            error = type(e).__name__
            if tries == 1:
                time.sleep(0.5)
    return 0, [], error


# -- search -------------------------------------------------------------------


def _cache_key(query: str, n: int, news: bool, days: Optional[int]) -> str:
    raw = json.dumps([" ".join(query.lower().split()), int(n), bool(news),
                      int(days or 0)])
    return "q|" + hashlib.sha1(raw.encode("utf-8")).hexdigest()


def search_detail(query: str, *, n: int = 10, news: bool = False,
                  site: Optional[str] = None, days: Optional[int] = None,
                  rule: str = "search") -> dict:
    """`search`, with what happened: {"results", "cached", "backend",
    "requests", "error"}. `requests` is what was billed — 0 on a cache hit."""
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
    hours = max(0, int(config.SEARCH_CACHE_HOURS))
    if db is not None and hours:
        hit = db.search_cache_get(
            key, since_utc=_iso(_utc_now() - timedelta(hours=hours)))
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

    first = backend()
    if first == "serper" and not config.SERPER_API_KEY:
        first = "brave"
    order = [first]
    if first == "serper" and config.BRAVE_API_KEY:
        order.append("brave")

    results: list = []
    used = ""
    error = ""
    for name in order:
        status, results, raised = _attempt(name, query, n=n, news=news, days=days)
        if status == 200:
            used, error = name, ""
            break
        error = raised or f"{name} answered HTTP {status}"
        retryable = status == 429 or status >= 500 or status == 0
        if db is not None:
            db.record_web_search(on_date=_marker(), rule_id=rule, searches=0,
                                 errors=1, backend=name)
        if not retryable:
            break
        if name == "serper" and "brave" in order:
            log.warning("[search] serper failed (%s); falling back to brave", error)
            continue
        if status >= 500:
            # ONE RETRY on a server error with no fallback to go to.
            status, results, raised = _attempt(name, query, n=n, news=news, days=days)
            if status == 200:
                used, error = name, ""
            break

    if not used:
        out.update(error=error or "the search did not succeed", backend=order[-1])
        _last_error = out["error"]
        log.warning("[search] query=%r backend=%s n=0 cached=no — failed: %s",
                    query[:120], order[-1], out["error"])
        return out

    out.update(results=results, backend=used, requests=1)
    _last_error = ""
    usage.count("requests")
    usage.spend(cost_per_request(used))
    if db is not None:
        db.record_web_search(on_date=_marker(), rule_id=rule, searches=1,
                             errors=0, backend=used)
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
    model, and a model must not be able to point this at the machine it runs on.
    """
    import news

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
    return news.digest_link_reason(url)


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
    import news
    key = "page|" + news.url_key(url)
    hours = max(0, int(config.SEARCH_CACHE_HOURS))
    if db is not None and hours:
        hit = db.search_cache_get(
            key, since_utc=_iso(_utc_now() - timedelta(hours=hours)))
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
    """`python -m search_backend` — offline: the HTTP boundary is stubbed."""
    import os
    import tempfile

    logging.basicConfig(level=logging.ERROR, format="%(levelname)-7s %(message)s")
    failures = 0

    def check(name, got, want):
        nonlocal failures
        ok = got == want
        failures += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")

    global _http
    real_http = _http
    saved = {k: getattr(config, k) for k in (
        "SEARCH_BACKEND", "SERPER_API_KEY", "BRAVE_API_KEY", "SEARCH_DAILY_BUDGET",
        "SEARCH_CACHE_HOURS", "WEB_SEARCH_ENABLED")}
    calls: list = []
    script: list = []

    def fake_http(method, url, *, headers, params=None, body=None):
        calls.append({"method": method, "url": url, "body": body, "params": params})
        return script.pop(0) if script else (200, {"organic": []})

    import db as dbmod
    tmp = tempfile.mkdtemp(prefix="saley-search-")
    store = dbmod.DB(os.path.join(tmp, "t.db"))
    try:
        _http = fake_http
        bind(lambda: store)
        config.SEARCH_BACKEND, config.SERPER_API_KEY = "serper", "k"
        config.BRAVE_API_KEY, config.SEARCH_DAILY_BUDGET = "", 3
        config.SEARCH_CACHE_HOURS, config.WEB_SEARCH_ENABLED = 6, True

        print("a serper search")
        script.append((200, {"organic": [
            {"title": "Ritu M - Co-founder - Shunya Labs | LinkedIn",
             "link": "https://www.linkedin.com/in/ritu-m", "snippet": "x" * 400},
            {"title": "no link"}]}))
        got = search('"Shunya Labs" research', site="linkedin.com/in", n=10, rule="R11")
        check("one result, the linkless one dropped", len(got), 1)
        check("the shape", sorted(got[0]), ["date", "snippet", "source", "title", "url"])
        check("the snippet is capped", len(got[0]["snippet"]), SNIPPET_MAX_CHARS)
        check("the source falls back to the host", got[0]["source"], "linkedin.com")
        check("site became a prefix", calls[0]["body"]["q"],
              'site:linkedin.com/in "Shunya Labs" research')
        check("POST /search", (calls[0]["method"], calls[0]["url"]),
              ("POST", SERPER_URL + "/search"))
        check("it was banked", budget()["used"], 1)

        print("\nthe cache")
        before = len(calls)
        again = search_detail('"Shunya Labs" research', site="linkedin.com/in", rule="R11")
        check("a repeat is a cache hit", again["cached"], True)
        check("...with no request", (len(calls) - before, again["requests"]), (0, 0))
        check("...and no budget", budget()["used"], 1)

        print("\nnews and days")
        script.append((200, {"news": [{"title": "T", "link": "https://a.com/1",
                                       "snippet": "s", "date": "2 hours ago",
                                       "source": "Reuters"}]}))
        got = search("Acme", news=True, days=7, n=8, rule="R8")
        check("POST /news", calls[-1]["url"], SERPER_URL + "/news")
        check("days became tbs", calls[-1]["body"].get("tbs"), "qdr:w")
        check("n is passed", calls[-1]["body"]["num"], 8)
        check("the outlet is the source", got[0]["source"], "Reuters")
        check("the date is kept", got[0]["date"], "2 hours ago")

        print("\nthe fallback")
        config.BRAVE_API_KEY = "b"
        script.extend([(429, {}), (200, {"web": {"results": [
            {"title": "B", "url": "https://b.com/x", "description": "d"}]}})])
        d = search_detail("fallback query", rule="t")
        check("serper 429 -> brave answered", (d["backend"], len(d["results"])),
              ("brave", 1))
        check("the brave call is a GET", calls[-1]["method"], "GET")
        config.BRAVE_API_KEY = ""

        print("\nthe budget")
        check("three requests used", budget()["used"], 3)
        before = len(calls)
        d = search_detail("one more", rule="t")
        check("a spent budget returns nothing", d["results"], [])
        check("...without a request", len(calls) - before, 0)
        check("...and says why", "budget is spent" in d["error"], True)
        check("last_error agrees", "budget is spent" in last_error(), True)

        print("\nnever raises")
        config.SEARCH_DAILY_BUDGET = 99

        def boom(*a, **k):
            raise TimeoutError("slow")

        _http = boom
        check("a timeout is []", search("anything new"), [])
        check("...and named", last_error(), "TimeoutError")
        _http = fake_http
        script.append((401, {}))
        check("a 401 is [] with no retry", search("bad key"), [])
        config.SERPER_API_KEY = ""
        check("no key says so", search_detail("x y")["error"], "SERPER_API_KEY is not set")
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
        _http = real_http
        bind(None)
        for k, v in saved.items():
            setattr(config, k, v)
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
