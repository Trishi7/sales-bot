"""THE ONE DAILY DIGEST — every proactive thing this bot has to say, once a day.

WHY THIS MODULE EXISTS. The bot used to speak whenever it noticed something: a
deadline reminder here, a chase there, a hygiene flag fifteen minutes later, an
escalation after that. Individually each was defensible; together they were a
drip of interruptions through the working day, and a channel that drips gets
muted. Muted is the only failure mode that matters — a bot nobody reads is worth
less than no bot at all.

So the rule is now absolute, and it is enforced in `bot.py` by there being no
other proactive send path at all:

    ONE proactive message a day, at SALES_DIGEST_TIME (IST), in the sales
    channel. Everything else the bot says is a REPLY to a person, or the
    ask-time deadline announcement — which is a direct answer to someone who
    just asked, not an interruption.

WHAT IS IN IT, hot first, because the order is the priority:

    HOT          They replied and we haven't come back. Speed to lead is the
                 most expensive failure in the sheet, so it leads the digest.

    THE CADENCE — Vaishnavi's daily list, computed by cadence.py against the
    OUTREACH TRACKER (the only tab with dates in it) and ranked urgent-first
    before it ever reaches this module. Five sections, grouped per owner with
    one @mention each:

    IN VAISHNAVI'S ORDER, from her daily-cadence line — "Follow-ups for this
    week · Intros to be done this week · Meetings for this week · Update tracker
    · Assets to be shared this week":

    FOLLOW-UPS   this week: cold follow-ups, recently-contacted rows that never
                 connected, rows worth trying on another channel, stalled next
                 steps, the one-per-company suggestion for a rejected account,
                 and the one-line count of the cold cohort.
    INTROS       to be done: connected, no membrane intro date.
    MEETINGS     this week: positive replies with no next step, and meetings
                 inside the prep window.
    UPDATE-TRACKER  rows whose rule inputs are blank ("no follow-up count or
                 last-followed date recorded"), places where the master tab and
                 the tracker disagree, sheet-health flags, and the ask to mark a
                 PoC unresponsive — which the bot asks for and NEVER writes.
    ASSETS       to be shared: a meeting happened and the assets or the next
                 step never followed it.

    THREE BUDGETS, because one number could not serve them. The URGENT items —
    a positive reply with no next step, a meeting in the prep window, a
    post-meeting gap — are NEVER truncated (URGENT_MAX is a hard ceiling against
    a broken sheet, not a target): they are what the digest is for, and cutting
    them defeats it. DIGEST_MAX_ITEMS caps everything else in the cadence, and
    UPDATE_TRACKER_MAX caps the tracker asks so a sparse sheet's fill-in
    requests can never eat the slots the real work needs. One closing line says
    how many were held, because a silently truncated list reads as a short day.

    The COLD SUMMARY sits outside all three: one line counting the
    never-connected rows past CONNECT_REMINDER_MAX_DAYS, which are suppressed
    from individual chases. A count of suppressed rows that could itself be
    suppressed would be worse than not suppressing anything.

    DEADLINES    Due today or tomorrow, addressed to the owner.
    OVERDUE      Chases and deadlines past due, GROUPED PER OWNER with one
                 @mention each, so a person with four overdue things is pinged
                 once and reads one line.
    ESCALATIONS  Past COS_NUDGE_MAX_ATTEMPTS. Kushal's section, at the bottom,
                 because it is the only part addressed to him specifically.
    HYGIENE      Stalled and dead-deal rows.
    MEETING PREP One brief per meeting inside MEETING_PREP_DAYS, written once
                 (deduped in SQLite) rather than every day until the meeting.
                 Reference material, not a task — which is why it sits at the
                 bottom and does not count towards "is there anything to post".

CARRY-FORWARD, AND WHY THERE IS NO "RESOLVED" LINE. An unresolved item simply
appears again tomorrow, with its age ("3rd day"). An item that got resolved
during the day simply isn't in tomorrow's digest. Announcing resolutions would
double the volume of the thing this module exists to reduce, and the team
already knows what they fixed.

AGE IS PERSISTED, NOT COUNTED IN MEMORY. `db.note_digest_item` records the day
an item first appeared, so a redeploy doesn't reset every age to day one. That
table is what makes "3rd day" honest rather than decorative.

EMPTY DAY = NO DIGEST. Never "nothing to report" — that is a message with no
information in it, and posting one teaches people the digest can be skipped.
"""
import logging
import re
from datetime import date, datetime
from typing import Optional

