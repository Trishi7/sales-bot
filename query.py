"""Discord reading primitives (READ-ONLY), scoped to the sales channels.

Everything the bot knows about what the team has actually said comes from here:

  - scan_recent_messages()     — one person's recent posts,
  - resolve_person()           — a free-text name → a Discord identity,
  - person_recent_messages()   — the two above combined, as a tool result,
  - channel_recent_activity()  — a NON-person, time-window digest ("what
    happened in the last two days"),
  - search_channel_history()   — a KEYWORD search over a channel's history,
    returning each match WITH the messages around it. This is what answers "did
    we ever send Acme the pricing?" — the answer lives in what people said, and
    a follow-up posted without a reply-to is only interpretable next to the
    message it follows.

SCOPE. Every scan here iterates `guardrails.readable_channel_ids()` — the sales
channels and nothing else. No function takes a caller-supplied channel LIST, only
an optional name/id FILTER that narrows within that set, so a new caller cannot
widen the bot's field of view by passing a different list. A channel outside the
scope is invisible: not an error, simply not there.

Identity resolution is Discord-only. The PM bot resolved names against its issue
tracker first and used Discord as a fallback; this bot has no such system, so a
name is matched against the people who actually post in the sales channels plus
the cached guild members.

Nothing here mutates anything. Errors are logged and swallowed; helpers return
empty / None rather than raising, so one unreadable channel never takes down a
whole answer.
"""
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord

import config
import guardrails

log = logging.getLogger(__name__)


