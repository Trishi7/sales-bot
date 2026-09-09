"""Reading the canonical tab as a PIPELINE rather than as a grid of strings.

`gtm_sheet.py` turns a worksheet into rows keyed by role. This module turns those
rows into the small judgements other modules are written in terms of: when was
this row last touched, is it open, has it been worked at all, what does the
funnel look like this week.

THE THREE ROW-HYGIENE FLAGS ARE GONE — hot_rows, stalled_rows, dead_deal_rows,
all_flags, digest_flag_line, flag_message, FLAG_HOT / FLAG_STALLED / FLAG_DEAD
and their two settings. They were the old per-row "what next" logic, and
`nextaction.py` — the NEXT-ACTION STATE MACHINE — replaces them outright.

WHY REPLACED RATHER THAN KEPT. The flags answered "is something wrong with this
row"; a row could be HOT and DEAD-DEAL at once, and `all_flags` carried an
ordering to decide which of the two to say out loud. That is a system with three
opinions and a tie-break. The state machine answers a better question — "what is
the single next thing somebody does about this row, when is it due, who owns it"
— and it cannot produce two answers, because its triggers are evaluated in order
and the first match wins.

WHAT SURVIVES HERE, and why it is not part of that move:
  - `last_touch`, `future_dates`, `is_open`, `is_engaged` and friends: small,
    honest readings of cells that several modules share.
  - the twice-weekly tracker reminder;
  - the funnel definition and the weekly funnel metrics — those are counts over
    a whole sheet, not a decision about any one row.

Every judgement here is DERIVED FROM CELLS THAT EXIST. A missing date is
"unknown", never "old"; an unparseable one is reported rather than guessed. The
bot must be able to say which cell it read, because a claim that can't be traced
back to a cell is one the team will (rightly) argue with.
"""
import logging
from datetime import date
from typing import Optional

import config
import deadlines as dl
import gtm_sheet

log = logging.getLogger(__name__)

def last_touch(row: dict) -> tuple[Optional[date], str]:
    """(date, which_column) of the most recent outbound touch on a row.

    Prefers "Last followed up date", falls back to the intro date, then first
    contact. Returns (None, "") when the row carries no readable date — which is
    itself a finding, and is reported as such rather than treated as ancient.
    """
    for role, label in (
        ("last_followed_up", "Last followed up date"),
        ("intro_date", "membrane Intro Date"),
        ("first_contacted", "First Contacted"),
    ):
        parsed = dl.parse_date(row.get(role))
        if parsed:
            return parsed, label
    return None, ""


def future_dates(row: dict, *, today: Optional[date] = None) -> list[tuple[date, str]]:
    """Every date on the row that is still in the future, with its column.

    This is what "no future date" in the dead-deal rule actually tests: a booked
    meeting next week means the deal is alive even with an empty Next Steps, and
    flagging it would be wrong.
    """
    today = today or dl.today_ist()
    out: list[tuple[date, str]] = []
    for role, label in (
        ("meeting_date", "Meeting Date"),
        ("last_followed_up", "Last followed up date"),
        ("intro_date", "membrane Intro Date"),
    ):
        parsed = dl.parse_date(row.get(role))
        if parsed and parsed >= today:
            out.append((parsed, label))
    # Any extra column whose header mentions a date can also keep a deal alive —
    # the tabs evolve, and a new "Next Call" column shouldn't make every row look
    # dead until this code is updated.
    for header, value in (row.get("_extra") or {}).items():
        if "date" not in gtm_sheet.normalise_header(header):
            continue
        parsed = dl.parse_date(value)
        if parsed and parsed >= today:
            out.append((parsed, header))
    return sorted(out)


def has_responded(row: dict) -> bool:
    """Did anything come back at all — positive, negative or unclassified?

    Used to decide whether a row can be STALLED. Someone who replied and said no
    has not gone silent; chasing them as if they had would be worse than useless.
    """
    return gtm_sheet.response_status(row.get("response")) != gtm_sheet.RESPONSE_NONE


def awaiting_our_reply(row: dict) -> bool:
    """Did they reply in a way that deserves an answer from us?

    Positive, or a reply the sheet didn't classify. A NEGATIVE reply is a real
    response but not a speed-to-lead failure — flagging "they said no and we
    didn't chase" as HOT would train the team to ignore the flag.
    """
    return gtm_sheet.response_status(row.get("response")) in (
        gtm_sheet.RESPONSE_POSITIVE,
        gtm_sheet.RESPONSE_UNKNOWN,
    )


