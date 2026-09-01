"""PHASE 1 OF THE SALES CADENCE — Vaishnavi's ten rules, computed once a day.

WHAT THIS IS. Vaishnavi's "Steps for Sales Bot" doc lists the things a human
would notice reading the tracker every morning: a follow-up that has gone cold,
a connection with no intro behind it, a positive reply nobody booked a meeting
off. This module is those checks, run against the OUTREACH TRACKER, and NOTHING
ELSE. It produces a list of items. It does not send anything.

    THERE IS NO PROACTIVE SEND PATH IN THIS FILE, and there must never be one.
    Every item here is handed to the EXISTING once-daily digest in bot.py, which
    is still the only unprompted message this bot writes. A rule that posted its
    own finding would undo the entire reason digest.py exists — see the header of
    that file. Nothing here can make the bot speak.

IT RUNS ON THE TRACKER, NOT THE MASTER TAB, and that is the correction this
version exists for. The master tab is status-only — Connected / Intro Sent /
Response Status / Meeting Done / Assets Shared and a "Month" that is a month. It
has no last-followed-up date, no follow-up count, no meeting date and no next
steps. Every date rule pointed at it read a blank and silently never fired, so
the cadence looked calm because it was blind. The tracker (live: the hidden
"Outreach Updates" tab) is the only tab in the spreadsheet with dates in it.

THE RULES, lettered as Vaishnavi lettered them, and the threshold each is tuned
by:

  FOLLOW-UPS
    a  stale_followup     Last followed-up date older than FOLLOWUP_STALE_DAYS
                          with no response -> chase the owner.
    b  intro_pending      Connected = Y but membrane Intro Date is blank.
    c  start_interacting  First Contacted set, never Connected, and
                          CONNECT_REMINDER_DAYS have passed.
    d  alt_channel        Follow-ups >= ALT_CHANNEL_AT with no response ->
                          recommend email / WhatsApp / a call instead.
    e  unresponsive       Follow-ups >= UNRESPONSIVE_AT with no response ->
                          ASK THE OWNER to mark the PoC unresponsive.
                          THE BOT NEVER WRITES THAT. Its only writable cell
                          anywhere is its own deadline column; marking a person
                          unresponsive is a judgement with consequences and it
                          belongs to whoever owns the row.
  RESPONSE MATRIX
    f  lock_meeting       Response positive but Next Steps blank -> lock a
                          meeting. This is the money rule: a positive reply
                          sitting with no next action is the most expensive
                          thing in the sheet.
    g  try_another_poc    A REJECTED company -> suggest a different PoC there,
                          ONCE per company. See the reconciliation below.
  MEETINGS
    h  meeting_soon       Meeting inside MEETING_PREP_DAYS -> urgent, and it is
                          what earns the one prep brief (prep.py).
    i  post_meeting       Meeting Date past and Assets Shared OR Next Steps
                          blank -> chase.
    j  nextstep_stall     Next Steps present but unchanged for
                          NEXTSTEP_STALL_DAYS. "Unchanged" is not something a
                          spreadsheet cell knows, so the bot keeps its own
                          history — see db.track_next_steps.

RULES (f) AND (g) WERE IN CONFLICT AND ARE RECONCILED HERE. Vaishnavi's doc says
"PoC response = N / no response -> suggest contacting another PoC"; the 27 Aug
meeting decided rejected leads are excluded from the cadence entirely and
revisited offline by humans. Both survive, split by what they were each for: a
REJECTED row is never chased, and instead the COMPANY gets ONE suggestion line
naming an alternative PoC. "No response" with a high follow-up count is not a
rejection at all — rules (d) and (e) already own that row.

EVERY THRESHOLD IS A PLACEHOLDER. Vaishnavi's "n" values were left unset in the
doc. The defaults in config.py are guesses, they are env vars, and tuning them
is a restart rather than a deploy.

MISSING DATA IS AN ASK, NOT A CHASE. The tracker is sparse — most rows have no
follow-up count and no next step. A rule fires as a CHASE only when every cell it
depends on is actually filled; when a required cell is blank the row becomes an
UPDATE-TRACKER fill-in ask instead ("no follow-up count or last-followed date
recorded — update the tracker"). Rules (b) and (f) are the exceptions: an empty
cell IS the signal there, which is the whole point of them. Without this split
the digest would read a blank counter as "0 follow-ups" and chase a row nobody
had touched, using the sheet's own gaps as evidence against the team.

EXCLUSION COMES FIRST. A rejected row is dropped before any rule runs — not
filtered out of the digest afterwards, dropped — and the only thing it can
produce is the one (g) suggestion for its company.

PRIORITY, from the 27 Aug meeting:
    URGENT      a positive reply awaiting our action (f), a meeting inside
                MEETING_PREP_DAYS (h), and post-meeting gaps (i)
    WAITING     the cadence items (a, c, d, e, j)
    INTRO       connected with no intro behind it (b)
    SUGGESTION  the rejected-company alternatives (g), last — a "no" is real
                work, but it is not today's work, and it must never push a
                booked meeting off the digest.

THE CAP IS HONEST. DIGEST_MAX_ITEMS items go out, urgent first, and the
UPDATE-TRACKER asks have their OWN budget (UPDATE_TRACKER_MAX) so that a sparse
sheet's fill-in requests can never eat the fifteen slots the real work needs.
What doesn't fit is COUNTED in a closing line that says how to see it, because a
silently truncated list reads as "there were only fifteen things" and that is a
lie the digest cannot afford to tell.

THIS MODULE IS PURE. It takes rows, a date and a few lookups, and returns dicts.
No sheet reads, no database writes, no Discord. That is what makes `python
cadence.py --demo` a real test of the rules rather than a test of the network.
"""
import logging
from datetime import date
from typing import Callable, Optional

import config
import deadlines as dl
import gtm_sheet

log = logging.getLogger(__name__)

# -- rule ids -----------------------------------------------------------------
RULE_STALE_FOLLOWUP = "stale_followup"        # a
RULE_INTRO_PENDING = "intro_pending"          # b
RULE_START_INTERACTING = "start_interacting"  # c
RULE_ALT_CHANNEL = "alt_channel"              # d
RULE_UNRESPONSIVE = "unresponsive"            # e
RULE_LOCK_MEETING = "lock_meeting"            # f
RULE_TRY_ANOTHER_POC = "try_another_poc"      # g
RULE_MEETING_SOON = "meeting_soon"            # h — the prep window
RULE_POST_MEETING = "post_meeting"            # i
RULE_NEXTSTEP_STALL = "nextstep_stall"        # j

# Not rules: the three things that are asks about the SHEET rather than about a
# prospect. They all land in the UPDATE-TRACKER section and share its budget.
# Not a rule either: the ONE line that accounts for every row rule (c) stopped
# chasing because it is past the cold ceiling. It is a summary, not a task, so it
# sits outside both caps — a count of suppressed rows that could itself be
# suppressed would be worse than not having the ceiling at all.
RULE_COLD_SUMMARY = "cold_summary"

RULE_FILL_IN = "fill_in"                      # a row whose rule inputs are blank
RULE_CROSSCHECK = "crosscheck"                # master and tracker disagree
RULE_DATA_QUALITY = "data_quality"            # broken formulas, stray vocabulary

RULE_LETTERS = {
    RULE_STALE_FOLLOWUP: "a",
    RULE_INTRO_PENDING: "b",
    RULE_START_INTERACTING: "c",
    RULE_ALT_CHANNEL: "d",
    RULE_UNRESPONSIVE: "e",
    RULE_LOCK_MEETING: "f",
    RULE_TRY_ANOTHER_POC: "g",
    RULE_MEETING_SOON: "h",
    RULE_POST_MEETING: "i",
    RULE_NEXTSTEP_STALL: "j",
    RULE_COLD_SUMMARY: "cold",
    RULE_FILL_IN: "fill",
    RULE_CROSSCHECK: "xchk",
    RULE_DATA_QUALITY: "dq",
}

# -- priority buckets ---------------------------------------------------------
URGENT = "urgent"
WAITING = "waiting"
INTRO = "intro"
SUGGESTION = "suggestion"

_BUCKET_RANK = {URGENT: 0, WAITING: 1, INTRO: 2, SUGGESTION: 3}

RULE_PRIORITY = {
    RULE_LOCK_MEETING: URGENT,     # positive response awaiting our action
    RULE_MEETING_SOON: URGENT,     # a meeting inside the prep window
    RULE_POST_MEETING: URGENT,     # post-meeting gap
    RULE_STALE_FOLLOWUP: WAITING,
    RULE_START_INTERACTING: WAITING,
    RULE_ALT_CHANNEL: WAITING,
    RULE_UNRESPONSIVE: WAITING,
    RULE_NEXTSTEP_STALL: WAITING,
    RULE_INTRO_PENDING: INTRO,     # "then b", after the waiting items
    RULE_TRY_ANOTHER_POC: SUGGESTION,
    RULE_COLD_SUMMARY: WAITING,    # never actually ranked — it bypasses the cap
}

# -- digest sections ----------------------------------------------------------
# Vaishnavi's daily cadence, in her words and IN HER ORDER:
#   "Follow-ups for this week · Intros to be done this week · Meetings for this
#    week · Update tracker · Assets to be shared this week."
# The section keys are defined HERE so a rule and its section can never drift
# apart; digest.py re-exports them because it owns rendering.
SECTION_FOLLOWUPS = "cadence_followups"
SECTION_INTROS = "cadence_intros"
SECTION_MEETINGS = "cadence_meetings"
SECTION_UPDATES = "cadence_updates"
SECTION_ASSETS = "cadence_assets"

