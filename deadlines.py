"""DEADLINE AUTHORITY — the bot sets dates rather than asking for them.

Sanctioned by Kushal: when someone asks about a deadline that doesn't exist, the
bot does not shrug and it does not ask "what date would you like?". It SETS one,
announces it in-channel, and invites objection:

    No deadline was set — setting the follow-up for Emami to Tue 26 Aug
    (outreach follow-up: 3 working days after the last touch).
    @Kushal @Vaishnavi shout to change.

That is the whole design intent: a default that someone can push back on beats an
empty cell nobody owns. "Shout to change" is not politeness — it is the consent
mechanism, which is why the announcement is mandatory and why the notify list
being empty is a startup warning.

WHERE THE DATE COMES FROM, in order:
  1. The STRATEGY DOC's cadence, when that document is readable. The team's own
     written cadence outranks anything hard-coded here.
  2. The defaults, in WORKING DAYS, IST: OUTREACH_FOLLOWUP_DAYS (3),
     REPLY_CHASE_DAYS (2), MEETING_PREP_DAYS (1).

Working days and IST are both deliberate. A follow-up due "in 3 days" set on a
Thursday should not land on Sunday, and the team works in IST, so a date computed
in UTC would drift a day for anything set after 18:30 local.

SQLITE IS AUTHORITATIVE, the sheet is a mirror. A deadline exists once it is in
the `deadlines` table; the write into the sheet's "Next Deadline (bot)" column
can fail without the deadline being lost. And a human-entered date in the
ORIGINAL always wins: `adopt_human_date` takes it over, and the bot never
overwrites it.
"""
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import config

log = logging.getLogger(__name__)

# The team works in IST. Every deadline date is a CALENDAR DATE in this zone.
IST = timezone(timedelta(hours=5, minutes=30), name="IST")

# The kinds of deadline the bot can set, with the env knob and the human-readable
# rule text that goes into the announcement (people accept a date far more
# readily when they can see the rule that produced it).
KIND_OUTREACH = "outreach_followup"
KIND_REPLY = "reply_chase"
KIND_MEETING_PREP = "meeting_prep"

KINDS: dict[str, dict] = {
    KIND_OUTREACH: {
        "label": "follow-up",
        "days_attr": "OUTREACH_FOLLOWUP_DAYS",
        "rule": "outreach follow-up: {n} working day(s) after the last touch",
    },
    KIND_REPLY: {
        "label": "reply chase",
        "days_attr": "REPLY_CHASE_DAYS",
        "rule": "reply chase: {n} working day(s) after we wrote",
    },
    KIND_MEETING_PREP: {
        "label": "meeting prep",
        "days_attr": "MEETING_PREP_DAYS",
        "rule": "meeting prep: {n} working day(s) before the meeting",
    },
}

# Date formats seen in the sheet, most explicit first. Sales sheets are typed by
# hand, so this is tolerant on purpose — but it never GUESSES between
# day-first and month-first: an unparseable date is returned as None and the
# answer says the cell couldn't be read, which is safer than a date six months
# out.
_DATE_FORMATS = (
    "%Y-%m-%d", "%Y/%m/%d",
    "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y",
    # Month-name forms with a HYPHEN or SLASH separator. "17-Mar-2026" is how the
    # real GTM tracker writes dates, and omitting it made every date in the sheet
    # unreadable — which in turn made 756 of 886 rows look like they had no dates
    # at all. Worth being generous here.
    "%d-%b-%Y", "%d-%B-%Y", "%d/%b/%Y", "%d/%B/%Y",
    "%d-%b-%y", "%d-%B-%y",
    "%b-%d-%Y", "%B-%d-%Y",
    "%d %b %Y", "%d %B %Y", "%b %d %Y", "%B %d %Y",
    "%d %b, %Y", "%d %B, %Y", "%b %d, %Y", "%B %d, %Y",
    "%d/%m/%y", "%d-%m-%y", "%d %b %y", "%d %B %y",
    # Month-and-year only ("Mar 2026"): treated as the 1st, since a follow-up
    # cadence needs *a* day and the 1st is the only non-arbitrary choice.
    "%b-%Y", "%B-%Y", "%b %Y", "%B %Y",
)
# Trailing ordinal suffixes ("21st Aug 2026") that strptime can't handle.
_ORDINAL_RE = re.compile(r"(\d{1,2})(st|nd|rd|th)\b", re.IGNORECASE)


