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
     R3  events                 Wed                 3 lines: registered soon, registration
                                                    closing, then suggestions
     R4  deliverables           Mon                 P1 only, due this week or passed
     R5  prospects              Tue, Thu            first contact FALSE or blank
     R6  li_no_dm               Tue, Fri            connected > 3d, no DM: the email check
     R7  dm_no_meeting          Mon                 DM > 21d and no meeting, or one
                                                    booked > 7d after the DM; 3 a
                                                    post, rotating, behind R13
     R8  meeting_prep           anchored            every Meeting Date: 5 and 3 days
                                                    before (after 14:00) and the day
                                                    itself (10:00); no cap, stored news
     R9  meeting_followup       anchored, 10:00     done + 3d, no next steps, laddered
    R10  closure_support        Mon                 a closure stage AND closure > 50;
                                                    highest first, no cap, stored news
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
    R_DM_NO_MEETING: "No meeting booked yet",
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
# Rule 13 names the people we are connected with. UNTIL 13 Oct 2026 Rule 7 read
# `covered_by_next_steps` and listed nobody Rule 13 covered; since 12 Oct that
# was every connected row, so Rule 7 named nobody at all. Rule 7 no longer
# reads it: it is the escalation BEHIND Rule 13, and the only thing between
# them is the one-mention-a-day dedup (`run`). The functions stay because
# Rule 13 and the questions about who is connected read them.


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
    """Is this person Rule 13's? EVERY row marked Connected, dated or not.

    IT FOLLOWS RULE 13's OWN TEST, EXACTLY. Until 12 Oct 2026 Rule 13 skipped a
    connected row with no LI Connected Date, so this required the date too and
    Rule 7 could still list such a person. Rule 13 now takes every connected
    row; if this still asked for the date, the no-date rows would be reminded
    about by Rule 13 AND listed by Rule 7.

    RULE 7 DOES NOT READ THIS ANY MORE (13 Oct 2026): see `_r_dm_no_meeting`.
    """
    return marked_connected(row, markers)


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
    """R2 — companies in the news that are on NEITHER sheet. WEB-DEPENDENT.

    ONE CARRIER ITEM, every Tuesday and Friday. The screen itself happens at
    research time (`bot._screen_companies`): it needs a fresh read of both
    sheets, the feed store and two model calls, and this engine reads no sheet
    and keeps no state. When nothing survives the screen the carrier has no
    research and nothing is posted (`drip.nothing_to_say`).

    THE CARRIER HAS NO CONTACT, so the one-mention-a-day dedup never sees it:
    R2 cannot win or lose a contact against another rule.

    PERMISSION IS ASKED before anything is added to the sheet — this rule
    proposes, it never writes.
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
EVENT_REGISTERED = "registered"       # block A
EVENT_REGISTER = "register"           # block B
EVENT_CARRIER = "carrier"
EVENT_BLOCK_A, EVENT_BLOCK_B = "A", "B"
# (EVENT_NO_DEADLINE and EVENT_UNCLEAR are gone, 14 Oct 2026, with the lines
# they stood for. A row with no usable registration deadline, and a row whose
# date cannot be read, are now skipped and logged.)


def remind_again_on(today: date, soonest: Optional[date]) -> tuple:
    """(date, word) for R3's "shall I remind you again?" offer.

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


def event_deadline_unknown(value) -> bool:
    """Does the "Last day for registration" cell say nothing usable? Blank,
    "not available", "TBD", or anything that does not read as a date: all the
    same thing. THE TWO EXISTING DEFINITIONS, AND NO THIRD
    (`gtm_sheet.is_unknown_value`, `events_discovery.deadline_is_unknown`),
    plus the date parser's own verdict for everything else."""
    import events_discovery

    if gtm_sheet.is_unknown_value(value) or events_discovery.deadline_is_unknown(value):
        return True
    return not gtm_sheet.parse_event_date(value)["known"]


def event_urgency(event: dict) -> tuple:
    """R3's RANKING when more than the post's three lines qualify, across both
    sheet blocks together:

      1. REGISTRATION CLOSING SOONEST (block B, by its close date);
      2. then THE EVENT STARTING SOONEST (block A, by its start date);
      3. ties in sheet order.

    A registration closing on Friday beats an event three weeks out: one is a
    door closing, the other is a date in a calendar. THE RANKING DECIDES WHO IS
    IN, NOT WHERE THEY SIT: the blocks still print A, B, C.

    NOTHING ROTATES. Next Wednesday the same ranking runs against a tab that
    has moved on, and an event leaves by happening or by its deadline passing.
    A rotation would hide the most urgent thing because it was urgent last
    week too.
    """
    if event["block"] == EVENT_BLOCK_B:
        return (0, dl.iso(event["deadline"]), int(event.get("sheet_row") or 0))
    return (1, dl.iso(event["start"]), int(event.get("sheet_row") or 0))