def is_open(row: dict) -> bool:
    """An open row is one still being worked: not connected/closed, and with no
    Reason recorded (a Reason means it was deliberately parked — the policy
    requires one to park, so its presence is the signal that someone decided)."""
    if (row.get("reason") or "").strip():
        return False
    return not gtm_sheet.is_yes(row.get("connected"))


def is_engaged(row: dict) -> bool:
    """Has this row had REAL two-way engagement, or is it just a name on a list?

    This distinction is what stops the hygiene flags being useless. The real
    tracker is 886 rows, most of them cold prospects who were emailed once and
    never replied. Without this test, "no next step = dead deal" fired on 756 of
    them — which is not a signal, it's a wall of noise that would get the bot
    muted on day one.

    A row is engaged when something came back or something real went out: an
    intro was made, they responded, a meeting exists, assets were shared, or
    we've followed up at least once. A single cold first-contact is NOT
    engagement — that's an unworked lead, and "unworked lead" is a different
    problem from "dead deal".
    """
    if has_responded(row):
        return True
    for role in ("intro_date", "meeting_date", "assets_shared"):
        if (row.get(role) or "").strip():
            return True
    count = followups_count(row)
    if count and count >= 1:
        return True
    # A recorded follow-up date is engagement even when the counter is blank.
    return bool((row.get("last_followed_up") or "").strip())


def followups_count(row: dict) -> Optional[int]:
    raw = str(row.get("followups_count") or "").strip()
    if not raw:
        return None
    digits = "".join(ch for ch in raw if ch.isdigit())
    return int(digits) if digits else None


def describe(row: dict) -> str:
    """One line identifying a row in a message: company, PoC, use case. Only
    fields that are actually filled in, so the line never asserts a blank."""
    parts = [(row.get("company") or "").strip() or "(unnamed row)"]
    poc = (row.get("poc") or "").strip()
    if poc:
        designation = (row.get("poc_designation") or "").strip()
        parts.append(f"{poc}{f', {designation}' if designation else ''}")
    use_case = (row.get("use_case") or "").strip()
    if use_case:
        parts.append(use_case)
    return " · ".join(parts)


# -- the three flags — REMOVED ------------------------------------------------
#
# hot_rows / stalled_rows / dead_deal_rows / all_flags / digest_flag_line /
# flag_message lived here. They are replaced by the NEXT-ACTION STATE MACHINE in
# nextaction.py, which produces exactly ONE action per active row instead of up
# to three overlapping flags with an ordering to pick between them.
#
# Where each flag's judgement went:
#   HOT       -> the PRIORITY OVERRIDE. Any replied row gets a meeting-proposal
#                action, due within MEETING_PROPOSAL_WORKING_DAYS, at the front
#                of the whole queue. Same instinct, but it now says what to DO.
#   STALLED   -> the lane cadence. A row with nothing since its last touch
#                produces a follow-up at FOLLOWUP_GRACE_DAYS, HOT_DEAL_DAYS or
#                SLOW_LANE_DAYS depending on its closure percentage — so a 20%
#                deal is no longer chased at the same rate as a 90% one.
#   DEAD-DEAL -> split in two, honestly. A row somebody has actually closed
#                (0% / Dead / Unresponsive / Won / Lost) now STOPS and is never
#                chased again; a row that is merely untidy keeps its ordinary
#                follow-up. The old flag conflated "finished" with "missing a
#                Next Steps cell", and only one of those is news.


# -- THE TWICE-WEEKLY TRACKER REMINDER ----------------------------------------
#
# Vaishnavi's Mon/Fri "update the tracker" prompt. It is a SECTION OF THE DAILY
# DIGEST on TRACKER_REMINDER_DAYS and it has no send path of its own — see the
# one-message rule at the top of digest.py. This function returns lines; only
# the digest can put them in front of anyone.
#
# WHY IT IS NOT JUST "please update the tracker". A bare reminder on a fixed
# schedule is the first thing a team learns to skim. So it carries the two
# things that make it worth reading: WHICH cells are the ones that matter, and
# HOW MANY rows are currently missing them — the number that goes down when
# somebody acts on it.
#
# PHASE 2 CHANGED WHICH CELLS THOSE ARE. The reminder used to count rows missing
# the follow-up count and last-followed date, because that is what the phase-1
# date rules read. Those rules are retired. What matters now is ACTIVATION: a
# row with no first-contact date and no connection date is invisible to
# everything the bot says unprompted, so the number worth quoting is how many
# rows are in that state. It is the same reminder aimed at the column that
# actually decides whether the bot can see a row at all.


