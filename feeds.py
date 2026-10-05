"""THE FEED LAYER — RSS in, rows out. No LLM in this file, and no search API.

R1 used to SEARCH for the day's AI news: one model call carrying a server-side
web search, ~28k tokens of page text per search, every hour. The news does not
need searching for — the outlets publish it as RSS, free, and Google News turns
any topic into a feed. So:

    poll()   reads NEWS_RSS_FEEDS, plus one Google News RSS query per entry in
             NEWS_TOPICS, and stores what is NEW in `news_feed_items`.

A POLL MAKES ZERO API CALLS. It is plain HTTP GETs, run every
NEWS_FEED_POLL_MINUTES from the sweep loop. What the light model later sees is
titles and summaries of the stored rows — and only the ones it has not scored.

ONE STORY, ONE ROW. Deduplicated on the same two identities the posted stories
use — `news.url_key` (the link, tracking stripped) and `news.headline_key`
(who did what) — so a funding round carried by four feeds is stored once, and
the first feed to carry it wins. The outlets' own feeds are read before the
Google News queries for exactly that reason: their links are the primary
report, Google's are redirects.

A DIGEST IS NOT A STORY. A link `news.digest_link_reason` refuses (a roundup, a
newsletter, a host on NEWS_BLOCKED_DOMAINS) is dropped here, before it can be
scored or posted.

NEVER RAISES. A feed that times out or returns nonsense is one line in the log
and one entry in the result's `errors`; the others still land.
"""
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional
from urllib.parse import quote_plus

import config
import news

log = logging.getLogger(__name__)

SUMMARY_MAX_CHARS = 300
GOOGLE_NEWS = "https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"

# Google News answers a bare topic with up to a hundred items, some of them
# weeks old. `when:2d` keeps a poll to what could still be news.
GOOGLE_NEWS_WINDOW = "when:2d"

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


def google_news_url(topic: str) -> str:
    topic = " ".join(str(topic or "").split())
    q = f'"{topic}"' if " " in topic else topic
    return GOOGLE_NEWS.format(q=quote_plus(f"{q} {GOOGLE_NEWS_WINDOW}"))


def sources() -> list:
    """[(url, topic_hint)] — the outlets' feeds first, then one Google News
    query per topic."""
    out: list = []
    seen: set = set()
    for url in config.NEWS_RSS_FEEDS or []:
        url = str(url or "").strip()
        if url and url not in seen:
            seen.add(url)
            out.append((url, ""))
    for topic in config.NEWS_TOPICS or []:
        topic = str(topic or "").strip()
        if not topic:
            continue
        url = google_news_url(topic)
        if url not in seen:
            seen.add(url)
            out.append((url, topic))
    return out


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


