"""THE TWELVE RULES — what the bot says on its own initiative, computed.

WHAT THIS REPLACES. The plan-v2 state machine that lived here produced exactly
one action per ACTIVE row, chosen by the first of twelve ordered triggers to
match. That shape is gone. It was built around a single question — "what does
this row need next?" — and seven of the twelve rules the team actually runs are
not about a row at all: two are about the news, one about a checklist, one about
a package list, one about a pipeline tab, one about an events tab.

WHAT REPLACED IT. `bot_rules.yaml` holds the twelve rules, and this module holds
one evaluator per rule. The file decides WHICH rules exist, WHEN each runs, HOW
MUCH one post may carry, WHERE it goes and whether it counts against the cap.
This module decides only HOW a rule works out that something is due.

    THE FILE IS THE SCHEDULE; THE CODE IS THE ARITHMETIC. Changing a weekday or
    a cap is an edit and a restart. That split is the whole point of the rewrite.

THE TWELVE, with the trigger name the file uses:

     R1  ai_news                weekdays            the news sweep
     R2  news_company_screen    Tue, Fri            news companies vs the pipeline
     R3  events                 alt. Wed            register / attend, until it passes
     R4  deliverables           Mon                 P1, due within 3 days or passed
     R5  prospects              Tue, Thu            first contact FALSE or blank
     R6  li_no_dm               Tue, Fri            connected > 3d, no DM
     R7  dm_no_meeting          Mon                 DM > 7d, no meeting
     R8  meeting_prep           anchored            T-5, T-3, and 10:00 on the day
     R9  meeting_followup       anchored            done + 3d, no next steps, laddered
    R10  closure_support        Mon                 deal/demo/quote AND closure > 50
    R11  new_pipeline_company   weekdays            1 working day after it appears
    R12  sales_packages         Thu                 Ready? is No or blank

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

WEB-DEPENDENT RULES STILL PRODUCE THEIR ITEMS. R1, R2, R3, R6, R8, R10 and R11
need research this bot cannot do yet. Each produces its item carrying
`web_pending=True` and the placeholder text, so the schedule is real and visible
before the research layer lands. Nothing is silently skipped waiting for it.

THIS MODULE IS PURE, AND THAT IS LOAD-BEARING. Tabs and dicts in, dicts out. It
reads no sheet, writes no database row and sends nothing — the snoozes, the
scheduled reminders, the new-company snapshot, the repeat counts and the R9
ladder state are all passed IN, computed by the caller. There is no
`guardrails.send` here and there must never be one.

    R11's SNAPSHOT IS THE REASON THAT MATTERS. Detecting a new company in the
    Master Pipeline needs yesterday's names stored somewhere, and storing them
    is a write. The caller takes the snapshot and passes `new_companies`; the
    engine only reads it. Had the engine taken the snapshot itself, every
    `cadence preview` would have advanced the state it was supposed to be
    previewing.
"""
import logging
from datetime import date, datetime, time, timedelta
from typing import Optional

import activation
import config
import deadlines as dl
import gtm_sheet
import focus as focus_mod
import rules as rules_mod

log = logging.getLogger(__name__)

# -- the twelve rule ids ------------------------------------------------------
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
    A row that is finished produces nothing, from any of the twelve rules, ever.

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

    Next Steps first, because that is where a person records what happens next;
    the sheet has no dated note column, so "most recent" is really "the most
    specific thing anybody wrote".
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


def _anchor_weeks_ok(today: date, anchor_iso: str, *, every: int = 2) -> tuple:
    """Is `today` on the every-Nth-week cadence from the anchor? (ok, why).

    R3 runs every OTHER Wednesday, anchored to EVENTS_ANCHOR_DATE. An anchor
    date rather than "odd ISO weeks" because the team picked a date, and an
    ISO-week parity rule silently flips its meaning in any year with 53 weeks.

    A date BEFORE the anchor is not on the cadence: the rule had not started.
    """
    anchor = dl.parse_date(anchor_iso)
    if anchor is None:
        return True, f"EVENTS_ANCHOR_DATE={anchor_iso!r} is unreadable, so every run day counts"
    if today < anchor:
        return False, f"the rule starts on {dl.format_date(anchor)}"
    weeks = (today - anchor).days // 7
    if (today - anchor).days % (7 * every) < 7 and weeks % every == 0:
        return True, f"week {weeks} from the anchor {dl.format_date(anchor)}"
    return False, (
        f"this is week {weeks} from the anchor {dl.format_date(anchor)}; "
        f"the rule runs every {every} weeks"
    )


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