def reminder_items(
    *,
    today: date,
    inactive_count: int = 0,
    rows_total: int = 0,
    active_count: int = 0,
    facts: Optional[list[dict]] = None,
) -> list[dict]:
    """The reminder, as digest items.

    `facts` are `meetings.facts()` entries. Any of them that talks about the
    tracker is included AS ITS OWN LINE, WITH ITS CITATION — if a meeting
    decided how the tracker is to be kept, the reminder says so and names the
    meeting rather than presenting the team's own decision back to them as the
    bot's opinion.

    Every item is flagged `forces_digest`: the reminder is a real ask, so it is
    worth the day's one message even when nothing else is outstanding. It is
    NOT an ITEM_SECTION, so it is never aged — "(3rd day)" on a standing
    twice-weekly reminder would be meaningless.
    """
    day_name = today.strftime("%A")
    lead = (
        f"{day_name} check — update the Outreach PoCs tab: first-contact date, "
        "connection date, response, meeting date, next steps."
    )
    if inactive_count > 0:
        lead += (
            f" {inactive_count} row(s)"
            + (f" of {rows_total}" if rows_total else "")
            + " have neither a first-contact date nor a connection date, so I treat "
            "them as not started and never bring them up. Fill either date in and "
            "they join the "
            + (f"{active_count} row(s) I am working from" if active_count
               else "rows I work from")
            + "."
        )
    elif rows_total:
        lead += (
            f" All {rows_total} row(s) carry a first-contact or connection date, so "
            "I can see every one of them — keep it that way."
        )
    items = [{"text": lead, "forces_digest": True}]

    # Anything a meeting said about the tracker, cited. Imported here rather
    # than at module scope: tracker.py is imported by the answer path on a box
    # where the notes folder may not exist, and this is the only place it needs
    # the meeting layer.
    import meetings as _meetings

    for fact in (facts or []):
        text = str(fact.get("text") or "")
        if "tracker" not in text.lower() and "outreach update" not in text.lower():
            continue
        items.append({
            "text": _meetings.cite(text, fact.get("note")),
            "forces_digest": True,
        })
        if len(items) >= 3:  # the lead plus at most two meeting lines
            break
    return items


# -- THE FUNNEL DEFINITION (the "Sales Funnel" pivot tab) ---------------------
#
# The playbook's funnel pivot defines the funnel, and this is its definition
# copied exactly:
#
#     Contacted -> Connected -> Intro Sent -> Positive (P/Y) -> Meeting Done
#     -> Assets Shared
#
# THE NUMBERS ARE RECOMPUTED FROM THE MASTER TAB RATHER THAN READ OUT OF THE
# PIVOT. A pivot is a snapshot with a date range baked into its title ("Sales
# Funnel - March-June 2026"); reporting last quarter's cached totals as this
# week's funnel is exactly the kind of quietly-wrong number a digest must not
# carry. The master tab is what the pivot is a pivot OF, so recomputing from it
# reproduces the pivot when the pivot is current and is right when it isn't.
#
# Assets Shared is deliberately NOT nested under Meeting Done: the live sheet
# has 59 rows with assets shared against 9 meetings done, because assets go out
# without a meeting all the time. A funnel drawn as a strict nesting would
# report that as an error rather than as what the team does.
FUNNEL_STAGES = (
    ("contacted", "Contacted"),
    ("connected", "Connected"),
    ("intro_sent", "Intro Sent"),
    ("positive", "Positive (P/Y)"),
    ("meeting_done", "Meeting Done"),
    ("assets_shared", "Assets Shared"),
)