def today_ist() -> date:
    """Today's calendar date in IST — the reference point for every deadline."""
    return datetime.now(IST).date()


def now_ist() -> datetime:
    return datetime.now(IST)


def is_working_day(d: date) -> bool:
    """Mon–Fri. Public holidays aren't modelled: the team's holiday list isn't
    anywhere the bot can read, and treating a holiday as a working day only makes
    a reminder a day early, which someone can shout about."""
    return d.weekday() < 5


def add_working_days(start: date, n: int) -> date:
    """`n` working days after `start`, skipping weekends.

    n=0 returns `start` itself, rolled forward to the next working day if it
    falls on a weekend — a deadline is never set for a Saturday.
    """
    d = start
    if n <= 0:
        while not is_working_day(d):
            d += timedelta(days=1)
        return d
    remaining = int(n)
    while remaining > 0:
        d += timedelta(days=1)
        if is_working_day(d):
            remaining -= 1
    return d


def ist_date_of(value, *, default: Optional[date] = None) -> Optional[date]:
    """The IST CALENDAR DATE of a stored timestamp, whatever zone it carries.

    THE PROBLEM THIS SOLVES IS A DAY-BOUNDARY ONE. Everything this bot schedules
    is reckoned in IST, and IST is UTC+5:30 — so the 5.5 hours either side of
    midnight are exactly where "which day is this?" has two answers. A proposal
    made at 00:30 IST was made at 19:00 UTC THE PREVIOUS DAY, and a comparison
    that took the date off the front of a UTC string would call it yesterday's.

    WHY NOT `substr(created_at, 1, 10)` IN SQL. That reads the first ten
    characters and calls them the date. It is right only while every writer
    happens to store IST — which is today's convention (`dl.now_ist()`) but is
    not enforced anywhere, and a single caller using `datetime.now(timezone.utc)`
    would silently shift every comparison by up to 5.5 hours with nothing to
    show for it. Parsing the offset costs a few microseconds on a handful of
    rows and cannot be wrong.

    ACCEPTS three shapes:
      - offset-aware ISO: "2026-09-22T00:30:00+05:30", "2026-09-21T19:00:00Z"
      - naive ISO:        "2026-09-22T00:30:00"  -> ASSUMED IST, the convention
                          every writer in this codebase follows
      - a bare date:      "2026-09-22"

    Returns `default` (None unless given) for anything unparseable, so a
    malformed row degrades to "not matched" rather than raising inside a sweep.
    """
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)
        return dt.astimezone(IST).date()
    if isinstance(value, date):
        return value

    text = str(value or "").strip()
    if not text:
        return default
    # "...Z" is UTC; fromisoformat only learned to read it in 3.11.
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        # A bare date, or something we cannot read.
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return default
    if dt.tzinfo is None:
        # NAIVE MEANS IST HERE. Every timestamp this bot writes to SQLite comes
        # from `now_ist()`, and reading a naive one as UTC would move it 5.5
        # hours backwards — the exact error this function exists to prevent.
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST).date()


def subtract_working_days(start: date, n: int) -> date:
    """`n` working days BEFORE `start`, skipping weekends.

    The mirror of `add_working_days`, and the one the proposal sweep needs: "was
    this proposed more than one working day ago" must not count a weekend
    nobody worked. A proposal made on Friday afternoon is not stale on Monday
    morning — it is stale on Tuesday.

    n=0 returns `start` itself, rolled BACK to the previous working day when it
    falls on a weekend.
    """
    d = start
    if n <= 0:
        while not is_working_day(d):
            d -= timedelta(days=1)
        return d
    remaining = int(n)
    while remaining > 0:
        d -= timedelta(days=1)
        if is_working_day(d):
            remaining -= 1
    return d


def previous_working_day(d: date) -> date:
    """The working day before `d` — when the "one day before due" reminder
    fires. Friday for a Monday deadline, never Sunday."""
    out = d - timedelta(days=1)
    while not is_working_day(out):
        out -= timedelta(days=1)
    return out


