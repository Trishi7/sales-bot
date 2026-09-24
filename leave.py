"""WHO IS OFF TODAY — read from the leave channel, classified by the model.

WHY THIS EXISTS. Addressing somebody who is on holiday is how a nudge becomes
noise they come back to a week later, and it is worse than not sending: the item
still needs doing, nobody has been told, and the bot looks like it is not paying
attention. So before the bot addresses an owner it asks whether that person said
they were off.

THE LEAVE CHANNEL IS THE ONE NON-SALES CHANNEL THIS BOT MAY READ, and it is
read-only in the strongest sense available here: `guardrails.may_read` admits it,
`guardrails.send` does not, and `read_leave_posts` refuses any channel that is
not HOLIDAY_CHANNEL_ID. Three separate checks, because "read-only" asserted in a
comment is not read-only.

HOW LEAVE IS DETECTED. The same approach the PM bot uses: take the recent posts
in that channel and have the model classify them, rather than pattern-matching
for "OOO" and "on leave". People announce leave in prose — *"heading out from
Thursday, back Monday"*, *"wfh but offline after 2"*, *"taking tomorrow off"* —
and a regex over that either misses most of it or fires on "I am off to the
client meeting". The model gets the post and today's date and answers one
question per person: are they away TODAY.

    IT FAILS OPEN, TO "EVERYBODY IS IN". An unreadable channel, a model outage
    or an unparseable answer all resolve to "nobody is on leave", and the caller
    addresses the owner as usual. That is the recoverable direction: the cost is
    one nudge to somebody who is away, and the cost of failing closed would be
    re-addressing every item to Vaishnavi whenever the API blinked — which is
    both wrong and invisible.

THE FALLBACK ORDER IS A CHAIN, NOT A SWAP. Owner on leave -> the first name in
LEAVE_FALLBACK_ORDER; that person on leave too -> the next; and so on. THE LAST
NAME IS NEVER TREATED AS ON LEAVE. Sid is the backstop — not because he is never
away, but because an item addressed to nobody is an item nobody chases, and the
bot does not get to decide there is no one left to tell.

NOTHING HERE SENDS OR WRITES. It reads one channel, asks the model, and caches
the answer for HOLIDAY_CACHE_MINUTES.
"""
import json
import logging
import re
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import config
import deadlines as dl

log = logging.getLogger(__name__)

# The prompt is deliberately narrow: one question, one JSON shape, no prose. A
# classifier that is allowed to explain itself is a classifier that sometimes
# returns an explanation instead of an answer.
LEAVE_PROMPT = """You are reading a team's LEAVE / OUT-OF-OFFICE channel and working
out WHO IS AWAY ON ONE SPECIFIC DAY.

You will be given today's date and a list of recent posts, each with its author
and the date it was posted.

For each post, decide whether it says its author (or somebody the post names) is
AWAY ON THE GIVEN DAY. Away means: on leave, on holiday, off sick, travelling and
unreachable, or otherwise not working that day.

NOT away:
- working from home, or remote. That is working.
- in meetings, heads-down, or busy. That is working.
- away on a date that is NOT the given day. "Off next Friday" posted today does
  not make them off today.
- a post about somebody being back ("back from leave") — that says they are IN.
- somebody else's leave mentioned in passing without saying it is happening now.

RESOLVE RELATIVE DATES against the post's own date, not against today.
"Tomorrow" in a post from the 3rd means the 4th. A range ("out Thu-Mon")
includes every day in it, and you should assume the nearest sensible year.

Output JSON ONLY, no prose, no fences:

{"away": [{"name": "<the person's name or display name, as written>",
           "from": "<YYYY-MM-DD or "">",
           "until": "<YYYY-MM-DD or "">",
           "quote": "<the few words that say so>"}]}

An empty list is a perfectly good answer and the most common one. DO NOT GUESS:
if a post is ambiguous about whether it covers the given day, leave the person
out. A missed leave costs one nudge; a wrong one silently redirects somebody's
work to a colleague."""


_lock = threading.RLock()
# {iso_date: {"at": datetime, "away": {normalised name: {...}}}}
_cache: dict = {}


def _now() -> datetime:
    """Now, in UTC, through the bot's clock — the leave lookback is an "N days
    ago" window over the leave channel, so it moves with a pretend day."""
    import clock
    return clock.now_ist().astimezone(timezone.utc)


def _norm(name: str) -> str:
    """A person's name as a comparable key. Display names drift in case and
    punctuation; "Vaishnavi R." and "vaishnavi r" are one person."""
    v = re.sub(r"[^a-z0-9]+", " ", str(name or "").strip().lower())
    return re.sub(r"\s+", " ", v).strip()


def configured() -> bool:
    return bool(int(getattr(config, "HOLIDAY_CHANNEL_ID", 0) or 0))