RULE_SECTION = {
    RULE_STALE_FOLLOWUP: SECTION_FOLLOWUPS,
    RULE_START_INTERACTING: SECTION_FOLLOWUPS,
    RULE_ALT_CHANNEL: SECTION_FOLLOWUPS,
    # (j) is "no movement for 10 days -> follow-up" in her words, so it sits
    # with the follow-ups rather than with the tracker asks.
    RULE_NEXTSTEP_STALL: SECTION_FOLLOWUPS,
    RULE_TRY_ANOTHER_POC: SECTION_FOLLOWUPS,
    # The cold summary is a follow-up line: it is about people we contacted and
    # never got to.
    RULE_COLD_SUMMARY: SECTION_FOLLOWUPS,
    RULE_INTRO_PENDING: SECTION_INTROS,
    RULE_LOCK_MEETING: SECTION_MEETINGS,
    RULE_MEETING_SOON: SECTION_MEETINGS,
    # (e) asks a human to WRITE something in the sheet, which is what the
    # update-tracker line is for. So do the fill-in, cross-check and
    # data-quality asks.
    RULE_UNRESPONSIVE: SECTION_UPDATES,
    RULE_FILL_IN: SECTION_UPDATES,
    RULE_CROSSCHECK: SECTION_UPDATES,
    RULE_DATA_QUALITY: SECTION_UPDATES,
    RULE_POST_MEETING: SECTION_ASSETS,
}

CADENCE_SECTIONS = (
    SECTION_FOLLOWUPS,
    SECTION_INTROS,
    SECTION_MEETINGS,
    SECTION_UPDATES,
    SECTION_ASSETS,
)

# The three UPDATE-TRACKER kinds. They share UPDATE_TRACKER_MAX rather than the
# DIGEST_MAX_ITEMS slots the actual work needs.
UPDATE_RULES = (RULE_FILL_IN, RULE_CROSSCHECK, RULE_DATA_QUALITY)


# -- reading a row ------------------------------------------------------------


def _text(row: dict, role: str) -> str:
    """A row cell as trimmed text. Spreadsheet ERROR values were already turned
    into blanks by `gtm_sheet.clean_cell` at parse time; this re-applies it so a
    hand-built row (a test, a demo fixture) behaves the same way."""
    return gtm_sheet.clean_cell(row.get(role))


def _row_blob(row: dict) -> str:
    """Every readable cell of a row as one lower-cased string.

    Used only by the rejection test, which has to look everywhere: the team
    writes "rejected" in whichever column is nearest to hand, and a marker in
    Other Updates means exactly what a marker in Status means.
    """
    parts = [str(v) for k, v in row.items() if not str(k).startswith("_")]
    parts += [str(v) for v in (row.get("_extra") or {}).values()]
    return " | ".join(parts).lower()


def _marker_hit(row: dict) -> str:
    """The explicit written-off marker somebody typed into this row, or "".

    Deliberately a bounded phrase match, never an inference. A blank cell is not
    a rejection, and neither is a low score or an old date: rejection has to be
    written down.
    """
    blob = _row_blob(row)
    if not blob:
        return ""
    for marker in config.CADENCE_REJECTED_MARKERS or []:
        m = str(marker or "").strip().lower()
        if not m:
            continue
        # Bounded on both sides so "lost" doesn't match "lostrand Ltd".
        for boundary_l in ("", " ", "|", ":", "-", "(", ","):
            probe = f"{boundary_l}{m}"
            idx = blob.find(probe)
            while idx != -1:
                after = blob[idx + len(probe): idx + len(probe) + 1]
                if after in ("", " ", "|", ":", ".", ",", ")", "-", "/", ";"):
                    return m
                idx = blob.find(probe, idx + 1)
    return ""


def is_rejected(row: dict) -> tuple[bool, str]:
    """Has this row been written off? Returns (rejected, the reason found).

    EXCLUDED FROM EVERY RULE AND EVERY DIGEST SECTION when true — never chased,
    revisited offline by people. Two ways to be rejected, and both are things a
    human wrote:
      - the Response cell says so: "N", "N - Rejected", "No" (section 2 of the
        phase-1 spec maps all three to REJECTED);
      - one of CADENCE_REJECTED_MARKERS appears anywhere in the row.

    The one thing a rejected row still produces is rule (g)'s single
    suggestion for its COMPANY, which is not a chase and does not address the
    rejected PoC.
    """
    if responded(row) == gtm_sheet.RESPONSE_NEGATIVE:
        return True, f"response {_text(row, 'response') or 'N'!r}"
    marker = _marker_hit(row)
    if marker:
        return True, f"marker {marker!r}"
    return False, ""


def responded(row: dict) -> str:
    """The row's response polarity, using the sheet's own vocabulary."""
    return gtm_sheet.response_status(row.get("response"))


def no_response(row: dict) -> bool:
    """Nothing has come back at all. Distinct from "they said no"."""
    return responded(row) == gtm_sheet.RESPONSE_NONE


def positive_response(row: dict) -> bool:
    """A reply we can act on: Y, P, "Did Respond", "P - Positive/In Progress".

    An UNCLASSIFIED reply counts too — somebody wrote something in the cell, so
    they DID come back, and the cost of asking about a real reply is far lower
    than the cost of missing one. The raw wording is reported separately by the
    data-quality flag so the sheet can standardise it.
    """
    return responded(row) in (gtm_sheet.RESPONSE_POSITIVE, gtm_sheet.RESPONSE_UNKNOWN)


def followups_recorded(row: dict) -> bool:
    """Did anybody write a follow-up count in this row?

    THIS IS THE DIFFERENCE BETWEEN "zero follow-ups" AND "nobody recorded the
    follow-ups". Rules (d) and (e) count follow-ups, the column is blank on most
    rows, and reading a blank as 0 would have been harmless — but reading it as
    a fact ("0 follow-ups, so nothing to escalate") is how a real gap stays
    invisible. A blank routes the row to an UPDATE-TRACKER ask instead.
    """
    return _text(row, "followups_count") != ""


def followups_count(row: dict) -> int:
    """Total follow-ups as a number. Blank or unreadable is 0 — and callers must
    check `followups_recorded` first, because 0 here means both."""
    raw = _text(row, "followups_count")
    digits = "".join(ch for ch in raw if ch.isdigit())
    return int(digits) if digits else 0


def days_since(value, today: date) -> Optional[int]:
    """Calendar days between a sheet date and today, or None if unreadable.

    CALENDAR days, not working days: Vaishnavi's thresholds are written as plain
    "n days" and a follow-up that went cold over a long weekend is just as cold
    on Tuesday. The deadline machinery elsewhere in this bot uses working days,
    and that difference is deliberate.
    """
    parsed = gtm_sheet.sheet_date(value)
    if parsed is None:
        return None
    return (today - parsed).days


def row_identity(row: dict) -> tuple[str, str]:
    """(company, poc) as displayed. A row always has a company — `_parse_values`
    drops the ones that don't."""
    return _text(row, "company") or "(unnamed row)", _text(row, "poc")


def describe_row(row: dict) -> str:
    """The company · PoC · designation prefix every cadence line opens with."""
    company, poc = row_identity(row)
    parts = [company]
    if poc:
        designation = _text(row, "poc_designation")
        parts.append(f"{poc}{f' ({designation})' if designation else ''}")
    return " · ".join(parts)


def row_key(row: dict) -> str:
    """The stable identity of a sheet row, for carry-forward and cross-checks.

    Built from the company and PoC, never from the sheet row NUMBER, which moves
    the moment somebody sorts the tab and would restart every item's age.
    """
    company, poc = row_identity(row)
    return f"{gtm_sheet.normalise_header(company)}|{gtm_sheet.normalise_header(poc)}"


# -- the rules ----------------------------------------------------------------


def _item(
    row: dict, *, rule: str, text: str, today: date, age_days: int = 0,
    detail: str = "", key: str = "",
) -> dict:
    """One finding. `key` is what carries an item's AGE across days, so it is
    built from the rule and the row identity — never from the sheet row number,
    which moves whenever somebody sorts the tab."""
    company, poc = row_identity(row)
    bucket = RULE_PRIORITY.get(rule, WAITING)
    return {
        "rule": rule,
        "letter": RULE_LETTERS.get(rule, "?"),
        "priority": bucket,
        "section": RULE_SECTION.get(rule, SECTION_UPDATES),
        "company": company,
        "poc": poc,
        "owner": _text(row, "owner"),
        "sheet_row": row.get("_row"),
        "text": text,
        "detail": detail,
        "age_days": max(0, int(age_days or 0)),
        "key": key or f"cadence:{rule}:{row_key(row)}",
        "_row": row,
    }


def rule_stale_followup(row: dict, *, today: date) -> Optional[dict]:
    """(a) Last followed-up date older than n days AND no response.

    Requires a READABLE last-followed-up date. Without one there is nothing to
    measure staleness against, and the row goes to the fill-in ask instead —
    see `fill_in_gaps`.
    """
    if not no_response(row):
        return None
    age = days_since(row.get("last_followed_up"), today)
    if age is None or age < max(0, config.FOLLOWUP_STALE_DAYS):
        return None
    return _item(
        row, rule=RULE_STALE_FOLLOWUP, today=today, age_days=age,
        text=(
            f"{describe_row(row)} — last followed up {age}d ago, still no response. "
            f"Follow up or park it."
        ),
        detail=f"Last followed up date is {_text(row, 'last_followed_up')!r}; "
               f"threshold is {config.FOLLOWUP_STALE_DAYS}d.",
    )


def rule_intro_pending(row: dict, *, today: date) -> Optional[dict]:
    """(b) Connected = Y but membrane Intro Date is blank.

    ONE OF THE TWO RULES WHERE A BLANK IS THE SIGNAL, so it is exempt from the
    missing-data routing: the empty intro date is the finding, not a gap in the
    evidence.
    """
    if not gtm_sheet.is_yes(row.get("connected")):
        return None
    if _text(row, "intro_date"):
        return None
    return _item(
        row, rule=RULE_INTRO_PENDING, today=today,
        text=f"{describe_row(row)} — connected, but no membrane intro date recorded. Intro pending.",
        detail="Connected says yes and membrane Intro Date is empty.",
    )


def contact_age(row: dict, *, today: date) -> Optional[int]:
    """Days since First Contacted on a row that was NEVER CONNECTED, else None.

    The shared input of rule (c) and the cold ceiling — one reading of the row,
    so the two can never disagree about which bucket it belongs in.
    """
    if gtm_sheet.is_yes(row.get("connected")):
        return None
    return days_since(row.get("first_contacted"), today)