def working_days_between(start: date, end: date) -> int:
    """Working days from `start` to `end`, negative when `end` is in the past.
    Used to decide whether a row is STALLED."""
    if end == start:
        return 0
    step = 1 if end > start else -1
    d, count = start, 0
    while d != end:
        d += timedelta(days=step)
        if is_working_day(d):
            count += step
    return count


def parse_date(value) -> Optional[date]:
    """A sheet cell → a date, or None when it can't be read confidently.

    Ambiguous numeric dates are read DAY-FIRST (the team is in India and the
    sheet is typed by hand), which is why "03/04/2026" is 3 April. Anything that
    doesn't match a known format returns None rather than a guess.
    """
    s = str(value or "").strip()
    if not s:
        return None
    s = _ORDINAL_RE.sub(r"\1", s)
    s = s.replace("–", "-").replace("—", "-")
    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(s, fmt).date()
        except ValueError:
            continue
        # A 2-digit year that lands in the far past is a typo, not 1926.
        if parsed.year < 100:
            parsed = parsed.replace(year=parsed.year + 2000)
        return parsed
    log.debug("[deadlines] could not parse date %r", value)
    return None


def format_date(d: date) -> str:
    """How a date is written to a human and into the sheet: "Tue 26 Aug 2026".
    Carries the weekday deliberately — "is that a Saturday?" is the first
    question anyone asks about a deadline."""
    return d.strftime("%a %d %b %Y")


def iso(d: date) -> str:
    """The storage form: unambiguous, sortable, comparable as a string."""
    return d.isoformat()


# -- cadence: the strategy doc outranks the defaults --------------------------

# "follow up after 4 working days", "chase replies in 2 days", "prep 1 day before
# the meeting". Deliberately narrow: a cadence is only adopted when the document
# states it in a recognisable form, because a misread cadence silently changes
# every deadline the bot sets.
_CADENCE_PATTERNS: dict[str, tuple[re.Pattern, ...]] = {
    KIND_OUTREACH: (
        re.compile(r"follow[\s-]?up[^.\n]{0,40}?(\d{1,2})\s*(?:working\s+)?days?", re.I),
        re.compile(r"(\d{1,2})\s*(?:working\s+)?days?[^.\n]{0,30}?between\s+follow[\s-]?ups?", re.I),
        re.compile(r"outreach\s+cadence[^.\n]{0,30}?(\d{1,2})\s*(?:working\s+)?days?", re.I),
    ),
    KIND_REPLY: (
        re.compile(r"(?:chase|nudge)[^.\n]{0,40}?repl(?:y|ies)[^.\n]{0,20}?(\d{1,2})\s*(?:working\s+)?days?", re.I),
        re.compile(r"repl(?:y|ies)[^.\n]{0,30}?within\s+(\d{1,2})\s*(?:working\s+)?days?", re.I),
    ),
    KIND_MEETING_PREP: (
        re.compile(r"prep[^.\n]{0,40}?(\d{1,2})\s*(?:working\s+)?days?\s+before", re.I),
        re.compile(r"(\d{1,2})\s*(?:working\s+)?days?\s+before[^.\n]{0,30}?meeting", re.I),
    ),
}


def cadence_from_strategy(text: Optional[str]) -> dict[str, int]:
    """Cadences stated in the strategy doc, as {kind: working_days}.

    Empty when the doc isn't readable or states nothing recognisable — which is
    the normal case today, since the strategy doc source is still awaiting
    access. Values outside 1–30 days are ignored as misreads.
    """
    if not text:
        return {}
    out: dict[str, int] = {}
    for kind, patterns in _CADENCE_PATTERNS.items():
        for pat in patterns:
            m = pat.search(text)
            if not m:
                continue
            try:
                n = int(m.group(1))
            except (TypeError, ValueError):
                continue
            if 1 <= n <= 30:
                out[kind] = n
                log.info(
                    "[deadlines] strategy doc sets the %s cadence to %d working day(s)",
                    kind, n,
                )
                break
    return out


def default_days(kind: str) -> int:
    """The configured default for a kind, in working days."""
    attr = KINDS.get(kind, {}).get("days_attr")
    return int(getattr(config, attr, 3)) if attr else 3


