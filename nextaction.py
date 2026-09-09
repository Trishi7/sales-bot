"""THE NEXT-ACTION STATE MACHINE — exactly ONE next action per ACTIVE row.

WHAT THIS REPLACES. The bot used to decide what a row needed in two places that
did not know about each other: the three row-hygiene flags in `tracker.py`
(HOT / STALLED / DEAD-DEAL) and the deadline kinds in `deadlines.py`. Between
them a single row could be HOT *and* DEAD-DEAL *and* carry an outreach deadline,
and the code had a dedup ordering to pick which of those to say out loud. That
is a system with three opinions and a tie-break, not an answer.

This module has one opinion. For each active row it produces **exactly one**:

    {type, owner, due_date, priority, ...}

or **nothing at all** — and "nothing at all" is a real, deliberate outcome, not
a failure to find something.

    THE FIRST MATCHING TRIGGER WINS. `TRIGGERS` below is an ordered list and it
    is evaluated top to bottom. That order IS the product decision: a row with a
    positive reply, a pending demo quote and four silent touches gets the
    meeting proposal, because that is the thing a human would do first. Nothing
    downstream has to break a tie, because no tie is ever produced.

THE TRIGGERS, in evaluation order, with the band each lands in:

     #  trigger              condition                                    band
     0  stop_closure         closure 0% / Dead / Unresponsive / Won / Lost  — STOP
     1  snoozed              a live snooze                                  — SILENT
     2  meeting_proposal     any positive or replied row                    P0
     3  scheduled_reminder   a one-off somebody asked for                   P1
     4  quote_chase          stage = Demo, no movement, DEMO_QUOTE_DAYS     P1
     5  progress_check       dm_sent_date + DM_PROGRESS_CHECK_DAYS          P2
     6  dm_check             connection + CONNECTION_DM_CHECK_DAYS, no DM   P2
     7  dm_sent_check        connection + CONNECT_DM_CHECK_HOURS, no DM     P2
     8  mark_unresponsive    silent touches >= UNRESPONSIVE_SUGGEST_AT      P2
     9  channel_switch       silent touches >= CHANNEL_SWITCH_AT            P2
    10  pulse_check          deal On Hold                                   P3
    11  followup             the lane cadence, off the last touch           P1/P2/P3

THE BANDS, and the order within a day (strategy section 5.1):

    P0  positive overrides      a reply is the most expensive thing to sit on
    P1  meetings & demo chases  a booked thing, or a quote somebody is waiting for
    P2  seven-day follow-ups    the ordinary cadence
    P3  slow lane               closure below the threshold, and parked deals

Two rows in the same band are ordered by due date, oldest first, then by company
so the list is stable day to day.

STOP MEANS STOP. A row whose closure cell says 0%, Dead, Unresponsive, Won or
Lost produces no action, ever, from any trigger — the priority override
included. A won deal does not need a meeting proposal and a dead one does not
need a follow-up, and a bot that keeps producing work for closed rows is a bot
whose queue nobody trusts.

WEEKENDS. No computed due date lands on a Saturday or a Sunday; each is shifted
forward to the Monday. The single exception is an explicitly scheduled reminder
— somebody asked to be reminded at a specific time, possibly on purpose at the
weekend, and moving their own instruction would be overruling them silently.

THIS MODULE IS PURE, AND THAT IS LOAD-BEARING. Rows in, dicts out. It reads no
sheet, writes no database row and sends nothing — the snoozes, the scheduled
reminders and the explicit activations are all passed IN. There is no
`guardrails.send` here and there must never be one: the queue is read by asking
for it ("cadence preview") or in the startup log, and wiring it into the daily
digest is a separate, deliberate change.
"""
import logging
import math
from datetime import date, timedelta
from typing import Optional

import activation
import config
import deadlines as dl
import gtm_sheet

log = logging.getLogger(__name__)

# -- action types -------------------------------------------------------------
MEETING_PROPOSAL = "meeting_proposal"
SCHEDULED_REMINDER = "scheduled_reminder"
QUOTE_CHASE = "quote_chase"
PROGRESS_CHECK = "progress_check"
DM_CHECK = "dm_check"
DM_SENT_CHECK = "dm_sent_check"
MARK_UNRESPONSIVE = "mark_unresponsive"
CHANNEL_SWITCH = "channel_switch"
PULSE_CHECK = "pulse_check"
FOLLOWUP = "followup"

# What each type IS, in one phrase, for the preview's group headings.
TYPE_LABELS = {
    MEETING_PROPOSAL: "Propose a meeting (they replied)",
    SCHEDULED_REMINDER: "Reminders you asked for",
    QUOTE_CHASE: "Quote chases (demo given)",
    PROGRESS_CHECK: "Progress checks (DM sent, nothing since)",
    DM_CHECK: "DM checks (connected a week, no DM)",
    DM_SENT_CHECK: "DM sent? (newly connected)",
    MARK_UNRESPONSIVE: "Mark unresponsive (your call, not mine)",
    CHANNEL_SWITCH: "Change of channel",
    PULSE_CHECK: "Pulse checks (deal on hold)",
    FOLLOWUP: "Follow-ups",
}

# -- priority bands -----------------------------------------------------------
P_OVERRIDE = 0
P_MEETING = 1
P_FOLLOWUP = 2
P_SLOW = 3

BAND_LABELS = {
    P_OVERRIDE: "positive override",
    P_MEETING: "meetings & demo chases",
    P_FOLLOWUP: "7-day follow-ups",
    P_SLOW: "slow lane",
}

# Why a row produced nothing. Reported rather than swallowed: "no action" and
# "I could not work out an action" are different states, and a queue that
# renders both as absence is a queue that hides its own failures.
SILENT_STOPPED = "stopped"
SILENT_SNOOZED = "snoozed"
SILENT_NO_ANCHOR = "no_anchor"
SILENT_NOT_DUE = "not_due"