def is_cold_contact(row: dict, *, today: date) -> bool:
    """Past the ceiling: contacted long ago, never connected. COLD, not "yet to
    start".

    WHY A CEILING AND NOT A HIGHER FLOOR. Rule (c) fired on 756 of the live
    sheet's 886 rows, because most of the tracker is March-June outreach that
    never connected. Raising CONNECT_REMINDER_DAYS does nothing about that:
    those rows are older than any threshold, so a higher floor still lets every
    one of them through. Only a ceiling separates "we contacted them last week
    and should follow through" from "we emailed them in March and never got
    anywhere", and only the first of those is a task.

    `CONNECT_REMINDER_MAX_DAYS = 0` turns the ceiling off.
    """
    ceiling = max(0, config.CONNECT_REMINDER_MAX_DAYS)
    if not ceiling:
        return False
    age = contact_age(row, today=today)
    return age is not None and age > ceiling


def rule_start_interacting(row: dict, *, today: date) -> Optional[dict]:
    """(c) First Contacted set, never Connected, n days gone — AND NOT TOO LONG.

    A blank Connected cell means NOT connected (section 2 of the spec), so only
    the First Contacted date has to be readable for this to be evidence.

    THE CEILING IS PART OF THE RULE. Past CONNECT_REMINDER_MAX_DAYS the row is
    cold and "start interacting" is the wrong sentence to put in front of
    somebody — those rows are counted in one summary line instead
    (`cold_summary`) and listed in full on demand.

    ONLY RULE (c) IS SUPPRESSED, not the whole row. A March row that is still
    being followed up every week is live work, and its (a), (d) and (e) lines
    are evidence of that; what is wrong on it is specifically the nudge to
    *start*, which is six months out of date.
    """
    if gtm_sheet.is_yes(row.get("connected")):
        return None
    age = contact_age(row, today=today)
    if age is None or age < max(0, config.CONNECT_REMINDER_DAYS):
        return None
    if is_cold_contact(row, today=today):
        return None
    return _item(
        row, rule=RULE_START_INTERACTING, today=today, age_days=age,
        text=(
            f"{describe_row(row)} — first contacted {age}d ago and still not connected. "
            f"Start interacting."
        ),
        detail=f"First Contacted is {_text(row, 'first_contacted')!r}; Connected is "
               f"{_text(row, 'connected') or 'blank'!r}; the window is "
               f"{config.CONNECT_REMINDER_DAYS}-{config.CONNECT_REMINDER_MAX_DAYS}d.",
    )


def rule_alt_channel(row: dict, *, today: date) -> Optional[dict]:
    """(d) n follow-ups, no response -> change the channel, not the message.

    Requires a RECORDED follow-up count. A blank counter is not zero follow-ups,
    it is an unrecorded number, and the row becomes a fill-in ask instead.
    """
    if not no_response(row) or not followups_recorded(row):
        return None
    count = followups_count(row)
    threshold = max(1, config.ALT_CHANNEL_AT)
    # Rule (e) supersedes this one: past the unresponsive threshold the ask is
    # "mark them unresponsive", not "try WhatsApp".
    if count < threshold or count >= max(1, config.UNRESPONSIVE_AT):
        return None
    return _item(
        row, rule=RULE_ALT_CHANNEL, today=today,
        text=(
            f"{describe_row(row)} — {count} follow-ups, no response. Try another channel "
            f"(email / WhatsApp / call)."
        ),
        detail=f"Total follow-ups till date is {count}; threshold is {threshold}.",
    )


def rule_unresponsive(row: dict, *, today: date) -> Optional[dict]:
    """(e) n follow-ups, no response -> ask the OWNER to mark them unresponsive.

    The bot asks. The bot does not write. Its only writable cell anywhere in any
    spreadsheet is its own deadline column, and marking a person unresponsive is
    a judgement with consequences for the relationship — it belongs to whoever
    owns the row.
    """
    if not no_response(row) or not followups_recorded(row):
        return None
    count = followups_count(row)
    threshold = max(1, config.UNRESPONSIVE_AT)
    if count < threshold:
        return None
    _company, poc = row_identity(row)
    who = poc or "the PoC"
    return _item(
        row, rule=RULE_UNRESPONSIVE, today=today,
        text=(
            f"{describe_row(row)} — {count} follow-ups, no response. Please mark {who} "
            f"\"unresponsive\" in the tracker (I don't write that cell)."
        ),
        detail=f"Total follow-ups till date is {count}; threshold is {threshold}. The bot "
               f"never writes this — it is the owner's call.",
    )


def rule_lock_meeting(row: dict, *, today: date) -> Optional[dict]:
    """(f) Response positive but Next Steps blank -> lock a meeting. URGENT.

    THE SECOND RULE WHERE A BLANK IS THE SIGNAL, so it too is exempt from the
    missing-data routing. An empty Next Steps cell next to a positive reply is
    the finding.

    A ROW WITH ANY MEETING DATE ON IT DOES NOT FIRE HERE, because on it "lock a
    meeting" is the wrong sentence:
      - a meeting today or later is already BOOKED. It is locked. If it is
        close, rule (h) says so instead.
      - a meeting in the PAST with no next step behind it is rule (i)'s row —
        it asks for the assets and the next step, which is the specific thing
        missing, rather than for another meeting in the abstract. Rule (i) now
        fires whenever EITHER assets or next steps is blank, so every past
        meeting reaching this point is already covered there and firing both
        would say the same row twice in two sections.
    """
    if not positive_response(row):
        return None
    if _text(row, "next_steps"):
        return None
    if gtm_sheet.sheet_date(row.get("meeting_date")) is not None:
        return None
    return _item(
        row, rule=RULE_LOCK_MEETING, today=today,
        text=(
            f"{describe_row(row)} — responded ({_text(row, 'response') or 'positive'}) "
            f"with no next step recorded. Lock a meeting."
        ),
        detail="Response is positive and Next Steps is empty.",
    )


def rule_post_meeting(row: dict, *, today: date) -> Optional[dict]:
    """(i) Meeting happened, and Assets Shared OR Next Steps is blank.

    OR, not AND. Vaishnavi's rule is "Meeting done but Assets Shared / Next
    Steps blank -> follow-up on next steps", and a meeting that produced a next
    step but no assets is still a promise nobody kept. Reading it as AND meant a
    row had to fail on both counts before anyone heard about it.

    Requires a READABLE meeting date; without one there is no "past".
    """
    age = days_since(row.get("meeting_date"), today)
    if age is None or age <= 0:
        return None
    assets = _text(row, "assets_shared")
    steps = _text(row, "next_steps")
    if assets and steps:
        return None
    missing = " and ".join(
        m for m in (
            "no assets shared" if not assets else "",
            "no next steps" if not steps else "",
        ) if m
    )
    return _item(
        row, rule=RULE_POST_MEETING, today=today, age_days=age,
        text=(
            f"{describe_row(row)} — met {age}d ago, {missing}. "
            f"Send what you promised and write the next step."
        ),
        detail=f"Meeting Date is {_text(row, 'meeting_date')!r}; Assets Shared is "
               f"{assets or 'empty'!r}, Next Steps is {steps or 'empty'!r}.",
    )


def rule_nextstep_stall(
    row: dict, *, today: date, stall_days: Optional[Callable[[dict], int]] = None
) -> Optional[dict]:
    """(j) Next Steps present but unchanged for n days.

    `stall_days(row)` supplies the days-unchanged count, because the sheet has
    no history: it comes from the bot's own `nextstep_state` table. Without that
    callable this rule cannot fire, which is the correct behaviour for a dry run
    with no database.
    """
    text = _text(row, "next_steps")
    if not text or stall_days is None:
        return None
    try:
        unchanged = int(stall_days(row) or 0)
    except Exception:
        log.debug("[cadence] next-step clock failed for %r", row.get("company"), exc_info=True)
        return None
    if unchanged < max(1, config.NEXTSTEP_STALL_DAYS):
        return None
    short = text if len(text) <= 60 else text[:57] + "…"
    return _item(
        row, rule=RULE_NEXTSTEP_STALL, today=today, age_days=unchanged,
        text=(
            f"{describe_row(row)} — next step \"{short}\" unchanged for {unchanged}d. "
            f"Move it or update the tracker."
        ),
        detail=f"Next Steps has read the same since {unchanged} day(s) ago; threshold is "
               f"{config.NEXTSTEP_STALL_DAYS}d.",
    )


def rule_meeting_soon(row: dict, *, today: date) -> Optional[dict]:
    """(h) A meeting inside MEETING_PREP_DAYS. URGENT, and it earns a prep brief.

    The brief itself is built by prep.py and attached to the same digest, once
    per meeting rather than every day until it happens.
    """
    meeting = gtm_sheet.sheet_date(row.get("meeting_date"))
    if meeting is None:
        return None
    days_away = (meeting - today).days
    if days_away < 0 or days_away > max(0, config.MEETING_PREP_DAYS):
        return None
    when = "today" if days_away == 0 else ("tomorrow" if days_away == 1 else f"in {days_away}d")
    item = _item(
        row, rule=RULE_MEETING_SOON, today=today,
        text=f"{describe_row(row)} — meeting {when} ({dl.format_date(meeting)}). Prep it.",
        detail=f"Meeting Date is {dl.iso(meeting)}, {days_away}d away; the prep window is "
               f"{config.MEETING_PREP_DAYS}d.",
    )
    item["meeting_date"] = dl.iso(meeting)
    item["days_away"] = days_away
    return item


# -- the cold summary: one line for everything rule (c) stopped chasing --------