def parse(content, *, topic_hint: str = "", now: Optional[datetime] = None) -> list:
    """One feed's bytes -> [{url, url_key, headline_key, title, summary, source,
    published_at, topic_hint, seen_at}]. Never raises; [] for nonsense.

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
        out.append({
            "url": url, "url_key": news.url_key(url),
            "headline_key": news.headline_key(title),
            "title": _clip(title, 300),
            "summary": _clip(summary, SUMMARY_MAX_CHARS),
            "source": _clip(source, 120),
            "published_at": utc_iso(_published(entry, now)),
            "topic_hint": topic_hint,
            "seen_at": utc_iso(now),
        })
    return out


# -- the poll -----------------------------------------------------------------


def poll(*, now: Optional[datetime] = None) -> dict:
    """Read every feed and store what is new. Zero API calls.

    Returns {"feeds", "ok", "fetched", "new", "errors"}. `fetched` is every
    item the feeds carried inside the keep window; `new` is how many of them
    the store had not seen — so a second poll straight after the first reports
    new=0.
    """
    now = now or datetime.now(timezone.utc)
    result = {"feeds": 0, "ok": 0, "fetched": 0, "new": 0, "errors": []}
    try:
        import feedparser  # noqa: F401
    except ImportError:
        result["errors"].append("feedparser is not installed (pip install feedparser)")
        log.error("[feeds] feedparser is not installed; no feed can be read")
        return result

    todo = sources()
    result["feeds"] = len(todo)
    if not todo:
        return result

    def one(pair):
        url, hint = pair
        try:
            return url, hint, _get(url), ""
        except Exception as e:
            return url, hint, b"", f"{type(e).__name__}: {str(e)[:120]}"

    with ThreadPoolExecutor(max_workers=6) as pool:
        fetched = list(pool.map(one, todo))

    keep_from = utc_iso(now - timedelta(days=max(1, int(config.NEWS_FEED_KEEP_DAYS))))
    items: list = []
    for url, hint, content, error in fetched:           # in `sources()` order
        if error:
            result["errors"].append(f"{url} — {error}")
            log.info("[feeds] could not read %s: %s", url, error)
            continue
        got = [i for i in parse(content, topic_hint=hint, now=now)
               if i["published_at"] >= keep_from]
        result["ok"] += 1
        items.extend(got)
    result["fetched"] = len(items)

    db = _db()
    if db is not None:
        try:
            result["new"] = db.news_feed_add(items)
            db.news_feed_prune(keep_from)
        except Exception:
            log.exception("[feeds] could not store the poll")
            result["errors"].append("the feed store could not be written")
    log.info("[feeds] poll: %d of %d feed(s) read, %d item(s), %d new%s — 0 API "
             "calls", result["ok"], result["feeds"], result["fetched"], result["new"],
             f", {len(result['errors'])} error(s)" if result["errors"] else "")
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
<description>&lt;p&gt;A public benchmark for tool-using agents.&lt;/p&gt;</description>
<pubDate>Thu, 01 Oct 2026 06:30:00 GMT</pubDate></item>
<item><title>The weekly AI newsletter</title>
<link>https://techcrunch.com/newsletter/week-40</link>
<pubDate>Thu, 01 Oct 2026 05:00:00 GMT</pubDate></item>
<item><title>No link here</title></item>
</channel></rss>"""
    google = b"""<?xml version="1.0"?><rss version="2.0"><channel>
<title>"evals" when:2d - Google News</title>
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
          "A public benchmark for tool-using agents.")
    check("the url key drops tracking", got[0]["url_key"],
          "techcrunch.com/2026/10/01/evals")
    check("the source is the feed", got[0]["source"], "TechCrunch AI")
    check("published is UTC ISO", got[0]["published_at"], "2026-10-01T06:30:00+00:00")

    print("\nparsing a Google News query")
    g = parse(google, topic_hint="evals", now=now)
    check("two stories", len(g), 2)
    check("the outlet comes off the title", g[0]["title"], "Lab Ships an Open Eval Suite")
    check("...and is the source", g[0]["source"], "Reuters")
    check("a summary that repeats the title is empty", g[0]["summary"], "")
    check("the topic it came from is kept", g[0]["topic_hint"], "evals")
    check("a date in the future is clamped to now", g[1]["published_at"], utc_iso(now))
    check("two outlets, one headline key",
          g[0]["headline_key"], got[0]["headline_key"])

    print("\nthe sources")
    saved = (config.NEWS_RSS_FEEDS, config.NEWS_TOPICS)
    config.NEWS_RSS_FEEDS, config.NEWS_TOPICS = ["https://a.com/feed"], ["evals",
                                                                         "voice agent"]
    src = sources()
    check("outlets first, then one query per topic",
          [s[1] for s in src], ["", "evals", "voice agent"])
    check("a multi-word topic is quoted, in India's edition",
          src[2][0], "https://news.google.com/rss/search?q=%22voice+agent%22+when%3A2d"
                     "&hl=en-IN&gl=IN&ceid=IN:en")

    print("\nthe poll")
    import db as dbmod
    global _get
    real_get = _get
    tmp = tempfile.mkdtemp(prefix="saley-feeds-")
    store = dbmod.DB(os.path.join(tmp, "t.db"))
    served: list = []

    def fake_get(url):
        served.append(url)
        if "voice" in url:
            raise TimeoutError("slow")
        return outlet if "a.com" in url else google

    try:
        _get = fake_get
        bind(lambda: store)
        first = poll(now=now)
        check("three feeds asked", (first["feeds"], len(served)), (3, 3))
        check("two read, one failed", (first["ok"], len(first["errors"])), (2, 1))
        check("three items carried", first["fetched"], 3)
        check("two stored — the Reuters copy is the TechCrunch story", first["new"], 2)
        check("the outlet's own link won",
              store.news_feed_between("2026-09-01", "2026-12-01")[-1]["url"]
              .startswith("https://techcrunch.com/"), True)
        second = poll(now=now)
        check("a second poll stores 0 new", second["new"], 0)
        check("the store holds two", store.news_feed_count(), 2)
        store.news_feed_set_scores(
            [{"url_key": "techcrunch.com/2026/10/01/evals", "importance": 4,
              "topic": "evals", "what": "a public benchmark"}], scored_at=utc_iso(now))
        rows = store.news_feed_between("2026-09-01", "2026-12-01")
        check("a score is written back",
              [r["importance"] for r in rows if "techcrunch" in r["url"]], [4])
    finally:
        _get = real_get
        bind(None)
        config.NEWS_RSS_FEEDS, config.NEWS_TOPICS = saved
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
