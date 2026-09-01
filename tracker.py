"""Reading the outreach tracker as a PIPELINE rather than as a grid of strings.

`gtm_sheet.py` turns a worksheet into rows keyed by role. This module turns those
rows into the judgements the policy is written in terms of: is this row hot, is
it stalled, is it a dead deal, who owns it, when was it last touched.

THE THREE FLAGS (config.SHEET_FLAGS_ENABLED). None of them posts a message of
its own any more: HOT is the first section of the ONE daily digest and
STALLED/DEAD-DEAL are its HYGIENE section, so each is surfaced once a day by
construction rather than by a per-row rate limit. See digest.py.

  HOT        Response? says yes, but nothing has gone back since the reply.
             Speed to lead: an answered prospect going cold because nobody
             replied is the most expensive failure in the sheet, so this is a
             SAME-DAY flag.

  STALLED    An open row, no response, last followed up more than
             STALLED_AFTER_DAYS working days ago. Silence kills deals; the
             cadence exists to be kept.

  DEAD-DEAL  An open row with an empty Next Steps AND no future date anywhere.
             "No next step = dead deal" — the flag asks the owner for the next
             action, or for the deal to be parked WITH a Reason (the lost-reason
             log only works if parking requires one).

Every judgement here is DERIVED FROM CELLS THAT EXIST. A missing date is
"unknown", never "old"; an unparseable one is reported rather than guessed. The
bot must be able to say which cell it read, because a flag that can't be traced
back to a cell is one the team will (rightly) argue with.
"""
import logging
from datetime import date
from typing import Optional

import config
import deadlines as dl
import gtm_sheet

log = logging.getLogger(__name__)

FLAG_HOT = "hot"
FLAG_STALLED = "stalled"
FLAG_DEAD = "dead_deal"

FLAG_LABELS = {
    FLAG_HOT: "HOT",
    FLAG_STALLED: "STALLED",
    FLAG_DEAD: "DEAD-DEAL",
}


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
        ("bot_deadline", config.BOT_DEADLINE_COLUMN),
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


# -- the three flags ----------------------------------------------------------


def hot_rows(rows: list[dict], *, today: Optional[date] = None) -> list[dict]:
    """Rows that replied and haven't been answered since.

    The test is a comparison of DATES, not a guess: a reply counts as unanswered
    when the last follow-up predates the response, or when there is no follow-up
    date at all. A row whose response date can't be read is included with
    `date_unclear` set, because "they replied and I can't tell if we came back"
    still needs a human's eye.
    """
    today = today or dl.today_ist()
    out = []
    for row in rows:
        if not awaiting_our_reply(row) or not is_open(row):
            continue

        # A booked future meeting IS the answer to a reply — the ball is not in
        # our court, so this isn't a speed-to-lead failure.
        meeting = dl.parse_date(row.get("meeting_date"))
        if meeting and meeting >= today:
            continue

        # "No follow-up since" is measured against the FOLLOW-UP column
        # specifically, not against `last_touch`'s fallback chain. That
        # distinction is the whole flag: an intro date is when we first wrote to
        # them, so treating it as a follow-up would mean a prospect who replied
        # and was never answered looks handled.
        followed = dl.parse_date(row.get("last_followed_up"))

        response_date = None
        for header, value in (row.get("_extra") or {}).items():
            h = gtm_sheet.normalise_header(header)
            if "response" in h and "date" in h:
                response_date = dl.parse_date(value)
                break

        if followed is None:
            why = "they replied and there's no follow-up date recorded at all"
        elif response_date is not None and followed <= response_date:
            why = (
                f"they replied on {dl.format_date(response_date)} and the last follow-up "
                f"({dl.format_date(followed)}) predates it"
            )
        else:
            # We answered after they replied, and nothing says otherwise.
            continue

        out.append({
            **row,
            "_flag": FLAG_HOT,
            "_why": why,
            "_date_unclear": followed is None and response_date is None,
        })
    return out


def stalled_rows(rows: list[dict], *, today: Optional[date] = None) -> list[dict]:
    """Open, unanswered rows whose last touch is older than STALLED_AFTER_DAYS
    working days."""
    today = today or dl.today_ist()
    threshold = max(1, config.STALLED_AFTER_DAYS)
    out = []
    for row in rows:
        if not is_open(row) or has_responded(row):
            continue
        # Only rows we've actually worked can stall. A cold name that was emailed
        # once is an unworked lead, not a stalled deal.
        if not is_engaged(row):
            continue
        # Something already scheduled means the deal isn't stalled, it's waiting.
        # A booked meeting or a live deadline is a next step, and chasing it as
        # silence would be wrong — that's the dead-deal rule's job, not this one.
        if future_dates(row, today=today):
            continue
        touched, column = last_touch(row)
        if touched is None:
            # No date at all is a hygiene problem of its own, reported honestly
            # rather than counted as stalled — the bot can't claim a row is old
            # when it can't read a date.
            continue
        age = dl.working_days_between(touched, today)
        if age > threshold:
            out.append({
                **row,
                "_flag": FLAG_STALLED,
                "_why": (
                    f"no response and the last touch ({column}: "
                    f"{dl.format_date(touched)}) was {age} working days ago"
                ),
                "_age_working_days": age,
            })
    return out