def _month_span(rows: list[dict]) -> str:
    """"Mar-Jun" for a set of rows, from their First Contacted dates.

    Named months rather than "older than 30 days", because "first contacted
    Mar-Jun" is the sentence that makes a reader recognise the cohort — it is
    the outreach push they remember, not an interval.
    """
    months = sorted(
        {d.replace(day=1) for d in (
            gtm_sheet.sheet_date(r.get("first_contacted")) for r in rows
        ) if d is not None}
    )
    if not months:
        return ""
    first, last = months[0], months[-1]
    if first == last:
        return first.strftime("%b %Y")
    if first.year == last.year:
        return f"{first.strftime('%b')}-{last.strftime('%b %Y')}"
    return f"{first.strftime('%b %Y')}-{last.strftime('%b %Y')}"


def cold_summary(cold_rows: list[dict], *, today: date) -> Optional[dict]:
    """ONE line accounting for every row past the cold ceiling. None when there
    are none.

    IT IS A COUNT, NOT A LIST, AND IT IS NOT CAPPED. 756 identical "start
    interacting" nudges is not a work list, so they are suppressed — but a
    suppression nobody is told about is just a bot hiding rows, so the count
    goes out every day and the full list is one question away. The line sits
    outside both caps for the same reason: a summary of what was held back that
    could itself be held back would be worse than not having the ceiling.
    """
    if not cold_rows:
        return None
    span = _month_span(cold_rows)
    companies = len({
        gtm_sheet.normalise_header(_text(r, "company")) for r in cold_rows
    } - {""})
    oldest = 0
    for row in cold_rows:
        age = contact_age(row, today=today)
        if age is not None:
            oldest = max(oldest, age)

    item = _item(
        {"company": "", "poc": "", "_extra": {}},
        rule=RULE_COLD_SUMMARY, today=today, age_days=oldest,
        key=f"cadence:{RULE_COLD_SUMMARY}",
        text=(
            f"{len(cold_rows)} cold contacts across {companies} compan"
            f"{'y' if companies == 1 else 'ies'} (never connected"
            + (f", first contacted {span}" if span else "")
            + f") — ask 'cold list' to see them."
        ),
        detail=(
            f"Past the {config.CONNECT_REMINDER_MAX_DAYS}d cold ceiling, so rule (c) does "
            f"not chase them individually; the oldest was first contacted {oldest}d ago. "
            f"Their other rules still fire — only the 'start interacting' nudge is "
            f"suppressed."
        ),
    )
    item["cold_rows"] = len(cold_rows)
    return item


def cold_list_text(result: dict, *, max_lines: Optional[int] = None) -> str:
    """The UNCAPPED cold list, as text, for the "cold list" question.

    Grouped by company, oldest first, because the decision this list exists to
    support is per-company ("do we go back at Unilever at all?") rather than per
    person.
    """
    limit = config.CADENCE_FULL_LIST_MAX if max_lines is None else max_lines
    rows = result.get("cold") or []
    today = result.get("today") or dl.today_ist()
    if not rows:
        return (
            "No cold contacts — every never-connected row is inside the "
            f"{config.CONNECT_REMINDER_MAX_DAYS}d window and is chased individually."
        )

    # GROUPED ON THE NORMALISED NAME, displayed under the first spelling seen.
    # The summary line counts companies the same way, and the two disagreeing by
    # one because the sheet spells a name two ways ("FERMAT" / "FERMAT") is the
    # kind of small wrongness that costs a report its credibility.
    by_company: dict[str, list[dict]] = {}
    display: dict[str, str] = {}
    for row in rows:
        company, _poc = row_identity(row)
        ckey = gtm_sheet.normalise_header(company) or company
        display.setdefault(ckey, company)
        by_company.setdefault(ckey, []).append(row)

    def oldest_of(group: list[dict]) -> int:
        ages = [contact_age(r, today=today) for r in group]
        return max([a for a in ages if a is not None] or [0])

    ordered = sorted(by_company.items(), key=lambda kv: (-oldest_of(kv[1]), kv[0]))
    lines = [
        f"**Cold contacts — {len(rows)} row(s) across {len(ordered)} "
        f"compan{'y' if len(ordered) == 1 else 'ies'}.** "
        f"Contacted more than {config.CONNECT_REMINDER_MAX_DAYS}d ago and never connected, "
        f"so rule (c) does not chase them one by one. Nothing here is rejected — these are "
        f"leads that went quiet before they started."
    ]
    for ckey, group in ordered:
        company = display.get(ckey, ckey)
        if limit and len(lines) >= limit:
            lines.append(f"…truncated at {limit} lines.")
            return "\n".join(lines)
        age = oldest_of(group)
        names = ", ".join(
            _text(r, "poc") or "(no PoC)" for r in group[:4]
        ) + ("…" if len(group) > 4 else "")
        lines.append(f"• **{company}** — {len(group)} PoC(s), oldest {age}d: {names}")
    return "\n".join(lines)


# -- (g): one suggestion per rejected COMPANY ---------------------------------


def company_suggestions(
    rejected_rows: list[dict], *, today: date,
    mapping_lookup: Optional[Callable[[str], list]] = None,
    active_companies: Optional[set] = None,
) -> list[dict]:
    """(g) ONE alternative-PoC suggestion per company that has a rejection.

    Per COMPANY, not per row: three PoCs at Unilever saying no is one piece of
    news ("Unilever needs a different door"), and three identical lines is the
    kind of repetition that gets a digest muted.

    Skipped entirely for a company that still has a LIVE row somewhere in the
    tracker — somebody there is already talking to us, and "try another PoC" is
    just wrong when there is an active conversation in flight.

    Candidates come from the Researcher Buyer Mapping and arrive WITH THEIR
    CAVEATS or not at all: a name from that sheet is only usable alongside its
    staleness verdict and its departure check, so the lookup passed in is
    expected to have applied them already.
    """
    active = active_companies or set()
    seen: dict[str, dict] = {}
    for row in rejected_rows:
        company, poc = row_identity(row)
        ckey = gtm_sheet.normalise_header(company)
        if not ckey or ckey in active:
            continue
        entry = seen.setdefault(ckey, {"row": row, "company": company, "pocs": []})
        if poc:
            entry["pocs"].append(poc)

    out: list[dict] = []
    for ckey, entry in seen.items():
        company = entry["company"]
        names: list[str] = []
        if mapping_lookup is not None:
            try:
                candidates = mapping_lookup(company) or []
            except Exception:
                log.debug("[cadence] mapping lookup failed for %r", company, exc_info=True)
                candidates = []
            names = [str(c).strip() for c in candidates if str(c).strip()][:3]
        said_no = ", ".join(entry["pocs"][:3]) or "the PoC"
        if names:
            suggestion = f"Mapped alternatives: {'; '.join(names)}."
        else:
            suggestion = ("No mapped alternative on file — worth a look at the org "
                          "yourself.")
        item = _item(
            entry["row"], rule=RULE_TRY_ANOTHER_POC, today=today,
            key=f"cadence:{RULE_TRY_ANOTHER_POC}:{ckey}",
            text=(
                f"{company} — {said_no} said no. Nobody else there is in play; try a "
                f"different PoC. {suggestion}"
            ),
            detail=(
                f"{len(entry['pocs']) or 1} rejected row(s) at {company}, and no live row "
                f"for that company. Suggestion only — the bot never re-contacts a "
                f"rejected lead."
            ),
        )
        # A suggestion is about the COMPANY, so it does not carry a PoC of its
        # own; naming the rejected person here would read as "chase them again".
        item["poc"] = ""
        out.append(item)
    return out


# -- missing-data routing (section 4 of the spec) ------------------------------

# role -> (what to call it in the ask, which rules go blind without it)
_GAP_LABELS = {
    "last_followed_up": ("last-followed-up date", "a"),
    "followups_count": ("follow-up count", "d/e"),
    "first_contacted": ("first-contacted date", "c"),
    "meeting_date": ("meeting date", "h/i"),
}


def fill_in_gaps(row: dict, *, today: date) -> Optional[dict]:
    """The UPDATE-TRACKER fill-in ask for one row, or None when nothing is missing.

    THIS IS WHAT A SPARSE SHEET PRODUCES INSTEAD OF A FALSE CHASE. A rule fires
    as a chase only when every cell it depends on is filled; when one is blank
    the row lands here.

    A gap is only worth asking about on a row that is ACTUALLY IN PLAY, which is
    the difference between a useful ask and eight hundred of them:
      - the missing last-followed-up date is asked for only when the row shows
        engagement (connected, or an intro went out, or a follow-up count
        exists). A row nobody has ever connected with is rule (c)'s business,
        not a bookkeeping gap.
      - the missing follow-up count is asked for only when there IS evidence of
        following up (a last-followed-up date, or a connection). "We have been
        following up and nobody wrote down how many" is a real gap; "we never
        started" is not.
      - a date cell with TEXT IN IT that cannot be read is always worth asking
        about, whatever else the row says: somebody typed something and the bot
        cannot use it.
    """
    connected = gtm_sheet.is_yes(row.get("connected"))
    intro = _text(row, "intro_date") != ""
    followed = _text(row, "last_followed_up") != ""
    counted = followups_recorded(row)
    engaged = connected or intro or counted or followed

    gaps: list[str] = []
    unreadable: list[str] = []

    for role in ("last_followed_up", "first_contacted", "meeting_date"):
        raw = _text(row, role)
        if raw and gtm_sheet.sheet_date(raw) is None:
            unreadable.append(f"{_GAP_LABELS[role][0]} {raw!r} is not a date I can read")

    if engaged and not followed and no_response(row):
        gaps.append("no last-followed-up date")
    if (followed or connected) and not counted:
        gaps.append("no follow-up count")
    if not _text(row, "first_contacted") and engaged:
        gaps.append("no first-contacted date")

    if not gaps and not unreadable:
        return None

    what = "; ".join(gaps + unreadable)
    # Age it by the oldest thing on the row, so the most-neglected rows are the
    # ones that survive the UPDATE_TRACKER_MAX cap.
    age = 0
    for role in ("first_contacted", "intro_date", "last_followed_up"):
        seen = days_since(row.get(role), today)
        if seen is not None:
            age = max(age, seen)

    return _item(
        row, rule=RULE_FILL_IN, today=today, age_days=age,
        text=f"{describe_row(row)} — {what}. Update the tracker.",
        detail=(
            "The rules that read those cells cannot fire on this row, so it is an "
            "ask rather than a chase: "
            + ", ".join(
                f"({_GAP_LABELS[r][1]})" for r in _GAP_LABELS
                if _GAP_LABELS[r][0] in what
            )
        ),
    )


