"""THE THIRTEEN RULES — what the bot says on its own initiative, computed.

WHAT THIS REPLACES. The plan-v2 state machine that lived here produced exactly
one action per ACTIVE row, chosen by the first of twelve ordered triggers to
match. That shape is gone. It was built around a single question — "what does
this row need next?" — and seven of the twelve rules the team actually runs are
not about a row at all: two are about the news, one about a checklist, one about
a package list, one about a pipeline tab, one about an events tab.

WHAT REPLACED IT. `bot_rules.yaml` holds the thirteen rules, and this module holds
one evaluator per rule. The file decides WHICH rules exist, WHEN each runs, HOW
MUCH one post may carry, WHERE it goes and whether it counts against the cap.
This module decides only HOW a rule works out that something is due.

    THE FILE IS THE SCHEDULE; THE CODE IS THE ARITHMETIC. Changing a weekday or
    a cap is an edit and a restart. That split is the whole point of the rewrite.

THE THIRTEEN, with the trigger name the file uses:

     R1  ai_news                weekdays            the news sweep
     R2  news_company_screen    Tue, Fri            news companies vs the pipeline
     R3  events                 alt. Wed            register / attend, until it passes
     R4  deliverables           Mon                 P1 only, due this week or passed
     R5  prospects              Tue, Thu            first contact FALSE or blank
     R6  li_no_dm               Tue, Fri            connected > 3d, no DM: the email check
     R7  dm_no_meeting          Mon                 DM > 7d, no meeting, and not a
                                                    person R13 covers
     R8  meeting_prep           anchored, 10:00     T-5, T-3, and on the day
     R9  meeting_followup       anchored, 10:00     done + 3d, no next steps, laddered
    R10  closure_support        Mon                 deal/demo/quote AND closure > 50
    R11  new_pipeline_company   Wed                 appeared since last Wednesday
    R12  sales_packages         Thu                 Ready? is No or blank
    R13  next_step_followups    weekdays, 15:00     connected contacts: the next step
                                                    on the Next Steps dropdown, 5 a post

TWO GATES SURVIVE THE REWRITE, and they run before any rule sees a row:

    STOP, FOREVER. A row whose deal status or prospect status says Won, Lost,
    Dead or Unresponsive — or whose closure is exactly 0% — produces nothing,
    from any rule, ever. This is the one piece of the old engine that is load-
    bearing rather than incidental: a bot that keeps producing work for closed
    rows is a bot whose queue nobody reads.

    SNOOZE. A live snooze silences a row. An EXPIRED one does not: the rule
    still fires and the due date becomes the snooze date, so a row somebody
    parked until the 20th shows up overdue on the 21st rather than being
    silently recomputed to something else.

ONE CONTACT, ONE MENTION, PER DAY. Several rules can legitimately select the
same person — a prospect who is also three days connected with no DM. `run()`
deduplicates across every rule by contact, keeping the item from the
earliest-listed rule in the file, and says in the result which rules lost. A
person hearing about themselves twice in one afternoon is how a bot gets muted.

WEB-DEPENDENT RULES STILL PRODUCE THEIR ITEMS. R1, R2, R3, R6, R8 and R10
need research this bot cannot do yet (R11 asks first and searches only on a
yes, so it is not one of them). Each produces its item carrying
`web_pending=True` and the placeholder text, so the schedule is real and visible
before the research layer lands. Nothing is silently skipped waiting for it.

THIS MODULE IS PURE, AND THAT IS LOAD-BEARING. Tabs and dicts in, dicts out. It
reads no sheet, writes no database row and sends nothing — the snoozes, the
new-company snapshot, the repeat counts, the R9
ladder state and R13's rotation state are all passed IN, computed by the caller. There is no
`guardrails.send` here and there must never be one.

    R11's SNAPSHOT IS THE REASON THAT MATTERS. Detecting a new company in the
    Master Pipeline needs yesterday's names stored somewhere, and storing them
    is a write. The caller takes the snapshot and passes `new_companies`; the
    engine only reads it. Had the engine taken the snapshot itself, every
    `cadence preview` would have advanced the state it was supposed to be
    previewing.
"""
import logging
import re
from datetime import date, datetime, time, timedelta
from typing import Optional

import activation
import config
import deadlines as dl
import gtm_sheet
import focus as focus_mod
import rules as rules_mod
import wording

log = logging.getLogger(__name__)

# -- the thirteen rule ids ------------------------------------------------------
# The id is the stable key: the dedup ledger, the drip's group keys and the
# SQLite state are all written against it. `bot_rules.yaml` carries the same
# ids, and `rules.py` refuses a file whose trigger names this module does not
# implement — so a rule can never be configured and silently uncomputed.
R_AI_NEWS = "ai_news"
R_NEWS_SCREEN = "news_company_screen"
R_EVENTS = "events"
R_DELIVERABLES = "deliverables"
R_PROSPECTS = "prospects"
R_LI_NO_DM = "li_no_dm"
R_DM_NO_MEETING = "dm_no_meeting"
R_MEETING_PREP = "meeting_prep"
R_MEETING_FOLLOWUP = "meeting_followup"
R_CLOSURE_SUPPORT = "closure_support"
R_NEW_COMPANY = "new_pipeline_company"
R_PACKAGES = "sales_packages"
R_NEXT_STEPS = "next_step_followups"

# The one instruction lane that is NOT a rule: a one-off somebody asked for by
# name ("remind me about Acme on Saturday"). It is kept out of bot_rules.yaml
# deliberately — it has no schedule, no cap and no weekday, because it runs when
# a person said it should. It is also the only due date in this module that is
# never weekend-shifted: somebody asking for a Saturday has decided about their
# own Saturday.
SCHEDULED_REMINDER = "scheduled_reminder"

# What each rule IS, in one phrase, for the preview headings and the drip's
# group labels. Keyed by TRIGGER, because that is what a rule entry names.
TYPE_LABELS = {
    R_AI_NEWS: "AI news",
    R_NEWS_SCREEN: "News companies to screen",
    R_EVENTS: "AI events & summits",
    R_DELIVERABLES: "Deliverables due",
    R_PROSPECTS: "Prospects to contact",
    R_LI_NO_DM: "Connected, no DM yet",
    R_DM_NO_MEETING: "DM sent, no meeting",
    R_MEETING_PREP: "Meeting prep",
    R_MEETING_FOLLOWUP: "Meeting done, no next steps",
    R_CLOSURE_SUPPORT: "Closure support",
    R_NEW_COMPANY: "New company in the pipeline",
    R_PACKAGES: "Sales packages not ready",
    R_NEXT_STEPS: "Next steps for connected contacts",
    SCHEDULED_REMINDER: "Reminders you asked for",
}

# -- priority bands -----------------------------------------------------------
# FEWER BANDS THAN BEFORE, and they mean something different. The old bands
# ranked ROWS by how hot the deal was; these rank ITEMS by how much the thing
# decays if it waits. A meeting tomorrow decays completely; a package status
# does not decay at all.
P_MEETING = 0        # anchored to a date that is coming whether we act or not
P_REPLY = 1          # somebody is waiting on us
P_CHASE = 2          # the ordinary rules
P_CONTEXT = 3        # news, screens, packages — useful, never urgent

BAND_LABELS = {
    P_MEETING: "meetings (dated, and they do not wait)",
    P_REPLY: "somebody is waiting on us",
    P_CHASE: "the ordinary chases",
    P_CONTEXT: "context (news, screens, packages)",
}

# Which band each rule's items land in. Held here rather than in the YAML
# because it is not a scheduling decision — it is what the item IS — and a file
# that could reorder urgency would let a cap change silently reprioritise a
# meeting behind a package status.
RULE_BANDS = {
    R_MEETING_PREP: P_MEETING,
    R_MEETING_FOLLOWUP: P_MEETING,
    SCHEDULED_REMINDER: P_REPLY,
    R_LI_NO_DM: P_REPLY,
    R_DM_NO_MEETING: P_CHASE,
    R_CLOSURE_SUPPORT: P_CHASE,
    R_DELIVERABLES: P_CHASE,
    R_PROSPECTS: P_CHASE,
    R_NEW_COMPANY: P_CHASE,
    R_NEXT_STEPS: P_CHASE,
    R_AI_NEWS: P_CONTEXT,
    R_NEWS_SCREEN: P_CONTEXT,
    R_EVENTS: P_CONTEXT,
    R_PACKAGES: P_CONTEXT,
}

# Why a row produced nothing. Reported rather than swallowed: "no action" and
# "I could not work out an action" are different states, and a queue that
# renders both as absence is a queue that hides its own failures.
SILENT_STOPPED = "stopped"
SILENT_SNOOZED = "snoozed"
SILENT_NO_ANCHOR = "no_anchor"
SILENT_NOT_DUE = "not_due"
SILENT_DEDUPED = "deduped"

SILENT_LABELS = {
    SILENT_STOPPED: "closed (0% / Dead / Unresponsive / Won / Lost) — never chased again",
    SILENT_SNOOZED: "snoozed",
    SILENT_NO_ANCHOR: "no readable date to count from",
    SILENT_NOT_DUE: "nothing due yet",
    SILENT_DEDUPED: "already named by an earlier rule today",
}


# -- reading a row ------------------------------------------------------------


def _text(row: dict, role: str) -> str:
    """A row cell as trimmed text, spreadsheet errors normalised to empty."""
    return gtm_sheet.clean_cell(row.get(role))


def _norm(value) -> str:
    return gtm_sheet.normalise_header(gtm_sheet.clean_cell(value))


def _matches_any(value, phrases) -> str:
    """The first phrase in `phrases` this cell says, or "".

    Whole-phrase matching, not substring: "won" must not match "won't", and
    "demo" must not match "demoted". A phrase matches when it IS the cell, is
    one of its words, or appears in it surrounded by spaces.
    """
    v = _norm(value)
    if not v:
        return ""
    for phrase in phrases or ():
        key = gtm_sheet.normalise_header(str(phrase))
        if not key:
            continue
        if v == key or key in v.split() or f" {key} " in f" {v} ":
            return phrase
    return ""


def closure_percent(row: dict) -> Optional[int]:
    """The closure cell as a whole percentage, or None when it isn't a number.

    "75%", "75", "0.75" and "75 %" all read as 75. The parsing lives in
    `gtm_sheet.closure_percent`; this is the row-level wrapper over it, so the
    two can never disagree about what "0.5" means.
    """
    return gtm_sheet.closure_percent(_text(row, "closure_prob"))


def stop_reason(row: dict) -> str:
    """Why this row is finished, or "" when it is still live.

    STOP SEMANTICS SURVIVED THE REWRITE UNCHANGED, and deliberately: it is the
    one piece of the old engine that was load-bearing rather than incidental.
    A row that is finished produces nothing, from any of the thirteen rules, ever.

    THREE WAYS TO BE FINISHED, and all three are things a human wrote:
      - the DEAL STATUS names one of CLOSURE_STOP_MARKERS (Won, Lost, Dead,
        Unresponsive);
      - the PROSPECT STATUS names one of them;
      - the closure probability is exactly 0%.

    A BLANK CLOSURE CELL IS NOT A STOP. That is why this is checked numerically
    rather than by reading "no percentage" as zero: most rows have never had the
    column filled in, and treating a blank as 0% would silence the whole sheet
    on the first run.
    """
    marker = _matches_any(row.get("deal_status"), config.CLOSURE_STOP_MARKERS)
    if marker:
        return f"deal status says {_text(row, 'deal_status')!r}"
    marker = _matches_any(row.get("prospect_status"), config.CLOSURE_STOP_MARKERS)
    if marker:
        return f"prospect status says {_text(row, 'prospect_status')!r}"
    marker = _matches_any(row.get("closure_prob"), config.CLOSURE_STOP_MARKERS)
    if marker:
        return f"closure says {_text(row, 'closure_prob')!r}"
    if closure_percent(row) == 0:
        return f"closure is {_text(row, 'closure_prob')!r} (0%)"
    return ""


def is_on_hold(row: dict) -> bool:
    """A deal parked by agreement. No rule selects one — the monthly pulse that
    used to is retired — but answers still need to say "parked", not "silent"."""
    return bool(_matches_any(row.get("deal_status"), config.DEAL_ON_HOLD_MARKERS))


def first_contact_done(row: dict) -> bool:
    """Has first contact happened? R5 selects rows where it has NOT.

    THE COLUMN IS A PEOPLE-TYPED BOOLEAN — "TRUE", "Yes", a tick, or blank — so
    it goes through `gtm_sheet.parse_flag`. A BLANK IS NOT A FALSE anywhere else
    in this codebase, and it is not one here either: what makes a row eligible
    is `parse_flag(...) is not True`, which covers blank, FALSE and anything
    unreadable. That is the safe direction for this rule specifically — the cost
    of offering a contact who was already approached is one correction, and the
    cost of never offering one is a prospect nobody ever contacts.

    A first-contact DATE also counts, whatever the flag says. A date is a
    stronger statement than a checkbox.
    """
    if gtm_sheet.parse_flag(row.get("first_contact")) is True:
        return True
    return _date(row, "first_contact_date") is not None


def _date(row: dict, role: str) -> Optional[date]:
    return gtm_sheet.sheet_date(row.get(role))


def last_note(row: dict) -> str:
    """The most recent thing written against this row, for R7's line.

    The notes column (Notes/Remarks; "Next Steps/Notes" before 7 Oct 2026),
    because that is where a person records what happens next; the sheet has no
    dated note column, so "most recent" is really "the most specific thing
    anybody wrote". `next_steps` is that column's role — never the Next Steps
    dropdown, which is `outreach_step`.
    """
    return _text(row, "next_steps")