SILENT_LABELS = {
    SILENT_STOPPED: "closed (0% / Dead / Unresponsive / Won / Lost) — never chased again",
    SILENT_SNOOZED: "snoozed",
    SILENT_NO_ANCHOR: "no readable date to count from",
    SILENT_NOT_DUE: "nothing due yet",
}


# -- reading a row ------------------------------------------------------------


def _text(row: dict, role: str) -> str:
    """A row cell as trimmed text, spreadsheet errors normalised to empty."""
    return gtm_sheet.clean_cell(row.get(role))


def _norm(value) -> str:
    return gtm_sheet.normalise_header(gtm_sheet.clean_cell(value))


def _matches_any(value, phrases) -> str:
    """The phrase from `phrases` that this cell matches, or "".

    Whole-phrase, case- and punctuation-insensitive, longest first so "closed
    lost" beats "lost" and the reported reason names the specific value the
    sheet actually holds.
    """
    v = _norm(value)
    if not v:
        return ""
    for phrase in sorted((str(p or "").strip() for p in phrases or []), key=len, reverse=True):
        if not phrase:
            continue
        key = gtm_sheet.normalise_header(phrase)
        if not key:
            continue
        if v == key or key in v.split() or f" {key} " in f" {v} ":
            return phrase
    return ""