# -- the master/tracker cross-check (section 1 of the spec) --------------------

# (master role, tracker test, what the disagreement is called). The tracker test
# takes a row and returns True / False / None, where None means "the tracker does
# not say", which is never a disagreement.
def _tracker_connected(row: dict) -> Optional[bool]:
    return True if gtm_sheet.is_yes(row.get("connected")) else False


def _tracker_intro(row: dict) -> Optional[bool]:
    return bool(_text(row, "intro_date"))


def _tracker_meeting_done(row: dict) -> Optional[bool]:
    meeting = gtm_sheet.sheet_date(row.get("meeting_date"))
    if meeting is None:
        return False if not _text(row, "meeting_date") else None
    return True


def _tracker_assets(row: dict) -> Optional[bool]:
    return bool(_text(row, "assets_shared"))


_CROSSCHECKS = (
    ("connected", _tracker_connected, "Connected"),
    ("intro_sent", _tracker_intro, "Intro Sent / membrane Intro Date"),
    ("meeting_done", _tracker_meeting_done, "Meeting Done / Meeting Date"),
    ("assets_shared", _tracker_assets, "Assets Shared"),
)


def crosscheck(
    tracker_rows: list[dict], master_rows: list[dict], *, today: date,
    limit: int = 5,
) -> list[dict]:
    """Where the master tab and the tracker disagree about the same row.

    THE BOT REPORTS THE DISAGREEMENT AND NEVER PICKS A WINNER. Both tabs are
    maintained by people; deciding which one is right is a judgement about what
    actually happened, and a bot that quietly adopted one of them would be
    rewriting a status nobody asked it to touch. So every disagreement becomes an
    UPDATE-TRACKER line naming both values, and a human resolves it.

    Matched on company + PoC. A row that exists in one tab and not the other is
    NOT reported: the two tabs cover different windows (587 master rows against
    886 tracker rows today), and "this row is missing" would be hundreds of lines
    of noise about a difference that is by design.
    """
    if not master_rows or not tracker_rows:
        return []
    by_key = {row_key(r): r for r in tracker_rows}

    out: list[dict] = []
    reported: set = set()
    for mrow in master_rows:
        key = row_key(mrow)
        trow = by_key.get(key)
        if trow is None or key in reported:
            # BOTH TABS HOLD DUPLICATE (company, PoC) ROWS — the live master tab
            # lists "Sandeep Jha" twice — and without this each copy reported the
            # same disagreement again. One row, one line.
            continue
        # Response polarity is the one comparison worth making across the two
        # vocabularies, and it is made through the shared normaliser so
        # "P - Positive/In Progress" and "P" are the same answer.
        differences: list[str] = []
        m_resp = gtm_sheet.response_status(mrow.get("response_status"))
        t_resp = responded(trow)
        if m_resp != t_resp:
            differences.append(
                f"response: master says {_text(mrow, 'response_status') or 'blank'!r} "
                f"({m_resp}), tracker says {_text(trow, 'response') or 'blank'!r} ({t_resp})"
            )
        for mrole, tracker_test, label in _CROSSCHECKS:
            raw = _text(mrow, mrole)
            if not raw:
                continue
            master_says = gtm_sheet.is_yes(raw)
            tracker_says = tracker_test(trow)
            if tracker_says is None or master_says == tracker_says:
                continue
            differences.append(
                f"{label}: master says {raw!r}, tracker says "
                f"{'yes' if tracker_says else 'no'}"
            )
        if not differences:
            continue
        reported.add(key)
        out.append(_item(
            trow, rule=RULE_CROSSCHECK, today=today,
            key=f"cadence:{RULE_CROSSCHECK}:{key}",
            text=(
                f"{describe_row(trow)} — master and tracker disagree: "
                + "; ".join(differences[:2])
                + ". Please fix whichever is wrong (I don't guess which)."
            ),
            detail="Nightly master/tracker consistency check. The bot never infers a "
                   "winner — both values are shown and a human decides.",
        ))
        if len(out) >= max(1, limit):
            break
    return out


# -- data-quality flags (section 6 of the spec) -------------------------------

# Master columns and the vocabulary each of them is allowed to hold. A value
# from outside its own list is the signature of a row whose cells have shifted.
_MASTER_VOCAB = {
    "connected": {"yes", "no"},
    "intro_sent": {"yes", "no"},
    "meeting_done": {"yes", "no"},
    "assets_shared": {"yes", "no"},
    "response_status": {"p positive in progress", "n rejected", "no response",
                        "p", "n", "y", ""},
}


def data_quality_flags(
    *, tracker_rows: list[dict], master_rows: Optional[list[dict]] = None,
    error_cells: Optional[list] = None, today: Optional[date] = None,
    tabs: Optional[dict] = None,
) -> list[dict]:
    """The three sheet-health findings, as (flag_key, signature, text) dicts.

    Each is ONE line, and each carries a SIGNATURE — a short string describing
    exactly what was found. The caller (bot.py) uses the signature to dedup the
    flag UNTIL IT IS FIXED: a flag whose signature has not changed since it was
    last reported is not repeated, because a daily reminder about a broken
    formula everybody already knows about is how a digest gets muted.

    `error_cells` is [(sheet_row, header, raw)] collected at parse time, from any
    tab — `tabs` maps a tab title to that list so the flag can name the tab.
    """
    today = today or dl.today_ist()
    out: list[dict] = []

    # 1. BROKEN FORMULAS. The values API returns "#REF!" as text; every one of
    #    them was normalised to empty when the row was parsed, which is right,
    #    and would otherwise be completely invisible.
    per_tab = dict(tabs or {})
    if error_cells:
        per_tab.setdefault("(cadence source)", list(error_cells))
    for title, cells in sorted(per_tab.items()):
        if not cells:
            continue
        by_col: dict[str, int] = {}
        for _row, header, _raw in cells:
            by_col[str(header)] = by_col.get(str(header), 0) + 1
        cols = ", ".join(
            f"{name!r} ({n} cell{'s' if n != 1 else ''})"
            for name, n in sorted(by_col.items(), key=lambda kv: -kv[1])[:4]
        )
        signature = f"{title}:{sorted(by_col.items())}"
        out.append({
            "flag_key": f"broken_formula:{title}",
            "signature": signature,
            "text": (
                f"Tab {title!r} has {len(cells)} cell(s) holding a spreadsheet error "
                f"(#REF! / #N/A / #VALUE!) in {cols}. I read those as EMPTY — a broken "
                f"formula is not a value. Fix the formula or clear the cells."
            ),
        })

    # 2. MISALIGNED MASTER ROWS. A "Mar-2026" in Connected or a "No Response" in
    #    Meeting Done means that row's cells have shifted, and every aggregate
    #    computed from it is wrong.
    misaligned: list[str] = []
    for mrow in master_rows or []:
        wrong = []
        for role, allowed in _MASTER_VOCAB.items():
            value = gtm_sheet.normalise_header(_text(mrow, role))
            if value and value not in allowed:
                wrong.append(f"{role}={_text(mrow, role)!r}")
        if wrong:
            company, poc = row_identity(mrow)
            misaligned.append(
                f"row {mrow.get('_row')} ({company}{f' / {poc}' if poc else ''}): "
                + ", ".join(wrong[:3])
            )
    if misaligned:
        out.append({
            "flag_key": "master_misaligned",
            "signature": f"{len(misaligned)}:{misaligned[:5]}",
            "text": (
                f"{len(misaligned)} master row(s) appear misaligned — a cell holds a "
                f"value from the wrong column's vocabulary: "
                + "; ".join(misaligned[:3])
                + (f" (and {len(misaligned) - 3} more)" if len(misaligned) > 3 else "")
            ),
        })

    # 3. RESPONSE VALUES NOBODY STANDARDISED. Listed ONCE so the sheet can be
    #    tidied; the rows themselves are still classified, by the prefix rules.
    strays: dict[str, int] = {}
    for row in list(tracker_rows or []) + list(master_rows or []):
        for role in ("response", "response_status"):
            raw = _text(row, role)
            if raw and not gtm_sheet.is_known_response(raw):
                strays[raw] = strays.get(raw, 0) + 1
    if strays:
        listed = ", ".join(
            f"{value!r} ({n})"
            for value, n in sorted(strays.items(), key=lambda kv: -kv[1])[:6]
        )
        out.append({
            "flag_key": "response_vocabulary",
            "signature": str(sorted(strays.items())),
            "text": (
                f"{len(strays)} response value(s) outside the known vocabulary "
                f"(Y / P / N / Did Respond / Awaited / No Response): {listed}. I read "
                f"them as 'they replied, polarity unclear'. Worth standardising."
            ),
        })

    return out


def quality_items(flags: list[dict], *, today: date) -> list[dict]:
    """Data-quality flags -> UPDATE-TRACKER items, in the standard item shape."""
    out: list[dict] = []
    for flag in flags or []:
        fake_row = {"company": "(the sheet)", "poc": "", "_extra": {}}
        item = _item(
            fake_row, rule=RULE_DATA_QUALITY, today=today,
            key=f"cadence:{RULE_DATA_QUALITY}:{flag['flag_key']}",
            text=flag["text"],
            detail="Sheet health, reported once and not repeated until it changes.",
        )
        item["flag_key"] = flag["flag_key"]
        item["signature"] = flag.get("signature", "")
        item["company"] = ""
        out.append(item)
    return out


# The order rules are evaluated in. One row can legitimately produce several
# items — a company can be both "connected with no intro" and "meeting
# tomorrow" — and both are true things somebody has to do.
ALL_RULES = (
    RULE_LOCK_MEETING,
    RULE_MEETING_SOON,
    RULE_POST_MEETING,
    RULE_STALE_FOLLOWUP,
    RULE_START_INTERACTING,
    RULE_ALT_CHANNEL,
    RULE_UNRESPONSIVE,
    RULE_NEXTSTEP_STALL,
    RULE_INTRO_PENDING,
    RULE_TRY_ANOTHER_POC,
)

