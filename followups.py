"""CHASES — the sales commitments this bot is waiting on.

Deadline chasing is the job. Sales work is a stream of promises made in passing —
"I'll send Acme the deck tomorrow", "will follow up with them after the call",
"pricing goes out by EOD" — each with an implied due time and nobody tracking it.
A CHASE is one such promise, and this module holds the pieces for spotting them
and for nudging on them:

  - `looks_like_commitment()` — a cheap keyword prefilter so the vast majority of
    channel chatter never reaches the LLM. Only what survives it is worth a call.
  - `due_at_from()` — turns the model's "how long did they give themselves"
    estimate into an absolute UTC due time, clamped to sane bounds.
  - `fallback_nudge()` — the deterministic reminder text, used when the model call
    for the persona-voiced nudge fails. The nudge must still go out: a chase that
    silently doesn't fire is the one failure mode this feature cannot have.

STRICTLY READ/REMIND ONLY. A chase's only possible effect is a REMINDER addressed
to a human, posted in the sales channel the promise was made in. It never
contacts a customer, never sends anything on anyone's behalf, and never leaves
Discord. It is capped by COS_NUDGE_WINDOW_HOURS / COS_NUDGE_MAX_ATTEMPTS
(enforced in db.py + bot.py), so the chasing is bounded from day one.
"""
import logging
import re
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

# UTC 'YYYY-MM-DD HH:MM:SS' — the same shape SQLite's CURRENT_TIMESTAMP writes,
# so stored timestamps sort and compare correctly as plain strings.
TS_FORMAT = "%Y-%m-%d %H:%M:%S"

# Bounds on a promise's due time. "Give me 2 minutes" isn't worth tracking as a
# chase. The upper bound is TWO WEEKS here rather than the PM bot's one: sales
# commitments legitimately run to "I'll circle back after their board meeting
# next Thursday", and clamping that to a week would fire the nudge early — which
# reads as nagging and gets the bot muted.
MIN_DUE_MINUTES = 15
MAX_DUE_MINUTES = 14 * 24 * 60


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_ts(dt: datetime) -> str:
    """A datetime → the UTC string form used in the DB."""
    return dt.astimezone(timezone.utc).strftime(TS_FORMAT)


def from_ts(ts: str) -> datetime:
    """A stored UTC string → an aware datetime. Falls back to `now` on a malformed
    value so a bad row can never crash the sweeper."""
    try:
        return datetime.strptime(str(ts), TS_FORMAT).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        log.warning("[followups] unparseable timestamp %r; treating as now", ts)
        return now_utc()


# First-person future commitment markers. Deliberately BROAD (cheap to run, and a
# false positive only costs one small LLM call, which then rejects it) but still
# anchored on someone speaking about their OWN next action — "i'll", "we'll",
# "will get", "let me check", "by tomorrow", "once the deploy lands".
_COMMITMENT_PATTERNS = [
    r"\bi(?:'| a)?ll\b",              # I'll / Ill / I will (contraction forms)
    r"\bwe(?:'| wi)?ll\b",            # we'll / we will
    r"\bi (?:will|can|shall)\b",
    r"\bwe (?:will|can|shall)\b",
    r"\bwill (?:confirm|update|check|share|send|get|let you know|revert|look)\b",
    r"\b(?:let me|lemme) (?:check|confirm|look|see|find out|get)\b",
    r"\b(?:give me|gimme) (?:a|an|\d+)\b",
    r"\bget back to (?:you|u|ya)\b",
    r"\bkeep you posted\b",
    r"\brevert(?:ing)? (?:on|with|back)\b",   # Indian-English "I'll revert on this"
    r"\bon it\b",
    r"\bwill do\b",
    r"\bshortly\b",
    # SALES commitments — the shapes this bot actually chases. Someone owing a
    # deck, a quote, a follow-up email or a call booking is the bread and butter
    # here, in place of the PM bot's deploy/release vocabulary.
    r"\b(?:i|we)(?:'ll| will| can)? ?(?:send|share|email|mail|forward|deliver)\b",
    r"\bwill (?:send|share|email|forward|follow up|reach out|circle back|ping|call)\b",
    r"\b(?:follow(?:ing)? up|circle back|reach out|touch base|check in) (?:with|on|to|after|by|tomorrow|today)\b",
    r"\b(?:i|we)(?:'ll| will)? ?(?:set ?up|schedule|book|arrange) (?:a|the|another)?\s*"
    r"(?:call|meeting|demo|intro|sync)\b",
    r"\b(?:send|share|get) (?:them|him|her|it|you|over) the (?:deck|proposal|quote|pricing|contract|deal memo|invoice|numbers|details)\b",
    r"\b(?:deck|proposal|quote|pricing|contract|invoice|numbers|list) (?:will|is|are|goes?) (?:be )?(?:out|ready|sent|going|coming)\b",
    r"\bby (?:eod|eow|cob|tomorrow|tonight|today|monday|tuesday|wednesday|thursday|friday|next week)\b",
    r"\bin (?:a|an|\d+)\s*(?:min|mins|minute|minutes|hour|hours|hr|hrs|day|days|week|weeks)\b",
    r"\b(?:after|once) (?:i|we|the|they|their) \b",
    # Delivery-ish commitments people still make about launches/campaigns.
    r"\bgoing live\b",
    r"\bgo(?:es)? live\b",
    r"\bwill (?:be )?(?:live|out|ready|done|sent)\b",
    r"\b(?:i|we)(?:'ll| will| can)? ?(?:launch|publish|post|roll(?:ing)? out)\b",
]
_COMMITMENT_RE = re.compile("|".join(_COMMITMENT_PATTERNS), re.IGNORECASE)