log = logging.getLogger(__name__)

# Section keys, in the order they are rendered. Hot first, hygiene last: the top
# of the message is the part that actually gets read.
SECTION_HOT = "hot"
SECTION_DEADLINES = "deadlines"
SECTION_OVERDUE = "overdue"
SECTION_ESCALATIONS = "escalations"
SECTION_HYGIENE = "hygiene"
# Not a proactive "item" — the weekly funnel numbers, which used to be their own
# scheduled post and now ride along on the digest of their weekday.
SECTION_FUNNEL = "funnel"

# THE PHASE-1 CADENCE SECTIONS — Vaishnavi's daily list, in her order and in her
# words. They come from cadence.py, which computes them against the tracker;
# the keys are defined THERE so that a rule and its section can never drift
# apart, and are re-exported here because this module owns rendering.
#
# They sit between HOT and DEADLINES on purpose: HOT is still "they replied and
# we said nothing", which outranks everything, and the cadence is the day's
# actual work list, which outranks the bot's own bookkeeping below it.
SECTION_CADENCE_FOLLOWUPS = "cadence_followups"
SECTION_CADENCE_INTROS = "cadence_intros"
SECTION_CADENCE_MEETINGS = "cadence_meetings"
SECTION_CADENCE_UPDATES = "cadence_updates"
SECTION_CADENCE_ASSETS = "cadence_assets"

# The prep briefs, and the one line that makes the cadence cap honest. Neither
# is an "item": a brief is reference material for a meeting already counted in
# CADENCE_MEETINGS, and the overflow line is a footnote about what was cut.
SECTION_PREP = "prep_briefs"
SECTION_CADENCE_OVERFLOW = "cadence_overflow"

CADENCE_SECTIONS = (
    SECTION_CADENCE_FOLLOWUPS,
    SECTION_CADENCE_INTROS,
    SECTION_CADENCE_MEETINGS,
    SECTION_CADENCE_UPDATES,
    SECTION_CADENCE_ASSETS,
)

SECTION_ORDER = (
    SECTION_HOT,
    SECTION_CADENCE_FOLLOWUPS,
    SECTION_CADENCE_INTROS,
    SECTION_CADENCE_MEETINGS,
    SECTION_CADENCE_UPDATES,
    SECTION_CADENCE_ASSETS,
    SECTION_CADENCE_OVERFLOW,
    SECTION_DEADLINES,
    SECTION_OVERDUE,
    SECTION_ESCALATIONS,
    SECTION_FUNNEL,
    SECTION_HYGIENE,
    SECTION_PREP,
)

SECTION_TITLES = {
    SECTION_HOT: "HOT — they replied, nothing has gone back",
    SECTION_CADENCE_FOLLOWUPS: "FOLLOW-UPS for this week",
    SECTION_CADENCE_INTROS: "INTROS to be done this week",
    SECTION_CADENCE_MEETINGS: "MEETINGS for this week",
    SECTION_CADENCE_UPDATES: "UPDATE TRACKER",
    SECTION_CADENCE_ASSETS: "ASSETS to be shared this week",
    SECTION_DEADLINES: "DEADLINES — due today or tomorrow",
    SECTION_OVERDUE: "OVERDUE",
    SECTION_ESCALATIONS: "ESCALATIONS — asked enough, needs a decision",
    SECTION_FUNNEL: "WEEKLY FUNNEL",
    SECTION_HYGIENE: "HYGIENE — stalled and dead-deal rows",
    # Deliberately untitled: every brief opens with its own
    # "MEETING PREP — <company> / <PoC> — <date>" header, and a section heading
    # above it would print the same two words twice.
    SECTION_PREP: "",
}

# Sections that count as "items". A digest containing ONLY the funnel block is
# still an empty day — the funnel is a report, not something anyone has to do.
# The prep briefs and the overflow footnote are excluded for the same reason:
# neither is a thing to do, and a digest that posted because a brief existed
# would be posting reference material at people with no work in it.
ITEM_SECTIONS = (
    SECTION_HOT,
    SECTION_CADENCE_FOLLOWUPS,
    SECTION_CADENCE_INTROS,
    SECTION_CADENCE_MEETINGS,
    SECTION_CADENCE_UPDATES,
    SECTION_CADENCE_ASSETS,
    SECTION_DEADLINES,
    SECTION_OVERDUE,
    SECTION_ESCALATIONS,
    SECTION_HYGIENE,
)