_RULE_FUNCS = {
    RULE_STALE_FOLLOWUP: rule_stale_followup,
    RULE_INTRO_PENDING: rule_intro_pending,
    RULE_START_INTERACTING: rule_start_interacting,
    RULE_ALT_CHANNEL: rule_alt_channel,
    RULE_UNRESPONSIVE: rule_unresponsive,
    RULE_LOCK_MEETING: rule_lock_meeting,
    RULE_POST_MEETING: rule_post_meeting,
    RULE_MEETING_SOON: rule_meeting_soon,
}


def evaluate_row(
    row: dict, *, today: date,
    stall_days: Optional[Callable[[dict], int]] = None,
) -> list[dict]:
    """Every rule that fires on ONE row. [] for a rejected row — the exclusion
    happens here, before any rule is evaluated, not as a filter afterwards.

    Rule (g) is NOT evaluated here: it is per-company, computed once over all the
    rejected rows by `company_suggestions`.
    """
    rejected, why = is_rejected(row)
    if rejected:
        log.debug("[cadence] excluding %r — rejected (%s)", row.get("company"), why)
        return []

    out: list[dict] = []
    for rule in ALL_RULES:
        if rule == RULE_TRY_ANOTHER_POC:
            continue
        try:
            if rule == RULE_NEXTSTEP_STALL:
                item = rule_nextstep_stall(row, today=today, stall_days=stall_days)
            else:
                item = _RULE_FUNCS[rule](row, today=today)
        except Exception:
            log.exception(
                "[cadence] rule %s failed on row %r; skipping that rule for this row",
                rule, row.get("company"),
            )
            continue
        if item:
            out.append(item)
    return out


# -- the daily run ------------------------------------------------------------


def sort_key(item: dict) -> tuple:
    """URGENT first, then WAITING, then INTRO, then SUGGESTION; within a bucket
    the oldest thing first, then alphabetically so the order is stable day to
    day."""
    return (
        _BUCKET_RANK.get(item.get("priority"), 9),
        -int(item.get("age_days") or 0),
        str(item.get("company") or "").lower(),
        str(item.get("rule") or ""),
    )


_UPDATE_KIND_RANK = {RULE_DATA_QUALITY: 0, RULE_CROSSCHECK: 1, RULE_FILL_IN: 2}


def _update_within_kind_key(item: dict) -> tuple:
    """Order INSIDE one update-tracker kind: oldest first, then alphabetical."""
    return (
        -int(item.get("age_days") or 0),
        str(item.get("company") or "").lower(),
        str(item.get("text") or ""),
    )


def order_updates(items: list[dict]) -> list[dict]:
    """UPDATE-TRACKER order: INTERLEAVED across the three kinds, not blocked.

    UPDATE_TRACKER_MAX is five, and the live sheet produces five cross-check
    disagreements and a hundred fill-in asks. Ranked in blocks, the five
    disagreements took the whole budget and the digest never once asked anybody
    to fill in a missing follow-up count — which is the ask the whole
    missing-data route exists to make. So the kinds take turns: the first of
    each, then the second of each, and a cap of any size carries a bit of
    everything that is wrong with the sheet today.
    """
    buckets: dict[str, list[dict]] = {}
    for item in items:
        buckets.setdefault(item.get("rule"), []).append(item)
    ordered: list[tuple] = []
    for rule, group in buckets.items():
        group.sort(key=_update_within_kind_key)
        rank = _UPDATE_KIND_RANK.get(rule, 9)
        for turn, item in enumerate(group):
            ordered.append(((turn, rank), item))
    ordered.sort(key=lambda pair: pair[0])
    return [item for _k, item in ordered]


def run(
    rows: list[dict], *, today: Optional[date] = None,
    mapping_lookup: Optional[Callable[[str], list]] = None,
    stall_days: Optional[Callable[[dict], int]] = None,
    limit: Optional[int] = None,
    urgent_limit: Optional[int] = None,
    update_limit: Optional[int] = None,
    master_rows: Optional[list[dict]] = None,
    error_cells: Optional[list] = None,
    error_cells_by_tab: Optional[dict] = None,
    quality_seen: Optional[Callable[[str, str], bool]] = None,
) -> dict:
    """THE DAILY CADENCE. Rows in, a ranked and capped result out.

    Returns:
        {"items": [...],        the capped, ranked cadence items for the digest
         "held": [...],         what the caps left out, in full
         "all": [...],          every cadence item, ranked
         "updates": [...],      the capped UPDATE-TRACKER asks
         "updates_held": [...], what the update cap left out
         "updates_all": [...],  every UPDATE-TRACKER ask, ranked
         "cold": [...],         the raw rows past the cold ceiling ("cold list")
         "cold_summary": {...}, the ONE line that accounts for them, or None
         "quality": [...],      the raw data-quality flags (for dedup recording)
         "excluded": int,       rows dropped as rejected
         "counts": {rule: n},   how many rows each rule fired on
         "rows": int,           rows considered
         "today": date}

    THREE BUDGETS, NOT ONE.
      - URGENT_MAX (`urgent_limit`) — a positive reply awaiting a next step (f),
        a meeting in the prep window (h), a post-meeting gap (i). NEVER
        truncated in practice: this is a hard ceiling against a broken sheet,
        not a target. Those items are what Vaishnavi prioritised, and cutting
        them defeats the digest.
      - DIGEST_MAX_ITEMS (`limit`) — everything else in the cadence.
      - UPDATE_TRACKER_MAX (`update_limit`) — the tracker asks.
    The cold summary sits outside all three: it is one line, and it is the line
    that keeps the ceiling honest.

    Passing 0 for any of them means NO cap, which is what the on-demand full
    list asks for; `limit=0` alone lifts the urgent ceiling too, since "give me
    everything" cannot sensibly mean "everything except the urgent overflow".

    `quality_seen(flag_key, signature)` returns True when that exact flag has
    already been reported and nothing about it has changed — the "deduped until
    fixed" rule. Omit it and every flag is reported.
    """
    today = today or dl.today_ist()
    cap = config.DIGEST_MAX_ITEMS if limit is None else limit
    ucap = config.UPDATE_TRACKER_MAX if update_limit is None else update_limit
    if urgent_limit is not None:
        gcap = urgent_limit
    elif limit == 0:
        gcap = 0                      # "everything" includes the urgent overflow
    else:
        gcap = config.URGENT_MAX

    items: list[dict] = []
    updates: list[dict] = []
    rejected_rows: list[dict] = []
    cold_rows: list[dict] = []
    live_companies: set = set()
    excluded = 0

    for row in rows or []:
        rejected, _why = is_rejected(row)
        if rejected:
            excluded += 1
            rejected_rows.append(row)
            continue
        live_companies.add(gtm_sheet.normalise_header(_text(row, "company")))
        if is_cold_contact(row, today=today):
            # Rule (c) has already declined to fire on it; this is what the
            # summary line counts and what "cold list" returns.
            cold_rows.append(row)
        items.extend(evaluate_row(row, today=today, stall_days=stall_days))
        try:
            gap = fill_in_gaps(row, today=today)
        except Exception:
            log.exception("[cadence] the fill-in check failed on %r", row.get("company"))
            gap = None
        if gap:
            updates.append(gap)

    # (g), once per rejected company that has nothing live in flight.
    try:
        items.extend(company_suggestions(
            rejected_rows, today=today, mapping_lookup=mapping_lookup,
            active_companies=live_companies,
        ))
    except Exception:
        log.exception("[cadence] the per-company suggestions failed; skipping rule (g)")

    # The nightly master/tracker cross-check.
    if config.CADENCE_CROSSCHECK_ENABLED and master_rows:
        try:
            updates.extend(crosscheck(
                rows or [], master_rows, today=today,
                limit=max(1, config.CADENCE_CROSSCHECK_MAX),
            ))
        except Exception:
            log.exception("[cadence] the master/tracker cross-check failed; skipping it")

    # Sheet health, deduped until it changes.
    quality: list[dict] = []
    if config.CADENCE_DATA_QUALITY_ENABLED:
        try:
            quality = data_quality_flags(
                tracker_rows=rows or [], master_rows=master_rows,
                error_cells=error_cells, tabs=error_cells_by_tab, today=today,
            )
        except Exception:
            log.exception("[cadence] the data-quality scan failed; skipping the flags")
            quality = []
        fresh = []
        for flag in quality:
            try:
                if quality_seen is not None and quality_seen(
                    flag["flag_key"], flag.get("signature", "")
                ):
                    log.info(
                        "[cadence] data-quality flag %s unchanged since it was last "
                        "reported — not repeating it", flag["flag_key"],
                    )
                    continue
            except Exception:
                log.debug("[cadence] quality dedup check failed", exc_info=True)
            fresh.append(flag)
        updates.extend(quality_items(fresh, today=today))

    items.sort(key=sort_key)
    updates = order_updates(updates)

    cold_item = None
    try:
        cold_item = cold_summary(cold_rows, today=today)
    except Exception:
        log.exception("[cadence] the cold summary failed; the cold rows are unreported")

    counts: dict[str, int] = {}
    for item in items + updates:
        counts[item["rule"]] = counts.get(item["rule"], 0) + 1
    if cold_item:
        counts[RULE_COLD_SUMMARY] = 1

    # THE SPLIT CAP. The urgent items are budgeted separately and, at any sane
    # URGENT_MAX, never truncated — they are the reason the digest exists. The
    # rest of the cadence shares DIGEST_MAX_ITEMS.
    urgent = [i for i in items if i["priority"] == URGENT]
    rest = [i for i in items if i["priority"] != URGENT]
    gshown, gheld = (
        (urgent[:gcap], urgent[gcap:]) if (gcap and len(urgent) > gcap) else (urgent, [])
    )
    rshown, rheld = (
        (rest[:cap], rest[cap:]) if (cap and len(rest) > cap) else (rest, [])
    )
    if gheld:
        log.warning(
            "[cadence] %d URGENT item(s) over URGENT_MAX=%s and held back. That ceiling "
            "is a guard against a broken sheet, not a target — if this is real, raise it "
            "or look at why every row reads as positive.",
            len(gheld), gcap,
        )
    # The summary is prepended rather than ranked: it is not a task competing
    # for a slot, it is the receipt for the rows that were suppressed.
    shown = ([cold_item] if cold_item else []) + gshown + rshown
    held = gheld + rheld
    ushown, uheld = (
        (updates[:ucap], updates[ucap:]) if (ucap and len(updates) > ucap) else (updates, [])
    )

    log.info(
        "[cadence] %d row(s) considered, %d excluded as rejected, %d cold (past the "
        "%sd ceiling), %d cadence item(s) (%d urgent / %d waiting / %d intro / "
        "%d suggestion) + %d update-tracker ask(s); showing %d urgent (cap %s) + %d other "
        "(cap %s) + %d ask(s) (cap %s), %d held",
        len(rows or []), excluded, len(cold_rows), config.CONNECT_REMINDER_MAX_DAYS,
        len(items),
        len(urgent),
        sum(1 for i in items if i["priority"] == WAITING),
        sum(1 for i in items if i["priority"] == INTRO),
        sum(1 for i in items if i["priority"] == SUGGESTION),
        len(updates), len(gshown), gcap or "none", len(rshown), cap or "none",
        len(ushown), ucap or "none", len(held) + len(uheld),
    )
    for rule in ALL_RULES + (RULE_COLD_SUMMARY,) + UPDATE_RULES:
        if counts.get(rule):
            log.info("[cadence]   rule %s (%s): %d item(s)",
                     RULE_LETTERS[rule], rule, counts[rule])

    return {
        "items": shown,
        "held": held,
        "all": items,
        "updates": ushown,
        "updates_held": uheld,
        "updates_all": updates,
        "cold": cold_rows,
        "cold_summary": cold_item,
        "quality": quality,
        "excluded": excluded,
        "counts": counts,
        "rows": len(rows or []),
        "today": today,
    }