def resolve_rule(kind: str, *, strategy_text: Optional[str] = None) -> tuple[int, str]:
    """(working_days, human_rule_text) for a deadline kind.

    The strategy doc wins when it states a cadence; otherwise the env default.
    The rule text goes verbatim into the announcement so the date is never a
    number out of nowhere.
    """
    spec = KINDS.get(kind) or KINDS[KIND_OUTREACH]
    cadence = cadence_from_strategy(strategy_text)
    if kind in cadence:
        n = cadence[kind]
        return n, spec["rule"].format(n=n) + ", per the strategy doc"
    n = default_days(kind)
    return n, spec["rule"].format(n=n)


def compute_due(
    kind: str,
    *,
    anchor: Optional[date] = None,
    strategy_text: Optional[str] = None,
) -> tuple[date, str, int]:
    """(due_date, rule_text, working_days) for a deadline of `kind`.

    `anchor` is the date the rule counts FROM — the last touch, the day we wrote,
    the meeting date. Defaults to today. Meeting prep counts BACKWARDS from the
    meeting; everything else counts forward.

    NO DEADLINE IS EVER SET IN THE PAST, whichever direction the rule counts.
    A backwards-computed date that has already passed is pulled to the next
    working day: prep for a meeting tomorrow is due today, not last week. The
    same floor applies to the forward rules, and it is not a corner case — the
    anchor for a follow-up is the row's LAST TOUCH, and the rows that most need
    a deadline are exactly the ones nobody has touched for weeks. Anchoring
    literally gave "OpenAI ... setting the follow-up to Mon 20 Jul" on 24 Aug: a
    deadline born five weeks overdue, which is both nonsense to read and instant
    work for the chaser. Due now is the honest answer, and the rule text says
    why so nobody thinks the bot can't count.
    """
    n, rule = resolve_rule(kind, strategy_text=strategy_text)
    base = anchor or today_ist()
    today = today_ist()

    if kind == KIND_MEETING_PREP:
        due = base
        for _ in range(n):
            due = previous_working_day(due)
    else:
        due = add_working_days(base, n)

    if due < today:
        overdue = working_days_between(due, today)
        due = add_working_days(today, 0)
        rule = (
            f"{rule}; that fell {overdue} working day(s) ago, so it is due now"
        )

    return due, rule, n


def announcement(
    *,
    thing: str,
    company: str,
    due: date,
    rule: str,
    mentions: str = "",
    unprompted: bool = False,
) -> str:
    """The in-channel announcement. Fixed shape on purpose — people learn to read
    it at a glance, and the "shout to change" is the consent mechanism.

    `unprompted` distinguishes a deadline set because someone asked from one the
    bot set on its own for a row that clearly needed it; the wording says which,
    because an unrequested date deserves a clearer invitation to object.
    """
    lead = (
        f"No deadline was set for {company} — setting {thing} to {format_date(due)}"
        if not unprompted
        else f"{company} has no next date — setting {thing} to {format_date(due)}"
    )
    tail = f" {mentions} shout to change." if mentions else " Shout to change."
    return f"{lead} ({rule}).{tail}"


def notify_mentions() -> str:
    """The mention string for DEADLINE_NOTIFY_IDS, roster-checked.

    Routed through `guardrails.mention_for`, so someone off the roster is named
    rather than pinged. The announcement still goes out either way — being
    unable to ping someone is not a reason to set a date silently.
    """
    import guardrails

    parts = []
    for uid in config.DEADLINE_NOTIFY_IDS:
        name = str(config.ROSTER_DISPLAY_NAMES.get(str(uid)) or "").strip()
        token = guardrails.mention_for(uid, name)
        if token and token != "there":
            parts.append(token)
    return " ".join(parts)


def sheet_cell_value(due: date, *, rule: str = "") -> str:
    """What goes into the sheet's "Next Deadline (bot)" cell.

    The date plus a short marker, so anyone reading the sheet can tell instantly
    that the bot put it there and that they may simply type over it — the
    conflict rule is that a human date wins, and that only works if a human can
    see which dates aren't theirs.
    """
    return f"{iso(due)} (bot)"


def is_bot_written(value) -> bool:
    """True when a deadline cell was written by the bot rather than a person.

    This is the conflict rule's test: anything NOT matching this shape is treated
    as human-entered and is never overwritten.
    """
    return "(bot)" in str(value or "").lower()