async def read_leave_posts(client, *, days: Optional[int] = None) -> list:
    """Recent posts from the leave channel, as [{author, author_id, date, text}].

    REFUSES ANY CHANNEL THAT IS NOT HOLIDAY_CHANNEL_ID, by id, before it reads a
    single message — so a mis-set id reads the wrong channel rather than reading
    every channel. `guardrails.may_read` is asked as well: two gates, because
    this is the one place the bot reads outside the sales channels at all.

    [] on any failure. The caller treats that as "nobody is on leave".
    """
    import guardrails

    if not configured():
        return []
    channel_id = int(config.HOLIDAY_CHANNEL_ID)
    if not guardrails.is_leave_channel(channel_id) or not guardrails.may_read(channel_id):
        log.error(
            "[leave] channel %s is not the configured leave channel; refusing to read it",
            channel_id,
        )
        return []

    channel = client.get_channel(channel_id)
    if channel is None:
        log.warning(
            "[leave] the leave channel %s is not visible to the bot. Either the id is "
            "wrong or the bot's role lacks View Channel on it — the server-side scope is "
            "the outer layer and it is doing its job if this is deliberate. Everybody "
            "will be treated as IN.", channel_id,
        )
        return []

    lookback = max(1, int(days or config.HOLIDAY_LOOKBACK_DAYS))
    after = _now() - timedelta(days=lookback)
    out: list = []
    try:
        async for msg in channel.history(limit=200, after=after, oldest_first=False):
            text = (msg.content or "").strip()
            if not text:
                continue
            author = getattr(msg.author, "display_name", "") or getattr(msg.author, "name", "")
            out.append({
                "author": author,
                "author_id": int(getattr(msg.author, "id", 0) or 0),
                "date": msg.created_at.date().isoformat() if msg.created_at else "",
                "text": text[:400],
            })
    except Exception:
        log.exception("[leave] could not read the leave channel; treating everyone as IN")
        return []
    log.info("[leave] read %d post(s) from the leave channel (last %d days)",
             len(out), lookback)
    return out


def _parse_reply(raw: str) -> list:
    """The model's JSON, or [] when it is not usable."""
    if not raw:
        return []
    text = raw.strip()
    fence = re.search(r"\{.*\}", text, re.DOTALL)
    if not fence:
        return []
    try:
        data = json.loads(fence.group(0))
    except (ValueError, TypeError):
        log.warning("[leave] the classifier returned unparseable JSON; treating everyone as IN")
        return []
    away = data.get("away")
    return away if isinstance(away, list) else []


def _covers(entry: dict, day: date) -> bool:
    """Does this away-entry cover `day`?

    A blank range means the model said "away" without saying when, and that is
    taken as today only — the post is recent and the question it was asked was
    about today. Widening a vague answer into a week would redirect somebody's
    work on the strength of a guess.
    """
    start = dl.parse_date(str(entry.get("from") or ""))
    end = dl.parse_date(str(entry.get("until") or ""))
    if start is None and end is None:
        return True
    if start is None:
        return day <= end
    if end is None:
        return day == start
    return start <= day <= end


async def who_is_away(client, llm, *, today: Optional[date] = None,
                      force: bool = False) -> dict:
    """{normalised name: {name, from, until, quote}} for the people off today.

    Cached for HOLIDAY_CACHE_MINUTES: the channel changes a few times a week,
    and re-reading it for every addressed owner in a post would be several
    history scans for one message.

    FAILS OPEN, to {}. See the module header.
    """
    day = today or dl.today_ist()
    key = dl.iso(day)
    ttl = max(1, int(config.HOLIDAY_CACHE_MINUTES)) * 60

    with _lock:
        hit = _cache.get(key)
        if hit and not force and (_now() - hit["at"]).total_seconds() < ttl:
            return dict(hit["away"])

    if not configured():
        return {}

    posts = await read_leave_posts(client)
    if not posts:
        with _lock:
            _cache[key] = {"at": _now(), "away": {}}
        return {}

    if llm is None:
        log.info("[leave] no model available; treating everyone as IN")
        return {}

    lines = "\n".join(
        f"[{p['date']}] {p['author']}: {p['text']}" for p in posts
    )
    prompt = f"Today is {key} ({day.strftime('%A')}).\n\nPosts:\n{lines}"
    try:
        raw = await llm.classify_leave(prompt=prompt)
    except Exception:
        log.exception("[leave] the classifier failed; treating everyone as IN")
        return {}

    away: dict = {}
    for entry in _parse_reply(raw):
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if not name or not _covers(entry, day):
            continue
        away[_norm(name)] = {
            "name": name,
            "from": str(entry.get("from") or ""),
            "until": str(entry.get("until") or ""),
            "quote": str(entry.get("quote") or "")[:200],
        }

    with _lock:
        _cache[key] = {"at": _now(), "away": dict(away)}
    if away:
        log.info(
            "[leave] %s: %d person(s) away — %s",
            key, len(away),
            ", ".join(f"{v['name']} ({v['quote'] or 'no quote'})" for v in away.values()),
        )
    else:
        log.info("[leave] %s: nobody is recorded as away", key)
    return dict(away)