def overflow_line(held: int, updates_held: int = 0) -> str:
    """The one closing line that makes the cap honest. "" when nothing was cut."""
    total = max(0, int(held or 0)) + max(0, int(updates_held or 0))
    if total <= 0:
        return ""
    return f"{total} more held — ask 'full cadence list' for everything."


def sections(items: list[dict]) -> dict[str, list[dict]]:
    """Ranked items -> {digest section key: [items]}, order preserved."""
    out: dict[str, list[dict]] = {key: [] for key in CADENCE_SECTIONS}
    for item in items:
        out.setdefault(item.get("section") or SECTION_UPDATES, []).append(item)
    return out


def meetings_needing_prep(items: list[dict]) -> list[dict]:
    """The rule (h) items, soonest first — the prep-brief candidates."""
    found = [i for i in items if i.get("rule") == RULE_MEETING_SOON]
    found.sort(key=lambda i: (int(i.get("days_away") or 0), str(i.get("company") or "")))
    return found


def full_list_text(result: dict, *, max_lines: Optional[int] = None) -> str:
    """The UNCAPPED list, as text, for the on-demand "full cadence list" answer.

    Grouped by priority with the rule letter on every line, because the question
    behind the request is usually "what did the digest leave out and why". The
    UPDATE-TRACKER asks are included, under their own heading, since they are the
    other half of what the cap holds back.
    """
    limit = config.CADENCE_FULL_LIST_MAX if max_lines is None else max_lines
    items = result.get("all") or []
    updates = result.get("updates_all") or []
    cold = result.get("cold") or []
    if not items and not updates:
        return "Nothing fires today — no cadence items at all."

    lines = [
        f"**Full cadence list — {len(items)} item(s) and {len(updates)} tracker ask(s)** "
        f"from {result.get('rows', 0)} row(s); "
        f"{result.get('excluded', 0)} rejected row(s) excluded."
    ]
    if cold:
        # Named here rather than enumerated: this list is already long, and the
        # cold rows have their own question.
        lines.append(
            f"Plus {len(cold)} cold contact(s) past the "
            f"{config.CONNECT_REMINDER_MAX_DAYS}d ceiling, not chased individually — "
            f"ask 'cold list' for those."
        )
    groups = [
        (URGENT, "URGENT"), (WAITING, "WAITING"), (INTRO, "INTROS"),
        (SUGGESTION, "SUGGESTIONS"),
    ]
    for bucket, title in groups:
        group = [i for i in items if i.get("priority") == bucket]
        if not group:
            continue
        lines.append("")
        lines.append(f"**{title} ({len(group)})**")
        for item in group:
            if limit and len(lines) >= limit:
                lines.append(f"…truncated at {limit} lines.")
                return "\n".join(lines)
            lines.append(f"• [{item['letter']}] {item['text']}")
    if updates:
        lines.append("")
        lines.append(f"**UPDATE TRACKER ({len(updates)})**")
        for item in updates:
            if limit and len(lines) >= limit:
                lines.append(f"…truncated at {limit} lines.")
                return "\n".join(lines)
            lines.append(f"• [{item['letter']}] {item['text']}")
    return "\n".join(lines)


# -- dry run ------------------------------------------------------------------


def dry_run_text(
    result: dict, *, source: str = "", staleness: str = "", roles: Optional[list] = None,
) -> str:
    """What a dry run prints: which tab got which role, which rows each rule fires
    on TODAY, and then the cap.

    This is the operator-facing view — the rule letter, the row, and the REASON
    in terms of the cells that were read, so a firing can be checked against the
    sheet without reading this file.
    """
    today = result.get("today")
    lines = [
        "=" * 78,
        f"CADENCE DRY RUN — {today} (nothing is sent; this is a read-only report)",
        f"source: {source or 'unknown'}" + (f"   [{staleness}]" if staleness else ""),
        f"rows considered: {result.get('rows', 0)}   "
        f"rejected+excluded: {result.get('excluded', 0)}   "
        f"cold: {len(result.get('cold') or [])}   "
        f"cadence items: {len(result.get('all') or [])}   "
        f"tracker asks: {len(result.get('updates_all') or [])}",
        "=" * 78,
        "",
    ]

    if roles:
        lines.append("TAB ROLES — which real tab matched which signature today")
        lines.append("-" * 78)
        for kind, label, titles in roles:
            lines.append(f"  {kind:<18} {label}")
            lines.append(f"  {'':<18} -> {titles}")
        lines.append("")

    lines += [
        "THRESHOLDS IN FORCE (all env-tunable, all placeholders until Vaishnavi tunes them):",
        f"  FOLLOWUP_STALE_DAYS={config.FOLLOWUP_STALE_DAYS}  "
        f"CONNECT_REMINDER_DAYS={config.CONNECT_REMINDER_DAYS}  "
        f"ALT_CHANNEL_AT={config.ALT_CHANNEL_AT}",
        f"  CONNECT_REMINDER_MAX_DAYS={config.CONNECT_REMINDER_MAX_DAYS} (the cold "
        f"ceiling)  UNRESPONSIVE_AT={config.UNRESPONSIVE_AT}  "
        f"NEXTSTEP_STALL_DAYS={config.NEXTSTEP_STALL_DAYS}",
        f"  MEETING_PREP_DAYS={config.MEETING_PREP_DAYS}  "
        f"URGENT_MAX={config.URGENT_MAX} (hard ceiling; urgent is never truncated)",
        f"  DIGEST_MAX_ITEMS={config.DIGEST_MAX_ITEMS} (everything else)  "
        f"UPDATE_TRACKER_MAX={config.UPDATE_TRACKER_MAX}",
        "",
    ]

    counts = result.get("counts") or {}
    everything = (result.get("all") or []) + (result.get("updates_all") or [])
    cold_item = result.get("cold_summary")
    if cold_item:
        everything = everything + [cold_item]
    lines.append("PER RULE — which rows fired today")
    lines.append("-" * 78)
    for rule in ALL_RULES + (RULE_COLD_SUMMARY,) + UPDATE_RULES:
        fired = [i for i in everything if i["rule"] == rule]
        letter = RULE_LETTERS[rule]
        lines.append(
            f"({letter}) {rule:<18} {counts.get(rule, 0):>4} item(s)"
            + ("" if fired else "   — nothing today")
        )
        for item in fired[:12]:
            where = f"row {item['sheet_row']}" if item.get("sheet_row") else "row ?"
            lines.append(f"      · {item['company'] or '(sheet)'} / {item['poc'] or '—'} "
                         f"[{where}] [{item['priority']}]")
            lines.append(f"        why: {item['detail']}")
        if len(fired) > 12:
            lines.append(f"      · …and {len(fired) - 12} more")
    lines.append("")

    shown = result.get("items") or []
    ushown = result.get("updates") or []
    held = result.get("held") or []
    uheld = result.get("updates_held") or []
    lines.append(
        f"DIGEST AS IT WOULD POST — urgent ceiling {config.URGENT_MAX} (never truncated), "
        f"other-cadence cap {config.DIGEST_MAX_ITEMS}, tracker-ask cap "
        f"{config.UPDATE_TRACKER_MAX}"
    )
    lines.append("-" * 78)
    by_section = sections(shown + ushown)
    for key in CADENCE_SECTIONS:
        group = by_section.get(key) or []
        if not group:
            continue
        lines.append(f"  [{key}] ({len(group)})")
        for item in group:
            lines.append(f"    • [{item['letter']}/{item['priority']}] {item['text']}")
    if not shown and not ushown:
        lines.append("  (nothing would post today)")
    line = overflow_line(len(held), len(uheld))
    if line:
        lines.append("")
        lines.append(f"  {line}")
        lines.append(
            "  (held back: "
            + ", ".join(f"{i['company']}[{i['letter']}]" for i in (held + uheld)[:20])
            + ("…" if len(held) + len(uheld) > 20 else "") + ")"
        )
    lines.append("")
    return "\n".join(lines)