def _r_events(rule, ctx) -> list:
    """R3 — the events worth a line THIS week. Every Wednesday. THREE LINES IN
    THE WHOLE POST, in up to three labelled blocks (rebuilt 14 Oct 2026):

      A  we are registered, and it starts within EVENTS_WINDOW_DAYS (14)
      B  we are not registered and the registration deadline is still ahead
      C  events not on the sheet at all, worth a look

    THIS FUNCTION DOES A AND B, from the AI Events & Summits tab (Event,
    Location, Date, Last day for registration, Registered, Link; "Attended?"
    is not read). C is the research layer's: what it finds rides on the
    carrier item below, and fills only the room the sheet left.

    PER ROW, with W = EVENTS_WINDOW_DAYS:

      the END date has passed           never mentioned
      started, not finished             kept; the line says "started Tuesday"
      registered, starts within W       block A
      registered, further out           not mentioned: W days is enough notice
      not registered, deadline ahead    block B when the deadline OR the start
                                        is within W (a deadline next week for
                                        an event in December still matters)
      not registered, deadline passed   skipped, logged "registration closed"
      not registered, NO USABLE         SKIPPED, logged, nothing posted. Blank,
      deadline                          "not available", "TBD" and anything
                                        unreadable are one thing
                                        (`event_deadline_unknown`)
      date unreadable                   skipped and logged. No line at all
      date with no year                 the next occurrence, and the line says
                                        the year was assumed (`year_assumed`)

    THE NO-DEADLINE LINE IS GONE AND IT IS MOST OF THE TAB. Until 14 Oct such a
    row got "X on <date> — no registration deadline on the sheet". On the day
    this shipped six of the seven rows were like that, so blocks A and B are
    usually EMPTY and the post is suggestions. THAT IS THE RULE WORKING, NOT A
    FAULT; do not reinstate the line. What revives block B is the DEADLINE
    BACKFILL (`events_discovery.rows_needing_deadline`, run by the caller,
    unchanged): every deadline it finds and an approver accepts turns a
    skipped row into a block B line.

    THE CAP IS THE RULE'S `max_items_per_post` (3), FOR THE WHOLE POST. The
    sheet gets first call: A and B take as many of the places as qualify, in
    `event_urgency`, and the carrier says how much room is left (`event_room`)
    for suggestions. There is no "+N more" line and no rotation.

    AN EMPTY OR UNREADABLE TAB POSTS NOTHING, not even the carrier, and says so
    at ERROR: "no rows" is not the same as "nothing coming up".

    PURE: rows in, items out. The search, the proposals and the reminder are
    the caller's.
    """
    today = ctx["today"]
    window = max(1, int(config.EVENTS_WINDOW_DAYS))
    horizon = today + timedelta(days=window)
    cap = max(1, int(getattr(rule, "max_items_per_post", 3) or 3))
    rows = list(ctx.get("events") or ())
    report = {"rows": len(rows), "block_a": 0, "block_b": 0, "skipped": [], "left_out": []}
    ctx.setdefault("reports", {})[getattr(rule, "id", "") or "R3"] = report
    if not rows:
        log.error("[R3] the AI Events & Summits tab came back with NO ROWS (empty or "
                  "unreadable), so nothing is posted. That is not the same as nothing "
                  "coming up.")
        return []

    def skip(row, name, why):
        report["skipped"].append({"sheet_row": row.get("_row"), "event": name, "why": why})
        log.info("[R3] skipped %r (row %s): %s", name, row.get("_row") or "?", why)

    found: list = []
    for row in rows:
        name = _text(row, "event")
        if not name:
            continue
        when = gtm_sheet.parse_event_date(row.get("event_date"))
        if not when["known"]:
            raw = _text(row, "event_date")
            skip(row, name, f"the date cannot be read ({raw!r})" if raw else "no date on the sheet")
            continue
        start = when["start"]
        end = when["end"] or start
        if end < today:
            skip(row, name, f"it ended on {dl.iso(end)}")
            continue
        registered = gtm_sheet.parse_flag(row.get("registered")) is True
        within = start <= horizon
        event = {"row": row, "name": name, "location": _text(row, "location"),
                 "start": start, "end": end, "deadline": None, "sheet_row": row.get("_row"),
                 "started": start < today,
                 "year_assumed": start.year if when.get("year_assumed") else 0,
                 "date_text": _text(row, "event_date")}
        if registered:
            if not within:
                skip(row, name, f"registered, but it starts on {dl.iso(start)}, more than "
                                f"{window} days out")
                continue
            event["block"] = EVENT_BLOCK_A
        else:
            cell = row.get("registration_deadline")
            if event_deadline_unknown(cell):
                skip(row, name, "not registered and no usable registration deadline "
                                f"({_text(row, 'registration_deadline')!r})")
                continue
            deadline = gtm_sheet.parse_event_date(cell)["start"]
            if deadline < today:
                skip(row, name, f"registration closed on {dl.iso(deadline)}")
                continue
            if not (within or deadline <= horizon):
                skip(row, name, f"registration closes on {dl.iso(deadline)} and it starts on "
                                f"{dl.iso(start)}, both more than {window} days out")
                continue
            event["block"], event["deadline"] = EVENT_BLOCK_B, deadline
        found.append(event)

    ranked = sorted(found, key=event_urgency)
    picked, left = ranked[:cap], ranked[cap:]
    for event in left:
        report["left_out"].append({"sheet_row": event["sheet_row"], "event": event["name"]})
        log.info("[R3] %r (row %s) qualifies but the post's %d line(s) went to more urgent "
                 "events", event["name"], event["sheet_row"] or "?", cap)
    report["block_a"] = sum(1 for e in picked if e["block"] == EVENT_BLOCK_A)
    report["block_b"] = sum(1 for e in picked if e["block"] == EVENT_BLOCK_B)
    room = cap - len(picked)
    log.info("[R3] %d row(s) read: %d registered and starting within %dd, %d with "
             "registration closing; %d skipped, %d left out by the cap of %d; room for %d "
             "suggestion(s)", len(rows), report["block_a"], window, report["block_b"],
             len(report["skipped"]), len(left), cap, room)

    lines: list = []
    for order, event in enumerate(picked):
        row = event["row"]
        fields = {"name": event["name"], "location": event["location"],
                  "start": dl.iso(event["start"]), "end": dl.iso(event["end"]),
                  "deadline": dl.iso(event["deadline"]) if event["deadline"] else "",
                  "started": event["start"].strftime("%A") if event["started"] else "",
                  "year_assumed": event["year_assumed"], "date_text": event["date_text"]}
        line = wording.event_sheet_line(fields)
        kind = EVENT_REGISTERED if event["block"] == EVENT_BLOCK_A else EVENT_REGISTER
        lines.append(_item(
            rule=rule, trigger=R_EVENTS, today=today,
            due=event["deadline"] or max(event["start"], today),
            why=(f"R3 (Wednesdays): block {event['block']} ({kind}), inside the "
                 f"{window}-day window; {order + 1} of {len(picked)} by urgency"),
            text=line, company=event["name"], sheet_row=row.get("_row"),
            extra={"event_kind": kind, "event_block": event["block"], "event_line": line,
                   "event_fields": fields, "pick_order": order,
                   "event_date": fields["start"], "event_deadline": fields["deadline"],
                   "event_link": _text(row, "link")},
        ))
    # THE CARRIER: nothing to read, somewhere for the research to land. It
    # says how many of the post's lines the sheet left for suggestions.
    carrier = _item(
        rule=rule, trigger=R_EVENTS, today=today, due=today,
        why="R3 (Wednesdays): looks for events the tab lacks and missing deadlines",
        text="", web_pending=True,
        extra={"event_kind": EVENT_CARRIER, "event_line": "", "event_room": room,
               "event_today": dl.iso(today), "event_window": window},
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

    THE LIST IS WALKED TOP TO BOTTOM AND THE FIRST PHRASE THE TITLE CARRIES
    WINS, so the order of the list is the design: "chief scientist" sits above
    "chief", "senior lecturer" above "lecturer".

    A PHRASE MATCHES AS WHOLE WORDS (11 Oct; it was a bare substring). With a
    substring, "Director" carried "cto" (dire-CTO-r) and ranked as a CTO,
    "Coordinator" carried "coo", "Leadership" carried "lead" and anything with
    "sr" in it was senior. Short entries such as cto, coo, vp, sr and lead are
    only safe as words. Punctuation is ignored ("Co-Founder/CTO" reads as the
    words co, founder, cto) and a plural "s" on the last word still matches
    ("Founders").

    Anything the list does not recognise sorts LAST, in sheet order — an
    unrecognised title is not evidence of seniority in either direction, so it
    waits rather than jumping the queue or being dropped.
    """
    want = gtm_sheet.normalise_header(designation or "").replace("?", " ")
    want = " ".join(want.split())
    if not want:
        return 10_000
    hay = f" {want} "
    for i, phrase in enumerate(config.PROSPECT_ROLE_ORDER or ()):
        key = " ".join(gtm_sheet.normalise_header(str(phrase)).replace("?", " ").split())
        if key and (f" {key} " in hay or f" {key}s " in hay):
            return i
    return 10_000


def _r_prospects(rule, ctx) -> list:
    """R5 — people with no first contact recorded, ONE COMPANY AT A TIME.

    Everything that ranks a company or a person is in `prospects.py`, because
    the post, the replies to it and the ad-hoc answers must give one order.
    What this does:

      1. Reads EVERY row the stop rules allow (`ctx["prospect_rows"]`), not
         only the "active" ones. The activation gate lets a row through once
         somebody has started on it, which is exactly what a never-contacted
         row has not had.
      2. Builds the company queue (`prospects.build_queue`: six stacking
         rules, then fewer-people-left, then sheet order) and asks which
         company is open (`prospects.resolve`, from `ctx["prospect_state"]`).
      3. Emits EVERYBODY left at the open company, in `prospects.order_people`
         order, each carrying `pick_order`. NOT just five: one contact is
         named once a day and an earlier rule keeps them (R13 sits before R5),
         so the post takes the first `max_items_per_post` that SURVIVE the
         dedup. That is how the post still carries five after R13 has claimed
         one of them.

    A COMPANY IS NEVER DROPPED. It leaves the open slot only when nobody there
    is left to contact or a person said to move on (`prospects.resolve`). This
    function has no branch that retires one.

    THE SAME FIVE EVERY TIME. Nothing counts how often somebody was named and
    nothing rotates: the five are the top five until one of them has a first
    contact recorded, at which point they are simply no longer a row this
    selects. (There is no way to drop a person who will never be contacted;
    that is an open gap, not something to invent here.)

    THE MONTH'S LAST POST carries two more lines when no First Contact Date
    was recorded at the open company this calendar month
    (`prospects.stalled_lines`). They close nothing.
    """
    import prospects

    today = ctx["today"]
    source = ctx.get("prospect_rows")
    rows = list((source if source is not None else ctx.get("rows")) or ())
    queue = prospects.build_queue(rows, today=today, snoozes=ctx.get("snoozes") or {})
    prospects.log_unmatched(queue, where=f"R5 on {today.isoformat()}")
    if not queue:
        return []

    # A FOCUS, WHEN ONE IS LIVE, PUTS THE COMPANIES IT MATCHES FIRST. It does
    # not take the slot from a company that is already open: only a person
    # saying "move on" does that.
    focus_note = ""
    active_focus = ctx.get("focus")
    if active_focus:
        flat = [r for e in queue for r in e["left"]]
        _ordered, focus_note = focus_mod.apply_to(flat, active_focus)
        value = str(active_focus.get("value") or "")
        hit = {e["key"] for e in queue if any(focus_mod.matches(r, value) for r in e["left"])}
        queue = [e for e in queue if e["key"] in hit] + [e for e in queue if e["key"] not in hit]

    view = prospects.resolve(queue, ctx.get("prospect_state"))
    current, nxt = view["open"], view["next"]
    if current is None:
        return []
    foot = (prospects.stalled_lines(view)
            if prospects.is_last_slot_of_month(today, rule.weekdays) else [])
    log.info("[rules] R5: %s is open (score %d: %s; %d left, %d reached); next is %s; "
             "%d paused", current["company"], current["score"],
             "+".join(current["flags"]) or "no rule", len(current["left"]),
             current["reached"], nxt["company"] if nxt else "nobody", len(view["paused"]))

    names = {e["key"]: e["company"] for e in [current] + ([nxt] if nxt else []) + view["paused"]}
    shared = {
        "reached": current["reached"], "left_total": len(current["left"]),
        "next_company": nxt["company"] if nxt else "",
        "foot": list(foot),
        # WHAT TO REMEMBER ONCE THE POST HAS LANDED (`bot._after_send`).
        "state": {**view["state"], "next": nxt["key"] if nxt else "", "names": names},
    }
    out = []
    for order, row in enumerate(current["left"]):
        key = _contact_key(row)
        designation = _text(row, "designation")
        rank = _role_rank(designation)
        out.append(_item(
            rule=rule, trigger=R_PROSPECTS, today=today, due=today,
            why=(f"R5: {current['company']} is the open company "
                 f"({'+'.join(current['flags']) or 'no priority rule'}); person "
                 f"{order + 1} of {len(current['left'])}"
                 + (f", role rank {rank}" if rank < 10_000 else ", title not in the role order")
                 + (f"; {focus_note}" if focus_note else "")),
            text=f"{_describe(row)} — no first contact recorded",
            company=current["company"], poc=_text(row, "name"),
            designation=designation, sheet_row=row.get("_row"),
            row_key=key, contact_key=key,
            extra={"pick_order": order, "role_rank": rank,
                   "signals": prospects.signals(row),
                   # THE EMAIL: on file, or to be looked up at send time for
                   # the contacts that make the post. The lookup still runs
                   # (R6 shares its cache); R5's message does not print it.
                   "email_on_file": _text(row, "email"),
                   "email_lookup": not _text(row, "email"),
                   "prospect": shared,
                   "focus_note": focus_note,
                   "focus": (active_focus or {}).get("value", "")},
        ))
    return out


# How many people R6 hands on, in order, for the sender to check emails for
# and choose its two lists from. More than the ten a post can name, because
# some will be kept by an earlier rule for the day and the email blocks may
# need to look past the first ten to find anybody with email news.
LI_NO_DM_SCAN = 30


def li_dm_state(row: dict) -> str:
    """What "LI DM Sent" says: "blank", "replied", "sent" or "other".

    THE CELL, NOT THE DATE (12 Oct 2026). The rule used to decide a DM had gone
    out by finding an LI DM Date. LI DM Sent is the column that answers it.
    The replied markers are checked FIRST; both lists take a person out, and
    they are separate so a later release can treat a reply differently. A
    ticked checkbox is "sent". Anything else that is not blank is "other", and
    is out too: the rule is for people whose cell is BLANK.
    """
    value = row.get("li_dm_sent")
    said = _norm(value)
    if not said:
        return "blank"

    def marks(names):
        return {gtm_sheet.normalise_header(str(m)) for m in (names or []) if str(m).strip()}
    if said in marks(config.LI_NO_DM_DM_REPLIED_MARKERS):
        return "replied"
    if said in marks(config.LI_NO_DM_DM_SENT_MARKERS) or gtm_sheet.parse_flag(value) is True:
        return "sent"
    return "other"


def li_no_dm_order(person: dict) -> tuple:
    """R6's ORDER, in one place: people WITH an LI Connected Date first,
    oldest connection first; then people with none, in sheet order. NOTHING
    ELSE REORDERS THEM: no rotation, no "whose turn is it"."""
    connected = person.get("connected")
    row_no = int(person.get("sheet_row") or 0)
    if connected is None:
        return (1, "", row_no)
    return (0, dl.iso(connected), row_no)


def _r_li_no_dm(rule, ctx) -> list:
    """R6 — connected on LinkedIn, no DM yet; and do we hold a working email.

    THREE COLUMNS AND NOTHING ELSE: LI Connected Date, LI DM Sent, Email id.
    It does not read "Sid - LI Addition".

    WHO IS IN:
      - LI DM Sent is BLANK (`li_dm_state`). Sent, Replied (or Received) take a
        person out; so does anything else written there.
      - an LI Connected Date LI_NO_DM_DAYS or more days old, OR NO LI CONNECTED
        DATE AT ALL: with no date there is nothing to count from, so the
        person is in straight away.
      - the row passes the stop and snooze gates, like every rule.

    IT READS THE WHOLE TAB (`ctx["prospect_rows"]`), NOT ONLY THE ACTIVE ROWS,
    for the reason R13 gives: a row is "active" once it carries a first-contact
    date or an LI Connected Date, and most of the people this rule is for carry
    neither. Fed only active rows it would see about a fifth of them.

    THE ORDER IS `li_no_dm_order` AND THERE IS NO ROTATION AND NO DROP. The
    people at the top are named on every post until their LI DM Sent cell is
    filled in. No drop clock, no mention counter, no "first named" date: this
    rule keeps NO state. The list moves when the sheet moves.

    WHAT IS HANDED ON: the first LI_NO_DM_SCAN people in that order, each with
    its place (`pick_order`) and its Email cell as written. The EMAIL WORK IS
    NOT DONE HERE: the search and the DNS lookup are network calls and belong
    to the sender (`bot._li_no_dm_email_work`), which also decides which of the
    post's two lists each person lands in.

    NOBODY QUALIFIES: one item carrying the all-clear notice, so a post still
    goes out in R6's slot (`drip.notice_of`, the way R10 says its columns are
    empty). AN EMPTY TAB is not "nobody qualifies": nothing is produced and it
    is logged at ERROR.
    """
    today = ctx["today"]
    days = max(0, int(config.LI_NO_DM_DAYS))
    rule_id = getattr(rule, "id", "") or "R6"
    source = ctx.get("prospect_rows")
    rows = list((source if source is not None else ctx.get("rows")) or ())
    report = {"rows": len(rows), "qualify": 0, "dated": 0, "undated": 0, "sent": 0,
              "replied": 0, "other": 0, "not_yet": 0, "handed_on": 0}
    ctx.setdefault("reports", {})[rule_id] = report
    if not rows:
        log.error("[rules] %s: the Outreach PoCs tab came back with NO ROWS, so nothing is "
                  "posted. An empty tab is not the same as nobody waiting on a DM.", rule_id)
        return []

    people = []
    for row in rows:
        if not (_text(row, "name") or _text(row, "company")):
            continue
        ok, _reason, until = row_gate(row, today=today, snoozes=ctx.get("snoozes") or {})
        if not ok:
            continue
        said = li_dm_state(row)
        if said != "blank":
            report[said] += 1
            continue
        connected = _date(row, "li_connected_date")
        if connected is not None and _due(connected, days) > today:
            report["not_yet"] += 1
            continue
        report["dated" if connected is not None else "undated"] += 1
        people.append({"row": row, "connected": connected, "sheet_row": row.get("_row"),
                       "until": until})
    report["qualify"] = len(people)
    people.sort(key=li_no_dm_order)
    handed = people[:max(1, int(LI_NO_DM_SCAN))]
    report["handed_on"] = len(handed)
    log.info("[rules] %s: %d row(s) read (the whole tab); %d qualify (%d with a connected date "
             "%d+ days old, %d with no connected date); out: %d DM sent, %d replied, %d other "
             "value, %d connected too recently; the first %d in order are handed on",
             rule_id, len(rows), len(people), report["dated"], days, report["undated"],
             report["sent"], report["replied"], report["other"], report["not_yet"], len(handed))
    if not people:
        return [_item(
            rule=rule, trigger=R_LI_NO_DM, today=today, due=today,
            why="R6: nobody has a blank LI DM Sent and a connection old enough",
            text=wording.LI_NO_DM_ALL_CLEAR,
            extra={"notice": wording.LI_NO_DM_ALL_CLEAR},
        )]

    out = []
    for order, person in enumerate(handed):
        row, connected = person["row"], person["connected"]
        elapsed = (today - connected).days if connected is not None else None
        who = wording.next_step_who(_text(row, "name"), _text(row, "company"))
        out.append(_item(
            rule=rule, trigger=R_LI_NO_DM, today=today,
            due=person["until"] or today,
            why=(f"R6 (Tue/Fri): LI DM Sent is blank; "
                 + (f"connected {dl.format_date(connected)}, {elapsed}d ago, "
                    f"LI_NO_DM_DAYS is {days}" if connected is not None
                    else "no LI Connected Date, so in straight away")
                 + f"; {order + 1} of {len(handed)} handed on, {len(people)} qualify"),
            text=f"{who}: connected, no DM yet",
            company=_text(row, "company"), poc=_text(row, "name"),
            designation=_text(row, "designation"), sheet_row=row.get("_row"),
            row_key=_contact_key(row), contact_key=_contact_key(row),
            extra={"pick_order": order, "who": who,
                   # R6 GIVES WAY to any other rule naming the same person
                   # today, wherever that rule sits in the file (see the dedup
                   # in `run`). It must never cost somebody their meeting prep.
                   "yields": True,
                   # THE EMAIL CELL AS WRITTEN. Read by the sender; NEVER
                   # printed: an address already in the sheet is not shown back.
                   "email_on_file": _text(row, "email"),
                   "connected_days": elapsed},
        ))
    return out


# -- R7: no meeting booked yet ------------------------------------------------
# THE ESCALATION BEHIND R13 (rebuilt 13 Oct 2026). R13 does the chasing: from a
# week after the DM its call tier says "We need to call these PoCs" every
# weekday. R7 says, on Mondays, that the chasing is not working.

DM_BLOCK_NONE = "none"          # no Meeting Date at all
DM_BLOCK_BOOKED = "booked"      # a Meeting Date, a long way after the DM


def _days_between(start: date, end: date) -> int:
    """Calendar days from `start` to `end`. R7's ONLY date arithmetic, in one
    place so a check can prove a row with no LI DM Date is never measured."""
    return (end - start).days


def _marks(names) -> set:
    return {gtm_sheet.normalise_header(str(m)) for m in (names or []) if str(m).strip()}


def dm_no_meeting_state(row: dict, *, today: date) -> dict:
    """Is ONE row R7's, and in which block? PURE.

    Returns {"status", "block", "why", "dm", "meeting"}; `status` is "in",
    "not_yet" or one of the reasons it is out ("replied", "not_sent",
    "no_date", "completed", "status", "on_hold").

    FOUR COLUMNS, IN THIS ORDER, and a row that fails one is not looked at
    again:

      1. LI DM SENT MUST SAY SENT. The cell, not the date: until 13 Oct this
         rule decided a DM had gone out by finding an LI DM Date. The REPLIED
         markers are checked FIRST (a reply is R13's meeting line, never this
         rule's); blank is no DM at all. R7's own marker lists.
      2. LI DM DATE MUST HOLD A DATE. NO DATE, NO LOOKUP: nothing is measured
         for the row. R13 still reminds about that person.
      3. MEETING STATUS MUST BE OPEN: blank or TBD (DM_NO_MEETING_STATUS_OPEN).
         Completed is out for good, whatever that list says.
      4. THE GAP, one of two ways:
           no Meeting Date   LI DM Date -> today         DM_NO_MEETING_DAYS
           a Meeting Date    LI DM Date -> Meeting Date  DM_NO_MEETING_BOOKED_GAP_DAYS
         In from the day the gap REACHES the number: day 20 is not yet, day 21
         is in. Booking a meeting is not an exit: with the status still open
         the person moves to the second block until it says Completed.

    DEAL STATUS ON HOLD IS OUT, FOR THIS RULE ONLY. Won and Lost stop every
    rule through CLOSURE_STOP_MARKERS (`row_gate`, which the caller runs
    first). DEAL_ON_HOLD_MARKERS is deliberately NOT in that list: adding it
    would change all thirteen rules, and a parked deal is still R13's to remind
    about. So it is checked here and nowhere else.
    """
    out = {"status": "", "block": "", "why": "", "dm": None, "meeting": None}

    def say(status, why, **more):
        out.update(status=status, why=why, **more)
        return out

    cell = row.get("li_dm_sent")
    said = _norm(cell)
    if said and said in _marks(config.DM_NO_MEETING_DM_REPLIED_MARKERS):
        return say("replied", "LI DM Sent says they replied: R13's meeting line")
    if not (said in _marks(config.DM_NO_MEETING_DM_SENT_MARKERS)
            or gtm_sheet.parse_flag(cell) is True):
        return say("not_sent", "LI DM Sent is blank" if not said
                   else f"LI DM Sent says {_text(row, 'li_dm_sent')!r}, which is not a sent marker")
    dm = _date(row, "li_dm_date")
    if dm is None:
        return say("no_date", "no LI DM Date, so nothing is measured")
    status = _text(row, "meeting_status")
    if gtm_sheet.is_meeting_completed(status):
        return say("completed", "the meeting is Completed", dm=dm)
    opens = {gtm_sheet.normalise_header(str(m)) for m in (config.DM_NO_MEETING_STATUS_OPEN or [])}
    if _norm(status) not in opens:
        return say("status", f"Meeting Status says {status!r}, which is not open", dm=dm)
    if is_on_hold(row):
        return say("on_hold", f"Deal Status says {_text(row, 'deal_status')!r} (on hold)", dm=dm)
    meeting = _date(row, "meeting_date")
    if meeting is None:
        need = max(0, int(config.DM_NO_MEETING_DAYS))
        gap = _days_between(dm, today)
        if gap < need:
            return say("not_yet", f"DM {gap}d ago, DM_NO_MEETING_DAYS is {need}", dm=dm)
        return say("in", f"DM sent {dl.format_date(dm)}, {gap}d ago, no Meeting Date "
                         f"(DM_NO_MEETING_DAYS is {need})", block=DM_BLOCK_NONE, dm=dm)
    need = max(0, int(config.DM_NO_MEETING_BOOKED_GAP_DAYS))
    gap = _days_between(dm, meeting)
    if gap < need:
        return say("not_yet", f"meeting {dl.format_date(meeting)}, {gap}d after the DM: "
                              f"booked promptly (DM_NO_MEETING_BOOKED_GAP_DAYS is {need})",
                   dm=dm, meeting=meeting)
    return say("in", f"DM sent {dl.format_date(dm)}, meeting {dl.format_date(meeting)}, "
                     f"{gap}d later, status still open (DM_NO_MEETING_BOOKED_GAP_DAYS is "
                     f"{need})", block=DM_BLOCK_BOOKED, dm=dm, meeting=meeting)


def dm_no_meeting_order(person: dict) -> tuple:
    """R7's ORDER, in one place.

      1. LEAST RECENTLY NAMED FIRST; never named ("") goes ahead of everybody.
         This is the rotation: nothing drops any more, so without it the same
         three names would be posted every Monday for ever.
      2. then LONGEST SINCE THE DM;
      3. then sheet order.

    NOT PROSPECT_ROLE_ORDER (it was the tie-break until 13 Oct): that is R5's
    seniority order and has no bearing on whether a meeting is overdue.
    """
    return (str(person.get("last_date") or ""), dl.iso(person["dm"]),
            int(person.get("sheet_row") or 0))


def _r_dm_no_meeting(rule, ctx) -> list:
    """R7 — DM'd three weeks ago and no meeting, or a meeting booked a long way
    after the DM. Mondays. See `dm_no_meeting_state` for who is in.

    IT READS THE WHOLE TAB (`ctx["prospect_rows"]`), for the reason R6 and R13
    give: a DM'd row need not have passed the activation gate.

    IT HANDS ON EXACTLY THE PEOPLE THE POST NAMES: the first
    DM_NO_MEETING_MAX_CONTACTS in `dm_no_meeting_order`, shared across both
    blocks. Fewer qualify, fewer are named; there is no padding and no "+N
    more" line. NOBODY IS DROPPED ON A TIMER: a person stays until the sheet
    moves.

    A PERSON AN EARLIER-LISTED RULE NAMES TODAY IS SKIPPED BEFORE THE THREE ARE
    PICKED (`ctx["taken"]`, in practice R13's five). The dedup in `run` would
    remove them anyway (R13 is listed first and wins the contact; R7 is NOT
    exempt), but picking first and deduping after would post two names on a
    day five qualify.

    `ctx["dm_no_meeting_state"]` is {contact_key: {"last_date"}} from the
    caller. NONE MEANS UNREADABLE AND NAMES NOBODY, like R13: an empty dict
    would start the rotation again from the top. PURE: the state is only
    read; the sender records who was named, after a real send.

    ONCE TODAY'S POST HAS GONE, TODAY'S PEOPLE STAY TODAY'S PEOPLE. The queue
    is recomputed on every sweep tick; without this the three just recorded
    would go to the back and three others would be handed on.
    """
    today = ctx["today"]
    rule_id = getattr(rule, "id", "") or "R7"
    source = ctx.get("prospect_rows")
    rows = list((source if source is not None else ctx.get("rows")) or ())
    report = {"rows": len(rows), "qualify": 0, "none": 0, "booked": 0, "taken": 0,
              "out": {}, "picked": [], "queued": [], "state": "ok"}
    ctx.setdefault("reports", {})[rule_id] = report
    if not rows:
        log.error("[rules] %s: the Outreach PoCs tab came back with NO ROWS, so nothing is "
                  "posted. An empty tab is not the same as nobody waiting on a meeting.",
                  rule_id)
        return []
    state = ctx.get("dm_no_meeting_state")
    if state is None:
        report["state"] = "unreadable"
        log.error("[rules] %s: the rotation state was not supplied or could not be read, "
                  "so nobody is named. Guessing would start the rotation from the top.",
                  rule_id)
        return []

    taken = ctx.get("taken") or set()
    snoozes = ctx.get("snoozes") or {}
    today_iso = dl.iso(today)
    people: list = []
    named_today: list = []
    for row in rows:
        ok, _reason, until = row_gate(row, today=today, snoozes=snoozes)
        if not ok:
            continue
        found = dm_no_meeting_state(row, today=today)
        if found["status"] != "in":
            report["out"][found["status"]] = report["out"].get(found["status"], 0) + 1
            continue
        key = _contact_key(row)
        last = str((state.get(key) or {}).get("last_date") or "")
        person = {"row": row, "key": key, "found": found, "dm": found["dm"],
                  "sheet_row": row.get("_row"), "last_date": last, "until": until}
        report[found["block"]] += 1
        if last == today_iso:
            named_today.append(person)
        elif key in taken:
            report["taken"] += 1
        else:
            people.append(person)
    report["qualify"] = len(people) + len(named_today) + report["taken"]

    cap = max(1, int(config.DM_NO_MEETING_MAX_CONTACTS))
    ranked = sorted(people, key=dm_no_meeting_order)
    picked, queued = ranked[:cap], ranked[cap:]
    if named_today:
        picked, queued = sorted(named_today, key=dm_no_meeting_order), ranked
        report["posted_today"] = True
    log.info("[rules] %s: %d row(s) read (the whole tab); %d qualify (%d with no Meeting "
             "Date, %d with a meeting booked late); %d named by an earlier rule today and "
             "skipped; %d picked, %d waiting their turn (nobody is dropped); out: %s",
             rule_id, len(rows), report["qualify"], report["none"], report["booked"],
             report["taken"], len(picked), len(queued),
             ", ".join(f"{n} {why}" for why, n in sorted(report["out"].items())) or "nobody")

    out = []
    for order, person in enumerate(picked):
        row, found = person["row"], person["found"]
        who = wording.next_step_who(_text(row, "name"), _text(row, "company"))
        entry = {"sheet_row": row.get("_row"), "poc": _text(row, "name"),
                 "company": _text(row, "company"), "block": found["block"],
                 "last_named": person["last_date"]}
        report["picked"].append(entry)
        out.append(_item(
            rule=rule, trigger=R_DM_NO_MEETING, today=today, due=person["until"] or today,
            why=("R7: already named in today's post" if named_today else
                 f"R7 (Mondays): {found['why']}; "
                 + (f"last named {person['last_date']}" if person["last_date"]
                    else "never named")
                 + f"; {order + 1} of {len(picked)} in the post, {len(queued)} waiting "
                   f"their turn"),
            text=f"{who}: " + ("a meeting is booked a long way after the DM"
                               if found["block"] == DM_BLOCK_BOOKED
                               else "DM'd, no meeting booked"),
            company=_text(row, "company"), poc=_text(row, "name"),
            designation=_text(row, "designation"), sheet_row=row.get("_row"),
            row_key=person["key"], contact_key=person["key"],
            extra={"pick_order": order, "who": who, "block": found["block"],
                   # THE MEETING DATE, for the second block's line only: that
                   # date is the point of the line. Nothing else is shown.
                   "meeting_iso": dl.iso(found["meeting"]) if found["meeting"] else ""},
        ))
    for person in queued:
        report["queued"].append({
            "sheet_row": person["row"].get("_row"), "poc": _text(person["row"], "name"),
            "company": _text(person["row"], "company"), "block": person["found"]["block"],
            "last_named": person["last_date"]})
    return out


def _r_meeting_prep(rule, ctx) -> list:
    """R8 — every meeting with a touch due today: 5 and 3 days before
    (MEETING_PREP_DAYS_BEFORE) and the day itself. Rebuilt 12 Oct 2026.

    ONE COLUMN IS READ: Meeting Date, on EVERY row of Outreach PoCs that has
    one (`ctx["meeting_rows"]`, the whole tab). NO ACTIVATION GATE AND NO
    MEETING STATUS: a row nobody has contacted yet qualifies, and a meeting
    marked Completed or cancelled with a date still to come is still chased.
    Both were accepted on 12 Oct; do not add a status check back in.

    TWO POSTS, AND THE ITEM SAYS WHICH (`post`): today's meetings at
    MEETING_DAYOF_TIME, always on their own, and everything else after
    MEETING_PREP_TIME. Each is outside the day's order and the 2-hour gap.

    NO CAP. Every meeting is handed on and every one is named; the rule's
    `max_items_per_post` is not applied (`drip.shown_contacts`).

    NEVER DROPPED BY THE DEDUP (`never_dropped`). "One contact, one mention a
    day" used to cost a meeting its prep whenever a rule listed earlier named
    the same person that day. A meeting is named whatever else is said, and
    one person's two meetings are two lines. In the other direction nothing
    changed: R8 still claims its contact, so a rule listed AFTER it, and R6
    (which gives way to everybody), do not name that person the same day.

    WHO IS PICKED, and the memory that stops a touch repeating, catches up a
    date that moved close, and asks for an unreadable date weekly, are
    `meeting_prep.pick`. `ctx["meeting_state"]` is `db.meeting_prep_state()`;
    the caller passes {"unreadable": True} when that table cannot be read and
    then NOTHING is posted: without the memory every touch due today would go
    out again on the next tick.

    NO FETCH. Until 12 Oct each meeting asked the research layer for a web
    search. The news line now comes from what R1 has already stored
    (`bot._meeting_news`, which reads; this function is handed nothing and
    asks for nothing).
    """
    import meeting_prep

    today = ctx["today"]
    report = ctx["reports"].setdefault("R8", {
        "today": 0, "prep": 0, "blank_names": [], "duplicates": [], "past": 0,
        "outside": 0, "quiet": 0, "done": 0, "asked": 0})
    state = ctx.get("meeting_state")
    if isinstance(state, dict) and state.get("unreadable"):
        log.error("[R8] the record of which touches have gone out cannot be read; nothing "
                  "is posted, so no touch can go out twice")
        report["state_unreadable"] = True
        return []
    rows = ctx.get("meeting_rows")
    rows = list(rows if rows is not None else (ctx.get("rows") or ()))
    if rows and not any("meeting_date" in r for r in rows):
        log.error("[R8] %d row(s) read and no Meeting Date column I can find by its header; "
                  "nothing is posted", len(rows))
        report["missing_column"] = True
        return []
    got = meeting_prep.pick(rows, today=today,
                            befores=_int_list(config.MEETING_PREP_DAYS_BEFORE), state=state)
    for skipped in got["blank_names"]:
        log.warning("[R8] row %s has a Meeting Date (%r) and no Name; skipped",
                    skipped.get("sheet_row"), skipped.get("meeting_date"))
    for dup in got["duplicates"]:
        log.info("[R8] %s (%s) is on the sheet twice for %s; row %s is not listed again",
                 dup["name"], dup["company"] or "no company", dup["meeting_date"],
                 dup.get("sheet_row"))
    for name in ("blank_names", "duplicates", "past", "outside", "quiet", "done", "asked"):
        report[name] = got[name]
    report["dates"] = got["dates"]

    waves = {post: meeting_prep.sent_today(state, today, post)
             for post in (meeting_prep.POST_TODAY, meeting_prep.POST_PREP)}
    out = []
    for item in got["items"]:
        row, meeting, post = item["row"], item["meeting"], item["post"]
        who = wording.next_step_who(item["name"], item["company"])
        iso = dl.iso(meeting) if meeting else ""
        if item["touch"] == meeting_prep.TOUCH_UNREADABLE:
            why = f"R8: the Meeting Date {item['raw']!r} cannot be read; asked for once a week"
            text = f"{who} — the Meeting Date cannot be read"
        elif item["touch"] == meeting_prep.TOUCH_CATCHUP:
            why = (f"R8: meeting on {dl.format_date(meeting)} moved to {item['days_out']} "
                   "day(s) away, past every earlier touch; the one catch-up touch")
            text = f"{who} — meeting on {dl.format_date(meeting)}"
        else:
            why = (f"R8: meeting on {dl.format_date(meeting)}, "
                   + ("the day itself" if item["days_out"] == 0
                      else f"{item['days_out']} days before"))
            text = f"{who} — meeting on {dl.format_date(meeting)}"
        report[post] += 1
        out.append(_item(
            rule=rule, trigger=R_MEETING_PREP, today=today,
            # NOT weekend-shifted: the touch belongs to its own day, and a
            # Saturday meeting still gets its morning note on the Saturday.
            due=today, why=why, text=text,
            company=item["company"], poc=item["name"], sheet_row=row.get("_row"),
            row_key=item["row_key"], contact_key=item["row_key"],
            # NONE OF THIS IS PRINTED except `who` and, on the afternoon post,
            # the date: the rest is what the sender, the order and the touch
            # record need.
            extra={"meeting_date": iso, "meeting_raw": item["raw"], "touch": item["touch"],
                   "days_out": item["days_out"], "block": item["block"], "post": post,
                   "wave": waves[post], "who": who, "pick_order": item["pick_order"],
                   "never_dropped": True,
                   "key": f"{getattr(rule, 'id', 'R8')}:{item['row_key']}:{iso}:{item['touch']}",
                   # NO CAP: 0 means "none", so no overflow is ever reported.
                   "max_items_per_post": 0,
                   "dayof_time": (config.MEETING_DAYOF_TIME if post == meeting_prep.POST_TODAY
                                  else config.MEETING_PREP_TIME)},
        ))
    log.info("[R8] %d row(s) read: %d for today's post, %d for the afternoon one; %d not on "
             "a touch today, %d touch(es) already out, %d past, %d beyond %d days, %d "
             "unreadable date(s) asked inside the last week, %d blank name(s), %d duplicate(s)",
             len(rows), report["today"], report["prep"], got["quiet"], got["done"], got["past"],
             got["outside"], meeting_prep.WINDOW_DAYS, got["asked"], len(got["blank_names"]),
             len(got["duplicates"]))
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


# -- R10: closure support ------------------------------------------------------
# What R10 posts when it cannot run because the columns it reads are empty
# (both of them, here; `wording.closure_empty_notice` names whichever is).
CLOSURE_EMPTY_NOTICE = wording.closure_empty_notice(["Prospect Status", "Closure Prob%"])

# A word in front of a stage that turns it into its opposite: "No deal" is not
# a deal, "Not a demo yet" is not a demo.
_STAGE_NEGATIONS = frozenset({"no", "not", "non", "never", "without"})


def closure_stage(value) -> str:
    """The CLOSURE_SUPPORT_STAGES phrase this Prospect Status names, or "".

    WHOLE WORDS, AND NOT NEGATED (13 Oct 2026). A stage matches when its words
    appear in the cell as whole words, in order ("Quote sent", "In
    negotiation"), never as part of a longer word ("demoted" is not "demo").
    A whole-word match is still not enough on its own: "No deal" carries the
    word "deal". So a cell with a negating word in front of the stage ("no",
    "not", "non", "never", "without") names no stage at all.
    """
    words = gtm_sheet.normalise_header(gtm_sheet.clean_cell(value)).replace("?", " ").split()
    if not words:
        return ""
    for phrase in config.CLOSURE_SUPPORT_STAGES or ():
        key = gtm_sheet.normalise_header(str(phrase)).replace("?", " ").split()
        if not key:
            continue
        for i in range(len(words) - len(key) + 1):
            if words[i:i + len(key)] == key:
                if _STAGE_NEGATIONS & set(words[:i]):
                    break
                return phrase
    return ""


def closure_order(deal: dict) -> tuple:
    """R10's ORDER, in one place: HIGHEST CLOSURE PERCENTAGE FIRST; a tie goes
    to sheet order.

    NO ROTATION, DELIBERATELY. R6, R7 and R13 rotate because they work a long
    backlog. Closure support is a short list of the deals closest to landing,
    and they stay in front of the team every week until they close: rotating a
    90% deal out because it appeared last Monday would hide the thing the post
    exists for. DO NOT "HARMONISE" THIS WITH THE ROTATING RULES. With no cap,
    a rotation would have nothing to do anyway.
    """
    return (-int(deal.get("closure_pct") or 0), int(deal.get("sheet_row") or 0))


def closure_news_company(sheet_ref) -> str:
    """The COMPANY a stored story was searched for, from its `sheet_ref`
    ("Synthflow AI — on Master Pipeline"), or "" when the story was not found
    by a company search.

    ONLY A COMPANY SEARCH COUNTS. A story found by searching for a PERSON reads
    "Jane Doe, Synthflow AI — on Outreach PoCs": it is about her, not news
    about the company, so it names no company here. A story from the industry
    feeds has no `sheet_ref` at all: nothing in the store says which company it
    is about, and working that out would take a model call or a substring
    match on the headline. This rule does neither.
    """
    who = str(sheet_ref or "").split(" \u2014 on ")[0].strip()
    return "" if (not who or ", " in who) else who


def closure_news_pick(company, stories) -> Optional[dict]:
    """The ONE story to show under a deal at `company`, or None.

    `stories` are rows of the news store first seen inside R10's window. THE
    COMPANY IS MATCHED THE WAY R2 MATCHES IT: the FULL NAME, normalised
    (`news_screen.name_key`), never a substring: "Nudge Security" is not
    "Nudge". THE MOST IMPORTANT STORY WINS, by the importance R1 already gave
    it; then the newest. A story with no headline, or whose link carries no
    http(s) scheme, is not a story to show: a headline and its link come from
    the store or they are not shown.
    """
    import news_screen

    key = news_screen.name_key(company)
    if not key:
        return None
    best = None
    for story in stories or ():
        if news_screen.name_key(closure_news_company(story.get("sheet_ref"))) != key:
            continue
        title = " ".join(str(story.get("title") or "").split())
        url = str(story.get("url") or "").strip()
        if not title or not re.match(r"^https?://\S+$", url):
            continue
        rank = (int(story.get("importance") or 0), str(story.get("published_at") or ""),
                str(story.get("seen_at") or ""))
        if best is None or rank > best[0]:
            best = (rank, {"headline": title, "url": url,
                           "published_at": str(story.get("published_at") or ""),
                           "seen_at": str(story.get("seen_at") or "")})
    return best[1] if best else None


def _r_closure_support(rule, ctx) -> list:
    """R10 — a closure stage AND a closure probability strictly above
    CLOSURE_SUPPORT_MIN. Mondays.

    EXACTLY 50 IS EXCLUDED. "Above 50" is the rule as written, and a boundary a
    bot decides for itself is a boundary nobody agreed to. `>` not `>=`, and the
    reason line says so, so nobody has to read this file to find out.

    A CLOSURE-STAGE ROW WITH NO PERCENTAGE IS SKIPPED SILENTLY: logged, never
    posted, never counted in the message. Nothing can be said about how close
    a deal is without a number.

    ONLY ACTIVE ROWS (`ctx["rows"]`), on purpose: a deal at a closure stage
    has been contacted. This rule does not read the whole tab.

    EVERY QUALIFYING DEAL IS HANDED ON, IN `closure_order`. THERE IS NO CAP: a
    closure stage AND above 50% should never be more than a handful, and if it
    is ever twenty that is a good week and all twenty should show. The rule's
    `max_items_per_post` is not applied to it (`drip.shown_contacts`).

    NO FETCH. Until 13 Oct 2026 each deal asked the research layer for a news
    search. The news line now comes from what R1 has already stored
    (`bot._closure_news`, which reads; this function is handed nothing and
    asks for nothing).

    AN EMPTY COLUMN IS SAID OUT LOUD; AN EMPTY RESULT IS NOT. When Prospect
    Status — or Closure Prob% — is blank on EVERY active row, the rule cannot
    run at all, and a silent Monday reads exactly like "no deal is close".
    So that one case posts a line naming the empty column(s)
    (`wording.closure_empty_notice`), EVERY MONDAY for as long as they are
    empty: it is the weekly reminder that they have not been filled in, so
    there is no "said this already" check. When the columns have values and no
    deal qualifies, that is an answer: nothing is posted, and the log says why.
    """
    today = ctx["today"]
    floor = int(config.CLOSURE_SUPPORT_MIN)
    rule_id = getattr(rule, "id", "") or "R10"
    rows = list(ctx.get("rows") or ())
    report = {"rows": len(rows), "looked": 0, "in_stage": 0, "no_percentage": [],
              "at_or_below": 0, "deals": []}
    ctx.setdefault("reports", {})[rule_id] = report
    if not rows:
        log.error("[R10] no active Outreach PoCs rows were handed over (the tab is empty, "
                  "unreadable, or nothing on it is active), so nothing is posted. That is "
                  "not the same as no deal being close.")
        return []
    deals = []
    with_status = with_pct = 0
    for row in rows:
        ok, _reason, _until = row_gate(row, today=today, snoozes=ctx.get("snoozes") or {})
        if not ok:
            continue
        report["looked"] += 1
        with_status += 1 if _text(row, "prospect_status") else 0
        with_pct += 1 if _text(row, "closure_prob") else 0
        stage = closure_stage(row.get("prospect_status"))
        if not stage:
            continue
        report["in_stage"] += 1
        pct = closure_percent(row)
        if pct is None:
            report["no_percentage"].append(
                {"sheet_row": row.get("_row"), "poc": _text(row, "name"),
                 "company": _text(row, "company")})
            continue
        if pct <= floor:
            report["at_or_below"] += 1
            continue
        deals.append({"row": row, "stage": stage, "closure_pct": pct,
                      "sheet_row": row.get("_row")})
    looked = report["looked"]
    if report["no_percentage"]:
        log.info("[R10] %d closure-stage row(s) have NO Closure Prob%% and are left out "
                 "(not posted, not counted): %s", len(report["no_percentage"]),
                 "; ".join(f"row {e['sheet_row']} {e['poc']} ({e['company']})"
                           for e in report["no_percentage"]))
    deals.sort(key=closure_order)
    out = []
    for order, deal in enumerate(deals):
        row, pct = deal["row"], deal["closure_pct"]
        who = wording.next_step_who(_text(row, "name"), _text(row, "company"))
        report["deals"].append({"sheet_row": row.get("_row"), "poc": _text(row, "name"),
                                "company": _text(row, "company"), "closure_pct": pct})
        out.append(_item(
            rule=rule, trigger=R_CLOSURE_SUPPORT, today=today, due=today,
            why=(f"R10 (Mondays): prospect status {_text(row, 'prospect_status')!r} "
                 f"({deal['stage']}) and closure {pct}%, above CLOSURE_SUPPORT_MIN "
                 f"({floor}%) — exactly {floor}% would not qualify; {order + 1} of "
                 f"{len(deals)}, highest first, no cap"),
            text=f"{who} is in the closure stage: {pct}% at {_text(row, 'prospect_status')}",
            company=_text(row, "company"), poc=_text(row, "name"),
            designation=_text(row, "designation"), sheet_row=row.get("_row"),
            row_key=_contact_key(row), contact_key=_contact_key(row),
            extra={"closure_pct": pct, "stage": _text(row, "prospect_status"),
                   "pick_order": order, "who": who},
        ))
    if out:
        log.info("[R10] %d deal(s) in a closure stage above %d%%, highest first, all "
                 "handed on (no cap): %s", len(out), floor,
                 "; ".join(f"{d['poc']} ({d['company']}) {d['closure_pct']}%"
                           for d in report["deals"]))
        return out
    if not looked:
        log.info("[R10] every active row is stopped or snoozed; nothing posted")
        return out
    if not with_status or not with_pct:
        empty = [name for name, n in (("Prospect Status", with_status),
                                      ("Closure Prob%", with_pct)) if not n]
        log.info("[R10] %s blank on all %d active row(s) — posting the empty-columns "
                 "notice instead of staying silent", " and ".join(empty), looked)
        text = wording.closure_empty_notice(empty)
        notice = _item(
            rule=rule, trigger=R_CLOSURE_SUPPORT, today=today, due=today,
            why=f"R10 (Mondays): {' and '.join(empty)} blank on every active row",
            text=text,
            extra={"notice": text, "empty_columns": empty},
        )
        notice["key"] = "R10:empty-columns"
        return [notice]
    log.info("[R10] no closure support today: %d active row(s), %d with a Prospect "
             "Status (%d in %s), %d with a Closure Prob%% (%d in a closure stage with "
             "none, %d at or below %d%%) — none is in a closure stage above %d%%. "
             "Nothing posted.", looked, with_status, report["in_stage"],
             "/".join(str(x) for x in config.CLOSURE_SUPPORT_STAGES), with_pct,
             len(report["no_percentage"]), report["at_or_below"], floor, floor)
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
    import poc_crosscheck

    today = ctx["today"]
    wait = max(0, int(config.NEW_COMPANY_AFTER_WORKING_DAYS))
    window = max(1, int(config.NEW_COMPANY_WINDOW_DAYS))
    # THE OUTREACH PoCs CROSS-CHECK (9 Oct). The caller hands over a SUMMARY of
    # that tab (`poc_crosscheck.build_index`: a row count and the mandatory
    # roles left blank, per company) and never a row, so this stays pure. A
    # company with no row there is Branch A; one whose rows lack a mandatory
    # field is Branch B; one that is complete produces nothing at all. None
    # (no summary: the tab was not read) is said in the log and treated as "no
    # rows", because the write path re-reads the tab and refuses a duplicate.
    index = ctx.get("poc_company_index")
    if index is None and ctx.get("new_companies"):
        log.info("[rules] R11: no Outreach PoCs summary was supplied; every new "
                 "company is treated as having no contacts yet")
    out = []
    # THE ORDER OF THE MASTER PIPELINE TAB, so the post lists companies the way
    # the team sees them on the sheet and not alphabetically.
    position: dict = {}
    for i, listed in enumerate(ctx.get("pipeline_companies") or ()):
        position.setdefault(poc_crosscheck.company_key(listed), i)
    # SEVERAL MASTER PIPELINE ROWS CAN NAME ONE COMPANY: each appears once.
    for seen, entry in enumerate(poc_crosscheck.collapse(ctx.get("new_companies"))):
        name = str((entry or {}).get("company") or "").strip()
        first_seen = dl.parse_date(str((entry or {}).get("first_seen") or ""))
        if not name or first_seen is None:
            continue
        due = shift_off_weekend(dl.add_working_days(first_seen, wait))
        if due > today:
            continue
        if (today - first_seen).days > window:
            continue
        # THREE STATES, ALL LISTED (9 Oct): no row yet, rows with a mandatory
        # gap, and rows with nothing missing. The last used to be left out; the
        # team wants it named too, because a yes then looks for MORE people.
        branch = poc_crosscheck.classify(name, index)
        out.append(_item(
            rule=rule, trigger=R_NEW_COMPANY, today=today, due=due,
            why=(f"R11 (Wednesdays): {name} first appeared in the Master Pipeline "
                 f"on {dl.format_date(first_seen)}, {(today - first_seen).days} day(s) ago"),
            text=(f"{name} is new in the Master Pipeline. Want me to look for "
                  "relevant PoCs for outreach?"),
            company=name, web_pending=False,
            extra={"first_seen": dl.iso(first_seen), "branch": branch,
                   "sheet_order": position.get(poc_crosscheck.company_key(name),
                                               10 ** 6 + seen),
                   # The start of the window, for the post's "since <day>".
                   "window_start": dl.iso(today - timedelta(days=window))},
        ))
    return out


def _r_sales_packages(rule, ctx) -> list:
    """R12 — every package whose Ready? is No or blank. Thursdays.

    BLANK READS AS NO (`gtm_sheet.ready_flag`). A package nobody has marked
    ready is a package nobody has said is ready, and the two mistakes do not
    cost the same: calling a finished package unready costs one question, and
    offering a prospect a half-built one costs a promise somebody else keeps.

    THE SALES PACKAGES TAB AND NOTHING ELSE (`ctx["packages"]`): no Outreach
    PoCs row, no activation gate, and no contact key, so the cross-rule dedup
    never touches it.

    WHO AND IN WHAT ORDER is `packages.not_ready`: one package a name (two rows
    with one name are logged and the furthest-along kept), highest % Completion
    first unless PACKAGES_ORDER says otherwise, a blank or unreadable per cent
    last, ties in sheet order. A row with a blank name is skipped. READY WINS:
    a package whose Ready? is Yes is not listed whatever its per cent says.

    THE CAP IS THE RULE'S `max_items_per_post` AND THERE IS NO SECOND ONE. The
    first that many are handed on, each carrying how many were held back, and
    the post says "(+N more next Thursday.)". NOTHING DROPS ON A CLOCK AND
    NOTHING ROTATES: a package leaves the list only when Ready? is set to Yes,
    which is also when a held-back one moves up.

    NOTHING QUALIFIES: no item, so no post. There is no "everything is ready"
    line, and no announcement when a package becomes ready.

    THE TAB WILL NOT READ, OR THE NAME OR READY? COLUMN IS MISSING: no post,
    said at ERROR. A column the team moved is found by its header; one renamed
    to something no alias matches reads as missing, and the rule stays silent
    rather than guess.
    """
    import packages as packages_mod

    today = ctx["today"]
    rule_id = getattr(rule, "id", "") or "R12"
    rows = list(ctx.get("packages") or ())
    report = {"rows": len(rows), "listed": [], "held_back": [], "duplicates": [],
              "blank_names": 0, "missing_columns": []}
    ctx.setdefault("reports", {})[rule_id] = report
    if not rows:
        log.error("[R12] the Sales Packages tab came back with NO ROWS (empty or "
                  "unreadable), so nothing is posted.")
        return []
    missing = packages_mod.columns_missing(rows)
    if missing:
        report["missing_columns"] = missing
        log.error("[R12] the Sales Packages tab has no %s column I can find (a header was "
                  "renamed to something I don't recognise), so nothing is posted. I don't "
                  "guess which column it is.", " or ".join(missing))
        return []
    found = packages_mod.not_ready(rows)
    report["blank_names"] = found["blank_names"]
    report["duplicates"] = found["duplicates"]
    for dup in found["duplicates"]:
        log.warning("[R12] %r is on the tab twice (rows %s and %s); keeping row %s, the "
                    "furthest along", dup["name"], dup["kept_row"], dup["dropped_row"],
                    dup["kept_row"])
    waiting = found["packages"]
    cap = max(1, int(getattr(rule, "max_items_per_post", 5) or 5))
    shown, held = waiting[:cap], waiting[cap:]
    report["listed"] = [p["name"] for p in shown]
    report["held_back"] = [p["name"] for p in held]
    log.info("[R12] %d row(s) read; %d package(s) not ready, order=%s: %d in the post, %d "
             "held back for a later Thursday; %d blank name(s) skipped, %d duplicate "
             "name(s)", len(rows), len(waiting), packages_mod.order(), len(shown), len(held),
             found["blank_names"], len(found["duplicates"]))
    out = []
    for order, pkg in enumerate(shown):
        out.append(_item(
            rule=rule, trigger=R_PACKAGES, today=today, due=today,
            why=(f"R12 (Thursdays): Ready? is {pkg['ready_raw'] or 'blank'}, status "
                 f"{pkg['status'] or '(blank)'}; {order + 1} of {len(shown)} in the post, "
                 f"{len(held)} held back"),
            # THE NAME ALONE. The renderer owns the wording
            # (`wording.packages_post`), and the agreed post is NAMES ONLY.
            text=pkg["name"],
            company=pkg["name"], sheet_row=pkg["sheet_row"],
            # `completion`, `status` and `ready` DECIDE THE ORDER AND WHO IS IN;
            # `product_doc` is the link behind the name. NONE OF THEM IS PRINTED.
            # The post shows no per cent, no status and no number of any kind:
            # do not add one back to the line.
            extra={"completion": pkg["completion"], "status": pkg["status"],
                   "ready": pkg["ready"], "product_doc": pkg["doc"],
                   "pick_order": order, "held_back": len(held)},
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

# What the Next Steps dropdown says, as a code. `dm` and `call` are two
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

# WHAT A PERSON IS REMINDED ABOUT, as a code.
ASK_REPLIED = "replied"            # they answered the DM: set up a meeting
ASK_CALL = "call"                  # the DM went out: call them
ASK_ADVANCE = "advance"            # email N went out a week ago: send N + 1
ASK_EMAIL_OUT = "email_out"        # email N has not gone out: send it
ASK_RESEARCH = "research"          # Next Steps says Research the PoC
ASK_BLANK = "blank"                # Next Steps is blank: research them
ASK_UNRESPONSIVE = "unresponsive"  # the calls ran out

# THE ORDER PEOPLE ARE PICKED IN, hottest first. The number is the tier.
#   1  replied to the DM            a real person is waiting on us
#   2  call due                     that clock has already run out
#   3  the next email is due        (the "advance" asks)
#   4  an email that has not gone out
#   5  research, and a blank Next Steps
#   6  unresponsive
#
# NOBODY IS EVER DROPPED (12 Oct 2026). A three-week drop, with a "last chance"
# tier in front of it, was built and reversed the same day: there is no point
# dropping somebody while their row still says work is due. The rule keeps
# reminding, ROTATES inside each tier (`_next_step_order`), and moves when the
# sheet moves.
TIER_REPLIED, TIER_CALL, TIER_ADVANCE, TIER_EMAIL, TIER_RESEARCH, TIER_UNRESPONSIVE = \
    1, 2, 3, 4, 5, 6
_TIER_OF_ASK = {
    ASK_REPLIED: TIER_REPLIED, ASK_CALL: TIER_CALL, ASK_ADVANCE: TIER_ADVANCE,
    ASK_EMAIL_OUT: TIER_EMAIL, ASK_RESEARCH: TIER_RESEARCH, ASK_BLANK: TIER_RESEARCH,
    ASK_UNRESPONSIVE: TIER_UNRESPONSIVE,
}

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


def dm_replied(row: dict) -> bool:
    """Does LI DM Sent say they REPLIED? Checked before `dm_sent`, always: a
    reply must never be read as a plain "sent".

    "RECEIVED" IS IN BOTH MARKER LISTS, ON PURPOSE. The dropdown holds blank,
    Sent and Received, and Received means they answered. Because this check
    runs FIRST, a Received DM takes the meeting branch and never the call
    ladder; because it is also a "sent" marker, anything that only asks
    "has the DM gone out?" still gets a yes, so nobody stalls at the DM step.
    DO NOT REORDER THE TWO CHECKS."""
    return bool(config.NEXT_STEP_DM_REPLIED_MARKERS and _matches_any(
        row.get("li_dm_sent"), config.NEXT_STEP_DM_REPLIED_MARKERS))


def dm_sent(row: dict) -> bool:
    """Has the LinkedIn DM gone out? READ OFF THE "LI DM Sent" CELL.

    NOT OFF THE DATE (12 Oct 2026). The rule used to decide the DM had gone
    out by finding an LI DM Date, so a DM that was sent and never dated stalled
    the person on a request to log the date. The cell is what says it
    happened: its whole value, normalised, is one of NEXT_STEP_DM_SENT_MARKERS,
    or it is a ticked checkbox. "Not sent" is not "sent".
    """
    value = row.get("li_dm_sent")
    marks = {gtm_sheet.normalise_header(str(m)) for m in (config.NEXT_STEP_DM_SENT_MARKERS or [])
             if str(m).strip()}
    return _norm(value) in marks or gtm_sheet.parse_flag(value) is True


def next_step_for(row: dict, *, today: date, entry: Optional[dict] = None) -> dict:
    """What ONE connected row is reminded about today.

    Returns {"status", "code", "signature", "ask", "due", "n", "why"}.
    `status` is "due", "waiting", "silent" or "unknown_step". PURE: `entry` is
    the row's stored state (or None) and is only read.

    IT IS A REMINDER OF WHAT NEEDS DOING. It never asks for a cell to be
    filled, never names a column and never raises a missing date.

      GO BY THE DROPDOWN. When Next Steps says "Send email 2" and the first
      email is not marked sent, the reminder is still about the second email.

      A BLANK DATE MEANS DUE NOW. "Two days after the previous step" and "a
      week after the email" cannot be counted from nothing, and waiting for a
      date nobody logged would hide the person for good. So a connected row
      with no LI Connected Date, an email marked sent with no date, and a DM
      marked sent with no date are all due straight away. None of them needs a
      date in the sheet.

    TWO KNOWN GAPS, WHERE NOTHING IS SAID (`silent`), on purpose:
      - Next Steps says "Reach by LI DM" and LI DM Sent is blank. Nothing
        reminds anybody to send the DM. THIS IS THE ONE KNOWN GAP OF THE 12 OCT
        RELEASE: the agreed message has no line for it, and none was invented.
      - Next Steps says "Send email 3" and the third email is marked sent.
        There is no fourth email, and nothing says "now move to the DM".
    """
    code = step_code(row.get("outreach_step"))
    out = {"status": "unknown_step", "code": code, "signature": "", "ask": "",
           "due": None, "n": 0, "why": ""}
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

    def silent(why):
        out.update(status="silent", why=why)
        return out

    if code in (STEP_BLANK, STEP_RESEARCH):
        due = (connected + timedelta(days=first)) if connected else today
        return done(
            ASK_BLANK if code == STEP_BLANK else ASK_RESEARCH, due,
            f"Next Steps is {'blank' if code == STEP_BLANK else 'Research the PoC'}; "
            + (f"LI Connected Date + NEXT_STEP_FIRST_DAYS ({first})" if connected
               else "no LI Connected Date, so due now"))

    n = _email_n(code)
    if n:
        if not _email_sent(row, n):
            previous = connected if n == 1 else _email_date(row, n - 1)
            due = (previous + timedelta(days=after_prev)) if previous else today
            return done(
                ASK_EMAIL_OUT, due,
                f"Send email {n}, not marked sent; "
                + (f"previous step {dl.iso(previous)} + NEXT_STEP_AFTER_PREVIOUS_DAYS "
                   f"({after_prev})" if previous
                   else "previous step date blank, so due now"), n=n)
        if n >= 3:
            return silent("the third email is marked sent; nothing follows it here")
        sent_on = _email_date(row, n)
        if sent_on is None:
            # MARKED SENT, NO DATE: the step is done and the person moves on.
            # The missing date is never mentioned.
            return done(ASK_ADVANCE, today,
                        f"email {n} is marked sent with no date, so due now", n=n)
        return done(
            ASK_ADVANCE, sent_on + timedelta(days=after_email),
            f"email {n} sent {dl.iso(sent_on)} + NEXT_STEP_AFTER_EMAIL_DAYS "
            f"({after_email})", n=n)

    # -- Reach by LI DM / Call the PoC ---------------------------------------
    if dm_replied(row):
        return done(ASK_REPLIED, today,
                    f"LI DM Sent says {_text(row, 'li_dm_sent')!r}: set up a meeting")
    if code == STEP_DM and not dm_sent(row):
        return silent("Reach by LI DM and LI DM Sent does not say sent (the known gap)")

    # THE CALL STAGE: the DM is marked sent, or Next Steps already says Call
    # the PoC (which is the call stage whatever else the row says).
    dm = _date(row, "li_dm_date")
    call_after = max(0, int(config.NEXT_STEP_CALL_AFTER_DM_DAYS))
    call_every = max(1, int(config.NEXT_STEP_CALL_EVERY_DAYS))
    call_until = max(0, int(config.NEXT_STEP_CALL_UNTIL_DAYS))
    same = bool(entry) and str(entry.get("signature") or "") == sig
    calls = int((entry or {}).get("calls") or 0) if same else 0
    last_call = dl.parse_date((entry or {}).get("last_call_date")) if same else None
    if dm is None:
        # NO DATE TO COUNT FROM: the first call is due straight away, and the
        # calls repeat from the last one. The ladder's end cannot be counted
        # either, so this reminder keeps coming until the row moves.
        due = (last_call + timedelta(days=call_every)) if (calls and last_call) else today
        return done(ASK_CALL, due,
                    f"the DM is marked sent with no date; call reminder {calls + 1}",
                    has_dm_date=False)
    start = dm + timedelta(days=call_after)
    end = dm + timedelta(days=call_until)
    if today < start:
        return done(ASK_CALL, start,
                    f"LI DM Date {dl.iso(dm)} + NEXT_STEP_CALL_AFTER_DM_DAYS "
                    f"({call_after})", has_dm_date=True)
    if today > end:
        return done(ASK_UNRESPONSIVE, today,
                    f"more than NEXT_STEP_CALL_UNTIL_DAYS ({call_until}) since the "
                    f"LI DM on {dl.iso(dm)}", has_dm_date=True)
    due = start
    if calls and last_call is not None:
        due = last_call + timedelta(days=call_every)
    return done(
        ASK_CALL, due,
        f"LI DM {dl.iso(dm)}, {(today - dm).days}d ago; call reminder "
        f"{calls + 1}, every NEXT_STEP_CALL_EVERY_DAYS ({call_every}) until day "
        f"{call_until}", has_dm_date=True)


def company_scores(rows) -> dict:
    """{company key: score} for EVERY company on the tab, by R5's six rules.

    R5's OWN WEIGHTS AND PHRASE LISTS, imported, never copied
    (`prospects.RULES`, `WEIGHTS`, `industry_is`, `is_uk`, `has_keyword`,
    `has_paper`), so the two rules agree on which companies matter.

    NOT `prospects.build_queue`: that queue holds only companies that still
    have somebody with NO first contact, and the people this rule names are
    connected, which usually means their first contact IS recorded. Most of
    their companies are not in that queue at all. So each company is scored
    here from ALL its rows: warm is two or more people there with a first
    contact (`prospects.WARM_MIN_REACHED`), and the three person signals count
    when anybody at the company carries them.
    """
    import prospects

    groups: dict = {}
    for row in rows or ():
        key = prospects.company_key(_text(row, "company"))
        if not key:
            continue
        g = groups.setdefault(key, {"reached": 0, "industries": [], "rows": []})
        g["rows"].append(row)
        g["industries"].append(_text(row, "industry"))
        if first_contact_done(row):
            g["reached"] += 1
    out: dict = {}
    for key, g in groups.items():
        found = {
            "warm": g["reached"] >= prospects.WARM_MIN_REACHED,
            "voice": any(prospects.industry_is(i, config.PROSPECT_VOICE_INDUSTRIES)
                         for i in g["industries"]),
            "lab": any(prospects.industry_is(i, config.PROSPECT_LAB_INDUSTRIES)
                       for i in g["industries"]),
            "uk": any(prospects.is_uk(r) for r in g["rows"]),
            "keyword": any(prospects.has_keyword(r) for r in g["rows"]),
            "paper": any(prospects.has_paper(r) for r in g["rows"]),
        }
        out[key] = sum(prospects.WEIGHTS[name] for name in prospects.RULES if found[name])
    return out


def _next_step_order(person: dict) -> tuple:
    """THE ORDER PEOPLE ARE PICKED IN, in one place.

    The tier first (see TIER_*). INSIDE A TIER, A ROTATION: least recently
    named first. A person never named for their current step sorts ahead of
    anybody who has been, because "" comes before every ISO date. Without it
    the same five would be named every weekday for ever and the rest of the
    tier would never be reached, now that nothing drops anybody.

    Then the OLDEST LI Connected Date, then the better R5 company score
    (`company_scores`), then sheet order. A row with NO LI Connected Date goes
    last among people named equally recently, in sheet order, because there is
    no date to sort it by.

    Priority is read and shown in the preview and is deliberately not
    used here: the tiers are the priority.

    R6 HAS NO ROTATION, BY DESIGN, AND THIS RULE DOES. They are not to be made
    alike.
    """
    connected = person.get("connected")
    row_no = person["row"].get("_row") or 0
    last = str(person.get("last_date") or "")
    if connected is None:
        return (person["tier"], last, 1, "", 0, row_no)
    return (person["tier"], last, 0, dl.iso(connected), -int(person.get("score") or 0), row_no)


def _r_next_step_followups(rule, ctx) -> list:
    """R13 — a reminder of the next step for people we are connected with.
    Weekdays, one post.

    WHO IS IN: every row whose "Sid - LI Addition" says Connected, that the
    stop and snooze gates let through. That is the only test. A connected row
    with NO LI Connected Date is in (it used to be skipped and only reported).
    A Completed meeting leaves for good (R9 takes over); a meeting booked for
    today or later pauses the person.

    WHO IS PICKED: at most `max_items_per_post`, in `_next_step_order`: the
    tier, then whoever was named least recently.

    NOBODY IS DROPPED AND NOTHING CLOSES A PERSON. A reminder keeps coming
    round, in its turn, until the sheet moves: the step changes, a meeting is
    booked or completed, or a stop marker takes the row out. (A three-week drop
    was built on 12 Oct 2026 and removed the same day. The `closed`,
    `closed_date` and `first_named` columns it used are left in the table,
    unread.)

    THE UNRESPONSIVE LINE REPEATS, AND THAT IS INTENDED. Once the call ladder
    has run out the person is in the last tier and comes round with the
    rotation like anybody else. The reminder names the action that ends it:
    setting Prospect Status to Unresponsive takes the row out through the
    ordinary stop markers. It is not a one-off, so do not make it one.

    THE EVALUATOR PICKS, NOT THE DRIP. Every other rule hands over everything
    that qualified and lets the post take what fits. Here the five ARE the
    rule; handing over thirty would let the cross-rule dedup and the post's
    own ordering choose a different five from the ones this order meant.

    `ctx["next_step_state"]` is {row_key: {...}} from the caller. NONE MEANS
    UNREADABLE, AND THEN NOTHING IS PRODUCED: with no state the rotation would
    restart from the top and the people named yesterday would be named again.
    Nothing is written here; the sender records a mention after a real send.

    A STORED ENTRY WHOSE SIGNATURE DIFFERS FROM THE ROW'S IS IGNORED. That is
    all "the state for the old step clears" needs (the person is new again and
    goes to the front of their tier), and it means a preview never has to
    write.

    ONCE TODAY'S POST HAS GONE, TODAY'S PEOPLE STAY TODAY'S PEOPLE. The queue
    is recomputed on every sweep tick. Without this, the tick after the post
    would pick the NEXT five, and the one-mention-a-day dedup would take those
    five away from R5 and R6 for a post that is never going to be sent, while
    freeing the five who really were named. So anyone the state says was named
    today is returned as-is and nobody else is; the drip does not send the
    group twice in a day.
    """
    today = ctx["today"]
    state = ctx.get("next_step_state")
    rule_id = getattr(rule, "id", "") or "R13"
    report = {"state": "ok", "connected": 0, "due": 0, "waiting": 0, "completed": 0,
              "no_date": [], "unknown_step": [], "paused": [],
              "silent": [], "picked": [], "queued": []}
    ctx.setdefault("reports", {})[rule_id] = report
    if state is None:
        report["state"] = "unreadable"
        log.info("[rules] %s: the reminder state was not supplied or could not be "
                 "read, so nobody is named", rule_id)
        return []

    markers = connected_markers()
    snoozes = ctx.get("snoozes") or {}
    # Every row, not only the "active" ones, for the reason R5 gives: a row
    # marked Connected may not have passed the activation gate.
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
    scores = company_scores(in_sheet_order)

    def person_of(row, key, found, *, last_date=""):
        import prospects
        return {"row": row, "key": key, "found": found, "ask": found["ask"],
                "tier": _TIER_OF_ASK.get(found["ask"], TIER_RESEARCH),
                "connected": _date(row, "li_connected_date"), "last_date": last_date,
                "score": scores.get(prospects.company_key(_text(row, "company")), 0)}

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
        key = _contact_key(row)
        stored = state.get(key) or None
        if stored and str(stored.get("last_date") or "") == today_iso:
            found = next_step_for(row, today=today, entry=stored)
            # WHAT THEY WERE NAMED FOR IS WHAT THE POST SAID, not what the row
            # says now: somebody may have updated the sheet since 15:00.
            person = person_of(row, key, found)
            person["ask"] = str(stored.get("last_ask") or found["ask"] or ASK_BLANK)
            named_today.append(person)
            continue
        if not has_connected_date(row):
            # STILL REPORTED, NO LONGER SKIPPED: the preview says who has no
            # date, and the person is reminded about like everybody else.
            raw = _text(row, "li_connected_date")
            report["no_date"].append(entry_of(
                row, f"LI Connected Date reads {raw!r}" if raw
                else "LI Connected Date is blank"))
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
        if found["status"] == "silent":
            report["silent"].append(entry_of(row, found["why"]))
            continue
        if found["status"] == "waiting":
            report["waiting"] += 1
            continue
        # THE ROTATION'S CLOCK BELONGS TO THE STEP: a person whose step changed
        # since they were last named is new again.
        same = bool(stored) and str(stored.get("signature") or "") == found["signature"]
        due_people.append(person_of(
            row, key, found, last_date=str(stored.get("last_date") or "") if same else ""))

    report["due"] = len(due_people)
    limit = max(1, int(getattr(rule, "max_items_per_post", 5) or 5))
    ranked = sorted(due_people, key=_next_step_order)
    picked, queued = ranked[:limit], ranked[limit:]
    if named_today:
        picked, queued = named_today, ranked
        report["posted_today"] = True

    out = []
    for position, person in enumerate(picked):
        row, found, ask = person["row"], person["found"], person["ask"]
        n = found["n"]
        label = {
            STEP_BLANK: "", STEP_RESEARCH: wording.STEP_RESEARCH,
            STEP_DM: wording.STEP_DM, STEP_CALL: wording.STEP_CALL,
        }.get(found["code"], wording.STEP_EMAILS[n - 1] if n else "")
        who = wording.next_step_who(_text(row, "name"), _text(row, "company"))
        line = wording.next_step_line(ask, who=who, n=n) or f"{who}: named in today's post"
        report["picked"].append({**entry_of(row, ask), "line": line})
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
                "step": found["code"], "step_label": label, "ask": ask,
                "signature": found["signature"],
                "pick_order": position, "tier": person["tier"],
                # WHAT THE GROUPED POST NEEDS: who they are and which group.
                "who": who, "poc_priority": _text(row, "poc_priority"),
                "email_n": n,
            },
        ))
    for person in queued:
        report["queued"].append(entry_of(person["row"], person["ask"]))

    log.info(
        "[rules] %s: %d connected, %d due -> %d picked, %d queued (they come round in "
        "turn; nobody is dropped); %d waiting, %d with no LI Connected Date (included), "
        "%d unknown step, %d with nothing to say, %d paused for a meeting, %d completed",
        rule_id, report["connected"], report["due"], len(picked), len(queued),
        report["waiting"], len(report["no_date"]), len(report["unknown_step"]),
        len(report["silent"]), len(report["paused"]), report["completed"],
    )
    return out


def next_step_report_lines(report: Optional[dict], rule_id: str = "R13") -> list:
    """The preview block that says who R13 did NOT name today, and why.

    A rule that names five people out of thirty looks, from the post alone,
    exactly like a rule that only found five. This is where the other
    twenty-five are accounted for.
    """
    if not report:
        return []
    if report.get("state") == "unreadable":
        return [f"**{rule_id} · next steps** — the reminder state could not be "
                "read, so nobody is named today."]
    if report.get("state") == "no_step_column":
        return [f"**{rule_id} · next steps** — the tab has no column mapped to "
                f"{', '.join(report.get('missing_columns') or [])}, so nobody is "
                "named today."]

    def names(entries, limit=12):
        shown = [
            f"{e.get('poc') or e.get('company') or '(unnamed)'} (row {e.get('sheet_row')}"
            + (f", {e['detail']}" if e.get("detail") else "")
            + (f", priority {e['poc_priority']}" if e.get("poc_priority") else "") + ")"
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
        ("silent", "Nothing to remind about (known gaps)"),
        ("no_date", "Connected with no LI Connected Date (included, sorted last)"),
        ("unknown_step", "Next Steps value I don't recognise (skipped)"),
        ("paused", "Paused for a booked meeting"),
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
    new_companies: Optional[list] = None, prospect_state: Optional[dict] = None,
    meeting_followups: Optional[dict] = None,
    focus: Optional[dict] = None,
    inactive: int = 0, day_rules: Optional[list] = None,
    prospect_rows: Optional[list] = None,
    next_step_state: Optional[dict] = None,
    poc_company_index: Optional[dict] = None,
    dm_no_meeting_state: Optional[dict] = None,
    meeting_rows: Optional[list] = None,
    meeting_state: Optional[dict] = None,
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

    `dm_no_meeting_state` is R7's rotation state from
    `db.dm_no_meeting_state()`, with the same rule: None names nobody.

    `meeting_rows` is EVERY row of Outreach PoCs, for R8, which reads one
    column and applies no activation gate (None = use `rows`).
    `meeting_state` is R8's memory from `db.meeting_prep_state()`. None here
    means "no memory" (every touch due by its date; a preview); the SENDER
    passes {"unreadable": True} when the table cannot be read, and R8 then
    names nobody.
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
        # R11's SUMMARY OF OUTREACH PoCs, built by the caller. None is kept as
        # None: "not supplied" and "the tab is empty" are different.
        "poc_company_index": (dict(poc_company_index)
                              if poc_company_index is not None else None),
        # R5: which company is open and which are paused, read by the caller.
        "prospect_state": dict(prospect_state or {}),
        "meeting_followups": meeting_followups or {},
        # R5 READS EVERY ROW, not only the active ones (None = use `rows`).
        "prospect_rows": list(prospect_rows) if prospect_rows is not None else None,
        # R13's ROTATION STATE. None (unreadable) is kept as None on purpose.
        "next_step_state": (dict(next_step_state)
                            if next_step_state is not None else None),
        # R7's ROTATION STATE. None (unreadable) is kept as None, like R13's.
        "dm_no_meeting_state": (dict(dm_no_meeting_state)
                                if dm_no_meeting_state is not None else None),
        # R8 READS EVERY ROW THAT HAS A MEETING DATE (None = use `rows`), and
        # its memory of which touches have gone out.
        "meeting_rows": list(meeting_rows) if meeting_rows is not None else None,
        "meeting_state": meeting_state,
        # THE CONTACTS EARLIER-LISTED RULES HAVE NAMED SO FAR TODAY, filled in
        # as the rules run (below). A rule that picks a fixed few (R7) reads it
        # so it does not spend a place on somebody the dedup will take away.
        "taken": set(),
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
        # A rule that `yields` (R6) takes a contact from nobody, so its people
        # are not "taken".
        ctx["taken"].update(it.get("contact_key") for it in items
                            if it.get("contact_key") and not it.get("yields"))
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
    # A RULE THAT GIVES WAY (`yields`, today only R6). R6 reads the whole tab and
    # hands on thirty people so the sender can choose ten; the other twenty were
    # never going to be named. Under plain file order it would still have taken
    # all thirty away from every rule listed after it, so on a Tuesday the
    # meeting prep (R8) or the follow-up (R9) for one of them simply vanished.
    # So an item marked `yields` loses to ANY other rule naming the same
    # contact, earlier OR later in the file, and never takes a contact from
    # one. Among themselves, and for every other rule, file order is unchanged.
    firm = {}
    for item in ordered:
        key = item.get("contact_key") or ""
        if key and not item.get("yields") and key not in firm:
            firm[key] = item
    seen: dict = {}
    actions: list = []
    deduped: list = []
    for item in ordered:
        key = item.get("contact_key") or ""
        if key and item.get("yields") and key in firm:
            seen.setdefault(key, firm[key])
        if key and item.get("never_dropped"):
            # R8: A MEETING IS NAMED WHATEVER ELSE NAMES THE PERSON TODAY. It
            # still claims the contact from the rules listed after it.
            seen.setdefault(key, item)
            actions.append(item)
            continue
        if key and key in seen and seen[key] is not item:
            keeper = seen[key]
            deduped.append({
                **item,
                "dropped_for": keeper.get("rule_id") or keeper.get("rule"),
                "dropped_why": (
                    f"{item.get('rule_id') or item.get('rule')} also selected this contact, "
                    f"but {keeper.get('rule_id') or keeper.get('rule')} "
                    f"({keeper.get('rule_name')}) "
                    + ("names them today and this rule gives way to every other rule"
                       if item.get("yields") else "is listed first")
                    + " and one contact is named at most once a day"
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

    print("\nR5 prospects — eligibility, one company, role order")
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
    check("ONE company is open (nothing scores, so fewer-left then sheet order)",
          sorted({a["company"] for a in out["actions"]}), ["Borealis"])
    out = run(today=TUE, rows=rows5, prospect_state={"open": "acme"},
              day_rules=[rules_mod.by_id("R5")])
    check("the company held open stays open, in role order",
          [a["poc"] for a in sorted(out["actions"], key=lambda a: a["pick_order"])],
          ["Ann", "Cy", "Zed"])
    check("...and nothing asks to skip anybody",
          [k for a in out["actions"] for k in ("ask_to_skip", "repeat_count") if k in a], [])
    # THE ROLE ORDER IS PINNED HERE: a machine's .env may still carry an older list.
    kept_order = config.PROSPECT_ROLE_ORDER
    config.PROSPECT_ROLE_ORDER = ["founder", "chief scientist", "cto", "chief", "head of",
                                  "director", "lead"]
    try:
        check("a Director is not a CTO (whole words)",
              (_role_rank("Director of Research"), _role_rank("Co-Founder/CTO"),
               _role_rank("Leadership Coach")), (5, 0, 10_000))
        check("Chief Scientist is not the generic chief",
              (_role_rank("Chief Scientist"), _role_rank("Chief of Staff")), (1, 3))
    finally:
        config.PROSPECT_ROLE_ORDER = kept_order


    print("\nR6 / R7 — the day thresholds")
    r6_rule = [rules_mod.by_id("R6")]

    def r6_run(rows_, **kw):
        got = run(today=MON, rows=rows_, day_rules=r6_rule, **kw)["actions"]
        return [a for a in got if not a.get("notice")], [a.get("notice") for a in got if a.get("notice")]

    r6 = row(li_connected_date="15-09-2026")
    check("connected 6d, LI DM Sent blank -> in", len(r6_run([r6])[0]), 1)
    r6b = row(li_connected_date="20-09-2026")
    check("connected 1d -> not yet, and with nobody else the all-clear notice",
          (len(r6_run([r6b])[0]), r6_run([r6b])[1]), (0, [wording.LI_NO_DM_ALL_CLEAR]))
    check("connected 3d -> in on day 3",
          (len(r6_run([row(li_connected_date="19-09-2026")])[0]),
           len(r6_run([row(li_connected_date="18-09-2026")])[0])), (0, 1))
    check("NO connected date -> in straight away", len(r6_run([row()])[0]), 1)
    for said in ("TRUE", "Sent", "sent", "Replied", "Received", "in progress"):
        check(f"LI DM Sent says {said!r} -> out",
              len(r6_run([row(li_connected_date="15-09-2026", li_dm_sent=said)])[0]), 0)
    check("an LI DM Date with LI DM Sent blank does NOT take a person out (the cell decides)",
          len(r6_run([row(li_connected_date="15-09-2026", li_dm_date="16-09-2026")])[0]), 1)
    check("it reads the whole tab: a row that is not 'active' is still picked",
          [a["poc"] for a in run(today=MON, rows=[], prospect_rows=[row(name="Hidden")],
                                 day_rules=r6_rule)["actions"]], ["Hidden"])
    order6 = [row(_row=2, name="NoDate2"), row(_row=3, name="New", li_connected_date="10-09-2026"),
              row(_row=4, name="Old", li_connected_date="01-09-2026"), row(_row=5, name="NoDate5")]
    check("the order: a connected date first, oldest first; then no date, in sheet order",
          [a["poc"] for a in sorted(r6_run(order6)[0], key=lambda a: a["pick_order"])],
          ["Old", "New", "NoDate2", "NoDate5"])
    check("an empty tab is not 'all clear': nothing at all",
          run(today=MON, rows=[], prospect_rows=[], day_rules=r6_rule)["actions"], [])
    check("R6's line never prints the email on file",
          "@" in r6_run([row(li_connected_date="15-09-2026", email="a@b.co")])[0][0]["text"], False)
    check("R7 is back on Mondays in the shipped file",
          ("R7" in [r.id for r in rules_mod.for_day(MON)],
           "R7" in [r.id for r in rules_mod.for_day(TUE)]), (True, False))
    # R7 IS THE ESCALATION BEHIND R13 (rebuilt 13 Oct 2026). MON is 21 Sep 2026,
    # so a DM on 31 Aug is 21 days old and one on 1 Sep is 20.
    config.DM_NO_MEETING_DAYS, config.DM_NO_MEETING_BOOKED_GAP_DAYS = 21, 7
    config.DM_NO_MEETING_MAX_CONTACTS = 3
    config.DM_NO_MEETING_DM_SENT_MARKERS = ["sent"]
    config.DM_NO_MEETING_DM_REPLIED_MARKERS = ["replied"]
    config.DM_NO_MEETING_STATUS_OPEN = ["", "tbd"]
    r7_rule = [rules_mod.by_id("R7")]

    def r7_run(rows, state=None, **kw):
        got = run(today=MON, rows=[], prospect_rows=rows, day_rules=kw.pop("day_rules", r7_rule),
                  dm_no_meeting_state={} if state is None else state, **kw)["actions"]
        return sorted(got, key=lambda a: a["pick_order"])      # the queue is ranked; the post is not

    def dm(**kw):
        kw.setdefault("li_dm_sent", "Sent")
        kw.setdefault("li_dm_date", "31-08-2026")
        return row(**kw)
    got = r7_run([dm(name="Ann", company="Acme", next_steps="waiting on legal")])
    check("Sent, DM 21d ago, no meeting, blank status -> in, first block",
          [(a["poc"], a["block"]) for a in got], [("Ann", DM_BLOCK_NONE)])
    check("...and nothing about days or the note travels on the line",
          ("day" in got[0]["text"], "legal" in got[0]["text"], "last_note" in got[0]), (False, False, False))
    check("20 days -> not yet", r7_run([dm(li_dm_date="01-09-2026")]), [])
    check("Replied, blank and an unknown value -> out",
          [len(r7_run([dm(li_dm_sent=v)])) for v in ("Replied", "", "in progress")], [0, 0, 0])
    check("no LI DM Date -> out", r7_run([dm(li_dm_date="")]), [])
    check("Meeting Status TBD is the same as blank; Completed is out",
          [len(r7_run([dm(meeting_status=v)])) for v in ("TBD", "tbd", "Completed")], [1, 1, 0])
    check("a meeting 5 days after the DM -> out; 10 days after -> in, second block",
          ([a["block"] for a in r7_run([dm(meeting_date="05-09-2026")])],
           [a["block"] for a in r7_run([dm(meeting_date="10-09-2026", meeting_status="TBD")])]),
          ([], [DM_BLOCK_BOOKED]))
    check("Deal Status On Hold is out of R7, and is still not a stop for any other rule",
          (r7_run([dm(deal_status="On Hold")]), stop_reason(dm(deal_status="On Hold"))), ([], ""))
    check("Won and Unresponsive are out, as everywhere",
          [len(r7_run([dm(**kw)])) for kw in ({"deal_status": "Won"}, {"prospect_status": "Unresponsive"})],
          [0, 0])
    marker = (config.NEXT_STEP_CONNECTED_MARKERS or ["Connected"])[0]
    # CHANGED 13 Oct 2026: R7 no longer hands every Connected row to R13 (that
    # was every DM'd row, so R7 named nobody). Only the dedup stands between them.
    conn = dm(sid_li_added=marker, li_connected_date="01-08-2026")
    check("a Connected person is still Rule 13's by its own test, and R7 now lists them too",
          (covered_by_next_steps(conn), len(r7_run([conn]))), (True, 1))
    seven = [dm(_row=10 + i, name=f"P{i}", company=f"Co{i}", li_dm_date=f"{20 + i:02d}-08-2026")
             for i in range(1, 8)]
    first = r7_run(seven)
    check("three a post: longest since the DM first, then sheet order",
          [a["poc"] for a in first], ["P1", "P2", "P3"])
    named = {a["contact_key"]: {"last_date": "2026-09-14"} for a in first}
    second = r7_run(seven, named)
    check("never named goes ahead of named", [a["poc"] for a in second], ["P4", "P5", "P6"])
    named.update({a["contact_key"]: {"last_date": "2026-09-15"} for a in second})
    check("...and the queue wraps: the last one, then the first two again",
          [a["poc"] for a in r7_run(seven, named)], ["P7", "P1", "P2"])
    today_named = {a["contact_key"]: {"last_date": dl.iso(MON)} for a in first}
    check("once today's post has gone, today's people stay today's people",
          [a["poc"] for a in r7_run(seven, today_named)], ["P1", "P2", "P3"])
    check("an unreadable rotation state names nobody",
          run(today=MON, rows=[], prospect_rows=seven, day_rules=r7_rule)["actions"], [])
    check("an empty tab posts nothing", r7_run([]), [])

    print("\nR8 meeting prep — one column, three touches, two posts")
    def preps(meeting_iso, today, **more):
        r = row(meeting_date=meeting_iso, **more)
        return run(today=today, rows=[], meeting_rows=[r],
                   day_rules=[rules_mod.by_id("R8")])["actions"]
    check("5 days before", [a["touch"] for a in preps("26-09-2026", date(2026, 9, 21))], ["5"])
    check("3 days before", [a["touch"] for a in preps("24-09-2026", date(2026, 9, 21))], ["3"])
    check("4 days out says nothing", preps("25-09-2026", date(2026, 9, 21)), [])
    check("the day itself goes at MEETING_DAYOF_TIME, in its own post",
          [(a["dayof_time"], a["post"]) for a in preps("21-09-2026", date(2026, 9, 21))],
          [(config.MEETING_DAYOF_TIME, "today")])
    check("the earlier touches go at MEETING_PREP_TIME, in the afternoon post",
          [(a["dayof_time"], a["post"]) for a in preps("26-09-2026", date(2026, 9, 21))
           + preps("24-09-2026", date(2026, 9, 21))],
          [(config.MEETING_PREP_TIME, "prep")] * 2)
    check("a past meeting produces nothing", preps("01-09-2026", date(2026, 9, 21)), [])
    check("a Completed meeting with a date to come is still chased (accepted 12 Oct)",
          len(preps("24-09-2026", MON, meeting_status="Completed")), 1)
    check("no activation is needed: only the Meeting Date is read",
          len(run(today=MON, rows=[], meeting_rows=[{"_row": 9, "name": "Zed", "company": "Zeta",
                                                    "meeting_date": "24-09-2026"}],
                  day_rules=[rules_mod.by_id("R8")])["actions"]), 1)
    twice = run(today=MON, rows=[], day_rules=[rules_mod.by_id("R8")],
                meeting_rows=[row(meeting_date="24-09-2026"), dict(row(meeting_date="26-09-2026"), _row=9)])
    check("never dropped by the dedup: one person's two meetings are two items",
          [(a["touch"], a["never_dropped"]) for a in twice["actions"]], [("3", True), ("5", True)])
    check("a record that cannot be read posts nothing",
          run(today=MON, rows=[], meeting_rows=[row(meeting_date="24-09-2026")],
              meeting_state={"unreadable": True},
              day_rules=[rules_mod.by_id("R8")])["actions"], [])

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
        "NEXT_STEP_DM_REPLIED_MARKERS", "NEXT_STEP_DM_SENT_MARKERS",
        "NEXT_STEP_PAUSE_FOR_BOOKED_MEETING",
        "NEXT_STEP_CONNECTED_MARKERS", "NEXT_STEP_TIME")}
    config.NEXT_STEP_FIRST_DAYS = config.NEXT_STEP_AFTER_PREVIOUS_DAYS = 2
    config.NEXT_STEP_AFTER_EMAIL_DAYS = config.NEXT_STEP_CALL_AFTER_DM_DAYS = 7
    config.NEXT_STEP_CALL_EVERY_DAYS, config.NEXT_STEP_CALL_UNTIL_DAYS = 3, 21
    config.NEXT_STEP_DM_REPLIED_MARKERS = ["replied", "responded", "received"]
    config.NEXT_STEP_DM_SENT_MARKERS = ["sent", "received"]
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
        return (f["status"], f["ask"], f["due"])

    check("blank: due 2 days after connecting",
          step(conn()), ("due", ASK_BLANK, date(2026, 9, 17)))
    check("blank: not yet", step(conn(li_connected_date="20-09-2026"))[0], "waiting")
    check("no LI Connected Date: due straight away",
          step(conn(li_connected_date="")), ("due", ASK_BLANK, MON))
    check("research", step(conn(outreach_step="Research the PoC"))[:2],
          ("due", ASK_RESEARCH))
    check("email 1 not sent: connected + 2",
          step(conn(outreach_step="Send email 1")),
          ("due", ASK_EMAIL_OUT, date(2026, 9, 17)))
    check("email 1 sent and dated: the second is due at +7, not yet",
          step(conn(outreach_step="Send email 1", email_1_sent="yes",
                    email_1_date="16.09.2026")),
          ("waiting", ASK_ADVANCE, date(2026, 9, 23)))
    check("email 1 sent and dated: due at +7",
          step(conn(outreach_step="Send email 1", email_1_sent="Yes",
                    email_1_date="14.09.2026"))[:2], ("due", ASK_ADVANCE))
    check("email 1 sent, NO date: the step is done, the second is due now",
          step(conn(outreach_step="Send email 1", email_1_sent="yes")),
          ("due", ASK_ADVANCE, MON))
    check("email 2 with the first email's cells blank: still about email 2, due now",
          step(conn(outreach_step="Send email 2")), ("due", ASK_EMAIL_OUT, MON))
    check("email 2 counts from the 1st email's date",
          step(conn(outreach_step="Send email 2", email_1_sent="yes",
                    email_1_date="20-09-2026")),
          ("waiting", ASK_EMAIL_OUT, date(2026, 9, 22)))
    check("email 3 marked sent: nothing is said",
          step(conn(outreach_step="Send email 3", email_3_sent="yes",
                    email_3_date="01-09-2026"))[0], "silent")
    check("Reach by LI DM, no DM sent: nothing is said (the known gap)",
          step(conn(outreach_step="Reach by LI DM", email_3_sent="yes",
                    email_3_date="18-09-2026"))[0], "silent")
    check("...'Not sent' is not 'sent'",
          step(conn(outreach_step="Reach by LI DM", li_dm_sent="Not sent"))[0], "silent")
    check("DM marked sent, NO date: a call, due straight away",
          step(conn(outreach_step="Reach by LI DM", li_dm_sent="Sent")),
          ("due", ASK_CALL, MON))
    check("Call the PoC is the call stage whatever LI DM Sent says",
          step(conn(outreach_step="Call the PoC")), ("due", ASK_CALL, MON))
    check("a replied DM is the replied ask, never a call",
          step(conn(outreach_step="Reach by LI DM", li_dm_sent="Replied",
                    li_dm_date="01-09-2026"))[:2], ("due", ASK_REPLIED))
    check("'Received' means they answered: the meeting ask, not a call and not 'DM not sent'",
          step(conn(outreach_step="Reach by LI DM", li_dm_sent="Received"))[:2],
          ("due", ASK_REPLIED))
    check("...and it still counts as the DM having gone out",
          (dm_replied(conn(li_dm_sent="Received")), dm_sent(conn(li_dm_sent="Received"))),
          (True, True))
    dm_row = conn(outreach_step="Reach by LI DM", li_dm_sent="Sent",
                  li_dm_date="14-09-2026")                 # a Monday
    check("DM 6 days ago: waiting",
          step(dm_row, today=date(2026, 9, 20))[:2], ("waiting", ASK_CALL))
    check("DM 7 days ago: the first call reminder",
          step(dm_row, today=MON), ("due", ASK_CALL, MON))
    sig13 = step_signature(dm_row)
    called = {"signature": sig13, "calls": 1, "last_call_date": "2026-09-21"}
    check("...not again the next day",
          step(dm_row, today=TUE, entry=called)[0], "waiting")
    check("...again 3 days later",
          step(dm_row, today=THU, entry=called), ("due", ASK_CALL, THU))
    check("changing Q to Call the PoC keeps the signature",
          step_signature(conn(outreach_step="Call the PoC", li_dm_sent="Sent",
                              li_dm_date="14-09-2026")), sig13)
    check("day 21 is still a call",
          step(dm_row, today=date(2026, 10, 5), entry={
              "signature": sig13, "calls": 4, "last_call_date": "2026-10-01"})[:2],
          ("due", ASK_CALL))
    check("day 22: the calls ran out",
          step(dm_row, today=date(2026, 10, 6))[:2], ("due", ASK_UNRESPONSIVE))
    check("an unknown Next Steps value is not guessed at",
          step(conn(outreach_step="ping them"))[0], "unknown_step")

    print("\nR13 next steps — who is in, the six tiers and the rotation")

    def r13_run(rows_, today=MON, state=None, **kw):
        return run(today=today, rows=rows_, day_rules=[r13],
                   next_step_state={} if state is None else state, **kw)

    people = [conn(_row=10 + i, company=f"Co{i}", name=f"P{i}",
                   li_connected_date=f"{15 - i:02d}-09-2026",
                   outreach_step="Send email 1") for i in range(8)]
    out13 = r13_run(people)
    check("five per post", len(out13["actions"]), 5)
    picked = sorted(out13["actions"], key=lambda a: a["pick_order"])
    check("the OLDEST connection first, not the top of the sheet",
          [a["poc"] for a in picked], ["P7", "P6", "P5", "P4", "P3"])
    check("the rest are queued, and counted",
          (out13["reports"]["R13"]["due"], len(out13["reports"]["R13"]["queued"])),
          (8, 3))
    check("every item carries the fixed time",
          {a["dayof_time"] for a in picked}, {"15:00"})
    check("...and is outside the cap", {a["counts_toward_cap"] for a in picked}, {False})
    check("the preview line says what to do and names no cell",
          picked[0]["text"], "P7 (Co7): send the first email")
    state13 = {a["contact_key"]: {"signature": a["signature"], "last_date": "2026-09-21"}
               for a in picked}
    day2 = sorted(r13_run(people, today=TUE, state=state13)["actions"],
                  key=lambda a: a["pick_order"])
    check("the next day THE TIER ROTATES: the three never named, then the oldest of yesterday's",
          [a["poc"] for a in day2], ["P2", "P1", "P0", "P7", "P6"])
    people[6]["outreach_step"] = "Send email 2"
    check("a changed step makes the person new again: with the never-named, oldest first",
          [a["poc"] for a in sorted(r13_run(people, today=TUE, state=state13)["actions"],
                                    key=lambda a: a["pick_order"])],
          ["P6", "P2", "P1", "P0", "P7"])
    people[6]["outreach_step"] = "Send email 1"
    caller = conn(_row=40, company="Zeta", name="Zed", outreach_step="Call the PoC",
                  li_dm_date="14-09-2026")
    replier = conn(_row=41, company="Yota", name="Yan", outreach_step="Reach by LI DM",
                   li_dm_sent="Replied")
    check("somebody who replied goes first, then a call that is due",
          [a["poc"] for a in sorted(r13_run(people + [caller, replier])["actions"],
                                    key=lambda a: a["pick_order"])][:2], ["Yan", "Zed"])
    nodate = conn(_row=5, name="Nod", company="NoDate Co", li_connected_date="",
                  outreach_step="Send email 1")
    with_nodate = sorted(r13_run(people[:3] + [nodate])["actions"],
                         key=lambda a: a["pick_order"])
    check("Connected with NO date is in, and last in its group despite its sheet row",
          [a["poc"] for a in with_nodate], ["P2", "P1", "P0", "Nod"])
    check("no state (unreadable) -> nobody is named",
          (len(run(today=MON, rows=people, day_rules=[r13])["actions"]),
           run(today=MON, rows=people, day_rules=[r13])["reports"]["R13"]["state"]),
          (0, "unreadable"))
    check("nobody due -> no items",
          r13_run([conn(li_connected_date="20-09-2026")])["actions"], [])
    after_post = r13_run(people, state=state13)
    check("after today's post, the same five and nobody new",
          sorted(a["poc"] for a in after_post["actions"]),
          ["P3", "P4", "P5", "P6", "P7"])
    check("...and the preview says the post has gone",
          "today's post has gone" in preview_text(after_post), True)

    one = conn(_row=60, company="Solo", name="Sol", outreach_step="Send email 1")
    key1, sig1 = activation.row_key(one), step_signature(one)
    day = date(2026, 10, 12)
    check("named 25 days ago with nothing moved: still named, normally. Nobody is dropped",
          [a["ask"] for a in r13_run([one], today=day, state={key1: {
              "signature": sig1, "last_date": dl.iso(day - timedelta(days=25))}})["actions"]],
          ["email_out"])
    check("a row the old drop had closed is named again: nothing closes a person",
          [a["ask"] for a in r13_run([one], today=day, state={key1: {
              "signature": sig1, "last_date": dl.iso(day - timedelta(days=30)), "closed": True,
              "closed_date": "2026-09-20"}})["actions"]], ["email_out"])
    gone = r13_run([dm_row], today=date(2026, 10, 6))["actions"]
    again = r13_run([dm_row], today=date(2026, 10, 9), state={
        activation.row_key(dm_row): {"signature": sig13, "last_date": "2026-10-06",
                                     "last_ask": "unresponsive"}})["actions"]
    check("the unresponsive reminder comes round again until the row is marked",
          ([a["ask"] for a in gone], [a["ask"] for a in again]),
          (["unresponsive"], ["unresponsive"]))
    check("the tiers are six, in this order",
          (TIER_REPLIED, TIER_CALL, TIER_ADVANCE, TIER_EMAIL, TIER_RESEARCH, TIER_UNRESPONSIVE),
          (1, 2, 3, 4, 5, 6))
    check("Prospect Status Unresponsive stops it",
          r13_run([conn(prospect_status="Unresponsive")])["actions"], [])
    check("a meeting booked ahead pauses it",
          len(r13_run([conn(meeting_date="25-09-2026")])["reports"]["R13"]["paused"]), 1)
    check("a past meeting, not completed, does not",
          len(r13_run([conn(meeting_date="10-09-2026")])["actions"]), 1)
    check("a completed meeting ends it",
          r13_run([conn(meeting_date="10-09-2026", meeting_status="Completed")])["actions"],
          [])
    old_layout = {k: v for k, v in conn().items()
                  if k not in NEXT_STEP_REQUIRED_ROLES}
    no_col = r13_run([old_layout])
    check("no Next Steps column on the tab: nobody is told a cell is blank",
          (no_col["actions"], no_col["reports"]["R13"]["state"]),
          ([], "no_step_column"))
    check("the preview says who was left out",
          "who is not in today's post" in preview_text(r13_run(people + [nodate])), True)
    check("...and still names the row with no date",
          "Nod (row 5" in preview_text(r13_run(people + [nodate])), True)
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
    # REBUILT 13 Oct 2026: whole-word stages, highest first, no cap, no fetch.
    check("the notice names whichever column is empty, and has lost its second sentence",
          (got[0]["notice"], "Fill them in" in CLOSURE_EMPTY_NOTICE),
          ("No closure support this week \u2014 Closure Prob% has empty values in the GTM sheet.", False))
    check("'No deal' is not a deal; 'Demoted' is not a demo; 'Quote sent' is a quote",
          [bool(closure_stage(v)) for v in ("No deal", "Deal", "Demoted", "Quote sent", "Not a demo")],
          [False, True, False, True, False])
    many = [row(_row=10 + i, company=f"Co{i}", name=f"D{i}", prospect_status="Demo",
                closure_prob=f"{60 + (i * 7) % 30}%") for i in range(12)]
    got = run(today=MON, rows=many, day_rules=[rules_mod.by_id("R10")])["actions"]
    check("twelve deals are twelve items, highest percentage first, ties in sheet order",
          (len(got), [a["closure_pct"] for a in sorted(got, key=lambda a: a["pick_order"])]
           == sorted((a["closure_pct"] for a in got), reverse=True),
           [a["web_pending"] for a in got] == [False] * 12), (12, True, True))
    check("a closure stage with no percentage is left out, with another deal present",
          [a["poc"] for a in run(today=MON, day_rules=[rules_mod.by_id("R10")], rows=[
              row(_row=2, name="NoPct", prospect_status="Demo"),
              row(_row=3, company="B", name="HasPct", prospect_status="Quote", closure_prob="70%")])["actions"]],
          ["HasPct"])
    check("a stored story is matched on the full company name, the most important one",
          (closure_news_pick("Nudge Security", [
              {"sheet_ref": "Nudge \u2014 on Master Pipeline", "title": "a", "url": "https://x.example/a",
               "importance": 5},
              {"sheet_ref": "Nudge Security \u2014 on Outreach PoCs", "title": "b", "url": "https://x.example/b",
               "importance": 2},
              {"sheet_ref": "nudge security ltd", "title": "c", "url": "https://x.example/c", "importance": 4},
              {"sheet_ref": "Nudge Security", "title": "no scheme", "url": "x.example/d", "importance": 5},
              {"sheet_ref": "Jo Roe, Nudge Security \u2014 on Outreach PoCs", "title": "person",
               "url": "https://x.example/e", "importance": 5}])["headline"],
           closure_news_pick("Nudge Security", [])), ("c", None))

    print("\nR7 — seniority is not part of its order any more")
    got = run(today=MON, rows=[], dm_no_meeting_state={}, day_rules=[rules_mod.by_id("R7")],
              prospect_rows=[row(li_dm_sent="Sent", li_dm_date="31-08-2026",
                                 designation="Co-Founder")])["actions"]
    check("no role rank travels with the item", "role_rank" in got[0], False)

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
    # REBUILT 14 Oct 2026: three lines in the whole post, the sheet first, by
    # urgency; no "no registration deadline" line and no "date unclear" line.
    r3 = rules_mod.by_id("R3")
    got = run(today=WED, events=tab, day_rules=[r3])["actions"]
    lines = {a["company"]: a for a in got if a.get("event_kind") != EVENT_CARRIER}
    carrier = next(a for a in got if a.get("event_kind") == EVENT_CARRIER)
    check("three lines at most: the two closing registrations, then the registered event",
          [(a["company"], a["event_block"]) for a in sorted(lines.values(), key=lambda a: a["pick_order"])],
          [("Next Tuesday Forum", "B"), ("Far But Closing", "B"), ("Registered Summit", "A")])
    check("...the sheet used all three places, so there is no room for a suggestion",
          carrier["event_room"], 0)
    check("registered, within the window: the name, then the date",
          lines["Registered Summit"]["event_line"].startswith("Registered Summit \u2014 "), True)
    check("not registered, deadline ahead: the line shows both dates",
          ", closes " in lines["Next Tuesday Forum"]["event_line"], True)
    check("no deadline on the sheet, and an unreadable date: neither has a line any more",
          [n for n in ("No Deadline Meetup", "Mystery Day") if n in lines], [])
    check("...and nothing says 'date unclear' or 'no registration deadline'",
          [a["event_line"] for a in lines.values()
           if "unclear" in a["event_line"] or "no registration deadline" in a["event_line"]], [])
    check("'not available' and 'TBD' are the same as blank",
          [event_deadline_unknown(v) for v in ("", "not available", "TBD", "soon", "20-10-2026")],
          [True, True, True, True, False])
    check("the carrier is always there with rows, and is the only item that wants research",
          [(a["event_kind"], a["web_pending"]) for a in got if a["web_pending"]],
          [(EVENT_CARRIER, True)])
    check("an empty tab posts nothing at all, not even the carrier",
          run(today=WED, events=[], day_rules=[r3])["actions"], [])
    one_row = run(today=WED, events=[ev(2, "Registered Summit", d(10), registered="Yes")],
                  day_rules=[r3])["actions"]
    check("one line from the sheet leaves room for two suggestions",
          [a["event_room"] for a in one_row if a["event_kind"] == EVENT_CARRIER], [2])
    check("it runs on the Wednesday after too (no alternate weeks)",
          bool(run(today=WED + timedelta(days=7), events=tab, day_rules=None)
               ["by_rule"].get("R3")), True)
    under_way = run(today=WED, day_rules=[r3], events=[
        ev(2, "Running Now", f"{(WED - timedelta(days=1)).day}-{(WED + timedelta(days=1)).day} "
                             f"{WED.strftime('%B %Y')}", registered="Yes"),
        ev(3, "Ended", d(-1), registered="Yes")])["actions"]
    check("started and not finished is kept and says when it started; ended is gone",
          [a["event_line"].endswith(", started Tuesday") for a in under_way
           if a["event_kind"] != EVENT_CARRIER], [True])
    check("the reminder is the next Monday, or tomorrow when something falls before it",
          (remind_again_on(WED, WED + timedelta(days=10)), remind_again_on(WED, WED + timedelta(days=2))),
          ((WED + timedelta(days=5), "on Monday"), (WED + timedelta(days=1), "tomorrow")))

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
