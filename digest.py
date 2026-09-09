"""WHEN THE BOT SPEAKS — the clock, and nothing else any more.

THIS FILE USED TO BE THE DIGEST. One proactive message a day at
SALES_DIGEST_TIME, carrying HOT / DEADLINES / OVERDUE / ESCALATIONS / HYGIENE
plus five cadence sections, a tracker reminder, a to-do line, a funnel block and
a plan check — grouped per owner, with a carry-forward "(3rd day)" marker on
every item and a per-section cap that said how many rows it had left out.

THAT FORMAT IS RETIRED. `render`, `counts`, `total_items`, `clip`,
`group_by_owner`, `age_note`, `ordinal`, `working_days_overdue`,
`overdue_phrase`, every `SECTION_*` key, `SECTION_ORDER`, `SECTION_TITLES`,
`ITEM_SECTIONS`, `CADENCE_SECTIONS` and `OWNER_GROUPED_SECTIONS` are gone.
`drip.py` replaces them.

WHY IT WENT. The digest was built to solve a real problem — six kinds of message
scattered through the day got the bot muted — and it solved it. Then it created
the opposite one. A wall of sections reads like a report: it gets skimmed, and
it asks a person to find their own name in it and work out which three of forty
lines are theirs. The drip keeps the volume contract that made the digest worth
having and spends it differently: at most three short messages a weekday, one
per (action type x owner), each with a single subject and a single owner. A
message with one ask is answerable. A document is not.

WHAT SURVIVES HERE is the clock, because "has the send time passed" is a
question about the calendar rather than about a format, and both `config` and
`bot` were already asking this module. Moving those two functions to a new home
would have been a rename with no meaning behind it.

THE ONE-MESSAGE RULE THAT REPLACED IT lives in `bot.py`: there is exactly one
`guardrails.send` on a proactive path (`_send_drip_message`), and
DAILY_MESSAGE_CAP bounds how often it runs. The kill switch is unchanged —
`SALES_DIGEST_ENABLED`, same name, same semantics, same suppressed-log line —
and the drip inherits it rather than introducing one of its own.
"""
import logging
from datetime import datetime

log = logging.getLogger(__name__)

# The fallback used when the configured time cannot be read. A bad value must
# degrade to a sensible hour, never to "never send".
DEFAULT_TIME = "10:00"


def parse_time(raw, *, default: str = DEFAULT_TIME) -> tuple[int, int]:
    """"HH:MM" -> (hour, minute). Falls back to `default` with a warning.

    Deliberately tolerant of surrounding whitespace and of a single-digit hour
    ("9:30"), and deliberately INTOLERANT of anything else. A time this cannot
    read is a configuration mistake somebody should hear about, and the warning
    names the value so it can be found — but it must not stop the bot speaking,
    which is why it returns a usable time rather than raising.
    """
    text = str(raw or "").strip()
    if not text:
        return _fallback_time(default)
    parts = text.split(":")
    if len(parts) != 2:
        log.warning(
            "[drip] SALES_DRIP_START=%r is not a HH:MM time — using %s IST instead.",
            raw, default,
        )
        return _fallback_time(default)
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except (TypeError, ValueError):
        log.warning(
            "[drip] SALES_DRIP_START=%r is not a HH:MM time — using %s IST instead.",
            raw, default,
        )
        return _fallback_time(default)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        log.warning(
            "[drip] SALES_DRIP_START=%r is out of range — using %s IST instead.",
            raw, default,
        )
        return _fallback_time(default)
    return hour, minute


def _fallback_time(default: str) -> tuple[int, int]:
    """The default, parsed without recursing. Hard-coded 10:00 if even that is
    malformed — there is no third fallback and there does not need to be."""
    try:
        hour, minute = default.split(":")
        return int(hour), int(minute)
    except Exception:
        return 10, 0


def is_due(now: datetime, *, hour: int, minute: int) -> bool:
    """Has the day's first send time passed?

    AT OR AFTER, not "in the minute of" — the sweeper ticks every
    COS_FOLLOWUP_CHECK_INTERVAL_MINUTES and would step straight over an
    exact-minute test.

    What stops "at or after" meaning "on every tick for the rest of the day" is
    no longer a single date marker: it is the per-slot rows in `drip_sends`,
    which record which of the day's messages have actually gone out. That is
    what lets a redeploy at 11:40 resume at slot 3 rather than replaying the
    morning.
    """
    return (now.hour, now.minute) >= (hour, minute)