_DEMO_ROWS = [
    # (a) stale follow-up
    {"_row": 2, "company": "Acme Labs", "poc": "Priya R", "poc_designation": "Head of Data",
     "last_followed_up": "2026-08-10", "response": "", "connected": "Y",
     "intro_date": "2026-07-01", "followups_count": "2", "owner": "Vaishnavi", "_extra": {}},
    # (b) connected, no intro
    {"_row": 3, "company": "Borealis", "poc": "Sam K", "connected": "Yes", "intro_date": "",
     "response": "", "first_contacted": "2026-08-20", "last_followed_up": "2026-08-27",
     "followups_count": "1", "owner": "Kushal", "_extra": {}},
    # (c) contacted, never connected
    {"_row": 4, "company": "Cinder", "poc": "Dev M", "first_contacted": "2026-08-01",
     "connected": "", "response": "", "_extra": {}},
    # (d) 4 follow-ups, silent
    {"_row": 5, "company": "Dynamo", "poc": "Ana T", "followups_count": "4", "response": "",
     "last_followed_up": "2026-08-26", "connected": "Y", "intro_date": "2026-08-02", "_extra": {}},
    # (e) 8 follow-ups, silent
    {"_row": 6, "company": "Everest", "poc": "Rob N", "followups_count": "8", "response": "",
     "last_followed_up": "2026-08-27", "connected": "Y", "intro_date": "2026-07-11", "_extra": {}},
    # (f) positive, no next steps  -> URGENT
    {"_row": 7, "company": "Fathom", "poc": "Lea W", "response": "P", "next_steps": "",
     "connected": "Y", "intro_date": "2026-08-05", "_extra": {}},
    # (g) said no -> a company-level SUGGESTION, and excluded from every rule
    {"_row": 8, "company": "Gantry", "poc": "Ivan S", "response": "N", "connected": "Y",
     "intro_date": "2026-08-06", "_extra": {}},
    # (i) meeting done, nothing after it -> URGENT. Assets shared, next steps not:
    #     the OR is the point, this used to need both to be blank.
    {"_row": 9, "company": "Halcyon", "poc": "Mei L", "meeting_date": "2026-08-21",
     "assets_shared": "Deck", "next_steps": "", "response": "P", "connected": "Y",
     "intro_date": "2026-08-01", "_extra": {}},
    # (h) meeting soon -> URGENT + a prep brief. The date cell holds TWO dates,
    #     which is what a rescheduled meeting looks like in the live sheet.
    {"_row": 10, "company": "Ionic", "poc": "Tara B", "meeting_date": "12-Aug-2026\n30-Aug-2026",
     "response": "P", "next_steps": "Send pricing", "connected": "Y",
     "intro_date": "2026-08-12", "_extra": {}},
    # explicitly written off -> excluded from everything
    {"_row": 11, "company": "Junco", "poc": "Omar F", "response": "", "status": "Rejected",
     "followups_count": "9", "connected": "Y", "_extra": {}},
    # PAST THE COLD CEILING: first contacted in March, never connected. Rule (c)
    # must NOT fire on it; it is counted in the one cold-summary line instead.
    {"_row": 13, "company": "Lantern", "poc": "Ravi D", "first_contacted": "2026-03-15",
     "connected": "", "response": "", "_extra": {}},
    {"_row": 14, "company": "Lantern", "poc": "Asha B", "first_contacted": "2026-04-02",
     "connected": "", "response": "", "_extra": {}},
    # a row with the follow-up gaps: connected, followed up, no count recorded.
    # It must NOT fire (d) or (e) — it becomes a fill-in ask.
    {"_row": 12, "company": "Kestrel", "poc": "Nils P", "connected": "Y",
     "intro_date": "2026-07-20", "last_followed_up": "2026-08-01", "followups_count": "",
     "response": "", "first_contacted": "2026-07-01", "_extra": {}},
]

_DEMO_MASTER = [
    # Agrees with the tracker on Acme, disagrees on Borealis (master says the
    # intro went out; the tracker's intro date is blank).
    {"_row": 2, "company": "Acme Labs", "poc": "Priya R", "connected": "Yes",
     "intro_sent": "Yes", "response_status": "No Response", "meeting_done": "No",
     "assets_shared": "No", "_extra": {}},
    {"_row": 3, "company": "Borealis", "poc": "Sam K", "connected": "Yes",
     "intro_sent": "Yes", "response_status": "No Response", "meeting_done": "No",
     "assets_shared": "No", "_extra": {}},
    # A misaligned row: a month in Connected, a response in Meeting Done.
    {"_row": 4, "company": "Cinder", "poc": "Dev M", "connected": "Mar-2026",
     "intro_sent": "No", "response_status": "No Response", "meeting_done": "No Response",
     "assets_shared": "No", "_extra": {}},
]


def _self_test() -> int:
    """`python cadence.py [--demo]` — the dry run.

    With `--demo` it runs the rules against built-in rows, which needs no sheet,
    no network and no database: it tests the RULES. Without it, it reads the real
    tracker tab and reports what would fire today.
    """
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")
    demo = "--demo" in sys.argv

    if demo:
        today = date(2026, 8, 28)

        def stall(row: dict) -> int:
            return 12 if (row.get("company") == "Ionic") else 0

        result = run(
            _DEMO_ROWS, today=today, stall_days=stall,
            mapping_lookup=lambda c: ["Dr Ada Vance (Tier 2, mapped 5 wks ago)"],
            master_rows=_DEMO_MASTER,
            error_cells_by_tab={"Demo Tab": [(7, "Dates", "#REF!")]},
        )
        print(dry_run_text(result, source="--demo fixture rows (no sheet read)"))
        print(full_list_text(result))

        everything = result["all"] + result["updates_all"]
        fired = {(i["company"], i["rule"]) for i in everything}
        expected = {
            "Acme Labs": RULE_STALE_FOLLOWUP,
            "Borealis": RULE_INTRO_PENDING,
            "Cinder": RULE_START_INTERACTING,
            "Dynamo": RULE_ALT_CHANNEL,
            "Everest": RULE_UNRESPONSIVE,
            "Fathom": RULE_LOCK_MEETING,
            "Halcyon": RULE_POST_MEETING,
            "Ionic": RULE_MEETING_SOON,
            "Kestrel": RULE_FILL_IN,
        }
        problems = [
            f"rules that did not fire: {c}:{r}"
            for c, r in expected.items() if (c, r) not in fired
        ]
        chased = [
            i for i in everything
            if i["company"] in ("Junco", "Gantry") and i["rule"] != RULE_TRY_ANOTHER_POC
        ]
        if chased:
            problems.append(
                "a rejected/written-off row was chased by "
                + ", ".join(sorted({i["rule"] for i in chased}))
                + " — exclusion is broken"
            )
        if len([i for i in result["all"] if i["rule"] == RULE_TRY_ANOTHER_POC]) != 2:
            problems.append("expected exactly one (g) suggestion per rejected company")
        if not [i for i in result["all"] if i["rule"] == RULE_TRY_ANOTHER_POC]:
            problems.append("no per-company suggestion for the rejected company")
        if [i for i in everything if i["company"] == "Kestrel"
                and i["rule"] in (RULE_ALT_CHANNEL, RULE_UNRESPONSIVE)]:
            problems.append("a blank follow-up count was read as a number — (d)/(e) fired")
        if not meetings_needing_prep(result["all"]):
            problems.append("no meeting_soon item for Ionic (the two-date cell)")
        if not [i for i in result["updates_all"] if i["rule"] == RULE_CROSSCHECK]:
            problems.append("the master/tracker cross-check found nothing on Borealis")
        if not [i for i in result["updates_all"] if i["rule"] == RULE_DATA_QUALITY]:
            problems.append("no data-quality flag for the #REF! cell / misaligned master row")
        if [i for i in everything if i["company"] == "Lantern"]:
            problems.append(
                "a row past the cold ceiling produced an individual item — rule (c) is "
                "still chasing the March cohort"
            )
        if len(result["cold"]) != 2:
            problems.append(
                "expected 2 cold rows past the ceiling, got %d" % len(result["cold"])
            )
        if not result.get("cold_summary"):
            problems.append("no cold summary line for the suppressed rows")
        elif result["cold_summary"] not in result["items"]:
            problems.append("the cold summary was subject to the cap; it must not be")
        if [i for i in result["held"] if i["priority"] == URGENT]:
            problems.append("an URGENT item was truncated — it has its own budget")
        if problems:
            for p in problems:
                print(f"FAIL: {p}")
            return 1
        print("PASS: every rule fired on its row; the rejected and written-off rows "
              "produced no chases; blank cells became asks, not chases; the cold "
              "cohort was summarised rather than chased; no urgent item was cut.")
        print()
        print(cold_list_text(result))
        return 0

    try:
        tab, source = gtm_sheet.SHEETS.cadence_tab()
    except gtm_sheet.SheetAccessError as e:
        print(f"Could not read the sheet: {e}\n{getattr(e, 'remedy', '')}")
        return 2
    if tab is None:
        print("No tab carries the tracker signature — the cadence cannot run.")
        return 2
    staleness = ""
    try:
        staleness = gtm_sheet.SHEETS.staleness_note(tab) or ""
    except Exception:
        pass
    master = gtm_sheet.SHEETS.tab(gtm_sheet.MASTER)
    errors = {}
    for kind in (gtm_sheet.TRACKER, gtm_sheet.MASTER, gtm_sheet.RESEARCHER_LINES,
                 gtm_sheet.PIPELINE, gtm_sheet.FUNNEL):
        for t in gtm_sheet.SHEETS.tabs_of(kind):
            if t.error_cells:
                errors[t.title] = t.error_cells
    result = run(
        tab.rows, master_rows=(master.rows if master else None),
        error_cells_by_tab=errors,
    )
    print(dry_run_text(
        result, source=f"{source} tab {tab.title!r}", staleness=staleness,
        roles=gtm_sheet.SHEETS.role_assignment(),
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