def is_away(name: str, away: dict) -> bool:
    """Is this person in the away map? Matched on the normalised name, and also
    on a first-name-only basis — the sheet writes "Vaishnavi" and Discord may
    say "Vaishnavi R.", and refusing to match those would make the check useless
    against exactly the names it exists to check."""
    key = _norm(name)
    if not key:
        return False
    if key in away:
        return True
    first = key.split(" ")[0]
    return any(k == first or k.split(" ")[0] == first for k in away)


def address_to(owner: str, away: dict) -> tuple:
    """(who to address, why) for one item's owner.

    THE CHAIN: owner -> LEAVE_FALLBACK_ORDER[0] -> [1] -> ... and the LAST name
    in that order is never treated as on leave. Returns the owner unchanged and
    an empty reason when nobody is away, which is the common case and must cost
    nothing to read.
    """
    order = [n for n in (config.LEAVE_FALLBACK_ORDER or []) if str(n).strip()]
    if not away or not is_away(owner, away):
        return owner, ""

    trail = [owner]
    for i, candidate in enumerate(order):
        last = i == len(order) - 1
        # THE BACKSTOP IS NEVER AWAY. Somebody has to be addressable.
        if last or not is_away(candidate, away):
            why = (
                " -> ".join(trail)
                + f" is on leave today, so this goes to {candidate}"
                + (" (the backstop, who is never treated as away)" if last and
                   is_away(candidate, away) else "")
            )
            return candidate, why
        trail.append(candidate)

    return owner, ""


def status() -> dict:
    """What the leave check is working from, for the boot report."""
    return {
        "channel_id": int(getattr(config, "HOLIDAY_CHANNEL_ID", 0) or 0),
        "configured": configured(),
        "lookback_days": int(config.HOLIDAY_LOOKBACK_DAYS),
        "cache_minutes": int(config.HOLIDAY_CACHE_MINUTES),
        "fallback_order": list(config.LEAVE_FALLBACK_ORDER or []),
    }


def _self_test() -> int:
    """`python -m leave` — the chain and the date-cover rules, no network."""
    logging.basicConfig(level=logging.WARNING, format="%(levelname)-7s %(name)s: %(message)s")
    failures = 0

    def check(name, got, want):
        nonlocal failures
        ok = got == want
        failures += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")

    config.LEAVE_FALLBACK_ORDER = ["Vaishnavi", "Sid"]
    none: dict = {}
    kushal = {"kushal": {"name": "Kushal", "from": "", "until": "", "quote": "off today"}}
    both = dict(kushal)
    both["vaishnavi"] = {"name": "Vaishnavi", "from": "", "until": "", "quote": "on leave"}
    all3 = dict(both)
    all3["sid"] = {"name": "Sid", "from": "", "until": "", "quote": "away"}

    print("the fallback chain")
    check("nobody away -> the owner", address_to("Kushal", none)[0], "Kushal")
    check("owner away -> Vaishnavi", address_to("Kushal", kushal)[0], "Vaishnavi")
    check("owner and Vaishnavi away -> Sid", address_to("Kushal", both)[0], "Sid")
    check("Sid is never treated as away", address_to("Kushal", all3)[0], "Sid")
    check("...and the reason says so",
          "backstop" in address_to("Kushal", all3)[1], True)
    check("Vaishnavi away, she is the owner -> Sid",
          address_to("Vaishnavi", both)[0], "Sid")
    check("a reason is given when it redirects",
          "on leave today" in address_to("Kushal", kushal)[1], True)
    check("no reason when it does not", address_to("Kushal", none)[1], "")

    print("\nname matching")
    check("exact", is_away("Kushal", kushal), True)
    check("case and punctuation", is_away("kushal.", kushal), True)
    check("display name with a surname", is_away("Kushal M", kushal), True)
    check("somebody else", is_away("Trishi", kushal), False)
    check("empty", is_away("", kushal), False)

    print("\nwhich days an entry covers")
    d = date(2026, 9, 21)
    check("no dates -> today only", _covers({}, d), True)
    check("a range that includes today",
          _covers({"from": "2026-09-20", "until": "2026-09-22"}, d), True)
    check("a range that does not",
          _covers({"from": "2026-09-22", "until": "2026-09-24"}, d), False)
    check("a single day that matches", _covers({"from": "2026-09-21"}, d), True)
    check("a single day that does not", _covers({"from": "2026-09-22"}, d), False)
    check("an open end that includes today", _covers({"until": "2026-09-30"}, d), True)

    print("\nparsing the classifier")
    check("clean JSON",
          [e["name"] for e in _parse_reply('{"away":[{"name":"Kushal"}]}')], ["Kushal"])
    check("JSON in a fence",
          [e["name"] for e in _parse_reply('```json\n{"away":[{"name":"Sid"}]}\n```')],
          ["Sid"])
    check("an empty list is a fine answer", _parse_reply('{"away":[]}'), [])
    check("prose is not", _parse_reply("nobody is away today"), [])
    check("broken JSON fails open", _parse_reply('{"away": [oops}'), [])
    check("nothing at all", _parse_reply(""), [])

    print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