# Sections rendered as free-form blocks rather than as owner-addressed bullets:
# their "text" is already the finished thing.
BLOCK_SECTIONS = (SECTION_FUNNEL, SECTION_PREP, SECTION_CADENCE_OVERFLOW)

_TIME_RE = re.compile(r"^\s*(\d{1,2})\s*[:.\s]\s*(\d{1,2})\s*$")


def parse_time(raw, *, default: str = "10:00") -> tuple[int, int]:
    """"10:00" -> (10, 0). The digest time is a WALL-CLOCK IST time, so it is
    parsed here and compared against `deadlines.now_ist()` — never against the
    server clock, which on a cloud box is UTC and would post at 15:30 local.

    Tolerant of "10.00", "10 00" and a bare "10". Anything unreadable falls back
    to the default with a warning rather than silently never firing: a digest
    that never posts is indistinguishable from a broken bot.
    """
    s = str(raw or "").strip()
    if not s:
        s = default
    if s.isdigit():
        s = f"{int(s)}:00"
    m = _TIME_RE.match(s)
    if not m:
        log.warning(
            "[digest] SALES_DIGEST_TIME=%r is not a HH:MM time — using %s IST instead.",
            raw, default,
        )
        return _fallback_time(default)
    hour, minute = int(m.group(1)), int(m.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        log.warning(
            "[digest] SALES_DIGEST_TIME=%r is out of range — using %s IST instead.",
            raw, default,
        )
        return _fallback_time(default)
    return hour, minute


def _fallback_time(default: str) -> tuple[int, int]:
    """The default, parsed without recursing back through the warning path (a
    caller can pass a broken default, and a RecursionError at import time would
    take the whole bot down over a formatting typo)."""
    m = _TIME_RE.match(str(default or "").strip())
    if not m:
        return 10, 0
    hour, minute = int(m.group(1)), int(m.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return 10, 0
    return hour, minute


def is_due(now: datetime, *, hour: int, minute: int) -> bool:
    """Has today's digest time passed?

    AT OR AFTER, not "in the minute of" — the sweeper ticks every
    COS_FOLLOWUP_CHECK_INTERVAL_MINUTES and would step straight over an
    exact-minute test. The once-a-day marker in SQLite is what stops "at or
    after" meaning "on every tick for the rest of the day".
    """
    return (now.hour, now.minute) >= (hour, minute)


def ordinal(n: int) -> str:
    """1 -> "1st", 2 -> "2nd", 3 -> "3rd", 11 -> "11th"."""
    n = int(n)
    if 10 <= (n % 100) <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def age_note(times_seen: int) -> str:
    """The carry-forward marker appended to an item's line.

    Empty on the first day — "(1st day)" on everything would be noise on a
    digest where most items are new. From the second day it is the whole point:
    an item on its fourth day is a different problem from one on its first.
    """
    n = int(times_seen or 1)
    return "" if n <= 1 else f" ({ordinal(n)} day)"


def group_by_owner(items: list[dict]) -> list[tuple[str, list[dict]]]:
    """Group OVERDUE items by who owns them, first-seen owner order preserved.

    This is the anti-nag rule in code: a person with four overdue things gets
    ONE @mention and one line, not four pings. Items with no identifiable owner
    fall into a single group keyed by the empty string, which the renderer
    addresses to the deadline-notify list instead and puts last — a line
    addressed to nobody in particular is the least actionable one in a section.
    """
    order: list[str] = []
    groups: dict[str, list[dict]] = {}
    for item in items:
        key = item.get("owner_key") or ""
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(item)
    ranked = sorted(enumerate(order), key=lambda pair: (pair[1] == "", pair[0]))
    return [(key, groups[key]) for _, key in ranked]


def render(
    *,
    day: date,
    sections: dict[str, list[dict]],
    unowned_mention: str = "",
    escalate_mention: str = "",
    staleness: str = "",
) -> str:
    """The whole digest as ONE body of text.

    Splitting into Discord-sized parts happens in `bot._split_for_discord`; this
    function never worries about the 2000-char limit, because a digest that
    dropped its HYGIENE section to fit would be lying about what it found.

    `sections` maps a section key to a list of item dicts:
        {"text": str, "age": int, "owner_mention": str, "owner_key": str}
    The FUNNEL section carries pre-rendered lines in "text" and no age.
    """
    lines = [f"**Daily sales digest — {day.strftime('%a %d %b %Y')}**"]

    for key in SECTION_ORDER:
        items = [i for i in (sections.get(key) or []) if i.get("text")]
        if not items:
            continue

        if key in BLOCK_SECTIONS:
            lines.append("")
            title = SECTION_TITLES.get(key)
            if title:
                lines.append(f"**{title}**")
            for item in items:
                lines.append(str(item["text"]))
            continue

        lines.append("")
        lines.append(f"**{SECTION_TITLES.get(key, key.upper())} ({len(items)})**")

        if key in CADENCE_SECTIONS:
            # Grouped per owner, ONE @mention each — the same anti-nag rule as
            # OVERDUE — but one bullet per item rather than a joined run-on.
            # A cadence line is a whole sentence ("met 7d ago, no assets shared
            # and no next steps"); four of them joined with semicolons is a
            # paragraph nobody finishes reading.
            for _owner_key, group in group_by_owner(items):
                who = (group[0].get("owner_mention") or "").strip() or unowned_mention
                if who:
                    lines.append(f"{who}")
                for item in group:
                    body = (str(item["text"]) + age_note(item.get("age", 1))).strip()
                    lines.append(f"• {body}")
            continue

        if key == SECTION_OVERDUE:
            for _owner_key, group in group_by_owner(items):
                who = (group[0].get("owner_mention") or "").strip() or unowned_mention
                parts = [
                    (str(item["text"]) + age_note(item.get("age", 1))).strip()
                    for item in group
                ]
                joined = "; ".join(p for p in parts if p)
                lines.append(f"• {who} — {joined}" if who else f"• {joined}")
            continue

        if key == SECTION_ESCALATIONS and escalate_mention:
            lines.append(escalate_mention)

        for item in items:
            who = (item.get("owner_mention") or "").strip()
            body = (str(item["text"]) + age_note(item.get("age", 1))).strip()
            lines.append(f"• {who} — {body}" if who else f"• {body}")

    if staleness:
        lines.append("")
        lines.append(staleness)

    return "\n".join(lines)


def counts(sections: dict[str, list[dict]]) -> dict[str, int]:
    """Items per section — what goes into the audit record, so an operator can
    see the shape of a day's digest without reading the message itself."""
    return {key: len(sections.get(key) or []) for key in SECTION_ORDER}


def total_items(sections: dict[str, list[dict]]) -> int:
    """How many ACTIONABLE items the digest holds. Zero means no digest today —
    the funnel block on its own is not a reason to post."""
    return sum(len(sections.get(key) or []) for key in ITEM_SECTIONS)


def clip(items: list[dict], limit: int, *, what: str) -> list[dict]:
    """Cap one section, but SAY SO. A silently truncated section reads as "there
    were only five stalled rows", which is worse than saying there were seventy.

    The overflow line is itself an item, so it renders in place and is counted.
    """
    limit = max(1, int(limit))
    if len(items) <= limit:
        return items
    dropped = len(items) - limit
    log.info("[digest] %s section clipped: %d shown, %d more", what, limit, dropped)
    return items[:limit] + [
        {
            "text": f"…and {dropped} more {what} item(s) — ask me for the full list.",
            "age": 1,
            "overflow": True,
        }
    ]


def working_days_overdue(due: Optional[date], today: date, *, dl) -> int:
    """How many working days an item is past due, floored at 0. `dl` is passed
    in rather than imported so this module stays free of an import cycle with
    `deadlines`, which imports `config`, which nothing here needs."""
    if due is None:
        return 0
    return max(0, dl.working_days_between(due, today))


def overdue_phrase(n: int) -> str:
    """"overdue 2 wd" / "due today". Working days, abbreviated, because these
    strings are joined several to a line and spelling out "working days" three
    times over would be most of a line spent on units."""
    n = int(n or 0)
    if n <= 0:
        return "due today"
    return f"overdue {n} wd"