# A promise needs enough words to carry a WHAT. "ok will do" alone is an ack, not
# something we can meaningfully chase.
_MIN_COMMITMENT_CHARS = 12


def looks_like_commitment(text: str) -> bool:
    """Cheap gate before the LLM: could this message plausibly be someone promising
    to come back with something? Tuned to over-accept — the model makes the real
    call, and a message that never reaches it can never become a chase."""
    t = (text or "").strip()
    if len(t) < _MIN_COMMITMENT_CHARS:
        return False
    return bool(_COMMITMENT_RE.search(t))


def due_at_from(due_minutes, *, default_minutes: int, promised_at: datetime) -> datetime:
    """When we may first nudge about a promise made at `promised_at`.

    `due_minutes` is the model's read of the deadline the person gave themselves
    ("in an hour" → 60, "by EOD" → whatever's left of the day). None/unusable —
    they promised without a time ("will update after testing") — falls back to
    `default_minutes` (COS_FOLLOWUP_DEFAULT_DUE_MINUTES). Clamped to
    [MIN_DUE_MINUTES, MAX_DUE_MINUTES] so a bad estimate can't schedule a nudge 90
    seconds or 3 months from now."""
    try:
        minutes = int(due_minutes)
    except (TypeError, ValueError):
        minutes = int(default_minutes)
    if minutes <= 0:
        minutes = int(default_minutes)
    minutes = max(MIN_DUE_MINUTES, min(MAX_DUE_MINUTES, minutes))
    return promised_at + timedelta(minutes=minutes)


def humanize_age(since: datetime, *, until: datetime = None) -> str:
    """"about an hour ago", "3 days ago" — how we refer to when the promise was
    made. Approximate on purpose: the nudge is a human reminder, not a stopwatch."""
    delta = (until or now_utc()) - since
    minutes = max(0, int(delta.total_seconds() // 60))
    if minutes < 90:
        return "about an hour ago" if minutes >= 40 else f"{max(1, minutes)} minutes ago"
    hours = minutes // 60
    if hours < 24:
        return f"about {hours} hours ago"
    days = hours // 24
    return "yesterday" if days == 1 else f"{days} days ago"


def mention_or_name(person_id, person_name: str) -> str:
    """How to address the person in a nudge.

    Delegates to `guardrails.mention_for`, which is the ONE place a mention token
    is produced: an @-mention only for someone on the team roster, a plain-text
    name for anyone else. Deliberately NOT `f"<@{id}>"` — a chase must never be
    the thing that pings a person this bot has no business pinging."""
    import guardrails  # local import: guardrails imports discord, this module is pure

    return guardrails.mention_for(person_id, person_name)


def fallback_nudge(item: dict) -> str:
    """The reminder text when the persona-voiced model call fails. Still first
    person, still a question — never a status line the person can ignore."""
    who = mention_or_name(item.get("person_id"), item.get("person_name", ""))
    what = (item.get("what") or "").strip() or "something you were going to come back on"
    when = humanize_age(from_ts(item.get("promised_at", "")))
    jump = item.get("jump_url") or ""
    tail = f" ({jump})" if jump else ""
    return f"{who} — you mentioned {what} {when}. Any update?{tail}"