# -- the twelve evaluators ----------------------------------------------------
# Each takes (rule, ctx) and returns a list of items. NONE of them sends, writes
# or reads a sheet: everything they need is in `ctx`, assembled by the caller.
#
# An evaluator that raises is caught by `run()`, logged with its rule id, and
# contributes nothing — one broken rule must not take the other eleven down.


def _r_ai_news(rule, ctx) -> list:
    """R1 — the daily news sweep. WEB-DEPENDENT.

    Funding rounds, AI/ML and leadership hires, papers by our PoCs, PoCs
    speaking at events or changing companies, job posts for evals / annotation /
    model-training roles, competitor news, major global AI news, regulation.

    ONE ITEM, not one per story. The research layer will expand it into the
    stories it found; until then it is a single placeholder so the schedule is
    visible without pretending to have read anything.
    """
    today = ctx["today"]
    return [_item(
        rule=rule, trigger=R_AI_NEWS, today=today, due=today,
        why="R1 runs every weekday",
        text="AI news: funding, hires, papers by our PoCs, competitor and regulation news",
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


def _r_events(rule, ctx) -> list:
    """R3 — register for, or attend, an AI event. Every other Wednesday.

    Remind until registration closes or the event happens. The REGISTRATION
    DEADLINE is used when it is known, otherwise the event date — a conference
    in November whose registration shut in September is not a November problem.

    Events already past are skipped. Events whose date the sheet cannot read are
    NOT skipped: they are carried with the reason, because "I cannot read this
    date" is a thing somebody should fix and silence would hide it.
    """
    today = ctx["today"]
    ok, why_not = _anchor_weeks_ok(today, config.EVENTS_ANCHOR_DATE, every=2)
    if not ok:
        return []
    out = []
    for row in ctx.get("events") or ():
        name = _text(row, "event")
        if not name:
            continue
        when = gtm_sheet.parse_event_date(row.get("event_date"))
        closes = gtm_sheet.parse_event_date(row.get("registration_deadline"))
        registered = gtm_sheet.parse_flag(row.get("registered"))
        if registered is True:
            continue
        # The deadline that actually bites: registration if we know it, else the
        # event itself.
        gate = closes["start"] if closes["known"] else when["start"]
        if gate is not None and gate < today:
            continue
        if when["known"] and when["start"] and when["start"] < today:
            continue
        where = _text(row, "location")
        bits = [name] + ([where] if where else [])
        if when["known"]:
            bits.append(f"on {dl.format_date(when['start'])}"
                        + ("" if when["start"] == when["end"]
                           else f"-{dl.format_date(when['end'])}"))
        else:
            bits.append(f"(date: {when['reason']})")
        why = f"R3 runs every other Wednesday — {why_not}"
        if closes["known"]:
            why += f"; registration closes {dl.format_date(closes['start'])}"
        out.append(_item(
            rule=rule, trigger=R_EVENTS, today=today,
            due=gate or today, why=why,
            text="Register or attend: " + " · ".join(bits),
            company=name, sheet_row=row.get("_row"), web_pending=True,
        ))
    return out


def _r_deliverables(rule, ctx) -> list:
    """R4 — P1 deliverables due within DELIVERABLE_NEAR_DAYS, or already past.

    THE OWNER IS THE Functional Dependency CELL, and blank means
    DELIVERABLE_DEFAULT_OWNER (Vaishnavi). Blank is common and it is not the
    same as unowned — a deliverable nobody is addressed about is a deliverable
    nobody chases.

    STATUS BLANK OR NOT DONE. Only the values in DELIVERABLE_DONE_MARKERS count
    as finished; everything else, blank included, is still open. The safe
    direction: chasing a finished item costs one correction, and skipping an
    unfinished one costs the deadline.

    DEADLINES CARRY NO YEAR on this tab ("25-Sep"), so they go through
    `gtm_sheet.parse_bare_deadline`, which reads them as the NEXT occurrence.
    """
    today = ctx["today"]
    near = max(0, int(config.DELIVERABLE_NEAR_DAYS))
    out = []
    for row in ctx.get("deliverables") or ():
        item_name = _text(row, "action_item")
        if not item_name:
            continue
        if not _matches_any(row.get("priority"), config.DELIVERABLE_P1_MARKERS):
            continue
        if _matches_any(row.get("status"), config.DELIVERABLE_DONE_MARKERS):
            continue
        raw = row.get("deadline")
        due = gtm_sheet.parse_bare_deadline(raw) or gtm_sheet.sheet_date(raw)
        if due is None:
            continue
        days = (due - today).days
        if days > near:
            continue
        owner = _text(row, "dependency") or config.DELIVERABLE_DEFAULT_OWNER
        status = _text(row, "status") or "(blank)"
        when = ("overdue by %d day(s)" % -days if days < 0
                else "due today" if days == 0 else "due in %d day(s)" % days)
        out.append(_item(
            rule=rule, trigger=R_DELIVERABLES, today=today, due=due,
            why=f"R4 (Mondays): P1, status {status}, {when}",
            text=f"{item_name} — {when}",
            owner=owner, company=item_name, sheet_row=row.get("_row"),
            extra={"deliverable": item_name, "status": status,
                   "dependency": _text(row, "dependency"),
                   "link": _text(row, "link")},
        ))
    return out


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
         first-contact date is recorded (`first_contact_done`).
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
    for row in ctx.get("rows") or ():
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
                       "focus_note": focus_note,
                       "focus": (active_focus or {}).get("value", "")},
            ))
    return out