def stage_funnel(master_rows: list[dict], *, by_vertical: bool = False) -> dict:
    """The six funnel stages counted off the MASTER tab's status columns.

    Returns {"total": n, "stages": {key: count}, "by_vertical": {vertical:
    {key: count}}}. `by_vertical` is what the pivot breaks down by, and it is
    computed only when asked for because the digest line does not use it.

    Every row that exists is "Contacted" — that is what the master tab is a list
    of, and it is how the pivot counts it (587 rows, 587 contacted).
    """
    stages = {key: 0 for key, _label in FUNNEL_STAGES}
    verticals: dict = {}
    for row in master_rows or []:
        vertical = str(row.get("poc_vertical") or "").strip() or "(no vertical)"
        bucket = verticals.setdefault(vertical, {k: 0 for k, _l in FUNNEL_STAGES})

        hits = {"contacted": True}
        hits["connected"] = gtm_sheet.is_yes(row.get("connected"))
        hits["intro_sent"] = gtm_sheet.is_yes(row.get("intro_sent"))
        hits["positive"] = (
            gtm_sheet.response_status(row.get("response_status"))
            == gtm_sheet.RESPONSE_POSITIVE
        )
        hits["meeting_done"] = gtm_sheet.is_yes(row.get("meeting_done"))
        hits["assets_shared"] = gtm_sheet.is_yes(row.get("assets_shared"))
        for key, hit in hits.items():
            if hit:
                stages[key] += 1
                bucket[key] += 1

    out = {"total": len(master_rows or []), "stages": stages}
    if by_vertical:
        out["by_vertical"] = verticals
    return out


def stage_funnel_lines(funnel: dict) -> list[str]:
    """The funnel as digest lines: the stages in order, with the drop-off.

    The drop-off is the number anybody acts on — "145 intros produced 18
    positives" is a conversion problem, and the raw counts alone hide it.
    """
    stages = funnel.get("stages") or {}
    total = funnel.get("total", 0)
    if not total:
        return []
    parts = " -> ".join(
        f"{label} {stages.get(key, 0)}" for key, label in FUNNEL_STAGES
    )
    lines = [f"FUNNEL ({total} rows on the master tab) — {parts}"]

    drops = []
    ordered = [key for key, _label in FUNNEL_STAGES]
    for prev, nxt in zip(ordered, ordered[1:]):
        # Assets Shared does not follow Meeting Done in practice, so a
        # conversion between them would be a made-up number.
        if nxt == "assets_shared":
            continue
        before, after = stages.get(prev, 0), stages.get(nxt, 0)
        if before:
            drops.append(f"{dict(FUNNEL_STAGES)[nxt].lower()} {round(100.0 * after / before)}%")
    if drops:
        lines.append("CONVERSION — " + " · ".join(drops))
    return lines


# -- funnel metrics (the weekly digest) ---------------------------------------


def funnel_metrics(rows: list[dict], *, since: date, today: Optional[date] = None) -> dict:
    """Leading and lagging indicators over the window `since`..today.

    LEADING is what the team CONTROLS this week — outreach sent, replies,
    meetings booked, follow-ups done against those that were due. LAGGING is what
    the market decided — pilots, paid, repeats. The split is the point: the digest
    exists so the team acts on the leading numbers and merely reports the lagging
    ones.

    Rows whose dates can't be read are counted in `unreadable_dates` rather than
    being silently dropped, so a shrinking denominator is visible instead of
    quietly flattering the numbers.
    """
    today = today or dl.today_ist()
    m = {
        "window_start": dl.iso(since),
        "window_end": dl.iso(today),
        "total_rows": len(rows),
        "outreach_sent": 0,
        "replies": 0,
        "meetings_booked": 0,
        "followups_done": 0,
        "followups_due": 0,
        "unreadable_dates": 0,
        "open_rows": 0,
        "pilots": 0,
        "paid": 0,
        "repeats": 0,
        "no_poc": 0,
        "no_next_step": 0,
    }

    for row in rows:
        if is_open(row):
            m["open_rows"] += 1
        if not (row.get("poc") or "").strip():
            m["no_poc"] += 1
        if not (row.get("next_steps") or "").strip():
            m["no_next_step"] += 1

        first = dl.parse_date(row.get("first_contacted"))
        intro = dl.parse_date(row.get("intro_date"))
        followed, _ = last_touch(row)
        meeting = dl.parse_date(row.get("meeting_date"))

        if row.get("first_contacted") and first is None:
            m["unreadable_dates"] += 1

        # New outreach in the window: first contact or the intro landed in it.
        if (first and since <= first <= today) or (intro and since <= intro <= today):
            m["outreach_sent"] += 1
        if has_responded(row):
            # Replies are counted against rows touched in the window; the sheet
            # has no response DATE column today, so this is scoped by activity.
            if (followed and since <= followed <= today) or (first and since <= first <= today):
                m["replies"] += 1
        if meeting and since <= meeting <= today:
            m["meetings_booked"] += 1
        if followed and since <= followed <= today:
            m["followups_done"] += 1

        # Due-but-not-done: an open, unanswered row whose last touch is older
        # than the cadence is a follow-up that WAS due in this window.
        if is_open(row) and not has_responded(row) and followed:
            if dl.working_days_between(followed, today) >= config.OUTREACH_FOLLOWUP_DAYS:
                m["followups_due"] += 1

        # Lagging: read from whatever the sheet actually says, across the status
        # columns and the free-text ones, because there is no dedicated stage
        # column yet.
        blob = " ".join(
            str(row.get(k) or "")
            for k in ("next_steps", "other_updates", "connected", "assets_shared", "use_case")
        )
        blob += " " + " ".join(str(v) for v in (row.get("_extra") or {}).values())
        low = gtm_sheet.normalise_header(blob)
        if "pilot" in low or "poc " in low or "proof of concept" in low:
            m["pilots"] += 1
        if "paid" in low or "invoice" in low or "contract signed" in low or "po " in low:
            m["paid"] += 1
        if "repeat" in low or "renewal" in low or "expansion" in low or "upsell" in low:
            m["repeats"] += 1

    m["reply_rate"] = (
        round(100.0 * m["replies"] / m["outreach_sent"], 1) if m["outreach_sent"] else None
    )
    return m