def last_touch(row: dict) -> tuple[Optional[date], str]:
    """The most recent thing that happened on this row, and which column said so.

    The LATEST readable date across the touch columns, not the first one found:
    a row first contacted in March and DM'd in August is an August row, and
    anchoring to March would make it permanently overdue.
    """
    best: Optional[date] = None
    label = ""
    for role, name in (
        ("li_dm_date", "LinkedIn DM"),
        ("meeting_date", "meeting"),
        ("li_connected_date", "LinkedIn connected"),
        ("first_contact_date", "first contact"),
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


# -- dates --------------------------------------------------------------------


def shift_off_weekend(due: date) -> date:
    """Saturday and Sunday shift forward to the Monday.

    Applied to every computed due date. The one exception is an explicitly
    scheduled reminder, which keeps the date its asker chose.
    """
    if not config.NEXT_ACTION_WEEKEND_SHIFT:
        return due
    while due.weekday() >= 5:          # 5 = Saturday, 6 = Sunday
        due += timedelta(days=1)
    return due


def _due(anchor: date, days: int) -> date:
    """`days` CALENDAR days after `anchor`, shifted off the weekend.

    Deliberately NOT pulled forward to today when it lands in the past. An
    overdue item should read as overdue — that is the most useful thing on its
    line, and a queue that quietly restamps everything as "due today" cannot be
    triaged.
    """
    return shift_off_weekend(anchor + timedelta(days=max(0, int(days))))


def _int_list(values) -> list:
    """["5", "3"] -> [5, 3], dropping anything that is not a whole number.

    Used for MEETING_PREP_DAYS_BEFORE. A bad entry is dropped rather than
    raising, and `config` logs it at startup by name — a typo in one touch must
    not take the other touches down with it.
    """
    out = []
    for v in values or ():
        text = str(v).strip()
        if text.isdigit():
            out.append(int(text))
    return sorted(set(out), reverse=True)


# -- the item -----------------------------------------------------------------


def _item(
    *, rule, trigger: str, today: date, due: Optional[date], why: str, text: str,
    owner: str = "", company: str = "", poc: str = "", designation: str = "",
    sheet_row=None, row_key: str = "", contact_key: str = "",
    web_pending: bool = False, destination: str = "", extra: Optional[dict] = None,
) -> dict:
    """One due item. The only shape this module returns.

    `contact_key` is what the DEDUP runs on. It is empty for items that are not
    about a person — a package, a deliverable, the news — because two of those
    in one day is not the failure dedup exists to prevent.
    """
    band = RULE_BANDS.get(trigger, P_CHASE)
    overdue = (today - due).days if (due is not None and due < today) else 0
    item = {
        "rule": trigger,
        "rule_id": getattr(rule, "id", ""),
        "rule_name": getattr(rule, "name", TYPE_LABELS.get(trigger, trigger)),
        "type": trigger,
        "label": TYPE_LABELS.get(trigger, trigger),
        "priority": band,
        "priority_label": BAND_LABELS.get(band, "?"),
        "due_date": due,
        "due_iso": dl.iso(due) if due else "",
        "overdue_days": overdue,
        "owner": owner,
        "company": company,
        "poc": poc,
        "poc_designation": designation,
        "sheet_row": sheet_row,
        "row_key": row_key,
        "contact_key": contact_key,
        "why": why,
        "text": text,
        "web_pending": bool(web_pending),
        "destination": destination or getattr(rule, "destination", rules_mod.DEST_CHANNEL),
        "counts_toward_cap": bool(getattr(rule, "counts_toward_cap", True)),
        "max_items_per_post": int(getattr(rule, "max_items_per_post", 5)),
        "key": f"{getattr(rule, 'id', trigger)}:{contact_key or company or text[:40]}",
    }
    if web_pending:
        # THE PLACEHOLDER IS ON THE ITEM, NOT INSTEAD OF IT. The rule still
        # fires, still takes its slot and still shows in the preview — what it
        # cannot do yet is say the researched part. Skipping the item instead
        # would make a configured rule look like a quiet week.
        item["text"] = f"{text} [{rules_mod.WEB_PENDING}]"
        item["web_note"] = rules_mod.WEB_PENDING
    if extra:
        item.update(extra)
    return item


def _describe(row: dict) -> str:
    company = _text(row, "company") or "(unnamed row)"
    poc = _text(row, "name")
    if not poc:
        return company
    designation = _text(row, "designation")
    return f"{company} · {poc}{f' ({designation})' if designation else ''}"


def _contact_key(row: dict) -> str:
    return activation.row_key(row)


# -- gates that run before any rule ------------------------------------------


def row_gate(row: dict, *, today: date, snoozes: dict) -> tuple:
    """(ok, silent_reason, snooze_until) for ONE Outreach PoCs row.

    The two survivors of the old engine, in the order they ran there:

      STOP, FOREVER — checked before everything.
      SNOOZE — a live one silences the row; an EXPIRED one does not, and the
      date it named replaces whatever the rule would have computed.
    """
    if stop_reason(row):
        return False, SILENT_STOPPED, None
    key = _contact_key(row)
    snooze = (snoozes or {}).get(key)
    until = dl.parse_date((snooze or {}).get("until_date")) if snooze else None
    if until is not None and until > today:
        return False, SILENT_SNOOZED, None
    return True, "", until


# -- who Rule 13 covers --------------------------------------------------------
# ONE CHECK, TWO READERS. Rule 13 names the people we are connected with, and
# Rule 7 (DM sent, no meeting) must never list a person Rule 13 covers
# (Vaishnavi, 7 Oct: "rule 13 supercedes this"; the team, 8 Oct: R7 comes back
# on Mondays for everybody else). Both rules read the functions below, so the
# two can never drift into disagreeing about who is "connected".


def connected_markers() -> set:
    """What "Sid - LI Addition" says when a person is connected, normalised."""
    return {gtm_sheet.normalise_header(str(m))
            for m in (config.NEXT_STEP_CONNECTED_MARKERS or []) if str(m).strip()}


def marked_connected(row: dict, markers: Optional[set] = None) -> bool:
    """Does this row's "Sid - LI Addition" say Connected?"""
    if markers is None:
        markers = connected_markers()
    return _norm(row.get("sid_li_added")) in markers


def has_connected_date(row: dict) -> bool:
    """Does this row's LI Connected Date read as a date?"""
    return _date(row, "li_connected_date") is not None


def covered_by_next_steps(row: dict, markers: Optional[set] = None) -> bool:
    """Is this person Rule 13's? Connected, WITH an LI Connected Date.

    BOTH HALVES, because Rule 13 itself needs both: a row marked Connected
    with no date is skipped by Rule 13 (and reported as a gap to fix), so it
    is not covered, and Rule 7 may still list it. Otherwise a DM'd person with
    a missing date would be chased by neither rule.
    """
    return marked_connected(row, markers) and has_connected_date(row)


# -- the thirteen evaluators --------------------------------------------------
# Each takes (rule, ctx) and returns a list of items. NONE of them sends, writes
# or reads a sheet: everything they need is in `ctx`, assembled by the caller.
#
# An evaluator that raises is caught by `run()`, logged with its rule id, and
# contributes nothing — one broken rule must not take the other twelve down.


def _r_ai_news(rule, ctx) -> list:
    """R1 — the daily AI industry feed on a topic list. WEB-DEPENDENT.

    The 24/29 Sep decision: R1 is no longer about our people. The main sweep
    covers the last 24 hours on NEWS_TOPICS (seeds, not limits) and the
    research layer (`bot._news_run`) expands this item into up to
    NEWS_MAX_ITEMS stories. The hourly breaking checks are not items at all —
    they post from the sweep tick, outside the drip.

    ONE ITEM, not one per story, and NO FIXED TIME (8 Oct). It used to be
    pinned to NEWS_MAIN_TIME; the team's order of the day replaced that, so it
    is spaced like any other rule and takes its place in `daily_order`
    (bot_rules.yaml): third on Monday and Friday, fourth on Tuesday, second on
    Wednesday and Thursday. What it covers is "since the previous AI news
    post's slot", worked out by the caller (`bot._main_window`).
    """
    today = ctx["today"]
    return [_item(
        rule=rule, trigger=R_AI_NEWS, today=today, due=today,
        why="R1 runs every weekday, in its place in the day's order",
        text="AI news: everything since the last AI news post, on the team's topic list",
        web_pending=True,
    )]


def _r_news_company_screen(rule, ctx) -> list:
    """R2 — companies in the news that are NOT in the Master Pipeline. WEB-DEPENDENT.

    The screen says why each is or is not relevant to membrane. PERMISSION IS
    ASKED before anything is added to the sheet — this rule proposes, it never
    writes, and neither does anything downstream of it.
    """
    today = ctx["today"]
    known = len(ctx.get("pipeline_companies") or ())
    return [_item(
        rule=rule, trigger=R_NEWS_SCREEN, today=today, due=today,
        why=f"R2 runs Tuesdays and Fridays; {known} companies are already in the Master Pipeline",
        text="Companies in the news that are not in the Master Pipeline, and why each is or "
             "is not relevant. I will ask before adding any of them",
        web_pending=True,
    )]


# What an R3 line is. `carrier` is not a line at all: it is the one item that
# is there every Wednesday so the research layer (new events, missing
# deadlines) has somewhere to put what it finds, even in a week with no event
# close enough to mention.
EVENT_REGISTERED = "registered"
EVENT_REGISTER = "register"
EVENT_NO_DEADLINE = "no_deadline"
EVENT_UNCLEAR = "unclear"
EVENT_CARRIER = "carrier"


def unclear_event_key(name: str) -> str:
    """The once-only key for an event whose date the sheet cannot read."""
    return "unclear|" + gtm_sheet.normalise_header(name)


def remind_again_on(today: date, soonest: Optional[date]) -> tuple:
    """(date, word) for R3's "want me to remind you again?" offer.

    The next EVENTS_REMIND_AGAIN_WEEKDAY after today — "Monday" — unless one of
    the events listed FALLS BEFORE that day, in which case a Monday reminder
    would arrive after the thing it is about, and the offer is "tomorrow".
    (The event's own date, not its registration deadline: a deadline this
    Friday is already in the line the reader has just been shown.)
    """
    weekday = int(config.EVENTS_REMIND_AGAIN_WEEKDAY)
    ahead = (weekday - today.weekday() - 1) % 7 + 1
    when = today + timedelta(days=ahead)
    if soonest is not None and soonest < when:
        return today + timedelta(days=1), "tomorrow"
    return when, "on " + when.strftime("%A")


def _r_events(rule, ctx) -> list:
    """R3 — the events worth a line THIS week. Every Wednesday.

    ONE PATH. This used to run every other Wednesday beside a second lane
    (`events.due_events`, one reminder per event at T-20, for ever) that read
    the same tab and knew nothing of this one. Both are gone; this is all of
    it. The research layer still looks for events the tab lacks and for
    missing registration deadlines, and what it finds rides on the carrier
    item below.

    PER ROW (Event, Date, Last day for registration, Registered, Link), with
    W = EVENTS_WINDOW_DAYS (14) — wide enough that an event next Tuesday is in
    THIS Wednesday's post:

      date passed                       never mentioned
      registered                        "You're registered for X on <date>"
                                        when the date is within W
      not registered, deadline ahead    "Register for X by <deadline> (event on
                                        <date>)" when the deadline or the date
                                        is within W
      not registered, deadline passed   skipped, logged "registration closed"
      not registered, no deadline       "X on <date> — no registration deadline
                                        on the sheet" when the date is within W
      date unreadable                   listed ONCE, "date unclear"
                                        (`ctx["events_unclear_seen"]` is what
                                        makes it once; the caller records it
                                        after the post)

    THE OFFER. Every line item carries `remind_on` / `remind_word`, worked out
    from the whole post: "Want me to remind you again on Monday?" — or
    "tomorrow" when something listed falls before Monday.
    """
    today = ctx["today"]
    window = max(1, int(config.EVENTS_WINDOW_DAYS))
    horizon = today + timedelta(days=window)
    seen_unclear = {str(k) for k in (ctx.get("events_unclear_seen") or ())}
    lines: list = []
    for row in ctx.get("events") or ():
        name = _text(row, "event")
        if not name:
            continue
        when = gtm_sheet.parse_event_date(row.get("event_date"))
        closes = gtm_sheet.parse_event_date(row.get("registration_deadline"))
        registered = gtm_sheet.parse_flag(row.get("registered")) is True
        start = when["start"] if when["known"] else None
        deadline = closes["start"] if closes["known"] else None
        soonest = None
        key = ""

        if start is None:
            key = unclear_event_key(name)
            if key in seen_unclear:
                log.info("[R3] skipped %r (row %s): date unclear, already listed once",
                         name, row.get("_row") or "?")
                continue
            raw = _text(row, "event_date")
            kind = EVENT_UNCLEAR
            line = f"{name} — date unclear" + (
                f" (the sheet says \"{raw}\")" if raw else " (no date on the sheet)")
            due = today
        else:
            end = when["end"] or start
            if end < today:
                log.info("[R3] skipped %r (row %s): it was on %s — passed",
                         name, row.get("_row") or "?", dl.iso(start))
                continue
            on = _short_date(start) + ("" if end == start else f" to {_short_date(end)}")
            within = start <= horizon
            if registered:
                if not within:
                    continue
                kind, due, soonest = EVENT_REGISTERED, start, start
                line = f"You're registered for {name} on {on}"
            elif deadline is not None:
                if deadline < today:
                    log.info("[R3] skipped %r (row %s): registration closed on %s",
                             name, row.get("_row") or "?", dl.iso(deadline))
                    continue
                if not (within or deadline <= horizon):
                    continue
                kind, due, soonest = EVENT_REGISTER, deadline, start
                line = f"Register for {name} by {_short_date(deadline)} (event on {on})"
            else:
                if not within:
                    continue
                kind, due, soonest = EVENT_NO_DEADLINE, start, start
                line = f"{name} on {on} — no registration deadline on the sheet"
        lines.append(_item(
            rule=rule, trigger=R_EVENTS, today=today, due=due,
            why=f"R3 (Wednesdays): {kind}, inside the {window}-day window",
            text=line, company=name, sheet_row=row.get("_row"),
            extra={"event_kind": kind, "event_line": line,
                   "event_date": dl.iso(start) if start else "",
                   "event_deadline": dl.iso(deadline) if deadline else "",
                   "event_link": _text(row, "link"), "event_soonest": soonest,
                   "event_unclear_key": key},
        ))

    dated = [i["event_soonest"] for i in lines if i.get("event_soonest")]
    remind_on, remind_word = remind_again_on(today, min(dated) if dated else None)
    for item in lines:
        item.pop("event_soonest", None)
        item["remind_on"] = dl.iso(remind_on)
        item["remind_word"] = remind_word
    # THE CARRIER: nothing to read, somewhere for the research to land.
    carrier = _item(
        rule=rule, trigger=R_EVENTS, today=today, due=today,
        why="R3 (Wednesdays): looks for events the tab lacks and missing deadlines",
        text="", web_pending=True,
        extra={"event_kind": EVENT_CARRIER, "event_line": ""},
    )
    carrier["text"] = ""
    carrier["key"] = "R3:carrier"
    return lines + [carrier]


def _r_deliverables(rule, ctx) -> list:
    """R4 — every open P1 deliverable due by the END OF THIS WEEK, or already past.

    ONE MONDAY POST, THE WHOLE WEEK. The window runs to the Sunday of the week
    it runs in, so Monday's message is the week's checklist rather than a drip
    of whatever falls inside DELIVERABLE_NEAR_DAYS. Anything already past its
    deadline is in too, however old.

    P1 ONLY. PRIORITY IS THE FILTER. A row whose Priority does not match
    DELIVERABLE_P1_MARKERS is never mentioned, whatever its deadline — a P2 due
    tomorrow is still not in the post. Each open row skipped for its priority
    is logged with that priority, so "why is X not in Monday's list" has an
    answer in the log. A SUNDAY run keeps the narrower window (P1 and within
    DELIVERABLE_NEAR_DAYS): the exception is "a P1 due on Monday", and widening
    a Sunday post to the whole week would spend the weekend's one message on
    things with four working days left.

    THIS IS THE ONE SELECTION. A real day, a test day and a simulation all
    reach it through `run()` with the day they are living, so none of them has
    a filter of its own.

    THE TEAM IS THE Functional Dependency CELL (Engineering, Sales, Legal …),
    and blank means DELIVERABLE_DEFAULT_OWNER. The post shows it on its own
    line under the title, then the due date, then the row's link if it has one
    (`drip.render_deliverables`, which also keeps an Action Item and a link
    from appearing twice).

    STATUS BLANK OR NOT DONE. Only DELIVERABLE_DONE_MARKERS count as finished;
    everything else, blank included, is open — chasing a finished item costs
    one correction, skipping an unfinished one costs the deadline.

    DEADLINES CARRY NO YEAR on this tab ("25-Sep"); see `_deliverable_due` for
    how one that has just passed is read as overdue rather than as next year's.
    A row with no readable deadline is left out: "due ?" is not a line anybody
    can act on.
    """
    today = ctx["today"]
    sunday = today.weekday() == 6
    monday = today - timedelta(days=today.weekday())
    if sunday:
        window_end = today + timedelta(days=max(0, int(config.DELIVERABLE_NEAR_DAYS)))
    else:
        window_end = monday + timedelta(days=6)
    out = []
    for row in ctx.get("deliverables") or ():
        item_name = _text(row, "action_item")
        if not item_name:
            continue
        if _matches_any(row.get("status"), config.DELIVERABLE_DONE_MARKERS):
            continue
        is_p1 = bool(_matches_any(row.get("priority"), config.DELIVERABLE_P1_MARKERS))
        if not is_p1:
            log.info(
                "[R4] skipped %r (row %s): priority %r is not P1 "
                "(DELIVERABLE_P1_MARKERS), deadline %r",
                item_name, row.get("_row") or "?",
                _text(row, "priority") or "(blank)",
                _text(row, "deadline") or "(blank)",
            )
            continue
        raw = row.get("deadline")
        due = _deliverable_due(raw, today=today)
        if due is None or due > window_end:
            continue
        days = (due - today).days
        team = _text(row, "dependency") or config.DELIVERABLE_DEFAULT_OWNER
        status = _text(row, "status") or "(blank)"
        priority = _text(row, "priority")
        when = ("overdue by %d day(s)" % -days if days < 0
                else "due today" if days == 0 else "due in %d day(s)" % days)
        out.append(_item(
            rule=rule, trigger=R_DELIVERABLES, today=today, due=due,
            why=(f"R4 ({'Sunday: P1 due soon' if sunday else 'Mondays: P1 due this week'})"
                 f": {priority}, status {status}, {when}"),
            text=f"{item_name} — {when}",
            owner=team, company=item_name, sheet_row=row.get("_row"),
            # WHAT THE POST SHOWS: the title, the team, the deadline and the
            # row's link. The remarks are still not carried — nothing renders
            # them, and a field nothing renders is a field somebody will one
            # day render by accident.
            extra={
                "deliverable": item_name, "item": item_name, "team": team,
                "link": _text(row, "link"),
                "deadline": dl.iso(due), "deadline_pretty": _short_date(due),
                "status": status,
                # NOT `priority`: that key is the item's numeric band, which
                # every sort in this module reads.
                "sheet_priority": priority, "is_p1": True,
                "days_left": days, "week_of": dl.iso(monday),
                "dependency": _text(row, "dependency"),
            },
        ))
    return out


# How recently a yearless deadline must have passed to be read as OVERDUE this
# year rather than as next year's. Three months: nothing on a weekly checklist
# is overdue by more than a quarter and still meant, and nothing is planned a
# full nine months out in "25-Sep" shorthand.
_BARE_LOOKBACK_DAYS = 90


def _deliverable_due(raw, *, today):
    """A deliverable's deadline as a date, or None.

    `gtm_sheet.parse_bare_deadline` reads "25-Sep" as the NEXT 25 September —
    right for planning, wrong for a checklist: on 29 Sep a row due "25-Sep"
    and still open came back as September NEXT YEAR, so "already past" could
    never fire for this tab. Here a next occurrence more than nine months away
    whose previous occurrence passed within `_BARE_LOOKBACK_DAYS` is read as
    that previous one — overdue. The rule's own `today` is passed through, so
    a test day or a simulation reads the sheet as of the day it pretends.
    """
    nxt = gtm_sheet.parse_bare_deadline(raw, today=today)
    if nxt is None:
        return gtm_sheet.sheet_date(raw)
    if (nxt - today).days > 270:
        try:
            prev = nxt.replace(year=nxt.year - 1)
        except ValueError:                      # 29 Feb
            prev = None
        if prev is not None and 0 < (today - prev).days <= _BARE_LOOKBACK_DAYS:
            return prev
    return nxt


def _short_date(d) -> str:
    """"Thu 2 Oct" — how a deliverable's deadline is written in the list."""
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b')}"


def _role_rank(designation: str) -> int:
    """Where this designation sits in PROSPECT_ROLE_ORDER. Lower is better.

    Founders before CXOs before chief scientists before researchers. Anything
    the list does not recognise sorts LAST, in sheet order — an unrecognised
    title is not evidence of seniority in either direction, so it waits rather
    than jumping the queue or being dropped.
    """
    want = gtm_sheet.normalise_header(designation or "")
    if not want:
        return 10_000
    for i, phrase in enumerate(config.PROSPECT_ROLE_ORDER or ()):
        key = gtm_sheet.normalise_header(str(phrase))
        if key and (key in want or want == key):
            return i
    return 10_000


def _r_prospects(rule, ctx) -> list:
    """R5 — contacts with no first contact recorded. Tue and Thu.

    FOUR CONSTRAINTS, and they interact:

      1. Eligible rows are those where First Contact is FALSE or blank AND no
         first-contact date is recorded (`first_contact_done`) — EVERY such
         row the stop rules allow, read from `ctx["prospect_rows"]`, not only
         the "active" ones. The activation gate lets a row through once
         somebody has started on it, which is exactly what a never-contacted
         row has not had: fed only active rows, this rule could see almost
         none of the people it exists to name.
      2. TWO COMPANIES A WEEK, in SHEET ORDER. The bot stays with a company
         until every contact on it has a first contact recorded, then moves on.
         Jumping around is how a company ends up half-contacted forever.
      3. Within a company, ROLE ORDER: founders > CXO > chief scientist >
         research > everything else.
      4. Five per post — enforced by the rule's `max_items_per_post` when the
         drip builds the message, not here, so the preview shows everything
         that qualified and the post shows what fits.

    `ctx["week_companies"]` is the companies already started this week, passed
    in by the caller. It is what makes constraint 2 hold across days: Thursday
    continues Tuesday's companies rather than starting two more.

    THE REPEAT ASK. `ctx["prospect_repeats"]` counts how many posts have carried
    a contact with nothing changed. At PROSPECT_REPEAT_ASK_AT the item asks
    whether to skip them instead of naming them again — three times is where
    repeating turns into nagging.
    """
    today = ctx["today"]
    repeats = ctx.get("prospect_repeats") or {}
    started = [c for c in (ctx.get("week_companies") or []) if c]
    per_week = max(1, int(config.PROSPECT_COMPANIES_PER_WEEK))
    ask_at = max(2, int(config.PROSPECT_REPEAT_ASK_AT))

    # Group eligible rows by company, preserving SHEET ORDER for both the
    # companies and the contacts inside them.
    by_company: dict = {}
    source = ctx.get("prospect_rows")
    for row in (source if source is not None else ctx.get("rows")) or ():
        ok, _reason, _until = row_gate(row, today=today, snoozes=ctx.get("snoozes") or {})
        if not ok:
            continue
        if first_contact_done(row):
            continue
        company = _text(row, "company")
        if not company:
            continue
        by_company.setdefault(company, []).append(row)

    # THE FOCUS GOES FIRST, BEFORE THE TWO-A-WEEK LIMIT PICKS COMPANIES.
    # Ordering the companies after the limit had already chosen two would mean a
    # focus could only reorder what it had not been allowed to influence — the
    # filter has to reach the choice itself or it does nothing useful.
    #
    # `focus_note` is carried onto every item so R5's message can say what the
    # filter did. It is never empty while a focus is live, including when the
    # focus matched NOTHING — a filter that silently matched nothing reads
    # exactly like a quiet week.
    focus_note = ""
    active_focus = ctx.get("focus")
    if active_focus:
        flat = [r for rows_ in by_company.values() for r in rows_]
        ordered_rows, focus_note = focus_mod.apply_to(flat, active_focus)
        regrouped: dict = {}
        for row in ordered_rows:
            regrouped.setdefault(_text(row, "company"), []).append(row)
        by_company = regrouped

    # Finish the companies already started this week before opening new ones.
    ordered = [c for c in started if c in by_company]
    for company in by_company:
        if company not in ordered and len(ordered) < per_week:
            ordered.append(company)

    out = []
    for company in ordered:
        members = sorted(
            by_company.get(company) or [],
            key=lambda r: (_role_rank(_text(r, "designation")), r.get("_row") or 0),
        )
        for row in members:
            key = _contact_key(row)
            seen = int(repeats.get(key) or 0)
            designation = _text(row, "designation")
            if seen >= ask_at:
                text = (f"{_describe(row)} — I have listed them {seen} times with nothing "
                        "changed. Shall I skip them?")
                why = (f"R5: named {seen} times unchanged, which is "
                       f"PROSPECT_REPEAT_ASK_AT ({ask_at}) or more")
            else:
                text = f"{_describe(row)} — no first contact recorded"
                why = (f"R5 (Tue/Thu): First Contact is "
                       f"{_text(row, 'first_contact') or 'blank'}"
                       + (f", role rank {_role_rank(designation)}" if designation else ""))
            out.append(_item(
                rule=rule, trigger=R_PROSPECTS, today=today, due=today,
                why=why + (f"; {focus_note}" if focus_note else ""),
                text=text, company=company, poc=_text(row, "name"),
                designation=designation, sheet_row=row.get("_row"),
                row_key=key, contact_key=key,
                extra={"repeat_count": seen, "ask_to_skip": seen >= ask_at,
                       "role_rank": _role_rank(designation),
                       # THE EMAIL: on file, or to be looked up at send time
                       # for the contacts that make the post (`email_lookup`
                       # is a request, not a promise — see bot._research_message).
                       "email_on_file": _text(row, "email"),
                       "email_lookup": not _text(row, "email"),
                       # WHAT "NOTHING CHANGED" MEANS for the repeat count: the
                       # cells somebody would touch if they had acted.
                       "signature": prospect_signature(row),
                       "focus_note": focus_note,
                       "focus": (active_focus or {}).get("value", "")},
            ))
    return out


def prospect_signature(row: dict) -> str:
    """The state of a never-contacted row, as one string. When it changes,
    R5's repeat count starts again (`db.record_prospect_mention`)."""
    return "|".join(_text(row, role) for role in (
        "first_contact", "first_contact_type", "first_contact_date", "sid_li_added",
        "li_connected_date", "li_dm_date", "email", "next_steps"))


def _r_li_no_dm(rule, ctx) -> list:
    """R6 — connected on LinkedIn more than LI_NO_DM_DAYS ago, still no DM.

    THIS IS THE EMAIL CHECK. It says whether an email is on file; when none
    is, the research layer looks for a verified public address and says where
    it found it, or that it found none — hence WEB-DEPENDENT, but only for the
    half of the rule that needs it.

    THE LINE TALKS ABOUT THE EMAIL ONLY. It used to add "no DM logged". Since
    7 Oct 2026 the sheet's sequence puts the DM after three emails and R13
    owns the step reminders, so a line chasing the DM here would ask for a
    step that is not due. Who is selected has not changed.
    """
    today = ctx["today"]
    days = max(0, int(config.LI_NO_DM_DAYS))
    out = []
    for row in ctx.get("rows") or ():
        ok, _reason, until = row_gate(row, today=today, snoozes=ctx.get("snoozes") or {})
        if not ok:
            continue
        connected = _date(row, "li_connected_date")
        if connected is None:
            continue
        if gtm_sheet.parse_flag(row.get("li_dm_sent")) is True:
            continue
        if _date(row, "li_dm_date") is not None:
            continue
        due = _due(connected, days)
        if due > today:
            continue
        email = _text(row, "email")
        elapsed = (today - connected).days
        text = (f"{_describe(row)} — connected {elapsed} day(s) ago. "
                + (f"Email on file: {email}" if email else "No email on file"))
        out.append(_item(
            rule=rule, trigger=R_LI_NO_DM, today=today,
            due=until or due,
            why=(f"R6 (Tue/Fri): connected {dl.format_date(connected)}, "
                 f"{elapsed}d ago, more than LI_NO_DM_DAYS ({days})"),
            text=text, company=_text(row, "company"), poc=_text(row, "name"),
            designation=_text(row, "designation"), sheet_row=row.get("_row"),
            row_key=_contact_key(row), contact_key=_contact_key(row),
            # THE LOOKUP IS A REQUEST: the sender asks for it, for the contacts
            # that make the post, up to EMAIL_LOOKUP_MAX_PER_POST.
            extra={"email_on_file": email, "connected_days": elapsed,
                   "email_lookup": not email},
        ))
    return out


def _r_dm_no_meeting(rule, ctx) -> list:
    """R7 — DM sent more than DM_NO_MEETING_DAYS ago, still no meeting. Mondays.

    NEVER A PERSON RULE 13 COVERS. Rule 13's call reminders took this rule's
    place on 7 Oct; on 8 Oct the team brought it back on Mondays for everybody
    Rule 13 does NOT cover. `covered_by_next_steps` is Rule 13's own check
    (Connected, with an LI Connected Date), read here rather than copied, so
    one person is never chased by both and never falls between the two.

    SHOWS DAYS SINCE THE DM AND THE LAST NOTE LOGGED, because those two are what
    a person needs to decide whether to chase again or leave it. "No reply after
    9 days, last note: waiting on their legal" is a sentence somebody can act
    on; "follow up with Acme" is not.
    """
    today = ctx["today"]
    days = max(0, int(config.DM_NO_MEETING_DAYS))
    markers = connected_markers()
    covered = 0
    out = []
    for row in ctx.get("rows") or ():
        ok, _reason, until = row_gate(row, today=today, snoozes=ctx.get("snoozes") or {})
        if not ok:
            continue
        sent = _date(row, "li_dm_date")
        if sent is None:
            continue
        if _date(row, "meeting_date") is not None:
            continue
        if covered_by_next_steps(row, markers):
            covered += 1
            continue
        due = _due(sent, days)
        if due > today:
            continue
        elapsed = (today - sent).days
        note = last_note(row)
        out.append(_item(
            rule=rule, trigger=R_DM_NO_MEETING, today=today, due=until or due,
            why=(f"R7 (Mondays): DM sent {dl.format_date(sent)}, {elapsed}d ago, "
                 f"more than DM_NO_MEETING_DAYS ({days}), no meeting date"),
            text=(f"{_describe(row)} — {elapsed} day(s) since the DM, no meeting booked. "
                  + (f"Last note: {note}" if note else "No note logged against it")),
            company=_text(row, "company"), poc=_text(row, "name"),
            designation=_text(row, "designation"), sheet_row=row.get("_row"),
            row_key=_contact_key(row), contact_key=_contact_key(row),
            extra={"days_since_dm": elapsed, "last_note": note,
                   "role_rank": _role_rank(_text(row, "designation"))},
        ))
    if covered:
        log.info("[rules] %s: %d DM'd contact(s) with no meeting are Rule 13's "
                 "(Connected, with an LI Connected Date) and are left to it; "
                 "%d listed here", getattr(rule, "id", "") or "R7", covered, len(out))
    return out


def _r_meeting_prep(rule, ctx) -> list:
    """R8 — prep touches at T-5, T-3 and on the day, each at MEETING_DAYOF_TIME.

    EVERY TOUCH HAS THE SAME FIXED TIME (8 Oct). Only the day-of touch used to;
    the two earlier ones took a spaced slot and so pushed the day's other
    posts back. A prep note is about a meeting, not about the day's order, so
    all three go at MEETING_DAYOF_TIME, outside the order and the gap.

    ANCHORED TO THE MEETING, NOT TO A WEEKDAY, which is why its weekday list in
    bot_rules.yaml is empty.

    TOUCHES ALREADY IN THE PAST ARE SKIPPED. A meeting booked with three days'
    notice gets the 3-day and day-of touches and never a late 5-day one —
    sending "five days to go" two days before is worse than sending nothing.

    RESCHEDULING RE-ANCHORS EVERYTHING, and costs no code: the meeting date is
    read fresh from the sheet on every run, so a moved meeting simply produces a
    different set of touches from the next run onwards.

    Checks the meeting is ready (deck, package, demo) and carries recent news
    about the person and the company — the news half is WEB-DEPENDENT.
    """
    today = ctx["today"]
    befores = _int_list(config.MEETING_PREP_DAYS_BEFORE)
    out = []
    for row in ctx.get("rows") or ():
        ok, _reason, _until = row_gate(row, today=today, snoozes=ctx.get("snoozes") or {})
        if not ok:
            continue
        meeting = _date(row, "meeting_date")
        if meeting is None or meeting < today:
            continue
        if gtm_sheet.is_meeting_completed(row.get("meeting_status")):
            continue
        days_out = (meeting - today).days
        touch = None
        if days_out == 0:
            touch = f"day of, {config.MEETING_DAYOF_TIME} IST"
        elif days_out in befores:
            touch = f"T-{days_out}"
        if touch is None:
            continue
        package = _text(row, "package")
        out.append(_item(
            rule=rule, trigger=R_MEETING_PREP, today=today,
            # The day-of touch is NOT weekend-shifted: the meeting is when the
            # meeting is, and a Saturday meeting still needs its morning note.
            due=meeting - timedelta(days=days_out),
            why=(f"R8: meeting on {dl.format_date(meeting)}, {touch} touch"
                 + (f" (skipping the T-{max(befores)} touch, already past)"
                    if befores and days_out < max(befores) and touch.startswith("T-")
                    else "")),
            text=(f"{_describe(row)} — meeting {('today' if days_out == 0 else f'in {days_out} day(s)')}"
                  f" on {dl.format_date(meeting)}. Deck, package and demo ready?"
                  + (f" Package: {package}." if package else "")),
            company=_text(row, "company"), poc=_text(row, "name"),
            designation=_text(row, "designation"), sheet_row=row.get("_row"),
            row_key=_contact_key(row), contact_key=_contact_key(row),
            web_pending=True,
            extra={"meeting_date": dl.iso(meeting), "touch": touch,
                   "days_out": days_out,
                   "dayof_time": config.MEETING_DAYOF_TIME},
        ))
    return out


def _r_meeting_followup(rule, ctx) -> list:
    """R9 — meeting completed, no next steps. Then every 3 days, up a ladder.

    AT MEETING_DAYOF_TIME, LIKE THE PREP (8 Oct): every rung has that fixed
    time, outside the day's order and the gap, so a follow-up never takes one
    of the spaced slots and never waits behind one.

    "NO NEXT STEPS" IS A BLANK NOTES CELL (Notes/Remarks, the `next_steps`
    role). It is never the Next Steps dropdown: a row whose dropdown says "Send
    email 1" has not had its meeting written up.

    THE LADDER IS ONE CHANNEL POST, THEN TWO DMs, THEN ONE ESCALATION TO SID,
    THEN STOP (MEETING_FOLLOWUP_LADDER). Running off the end stops the chase
    permanently, and that end is the point: a follow-up loop with no last rung
    is the thing that gets a bot muted.

    `ctx["meeting_followups"]` is {row_key: {"sent": n, "last_iso": "..."}},
    passed in by the caller. The engine reads the rung it is on; it does not
    advance it — advancing is a write, and a write in here would mean every
    `cadence preview` burned a rung.

    GUARDRAILS STILL REFUSE DMs. A rung resolving to "dm" or "escalation" is
    computed, ranked and shown, and the item says plainly that it would have
    been a DM. Dropping it would hide a rule that is supposed to be running.
    """
    today = ctx["today"]
    after = max(0, int(config.MEETING_FOLLOWUP_AFTER_DAYS))
    every = max(1, int(config.MEETING_FOLLOWUP_EVERY_DAYS))
    ladder = [str(d).strip().lower() for d in (config.MEETING_FOLLOWUP_LADDER or []) if str(d).strip()]
    state = ctx.get("meeting_followups") or {}
    out = []
    for row in ctx.get("rows") or ():
        ok, _reason, _until = row_gate(row, today=today, snoozes=ctx.get("snoozes") or {})
        if not ok:
            continue
        if not gtm_sheet.is_meeting_completed(row.get("meeting_status")):
            continue
        if _text(row, "next_steps"):
            continue
        met = _date(row, "meeting_date")
        if met is None or met > today:
            continue
        key = _contact_key(row)
        sent = int((state.get(key) or {}).get("sent") or 0)
        if not ladder or sent >= len(ladder):
            continue                    # off the end of the ladder: STOP, for good
        last_iso = str((state.get(key) or {}).get("last_iso") or "")
        last = dl.parse_date(last_iso) if last_iso else None
        due = _due(last, every) if last else _due(met, after)
        if due > today:
            continue
        rung = ladder[sent]
        remaining = len(ladder) - sent - 1

        # WHERE THIS RUNG ACTUALLY GOES, once the DM switch is taken into
        # account. With SALES_DMS_ENABLED off, rungs 2-3 are ordinary channel
        # follow-ups and the escalation rung is a channel post ADDRESSED TO SID.
        #
        # THE MESSAGE NEVER SAYS "this would have been a DM". It used to, and it
        # was the wrong thing to tell anybody: the reader does not care about
        # the bot's delivery plumbing, and a nudge that spends a clause
        # apologising for its own channel reads as a bot with a problem rather
        # than a colleague with a question. The fallback is logged instead —
        # `[dm] fallback-to-channel` — where an operator can see it and the
        # team cannot.
        effective = rung
        addressed_to = ""
        if rung in (rules_mod.DEST_DM, rules_mod.DEST_ESCALATION)                 and not config.SALES_DMS_ENABLED:
            effective = rules_mod.DEST_CHANNEL
            if rung == rules_mod.DEST_ESCALATION:
                addressed_to = config.ESCALATION_ADDRESSEE
            log.info(
                "[dm] fallback-to-channel rung=%d item=%s (%s) — SALES_DMS_ENABLED is "
                "off, so this posts in the channel%s",
                sent + 1, key, rung,
                f" addressed to {addressed_to}" if addressed_to else "",
            )

        out.append(_item(
            rule=rule, trigger=R_MEETING_FOLLOWUP, today=today, due=due,
            destination=effective,
            owner=addressed_to or "",
            why=(f"R9: meeting {dl.format_date(met)} is completed with no next steps; "
                 f"rung {sent + 1} of {len(ladder)} ({rung}), {remaining} left before I stop"
                 + (f"; posting in channel to {addressed_to} because DMs are off"
                    if effective != rung else "")),
            text=(f"{_describe(row)} — the meeting on {dl.format_date(met)} has no next "
                  "steps logged. What are the next steps, which package was discussed, and "
                  "what is the estimated deal size?"),
            company=_text(row, "company"), poc=_text(row, "name"),
            designation=_text(row, "designation"), sheet_row=row.get("_row"),
            row_key=key, contact_key=key,
            extra={"rung": sent, "rung_destination": rung,
                   "delivered_to": effective,
                   "addressed_to": addressed_to,
                   "rungs_total": len(ladder), "rungs_left": remaining,
                   "meeting_date": dl.iso(met),
                   "dayof_time": config.MEETING_DAYOF_TIME},
        ))
    return out


# What R10 posts when it cannot run because the columns it reads are empty.
CLOSURE_EMPTY_NOTICE = (
    "No closure support this week — Prospect Status and Closure Prob% are empty "
    "in the GTM sheet. Fill them in and I'll pick it up next Monday.")


def _r_closure_support(rule, ctx) -> list:
    """R10 — deal / demo / quote AND closure strictly above CLOSURE_SUPPORT_MIN.

    EXACTLY 50 IS EXCLUDED. "Above 50" is the rule as written, and a boundary a
    bot decides for itself is a boundary nobody agreed to. `>` not `>=`, and the
    reason line says so, so nobody has to read this file to find out.

    Asks what is needed for the next stage and shares relevant news — the news
    half is WEB-DEPENDENT.

    AN EMPTY COLUMN IS SAID OUT LOUD; AN EMPTY RESULT IS NOT. When Prospect
    Status — or Closure Prob% — is blank on EVERY active row, the rule cannot
    run at all, and a silent Monday reads exactly like "no deal is close".
    So that one case posts a line saying which columns are empty
    (CLOSURE_EMPTY_NOTICE). When the columns have values and no deal
    qualifies, that is an answer: nothing is posted, and the log says why.
    """
    today = ctx["today"]
    floor = int(config.CLOSURE_SUPPORT_MIN)
    out = []
    looked = with_status = with_pct = in_stage = 0
    for row in ctx.get("rows") or ():
        ok, _reason, _until = row_gate(row, today=today, snoozes=ctx.get("snoozes") or {})
        if not ok:
            continue
        looked += 1
        with_status += 1 if _text(row, "prospect_status") else 0
        with_pct += 1 if _text(row, "closure_prob") else 0
        stage = _matches_any(row.get("prospect_status"), config.CLOSURE_SUPPORT_STAGES)
        if not stage:
            continue
        in_stage += 1
        pct = closure_percent(row)
        if pct is None or pct <= floor:
            continue
        out.append(_item(
            rule=rule, trigger=R_CLOSURE_SUPPORT, today=today, due=today,
            why=(f"R10 (Mondays): prospect status {_text(row, 'prospect_status')!r} "
                 f"({stage}) and closure {pct}%, above CLOSURE_SUPPORT_MIN ({floor}%) "
                 f"— exactly {floor}% would not qualify"),
            # THE SUPPORTIVE VERSION, from the call: an offer of help, not a
            # "what is needed" that reads as a chase.
            text=(f"{_describe(row)} is in the closure stage — anything I can pull "
                  "together to help it along: the PoC's background, the company, a "
                  "package summary? Say the word."),
            company=_text(row, "company"), poc=_text(row, "name"),
            designation=_text(row, "designation"), sheet_row=row.get("_row"),
            row_key=_contact_key(row), contact_key=_contact_key(row),
            web_pending=True,
            extra={"closure_pct": pct, "stage": _text(row, "prospect_status")},
        ))
    if out or not looked:
        return out
    if not with_status or not with_pct:
        empty = [name for name, n in (("Prospect Status", with_status),
                                      ("Closure Prob%", with_pct)) if not n]
        log.info("[R10] %s blank on all %d active row(s) — posting the empty-columns "
                 "notice instead of staying silent", " and ".join(empty), looked)
        notice = _item(
            rule=rule, trigger=R_CLOSURE_SUPPORT, today=today, due=today,
            why=f"R10 (Mondays): {' and '.join(empty)} blank on every active row",
            text=CLOSURE_EMPTY_NOTICE,
            extra={"notice": CLOSURE_EMPTY_NOTICE, "empty_columns": empty},
        )
        notice["key"] = "R10:empty-columns"
        return [notice]
    log.info("[R10] no closure support today: %d active row(s), %d with a Prospect "
             "Status (%d in %s), %d with a Closure Prob%% — none is in a closure "
             "stage above %d%%. Nothing posted.", looked, with_status, in_stage,
             "/".join(str(x) for x in config.CLOSURE_SUPPORT_STAGES), with_pct, floor)
    return out


def _r_new_pipeline_company(rule, ctx) -> list:
    """R11 — a company that has just appeared in the Master Pipeline.

    THERE IS NO CREATED-DATE COLUMN on that tab, so "appeared" means "in today's
    names and not in the last snapshot". The snapshot is taken by the CALLER and
    the first-seen dates arrive in `ctx["new_companies"]` as
    [{"company": name, "first_seen": iso}] — the engine only reads them. Had the
    engine taken the snapshot, every `cadence preview` would have advanced the
    state it was previewing.

    ONE WORKING DAY AFTER IT APPEARS (NEW_COMPANY_AFTER_WORKING_DAYS), and only
    within NEW_COMPANY_WINDOW_DAYS — without a window, a snapshot gap (the bot
    was down for a week) would dump every company added in that gap into one
    post.

    THE RULE RUNS ON WEDNESDAYS ONLY (8 Oct), AND THE TWO NUMBERS ABOVE STILL
    GIVE EVERY COMPANY EXACTLY ONE WEDNESDAY. The snapshot is taken on every
    run of the queue, whatever the weekday, so a company is noticed the day it
    appears. With a wait of 1 working day and a window of 7 days: one that
    appears Thursday to Tuesday is 1 to 6 days old on the next Wednesday (asked
    then, and 8 or more days old on the one after: not asked twice); one that
    appears ON a Wednesday is not yet due that day and is exactly 7 days old on
    the next, which the window still admits (`> window`, not `>=`). A window
    below 7 would lose Wednesday's companies and a window of 14 or more would
    ask twice, so 7 is the value a weekly run needs.

    ASKS FIRST, SEARCHES ON A YES. The item names the company and nothing
    else; the drip posts "want me to look for relevant PoCs?" and opens a
    `poc_lookup` proposal against that message. Only an approver's yes runs a
    search (bot._apply_poc_lookup). Nothing is ever added to Outreach PoCs
    without a separate yes.
    """
    today = ctx["today"]
    wait = max(0, int(config.NEW_COMPANY_AFTER_WORKING_DAYS))
    window = max(1, int(config.NEW_COMPANY_WINDOW_DAYS))
    out = []
    for entry in ctx.get("new_companies") or ():
        name = str((entry or {}).get("company") or "").strip()
        first_seen = dl.parse_date(str((entry or {}).get("first_seen") or ""))
        if not name or first_seen is None:
            continue
        due = shift_off_weekend(dl.add_working_days(first_seen, wait))
        if due > today:
            continue
        if (today - first_seen).days > window:
            continue
        out.append(_item(
            rule=rule, trigger=R_NEW_COMPANY, today=today, due=due,
            why=(f"R11 (Wednesdays): {name} first appeared in the Master Pipeline "
                 f"on {dl.format_date(first_seen)}, {(today - first_seen).days} day(s) ago"),
            text=(f"{name} is new in the Master Pipeline. Want me to look for "
                  "relevant PoCs for outreach?"),
            company=name, web_pending=False,
            extra={"first_seen": dl.iso(first_seen)},
        ))
    return out


def _r_sales_packages(rule, ctx) -> list:
    """R12 — every package whose Ready? is No or blank. Thursdays.

    BLANK READS AS NO (`gtm_sheet.ready_flag`). A package nobody has marked
    ready is a package nobody has said is ready, and the two mistakes do not
    cost the same: calling a finished package unready costs one question, and
    offering a prospect a half-built one costs a promise somebody else keeps.
    """
    today = ctx["today"]
    out = []
    for row in ctx.get("packages") or ():
        name = _text(row, "name") or _text(row, "package")
        if not name:
            continue
        if gtm_sheet.ready_flag(row.get("ready")) == "Yes":
            continue
        completion = _text(row, "completion")
        status = _text(row, "status") or "(blank)"
        raw_ready = _text(row, "ready") or "blank"
        out.append(_item(
            rule=rule, trigger=R_PACKAGES, today=today, due=today,
            why=f"R12 (Thursdays): Ready? is {raw_ready}, status {status}",
            text=(f"{name} — {status}"
                  + (f", {completion} complete" if completion else "")
                  + ". What is the status, and when will it be ready?"),
            company=name, sheet_row=row.get("_row"),
            extra={"completion": completion, "status": status,
                   "ready": gtm_sheet.ready_flag(row.get("ready"))},
        ))
    return out


# THERE IS NO REMINDER LANE HERE ANY MORE. One-off reminders used to be
# emitted from this module too (`_scheduled_reminders`), as drip items due "on
# or before today" — alongside the exact-minute loop in bot.py reading the same
# table. A reminder with a company attached therefore went out twice: once in
# the morning drip and once at its minute; and one whose date had passed was
# re-posted by the drip every day, because the drip never closed it.
#
# `bot._fire_due_reminders` is the only sender now. SCHEDULED_REMINDER stays as
# a name because drip.NEVER_COUNTED and the "reminder" heading still use it.


# -- R13: next steps for connected contacts -----------------------------------

# What the Next Steps dropdown (Q) says, as a code. `dm` and `call` are two
# options on the dropdown and ONE stage of the rule: see `step_signature`.
STEP_BLANK = "blank"
STEP_RESEARCH = "research"
STEP_DM = "dm"
STEP_CALL = "call"
STEP_UNKNOWN = "unknown"

# THE COLUMNS THE RULE CANNOT RUN WITHOUT. A row parsed from the sheet carries
# a key for every role its tab mapped, blank or not, so a role no row carries
# is a column the bot cannot see (the pre-7 Oct layout, or a renamed header).
NEXT_STEP_REQUIRED_ROLES = (
    "outreach_step", "email_1_sent", "email_1_date", "email_2_sent",
    "email_2_date", "email_3_sent", "email_3_date",
)

# WHETHER THE REMINDER TO MARK SOMEBODY UNRESPONSIVE ENDS THE RULE FOR THEM FOR
# GOOD. True: once it has gone out the rule never names them again, whatever
# the cells later say. False: a later change to their step reopens them.
NEXT_STEP_CLOSED_IS_FINAL = True

_EMAIL_STEP_RE = re.compile(r"send e ?mail ([123])")


def step_code(raw) -> str:
    """The Next Steps cell as a code: blank, research, email1..3, dm, call, or
    unknown.

    COMPARED NORMALISED, because a dropdown gets retyped: "Send Email 1" is
    "Send email 1", and "Call PoC" is "Call the PoC". Anything else is
    `unknown`, and the caller skips the row and SAYS SO — guessing which step
    a free-text cell means would put a made-up ask in front of the team.
    """
    text = " ".join(w for w in _norm(raw).split() if w != "the")
    if not text:
        return STEP_BLANK
    if text == "research poc":
        return STEP_RESEARCH
    m = _EMAIL_STEP_RE.fullmatch(text)
    if m:
        return f"email{m.group(1)}"
    if text in ("reach by li dm", "reach by linkedin dm"):
        return STEP_DM
    if text == "call poc":
        return STEP_CALL
    return STEP_UNKNOWN


def _email_n(code: str) -> int:
    return int(code[-1]) if str(code).startswith("email") else 0


def _email_sent(row: dict, n: int) -> bool:
    return gtm_sheet.parse_flag(row.get(f"email_{n}_sent")) is True


def _email_date(row: dict, n: int) -> Optional[date]:
    return _date(row, f"email_{n}_date")


def step_signature(row: dict, code: Optional[str] = None) -> str:
    """The step a row is on, as one string: the stage and the cells it turns on.

    WHEN THIS CHANGES, THE PERSON IS NEW AGAIN — their place in the rotation and
    their call count belong to the old step. So it holds only what somebody
    would touch to move the step on, and nothing that changes by itself.

    "Reach by LI DM" AND "Call the PoC" SHARE ONE STAGE. The rule itself asks
    people to change the first to the second once the calls start, and doing
    what it asked must not restart the call clock.
    """
    code = code or step_code(row.get("outreach_step"))
    connected = _date(row, "li_connected_date")
    c_iso = dl.iso(connected) if connected else ""
    if code == STEP_BLANK:
        return f"start|{c_iso}"
    if code == STEP_RESEARCH:
        return f"research|{c_iso}"
    n = _email_n(code)
    if n:
        when = _email_date(row, n)
        return (f"email{n}|{'yes' if _email_sent(row, n) else ''}|"
                f"{dl.iso(when) if when else ''}")
    if code in (STEP_DM, STEP_CALL):
        dm = _date(row, "li_dm_date")
        return f"dm|{_norm(row.get('li_dm_sent'))}|{dl.iso(dm) if dm else ''}"
    return f"unknown|{_norm(row.get('outreach_step'))}"


def _day_month(d: date) -> str:
    """"5 Oct" — a sheet date the way a person says it in a sentence."""
    return f"{d.day} {d.strftime('%b')}"


def next_step_for(row: dict, *, today: date, entry: Optional[dict] = None) -> dict:
    """What ONE connected, dated row needs from the rule today.

    Returns {"status": "due" | "waiting" | "unknown_step", "code", "signature",
    "ask", "due", "missing", "n", "when", "set_call", "why"}. PURE: `entry` is
    the row's stored state (or None) and is only read.

    THE TABLE THIS IMPLEMENTS is in bot_rules.yaml above R13, with the setting
    each number comes from. Two readings worth stating here:

      GO BY THE DROPDOWN. When Next Steps says "Send email 2" and 1st Email
      Sent is blank, the ask is still about email 2, and the line also asks
      for the cell that is missing. The dropdown is what the team maintains
      first; arguing with it would stall the row on a bookkeeping gap.

      A BLANK PREVIOUS DATE MEANS DUE NOW. "Two days after the previous step"
      cannot be counted from nothing, and waiting for a date nobody logged
      would hide the row for good.
    """
    code = step_code(row.get("outreach_step"))
    out = {"status": "unknown_step", "code": code, "signature": "", "ask": "",
           "due": None, "missing": [], "n": 0, "when": "", "set_call": False,
           "why": ""}
    if code == STEP_UNKNOWN:
        out["why"] = f"Next Steps says {_text(row, 'outreach_step')!r}"
        return out
    sig = step_signature(row, code)
    out["signature"] = sig
    connected = _date(row, "li_connected_date")
    first = max(0, int(config.NEXT_STEP_FIRST_DAYS))
    after_prev = max(0, int(config.NEXT_STEP_AFTER_PREVIOUS_DAYS))
    after_email = max(0, int(config.NEXT_STEP_AFTER_EMAIL_DAYS))

    def done(ask, due, why, **more):
        out.update(ask=ask, due=due, why=why, **more)
        out["status"] = "due" if today >= due else "waiting"
        return out

    def gaps(n: int) -> list:
        """The cells email N's step implies and the row lacks."""
        cells = []
        if not _email_sent(row, n):
            cells.append(wording.email_sent_cell(n))
        if _email_date(row, n) is None:
            cells.append(wording.email_date_cell(n))
        return cells

    if code in (STEP_BLANK, STEP_RESEARCH):
        due = (connected + timedelta(days=first)) if connected else today
        return done(
            "ask_next" if code == STEP_BLANK else "researched", due,
            f"Next Steps is {'blank' if code == STEP_BLANK else 'Research the PoC'}; "
            f"LI Connected Date + NEXT_STEP_FIRST_DAYS ({first})")

    n = _email_n(code)
    if n:
        if not _email_sent(row, n):
            previous = connected if n == 1 else _email_date(row, n - 1)
            due = (previous + timedelta(days=after_prev)) if previous else today
            return done(
                "email_out", due,
                f"Send email {n}, {wording.email_sent_cell(n)} not yes; "
                + (f"previous step {dl.iso(previous)} + NEXT_STEP_AFTER_PREVIOUS_DAYS "
                   f"({after_prev})" if previous
                   else "previous step date blank, so due now"),
                n=n, missing=gaps(n - 1) if n >= 2 else [])
        sent_on = _email_date(row, n)
        if sent_on is not None:
            return done(
                "advance", sent_on + timedelta(days=after_email),
                f"email {n} sent {dl.iso(sent_on)} + NEXT_STEP_AFTER_EMAIL_DAYS "
                f"({after_email})", n=n, when=_day_month(sent_on))
        return done("log_date", today,
                    f"{wording.email_sent_cell(n)} is yes with no date", n=n)

    # -- Reach by LI DM / Call the PoC ---------------------------------------
    dm = _date(row, "li_dm_date")
    if config.NEXT_STEP_DM_REPLIED_MARKERS and _matches_any(
            row.get("li_dm_sent"), config.NEXT_STEP_DM_REPLIED_MARKERS):
        return done("dm_replied", today,
                    f"LI DM Sent says {_text(row, 'li_dm_sent')!r}: no call chase")
    if dm is None:
        if code == STEP_DM:
            third = _email_date(row, 3)
            due = (third + timedelta(days=after_prev)) if third else today
            return done(
                "dm_out", due,
                "Reach by LI DM, no LI DM Date; "
                + (f"3rd Email Date {dl.iso(third)} + NEXT_STEP_AFTER_PREVIOUS_DAYS "
                   f"({after_prev})" if third else "3rd Email Date blank, so due now"),
                missing=gaps(3))
        return done("call_no_dm_date", today, "Call the PoC with no LI DM Date")

    call_after = max(0, int(config.NEXT_STEP_CALL_AFTER_DM_DAYS))
    call_every = max(1, int(config.NEXT_STEP_CALL_EVERY_DAYS))
    call_until = max(0, int(config.NEXT_STEP_CALL_UNTIL_DAYS))
    start = dm + timedelta(days=call_after)
    end = dm + timedelta(days=call_until)
    when = _day_month(dm)
    if today < start:
        return done("call", start,
                    f"LI DM Date {dl.iso(dm)} + NEXT_STEP_CALL_AFTER_DM_DAYS "
                    f"({call_after})", when=when)
    if today > end:
        return done("unresponsive", today,
                    f"more than NEXT_STEP_CALL_UNTIL_DAYS ({call_until}) since the "
                    f"LI DM on {dl.iso(dm)}", when=when)
    same = bool(entry) and str(entry.get("signature") or "") == sig
    calls = int((entry or {}).get("calls") or 0) if same else 0
    last_call = dl.parse_date((entry or {}).get("last_call_date")) if same else None
    due = start
    if calls and last_call is not None:
        due = last_call + timedelta(days=call_every)
    return done(
        "call", due,
        f"LI DM {dl.iso(dm)}, {(today - dm).days}d ago; call reminder "
        f"{calls + 1}, every NEXT_STEP_CALL_EVERY_DAYS ({call_every}) until day "
        f"{call_until}",
        when=when, set_call=(code == STEP_DM))


def _next_step_order(person: dict) -> tuple:
    """THE ROTATION'S ORDER, in one place: least recently named first, then
    sheet order.

    Never named sorts first ("" is before every ISO date), so the list is
    walked top to bottom and then from the top again. Sheet order is what
    Vaishnavi asked for; Priority (AF) is read and shown in the preview and
    deliberately not used here.
    """
    return (person["last_date"], person["row"].get("_row") or 0)


def _r_next_step_followups(rule, ctx) -> list:
    """R13 — the next step for people we are connected with. Weekdays, one post.

    WHO IS IN: rows whose "Sid - LI Addition" says Connected and whose LI
    Connected Date reads as a date, that the stop and snooze gates let through.
    A connected row with no date is skipped, logged and listed in the report,
    because "connected, but when?" is a gap somebody can fix in ten seconds
    and cannot fix if nobody tells them.

    WHO IS PICKED: at most `max_items_per_post`. Call reminders that are due
    go first — that clock is counted from a date and runs out — then the
    people named least recently (`_next_step_order`).

    THE EVALUATOR PICKS, NOT THE DRIP. Every other rule hands over everything
    that qualified and lets the post take what fits. Here the five ARE the
    rule; handing over thirty would let the cross-rule dedup and the post's
    own ordering choose a different five from the ones the rotation meant.

    `ctx["next_step_state"]` is {row_key: {...}} from the caller. NONE MEANS
    UNREADABLE, AND THEN NOTHING IS PRODUCED: with no state the rotation would
    restart and people already closed would be chased again. Nothing is
    written here; the sender records a mention after a real send.

    A STORED ENTRY WHOSE SIGNATURE DIFFERS FROM THE ROW'S IS IGNORED — that is
    all "the state for the old step clears" needs, and it means a preview
    never has to write. The one thing that survives a step change is `closed`.

    ONCE TODAY'S POST HAS GONE, TODAY'S PEOPLE STAY TODAY'S PEOPLE. The queue
    is recomputed on every sweep tick. Without this, the tick after the post
    would pick the NEXT five (the first five now read "named today"), and the
    one-mention-a-day dedup would take those five away from R5 and R6 for a
    post that is never going to be sent, while freeing the five who really
    were named. So anyone the state says was named today is returned as-is and
    nobody else is; the drip does not send the group twice in a day.
    """
    today = ctx["today"]
    rule_id = getattr(rule, "id", "") or "R13"
    report = {"connected": 0, "due": 0, "picked": [], "queued": [], "waiting": 0,
              "no_date": [], "unknown_step": [], "paused": [], "completed": 0,
              "closed": [], "state": "ok"}
    ctx.setdefault("reports", {})[rule_id] = report
    state = ctx.get("next_step_state")
    if state is None:
        report["state"] = "unreadable"
        log.info("[rules] %s: the rotation state was not supplied or could not be "
                 "read, so nobody is named", rule_id)
        return []

    markers = connected_markers()
    snoozes = ctx.get("snoozes") or {}
    # Every row, not only the "active" ones, for the reason R5 gives: a row
    # marked Connected with no date may not have passed the activation gate.
    source = ctx.get("prospect_rows")
    due_people: list = []
    named_today: list = []
    today_iso = dl.iso(today)

    def entry_of(row, detail=""):
        return {"sheet_row": row.get("_row"), "poc": _text(row, "name"),
                "company": _text(row, "company"), "detail": detail,
                "poc_priority": _text(row, "poc_priority")}

    in_sheet_order = sorted(
        (source if source is not None else ctx.get("rows")) or (),
        key=lambda r: r.get("_row") or 0)
    # A COLUMN THE BOT CANNOT SEE IS NOT A BLANK CELL. Without this, a sheet
    # with no Next Steps column would have every connected contact reported as
    # "Next Steps is blank", which states something about the sheet that the
    # bot has not read. Nothing is posted and the report names the columns.
    seen_roles = set().union(*(r.keys() for r in in_sheet_order)) if in_sheet_order else set()
    absent = [role for role in NEXT_STEP_REQUIRED_ROLES if role not in seen_roles]
    if in_sheet_order and absent:
        report["state"] = "no_step_column"
        report["missing_columns"] = absent
        log.warning(
            "[rules] %s: the Outreach PoCs tab has no column mapped to %s, so "
            "nobody is named. Check the header row (Next Steps, 1st/2nd/3rd Email "
            "Sent and Date) or GTM_COLUMN_MAP.", rule_id, ", ".join(absent))
        return []
    for row in in_sheet_order:
        if not marked_connected(row, markers):
            continue
        report["connected"] += 1
        if "outreach_step" not in row:
            # This one row has no Next Steps cell to read (the others do), so
            # it is skipped rather than reported as blank.
            continue
        ok, _reason, _until = row_gate(row, today=today, snoozes=snoozes)
        if not ok:
            continue
        named =state.get(_contact_key(row)) or {}
        if str(named.get("last_date") or "") == today_iso:
            named_today.append({
                "row": row, "key": _contact_key(row), "last_date": today_iso,
                "found": next_step_for(row, today=today, entry=named),
            })
            continue
        if not has_connected_date(row):
            raw = _text(row, "li_connected_date")
            report["no_date"].append(entry_of(
                row, f"LI Connected Date reads {raw!r}" if raw
                else "LI Connected Date is blank"))
            log.info("[rules] %s: row %s (%s) is Connected with no readable LI "
                     "Connected Date (%r) — skipped", rule_id, row.get("_row"),
                     _describe(row), raw)
            continue
        key = _contact_key(row)
        stored = state.get(key) or None
        if stored and stored.get("closed") and (
                NEXT_STEP_CLOSED_IS_FINAL
                or str(stored.get("signature") or "") == step_signature(row)):
            report["closed"].append(entry_of(
                row, "asked to mark Unresponsive on "
                     f"{stored.get('closed_date') or 'an earlier day'}"))
            continue
        if gtm_sheet.is_meeting_completed(row.get("meeting_status")):
            report["completed"] += 1
            continue
        meeting = _date(row, "meeting_date")
        if (config.NEXT_STEP_PAUSE_FOR_BOOKED_MEETING and meeting is not None
                and meeting >= today):
            report["paused"].append(entry_of(
                row, f"meeting on {dl.format_date(meeting)}"))
            continue
        found = next_step_for(row, today=today, entry=stored)
        if found["status"] == "unknown_step":
            raw = _text(row, "outreach_step")
            report["unknown_step"].append(entry_of(row, f"Next Steps says {raw!r}"))
            log.info("[rules] %s: row %s (%s) has a Next Steps value I don't know "
                     "(%r) — skipped", rule_id, row.get("_row"), _describe(row), raw)
            continue
        if found["status"] == "waiting":
            report["waiting"] += 1
            continue
        same = bool(stored) and str(stored.get("signature") or "") == found["signature"]
        due_people.append({
            "row": row, "key": key, "found": found,
            "last_date": str(stored.get("last_date") or "") if same else "",
        })

    report["due"] = len(due_people)
    calls = sorted((p for p in due_people if p["found"]["ask"] == "call"),
                   key=_next_step_order)
    others = sorted((p for p in due_people if p["found"]["ask"] != "call"),
                    key=_next_step_order)
    limit = max(1, int(getattr(rule, "max_items_per_post", 5) or 5))
    ranked = calls + others
    picked, queued = ranked[:limit], ranked[limit:]
    if named_today:
        picked, queued = named_today, ranked
        report["posted_today"] = True

    out = []
    for position, person in enumerate(picked):
        row, found = person["row"], person["found"]
        n = found["n"]
        label = {
            STEP_BLANK: "", STEP_RESEARCH: wording.STEP_RESEARCH,
            STEP_DM: wording.STEP_DM, STEP_CALL: wording.STEP_CALL,
        }.get(found["code"], wording.STEP_EMAILS[n - 1] if n else "")
        who = wording.next_step_who(_text(row, "name"), _text(row, "company"))
        line = wording.next_step_line(
            found["ask"], who=who, step_label=label, n=n, when=found["when"],
            missing=found["missing"], set_call=found["set_call"],
        ) or f"{who}: named in today's post"
        report["picked"].append({**entry_of(row, found["ask"]), "line": line})
        out.append(_item(
            rule=rule, trigger=R_NEXT_STEPS, today=today, due=found["due"] or today,
            why=("R13: already named in today's post" if named_today else
                 f"R13: {found['why']}; {position + 1} of {len(picked)} in today's "
                 f"post, {len(queued)} more due and waiting their turn"),
            text=line, company=_text(row, "company"), poc=_text(row, "name"),
            designation=_text(row, "designation"), sheet_row=row.get("_row"),
            row_key=person["key"], contact_key=person["key"],
            extra={
                # A FIXED TIME, read by drip.pinned_time like R8's day-of touch.
                "dayof_time": config.NEXT_STEP_TIME,
                "step": found["code"], "step_label": label, "ask": found["ask"],
                "signature": found["signature"], "missing": list(found["missing"]),
                "pick_order": position,
                # THE OPENER IS A FUNCTION OF THE DAY, never random: the same
                # queue has to give the same post live, in test mode and in a
                # simulation.
                "opener_index": today.toordinal() % len(wording.NEXT_STEP_OPENERS),
                "poc_priority": _text(row, "poc_priority"),
                "email_n": n,
            },
        ))
    for person in queued:
        report["queued"].append(entry_of(person["row"], person["found"]["ask"]))

    log.info(
        "[rules] %s: %d connected, %d due -> %d picked, %d queued; %d waiting, %d "
        "with no date, %d unknown step, %d paused for a meeting, %d completed, %d "
        "closed", rule_id, report["connected"], report["due"], len(picked),
        len(queued), report["waiting"], len(report["no_date"]),
        len(report["unknown_step"]), len(report["paused"]), report["completed"],
        len(report["closed"]),
    )
    return out


def next_step_report_lines(report: Optional[dict], rule_id: str = "R13") -> list:
    """The preview block that says who R13 did NOT name today, and why.

    A rule that names five people out of thirty looks, from the post alone,
    exactly like a rule that only found five. This is where the other
    twenty-five are accounted for — above all the connected rows with no date,
    which nothing else would ever mention.
    """
    if not report:
        return []
    if report.get("state") == "unreadable":
        return [f"**{rule_id} · next steps** — the rotation state could not be "
                "read, so nobody is named today."]
    if report.get("state") == "no_step_column":
        return [f"**{rule_id} · next steps** — the tab has no column mapped to "
                f"{', '.join(report.get('missing_columns') or [])}, so nobody is "
                "named today."]

    def names(entries, limit=12):
        shown = [
            f"{e.get('poc') or e.get('company') or '(unnamed)'} (row {e.get('sheet_row')}"
            + (f", {e['detail']}" if e.get("detail") else "") + ")"
            for e in entries[:limit]
        ]
        more = len(entries) - len(shown)
        return ", ".join(shown) + (f", and {more} more" if more > 0 else "")

    lines = []
    if report.get("posted_today"):
        lines.append(f"**{rule_id} · today's post has gone** — the people above "
                     "are the ones it named.")
    lines += [
        f"**{rule_id} · who is not in today's post** — "
        f"{report.get('connected', 0)} connected, {report.get('due', 0)} due, "
        f"{len(report.get('picked') or [])} picked, "
        f"{report.get('waiting', 0)} not due yet, "
        f"{report.get('completed', 0)} with a completed meeting"
    ]
    for key, label in (
        ("queued", "Due, waiting their turn"),
        ("no_date", "Connected with no LI Connected Date (skipped)"),
        ("unknown_step", "Next Steps value I don't recognise (skipped)"),
        ("paused", "Paused for a booked meeting"),
        ("closed", "Closed after the Unresponsive reminder"),
    ):
        entries = list(report.get(key) or [])
        if entries:
            lines.append(f"  • {label}: {names(entries)}")
    return lines


# TRIGGER NAME -> EVALUATOR. `rules.py` refuses a rules file naming anything not
# in `rules.KNOWN_TRIGGERS`, and the self-test asserts these two lists agree —
# so a rule can never be configured, listed in the preview, and silently
# computing nothing.
EVALUATORS = {
    R_AI_NEWS: _r_ai_news,
    R_NEWS_SCREEN: _r_news_company_screen,
    R_EVENTS: _r_events,
    R_DELIVERABLES: _r_deliverables,
    R_PROSPECTS: _r_prospects,
    R_LI_NO_DM: _r_li_no_dm,
    R_DM_NO_MEETING: _r_dm_no_meeting,
    R_MEETING_PREP: _r_meeting_prep,
    R_MEETING_FOLLOWUP: _r_meeting_followup,
    R_CLOSURE_SUPPORT: _r_closure_support,
    R_NEW_COMPANY: _r_new_pipeline_company,
    R_PACKAGES: _r_sales_packages,
    R_NEXT_STEPS: _r_next_step_followups,
}


def sort_key(item: dict) -> tuple:
    """PRIORITY BAND FIRST, then the oldest due date, then the company.

    Band before date on purpose: a meeting tomorrow outranks a package status
    that went stale three weeks ago, because the meeting is the thing that
    decays. Company last so the list is stable day to day.
    """
    due = item.get("due_date")
    return (
        int(item.get("priority", 9)),
        due or date.max,
        str(item.get("company") or "").lower(),
        str(item.get("poc") or "").lower(),
    )


def run(
    *, today: Optional[date] = None, rows: Optional[list] = None,
    snoozes: Optional[dict] = None, scheduled: Optional[dict] = None,
    deliverables: Optional[list] = None, packages: Optional[list] = None,
    events: Optional[list] = None, pipeline_companies: Optional[list] = None,
    new_companies: Optional[list] = None, prospect_repeats: Optional[dict] = None,
    week_companies: Optional[list] = None, meeting_followups: Optional[dict] = None,
    focus: Optional[dict] = None,
    inactive: int = 0, day_rules: Optional[list] = None,
    prospect_rows: Optional[list] = None, events_unclear_seen=None,
    next_step_state: Optional[dict] = None,
) -> dict:
    """THE QUEUE. Everything the thirteen rules make due today, deduped and ranked.

    Returns:
        {"actions": [...],        ranked, deduped, ready for the drip
         "by_rule": {id: [...]},  every item its rule produced, BEFORE dedup
         "by_type": {trigger: [...]},
         "by_band": {band: [...]},
         "rules_run": [...],      which rules ran today, and why each did or not
         "deduped": [...],        items dropped, with which rule kept the contact
         "silent": {reason: n},   why rows produced nothing
         "stopped": [...], "snoozed": [...],
         "web_pending": int,      items waiting on the research layer
         "reports": {id: {...}},  what a rule has to say about who it left out
                                  (R13: the rotation, the rows with no date)
         "rows": int, "inactive": int, "today": date}

    IT SENDS NOTHING AND WRITES NOTHING. Everything it needs was passed in.

    `next_step_state` is R13's rotation state from `db.next_step_state()`.
    LEFT AT None, R13 NAMES NOBODY: None is what an unreadable table returns,
    and a caller that has no state to give must not be handed a rotation that
    starts from the top. Pass {} to say "nobody has been named yet".
    """
    today = today or dl.today_ist()
    rows = list(rows or [])
    ctx = {
        "today": today, "rows": rows,
        "snoozes": snoozes or {},
        "deliverables": list(deliverables or []),
        "packages": list(packages or []),
        "events": list(events or []),
        "pipeline_companies": list(pipeline_companies or []),
        "new_companies": list(new_companies or []),
        "prospect_repeats": prospect_repeats or {},
        "week_companies": list(week_companies or []),
        "meeting_followups": meeting_followups or {},
        # R5 READS EVERY ROW, not only the active ones (None = use `rows`).
        "prospect_rows": list(prospect_rows) if prospect_rows is not None else None,
        # Events whose unreadable date has already been listed once.
        "events_unclear_seen": set(events_unclear_seen or ()),
        # R13's ROTATION STATE. None (unreadable) is kept as None on purpose.
        "next_step_state": (dict(next_step_state)
                            if next_step_state is not None else None),
        # What an evaluator wants the preview to say about the rows it left out.
        "reports": {},
        # THE LIVE FOCUS, or None. Read by R5 only — a focus is about who to
        # CONTACT NEXT, and applying it to the meeting rules would silence prep
        # for a meeting that is happening tomorrow because the company is off
        # the current theme.
        "focus": focus or None,
    }

    day_rules = day_rules if day_rules is not None else rules_mod.for_day(today)
    all_rules = rules_mod.safe_load()
    running = {r.id for r in day_rules}

    by_rule: dict = {}
    rules_run: list = []
    produced: list = []

    for rule in all_rules:
        if rule.id not in running:
            rules_run.append({
                "id": rule.id, "name": rule.name, "ran": False,
                "why": ("disabled in bot_rules.yaml" if not rule.enabled
                        else f"does not run on {today.strftime('%A')} "
                             f"(runs {rule.weekday_label()})"),
                "items": 0,
            })
            continue
        fn = EVALUATORS.get(rule.trigger)
        if fn is None:
            # rules.py refuses this at load time; belt and braces, and loud.
            log.error("[rules] %s names trigger %r, which has no evaluator",
                      rule.id, rule.trigger)
            continue
        try:
            items = list(fn(rule, ctx) or [])
        except Exception:
            log.exception(
                "[rules] %s (%s) failed; it contributes nothing today and the other "
                "rules are unaffected", rule.id, rule.trigger,
            )
            rules_run.append({"id": rule.id, "name": rule.name, "ran": True,
                              "why": "the evaluator raised — see the log", "items": 0})
            continue
        by_rule[rule.id] = items
        produced.extend(items)
        rules_run.append({
            "id": rule.id, "name": rule.name, "ran": True,
            "why": (f"runs {rule.weekday_label()}" if not rule.anchored
                    else "anchored to the meeting date"),
            "items": len(items),
        })

    # NO REMINDER LANE: `scheduled` is accepted for older callers and ignored.
    # Reminders are sent by `bot._fire_due_reminders` alone, at their minute.

    # ---- DEDUP: ONE CONTACT, ONE MENTION, PER DAY --------------------------
    # Several rules can legitimately select the same person — a prospect who is
    # also three days connected with no DM. The item from the EARLIEST-LISTED
    # rule in bot_rules.yaml wins, because file order is the team's own
    # statement of which rule matters more, and the loser is recorded rather
    # than dropped silently so `cadence preview` can say what happened.
    #
    # Items with no contact (a package, a deliverable, the news) are never
    # deduped against each other: two of those in a day is not the failure this
    # exists to prevent.
    rule_order = {r.id: i for i, r in enumerate(all_rules)}
    ordered = sorted(
        produced,
        key=lambda it: (rule_order.get(it.get("rule_id"), 10_000), sort_key(it)),
    )
    seen: dict = {}
    actions: list = []
    deduped: list = []
    for item in ordered:
        key = item.get("contact_key") or ""
        if key and key in seen:
            keeper = seen[key]
            deduped.append({
                **item,
                "dropped_for": keeper.get("rule_id") or keeper.get("rule"),
                "dropped_why": (
                    f"{item.get('rule_id') or item.get('rule')} also selected this contact, "
                    f"but {keeper.get('rule_id') or keeper.get('rule')} "
                    f"({keeper.get('rule_name')}) is listed first and one contact is "
                    f"named at most once a day"
                ),
            })
            continue
        if key:
            seen[key] = item
        actions.append(item)

    actions.sort(key=sort_key)

    # ---- why the rest of the rows said nothing -----------------------------
    silent: dict = {}
    stopped: list = []
    snoozed: list = []
    for row in rows:
        ok, reason, _until = row_gate(row, today=today, snoozes=ctx["snoozes"])
        if ok:
            continue
        silent[reason] = silent.get(reason, 0) + 1
        if reason == SILENT_STOPPED:
            stopped.append({
                "company": _text(row, "company"), "poc": _text(row, "name"),
                "why": stop_reason(row), "sheet_row": row.get("_row"),
            })
        elif reason == SILENT_SNOOZED:
            entry = (ctx["snoozes"] or {}).get(_contact_key(row)) or {}
            snoozed.append({
                "company": _text(row, "company"), "poc": _text(row, "name"),
                "until": str(entry.get("until_date") or ""),
                "note": str(entry.get("note") or ""),
                "sheet_row": row.get("_row"),
            })
    if deduped:
        silent[SILENT_DEDUPED] = len(deduped)

    by_type: dict = {}
    by_band: dict = {}
    for item in actions:
        by_type.setdefault(item["rule"], []).append(item)
        by_band.setdefault(item["priority"], []).append(item)

    web_pending = sum(1 for a in actions if a.get("web_pending"))

    log.info(
        "[rules] %s: %d rule(s) ran of %d -> %d item(s) (%d deduped, %d awaiting web "
        "research). %d ACTIVE row(s), %d inactive. NOTHING WAS SENT — computation only.",
        dl.iso(today), sum(1 for r in rules_run if r["ran"]), len(all_rules),
        len(actions), len(deduped), web_pending, len(rows), max(0, int(inactive or 0)),
    )
    for entry in rules_run:
        if entry["ran"]:
            log.info("[rules]   %-4s %-32s %d item(s)",
                     entry["id"], entry["name"], entry["items"])

    return {
        "actions": actions,
        "by_rule": by_rule,
        "by_type": by_type,
        "by_band": by_band,
        "rules_run": rules_run,
        "deduped": deduped,
        "silent": silent,
        "stopped": stopped,
        "snoozed": snoozed,
        "web_pending": web_pending,
        "reports": ctx["reports"],
        "rows": len(rows),
        "inactive": max(0, int(inactive or 0)),
        "today": today,
    }


# -- the preview --------------------------------------------------------------


def preview_text(result: dict, *, max_lines: Optional[int] = None) -> str:
    """`cadence preview` — today's due items, GROUPED BY RULE.

    GROUPED BY RULE AND NOT BY OWNER, which is the change that matters. The old
    preview grouped by action type because the engine had one opinion per row;
    this one groups by the rule that produced each item, names the rule, and
    prints the reason line the evaluator wrote. Somebody reading it can check
    the output against `bot_rules.yaml` line by line without opening the code.

    Every section says WHICH RULE and WHY. "R6 Connected, no DM yet — 4 items"
    followed by "connected 12 Sep, 9d ago, more than LI_NO_DM_DAYS (3)" is a
    thing a person can agree or disagree with. "4 follow-ups" is not.

    It also prints the rules that did NOT run and why, because "R4 produced
    nothing" and "R4 does not run on a Thursday" look identical in a list that
    only shows what fired — and only one of them is worth investigating.
    """
    if not result:
        return "_the rules engine is off (NEXT_ACTION_ENABLED=false), so I have not looked._"

    today = result.get("today") or dl.today_ist()
    actions = list(result.get("actions") or [])
    rules_run = list(result.get("rules_run") or [])
    st = rules_mod.status()

    head = (
        f"**Rules preview — {dl.format_date(today)} ({today.strftime('%A')})**\n"
        f"_{len(actions)} item(s) from {sum(1 for r in rules_run if r['ran'])} rule(s) "
        f"of {st.get('count', 0)} in {st.get('path') or 'bot_rules.yaml'}. "
        f"{result.get('rows', 0)} active row(s), {result.get('inactive', 0)} inactive. "
        f"Nothing here has been sent._"
    )
    lines = [head, ""]

    if not st.get("loaded"):
        lines.append(
            f"**NO RULES LOADED** — {st.get('error') or 'the rules file could not be read'}. "
            f"{st.get('remedy') or ''}".rstrip()
        )
        return "\n".join(lines)

    if not actions:
        lines.append("_Nothing is due today._")
    else:
        # One section per rule, in the order the rules file lists them.
        order = {r["id"]: i for i, r in enumerate(rules_run)}
        grouped: dict = {}
        for item in actions:
            grouped.setdefault(item.get("rule_id") or "—", []).append(item)

        for rule_id in sorted(grouped, key=lambda k: order.get(k, 999)):
            items = grouped[rule_id]
            name = items[0].get("rule_name") or items[0].get("label")
            cap = items[0].get("max_items_per_post") or 0
            dest = items[0].get("destination") or "channel"
            counts = items[0].get("counts_toward_cap", True)
            over = max(0, len(items) - cap) if cap else 0
            lines.append(
                f"**{rule_id} · {name}** — {len(items)} item(s), "
                f"{cap}/post to the {dest}"
                + ("" if counts else ", outside the daily cap")
                + (f" · {over} roll to the next run" if over else "")
            )
            for item in items:
                # The placeholder is already inside `text` (see `_item`), so it
                # is not repeated here — only reworded, because a preview is
                # un-researched by design and the research comes at send time.
                overdue = (f" · {item['overdue_days']}d overdue"
                           if item.get("overdue_days") else "")
                owner = f" → {item['owner']}" if item.get("owner") else ""
                lines.append(f"  • {rules_mod.preview_text_of(item.get('text', ''))}"
                             f"{overdue}{owner}")
                lines.append(f"    _why: {item.get('why', '')}_")
            lines.append("")

    # WHO R13 LEFT OUT, AND WHY — printed even when it named nobody, because the
    # rows with no LI Connected Date are never mentioned anywhere else.
    for rule_id, report in sorted((result.get("reports") or {}).items()):
        block = next_step_report_lines(report, rule_id)
        if block:
            lines.extend(block)
            lines.append("")

    deduped = list(result.get("deduped") or [])
    if deduped:
        lines.append(f"**Deduped — {len(deduped)} item(s), one contact named once a day**")
        for item in deduped[:10]:
            lines.append(f"  • {item.get('company')} · {item.get('poc')} — "
                         f"{item.get('dropped_why')}")
        if len(deduped) > 10:
            lines.append(f"  • …and {len(deduped) - 10} more")
        lines.append("")

    quiet = [r for r in rules_run if not r["ran"]]
    if quiet:
        lines.append("**Not running today**")
        for entry in quiet:
            lines.append(f"  • {entry['id']} {entry['name']} — {entry['why']}")
        lines.append("")

    empty = [r for r in rules_run if r["ran"] and not r["items"]]
    if empty:
        lines.append(
            "**Ran, found nothing:** "
            + ", ".join(f"{e['id']} {e['name']}" for e in empty)
        )
        lines.append("")

    pending = int(result.get("web_pending") or 0)
    if pending:
        lines.append(
            f"_{pending} item(s) say **{rules_mod.RESEARCH_AT_SEND}** — their web "
            "research is done for each message just before it goes out, not for the "
            "preview, so the schedule is real and the researched half of each line "
            "arrives with the message itself._"
        )

    silent = result.get("silent") or {}
    if silent:
        lines.append("")
        lines.append(_silent_summary(result))

    text = "\n".join(lines).rstrip()
    if max_lines:
        rows = text.splitlines()
        if len(rows) > max_lines:
            text = "\n".join(rows[:max_lines]) + f"\n… ({len(rows) - max_lines} more lines)"
    return text


def _silent_summary(result: dict) -> str:
    """One line naming why rows produced nothing. "Nothing due" and "I could not
    read a date" are different states and a preview that renders both as absence
    is hiding its own failures."""
    silent = result.get("silent") or {}
    if not silent:
        return ""
    parts = [f"{SILENT_LABELS.get(k, k)}: {v}" for k, v in sorted(silent.items())]
    return "_Quiet rows — " + "; ".join(parts) + "._"


def _self_test() -> int:
    """`python -m nextaction` — the thirteen rules, on rows built here."""
    logging.basicConfig(level=logging.WARNING, format="%(levelname)-7s %(name)s: %(message)s")
    failures = 0

    def check(name, got, want):
        nonlocal failures
        ok = got == want
        failures += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")

    MON, TUE, WED, THU, FRI = (date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23),
                               date(2026, 9, 24), date(2026, 9, 25))

    def row(**kw):
        r = {"_row": kw.pop("_row", 2), "_extra": {}}
        r.setdefault("company", "Acme")
        r.setdefault("name", "Ann")
        r.update(kw)
        return r

    print("evaluator coverage")
    check("every known trigger has an evaluator",
          sorted(EVALUATORS) == sorted(rules_mod.KNOWN_TRIGGERS), True)
    check("every rule in the file resolves",
          all(r.trigger in EVALUATORS for r in rules_mod.safe_load()), True)

    print("\nSTOP semantics survive")
    for cell, field in (("Won", "deal_status"), ("Lost", "deal_status"),
                        ("Dead", "deal_status"), ("Unresponsive", "deal_status"),
                        ("Dead", "prospect_status")):
        r = row(**{field: cell}, li_connected_date="01-09-2026")
        ok, reason, _ = row_gate(r, today=MON, snoozes={})
        check(f"{field}={cell} stops the row", (ok, reason), (False, SILENT_STOPPED))
    check("0% stops the row",
          row_gate(row(closure_prob="0%"), today=MON, snoozes={})[1], SILENT_STOPPED)
    check("a blank closure does NOT stop the row",
          row_gate(row(), today=MON, snoozes={})[0], True)

    print("\nR5 prospects — eligibility, role order, repeats")
    rows5 = [
        row(_row=2, company="Acme", name="Zed", designation="Researcher"),
        row(_row=3, company="Acme", name="Ann", designation="Co-Founder"),
        row(_row=4, company="Acme", name="Cy", designation="CTO"),
        row(_row=5, company="Borealis", name="Bo", designation="Founder"),
        row(_row=6, company="Cinder", name="Di", designation="Founder"),
        row(_row=7, company="Acme", name="Skip", designation="Founder", first_contact="TRUE"),
    ]
    out = run(today=TUE, rows=rows5, day_rules=[rules_mod.by_id("R5")])
    names = [a["poc"] for a in out["actions"]]
    check("first contact TRUE is excluded", "Skip" not in names, True)
    check("role order founders > CXO > research",
          [n for n in names if n in ("Ann", "Cy", "Zed")], ["Ann", "Cy", "Zed"])
    check("two companies a week only",
          sorted({a["company"] for a in out["actions"]}), ["Acme", "Borealis"])
    out = run(today=TUE, rows=rows5, week_companies=["Cinder"],
              day_rules=[rules_mod.by_id("R5")])
    check("a company started this week is finished first",
          "Cinder" in {a["company"] for a in out["actions"]}, True)
    key = activation.row_key(rows5[1])
    out = run(today=TUE, rows=[rows5[1]], prospect_repeats={key: 3},
              day_rules=[rules_mod.by_id("R5")])
    check("three unchanged listings asks to skip",
          out["actions"][0]["ask_to_skip"], True)
    check("...and says so in the text",
          "skip them" in out["actions"][0]["text"], True)

    print("\nR6 / R7 — the day thresholds")
    r6 = row(li_connected_date="15-09-2026")
    check("connected 6d, no DM -> due",
          len(run(today=MON, rows=[r6], day_rules=[rules_mod.by_id("R6")])["actions"]), 1)
    r6b = row(li_connected_date="20-09-2026")
    check("connected 1d -> not due",
          len(run(today=MON, rows=[r6b], day_rules=[rules_mod.by_id("R6")])["actions"]), 0)
    r6c = row(li_connected_date="15-09-2026", li_dm_sent="TRUE")
    check("DM already sent -> not due",
          len(run(today=MON, rows=[r6c], day_rules=[rules_mod.by_id("R6")])["actions"]), 0)
    check("R6's line is about the email, not the DM",
          "no DM logged" in run(today=MON, rows=[r6],
                                day_rules=[rules_mod.by_id("R6")])["actions"][0]["text"],
          False)
    check("R7 is back on Mondays in the shipped file",
          ("R7" in [r.id for r in rules_mod.for_day(MON)],
           "R7" in [r.id for r in rules_mod.for_day(TUE)]), (True, False))
    r7 = row(li_dm_date="10-09-2026", next_steps="waiting on legal")
    got = run(today=MON, rows=[r7], day_rules=[rules_mod.by_id("R7")])["actions"]
    check("DM 11d ago, no meeting -> due", len(got), 1)
    check("...shows days since the DM", "11 day(s) since the DM" in got[0]["text"], True)
    check("...and the last note", "waiting on legal" in got[0]["text"], True)
    # RULE 13 COMES FIRST: its own check decides who R7 leaves alone.
    marker = (config.NEXT_STEP_CONNECTED_MARKERS or ["Connected"])[0]
    r7_r13 = row(li_dm_date="10-09-2026", sid_li_added=marker,
                 li_connected_date="01-09-2026")
    check("a DM'd person Rule 13 covers is Rule 13's, not R7's",
          (covered_by_next_steps(r7_r13),
           len(run(today=MON, rows=[r7_r13],
                   day_rules=[rules_mod.by_id("R7")])["actions"])), (True, 0))
    r7_nodate = row(li_dm_date="10-09-2026", sid_li_added=marker)
    check("Connected with NO date is not covered, so R7 still lists them",
          (covered_by_next_steps(r7_nodate),
           len(run(today=MON, rows=[r7_nodate],
                   day_rules=[rules_mod.by_id("R7")])["actions"])), (False, 1))
    r7_notconn = row(li_dm_date="10-09-2026", li_connected_date="01-09-2026")
    check("a date but not marked Connected is not covered either",
          (covered_by_next_steps(r7_notconn),
           len(run(today=MON, rows=[r7_notconn],
                   day_rules=[rules_mod.by_id("R7")])["actions"])), (False, 1))

    print("\nR8 meeting prep — touches, and the past ones skipped")
    def preps(meeting_iso, today):
        r = row(meeting_date=meeting_iso)
        return run(today=today, rows=[r], day_rules=[rules_mod.by_id("R8")])["actions"]
    check("T-5 fires", [a["touch"] for a in preps("26-09-2026", date(2026, 9, 21))], ["T-5"])
    check("T-3 fires", [a["touch"] for a in preps("24-09-2026", date(2026, 9, 21))], ["T-3"])
    check("T-4 does not", preps("25-09-2026", date(2026, 9, 21)), [])
    check("day-of fires at the configured time",
          [a["dayof_time"] for a in preps("21-09-2026", date(2026, 9, 21))],
          [config.MEETING_DAYOF_TIME])
    check("the T-5 and T-3 touches have the same fixed time",
          ([a["dayof_time"] for a in preps("26-09-2026", date(2026, 9, 21))],
           [a["dayof_time"] for a in preps("24-09-2026", date(2026, 9, 21))]),
          ([config.MEETING_DAYOF_TIME], [config.MEETING_DAYOF_TIME]))
    check("a meeting booked in 3 days never gets a late T-5",
          [a["touch"] for a in preps("24-09-2026", date(2026, 9, 21))], ["T-3"])
    check("a past meeting produces nothing", preps("01-09-2026", date(2026, 9, 21)), [])
    check("a completed meeting produces no prep",
          len(run(today=MON, rows=[row(meeting_date="24-09-2026", meeting_status="Completed")],
                  day_rules=[rules_mod.by_id("R8")])["actions"]), 0)

    print("\nR9 meeting follow-up — the ladder, and its end")
    done = row(meeting_date="15-09-2026", meeting_status="Completed")
    k = activation.row_key(done)
    ladder = config.MEETING_FOLLOWUP_LADDER
    for rung in range(len(ladder)):
        got = run(today=MON, rows=[done], meeting_followups={k: {"sent": rung}},
                  day_rules=[rules_mod.by_id("R9")])["actions"]
        check(f"rung {rung} -> {ladder[rung]}",
              [a["rung_destination"] for a in got], [ladder[rung]])
    got = run(today=MON, rows=[done], meeting_followups={k: {"sent": len(ladder)}},
              day_rules=[rules_mod.by_id("R9")])["actions"]
    check("off the end of the ladder -> STOP", got, [])
    check("next steps written -> no follow-up",
          len(run(today=MON, rows=[row(meeting_date="15-09-2026",
                                       meeting_status="Completed",
                                       next_steps="sending the deck")],
                  day_rules=[rules_mod.by_id("R9")])["actions"]), 0)

    check("the Next Steps DROPDOWN is not a note: R9 still follows up",
          len(run(today=MON, rows=[row(meeting_date="15-09-2026",
                                       meeting_status="Completed",
                                       outreach_step="Send email 1")],
                  day_rules=[rules_mod.by_id("R9")])["actions"]), 1)

    print("\nR13 next steps — reading the dropdown")
    for raw, want in (("", STEP_BLANK), ("Research the PoC", STEP_RESEARCH),
                      ("Send Email 1", "email1"), ("send e-mail 3", "email3"),
                      ("Reach by LI DM", STEP_DM), ("Call PoC", STEP_CALL),
                      ("Call the PoC", STEP_CALL), ("ping them", STEP_UNKNOWN),
                      ("Send email 4", STEP_UNKNOWN)):
        check(f"step_code({raw!r})", step_code(raw), want)

    print("\nR13 next steps — the timing table (the defaults)")
    saved13 = {k: getattr(config, k) for k in (
        "NEXT_STEP_FIRST_DAYS", "NEXT_STEP_AFTER_PREVIOUS_DAYS",
        "NEXT_STEP_AFTER_EMAIL_DAYS", "NEXT_STEP_CALL_AFTER_DM_DAYS",
        "NEXT_STEP_CALL_EVERY_DAYS", "NEXT_STEP_CALL_UNTIL_DAYS",
        "NEXT_STEP_DM_REPLIED_MARKERS", "NEXT_STEP_PAUSE_FOR_BOOKED_MEETING",
        "NEXT_STEP_CONNECTED_MARKERS", "NEXT_STEP_TIME")}
    config.NEXT_STEP_FIRST_DAYS = config.NEXT_STEP_AFTER_PREVIOUS_DAYS = 2
    config.NEXT_STEP_AFTER_EMAIL_DAYS = config.NEXT_STEP_CALL_AFTER_DM_DAYS = 7
    config.NEXT_STEP_CALL_EVERY_DAYS, config.NEXT_STEP_CALL_UNTIL_DAYS = 3, 21
    config.NEXT_STEP_DM_REPLIED_MARKERS = ["replied", "responded"]
    config.NEXT_STEP_PAUSE_FOR_BOOKED_MEETING = True
    config.NEXT_STEP_CONNECTED_MARKERS = ["connected"]
    config.NEXT_STEP_TIME = "15:00"
    r13 = rules_mod.by_id("R13")

    def conn(**kw):
        base = dict(sid_li_added="Connected", li_connected_date="15-09-2026",
                    **{role: "" for role in NEXT_STEP_REQUIRED_ROLES})
        base.update(kw)
        return row(**base)

    def step(r, today=MON, entry=None):
        f = next_step_for(r, today=today, entry=entry)
        return (f["status"], f["ask"], f["due"], f["missing"])

    check("blank: due 2 days after connecting",
          step(conn()), ("due", "ask_next", date(2026, 9, 17), []))
    check("blank: not yet", step(conn(li_connected_date="20-09-2026"))[0], "waiting")
    check("research", step(conn(outreach_step="Research the PoC"))[:2],
          ("due", "researched"))
    check("email 1 not sent: connected + 2",
          step(conn(outreach_step="Send email 1")),
          ("due", "email_out", date(2026, 9, 17), []))
    check("email 1 sent and dated: +7, not yet",
          step(conn(outreach_step="Send email 1", email_1_sent="yes",
                    email_1_date="16.09.2026"))[:3],
          ("waiting", "advance", date(2026, 9, 23)))
    check("email 1 sent and dated: due at +7",
          step(conn(outreach_step="Send email 1", email_1_sent="Yes",
                    email_1_date="14.09.2026"))[:2], ("due", "advance"))
    check("email 1 sent, no date: due now",
          step(conn(outreach_step="Send email 1", email_1_sent="yes")),
          ("due", "log_date", MON, []))
    check("email 2, email 1 cells blank: due now, and both are asked for",
          step(conn(outreach_step="Send email 2")),
          ("due", "email_out", MON, ["1st Email Sent", "1st Email Date"]))
    check("email 2 counts from the 1st email's date",
          step(conn(outreach_step="Send email 2", email_1_sent="yes",
                    email_1_date="20-09-2026")),
          ("waiting", "email_out", date(2026, 9, 22), []))
    check("DM step, no DM date: counts from the 3rd email",
          step(conn(outreach_step="Reach by LI DM", email_3_sent="yes",
                    email_3_date="18-09-2026")),
          ("due", "dm_out", date(2026, 9, 20), []))
    check("Call the PoC with no DM date",
          step(conn(outreach_step="Call the PoC"))[:2], ("due", "call_no_dm_date"))
    check("a replied DM is not chased with calls",
          step(conn(outreach_step="Reach by LI DM", li_dm_sent="Replied",
                    li_dm_date="01-09-2026"))[:2], ("due", "dm_replied"))
    dm_row = conn(outreach_step="Reach by LI DM", li_dm_sent="Sent",
                  li_dm_date="14-09-2026")                 # a Monday
    check("DM 6 days ago: waiting",
          step(dm_row, today=date(2026, 9, 20))[:2], ("waiting", "call"))
    check("DM 7 days ago: the first call reminder",
          step(dm_row, today=MON)[:3], ("due", "call", MON))
    sig13 = step_signature(dm_row)
    called = {"signature": sig13, "calls": 1, "last_call_date": "2026-09-21"}
    check("...not again the next day",
          step(dm_row, today=TUE, entry=called)[0], "waiting")
    check("...again 3 days later",
          step(dm_row, today=THU, entry=called)[:3], ("due", "call", THU))
    check("changing Q to Call the PoC keeps the signature",
          step_signature(conn(outreach_step="Call the PoC", li_dm_sent="Sent",
                              li_dm_date="14-09-2026")), sig13)
    check("day 21 is still a call",
          step(dm_row, today=date(2026, 10, 5), entry={
              "signature": sig13, "calls": 4, "last_call_date": "2026-10-01"})[:2],
          ("due", "call"))
    check("day 22 asks for Unresponsive",
          step(dm_row, today=date(2026, 10, 6))[:2], ("due", "unresponsive"))
    check("an unknown Next Steps value is not guessed at",
          step(conn(outreach_step="ping them"))[0], "unknown_step")

    print("\nR13 next steps — who is in, the pick and the rotation")

    def r13_run(rows_, today=MON, state=None, **kw):
        return run(today=today, rows=rows_, day_rules=[r13],
                   next_step_state={} if state is None else state, **kw)

    people = [conn(_row=10 + i, company=f"Co{i}", name=f"P{i}",
                   outreach_step="Send email 1") for i in range(8)]
    out13 = r13_run(people)
    check("five per post", len(out13["actions"]), 5)
    picked = sorted(out13["actions"], key=lambda a: a["pick_order"])
    check("top of the sheet first", [a["poc"] for a in picked],
          ["P0", "P1", "P2", "P3", "P4"])
    check("the rest are queued, and counted",
          (out13["reports"]["R13"]["due"], len(out13["reports"]["R13"]["queued"])),
          (8, 3))
    check("every item carries the fixed time",
          {a["dayof_time"] for a in picked}, {"15:00"})
    check("...and is outside the cap", {a["counts_toward_cap"] for a in picked}, {False})
    check("the line names the person, the company and the cell",
          picked[0]["text"],
          "P0 (Co0): Next Steps says Send email 1. Has it gone out? If so, mark "
          "1st Email Sent and the date.")
    state13 = {a["contact_key"]: {"signature": a["signature"],
                                  "last_date": "2026-09-21"} for a in picked}
    day2 = sorted(r13_run(people, today=TUE, state=state13)["actions"],
                  key=lambda a: a["pick_order"])
    check("the next day: the never-named first, then it wraps to the top",
          [a["poc"] for a in day2], ["P5", "P6", "P7", "P0", "P1"])
    people[1]["outreach_step"] = "Send email 2"
    day2b = sorted(r13_run(people, today=TUE, state=state13)["actions"],
                   key=lambda a: a["pick_order"])
    check("a changed step makes the person new again",
          [a["poc"] for a in day2b][:2], ["P1", "P5"])
    people[1]["outreach_step"] = "Send email 1"
    caller = conn(_row=40, company="Zeta", name="Zed", outreach_step="Call the PoC",
                  li_dm_date="14-09-2026")
    check("a call reminder that is due goes first",
          sorted(r13_run(people + [caller])["actions"],
                 key=lambda a: a["pick_order"])[0]["poc"], "Zed")
    check("no state (unreadable) -> nobody is named",
          (len(run(today=MON, rows=people, day_rules=[r13])["actions"]),
           run(today=MON, rows=people, day_rules=[r13])["reports"]["R13"]["state"]),
          (0, "unreadable"))
    check("nobody due -> no items",
          r13_run([conn(li_connected_date="20-09-2026")])["actions"], [])
    after_post = r13_run(people, state=state13)
    check("after today's post, the same five and nobody new",
          sorted(a["poc"] for a in after_post["actions"]),
          ["P0", "P1", "P2", "P3", "P4"])
    check("...and the preview says the post has gone",
          "today's post has gone" in preview_text(after_post), True)
    nodate = conn(_row=50, name="Nod", li_connected_date="")
    rep13 = r13_run([nodate, row(_row=51, name="Stranger", sid_li_added="Requested",
                                 li_connected_date="15-09-2026")])["reports"]["R13"]
    check("Connected with no date: skipped and listed",
          ([e["poc"] for e in rep13["no_date"]], rep13["connected"]), (["Nod"], 1))
    check("Prospect Status Unresponsive stops it",
          r13_run([conn(prospect_status="Unresponsive")])["actions"], [])
    check("a meeting booked ahead pauses it",
          len(r13_run([conn(meeting_date="25-09-2026")])["reports"]["R13"]["paused"]), 1)
    check("a past meeting, not completed, does not",
          len(r13_run([conn(meeting_date="10-09-2026")])["actions"]), 1)
    check("a completed meeting ends it",
          r13_run([conn(meeting_date="10-09-2026", meeting_status="Completed")])["actions"],
          [])
    closed_key = activation.row_key(dm_row)
    check("once the Unresponsive reminder has gone, never again",
          r13_run([dm_row], today=date(2026, 10, 7),
                  state={closed_key: {"signature": "anything", "closed": True,
                                      "closed_date": "2026-10-06"}})["actions"], [])
    old_layout = {k: v for k, v in conn().items()
                  if k not in NEXT_STEP_REQUIRED_ROLES}
    no_col = r13_run([old_layout])
    check("no Next Steps column on the tab: nobody is told a cell is blank",
          (no_col["actions"], no_col["reports"]["R13"]["state"]),
          ([], "no_step_column"))
    check("the preview says who was left out",
          "who is not in today's post" in preview_text(r13_run(people + [nodate])), True)
    check("...and names the row with no date",
          "Nod (row 50" in preview_text(r13_run(people + [nodate])), True)
    for k13, v13 in saved13.items():
        setattr(config, k13, v13)

    print("\nR10 closure support — exactly 50 is excluded")
    other = row(_row=9, company="Other", name="Olu", prospect_status="Lead",
                closure_prob="10%")
    for pct, want in (("51%", 1), ("50%", 0), ("49%", 0), ("0.6", 1), ("", 0)):
        r = row(prospect_status="Demo", closure_prob=pct)
        check(f"closure {pct!r}",
              len(run(today=MON, rows=[r, other],
                      day_rules=[rules_mod.by_id("R10")])["actions"]),
              want)
    check("wrong stage -> nothing",
          len(run(today=MON, rows=[row(prospect_status="Lead", closure_prob="80%")],
                  day_rules=[rules_mod.by_id("R10")])["actions"]), 0)
    blank = [row(_row=2, company="A", name="a"), row(_row=3, company="B", name="b")]
    got = run(today=MON, rows=blank, day_rules=[rules_mod.by_id("R10")])["actions"]
    check("both columns blank on every active row -> the notice, once",
          [a.get("notice") for a in got], [CLOSURE_EMPTY_NOTICE])
    got = run(today=MON, rows=[row(_row=2, prospect_status="Demo"),
                               row(_row=3, company="B", name="b", prospect_status="Lead")],
              day_rules=[rules_mod.by_id("R10")])["actions"]
    check("Closure Prob% alone blank on every row -> the notice too",
          [a.get("empty_columns") for a in got], [["Closure Prob%"]])
    check("...and it asks for no web research", [a["web_pending"] for a in got], [False])
    check("no active rows at all -> silent",
          run(today=MON, rows=[], day_rules=[rules_mod.by_id("R10")])["actions"], [])

    print("\nR7 — the role rank travels with the item")
    got = run(today=MON, rows=[row(li_dm_date="10-09-2026", designation="Co-Founder")],
              day_rules=[rules_mod.by_id("R7")])["actions"]
    check("a founder ranks first", got[0]["role_rank"], _role_rank("Founder"))

    print("\nR5 — every never-contacted row, not only the active ones")
    never = row(_row=20, company="Zeta", name="Zed", designation="Founder")
    got = run(today=TUE, rows=[], prospect_rows=[never],
              day_rules=[rules_mod.by_id("R5")])["actions"]
    check("a row that is not active is still a prospect", [a["poc"] for a in got], ["Zed"])
    check("...and it asks for an email lookup, since none is on file",
          (got[0]["email_lookup"], got[0]["email_on_file"]), (True, ""))
    got = run(today=TUE, rows=[], day_rules=[rules_mod.by_id("R5")],
              prospect_rows=[row(_row=21, company="Zeta", name="Dee", deal_status="Dead"),
                             row(_row=22, company="Zeta", name="Em", email="em@zeta.ai")],
              )["actions"]
    check("a stop rule still blocks; an email on file needs no lookup",
          [(a["poc"], a["email_lookup"]) for a in got], [("Em", False)])
    check("the signature changes when the row does",
          prospect_signature(never) == prospect_signature(dict(never, email="z@zeta.ai")),
          False)

    print("\nR3 events — every Wednesday, a 14-day window")
    def ev(n, name, when, deadline="", registered="", link=""):
        return {"_row": n, "_extra": {}, "event": name, "event_date": when,
                "registration_deadline": deadline, "registered": registered,
                "link": link}

    def d(days):
        return (WED + timedelta(days=days)).strftime("%d-%m-%Y")

    tab = [
        ev(2, "Registered Summit", d(10), registered="Yes"),
        ev(3, "Next Tuesday Forum", d(6), deadline=d(4)),
        ev(4, "Past Expo", d(-3)),
        ev(5, "Closed Conf", d(12), deadline=d(-1)),
        ev(6, "No Deadline Meetup", d(9)),
        ev(7, "Far Away Con", d(40), deadline=d(30)),
        ev(8, "Far But Closing", d(40), deadline=d(5)),
        ev(9, "Registered Far", d(30), registered="Yes"),
        ev(10, "Mystery Day", "sometime in spring"),
    ]
    r3 = rules_mod.by_id("R3")
    got = run(today=WED, events=tab, day_rules=[r3])["actions"]
    lines = {a["company"]: a for a in got if a.get("event_kind") != EVENT_CARRIER}
    check("the right rows have a line", sorted(lines),
          ["Far But Closing", "Mystery Day", "Next Tuesday Forum", "No Deadline Meetup",
           "Registered Summit"])
    check("registered, within the window",
          lines["Registered Summit"]["event_line"].startswith(
              "You're registered for Registered Summit on "), True)
    check("not registered, deadline ahead",
          (lines["Next Tuesday Forum"]["event_kind"],
           lines["Next Tuesday Forum"]["event_line"].startswith(
               "Register for Next Tuesday Forum by "),
           "(event on " in lines["Next Tuesday Forum"]["event_line"]),
          (EVENT_REGISTER, True, True))
    check("a far event whose DEADLINE is inside the window is in",
          lines["Far But Closing"]["event_kind"], EVENT_REGISTER)
    check("no deadline on the sheet",
          lines["No Deadline Meetup"]["event_line"].endswith(
              "— no registration deadline on the sheet"), True)
    check("an unreadable date is listed, as unclear",
          "date unclear" in lines["Mystery Day"]["event_line"], True)
    got2 = run(today=WED, events=tab, day_rules=[r3],
               events_unclear_seen={unclear_event_key("Mystery Day")})["actions"]
    check("...and only once", "Mystery Day" in [a["company"] for a in got2], False)
    check("the carrier is always there, and is the only item that wants research",
          [(a["event_kind"], a["web_pending"]) for a in got if a["web_pending"]],
          [(EVENT_CARRIER, True)])
    check("an empty tab still has its carrier",
          [a["event_kind"] for a in run(today=WED, events=[], day_rules=[r3])["actions"]],
          [EVENT_CARRIER])
    check("it runs on the Wednesday after too (no alternate weeks)",
          bool(run(today=WED + timedelta(days=7), events=tab, day_rules=None)
               ["by_rule"].get("R3")), True)
    check("nothing listed falls before Monday -> the offer is Monday, even with "
          "a deadline this week",
          {a["remind_word"] for a in lines.values()}, {"on Monday"})
    soon = run(today=WED, events=tab + [ev(11, "Friday Meetup", d(2))],
               day_rules=[r3])["actions"]
    check("an event before Monday -> the offer is tomorrow",
          {a["remind_word"] for a in soon if a["event_kind"] != EVENT_CARRIER},
          {"tomorrow"})
    later = run(today=WED, events=[ev(2, "Registered Summit", d(10), registered="Yes")],
                day_rules=[r3])["actions"]
    check("nothing before Monday -> the offer is Monday",
          [(a["remind_word"], a["remind_on"]) for a in later
           if a["event_kind"] != EVENT_CARRIER],
          [("on Monday", dl.iso(WED + timedelta(days=5)))])

    print("\nR11 new company — one working day after it appeared")
    nc = [{"company": "Nova", "first_seen": "2026-09-18"}]     # a Friday
    check("Friday + 1 working day = Monday",
          len(run(today=MON, rows=[], new_companies=nc,
                  day_rules=[rules_mod.by_id("R11")])["actions"]), 1)
    check("not yet due on the Friday itself",
          len(run(today=date(2026, 9, 18), rows=[], new_companies=nc,
                  day_rules=[rules_mod.by_id("R11")])["actions"]), 0)
    old = [{"company": "Stale", "first_seen": "2026-08-01"}]
    check("outside the window, nothing",
          len(run(today=MON, rows=[], new_companies=old,
                  day_rules=[rules_mod.by_id("R11")])["actions"]), 0)

    print("\nR12 packages — blank Ready? is No")
    pk = [{"_row": 2, "_extra": {}, "name": "Hinglish STT", "ready": "", "status": "In progress"},
          {"_row": 3, "_extra": {}, "name": "Vibe check", "ready": "Yes", "status": "Done"},
          {"_row": 4, "_extra": {}, "name": "Image A/B", "ready": "No", "status": "Not started"}]
    got = run(today=THU, rows=[], packages=pk, day_rules=[rules_mod.by_id("R12")])["actions"]
    check("blank and No are selected, Yes is not",
          sorted(a["company"] for a in got), ["Hinglish STT", "Image A/B"])

    print("\nR4 deliverables — P1 only, open, due by Sunday or already past")
    # MON is Mon 21 Sep 2026, so the week ends Sun 27 Sep.
    dl_rows = [
        {"_row": 2, "_extra": {}, "action_item": "MSA", "priority": "P1",
         "status": "", "deadline": "22-Sep", "dependency": "Sid"},
        {"_row": 3, "_extra": {}, "action_item": "NDA", "priority": "P1",
         "status": "Done", "deadline": "22-Sep", "dependency": ""},
        {"_row": 4, "_extra": {}, "action_item": "Website", "priority": "P2",
         "status": "", "deadline": "27-Sep", "dependency": "", "remarks": "copy"},
        {"_row": 5, "_extra": {}, "action_item": "Dashboard", "priority": "P1",
         "status": "", "deadline": "31-Dec", "dependency": ""},
        {"_row": 6, "_extra": {}, "action_item": "Pitch deck", "priority": "P3",
         "status": "In progress", "deadline": "10-Sep", "dependency": "Sales"},
        {"_row": 7, "_extra": {}, "action_item": "Next week", "priority": "P1",
         "status": "", "deadline": "28-Sep", "dependency": ""},
        {"_row": 8, "_extra": {}, "action_item": "Overview doc", "priority": "P1",
         "status": "In progress", "deadline": "10-Sep", "dependency": "",
         "remarks": "copy", "link": "https://docs.google.com/document/d/x"},
    ]
    got = run(today=MON, rows=[], deliverables=dl_rows,
              day_rules=[rules_mod.by_id("R4")])["actions"]
    check("P1 only: open, due by Sunday or past — the P2 and the P3 are not there",
          sorted(a["deliverable"] for a in got), ["MSA", "Overview doc"])
    check("a P2 due THIS WEEK is skipped, whatever its deadline",
          "Website" in [a["deliverable"] for a in got], False)
    check("an overdue P3 is skipped too",
          "Pitch deck" in [a["deliverable"] for a in got], False)
    check("a deadline 11 days ago is read as overdue, not as next year",
          next(a["days_left"] for a in got if a["deliverable"] == "Overview doc"), -11)
    check("the team comes from Functional Dependency",
          next(a["team"] for a in got if a["deliverable"] == "MSA"), "Sid")
    check("blank team -> the default owner",
          next(a["team"] for a in got if a["deliverable"] == "Overview doc"),
          config.DELIVERABLE_DEFAULT_OWNER)
    check("extra carries the priority and the pretty date",
          next((a["sheet_priority"], a["deadline_pretty"], a["is_p1"])
               for a in got if a["deliverable"] == "Overview doc"),
          ("P1", "Thu 10 Sep", True))
    check("...the link travels with it; the remarks still do not",
          sorted({k for a in got for k in ("remarks", "link") if k in a}), ["link"])
    sun = MON + timedelta(days=6)
    got = run(today=sun, rows=[], deliverables=dl_rows,
              day_rules=[rules_mod.by_id("R4")])["actions"]
    check("a Sunday run (if R4 ran on Sundays) keeps P1 + DELIVERABLE_NEAR_DAYS",
          sorted(a["deliverable"] for a in got), ["MSA", "Next week", "Overview doc"])

    print("\nDEDUP — one contact, one mention, per day")
    dup = row(li_connected_date="15-09-2026")      # R5 eligible AND R6 due
    out = run(today=TUE, rows=[dup],
              day_rules=[rules_mod.by_id("R5"), rules_mod.by_id("R6")])
    check("the contact appears once", len(out["actions"]), 1)
    check("the earlier rule in the file keeps it", out["actions"][0]["rule_id"], "R5")
    check("the loser is recorded, not dropped silently", len(out["deduped"]), 1)
    check("...naming which rule kept it", out["deduped"][0]["dropped_for"], "R5")
    check("non-contact items are never deduped against each other",
          len(run(today=THU, rows=[], packages=pk,
                  day_rules=[rules_mod.by_id("R12")])["actions"]), 2)

    print("\nweb-pending placeholder")
    out = run(today=MON, rows=[], day_rules=[rules_mod.by_id("R1")])
    check("R1 still produces an item", len(out["actions"]), 1)
    check("...carrying the placeholder",
          rules_mod.WEB_PENDING in out["actions"][0]["text"], True)
    check("...and counted", out["web_pending"], 1)

    print("\nthe preview groups by rule")
    out = run(today=TUE, rows=rows5, day_rules=rules_mod.for_day(TUE))
    text = preview_text(out)
    check("names the rule id", "R5" in text, True)
    check("names the rule", "Prospects to contact" in text, True)
    check("prints a why line", "_why:" in text, True)
    check("lists rules that did not run", "Not running today" in text, True)

    print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