def dead_deal_rows(rows: list[dict], *, today: Optional[date] = None) -> list[dict]:
    """Open rows with an empty Next Steps AND no future date anywhere.

    Both conditions are required. An empty Next Steps with a meeting booked next
    Tuesday is untidy, not dead, and flagging it would spend the team's patience
    on the wrong rows.
    """
    today = today or dl.today_ist()
    out = []
    for row in rows:
        if not is_open(row):
            continue
        # "No next step = dead deal" only means anything for a deal that was
        # actually in play. Applied to every cold row on a prospect list it fires
        # on hundreds of them and stops being a signal at all.
        if not is_engaged(row):
            continue
        if (row.get("next_steps") or "").strip():
            continue
        if future_dates(row, today=today):
            continue
        out.append({
            **row,
            "_flag": FLAG_DEAD,
            "_why": "no next step recorded and no future date on the row",
        })
    return out


def all_flags(rows: list[dict], *, today: Optional[date] = None) -> list[dict]:
    """Every flagged row, most actionable framing first, each row appearing at
    most once. Two messages about one row is how a bot becomes noise.

    Order is hot → dead → stalled, deliberately. A row that is both dead and
    stalled gets the DEAD-DEAL message, because "there's no next step — what is
    it, or park it with a Reason" gives the owner something to do, whereas "this
    is 11 days old" only tells them something they can already see.

    Dedup is by SHEET ROW, not by company. The tracker holds one row per PoC, so
    a single company legitimately owns a dozen rows — keying this by company
    meant the first flagged OpenAI row silenced every other OpenAI row in every
    category, and on the live sheet that hid all 70 stalled rows behind rows
    already claimed by the hot and dead-deal finders. Company-level quieting is
    real, but it belongs in the sweep, which already suppresses a repeat of the
    same (flag, company) within a day.
    """
    today = today or dl.today_ist()
    seen: set[int] = set()
    out: list[dict] = []
    for finder in (hot_rows, dead_deal_rows, stalled_rows):
        for row in finder(rows, today=today):
            key = row.get("_row")
            if key in seen:
                continue
            seen.add(key)
            out.append(row)
    return out


def digest_flag_line(row: dict) -> str:
    """One flag as a LINE IN THE DAILY DIGEST.

    Shorter than `flag_message` on purpose. A flag that was its own message
    could afford a sentence of coaching ("speed to lead matters more than
    anything else in the sheet"); forty of them stacked in one digest cannot,
    and the coaching is the first thing that turns a digest into a wall. What
    survives is the part nobody can reconstruct themselves: the label, the row,
    and WHICH CELLS the judgement was read from.
    """
    label = FLAG_LABELS.get(row.get("_flag"), "FLAG")
    return f"{label} — {describe(row)}: {row.get('_why', '')}".rstrip(" .") + "."


def flag_message(row: dict, *, mentions: str = "") -> str:
    """The full in-channel text for one flag, with the coaching line.

    NO LONGER POSTED PROACTIVELY — the digest uses `digest_flag_line` instead.
    This is kept for the answer path: when someone asks "what's stalled?", a
    handful of flags with their reasoning is exactly the right answer, and the
    reasoning is what stops a flag being argued with instead of acted on."""
    kind = row.get("_flag")
    label = FLAG_LABELS.get(kind, "FLAG")
    who = describe(row)
    why = row.get("_why", "")
    tail = f" {mentions}" if mentions else ""

    if kind == FLAG_HOT:
        return (
            f"{label} — {who}: {why}. Speed to lead matters more than anything else "
            f"in the sheet; who is replying today?{tail}"
        )
    if kind == FLAG_STALLED:
        n = row.get("_age_working_days")
        return (
            f"{label} — {who}: {why}. Either the next touch goes out, or park it with "
            f"a Reason so the lost-reason log is honest.{tail}"
            if n is not None
            else f"{label} — {who}: {why}.{tail}"
        )
    if kind == FLAG_DEAD:
        return (
            f"{label} — {who}: {why}. No next step is a dead deal. What's the next "
            f"action, or should it be parked with a Reason?{tail}"
        )
    return f"{label} — {who}: {why}.{tail}"


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