def _r_li_no_dm(rule, ctx) -> list:
    """R6 — connected on LinkedIn more than LI_NO_DM_DAYS ago, still no DM.

    SAYS WHETHER AN EMAIL IS ON FILE. When one is, the item carries it and the
    ask is simply the DM. When none is, the research layer will look for a
    verified public address and say where it found it, or that it found none —
    hence WEB-DEPENDENT, but only for the half of the rule that needs it.
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
        text = (f"{_describe(row)} — connected {elapsed} day(s) ago, no DM logged. "
                + (f"Email on file: {email}" if email else "No email on file"))
        out.append(_item(
            rule=rule, trigger=R_LI_NO_DM, today=today,
            due=until or due,
            why=(f"R6 (Tue/Fri): connected {dl.format_date(connected)}, "
                 f"{elapsed}d ago, more than LI_NO_DM_DAYS ({days})"),
            text=text, company=_text(row, "company"), poc=_text(row, "name"),
            designation=_text(row, "designation"), sheet_row=row.get("_row"),
            row_key=_contact_key(row), contact_key=_contact_key(row),
            web_pending=not email,
            extra={"email_on_file": email, "connected_days": elapsed},
        ))
    return out


def _r_dm_no_meeting(rule, ctx) -> list:
    """R7 — DM sent more than DM_NO_MEETING_DAYS ago, still no meeting. Mondays.

    SHOWS DAYS SINCE THE DM AND THE LAST NOTE LOGGED, because those two are what
    a person needs to decide whether to chase again or leave it. "No reply after
    9 days, last note: waiting on their legal" is a sentence somebody can act
    on; "follow up with Acme" is not.
    """
    today = ctx["today"]
    days = max(0, int(config.DM_NO_MEETING_DAYS))
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
            extra={"days_since_dm": elapsed, "last_note": note},
        ))
    return out


def _r_meeting_prep(rule, ctx) -> list:
    """R8 — prep touches at T-5, T-3 and MEETING_DAYOF_TIME on the day.

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
                   "dayof_time": config.MEETING_DAYOF_TIME if days_out == 0 else ""},
        ))
    return out