def closure_percent(row: dict) -> Optional[int]:
    """The closure cell as a whole percentage, or None when it isn't a number.

    "75%", "75", "0.75" and "75 %" all read as 75. A fraction is recognised only
    when it is written as a decimal below 1, because "0.5" in a percentage
    column means half and "50" means half — reading 0.5 as half a percent would
    silently move a live deal into the slow lane.
    """
    raw = _text(row, "closure").replace("%", "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if 0 < value < 1:
        value *= 100
    if value < 0 or value > 100:
        return None
    return int(round(value))


def stop_reason(row: dict) -> str:
    """Why this row is finished, or "" when it is still live.

    TWO WAYS TO BE FINISHED, and both are things a human wrote:
      - the closure cell names one of CLOSURE_STOP_MARKERS (Dead, Unresponsive,
        Won, Lost);
      - the closure cell is exactly 0%.

    A BLANK CLOSURE CELL IS NOT A STOP. That is the whole reason this is checked
    numerically rather than by treating "no percentage" as zero: most rows have
    never had the column filled in, and reading a blank as 0% would silence the
    entire sheet on the first run.
    """
    marker = _matches_any(row.get("closure"), config.CLOSURE_STOP_MARKERS)
    if marker:
        return f"closure says {_text(row, 'closure')!r}"
    pct = closure_percent(row)
    if pct == 0:
        return f"closure is {_text(row, 'closure')!r} (0%)"
    # The deal-status cell sometimes carries the same verdict.
    marker = _matches_any(row.get("deal_status"), config.CLOSURE_STOP_MARKERS)
    if marker:
        return f"deal status says {_text(row, 'deal_status')!r}"
    return ""


def is_on_hold(row: dict) -> bool:
    return bool(_matches_any(row.get("deal_status"), config.DEAL_ON_HOLD_MARKERS))


def is_in_progress(row: dict) -> bool:
    return bool(_matches_any(row.get("deal_status"), config.DEAL_IN_PROGRESS_MARKERS))


def is_demo_stage(row: dict) -> bool:
    """Has this prospect had a demo? Read from the prospect/stage cell, and — as
    a fallback — from the deal-status cell, because sheets in the wild put the
    stage in whichever of the two columns is nearer to hand."""
    return bool(
        _matches_any(row.get("prospect_stage"), config.PROSPECT_DEMO_MARKERS)
        or _matches_any(row.get("deal_status"), config.PROSPECT_DEMO_MARKERS)
    )


def first_contact_was_email(row: dict) -> bool:
    """Did the first contact go out by email?

    Drives the ONE type-aware line in the whole engine: the progress check asks
    for an email address or a phone number, and it must not ask somebody for the
    email address we already used.

    An UNREADABLE or BLANK type returns False, which means the ask is included.
    That is the safe direction: a redundant "do we have their email?" is
    awkward, a missing one loses the contact detail the check exists to get.
    """
    return bool(_matches_any(row.get("first_contact_type"), config.EMAIL_CONTACT_TYPES))


def has_replied(row: dict) -> bool:
    """Did they come back at all — positively or unclassifiably?

    An UNCLASSIFIED reply counts. Somebody wrote something in the response cell,
    so they did answer, and the cost of proposing a meeting to a warm reply is
    far lower than the cost of leaving a real one to go cold. A cell that says
    "N"/rejected does not count, and a rejected row has usually already stopped
    at trigger 0.
    """
    return gtm_sheet.response_status(row.get("response")) in (
        gtm_sheet.RESPONSE_POSITIVE, gtm_sheet.RESPONSE_UNKNOWN,
    )


def silent_touches(row: dict) -> int:
    """Follow-ups sent with nothing back. 0 when they have replied.

    The count comes from the sheet's own follow-up counter; a blank counter is
    0, and 0 never trips a threshold, so a sparse sheet produces no escalation
    it cannot justify.
    """
    if has_replied(row):
        return 0
    raw = _text(row, "followups_count")
    digits = "".join(ch for ch in raw if ch.isdigit())
    return int(digits) if digits else 0


def _date(row: dict, role: str) -> Optional[date]:
    return gtm_sheet.sheet_date(row.get(role))


def last_touch(row: dict) -> tuple[Optional[date], str]:
    """The most recent thing that happened on this row, and which column said so.

    The LATEST readable date across the touch columns, not the first one found:
    a row with a first-contact date in March and a DM in August is an August
    row, and anchoring a cadence to March would make it permanently overdue.
    """
    best: Optional[date] = None
    label = ""
    for role, name in (
        ("last_followed_up", "last followed up"),
        ("dm_sent_date", "DM sent"),
        ("meeting_date", "meeting"),
        ("connected", "connection date"),
        ("intro_date", "intro date"),
        ("first_contacted", "first contact date"),
    ):
        value = _date(row, role)
        if value is None:
            continue
        # A meeting in the FUTURE is not a touch that has happened yet.
        if role == "meeting_date" and value > dl.today_ist():
            continue
        if best is None or value > best:
            best, label = value, name
    return best, label


def has_movement(row: dict, *, today: date) -> bool:
    """Has anything moved on this row since the demo?

    Movement is a next step written down, or a meeting still to come. Both are
    somebody's own record that the row is in hand, and chasing a quote past
    either would be the bot talking over the team.
    """
    if _text(row, "next_steps"):
        return True
    meeting = _date(row, "meeting_date")
    return bool(meeting and meeting >= today)


# -- dates --------------------------------------------------------------------


def shift_off_weekend(due: date) -> date:
    """Saturday and Sunday shift forward to the Monday.

    Applied to every computed due date. The one exception is an explicitly
    scheduled reminder, which keeps the date its asker chose — see the module
    header.
    """
    if not config.NEXT_ACTION_WEEKEND_SHIFT:
        return due
    while due.weekday() >= 5:          # 5 = Saturday, 6 = Sunday
        due += timedelta(days=1)
    return due


def hours_as_days(hours: int) -> int:
    """48 hours -> 2 days, 36 -> 2, 1 -> 1.

    ROUNDED UP, because the sheet holds dates and not times: the bot cannot know
    what time on the connection date the connection happened, and rounding down
    would fire the check before the window it was given had actually passed.
    """
    return max(1, int(math.ceil(max(0, int(hours or 0)) / 24.0)))


def _due(anchor: date, days: int) -> date:
    """`days` CALENDAR days after `anchor`, shifted off the weekend.

    Deliberately NOT pulled forward to today when it lands in the past. An
    overdue action should read as overdue — that is the single most useful thing
    on its line, and a queue that quietly restamps everything as "due today"
    cannot be triaged.
    """
    return shift_off_weekend(anchor + timedelta(days=max(0, int(days))))


def _due_working(anchor: date, working_days: int) -> date:
    """`working_days` WORKING days after `anchor`. Already weekend-safe by
    construction, but shifted anyway so one function owns the guarantee."""
    return shift_off_weekend(dl.add_working_days(anchor, max(0, int(working_days))))


# -- the action ---------------------------------------------------------------


def _action(
    row: dict, *, type_: str, band: int, due: Optional[date], text: str, why: str,
    today: date, anchor: Optional[date] = None, anchor_label: str = "",
) -> dict:
    """One next action. The only shape this module returns."""
    company = _text(row, "company") or "(unnamed row)"
    poc = _text(row, "poc")
    overdue = 0
    if due is not None and due < today:
        overdue = (today - due).days
    return {
        "type": type_,
        "label": TYPE_LABELS.get(type_, type_),
        "owner": _text(row, "owner"),
        "due_date": due,
        "due_iso": dl.iso(due) if due else "",
        "priority": band,
        "priority_label": BAND_LABELS.get(band, "?"),
        "overdue_days": overdue,
        "company": company,
        "poc": poc,
        "poc_designation": _text(row, "poc_designation"),
        "sheet_row": row.get("_row"),
        "row_key": activation.row_key(row),
        "anchor": dl.iso(anchor) if anchor else "",
        "anchor_label": anchor_label,
        "why": why,
        "text": text,
        "key": f"nextaction:{type_}:{activation.row_key(row)}",
    }


def _describe(row: dict) -> str:
    company = _text(row, "company") or "(unnamed row)"
    poc = _text(row, "poc")
    if not poc:
        return company
    designation = _text(row, "poc_designation")
    return f"{company} · {poc}{f' ({designation})' if designation else ''}"


# -- the lane -----------------------------------------------------------------


def lane(row: dict) -> tuple[str, int, int, str]:
    """(name, cadence_days, band, why) — how hard this row is worked.

    THREE LANES:
      fast   closure above CLOSURE_HOT_THRESHOLD, or the deal marked In
             Progress. HOT_DEAL_DAYS, near the front of the queue.
      slow   closure at or below the threshold. SLOW_LANE_DAYS, at the back.
             ON the threshold counts as slow: "50%" is not "more likely than
             not", and a coin-flip deal does not earn a weekly chase.
      normal no closure recorded. The ordinary FOLLOWUP_GRACE_DAYS cadence.
    """
    pct = closure_percent(row)
    if is_in_progress(row):
        return ("fast", max(1, config.HOT_DEAL_DAYS), P_MEETING,
                f"deal status is {_text(row, 'deal_status')!r}")
    if pct is not None and pct > max(0, config.CLOSURE_HOT_THRESHOLD):
        return ("fast", max(1, config.HOT_DEAL_DAYS), P_MEETING,
                f"closure {pct}% is above {config.CLOSURE_HOT_THRESHOLD}%")
    if pct is not None:
        return ("slow", max(1, config.SLOW_LANE_DAYS), P_SLOW,
                f"closure {pct}% is not above {config.CLOSURE_HOT_THRESHOLD}%")
    return ("normal", max(1, config.FOLLOWUP_GRACE_DAYS), P_FOLLOWUP,
            "no closure percentage recorded")


# -- the triggers -------------------------------------------------------------
# Each returns an action dict, or None to fall through to the next one. They are
# evaluated in the order of TRIGGERS at the bottom of this section, and the
# FIRST one to return wins — that ordering is the product decision, and it is
# why a row can never produce two competing instructions.


def _t_meeting_proposal(row, ctx) -> Optional[dict]:
    """(2) THE PRIORITY OVERRIDE — strategy section 5.1.

    Any row that replied, positively or unclassifiably, needs a meeting
    proposed, and it goes to the front of the whole queue. This is the most
    expensive thing in the sheet to sit on: a reply is a door somebody opened,
    and it closes again on its own.

    Due MEETING_PROPOSAL_WORKING_DAYS working days from the reply, and left in
    the past when that has already gone — an overdue reply is the truest line
    the queue can carry.
    """
    if not has_replied(row):
        return None
    today = ctx["today"]
    anchor, label = last_touch(row)
    anchor = anchor or today
    due = _due_working(anchor, config.MEETING_PROPOSAL_WORKING_DAYS)
    response = _text(row, "response") or "a reply"
    return _action(
        row, type_=MEETING_PROPOSAL, band=P_OVERRIDE, due=due, today=today,
        anchor=anchor, anchor_label=label or "last touch",
        why=(f"response cell says {response!r}; due "
             f"{config.MEETING_PROPOSAL_WORKING_DAYS} working day(s) after the "
             f"{label or 'last touch'} ({dl.format_date(anchor)})"),
        text=(f"{_describe(row)} — they replied ({response}). Propose a meeting. "
              f"This is front of the queue however old anything else on the row is."),
    )


def _t_scheduled_reminder(row, ctx) -> Optional[dict]:
    """(3) A ONE-OFF SOMEBODY ASKED FOR, at the time they asked for it.

    The only date in this module that is NOT weekend-shifted. Somebody asking to
    be reminded on a Saturday has made a decision about their own Saturday.

    The SOONEST open reminder wins when there are several; the rest are still in
    the database and surface on their own days.
    """
    reminders = ctx["scheduled"].get(activation.row_key(row)) or []
    if not reminders:
        return None
    today = ctx["today"]
    due_reminders = [r for r in reminders if dl.parse_date(r.get("due_date")) is not None]
    if not due_reminders:
        return None
    soonest = min(due_reminders, key=lambda r: dl.parse_date(r["due_date"]))
    due = dl.parse_date(soonest["due_date"])
    if due > today:
        return None
    when = str(soonest.get("due_time") or "").strip()
    what = str(soonest.get("what") or "").strip() or "the reminder you asked for"
    return _action(
        row, type_=SCHEDULED_REMINDER, band=P_MEETING, due=due, today=today,
        anchor=due, anchor_label="the date you asked for",
        why=(f"you asked for this on {dl.format_date(due)}"
             + (f" ({when})" if when else "")
             + " — a scheduled reminder keeps its exact date and is never moved "
               "off a weekend"),
        text=f"{_describe(row)} — {what}" + (f" ({when})" if when else ""),
    )


def _t_quote_chase(row, ctx) -> Optional[dict]:
    """(4) DEMO GIVEN, NOTHING MOVED — chase the quote.

    Requires all three: the stage says Demo, nothing has moved (no next step and
    no meeting still to come), and DEMO_QUOTE_DAYS have passed. A demo with a
    next step written under it is in hand, and the bot has nothing to add.
    """
    if not is_demo_stage(row):
        return None
    today = ctx["today"]
    if has_movement(row, today=today):
        return None
    anchor, label = last_touch(row)
    if anchor is None:
        return None
    days = max(1, config.DEMO_QUOTE_DAYS)
    if (today - anchor).days < days:
        return None
    return _action(
        row, type_=QUOTE_CHASE, band=P_MEETING, due=_due(anchor, days), today=today,
        anchor=anchor, anchor_label=label,
        why=(f"stage is {_text(row, 'prospect_stage') or _text(row, 'deal_status')!r}, "
             f"nothing recorded since the {label} ({dl.format_date(anchor)}), and "
             f"{days}d have passed"),
        text=(f"{_describe(row)} — demo done {(today - anchor).days}d ago and nothing "
              f"has moved since. Send the quote, or write down what is blocking it."),
    )


def _t_progress_check(row, ctx) -> Optional[dict]:
    """(5) DM SENT, NOTHING SINCE — the progress check.

    THE ONE TYPE-AWARE ACTION. It asks for an email address or a phone number,
    unless the first contact was already by email — in which case we have the
    email and asking for it reads as a bot that does not read its own sheet.
    """
    sent = _date(row, "dm_sent_date")
    if sent is None:
        return None
    today = ctx["today"]
    days = max(1, config.DM_PROGRESS_CHECK_DAYS)
    if (today - sent).days < days:
        return None
    ask = (
        ""
        if first_contact_was_email(row)
        else " Ask for an email address or a phone number while you are there."
    )
    type_note = (
        f"first contact was {_text(row, 'first_contact_type')!r}, so the contact-detail "
        "ask is skipped"
        if first_contact_was_email(row)
        else (f"first contact was "
              f"{_text(row, 'first_contact_type') or 'not recorded'!r}, so the "
              "contact-detail ask is included")
    )
    return _action(
        row, type_=PROGRESS_CHECK, band=P_FOLLOWUP, due=_due(sent, days), today=today,
        anchor=sent, anchor_label="DM sent",
        why=(f"DM sent {dl.format_date(sent)}, {(today - sent).days}d ago, threshold "
             f"{days}d; {type_note}"),
        text=(f"{_describe(row)} — the DM went out {(today - sent).days}d ago and there "
              f"is nothing since. Check where it got to.{ask}"),
    )


def _t_dm_check(row, ctx) -> Optional[dict]:
    """(6) CONNECTED A WEEK, STILL NO DM.

    The harder half of the connection pair. A week is long enough that "did you
    DM them?" is a real question rather than a prompt.
    """
    connected = _date(row, "connected")
    if connected is None or _date(row, "dm_sent_date") is not None:
        return None
    today = ctx["today"]
    days = max(1, config.CONNECTION_DM_CHECK_DAYS)
    age = (today - connected).days
    if age < days:
        return None
    return _action(
        row, type_=DM_CHECK, band=P_FOLLOWUP, due=_due(connected, days), today=today,
        anchor=connected, anchor_label="connection date",
        why=(f"connected {dl.format_date(connected)}, {age}d ago, and no DM-sent date "
             f"is recorded; threshold {days}d"),
        text=(f"{_describe(row)} — connected {age}d ago and still no DM recorded. "
              f"Send it, or write the DM-sent date in if it already went."),
    )


def _t_dm_sent_check(row, ctx) -> Optional[dict]:
    """(7) NEWLY CONNECTED — "DM sent?"

    CONNECT_DM_CHECK_HOURS after the connection, rounded up to whole days
    because the sheet holds dates and not times. A prompt rather than a chase:
    the connection is still warm and the answer is usually "yes, forgot to
    write it down".
    """
    connected = _date(row, "connected")
    if connected is None or _date(row, "dm_sent_date") is not None:
        return None
    today = ctx["today"]
    days = hours_as_days(config.CONNECT_DM_CHECK_HOURS)
    age = (today - connected).days
    if age < days:
        return None
    return _action(
        row, type_=DM_SENT_CHECK, band=P_FOLLOWUP, due=_due(connected, days), today=today,
        anchor=connected, anchor_label="connection date",
        why=(f"connected {dl.format_date(connected)}, {age}d ago, no DM-sent date; "
             f"threshold {config.CONNECT_DM_CHECK_HOURS}h (~{days}d)"),
        text=(f"{_describe(row)} — connected {age}d ago. DM sent? If it has gone, put "
              f"the date in; if not, now is while it is still warm."),
    )


def _t_mark_unresponsive(row, ctx) -> Optional[dict]:
    """(8) ENOUGH. Suggest marking the PoC Unresponsive.

    THE BOT NEVER WRITES THAT CELL. Marking somebody unresponsive ends the row
    for good — trigger 0 will stop it from then on — and a judgement with that
    consequence belongs to whoever owns the row, not to a counter.
    """
    touches = silent_touches(row)
    at = max(1, config.UNRESPONSIVE_SUGGEST_AT)
    if touches < at:
        return None
    today = ctx["today"]
    anchor, label = last_touch(row)
    anchor = anchor or today
    return _action(
        row, type_=MARK_UNRESPONSIVE, band=P_FOLLOWUP,
        due=_due(anchor, 0), today=today, anchor=anchor, anchor_label=label,
        why=f"{touches} follow-up(s) with no response; threshold {at}",
        text=(f"{_describe(row)} — {touches} follow-ups, nothing back. Worth marking "
              f"them Unresponsive in the sheet. I do not write that cell; it stops the "
              f"row for good, so it is your call."),
    )


def _t_channel_switch(row, ctx) -> Optional[dict]:
    """(9) FOUR TOUCHES ON ONE CHANNEL — try another one.

    Counsel, not a chase. The row is still live; what has failed is the medium.
    The suggestion names what was already tried, when the sheet records it, so
    it is a useful sentence rather than a generic one.
    """
    touches = silent_touches(row)
    at = max(1, config.CHANNEL_SWITCH_AT)
    if touches < at:
        return None
    today = ctx["today"]
    anchor, label = last_touch(row)
    anchor = anchor or today
    tried = _text(row, "first_contact_type")
    return _action(
        row, type_=CHANNEL_SWITCH, band=P_FOLLOWUP,
        due=_due(anchor, 0), today=today, anchor=anchor, anchor_label=label,
        why=f"{touches} follow-up(s) with no response; threshold {at}",
        text=(f"{_describe(row)} — {touches} follow-ups"
              + (f" on {tried}" if tried else "")
              + " and nothing back. Try a different channel before spending another one."),
    )


def _t_pulse_check(row, ctx) -> Optional[dict]:
    """(10) DEAL ON HOLD — a pulse, monthly, and nothing else.

    A deal parked by agreement is not a deal to chase, and treating it as one is
    how a bot gets told to stop talking about an account entirely. It gets a
    check-in every ON_HOLD_PULSE_DAYS and no follow-up, no channel counsel and
    no unresponsive suggestion — this trigger sits above the follow-up so those
    can never reach a parked row.
    """
    if not is_on_hold(row):
        return None
    today = ctx["today"]
    anchor, label = last_touch(row)
    if anchor is None:
        return None
    days = max(1, config.ON_HOLD_PULSE_DAYS)
    if (today - anchor).days < days:
        return None
    return _action(
        row, type_=PULSE_CHECK, band=P_SLOW, due=_due(anchor, days), today=today,
        anchor=anchor, anchor_label=label,
        why=(f"deal status is {_text(row, 'deal_status')!r}; last touch "
             f"{dl.format_date(anchor)}, {(today - anchor).days}d ago; pulse every {days}d"),
        text=(f"{_describe(row)} — on hold since the {label} "
              f"({dl.format_date(anchor)}). Monthly pulse: still parked, or has "
              f"something changed?"),
    )


def _t_followup(row, ctx) -> Optional[dict]:
    """(11) THE ORDINARY CADENCE, at whatever pace the lane sets.

    The catch-all, and the trigger the FOLLOWUP_GRACE_DAYS number belongs to:
    first contact plus the grace period, with nothing since. It anchors on the
    LAST TOUCH rather than on the first contact specifically, because a row
    followed up last week is not seven days stale just because it was first
    contacted in March.

    It also carries the alternate-PoC suggestion. Once a row has been followed
    up at least once with nothing back, "chase them again" is weaker advice than
    "chase them, or find somebody else at that company", so the line says both.
    """
    today = ctx["today"]
    anchor, label = last_touch(row)
    if anchor is None:
        return None
    name, days, band, lane_why = lane(row)
    age = (today - anchor).days
    if age < days:
        return None
    touches = silent_touches(row)
    alternate = (
        f" If they stay quiet, try a different PoC at {_text(row, 'company')}."
        if touches >= 1 else ""
    )
    lane_note = {
        "fast": " This one is in the fast lane.",
        "slow": " Slow lane — it is not urgent, it is just not forgotten.",
    }.get(name, "")
    return _action(
        row, type_=FOLLOWUP, band=band, due=_due(anchor, days), today=today,
        anchor=anchor, anchor_label=label,
        why=(f"{label} was {dl.format_date(anchor)}, {age}d ago; {name} lane "
             f"cadence {days}d ({lane_why})"),
        text=(f"{_describe(row)} — {age}d since the {label} and nothing since. "
              f"Follow up.{alternate}{lane_note}"),
    )


# THE ORDER IS THE PRODUCT DECISION. Read top to bottom, first match wins.
TRIGGERS = (
    (MEETING_PROPOSAL, _t_meeting_proposal),
    (SCHEDULED_REMINDER, _t_scheduled_reminder),
    (QUOTE_CHASE, _t_quote_chase),
    (PROGRESS_CHECK, _t_progress_check),
    (DM_CHECK, _t_dm_check),
    (DM_SENT_CHECK, _t_dm_sent_check),
    (MARK_UNRESPONSIVE, _t_mark_unresponsive),
    (CHANNEL_SWITCH, _t_channel_switch),
    (PULSE_CHECK, _t_pulse_check),
    (FOLLOWUP, _t_followup),
)


# -- the machine --------------------------------------------------------------


def next_action(
    row: dict, *, today: date, snoozes: Optional[dict] = None,
    scheduled: Optional[dict] = None,
) -> tuple[Optional[dict], str]:
    """(action, silent_reason) for ONE row. Exactly one of the two is set.

    The action is None when the row is stopped, snoozed, or simply has nothing
    due — and `silent_reason` says which, so a caller can tell "finished" from
    "quiet today" from "I could not read a date".

    THE SNOOZE RE-ARM. A live snooze silences the row entirely. An EXPIRED
    snooze does not: the row's normal trigger runs, and its due date is replaced
    by the snooze date. Somebody who said "follow up on the 20th" said when, and
    a bot that recomputed a different date would be overruling them — so a
    snooze that has passed shows up overdue, which is the true statement.
    """
    ctx = {
        "today": today,
        "snoozes": snoozes or {},
        "scheduled": scheduled or {},
    }

    # (0) STOP, FOREVER. Before everything, including the priority override.
    stopped = stop_reason(row)
    if stopped:
        return None, SILENT_STOPPED

    key = activation.row_key(row)

    # (1) SNOOZE.
    snooze = (ctx["snoozes"] or {}).get(key)
    snooze_until = dl.parse_date((snooze or {}).get("until_date")) if snooze else None
    if snooze_until is not None and snooze_until > today:
        return None, SILENT_SNOOZED

    for type_, fn in TRIGGERS:
        try:
            action = fn(row, ctx)
        except Exception:
            log.exception(
                "[nextaction] trigger %s failed on %r; skipping that trigger for this row",
                type_, row.get("company"),
            )
            continue
        if not action:
            continue
        if snooze_until is not None:
            # RE-ARMED. The scheduled-reminder trigger keeps its own date — that
            # is a second explicit instruction and the newer one does not
            # silently outrank it; everything else takes the snooze date.
            if action["type"] != SCHEDULED_REMINDER:
                action["due_date"] = snooze_until
                action["due_iso"] = dl.iso(snooze_until)
                action["overdue_days"] = max(0, (today - snooze_until).days)
                note = str((snooze or {}).get("note") or "").strip()
                action["why"] = (
                    f"due date re-armed to {dl.format_date(snooze_until)} because you "
                    f"asked me to come back to it then"
                    + (f" ({note})" if note else "")
                    + f". Underlying trigger: {action['why']}"
                )
        return action, ""

    if last_touch(row)[0] is None:
        return None, SILENT_NO_ANCHOR
    return None, SILENT_NOT_DUE


def sort_key(action: dict) -> tuple:
    """PRIORITY BAND FIRST, then the oldest due date, then the company.

    Band before date on purpose: a positive reply due tomorrow outranks a slow-
    lane follow-up that went overdue three weeks ago, because the reply is the
    thing that decays. Company last so the list is stable day to day.
    """
    due = action.get("due_date")
    return (
        int(action.get("priority", 9)),
        due or date.max,
        str(action.get("company") or "").lower(),
        str(action.get("poc") or "").lower(),
    )


def run(
    rows: list, *, today: Optional[date] = None, snoozes: Optional[dict] = None,
    scheduled: Optional[dict] = None, inactive: int = 0,
) -> dict:
    """THE QUEUE. Active rows in, one ranked action per row out.

    `rows` ARE ALREADY THE ACTIVE ROWS — the caller filters through
    `activation.active_rows` first, so nothing here can produce work for a row
    with no first-contact and no connection date. `inactive` is passed in only
    so the result can SAY how many were held back.

    Returns:
        {"actions": [...],   ranked, one per row that produced anything
         "by_type": {type: [actions]},
         "by_band": {band: [actions]},
         "silent": {reason: count},      why the rest produced nothing
         "stopped": [...],               rows that are finished, named
         "snoozed": [...],               rows sleeping, with their dates
         "rows": int,                    active rows considered
         "inactive": int,
         "today": date}

    IT SENDS NOTHING AND WRITES NOTHING. See the module header.
    """
    today = today or dl.today_ist()
    snoozes = snoozes or {}
    scheduled = scheduled or {}

    actions: list = []
    silent: dict = {}
    stopped: list = []
    snoozed: list = []

    for row in rows or []:
        try:
            action, reason = next_action(
                row, today=today, snoozes=snoozes, scheduled=scheduled
            )
        except Exception:
            log.exception(
                "[nextaction] the state machine failed on %r; that row produces nothing",
                row.get("company"),
            )
            silent["error"] = silent.get("error", 0) + 1
            continue
        if action:
            actions.append(action)
            continue
        silent[reason] = silent.get(reason, 0) + 1
        if reason == SILENT_STOPPED:
            stopped.append({
                "company": _text(row, "company"), "poc": _text(row, "poc"),
                "why": stop_reason(row), "sheet_row": row.get("_row"),
            })
        elif reason == SILENT_SNOOZED:
            entry = snoozes.get(activation.row_key(row)) or {}
            snoozed.append({
                "company": _text(row, "company"), "poc": _text(row, "poc"),
                "until": str(entry.get("until_date") or ""),
                "note": str(entry.get("note") or ""),
                "sheet_row": row.get("_row"),
            })

    actions.sort(key=sort_key)

    by_type: dict = {}
    by_band: dict = {}
    for action in actions:
        by_type.setdefault(action["type"], []).append(action)
        by_band.setdefault(action["priority"], []).append(action)

    log.info(
        "[nextaction] %d ACTIVE row(s) considered (%d inactive, never looked at) -> "
        "%d action(s): %s. Silent: %s. NOTHING WAS SENT — this is computation only.",
        len(rows or []), max(0, int(inactive or 0)), len(actions),
        ", ".join(f"{t}={len(v)}" for t, v in sorted(by_type.items())) or "none",
        ", ".join(f"{SILENT_LABELS.get(k, k)}={v}" for k, v in sorted(silent.items()))
        or "none",
    )

    return {
        "actions": actions,
        "by_type": by_type,
        "by_band": by_band,
        "silent": silent,
        "stopped": stopped,
        "snoozed": snoozed,
        "rows": len(rows or []),
        "inactive": max(0, int(inactive or 0)),
        "today": today,
    }


# -- the preview --------------------------------------------------------------


def preview_text(result: dict, *, max_lines: Optional[int] = None) -> str:
    """TODAY'S QUEUE, grouped by TYPE then OWNER. Sends nothing.

    Grouped by type first rather than by owner because the question behind
    "cadence preview" is *what kind of work is waiting*, and a list sorted by
    person answers a different one. Owner is the sub-grouping so each person can
    still find their own rows inside a type.

    Every line carries its due date and its reason, because a queue nobody can
    check is a queue nobody will act on.
    """
    today = result.get("today")
    cap = config.NEXT_ACTION_PREVIEW_MAX if max_lines is None else max_lines
    actions = result.get("actions") or []

    lines = [
        f"**Cadence preview — {today.strftime('%a %d %b %Y') if today else 'today'}**",
        f"_{len(actions)} action(s) across {result.get('rows', 0)} active row(s). "
        f"Nothing has been sent; this is what the queue holds right now._",
        "",
    ]
    if not actions:
        lines.append("Nothing is due. " + _silent_summary(result))
        return "\n".join(lines)

    shown = 0
    for band in sorted(result.get("by_band") or {}):
        band_actions = result["by_band"][band]
        lines.append(f"__{BAND_LABELS.get(band, band).upper()} ({len(band_actions)})__")
        # Within a band, group by type, keeping the ranked order of first
        # appearance so the headings follow the same priority the list does.
        seen_types: list = []
        for action in band_actions:
            if action["type"] not in seen_types:
                seen_types.append(action["type"])
        for type_ in seen_types:
            group = [a for a in band_actions if a["type"] == type_]
            lines.append(f"**{TYPE_LABELS.get(type_, type_)}** ({len(group)})")
            by_owner: dict = {}
            for action in group:
                by_owner.setdefault(action.get("owner") or "(unassigned)", []).append(action)
            for owner in sorted(by_owner):
                lines.append(f"  _{owner}_")
                for action in by_owner[owner]:
                    if shown >= max(1, cap):
                        break
                    due = action.get("due_iso") or "no date"
                    overdue = action.get("overdue_days") or 0
                    stamp = f"{due}{f' — {overdue}d OVERDUE' if overdue else ''}"
                    lines.append(f"    • [{stamp}] {action['text']}")
                    lines.append(f"      why: {action['why']}")
                    shown += 1
                if shown >= max(1, cap):
                    break
            if shown >= max(1, cap):
                break
        lines.append("")
        if shown >= max(1, cap):
            break

    if len(actions) > shown:
        lines.append(f"_{len(actions) - shown} more not shown (preview cap "
                     f"{max(1, cap)})._")
    lines.append(_silent_summary(result))
    return "\n".join(lines)


def _silent_summary(result: dict) -> str:
    """One line accounting for every active row that produced nothing.

    A queue that only lists what it found looks complete when it is not. This is
    the receipt for the rest.
    """
    silent = result.get("silent") or {}
    if not silent:
        return "_Every active row produced an action._"
    parts = [
        f"{count} {SILENT_LABELS.get(reason, reason)}"
        for reason, count in sorted(silent.items(), key=lambda kv: -kv[1])
    ]
    return "_No action for " + "; ".join(parts) + "._"


def _self_test() -> int:
    """`python -m nextaction` — the state machine on fixtures, offline."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")
    today = date(2026, 9, 9)          # a Wednesday
    failures = 0

    def check(name, got, want):
        nonlocal failures
        ok = got == want
        failures += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")

    def row(**kw):
        base = {"_row": 2, "company": "Acme", "poc": "Ann", "_extra": {}}
        base.update(kw)
        return base

    def act(**kw):
        return next_action(row(**kw), today=today, **kw.pop("_ctx", {}))

    print("stop, before everything")
    for closure in ("0%", "Dead", "Unresponsive", "Won", "Lost"):
        a, why = next_action(row(closure=closure, response="P",
                                 first_contacted="01-08-2026"), today=today)
        check(f"closure {closure!r} stops the row (even with a reply)", (a, why),
              (None, SILENT_STOPPED))
    a, _ = next_action(row(closure="", first_contacted="01-08-2026"), today=today)
    check("a BLANK closure does not stop the row", a is not None, True)

    print("\nthe priority override")
    a, _ = next_action(row(response="P", first_contacted="01-08-2026",
                           followups_count="9", connected="01-09-2026"), today=today)
    check("a reply beats every other trigger", a["type"], MEETING_PROPOSAL)
    check("...and is band 0", a["priority"], P_OVERRIDE)

    print("\nthe connection pair")
    a, _ = next_action(row(connected="06-09-2026"), today=today)   # 3d ago
    check("connected 3d, no DM -> DM sent?", a["type"], DM_SENT_CHECK)
    a, _ = next_action(row(connected="30-08-2026"), today=today)   # 10d ago
    check("connected 10d, no DM -> the harder DM check", a["type"], DM_CHECK)
    a, _ = next_action(row(connected="30-08-2026", dm_sent_date="31-08-2026"), today=today)
    check("DM sent 9d ago -> progress check", a["type"], PROGRESS_CHECK)

    print("\nthe type-aware ask")
    a, _ = next_action(row(dm_sent_date="31-08-2026", first_contact_type="LinkedIn"),
                       today=today)
    check("non-email first contact ASKS for contact details",
          "Ask for an email address" in a["text"], True)
    a, _ = next_action(row(dm_sent_date="31-08-2026", first_contact_type="Email"),
                       today=today)
    check("email first contact does NOT ask",
          "Ask for an email address" in a["text"], False)

    print("\ndemo and lanes")
    a, _ = next_action(row(prospect_stage="Demo", meeting_date="01-09-2026"), today=today)
    check("demo, no movement, 8d -> quote chase", a["type"], QUOTE_CHASE)
    a, _ = next_action(row(prospect_stage="Demo", meeting_date="01-09-2026",
                           next_steps="quote drafted"), today=today)
    check("...but a written next step means no chase", a["type"] if a else None, FOLLOWUP)
    check("closure 80% is the fast lane", lane(row(closure="80%"))[0], "fast")
    check("closure 50% is the SLOW lane", lane(row(closure="50%"))[0], "slow")
    check("In Progress is the fast lane", lane(row(deal_status="In Progress"))[0], "fast")

    print("\nsilent touches")
    a, _ = next_action(row(followups_count="4", first_contacted="01-08-2026"), today=today)
    check("4 silent touches -> channel switch", a["type"], CHANNEL_SWITCH)
    a, _ = next_action(row(followups_count="7", first_contacted="01-08-2026"), today=today)
    check("7 silent touches -> suggest unresponsive", a["type"], MARK_UNRESPONSIVE)
    a, _ = next_action(row(followups_count="7", response="P",
                           first_contacted="01-08-2026"), today=today)
    check("...but a reply resets the count", a["type"], MEETING_PROPOSAL)

    print("\non hold")
    a, _ = next_action(row(deal_status="On Hold", first_contacted="01-06-2026"), today=today)
    check("a parked deal gets a pulse, not a chase", a["type"], PULSE_CHECK)
    check("...at the back of the queue", a["priority"], P_SLOW)

    print("\nsnooze")
    snoozed_row = row(first_contacted="01-08-2026")
    key = activation.row_key(snoozed_row)
    live = {key: {"until_date": "2026-09-20", "note": "follow up in 11 days"}}
    a, why = next_action(snoozed_row, today=today, snoozes=live)
    check("a live snooze silences the row", (a, why), (None, SILENT_SNOOZED))
    past = {key: {"until_date": "2026-09-04", "note": "follow up on the 4th"}}
    a, _ = next_action(snoozed_row, today=today, snoozes=past)
    check("an expired snooze re-arms the due date", a["due_iso"], "2026-09-04")
    check("...and reads as overdue", a["overdue_days"], 5)

    print("\nweekends")
    check("Saturday shifts to Monday",
          shift_off_weekend(date(2026, 9, 12)), date(2026, 9, 14))
    check("Sunday shifts to Monday",
          shift_off_weekend(date(2026, 9, 13)), date(2026, 9, 14))
    check("a weekday is untouched",
          shift_off_weekend(date(2026, 9, 11)), date(2026, 9, 11))
    sat_row = row(connected="10-09-2026")     # +2d = Sat 12 Sep
    a, _ = next_action(sat_row, today=date(2026, 9, 14))
    check("no computed due date lands on a weekend", a["due_date"].weekday() < 5, True)

    print("\nexplicit schedule (the weekend exception)")
    sched_row = row(first_contacted="01-08-2026")
    sched = {activation.row_key(sched_row): [
        {"due_date": "2026-09-05", "due_time": "morning", "what": "call Ann back"}
    ]}
    a, _ = next_action(sched_row, today=today, scheduled=sched)
    check("a scheduled reminder wins over the follow-up", a["type"], SCHEDULED_REMINDER)
    check("...and keeps its exact Saturday", a["due_iso"], "2026-09-05")
    check("...which really is a Saturday", a["due_date"].weekday(), 5)

    print("\nexactly one action per row")
    busy = row(connected="01-08-2026", dm_sent_date="05-08-2026", followups_count="8",
               prospect_stage="Demo", first_contacted="01-07-2026")
    a, _ = next_action(busy, today=today)
    check("a row matching five triggers yields ONE action", isinstance(a, dict), True)
    check("...the highest-ranked one", a["type"], QUOTE_CHASE)

    print("\nrun() and the ordering")
    result = run([
        row(company="Zeta", poc="Zed", first_contacted="01-07-2026", followups_count="1"),
        row(company="Acme", poc="Ann", response="P", first_contacted="01-08-2026"),
        row(company="Beta", poc="Bob", closure="Won"),
    ], today=today, inactive=4)
    check("three rows, two actions", len(result["actions"]), 2)
    check("the reply is first", result["actions"][0]["type"], MEETING_PROPOSAL)
    check("the won deal is stopped", result["silent"].get(SILENT_STOPPED), 1)
    check("inactive is carried through", result["inactive"], 4)

    print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