def funnel_lines(metrics: dict) -> list[str]:
    """The funnel numbers as lines, WITHOUT a heading of their own.

    Split out from `digest_message` because these numbers no longer get their
    own weekly post — they are a section of the daily digest on
    WEEKLY_DIGEST_WEEKDAY, and that section already carries a heading. One
    renderer, two callers, no chance of the two drifting apart.

    Deliberately compact: leading on one line, lagging on the next, hygiene
    last, because hygiene is the thing anyone can actually fix on a Friday
    afternoon.
    """
    rr = metrics.get("reply_rate")
    rr_text = f"{rr}%" if rr is not None else "n/a"
    lines = [
        f"Window {metrics['window_start']} → {metrics['window_end']}",
        (
            f"LEADING — outreach sent {metrics['outreach_sent']} · "
            f"replies {metrics['replies']} ({rr_text}) · "
            f"meetings booked {metrics['meetings_booked']} · "
            f"follow-ups done {metrics['followups_done']} vs {metrics['followups_due']} due"
        ),
        (
            f"LAGGING — pilots {metrics['pilots']} · paid {metrics['paid']} · "
            f"repeats {metrics['repeats']}"
        ),
    ]
    hygiene = []
    if metrics.get("no_poc"):
        hygiene.append(f"{metrics['no_poc']} row(s) with no named PoC")
    if metrics.get("no_next_step"):
        hygiene.append(f"{metrics['no_next_step']} with no next step")
    if metrics.get("unreadable_dates"):
        hygiene.append(f"{metrics['unreadable_dates']} with a date I couldn't read")
    if hygiene:
        lines.append("HYGIENE — " + " · ".join(hygiene))
    return lines


def digest_message(metrics: dict, *, flags_summary: str = "", staleness: str = "") -> str:
    """The funnel numbers as a standalone block, heading and all.

    NO LONGER POSTED ON A SCHEDULE — `funnel_lines` feeds the daily digest
    instead. This is the answer path: "how did the funnel do this week?" wants
    the heading and the window spelled out.
    """
    rr = metrics.get("reply_rate")
    rr_text = f"{rr}%" if rr is not None else "n/a"
    lines = [
        f"**Weekly funnel** ({metrics['window_start']} → {metrics['window_end']})",
        (
            f"LEADING — outreach sent {metrics['outreach_sent']} · "
            f"replies {metrics['replies']} ({rr_text}) · "
            f"meetings booked {metrics['meetings_booked']} · "
            f"follow-ups done {metrics['followups_done']} vs {metrics['followups_due']} due"
        ),
        (
            f"LAGGING — pilots {metrics['pilots']} · paid {metrics['paid']} · "
            f"repeats {metrics['repeats']}"
        ),
    ]
    hygiene = []
    if metrics.get("no_poc"):
        hygiene.append(f"{metrics['no_poc']} row(s) with no named PoC")
    if metrics.get("no_next_step"):
        hygiene.append(f"{metrics['no_next_step']} with no next step")
    if metrics.get("unreadable_dates"):
        hygiene.append(f"{metrics['unreadable_dates']} with a date I couldn't read")
    if hygiene:
        lines.append("HYGIENE — " + " · ".join(hygiene))
    if flags_summary:
        lines.append(flags_summary)
    if staleness:
        lines.append(staleness)
    return "\n".join(lines)