def _r_meeting_followup(rule, ctx) -> list:
    """R9 — meeting completed, no next steps. Then every 3 days, up a ladder.

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
                   "meeting_date": dl.iso(met)},
        ))
    return out


def _r_closure_support(rule, ctx) -> list:
    """R10 — deal / demo / quote AND closure strictly above CLOSURE_SUPPORT_MIN.

    EXACTLY 50 IS EXCLUDED. "Above 50" is the rule as written, and a boundary a
    bot decides for itself is a boundary nobody agreed to. `>` not `>=`, and the
    reason line says so, so nobody has to read this file to find out.

    Asks what is needed for the next stage and shares relevant news — the news
    half is WEB-DEPENDENT.
    """
    today = ctx["today"]
    floor = int(config.CLOSURE_SUPPORT_MIN)
    out = []
    for row in ctx.get("rows") or ():
        ok, _reason, _until = row_gate(row, today=today, snoozes=ctx.get("snoozes") or {})
        if not ok:
            continue
        stage = _matches_any(row.get("prospect_status"), config.CLOSURE_SUPPORT_STAGES)
        if not stage:
            continue
        pct = closure_percent(row)
        if pct is None or pct <= floor:
            continue
        out.append(_item(
            rule=rule, trigger=R_CLOSURE_SUPPORT, today=today, due=today,
            why=(f"R10 (Mondays): prospect status {_text(row, 'prospect_status')!r} "
                 f"({stage}) and closure {pct}%, above CLOSURE_SUPPORT_MIN ({floor}%) "
                 f"— exactly {floor}% would not qualify"),
            text=(f"{_describe(row)} — {pct}% at {_text(row, 'prospect_status')}. "
                  "What is needed to move it to the next stage?"),
            company=_text(row, "company"), poc=_text(row, "name"),
            designation=_text(row, "designation"), sheet_row=row.get("_row"),
            row_key=_contact_key(row), contact_key=_contact_key(row),
            web_pending=True,
            extra={"closure_pct": pct, "stage": _text(row, "prospect_status")},
        ))
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

    Fills in funding, location and industry and suggests PoCs — WEB-DEPENDENT.
    Permission is asked before anything is added to Outreach PoCs.
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
            why=(f"R11: {name} first appeared in the Master Pipeline on "
                 f"{dl.format_date(first_seen)}, {wait} working day(s) ago"),
            text=(f"{name} is new in the Master Pipeline. I can fill in funding, location "
                  "and industry and suggest PoCs with designation, LinkedIn URL and paper "
                  "link — shall I?"),
            company=name, web_pending=True,
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


def _scheduled_reminders(ctx) -> list:
    """The one lane that is NOT a rule: one-offs somebody asked for by name.

    Kept out of bot_rules.yaml deliberately — it has no weekday, no cap and no
    schedule, because it runs when a person said it should. It is also the only
    due date in this module that is never weekend-shifted.
    """
    today = ctx["today"]
    scheduled = ctx.get("scheduled") or {}
    out = []
    for row in ctx.get("rows") or ():
        ok, _reason, _until = row_gate(row, today=today, snoozes=ctx.get("snoozes") or {})
        if not ok:
            continue
        key = _contact_key(row)
        entries = scheduled.get(key) or []
        for entry in entries if isinstance(entries, list) else [entries]:
            when = dl.parse_date(str((entry or {}).get("due_date") or ""))
            if when is None or when > today:
                continue
            about = str((entry or {}).get("about") or "").strip()
            out.append(_item(
                rule=None, trigger=SCHEDULED_REMINDER, today=today, due=when,
                why=f"you asked me to come back to this on {dl.format_date(when)}",
                text=f"{_describe(row)} — {about or 'the reminder you asked for'}",
                company=_text(row, "company"), poc=_text(row, "name"),
                designation=_text(row, "designation"), sheet_row=row.get("_row"),
                row_key=key, contact_key=key,
                destination=rules_mod.DEST_CHANNEL,
            ))
    return out


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
) -> dict:
    """THE QUEUE. Everything the twelve rules make due today, deduped and ranked.

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
         "rows": int, "inactive": int, "today": date}

    IT SENDS NOTHING AND WRITES NOTHING. Everything it needs was passed in.
    """
    today = today or dl.today_ist()
    rows = list(rows or [])
    ctx = {
        "today": today, "rows": rows,
        "snoozes": snoozes or {}, "scheduled": scheduled or {},
        "deliverables": list(deliverables or []),
        "packages": list(packages or []),
        "events": list(events or []),
        "pipeline_companies": list(pipeline_companies or []),
        "new_companies": list(new_companies or []),
        "prospect_repeats": prospect_repeats or {},
        "week_companies": list(week_companies or []),
        "meeting_followups": meeting_followups or {},
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

    # The explicit lane, always. It is not a rule and has no schedule.
    explicit = _scheduled_reminders(ctx)
    produced.extend(explicit)
    if explicit:
        rules_run.append({"id": "—", "name": "Reminders you asked for", "ran": True,
                          "why": "one-offs somebody asked for by name", "items": len(explicit)})

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
                # is not repeated here. One marker, one place, greppable in one
                # pass when the research layer lands.
                overdue = (f" · {item['overdue_days']}d overdue"
                           if item.get("overdue_days") else "")
                owner = f" → {item['owner']}" if item.get("owner") else ""
                lines.append(f"  • {item.get('text', '')}{overdue}{owner}")
                lines.append(f"    _why: {item.get('why', '')}_")
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
            f"_{pending} item(s) are marked **{rules_mod.WEB_PENDING}** — those rules "
            "need web research this bot cannot do yet, so the schedule is real but the "
            "researched half of each line is not there. They are shown rather than "
            "skipped so a configured rule never looks like a quiet week._"
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
    """`python -m nextaction` — the twelve rules, on rows built here."""
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
    r7 = row(li_dm_date="10-09-2026", next_steps="waiting on legal")
    got = run(today=MON, rows=[r7], day_rules=[rules_mod.by_id("R7")])["actions"]
    check("DM 11d ago, no meeting -> due", len(got), 1)
    check("...shows days since the DM", "11 day(s) since the DM" in got[0]["text"], True)
    check("...and the last note", "waiting on legal" in got[0]["text"], True)

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

    print("\nR10 closure support — exactly 50 is excluded")
    for pct, want in (("51%", 1), ("50%", 0), ("49%", 0), ("0.6", 1), ("", 0)):
        r = row(prospect_status="Demo", closure_prob=pct)
        check(f"closure {pct!r}",
              len(run(today=MON, rows=[r], day_rules=[rules_mod.by_id("R10")])["actions"]),
              want)
    check("wrong stage -> nothing",
          len(run(today=MON, rows=[row(prospect_status="Lead", closure_prob="80%")],
                  day_rules=[rules_mod.by_id("R10")])["actions"]), 0)

    print("\nR3 events — every other Wednesday from the anchor")
    ok, _ = _anchor_weeks_ok(date(2026, 9, 23), "2026-09-23")
    check("the anchor Wednesday runs", ok, True)
    check("the next Wednesday does not", _anchor_weeks_ok(date(2026, 9, 30), "2026-09-23")[0], False)
    check("the one after does", _anchor_weeks_ok(date(2026, 10, 7), "2026-09-23")[0], True)
    check("before the anchor, nothing", _anchor_weeks_ok(date(2026, 9, 16), "2026-09-23")[0], False)

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

    print("\nR4 deliverables — P1, not done, near or passed")
    dl_rows = [
        {"_row": 2, "_extra": {}, "action_item": "MSA", "priority": "P1",
         "status": "", "deadline": "22-Sep", "dependency": "Sid"},
        {"_row": 3, "_extra": {}, "action_item": "NDA", "priority": "P1",
         "status": "Done", "deadline": "22-Sep", "dependency": ""},
        {"_row": 4, "_extra": {}, "action_item": "Website", "priority": "P2",
         "status": "", "deadline": "22-Sep", "dependency": ""},
        {"_row": 5, "_extra": {}, "action_item": "Dashboard", "priority": "P1",
         "status": "", "deadline": "31-Dec", "dependency": ""},
    ]
    got = run(today=MON, rows=[], deliverables=dl_rows,
              day_rules=[rules_mod.by_id("R4")])["actions"]
    check("only the near P1 that is not done", [a["deliverable"] for a in got], ["MSA"])
    check("owner comes from Functional Dependency", got[0]["owner"], "Sid")
    dl_rows[0]["dependency"] = ""
    got = run(today=MON, rows=[], deliverables=dl_rows,
              day_rules=[rules_mod.by_id("R4")])["actions"]
    check("blank dependency -> the default owner",
          got[0]["owner"], config.DELIVERABLE_DEFAULT_OWNER)

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