def _display(user) -> str:
    """Best display label for a Discord user/member (mirrors bot._display)."""
    return (
        getattr(user, "display_name", None)
        or getattr(user, "name", None)
        or str(user)
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# -- Discord activity scan ---------------------------------------------------


async def scan_recent_messages(
    client: discord.Client,
    *,
    author_id: Optional[int] = None,
    author_name: Optional[str] = None,
    days: int = config.QUERY_DISCORD_LOOKBACK_DAYS,
    max_per_channel: int = config.QUERY_MAX_MESSAGES_PER_CHANNEL,
) -> list[dict]:
    """Scan the SALES CHANNELS ONLY for recent messages by one person.

    The channel list comes from `guardrails.readable_channel_ids()` and is not a
    parameter — this function cannot be pointed at any other channel.

    Matching: prefer an exact `author_id`; when no id is given, fall back to a
    case-insensitive display-name match against `author_name`. At most
    `max_per_channel` messages are scanned per channel (newest first, going back
    `days`), bounding API cost. Channels the bot can't read are logged and
    skipped — never raises.

    Returns a small list of dicts across all channels, newest first:
        {channel, timestamp, text, jump_url, attachment_urls}
    """
    if author_id is None and not (author_name or "").strip():
        log.warning("[query.scan] called with neither author_id nor author_name; nothing to match")
        return []

    channel_ids = guardrails.readable_channel_ids()
    cutoff = _utcnow() - timedelta(days=max(0, int(days)))
    wanted_name = (author_name or "").strip().lower()
    log.info(
        "[query.scan] step 1/2: author_id=%s author_name=%r days=%d max_per_channel=%d channels=%d",
        author_id, author_name, days, max_per_channel, len(channel_ids),
    )

    out: list[dict] = []
    for cid in channel_ids:
        channel = client.get_channel(cid)
        if channel is None:
            log.debug("[query.scan] channel %s not visible to the bot; skip", cid)
            continue
        channel_name = getattr(channel, "name", str(cid))
        scanned = matched = 0
        try:
            async for msg in channel.history(
                after=cutoff, limit=int(max_per_channel), oldest_first=False
            ):
                scanned += 1
                author = msg.author
                if author is None:
                    continue
                if author_id is not None:
                    if getattr(author, "id", None) != author_id:
                        continue
                elif _display(author).strip().lower() != wanted_name:
                    continue
                matched += 1
                out.append(
                    {
                        "channel": channel_name,
                        "timestamp": msg.created_at,
                        "text": (msg.content or "").strip(),
                        "jump_url": msg.jump_url,
                        "attachment_urls": [a.url for a in msg.attachments],
                    }
                )
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning(
                "[query.scan] cannot read #%s (%s): %s — skip", channel_name, cid, type(e).__name__
            )
            continue
        log.debug(
            "[query.scan] #%s: scanned %d, matched %d", channel_name, scanned, matched
        )

    out.sort(key=lambda r: r["timestamp"], reverse=True)
    log.info("[query.scan] step 2/2: %d matching message(s) across all channels", len(out))
    return out


# -- identity resolution -----------------------------------------------------


def _match_discord_name(display: Optional[str], username: Optional[str], wanted: str) -> Optional[str]:
    """Return "exact" / "partial" / None for a Discord display/username vs. wanted."""
    for f in (display, username):
        fl = (f or "").strip().lower()
        if not fl:
            continue
        if fl == wanted:
            return "exact"
        first = fl.split()[0] if fl.split() else fl
        if first and first == wanted:
            return "exact"
    if len(wanted) >= 2:
        for f in (display, username):
            fl = (f or "").strip().lower()
            if fl and wanted in fl:
                return "partial"
    return None


def _discord_candidate(entry: dict) -> dict:
    return {
        "source": "discord",
        "id": entry.get("id"),
        "name": entry.get("name"),
        "display_name": entry.get("display_name"),
    }


def _lookup_discord_user(client: discord.Client, user_id: int) -> dict:
    """Best-effort {id, name, display_name} for a known Discord id, checking the
    user cache then guild members. Falls back to id-only when uncached."""
    user = client.get_user(user_id)
    if user is not None:
        return {"id": user_id, "name": getattr(user, "name", None), "display_name": _display(user)}
    for guild in getattr(client, "guilds", []) or []:
        member = guild.get_member(user_id)
        if member is not None:
            return {"id": user_id, "name": getattr(member, "name", None), "display_name": _display(member)}
    return {"id": user_id, "name": None, "display_name": None}


def _roster_candidates(wanted: str) -> list[dict]:
    """Roster entries matching `wanted`, from config.ROSTER_DISPLAY_NAMES.

    The candidate pool is built from who has POSTED recently, which misses a
    quiet teammate entirely. The roster fills that gap: someone on it can be
    named in an answer even if they haven't said anything in the window. It
    grants nothing — a roster hit still has to pass `guardrails.mention_for`
    before it can become a ping."""
    out: list[dict] = []
    for uid, display in (config.ROSTER_DISPLAY_NAMES or {}).items():
        name = str(display or "").strip()
        if not name or _match_discord_name(name, None, wanted) is None:
            continue
        try:
            out.append({"id": int(str(uid).strip()), "name": None, "display_name": name})
        except (TypeError, ValueError):
            log.warning("[query.resolve] ROSTER_DISPLAY_NAMES key %r is not a Discord id; skip", uid)
    return out


async def _collect_discord_pool(
    client: discord.Client, *, days: int, max_per_channel: int
) -> dict[int, dict]:
    """Pool of candidate Discord identities: recent posters in the SALES channels
    plus any cached guild members. id → {id, name, display_name}."""
    cutoff = _utcnow() - timedelta(days=max(0, int(days)))
    pool: dict[int, dict] = {}

    for cid in guardrails.readable_channel_ids():
        channel = client.get_channel(cid)
        if channel is None:
            continue
        try:
            async for msg in channel.history(
                after=cutoff, limit=int(max_per_channel), oldest_first=False
            ):
                a = msg.author
                if a is None or getattr(a, "bot", False):
                    continue
                pool.setdefault(
                    a.id, {"id": a.id, "name": getattr(a, "name", None), "display_name": _display(a)}
                )
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning("[query.resolve] cannot read channel %s (%s); skip", cid, type(e).__name__)
            continue

    for guild in getattr(client, "guilds", []) or []:
        for m in getattr(guild, "members", []) or []:
            if getattr(m, "bot", False):
                continue
            pool.setdefault(
                m.id, {"id": m.id, "name": getattr(m, "name", None), "display_name": _display(m)}
            )

    return pool


async def resolve_person(
    name: str,
    *,
    client: discord.Client,
    days: int = config.QUERY_DISCORD_LOOKBACK_DAYS,
    max_per_channel: int = config.QUERY_MAX_MESSAGES_PER_CHANNEL,
) -> dict:
    """Map a free-text `name` to a Discord identity.

    Discord-only: there is no second system to reconcile against here. `name` is
    matched, case-insensitively and allowing first-name / partial matches,
    against two pools:
      1. people who have POSTED in the sales channels recently, plus the cached
         guild members — the ones the bot has actually seen;
      2. the configured roster display names, so a teammate who has been quiet
         can still be resolved.

    Returns:
        {
          "query_name": str,
          "discord_user": {id, name, display_name} | None,
          "ambiguous": bool,
          "candidates": [ {source, id, name, display_name}, ... ],
        }

    When several people plausibly match, `ambiguous` is True, `discord_user` is
    left None and the options come back in `candidates` — so the caller asks
    which person rather than guessing. Guessing here would mean answering about,
    or nudging, the wrong human. Read-only; never raises.
    """
    result = {
        "query_name": name,
        "discord_user": None,
        "ambiguous": False,
        "candidates": [],
    }
    wanted = (name or "").strip().lower()
    if not wanted:
        log.info("[query.resolve] empty name; nothing to resolve")
        return result

    log.info("[query.resolve] step 1/2: building candidate pool for %r", name)
    try:
        pool = await _collect_discord_pool(client, days=days, max_per_channel=max_per_channel)
    except Exception:
        log.exception("[query.resolve] building the candidate pool raised; treating as empty")
        pool = {}

    # Roster entries fill in anyone who hasn't posted in the window. setdefault:
    # a live identity (real username, current display name) always wins over the
    # static roster copy of the same person.
    for entry in _roster_candidates(wanted):
        pool.setdefault(entry["id"], entry)

    exacts, partials = [], []
    for entry in pool.values():
        verdict = _match_discord_name(entry.get("display_name"), entry.get("name"), wanted)
        if verdict == "exact":
            exacts.append(entry)
        elif verdict == "partial":
            partials.append(entry)
    # An exact match beats any number of partials: "Sam" should resolve to Sam,
    # not become ambiguous because Samyuktha also matches as a substring.
    matches = exacts or partials

    if len(matches) == 1:
        result["discord_user"] = {
            "id": matches[0].get("id"),
            "name": matches[0].get("name"),
            "display_name": matches[0].get("display_name"),
        }
        log.info("[query.resolve] matched → %s", result["discord_user"].get("display_name"))
    elif len(matches) > 1:
        result["ambiguous"] = True
        result["candidates"] = [_discord_candidate(e) for e in matches]
        log.info("[query.resolve] %d matches for %r — ambiguous", len(matches), name)
    else:
        log.info("[query.resolve] no match for %r", name)

    log.info(
        "[query.resolve] step 2/2: DONE discord=%s ambiguous=%s candidates=%d",
        bool(result["discord_user"]), result["ambiguous"], len(result["candidates"]),
    )
    return result




# -- person recent Discord activity (read-only) -------------------------------

# Phrasing that signals a promised thing may already have been delivered — the
# sales version of a "done" signal. Used to flag messages worth reading first
# when answering "did that ever go out?", and to spot a chase that has quietly
# resolved itself.
_DONE_SIGNAL_RE = re.compile(
    r"\b(done|sent|shared|delivered|booked|scheduled|signed|closed|"
    r"went out|gone out|sent it over|shipped|live now|it'?s live|"
    r"they'?ve? (?:replied|responded|confirmed)|confirmed)\b",
    re.IGNORECASE,
)


def is_done_signal(text: str) -> bool:
    """True if a message reads like something promised has been delivered
    ("sent", "booked", "went out"). A hint for the reader, never a conclusion:
    "sent" in "haven't sent it yet" matches too, so the engine is told to read
    the message rather than trust the flag."""
    return bool(_DONE_SIGNAL_RE.search(text or ""))


async def person_recent_messages(
    client: discord.Client,
    *,
    name: str,
    days: int = config.QUERY_DISCORD_LOOKBACK_DAYS,
) -> dict:
    """Resolve `name` to a Discord identity and return their recent SALES-channel
    messages (newest first), each flagged with `done_signal`. Read-only.

    Returns {person, ambiguous, candidates, messages:[{channel, timestamp, text,
    jump_url, done_signal}]}. On an ambiguous name it returns the candidates and
    no messages, so the engine asks which person instead of answering about the
    wrong one. Never raises."""
    try:
        resolution = await resolve_person(name, client=client)
    except Exception:
        log.exception("[query.person_recent] resolve_person raised for %r", name)
        resolution = {"ambiguous": False, "candidates": [], "discord_user": None}

    if resolution.get("ambiguous"):
        return {
            "person": name,
            "ambiguous": True,
            "candidates": resolution.get("candidates", []),
            "messages": [],
        }

    du = resolution.get("discord_user") or {}
    scan_id = du.get("id")
    scan_name = None if scan_id else (du.get("display_name") or name)

    try:
        msgs = await scan_recent_messages(
            client, author_id=scan_id, author_name=scan_name, days=max(1, int(days))
        )
    except Exception:
        log.exception("[query.person_recent] scan_recent_messages raised for %r", name)
        msgs = []

    out: list[dict] = []
    for m in msgs[:25]:
        ts = m.get("timestamp")
        text = (m.get("text") or "").strip()
        snippet = text[:300] + ("…" if len(text) > 300 else "")
        out.append(
            {
                "channel": m.get("channel"),
                "timestamp": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
                "text": snippet,
                "jump_url": m.get("jump_url"),
                "done_signal": is_done_signal(text),
            }
        )

    return {
        "person": du.get("display_name") or name,
        "ambiguous": False,
        "candidates": [],
        "messages": out,
    }


# -- channel-wide Discord digest (read-only, NOT person-scoped) ---------------


def _channel_matches(channel, wanted: str) -> bool:
    """True if a SALES `channel` matches a user-supplied channel name or id.

    Accepts "#dev", "dev", a substring ("deploy" → #deployments), or the raw
    channel id. An empty/blank `wanted` matches every sales channel. This only
    NARROWS within the sales channels — it can never reach outside them.
    """
    w = (wanted or "").strip().lstrip("#").lower()
    if not w:
        return True
    if w == str(getattr(channel, "id", "")).lower():
        return True
    name = (getattr(channel, "name", "") or "").strip().lower()
    return bool(name) and (name == w or w in name)


async def channel_recent_activity(
    client: discord.Client,
    *,
    days: int = config.QUERY_CHANNEL_SCAN_DEFAULT_DAYS,
    channel: Optional[str] = None,
    max_per_channel: int = config.QUERY_MAX_MESSAGES_PER_CHANNEL,
    max_messages: int = config.QUERY_CHANNEL_DIGEST_MAX_MESSAGES,
) -> dict:
    """Digest of what happened in the SALES channels over the last `days`.

    The channel-wide counterpart to `person_recent_messages`: nobody is named, so
    this answers "what's been going on in Discord in the last 2 days". Optionally
    narrowed to one channel by name/id via `channel`.

    Bounds (all enforced here, never by the caller's honour system):
      - `days` is clamped to [1, config.QUERY_CHANNEL_SCAN_MAX_DAYS],
      - at most `max_per_channel` messages are read per channel (the same cap the
        person-scoped scan uses),
      - at most `max_messages` messages are RETURNED across all channels — but the
        per-channel and per-person COUNTS always reflect everything scanned, and
        `truncated` says whether the message list was cut.

    Bot messages and empty posts (no text, no attachment) are skipped. Channels
    the bot can't read are logged and skipped. Read-only; never raises.

    Returns {days, channel_filter, channels[], people[], message_count,
    returned, truncated, messages[{channel, author, timestamp, text, jump_url,
    done_signal}], note?}.
    """
    channel_ids = guardrails.readable_channel_ids()
    if not channel_ids:
        return {
            "enabled": False,
            "note": "No sales channels are configured (SALES_CHANNEL_IDS unset).",
        }

    try:
        days = int(days)
    except (TypeError, ValueError):
        days = config.QUERY_CHANNEL_SCAN_DEFAULT_DAYS
    days = max(1, min(int(config.QUERY_CHANNEL_SCAN_MAX_DAYS), days))
    cutoff = _utcnow() - timedelta(days=days)
    wanted_channel = (channel or "").strip()

    log.info(
        "[query.channel_scan] step 1/3: days=%d channel=%r max_per_channel=%d channels=%d",
        days, wanted_channel or "(all)", max_per_channel, len(channel_ids),
    )

    collected: list[dict] = []
    channel_rows: list[dict] = []
    people: dict[str, dict] = {}
    matched_any = False

    for cid in channel_ids:
        chan = client.get_channel(cid)
        if chan is None:
            log.debug("[query.channel_scan] channel %s not visible to the bot; skip", cid)
            continue
        if not _channel_matches(chan, wanted_channel):
            continue
        matched_any = True
        channel_name = getattr(chan, "name", str(cid))
        count = 0
        try:
            async for msg in chan.history(
                after=cutoff, limit=int(max_per_channel), oldest_first=False
            ):
                author = msg.author
                if author is None or getattr(author, "bot", False):
                    continue
                text = (msg.content or "").strip()
                attachments = [a.url for a in msg.attachments]
                if not text and not attachments:
                    continue
                count += 1
                who = _display(author)
                entry = people.setdefault(
                    who, {"person": who, "message_count": 0, "channels": []}
                )
                entry["message_count"] += 1
                if channel_name not in entry["channels"]:
                    entry["channels"].append(channel_name)
                collected.append(
                    {
                        "channel": channel_name,
                        "author": who,
                        "timestamp": msg.created_at,
                        "text": text,
                        "jump_url": msg.jump_url,
                        "attachment_count": len(attachments),
                        "done_signal": is_done_signal(text),
                    }
                )
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning(
                "[query.channel_scan] cannot read #%s (%s): %s — skip",
                channel_name, cid, type(e).__name__,
            )
            continue
        channel_rows.append({"channel": channel_name, "message_count": count})
        log.debug("[query.channel_scan] #%s: %d message(s)", channel_name, count)

    if wanted_channel and not matched_any:
        log.info("[query.channel_scan] no sales channel matched %r", wanted_channel)
        return {
            "days": days,
            "channel_filter": wanted_channel,
            "channels": [],
            "people": [],
            "message_count": 0,
            "returned": 0,
            "truncated": False,
            "messages": [],
            "note": (
                f"No SALES channel matches '{wanted_channel}'. The sales channels are "
                "the only ones I can read at all."
            ),
        }

    collected.sort(key=lambda r: r["timestamp"], reverse=True)
    total = len(collected)
    kept = collected[: max(1, int(max_messages))]

    messages = []
    for m in kept:
        ts = m["timestamp"]
        text = m["text"]
        snippet = text[:300] + ("…" if len(text) > 300 else "")
        row = {
            "channel": m["channel"],
            "author": m["author"],
            "timestamp": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
            "text": snippet,
            "jump_url": m["jump_url"],
            "done_signal": m["done_signal"],
        }
        if m["attachment_count"]:
            row["attachment_count"] = m["attachment_count"]
        messages.append(row)

    people_rows = sorted(
        people.values(), key=lambda r: r["message_count"], reverse=True
    )
    log.info(
        "[query.channel_scan] step 3/3: %d message(s) from %d person(s) across %d channel(s); returning %d",
        total, len(people_rows), len(channel_rows), len(messages),
    )

    out = {
        "days": days,
        "channel_filter": wanted_channel or None,
        "channels": channel_rows,
        "people": people_rows,
        "message_count": total,
        "returned": len(messages),
        "truncated": total > len(messages),
        "messages": messages,
    }
    if not total:
        out["note"] = (
            f"No messages in the sales channels in the last {days} day(s)."
        )
    elif out["truncated"]:
        out["note"] = (
            f"Showing the {len(messages)} most recent of {total} messages; the per-channel "
            "and per-person counts cover all of them."
        )
    return out


# -- channel history KEYWORD search (read-only, with context windows) ---------

# How much of a message body survives into the result. A match gets more room than
# a context line: the match is what the answer quotes, the context is only there to
# give it an antecedent. Both are hard truncations — this tool returns SNIPPETS,
# never a channel dump.
_HISTORY_MATCH_CHARS = 300
_HISTORY_CONTEXT_CHARS = 180


def _term_pattern(term: str) -> Optional[re.Pattern]:
    """Compile ONE search term to a case-insensitive pattern.

    Word-ish terms get \\b boundaries so "post" doesn't match "postpone"; terms
    that start/end with a non-word character (an issue key fragment, an emoji, a
    "5-picture" style token) fall back to a plain substring match.
    """
    t = (term or "").strip()
    if len(t) < 2:
        return None
    esc = re.escape(t)
    left = r"\b" if t[0].isalnum() else ""
    right = r"\b" if t[-1].isalnum() else ""
    try:
        return re.compile(left + esc + right, re.IGNORECASE)
    except re.error:
        log.warning("[query.history] term %r failed to compile; skipping", term)
        return None


def _history_row(msg, *, channel_name: str, limit: int) -> dict:
    """Compact, token-bounded row for one Discord message."""
    text = (msg.content or "").strip()
    snippet = text[:limit] + ("…" if len(text) > limit else "")
    row = {
        "channel": channel_name,
        "author": _display(msg.author),
        "timestamp": msg.created_at.isoformat(),
        "text": snippet,
        "jump_url": msg.jump_url,
    }
    attachments = len(getattr(msg, "attachments", []) or [])
    if attachments:
        row["attachment_count"] = attachments
    return row


async def search_channel_history(
    client: discord.Client,
    *,
    terms,
    channel: Optional[str] = None,
    days_back: int = 14,
    author: Optional[str] = None,
    context_window: int = 4,
    max_matches: int = config.QUERY_HISTORY_MAX_MATCHES,
    max_per_channel: int = config.QUERY_MAX_MESSAGES_PER_CHANNEL,
) -> dict:
    """KEYWORD-search a sales channel's message history, READ-ONLY.

    The counterpart to `channel_recent_activity`: that one answers "what happened
    lately" over a short window; this one answers "did we ever send Acme the
    pricing / what did we agree with them / has anyone spoken to them since" by
    looking further back for specific words. With no CRM wired up, what the team
    said in channel IS the record, so this is where those answers live.

    Each match comes back WITH `context_window` messages before and after it,
    which is what makes unreplied continuations usable: "they came back asking
    for a discount" with no reply-to is meaningless alone and decisive next to
    the message it follows.

    Bounds (enforced here, not by the caller):
      - `days_back` clamped to [1, config.QUERY_HISTORY_MAX_DAYS],
      - `context_window` clamped to [0, config.QUERY_HISTORY_MAX_CONTEXT],
      - at most `max_per_channel` messages READ per channel,
      - at most `max_matches` matches RETURNED (`match_count` still reports the
        true total, so truncation is visible),
      - every message body truncated to a snippet.

    `terms` may be a list or a space/comma-separated string; a message matches if
    ANY term appears in it. `author` (a display/user name, or "me" already
    substituted by the caller) restricts which messages count as MATCHES — the
    context around them still includes everyone, so "find MY report" also surfaces
    the replies to it.

    Returns {enabled, channels_searched[], days_back, terms[], author_filter,
    author_matched, match_count, returned, truncated, matches[], note?} where each
    match is {channel, author, timestamp, text, jump_url, done_signal,
    matched_terms[], context_before[], context_after[]}. Bots and empty messages
    are skipped. Never raises.
    """
    channel_ids = guardrails.readable_channel_ids()
    if not channel_ids:
        return {
            "enabled": False,
            "note": "No sales channels are configured (SALES_CHANNEL_IDS unset).",
        }

    if isinstance(terms, str):
        term_list = [t for t in re.split(r"[,\n]| {2,}", terms) if t.strip()]
        if len(term_list) <= 1:
            term_list = terms.split()
    else:
        term_list = [str(t) for t in (terms or [])]
    term_list = [t.strip() for t in term_list if str(t).strip()]
    patterns = [(t, p) for t in term_list if (p := _term_pattern(t)) is not None]
    if not patterns:
        return {
            "enabled": True,
            "error": "search_channel_history needs at least one term of 2+ characters.",
        }

    try:
        days_back = int(days_back)
    except (TypeError, ValueError):
        days_back = 14
    days_back = max(1, min(int(config.QUERY_HISTORY_MAX_DAYS), days_back))
    try:
        context_window = int(context_window)
    except (TypeError, ValueError):
        context_window = 4
    context_window = max(0, min(int(config.QUERY_HISTORY_MAX_CONTEXT), context_window))

    wanted_channel = (channel or "").strip()
    wanted_author = (author or "").strip().lower()
    cutoff = _utcnow() - timedelta(days=days_back)

    log.info(
        "[query.history] step 1/3: terms=%s channel=%r days_back=%d author=%r context=%d",
        term_list, wanted_channel or "(all sales channels)", days_back, author, context_window,
    )

    hits: list[dict] = []          # {row, term_hits, matched_terms, ts, before[], after[]}
    channels_searched: list[str] = []
    author_matched = False
    matched_any_channel = False

    for cid in channel_ids:
        chan = client.get_channel(cid)
        if chan is None:
            log.debug("[query.history] channel %s not visible to the bot; skip", cid)
            continue
        if not _channel_matches(chan, wanted_channel):
            continue
        matched_any_channel = True
        channel_name = getattr(chan, "name", str(cid))

        # Read newest-first (cheapest for Discord), then flip to chronological so a
        # match's neighbours are its real antecedents and continuations.
        window: list = []
        try:
            async for msg in chan.history(
                after=cutoff, limit=int(max_per_channel), oldest_first=False
            ):
                if msg.author is None or getattr(msg.author, "bot", False):
                    continue
                if not (msg.content or "").strip() and not msg.attachments:
                    continue
                window.append(msg)
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning(
                "[query.history] cannot read #%s (%s): %s — skip",
                channel_name, cid, type(e).__name__,
            )
            continue
        window.reverse()
        channels_searched.append(channel_name)

        for i, msg in enumerate(window):
            text = (msg.content or "").strip()
            matched_terms = [t for t, p in patterns if p.search(text)]
            if not matched_terms:
                continue
            if wanted_author:
                if _match_discord_name(
                    _display(msg.author), getattr(msg.author, "name", None), wanted_author
                ) is None:
                    continue
                author_matched = True
            row = _history_row(msg, channel_name=channel_name, limit=_HISTORY_MATCH_CHARS)
            row["done_signal"] = is_done_signal(text)
            row["matched_terms"] = matched_terms
            hits.append(
                {
                    "row": row,
                    "term_hits": len(matched_terms),
                    "ts": msg.created_at,
                    "before": [
                        _history_row(m, channel_name=channel_name, limit=_HISTORY_CONTEXT_CHARS)
                        for m in window[max(0, i - context_window):i]
                    ],
                    "after": [
                        _history_row(m, channel_name=channel_name, limit=_HISTORY_CONTEXT_CHARS)
                        for m in window[i + 1:i + 1 + context_window]
                    ],
                }
            )

    if wanted_channel and not matched_any_channel:
        log.info("[query.history] no sales channel matched %r", wanted_channel)
        return {
            "enabled": True,
            "channels_searched": [],
            "days_back": days_back,
            "terms": term_list,
            "author_filter": author or None,
            "match_count": 0,
            "returned": 0,
            "truncated": False,
            "matches": [],
            "note": (
                f"No SALES channel matches '{wanted_channel}'. The sales channels are the "
                "only ones I can read — retry without 'channel' to search all of them."
            ),
        }

    total = len(hits)
    # Keep the most on-topic (most distinct terms), breaking ties by recency — then
    # put what survives back in chronological order, so the answer reads as a story:
    # report → fix claim → investigation.
    hits.sort(key=lambda h: (h["term_hits"], h["ts"]), reverse=True)
    kept = hits[: max(1, int(max_matches))]
    kept.sort(key=lambda h: h["ts"])

    matches = []
    for h in kept:
        row = dict(h["row"])
        if h["before"]:
            row["context_before"] = h["before"]
        if h["after"]:
            row["context_after"] = h["after"]
        matches.append(row)

    out = {
        "enabled": True,
        "channels_searched": channels_searched,
        "days_back": days_back,
        "terms": term_list,
        "author_filter": author or None,
        "author_matched": bool(author_matched) if wanted_author else None,
        "context_window": context_window,
        "match_count": total,
        "returned": len(matches),
        "truncated": total > len(matches),
        "matches": matches,
    }
    if not total:
        scope = f"#{wanted_channel}" if wanted_channel else "the sales channels"
        who = f" from {author}" if wanted_author else ""
        out["note"] = (
            f"No message{who} in {scope} in the last {days_back} day(s) matched "
            f"{term_list}. Before concluding nothing was reported, try broader/other "
            "terms, drop the author filter, or widen days_back "
            f"(max {config.QUERY_HISTORY_MAX_DAYS})."
        )
    elif out["truncated"]:
        out["note"] = (
            f"Showing {len(matches)} of {total} matching messages (the most on-topic, in "
            "date order); narrow the terms or the channel to see different ones."
        )
    log.info(
        "[query.history] step 3/3: %d match(es) across %d channel(s); returning %d",
        total, len(channels_searched), len(matches),
    )
    return out
